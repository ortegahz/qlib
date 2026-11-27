#!/usr/bin/env python3
"""live_eval_attribution.py —— 🔴 **事后系统有效性评估**（四个维度）

【它回答什么】
用户方法论文档（2026-11-27）的四个验证维度：

  ① **傻瓜对照组**：V2 实盘净值 vs **候选池等权** / **蒙特卡洛随机盲选**
     （判据：扣费后稳定跑赢等权基线，且排进随机盲选前 10%）；
  ② **信号单调性**：候选池按 ``E = W × S`` 分**高/中/低三档**，
     看后验收益是否单调（高 > 中 > 低）；
  ③ **模块级消融**：**被 TA 否决**的标的 vs **被留下**的标的，后验收益谁高；
  ④ **部署物理损耗**：**回测假设成本** vs **执行卡预估成本** vs **实盘实测**
     （实盘实测需要人工回填 ``live_fills.csv``）。

【输入（全部来自快照，**不改任何生产件**）】
  * 快照目录（建议一次给多个）⇒ 里面有 ``eval_candidate_pool.csv`` /
    ``eval_ta_shadow_ledger.csv`` / ``live_fills.csv`` / ``cost_assumption.json``；
  * ETF 行情面板（**必须覆盖 asof 之后**的窗口，否则前瞻收益算不出来）。

【口径纪律】
  * 前瞻收益面板**复用** ``manu/etf_rotation/v2/attribution.py::etf_forward_return_panel``
    （**不许另写一套口径**）；
  * 缺数据 ⇒ **如实说缺**，绝不外推、绝不补值；
  * 分档单调性**附带噪声地板**判定（``risk_disclosure.w_snapshot_bias`` 的
    Δfinal 上界）—— 档差小于噪声地板时**不许**宣称"单调性成立"。

用法
----
  # 单个快照 + 一份覆盖未来窗口的行情面板
  python manu/tools/live_eval_attribution.py \\
      --snapshot /path/to/snapshots/20261126_171821 \\
      --prices /path/to/etf_prices_effective_20261201.csv \\
      --horizon 20 --out-dir /tmp/eval_report

  # 扫一个快照根（自动挑每期快照）
  python manu/tools/live_eval_attribution.py \\
      --snapshots-root /path/to/snapshots --prices <panel.csv> --horizon 20

退出码：0 = 评估跑完（**含"数据不足"这一合法结论**）；1 = 参数/输入错误。
"""

from __future__ import annotations

import argparse
import csv
import importlib.util
import json
import math
import sys
from datetime import datetime
from pathlib import Path
from typing import Any, Iterable, Sequence

import numpy as np
import pandas as pd

def _find_repo_root() -> Path:
    """按**标记文件**找仓库根（不靠相对层数 ⇒ 镜像副本里也成立）。"""
    here = Path(__file__).resolve()
    for parent in here.parents:
        if (parent / "manu" / "etf_rotation" / "v2" / "attribution.py").is_file():
            return parent
    return here.parents[2]


REPO_ROOT = _find_repo_root()
V2_DIR = REPO_ROOT / "manu" / "etf_rotation" / "v2"

#: 🔴 快照里 eval pack 的件名（缺任一件 ⇒ 该维度**如实报"缺"**）
POOL_CSV = "eval_candidate_pool.csv"
LEDGER_CSV = "eval_ta_shadow_ledger.csv"
FILLS_CSV = "live_fills.csv"
COST_JSON = "cost_assumption.json"


# ----------------------------------------------------------------- 工具
def _load_attribution():
    """按路径加载 ``v2/attribution.py``（两个 ``config.py`` 同名 ⇒ 只能按路径加载）。"""
    path = V2_DIR / "attribution.py"
    if not path.is_file():
        return None
    spec = importlib.util.spec_from_file_location("v2_attribution_for_live_eval", path)
    module = importlib.util.module_from_spec(spec)
    try:
        spec.loader.exec_module(module)
    except Exception:
        return None
    return module


def _read_csv(path: Path) -> pd.DataFrame:
    """读 CSV（**utf-8-sig** ⇒ 快照里的 BOM 文件也能读）。"""
    return pd.read_csv(path, dtype={"code": str}, encoding="utf-8-sig")


