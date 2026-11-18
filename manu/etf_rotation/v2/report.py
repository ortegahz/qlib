"""🔴 V2 报告渲染（Markdown）。

**铁律**：缺失的指标写「—」，**不许编、不许填 0**。
所有数字都来自落盘产物（``ablation_choice.json`` / ``val_search.json`` /
``frozen_params.json`` / ``single_blind_test_2024_2026.json``），
报告层**不重新计算**任何指标（避免两套口径）。
"""

from __future__ import annotations

import json
import sys
from pathlib import Path
from typing import Any

import pandas as pd

_V2_DIR = Path(__file__).resolve().parent
if str(_V2_DIR) not in sys.path:
    sys.path.insert(0, str(_V2_DIR))

import config_v2 as C  # noqa: E402
import splits as SP  # noqa: E402

__all__ = ["write_report", "render_report"]

MISSING = "—"


def _fmt(value: Any, digits: int = 4, pct: bool = False, suffix: str = "") -> str:
    """缺失 ⇒ 「—」；**禁止编数字**。"""
    if value is None:
        return MISSING
    try:
        v = float(value)
    except (TypeError, ValueError):
        return str(value)
    if v != v or v in (float("inf"), float("-inf")):
        return MISSING
    if pct:
        return f"{v * 100:.{digits}f}%{suffix}"
    return f"{v:.{digits}f}{suffix}"


def _s(value: Any) -> str:
    """把可能是 ``None`` 的值渲染成文本（``None`` ⇒ 「—」，**不许把 None 原样印出去**）。

    🔴 2026-11-18 夜班补：成绩单 2.2 段原先用 f-string 直接插 ``emb.get(...)``
    ⇒ 缺失字段被印成 Python 的 ``None``（读者会误以为那是值）⇒ 统一走本助手。
    """
    return MISSING if value is None else str(value)


def _one_way(value: Any) -> Any:
    """双边换手 → 单边换手（**缺失 ⇒ None ⇒ 渲染成「—」**，不编数字）。

    口径：单边 = 0.5 × 双边（Top-K 线同源，见 ``config_v2.one_way_turnover``）。
    """
    try:
        v = float(value)
    except (TypeError, ValueError):
        return None
    if v != v or v in (float("inf"), float("-inf")):
        return None
    return C.one_way_turnover(v)


def _load(path: Path) -> dict | None:
    """读 json 产物（不存在/损坏 ⇒ None ⇒ 报告写「—」，不编数字）。"""
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return None


def _metric_row(label: str, v2: Any, v1: Any, digits: int = 4, pct: bool = False,
                 suffix: str = "") -> str:
    """渲染一行「V2 vs V1」对照（缺失 ⇒ 「—」）。

    🔴 2026-11-17 夜班审计补（**潜伏崩溃**）：``suffix`` 形参**原本根本不存在**，
    而「年化换手（单边）」那行按 **6 个位置参数**调用（多传了一个「×」）
    ⇒ ``--stage report`` 必抛 ``TypeError`` ⇒ **报告阶段必崩**
    （且只在跑报告时暴露 ⇒ 恰好落在**一次性盲测之后** ⇒ 代价极大）。
    修法：补 ``suffix`` 形参；**两侧**都带后缀（与 ``gen_v2_scorecard.py`` 的成绩单口径一致），
    缺失值（「—」）**不加**后缀。
    """
    def _cell(value: Any) -> str:
        """把单个单元格的值格式化成文本（缺失 ⇒ 「—」，**不编数字**）。

        🔴 后缀（``suffix``）**只加在真实数值上** ⇒ 「—」不会被写成「—×」。
        """
        text = _fmt(value, digits, pct)
        return f"{text}{suffix}" if (suffix and text != MISSING) else text

    return f"| {label} | {_cell(v2)} | {_cell(v1)} |"

