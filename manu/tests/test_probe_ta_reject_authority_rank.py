"""🔴 产物级闸门「TA 否决零误伤」的**权威层级**判据回归（2026-11-11 判据换版）。

【为什么换版】
闸门初版判据是「否决名单里有代码，且**任何来源**说过要买它 ⇒ 误伤」。
实测把**真否决**也判红了：

  · 交付件 `20261111_121218` 的 `ta_screening.rejected` =
    ['512170'（研究经理）, '563020'（研究经理）, '159207'（**Trader 交易计划**）]；
  · `159207` 在**研究经理的提案表**里是「买 4%」（`llm_targets.who='research'`），
    但 **Trader 的交易计划**明写「159207因最终分-0.1155、E仅0.032，**不建议保留4%象征仓**」
    ⇒ **Trader 覆盖了研究经理的提案** ⇒ **159207 是真否决**，
    「一票否决 + 顺延」正是这么设计的（把 159207 换成 512010）。

⇒ 判据必须**分层级**比：**买侧证据的权威层级 ≥ 否决的权威层级** 才算误伤。

层级：观点 0 < 研究经理 10 < 辩论/风险裁决 20 < Trader 30 < 最终交易决定 40。
"""

from __future__ import annotations

import copy
import json
from pathlib import Path

from manu.etf_rotation.probes import probe_verify_guards as P  # type: ignore  # noqa: E402

#: 真实决策件（今日交付件）—— 有则用真 md 回归，没有就跳过
_REAL_SNAPSHOT = Path(
    "/media/manu/1TB-Volume/workspace/etf_rotation_20261110/server_runs_etf_rotation/snapshots"
)


def _plan() -> dict:
    """今日交付件**形状**：159207 在研究经理层被买、在 Trader 层被否决。"""
    return {
        "ta_screening": {
            "enabled": True,
            "rejected": [
                {"code": "512170", "name": "医疗ETF华宝", "reason": "",
                 "source_section": "研究经理"},
                {"code": "563020", "name": "红利低波ETF易方达", "reason": "",
                 "source_section": "研究经理"},
                {"code": "159207", "name": "高股息ETF广发",
                 "reason": "159207因最终分-0.1155、E仅0.032，不建议保留4%象征仓",
                 "source_section": "Trader 交易计划"},
            ],
        },
        "llm_targets": {
            "515450": {"direction": "买", "target_pct": 16.0, "who": "research"},
            "159207": {"direction": "买", "target_pct": 4.0, "who": "research"},
            "512170": {"direction": "不买", "target_pct": 0.0, "who": "bull"},
        },
        "llm_final_decision": {
            "available": True,
            "targets": {"563020": {"direction": "不建仓", "target_pct": 0.0}},
        },
    }


def _md_with_buy_row(section: str, code: str, direction: str = "买") -> str:
    return (
        f"## {section}\n\n"
        "| 标的 | 方向 | 基准目标 |\n"
        "|---|---:|---:|\n"
        f"| **{code}** | **{direction}** | 16% |\n"
    )


# ---------------------------------------------------------------- 层级判据

def test_lower_level_buy_evidence_does_not_outrank_higher_level_veto() -> None:
    """研究经理层（提案）说买 + Trader 层（最终）说不买 ⇒ **真否决**，不算误伤。"""
    plan = _plan()
    md = _md_with_buy_row("二、研究经理结论", "159207")
    verdict = P.find_ta_reject_mis_hits(plan, md)
    assert verdict["status"] == "pass", (
        f"低层级买侧证据不得推翻高层级否决，实得 {verdict['status']}：{verdict['detail']}"
    )
    assert [hit["code"] for hit in verdict["mis_hits"]] == []


def test_same_level_buy_evidence_is_a_mishit() -> None:
    """同一层级（研究经理）既说买又说否决 ⇒ **归属错位** ⇒ 误伤。"""
    plan = _plan()
    plan["ta_screening"]["rejected"] = [
        {"code": "515450", "name": "", "reason": "", "source_section": "研究经理"},
    ]
    plan["llm_targets"]["515450"] = {"direction": "买", "target_pct": 16.0, "who": "research"}
    md = _md_with_buy_row("二、研究经理结论", "515450")
    verdict = P.find_ta_reject_mis_hits(plan, md)
    assert verdict["status"] == "fail", "同层自相矛盾必须判红"
    assert [hit["code"] for hit in verdict["mis_hits"]] == ["515450"]


def test_higher_level_buy_evidence_outranks_lower_level_veto() -> None:
    """更高层级（最终交易决定）说买 + 低层级（研究经理）说不买 ⇒ 误伤。"""
    plan = _plan()
    plan["ta_screening"]["rejected"] = [
        {"code": "515450", "name": "", "reason": "", "source_section": "研究经理"},
    ]
    plan["llm_targets"]["515450"] = {"direction": "买", "target_pct": 16.0, "who": "research"}
    md = _md_with_buy_row("六、最终交易决定（原始输出）", "515450")
    verdict = P.find_ta_reject_mis_hits(plan, md)
    assert verdict["status"] == "fail", "更高层级的买侧证据必须能判红"


