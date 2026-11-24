from pathlib import Path

from sqlalchemy import create_engine, inspect
import yaml

from graphdb.plan import build_plan, is_table_source
from graphdb.neo4j_writer import _write, publish
import pytest


ROOT = Path(__file__).resolve().parents[1]
DEFINITION = ROOT / "definitions/olist/sales.ossie.yaml"


def _inspector():
    data = yaml.safe_load(DEFINITION.read_text())
    engine = create_engine("sqlite:///:memory:")
    with engine.begin() as connection:
        for ds in data["datasets"]:
            if not is_table_source(ds["source"]):
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


def test_legacy_custom_extensions_status_is_read(tmp_path):
    """0.2.0.dev0 형식(custom_extensions, data 는 JSON 문자열)의 승인 상태도 플러그인처럼 읽는다."""
    data = yaml.safe_load(DEFINITION.read_text())
    orders = next(ds for ds in data["datasets"] if ds["name"] == "orders")
    metric = orders["metrics"][0]
    metric.pop("nonportable_extensions", None)
    metric["custom_extensions"] = [{"vendor_name": "TEXT2SQL", "data": '{"status": "approved"}'}]
    definition = tmp_path / "legacy.ossie.yaml"
    definition.write_text(yaml.safe_dump(data, allow_unicode=True))
    plan = _plan(definition)
    node = next(n for n in plan.nodes.values() if n["label"] == "Metric" and n["ref"] == metric["name"])
    assert node["status"] == "approved"

# 플러그인 engine.SOURCE_NAME, MCP contracts.is_table_source 와 같은 사례 표. 세 곳이 어긋나면 여기서 드러난다
SOURCE_CASES = [
    ("orders", True),
    ("  main.orders  ", True),
    ("warehouse.main.orders", True),
    ("a.b.c.d", False),
    ("SELECT order_id FROM orders", False),
    ("select order_id from orders", False),
    ("WITH t AS (SELECT 1 AS x) SELECT x FROM t", False),
    ("(SELECT order_id FROM orders)", False),
    ("-- 주문\nSELECT order_id FROM orders", False),
    ("/* 주문 */ SELECT order_id FROM orders", False),
]


@pytest.mark.parametrize("source, table", SOURCE_CASES)
def test_source_kind_follows_plugin_rule(source, table):
    assert is_table_source(source) is table


@pytest.mark.parametrize("prefix", ["WITH t AS (SELECT 1 AS x) ", "("])
def test_non_flat_sql_source_is_rejected_as_sql(tmp_path, prefix):
    """판별 규칙이 바뀌어 WITH·괄호 소스는 테이블 이름 오류가 아니라 '평평한 SELECT 만' 오류로 막힌다."""
    data = yaml.safe_load(DEFINITION.read_text())
    order_lines = next(ds for ds in data["datasets"] if ds["name"] == "order_lines")
    order_lines["source"] = prefix + order_lines["source"] + (")" if prefix == "(" else "")
    definition = tmp_path / "nested.ossie.yaml"
    definition.write_text(yaml.safe_dump(data, allow_unicode=True))
    with pytest.raises(ValueError, match="only a flat SELECT is supported"):
        _plan(definition)


def test_comment_before_select_is_a_sql_source(tmp_path):
    data = yaml.safe_load(DEFINITION.read_text())
    order_lines = next(ds for ds in data["datasets"] if ds["name"] == "order_lines")
    order_lines["source"] = "-- 주문 상품 줄\n" + order_lines["source"]
    definition = tmp_path / "commented.ossie.yaml"
    definition.write_text(yaml.safe_dump(data, allow_unicode=True))
    plan = _plan(definition)
    assert not plan.diagnostics, plan.diagnostics


