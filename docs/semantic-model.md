# 시맨틱 모델 작성 규칙

## 파일과 정본

`definitions/<DB>/<주제>.ossie.yaml`이 데이터셋·필드·조인·지표와 승인 상태의 정본입니다. `<DB>` 디렉터리는 하나의 DB 접속 범위이며 파일 하나가 주제 영역 하나입니다. Ossie 파일끼리는 참조할 수 없으므로 함께 계산하거나 조인하는 데이터셋을 같은 파일에 둡니다.

`sales.ontology.yaml`은 업무 개념과 관계를 정의합니다. `sales.mapping.yaml`은 개념과 시맨틱 필드를 연결합니다. 지표 계산식은 온톨로지에 다시 정의하지 않습니다. Olist에서 `Customer`는 실제 사람(`customer_unique_id`)이고 `CustomerAccount`는 주문별 고객 ID(`customer_id`)입니다.

`definitions/olist/`가 현재 예제입니다. 플러그인의 일반 조회 경로는 이 파일을 직접 읽지 않고 Graph MCP의 활성 모델을 사용합니다. 파일을 수정했다면 [GraphDB 게시 절차](graphdb.md)를 거쳐야 조회에 반영됩니다.

## Ossie 버전과 확장 필드

현재 정의는 Apache Ossie `0.3.0.dev0`(apache/ossie 커밋 `da73e0e`, 2026-11-15)을 따릅니다. 2026-11-16에 `0.2.0.dev0`에서 올렸습니다. 개발 중인 규격이므로 검증기 버전을 바꿀 때 세 정의 파일을 함께 검사합니다.

전용 규격 필드가 없는 정보는 `nonportable_extensions`의 `vendor_name: TEXT2SQL` 항목에 YAML 객체로 적습니다.

| 항목 | 위치·규칙 |
|---|---|
| 승인 상태 | 지표·조인 확장의 `data.status`: `draft` 또는 `approved` |
| 정의 번호 | 모델 확장의 `data.revision`: 정의 수정 때마다 1 증가 |
| 결정 기록 | 모델·지표 확장의 `data.decisions`, `data.decision`, `data.discovery` 등 |
| 필터 조건 | 지표 식 안의 `FILTER (WHERE ...)` 또는 `CASE` |
| 시간 기준 | `ai_context.instructions`의 설명 |

승인된 지표와 조인만 실행에 사용합니다. 새 지표는 `text2sql-plugin`의 `define-metric`으로 초안을 만들고 실제 값으로 검사한 뒤 승인합니다. 확장 필드의 날짜는 문자열로 쓰고 날짜 차이는 `DATEDIFF(day, 시작, 끝)` 형식을 사용합니다. 이전 `custom_extensions`와 JSON 문자열 `data`는 Ossie 0.3 검증 형식이 아닙니다.

## 정의 검증

```bash
export SEMANTIC_MODEL="$(pwd)/definitions/olist/sales.ossie.yaml"
t2s check --static "$SEMANTIC_MODEL"
DATABASE_URL='sqlite:////절대/경로/olist.db' t2s check "$SEMANTIC_MODEL"
```

Ossie 공식 검증기를 사용할 때는 로컬 Ossie 체크아웃 경로를 `OSSIE_DIR`로 지정합니다.

```bash
uv run --with "$OSSIE_DIR/python" --script "$OSSIE_DIR/validation/validate.py" definitions/olist/sales.ossie.yaml
uv run --with "$OSSIE_DIR/python" --script "$OSSIE_DIR/validation/validate.py" --schema "$OSSIE_DIR/ontology/ontology.json" definitions/olist/sales.ontology.yaml
uv run --with "$OSSIE_DIR/python" --script "$OSSIE_DIR/validation/validate.py" --schema "$OSSIE_DIR/ontology/mapping.json" definitions/olist/sales.mapping.yaml
```

## 계산용 데이터셋

현재 지표는 모두 데이터셋 지표입니다. 여러 테이블을 읽는 지표는 `source`에 SQL 조회문을 둔 계산용 데이터셋에 배치했습니다. 이는 현재 Ossie 계산 계층에서 모델 지표 하나가 데이터셋 둘을 직접 읽지 못하는 점을 반영합니다.

- `order_lines`는 주문 상품 한 줄에 주문 상태·고객·판매자 정보를 붙입니다. `revenue`, `freight`, `purchasing_customers`, `same_state_item_rate`, `shipping_limit_on_time_rate`를 계산합니다.
- `review_lines`는 리뷰 한 건에 주문 상태를 붙입니다. `avg_review_score`를 계산합니다.

두 데이터셋은 원래 표의 행과 기본키로 이어집니다. 묶거나 거를 때는 원래 표의 필드를 사용합니다. `t2s`가 받는 SQL 소스는 단일 `SELECT`이며 하위 쿼리, `WITH`, 윈도 함수를 사용하지 않습니다. 이 파일의 다른 데이터셋이 가리키는 테이블만 읽을 수 있습니다.