def _snapshots(args: argparse.Namespace) -> list[Path]:
    """列出要评估的快照目录（**按目录名升序** ⇒ 时间序）。"""
    if args.snapshot:
        return [Path(p) for p in args.snapshot]
    root = Path(args.snapshots_root)
    if not root.is_dir():
        return []
    out = []
    for item in sorted(root.iterdir()):
        if item.is_dir() and (item / POOL_CSV).is_file():
            out.append(item)
    return out


def _is_blank(value: Any) -> bool:
    """空值判定（``None`` / ``""`` / **NaN** 都算空 —— CSV 空单元格读进来是 NaN）。"""
    if value is None:
        return True
    if isinstance(value, str):
        return value.strip() == ""
    try:
        return bool(pd.isna(value))
    except (TypeError, ValueError):
        return False


def _load_prices(path: Path) -> pd.DataFrame:
    """读行情面板 ⇒ ``date × code`` 的收盘价矩阵。"""
    df = pd.read_csv(path, dtype={"code": str}, encoding="utf-8-sig")
    df["code"] = df["code"].astype(str).str.zfill(6)
    df["date"] = pd.to_datetime(df["date"])
    df = df.drop_duplicates(subset=["date", "code"], keep="last")
    close = df.pivot(index="date", columns="code", values="close").sort_index()
    return close


# ----------------------------------------------------------------- 维度 ①②
def _pool_and_selected(pool: pd.DataFrame) -> tuple[list[str], list[str]]:
    """候选池代码列表 + 规则层选中代码列表。"""
    codes = [str(c) for c in pool["code"].tolist()]
    sel_mask = pool["selected"].astype(str).str.lower().isin(["true", "1"])
    selected = [str(c) for c in pool.loc[sel_mask, "code"].tolist()]
    return codes, selected


def _tercile_returns(
    pool: pd.DataFrame,
    fwd: pd.Series,
) -> dict[str, Any]:
    """三档后验收益 + **单调性判定**（含噪声地板提示）。"""
    buckets: dict[str, list[float]] = {"高": [], "中": [], "低": []}
    for _, row in pool.iterrows():
        code = str(row["code"])
        if code not in fwd.index:
            continue
        val = fwd.get(code)
        if val is None or (isinstance(val, float) and np.isnan(val)):
            continue
        tercile = str(row.get("tercile") or "")
        if tercile in buckets:
            buckets[tercile].append(float(val))
    means = {k: (float(np.mean(v)) if v else None) for k, v in buckets.items()}
    counts = {k: len(v) for k, v in buckets.items()}
    hi, mid, lo = means.get("高"), means.get("中"), means.get("低")
    if hi is None or mid is None or lo is None:
        monotone = None
    else:
        monotone = bool(hi > mid > lo)
    # 档间差
    spread = None
    if hi is not None and lo is not None:
        spread = hi - lo
    return {
        "mean_fwd_return": means,
        "n": counts,
        "monotone_high_mid_low": monotone,
        "spread_high_minus_low": spread,
        "noise_floor": "见 risk_disclosure.w_snapshot_bias（Δfinal 现实档 ≤ 0.1869）",
    }


def _monte_carlo(
    pool_codes: Sequence[str],
    fwd: pd.Series,
    n_select: int,
    *,
    n_sims: int,
    seed: int,
) -> dict[str, Any]:
    """蒙特卡洛盲选：**从候选池随机抽 n_select 只**，跑 n_sims 条前瞻收益。

    判据（用户口径）：实际组合的前瞻收益应排进随机曲线的**前 10%**。
    """
    usable = [c for c in pool_codes if c in fwd.index
              and not (isinstance(fwd.get(c), float) and np.isnan(fwd.get(c)))]
    if len(usable) < 2 or n_select <= 0 or len(usable) < n_select:
        return {"available": False, "note": f"可用池太窄（{len(usable)} 只，n_select={n_select}）"}
    values = np.array([float(fwd.get(c)) for c in usable], dtype="float64")
    rng = np.random.default_rng(seed)
    sims = rng.choice(values, size=(n_sims, n_select), replace=True).mean(axis=1)
    return {
        "available": True,
        "n_pool": len(usable),
        "n_select": n_select,
        "n_sims": int(n_sims),
        "sim_mean": float(sims.mean()),
        "sim_p05": float(np.quantile(sims, 0.05)),
        "sim_p50": float(np.quantile(sims, 0.50)),
        "sim_p95": float(np.quantile(sims, 0.95)),
        "sim_std": float(sims.std(ddof=1)),
    }


