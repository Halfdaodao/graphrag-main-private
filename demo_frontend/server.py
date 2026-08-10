"""Small browser UI for exercising the local GraphRAG project."""

from __future__ import annotations

import json
import base64
import hashlib
import os
import re
import shutil
import subprocess
import sys
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import urlparse


ROOT = Path(__file__).resolve().parents[1]
STATIC = Path(__file__).resolve().parent
MODULE3_ROOT = ROOT / "module3_workspace"
EVIDENCE_DIR = MODULE3_ROOT / "evidence"
WIKI_DIR = MODULE3_ROOT / "wiki"
TEST_CASES_ROOT = MODULE3_ROOT / "test_cases"
GRAPH_INPUT_DIR = MODULE3_ROOT / "graph_input"
MANIFEST_PATH = MODULE3_ROOT / "manifest.json"
NEO4J_URI_DEFAULT = "bolt://127.0.0.1:7687"
NEO4J_USER_DEFAULT = "neo4j"
NEO4J_DATABASE_DEFAULT = "neo4j"
ALLOWED_METHODS = {"global", "local", "basic", "drift"}
SECRET_PATTERN = re.compile(r"\b(?:sk|sk-proj)-[A-Za-z0-9_-]+\b")


def _load_local_env() -> None:
    env_file = ROOT / ".env"
    if not env_file.exists():
        return
    for line in env_file.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, value = line.split("=", 1)
        os.environ.setdefault(key.strip(), value.strip().strip('"'))


_load_local_env()


def run_cli(args: list[str], timeout: int = 1800) -> tuple[int, str]:
    # Use the project's interpreter directly. In a Windows background process,
    # invoking ``uv run`` can inherit workspace arguments from the shell and
    # make the GraphRAG CLI see unrelated directory names as query arguments.
    python = ROOT / ".venv" / "Scripts" / "python.exe"
    command = [str(python), "-m", "graphrag", *args]
    completed = subprocess.run(
        command,
        cwd=ROOT,
        capture_output=True,
        text=True,
        timeout=timeout,
        encoding="utf-8",
        errors="replace",
    )
    output = (completed.stdout + "\n" + completed.stderr).strip()
    output = SECRET_PATTERN.sub("sk-***REDACTED***", output)
    return completed.returncode, output


def _compact_index_output(output: str) -> str:
    """Keep the browser log useful without dumping GraphRAG dataframes."""
    workflows: list[str] = []
    warnings: list[str] = []
    for raw_line in output.splitlines():
        line = raw_line.strip()
        if line.startswith("Starting workflow:"):
            name = line.split(":", 1)[1].strip()
            if name not in workflows:
                workflows.append(name)
        elif line.startswith("Workflow complete:"):
            name = line.split(":", 1)[1].strip()
            if name not in workflows:
                workflows.append(name)
        elif "WARNING" in line or "ERROR" in line or "Traceback" in line:
            warnings.append(line)
        elif "Pipeline complete" in line:
            workflows.append("Pipeline complete")
    lines = ["GraphRAG 索引已启动。"]
    if workflows:
        lines.append("完成阶段：" + " → ".join(workflows))
    if warnings:
        lines.append("注意：")
        lines.extend(warnings[-5:])
    if "Pipeline complete" not in workflows:
        lines.append("索引尚未确认完成，请查看服务端日志。")
    return "\n".join(lines)


def _ensure_module3_dirs() -> None:
    for directory in (EVIDENCE_DIR, WIKI_DIR, GRAPH_INPUT_DIR, TEST_CASES_ROOT / "module1", TEST_CASES_ROOT / "module2"):
        directory.mkdir(parents=True, exist_ok=True)


def _safe_slug(value: str, fallback: str = "page") -> str:
    slug = re.sub(r"[^\w\u4e00-\u9fff.-]+", "-", value, flags=re.UNICODE).strip("-.")
    return (slug[:80] or fallback).lower()


def _read_frontmatter(text: str) -> tuple[dict, str]:
    if not text.startswith("---"):
        return {}, text
    parts = text.split("---", 2)
    if len(parts) != 3:
        return {}, text
    metadata: dict = {}
    for line in parts[1].splitlines():
        match = re.match(r"^([A-Za-z][\w-]*):\s*(.*)$", line.strip())
        if not match:
            continue
        key, value = match.groups()
        value = value.strip()
        if value.startswith("[") and value.endswith("]"):
            try:
                metadata[key] = json.loads(value.replace("'", '"'))
            except json.JSONDecodeError:
                metadata[key] = [item.strip().strip('"\'') for item in value[1:-1].split(",") if item.strip()]
        else:
            metadata[key] = value.strip('"\'')
    return metadata, parts[2].lstrip("\r\n")


def _load_wiki_page(path: Path) -> dict:
    if path.suffix.lower() == ".json":
        page = json.loads(path.read_text(encoding="utf-8"))
        page["_source_file"] = path.name
        return page
    metadata, body = _read_frontmatter(path.read_text(encoding="utf-8"))
    title = metadata.get("title") or path.stem
    related = metadata.get("related", [])
    links = [{"target": item, "linkType": "RELATED"} for item in related]
    return {
        "contractVersion": "prototype-markdown",
        "projectId": "module3-poc",
        "wikiPageId": f"wiki:{_safe_slug(str(title))}",
        "wikiPageVersion": 1,
        "pageType": str(metadata.get("type", "CONCEPT")).upper(),
        "lifecycleStatus": "PUBLISHED",
        "title": str(title),
        "summary": body.splitlines()[0].lstrip("# ") if body.strip() else "",
        "bodyMarkdown": body,
        "evidenceRefs": [],
        "links": links,
        "sourceNames": metadata.get("sources", []),
        "_source_file": path.name,
    }


