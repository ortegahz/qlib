#!/usr/bin/env python3
"""🔴 对账两个 ETF 行情面板（**尺度一致性**审计）。

【为什么必须有它】
扩展行情面板（为了把 5 折滚动前推的**折 1（2019）**变成可评）必须与**封存面板**
**同复权口径**（``adjust="qfq"`` 前复权）。早先有一版抓取脚本写死了 ``adjust=""``
（不复权）⇒ 与封存面板**不是同一个价格尺度**：实测 296 只里 **84 只**的中位比值 ≠ 1
（有 0.309 / 0.358 / 3.0 / 5.0 / 6.0 这种**分红 / 份额折算因子**）
⇒ 直接混用会让 MA / 动量 / 阈值判断全错。

【本脚本干什么】
把两个面板在**共同区间**上按 ``(date, code)`` 对齐，逐 code 算 **收盘价相对偏差**
的中位数，并给出：
  * ``max_median_rel_diff``  —— 最坏的那只 code 的中位相对偏差（**尺度是否一致**的判据）；
  * ``n_codes_mismatch``     —— 中位相对偏差 > 1e-6 的 code 只数；
  * ``seam`` 段（可选）      —— 拼接点的两侧是否同尺度。

用法::

    python -u manu/tools/v2_price_panel_check.py \
        --a <封存面板.csv> --b <扩展面板.csv> [--seam-from 2020-01-02 --seam-to 2020-02-01]
"""

from __future__ import annotations

import argparse
import json

import pandas as pd


def _load(path: str) -> pd.DataFrame:
    df = pd.read_csv(path, dtype={"code": str})
    df["code"] = df["code"].astype(str).str.zfill(6)
    df["date"] = df["date"].astype(str)
    for col in ("close", "amount"):
        if col in df.columns:
            df[col] = pd.to_numeric(df[col], errors="coerce")
    return df[["date", "code", "close"] + (["amount"] if "amount" in df.columns else [])]


def _compare(a: pd.DataFrame, b: pd.DataFrame) -> dict:
    m = a.merge(b, on=["date", "code"], suffixes=("_a", "_b"))
    if m.empty:
        return {"n_rows": 0, "n_codes": 0}
    base = m["close_a"].abs().clip(lower=1e-9)
    m["rel"] = (m["close_b"] - m["close_a"]).abs() / base
    per_code = m.groupby("code")["rel"].median()
    worst = per_code.sort_values(ascending=False).head(10)
    return {
        "n_rows": int(len(m)),
        "n_codes": int(m["code"].nunique()),
        "date_min": str(m["date"].min()),
        "date_max": str(m["date"].max()),
        "max_median_rel_diff": float(per_code.max()),
        "mean_median_rel_diff": float(per_code.mean()),
        "n_codes_mismatch_gt_1e-6": int((per_code > 1e-6).sum()),
        "n_codes_mismatch_gt_1e-3": int((per_code > 1e-3).sum()),
        "worst_codes": {str(k): float(v) for k, v in worst.items()},
    }


def main() -> int:
    ap = argparse.ArgumentParser(description="ETF 行情面板尺度一致性对账")
    ap.add_argument("--a", required=True, help="面板 A（如封存面板）")
    ap.add_argument("--b", required=True, help="面板 B（如扩展面板）")
    ap.add_argument("--seam-from", default="", help="拼接段起始日 YYYY-MM-DD")
    ap.add_argument("--seam-to", default="", help="拼接段结束日 YYYY-MM-DD")
    ap.add_argument("--out", default="", help="结论 JSON 落盘路径")
    args = ap.parse_args()

    a, b = _load(args.a), _load(args.b)
    out: dict = {"a": args.a, "b": args.b, "overall": _compare(a, b)}
    if args.seam_from and args.seam_to:
        sa = a[(a["date"] >= args.seam_from) & (a["date"] <= args.seam_to)]
        sb = b[(b["date"] >= args.seam_from) & (b["date"] <= args.seam_to)]
        out["seam"] = {"from": args.seam_from, "to": args.seam_to, **_compare(sa, sb)}
    print(json.dumps(out, ensure_ascii=False, indent=2))
    if args.out:
        with open(args.out, "w", encoding="utf-8") as fh:
            json.dump(out, fh, ensure_ascii=False, indent=2)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