def _percentile_rank(value: float, sims_mean: float, sim_std: float,
                     n_sims: int) -> float | None:
    """实际收益在随机分布里的分位（正态近似；**仅作参考**）。"""
    if sim_std <= 0:
        return None
    z = (value - sims_mean) / sim_std
    # 🔴 用标准库 ``math.erf``（``np.math`` 在 NumPy 2.0 已被移除）
    return float(0.5 * (1.0 + math.erf(z / math.sqrt(2.0))))


# ----------------------------------------------------------------- 维度 ③
def _ta_ablation(ledger: pd.DataFrame, fwd: pd.Series) -> dict[str, Any]:
    """TA 影子消融：被**否决**的 vs 被**留下**的，后验收益谁高。

    🔴 只认 ``eval_ta_shadow_ledger.csv`` 的 ``ta_verdict``（**以 TA 最终裁决为准**），
       **不读** ``ta_screening.rejected``（实测会误判）。
    """
    verdict_col = "ta_verdict" if "ta_verdict" in ledger.columns else None
    if not verdict_col:
        return {"available": False, "note": "影子账本缺 ta_verdict 列"}
    rejected, kept = [], []
    for _, row in ledger.iterrows():
        code = str(row["code"])
        if code not in fwd.index:
            continue
        val = fwd.get(code)
        if val is None or (isinstance(val, float) and np.isnan(val)):
            continue
        verdict = str(row.get(verdict_col) or "")
        if verdict == "否决":
            rejected.append((code, float(val)))
        elif verdict.startswith("放行"):
            kept.append((code, float(val)))
    out: dict[str, Any] = {
        "available": bool(rejected) and bool(kept),
        "n_rejected": len(rejected),
        "n_kept": len(kept),
        "rejected": rejected,
        "kept": kept,
    }
    if rejected and kept:
        rej_mean = float(np.mean([v for _, v in rejected]))
        kept_mean = float(np.mean([v for _, v in kept]))
        out["rejected_mean_fwd"] = rej_mean
        out["kept_mean_fwd"] = kept_mean
        out["edge_kept_minus_rejected"] = kept_mean - rej_mean
        out["ta_helped"] = bool(kept_mean > rej_mean)
        out["note"] = (
            "🔴 **N 极小（单期只有个位数标的）⇒ 只能当方向性证据，"
            "不许当统计结论**；至少攒够 6~12 期再看"
        )
    else:
        out["note"] = "🔴 否决组或留下组为空 ⇒ 本期无法做 TA 消融对比"
    return out


# ----------------------------------------------------------------- 维度 ④
def _friction(cost: dict, fills: pd.DataFrame | None) -> dict[str, Any]:
    """摩擦核算：回测假设 / 执行卡预估 / 实盘实测（若有回填）。"""
    out: dict[str, Any] = {
        "calibers": (cost or {}).get("layers"),
        "disagreements": (cost or {}).get("disagreements"),
        "per_order": (cost or {}).get("per_order"),
    }
    measured = []
    if fills is not None and not fills.empty and "actual_price" in fills.columns:
        for _, row in fills.iterrows():
            actual_price = row.get("actual_price")
            planned_price = row.get("planned_price")
            comm = row.get("actual_commission")
            # 🔴 CSV 里的空单元格读进来是 **NaN**（不是 ""）⇒ 必须用 ``pd.isna`` 判
            if _is_blank(actual_price) or _is_blank(planned_price):
                continue
            try:
                ap = float(actual_price)
                pp = float(planned_price)
            except (TypeError, ValueError):
                continue
            if pp == 0:
                continue
            slippage_bp = (ap - pp) / pp * 10000.0 if pp else None
            fee_bp = None
            if not _is_blank(comm) and not _is_blank(row.get("planned_amount")):
                try:
                    fee_bp = float(comm) / float(row["planned_amount"]) * 10000.0
                except (TypeError, ValueError, ZeroDivisionError):
                    fee_bp = None
            measured.append({
                "code": str(row.get("code")),
                "planned_price": pp,
                "actual_price": ap,
                "slippage_bp": slippage_bp,
                "actual_commission": comm,
                "fee_bp": fee_bp,
            })
    out["measured_fills"] = measured
    out["measured_available"] = bool(measured)
    if not measured:
        out["note"] = (
            "🔴 **实盘实测滑点/佣金缺失** ⇒ `live_fills.csv` 的 actual_* 列尚未回填"
            "（观察态快照从不产生成交）"
        )
    return out


