r"""🔴 2026-11-11（第九轮续）：**第 5 组根因** —— 决策件「散文体权威节」下的否决误伤/漏放回归。

事故（2026-11-11 15:52 真机跑批，每日链快照 `20261111_152132`）：

  本轮新决策件的权威节是**散文 + 编号条目**形态（不是上一轮的表格形态），
  于是一口气暴露 **4 处**真缺陷（全是 `llm_final.py` 的否决归属层）：

  ① **假护栏**：「一票否决」被当成**否定词**放进 `_REJECT_NEGATIONS`
     ⇒ 任何含这四个字的子句被 `_guarded()` 整条中和
     ⇒ 「516620 **正式一票否决**，不买」这种**真否决**被**放过**（漏放）
     ⇒ 该标的没进否决名单 ⇒ 席位顺延给本该被否的标的。
  ② **第三方立场的元指代**：「熊方的**否决**拿不出更优替代」
     ⇒ 否决权属于**对方**，不是对子句里那只标的的否决
     ⇒ 512800（研究经理明写「持 / 减至目标权重」）被误判成否决 ⇒ 计划把它变成「维持」席位。
  ③ **点名裁决不生效**：权威节里逐只点名的编号条目（`1) 512800（…）：方向「持 / 减至目标权重」`）
     是这一只**本轮的直接裁决**，散文推断**不得推翻**它。
  ④ **弱化限定词**：「对 `512800` 本体的一票否决**理由不足**」⇒ 这条否决**不成立**；
     以及「**不能**仅因模型存在局限就一票否决 `512800`」⇒ 否定式否决（护栏词表缺「不能」）。

本文件钉死这 4 条护栏（每条配**变异反证**证明它有牙），并给出**真实件**的端到端断言。

本文件自带夹具，不依赖服务器产物、不联网。
"""

from __future__ import annotations

import importlib.util
import pathlib
import sys

_ETF_DIR = pathlib.Path(__file__).resolve().parents[1]
if str(_ETF_DIR) not in sys.path:
    sys.path.insert(0, str(_ETF_DIR))

_LLF_PATH = _ETF_DIR / "llm_final.py"


def _load_llm_final():
    spec = importlib.util.spec_from_file_location("_llf_round3b_under_test", _LLF_PATH)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


LLF = _load_llm_final()


def _codes(text: str) -> list[str]:
    return [r["code"] for r in LLF.extract_rejections(text)]


# ------------------------------------------------------------------ 夹具（真实 md 原文节选）

#: 权威节「Strategic Actions」编号条目（真实 md `20261111_152132` 原文节选）。
_SNIPPET_EXPLICIT_ITEMS = (
    "1) 512800（银行ETF华宝）：方向「持 / 减至目标权重」。目标 16% ≈ 4,776.75 元；"
    "以信号日价 0.874 元计约 5,465 股，从现有 7,700 股**净卖出约 2,200–2,300 股**"
    "——不下调到 0（否决熊方），不抬到 20%（超配 + 因子簇相关性 0.83 预警）。\n"
    "2) 515450（红利低波50ETF南方）：建仓至标准槽位 16%（≈4,776.75 元，股数按 12/1 价格由规则层折算）。"
    "生效前提：12/1 重跑后仍站上 MA60、mom120>0、E>0、上游非退化检查通过。\n"
    "4) 516620（影视ETF国泰）：**正式一票否决，不买**（E -0.3617、最终分 -0.4777、池内分位 100%）。\n"
    "5) 159207（高股息ETF广发）：不建仓（最终分 -0.5089、与 515450 相关性 0.834 属红利簇重复）。\n"
)

#: 第三方立场的元指代（真实 md 同段原文节选）。
_SNIPPET_META_VETO = (
    "决定最终落在 512800「Overweight（保留为组合锚定仓）」而不是熊方的「卖」的是三条："
    "第一，熊方的否决拿不出更优替代——515020/516310/512820 与 512800 成分重叠 0.986–0.991、"
    "相关性 0.981–0.990，连最容易翻的一对 512800↔512820 最终分也只差 0.01691\n"
)

#: 被弱化的否决（真实 md 研究经理段原文节选）。
_SNIPPET_WEAKENED_VETO = (
    "空头对组合风险层的担忧成立，但对 `512800` 本体的一票否决理由不足：\n"
)

#: 否定式否决（真实 md 研究经理段原文节选）。
_SNIPPET_NEGATED_VETO = (
    "因此，提示中既没有可核验的突发财务或政策黑天鹅，也没有达到规则冲突阈值，"
    "不能仅因模型存在局限就一票否决`512800`\n"
)

#: 括号里的否决：`(否决 159207)` 的对象是 **159207**，不是句首出现的 515450。
_SNIPPET_PAREN_VETO = (
    "正确做法是限制红利簇只留 515450 一只（否决 159207），而不是砍掉组合里最强的一只\n"
)

