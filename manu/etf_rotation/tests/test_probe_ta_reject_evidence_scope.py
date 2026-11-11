"""🔴 2026-11-11（闸门**假绿**修复）：产物级闸门「TA 否决名单零误伤」的**证据口径**回归。

【假绿实录】
服务器快照 `20261111_122825`：
  · `final_rebalance_plan.json` 的 `ta_screening.rejected` =
    ['515450'（研究经理）, '512170'（研究经理）, '159207'（研究经理）,
     '563020'（研究经理）, '511990'（风险裁决）]；
  · 决策 md `tradingagents_decision.md` **L647**（`## 六、最终交易决定（原始输出）` 段落下）
    `- **515450：条件性买入12%**，最终分0.6576…` ⇒ **层级 40 的买侧证据**；
  · 旧实现只读**表格行** ⇒ 看不见 L647 ⇒ 又 L242（多头段落的表格行）层级 0
    ⇒ 买侧证据层级 0 < 否决层级 10 ⇒ 闸门判 `pass`（「无一只落在买侧 ⇒ 零误伤」）
    ⇒ **假绿**（真否决里混着 TA 明写要买的 515450）。

【2026-11-11 13:0x 补记】该快照的**派生件已就地重生成**（5 个真缺陷修完）⇒ 它的
计划件**不再**含假绿 ⇒ 本文件的断言对象改为**留档的缺陷件**
（`server_runs_etf_rotation/_defect_backup_20261111/`，只读证据）⇒ 断言**一字未放松**。

【两个被修的缺陷】
  1. `_md_row_ranks()`：层级**不继承祖先标题**（只取「该行之前最后一个标题」）
     ⇒ 子标题下的行拿不到祖先（`## 六、最终交易决定（原始输出）`）的层级 40
     ⇒ 修法：**markdown 标题栈语义**（标题遇到同级或更高级标题才关闭）
     ⇒ 某行层级 = 所有仍然开着的祖先标题层级里的**最大值**；
  2. `_md_directions()`：只读**表格行**，看不见权威段落里**逐标的裁决的列表行**
     ⇒ 修法：**只放开层级 ≥ 10 的段落里的列表行**（`- ` / `* ` / `N. ` / `N、`）
     —— 普通散文段落**仍然不算证据**（散文归属正是老事故根因）。

【口径要点】
  · 层级：观点 0 < 研究经理 10 < 辩论/风险裁决 20 < Trader 30 < 最终交易决定 40；
  · 买侧证据的层级 **≥ 否决的层级** 才算误伤（同层自相矛盾 ⇒ 归属错位）；
  · 列表行证据：方向词必须落在**代码之后** ≤ 12 字内、代码之间不许夹别的代码、
    否定（「不应建仓」）不算方向、方向词**复用** `_classify_direction` 闭集。

本文件与 `tests/test_probe_ta_reject_zero_mishit.py`（判据口径）、
`tests/test_probe_ta_reject_authority_rank.py`（层级判据）互补，不重复它们。
"""

from __future__ import annotations

import json
from pathlib import Path

from manu.etf_rotation.probes import probe_verify_guards as P  # type: ignore  # noqa: E402

#: 真实交付件快照（**仓库外**路径 ⇒ 不在本机就 skip，不许让测试在别的机器上崩）
_REAL_SNAPSHOT = Path(
    "/media/manu/1TB-Volume/workspace/etf_rotation_20261110/server_runs_etf_rotation/snapshots"
)
#: 2026-11-11 12:28 那份**假绿**交付件（**当时的**快照目录）
_FALSE_GREEN_SNAPSHOT = _REAL_SNAPSHOT / "20261111_122825"
#: 🔴 2026-11-11 13:0x：**该快照的派生件已被就地重生成**（否决误伤等 5 个缺陷修完）
#:    ⇒ 它的 `final_rebalance_plan.json` **不再**含假绿 ⇒ 本测试的断言对象改成
#:    **留档的缺陷件**（`_defect_backup_20261111/`，服务器侧同步一份，只读证据）。
#:    ⇒ 决策 md **逐字节未变**（分析层没动）⇒ 两处取到的 md 内容完全相同。
_DEFECT_BACKUP = (
    _REAL_SNAPSHOT.parent / "_defect_backup_20261111"
)


