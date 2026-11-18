"""🔴 2026-11-11 新增：探针 A20⑥ 的**句级「部分卖出」护栏**回归。

事故来源（2026-11-11 服务器闸门复跑，`probe_llm_parser` A20⑥ 红灯）：

  真机交付件（快照 ``20261111_122825``）的「最终交易决定」章节里 LLM 写：

      **Executive Summary**:  最终决策是：**卖出（Sell）512800，但属于减仓而非清仓式看空。**

  ⇒ 子句切分在「，」处把**限定语**切掉了 ⇒ 探针只剩「卖出（Sell）512800」
  ⇒ 裸「卖出」规则命中 ⇒ 512800 被判成**零方向（清仓）**
  ⇒ 而计划里 512800 是「减仓至 12%」（解析器侧已修好，见 ``llm_final`` 的句级护栏）
  ⇒ 探针报「原文写明零方向，但计划目标=12.0（必须 0.0）」⇒ **探针误杀**。

本文件钉死四件事（自带夹具，不依赖服务器产物）：

  ① **真夹具**：真实句式里 512800 **不是**零方向；同段的真清仓代码**仍然是**（不许把探针改废）。
  ② **护栏只挡「裸卖出」**：明写「清仓 / 全部卖出」的代码**永远**进零方向集合。
  ③ **泛指时保守**：句里有**两只**代码时，「减仓」标记的泛指**不外溢**到另一只
     ⇒ 探针不替解析器兜底、不制造假绿。
  ④ **变异反证**：把护栏改回旧口径（无句级护栏）⇒ 断言必须转红（证明本测试有牙）。
"""

from __future__ import annotations

import importlib.util
import pathlib
import sys

_ETF_DIR = pathlib.Path(__file__).resolve().parents[1]
sys.path.insert(0, str(_ETF_DIR))

_PLP_PATH = _ETF_DIR / "probes" / "probe_llm_parser.py"


def _load(name: str, path: pathlib.Path):
    spec = importlib.util.spec_from_file_location(name, path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


PLP = _load("_plp_under_test", _PLP_PATH)

#: 真机交付件里那一句（原样截取）
_REAL_SENTENCE = (
    "**Rating**: Sell  **Executive Summary**:  最终决策是：**卖出（Sell）512800，"
    "但属于减仓而非清仓式看空。**"
)

#: 真机交付件里「159207、563020、511990：不买」那一句
_REAL_NOBUY = "**159207、563020、511990：不买**；515020、516310、512820也不换入"


def _zero(md: str) -> set[str]:
    codes, _note = PLP._md_zero_direction_codes(md)
    return codes


# --------------------------------------------------------------------- ① 真夹具
def test_real_sentence_partial_sell_is_not_zero_direction() -> None:
    """① 真实句式：512800 是「减仓」⇒ **不是**零方向。"""
    md = f"## 六、最终交易决定\n\n{_REAL_SENTENCE}\n"
    codes = _zero(md)
    assert "512800" not in codes, f"512800 又被误判成零方向：{sorted(codes)}"


def test_real_nobuy_still_zero_direction() -> None:
    """① 同段的「不买」那三只**必须**仍在零方向集合里（不许把探针改废）。"""
    md = f"## 六、最终交易决定\n\n{_REAL_NOBUY}\n"
    codes = _zero(md)
    for code in ("159207", "563020", "511990"):
        assert code in codes, f"真·不买 漏判：{code} 不在 {sorted(codes)}"


# --------------------------------------------------------------------- ② 护栏边界
def test_explicit_clear_out_is_never_shielded() -> None:
    """② 明写「全部卖出」的代码**永远**进零方向集合（护栏只挡裸「卖出」）。"""
    md = ("## 六、最终交易决定\n\n"
          "`159929`：全部卖出，退出本轮组合；512800属于减仓而非清仓。\n")
    codes = _zero(md)
    assert "159929" in codes, f"真·清仓被护栏误挡：{sorted(codes)}"


def test_named_partial_sell_marker_excludes_named_code() -> None:
    """② 点名的「减仓」标记 ⇒ 被点名的代码不进零方向集合。"""
    md = ("## 六、最终交易决定\n\n"
          "512800：卖出，同时把 512800 的仓位按减仓处理，保留核心仓。\n")
    codes = _zero(md)
    assert "512800" not in codes, f"点名的减仓没生效：{sorted(codes)}"


# --------------------------------------------------------------------- ③ 泛指保守
def test_generic_partial_marker_does_not_leak_to_other_code() -> None:
    """③ 句里有两只代码时，「减仓」的泛指**不外溢** ⇒ 另一只的裸卖出仍判零方向。

    🔴 这是探针的**牙**：不许因为加了护栏就把真正的清仓漏掉（假绿）。
    """
    md = ("## 六、最终交易决定\n\n"
          "卖出159929，同时 512800 的仓位属于减仓处理。\n")
    codes = _zero(md)
    assert "159929" in codes, f"裸卖出被护栏误挡（假绿）：{sorted(codes)}"


# --------------------------------------------------------------------- ④ 变异反证
def test_guard_is_load_bearing() -> None:
    """④ 把句级护栏摘掉 ⇒ ① 的断言必须转红（证明本测试有牙）。"""
    old = PLP._md_sentence_partial_sell_codes
    try:
        PLP._md_sentence_partial_sell_codes = lambda sentence: set()
        md = f"## 六、最终交易决定\n\n{_REAL_SENTENCE}\n"
        codes = _zero(md)
        assert "512800" in codes, "摘掉护栏后 512800 仍未进零方向集合 ⇒ 夹具不敏感"
    finally:
        PLP._md_sentence_partial_sell_codes = old