def _load_evidence_units() -> tuple[list[dict], list[dict]]:
    snapshots: list[dict] = []
    units: list[dict] = []
    for path in sorted(EVIDENCE_DIR.glob("*.json")):
        payload = json.loads(path.read_text(encoding="utf-8"))
        snapshots.append(payload)
        for unit in payload.get("evidenceUnits", []):
            units.append(unit)
    return snapshots, units


def prepare_module3_input() -> dict:
    _ensure_module3_dirs()
    snapshots, evidence_units = _load_evidence_units()
    pages = [_load_wiki_page(path) for path in sorted(WIKI_DIR.iterdir()) if path.suffix.lower() in {".json", ".md"}]
    if not pages:
        raise ValueError("请先导入模块2 WikiPage JSON/Markdown")
    by_id = {unit.get("evidenceId"): unit for unit in evidence_units}
    for old in GRAPH_INPUT_DIR.glob("*.md"):
        old.unlink()
    entries: list[dict] = []
    for index, page in enumerate(pages):
        title = str(page.get("title") or page.get("wikiPageId") or f"wiki-page-{index}")
        page_id = str(page.get("wikiPageId") or f"wiki:{_safe_slug(title)}")
        refs = page.get("evidenceRefs") or []
        linked_units = [by_id[ref.get("evidenceId")] for ref in refs if ref.get("evidenceId") in by_id]
        links = page.get("links") or []
        lines = [
            f"# {title}",
            f"Wiki page id: {page_id}",
            f"Page type: {page.get('pageType', 'CONCEPT')}",
            f"Summary: {page.get('summary', '')}",
            "",
            str(page.get("bodyMarkdown") or ""),
        ]
        if links:
            lines.extend(["", "## Wiki links"])
            for link in links:
                target = link.get("targetWikiPageId") or link.get("target") or link.get("targetTitle") or ""
                predicate = link.get("linkType") or link.get("predicate") or "RELATED_TO"
                lines.append(f"{title} -[{predicate}]-> {target}")
        if linked_units:
            lines.extend(["", "## Evidence from module 1"])
            for unit in linked_units:
                evidence_id = unit.get("evidenceId", "")
                lines.append(f"Evidence ID: {evidence_id}")
                lines.append(str(unit.get("text", "")))
                lines.append(f"Evidence heading: {' / '.join(unit.get('headingPath', []))}")
        output_name = f"{index:03d}-{_safe_slug(title)}.md"
        (GRAPH_INPUT_DIR / output_name).write_text("\n".join(lines).strip() + "\n", encoding="utf-8")
        entries.append({
            "graphInputFile": output_name,
            "wikiPageId": page_id,
            "wikiPageVersion": page.get("wikiPageVersion", 1),
            "title": title,
            "evidenceRefs": refs,
            "links": links,
        })
    manifest = {
        "contractVersion": "module3-poc-1.0",
        "input": {"evidenceSnapshots": [item.get("evidenceSnapshotId") for item in snapshots], "wikiPages": entries},
        "counts": {"evidenceSnapshots": len(snapshots), "evidenceUnits": len(evidence_units), "wikiPages": len(pages)},
        "graphInputDir": str(GRAPH_INPUT_DIR),
    }
    MANIFEST_PATH.write_text(json.dumps(manifest, ensure_ascii=False, indent=2), encoding="utf-8")
    return manifest


def _read_parquet_rows(name: str, limit: int = 100) -> list[dict]:
    path = ROOT / "output" / name
    if not path.exists():
        return []
    try:
        import pandas as pd
        return json.loads(pd.read_parquet(path).head(limit).to_json(orient="records", force_ascii=False, date_format="iso"))
    except Exception as exc:  # noqa: BLE001
        return [{"error": f"读取 {name} 失败: {exc}"}]


def _neo4j_config(data: dict | None = None) -> dict:
    data = data or {}
    return {
        "uri": str(data.get("uri") or os.environ.get("NEO4J_URI") or NEO4J_URI_DEFAULT),
        "user": str(data.get("user") or os.environ.get("NEO4J_USERNAME") or NEO4J_USER_DEFAULT),
        "password": str(data.get("password") or os.environ.get("NEO4J_PASSWORD") or ""),
        "database": str(data.get("database") or os.environ.get("NEO4J_DATABASE") or NEO4J_DATABASE_DEFAULT),
    }


