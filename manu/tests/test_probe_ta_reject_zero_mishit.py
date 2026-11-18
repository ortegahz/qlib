"""🔴 2026-11-11 新增：**产物级**闸门「TA 否决名单零误伤」的回归测试。

【被测对象】
`probes/probe_verify_guards.py` 里的 `find_ta_reject_mis_hits()` ——
交付件 `final_rebalance_plan.json` 的 `ta_screening.rejected` 里
**不得出现**同一份交付件里 TA 明确要买的标的（买/增持/加仓/建仓/buy）。

【为什么要有这条闸门】
2026-11-11 12:05 实测事故：交付件快照
`/mnt/data/siping/runs/etf_rotation_results/snapshots/20261111_100742/`
的 `final_rebalance_plan.json` 里
`ta_screening.rejected` = ['515450','512170','563020','159207']，
而**同一份交付件**的 `tradingagents_decision.md` 第 47/78/191 行明写
「| 买 | 515450 | 红利低波50ETF南方 | 16% | …」「515450 **买**（16%…）」
⇒ **TA 明确要买 515450，却被当成一票否决** ⇒ 误伤
⇒ 5 个席位里 515450 被换成别的标的（512800/515650/512890/516620/512010）
⇒ TA 要的红利低波 515450 从计划里消失。
根因在**散文否决解析的归属**（解析逻辑由主 agent 负责修）；
本文件只钉死**闸门本身**：判据、证据口径、变异负例。

【口径要点】
  · 证据一：计划件自带的 LLM 方向（`llm_targets` / `llm_final_decision.targets`）；
  · 证据二：决策 md **表格行**里的方向格；
  · 两条任意一条成立 ⇒ FAIL；两条都拿不到 ⇒ **不适用（≠ 通过）**；
  · **散文不算证据**（散文归属正是事故根因，拿散文当证据 = 重犯同一个错）。

🔴 2026-11-11 **判据换版（权威层级）**：买侧证据必须**层级 ≥ 否决的层级**才算误伤
（层级：观点 0 < 研究经理 10 < 辩论/风险裁决 20 < Trader 30 < 最终交易决定 40）。
理由：研究经理的**提案表**与 Trader 的**最终裁决**互相矛盾，是「一票否决 + 顺延」的
**设计意图**（研究经理提案买 159207 4%，Trader 写「不建议保留 4% 象征仓」⇒ **真否决**）。
⇒ 本文件的层级判据回归在 `tests/test_probe_ta_reject_authority_rank.py`。
"""

from __future__ import annotations

import copy
import json
from pathlib import Path

from manu.etf_rotation.probes import probe_verify_guards as P  # type: ignore  # noqa: E402

#: 真实决策件夹具（2026-11-10 服务器实跑裁剪版）—— 用真 md 验表格行解析
_FIXTURE_MD = Path(__file__).resolve().parent / "fixtures" / "ta_decision_real_20261110.md"


def _incident_plan() -> dict:
    """2026-11-11 事故交付件的**形状**（合成夹具，不含生产数值）。

    · `rejected` = 515450 / 512170 / 563020 / 159207（与事故件一致）；
    · `llm_targets`：515450 / 159207 方向 = **买**（TA 明写要买）；
    · `llm_final_decision.targets`：563020 方向 = 不建仓（Trader 最终裁决）。
    """
    return {
        "ta_screening": {
            "enabled": True,
            "rejected": [
                {"code": "515450", "name": "相关性",
                 "reason": "但结论不变：与 515450 相关性 0.917、红利敞口只用 515450 表达足够，"
                           "不建仓、更不给 20%", "source_section": "研究经理"},
                {"code": "512170", "name": "医疗ETF华宝",
                 "reason": "并把 159207 从 16% 压到 4%", "source_section": "研究经理"},
                {"code": "563020", "name": "红利低波ETF易方达",
                 "reason": "**五、方向性判断汇总**：512800 **减**；515450 **买**（16%…）",
                 "source_section": "研究经理"},
                {"code": "159207", "name": "高股息ETF广发",
                 "reason": "159207因最终分-0.1155、E仅0.032，不建议保留4%象征仓",
                 "source_section": "Trader 交易计划"},
            ],
        },
        "llm_targets": {
            "512800": {"direction": "减", "target_pct": 16.0},
            "515450": {"direction": "买", "target_pct": 16.0},
            "515650": {"direction": "买", "target_pct": 12.0},
            "159207": {"direction": "买", "target_pct": 4.0},
            "512170": {"direction": "不买", "target_pct": 0.0},
        },
        "llm_final_decision": {
            "available": True,
            "targets": {
                "512800": {"direction": "减", "target_pct": 16.0},
                "563020": {"direction": "不建仓", "target_pct": 0.0},
                "512170": {"direction": "不买", "target_pct": 0.0},
            },
        },
    }


