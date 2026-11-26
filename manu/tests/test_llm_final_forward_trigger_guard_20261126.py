"""🔴 2026-11-26 新增：**前瞻触发清单** ＋ **反事实推演** 被当成本轮裁决的回归测试。

【真机事故（11-25 17:31 真机跑批 `snapshots/20261125_180119`）】
TA 权威裁决明明写「**买 563020 / 512800 减仓到 16%**」，
交付计划却把 **563020 与 512800 双双记成「一票否决」**
⇒ 563020 没买、512800 一股没卖、顺延位买了 TA 从**未点名**的 512890
⇒ 交付层与 TA 权威裁决**背离**（宁漏勿误口径被「假否决」顶穿）。

【两条根因 ⇒ 两条护栏（`manu/llm_final.py`）】

① **前瞻触发上下文**（`_FORWARD_TRIGGER_RE` ＋ `_is_conditional` 的新分支）：
   句子里出现「风险触发 / 触发条件 / 触发线 / 条件式降级 / 条件式处理 /
   什么会改变 / 转更消极 / 转更积极」⇒ 这句里的**否决动词与方向动词**
   都不是本轮裁决（只是**未来触发器清单**）；
   ⚠️ 「**已触发 / 已经触发**」＝**既成事实** ⇒ **不豁免**（不许放过真否决）。

② **反事实句式**（`_COUNTERFACTUAL_TAIL_RE` ＋ `_counterfactual_veto_positions`）：
   否决动词后面跟「**代码 ＋ 后 / 之后 / 的话 / 情况下**」⇒ **反事实推演**
   （"如果否决了会怎样"）⇒ **不作数**。

【本文件钉死（夹具全部内联自造，不读 runs/ 下的真实快照、不依赖服务器产物）】

  A 前瞻触发清单 ⇒ 512800 / 563020 都**不得**进否决名单；
  B 反事实句式   ⇒ 512800 **不得**进否决名单；
  C 变异反证（证明护栏**承重**）：
    C1 删掉「风险触发（任一出现即调整，不等下个节点）：」前缀 ⇒ 512800 必须重新被拒；
    C2 「④ 若 …」改成「④ 同时…」 ⇒ 563020 必须被拒；
    C3 「否决 512800 后」改成「否决 512800」 ⇒ 512800 必须被拒；
    C4/C5 把两条护栏的正则**清空** ⇒ A / B 必须**转红**；
  D 「已触发…」＝既成事实 ⇒ 512800 **必须**被拒（连变异后也必须被拒）；
  E 方向词同源：「转更消极（清仓…）」句里的 563020 **不得**被判成「卖 / 0%」；
  F 不误伤真裁决：**同一次** `extract_final_decision` 调用里，
    512800 仍然是「减 / 卖出 2,400 份 / 保留 5,300 份」。

⚠️ **镜像纪律**：本文件在 `manu/etf_rotation/tests/` 与 `manu/tests/` 各有一份，
   两份**必须逐字一致**（各测自己那份镜像的 `llm_final.py`）。
"""

from __future__ import annotations

import importlib.util
import pathlib
import re

#: 本测试文件所在目录的**上一级** ＝ 放着 `llm_final.py` 的那一层
#: （`manu/etf_rotation/` 或 `manu/` ⇒ 两个镜像位置各测自己那份镜像）。
_MODULE_DIR = pathlib.Path(__file__).resolve().parents[1]
_LLF_PATH = _MODULE_DIR / "llm_final.py"


def _load_llm_final():
    """按**路径**加载被测模块（`manu/` 不在 pytest 默认 `sys.path` 上）。"""
    spec = importlib.util.spec_from_file_location("_llf_forward_trigger_under_test", _LLF_PATH)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


LLF = _load_llm_final()


def _codes(text: str) -> list[str]:
    """被点名否决的代码清单。"""
    return [str(row.get("code")) for row in LLF.extract_rejections(text)]


def _slot(md: str, code: str) -> dict:
    """`extract_final_decision` 里某只标的的裁决槽位（没有 ⇒ 空 dict）。"""
    return dict(LLF.extract_final_decision(md).get("targets", {}).get(code, {}))