def test_metric_field_keeps_every_role(tmp_path):
    """값과 조건에 함께 쓰인 필드는 roles 에 둘 다 남고, role 은 예전처럼 대표 값 하나다."""
    data = yaml.safe_load(DEFINITION.read_text())
    orders = next(ds for ds in data["datasets"] if ds["name"] == "orders")
    orders.setdefault("metrics", []).append({
        "name": "open_status_kinds",
        "expression": {"sql": "COUNT(DISTINCT order_status) FILTER (WHERE order_status <> 'canceled')"},
        "nonportable_extensions": [{"vendor_name": "TEXT2SQL", "data": {"status": "draft"}}],
    })
    definition = tmp_path / "roles.ossie.yaml"
    definition.write_text(yaml.safe_dump(data, allow_unicode=True))
    plan = _plan(definition)
    def lineage(ref):
        metric = next(uid for uid, node in plan.nodes.items() if node["label"] == "Metric" and node["ref"] == ref)
        return {plan.nodes[target]["ref"]: (props["role"], props["roles"])
                for (source, kind, target, _), props in plan.edges.items()
                if source == metric and kind == "DERIVED_FROM"}
    assert lineage("open_status_kinds") == {"orders.order_status": ("measure", ["measure", "condition"])}
    revenue = lineage("revenue")
    assert revenue["order_lines.price"] == ("measure", ["measure"])
    assert revenue["orders.order_id"] == ("join", ["join"])


def test_n_to_1_ignores_key_column_order(tmp_path):
    """복합 키를 다른 순서로 적어도 키와 같으면 n_to_1 이다(플러그인 engine 과 같은 규칙). 키가 아니면 false."""
    data = yaml.safe_load(DEFINITION.read_text())
    joins = {join["name"]: join for join in data["relationships"]}
    composite = next(join for join in joins.values() if len(join["to_columns"]) > 1)
    composite["from_columns"] = list(reversed(composite["from_columns"]))
    composite["to_columns"] = list(reversed(composite["to_columns"]))
    single = next(join for join in joins.values() if len(join["to_columns"]) == 1)
    target = next(ds for ds in data["datasets"] if ds["name"] == single["to"])
    not_key = next(f["name"] for f in target["fields"] if f["name"] not in (target.get("primary_key") or []))
    data["relationships"].append({**single, "name": "not_a_key", "to_columns": [not_key]})
    definition = tmp_path / "key_order.ossie.yaml"
    definition.write_text(yaml.safe_dump(data, allow_unicode=True))
    plan = _plan(definition)
    n_to_1 = {props["name"]: props["n_to_1"] for (_, kind, _, _), props in plan.edges.items() if kind == "JOINS"}
    assert n_to_1[composite["name"]] is True
    assert n_to_1["not_a_key"] is False


def test_publish_creates_the_bm25_fulltext_index(monkeypatch):
    """게시할 때 정의 텍스트 전문 검색 인덱스(Lucene BM25, cjk 분석기)를 만든다. 실제 Neo4j 에는 붙지 않는다."""
    import neo4j

    queries = []

    class Result:
        def consume(self):
            pass

    class Session:
        def __enter__(self):
            return self

        def __exit__(self, *exc):
            return False

        def run(self, query, **parameters):
            queries.append(query)
            return Result()

        def execute_write(self, work, plan):
            queries.append("WRITE")

    class Driver:
        def verify_connectivity(self):
            pass

        def session(self, database):
            return Session()

        def close(self):
            pass

    monkeypatch.setattr(neo4j.GraphDatabase, "driver", lambda uri, auth: Driver())
    publish(_plan(), uri="bolt://unused", user="unused", password="unused")
    index = next(q for q in queries if "FULLTEXT INDEX" in q)
    assert "definition_text IF NOT EXISTS FOR (n:Definition)" in index
    assert "[n.ref, n.name, n.description, n.synonyms]" in index and "'cjk'" in index
    assert queries.index(index) < queries.index("WRITE")  # 스키마 명령은 쓰기 트랜잭션 밖에서 먼저 한다