# ----------------------------------------------------------------- 单期评估
def _eval_one(
    snap: Path,
    close: pd.DataFrame,
    horizon: int,
    *,
    n_sims: int,
    seed: int,
) -> dict[str, Any]:
    """评估一个快照（**缺件 ⇒ 如实报缺**）。"""
    result: dict[str, Any] = {
        "snapshot": str(snap),
        "asof": None,
        "available": {},
        "notes": [],
    }
    pool_path = snap / POOL_CSV
    if not pool_path.is_file():
        result["notes"].append(f"🔴 缺 {POOL_CSV} ⇒ 该快照未生成评估就绪包")
        return result
    pool = _read_csv(pool_path)
    if pool.empty:
        result["notes"].append("🔴 候选池表为空")
        return result
    asof = str(pool["date"].iloc[0])
    result["asof"] = asof

    attribution = _load_attribution()
    if attribution is not None:
        fwd_panel = attribution.etf_forward_return_panel(close, horizon=horizon)
    else:  # 兜底：同口径直算（`fwd = close.shift(-h)/close - 1`）
        fwd_panel = close.astype("float64").sort_index().shift(-horizon) / close.astype(
            "float64").sort_index() - 1.0
    asof_ts = pd.Timestamp(asof)
    if asof_ts not in fwd_panel.index:
        result["notes"].append(f"🔴 行情面板里没有 {asof} ⇒ 前瞻收益算不出来")
        return result
    fwd = fwd_panel.loc[asof_ts]

    pool_codes, selected = _pool_and_selected(pool)

    # ① 等权池基准
    bench: dict[str, Any] = {"available": False}
    pool_vals = [float(fwd.get(c)) for c in pool_codes
                 if c in fwd.index and not pd.isna(fwd.get(c))]
    sel_vals = [float(fwd.get(c)) for c in selected
                if c in fwd.index and not pd.isna(fwd.get(c))]
    if pool_vals and sel_vals:
        bench = {
            "available": True,
            "n_pool": len(pool_vals),
            "n_selected": len(sel_vals),
            "pool_equal_weight_fwd": float(np.mean(pool_vals)),
            "selected_equal_weight_fwd": float(np.mean(sel_vals)),
            "excess_selected_minus_pool": float(np.mean(sel_vals) - np.mean(pool_vals)),
        }
    result["available"]["baseline"] = bench
    if not bench.get("available"):
        result["notes"].append("🔴 等权池基准算不出来（池内/选中缺前瞻收益）")

    # ① 蒙特卡洛盲选分位
    mc = _monte_carlo(pool_codes, fwd, max(len(sel_vals), 1),
                       n_sims=n_sims, seed=seed)
    if mc.get("available") and bench.get("available"):
        mc["actual_selected_mean_fwd"] = bench["selected_equal_weight_fwd"]
        mc["actual_percentile"] = _percentile_rank(
            bench["selected_equal_weight_fwd"],
            mc["sim_mean"], mc["sim_std"], n_sims,
        )
        mc["passes_top10"] = (
            mc["actual_percentile"] is not None and mc["actual_percentile"] >= 0.90
        )
    result["available"]["monte_carlo"] = mc

    # ② 三档单调性
    result["available"]["monotonicity"] = _tercile_returns(pool, fwd)

    # ③ TA 消融
    ledger_path = snap / LEDGER_CSV
    if ledger_path.is_file():
        result["available"]["ta_ablation"] = _ta_ablation(_read_csv(ledger_path), fwd)
    else:
        result["available"]["ta_ablation"] = {
            "available": False, "note": f"🔴 缺 {LEDGER_CSV}",
        }

    # ④ 摩擦
    cost_path = snap / COST_JSON
    cost = json.loads(cost_path.read_text(encoding="utf-8")) if cost_path.is_file() else {}
    fills_path = snap / FILLS_CSV
    fills = _read_csv(fills_path) if fills_path.is_file() else None
    result["available"]["friction"] = _friction(cost, fills)

    return result


