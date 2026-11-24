"""Publish the metadata lineage graph using the documented composite keys."""

from __future__ import annotations

from datetime import datetime, timezone
import json
from typing import Any
from uuid import uuid4

from .plan import GraphPlan


DEFINITION_LABELS = {"Model", "Dataset", "Field", "Metric"}
CATALOG_LABELS = {"Table", "Column"}
RELATIONSHIPS = {"HAS", "DERIVED_FROM", "JOINS", "REFERENCES"}
DEFINITION_KEY = ("tenant", "model", "version", "id")
TABLE_KEY = ("tenant", "source_id", "catalog", "schema", "name")
COLUMN_KEY = ("tenant", "source_id", "catalog", "schema", "table", "name")
# BM25-scored (Lucene) search over definition text, read by the Graph MCP's graph_search_definitions.
# The cjk analyzer splits Korean into bigrams, so "매출" matches "매출액". IF NOT EXISTS keeps an
# existing index as is: to change fields or analyzer, drop definition_text and publish again.
FULLTEXT_INDEX = ("CREATE FULLTEXT INDEX definition_text IF NOT EXISTS FOR (n:Definition) "
                  "ON EACH [n.ref, n.name, n.description, n.synonyms] "
                  "OPTIONS {indexConfig: {`fulltext.analyzer`: 'cjk'}}")


def publish(plan: GraphPlan, *, uri: str, user: str, password: str,
            database: str = "neo4j", allow_partial: bool = False) -> None:
    """Create the new version and switch ActiveVersion in one transaction."""
    if plan.diagnostics and not allow_partial:
        raise ValueError("Unresolved graph plan:\n" + "\n".join(plan.diagnostics))
    from neo4j import GraphDatabase

    driver = GraphDatabase.driver(uri, auth=(user, password))
    try:
        driver.verify_connectivity()
        with driver.session(database=database) as session:
            session.run("CREATE CONSTRAINT definition_key IF NOT EXISTS FOR (n:Definition) "
                        "REQUIRE (n.tenant, n.model, n.version, n.id) IS UNIQUE").consume()
            session.run("CREATE CONSTRAINT table_key IF NOT EXISTS FOR (n:Table) "
                        "REQUIRE (n.tenant, n.source_id, n.catalog, n.schema, n.name) IS UNIQUE").consume()
            session.run("CREATE CONSTRAINT column_key IF NOT EXISTS FOR (n:Column) "
                        "REQUIRE (n.tenant, n.source_id, n.catalog, n.schema, n.table, n.name) IS UNIQUE").consume()
            session.run("CREATE CONSTRAINT active_key IF NOT EXISTS FOR (n:ActiveVersion) "
                        "REQUIRE (n.tenant, n.model) IS UNIQUE").consume()
            session.run("CREATE CONSTRAINT catalog_lock IF NOT EXISTS FOR (n:CatalogLock) "
                        "REQUIRE (n.tenant, n.source_id) IS UNIQUE").consume()
            session.run(FULLTEXT_INDEX).consume()  # schema command: outside the write transaction
            session.execute_write(_write, plan)
    finally:
        driver.close()


def _key(item: dict[str, Any]) -> tuple[str, ...]:
    return DEFINITION_KEY if item["label"] in DEFINITION_LABELS else (
        TABLE_KEY if item["label"] == "Table" else COLUMN_KEY)


def _pattern(prefix: str, keys: tuple[str, ...]) -> str:
    return ", ".join(f"{name}:${prefix}_{name}" for name in keys)


def _parameters(prefix: str, item: dict[str, Any]) -> dict[str, Any]:
    return {f"{prefix}_{name}": item[name] for name in _key(item)}


def _write(tx: Any, plan: GraphPlan) -> None:
    now = datetime.now(timezone.utc).isoformat()
    for _, item in sorted(plan.nodes.items()):
        label = item["label"]
        if label not in DEFINITION_LABELS | CATALOG_LABELS:
            raise ValueError(f"Unsupported label: {label}")
        properties = {k: v for k, v in item.items() if k not in ("label", "uid") and v is not None}
        key = _key(item)
        parameters = _parameters("n", item)
        parameters["properties"] = properties
        if label in DEFINITION_LABELS:
            tx.run(f"MERGE (n:Definition:{label} {{{_pattern('n', key)}}}) "
                   "SET n += $properties",
                   **parameters).consume()
        else:
            tx.run(f"MERGE (n:{label} {{{_pattern('n', key)}}}) "
                   "SET n += $properties REMOVE n:GraphEntity:CatalogEntity, n.uid",
                   **parameters).consume()

    for (source, kind, target, _), properties in sorted(plan.edges.items()):
        if kind not in RELATIONSHIPS:
            raise ValueError(f"Unsupported relationship: {kind}")
        source_item, target_item = plan.nodes[source], plan.nodes[target]
        source_label, target_label = source_item["label"], target_item["label"]
        parameters = {**_parameters("a", source_item), **_parameters("b", target_item),
                      "properties": properties}
        relationship_key = ""
        if kind in {"JOINS", "REFERENCES"}:
            relationship_key = " {name:$relationship_name}"
            parameters["relationship_name"] = properties["name"]
        tx.run(f"MATCH (a:{source_label} {{{_pattern('a', _key(source_item))}}}), "
               f"(b:{target_label} {{{_pattern('b', _key(target_item))}}}) "
               f"MERGE (a)-[r:{kind}{relationship_key}]->(b) "
               "SET r += $properties",
               **parameters).consume()

    tx.run("MERGE (a:ActiveVersion {tenant:$tenant, model:$model}) "
           "SET a.commit=$commit, a.activated_at=datetime($activated_at) "
           "REMOVE a.version_id "
           "WITH a OPTIONAL MATCH (a)-[old:POINTS_TO]->() DELETE old",
           tenant=plan.tenant, model=plan.model, commit=plan.version_id,
           activated_at=now).consume()

    # The blog's active-version contract keeps only the newly activated definition.
    tx.run("MATCH (n:Definition {tenant:$tenant, model:$model}) "
           "WHERE n.version IS NULL OR n.version <> $commit DETACH DELETE n",
           tenant=plan.tenant, model=plan.model, commit=plan.version_id).consume()

    tx.run("CREATE (:LoadRun {tenant:$tenant, model:$model, commit:$commit, "
           "started_at:datetime($at), finished_at:datetime($at), result:'ok', "
           "nodes:$nodes, relationships:$relationships, warnings:[], diagnostics:$diagnostics})",
           tenant=plan.tenant, model=plan.model, commit=plan.version_id,
           at=now, nodes=len(plan.nodes), relationships=len(plan.edges),
           diagnostics=json.dumps(plan.diagnostics, ensure_ascii=False)).consume()
    tx.run("CREATE (:CatalogRun {run_id:$run_id, tenant:$tenant, source_id:$source_id, "
           "started_at:datetime($at), finished_at:datetime($at), status:'SUCCESS', "
           "tables_seen:$tables_seen, columns_seen:$columns_seen})",
           run_id=str(uuid4()), tenant=plan.tenant,
           source_id=next((node["source_id"] for node in plan.nodes.values()
                           if node["label"] == "Table"), ""),
           at=now,
           tables_seen=sum(node["label"] == "Table" for node in plan.nodes.values()),
           columns_seen=sum(node["label"] == "Column" for node in plan.nodes.values())).consume()
