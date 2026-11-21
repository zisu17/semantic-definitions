# GraphDB 계보와 배포

## 그래프의 역할

Neo4j는 Git의 Ossie·온톨로지·매핑 정의와 실제 DB 카탈로그를 연결한 조회용 그래프입니다. 정의를 직접 수정하는 정본은 아닙니다. `Dataset`, `Field`, `Metric`, `Concept` 등의 정의 노드는 버전별로 보존하고 `Table`, `Column`은 관측한 물리 객체를 나타냅니다.

직접 테이블을 읽는 데이터셋은 해당 `Table`로 연결합니다. SQL을 소스로 쓰는 데이터셋은 `SourceQuery`를 거쳐 FROM·JOIN의 여러 `Table`로 연결합니다. SELECT 출력식에서 필드 값의 원본 컬럼을 찾고 ON·WHERE·GROUP BY 등의 컬럼은 행 집합 영향으로 따로 기록합니다. `JoinRule`은 Ossie에 선언된 시맨틱 조인 규칙입니다.

`revenue`의 대표적인 값 계보는 다음과 같습니다.

```text
Metric(revenue) → Field(order_lines.price) → Column(order_items.price)
Metric(revenue) → Field(order_lines.order_status) → Column(orders.order_status)
```

`orders.order_id` 같은 SQL 조인 키는 `ROW_DEPENDS_ON`으로 연결합니다. 역방향 영향 조회에서는 값 출처와 행 집합 영향 후보를 구분해야 합니다.

## 계획 생성과 게시

`DATABASE_URL`로 원본 DB에 접속할 수 있어야 실제 테이블·컬럼을 확인할 수 있습니다.

```bash
python -m pip install -e .
export DATABASE_URL='sqlite:////절대/경로/olist.db'
semantic-graph plan definitions/olist/sales.ossie.yaml \
  --ontology definitions/olist/sales.ontology.yaml \
  --mapping definitions/olist/sales.mapping.yaml \
  --tenant olist --source-id olist
```

`diagnostics`가 비었는지 확인한 뒤 `NEO4J_URI`, `NEO4J_USER`, `NEO4J_PASSWORD`를 환경변수로 설정해 같은 인수로 `publish`를 실행합니다. 미해결 물리 객체가 있으면 기본적으로 게시를 중단하고 활성 버전을 바꾸지 않습니다. SQL 파싱 오류도 실패로 처리합니다.

```bash
semantic-graph publish definitions/olist/sales.ossie.yaml \
  --ontology definitions/olist/sales.ontology.yaml \
  --mapping definitions/olist/sales.mapping.yaml \
  --tenant olist --source-id olist
```

`version_id`는 세 정의 파일의 바이트 내용으로 만든 SHA-256입니다. 새 버전의 노드·관계를 한 트랜잭션에서 적재한 뒤 `ActiveVersion`을 전환합니다. 이전 정의 버전은 계보 비교와 복구를 위해 보존합니다. 같은 버전을 다시 게시해도 노드가 중복되지 않도록 적재합니다.

현재 물리 타입은 `Column.raw_type`에 저장합니다. 처음 연결을 만든 때의 타입은 계보 관계의 `type_at_activation`에 남깁니다. 이는 승인 당시 타입과 다를 수 있으므로 활성화 시점의 기준값으로 해석해야 합니다.

## CI 준비 상태

`.github/workflows/graphdb.yml`은 `main`의 정의·적재기 변경에 반응하지만 저장소 변수 `GRAPHDB_DEPLOY_ENABLED=true`를 설정해야 게시 작업이 실행됩니다. 현재 자동 게시를 활성화하지 않았습니다.

로컬 `127.0.0.1` Neo4j에 접근하려면 `graphdb-local` 레이블이 있는 사설 GitHub Actions 러너가 필요합니다. 그 러너에서 원본 DB에도 접속할 수 있어야 합니다.

| GitHub Secret | 값 |
|---|---|
| `GRAPH_DATABASE_URL` | 원본 DB의 SQLAlchemy 접속 URL |
| `NEO4J_URI` | 러너에서 접근 가능한 Bolt URI |
| `NEO4J_USER` | Neo4j 사용자 |
| `NEO4J_PASSWORD` | Neo4j 비밀번호 |

기본 스키마·카탈로그가 `main`이 아니면 저장소 변수 `GRAPH_SCHEMA`, `GRAPH_CATALOG`도 지정합니다. 비밀번호와 DB 데이터는 Git에 올리지 않습니다.