# --------------------------------------------------------------------------- #
# 夹具（全部内联自造 ⇒ 不依赖服务器产物 / 不读 runs/ 下的真实快照）
# --------------------------------------------------------------------------- #

#: 🔴 A：前瞻触发清单（**合成 md 一段**；形态照 11-25 真机件「执行与风控纪律」那一行）。
#:
#: ⚠️ ④ 单独成句（「。」断开）的原因：护栏是**句级**口径 ⇒ ④ 这句里
#:    「**若**」能被 `_is_conditional` 单独看见 ⇒ 钉的才是**逐句**真值；
#:    否则 C2（把「若」改成「同时」）会因为整句仍被「风险触发」兜住而**没有牙**。
_MD_FORWARD_TRIGGER = (
    "## 六、最终交易决定\n\n"
    "12. **风险触发（任一出现即调整，不等下个节点）：** ① 可核实的银行信用事件 → 对 512800 一票否决、"
    "权益降至 ≤16%；② 512800 跌破 MA60 → 按规则强制退出；③ 组合回撤触及 -15% → 整体转现金。"
    "④ 若 563020 与 512800 的重叠≥0.90 → 一票否决 563020\n"
)

#: 🔴 B：反事实推演（11-25 真机原文的口径：**先推演、再给结论**）。
_MD_COUNTERFACTUAL = (
    "## 六、最终交易决定\n\n"
    "按 3.5 的顺延规则，否决 512800 后能顺延进来的只有医药/消费那批最终分 -0.73~-1.90 的候选，"
    "那是明显更差的一笔交易。结论：512800 不是清仓对象，但当前 23.28% 高于 16% 规则目标，"
    "方向是减到目标。\n"
)

#: 🔴 D：「已触发」＝**既成事实**（不是前瞻条件）⇒ 不许被前瞻护栏放过。
_MD_ALREADY_TRIGGERED = (
    "## 六、最终交易决定\n\n"
    "已触发银行信用事件，按规则对 512800 一票否决。\n"
)

#: 🔴 E + F：**同一次** `extract_final_decision` 调用（同一份 md）。
#:    - 第 1 条是**真裁决**（减仓 ⇒ 方向「减」、股数明确）；
#:    - 第 12 条是**前瞻触发清单**（「转更消极」⇒ 里面的「清仓」不是本轮裁决）。
_MD_DIRECTION = (
    "## 六、最终交易决定\n\n"
    "1. **512800（银行ETF华宝）：减仓至 16%，卖出约 2,400 份，保留约 5,300 份。**\n\n"
    "12. **转更消极（清仓或降至 16% 以下、权益转向现金）：** 银行板块出现可核实信用事件、"
    "512800 跌破 MA60、563020 的 E 转负或失去 MA60 趋势确认。\n"
)

#: A 的「风险触发」前缀（C1 变异要删掉它）
_FWD_PREFIX = "风险触发（任一出现即调整，不等下个节点）："


# --------------------------------------------------------------------------- #
# A：前瞻触发清单不是当前否决
# --------------------------------------------------------------------------- #

def test_A_前瞻触发清单不是当前一票否决() -> None:
    """「风险触发（任一出现即调整…）」是**未来触发器** ⇒ 512800 / 563020 都不是本轮否决。"""
    codes = _codes(_MD_FORWARD_TRIGGER)
    assert "512800" not in codes, (
        f"🔴 前瞻触发清单里的「对 512800 一票否决」被当成本轮否决（codes={codes}）"
        "⇒ 512800 被冻成「维持席位」⇒ TA 明写的「减到 16%」一股没卖"
    )
    assert "563020" not in codes, (
        f"🔴 前瞻触发清单里的「一票否决 563020」被当成本轮否决（codes={codes}）"
        "⇒ TA 明写「买 563020」却没买 ⇒ 顺延位买了 TA 从未点名的标的"
    )


