#!/usr/bin/env python3
"""🔴 ETF 行情面板**拼接器**（前复权口径对账 + 拼接，2026-11-18）

【为什么需要它】
  封存面板 ``datasets/etf/etf_prices.csv`` 起点 = **2020-01-02** ⇒ V2 滚动前推
  的**折 1（验证段 2019）**根本拿不到 ETF 行情 ⇒ 5 折 OOS 拼不成 2019–2023
  （≈60 个月频调仓点）⇒ 样本量不足。
  ⇒ 需要把 **2018-01-02 ~ 2020-01-23** 的**前复权**扩展段接上去。

【🔴 口径红线（fail-closed）】
  扩展段与封存面板**必须同为前复权（qfq）**。东方财富的「前复权」在
  ``adjust="qfq"`` 下**以请求区间末日为基准**向前调整 ⇒ 不同 ``end`` 抓到��
  序列**不可直接混用**（份额折算 / 分红因子会差一个常数倍）。
  ⇒ 本脚本**先在重叠区逐只比价**（base.close / ext.close）：
     每只的**中位比值必须 ≈ 1.0**（默认容差 ``--tol 1e-6``），
     且重叠覆盖只数 ≥ ``--min-overlap-codes``；
     **任一只不过 ⇒ 直接抛错、不写任何文件**。

【🔴 只读原则】
  * ``--base``（封存面板）**只读**，绝不覆盖；
  * 输出写到 ``--out``（**必须是新目录**）；
  * 拼接后 ``(date, code)`` 唯一；重叠区**以 base 为准**（已验证逐位一致）。

【用法（服务器）】
    export OMP_NUM_THREADS=4
    python manu/tools/merge_etf_price_panels.py \
        --base  /mnt/data/siping/datasets/manu/etf_rotation/datasets/etf/etf_prices.csv \
        --ext   /mnt/data/siping/datasets/manu/etf_rotation/datasets/etf_ext_qfq/etf_prices.csv \
        --out   /mnt/data/siping/datasets/manu/etf_rotation/datasets/etf_ext_qfq_merged/etf_prices.csv
"""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import sys
import time
from pathlib import Path

import pandas as pd

PRICE_COLS = ["date", "code", "open", "high", "low", "close", "volume", "amount"]


def _log(msg: str) -> None:
    print(f"[{time.strftime('%H:%M:%S')}] {msg}", flush=True)


def _md5(path: Path) -> str:
    h = hashlib.md5()
    with path.open("rb") as fh:
        for chunk in iter(lambda: fh.read(1 << 20), b""):
            h.update(chunk)
    return h.hexdigest()


def _load(path: Path) -> pd.DataFrame:
    df = pd.read_csv(path, dtype={"code": str})
    missing = [c for c in PRICE_COLS if c not in df.columns]
    if missing:
        raise RuntimeError(f"🔴 面板缺列：{path} 缺 {missing}")
    df["code"] = df["code"].astype(str).str.zfill(6)
    df["date"] = pd.to_datetime(df["date"])
    return df[PRICE_COLS]


def compare_overlap(base: pd.DataFrame, ext: pd.DataFrame, tol: float) -> dict:
    """🔴 重叠区逐只比价（base.close / ext.close）。返回统计；不过 ⇒ 抛错。"""
    m = base.merge(ext, on=["date", "code"], suffixes=("_base", "_ext"))
    if m.empty:
        raise RuntimeError("🔴 两面板**无重叠区** ⇒ 无法验证复权口径，拒绝拼接")
    m = m[(m["close_base"] > 0) & (m["close_ext"] > 0)].copy()
    m["ratio"] = m["close_base"] / m["close_ext"]
    g = m.groupby("code")["ratio"].agg(["min", "max", "median", "count"])
    g["max_dev"] = (g["median"] - 1.0).abs()
    g["spread"] = (g["max"] - g["min"]).abs() / g["median"].replace(0.0, pd.NA)
    bad = g[g["max_dev"] > tol]
    stats = {
        "n_overlap_rows": int(len(m)),
        "n_overlap_codes": int(len(g)),
        "overlap_date_min": str(m["date"].min().date()),
        "overlap_date_max": str(m["date"].max().date()),
        "tol": float(tol),
        "n_codes_off_ratio": int(len(bad)),
        "worst_off_ratio_codes": bad["max_dev"].sort_values(ascending=False).head(10).round(9).to_dict(),
        "max_intra_code_spread": float(g["spread"].max()) if len(g) else None,
        "ratio_median_overall": float(g["median"].median()) if len(g) else None,
    }
    return stats