def _false_green_plan_path() -> Path:
    """返回**含假绿**的那份计划件路径（优先留档，其次活快照）。"""
    backup = _DEFECT_BACKUP / "final_rebalance_plan.json"
    if backup.is_file():
        return backup
    return _FALSE_GREEN_SNAPSHOT / "final_rebalance_plan.json"


def _false_green_md_path() -> Path:
    """返回配套的决策 md 路径（留档目录优先，其次活快照）。"""
    backup = _DEFECT_BACKUP / "tradingagents_decision.md"
    if backup.is_file():
        return backup
    return _FALSE_GREEN_SNAPSHOT / "tradingagents_decision.md"


def _plan(rejected: list[tuple[str, str]]) -> dict:
    """合成计划件：`rejected` = [(代码, 否决出处章节)]。"""
    return {
        "ta_screening": {
            "enabled": True,
            "rejected": [
                {"code": code, "name": "", "reason": "", "source_section": section}
                for code, section in rejected
            ],
        },
        "llm_targets": {},
        "llm_final_decision": {"available": True, "targets": {}},
    }


def _buy_side_directions(md: str, code: str) -> list[tuple[str, str]]:
    """该代码在 md 里的**买侧**方向证据 ⇒ `[(来源, 方向词)]`。"""
    return [
        (src, direction)
        for src, direction in P._md_directions(md, code)
        if P._is_buy_side(P._classify_direction(direction))
    ]


# ---------------------------------------------------------------- 1. 真实交付件（假绿 → 判红）

def test_real_false_green_delivery_now_fails_with_515450() -> None:
    """真实交付件 `20261111_122825` ⇒ 闸门必须判 `fail` 且误伤含 `515450`（假绿被拆穿）。"""
    plan_path = _false_green_plan_path()
    md_path = _false_green_md_path()
    if not plan_path.is_file() or not md_path.is_file():
        import pytest  # noqa: PLC0415

        pytest.skip(f"真实交付件不在本机：{_FALSE_GREEN_SNAPSHOT}")
    plan = json.loads(plan_path.read_text(encoding="utf-8"))
    md = md_path.read_text(encoding="utf-8")

    verdict = P.find_ta_reject_mis_hits(plan, md)
    assert verdict["status"] == "fail", f"假绿必须被拆穿，实得 {verdict['status']}：{verdict['detail']}"
    mis_hit_codes = [hit["code"] for hit in verdict["mis_hits"]]
    assert "515450" in mis_hit_codes, f"误伤必须含 515450，实得 {mis_hit_codes}"
    # 否决层级 10（研究经理）< 买侧层级 40（最终交易决定）⇒ 误伤成立
    hit = next(hit for hit in verdict["mis_hits"] if hit["code"] == "515450")
    assert hit["buy_rank"] == 40, f"买侧层级必须是 40（最终交易决定），实得 {hit['buy_rank']}"
    assert hit["veto_rank"] == 10, f"否决层级必须是 10（研究经理），实得 {hit['veto_rank']}"
    assert "md:L647" in {src for src, _d in hit["evidence"]}, (
        f"误伤证据必须含最终交易决定段的 L647，实得 {hit['evidence']}"
    )


def test_real_false_green_delivery_keeps_true_vetoes_clean() -> None:
    """同一份交付件里 159207 / 563020 / 511990 是**真否决** ⇒ 不得产生买侧证据。"""
    md_path = _FALSE_GREEN_SNAPSHOT / "tradingagents_decision.md"
    if not md_path.is_file():
        import pytest  # noqa: PLC0415

        pytest.skip(f"真实交付件不在本机：{_FALSE_GREEN_SNAPSHOT}")
    md = md_path.read_text(encoding="utf-8")
    for code in ("159207", "563020", "511990"):
        buy = _buy_side_directions(md, code)
        assert not buy, f"{code} 是真否决（L650「不买」）⇒ 不得有买侧证据，实得 {buy}"


# ---------------------------------------------------------------- 2/3. 层级：祖先继承 vs 观点层

