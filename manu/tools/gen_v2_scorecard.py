#!/usr/bin/env python
"""🔴 生成 V2 成绩单（对照 V1 baseline）。

用法：
    /home/huangzhe/anaconda3/envs/ql/bin/python manu/tools/gen_v2_scorecard.py \\
        [--v2-root /mnt/data/siping/runs/etf_rotation_results/v2] \\
        [--out <md 落点>] [--copy-to <第二落点>]

口径要点（**必须原样出现在成绩单里**，不许只报好看的那个）：
  * 换手：**V1 与 V2 都是 Σ|Δw| 双边口径**（V1 16.57× / V2 双边与单边并列给出）；
    但**换手来源不同源**：V2 = 「目标权重差（含 Rank Buffer 留存量）」，
    V1 baseline = 「上期目标权重未漂移」⇒ 数值**只做趋势性对比**。
    ⇒ 表格**并列**给出双边与单边（单边只走唯一实现 `config_v2.one_way_turnover`）；
    换手 ≤4× 现为**披露基准**（2026-11-18 起不再是选参硬门槛），未达标也**如实披露**。
  * Sharpe：V2 = 标准口径 `mean(超额)/std(ddof=1)·√ppy`；V1 `backtest.py:305` 是粗口径
    （年化/年化波动）⇒ 对比表标注用的是哪一个。
  * embargo：实际 25 个交易日（> 用户下限 20，因标签末端 t+25）⇒ 属收紧，须披露。
  * 成分股：只有 2026-11-04 单快照 ⇒ 回测用 2026 权重倒推历史，须标注口径。
"""
from __future__ import annotations

import math
import argparse
import json
import os
import sys
from pathlib import Path

#: 🔴 工程根：**优先**读环境变量 ``ETF_ROTATION_REPO_ROOT``（换 checkout 路径也能跑）
#:    ⇒ 缺省值保持既有服务器路径**不变**（行为兼容，不许改默认值）。
#:    同一约定见 ``manu/etf_rotation/daily/daily_config.py::REPO_ROOT``。
PROJ = Path(os.environ.get("ETF_ROTATION_REPO_ROOT", "/tmp/pycharm_project_3b68a403"))
DEFAULT_V2_ROOT = Path("/mnt/data/siping/runs/etf_rotation_results/v2")


def _load(path: Path):
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except Exception:
        return None


