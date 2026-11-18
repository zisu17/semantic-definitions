# /// script
# requires-python = ">=3.9"
# dependencies = ["pyyaml"]
# ///
"""정의 파일에서 고른 지표만 남긴 실험용 파일을 뽑는다. 원본 정의 파일은 건드리지 않는다.

    uv run scripts/pick_metrics.py definitions/olist/sales.ossie.yaml revenue order_count
    uv run scripts/pick_metrics.py definitions/olist/sales.ossie.yaml payment_amount -o /다른/경로.ossie.yaml

지표를 하나씩 넣어 가며 정답률을 견줄 때 쓴다. 결과는 build/<원본 이름>.picked.ossie.yaml 이다(git 에 올리지 않는다).
테이블·필드·연결과 승인 상태는 원본 그대로다. 지표를 빼려고 원본의 status 를 바꾸지 않는다. status 는 승인 기록이다.
뽑은 파일은 모델 이름 끝에 `_picked` 가 붙고, 설명에 남긴 지표 이름이 적힌다. 답변 기록에서 어떤 묶음이었는지 보이게 하려는 것이다.
"""
import argparse
import pathlib
import sys

import yaml


def pick(model: dict, names: list[str]) -> dict:
    have = [m["name"] for d in model["datasets"] for m in d.get("metrics", [])] + [m["name"] for m in model.get("metrics", [])]
    if missing := [n for n in names if n not in have]:
        sys.exit(f"없는 지표: {', '.join(missing)} (있는 것: {', '.join(have)})")
    for holder in [*model["datasets"], model]:
        kept = [m for m in holder.get("metrics", []) if m["name"] in names]
        holder.pop("metrics", None)
        if kept:
            holder["metrics"] = kept
    model["name"] += "_picked"
    model["description"] = f"{model.get('description', '')} (실험용. 지표 {len(names)}개만 남김: {', '.join(sorted(names))})"
    return model


def main() -> None:
    p = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    p.add_argument("definition", type=pathlib.Path, help="원본 정의 파일(.ossie.yaml)")
    p.add_argument("metrics", nargs="+", help="남길 지표 이름")
    p.add_argument("-o", "--out", type=pathlib.Path)
    a = p.parse_args()
    out = a.out or pathlib.Path("build") / a.definition.name.replace(".ossie.yaml", ".picked.ossie.yaml")
    out.parent.mkdir(parents=True, exist_ok=True)
    head = f"# 실험용으로 뽑은 파일이다. 고치지 않는다. 원본은 {a.definition} 이다.\n"
    out.write_text(head + yaml.safe_dump(pick(yaml.safe_load(a.definition.read_text()), a.metrics), allow_unicode=True, sort_keys=False, width=100000))
    print(out)


if __name__ == "__main__":
    main()
