r"""🔴 2026-11-11（第九轮）：**TA 一票否决的误伤**四类根因回归（真实交付件实证）。

事故（G-TA1 产物级闸门实测红灯，2026-11-11 14:54 研究链 canonical 根 / 每日链快照 `20261111_142244`）：

  **TA 明确要买、规则层真实入选的标的，被当成「一票否决」剔出计划** ⇒ 席位顺延给更弱的标的。

  研究链 canonical 根：
    md「`- 即使否决 \`515450\`，它也不能作为顺延者：两者同属红利族……`」
    ⇒ 「否决 515450」是**假设**（讲顺延资格）⇒ 却被当成**真否决**
    ⇒ 515450（最终分 0.8672、动量 +6.31%、站上 MA60、未拥挤、TA 权威表写「买（条件性）」）
       被踢出计划 ⇒ 顺延位只能给 **563020**（最终分 −0.4683）⇒ **误伤**。

  每日链快照（**另两处**）：
    ① 「`| \`512800\` | **减，保留** | …不作正式否决 |`」⇒ 否定护栏词表缺「不作」⇒ 512800 被误否决
       ⇒ 规则层与 TA 都说「减到 16%」，交付件却「维持 22.20% 不卖」。
    ② 「……`**\`512800\`卖出超配部分、保留16%核心；\`515450\`复核通过后买入；……；
        弱信号、拥挤及同族重复候选否决；……」⇒ 末句「否决」是**泛指**（没有自己的代码）
       ⇒ 旧口径**继承上一子句的代码** ⇒ 把 512800 / 515450 一起误否决。

本文件钉死**四条护栏**（每条都配**变异反证**证明它有牙）：

  ① **让步/假设连词**（即使/即便/就算/哪怕/纵使/假设/假定）管辖的否决动词 ⇒ 不算否决；
     但**只有紧邻**才认（「即使拥挤度 92% 也不买 X」仍是真否决）。
  ② **自带裁决的代码**（买/卖/减/保留/持有…）**不许**被下一子句继承成否决主体。
  ③ **否定护栏**覆盖「不作正式否决」这类形式。
  ④ **「避免与 X 重复/重叠」**里的 X 是**被保留的那只**，不是被否决的那只。

本文件自带夹具，不依赖服务器产物、不联网。
"""

from __future__ import annotations

import importlib.util
import pathlib
import re
import sys

_ETF_DIR = pathlib.Path(__file__).resolve().parents[1]
if str(_ETF_DIR) not in sys.path:
    sys.path.insert(0, str(_ETF_DIR))

_LLF_PATH = _ETF_DIR / "llm_final.py"


def _load_llm_final():
    spec = importlib.util.spec_from_file_location("_llf_round3_under_test", _LLF_PATH)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


LLF = _load_llm_final()


def _codes(text: str) -> list[str]:
    return [r["code"] for r in LLF.extract_rejections(text)]


# ------------------------------------------------------------------ 夹具（真实 md 原文节选）

#: 研究经理段：「即使否决 515450，它也不能作为顺延者」——**假设句**，不是裁决。
_SNIPPET_HYPOTHETICAL = (
    "- `563020` 实际是候选但**未被规则层选中**，不是当前目标 `515450`。\n"
    "- 即使否决 `515450`，它也不能作为顺延者：两者同属红利族，而顺延必须选择不同主题资产。\n"
    "- 保留相对信号较好的 `515450`，但否决最弱的 `515650`：`515650` 最终分 **-1.0432**、E为 **-0.8598**，拥挤分位 **92%**。\n"
)

#: 风险裁决总结句：末句「否决」是**泛指**；被继承的两只**各自已有裁决**。
_SNIPPET_INHERIT = (
    "综上，最终执行方向是：**`512800`卖出超配部分、保留16%核心；`515450`复核通过后买入；"
    "其余非目标旧仓退出；弱信号、拥挤及同族重复候选否决；防守槽位全部留现金。\n"
)

#: 表格行：方向格写「减，保留」，理由格里出现「**不作正式否决**」⇒ 不是否决。
_SNIPPET_NOT_VETO_ROW = (
    "| 标的 | 方向 | 理由 |\n"
    "| `512800` | **减，保留** | 从当前22.54%回到规则层16%目标；不加到20%，不作正式否决 |\n"
)