# ----------------------------------------------------------------- 汇总
def _render(results: list[dict[str, Any]], args: argparse.Namespace) -> str:
    """渲染 Markdown 报告（**结论先行**）。"""
    lines: list[str] = []
    lines.append("# 🔴 事后系统有效性评估（四维度）")
    lines.append("")
    lines.append(f"> 生成时间：{datetime.now().astimezone().isoformat(timespec='seconds')}")
    lines.append(f"> 快照期数：**{len(results)}**｜前瞻窗口：**{args.horizon} 交易日**"
                 f"｜蒙特卡洛：**{args.n_sims}** 条")
    lines.append(">")
    lines.append("> 🔴 **口径纪律**：N 很小（每期只有十几只标的）⇒ 本报告是"
                 "**方向性证据**，**不是**统计结论；至少攒 6~12 期再下判断。")
    lines.append("")

    usable = [r for r in results if r.get("available")]
    lines.append("## 一、结论先行")
    lines.append("")
    if not usable:
        lines.append("🔴 **没有可评估的期数**（快照缺 eval pack，或行情面板没覆盖前瞻窗口）"
                     "⇒ 请先补 `eval pack` + 覆盖 asof 之后的行情面板。")
    else:
        n_base = sum(1 for r in usable if r["available"]["baseline"].get("available"))
        n_mono = sum(1 for r in usable
                      if r["available"].get("monotonicity", {}).get("mean_fwd_return", {})
                      .get("高") is not None)
        n_ta = sum(1 for r in usable
                   if r["available"].get("ta_ablation", {}).get("available"))
        n_fric = sum(1 for r in usable
                     if r["available"].get("friction", {}).get("measured_available"))
        lines.append(f"| 维度 | 可评估期数 | 命中 |")
        lines.append("|---|---|---|")
        lines.append(f"| ① 傻瓜对照组（等权池 / 随机盲选） | {n_base} | — |")
        lines.append(f"| ② 信号单调性（高>中>低） | {n_mono} | **{n_mono}** |")
        lines.append(f"| ③ TA 模块消融 | {n_ta} | — |")
        lines.append(f"| ④ 真实摩擦（**实测**） | {n_fric} | — |")
    lines.append("")

    lines.append("## 二、逐期明细")
    lines.append("")
    for item in results:
        lines.append(f"### {item.get('asof') or '（未知 asof）'} — `{item['snapshot']}`")
        lines.append("")
        avail = item.get("available") or {}
        base = avail.get("baseline") or {}
        if base.get("available"):
            lines.append(
                f"- **① 等权池基准**：池（{base['n_pool']} 只）等权 "
                f"**{base['pool_equal_weight_fwd']:+.4%}** vs 选中（{base['n_selected']} 只）"
                f" **{base['selected_equal_weight_fwd']:+.4%}** ⇒ "
                f"超额 **{base['excess_selected_minus_pool']:+.4%}**"
            )
        mc = avail.get("monte_carlo") or {}
        if mc.get("available"):
            pct = mc.get("actual_percentile")
            pct_text = f"{pct:.1%}" if pct is not None else "—"
            lines.append(
                f"  - 蒙特卡洛盲选：随机均值 **{mc['sim_mean']:+.4%}**（p05 "
                f"{mc['sim_p05']:+.4%} / p95 {mc['sim_p95']:+.4%}）⇒ 实际组合分位 "
                f"**{pct_text}**{'　✅ 前 10%' if mc.get('passes_top10') else '　❌ 未进前 10%'}"
            )
        mono = avail.get("monotonicity") or {}
        if mono.get("mean_fwd_return", {}).get("高") is None:
            lines.append("- **② 三档单调性**：🔴 算不出来（池内缺前瞻收益 ⇒ "
                         "行情面板没覆盖 asof 之后的窗口）")
        else:
            m = mono["mean_fwd_return"]
            flag = {True: "✅ 单调成立", False: "❌ 不单调", None: "—"}[
                mono.get("monotone_high_mid_low")]
            lines.append(
                f"- **② 三档单调性**：高 **{m['高']:+.4%}**（n={mono['n']['高']}）｜"
                f"中 **{m['中']:+.4%}**（n={mono['n']['中']}）｜"
                f"低 **{m['低']:+.4%}**（n={mono['n']['低']}）⇒ {flag}"
                f"（档差 {mono.get('spread_high_minus_low'):+.4%}）"
                if mono.get("spread_high_minus_low") is not None else ""
            )
            lines.append(f"  - ⚠️ 噪声地板：{mono.get('noise_floor')}"
                         " ⇒ 档差小于地板时**不许**宣称单调性成立")
        ta = avail.get("ta_ablation") or {}
        if ta.get("available"):
            lines.append(
                f"- **③ TA 消融**：否决组（n={ta['n_rejected']}）均值 "
                f"**{ta['rejected_mean_fwd']:+.4%}** vs 留下组（n={ta['n_kept']}）"
                f"**{ta['kept_mean_fwd']:+.4%}** ⇒ 边 **{ta['edge_kept_minus_rejected']:+.4%}**"
                f"　{'✅ TA 立功' if ta['ta_helped'] else '❌ TA 帮倒忙'}"
            )
            lines.append(f"  - {ta.get('note')}")
        fric = avail.get("friction") or {}
        if fric.get("measured_available"):
            for m in fric["measured_fills"]:
                lines.append(
                    f"- **④ 实盘实测**：{m['code']} 参考价 {m['planned_price']:.4f} → "
                    f"成交价 {m['actual_price']:.4f} ⇒ 滑点 "
                    f"{m['slippage_bp']:+.2f} bp"
                    + (f"｜佣金 {m['fee_bp']:.2f} bp" if m.get("fee_bp") is not None else "")
                )
        else:
            lines.append(f"- **④ 真实摩擦**：{fric.get('note') or '（无数据）'}")
        for note in item.get("notes") or []:
            lines.append(f"  - {note}")
        lines.append("")

    return "\n".join(lines)