def main() -> int:
    ap = argparse.ArgumentParser(description="ETF 行情面板拼接（前复权口径对账 + fail-closed）")
    ap.add_argument("--base", required=True, help="封存面板 CSV（只读）")
    ap.add_argument("--ext", required=True, help="扩展段 CSV（前复权）")
    ap.add_argument("--out", required=True, help="输出 CSV（须为新目录）")
    ap.add_argument("--manifest", default="", help="输出 manifest（默认 <out>.manifest.json）")
    ap.add_argument("--tol", type=float, default=1e-6, help="逐只比值中位数容差（默认 1e-6）")
    ap.add_argument("--min-overlap-codes", type=int, default=20,
                    help="重叠区最少覆盖只数（默认 20；太少 ⇒ 口径没验透）")
    args = ap.parse_args()

    base_path, ext_path, out_path = Path(args.base), Path(args.ext), Path(args.out)
    for p in (base_path, ext_path):
        if not p.is_file():
            raise FileNotFoundError(f"🔴 面板不存在：{p}")
    if out_path.resolve() in (base_path.resolve(), ext_path.resolve()):
        raise RuntimeError("🔴 输出路径不许覆盖输入面板")

    base = _load(base_path)
    ext = _load(ext_path)
    _log(f"base = {base_path}｜{base['date'].min().date()}~{base['date'].max().date()}"
         f"｜{base['code'].nunique()} 只｜{len(base)} 行")
    _log(f"ext  = {ext_path}｜{ext['date'].min().date()}~{ext['date'].max().date()}"
         f"｜{ext['code'].nunique()} 只｜{len(ext)} 行")

    stats = compare_overlap(base, ext, args.tol)
    _log(f"重叠比价：{stats['n_overlap_codes']} 只 / {stats['n_overlap_rows']} 行"
         f"（{stats['overlap_date_min']}~{stats['overlap_date_max']}）"
         f"｜中位比值 {stats['ratio_median_overall']}｜口径不符 {stats['n_codes_off_ratio']} 只")
    if stats["n_overlap_codes"] < args.min_overlap_codes:
        raise RuntimeError(
            f"🔴 重叠只数不足：{stats['n_overlap_codes']} < {args.min_overlap_codes}"
            " ⇒ 复权口径没验透，拒绝拼接"
        )
    if stats["n_codes_off_ratio"] > 0:
        raise RuntimeError(
            "🔴 复权口径不一致（base/ext 比值 ≠ 1）："
            f"{stats['worst_off_ratio_codes']} ⇒ 两面板**不可**混用，拒绝拼接"
        )

    base_min = base["date"].min()
    head = ext[ext["date"] < base_min]
    merged = pd.concat([head, base], ignore_index=True)
    dup = int(merged.duplicated(subset=["date", "code"]).sum())
    merged = (merged.sort_values(["date", "code"])
              .drop_duplicates(subset=["date", "code"], keep="last")
              .reset_index(drop=True))
    merged["date"] = merged["date"].dt.strftime("%Y-%m-%d")
    _log(f"拼接完成：扩展段 {len(head)} 行（< {base_min.date()}）+ base {len(base)} 行"
         f" ⇒ {len(merged)} 行｜去重前重复 {dup} 条")

    out_path.parent.mkdir(parents=True, exist_ok=True)
    tmp = out_path.with_suffix(out_path.suffix + ".tmp")
    merged.to_csv(tmp, index=False)
    os.replace(tmp, out_path)

    manifest_path = Path(args.manifest) if args.manifest else Path(str(out_path) + ".manifest.json")
    manifest = {
        "generated_at": time.strftime("%Y-%m-%dT%H:%M:%S"),
        "base": {"path": str(base_path), "md5": _md5(base_path),
                 "date_min": str(base["date"].min().date()), "date_max": str(base["date"].max().date()),
                 "n_codes": int(base["code"].nunique()), "n_rows": int(len(base))},
        "ext": {"path": str(ext_path), "md5": _md5(ext_path),
                "date_min": str(ext["date"].min().date()), "date_max": str(ext["date"].max().date()),
                "n_codes": int(ext["code"].nunique()), "n_rows": int(len(ext))},
        "overlap_check": stats,
        "merged": {"path": str(out_path), "md5": _md5(out_path),
                   "date_min": str(merged["date"].min()), "date_max": str(merged["date"].max()),
                   "n_codes": int(merged["code"].nunique()), "n_rows": int(len(merged)),
                   "n_rows_from_ext_head": int(len(head))},
        "note": (
            "🔴 本文件是**派生输入**（不是新的事实源）⇒ 事实源仍是 base（封存面板）；"
            "ext 段仅用于把行情起点前推到 2018-01-02，让滚动前推折 1（2019）可评。"
            "🔴 拼接前已逐只比价（base.close / ext.close 中位比值必须 = 1.0）⇒ "
            "两段同为**前复权（qfq）**且复权基准一致。"
            "🔴 重叠区以 base 为准。"
            "🔴 2018-01 之前 / 2020-01 之后**退市**的 ETF 不在候选池内 ⇒ "
            "早期样本池天然小于现在（口径披露项，不是 bug）。"
        ),
    }
    manifest_path.write_text(json.dumps(manifest, ensure_ascii=False, indent=2), encoding="utf-8")
    _log(f"✅ 输出 {out_path}（{len(merged)} 行 / {merged['code'].nunique()} 只）"
         f"｜manifest {manifest_path}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
