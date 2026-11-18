"""聚类质量诊断：现役口径（同族硬分区 + 直接冲突对）下，组合会不会同时持有近似重复的 ETF？

【为什么要有这个探针】
#21 重构把「并查集传递闭包」（实测 207 只巨型簇）换成了「族硬分区 + 直接冲突对」。
两者的**松紧方向相反**：
- 老口径：过紧 ⇒ 跨族 ETF 被串成一簇 ⇒ 组合被压成 1~2 只；
- 新口径：可能**过松** ⇒  corr 0.90~0.95 的「近似重复品」不再被拦（阈值只拦 ≥0.95）。

【本探针产出】
1. 每期持仓内部的 corr120 最大值 / ≥0.90 对数 / ≥0.95 对数；
2. 现役持仓（2026-11-03）两两 corr120 与持仓重叠率明细；
3. 老口径（并查集）在同一批池子上的簇规模分布（验证「207 巨型簇」确实存在）。
"""
from __future__ import annotations

import json
import sys
from pathlib import Path

import numpy as np
import pandas as pd

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

import config as C  # noqa: E402
from engine.filters import _weight_overlap, cluster_by_overlap  # noqa: E402
from engine.features import load_prices  # noqa: E402
from engine.weights import load_holdings  # noqa: E402

RUN_ROOT = C.ETF_RUN_ROOT


def corr120(prices: pd.DataFrame, codes: list[str], asof: pd.Timestamp) -> pd.DataFrame:
    px = prices[prices["code"].astype(str).isin(codes)].copy()
    px["date"] = pd.to_datetime(px["date"])
    wide = px.pivot_table(index="date", columns="code", values="close", aggfunc="last")
    wide = wide[wide.index <= asof].tail(int(C.THEME_CORR_WINDOW))
    rets = wide.pct_change().dropna(how="all")
    return rets.corr(min_periods=30)


def main() -> int:
    prices = load_prices(None)
    holdings = load_holdings(None)
    bt = json.loads((RUN_ROOT / "etf_rotation_backtest_v4_prefer1.json").read_text(encoding="utf-8"))
    log = bt["rebalance_log"]

    print("=" * 78)
    print("A. 每期持仓内部的同质度（现役口径，prefer_held=1）")
    print("=" * 78)
    rows = []
    for entry in log:
        d = pd.Timestamp(entry["signal_date"])
        codes = [h["code"] for h in entry["holdings_detail"]]
        if len(codes) < 2:
            continue
        cm = corr120(prices, codes, d)
        vals = cm.values[np.triu_indices(len(codes), 1)]
        vals = vals[~np.isnan(vals)]
        if vals.size == 0:
            continue
        rows.append({
            "signal_date": entry["signal_date"],
            "n": len(codes),
            "max_corr": float(np.nanmax(vals)),
            "n_ge_090": int((vals >= 0.90).sum()),
            "n_ge_095": int((vals >= 0.95).sum()),
        })
    df = pd.DataFrame(rows)
    print(f"期数 {len(df)}；最大两两 corr 的分布："
          f"中位 {df['max_corr'].median():.3f}、p90 {df['max_corr'].quantile(0.9):.3f}、"
          f"最高 {df['max_corr'].max():.3f}")
    print(f"每期内部 corr≥0.90 的对数：均值 {df['n_ge_090'].mean():.2f}，"
          f"最多 {int(df['n_ge_090'].max())} 对；"
          f"corr≥0.95：均值 {df['n_ge_095'].mean():.2f}，最多 {int(df['n_ge_095'].max())} 对")
    worst = df.sort_values("max_corr", ascending=False).head(5)
    print("同质度最高的 5 期：")
    for _, r in worst.iterrows():
        print(f"  {r['signal_date']}  max_corr={r['max_corr']:.3f}  "
              f"≥0.90: {int(r['n_ge_090'])} 对  ≥0.95: {int(r['n_ge_095'])} 对")

    print()
    print("=" * 78)
    print("B. 现役持仓（2026-11-03）两两明细")
    print("=" * 78)
    latest = log[-1]
    codes = [h["code"] for h in latest["holdings_detail"]]
    names = {h["code"]: h["name"] for h in latest["holdings_detail"]}
    asof = pd.Timestamp(latest["signal_date"])
    cm = corr120(prices, codes, asof)
    wmap: dict[str, dict[str, float]] = {}
    h = holdings[holdings["etf_code"].astype(str).isin(codes)].copy()
    h["weight_pct"] = h["weight_pct"].fillna(0.0)
    for code, grp in h.groupby("etf_code"):
        wmap[str(code)] = dict(zip(grp["instrument"].astype(str), grp["weight_pct"].astype(float)))
    print(f"{'A':>7} {'B':>7} {'corr120':>9} {'重叠':>8}  名称")
    for i in range(len(codes)):
        for j in range(i + 1, len(codes)):
            a, b = codes[i], codes[j]
            c = cm.at[a, b] if a in cm.index and b in cm.columns else float("nan")
            ov = _weight_overlap(wmap.get(a, {}), wmap.get(b, {}))
            print(f"{a:>7} {b:>7} {c:>9.3f} {ov:>8.3f}  {names.get(a)} × {names.get(b)}")

    print()
    print("=" * 78)
    print("C. 老口径（并查集传递闭包）的簇规模分布 —— 验证「207 巨型簇」")
    print("=" * 78)
    universe = sorted(holdings["etf_code"].astype(str).unique())
    name_map = {}
    try:
        uni = pd.read_csv(C.ETF_DATA_ROOT / "etf_universe.csv", dtype={"code": str})
        if "name" in uni.columns:
            name_map = dict(zip(uni["code"].astype(str), uni["name"].astype(str)))
    except Exception:
        pass
    theme_map = cluster_by_overlap(holdings, universe, names=name_map, prices=prices, asof=asof)
    sizes = pd.Series(list(theme_map.values())).value_counts()
    print(f"ETF 总数 {len(universe)}；簇数 {len(sizes)}；最大簇 {int(sizes.iloc[0])} 只"
          f"（占比 {sizes.iloc[0] / len(universe):.1%}）")
    print("最大的 5 个簇：")
    for theme, n in sizes.head(5).items():
        members = [c for c, t in theme_map.items() if t == theme][:8]
        print(f"  {theme}: {n} 只，例如 {members}")
    out = {
        "per_period": df.to_dict(orient="records"),
        "max_corr_median": float(df["max_corr"].median()),
        "max_corr_max": float(df["max_corr"].max()),
        "legacy_n_clusters": int(len(sizes)),
        "legacy_largest_cluster": int(sizes.iloc[0]),
    }
    (RUN_ROOT / "probe_cluster_quality.json").write_text(
        json.dumps(out, ensure_ascii=False, indent=2), encoding="utf-8")
    print()
    print(f"已写出 {RUN_ROOT / 'probe_cluster_quality.json'}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
