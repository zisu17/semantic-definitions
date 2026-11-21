# GraphDB 계보와 배포

[메타데이터와 지표 계보를 위한 GraphDB 설계](https://zisu17.github.io/data/text2sql-metadata-lineage-graph-design/)의 노드·관계 계약을 따릅니다.

## 그래프 범위

정의 노드는 `Model`, `Dataset`, `Field`, `Metric`입니다. 모두 `Definition` 라벨을 함께 가집니다. 물리 카탈로그는 `Table`, `Column`입니다. 운영 노드는 `ActiveVersion`, `LoadRun`, `CatalogRun`을 사용합니다. 온톨로지와 매핑 파일은 이 그래프에 적재하지 않습니다.

| 관계 | 시작 → 종료 | 의미 |
|---|---|---|
| `HAS` | Model→Dataset, Dataset→Field, Model/Dataset→Metric, Table→Column | 소속 |
| `DERIVED_FROM` | Dataset→Table, Field→Column, Metric→Field | 데이터 소스와 지표 계보 |
| `JOINS` | Dataset→Dataset | Ossie에 선언된 조인 규칙 |
| `REFERENCES` | Table→Table | DB에 선언된 외래키 |

SQL 조회문을 `source`로 쓰는 데이터셋은 단일 물리 테이블을 소스로 갖지 않으므로 Dataset→Table을 만들지 않습니다. 조회문의 출력 필드는 실제 입력 Column에 연결하고 고정 JOIN 키는 해당 데이터셋의 Metric→Field 계보에 `role: join`으로 연결합니다.

현재 Olist 정의는 글 작성 당시보다 커졌습니다. 2026-11-21 기준 계획에는 Model 1, Dataset 10, Field 61, Metric 22, Table 8, Column 47로 총 149개 노드가 있습니다. 운영 노드가 처음 게시되면 ActiveVersion·LoadRun·CatalogRun이 각각 하나씩 추가됩니다. 관계는 `HAS` 140, `DERIVED_FROM` 140, `JOINS` 9로 총 289개입니다. 따라서 블로그에 기록된 과거 데이터의 118개 노드·181개 관계와 수치가 같지 않습니다.

## 게시

`DATABASE_URL`로 원본 DB에 접속할 수 있어야 실제 테이블·컬럼과 외래키를 확인할 수 있습니다.

```bash
python -m pip install -e .
export DATABASE_URL='sqlite:////절대/경로/olist.db'
semantic-graph plan definitions/olist/sales.ossie.yaml --tenant olist --source-id olist
```

`diagnostics`가 비었는지 확인한 뒤 `NEO4J_URI`, `NEO4J_USER`, `NEO4J_PASSWORD`를 설정합니다.

```bash
semantic-graph publish definitions/olist/sales.ossie.yaml --tenant olist --source-id olist
```

`version`은 Ossie 파일의 바이트 내용 해시입니다. 새 정의를 복합 키로 적재하고 `ActiveVersion.commit`을 전환한 뒤 이전 정의 노드를 제거합니다. 이 과정은 한 트랜잭션에서 실행합니다. Table·Column은 물리 복합 키로 재사용합니다. `LoadRun`과 `CatalogRun`은 게시할 때마다 하나씩 기록합니다.

정의는 `(tenant, model, version, id)`, Table은 `(tenant, source_id, catalog, schema, name)`, Column은 `(tenant, source_id, catalog, schema, table, name)`으로 식별합니다. 게시기는 이 세 복합 유일 제약과 `ActiveVersion(tenant, model)`, `CatalogLock(tenant, source_id)` 제약을 생성합니다. 현재 게시기는 CatalogLock 노드를 만들지 않습니다.

## CI

`.github/workflows/graphdb.yml`은 `main`의 정의·적재기 변경에 반응하지만 저장소 변수 `GRAPHDB_DEPLOY_ENABLED=true`를 설정해야 게시 작업이 실행됩니다. `graphdb-local` 레이블이 있는 사설 러너에서 원본 DB와 Neo4j에 접근할 수 있어야 합니다. `GRAPH_DATABASE_URL`, `NEO4J_URI`, `NEO4J_USER`, `NEO4J_PASSWORD`를 GitHub Secrets에 지정하고 필요하면 `GRAPH_SCHEMA`, `GRAPH_CATALOG`도 설정합니다.