def _incident_md() -> str:
    """事故交付件 `tradingagents_decision.md` 的**形状**（合成夹具）。"""
    return "\n".join([
        "## 二、研究经理结论",
        "",
        "**五、方向性判断汇总**：512800 **减**（22.54% → 16%）；515450 **买**（16%）；"
        "159207 **小买**（4%）；512170 **不买**；563020 **不买**。",
        "",
        "| 动作 | 代码 | 名称 | 目标权重 | 目标金额 |",
        "|---|---|---|---:|---:|",
        "| 减 | 512800 | 银行ETF华宝 | 16% | ~4,777 |",
        "| 买 | 515450 | 红利低波50ETF南方 | 16% | ~4,777 |",
        "| 买 | 515650 | 消费50ETF富国 | 12% | ~3,583 |",
        "| 买 | 159207 | 高股息ETF广发 | 4% | ~1,194 |",
        "",
        "## 六、最终交易决定",
        "",
        "| 标的 | 方向 | 建议目标 | 核心理由 |",
        "|---|---:|---:|---|",
        "| **512800** | **减** | **16%** | 入选标的第一。 |",
        "| **515450** | **买** | **16%，约4,776.75元** | 最终入选的非银行标的中最强。 |",
        "| **515650** | **买** | **12%，约3,582.56元** | 消费修复和增长弹性仓。 |",
        "| **159207** | **买** | **8%，约2,388.37元** | 可作为高股息补充。 |",
        "| **512170** | **不买** | 0 | 最终分-0.5848、E仅0.0133。 |",
        "| **563020** | **不买** | 0 | 未被规则选入，且与515450相关性0.917。 |",
        "",
        "| 标的 | 方向 | 基准目标 | 熊方理由 |",
        "|---|---:|---:|---|",
        "| **515450** | **买，但仅在数据复核通过后** | **4%** | E为0.3353、动量为正。 |",
        "| **159207** | **不建仓** | 0 | 强度接近横盘。 |",
        "| **512170** | **不建仓** | 0 | 是五只机械目标中最弱。 |",
        "| **563020** | **不建仓** | 0 | 明显低于515450。 |",
        "",
    ])


def test_incident_shape_must_fail() -> None:
    """2026-11-11 事故形状 ⇒ 闸门必须 FAIL，且点名 515450 / 159207。"""
    verdict = P.find_ta_reject_mis_hits(_incident_plan(), _incident_md())
    assert verdict["status"] == "fail", f"事故形状必须 FAIL，实得 {verdict['status']}"
    hit_codes = [hit["code"] for hit in verdict["mis_hits"]]
    assert "515450" in hit_codes, f"515450 必须被点名，实得 {hit_codes}"
    assert "159207" in hit_codes, f"159207 必须被点名，实得 {hit_codes}"
    assert verdict["unjudged"] == [], "每一只都要取到方向证据"


