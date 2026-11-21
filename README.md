# semantic-definitions

지표 정의 파일과 이를 Neo4j 계보 그래프로 적재하는 코드를 두는 저장소입니다. 질문에 답하는 도구는 text2sql 플러그인(`text2sql-plugin` 저장소)에 있습니다.

## 들어 있는 것

```
definitions/<DB>/<주제>.ossie.yaml     지표·데이터셋·연결 정의 (Apache Ossie 시맨틱 모델)
definitions/<DB>/<주제>.ontology.yaml  업무 개념과 그 관계
definitions/<DB>/<주제>.mapping.yaml   개념 ↔ 데이터셋·필드 연결
scripts/pick_metrics.py               정의 파일에서 고른 지표만 남긴 실험용 파일을 뽑는다
graphdb/                              Ossie 정의와 DB 카탈로그의 Neo4j 계보 적재기
```

- 폴더 하나가 DB(접속) 하나입니다. 파일 하나가 그 DB 안의 주제 영역 하나입니다.
- **정본은 git 에 있는 Ossie 파일입니다.** 테이블·필드·연결·지표와 그 승인 상태를 한 파일에 둡니다. 연결의 승인 상태는 이 파일 한 곳에만 있습니다.
- 같이 계산하는 테이블은 같은 `.ossie.yaml` 에 둡니다. Ossie 는 파일끼리 참조하지 못합니다.
- 첫 정의는 `definitions/olist/` 입니다. Olist(브라질 이커머스 공개 데이터) 주문·매출 샘플입니다. text2sql 플러그인 저장소의 `tests/fixtures/olist/` 는 시험용 사본이라 여기를 고쳐도 따라 바뀌지 않습니다.

### Ossie 규격 버전

정의 파일은 Apache Ossie `0.3.0.dev0`(apache/ossie 커밋 `da73e0e`, 2026-11-15)을 따릅니다. 2026-11-16 에 `0.2.0.dev0`(커밋 `e2f3698`)에서 올렸습니다.

- 우리 확장 칸은 `nonportable_extensions` 의 `vendor_name: TEXT2SQL` 항목입니다. 승인 상태(`status`), 결정 목록(`decisions`), 정의 번호(`revision`)를 `data` 에 YAML 객체로 적습니다. 0.2 의 `custom_extensions` 와 JSON 문자열 `data` 는 0.3 검증기가 받지 않습니다.
- 확장 칸 안의 날짜는 따옴표로 감싸 문자열로 적습니다. 날짜 객체는 규격 오류입니다.
- 날짜 차이는 규격대로 `DATEDIFF(day, 시작, 끝)` 처럼 단위를 따옴표 없이 씁니다.
- `0.3.0.dev0` 은 개발 중인 판이라 규격이 또 바뀔 수 있습니다. 검증기 커밋을 올릴 때는 세 정의 파일을 모두 새 검증기로 다시 검사합니다.
- t2s 는 전환 기간 동안 옛 형식(`custom_extensions`)도 읽습니다. text2sql 플러그인의 시험용 사본(`tests/fixtures/olist/`)과 text2sql-agent 의 시험용 사본은 아직 옛 형식입니다. text2sql-agent 의 `definitions/olist/` 는 이 정본을 그대로 복사한 것입니다.

최신 검증기는 ossie 파이썬 패키지가 함께 있어야 돕니다. ossie 저장소를 받아 둔 경로를 `<ossie>` 라고 하면 다음처럼 검사합니다.

```bash
uv run --with <ossie>/python --script <ossie>/validation/validate.py definitions/olist/sales.ossie.yaml
```

```bash
uv run --with <ossie>/python --script <ossie>/validation/validate.py --schema <ossie>/ontology/ontology.json definitions/olist/sales.ontology.yaml
```

```bash
uv run --with <ossie>/python --script <ossie>/validation/validate.py --schema <ossie>/ontology/mapping.json definitions/olist/sales.mapping.yaml
```

### 지난 구조 변경

