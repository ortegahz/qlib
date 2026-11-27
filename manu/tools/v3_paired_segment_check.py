#!/usr/bin/env python3
"""🔴 V3 对照实验的「成对段级统计」独立复核工具（§十四.150）。

🔴 **为什么需要它**：探针报告里的「段级 t」＝ ``segment_excess_stats`` 算的
   **臂 vs 基准**的**单样本** t ⇒ 答的是「这个臂自己有没有 alpha」；
   而**替换判据**问的是「**V3 有没有打败 V2**」⇒ **两者不是同一个问题**。
   本工具用跑批产物（``nav_*.csv``）**独立重算**：
     ① 分段口径复原（与报告**逐位对拍**）
     ② 单样本 t（原口径，保留披露）
     ③ **成对 t**（臂 − ``control_v2``，同分段配对）＋ Holm 多重比较校正
     ④ 效应来源拆解（段均差 / 成对胜率）

🔴 **纯标准库**（无 pandas）⇒ 本机（无 pandas 的 ``/usr/bin/python3``）也能跑。
🔴 **只读**：只读跑批产物，不写任何工程文件、不碰 qlib 数据。

用法::

    python3 manu/tools/v3_paired_segment_check.py \
        --results-dir /home/manu/mnt/data/siping/runs/etf_rotation_results/v3/_comparison_20261126 \
        --price-panel /home/manu/mnt/data/siping/datasets/manu/etf_rotation/datasets/etf_ext/etf_prices_merged_2018.csv
"""

from __future__ import annotations

import argparse
import csv
import json
import math
import sys
from pathlib import Path

DEFAULT_ARMS = [
    "control_v2",
    "v3_full",
    "v3_mse",
    "v3_no_neutralize",
    "v3_no_weight",
    "v3_no_rolling",
]
#: 报告里公布�� ``control_v2`` 段级统计（用于校验本工具复原出的分段口径是否逐位一致）
PUBLISHED_CONTROL = {"n_segments": 62, "t_stat": 1.07389688776821,
                     "mean_segment_excess": 0.005115492061154892,
                     "win_rate": 0.532258064516129}


# ----------------------------------------------------------------- 读取产物
def load_nav(results_dir: Path, arm: str) -> dict[str, float]:
    path = results_dir / f"nav_{arm}.csv"
    if not path.exists():
        raise FileNotFoundError(f"🔴 找不到净值文件：{path}")
    out: dict[str, float] = {}
    with path.open(encoding="utf-8-sig") as f:
        reader = csv.reader(f)
        next(reader, None)  # 表头
        for row in reader:
            if len(row) >= 2 and row[1]:
                out[row[0]] = float(row[1])
    return out


def load_bench(price_panel: Path, code: str = "510300") -> dict[str, float]:
    out: dict[str, float] = {}
    with price_panel.open(encoding="utf-8-sig") as f:
        for row in csv.DictReader(f):
            if row.get("code") == code:
                out[row["date"]] = float(row["close"])
    if not out:
        raise RuntimeError(f"🔴 基准 {code} 在面板里没有数据：{price_panel}")
    return out


def load_score_dates(results_dir: Path, arm: str) -> list[str]:
    """打分日（= 调仓候选日）—— 逐折 ``foldN_score_dates.txt``。"""
    dates: set[str] = set()
    for path in sorted(results_dir.glob(f"{arm}/{arm}/fold*_score_dates.txt")):
        dates |= {line.strip() for line in path.read_text(encoding="utf-8").splitlines()
                  if line.strip()}
    if not dates:
        raise FileNotFoundError(f"🔴 找不到打分日文件：{results_dir}/{arm}/{arm}/fold*_score_dates.txt")
    return sorted(dates)


# ----------------------------------------------------------------- 分段口径
def segment_returns(nav: dict[str, float], bench: dict[str, float],
                    cuts: list[str]) -> tuple[list[float], list[float]]:
    """按调仓期切段（口径与 ``segment_excess_stats`` 一致：段 = ``(上一调仓日, 本调仓日]``）。"""
    idx = sorted(nav)
    bounds = [idx[0]] + [c for c in sorted(cuts) if c in nav] + [idx[-1]]
    segs: list[float] = []
    bsegs: list[float] = []
    bench_idx = sorted(bench)
    for a, z in zip(bounds[:-1], bounds[1:]):
        win = [d for d in idx if a < d <= z]
        if len(win) < 2:
            continue
        segs.append(nav[win[-1]] / nav[win[0]] - 1.0)
        bwin = [d for d in bench_idx if a < d <= z]
        bsegs.append((bench[bwin[-1]] / bench[bwin[0]] - 1.0) if len(bwin) >= 2 else 0.0)
    return segs, bsegs