def test_legit_rejections_are_not_mis_hits() -> None:
    """卖侧 / 不买 / 不建仓的方向**不算**误伤（闸门不许把合法否决也判红）。"""
    plan = _incident_plan()
    plan["ta_screening"]["rejected"] = [
        row for row in plan["ta_screening"]["rejected"] if row["code"] in ("512170", "563020")
    ]
    verdict = P.find_ta_reject_mis_hits(plan, _incident_md())
    assert verdict["status"] == "pass", f"合法否决必须 PASS，实得 {verdict['status']}"
    assert verdict["mis_hits"] == []


def test_mutation_negative_must_fail() -> None:
    """**变异负例**：把买侧代码塞进 rejected ⇒ 闸门必须 FAIL（证明闸门有牙）。"""
    plan = _incident_plan()
    clean = P._drop_rejected_codes(plan, ["515450", "159207"])
    assert P.find_ta_reject_mis_hits(clean, _incident_md())["status"] == "pass"

    code = P._pick_buy_side_code(clean, [])
    assert code == "515450", f"变异代码应当是买侧的 515450，实得 {code}"
    mutated = P._inject_rejected_code(clean, code)
    verdict = P.find_ta_reject_mis_hits(mutated, _incident_md())
    assert verdict["status"] == "fail", "变异后必须 FAIL（否则闸门没牙）"
    assert [hit["code"] for hit in verdict["mis_hits"]] == [code]


def test_mutation_without_any_buy_side_code_still_has_teeth() -> None:
    """交付件里**一个买侧代码都没有** ⇒ 负例用兜底夹具自造一条（负例不许空转）。"""
    plan = _incident_plan()
    plan["llm_targets"] = {"512800": {"direction": "减", "target_pct": 16.0}}
    plan["ta_screening"]["rejected"] = [
        {"code": "512170", "name": "医疗ETF华宝", "reason": "", "source_section": "研究经理"},
    ]
    assert P.find_ta_reject_mis_hits(plan, _incident_md())["status"] == "pass"

    assert P._pick_buy_side_code(plan, []) is None
    fixture = P._inject_buy_side_target(plan, "599999")
    mutated = P._inject_rejected_code(fixture, "599999")
    verdict = P.find_ta_reject_mis_hits(mutated, _incident_md())
    assert verdict["status"] == "fail", "自造买侧夹具后也必须 FAIL（闸门永远有牙）"


def test_md_table_row_alone_is_enough() -> None:
    """证据二单独成立即 FAIL：计划件没记方向，但决策 md 表格行明写「买」。"""
    plan = _incident_plan()
    plan["llm_targets"].pop("515450")
    plan["llm_final_decision"]["targets"].pop("515450", None)
    verdict = P.find_ta_reject_mis_hits(plan, _incident_md())
    assert verdict["status"] == "fail", "md 表格行的买侧方向必须能单独判误伤"
    assert "515450" in [hit["code"] for hit in verdict["mis_hits"]]


def test_prose_is_not_evidence() -> None:
    """**散文不算证据**（散文归属正是事故根因）⇒ 只有散文说「买」时不判误伤。"""
    plan = _incident_plan()
    plan["ta_screening"]["rejected"] = [
        {"code": "563020", "name": "红利低波ETF易方达", "reason": "", "source_section": "研究经理"},
    ]
    plan["llm_targets"].pop("563020", None)
    plan["llm_final_decision"]["targets"]["563020"] = {"direction": "不建仓", "target_pct": 0.0}
    md = "## 五、方向性判断汇总\n\n512800 **减**；563020 **买**（16%…）—— 这只是散文。\n"
    verdict = P.find_ta_reject_mis_hits(plan, md)
    assert verdict["mis_hits"] == [], "散文里的「买」不许当成买侧证据"
    assert verdict["status"] == "pass"


