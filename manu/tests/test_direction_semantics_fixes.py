"""🔴 2026-11-11 新增：critic 生产代码审计抓到的 4 个真缺陷的回归。

审计来源：`01a1c873-947b-76d0-93b8-3ff16ccd369c`（critic，生产代码审查）
                `01a1c873-9fca-7ef0-ab70-adf9684fbac8`（auditor，探针/锁/金标审查）

本文件钉死四件事（自带夹具，**不依赖服务器产物**）：

  ① **方向词表单一事实源**（高危）
     解析器 `final_rebalance_plan._ZERO_DIR_WORDS` 与守卫
     `verify_artifacts.LLM_DIRECTION_ZERO` **原本是两份分叉的副本**
     ⇒ 解析器吐得出的「清」「Sell」不在解析器 ZERO 档（同类「清掉+8%」只被守卫
       拦死、不被强制归零）；「卖掉/清零/不建仓」在解析器侧是**死条目**。
     ⇒ 现在两边都引用 `direction_vocab`。

  ② **理由列里的方向词不许强制归零**（高危）
     方向只从**理由列**兜底扫出来时（表头没指认方向列 / 方向列写「—」），
     那个词是**散文**不是裁决 ⇒ 旧实现拿它强制归零 ⇒ 合法目标被**静默改成 0%**
     且守卫因「方向=卖 + 目标=0%」自洽而**放行** ⇒ 静默降级。

  ③ **部分卖出要吃得下 markdown 噪声**（中危）
     「卖出**约2,600份**」旧正则漏判成**清仓** ⇒ 被强制归零且守卫放行 ⇒ 静默降级。

  ④ **守卫在「当前权重不可比」时跳过账户相对约束**（实跑假阳性）
     `current_pct == 0.0` = **账户里没这只**（未持有）⇒ 「目标 < 当前」没有可比
     基准（LLM 的「减 11.17%→6%」是相对**研究基线组合**说的）⇒ 实测 159929 误报。
     ⇒ `current_pct` 为 None（未知）或 <= 0.0（未持有）时跳过 REDUCE/INCREASE；
       ZERO / POS 两档**不受影响**，继续严查。
"""

from __future__ import annotations

import pathlib
import re
import sys

_ETF_DIR = pathlib.Path(__file__).resolve().parents[1]
if str(_ETF_DIR) not in sys.path:
    sys.path.insert(0, str(_ETF_DIR))

import final_rebalance_plan as FRP  # noqa: E402
import verify_artifacts as VA  # noqa: E402