지표를 파일 하나씩으로 나눠 두고 합쳐 쓰는 방식은 2026-11-11 에 시도했다가 되돌렸습니다. 나눈 지표 파일은 Ossie 문서가 아니라서 공식 검증기, 플러그인의 검사 훅, `define-metric` 스킬, 그래프 적재기가 모두 읽지 못했습니다. 지표가 많아져 한 파일로 다루기 어려워지면 그때 다시 정합니다.

## 여기 없는 것

- **DB 데이터와 접속 정보.** 접속 주소는 각자 `DATABASE_URL` 환경변수로 줍니다.
- **카탈로그 소스 설정(`catalog/<DB>/sources.yaml`).**

## 쓰는 법

플러그인에 정의 파일 경로를 알려 줍니다.

```bash
export SEMANTIC_MODEL=~/PycharmProjects/semantic-definitions/definitions/olist/sales.ossie.yaml
```

정의를 고친 뒤에는 검사합니다. `--static` 은 DB 없이 파일만 봅니다.

```bash
t2s check --static definitions/olist/sales.ossie.yaml
```

```bash
DATABASE_URL=sqlite:////경로/olist.db t2s check definitions/olist/sales.ossie.yaml
```

새 지표는 손으로 쓰기보다 플러그인의 `define-metric` 스킬로 만듭니다. 결정부터 묻고, 실제 값으로 확인한 뒤 승인 전(draft) 상태로 적습니다. 승인한 지표만 답변에 쓰입니다.

- 정의를 고칠 때마다 모델의 `revision` 을 1 올립니다. 규격 판 올리기처럼 지표 뜻이 그대로인 고침도 셉니다.

### 계산용 데이터셋

지표는 모두 데이터셋 지표입니다. 표 여러 개가 필요한 지표는 `source` 에 조회문을 둔 계산용 데이터셋에 둡니다. Ossie 2·3층이 집계 하나가 데이터셋 둘을 읽는 모델 지표를 계산하지 않기 때문입니다(2026-11-16).

- `order_lines`: 주문 상품 1줄에 주문 상태·택배사 인계일·고객·판매자 주를 붙였습니다. `revenue`, `freight`, `purchasing_customers`, `same_state_item_rate`, `shipping_limit_on_time_rate` 가 여기 있습니다.
- `review_lines`: 리뷰 1건에 주문 상태를 붙였습니다. `avg_review_score` 가 여기 있습니다.
- 두 데이터셋은 원래 표(`order_items`, `order_reviews`)의 같은 행에 기본키끼리 이어져 있습니다. 묶거나 거를 때는 원래 표의 필드를 씁니다.
- t2s 는 조회문을 좁게 받습니다. SELECT 문 하나이고, 하위 쿼리·WITH·윈도 함수가 없어야 합니다. 이 파일의 다른 데이터셋이 이름으로 가리키는 테이블만 읽을 수 있습니다.

## 지표를 골라 넣는 실험

지표를 하나씩 넣어 가며 정답률을 견줄 때는 정본을 고치지 않고 실험용 파일을 뽑습니다.

```bash
uv run scripts/pick_metrics.py definitions/olist/sales.ossie.yaml revenue order_count payment_amount
```

- 결과는 `build/sales.picked.ossie.yaml` 입니다. `build/` 는 git 에 올리지 않습니다. 실험할 때는 `SEMANTIC_MODEL` 이 이 파일을 가리키게 합니다.
- 뽑은 파일은 모델 이름 끝에 `_picked` 가 붙고 설명에 남긴 지표 이름이 적힙니다. 답변 기록에서 어떤 묶음으로 답했는지 보입니다.
- 지표를 빼려고 정본의 `status` 를 바꾸지 않습니다. `status` 는 승인 기록입니다.

## 평가셋 v0.3 용 지표

`definitions/olist/sales.ossie.yaml` 의 지표 22개 중 17개는 평가셋 v0.3(49문항)에 맞춰 만든 지표입니다. 결정 id 가 `eval_accuracy_comparison` 인 것들입니다.

