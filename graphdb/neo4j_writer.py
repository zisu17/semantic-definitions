"""Atomic publication of a fully resolved graph plan."""

from __future__ import annotations

from datetime import datetime, timezone
from typing import Any

from .plan import GraphPlan


DEFINITION_LABELS = {"Model", "Dataset", "Field", "Metric", "SourceQuery", "JoinRule", "Concept"}
CATALOG_LABELS = {"Table", "Column"}
RELATIONSHIPS = {"HAS", "HAS_SOURCE_QUERY", "READS_FROM", "VALUE_DERIVED_FROM",
                 "USES_FIELD", "ROW_DEPENDS_ON", "HAS_JOIN_RULE", "FROM_DATASET",
                 "TO_DATASET", "MAPS_TO"}


def publish(plan: GraphPlan, *, uri: str, user: str, password: str,
            database: str = "neo4j", allow_partial: bool = False) -> None:
    """Switch ActiveVersion only after all nodes and edges commit successfully."""
    if plan.diagnostics and not allow_partial:
        raise ValueError("Unresolved graph plan:\n" + "\n".join(plan.diagnostics))
    from neo4j import GraphDatabase

    driver = GraphDatabase.driver(uri, auth=(user, password))
    try:
        driver.verify_connectivity()
        with driver.session(database=database) as session:
            session.run("CREATE CONSTRAINT graph_uid IF NOT EXISTS FOR (n:GraphEntity) REQUIRE n.uid IS UNIQUE").consume()
            session.run("CREATE CONSTRAINT active_graph_key IF NOT EXISTS FOR (n:ActiveVersion) REQUIRE (n.tenant, n.model) IS UNIQUE").consume()
            session.execute_write(_write, plan)
    finally:
        driver.close()


def _write(tx: Any, plan: GraphPlan) -> None:
    model_uid = None
    for uid, item in sorted(plan.nodes.items()):
        label = item["label"]
        if label not in DEFINITION_LABELS | CATALOG_LABELS:
            raise ValueError(f"Unsupported label: {label}")
        if label == "Model":
            model_uid = uid
        properties = {k: v for k, v in item.items() if k not in ("label", "uid") and v is not None}
        group = "Definition" if label in DEFINITION_LABELS else "CatalogEntity"
        if label in CATALOG_LABELS:
            # Earlier loaders used the composite physical key but no uid.
            # Adopt those nodes before MERGE so existing constraints and
            # catalog relationships remain intact.
            key_names = (["tenant", "source_id", "catalog", "schema", "name"]
                         if label == "Table" else
                         ["tenant", "source_id", "catalog", "schema", "table", "name"])
            pattern = ", ".join(f"{name}:${name}" for name in key_names)
            tx.run(f"MATCH (n:{label} {{{pattern}}}) WHERE n.uid IS NULL "
                   "SET n:GraphEntity:CatalogEntity, n.uid=$uid",
                   uid=uid, **{name: properties[name] for name in key_names}).consume()
        tx.run(f"MERGE (n:GraphEntity {{uid:$uid}}) SET n:{group}:{label} SET n += $properties",
               uid=uid, properties=properties).consume()
    for (source, kind, target), properties in sorted(plan.edges.items()):
        if kind not in RELATIONSHIPS:
            raise ValueError(f"Unsupported relationship: {kind}")
        tx.run(f"MATCH (a:GraphEntity {{uid:$source}}), (b:GraphEntity {{uid:$target}}) "
               f"MERGE (a)-[r:{kind}]->(b) "
               "ON CREATE SET r += $properties",
               source=source, target=target, properties=properties).consume()
    if model_uid is None:
        raise ValueError("Graph plan has no Model")
    tx.run("MATCH (m:Model {uid:$model_uid}) "
           "MERGE (a:ActiveVersion {tenant:$tenant, model:$model}) "
           "SET a.version_id=$version_id, a.activated_at=datetime($activated_at) "
           "MERGE (a)-[:POINTS_TO]->(m)",
           model_uid=model_uid, tenant=plan.tenant, model=plan.model,
           version_id=plan.version_id,
           activated_at=datetime.now(timezone.utc).isoformat()).consume()
    # Only one active model edge may remain. Historical definition snapshots stay intact.
    tx.run("MATCH (a:ActiveVersion {tenant:$tenant, model:$model})-[r:POINTS_TO]->(m:Model) "
           "WHERE m.uid <> $model_uid DELETE r",
           tenant=plan.tenant, model=plan.model, model_uid=model_uid).consume()