# --------------------------------------------------------------------------- ① 单一事实源
class TestDirectionVocabSingleSource:
    """方向词表必须是**同一份**，且解析器吐得出的词都得在守卫词表里。"""

    def test_parser_and_guard_share_same_set(self) -> None:
        assert FRP._ZERO_DIR_WORDS == VA.LLM_DIRECTION_ZERO, (
            "解析器与守卫的 ZERO 词表必须逐字相同（单一事实源）"
        )
        assert FRP._ZERO_DIR_WORDS is VA.LLM_DIRECTION_ZERO, (
            "两边必须引用**同一个对象**（不是两份内容相同的副本）"
        )

    def test_clear_and_sell_are_zero(self) -> None:
        """① 「清」「Sell」是解析器**吐得出**的词 ⇒ 必须在 ZERO 档里。"""
        assert FRP._classify_dir_cell("清") == "清"
        assert FRP._classify_dir_cell("Sell") == "Sell"
        assert "清" in FRP._ZERO_DIR_WORDS, "「清」必须属 ZERO ⇒ 目标权重必须 0%"
        assert "Sell" in FRP._ZERO_DIR_WORDS, "「Sell」必须属 ZERO ⇒ 目标权重必须 0%"

    def test_every_emitted_direction_is_classified(self) -> None:
        """① 解析器能吐出的方向词，守卫**一个都不许漏**（漏一个 = 假绿）。"""
        for cell in ("买", "买入", "加仓", "增持", "Buy", "Overweight",
                     "减持", "减仓", "减", "Underweight",
                     "增", "加",
                     "持有", "保留", "维持", "不卖", "Hold", "Neutral",
                     "卖", "卖出", "卖掉", "清仓", "清掉", "清零", "清",
                     "不买", "Sell"):
            direction = FRP._classify_dir_cell(cell)
            assert direction, f"方向格 {cell!r} 必须解析得出方向词"
            assert direction in VA.LLM_DIRECTION_ALL, (
                f"方向格 {cell!r} 解析出的 {direction!r} 不在守卫的已分类词表里 ⇒ 守卫会误判 FAIL"
            )

    def test_guard_vocab_is_superset_of_parser(self) -> None:
        """① 守卫词表是**解析器的超集**（守卫认得的 ⊇ 解析器吐得出的）。

        守卫多认的词（Reduce / Increase / Add / Keep / 建仓 / 不建仓）是
        **防御性**的：LLM 原文若出现这些写法，守卫照样能校验它们的语义。
        """
        for word in ("Reduce", "Increase", "Add", "Keep", "建仓", "不建仓", "退出"):
            assert word in VA.LLM_DIRECTION_ALL, f"守卫必须认得 {word!r}"

    def test_build_position_vocab_entries(self) -> None:
        """① 「建仓」与「不建仓」在共享词表里各自就位（POS / ZERO）。

        ⚠️ 解析器词法**不认**这两个词（同上，`_LLM_DIR_CELL` 没有）⇒ 方向格写
        「建仓/不建仓」的行解析不出方向 ⇒ 该行被丢弃。**既有行为，本轮故意不改**
        （理由见 `test_not_built_position_is_zero_vocab_entry` 的范围决定说明）。
        """
        assert "建仓" in VA.LLM_DIRECTION_POS, "「建仓」必须属 POS（目标必须 >0%）"
        assert "建仓" in VA.LLM_DIRECTION_ALL

    def test_not_built_position_is_zero_vocab_entry(self) -> None:
        """① 「不建仓」在共享词表里是 ZERO（语义已就位）。

        ⚠️ **已知范围决定（2026-11-11）**：解析器的词法（`_LLM_DIR_CELL`）**不认**
        「不建仓」⇒ 该格解析不出方向 ⇒ 那一行会被丢弃。这是**既有行为**，
        本轮**故意不改**：改了会让空头视图多出三行「不建仓 0%」意见 ⇒
        冲突表结构变化 ⇒ 需要连带重建基线并重新验收，收益（多保留三行已被
        规则层否决的标的）远小于风险。词表侧已就位 ⇒ 将来一旦词法支持它，
        语义（目标 0%）立刻正确。
        """
        assert "不建仓" in FRP._ZERO_DIR_WORDS, "「不建仓」必须属 ZERO（目标必须 0%）"
        assert "不建仓" in VA.LLM_DIRECTION_ZERO


# --------------------------------------------------------------------------- ② 理由列方向词
_MD_RESEARCH = """
## 研究经理结论

| 标的 | 方向 | 目标 | 理由 |
|---|---|---|---|
| 512800 | {direction_cell} | 10% | 逢高卖出部分，保留底仓 |
"""


def _write_decision(tmp_path: pathlib.Path, direction_cell: str) -> str:
    """把决策 md 落成临时文件 ⇒ 返回路径（``_llm_target_views`` 收的是**路径**）。"""
    path = tmp_path / "tradingagents_decision.md"
    path.write_text(_MD_RESEARCH.format(direction_cell=direction_cell), encoding="utf-8")
    return str(path)