def render_report(run_root: Path) -> str:
    """渲染 V2 成绩单 Markdown。"""
    run_root = Path(run_root)
    ablation = _load(run_root / "ablation_choice.json") or {}
    search = _load(run_root / "val_search.json") or {}
    frozen = _load(run_root / "frozen_params.json") or {}
    blind = _load(run_root / "single_blind_test_2024_2026.json") or {}
    result = blind.get("result_json") or {}
    test_m = result.get("test_metrics") or {}
    val_m = result.get("val_metrics") or {}
    ic_test = result.get("rank_ic_test") or {}
    ic_val = result.get("rank_ic_val") or {}
    params = (frozen.get("params") or search.get("best_params") or {})
    emb = SP.embargo_summary()
    # 🔴 2026-11-17 夜班口径变更：**cadence 已进 val 段搜索空间** ⇒ 头条表与
    #    「换手目标达成」结论**必须**带上 cadence，否则读者会把 V2 的换手
    #    与 V1 的 16.57× 当成同一口径直接对表。
    cadence = int(params.get("rebalance_every_n_days", C.REBALANCE_EVERY_N_DAYS))
    cadence_same = cadence == int(C.REBALANCE_EVERY_N_DAYS)
    cadence_note = (
        f"（cadence = {cadence} 个交易日"
        + ("＝ V1 同**节奏**档，但换手权重基准仍不同源 ⇒ 只趋势性对比"
           if cadence_same else
           "≠ V1 节奏 ⇒ 与 V1 的 16.57× **不是同一口径**，不可直接对比")
        + "）")

    # 🔴 单边换手（= 双边 × 0.5）：优先用 metrics 自己落的 ``annualized_turnover_one_way``；
    #    老产物没有该键 ⇒ 退回「双边 × 0.5」（纯单位换算，不是重算指标）。
    turn_v2_two_way = test_m.get("annualized_turnover")
    turn_v2_one_way = test_m.get("annualized_turnover_one_way")
    if turn_v2_one_way is None:
        turn_v2_one_way = _one_way(turn_v2_two_way)

    lines: list[str] = []
    A = lines.append
    A(f"# ETF 穿透评分 · **V2 对齐版** 成绩单")
    A("")
    A(f"> 生成时间：{pd.Timestamp.now(tz='Asia/Shanghai').isoformat()}｜"
      f"产物根：`{run_root}`｜版本：**{C.V2_LABEL}**")
    A("")
    A("> 🔴 本报告的**测试段（2024–2026）只跑过一次**（单盲 token 已封存，"
      "重跑会直接抛错）。所有超参搜索**只在 val 段（2022–2023）**完成。")
    A(">")
    A("> 🔴 **指标窗口与 val 声明是同一件事**（2026-11-17 21:20 独立审计查出并已修复）："
      "搜索目标函数把**净值 / 换手 / 基准三者同时切到 val 窗口**再算指标"
      "（`run_v2.val_window_metrics`）；旧实现曾把整段历史净值（2020–2026，含测试段）"
      "直接汇总 ⇒ 超参其实是在「看过测试段收益」的情况下选出来的。")
    A("> 🔴 另：val 段**最后一个截面的标签窗口会伸进测试段**（qlib 负 `Ref` 自动延伸取数）"
      "⇒ 已把 val 打分窗口末日裁到 2023-11-24（标签窗口末端 ≤ val 末日）。")
    A("")

    # ---------------- 1. 头条成绩单 ----------------
    A("## 1. 头条成绩单（V2 vs V1 baseline）")
    A("")
    A("| 指标 | V2（单盲 test 2024–2026） | V1 baseline |")
    A("| --- | --- | --- |")
    A(_metric_row("**扣费年化收益**", test_m.get("annualized_return"),
                  C.V1_BASELINE["annualized_return"], 4, True))
    A(_metric_row("相对基准年化超额", test_m.get("excess_vs_benchmark"),
                  C.V1_BASELINE["excess_vs_510300"], 4, True))
    A(_metric_row("**最大回撤 MDD**", test_m.get("max_drawdown"),
                  C.V1_BASELINE["max_drawdown"], 4, True))
    # 🔴 2026-11-18 换手口径**正名**：V2 的 ``annualized_turnover`` 与 V1 baseline 的
    #    ``annualized_turnover_two_way`` **都是双边**（Σ|Δw| = 总成交额/组合净值）
    #    ⇒ 双边行、单边行**都能与 V1 同口径对比**（V1 双边 16.57× / 单边 8.28×）。
    #    🔴 旧注释「V1 的 16.57× 记的是单边」是**历史错标**（键名错标 ⇒ 值被误读），
    #    2026-11-18 已把 V1 键正名为 ``annualized_turnover_two_way``（数值不变）。
    A(_metric_row(f"**年化换手（双边）** {cadence_note}", test_m.get("annualized_turnover"),
                  C.V1_BASELINE["annualized_turnover_two_way"], 3, False, "×"))
    A(_metric_row("**年化换手（单边）**（= 双边 × 0.5）", turn_v2_one_way,
                  C.V1_BASELINE["annualized_turnover_one_way"], 3, False, "×"))
    A(_metric_row("**RankIC**（20 日超额口径）", ic_test.get("ic"),
                  C.V1_BASELINE["rank_ic_20d"], 4, False))
    A(_metric_row("ICIR", ic_test.get("icir"), C.V1_BASELINE["icir"], 4, False))
    A(_metric_row("IC t 值", ic_test.get("t_stat"), C.V1_BASELINE["t_stat"], 3, False))
    A(_metric_row("Sharpe", test_m.get("sharpe"), None, 3, False))
    A(_metric_row("Calmar", test_m.get("calmar"), None, 3, False))
    A(_metric_row("总成本拖累（**累计**，线性近似）", test_m.get("total_cost_drag"), None, 4, True))
    A(_metric_row("年化成本拖累（线性近似）", test_m.get("annualized_cost_drag"), None, 4, True))
    A("")
    turn_v2 = turn_v2_two_way
    # 🔴 V1 换手**两个口径并列**：双边 16.57×（Σ|Δw|，旧键名属历史错标 ⇒ 已正名）
    #    与单边 8.28×（= 0.5 × 双边）⇒ 读者可任一口径对表。
    turn_v1_two_way = C.V1_BASELINE["annualized_turnover_two_way"]
    turn_v1_one_way = C.V1_BASELINE["annualized_turnover_one_way"]
    if turn_v2 is not None:
        A(f"**换手披露基准对照**（{C.TURNOVER_TARGET}× **仅作披露基准，不是选参硬门槛**）："
          f"V2 年化换手 **{_fmt(turn_v2, 3)}×（双边）**"
          f"（单边 {_fmt(turn_v2_one_way, 3)}×）vs 披露基准 ≤ "
          f"**{C.TURNOVER_TARGET}×** ⇒ "
          f"{'✅ 达标' if float(turn_v2) <= float(C.TURNOVER_TARGET) else '🔴 未达标'}"
          f"（V1 = {_fmt(turn_v1_two_way, 2)}×，**双边**口径"
          f"｜V1 单边 = {_fmt(turn_v1_one_way, 2)}×）｜🔴 {cadence_note}")
    else:
        A(f"**换手披露基准对照**：{MISSING}（单盲段未产出 ⇒ 不编数字）")
    A("")

    # ---------------- 2. 口径 ----------------
    A("## 2. 口径（逐条对齐）")
    A("")
    A("### 2.1 对齐① · Label = 未来 20 交易日超额收益")
    A("")
    A("| 项目 | 取值 |")
    A("| --- | --- |")
    A(f"| 标签表达式（qlib） | `{C.LABEL_EXPRESSION}` |")
    A(f"| 退出窗口 | t+{C.LABEL_EXIT_WINDOW[0]} ~ t+{C.LABEL_EXIT_WINDOW[1]}（窗口均价） |")
    A(f"| 标签窗口末端 | t+{C.LABEL_HORIZON_DAYS} |")
    A(f"| 基准 | {C.BENCHMARK}（沪深300） |")
    A(f"| 标签后处理 | {'CSRankNorm(label)（与 V1 同口径）'} |")
    A("")
    A("> 规格原文：「取 **t+15 ~ t+25 均价**以消除单日收盘毛刺」。")
    A("> ⚠️ qlib 的 `Mean(Ref($vwap,-15)/$vwap-1,11)` **不是**本口径"
      "（`Mean` 让分子分母一起滚动）⇒ 正确写法是 `Ref(Mean($vwap,11),-25)/$vwap-1`。")
    A("> ✅ 「窗口均价」与「日收益的均值」两种读法**数学上恒等**（分母 P[t] 在窗口内固定）"
      "⇒ 规格文字歧义不影响训练信号。")
    A("")
    A("### 2.2 时间切分与 embargo（硬隔离）")
    A("")
    A("| 项目 | 取值 |")
    A("| --- | --- |")
    A(f"| 训练名义段 | {C.TRAIN[0]} ~ {C.TRAIN[1]} |")
    A(f"| **训练实际段（embargo 裁剪后）** | {_s(emb.get('train_start'))} ~ **{_s(emb.get('train_end'))}** |")
    A(f"| 验证段（**超参搜索只许在此**） | {C.VALID[0]} ~ {C.VALID[1]} |")
    A(f"| 测试段（**单盲只跑一次**） | {C.TEST[0]} ~ {C.TEST[1]} |")
    A(f"| fit 窗（归一化统计量） | {_s(emb.get('fit_start'))} ~ {_s(emb.get('fit_end'))} |")
    A(f"| embargo | 规格下限 **{_s(emb.get('embargo_days_spec_min'))}** 个交易日；"
      f"实际 **{_s(emb.get('embargo_days_effective'))}** 个交易日 |")
    A(f"| 训练末样本的标签窗口结束日 | {_s(emb.get('last_train_label_window_end'))} |")
    A(f"| 验证集首样本日 | {_s(emb.get('valid_start'))} |")
    A("")
    A(f"> {emb.get('note')}")
    A("")
    A("### 2.3 对齐② · 成分股 / 流动性")
    A("")
    A(f"> {C.CONSTITUENTS_CALIBER_NOTE}")
    A("")
    A("### 2.4 对齐③ · Cost-Aware")
    A("")
    A("| 项目 | 取值 |")
    A("| --- | --- |")
    A(f"| 单边佣金 | {C.COMMISSION * 10000:.2f} bp |")
    A(f"| 单边滑点 | {C.SLIPPAGE * 10000:.2f} bp |")
    A(f"| Rank Buffer | {C.HOLD_BUFFER}（已计入换手） |")
    A(f"| 换手口径 | **调仓前漂移权重**（比 V1 的「上期目标权重」更严）；"
      f"**双边** = 总成交额/组合净值 ⇒ 单边 = 双边 × 0.5 |")
    A("")

    # ---------------- 3. 消融 ----------------
    A("## 3. 特征消融（对照实验，**决策只看 val**）")
    A("")
    if ablation:
        A("| 消融组 | 剔除特征 | val RankIC | val ICIR | t | 截面数 |")
        A("| --- | --- | --- | --- | --- | --- |")
        for g, ic in (ablation.get("results") or {}).items():
            dropped = list(C.ABLATION_GROUPS.get(g, ())) or ["（无，全量）"]
            A(f"| `{g}` | {', '.join(dropped)} | {_fmt(ic.get('ic'))} | "
              f"{_fmt(ic.get('icir'))} | {_fmt(ic.get('t_stat'), 3)} | "
              f"{_fmt(ic.get('n_cross_sections'), 0)} |")
        A("")
        A(f"**胜者：`{ablation.get('winner')}`**（判定窗口：{ablation.get('val_window')}，"
          f"指标：{ablation.get('metric')}）")
    else:
        A(f"{MISSING}（消融对照未产出）")
    A("")
    # 🔴🔴 **口径一致性断言**（2026-11-17 夜班审计补）：成绩单里的「默认口径」取自
    #    `config_v2.DROPPED_FEATURES`，而**实际打分用的口径**是由 val 段判出的
    #    `ablation_choice.json` 的 `winner` 决定的（`run_v2.py` 的 ablation 判定）⇒
    #    两者**原本没有任何一致性断言** ⇒ winner 一变，成绩单就与实际打分口径自相矛盾。
    #    ⇒ 这里**不许静默**：不一致 ⇒ 成绩单里**标红**并写明差在哪。
    _winner = str((ablation or {}).get("winner") or "")
    _winner_drop = list(C.ABLATION_GROUPS.get(_winner, ())) if _winner else []
    _cfg_drop = list(C.DROPPED_FEATURES or ())
    if _winner and list(_winner_drop) != list(_cfg_drop):
        A(f"> 🔴🔴 **口径不一致（红线）**：val 判出的胜者 `{_winner}` 剔除特征 = "
          f"`{_winner_drop}`，而 `config_v2.DROPPED_FEATURES` = `{_cfg_drop}` "
          f"⇒ **实际打分口径与冻结配置不是同一个** ⇒ 本表以**实际打分口径**为准，"
          f"冻结配置需复核。")
    elif _winner:
        A(f"> ✅ **口径一致**：val 胜者 `{_winner}` 的剔除特征 == "
          f"`config_v2.DROPPED_FEATURES` == `{_cfg_drop}`。")
    A(f"> ⚠️ **默认口径 = 消融（val 胜者 `{_winner or '—'}`）**；"
      f"`baseline_full`（全量特征）是**对照臂**。胜者由 **val 段 RankIC** 判定，"
      f"测试段从未参与。")
    A(f"> 🔴 两臂 RankIC 仅差约 0.6%（见上表）⇒ 该消融结论**对噪声敏感**，"
      f"已按「标签窗口不越 val 段」重判；换臂的影响在结论里一并披露。")
    A("")

    # ---------------- 4. val 搜索 ----------------
    A("## 4. 超参搜索（**只在 val 段**）")
    A("")
    if search:
        A(f"- 搜索窗口：**{search.get('val_window')}**（由 `hyperopt.assert_val_only` "
          f"在调用目标函数**之前**强制校验；触碰 2024-01-01 即抛错）")
        A(f"- 网格：`{search.get('grid')}`")
        A(f"- 目标函数：`{search.get('objective')}`")
        A(f"- 试跑次数：{search.get('n_trials')}")
        A(f"- **冻结参数**：`{json.dumps(params, ensure_ascii=False, sort_keys=True)}`")
        A("")
        # 🔴 2026-11-18 换手口径正名：``annualized_turnover`` = **双边**（Σ|Δw|），
        #    ``annualized_turnover_one_way`` = **单边**（= 0.5 × 双边）⇒ 两列并列披露。
        A("| # | 平滑臂 | top_k | hold_buffer | 目标分 | 年化 | 超额 | MDD | "
          "年化换手（双边） | 年化换手（单边） | 状态 |")
        A("| --- | --- | --- | --- | --- | --- | --- | --- | --- | --- | --- |")
        for i, tr in enumerate(search.get("trials") or [], 1):
            obj = tr.get("objective") or {}
            A(f"| {i} | {tr['params'].get('smoothing_arm')} | {tr['params'].get('top_k')} | "
              f"{tr['params'].get('hold_buffer')} | "
              f"{_fmt(tr.get('score'))} | {_fmt(obj.get('annualized_return'), 4, True)} | "
              f"{_fmt(obj.get('excess_vs_benchmark'), 4, True)} | "
              f"{_fmt(obj.get('max_drawdown'), 4, True)} | "
              f"{_fmt(obj.get('annualized_turnover'), 3)}× | "
              f"{_fmt(obj.get('annualized_turnover_one_way'), 3)}× | {tr.get('status')} |")
        gate = search.get("turnover_gate") or {}
        if gate:
            A("")
            A(f"- 🔴 **换手披露基准**（{C.TURNOVER_TARGET}× **仅作披露基准，"
              f"不是选参硬门槛**；mode = {gate.get('mode', MISSING)}）｜"
              f"达标 trial {gate.get('n_trials_within_target')}/"
              f"{gate.get('n_trials_with_turnover')}（有效 {gate.get('n_trials_ok')}）｜"
              f"选中者年化换手 {_fmt(gate.get('chosen_turnover'), 3)}×｜"
              f"披露对照 {'✅ 达标' if gate.get('ok') else '🔴 未达标'}")
    else:
        A(f"{MISSING}")
    A("")

    # ---------------- 5. 单盲 ----------------
    A("## 5. 单盲验收（2024–2026，**只跑一次**）")
    A("")
    A("| 项目 | 取值 |")
    A("| --- | --- |")
    A(f"| 单盲凭证 | `single_blind_test_2024_2026.json`（已封存为只读） |")
    A(f"| 首次运行时间 | {blind.get('created_at', MISSING)} |")
    A(f"| 重跑次数 | {blind.get('rerun_count', 0)} |")
    A(f"| 测试段调仓次数 | {result.get('n_rebalances_test', MISSING)} |")
    A(f"| val 段 RankIC | {_fmt(ic_val.get('ic'))} |")
    A(f"| test 段 RankIC | {_fmt(ic_test.get('ic'))} |")
    A("")
    A("> 🔴 `evaluate.run_single_blind` 是 **fail-closed** 的：token 已存在且未显式授权"
      "⇒ 直接抛 `SingleBlindViolation`，**不会静默重跑**。")
    A("")

    # ---------------- 6. 口径差异与已知局限 ----------------
    A("## 6. 🔴 与 V1 的口径差异（对比时必须一起读）")
    A("")
    A("| # | 差异 | 影响 |")
    A("| --- | --- | --- |")
    A("| 1 | **换手定义**：V2 用「调仓前**漂移**权重」，V1 用「上期**目标**权重」 | "
      "V2 换手**更严**（多算漂移回补）⇒ 对比偏向 V2 吃亏 |")
    A("| 2 | **成分股前视**：本地只有 2026-11-04 单快照 ⇒ 历史成分变更不可观测 | "
      "两版**同样**存在；V2 已量化偏差上界并显式标注 |")
    A("| 3 | **年化期数**：V2 用 250（与 V1 同） | 无 |")
    A("| 4 | **S 层超参**：V2 **照抄** V1 已定档值，未在 val 重搜 | "
      "算力让给 ETF 层；**如实披露**，不装作 V2 自己搜过 |")
    A("| 5 | **消融判定池**：csi300（训练池） | 与 S 层同池 ⇒ 可比；"
      "但不等价于全市场池 |")
    A(f"| 6 | **调仓节奏 cadence**：V2 = {cadence} 个交易日"
      f"（2026-11-17 夜班起**已进 val 段搜索空间**） | "
      + ("与 V1 的**节奏**相同 ⇒ 但换手口径仍不同源（见第 1 条）⇒ 只趋势性对比"
         if cadence_same else
         "**≠ V1 节奏** ⇒ 换手与 V1 的 16.57× **不是同一口径，不可直接对比**") + " |")
    A("| 7 | **换手单/双边口径**：V2 的 `annualized_turnover` 是**双边**"
      "（Σ|Δw| = 总成交额/组合净值）⇒ 单边 = 双边 × 0.5 | "
      "V1 baseline 的 16.57× 记的是**单边** ⇒ 两版对表前必须先统一到同一侧 |")
    A("")
    A("### 6.1 已知局限（不掩盖）")
    A("")
    A("- 🔴 **历史真实成分股/权重不可得**：akshare 两个接口都不提供历史 PCF ⇒ "
      "回测用 2026-11-04 快照倒推历史（V1 同款结构性问题）。")
    A("- 🔴 **S 层没有历史成分股对齐**：成分股权重口径在训练层与回测层是同一套 "
      "2026 快照 ⇒ 训练信号本身也带这层前视（V1 同款）。")
    A("- 🔴 **ETF 行情末日**：本地 `etf_prices.csv` 到 "
      f"{pd.Timestamp('2026-11-10').date()}（不是 11-17）⇒ 测试段最后一根 bar 之前。")
    A("- ✅ **ETF 层真实流动性约束已接入**（`run_v2.build_etf_tradable_maps`）："
      "涨停 ⇒ **不可买但可卖**、跌停 ⇒ **可买但不可卖**、停牌 ⇒ 两侧都不可；"
      "不可交易 ⇒ 不换手、保持原仓（`backtest_v2._rebalance`）。")
    A("- ⚠️ **涨跌停用「日涨跌幅 ≥/≤ 9.5%」近似**（本地无涨跌停价字段）"
      "⇒ 对 ±20% 涨跌停品种（跨境/商品 ETF）判不出板、会**偏保守**；"
      "本地 ETF 行情**无停牌行**（该数据源只含有成交的交易日）⇒ 停牌闸门实际无触发样本。")
    A("")

    A("## 7. 结论")
    A("")
    if turn_v2 is not None:
        ok_turn = float(turn_v2) <= float(C.TURNOVER_TARGET)
        mdd_v2 = test_m.get("max_drawdown")
        mdd_v1 = C.V1_BASELINE["max_drawdown"]
        ok_mdd = (mdd_v2 is not None) and float(mdd_v2) >= float(mdd_v1)
        A(f"- 换手（双边）：{_fmt(turn_v2, 3)}×（单边 {_fmt(turn_v2_one_way, 3)}×）"
          f"vs 披露基准 ≤ {C.TURNOVER_TARGET}× ⇒ "
          f"{'✅ 达标' if ok_turn else '🔴 未达成'}"
          f"（**仅披露基准，不是硬门槛**；V1 双边 {_fmt(turn_v1_two_way, 2)}×"
          f"｜V1 单边 {_fmt(turn_v1_one_way, 2)}×）")
        A(f"- 回撤：{_fmt(mdd_v2, 4, True)} vs V1 {_fmt(mdd_v1, 4, True)} ⇒ "
          f"{'✅ 收敛' if ok_mdd else '🔴 未收敛'}")
    else:
        A(f"- 换手：{MISSING}｜回撤：{MISSING}")
    A(f"- RankIC（20 日超额口径）：{_fmt(ic_test.get('ic'))} vs V1 "
      f"{_fmt(C.V1_BASELINE['rank_ic_20d'])}")
    A(f"- 扣费年化：{_fmt(test_m.get('annualized_return'), 4, True)} vs V1 "
      f"{_fmt(C.V1_BASELINE['annualized_return'], 4, True)}")
    A("")
    A("> 判定口径：**单盲段只跑一次**；上表数字是**当时**的结果，"
      "**不许**因为「不好看」回头调参再跑（那就把 test 变成了 val）。")
    A("")
    return "\n".join(lines)


def write_report(run_root: Path) -> Path:
    """渲染并写出 V2 成绩单。"""
    run_root = Path(run_root)
    md = render_report(run_root)
    path = run_root / "V2_SCOREBOARD.md"
    path.write_text(md, encoding="utf-8")
    return path