#: 「避免与 X 重复」：X 是**被保留的那只**，前三个才是被否的。
_SNIPPET_AVOID_REF = (
    "- `515020`、`516310`、`512820` 不买，避免与 `512800` 重复银行暴露；"
    "其他红利低波同族产品不买，避免与 `515450` 重复\n"
)


# ------------------------------------------------------------------ ① 让步/假设句

def test_hypothetical_clause_is_not_a_veto():
    """「即使否决 515450」是**假设** ⇒ 515450 不许被判成一票否决。"""
    assert "515450" not in _codes(_SNIPPET_HYPOTHETICAL)


def test_hypothetical_passive_form_is_not_a_veto():
    """「即使 515450 被否决」「假设 515450 被否决」⇒ 同样不作数。"""
    for text in (
        "- 即使 `515450` 被否决，那么顺延给 `563020`\n",
        "- 假设 `515450` 被否决，那么顺延给 `563020`\n",
        "- 假定 `515450` 被否决，那么顺延给 `563020`\n",
    ):
        assert "515450" not in _codes(text), text


def test_real_vetoes_in_same_sentence_still_veto():
    """同一句里的**真否决**（否决最弱的 515650）不许被放过。"""
    assert "515650" in _codes(_SNIPPET_HYPOTHETICAL)


def test_concessive_with_content_is_still_a_real_veto():
    """让步词与动词之间**夹了内容** ⇒ 仍是真否决（护栏不许过宽）。"""
    for text in (
        "- 即使拥挤度 92% 也不买 `515650`\n",
        "- 即使拥挤度 92%，也不买 `515650`\n",
        "- 即便量能放大也要否决 `515650`\n",
    ):
        assert "515650" in _codes(text), text


# ------------------------------------------------------------------ ② 跨子句继承

def test_generic_veto_does_not_inherit_decided_codes():
    """泛指的「弱信号、拥挤及同族重复候选否决」⇒ 不许把已裁决的 512800 / 515450 拖下水。"""
    codes = _codes(_SNIPPET_INHERIT)
    assert "512800" not in codes
    assert "515450" not in codes


def test_keep_and_buy_clauses_keep_their_codes():
    """「保留相对信号较好的 515450，但否决最弱的 515650」⇒ 515650 否决、515450 不否决。"""
    codes = _codes("- 保留相对信号较好的 `515450`，但否决最弱的 `515650`：`515650` 最终分 **-1.0432**。\n")
    assert "515650" in codes
    assert "515450" not in codes


# ------------------------------------------------------------------ ③ 否定护栏

def test_not_a_formal_veto_row_is_not_vetoed():
    """「减，保留 …… 不作正式否决」⇒ 512800 不许被否决。"""
    assert "512800" not in _codes(_SNIPPET_NOT_VETO_ROW)


def test_real_veto_row_still_vetoed():
    """「不买，正式否决」的方向格 ⇒ 仍判否决（护栏不许把真否决也放过）。"""
    row = "| 标的 | 方向 | 理由 |\n| `563020` | **不买，正式否决** | E 为负、拥挤分位 100% |\n"
    assert "563020" in _codes(row)


# ------------------------------------------------------------------ ④ 「避免与 X 重复」

def test_avoid_reference_code_is_not_vetoed():
    """「避免与 512800 重复」⇒ 512800 是**被保留的那只**，不是被否决的那只。"""
    codes = _codes(_SNIPPET_AVOID_REF)
    assert "512800" not in codes
    assert "515450" not in codes
    for code in ("515020", "516310", "512820"):
        assert code in codes, code


# ------------------------------------------------------------------ 变异反证（每条护栏都承重）

def _restore(saved):
    for key, value in saved.items():
        setattr(LLF, key, value)


def test_mutation_hypothetical_guard_is_load_bearing():
    """关掉让步/假设护栏 ⇒ 515450 立刻被误伤。"""
    saved = {"_HYPOTHETICAL_MARKERS": LLF._HYPOTHETICAL_MARKERS}
    try:
        LLF._HYPOTHETICAL_MARKERS = ()
        assert "515450" in _codes(_SNIPPET_HYPOTHETICAL), "护栏关掉后必须重新误伤"
    finally:
        _restore(saved)