def test_final_decision_list_row_is_authority_40() -> None:
    """`## 六、最终交易决定（原始输出）` 下的列表行 ⇒ 层级 40（买侧证据算数）。"""
    md = "\n".join([
        "# TradingAgents 决策纪要 · ETF 轮动",
        "",
        "## 六、最终交易决定（原始输出）",
        "",
        "- **512170：条件性买入最多4%**，最终分-0.5848、E仅0.0133。",
    ])
    assert P._md_row_ranks(md)[5] == 40, "最终交易决定段的列表行层级必须是 40"
    assert P._md_direction_ranks(md, "512170")["md:L5"] == 40
    buy = _buy_side_directions(md, "512170")
    assert buy, "权威段落里逐标的裁决的列表行必须能取到买侧方向"


def test_opinion_section_list_row_is_authority_0() -> None:
    """`### 多头（Bull）` 下的同一行 ⇒ 层级 **0**（观点层，没有否决权 ⇒ 不算裁决）。"""
    md = "\n".join([
        "# TradingAgents 决策纪要 · ETF 轮动",
        "",
        "### 多头（Bull）",
        "",
        "- **512170：条件性买入最多4%**，最终分-0.5848、E仅0.0133。",
    ])
    assert P._md_row_ranks(md)[5] == 0, "多头（观点层）的列表行层级必须是 0，不得变成 40"
    # 层级 < 10 的行**根本不作为证据**（观点层没有否决权）
    assert P._md_directions(md, "512170") == [], "观点层的列表行不得作为方向证据"
    # 观点层的「买」推不翻研究经理层的否决
    plan = _plan([("512170", "研究经理")])
    verdict = P.find_ta_reject_mis_hits(plan, md)
    assert verdict["status"] != "fail", f"观点层买侧证据不得判红，实得 {verdict['status']}"
    assert [hit["code"] for hit in verdict["mis_hits"]] == []


def test_sub_heading_inherits_ancestor_authority() -> None:
    """缺陷 1 的正解：权威段落**再开一级小节** ⇒ 小节里的行**继承**祖先层级 40。"""
    md = "\n".join([
        "# TradingAgents 决策纪要 · ETF 轮动",
        "",
        "## 六、最终交易决定（原始输出）",
        "",
        "### 逐标的结论",
        "",
        "- **512170：条件性买入最多4%**，最终分-0.5848、E仅0.0133。",
    ])
    assert P._md_row_ranks(md)[7] == 40, "子标题下的行必须继承祖先（最终交易决定）的层级 40"


# ---------------------------------------------------------------- 4. 权威段落里的真否决

def test_authoritative_veto_list_row_is_not_buy_side() -> None:
    """`- **159207、563020：不买**` ⇒ **真否决**，不得产生买侧证据。"""
    md = "\n".join([
        "# TradingAgents 决策纪要 · ETF 轮动",
        "",
        "## 六、最终交易决定（原始输出）",
        "",
        "- **159207、563020：不买**；515020 也不换入。",
    ])
    for code in ("159207", "563020"):
        buy = _buy_side_directions(md, code)
        assert not buy, f"{code} 是真否决 ⇒ 不得有买侧证据，实得 {buy}"
    plan = _plan([("159207", "研究经理"), ("563020", "研究经理")])
    verdict = P.find_ta_reject_mis_hits(plan, md)
    assert verdict["status"] != "fail", f"真否决不得判红，实得 {verdict['status']}"
    assert [hit["code"] for hit in verdict["mis_hits"]] == []


def test_negated_direction_in_list_row_is_not_buy_side() -> None:
    """`- **563020不应建仓，更不应给20%。**` ⇒ 被否定的方向，不是买侧证据。"""
    md = "\n".join([
        "# TradingAgents 决策纪要 · ETF 轮动",
        "",
        "## 六、最终交易决定（原始输出）",
        "",
        "4. **563020不应建仓，更不应给20%。**",
    ])
    assert not _buy_side_directions(md, "563020"), "被否定的方向不得算买侧证据"
    plan = _plan([("563020", "研究经理")])
    verdict = P.find_ta_reject_mis_hits(plan, md)
    assert verdict["status"] != "fail", f"被否定的方向不得判红，实得 {verdict['status']}"


