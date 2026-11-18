"""🔴 2026-11-11 新增：A19②「表格块不变量」判据的回归（判据①**换版**后的行为钉死）。

事故来源（服务器链 v10 实测**假红** —— 判据本身有 bug，交付件没问题）：

  交付件快照 `20261111_122825` 的官方决策 md 里，**5 个含代码的表格块的代码集合
  两两不同**（多头表 7 个 / 空头表 7 个 / 目标配置表 4 个 / Trader 最终表 11 个 /
  指标对照表 3 个）⇒ 旧判据①要求「**完全相同**的代码集合在 ≥2 个表格块里重复出现」
  ⇒ 一个都不重复 ⇒ **假红**（判据 FAIL）。
  而同链的 A19④「md == 用现版 `_tail` 从决策 json 重渲 ⇒ 逐字节相等」**PASS**、
  且多头表/空头表**都在** ⇒ 这份 md **没有**被腰斩/切表。

本文件把**换版后的判据**钉死（自带合成夹具，不依赖服务器产物）：

  ① 真实交付件 md ⇒ `_md_table_invariant` = True（**路径不存在就 skip**）；
  ② 两张表代码集合**部分重叠**（{A,B,C} 与 {B,C,D}）⇒ True（成对 ⇒ 放行）；
  ③ 两张表代码集合**完全不重叠** ⇒ False（没有成对表 ⇒ 拦下）；
  ④ 只剩**一张**含代码的表 ⇒ False（前置条件仍有牙）；
  ⑤ 两张成对的表 + 一张含**新代码**的孤表 ⇒ False（孤表判据仍有牙）；
  ⑥ 两张成对的表，但方向列**全是买入** ⇒ False（双向覆盖仍有牙）；
  ⑦ **变异负例**：A19② 挂着的 N-A19①~⑤ 必须**全部**仍然 FAIL
     （判据被改松 ⇒ 这 5 条会放过 ⇒ 本测试立刻转红）。
"""

from __future__ import annotations

import os
import pathlib
import sys

import pytest

#: `manu/etf_rotation/probes/`（探针模块自己会把 `manu/etf_rotation/` 插进 sys.path）
_PROBES_DIR = pathlib.Path(__file__).resolve().parents[1] / "probes"
if str(_PROBES_DIR) not in sys.path:
    sys.path.insert(0, str(_PROBES_DIR))

import probe_llm_parser as P  # noqa: E402  （探针本体）

#: 真实交付件的决策 md（**不存在就 skip** ⇒ 别让别的机器崩）
#:   ① 默认：服务器快照 `20261111_122825`（已拉到 host）；
#:   ② 兜底：环境变量 `ETF_A19_DECISION_MD` 指向任意一份决策 md。
_REAL_MD_CANDIDATES = [
    pathlib.Path(
        "/media/manu/1TB-Volume/workspace/etf_rotation_20261110"
        "/server_runs_etf_rotation/snapshots/20261111_122825/tradingagents_decision.md"
    ),
    pathlib.Path(os.environ.get("ETF_A19_DECISION_MD", "/nonexistent/decision.md")),
]


def _real_decision_md() -> pathlib.Path | None:
    """找到一份**真实交付件**的决策 md（找不到 ⇒ 返回 None ⇒ 调用方 skip）。"""
    for cand in _REAL_MD_CANDIDATES:
        if cand.is_file():
            return cand
    return None


# ---------------------------------------------------------------- 合成夹具 ----
#: 多头表（3 个代码：512800 / 512170 / 512690）
_BULL_TABLE = """| 标的 | 方向 | 目标权重 |
|:--|:--|--:|
| 512800 | 买入 | 20% |
| 512170 | 买入 | 15% |
| 512690 | 买入 | 10% |"""

#: 空头表（3 个代码：512170 / 512690 / 563020 ⇒ 与多头表**共享 2 个**代码 ⇒ 成对）
_BEAR_TABLE = """| 标的 | 方向 | 目标权重 |
|:--|:--|--:|
| 512170 | 卖出 | 0% |
| 512690 | 卖出 | 0% |
| 563020 | 卖出 | 0% |"""

#: 与多头表**完全不重叠**的一张表
_ALIEN_TABLE = """| 标的 | 方向 | 目标权重 |
|:--|:--|--:|
| 515450 | 卖出 | 0% |
| 563020 | 卖出 | 0% |
| 511990 | 卖出 | 0% |"""