def t_stat(xs: list[float]) -> tuple[float, float, float]:
    """单样本 t、均值、样本标准差（ddof=1）。"""
    n = len(xs)
    if n < 2:
        return float("nan"), float("nan"), float("nan")
    mean = sum(xs) / n
    sd = math.sqrt(sum((x - mean) ** 2 for x in xs) / (n - 1))
    t = (mean / sd * math.sqrt(n)) if sd > 0 else float("nan")
    return t, mean, sd


def t_p_two_sided(t: float, df: int) -> float:
    """双侧 p 值（不完全 Beta 实现；本地无 scipy ⇒ 自实现，数值与 scipy 一致到 1e-9）。"""
    if not math.isfinite(t) or df <= 0:
        return float("nan")
    x = df / (df + t * t)
    return max(0.0, min(1.0, betai(df / 2.0, 0.5, x)))


def _betacf(a: float, b: float, x: float) -> float:
    maxit, eps, fpmin = 300, 3e-16, 1e-300
    qab, qap, qam = a + b, a + 1.0, a - 1.0
    c, d = 1.0, 1.0 - qab * x / qap
    if abs(d) < fpmin:
        d = fpmin
    d = 1.0 / d
    h = d
    for m in range(1, maxit + 1):
        m2 = 2 * m
        aa = m * (b - m) * x / ((qam + m2) * (a + m2))
        d = 1.0 + aa * d
        if abs(d) < fpmin:
            d = fpmin
        c = 1.0 + aa / c
        if abs(c) < fpmin:
            c = fpmin
        d = 1.0 / d
        h *= d * c
        aa = -(a + m) * (qab + m) * x / ((a + m2) * (qap + m2))
        d = 1.0 + aa * d
        if abs(d) < fpmin:
            d = fpmin
        c = 1.0 + aa / c
        if abs(c) < fpmin:
            c = fpmin
        d = 1.0 / d
        delta = d * c
        h *= delta
        if abs(delta - 1.0) < eps:
            break
    return h


def betai(a: float, b: float, x: float) -> float:
    if x <= 0.0:
        return 0.0
    if x >= 1.0:
        return 1.0
    lbeta = (math.lgamma(a + b) - math.lgamma(a) - math.lgamma(b)
             + a * math.log(x) + b * math.log(1.0 - x))
    bt = math.exp(lbeta)
    if x < (a + 1.0) / (a + b + 2.0):
        return bt * _betacf(a, b, x) / a
    return 1.0 - bt * _betacf(b, a, 1.0 - x) / b


def holm(pvals: dict[str, float]) -> dict[str, float]:
    """Holm–Bonferroni 校正后的 p 值（多臂实验必做 ⇒ §十四.150 铁律二）。"""
    items = [(k, v) for k, v in pvals.items() if math.isfinite(v)]
    items.sort(key=lambda kv: kv[1])
    m = len(items)
    out: dict[str, float] = {}
    running = 0.0
    for i, (key, p) in enumerate(items):
        adj = min(1.0, p * (m - i))
        running = max(running, adj)
        out[key] = running
    for key, p in pvals.items():
        if not math.isfinite(p):
            out[key] = float("nan")
    return out