class TestReasonColumnDirectionDoesNotForceZero:
    """方向只从**理由列**兜底扫出来时，**不许**把目标静默改成 0%。"""

    def test_reason_derived_direction_keeps_target(self, tmp_path: pathlib.Path) -> None:
        """② 方向列写「—」（方向只能从理由列兜底）⇒ 目标保持 10%，不许变 0%。"""
        md = _write_decision(tmp_path, "—")
        views = FRP._llm_target_views(md, total_assets=29854.67,
                                      account_weights={"512800": 22.54})
        assert "512800" in views, "夹具没解析出 512800 ⇒ 断言不适用"
        view = views["512800"][0]
        assert view["target_pct"] == 10.0, (
            f"理由列兜底出的方向词不许强制归零；实得 target_pct={view['target_pct']!r}"
        )
        assert view["direction"] == "卖出", (
            "方向词仍要如实记录 ⇒ 守卫才能**响亮拦下**这行歧义（而不是静默改写成 0%）"
        )

    def test_explicit_direction_cell_still_forces_zero(self, tmp_path: pathlib.Path) -> None:
        """② 对照组：方向**就在方向列**（真裁决）⇒ 照旧强制归零（修法不许过头）。"""
        md = _write_decision(tmp_path, "卖出")
        views = FRP._llm_target_views(md, total_assets=29854.67,
                                      account_weights={"512800": 22.54})
        view = views["512800"][0]
        assert view["target_pct"] == 0.0, (
            f"方向列明确写「卖出」⇒ 目标必须 0%；实得 {view['target_pct']!r}"
        )

    def test_guard_flags_reason_derived_contradiction_loudly(self) -> None:
        """② 理由列兜底出的「卖出 + 10%」⇒ 守卫必须**响亮报错**（不许真空通过）。"""
        plan = {"llm_targets": {"512800": {"direction": "卖出", "target_pct": 10.0,
                                           "current_pct": 22.54}}}
        ok, detail = VA._check_llm_direction_semantics(plan)
        assert not ok, "歧义行必须被守卫拦下（响亮失败）"
        assert "512800" in detail, f"守卫的失败明细必须点名 512800，实得 {detail!r}"


# --------------------------------------------------------------------------- ③ 部分卖出
class TestPartialSellMarkdownNoise:
    """「卖出**约2,600份**」= 部分卖出（减），**不是**清仓。"""

    def test_markdown_noise_is_partial_sell(self) -> None:
        for cell in ("卖出**约2,600份**", "卖出约 2,600 股", "卖出约2,600份",
                     "卖出 **约 2,600 份**", "卖掉**约1,000份**"):
            assert FRP._classify_dir_cell(cell) == "减", (
                f"{cell!r} 必须判成「减」（部分卖出），实得 {FRP._classify_dir_cell(cell)!r}"
            )

    def test_old_regex_missed_it(self) -> None:
        """③ 变异反证：旧正则（`\\s*`）漏判 ⇒ 证明本测试**有牙**。"""
        old = re.compile(r"卖出\s*(?:约)?\s*[\d,]+(?:\.\d+)?\s*(?:股|份)")
        assert old.search("卖出**约2,600份**") is None, "旧口径没复现事故 ⇒ 夹具不敏感"
        assert FRP._PARTIAL_SELL_RE.search("卖出**约2,600份**") is not None, "新口径必须命中"

    def test_plain_sell_still_liquidation(self) -> None:
        """③ 回归：裸「卖出」（不带数量）仍是清仓 ⇒ ZERO。"""
        assert FRP._classify_dir_cell("卖出") == "卖出"
        assert FRP._classify_dir_cell("卖出") in FRP._ZERO_DIR_WORDS


# --------------------------------------------------------------------------- ④ 守卫可比性
def _plan(direction: str, target_pct: float, current_pct: float | None) -> dict:
    return {"llm_targets": {"159929": {"direction": direction, "target_pct": target_pct,
                                       "current_pct": current_pct}}}


