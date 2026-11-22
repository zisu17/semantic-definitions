# Olist 평가 지표

`definitions/olist/sales.ossie.yaml`에는 지표 28개가 있습니다. 값 검증에 사용하는 17개 기준은 평가셋 v0.3에서 가져왔습니다.

## 예상값의 의미

각 지표의 확장 필드 `expected`에는 평가셋 정답 한 건의 예상값과 허용 오차를 기록했습니다. `t2s check`가 어긋남을 보고할 때는 `notes`와 함께 원인을 확인합니다. 이 값은 업무 담당자가 승인한 기준값이 아닙니다.

평가용 지표마다 기본 주문 상태 조건이 다릅니다.

- `order_count`, `revenue`, `freight`, `purchasing_customers`는 `canceled`와 `unavailable`을 제외합니다.
- `all_order_count`, `customer_count`, `item_price_total`, `item_freight_total`, `average_review_score`는 모든 주문 상태를 기본으로 포함합니다. 상품·카테고리별 주문 수는 해당 그룹 안에서 서로 다른 주문 ID를 셉니다.
- `failed_order_count`와 `failed_order_rate`에서 실패는 `canceled` 또는 `unavailable`입니다. 실패율의 분모는 조회 범위의 고유 주문이며 첫 결제 수단별 집계라면 각 주문의 첫 결제 행을 먼저 고릅니다.
- 결제 건수와 상품 줄 수는 각각의 원본 행을 세며 모든 주문 상태를 기본으로 포함합니다.
- 지연·배송 일수 계열은 배송 완료 주문을 기준으로 합니다.
- 두 지표를 나눠 비율을 만들 때는 같은 주문 상태 조건을 양쪽에 적용합니다.

집계 단위와 컬럼 설명은 스터디의 [공통 지표 사전](https://github.com/zisu17/text-to-sql-study/blob/main/experiments/inputs/B3/metrics.md)과 [수정 데이터 사전](https://github.com/zisu17/text-to-sql-study/blob/main/experiments/inputs/B1-corrected/column-descriptions.md)을 참고합니다. 날짜 범위, 출력 열, 순위 기준과 같은 문항 조건은 [공통 평가셋 v0.3](https://github.com/zisu17/text-to-sql-study/blob/main/evals/combined/model_inputs.v0.3.json)에 있습니다.

## 지표 부분집합 실험

정답률 비교를 위해 지표를 일부만 사용할 때는 정본의 승인 상태를 바꾸지 않습니다.

```bash
uv run scripts/pick_metrics.py definitions/olist/sales.ossie.yaml revenue order_count payment_amount
```

결과는 Git에서 제외된 `build/sales.picked.ossie.yaml`입니다. 모델 이름에는 `_picked`가 붙고 설명에 선택한 지표 이름이 남습니다. 이 파일은 오프라인 정의 검사와 평가 실험용입니다. 일반 조회는 Graph MCP의 활성 모델만 사용하므로 부분집합으로 조회하려면 별도 테스트 모델로 게시해야 합니다. 운영 활성 모델을 실험용 부분집합으로 바꾸지 않습니다.

정의가 활성화된 상태에서 `t2s run`은 집계 SQL을 막습니다. 지표로 표현하지 못한 집계는 `t2s sql`로 계산할 수 있지만 정의되지 않은 계산으로 구분해 평가 기록에 남깁니다.