def _sync_neo4j_replace_all_legacy(data: dict) -> dict:
    """Legacy replace-all projection retained only for historical reference."""
    try:
        from neo4j import GraphDatabase
    except ImportError as exc:
        raise ValueError("未安装 Neo4j Python 驱动，请先安装 neo4j") from exc
    config = _neo4j_config(data)
    if not config["password"]:
        raise ValueError("请输入 Neo4j 密码，密码不会写入日志或文件")
    entity_rows = _read_parquet_rows("entities.parquet", limit=100000)
    relation_rows = _read_parquet_rows("relationships.parquet", limit=100000)
    if not entity_rows:
        raise ValueError("没有找到实体结果，请先运行模块 3 索引")
    if entity_rows and "error" in entity_rows[0]:
        raise ValueError(entity_rows[0]["error"])
    if relation_rows and "error" in relation_rows[0]:
        raise ValueError(relation_rows[0]["error"])
    manifest = json.loads(MANIFEST_PATH.read_text(encoding="utf-8")) if MANIFEST_PATH.exists() else {}
    input_key = json.dumps(manifest.get("input", {}), ensure_ascii=False, sort_keys=True)
    snapshot_hash = hashlib.sha1(input_key.encode("utf-8")).hexdigest()[:12]
    snapshot_id = f"module3:{manifest.get('contractVersion', 'unknown')}:{snapshot_hash}"
    nodes = []
    title_to_id = {}
    for row in entity_rows:
        title = str(row.get("title") or "unknown")
        stable_key = hashlib.sha1(title.casefold().strip().encode("utf-8")).hexdigest()[:20]
        entity_id = f"entity:{stable_key}"
        title_to_id[title] = entity_id
        nodes.append({
            "id": entity_id,
            "title": title,
            "entity_type": str(row.get("type") or "UNKNOWN"),
            "description": str(row.get("description") or ""),
            "frequency": row.get("frequency"),
            "degree": row.get("degree"),
            "graph_snapshot": snapshot_id,
        })
    edges = []
    for row in relation_rows:
        source_title = str(row.get("source") or "")
        target_title = str(row.get("target") or "")
        source_id = title_to_id.get(source_title) or f"entity:{_safe_slug(source_title, 'source')}"
        target_id = title_to_id.get(target_title) or f"entity:{_safe_slug(target_title, 'target')}"
        relation_key = "|".join((snapshot_id, source_id, target_id, str(row.get("description") or "")))
        edges.append({
            "id": f"module3:relation:{hashlib.sha1(relation_key.encode('utf-8')).hexdigest()[:20]}",
            "source_id": source_id,
            "target_id": target_id,
            "source_title": source_title,
            "target_title": target_title,
            "description": str(row.get("description") or ""),
            "weight": row.get("weight"),
            "combined_degree": row.get("combined_degree"),
            "graph_snapshot": snapshot_id,
        })
    wiki_nodes = []
    wiki_edges = []
    for page in manifest.get("input", {}).get("wikiPages", []):
        page_id = str(page.get("wikiPageId") or f"wiki:{_safe_slug(str(page.get('title', 'page')))}")
        wiki_nodes.append({
            "id": page_id,
            "title": str(page.get("title") or page_id),
            "page_version": page.get("wikiPageVersion", 1),
            "evidence_refs": json.dumps(page.get("evidenceRefs", []), ensure_ascii=False),
            "graph_snapshot": snapshot_id,
        })
        for index, link in enumerate(page.get("links", [])):
            target = str(link.get("targetWikiPageId") or link.get("target") or link.get("targetTitle") or "")
            if not target:
                continue
            target_id = target if target.startswith("wiki:") else f"wiki:{_safe_slug(target, 'target')}"
            wiki_edges.append({
                "id": f"{page_id}:link:{index}:{target_id}",
                "source_id": page_id,
                "target_id": target_id,
                "target_title": target,
                "predicate": str(link.get("linkType") or link.get("predicate") or "RELATED"),
                "graph_snapshot": snapshot_id,
            })
    driver = GraphDatabase.driver(config["uri"], auth=(config["user"], config["password"]))
    try:
        driver.verify_connectivity()
        with driver.session(database=config["database"]) as session:
            session.run("""
                MATCH (n)
                WHERE n.graph_snapshot STARTS WITH 'module3:'
                DETACH DELETE n
            """).consume()
            session.run("""
                CREATE CONSTRAINT entity_id_unique IF NOT EXISTS
                FOR (n:Entity) REQUIRE n.id IS UNIQUE
            """).consume()
            session.run("""
                CREATE CONSTRAINT relation_id_unique IF NOT EXISTS
                FOR ()-[r:RELATED_TO]-() REQUIRE r.id IS UNIQUE
            """).consume()
            session.run("""
                CREATE CONSTRAINT wiki_page_id_unique IF NOT EXISTS
                FOR (n:WikiPage) REQUIRE n.id IS UNIQUE
            """).consume()
            session.run("""
                UNWIND $rows AS row
                MERGE (n:Entity {id: row.id})
                SET n.title = row.title,
                    n.entity_type = row.entity_type,
                    n.description = row.description,
                    n.frequency = row.frequency,
                    n.degree = row.degree,
                    n.graph_snapshot = row.graph_snapshot,
                    n.updated_at = datetime()
            """, rows=nodes).consume()
            session.run("""
                UNWIND $rows AS row
                MERGE (s:Entity {id: row.source_id})
                ON CREATE SET s.title = row.source_title, s.entity_type = 'UNKNOWN'
                MERGE (t:Entity {id: row.target_id})
                ON CREATE SET t.title = row.target_title, t.entity_type = 'UNKNOWN'
                MERGE (s)-[r:RELATED_TO {id: row.id}]->(t)
                SET r.description = row.description,
                    r.weight = row.weight,
                    r.combined_degree = row.combined_degree,
                    r.graph_snapshot = row.graph_snapshot,
                    r.updated_at = datetime()
            """, rows=edges).consume()
            session.run("""
                UNWIND $rows AS row
                MERGE (n:WikiPage {id: row.id})
                SET n.title = row.title,
                    n.page_version = row.page_version,
                    n.evidence_refs = row.evidence_refs,
                    n.graph_snapshot = row.graph_snapshot,
                    n.updated_at = datetime()
            """, rows=wiki_nodes).consume()
            session.run("""
                UNWIND $rows AS row
                MERGE (s:WikiPage {id: row.source_id})
                MERGE (t:WikiPage {id: row.target_id})
                ON CREATE SET t.title = row.target_title,
                              t.graph_snapshot = row.graph_snapshot
                MERGE (s)-[r:WIKI_LINK {id: row.id}]->(t)
                SET r.predicate = row.predicate,
                    r.graph_snapshot = row.graph_snapshot,
                    r.updated_at = datetime()
            """, rows=wiki_edges).consume()
        return {"ok": True, "uri": config["uri"], "database": config["database"], "nodes": len(nodes), "relationships": len(edges), "wiki_pages": len(wiki_nodes), "wiki_links": len(wiki_edges), "snapshot": snapshot_id}
    finally:
        driver.close()


