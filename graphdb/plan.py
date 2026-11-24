"""Build a deterministic, inspectable graph plan before writing to Neo4j."""

from __future__ import annotations

from dataclasses import dataclass, field
from hashlib import sha256
import json
import re
from pathlib import Path
from typing import Any

import sqlglot
from sqlglot import exp
import yaml


# Same rule as the text2sql plugin's engine.SOURCE_NAME: a dataset source that is a bare
# [catalog.][schema.]table name is a table; anything else is a SQL query. Keep the two in sync.
SOURCE_NAME = re.compile(r"[A-Za-z_][\w$]*(\.[A-Za-z_][\w$]*){0,2}")


def is_table_source(source: str) -> bool:
    return bool(SOURCE_NAME.fullmatch(source.strip()))


ROLE_ORDER = ["measure", "condition", "join"]  # also the priority of the representative `role`


@dataclass
class GraphPlan:
    tenant: str
    model: str
    version_id: str
    nodes: dict[str, dict[str, Any]] = field(default_factory=dict)
    edges: dict[tuple[str, str, str, str], dict[str, Any]] = field(default_factory=dict)
    diagnostics: list[str] = field(default_factory=list)

    def node(self, uid: str, label: str, **properties: Any) -> str:
        self.nodes[uid] = {"label": label, "uid": uid, **properties}
        return uid

    def edge(self, source: str, kind: str, target: str, **properties: Any) -> None:
        if source not in self.nodes or target not in self.nodes:
            raise ValueError(f"Edge endpoint missing: {source} -{kind}-> {target}")
        key = (source, kind, target, properties.get("name", "") if kind in {"JOINS", "REFERENCES"} else "")
        if "role" in properties:
            properties["roles"] = [properties["role"]]
        if key in self.edges:
            old = self.edges[key]
            for name, value in properties.items():
                if name == "role":
                    # `roles` keeps every role a field plays in a metric; `role` stays the
                    # highest-priority one for readers of the previous graph schema.
                    old["roles"] = sorted(set(old.get("roles", [])) | {value}, key=ROLE_ORDER.index)
                    old[name] = old["roles"][0]
                elif name not in old:
                    old[name] = value
        else:
            self.edges[key] = properties

    def summary(self) -> dict[str, Any]:
        labels: dict[str, int] = {}
        relations: dict[str, int] = {}
        for node in self.nodes.values():
            labels[node["label"]] = labels.get(node["label"], 0) + 1
        for _, kind, _, _ in self.edges:
            relations[kind] = relations.get(kind, 0) + 1
        return {"tenant": self.tenant, "model": self.model, "version_id": self.version_id,
                "nodes": labels, "relationships": relations, "diagnostics": self.diagnostics}


def _read_yaml(path: Path) -> dict[str, Any]:
    data = yaml.safe_load(path.read_text(encoding="utf-8"))
    if not isinstance(data, dict):
        raise ValueError(f"Expected YAML mapping: {path}")
    return data


def _bundle(paths: list[Path]) -> str:
    digest = sha256()
    for path in paths:
        digest.update(path.name.encode())
        digest.update(b"\0")
        digest.update(path.read_bytes())
        digest.update(b"\0")
    return digest.hexdigest()


def _extension(item: dict[str, Any]) -> dict[str, Any]:
    """TEXT2SQL vendor data, read the same way as the text2sql plugin's ``ossie.ext``.

    Ossie 0.3.0.dev0 writes ``nonportable_extensions`` with object data; 0.2.0.dev0 wrote
    ``custom_extensions`` with a JSON string.
    """
    for key in ("nonportable_extensions", "custom_extensions"):
        for entry in item.get(key) or []:
            if entry.get("vendor_name") == "TEXT2SQL":
                data = entry.get("data") or {}
                return json.loads(data) if isinstance(data, str) else dict(data)
    return {}


def _text(item: dict[str, Any]) -> dict[str, Any]:
    context = item.get("ai_context") or {}
    result = {"name": item.get("name", ""), "description": item.get("description", ""),
              "synonyms": context.get("synonyms") or [], "instructions": context.get("instructions") or ""}
    if context.get("examples"):
        result["examples"] = context["examples"]
    return result


def _sql(value: Any) -> str:
    return value.get("sql", "") if isinstance(value, dict) else str(value or "")


def _parse(sql: str, context: str) -> exp.Expression:
    try:
        # Ossie uses DATEDIFF(unit, start, end). Snowflake's parser preserves
        # both column arguments and treats the first argument as a unit.
        return sqlglot.parse_one(sql, read="snowflake")
    except Exception as error:
        raise ValueError(f"Cannot parse {context}: {error}") from error