class TestGuardCurrentWeightComparability:
    """`current_pct` = None（未知）/ 0.0（**未持有**）⇒ 账户相对约束不可比 ⇒ 跳过。"""

    def test_reduce_with_zero_current_is_not_flagged(self) -> None:
        """④ 实跑假阳性：159929 不在账户（未持有 ⇒ 0%）⇒ 「减 6%」不许被拦。"""
        ok, detail = VA._check_llm_direction_semantics(_plan("减", 6.0, 0.0))
        assert ok, f"未持有 ⇒ 无可比基准 ⇒ 不许拦；detail={detail!r}"

    def test_reduce_with_unknown_current_is_not_flagged(self) -> None:
        ok, detail = VA._check_llm_direction_semantics(_plan("减", 6.0, None))
        assert ok, f"当前权重未知 ⇒ 不许拦；detail={detail!r}"

    def test_reduce_with_real_current_is_still_flagged(self) -> None:
        """④ 对照组：账户**真的持有** 22.54% ⇒ 「减 ⇒ 目标 < 当前」照旧严查。"""
        ok, detail = VA._check_llm_direction_semantics(_plan("减", 26.0, 22.54))
        assert not ok, "可比时必须拦（守卫不许被削弱成摆设）"
        assert "159929" in detail

    def test_zero_direction_still_checked_when_current_is_zero(self) -> None:
        """④ ZERO 档**不受**本次放宽影响：方向「卖出」+ 目标 8% ⇒ 照旧 FAIL。"""
        ok, detail = VA._check_llm_direction_semantics(_plan("卖出", 8.0, 0.0))
        assert not ok, "ZERO 档必须照旧严查"
        assert "159929" in detail

    def test_pos_direction_still_checked_when_current_is_zero(self) -> None:
        """④ POS 档同理：「买」+ 目标 0% ⇒ 照旧 FAIL。"""
        ok, detail = VA._check_llm_direction_semantics(_plan("买", 0.0, 0.0))
        assert not ok, "POS 档必须照旧严查"
        assert "159929" in detail


# --------------------------------------------------------------------------- ⑤ 区间目标
class TestRangeTargetExtraction:
    """「区间在前、基准在后」的目标写法 ⇒ 必须取**基准值**，不许取区间下沿。

    🔴 2026-11-11 服务器真机实测（asof=2026-11-03 基线重建）：
       Trader 的最终裁决把 512800 的目标写成
         `| 512800 | 22.54% | 减仓 | **0%—10%，基准约10%** | … |`
       ⇒ 旧口径「取第一个存活值」取到区间**下沿 0%** ⇒ 说话人明说「保留约 10%」
         被写成**目标 0%（清仓）** ⇒ 交付件的 LLM 目标全被抽空 ⇒ 分歧表空
         ⇒ 情景生成器与三个闸门探针连环失败（且这个错误**不会**被任何守卫拦下）。
    """

    def test_range_with_baseline_takes_baseline(self) -> None:
        assert FRP._extract_target_pct("**0%—10%，基准约10%**") == 10.0
        assert FRP._extract_target_pct("0%—10%，基准约10%") == 10.0
        assert FRP._extract_target_pct("0%—10%，目标 8%") == 8.0
        assert FRP._extract_target_pct("5%—15%，中枢10%") == 10.0

    def test_old_behavior_took_lower_bound(self) -> None:
        """变异反证：旧口径取第一个存活值 = 区间下沿 0.0 ⇒ 证明本测试**有牙**。"""
        cell = "**0%—10%，基准约10%**"
        first = float(FRP._LLM_PCT_LOOSE.search(cell).group(1))
        assert first == 0.0, "旧口径没复现事故 ⇒ 夹具不敏感"
        assert FRP._extract_target_pct(cell) == 10.0

    def test_existing_range_rules_unchanged(self) -> None:
        """回归：reviewer A4 已定的区间口径**不许**被改坏。"""
        assert FRP._extract_target_pct("5%—10%") == 5.0, "裸区间仍取下界"
        assert FRP._extract_target_pct("目标(区间) 8%—10%") == 8.0
        assert FRP._extract_target_pct("约9%，执行区间8%—10%") == 9.0, "基准在前仍取基准"
        assert FRP._extract_target_pct("10%~12%") == 10.0

    def test_single_pct_rules_unchanged(self) -> None:
        """回归：单值与上界写法不受影响。"""
        for cell, want in (("**不高于10%**", 10.0), ("**20%**", 20.0), ("不超过10%", 10.0),
                           ("减至约10%，上限15%", 10.0), ("约10%", 10.0), ("基准约10%", 10.0)):
            assert FRP._extract_target_pct(cell) == want, f"{cell!r} 必须判成 {want}"
