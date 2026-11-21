from pathlib import Path

from sqlalchemy import create_engine, inspect
import yaml

from graphdb.plan import build_plan
from graphdb.neo4j_writer import _write, publish
import pytest


ROOT = Path(__file__).resolve().parents[1]
DEFINITION = ROOT / "definitions/olist/sales.ossie.yaml"
ONTOLOGY = ROOT / "definitions/olist/sales.ontology.yaml"
MAPPING = ROOT / "definitions/olist/sales.mapping.yaml"


def _inspector():
    data = yaml.safe_load(DEFINITION.read_text())
    engine = create_engine("sqlite:///:memory:")
    with engine.begin() as connection:
        for ds in data["datasets"]:
            if ds["source"].lstrip().upper().startswith("SELECT"):
                continue
            columns = ", ".join('"' + field["name"] + '" TEXT' for field in ds["fields"])
            connection.exec_driver_sql(f'CREATE TABLE "{ds["source"]}" ({columns})')
    return engine, inspect(engine)


def test_current_olist_lineage_and_impact():
    engine, inspector = _inspector()
    try:
        plan = build_plan(DEFINITION, tenant="olist", source_id="olist", inspector=inspector,
                          ontology=ONTOLOGY, mapping=MAPPING)
    finally:
        engine.dispose()
    assert not plan.diagnostics, plan.diagnostics
    labels = plan.summary()["nodes"]
    assert (labels["Dataset"], labels["Field"], labels["Metric"],
            labels["SourceQuery"], labels["JoinRule"]) == (10, 61, 22, 2, 9)
    revenue = next(uid for uid, node in plan.nodes.items()
                   if node["label"] == "Metric" and node["ref"] == "revenue")
    measure_fields = {target for (source, relation, target), props in plan.edges.items()
                      if source == revenue and relation == "USES_FIELD" and "measure" in props["roles"]}
    condition_fields = {target for (source, relation, target), props in plan.edges.items()
                        if source == revenue and relation == "USES_FIELD" and "condition" in props["roles"]}
    assert any(plan.nodes[x]["ref"] == "order_lines.price" for x in measure_fields)
    assert any(plan.nodes[x]["ref"] == "order_lines.order_status" for x in condition_fields)
    def origins(fields):
        return {(plan.nodes[target]["table"], plan.nodes[target]["name"])
                for (source, relation, target) in plan.edges
                if source in fields and relation == "VALUE_DERIVED_FROM"}
    assert ("order_items", "price") in origins(measure_fields)
    assert ("orders", "order_status") in origins(condition_fields)
    order_lines_query = next(uid for uid, node in plan.nodes.items()
                             if node["label"] == "SourceQuery" and node["ref"] == "order_lines")
    read_tables = {plan.nodes[target]["name"] for source, relation, target in plan.edges
                   if source == order_lines_query and relation == "READS_FROM"}
    assert read_tables == {"order_items", "orders", "customers", "sellers"}
    join_columns = {(plan.nodes[target]["table"], plan.nodes[target]["name"])
                    for (source, relation, target), props in plan.edges.items()
                    if source == order_lines_query and relation == "ROW_DEPENDS_ON"
                    and "join" in props["roles"]}
    assert {("orders", "order_id"), ("order_items", "order_id")} <= join_columns
    late_rate = next(uid for uid, node in plan.nodes.items()
                     if node["label"] == "Metric" and node["ref"] == "late_delivery_rate")
    assert any(plan.nodes[target]["ref"] == "orders.order_status" and
               "condition" in props["roles"]
               for (source, relation, target), props in plan.edges.items()
               if source == late_rate and relation == "USES_FIELD")


def test_content_hash_changes_with_mapping(tmp_path):
    engine, inspector = _inspector()
    try:
        original = build_plan(DEFINITION, tenant="olist", source_id="olist", inspector=inspector,
                              ontology=ONTOLOGY, mapping=MAPPING)
        changed_mapping = tmp_path / MAPPING.name
        changed_mapping.write_bytes(MAPPING.read_bytes() + b"\n# changed\n")
        changed = build_plan(DEFINITION, tenant="olist", source_id="olist", inspector=inspector,
                             ontology=ONTOLOGY, mapping=changed_mapping)
    finally:
        engine.dispose()
    assert original.version_id != changed.version_id


def test_qualified_table_source_resolves_schema_and_table(tmp_path):
    data = yaml.safe_load(DEFINITION.read_text())
    data["datasets"][0]["source"] = "main.orders"
    definition = tmp_path / "qualified.ossie.yaml"
    definition.write_text(yaml.safe_dump(data, allow_unicode=True))
    engine, inspector = _inspector()
    try:
        plan = build_plan(definition, tenant="olist", source_id="olist", inspector=inspector)
    finally:
        engine.dispose()
    assert not plan.diagnostics, plan.diagnostics
    orders_uid = next(uid for uid, node in plan.nodes.items()
                      if node["label"] == "Dataset" and node["ref"] == "orders")
    assert any(source == orders_uid and relation == "READS_FROM" and
               plan.nodes[target]["schema"] == "main" and plan.nodes[target]["name"] == "orders"
               for source, relation, target in plan.edges)


def test_unknown_sql_alias_is_not_resolved_by_column_name(tmp_path):
    data = yaml.safe_load(DEFINITION.read_text())
    order_lines = next(ds for ds in data["datasets"] if ds["name"] == "order_lines")
    order_lines["source"] = order_lines["source"].replace("oi.price", "missing.price")
    definition = tmp_path / "bad_alias.ossie.yaml"
    definition.write_text(yaml.safe_dump(data, allow_unicode=True))
    engine, inspector = _inspector()
    try:
        plan = build_plan(definition, tenant="olist", source_id="olist", inspector=inspector)
    finally:
        engine.dispose()
    assert any("unresolved projection price.missing.price" in item for item in plan.diagnostics)


def test_join_keys_must_name_fields_on_both_sides(tmp_path):
    data = yaml.safe_load(DEFINITION.read_text())
    data["relationships"][0]["from_columns"] = ["missing_order_id"]
    definition = tmp_path / "bad_join.ossie.yaml"
    definition.write_text(yaml.safe_dump(data, allow_unicode=True))
    engine, inspector = _inspector()
    try:
        plan = build_plan(definition, tenant="olist", source_id="olist", inspector=inspector)
    finally:
        engine.dispose()
    assert any("unknown from field order_items.missing_order_id" in item
               for item in plan.diagnostics)


def test_publish_rejects_unresolved_plan_before_connecting():
    engine, inspector = _inspector()
    try:
        plan = build_plan(DEFINITION, tenant="olist", source_id="olist", inspector=inspector)
    finally:
        engine.dispose()
    plan.diagnostics.append("unresolved physical column")
    with pytest.raises(ValueError, match="Unresolved graph plan"):
        publish(plan, uri="bolt://unused", user="unused", password="unused")


def test_active_pointer_switch_is_last_write():
    engine, inspector = _inspector()
    try:
        plan = build_plan(DEFINITION, tenant="olist", source_id="olist", inspector=inspector)
    finally:
        engine.dispose()

    class Result:
        def consume(self):
            pass

    class Tx:
        queries = []

        def run(self, query, **parameters):
            self.queries.append(query)
            return Result()

    tx = Tx()
    _write(tx, plan)
    assert "SET a.version_id" in tx.queries[-2]
    assert "DELETE r" in tx.queries[-1]
    assert all("DETACH DELETE" not in query for query in tx.queries)