def test_A_前瞻触发清单不得产出任何否决条目() -> None:
    """整段前瞻触发清单 ⇒ **一个**否决条目都不许产出（名单必须干净）。"""
    assert _codes(_MD_FORWARD_TRIGGER) == [], (
        "前瞻触发清单里出现了否决条目 ⇒ 护栏漏放：" f"{_codes(_MD_FORWARD_TRIGGER)}"
    )


# --------------------------------------------------------------------------- #
# B：反事实句式不是否决
# --------------------------------------------------------------------------- #

def test_B_反事实句式不是一票否决() -> None:
    """「否决 512800 **后**能顺延的只有…」是**假设推演** ⇒ 512800 不是被否决。"""
    codes = _codes(_MD_COUNTERFACTUAL)
    assert "512800" not in codes, (
        f"🔴 反事实推演里的「否决 512800 后」被当成本轮否决（codes={codes}）"
        "⇒ 512800 被冻成「维持席位」⇒ TA 明写的「减到目标」一股没卖"
    )


# --------------------------------------------------------------------------- #
# C：变异反证（真否决照旧命中 ⇒ 护栏不许过宽，且必须**承重**）
# --------------------------------------------------------------------------- #

def test_C1_删掉前瞻触发前缀_512800必须重新被拒() -> None:
    """删掉「风险触发…」前缀 ⇒ ① 就是**本轮**一票否决 ⇒ 512800 必须被拒。"""
    codes = _codes(_MD_FORWARD_TRIGGER.replace(_FWD_PREFIX, ""))
    assert "512800" in codes, (
        f"🔴 删掉前瞻触发前缀后 512800 仍没被拒（codes={codes}）⇒ 护栏把真否决也吞了"
    )


def test_C2_若改成同时_563020必须被拒() -> None:
    """「④ 若 …」→「④ 同时 …」⇒ 变成**本轮**一票否决 ⇒ 563020 必须被拒。"""
    codes = _codes(_MD_FORWARD_TRIGGER.replace("④ 若 ", "④ 同时 "))
    assert "563020" in codes, (
        f"🔴 「④ 同时 …」的 563020 没被拒（codes={codes}）⇒ 条件词护栏失效"
    )


def test_C3_反事实尾词去掉_512800必须被拒() -> None:
    """「否决 512800 **后**」→「否决 512800」⇒ 不再是反事实 ⇒ 512800 必须被拒。"""
    codes = _codes(_MD_COUNTERFACTUAL.replace("否决 512800 后", "否决 512800"))
    assert "512800" in codes, (
        f"🔴 去掉「后」之后 512800 没被拒（codes={codes}）⇒ 反事实护栏把真否决也吞了"
    )


def test_C4_变异_清空前瞻触发正则_A必须转红() -> None:
    """把 `_FORWARD_TRIGGER_RE` 清空 ⇒ A 必须**转红**（证明这层护栏在承重）。

    ⚠️ 变异后回来的只有 **512800**（① 那句的保护来自「风险触发」这句级口径）；
       ④ 那句的保护来自**「若」**这个条件词（既有护栏，由 C2 单独钉死）⇒ 不在此断言。
    """
    saved = LLF._FORWARD_TRIGGER_RE
    try:
        LLF._FORWARD_TRIGGER_RE = re.compile(r"(?!x)x")
        codes = _codes(_MD_FORWARD_TRIGGER)
        assert "512800" in codes, (
            f"变异后 512800 仍未被拒（codes={codes}）⇒ 前瞻触发护栏没有承重"
        )
    finally:
        LLF._FORWARD_TRIGGER_RE = saved
    assert _codes(_MD_FORWARD_TRIGGER) == [], "恢复后前瞻触发清单里又冒出否决条目"


def test_C5_变异_清空反事实尾词正则_B必须转红() -> None:
    """把 `_COUNTERFACTUAL_TAIL_RE` 清空 ⇒ B 的断言必须**转红**（证明这层护栏在承重）。"""
    saved = LLF._COUNTERFACTUAL_TAIL_RE
    try:
        LLF._COUNTERFACTUAL_TAIL_RE = re.compile(r"(?!x)x")
        codes = _codes(_MD_COUNTERFACTUAL)
        assert "512800" in codes, "变异后 512800 仍未被拒 ⇒ 这条断言没有牙"
    finally:
        LLF._COUNTERFACTUAL_TAIL_RE = saved
    assert "512800" not in _codes(_MD_COUNTERFACTUAL), "恢复后反事实句式又被当成否决"