def sync_neo4j(data: dict) -> dict:
    """Incrementally project module 1/2 provenance and the GraphRAG graph into Neo4j."""
    try:
        from neo4j import GraphDatabase
    except ImportError as exc:
        raise ValueError("Neo4j Python driver is not installed") from exc

    config = _neo4j_config(data)
    if not config["password"]:
        raise ValueError("Neo4j password is not configured")
    entity_rows = _read_parquet_rows("entities.parquet", limit=100000)
    relation_rows = _read_parquet_rows("relationships.parquet", limit=100000)
    text_unit_rows = _read_parquet_rows("text_units.parquet", limit=100000)
    document_rows = _read_parquet_rows("documents.parquet", limit=100000)
    for rows in (entity_rows, relation_rows, text_unit_rows, document_rows):
        if rows and "error" in rows[0]:
            raise ValueError(rows[0]["error"])
    if not entity_rows:
        raise ValueError("No GraphRAG entity output found; run indexing first")
    if not MANIFEST_PATH.exists():
        raise ValueError("No module 3 manifest found; prepare input first")

    manifest = json.loads(MANIFEST_PATH.read_text(encoding="utf-8"))
    pages = manifest.get("input", {}).get("wikiPages", [])
    page_by_id = {
        str(page.get("wikiPageId") or f"wiki:{_safe_slug(str(page.get('title', 'page')))}"): page
        for page in pages
    }
    page_ids = list(page_by_id)
    if not page_ids:
        raise ValueError("No WikiPage input found")
    input_key = json.dumps(manifest.get("input", {}), ensure_ascii=False, sort_keys=True)
    snapshot_id = f"module3:{hashlib.sha1(input_key.encode('utf-8')).hexdigest()[:12]}"

    snapshots, evidence_units = _load_evidence_units()
    snapshots_by_id = {str(row.get("evidenceSnapshotId")): row for row in snapshots}
    evidence_by_id = {str(row.get("evidenceId")): row for row in evidence_units}
    document_nodes: dict[str, dict] = {}
    evidence_nodes: dict[str, dict] = {}
    page_document_edges: list[dict] = []
    page_evidence_edges: list[dict] = []
    wiki_nodes: list[dict] = []

    for page_id, page in page_by_id.items():
        wiki_nodes.append({
            "id": page_id,
            "title": str(page.get("title") or page_id),
            "page_version": page.get("wikiPageVersion", 1),
            "evidence_refs": json.dumps(page.get("evidenceRefs", []), ensure_ascii=False),
            "graph_snapshot": snapshot_id,
        })
        for ref in page.get("evidenceRefs") or []:
            snapshot_id_raw = str(ref.get("evidenceSnapshotId") or "")
            evidence_id_raw = str(ref.get("evidenceId") or "")
            snapshot = snapshots_by_id.get(snapshot_id_raw, {})
            document = snapshot.get("document") or {}
            raw_document_id = str(document.get("documentId") or snapshot_id_raw)
            if raw_document_id:
                document_id = f"document:{raw_document_id}"
                document_nodes[document_id] = {
                    "id": document_id,
                    "document_id": raw_document_id,
                    "document_version_id": str(document.get("documentVersionId") or ""),
                    "title": str(document.get("title") or raw_document_id),
                    "category_code": str(document.get("categoryCode") or ""),
                    "graph_snapshot": snapshot_id,
                }
                page_document_edges.append({"id": f"has_wiki:{document_id}:{page_id}", "document_id": document_id, "page_id": page_id})
            evidence = evidence_by_id.get(evidence_id_raw, {})
            if evidence_id_raw:
                evidence_id = f"evidence:{evidence_id_raw}"
                evidence_nodes[evidence_id] = {
                    "id": evidence_id,
                    "evidence_id": evidence_id_raw,
                    "snapshot_id": snapshot_id_raw,
                    "heading": " / ".join(evidence.get("headingPath") or []),
                    "text": str(evidence.get("text") or ""),
                    "graph_snapshot": snapshot_id,
                }
                page_evidence_edges.append({"id": f"has_evidence:{page_id}:{evidence_id}", "page_id": page_id, "evidence_id": evidence_id})

    page_by_graph_file = {
        str(page.get("graphInputFile")): page_id for page_id, page in page_by_id.items()
    }
    document_to_pages: dict[str, set[str]] = {}
    for row in document_rows:
        page_id = page_by_graph_file.get(str(row.get("title") or ""))
        if page_id:
            document_to_pages.setdefault(str(row.get("id") or ""), set()).add(page_id)
            for text_unit_id in row.get("text_unit_ids") or []:
                document_to_pages.setdefault(str(text_unit_id), set()).add(page_id)
    text_unit_to_pages: dict[str, set[str]] = {}
    for row in text_unit_rows:
        page_ids_for_unit = document_to_pages.get(str(row.get("document_id") or ""), set())
        if page_ids_for_unit:
            text_unit_to_pages.setdefault(str(row.get("id") or ""), set()).update(page_ids_for_unit)

    entities: list[dict] = []
    title_to_id: dict[str, str] = {}
    mention_edges: list[dict] = []
    for row in entity_rows:
        title = str(row.get("title") or "unknown")
        entity_id = f"entity:{hashlib.sha1(title.casefold().strip().encode('utf-8')).hexdigest()[:20]}"
        title_to_id[title] = entity_id
        entities.append({
            "id": entity_id,
            "title": title,
            "entity_type": str(row.get("type") or "OTHER"),
            "description": str(row.get("description") or ""),
            "frequency": row.get("frequency"),
            "degree": row.get("degree"),
            "graph_snapshot": snapshot_id,
        })
        source_pages = set().union(*(text_unit_to_pages.get(str(unit_id), set()) for unit_id in row.get("text_unit_ids") or []))
        for page_id in source_pages:
            mention_edges.append({"id": f"mentions:{page_id}:{entity_id}", "page_id": page_id, "entity_id": entity_id})

    relation_edges: list[dict] = []
    for row in relation_rows:
        source_title = str(row.get("source") or "")
        target_title = str(row.get("target") or "")
        source_id = title_to_id.get(source_title) or f"entity:{_safe_slug(source_title, 'source')}"
        target_id = title_to_id.get(target_title) or f"entity:{_safe_slug(target_title, 'target')}"
        source_pages = set().union(*(text_unit_to_pages.get(str(unit_id), set()) for unit_id in row.get("text_unit_ids") or []))
        for page_id in source_pages:
            relation_key = "|".join((page_id, source_id, target_id, str(row.get("description") or "")))
            relation_edges.append({
                "id": f"module3:relation:{hashlib.sha1(relation_key.encode('utf-8')).hexdigest()[:20]}",
                "source_id": source_id,
                "target_id": target_id,
                "source_title": source_title,
                "target_title": target_title,
                "description": str(row.get("description") or ""),
                "weight": row.get("weight"),
                "combined_degree": row.get("combined_degree"),
                "wiki_page_id": page_id,
                "graph_snapshot": snapshot_id,
            })

    wiki_links: list[dict] = []
    for page_id, page in page_by_id.items():
        for index, link in enumerate(page.get("links") or []):
            target = str(link.get("targetWikiPageId") or link.get("target") or link.get("targetTitle") or "")
            if target:
                target_id = target if target in page_by_id else f"wiki:{_safe_slug(target, 'target')}"
                wiki_links.append({
                    "id": f"wiki_link:{page_id}:{index}:{target_id}", "source_id": page_id, "target_id": target_id,
                    "target_title": target, "predicate": str(link.get("linkType") or link.get("predicate") or "RELATED"),
                    "graph_snapshot": snapshot_id,
                })

    driver = GraphDatabase.driver(config["uri"], auth=(config["user"], config["password"]))
    try:
        driver.verify_connectivity()
        with driver.session(database=config["database"]) as session:
            for statement in (
                "CREATE CONSTRAINT entity_id_unique IF NOT EXISTS FOR (n:Entity) REQUIRE n.id IS UNIQUE",
                "CREATE CONSTRAINT wiki_page_id_unique IF NOT EXISTS FOR (n:WikiPage) REQUIRE n.id IS UNIQUE",
                "CREATE CONSTRAINT document_id_unique IF NOT EXISTS FOR (n:SourceDocument) REQUIRE n.id IS UNIQUE",
                "CREATE CONSTRAINT evidence_id_unique IF NOT EXISTS FOR (n:Evidence) REQUIRE n.id IS UNIQUE",
                "CREATE CONSTRAINT relation_id_unique IF NOT EXISTS FOR ()-[r:RELATED_TO]-() REQUIRE r.id IS UNIQUE",
            ):
                session.run(statement).consume()
            # One-time migration cleanup for relationships created by the old replace-all implementation.
            session.run("MATCH ()-[r]->() WHERE r.graph_snapshot STARTS WITH 'module3:' AND r.projection_version IS NULL DELETE r").consume()
            session.run("MATCH (p:WikiPage)-[r:HAS_EVIDENCE|MENTIONS|WIKI_LINK]->() WHERE p.id IN $page_ids DELETE r", page_ids=page_ids).consume()
            session.run("MATCH (:SourceDocument)-[r:HAS_WIKI]->(p:WikiPage) WHERE p.id IN $page_ids DELETE r", page_ids=page_ids).consume()
            session.run("MATCH ()-[r:RELATED_TO]->() WHERE r.wiki_page_id IN $page_ids DELETE r", page_ids=page_ids).consume()
            session.run("UNWIND $rows AS row MERGE (n:SourceDocument {id: row.id}) SET n.document_id=row.document_id, n.document_version_id=row.document_version_id, n.title=row.title, n.category_code=row.category_code, n.graph_origin='module3', n.projection_version='incremental-v2', n.graph_snapshot=row.graph_snapshot, n.updated_at=datetime()", rows=list(document_nodes.values())).consume()
            session.run("UNWIND $rows AS row MERGE (n:Evidence {id: row.id}) SET n.evidence_id=row.evidence_id, n.snapshot_id=row.snapshot_id, n.heading=row.heading, n.text=row.text, n.graph_origin='module3', n.projection_version='incremental-v2', n.graph_snapshot=row.graph_snapshot, n.updated_at=datetime()", rows=list(evidence_nodes.values())).consume()
            session.run("UNWIND $rows AS row MERGE (n:WikiPage {id: row.id}) SET n.title=row.title, n.page_version=row.page_version, n.evidence_refs=row.evidence_refs, n.graph_origin='module3', n.projection_version='incremental-v2', n.graph_snapshot=row.graph_snapshot, n.updated_at=datetime()", rows=wiki_nodes).consume()
            session.run("UNWIND $rows AS row MERGE (n:Entity {id: row.id}) SET n.title=row.title, n.entity_type=row.entity_type, n.description=row.description, n.frequency=row.frequency, n.degree=row.degree, n.graph_origin='module3', n.projection_version='incremental-v2', n.graph_snapshot=row.graph_snapshot, n.updated_at=datetime()", rows=entities).consume()
            session.run("UNWIND $rows AS row MATCH (d:SourceDocument {id: row.document_id}) MATCH (p:WikiPage {id: row.page_id}) MERGE (d)-[r:HAS_WIKI {id: row.id}]->(p) SET r.graph_origin='module3', r.projection_version='incremental-v2', r.updated_at=datetime()", rows=page_document_edges).consume()
            session.run("UNWIND $rows AS row MATCH (p:WikiPage {id: row.page_id}) MATCH (e:Evidence {id: row.evidence_id}) MERGE (p)-[r:HAS_EVIDENCE {id: row.id}]->(e) SET r.graph_origin='module3', r.projection_version='incremental-v2', r.updated_at=datetime()", rows=page_evidence_edges).consume()
            session.run("UNWIND $rows AS row MATCH (p:WikiPage {id: row.page_id}) MATCH (e:Entity {id: row.entity_id}) MERGE (p)-[r:MENTIONS {id: row.id}]->(e) SET r.graph_origin='module3', r.projection_version='incremental-v2', r.updated_at=datetime()", rows=mention_edges).consume()
            session.run("UNWIND $rows AS row MERGE (s:Entity {id: row.source_id}) ON CREATE SET s.title=row.source_title, s.entity_type='OTHER', s.graph_origin='module3', s.projection_version='incremental-v2' MERGE (t:Entity {id: row.target_id}) ON CREATE SET t.title=row.target_title, t.entity_type='OTHER', t.graph_origin='module3', t.projection_version='incremental-v2' MERGE (s)-[r:RELATED_TO {id: row.id}]->(t) SET r.description=row.description, r.weight=row.weight, r.combined_degree=row.combined_degree, r.wiki_page_id=row.wiki_page_id, r.graph_origin='module3', r.projection_version='incremental-v2', r.graph_snapshot=row.graph_snapshot, r.updated_at=datetime()", rows=relation_edges).consume()
            session.run("UNWIND $rows AS row MERGE (s:WikiPage {id: row.source_id}) MERGE (t:WikiPage {id: row.target_id}) ON CREATE SET t.title=row.target_title, t.graph_origin='module3', t.projection_version='incremental-v2' MERGE (s)-[r:WIKI_LINK {id: row.id}]->(t) SET r.predicate=row.predicate, r.graph_origin='module3', r.projection_version='incremental-v2', r.graph_snapshot=row.graph_snapshot, r.updated_at=datetime()", rows=wiki_links).consume()
            session.run("MATCH (n:Evidence {graph_origin:'module3'}) WHERE NOT (n)<-[:HAS_EVIDENCE]-() DETACH DELETE n").consume()
            session.run("MATCH (n:SourceDocument {graph_origin:'module3'}) WHERE NOT (n)-[:HAS_WIKI]->() DETACH DELETE n").consume()
        return {"ok": True, "uri": config["uri"], "database": config["database"], "documents": len(document_nodes), "evidence": len(evidence_nodes), "wiki_pages": len(wiki_nodes), "entities": len(entities), "relationships": len(relation_edges), "snapshot": snapshot_id, "mode": "incremental-v2"}
    finally:
        driver.close()