def test_direction_of_another_code_is_not_attributed() -> None:
    """方向词只算**它自己那个标的**的 ⇒ 代码与方向词之间夹别的代码 ⇒ 不回挂。"""
    md = "\n".join([
        "# TradingAgents 决策纪要 · ETF 轮动",
        "",
        "## 六、最终交易决定（原始输出）",
        "",
        "- 买入 `515450` **5%**、试探买入 `515650` **2%**。",
    ])
    # 「买入 515450」归 515450、「试探买入 515650」归 515650 ⇒ 每个代码**只有一条**证据
    # （若互相回挂，515450 会拿到 515650 的那条 ⇒ 变成两条）
    assert len(_buy_side_directions(md, "515450")) == 1, "515650 的方向不许挂到 515450 头上"
    assert len(_buy_side_directions(md, "515650")) == 1


def test_far_direction_before_code_is_not_attributed() -> None:
    """方向词离代码太远（前面）⇒ **不许**回挂（「后续加仓的唯一条件：512170 …」）。"""
    md = "\n".join([
        "# TradingAgents 决策纪要 · ETF 轮动",
        "",
        "## 六、最终交易决定（原始输出）",
        "",
        "5. 后续加仓的唯一条件：512170 的 E 转正且 515450 未破 MA60 时，才把 512170 补到 16%。",
    ])
    assert not _buy_side_directions(md, "512170"), "远处的前置方向词不许回挂到代码上"
    assert not _buy_side_directions(md, "515450")


# ---------------------------------------------------------------- 5. 普通散文仍然不是证据

def test_prose_paragraph_is_not_evidence() -> None:
    """普通散文段落（非列表行）里的「买」⇒ **不得**作为证据（上一轮的设计意图不许丢）。"""
    md = "\n".join([
        "# TradingAgents 决策纪要 · ETF 轮动",
        "",
        "## 六、最终交易决定（原始输出）",
        "",
        "关于 512170：最终分-0.5848，条件性买入只在复核通过后才执行。",
    ])
    assert P._md_directions(md, "512170") == [], "散文段落不得作为方向证据"
    plan = _plan([("512170", "研究经理")])
    verdict = P.find_ta_reject_mis_hits(plan, md)
    assert verdict["status"] != "fail", f"散文不得判红，实得 {verdict['status']}"
    assert [hit["code"] for hit in verdict["mis_hits"]] == []


# ---------------------------------------------------------------- 6. 变异负例：闸门必须有牙

def test_mutation_injecting_buy_side_code_into_rejected_fails() -> None:
    """把一个买侧代码塞进 `ta_screening.rejected` ⇒ 闸门必须 FAIL（证明有牙）。"""
    md = "\n".join([
        "# TradingAgents 决策纪要 · ETF 轮动",
        "",
        "## 六、最终交易决定（原始输出）",
        "",
        "- **512170：条件性买入最多4%**，最终分-0.5848、E仅0.0133。",
    ])
    plan = _plan([])
    assert P.find_ta_reject_mis_hits(plan, md)["status"] != "fail", "基线（不在否决名单里）不该判红"

    mutated = P._inject_rejected_code(plan, "512170")
    for row in mutated["ta_screening"]["rejected"]:
        row["source_section"] = "研究经理"
    verdict = P.find_ta_reject_mis_hits(mutated, md)
    assert verdict["status"] == "fail", "买侧代码被塞进否决名单 ⇒ 闸门必须 FAIL"
    assert "512170" in [hit["code"] for hit in verdict["mis_hits"]]


def test_mutation_lowering_buy_row_to_opinion_level_passes() -> None:
    """变异反证：把同一行挪到观点层（多头）⇒ 闸门不再判红（层级判据在起作用）。"""
    buy_md = "\n".join([
        "# TradingAgents 决策纪要 · ETF 轮动",
        "",
        "## 六、最终交易决定（原始输出）",
        "",
        "- **512170：条件性买入最多4%**，最终分-0.5848、E仅0.0133。",
    ])
    opinion_md = "\n".join([
        "# TradingAgents 决策纪要 · ETF 轮动",
        "",
        "### 多头（Bull）",
        "",
        "- **512170：条件性买入最多4%**，最终分-0.5848、E仅0.0133。",
    ])
    plan = _plan([("512170", "研究经理")])
    assert P.find_ta_reject_mis_hits(plan, buy_md)["status"] == "fail"
    assert P.find_ta_reject_mis_hits(plan, opinion_md)["status"] != "fail"
