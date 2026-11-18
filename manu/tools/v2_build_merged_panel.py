#!/usr/bin/env python3
"""🔴 拼「扩展行情面板」：**封存段逐位不变** + **前置历史（2018–2019）**。

【口径（关键）】
* 封存面板 ``<etf>/etf_prices.csv``（2020-01-02 ~ 2026-11-10，``adjust="qfq"``）
  是**单盲封存产物的输入指纹** ⇒ **一个字都不许改** ⇒ 合并后 2020+ 的行**原样搬运**。
* 前置历史来自同口径（qfq）的增量抓取（``etf_ext_qfq/etf_prices.csv``）。
* 🔴 拼接段必须**同尺度**：实测拼接段（2020-01-02 ~ 2020-01-23，73 只）相对偏差
  **= 0.0** ⇒ qfq 基准与请求区间无关、且期间无新分红 ⇒ 可以直接拼接。

用法::

    python -u manu/tools/v2_build_merged_panel.py \
        --canon <封存面板.csv> --tail <前置增量.csv> --out <合并面板.csv> \
        [--seam-from 2020-01-02 --seam-to 2020-01-23]
"""

from __future__ import annotations

import argparse
import json

import pandas as pd

COLS = ["date", "code", "open", "high", "low", "close", "volume", "amount"]


def _load(path: str) -> pd.DataFrame:
    df = pd.read_csv(path, dtype={"code": str})
    df["code"] = df["code"].astype(str).str.zfill(6)
    df["date"] = df["date"].astype(str)
    for col in ("open", "high", "low", "close", "volume", "amount"):
        if col in df.columns:
            df[col] = pd.to_numeric(df[col], errors="coerce")
    return df


def main() -> int:
    ap = argparse.ArgumentParser(description="拼扩展行情面板（封存段逐位不变 + 前置历史）")
    ap.add_argument("--canon", required=True)
    ap.add_argument("--tail", required=True)
    ap.add_argument("--out", required=True)
    ap.add_argument("--canon-start", default="2020-01-02", help="封存面板起点（= 拼接点）")
    ap.add_argument("--seam-from", default="", help="拼接段起始日（自检用）")
    ap.add_argument("--seam-to", default="", help="拼接段结束日（自检用）")
    ap.add_argument("--report", default="", help="自检结论 JSON 落盘")
    args = ap.parse_args()

    canon, tail = _load(args.canon), _load(args.tail)
    pre = tail[tail["date"] < args.canon_start][COLS].copy()
    merged = pd.concat([pre, canon[COLS]], ignore_index=True)
    merged = merged.drop_duplicates(subset=["date", "code"], keep="first")
    merged = merged.sort_values(["code", "date"]).reset_index(drop=True)
    merged.to_csv(args.out, index=False)

    rep: dict = {
        "canon": args.canon, "tail": args.tail, "out": args.out,
        "canon_start": args.canon_start,
        "n_rows": int(len(merged)),
        "n_codes": int(merged["code"].nunique()),
        "date_min": str(merged["date"].min()),
        "date_max": str(merged["date"].max()),
        "n_rows_from_tail": int(len(pre)),
        "n_codes_from_tail": int(pre["code"].nunique()),
        "n_rows_from_canon": int(len(merged) - len(pre)),
    }
    # 🔴 拼接段自检：前置增量 vs 封存段在**同一批日期**上必须同尺度
    if args.seam_from and args.seam_to:
        a = canon[(canon["date"] >= args.seam_from) & (canon["date"] <= args.seam_to)]
        b = tail[(tail["date"] >= args.seam_from) & (tail["date"] <= args.seam_to)]
        m = a.merge(b, on=["date", "code"], suffixes=("_c", "_t"))
        if len(m):
            rel = (m["close_t"] - m["close_c"]).abs() / m["close_c"].abs().clip(lower=1e-9)
            per_code = m.assign(r=rel).groupby("code")["r"].median()
            rep["seam_check"] = {
                "from": args.seam_from, "to": args.seam_to,
                "n_rows": int(len(m)), "n_codes": int(m["code"].nunique()),
                "max_median_rel_diff": float(per_code.max()),
                "n_codes_mismatch_gt_1e-6": int((per_code > 1e-6).sum()),
            }
    print(json.dumps(rep, ensure_ascii=False, indent=2))
    if args.report:
        with open(args.report, "w", encoding="utf-8") as fh:
            json.dump(rep, fh, ensure_ascii=False, indent=2)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