def build_query_evidence(query: str, answer: str) -> dict:
    """Resolve answer terms back to the Neo4j provenance graph for the UI."""
    config = _neo4j_config({})
    if not config["password"]:
        return {"entities": [], "paths": [], "sources": []}
    _, units = _load_evidence_units()
    unit_by_id = {str(unit.get("evidenceId")): unit for unit in units}
    driver = None
    try:
        from neo4j import GraphDatabase

        driver = GraphDatabase.driver(config["uri"], auth=(config["user"], config["password"]))
        with driver.session(database=config["database"]) as session:
            entity_rows = list(session.run(
                "MATCH (n:Entity) WHERE $answer CONTAINS n.title OR $question CONTAINS n.title "
                "RETURN n.id AS id, n.title AS title, n.entity_type AS type LIMIT 12",
                parameters={"answer": answer, "question": query},
            ))
            entity_ids = [row["id"] for row in entity_rows]
            path_rows = list(session.run(
                "MATCH (w:WikiPage)-[:MENTIONS]->(s:Entity)-[r:RELATED_TO]->(t:Entity) "
                "WHERE s.id IN $entity_ids OR t.id IN $entity_ids "
                "RETURN w.title AS wiki_title, w.id AS wiki_page_id, s.title AS source, t.title AS target, "
                "r.description AS description, r.weight AS weight LIMIT 12",
                entity_ids=entity_ids,
            )) if entity_ids else []
            page_ids = list(dict.fromkeys(row["wiki_page_id"] for row in path_rows))
            source_query = (
                "MATCH (d:SourceDocument)-[:HAS_WIKI]->(w:WikiPage)-[:HAS_EVIDENCE]->(e:Evidence) "
                "WHERE w.id IN $page_ids "
                "RETURN d.title AS document_title, d.document_id AS document_id, d.document_version_id AS document_version_id, "
                "w.title AS wiki_title, w.id AS wiki_page_id, e.evidence_id AS evidence_id, e.heading AS heading, e.text AS text LIMIT 12"
            )
            source_rows = list(session.run(source_query, page_ids=page_ids)) if page_ids else []
    except Exception as exc:  # noqa: BLE001
        return {"entities": [], "paths": [], "sources": [], "error": str(exc)}
    finally:
        if driver is not None:
            driver.close()
    sources = []
    for row in source_rows:
        unit = unit_by_id.get(str(row["evidence_id"]), {})
        pages = sorted({str(item.get("pageNumber")) for item in unit.get("sourceFragments", []) if item.get("pageNumber") is not None})
        sources.append({
            **dict(row),
            "page_numbers": pages,
        })
    return {
        "entities": [dict(row) for row in entity_rows],
        "paths": [dict(row) for row in path_rows],
        "sources": sources,
    }