- 계산 규칙은 평가셋 문항의 조건에서 가져왔습니다. 지표마다 평가셋 정답 하나를 예상값(`expected`)으로 적어 두었습니다. `t2s check` 가 어긋나면 `notes` 로 알립니다. 업무 담당자가 확인한 값이 아닙니다.
- 지표마다 기본으로 넣는 주문 상태가 다릅니다. 매출·주문 수·구매 고객 수는 취소와 재고 없음을 뺍니다. 결제·상품 줄 수 계열은 모든 상태를 넣습니다. 지연·배송 일수 계열은 배송 완료만 넣습니다. 두 지표를 나눌 때는 같은 주문 상태 조건을 양쪽에 붙입니다.
- 문항별 지원 상태 표와 그 표를 만드는 스크립트는 text2sql-agent 저장소의 `evals/study-v0.3/` 에 있습니다. 문항별 계산 방법이 들어 있어서 정의 저장소에 두지 않습니다(2026-11-16 에 옮김).

- 정의를 켠 상태(`SEMANTIC_MODEL` 이 있는 상태)에서 `t2s run` 은 집계 SQL 을 막습니다. 지표가 없는 집계는 `t2s sql` 로 계산할 수 있지만 "정의 안 된 계산"으로 표시됩니다. 정답률을 견줄 때 어느 답이 정의 안 된 계산이었는지 함께 적습니다.

## GraphDB 계보 적재

GraphDB는 이 저장소의 Ossie·온톨로지·매핑 파일에서 다시 만드는 조회용 사본입니다. 적재기는 DB 카탈로그를 조회해 실제 테이블·컬럼을 확인합니다. SQL을 소스로 쓰는 `order_lines`와 `review_lines`는 FROM·JOIN 테이블을 모두 연결하고 SELECT 출력 컬럼의 값 계보와 ON·WHERE 등의 행 집합 의존성을 구분합니다.

```bash
python -m pip install -e .
export DATABASE_URL='sqlite:////절대/경로/olist.db'
semantic-graph plan definitions/olist/sales.ossie.yaml \
  --ontology definitions/olist/sales.ontology.yaml \
  --mapping definitions/olist/sales.mapping.yaml \
  --tenant olist --source-id olist
```

계획에 미해결 테이블·컬럼이 있으면 `publish`는 기본적으로 실패하고 활성 버전을 바꾸지 않습니다. SQL과 정의 파일의 파싱 오류도 실패합니다. 정상 계획을 확인한 뒤 다음 환경변수로 적재합니다.

```bash
export NEO4J_URI='bolt://호스트:7687'
export NEO4J_USER='neo4j'
export NEO4J_PASSWORD='비밀번호'
semantic-graph publish definitions/olist/sales.ossie.yaml \
  --ontology definitions/olist/sales.ontology.yaml \
  --mapping definitions/olist/sales.mapping.yaml \
  --tenant olist --source-id olist
```

`version_id`는 세 정의 파일의 바이트 내용에서 만든 SHA-256입니다. 새 버전의 노드·관계를 한 트랜잭션에서 적재하고 마지막에 `ActiveVersion`을 전환합니다. 이전 정의 버전은 보존합니다. 물리 컬럼의 현재 타입은 `Column.raw_type`, 처음 연결을 만든 때의 타입은 계보 관계의 `type_at_activation`에 기록합니다. 타입이 바뀌면 둘을 비교하되 승인 당시 타입으로 오해하지 않도록 활성화 시점이라는 이름을 사용합니다.

GitHub Actions의 `graphdb.yml`은 `main`에 정의 변경이 들어오고 `GRAPHDB_DEPLOY_ENABLED=true` 저장소 변수를 설정한 뒤 작동합니다. 로컬 주소 `127.0.0.1`의 Neo4j는 GitHub 공개 러너에서 접근할 수 없으므로 `graphdb-local` 레이블을 붙인 사설 러너가 필요합니다. 그 러너에서 원본 DB와 Neo4j에 모두 접근할 수 있어야 합니다. `GRAPH_DATABASE_URL`, `NEO4J_URI`, `NEO4J_USER`, `NEO4J_PASSWORD` Secret을 설정합니다. 다른 스키마를 쓰면 `GRAPH_SCHEMA`, `GRAPH_CATALOG` 변수를 설정합니다.