def test_no_evidence_at_all_is_not_applicable() -> None:
    """两条证据都拿不到 ⇒ 判「不适用（≠ 通过）」，不许默默记成通过。"""
    plan = _incident_plan()
    plan["llm_targets"].pop("515450")
    plan["llm_final_decision"]["targets"].pop("515450", None)
    plan["ta_screening"]["rejected"] = [
        {"code": "515450", "name": "", "reason": "", "source_section": "研究经理"},
    ]
    verdict = P.find_ta_reject_mis_hits(plan, "# 决策纪要（没有表格行）\n")
    assert verdict["status"] == "na", f"必须判不适用，实得 {verdict['status']}"
    assert verdict["unjudged"] == ["515450"]
    assert "不适用 ≠ 通过" in verdict["detail"]


def test_screening_disabled_is_not_applicable() -> None:
    """本轮没跑 TA 合规筛选 ⇒ 无从校验 ⇒ 不适用（≠ 通过）。"""
    plan = _incident_plan()
    plan["ta_screening"]["enabled"] = False
    verdict = P.find_ta_reject_mis_hits(plan, _incident_md())
    assert verdict["status"] == "na", f"必须判不适用，实得 {verdict['status']}"
    assert "不适用 ≠ 通过" in verdict["detail"]


def test_empty_rejected_list_is_zero_mis_hit() -> None:
    """否决名单为空 ⇒ 零误伤（空集恒满足该不变量）。"""
    plan = _incident_plan()
    plan["ta_screening"]["rejected"] = []
    verdict = P.find_ta_reject_mis_hits(plan, _incident_md())
    assert verdict["status"] == "pass", f"空否决名单必须 PASS，实得 {verdict['status']}"


def test_direction_classifier_is_closed_set() -> None:
    """方向词表是**闭集**：修饰词可剥、带条件的半句**不算**方向、ASCII 大小写不敏感。"""
    assert P._classify_direction("买") == "买"
    assert P._classify_direction("**买**") == "买"
    assert P._classify_direction("小买") == "买"
    assert P._classify_direction("buy") == "Buy"
    assert P._classify_direction("不买") == "不买"
    assert P._classify_direction("不建仓") == "不建仓"
    # 🔴 带条件的半句是熊方附条件意见，**不是** TA 的裁决 ⇒ 不许当买侧证据
    assert P._classify_direction("买，但仅在数据复核通过后") is None
    assert P._is_buy_side(P._classify_direction("不买")) is False
    assert P._is_buy_side(P._classify_direction("增持")) is True


def test_md_table_parsing_ignores_prose_cells() -> None:
    """「与 515450 相关性 0.917」这种**顺带提到**代码的格子**不算**代码格。"""
    md = "\n".join([
        "| 标的 | 方向 | 核心理由 |",
        "|---|---:|---|",
        "| **563020** | **不买** | 未被规则选入，且与515450相关性0.917。 |",
    ])
    assert P._md_directions(md, "563020") == [("md:L3", "不买")]
    assert P._md_directions(md, "515450") == [], "顺带提到的代码不许继承别人的方向"


def test_md_table_parsing_on_real_decision_fixture() -> None:
    """真决策件夹具 ⇒ 表格行的方向格解析正确（买 / 不买 / 0仓观察）。"""
    md = _FIXTURE_MD.read_text(encoding="utf-8")
    buy = P._md_directions(md, "515450")
    assert any(direction == "买" for _, direction in buy), f"515450 应取到买侧方向，实得 {buy}"
    # 「持（0仓观察）」不是买侧 ⇒ 159207 不得被误判成买
    assert not P._is_buy_side(P._classify_direction("持（0仓观察）"))
    assert "持（0仓观察）" not in {d for _, d in P._md_directions(md, "159207")}


def test_gate_survives_plan_roundtrip() -> None:
    """交付件计划经过 JSON 往返（磁盘形态）后判定不变。"""
    plan = _incident_plan()
    reloaded = json.loads(json.dumps(plan, ensure_ascii=False))
    assert copy.deepcopy(plan) == reloaded
    assert P.find_ta_reject_mis_hits(reloaded, _incident_md())["status"] == "fail"