#: 配套动作总结句（真实 md 风险裁决段原文节选）。
_SNIPPET_SUMMARY = (
    "配套动作：515450 条件性建仓至 16%；515650 暂缓（放量 1.80／分位 94% 双阈值触发）；"
    "516620、159207、563020 以及 515020/516310/512820 正式一票否决；"
    "防守槽位保留 0 只、未配置权重全部以现金体现\n"
)


# ------------------------------------------------------------------ ① 「一票否决」不是否定词

def test_one_vote_veto_is_not_treated_as_negation():
    """🔴「正式一票否决」是**真否决** ⇒ 旧口径把它当否定词 ⇒ 整条被中和（漏放）。"""
    assert LLF._guarded("516620 正式一票否决") is False, "「一票否决」不许再当否定词"
    assert "516620" in _codes("516620 正式一票否决（E -0.3617、最终分 -0.4777）")


def test_one_vote_veto_mutation_guard_is_load_bearing():
    """变异反证：把「一票否决」塞回否定词表 ⇒ 上一条必须变红。"""
    saved = LLF._REJECT_NEGATIONS
    try:
        LLF._REJECT_NEGATIONS = ("一票否决",) + saved
        assert LLF._guarded("516620 正式一票否决") is True, "变异后应当被中和（证明护栏承重）"
    finally:
        LLF._REJECT_NEGATIONS = saved


def test_conditional_one_vote_veto_is_still_guarded():
    """「若一票否决」是**假设** ⇒ 仍须被中和（不许因修 ① 而放过假设句）。"""
    assert LLF._guarded("若一票否决 512800，则顺延") is True


# ------------------------------------------------------------------ ② 第三方立场的元指代

def test_meta_veto_of_other_party_is_not_a_veto():
    """🔴「熊方的否决拿不出更优替代」⇒ 否决权属于**对方** ⇒ 不否决 512800。"""
    codes = _codes(_SNIPPET_META_VETO)
    assert "512800" not in codes, f"512800 被误判成否决（实测 {codes}）"
    assert "515020" not in codes and "516310" not in codes and "512820" not in codes, (
        f"这三条的否决出处不在本句（实测 {codes}）"
    )


def test_meta_veto_mutation_guard_is_load_bearing():
    """变异反证：把 `_META_VETO_RE` 清空 ⇒ 上一条必须变红。"""
    saved = LLF._META_VETO_RE
    try:
        LLF._META_VETO_RE = __import__("re").compile(r"(?!x)x")
        codes = _codes(_SNIPPET_META_VETO)
        assert "512800" in codes, "变异后应当误伤（证明护栏承重）"
    finally:
        LLF._META_VETO_RE = saved


def test_verb_first_meta_veto_is_not_a_veto():
    """「不下调到 0（否决熊方）」⇒ 动词后置写法同样不算否决。"""
    codes = _codes("1) 512800（银行ETF华宝）：方向「持 / 减至目标权重」，不下调到 0（否决熊方）\n")
    assert "512800" not in codes, f"512800 被误判成否决（实测 {codes}）"


# ------------------------------------------------------------------ ③ 点名裁决优先

def test_explicit_numbered_items_beat_prose_inference():
    """🔴 权威节「逐只点名」的编号条目 = 这一只**本轮的直接裁决** ⇒ 散文不得推翻。

    真实件上 515450 被散文里的「（否决 159207）」误伤 ⇒ 由点名条目（`2) 515450：建仓…`）救回。
    """
    codes = _codes(_SNIPPET_EXPLICIT_ITEMS + _SNIPPET_PAREN_VETO)
    assert "512800" not in codes, f"512800（点名说『持 / 减至目标权重』）被误否决（实测 {codes}）"
    assert "515450" not in codes, f"515450（点名说『建仓至标准槽位 16%』）被误否决（实测 {codes}）"
    assert "516620" in codes, f"516620（点名说『正式一票否决』）漏判（实测 {codes}）"
    assert "159207" in codes, f"159207（点名说『不建仓』）漏判（实测 {codes}）"


def test_explicit_items_mutation_guard_is_load_bearing():
    """变异反证：让点名条目识别失效 ⇒ 上一条必须变红（证明这层护栏真在承重）。"""
    saved = LLF._EXPLICIT_ITEM_RE
    try:
        LLF._EXPLICIT_ITEM_RE = __import__("re").compile(r"(?!x)x")
        codes = _codes(_SNIPPET_EXPLICIT_ITEMS + _SNIPPET_PAREN_VETO)
        assert "515450" in codes, "变异后应当误伤 515450（证明护栏承重）"
    finally:
        LLF._EXPLICIT_ITEM_RE = saved


def test_bullet_item_is_not_an_explicit_verdict():
    """🔴 项目符号条目**太弱**：`- 515450 若未通过复核，则不买` 是**条件句风险披露**，不是裁决。

    ⇒ 把弱条目当裁决会**凭空造出否决**（上一轮真实件上造出过 515450）。
    """
    codes = _codes("- `515450`若未通过12月1日复核，则不买，权益敞口进一步降至约16%\n")
    assert "515450" not in codes, f"条件句被当成否决（实测 {codes}）"