def _fmt(value, digits: int = 4, pct: bool = True) -> str:
    if value is None:
        return "—"
    if isinstance(value, str):
        return value
    if not isinstance(value, (int, float)):
        return str(value)
    # 🔴 2026-11-18 续四（reviewer NIT-4）：**NaN / ±inf 也要渲染成「—」**
    #    ⇒ 换手口径改成「双边算不出来 ⇒ 单边也是 NaN」之后，
    #      `f"{nan:.4f}"` 会把**字面量 nan** 写进报告（读者会当成数字 0）。
    #    ⇒ 与 `v2/report.py::_fmt` / `daily/report.py` 的既有约定一致。
    if not math.isfinite(float(value)):
        return "—"
    if pct:
        return f"{value * 100:.2f}%"
    return f"{value:.{digits}f}"


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--v2-root", default=str(DEFAULT_V2_ROOT))
    ap.add_argument("--out", default="")
    ap.add_argument("--copy-to", default="")
    args = ap.parse_args()

    root = Path(args.v2_root)
    sys.path.insert(0, str(PROJ / "manu" / "etf_rotation" / "v2"))
    import config_v2 as C  # noqa: PLC0415

    v1 = dict(C.V1_BASELINE)
    search = _load(root / "val_search.json")
    arms = _load(root / "arm_search_val.json")
    frozen = _load(root / "frozen_params.json")
    token = _load(root / "single_blind_test_2024_2026.json")

    def _turn_pair(metrics: dict) -> tuple[str, str]:
        """年化换手的**双边 / 单边**并列披露。

        🔴 双边 = `Σ|Δw|`（= 总成交额/净值）；**单边只走唯一实现**
        `config_v2.one_way_turnover`（🔴 不许手写系数）。
        指标缺失 ⇒ 两列都写「—」（**不编数字**）。
        """
        turn = metrics.get("annualized_turnover")
        if not isinstance(turn, (int, float)):
            return "—", "—"
        return _fmt(turn, pct=False), _fmt(C.one_way_turnover(turn), pct=False)
    summary = ((token or {}).get("result_summary") or {})
    tm = summary.get("test_metrics") or {}
    vm = summary.get("val_metrics") or {}
    ic_t = summary.get("rank_ic_test") or {}
    ic_v = summary.get("rank_ic_val") or {}
    params = (frozen or {}).get("params") or (search or {}).get("best_params") or {}

    lines: list[str] = []
    A = lines.append
    A("# V2 成绩单 · ETF 穿透评分 V2 对齐版（对照 V1 baseline）")
    A("")
    A(f"- 生成时间：{summary.get('generated_at') or '—'}｜产物根：`{root}`")
    A(f"- V2 版本标识：`{C.V2_LABEL}`（`{C.V2_VERSION}`）｜V1 冻结保留，未改写。")
    A("")

    A("## 1. 冻结参数（val 段选出，测试段未参与）")
    A("")
    A("| 项 | 值 |")
    A("| --- | --- |")
    for key in ("arm", "arm_id", "smoothing_method", "smoothing_alpha", "smoothing_window",
                "top_k", "hold_buffer", "alpha_weight", "momentum_weight",
                "entry_ma_window", "exit_ma_window"):
        if key in params:
            A(f"| `{key}` | {params[key]} |")
    # 🔴 2026-11-17 夜班口径变更：**cadence 已进 val 段搜索空间** ⇒ 这里必须打印
    #    **冻结产物里的真实 cadence**，不许再硬写常量 20（那会与冻结参数自相矛盾）。
    _cad = params.get("rebalance_every_n_days", C.REBALANCE_EVERY_N_DAYS)
    _cad_same = int(_cad) == int(C.REBALANCE_EVERY_N_DAYS)
    A(f"| 再平衡节奏 cadence（**val 段搜出**） | **{int(_cad)} 个交易日**"
      f"{'（= V1 同**节奏**档，但换手**来源**仍不同源 ⇒ 只趋势性对比）' if _cad_same else '（**≠ V1 节奏 ⇒ 与 V1 的 16.57×（双边）不是同一口径，不可直接对比**）'} |")
    A(f"| cadence 的 val 段调仓次数 | {params.get('n_rebalances_val', '—')}"
      f"{'（< ' + str(C.CADENCE_MIN_REBALANCES) + ' ⇒ 样本不足，年化/回撤噪声主导）' if str(params.get('n_rebalances_val', '')) not in ('', 'None') and int(params.get('n_rebalances_val') or 0) < int(C.CADENCE_MIN_REBALANCES) else ''} |")
    A(f"| 信号日打分栅格 | {C.SIGNAL_STRIDE_N_DAYS} 个交易日（只管 S 层打分疏密） |")
    A(f"| 消融默认口径 | 消融 `{C.DROPPED_FEATURES}`（val 判定胜者 `{C.ABLATION_WINNER}`） |")
    A("")

    if arms:
        A("### 1.1 三臂对照（**只在 val 段**）")
        A("")
        results = arms.get("results") or {}
        if results:
            A("| 臂 | 年化 | 超额 | MDD | 换手(年化,**双边**) | 换手(年化,**单边**) | 目标分 |")
            A("| --- | --- | --- | --- | --- | --- | --- |")
            for arm_id, row in results.items():
                _two, _one = _turn_pair(row)
                A(f"| `{arm_id}` | {_fmt(row.get('annualized_return'))} | "
                  f"{_fmt(row.get('excess_vs_benchmark'))} | {_fmt(row.get('max_drawdown'))} | "
                  f"{_two}× | {_one}× | {_fmt(row.get('score'), pct=False)} |")
            A("")
            A(f"- 判定窗口：`{arms.get('val_window')}`｜胜者：`{arms.get('winner')}`")
            A("")

    if search:
        A("### 1.2 val 段网格搜索")
        A("")
        A(f"- 搜索空间：`{json.dumps(search.get('grid'), ensure_ascii=False)}`")
        A(f"- 目标函数：`{search.get('objective')}`")
        # 🔴 2026-11-18：产物里的 ``objective`` 串若是**改版前**措辞 ⇒ 显式披露
        #    （不许把「旧公式」当现行口径念；数字不受影响，排序口径已变）。
        _obj = str(search.get("objective") or "")
        if _obj and ("0.05" in _obj or "4.0" in _obj) and "封顶" not in _obj:
            A("- 🔴 **口径披露**：本产物的 `objective` 串是**改版前**的旧公式"
              "（换手惩罚未封顶、4.0 还是硬红线）⇒ **选参 score 被旧惩罚污染**；"
              "现行口径 = 惩罚**封顶** + 4.0× **只作披露基准**"
              "（唯一事实源 `config_v2.OBJECTIVE_DESCRIPTION`）⇒ 指标数字不受影响，"
              "**排序口径已变** ⇒ 重跑后才有可比的新 `score`。")
        A(f"- 试点数：{search.get('n_trials')}｜最优得分：{_fmt(search.get('best_score'), pct=False)}")
        A("")

    A("## 2. V1 ↔ V2 对照表")
    A("")
    A("| 指标 | V1 baseline | V2（val 选参） | V2（test 单盲） | 口径备注 |")
    A("| --- | --- | --- | --- | --- |")
    A(f"| 年化收益 | {_fmt(v1.get('annualized_return'))} | {_fmt(vm.get('annualized_return'))} | "
      f"{_fmt(tm.get('annualized_return'))} | 扣费后（佣金 1.5bp + 滑点 5bp） |")
    A(f"| 超额（vs 510300） | {_fmt(v1.get('excess_vs_510300'))} | "
      f"{_fmt(vm.get('excess_vs_benchmark'))} | {_fmt(tm.get('excess_vs_benchmark'))} | 基准 ETF 沪深300 |")
    A(f"| 最大回撤 MDD | {_fmt(v1.get('max_drawdown'))} | {_fmt(vm.get('max_drawdown'))} | "
      f"{_fmt(tm.get('max_drawdown'))} | 负值 |")
    _vm_two, _vm_one = _turn_pair(vm)
    _tm_two, _tm_one = _turn_pair(tm)
    # 🔴 2026-11-18 换手口径正名：V1 baseline 的 16.57× 是**双边**（旧键名属历史错标）
    #    ⇒ 双边行与单边行**都能**填 V1（双边 16.57× / 单边 8.28×）。
    A(f"| 年化换手（**双边**） | {_fmt(v1.get('annualized_turnover_two_way'), digits=2, pct=False)}× | "
      f"{_vm_two}× | {_tm_two}× | "
      "🔴 V1 与 V2 均为 `Σ|Δw|`（= 总成交额/净值）⇒ **双边**口径同表可比 |")
    A(f"| 年化换手（**单边**） | {_fmt(v1.get('annualized_turnover_one_way'), digits=2, pct=False)}× | "
      f"{_vm_one}× | {_tm_one}× | "
      "🔴 单边 = 0.5 × 双边（Top-K 线同源口径；唯一实现 `config_v2.one_way_turnover`）|")
    A(f"| Sharpe | — | {_fmt(vm.get('sharpe'), pct=False)} | {_fmt(tm.get('sharpe'), pct=False)} | "
      "🔴 V2 = 标准口径；V1 `backtest.py:305` 是粗口径（本行不混用） |")
    A(f"| RankIC（1 日） | {_fmt(v1.get('rank_ic_1d'), pct=False)} | "
      f"{_fmt(ic_v.get('ic'), pct=False)} | {_fmt(ic_t.get('ic'), pct=False)} | 逐日截面 Spearman |")
    A(f"| ICIR | {_fmt(v1.get('icir'), pct=False)} | {_fmt(ic_v.get('icir'), pct=False)} | "
      f"{_fmt(ic_t.get('icir'), pct=False)} | `mean/std(ddof=1)`，**不年化** |")
    A(f"| t 统计量 | {_fmt(v1.get('t_stat'), pct=False)} | {_fmt(ic_v.get('t_stat'), pct=False)} | "
      f"{_fmt(ic_t.get('t_stat'), pct=False)} | `mean/(std/√n)` |")
    A("")
    A("> 🔴 **换手口径脚注**：**V1 与 V2 均为 Σ|Δw| 双边口径**"
      f"（V1 双边 {_fmt(v1.get('annualized_turnover_two_way'), digits=2, pct=False)}×"
      f" / V2 双边 {_tm_two}×）⇒ **单边 = 0.5 × 双边**"
      f"（V1 单边 {_fmt(v1.get('annualized_turnover_one_way'), digits=2, pct=False)}×"
      f" / V2 单边 {_tm_one}×，由唯一实现 `config_v2.one_way_turnover` 换算）"
      "⇒ **双边与单边之间不可直接相减** ⇒ 只做趋势性对比。")
    A("")

    A("## 3. 验收断言")
    A("")
    turn_test = tm.get("annualized_turnover")
    mdd_test = tm.get("max_drawdown")
    A(f"- **换手披露基准** ≤ {C.TURNOVER_TARGET}×（V2 **双边**口径；"
      f"🔴 2026-11-18 起**不再是选参硬门槛**，只作披露基准）："
      f"test 双边 = **{_tm_two}×**（单边 **{_tm_one}×**）⇒ "
      f"{'✅ 达标' if isinstance(turn_test, (int, float)) and turn_test <= C.TURNOVER_TARGET else '❌ 未达标（**如实披露**，不因此筛掉候选）'}；"
      f"V1 = {_fmt(v1.get('annualized_turnover_two_way'), digits=2, pct=False)}×"
      f"（🔴 V1 与 V2 均为 Σ|Δw| **双边**口径"
      f"；V1 单边 = {_fmt(v1.get('annualized_turnover_one_way'), digits=2, pct=False)}×，见上表）")
    A(f"- 回撤收敛（V1 MDD {v1.get('max_drawdown')}）：test MDD = **{_fmt(mdd_test)}** ⇒ "
      f"{'✅ 收敛' if isinstance(mdd_test, (int, float)) and isinstance(v1.get('max_drawdown'), (int, float)) and mdd_test > v1.get('max_drawdown') else '❌ 未收敛'}")
    A(f"- 单盲封存：token = `single_blind_test_2024_2026.json`"
      f"（首次 {((token or {}).get('created_at')) or '—'}｜重跑次数 {((token or {}).get('rerun_count')) or 0}）")
    A("")

    A("## 4. 必须披露的偏差（不许只报好看的那个）")
    A("")
    A("1. **embargo 实际 25 > 用户下限 20**：V2 标签末端 = t+25，只裁 20 天会让末样本标签窗口伸进验证段 ⇒ 取 25（属**收紧**）；若标签改 t+15~t+20 ⇒ 可回 20。")
    A("2. **换手**口径**（双边）同源、**来源**不同源**：V2 与 V1 都是 `Σ|Δw|`（**双边**），"
      "但 V2 = 「调仓前**漂移**权重」之差（含 Rank Buffer 留存量，`backtest_v2.py`）"
      "vs V1 = 「上期**目标**权重未漂移」（`backtest.py:204`）"
      "⇒ 数值只做趋势性对比；单边 = 0.5 × 双边（唯一实现 `config_v2.one_way_turnover`）。")
    A("3. **Sharpe 口径不同**：V2 标准口径 vs V1 粗口径（`backtest.py:305`）⇒ 本表不混用。")
    A("4. **成分股历史不可观测**：本地只有 2026-11-04 单快照 ⇒ 回测用 2026 权重倒推历史（已标注 ＋ 给出偏差上界，未编造历史快照）。")
    A("5. **ETF 层超参（MA 窗口/Top-K/缓冲带/平滑臂）已迁回 val 段搜**：V1 的网格跑在 test 区间上（`analyze.py:2002/2035` 自认样本内最优）⇒ V1 的 19.4627% **不可**直接当 V2 的对照基线。")
    _cad = params.get("rebalance_every_n_days", C.REBALANCE_EVERY_N_DAYS)
    _nreb = params.get("n_rebalances_val")
    A(f"6. **调仓节奏 cadence = {int(_cad)} 个交易日**（2026-11-17 夜班起**已进 val 段搜索空间**，"
      "候选 {20,40,60,90}）"
      + ("⇒ 与 V1 的**节奏**相同，但换手口径仍不同源 ⇒ 只趋势性对比。"
         if int(_cad) == int(C.REBALANCE_EVERY_N_DAYS) else
         "⇒ **≠ V1 节奏** ⇒ 与 V1 的 16.57×（双边）**不是同一口径**，不可直接对比。"))
    if _nreb is not None:
        A(f"7. **val 段样本量**：冻结 cadence 在 val 段只有 **{int(_nreb)}** 次调仓"
          + (f"（< 门槛 {int(C.CADENCE_MIN_REBALANCES)}）⇒ **样本不足**，"
             "val 段的年化 / 回撤**噪声主导**，**不可据此选参**；"
             "最终判据是**盲测段（2024–2026）**的调仓次数。" if int(_nreb) < int(C.CADENCE_MIN_REBALANCES)
             else f"（≥ 门槛 {int(C.CADENCE_MIN_REBALANCES)}）⇒ 样本量可接受。"))
    A("")

    text = "\n".join(lines) + "\n"
    out = Path(args.out) if args.out else (root / "v2_scorecard.md")
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text(text, encoding="utf-8")
    print(f"[OK] 成绩单已写出：{out}")
    if args.copy_to:
        dst = Path(args.copy_to)
        dst.parent.mkdir(parents=True, exist_ok=True)
        dst.write_text(text, encoding="utf-8")
        print(f"[OK] 已复制到：{dst}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