def test_opinion_level_buy_evidence_is_not_enough() -> None:
    """**观点层**（多头/空头）说买 ⇒ 不构成「TA 要买」⇒ 不得判红。"""
    plan = _plan()
    plan["ta_screening"]["rejected"] = [
        {"code": "563020", "name": "", "reason": "", "source_section": "研究经理"},
    ]
    plan["llm_targets"]["563020"] = {"direction": "买", "target_pct": 16.0, "who": "bull"}
    md = _md_with_buy_row("三、多空辩论要点", "563020")
    verdict = P.find_ta_reject_mis_hits(plan, md)
    assert verdict["status"] == "pass", "观点层的「买」不是裁决"


def test_mutation_unknown_veto_section_is_conservative() -> None:
    """变异：否决出处章节认不出来 ⇒ 层级 0 ⇒ **任何**买侧证据都算矛盾（最保守）。"""
    plan = _plan()
    plan["ta_screening"]["rejected"] = [
        {"code": "159207", "name": "", "reason": "", "source_section": ""},
    ]
    md = _md_with_buy_row("二、研究经理结论", "159207")
    verdict = P.find_ta_reject_mis_hits(plan, md)
    assert verdict["status"] == "fail", "出处认不出来时必须按最保守口径判红"


def test_mutation_lower_the_veto_level_makes_it_a_mishit() -> None:
    """变异：把否决出处从 Trader 层改成研究经理层 ⇒ 159207 立刻变误伤（证明层级在起作用）。"""
    plan = _plan()
    md = _md_with_buy_row("二、研究经理结论", "159207")
    assert P.find_ta_reject_mis_hits(plan, md)["status"] == "pass"

    mutated = copy.deepcopy(plan)
    for row in mutated["ta_screening"]["rejected"]:
        if row["code"] == "159207":
            row["source_section"] = "研究经理"
    verdict = P.find_ta_reject_mis_hits(mutated, md)
    assert verdict["status"] == "fail", "层级下调后必须判红（否则层级判据没生效）"


def test_authority_rank_table_is_closed() -> None:
    """层级闭集：认不出来的章节 ⇒ 0（观点层/未知）。"""
    assert P._authority_rank("二、研究经理结论（Research Manager）") == 10
    assert P._authority_rank("五、Trader 交易计划") == 30
    assert P._authority_rank("六、最终交易决定（原始输出）") == 40
    assert P._authority_rank("### 辩论裁决") == 20
    assert P._authority_rank("### 风险裁决") == 20
    assert P._authority_rank("### 空头（Bear）") == 0
    assert P._authority_rank("") == 0


# ---------------------------------------------------------------- 真实交付件回归

def test_real_new_delivery_has_zero_mishits() -> None:
    """今日**修好后**的交付件 `20261111_121218` ⇒ 闸门必须 PASS。"""
    snap = _REAL_SNAPSHOT / "20261111_121218"
    plan_path = snap / "final_rebalance_plan.json"
    md_path = snap / "tradingagents_decision.md"
    if not plan_path.is_file() or not md_path.is_file():
        import pytest  # noqa: PLC0415

        pytest.skip(f"真实交付件不在本机：{snap}")
    plan = json.loads(plan_path.read_text(encoding="utf-8"))
    md = md_path.read_text(encoding="utf-8")
    verdict = P.find_ta_reject_mis_hits(plan, md)
    assert verdict["status"] == "pass", f"修好后交付件必须零误伤：{verdict['detail']}"


def test_real_old_delivery_still_catches_the_mishit() -> None:
    """今日**修之前**的交付件 `20261111_100742` ⇒ 闸门必须仍然抓到 515450（不许被调松）。"""
    snap = _REAL_SNAPSHOT / "20261111_100742"
    plan_path = snap / "final_rebalance_plan.json"
    md_path = snap / "tradingagents_decision.md"
    if not plan_path.is_file() or not md_path.is_file():
        import pytest  # noqa: PLC0415

        pytest.skip(f"真实交付件不在本机：{snap}")
    plan = json.loads(plan_path.read_text(encoding="utf-8"))
    md = md_path.read_text(encoding="utf-8")
    verdict = P.find_ta_reject_mis_hits(plan, md)
    assert verdict["status"] == "fail", "旧交付件必须仍然判红（闸门不许被调松）"
    assert "515450" in [hit["code"] for hit in verdict["mis_hits"]]
    assert "159207" not in [hit["code"] for hit in verdict["mis_hits"]], (
        "159207 是 Trader 层的**真否决** ⇒ 不得判红"
    )