def _columns(expression: exp.Expression) -> list[exp.Column]:
    return list(expression.find_all(exp.Column))


def _table_id(tenant: str, source_id: str, catalog: str, schema: str, table: str) -> str:
    return "table|" + "|".join((tenant, source_id, catalog, schema, table))


def _column_id(table_uid: str, column: str) -> str:
    return f"column|{table_uid}|{column}"


def build_plan(definition: Path, *, tenant: str, source_id: str, inspector: Any,
               ontology: Path | None = None, mapping: Path | None = None,
               catalog: str = "main", schema: str = "main") -> GraphPlan:
    """Build the Model/Dataset/Field/Metric and Table/Column lineage graph."""
    if ontology or mapping:
        raise ValueError("Ontology and mapping are outside this graph design")
    definition = Path(definition)
    data = _read_yaml(definition)
    version = _bundle([definition])
    model = data["name"]
    plan = GraphPlan(tenant, model, version)
    def_uid = lambda object_id: f"definition|{tenant}|{model}|{version}|{object_id}"
    model_uid = def_uid(f"model:{model}")
    plan.node(model_uid, "Model", tenant=tenant, model=model, version=version,
              id=f"model:{model}", ref=model, revision=_extension(data).get("revision", 0),
              ossie_version=str(data.get("version", "")), **_text(data))

    physical_cache: dict[tuple[str, str], tuple[str, dict[str, tuple[str, str]]]] = {}

    def physical(table_name: str, table_schema: str = schema) -> tuple[str, dict[str, tuple[str, str]]]:
        key = (table_schema, table_name)
        if key in physical_cache:
            return physical_cache[key]
        views = set(inspector.get_view_names(schema=table_schema))
        tables = set(inspector.get_table_names(schema=table_schema))
        if table_name not in tables | views:
            raise ValueError(f"Physical table/view absent: {table_schema}.{table_name}")
        table_uid = _table_id(tenant, source_id, catalog, table_schema, table_name)
        pk = inspector.get_pk_constraint(table_name, schema=table_schema).get("constrained_columns") or []
        plan.node(table_uid, "Table", tenant=tenant, source_id=source_id, catalog=catalog,
                  schema=table_schema, name=table_name,
                  kind="view" if table_name in views else "table", pk=pk,
                  fqn_lc=f"{catalog}.{table_schema}.{table_name}".lower())
        columns: dict[str, tuple[str, str]] = {}
        for ordinal, col in enumerate(inspector.get_columns(table_name, schema=table_schema), 1):
            name = col["name"]
            uid = _column_id(table_uid, name)
            raw_type = str(col["type"])
            plan.node(uid, "Column", tenant=tenant, source_id=source_id, catalog=catalog,
                      schema=table_schema, table=table_name, name=name,
                      raw_type=raw_type, nullable=bool(col.get("nullable", True)),
                      ordinal=ordinal)
            plan.edge(table_uid, "HAS", uid)
            columns[name] = (uid, raw_type)
        physical_cache[key] = (table_uid, columns)
        return physical_cache[key]

    datasets = data.get("datasets") or []
    ds_uids: dict[str, str] = {}
    field_uids: dict[tuple[str, str], str] = {}
    metric_items: list[tuple[str, dict[str, Any], str]] = []
    base_fields_by_column: dict[str, set[str]] = {}
    query_join_columns: dict[str, set[str]] = {}
    join_key_fields: set[tuple[str, str]] = set()
    for join in data.get("relationships") or []:
        for side, key in (("from", "from_columns"), ("to", "to_columns")):
            join_key_fields.update((join[side], name) for name in join.get(key) or [])

    for ds in datasets:
        name = ds["name"]
        uid = def_uid(f"dataset:{name}")
        ds_uids[name] = uid
        unique_keys = [",".join(key) for key in ds.get("unique_keys") or []]
        plan.node(uid, "Dataset", tenant=tenant, model=model, version=version,
                  id=f"dataset:{name}", ref=name, source=ds["source"],
                  primary_key=ds.get("primary_key") or [], unique_keys=unique_keys, **_text(ds))
        plan.edge(model_uid, "HAS", uid)
        keys = set(ds.get("primary_key") or [])
        keys.update(value for key in ds.get("unique_keys") or [] for value in key)
        for field_data in ds.get("fields") or []:
            field_name = field_data["name"]
            field_uid = def_uid(f"field:{name}.{field_name}")
            field_uids[(name, field_name)] = field_uid
            expression = _sql(field_data.get("expression"))
            datatype = field_data.get("datatype", "")
            plan.node(field_uid, "Field", tenant=tenant, model=model, version=version,
                      id=f"field:{name}.{field_name}", ref=f"{name}.{field_name}",
                      dataset=name, expression=expression, datatype=datatype,
                      is_time=datatype.lower() in {"date", "datetime", "timestamp"},
                      is_key=field_name in keys or (name, field_name) in join_key_fields,
                      **_text(field_data))
            plan.edge(uid, "HAS", field_uid)
        for metric in ds.get("metrics") or []:
            metric_items.append((name, metric, uid))
    for metric in data.get("metrics") or []:
        metric_items.append(("", metric, model_uid))

    def bind_field(field_uid: str, column_uid: str) -> None:
        plan.edge(field_uid, "DERIVED_FROM", column_uid)
        node = plan.nodes[field_uid]
        column = plan.nodes[column_uid]
        source_ref = f"{column['table']}.{column['name']}"
        node["source_columns"] = sorted(set(node.get("source_columns", [])) | {source_ref})

    for ds in datasets:
        name, source = ds["name"], ds["source"]
        if not is_table_source(source):
            continue
        source_parts = source.strip().split(".")
        if len(source_parts) == 3 and source_parts[0] != catalog:
            plan.diagnostics.append(f"source {name}: table is outside catalog {catalog}: {source}")
            continue
        table_name = source_parts[-1]
        table_schema = source_parts[-2] if len(source_parts) >= 2 else schema
        try:
            table_uid, columns = physical(table_name, table_schema)
        except ValueError as error:
            plan.diagnostics.append(f"source {name}: {error}")
            continue
        plan.edge(ds_uids[name], "DERIVED_FROM", table_uid)
        plan.nodes[ds_uids[name]].update(src_source_id=source_id, src_catalog=catalog,
                                         src_schema=table_schema, src_name=table_name)
        for field_data in ds.get("fields") or []:
            field_name = field_data["name"]
            field_uid = field_uids[(name, field_name)]
            expr = _parse(_sql(field_data.get("expression")), f"field {name}.{field_name}")
            plan.nodes[field_uid]["is_passthrough"] = isinstance(expr, exp.Column)
            for col in _columns(expr):
                if col.name not in columns:
                    plan.diagnostics.append(f"field {name}.{field_name}: absent physical column {col.name}")
                    continue
                column_uid, _ = columns[col.name]
                bind_field(field_uid, column_uid)
                base_fields_by_column.setdefault(column_uid, set()).add(field_uid)

    for ds in datasets:
        name, source = ds["name"], ds["source"]
        if is_table_source(source):
            continue
        parsed = _parse(source, f"source {name}")
        if not isinstance(parsed, exp.Select) or any(isinstance(x, (exp.Subquery, exp.With)) for x in parsed.walk()):
            raise ValueError(f"source {name}: only a flat SELECT is supported")
        alias_map: dict[str, tuple[str, dict[str, tuple[str, str]]]] = {}
        for table_expr in parsed.find_all(exp.Table):
            if table_expr.catalog and table_expr.catalog != catalog:
                plan.diagnostics.append(f"source {name}: table is outside catalog {catalog}: {table_expr.sql()}")
                continue
            try:
                alias_map[table_expr.alias_or_name] = physical(table_expr.name, table_expr.db or schema)
            except ValueError as error:
                plan.diagnostics.append(f"source {name}: {error}")

        def resolve(col: exp.Column) -> tuple[str, str] | None:
            matches = ([alias_map[col.table]] if col.table in alias_map else []) if col.table else [
                table_ref for table_ref in alias_map.values() if col.name in table_ref[1]]
            if len(matches) != 1 or col.name not in matches[0][1]:
                return None
            return matches[0][1][col.name]

        outputs: dict[str, list[str]] = {}
        for projection in parsed.expressions:
            output_name = projection.alias_or_name
            outputs[output_name] = []
            for col in _columns(projection):
                bound = resolve(col)
                if bound is None:
                    plan.diagnostics.append(f"source {name}: unresolved projection {output_name}.{col.sql()}")
                else:
                    outputs[output_name].append(bound[0])
        for field_data in ds.get("fields") or []:
            field_name = field_data["name"]
            field_uid = field_uids[(name, field_name)]
            expr = _parse(_sql(field_data.get("expression")), f"field {name}.{field_name}")
            plan.nodes[field_uid]["is_passthrough"] = isinstance(expr, exp.Column)
            for col in _columns(expr):
                if col.name not in outputs:
                    plan.diagnostics.append(f"field {name}.{field_name}: absent SQL output {col.name}")
                for column_uid in outputs.get(col.name, []):
                    bind_field(field_uid, column_uid)
        query_join_columns[name] = set()
        for join in parsed.args.get("joins") or []:
            on = join.args.get("on")
            if on is None:
                continue
            for col in _columns(on):
                bound = resolve(col)
                if bound is None:
                    plan.diagnostics.append(f"source {name}: unresolved join column {col.sql()}")
                else:
                    query_join_columns[name].add(bound[0])

    for name, metric, owner_uid in metric_items:
        metric_name = metric["name"]
        metric_uid = def_uid(f"metric:{name}.{metric_name}" if name else f"metric:{metric_name}")
        expression = _sql(metric.get("expression"))
        properties = dict(tenant=tenant, model=model, version=version,
                          id=f"metric:{name}.{metric_name}" if name else f"metric:{metric_name}",
                          ref=metric_name, expression=expression,
                          datatype=metric.get("datatype", ""),
                          status=_extension(metric).get("status", "draft"), usable=True,
                          **_text(metric))
        if name:
            properties["dataset"] = name
        plan.node(metric_uid, "Metric", **properties)
        plan.edge(owner_uid, "HAS", metric_uid)
        parsed = _parse(expression, f"metric {metric_name}")
        for col in _columns(parsed):
            field_uid = field_uids.get((name, col.name))
            if field_uid is None:
                plan.diagnostics.append(f"metric {metric_name}: unknown field {col.name}")
                continue
            role = "measure"
            child, parent = col, col.parent
            while parent is not None:
                if isinstance(parent, exp.Where) or (isinstance(parent, exp.If) and child.arg_key == "this"):
                    role = "condition"
                    break
                child, parent = parent, parent.parent
            plan.edge(metric_uid, "DERIVED_FROM", field_uid, role=role)
        for column_uid in query_join_columns.get(name, set()):
            for field_uid in base_fields_by_column.get(column_uid, set()):
                plan.edge(metric_uid, "DERIVED_FROM", field_uid, role="join")

    for join in data.get("relationships") or []:
        name = join["name"]
        source_ds = next((d for d in datasets if d["name"] == join["from"]), None)
        target_ds = next((d for d in datasets if d["name"] == join["to"]), None)
        from_columns, to_columns = join["from_columns"], join["to_columns"]
        if not from_columns or len(from_columns) != len(to_columns):
            plan.diagnostics.append(f"join {name}: key columns must be nonempty and have equal lengths")
        for side, dataset, columns in (("from", source_ds, from_columns), ("to", target_ds, to_columns)):
            if dataset is None:
                plan.diagnostics.append(f"join {name}: unknown {side} dataset {join[side]}")
                continue
            names = {field["name"] for field in dataset.get("fields") or []}
            for column in columns:
                if column not in names:
                    plan.diagnostics.append(f"join {name}: unknown {side} field {dataset['name']}.{column}")
        if source_ds is None or target_ds is None:
            continue
        target_keys = [target_ds.get("primary_key") or []] + (target_ds.get("unique_keys") or [])
        extension = _extension(join)
        context = join.get("ai_context") or {}
        # Same rule as the text2sql plugin: the target columns equal a key as a set (column order is free).
        n_to_1 = any(key and set(key) == set(to_columns) for key in target_keys)
        plan.edge(ds_uids[join["from"]], "JOINS", ds_uids[join["to"]], name=name,
                  from_columns=from_columns, to_columns=to_columns,
                  status=extension.get("status", "draft"), n_to_1=n_to_1,
                  synonyms=context.get("synonyms") or [], instructions=context.get("instructions") or "")

    for (table_schema, table_name), (table_uid, _) in list(physical_cache.items()):
        for fk in inspector.get_foreign_keys(table_name, schema=table_schema):
            target_name = fk.get("referred_table")
            if not target_name:
                continue
            try:
                target_uid, _ = physical(target_name, fk.get("referred_schema") or table_schema)
            except ValueError as error:
                plan.diagnostics.append(f"foreign key {table_name}: {error}")
                continue
            plan.edge(table_uid, "REFERENCES", target_uid, name=fk.get("name") or
                      f"{table_name}:{','.join(fk.get('constrained_columns') or [])}",
                      columns=fk.get("constrained_columns") or [],
                      ref_columns=fk.get("referred_columns") or [])
    return plan