# --------------------------------------------------------------------------- #
# D：「已触发」＝既成事实 ⇒ 不豁免
# --------------------------------------------------------------------------- #

def test_D_已触发是既成事实_必须被拒() -> None:
    """「已触发银行信用事件，按规则对 512800 一票否决」⇒ 既成事实 ⇒ 512800 **必须**被拒。"""
    codes = _codes(_MD_ALREADY_TRIGGERED)
    assert "512800" in codes, (
        f"🔴 「已触发」＝既成事实却不判否决（codes={codes}）⇒ 前瞻护栏把真否决放过了"
    )


def test_D2_变异_清空前瞻触发正则_已触发仍然被拒() -> None:
    """变异后仍必须被拒 ⇒ 证明「已触发」的豁免**不依赖**前瞻护栏（口径自带既成事实判定）。"""
    saved = LLF._FORWARD_TRIGGER_RE
    try:
        LLF._FORWARD_TRIGGER_RE = re.compile(r"(?!x)x")
        codes = _codes(_MD_ALREADY_TRIGGERED)
        assert "512800" in codes, "变异后「已触发」的真否决被放过 ⇒ 既成事实口径有洞"
    finally:
        LLF._FORWARD_TRIGGER_RE = saved


# --------------------------------------------------------------------------- #
# E：方向词同源（前置触发清单里的方向词不是本轮裁决）
# --------------------------------------------------------------------------- #

def test_E_转更消极句里的方向词不作数() -> None:
    """「转更消极（清仓或降至 16% 以下…）」句里的 563020 **不得**被判成「卖 / 0%」。"""
    slot = _slot(_MD_DIRECTION, "563020")
    assert slot.get("direction") != "卖", (
        f"🔴 前瞻触发句里的方向词被当成本轮裁决（direction={slot.get('direction')}）"
        "⇒ 563020 被写成清仓 0% ⇒ TA 明写「买 563020」却变成清仓"
    )
    assert slot.get("target_pct") != 0.0, (
        f"🔴 前瞻触发句里的 563020 被记成目标 0%（target_pct={slot.get('target_pct')}）"
    )


def test_E2_变异_清空前瞻触发正则_563020必须变成卖零() -> None:
    """变异后 563020 **必须**变成「卖 / 0%」⇒ 证明 E 这条断言不是恒绿（护栏在承重）。"""
    saved = LLF._FORWARD_TRIGGER_RE
    try:
        LLF._FORWARD_TRIGGER_RE = re.compile(r"(?!x)x")
        slot = _slot(_MD_DIRECTION, "563020")
        assert slot.get("direction") == "卖", (
            f"变异后 563020 的方向不是「卖」（实测 {slot.get('direction')}）⇒ E 这条断言没有牙"
        )
        assert slot.get("target_pct") == 0.0, (
            f"变异后 563020 的目标不是 0%（实测 {slot.get('target_pct')}）⇒ E 这条断言没有牙"
        )
    finally:
        LLF._FORWARD_TRIGGER_RE = saved


# --------------------------------------------------------------------------- #
# F：不误伤真裁决（同一次调用里）
# --------------------------------------------------------------------------- #

def test_F_同一次调用里真裁决不被误伤() -> None:
    """同一份 md 里，512800 的**真裁决**必须原样抽出（减 / 2,400 份 / 5,300 份）。"""
    slot = _slot(_MD_DIRECTION, "512800")
    assert slot.get("direction") == "减", (
        f"🔴 真裁决的方向被前瞻护栏误伤（direction={slot.get('direction')}）"
    )
    assert slot.get("shares_sell") == 2400, (
        f"🔴 真裁决的卖出股数丢了（shares_sell={slot.get('shares_sell')}）"
    )
    assert slot.get("shares_keep") == 5300, (
        f"🔴 真裁决的保留股数丢了（shares_keep={slot.get('shares_keep')}）"
    )
