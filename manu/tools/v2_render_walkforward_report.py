#!/usr/bin/env python3
"""把 5 折滚动前推的产物渲染成**中文验收报告**（Markdown）。

输入（探针产物）：``<root>/<subdir>/`` 下
  * ``walkforward_summary.json``（切分摘要 / 最优配置 / 面板覆盖 / 披露项）
  * ``oos_grid.csv``（24 组配置的 OOS 指标全表）
  * ``per_fold.csv``（最优配置的分折明细）
输出：``<root>/V2_WALKFORWARD_REPORT_20261118.md``

🔴 纪律：报告里**只许**出现 2019–2023 的 5 折 OOS 数字；
测试段（2024–2026）单盲已消耗 ⇒ **一个字都不许出现**。
"""

from __future__ import annotations

import argparse
import json
import math
import sys
from pathlib import Path

import pandas as pd

#: 🔴 单边换手换算的**唯一实现**（``config_v2.one_way_turnover``）——不许手写系数。
_V2_DIR = Path(__file__).resolve().parents[1] / "etf_rotation" / "v2"
if str(_V2_DIR) not in sys.path:
    sys.path.insert(0, str(_V2_DIR))
import config_v2  # noqa: E402


def _fmt(x, pct: bool = True, nd: int = 2) -> str:
    if x is None or (isinstance(x, float) and pd.isna(x)):
        return "—"
    try:
        v = float(x)
    except (TypeError, ValueError):
        return str(x)
    return f"{v * 100:.{nd}f}%" if pct else f"{v:.{nd}f}"


def _one_way(x) -> float | None:
    """双边换手 → 单边换手（**单边 = 0.5 × 双边**；缺失 ⇒ None ⇒ 渲染成「—」）。

    口径与 Top-K 线同源（``0.5 × Σ|Δw|`` ⇒ Σ|Δw| 是双边）。
    🔴 换算**只走唯一实现** ``config_v2.one_way_turnover``（不许手写系数）；
    **「缺失 ⇒ None ⇒ 渲染成「—」」的既有语义保持不变**（非有限 / 非法 ⇒ 同样 None）。
    """
    if x is None or (isinstance(x, float) and pd.isna(x)):
        return None
    try:
        turn = float(x)
    except (TypeError, ValueError):
        return None
    if not math.isfinite(turn):
        return None
    return config_v2.one_way_turnover(turn)


