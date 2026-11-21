"""Inspect and publish a graph built from semantic-definitions."""

from __future__ import annotations

import argparse
import json
import os
from pathlib import Path

from sqlalchemy import create_engine, inspect

from .plan import build_plan
from .neo4j_writer import publish


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("command", choices=["plan", "publish"])
    parser.add_argument("definition", type=Path)
    parser.add_argument("--tenant", required=True)
    parser.add_argument("--source-id", required=True)
    parser.add_argument("--catalog", default=os.getenv("GRAPH_CATALOG", "main"))
    parser.add_argument("--schema", default=os.getenv("GRAPH_SCHEMA", "main"))
    parser.add_argument("--database-url", default=os.getenv("DATABASE_URL"))
    parser.add_argument("--neo4j-uri", default=os.getenv("NEO4J_URI"))
    parser.add_argument("--neo4j-user", default=os.getenv("NEO4J_USER"))
    parser.add_argument("--neo4j-password", default=os.getenv("NEO4J_PASSWORD"))
    parser.add_argument("--neo4j-database", default=os.getenv("NEO4J_DATABASE", "neo4j"))
    parser.add_argument("--allow-partial", action="store_true")
    args = parser.parse_args()
    if not args.database_url:
        parser.error("DATABASE_URL or --database-url is required for physical lineage")
    engine = create_engine(args.database_url)
    try:
        plan = build_plan(args.definition, tenant=args.tenant, source_id=args.source_id,
                          inspector=inspect(engine),
                          catalog=args.catalog, schema=args.schema)
    finally:
        engine.dispose()
    print(json.dumps(plan.summary(), ensure_ascii=False, indent=2))
    if args.command == "publish":
        if not all((args.neo4j_uri, args.neo4j_user, args.neo4j_password)):
            parser.error("NEO4J_URI, NEO4J_USER and NEO4J_PASSWORD are required")
        publish(plan, uri=args.neo4j_uri, user=args.neo4j_user,
                password=args.neo4j_password, database=args.neo4j_database,
                allow_partial=args.allow_partial)


if __name__ == "__main__":
    main()