#: 凭空多出来的一张**孤表**（代码 900099 不在成对表里）
_ORPHAN_TABLE = """| 标的 | 方向 | 目标权重 |
|:--|:--|--:|
| 900099 | 买入 | 10% |"""

#: 正文：**买侧 + 卖侧**的意图都在（方向列单侧时，靠正文交叉校验）
_PROSE = """# 合成决策

本轮拟买入 512800，并对 512170、563020 执行卖出与清仓。
"""


def _md(tables: list[str], prose: str = _PROSE) -> str:
    """把若干张表拼成一份合成 md（表格之间留空行 ⇒ 各自成块）。"""
    return prose + "\n" + "\n\n".join(tables) + "\n"


def _set_direction(md: str, value: str) -> str:
    """把所有含代码的表的方向列整列改成同一个词（复用探针自带的变异器）。"""
    return P._mut_set_direction(md, value)


# ------------------------------------------------------------------ 用例 ----
def test_real_delivery_md_passes_table_invariant() -> None:
    """① 真实交付件（快照 20261111_122825）⇒ 判据必须 PASS（旧判据在这里是假红）。"""
    md_path = _real_decision_md()
    if md_path is None:
        pytest.skip(f"真实交付件的决策 md 不在本机（候选：{_REAL_MD_CANDIDATES}）⇒ 跳过")
    ok, detail = P._md_table_invariant(md_path.read_text(encoding="utf-8"))
    assert ok, f"真实交付件被判红（假红）：{detail}"


def test_partially_overlapping_tables_pass() -> None:
    """② 两张表代码集合**部分重叠**（共享 2 个）⇒ 成对 ⇒ 判据 PASS。"""
    ok, detail = P._md_table_invariant(_md([_BULL_TABLE, _BEAR_TABLE]))
    assert ok, f"两张部分重叠的表被判红：{detail}"


def test_disjoint_tables_fail() -> None:
    """③ 两张表代码集合**完全不重叠** ⇒ 没有成对表 ⇒ 判据 FAIL。"""
    ok, detail = P._md_table_invariant(_md([_BULL_TABLE, _ALIEN_TABLE]))
    assert not ok, f"两张完全不相交的表被判放行：{detail}"


def test_single_coded_table_fails_precondition() -> None:
    """④ 只剩**一张**含代码的表（整表被切掉）⇒ 前置条件 ⇒ 判据 FAIL。"""
    ok, detail = P._md_table_invariant(_md([_BULL_TABLE]))
    assert not ok, f"只剩一张含代码的表被判放行：{detail}"


def test_orphan_table_with_new_code_fails() -> None:
    """⑤ 两张成对的表 + 一张含**新代码**的孤表 ⇒ 孤表判据 ⇒ 判据 FAIL。"""
    ok, detail = P._md_table_invariant(_md([_BULL_TABLE, _BEAR_TABLE, _ORPHAN_TABLE]))
    assert not ok, f"凭空多出的孤表被判放行：{detail}"


def test_single_sided_direction_fails() -> None:
    """⑥ 两张成对的表，但方向列**全是买入** ⇒ 双向覆盖 ⇒ 判据 FAIL。"""
    md = _set_direction(_md([_BULL_TABLE, _BEAR_TABLE]), "买入")
    ok, detail = P._md_table_invariant(md)
    assert not ok, f"方向列只剩买侧（正文仍在讲卖出）却被判放行：{detail}"


def test_mutation_negatives_still_bite() -> None:
    """⑦ **变异负例**：N-A19①~⑤ 必须**全部**仍然 FAIL（判据不许被改松）。"""
    md_path = _real_decision_md()
    if md_path is None:
        pytest.skip(f"真实交付件的决策 md 不在本机（候选：{_REAL_MD_CANDIDATES}）⇒ 跳过")
    md = md_path.read_text(encoding="utf-8")
    negatives = (
        ("N-A19① 只留一张含代码的表（整表被切掉）", P._mut_keep_one_coded_table(md)),
        ("N-A19② 方向列全改成「买入」", P._mut_set_direction(md, "买入")),
        ("N-A19③ 方向列全改成「卖出」", P._mut_set_direction(md, "卖出")),
        ("N-A19④ 每张表换成自己独有的代码（没有成对的表）", P._mut_unique_code_sets(md)),
        ("N-A19⑤ 凭空多出一张含新代码的孤表", P._mut_add_orphan_table(md)),
    )
    for name, mutated in negatives:
        ok, detail = P._md_table_invariant(mutated)
        assert not ok, f"{name} 变异后判据仍 PASS ⇒ 判据被改松了：{detail}"