def test_mutation_own_decision_guard_is_load_bearing():
    """关掉「自带裁决」护栏 ⇒ 512800 / 515450 立刻被误伤。"""
    saved = {"_OWN_DECISION_WORDS": LLF._OWN_DECISION_WORDS}
    try:
        LLF._OWN_DECISION_WORDS = ()
        # 「继承」是**逐子句覆盖**的 ⇒ 末句只会继承**最后一个**有代码的子句（515450）
        # ⇒ 512800 在本夹具里另有护栏（见 ④ 的引用护栏），本变异只盯 515450。
        assert "515450" in _codes(_SNIPPET_INHERIT), "护栏关掉后必须重新误伤"
    finally:
        _restore(saved)


def test_mutation_negation_guard_is_load_bearing():
    """从否定护栏里去掉「不作」⇒ 512800 立刻被误伤。"""
    saved = {"_REJECT_NEGATION_RE": LLF._REJECT_NEGATION_RE}
    try:
        pattern = LLF._REJECT_NEGATION_RE.pattern.replace("|不作|不做", "")
        assert pattern != LLF._REJECT_NEGATION_RE.pattern, "否定护栏词表形态变了 ⇒ 变异无效"
        LLF._REJECT_NEGATION_RE = re.compile(pattern)
        assert "512800" in _codes(_SNIPPET_NOT_VETO_ROW), "护栏关掉后必须重新误伤"
    finally:
        _restore(saved)


def test_mutation_avoid_ref_guard_is_load_bearing():
    """关掉「避免与 X 重复」引用护栏 ⇒ 512800 立刻被误伤。"""
    saved = {"_MD_REF_AVOID_RE": LLF._MD_REF_AVOID_RE}
    try:
        LLF._MD_REF_AVOID_RE = re.compile(r"(?!x)x")
        assert "512800" in _codes(_SNIPPET_AVOID_REF), "护栏关掉后必须重新误伤"
    finally:
        _restore(saved)


# ------------------------------------------------------------------ ⑤ 探针 A20⑥ 的「部分卖出」口径

def _load_probe():
    probe_path = _ETF_DIR / "probes" / "probe_llm_parser.py"
    spec = importlib.util.spec_from_file_location("_plp_round3_under_test", probe_path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


PLP = _load_probe()


def test_probe_a20_partial_sell_is_not_zero_direction():
    """🔴 A20⑥ 假红（每日链快照 `20261111_142244`）：

    「…**`512800`卖出超配部分、保留16%核心」⇒ 裸「卖出」命中零方向
    ⇒ 探针把 512800 判成**清仓**⇒ 而计划里 512800 是「减至 16%」⇒ **假红**。
    ⇒ 子句里有**持有语义** ⇒ 「卖超配、留底仓」⇒ **部分卖出**，不是零方向。
    """
    clause = "最终执行方向是：**`512800`卖出超配部分、保留16%核心"
    assert PLP._md_clause_zero_word(clause) is None


def test_probe_a20_real_liquidation_is_still_zero_direction():
    """真清仓 ⇒ 仍判零方向（护栏不许把真零方向放过）。"""
    assert PLP._md_clause_zero_word("全部卖出 `512800`，清仓") is not None
    assert PLP._md_clause_zero_word("`563020` 不买，不建仓") is not None


def test_mutation_probe_partial_sell_guards_are_load_bearing():
    """变异：关掉「持有语义」词表 **与** 「卖出…部分」正则
    ⇒ 512800 立刻被误判成零方向 ⇒ 证明这两道护栏承重。

    🔴 两道护栏是**并联**的（任一命中即判部分卖出）⇒ 变异必须同时关掉两���，
       否则会「测不出东西」（本文件第一版就踩过这个坑）。
    """
    saved = {
        "_MD_KEEP_TOKENS": PLP._MD_KEEP_TOKENS,
        "_MD_SELL_PARTIAL_RE": PLP._MD_SELL_PARTIAL_RE,
    }
    try:
        PLP._MD_KEEP_TOKENS = ()
        PLP._MD_SELL_PARTIAL_RE = re.compile(r"(?!x)x")
        clause = "最终执行方向是：**`512800`卖出超配部分、保留16%核心"
        assert PLP._md_clause_zero_word(clause) is not None, "护栏关掉后必须重新误判"
    finally:
        for key, value in saved.items():
            setattr(PLP, key, value)