def main() -> int:
    ap = argparse.ArgumentParser(description="渲染 5 折滚动前推验收报告")
    ap.add_argument("--root", default="/mnt/data/siping/runs/etf_rotation_results/v2")
    ap.add_argument("--subdir", default="_walkforward_ext2018")
    ap.add_argument("--out", default="")
    args = ap.parse_args()

    root = Path(args.root)
    sub = root / args.subdir
    summary = json.loads((sub / "walkforward_summary.json").read_text(encoding="utf-8"))
    grid = pd.read_csv(sub / "oos_grid.csv")
    per_fold = pd.read_csv(sub / "per_fold.csv")
    best = summary.get("best") or {}
    sp = summary.get("split_summary") or {}
    cov = summary.get("price_panel_coverage") or {}

    lines: list[str] = []
    a = lines.append
    a("# 🔴 V2 · 5 折滚动前推（expanding window）验收报告")
    a("")
    a(f"- 生成时间：{summary.get('generated_at')}")
    a(f"- OOS 窗口：**{sp.get('oos_window')}**｜折数：{sp.get('n_folds')}")
    a(f"- 目标函数：{summary.get('objective')}")
    # 🔴 2026-11-18：产物里的 ``objective`` 串若是**改版前**的措辞（换手惩罚未封顶、
    #    4.0 还是硬红线）⇒ **不许**把它当现行口径念 ⇒ 显式披露差异。
    _obj = str(summary.get("objective") or "")
    if _obj and ("0.05" in _obj or "4.0" in _obj) and "封顶" not in _obj:
        a("- 🔴 **口径披露**：本产物生成于**换手惩罚封顶改造之前**"
          "（⇒ 其 `objective` 串仍是旧公式「− 0.05 × max(0, 换手 − 4.0)」，"
          "**选参 score 被旧惩罚污染**）；现行口径 = 惩罚**封顶** + 4.0× **只作披露基准**"
          "（见 `config_v2.OBJECTIVE_DESCRIPTION`）⇒ 数字（年化/回撤/换手）**不受影响**，"
          "但**排序口径已变** ⇒ 重跑后才有可比的新 `score`。")
    a(f"- 网格规模：{(summary.get('grid') or {}).get('n_configs')} 组"
      f"（cadence={((summary.get('grid') or {}).get('locked') or {}).get('rebalance_every_n_days')}"
      f" / top_k={((summary.get('grid') or {}).get('locked') or {}).get('top_k')} **已焊死**）")
    a("")

    a("## 一、切分表（embargo 25 交易日，从验证段起点倒推）")
    a("")
    a("| 折 | 训练段 | 训练末日(名义) | 标签窗口结束 | 验证年 | 交易日数 |")
    a("|---|---|---|---|---|---|")
    for f in sp.get("folds") or []:
        a(f"| {f.get('fold_id')} | {f.get('train_start')} ~ {f.get('train_end')} "
          f"| {f.get('train_end_raw')} | {f.get('last_train_label_window_end')} "
          f"| {f.get('valid_start')} ~ {f.get('valid_end')} | {f.get('n_valid_days')} |")
    a("")

    a("## 二、ETF 行情面板口径")
    a("")
    a(f"- 覆盖区间：**{cov.get('first_date')} ~ {cov.get('last_date')}**"
      f"（{cov.get('n_days')} 个交易日）")
    a(f"- 说明：{cov.get('note')}")
    a(f"- 作废折：{summary.get('void_folds') or '无'}")
    a("")

    a("## 三、最优配置（按目标函数）")
    a("")
    p = best.get("params") or {}
    a(f"- 方案：**{best.get('arm')}**（{best.get('arm_desc')}）")
    a(f"- 缓冲带 hold_buffer = **{best.get('hold_buffer')}**｜负分闸门 = **{best.get('gate')}**")
    a(f"- 目标函数分：**{_fmt(best.get('score'), False, 4)}**")
    a("")
    a("| 指标 | 值 |")
    a("|---|---|")
    om = best.get("oos_metrics") or {}
    a(f"| 年化收益 | {_fmt(om.get('annualized_return'))} |")
    a(f"| 最大回撤 | {_fmt(om.get('max_drawdown'))} |")
    a(f"| 夏普 | {_fmt(om.get('sharpe'), False)} |")
    a(f"| 年化超额（几何，扣费后） | {_fmt(om.get('excess_vs_benchmark'))} |")
    a(f"| 年化换手（双边） | {_fmt(om.get('annualized_turnover'), False)}× |")
    a(f"| 年化换手（单边）= 双边 × 0.5 | "
      f"{_fmt(_one_way(om.get('annualized_turnover')), False)}× |")
    a(f"| 成本拖累（累计） | {_fmt(om.get('total_cost_drag'))} |")
    a(f"| ETF 层 20 日 RankIC | {_fmt(best.get('etf_rank_ic'), False, 4)} |")
    a("")

    if len(per_fold):
        pf = per_fold.copy()
        if best.get("arm"):
            pf = pf[(pf["arm"] == best.get("arm"))
                    & (pf["hold_buffer"] == best.get("hold_buffer"))
                    & (pf["gate"] == best.get("gate"))]
        a("### 分折明细")
        a("")
        a("| 折 | 年 | 年化 | 回撤 | 年化换手（双边） | 调仓次数 |")
        a("|---|---|---|---|---|---|")
        for _, r in pf.sort_values("fold_id").iterrows():
            a(f"| {r.get('fold_id')} | {r.get('year')} | {_fmt(r.get('annualized_return'))} "
              f"| {_fmt(r.get('max_drawdown'))} | {_fmt(r.get('annualized_turnover'), False)}× "
              f"| {r.get('n_rebalances')} |")
        a("")

    a("## 四、网格全表（按目标函数分降序）")
    a("")
    cols = [c for c in ("arm", "hold_buffer", "gate", "annualized_return", "max_drawdown",
                        "annualized_turnover", "excess_vs_benchmark", "etf_rank_ic", "score")
            if c in grid.columns]
    # 🔴 2026-11-18 换手口径正名：``annualized_turnover`` = **双边**（Σ|Δw|），
    #    ``annualized_turnover_one_way`` = **单边**（= 0.5 × 双边）
    #    ⇒ 表头把双边 / 单边**标出来**，并把单边**新增**成并列的一列。
    #    单边值优先取 CSV 里已有的 ``annualized_turnover_one_way`` 列；
    #    老 CSV 无该列 ⇒ 退回「双边 × 0.5」（纯单位换算，不是重算指标）。
    _HEAD = {"annualized_turnover": "年化换手（双边）",
             "annualized_turnover_one_way": "年化换手（单边）"}
    _has_one_way_col = "annualized_turnover_one_way" in grid.columns
    display: list[tuple[str, str]] = []
    for c in cols:
        display.append((c, _HEAD.get(c, c)))
        if c == "annualized_turnover":
            display.append(("annualized_turnover_one_way", _HEAD["annualized_turnover_one_way"]))
    a("| " + " | ".join(head for _, head in display) + " |")
    a("|" + "---|" * len(display))
    for _, r in grid.sort_values("score", ascending=False).iterrows():
        vals = []
        for c, _head in display:
            if c == "annualized_turnover_one_way":
                one = (r.get(c) if _has_one_way_col
                       else _one_way(r.get("annualized_turnover")))
                vals.append(_fmt(one, False, 4))
            elif c in ("arm", "gate"):
                vals.append(str(r.get(c)))
            elif c == "hold_buffer":
                vals.append(str(r.get(c)))
            elif c in ("annualized_return", "max_drawdown", "excess_vs_benchmark"):
                vals.append(_fmt(r.get(c)))
            else:
                vals.append(_fmt(r.get(c), False, 4))
        a("| " + " | ".join(vals) + " |")
    a("")

    a("## 五、披露项（不掩盖）")
    a("")
    for item in (summary.get("disclosure") or []) if isinstance(summary.get("disclosure"), list) \
            else ([summary.get("disclosure")] if summary.get("disclosure") else []):
        a(f"- {item}")
    a("- 🔴 **测试段（2024–2026）单盲已消耗** ⇒ 本报告**不含**任何测试段数字，"
      "候选参数**不得**回头用它验收。")
    a("")

    a("## 六、下一步")
    a("")
    a("1. 用本报告的候选参数**冻结**成 `frozen_params_v2_walkforward.json`；")
    a("2. **等一段新的前向窗口**（2026-11 之后的新数据）做**新的盲测**；")
    a("3. 或者由用户显式授权**解封测试段**重跑一次（会作废现有单盲记录，须留痕）。")
    a("")

    out = Path(args.out) if args.out else root / "V2_WALKFORWARD_REPORT_20261118.md"
    out.write_text("\n".join(lines) + "\n", encoding="utf-8")
    print(f"[OK] 报告已写出：{out}（{len(lines)} 行）")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
