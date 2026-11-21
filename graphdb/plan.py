"""Build a deterministic, inspectable graph plan before writing to Neo4j."""

from __future__ import annotations

from dataclasses import dataclass, field
from hashlib import sha256
import json
from pathlib import Path
from typing import Any

import sqlglot
from sqlglot import exp
import yaml


@dataclass
class GraphPlan:
    tenant: str
    model: str
    version_id: str
    nodes: dict[str, dict[str, Any]] = field(default_factory=dict)
    edges: dict[tuple[str, str, str], dict[str, Any]] = field(default_factory=dict)
    diagnostics: list[str] = field(default_factory=list)

    def node(self, uid: str, label: str, **properties: Any) -> str:
        self.nodes[uid] = {"label": label, "uid": uid, **properties}
        return uid

    def edge(self, source: str, kind: str, target: str, **properties: Any) -> None:
        if source not in self.nodes or target not in self.nodes:
            raise ValueError(f"Edge endpoint missing: {source} -{kind}-> {target}")
        key = (source, kind, target)
        if key in self.edges:
            old = self.edges[key]
            for name, value in properties.items():
                if name == "roles":
                    old[name] = sorted(set(old.get(name, [])) | set(value))
                elif name not in old:
                    old[name] = value
        else:
            self.edges[key] = properties

    def summary(self) -> dict[str, Any]:
        labels: dict[str, int] = {}
        relations: dict[str, int] = {}
        for node in self.nodes.values():
            labels[node["label"]] = labels.get(node["label"], 0) + 1
        for _, kind, _ in self.edges:
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
    for entry in item.get("nonportable_extensions", []):
        if entry.get("vendor_name") == "TEXT2SQL":
            return entry.get("data") or {}
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
    """Resolve only observed physical objects; failed bindings remain diagnostics."""
    definition = Path(definition)
    paths = [definition]
    if ontology and mapping:
        paths += [Path(ontology), Path(mapping)]
    elif ontology or mapping:
        raise ValueError("ontology and mapping must be supplied together")
    data = _read_yaml(definition)
    version = _bundle(paths)
    model = data["name"]
    plan = GraphPlan(tenant, model, version)
    def_uid = lambda object_id: f"definition|{tenant}|{model}|{version}|{object_id}"
    model_uid = def_uid(f"model:{model}")
    plan.node(model_uid, "Model", tenant=tenant, model=model, version_id=version,
              id=f"model:{model}", ref=model, revision=_extension(data).get("revision", 0),
              **_text(data))

    physical_cache: dict[tuple[str, str], tuple[str, dict[str, tuple[str, str]]]] = {}

    def physical(table_name: str, table_schema: str = schema) -> tuple[str, dict[str, tuple[str, str]]]:
        key = (table_schema, table_name)
        if key in physical_cache:
            return physical_cache[key]
        names = set(inspector.get_table_names(schema=table_schema)) | set(inspector.get_view_names(schema=table_schema))
        if table_name not in names:
            raise ValueError(f"Physical table/view absent: {table_schema}.{table_name}")
        table_uid = _table_id(tenant, source_id, catalog, table_schema, table_name)
        plan.node(table_uid, "Table", tenant=tenant, source_id=source_id, catalog=catalog,
                  schema=table_schema, name=table_name,
                  kind="view" if table_name in inspector.get_view_names(schema=table_schema) else "table")
        columns: dict[str, tuple[str, str]] = {}
        for col in inspector.get_columns(table_name, schema=table_schema):
            name = col["name"]
            uid = _column_id(table_uid, name)
            raw_type = str(col["type"])
            plan.node(uid, "Column", tenant=tenant, source_id=source_id, catalog=catalog,
                      schema=table_schema, table=table_name, name=name,
                      raw_type=raw_type, nullable=bool(col.get("nullable", True)))
            plan.edge(table_uid, "HAS", uid)
            columns[name] = (uid, raw_type)
        physical_cache[key] = (table_uid, columns)
        return physical_cache[key]

    datasets = data.get("datasets") or []
    ds_uids: dict[str, str] = {}
    field_uids: dict[tuple[str, str], str] = {}
    for ds in datasets:
        name = ds["name"]
        uid = def_uid(f"dataset:{name}")
        ds_uids[name] = uid
        plan.node(uid, "Dataset", tenant=tenant, model=model, version_id=version,
                  id=f"dataset:{name}", ref=name, source=ds["source"],
                  primary_key=ds.get("primary_key") or [], **_text(ds))
        plan.edge(model_uid, "HAS", uid)
        for field_data in ds.get("fields") or []:
            field_name = field_data["name"]
            field_uid = def_uid(f"field:{name}.{field_name}")
            field_uids[(name, field_name)] = field_uid
            plan.node(field_uid, "Field", tenant=tenant, model=model, version_id=version,
                      id=f"field:{name}.{field_name}", ref=f"{name}.{field_name}",
                      expression=_sql(field_data.get("expression")),
                      datatype=field_data.get("datatype", ""), **_text(field_data))
            plan.edge(uid, "HAS", field_uid)
        for metric in ds.get("metrics") or []:
            metric_name = metric["name"]
            metric_uid = def_uid(f"metric:{name}.{metric_name}")
            expression = _sql(metric.get("expression"))
            plan.node(metric_uid, "Metric", tenant=tenant, model=model, version_id=version,
                      id=f"metric:{name}.{metric_name}", ref=metric_name, dataset=name,
                      expression=expression, datatype=metric.get("datatype", ""),
                      status=_extension(metric).get("status", "draft"), **_text(metric))
            plan.edge(uid, "HAS", metric_uid)
            parsed = _parse(expression, f"metric {metric_name}")
            for col in _columns(parsed):
                field_uid = field_uids.get((name, col.name))
                if not field_uid:
                    plan.diagnostics.append(f"metric {metric_name}: unknown field {col.name}")
                    continue
                role = "measure"
                node = col
                parent = col.parent
                while parent is not None:
                    if isinstance(parent, exp.Where) or (isinstance(parent, exp.If) and node.arg_key == "this"):
                        role = "condition"
                        break
                    node = parent
                    parent = parent.parent
                plan.edge(metric_uid, "USES_FIELD", field_uid, roles=[role])

    for ds in datasets:
        name, source = ds["name"], ds["source"]
        dataset_uid = ds_uids[name]
        if source.lstrip().upper().startswith("SELECT"):
            query_uid = def_uid(f"query:{name}")
            plan.node(query_uid, "SourceQuery", tenant=tenant, model=model, version_id=version,
                      id=f"query:{name}", ref=name, sql=source, **_text(ds))
            plan.edge(dataset_uid, "HAS_SOURCE_QUERY", query_uid)
            parsed = _parse(source, f"source {name}")
            if not isinstance(parsed, exp.Select) or any(isinstance(x, (exp.Subquery, exp.With)) for x in parsed.walk()):
                raise ValueError(f"source {name}: only a flat SELECT is supported")
            alias_map: dict[str, tuple[str, dict[str, tuple[str, str]]]] = {}
            for table_expr in parsed.find_all(exp.Table):
                table_name = table_expr.name
                try:
                    table_ref = physical(table_name, table_expr.db or schema)
                except ValueError as error:
                    plan.diagnostics.append(f"source {name}: {error}")
                    continue
                alias_map[table_expr.alias_or_name] = table_ref
                plan.edge(query_uid, "READS_FROM", table_ref[0])
            def matching_tables(col: exp.Column) -> list[tuple[str, dict[str, tuple[str, str]]]]:
                if col.table:
                    return [alias_map[col.table]] if col.table in alias_map else []
                return [table_ref for table_ref in alias_map.values() if col.name in table_ref[1]]

            projections = {p.alias_or_name: p for p in parsed.expressions}
            output_bindings = {}
            for output_name, projection in projections.items():
                resolved = []
                for col in _columns(projection):
                    matches = matching_tables(col)
                    if len(matches) != 1 or col.name not in matches[0][1]:
                        plan.diagnostics.append(f"source {name}: unresolved projection {output_name}.{col.sql()}")
                        continue
                    resolved.append(matches[0][1][col.name])
                output_bindings[output_name] = resolved
            for field_data in ds.get("fields") or []:
                field_name = field_data["name"]
                expr = _parse(_sql(field_data.get("expression")), f"field {name}.{field_name}")
                for col in _columns(expr):
                    for column_uid, raw_type in output_bindings.get(col.name, []):
                        plan.edge(field_uids[(name, field_name)], "VALUE_DERIVED_FROM", column_uid,
                                  type_at_activation=raw_type)
                    if col.name not in output_bindings:
                        plan.diagnostics.append(f"field {name}.{field_name}: absent SQL output {col.name}")
            clauses: list[tuple[str, exp.Expression | None]] = []
            for join in parsed.args.get("joins") or []:
                clauses.append(("join", join.args.get("on")))
            clauses += [("filter", parsed.args.get("where")), ("group", parsed.args.get("group")),
                        ("filter", parsed.args.get("having"))]
            for role, clause in clauses:
                if clause is None:
                    continue
                for col in _columns(clause):
                    matches = matching_tables(col)
                    if len(matches) != 1 or col.name not in matches[0][1]:
                        plan.diagnostics.append(f"source {name}: unresolved {role} column {col.sql()}")
                        continue
                    column_uid, raw_type = matches[0][1][col.name]
                    plan.edge(query_uid, "ROW_DEPENDS_ON", column_uid, roles=[role],
                              type_at_activation=raw_type)
        else:
            source_parts = source.split(".")
            if len(source_parts) > 2 or any(not part for part in source_parts):
                plan.diagnostics.append(f"source {name}: invalid table reference {source}")
                continue
            table_schema, table_name = (source_parts if len(source_parts) == 2 else (schema, source))
            try:
                table_ref = physical(table_name, table_schema)
            except ValueError as error:
                plan.diagnostics.append(f"source {name}: {error}")
                continue
            plan.edge(dataset_uid, "READS_FROM", table_ref[0])
            for field_data in ds.get("fields") or []:
                field_name = field_data["name"]
                parsed_field = _parse(_sql(field_data.get("expression")), f"field {name}.{field_name}")
                for col in _columns(parsed_field):
                    if col.name in table_ref[1]:
                        column_uid, raw_type = table_ref[1][col.name]
                        plan.edge(field_uids[(name, field_name)], "VALUE_DERIVED_FROM", column_uid,
                                  type_at_activation=raw_type)
                    else:
                        plan.diagnostics.append(f"field {name}.{field_name}: absent physical column {col.name}")

    for join in data.get("relationships") or []:
        name = join["name"]
        uid = def_uid(f"join:{name}")
        source_dataset = next((d for d in datasets if d["name"] == join["from"]), None)
        target = next((d for d in datasets if d["name"] == join["to"]), None)
        from_columns = join["from_columns"]
        to_columns = join["to_columns"]
        if not from_columns or len(from_columns) != len(to_columns):
            plan.diagnostics.append(f"join {name}: key columns must be nonempty and have equal lengths")
        for side, dataset, columns in (("from", source_dataset, from_columns), ("to", target, to_columns)):
            if dataset is None:
                continue
            field_names = {item["name"] for item in dataset.get("fields") or []}
            for column in columns:
                if column not in field_names:
                    plan.diagnostics.append(f"join {name}: unknown {side} field {dataset['name']}.{column}")
        target_keys = [target.get("primary_key") or []] if target else []
        target_keys += target.get("unique_keys") or [] if target else []
        declared = join.get("to_columns") in target_keys
        plan.node(uid, "JoinRule", tenant=tenant, model=model, version_id=version,
                  id=f"join:{name}", ref=name, from_columns=join["from_columns"],
                  to_columns=join["to_columns"], status=_extension(join).get("status", "draft"),
                  declared_n_to_1=declared, data_verified=False, **_text(join))
        plan.edge(model_uid, "HAS_JOIN_RULE", uid)
        for relation, endpoint in (("FROM_DATASET", join["from"]), ("TO_DATASET", join["to"])):
            if endpoint in ds_uids:
                plan.edge(uid, relation, ds_uids[endpoint])
            else:
                plan.diagnostics.append(f"join {name}: unknown dataset {endpoint}")

    if ontology and mapping:
        ont = _read_yaml(Path(ontology))
        mappings = _read_yaml(Path(mapping))
        concept_uids = {}
        for concept in ont.get("ontology") or []:
            concept_name = concept["concept"]
            uid = def_uid(f"concept:{concept_name}")
            concept_uids[concept_name] = uid
            plan.node(uid, "Concept", tenant=tenant, model=model, version_id=version,
                      id=f"concept:{concept_name}", ref=concept_name,
                      concept_type=concept.get("type", ""), description=concept.get("description", ""))
            plan.edge(model_uid, "HAS", uid)

        def walk_mapping(value: Any, current_concept: str | None = None) -> None:
            if isinstance(value, list):
                for part in value:
                    walk_mapping(part, current_concept)
            elif isinstance(value, dict):
                concept_name = value.get("concept", current_concept)
                expression = value.get("expression")
                if isinstance(expression, str) and concept_name in concept_uids:
                    parts = expression.split(".")
                    field_uid = field_uids.get(tuple(parts)) if len(parts) == 2 else None
                    if field_uid:
                        plan.edge(concept_uids[concept_name], "MAPS_TO", field_uid)
                    else:
                        plan.diagnostics.append(f"concept {concept_name}: unresolved mapping {expression}")
                for key, part in value.items():
                    if key not in ("expression", "concept"):
                        walk_mapping(part, concept_name)

        walk_mapping(mappings.get("concept_mappings") or [])
    return plan
