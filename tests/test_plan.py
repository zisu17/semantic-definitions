from pathlib import Path

from sqlalchemy import create_engine, inspect
import yaml

from graphdb.plan import build_plan
from graphdb.neo4j_writer import _write, publish
import pytest


ROOT = Path(__file__).resolve().parents[1]
DEFINITION = ROOT / "definitions/olist/sales.ossie.yaml"


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


def _plan(definition=DEFINITION):
    engine, inspector = _inspector()
    try:
        return build_plan(definition, tenant="olist", source_id="olist", inspector=inspector)
    finally:
        engine.dispose()


def test_current_olist_follows_published_graph_design():
    plan = _plan()
    assert not plan.diagnostics, plan.diagnostics
    assert plan.summary()["nodes"] == {
        "Model": 1, "Dataset": 10, "Field": 61, "Metric": 28,
        "Table": 8, "Column": 47,
    }
    assert plan.summary()["relationships"] == {
        "HAS": 146, "DERIVED_FROM": 148, "JOINS": 9,
    }
    assert all(node.get("version") == plan.version_id
               for node in plan.nodes.values() if node["label"] in {"Model", "Dataset", "Field", "Metric"})
    revenue = next(uid for uid, node in plan.nodes.items()
                   if node["label"] == "Metric" and node["ref"] == "revenue")
    fields = {(plan.nodes[target]["ref"], props["role"])
              for (source, kind, target, _), props in plan.edges.items()
              if source == revenue and kind == "DERIVED_FROM"}
    assert ("order_lines.price", "measure") in fields
    assert ("order_lines.order_status", "condition") in fields
    assert ("order_items.order_id", "join") in fields
    assert ("orders.order_id", "join") in fields
    price = next(uid for uid, node in plan.nodes.items()
                 if node["label"] == "Field" and node["ref"] == "order_lines.price")
    assert any(source == price and kind == "DERIVED_FROM" and
               plan.nodes[target].get("table") == "order_items" and
               plan.nodes[target]["name"] == "price"
               for source, kind, target, _ in plan.edges)


def test_order_failure_rate_has_order_and_status_lineage():
    plan = _plan()
    metric = next(uid for uid, node in plan.nodes.items()
                  if node["label"] == "Metric" and node["ref"] == "failed_order_rate")
    fields = {plan.nodes[target]["ref"]: props["role"]
              for (source, kind, target, _), props in plan.edges.items()
              if source == metric and kind == "DERIVED_FROM"}
    assert fields == {"orders.order_id": "measure", "orders.order_status": "condition"}


def test_version_depends_on_ossie_definition_only(tmp_path):
    original = _plan()
    definition = tmp_path / DEFINITION.name
    definition.write_bytes(DEFINITION.read_bytes() + b"\n# changed\n")
    changed = _plan(definition)
    assert original.version_id != changed.version_id


def test_qualified_table_source_resolves_schema_and_table(tmp_path):
    data = yaml.safe_load(DEFINITION.read_text())
    data["datasets"][0]["source"] = "main.orders"
    definition = tmp_path / "qualified.ossie.yaml"
    definition.write_text(yaml.safe_dump(data, allow_unicode=True))
    plan = _plan(definition)
    assert not plan.diagnostics, plan.diagnostics
    orders_uid = next(uid for uid, node in plan.nodes.items()
                      if node["label"] == "Dataset" and node["ref"] == "orders")
    assert any(source == orders_uid and kind == "DERIVED_FROM" and
               plan.nodes[target]["schema"] == "main" and plan.nodes[target]["name"] == "orders"
               for source, kind, target, _ in plan.edges)


def test_unknown_sql_alias_is_not_resolved_by_column_name(tmp_path):
    data = yaml.safe_load(DEFINITION.read_text())
    order_lines = next(ds for ds in data["datasets"] if ds["name"] == "order_lines")
    order_lines["source"] = order_lines["source"].replace("oi.price", "missing.price")
    definition = tmp_path / "bad_alias.ossie.yaml"
    definition.write_text(yaml.safe_dump(data, allow_unicode=True))
    plan = _plan(definition)
    assert any("unresolved projection price.missing.price" in item for item in plan.diagnostics)


def test_join_keys_must_name_fields_on_both_sides(tmp_path):
    data = yaml.safe_load(DEFINITION.read_text())
    data["relationships"][0]["from_columns"] = ["missing_order_id"]
    definition = tmp_path / "bad_join.ossie.yaml"
    definition.write_text(yaml.safe_dump(data, allow_unicode=True))
    plan = _plan(definition)
    assert any("unknown from field order_items.missing_order_id" in item
               for item in plan.diagnostics)


def test_multiple_named_joins_between_same_datasets_are_preserved(tmp_path):
    data = yaml.safe_load(DEFINITION.read_text())
    duplicate = dict(data["relationships"][0])
    duplicate["name"] = "second_order_items_to_orders"
    data["relationships"].append(duplicate)
    definition = tmp_path / "two_joins.ossie.yaml"
    definition.write_text(yaml.safe_dump(data, allow_unicode=True))
    plan = _plan(definition)
    assert plan.summary()["relationships"]["JOINS"] == 10


def test_publish_rejects_unresolved_plan_before_connecting():
    plan = _plan()
    plan.diagnostics.append("unresolved physical column")
    with pytest.raises(ValueError, match="Unresolved graph plan"):
        publish(plan, uri="bolt://unused", user="unused", password="unused")


def test_writer_activates_new_version_before_removing_old_definitions():
    plan = _plan()

    class Result:
        def consume(self):
            pass

    class Tx:
        def __init__(self):
            self.queries = []

        def run(self, query, **parameters):
            self.queries.append(query)
            return Result()

    tx = Tx()
    _write(tx, plan)
    activation = next(i for i, query in enumerate(tx.queries) if "SET a.commit" in query)
    cleanup = next(i for i, query in enumerate(tx.queries) if "DETACH DELETE n" in query)
    assert activation < cleanup
    assert "CREATE (:LoadRun" in tx.queries[-2]
    assert "CREATE (:CatalogRun" in tx.queries[-1]
    assert not any("SourceQuery" in query or "JoinRule" in query or "Concept" in query
                   for query in tx.queries)