def module3_status() -> dict:
    _ensure_module3_dirs()
    manifest = json.loads(MANIFEST_PATH.read_text(encoding="utf-8")) if MANIFEST_PATH.exists() else None
    return {
        "evidenceFiles": sorted(path.name for path in EVIDENCE_DIR.glob("*.json")),
        "wikiFiles": sorted(path.name for path in WIKI_DIR.iterdir() if path.suffix.lower() in {".json", ".md"}),
        "prepared": bool(manifest),
        "manifest": manifest,
        "graphInputFiles": sorted(path.name for path in GRAPH_INPUT_DIR.glob("*.md")),
    }


class Handler(BaseHTTPRequestHandler):
    """Serve the UI and a few safe local GraphRAG operations."""

    def _send(self, status: int, body: str, content_type: str) -> None:
        payload = body.encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", content_type)
        self.send_header("Content-Length", str(len(payload)))
        self.end_headers()
        self.wfile.write(payload)

    def do_GET(self) -> None:  # noqa: N802
        path = urlparse(self.path).path
        if path == "/":
            self._send(200, (STATIC / "index.html").read_text(encoding="utf-8"), "text/html; charset=utf-8")
            return
        if path == "/api/status":
            output_dir = ROOT / "output"
            files = sorted(p.name for p in output_dir.glob("*.parquet")) if output_dir.exists() else []
            self._send(200, json.dumps({"root": str(ROOT), "indexed": bool(files), "files": files}), "application/json")
            return
        if path == "/api/module3/status":
            self._send(200, json.dumps(module3_status(), ensure_ascii=False), "application/json")
            return
        if path == "/api/module3/results":
            payload = {"manifest": module3_status().get("manifest")}
            for name in ("entities.parquet", "relationships.parquet", "communities.parquet", "community_reports.parquet"):
                payload[name.removesuffix(".parquet")] = _read_parquet_rows(name)
            self._send(200, json.dumps(payload, ensure_ascii=False), "application/json")
            return
        if path == "/api/neo4j/config":
            self._send(200, json.dumps({
                "uri": os.environ.get("NEO4J_URI") or NEO4J_URI_DEFAULT,
                "user": os.environ.get("NEO4J_USERNAME") or NEO4J_USER_DEFAULT,
                "database": os.environ.get("NEO4J_DATABASE") or NEO4J_DATABASE_DEFAULT,
            }, ensure_ascii=False), "application/json")
            return
        self._send(404, "Not found", "text/plain; charset=utf-8")

    def do_POST(self) -> None:  # noqa: N802
        path = urlparse(self.path).path
        try:
            length = int(self.headers.get("Content-Length", "0"))
            data = json.loads(self.rfile.read(length) or "{}")
            if path == "/api/upload":
                name = Path(str(data.get("name", ""))).name
                encoded = str(data.get("content", ""))
                allowed = {".txt", ".md", ".csv", ".json", ".jsonl"}
                if not name or Path(name).suffix.lower() not in allowed:
                    raise ValueError("仅支持 .txt、.md、.csv、.json、.jsonl 文件")
                if not encoded:
                    raise ValueError("文件内容为空")
                payload = base64.b64decode(encoded, validate=True)
                input_dir = ROOT / "input"
                input_dir.mkdir(exist_ok=True)
                source = Path(name)
                saved_name = source.name
                (input_dir / saved_name).write_bytes(payload)
                self._send(200, json.dumps({"ok": True, "name": saved_name, "size": len(payload)}, ensure_ascii=False), "application/json")
                return
            if path == "/api/module3/upload":
                kind = str(data.get("kind", "")).lower()
                name = Path(str(data.get("name", ""))).name
                encoded = str(data.get("content", ""))
                allowed = {"evidence": {".json"}, "wiki": {".json", ".md"}}
                if kind not in allowed or Path(name).suffix.lower() not in allowed[kind]:
                    raise ValueError("模块1只支持 EvidenceSnapshot JSON；模块2支持 WikiPage JSON 或 Markdown")
                if not encoded:
                    raise ValueError("文件内容为空")
                payload = base64.b64decode(encoded, validate=True)
                _ensure_module3_dirs()
                target_dir = EVIDENCE_DIR if kind == "evidence" else WIKI_DIR
                (target_dir / name).write_bytes(payload)
                self._send(200, json.dumps({"ok": True, "kind": kind, "name": name, "size": len(payload)}, ensure_ascii=False), "application/json")
                return
            if path == "/api/module3/load_examples":
                _ensure_module3_dirs()
                contract_roots = [
                    Path(r"D:\euos-service-py"),
                    Path(r"D:\ecos-service-py"),
                    Path(r"D:\esos-service-py"),
                ]
                contract_root = next((path for path in contract_roots if path.exists()), None)
                if contract_root is None:
                    raise ValueError("找不到 euos-service-py / ecos-service-py 的契约样例目录")
                evidence_example = contract_root / "contracts" / "examples" / "evidence" / "perkins-maintenance-snapshot.json"
                wiki_example = contract_root / "contracts" / "examples" / "wiki" / "procedure-page.json"
                if not evidence_example.exists() or not wiki_example.exists():
                    raise ValueError(f"契约样例不完整：{contract_root}")
                shutil.copy2(evidence_example, TEST_CASES_ROOT / "module1" / "evidence-snapshot.json")
                shutil.copy2(wiki_example, TEST_CASES_ROOT / "module2" / "wiki-page.json")
                shutil.copy2(evidence_example, EVIDENCE_DIR / "module1-evidence-snapshot.json")
                shutil.copy2(wiki_example, WIKI_DIR / "module2-wiki-page.json")
                self._send(200, json.dumps({"ok": True, "message": "已加载模块1/模块2真实契约样例"}, ensure_ascii=False), "application/json")
                return
            if path == "/api/module3/prepare":
                manifest = prepare_module3_input()
                self._send(200, json.dumps({"ok": True, "manifest": manifest}, ensure_ascii=False), "application/json")
                return
            if path == "/api/neo4j/sync":
                result = sync_neo4j(data)
                self._send(200, json.dumps(result, ensure_ascii=False), "application/json")
                return
            if path == "/api/query":
                query = str(data.get("query", "")).strip()
                method = str(data.get("method", "global"))
                if not query:
                    raise ValueError("请输入问题")
                if method not in ALLOWED_METHODS:
                    raise ValueError("不支持的检索方式")
                # Put options before the free-form query. This avoids Windows command
                # line parsing treating parts of a long query as extra arguments.
                code, output = run_cli(["query", "--root", str(ROOT), "--method", method, query])
                evidence = build_query_evidence(query, output) if code == 0 else None
                response = {"ok": code == 0, "code": code, "output": output}
                if evidence is not None:
                    response["evidence"] = evidence
                self._send(200, json.dumps(response, ensure_ascii=False), "application/json")
                return
            elif path == "/api/index":
                has_existing_index = (ROOT / "output" / "documents.parquet").exists()
                index_command = "update" if has_existing_index else "index"
                code, output = run_cli([index_command, "--root", str(ROOT), "--verbose"], timeout=7200)
                output = _compact_index_output(output)
                output = ("索引模式：" + ("增量更新（只处理新增或变化的文档）" if has_existing_index else "首次全量索引") + "\n" + output)
                neo4j_result = None
                if code == 0:
                    try:
                        neo4j_result = sync_neo4j({})
                        output += "\n\nNeo4j 自动同步完成：" + json.dumps(neo4j_result, ensure_ascii=False)
                    except Exception as exc:  # noqa: BLE001
                        neo4j_result = {"ok": False, "error": str(exc)}
                        output += "\n\nNeo4j 自动同步失败：" + str(exc)
            else:
                self._send(404, "Not found", "text/plain; charset=utf-8")
                return
            response = {"ok": code == 0, "code": code, "output": output}
            if path == "/api/index":
                response["neo4j"] = neo4j_result
            self._send(200, json.dumps(response, ensure_ascii=False), "application/json")
        except Exception as exc:  # noqa: BLE001
            self._send(400, json.dumps({"ok": False, "error": str(exc)}, ensure_ascii=False), "application/json")


if __name__ == "__main__":
    port = int(sys.argv[1]) if len(sys.argv) > 1 else 8765
    print(f"GraphRAG demo: http://127.0.0.1:{port}")
    ThreadingHTTPServer(("127.0.0.1", port), Handler).serve_forever()