# ------------------------------------------------------------------ ④ 弱化限定词 / 否定式否决

def test_weakened_veto_is_not_a_veto():
    """🔴「一票否决**理由不足**」⇒ 这条否决**不成立** ⇒ 512800 不许被否。"""
    codes = _codes(_SNIPPET_WEAKENED_VETO)
    assert "512800" not in codes, f"被弱化的否决被当真（实测 {codes}）"


def test_negated_veto_is_not_a_veto():
    """🔴「**不能**仅因模型存在局限就一票否决 `512800`」⇒ 否定式否决 ⇒ 不许被否。"""
    codes = _codes(_SNIPPET_NEGATED_VETO)
    assert "512800" not in codes, f"否定式否决被当真（实测 {codes}）"


def test_negated_veto_mutation_guard_is_load_bearing():
    """变异反证：把「不能」从否定正则里去掉 ⇒ 上一条必须变红。"""
    saved = LLF._REJECT_NEGATION_RE
    try:
        LLF._REJECT_NEGATION_RE = __import__("re").compile(
            r"(?:不得|不可|不应|无须|无需|不予|并非|不是|不构成|不算|避免|禁止|杜绝|而不是|而非|不作|不做)"
            r"[^，；。｜|\n]{0,16}?"
            r"(?:否决|剔除|排除|不选|移除|放弃|回避|暂缓|拒绝|不配置|不予配置|不参与|不纳入)"
        )
        codes = _codes(_SNIPPET_NEGATED_VETO)
        assert "512800" in codes, "变异后应当误伤（证明护栏承重）"
    finally:
        LLF._REJECT_NEGATION_RE = saved


# ------------------------------------------------------------------ 端到端（真实件形态）

def test_real_world_authority_shape_end_to_end():
    """🔴 真实 md 的权威节形态 ⇒ 最终否决名单必须**精确**等于这 7 只。

    真实快照 `20261111_152132` 的权威节逐只裁决：
      512800 → 持/减至目标权重（**不是否决**）｜515450 → 建仓 16%（**不是否决**）
      516620 → 正式一票否决｜159207 → 不建仓｜563020 → 否决替代资格
      515650 → 暂缓（配套动作）｜515020/516310/512820 → 正式一票否决（配套动作）
    """
    text = (
        "## 二、研究经理结论（Research Manager）\n"
        + _SNIPPET_EXPLICIT_ITEMS
        + _SNIPPET_WEAKENED_VETO
        + _SNIPPET_NEGATED_VETO
        + "## 三、风险裁决\n"
        + _SNIPPET_META_VETO
        + _SNIPPET_PAREN_VETO
        + _SNIPPET_SUMMARY
    )
    codes = set(_codes(text))
    assert codes == {
        "516620", "159207", "563020", "515650", "515020", "516310", "512820",
    }, f"否决名单与真实件不符（实测 {sorted(codes)}）"
    assert "512800" not in codes, "512800 必须留在计划里（减仓，不是被否决）"
    assert "515450" not in codes, "515450 必须留在计划里（建仓，不是被否决）"


# ------------------------------------------------------------------ 每日链：TA 上下文与 LLM 调用分离

def test_daily_chain_always_builds_ta_context():
    """🔴 `--skip-llm` 复跑时**仍然必须**构建 TA 上下文（本地确定性计算，不调 API）。

    旧口径把 `build_ta_context.py` 放在 `else`（真 LLM）分支 ⇒ `--skip-llm` 复跑出来的快照
    **缺** `ta_research_context.md` / `ta_context_meta.json` ⇒ 快照结构不完整
    ⇒ 闸门「取最新 LLM 完整快照」会跳过它 ⇒ 这一轮白跑（实测 2026-11-11 15:08）。
    """
    import ast

    src = (_ETF_DIR / "daily" / "agents_rebalance.py").read_text(encoding="utf-8")
    tree = ast.parse(src)
    calls = [
        node
        for node in ast.walk(tree)
        if isinstance(node, ast.Call)
        and isinstance(node.func, ast.Name)
        and node.func.id == "_run_cmd"
        and "build_ta_context.py" in ast.dump(node)
    ]
    assert calls, "没找到 build_ta_context.py 的调用"

    skip_branches: list[ast.AST] = []
    for node in ast.walk(tree):
        if isinstance(node, ast.If):
            src_seg = ast.dump(node.test)
            if "skip_llm" in src_seg:
                skip_branches.extend(node.body)
    for call in calls:
        for branch in skip_branches:
            assert call not in ast.walk(branch), (
                "build_ta_context.py 不许在 `if skip_llm:` 分支里"
                "（--skip-llm 复跑会产出结构不完整的快照）"
            )