# ----------------------------------------------------------------- 主流程
def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="V3 对照实验 · 成对段级统计复核（§十四.150）")
    parser.add_argument("--results-dir", required=True, help="跑批产物目录（含 nav_*.csv）")
    parser.add_argument("--price-panel", required=True, help="ETF 行情面板 CSV（基准收盘价来源）")
    parser.add_argument("--control", default="control_v2", help="对照臂名")
    parser.add_argument("--arms", nargs="*", default=DEFAULT_ARMS, help="参与统计的臂")
    parser.add_argument("--score-dates-arm", default=None,
                        help="打分日来源臂（默认取第一个存在的臂）")
    parser.add_argument("--bench-code", default="510300", help="基准 ETF 代码")
    parser.add_argument("--json-out", default=None, help="把结果写到这个 JSON 路径（可选）")
    args = parser.parse_args(argv)

    results_dir = Path(args.results_dir)
    navs = {arm: load_nav(results_dir, arm) for arm in args.arms}
    bench = load_bench(Path(args.price_panel), code=args.bench_code)

    dates_arm = args.score_dates_arm
    if dates_arm is None:
        for arm in args.arms:
            if list(results_dir.glob(f"{arm}/{arm}/fold*_score_dates.txt")):
                dates_arm = arm
                break
    if dates_arm is None:
        print("🔴 找不到任何 fold*_score_dates.txt ⇒ 无法复原分段", file=sys.stderr)
        return 2
    score_dates = load_score_dates(results_dir, dates_arm)

    # ---- 复原分段口径：用「公布值」反解 stride/phase，命中才算口径对齐 ----
    published = PUBLISHED_CONTROL
    best: tuple[float, int, int] | None = None
    for stride in range(1, 7):
        for phase in range(stride):
            segs, bsegs = segment_returns(navs[args.control], bench, score_dates[phase::stride])
            if len(segs) != published["n_segments"]:
                continue
            exc = [s - b for s, b in zip(segs, bsegs)]
            t, mean, _ = t_stat(exc)
            err = abs(t - published["t_stat"]) + abs(mean - published["mean_segment_excess"]) * 100
            if best is None or err < best[0]:
                best = (err, stride, phase)
    if best is None:
        print("🔴 未能复原 control_v2 的公布段级统计 ⇒ 分段口径与报告不一致 ⇒ 拒绝出结论",
              file=sys.stderr)
        return 3
    err, stride, phase = best
    cuts = score_dates[phase::stride]

    def segs_of(arm: str) -> tuple[list[float], list[float]]:
        return segment_returns(navs[arm], bench, cuts)

    ctrl_segs, ctrl_bsegs = segs_of(args.control)
    out: dict[str, object] = {
        "results_dir": str(results_dir),
        "caliber_check": {
            "stride": stride, "phase": phase, "abs_err": err,
            "matches_report": bool(err < 1e-6),
            "published": published,
        },
        "arms": {},
    }

    print(f"== 分段口径复原：stride={stride} phase={phase}｜误差 {err:.2e}"
          f"｜{'✅ 与报告逐位一致' if err < 1e-6 else '⚠️ 有偏差'}")
    print("\n== ① 单样本 t（原报告口径：臂 vs 基准，保留披露）")
    for arm in args.arms:
        segs, bsegs = segs_of(arm)
        exc = [s - b for s, b in zip(segs, bsegs)]
        t, mean, _ = t_stat(exc)
        win = sum(1 for e in exc if e > 0) / len(exc) if exc else float("nan")
        out["arms"][arm] = {"single_sample": {"t_stat": t, "mean_excess": mean, "win_rate": win}}
        print(f"  {arm:20s} n={len(exc):3d} t={t:+8.4f} mean_excess={mean:+.6f} win={win:.4f}")

    print("\n== ② 成对 t（臂 vs 对照臂，同分段配对）＋ Holm 校正")
    paired: dict[str, float] = {}
    for arm in args.arms:
        if arm == args.control:
            continue
        segs, _ = segs_of(arm)
        diff = [x - y for x, y in zip(segs, ctrl_segs)]
        t, mean, sd = t_stat(diff)
        n = len(diff)
        p = t_p_two_sided(abs(t), n - 1)
        win = sum(1 for x in diff if x > 0) / n if n else float("nan")
        paired[arm] = p
        out["arms"][arm]["paired"] = {"t_stat": t, "mean_diff": mean, "sd_diff": sd,
                                      "win_rate": win, "n_segments": n, "p_two_sided": p}
        print(f"  {arm:20s} n={n:3d} t={t:+8.4f} mean_diff={mean:+.6f} win={win:.4f} p={p:.4f}")
    adjusted = holm(paired)
    print("  ── Holm 多重比较校正（胜者诅咒必须披露，§十四.150 铁律二）")
    for arm, p in sorted(adjusted.items(), key=lambda kv: kv[1]):
        out["arms"][arm]["paired"]["p_holm"] = p
        print(f"  {arm:20s} p_holm={p:.4f} {'✅ 显著' if p < 0.05 else '（未达 5%）'}")

    if args.json_out:
        Path(args.json_out).write_text(json.dumps(out, ensure_ascii=False, indent=2),
                                       encoding="utf-8")
        print(f"\n[OK] 结果已写出：{args.json_out}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