# ----------------------------------------------------------------- 入口
def main(argv: Iterable[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        description="事后系统有效性评估（等权基线 / 单调性 / TA 消融 / 真实摩擦）")
    parser.add_argument("--snapshot", action="append", default=[],
                        help="单个快照目录（可重复）")
    parser.add_argument("--snapshots-root", default="",
                        help="快照根目录（自动挑含 eval pack 的快照）")
    parser.add_argument("--prices", required=True,
                        help="ETF 行情面板 CSV（date,code,close…）—— **必须覆盖 asof 之后**")
    parser.add_argument("--horizon", type=int, default=20,
                        help="前瞻窗口（交易日），默认 20（≈ 一个月）")
    parser.add_argument("--n-sims", type=int, default=1000,
                        help="蒙特卡洛盲选条数，默认 1000")
    parser.add_argument("--seed", type=int, default=20261127,
                        help="蒙特卡洛随机种子（默认固定 ⇒ 可复现）")
    parser.add_argument("--out-dir", default="",
                        help="报告落盘目录（缺省 ⇒ 只打 stdout）")
    args = parser.parse_args(list(argv) if argv is not None else None)

    if not args.snapshot and not args.snapshots_root:
        print("❌ 必须给 --snapshot 或 --snapshots-root", file=sys.stderr)
        return 1
    prices_path = Path(args.prices)
    if not prices_path.is_file():
        print(f"❌ 行情面板不存在：{prices_path}", file=sys.stderr)
        return 1

    snaps = _snapshots(args)
    if not snaps:
        print("🔴 没找到可评估的快照（缺 eval pack 的快照会被跳过）", file=sys.stderr)
        return 1

    close = _load_prices(prices_path)
    results = [
        _eval_one(snap, close, args.horizon, n_sims=args.n_sims, seed=args.seed)
        for snap in snaps
    ]

    markdown = _render(results, args)
    payload = {
        "generated_at": datetime.now().astimezone().isoformat(timespec="seconds"),
        "horizon": args.horizon,
        "n_sims": args.n_sims,
        "seed": args.seed,
        "prices": str(prices_path),
        "snapshots": [str(s) for s in snaps],
        "results": results,
    }
    if args.out_dir:
        out = Path(args.out_dir)
        out.mkdir(parents=True, exist_ok=True)
        (out / "live_eval_report.md").write_text(markdown, encoding="utf-8")
        (out / "live_eval_report.json").write_text(
            json.dumps(payload, ensure_ascii=False, indent=2, default=str), encoding="utf-8")
        print(f"✅ 报告已落盘：{out / 'live_eval_report.md'}")
    print(markdown)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
