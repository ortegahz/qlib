"""🔴 TA 否决解析的「顿号并列项（item）作用域」回归（2026-11-11 真缺陷）。

事故来源（真实交付件 `snapshots/20261111_122825`，TA「研究经理」段落原文）::

    **Rationale**: 先说结论：空头赢在"证据强度"和"总量约束"，多头赢在"结构与风险质量"，
    我按强弱配比取中间偏空的一档——权益小幅上调（48.02%→约56%）、512800减到标准槽位16%、
    红利低波只用515450、512170只给半仓、159207与563020不买、防守全部用现金。

根因：`_prose_hits()` 的子句切分只按「，」「；」（`_CLAUSE_SPLIT_RE`）⇒ **不切顿号「、」**
⇒ 一串并列项被合并成**同一个子句** ⇒ 该子句**末尾**的否决动词（`不买`）**反向污染**
前面的标的（`_REJECT_WINDOW=40` ⇒ 距 515450 / 512170 也在窗口内）。

误伤代价：TA「最终交易决定」明写 `515450：条件性买入12%`、`512170：条件性买入最多4%`
⇒ 这两只**不是**否决；错判成否决 ⇒ 席位被顺延成别的标的。

本文件钉死（自带夹具，不依赖服务器产物、不联网）：

  ① 真实原文 ⇒ 只判出 `{159207, 563020}`（**顺序无关**）；
  ② **R2 头作用域**：`不买512800、515450、515650` ⇒ 三只**都**否（不许漏判）；
  ③ **R3 尾作用域**：`512800、515450不买` ⇒ 两只**都**否（不许漏判）；
  ④ 并列项里的**自带动作**（`只` / `仅` / `保留` …）⇒ 自成一体，不被别的项管辖；
  ⑤ **比较提及**（`弱于同族 515450（相关性 0.917）`）⇒ 不是裁决对象；
  ⑥ **既有夹具不许回归**：`fixtures/ta_decision_real_20261110.md` 的结果集**一字不变**；
  ⑦ **变异负例**：把顿号切分 / 动作词表关掉 ⇒ 对应断言必须**转红**（证明闸门有牙）。
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
_FIXTURE_PATH = pathlib.Path(__file__).resolve().parent / "fixtures" / "ta_decision_real_20261110.md"


def _load_llm_final():
    spec = importlib.util.spec_from_file_location("_llf_item_scope_under_test", _LLF_PATH)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


LLF = _load_llm_final()


def _codes(text: str) -> set[str]:
    return {r["code"] for r in LLF.extract_rejections(text)}


def _md(body: str) -> str:
    return f"## 六、最终交易决定\n\n{body}\n"


# ---------------------------------------------------------------- 真实原文

#: 真实交付件 `20261111_122825` 里 TA「研究经理」段落的 **Rationale** 原句（一字不改）。
_REAL_RATIONALE = (
    "**Rationale**: 先说结论：空头赢在\"证据强度\"和\"总量约束\"，多头赢在\"结构与风险质量\"，"
    "我按强弱配比取中间偏空的一档——权益小幅上调（48.02%→约56%）、512800减到标准槽位16%、"
    "红利低波只用515450、512170只给半仓、159207与563020不买、防守全部用现金。"
)


def test_真实原文_只判出不买的那两只():
    """🔴 真缺陷现场：并列项里的 `不买` **不许**反向污染前面的标的。"""
    codes = _codes(_REAL_RATIONALE)
    assert codes == {"159207", "563020"}, codes


def test_真实原文_带动作词的两只不许被否():
    codes = _codes(_REAL_RATIONALE)
    assert "515450" not in codes, "515450 是「红利低波只用515450」⇒ 不得判成否决"
    assert "512170" not in codes, "512170 是「512170只给半仓」⇒ 不得判成否决"
    assert "512800" not in codes, "512800 是「512800减到标准槽位16%」⇒ 不得判成否决"


# ---------------------------------------------------------------- R2 / R3 作用域

def test_R2头作用域_不买带头的并列项全否():
    """`不买512800、515450、515650` ⇒ 动词在**第一项** ⇒ 管辖其后**所有**项。"""
    codes = _codes(_md("不买512800、515450、515650。"))
    assert codes == {"512800", "515450", "515650"}, codes


def test_R3尾作用域_不买带尾的并列项全否():
    """`512800、515450不买` ⇒ 动词在**最后一项** ⇒ 管辖其前**所有**项。"""
    codes = _codes(_md("512800、515450不买。"))
    assert codes == {"512800", "515450"}, codes


def test_剔除并列项_两只都否():
    codes = _codes(_md("剔除159207、563020。"))
    assert codes == {"159207", "563020"}, codes


def test_并列项里的自带动作_只判不买的那一项():
    """`只` / `只给半仓` ⇒ 自带动作 ⇒ 不被别的项的否决动词管辖。"""
    codes = _codes(_md("红利低波只用515450、512170只给半仓、159207与563020不买。"))
    assert codes == {"159207", "563020"}, codes


def test_动作词保护_只与仅():
    codes = _codes(_md("512800仅保留、515450只给8%、159207不买。"))
    assert codes == {"159207"}, codes


def test_动作词保护_保留与维持():
    codes = _codes(_md("512800保留、515450维持原仓、159207不买。"))
    assert codes == {"159207"}, codes


def test_动作词保护_减到与配置():
    codes = _codes(_md("512800减到16%、515450配置8%、159207不买。"))
    assert codes == {"159207"}, codes


# ---------------------------------------------------------------- 闸门有牙（不许「一律不判」）

def test_变异负例_同一只标的被点名不买时必须被否():
    """🔴 证明闸门**有牙**：同一只 515450，被**点名**`不买` 时必须判成否决。"""
    codes = _codes(_md("红利低波只用515450、515450不买。"))
    assert codes == {"515450"}, codes


def test_变异负例_并列项里的不买必须命中():
    codes = _codes(_md("159207与563020不买。"))
    assert codes == {"159207", "563020"}, codes


def test_变异负例_短距离的点名否决仍然命中():
    codes = _codes(_md("剔除 515450。"))
    assert codes == {"515450"}, codes


# ---------------------------------------------------------------- 比较提及

def test_比较提及_代码后带相关性指标不算裁决对象():
    """🔴 真实交付件 Trader 段：`563020 … 但弱于同族 515450（相关性 0.917）绝不建仓`
    ⇒ 「不建仓」说的是 563020；515450 只是**比较对象**（TA 明写「买 515450」）。"""
    body = (
        "563020 E 实为 +0.1074 但弱于同族 515450（相关性 0.917）绝不建仓。"
    )
    codes = _codes(_md(body))
    assert codes == {"563020"}, codes


def test_比较提及_关掉豁护栏后515450会被误伤():
    """变异负例：关掉「比较指标」豁免 ⇒ 515450 重新被误伤 ⇒ 证明该豁免承重。"""
    saved = LLF._MD_REF_METRIC_RE
    try:
        LLF._MD_REF_METRIC_RE = re.compile(r"(?!x)x")
        body = "563020 E 实为 +0.1074 但弱于同族 515450（相关性 0.917）绝不建仓。"
        codes = _codes(_md(body))
        assert "515450" in codes, "豁免关掉后 515450 必须重新被误伤（否则本变异无效）"
    finally:
        LLF._MD_REF_METRIC_RE = saved


# ---------------------------------------------------------------- 既有夹具不许回归

#: 改动**前**实测的夹具结果集（`extract_rejections` 的 13 只）——一字不许变。
_FIXTURE_CODES_BEFORE = {
    "159207", "159307", "159547", "511990", "512170", "512820", "512890",
    "515020", "515080", "515100", "515180", "516310", "563020",
}


def test_既有夹具_结果集不许变():
    """🔴 `fixtures/ta_decision_real_20261110.md` 的否决结果集必须与改动前**完全一致**。"""
    text = _FIXTURE_PATH.read_text(encoding="utf-8")
    codes = _codes(text)
    assert codes == _FIXTURE_CODES_BEFORE, {
        "新增": sorted(codes - _FIXTURE_CODES_BEFORE),
        "丢失": sorted(_FIXTURE_CODES_BEFORE - codes),
    }


# ---------------------------------------------------------------- 变异负例（闸门承重）

def test_变异_关掉顿号切分则并列项被合并成一项():
    """变异：顿号切分关掉 ⇒ 并列项被合并成**同一个子句** ⇒ `不买` 反向污染前面的标的。

    ⇒ 这正是本缺陷的**原始形态**（`512800、515450、512170、159207与563020` 一个子句）。
    """
    saved = LLF._ITEM_SPLIT_RE
    try:
        LLF._ITEM_SPLIT_RE = re.compile(r"(?!x)x")
        codes = _codes(_md("红利低波只用515450、512170只给半仓、159207与563020不买。"))
        assert codes == {"159207", "512170", "515450", "563020"}, codes
    finally:
        LLF._ITEM_SPLIT_RE = saved


def test_变异_清空动作词表则自带动作的项被误伤():
    """变异：动作词表清空 ⇒ `只用515450` 不再自保 ⇒ 证明动作词护栏承重。"""
    saved = LLF._ITEM_ACTION_WORDS
    try:
        LLF._ITEM_ACTION_WORDS = ()
        codes = _codes(_md("红利低波只用515450、512170只给半仓、159207与563020不买。"))
        assert "515450" in codes, "动作词护栏关掉后 515450 必须重新被误伤（否则本变异无效）"
        assert "512170" in codes, "动作词护栏关掉后 512170 必须重新被误伤（否则本变异无效）"
    finally:
        LLF._ITEM_ACTION_WORDS = saved
