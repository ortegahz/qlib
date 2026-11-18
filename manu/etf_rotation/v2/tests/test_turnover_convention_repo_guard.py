# -*- coding: utf-8 -*-
"""🔴 全仓「换手口径 + 废除硬拦截」守卫（2026-11-18）

【为什么要有这一条】
用户 2026-11-18 拍板：**废除月频策略的「≤4.0× 换手」硬拦截** ⇒ 改成
``cadence=20`` 先验硬锁 ＋ ``hold_buffer=2`` ＋ **单/双边换手率并列披露**。
⇒ 这三条必须**在全仓每一处**都成立 ⇒ 只改主路径、留死角 ⇒ 口径会被悄悄改回去
（历史上就出过「研究链改了、每日链没改」的分叉）。
⇒ 本文件把这三条变成**可执行的断言**（不是文档承诺）。

三条守卫
--------
① **惩罚唯一实现**：全仓**只准**有 ``config_v2.turnover_penalty`` 一个换手惩罚实现。
   * 允许：``config_v2.turnover_penalty``（唯一实现，封顶 ``TURNOVER_PENALTY_CAP``）；
   * 允许：``manu/tools/v2_rerank_val_objective.py::_old_penalty``
     （**故意保留**的旧口径审计函数，用来复现 2026-11-17 那次「蜗牛参数」事故）；
   * 其余一律违规。**AST 判定** ⇒ 注释 / docstring 里引用旧公式**不算**违规。
② **闸门模式显式**：每个 ``apply_turnover_gate(...)`` 调用点都**必须**显式传 ``mode=``
   （函数默认值是旧行为 ``gate`` ⇒ 吃默认值 = 口径被悄悄改回去）。
③ **单/双边并列披露**：凡在**同一个 dict 字面量**里落盘 ``"annualized_turnover"``，
   就必须**同一个 dict** 里同时落 ``"annualized_turnover_one_way"``
   （并列披露 = 同一条记录里两个数 ⇒ 只看「文件里出现过」太弱，故按 AST 同字典判）。
④ **报告层并列披露**：两个报告出口都必须同时出现「年化换手（双边）」与「年化换手（单边）」。

续三新增守卫（2026-11-18 第二/三轮）
-----------------------------------
⑨ **换手达标标记不许参与选参**（``turnover_ok`` 只是**披露标记**）：
   🔴 判定＝**扫全树**里任意「**条件/过滤位置**」出现 ``turnover_ok``
   （推导式 ``ifs`` / ``if`` / ``if 表达式`` / ``while`` / ``assert`` / 谓词调用
   ``filter`` / ``sorted(key=)`` / ``min`` / ``max`` / ``any`` / ``all``）
   ⇒ 只在**条件语境**出现才算违规；**披露语境**（写进 dict / CSV / md 的赋值）**不算**。
⑪ **表格列头必须带口径**：表头 / 表格单元格里的「换手」列必须同时标明**双边 / 单边**
   （正文散文里的「年化换手 14.10×」**不算**违规）。
⑫ **单边换算唯一实现**（铁律 ``rules.md`` §十四.112.4）：
   「单边换算一律走 ``config_v2.one_way_turnover()``（**唯一实现**）⇒ 🔴 不许手写 ``* 0.5``」
   * 豁免：``one_way_turnover`` 函数体本身（唯一实现本体）；
   * 豁免：``TURNOVER_ONE_WAY_FACTOR`` 常量定义（``= 0.5`` 是**赋值**，不是乘法）；
   * 其余凡**换手语境**里的 ``* 0.5`` / ``0.5 *`` / ``/ 2`` 一律违规（**AST** 判定，不是正则）。
   * 🔴 **换手语境**的判定（2026-11-18 收紧 ⇒ 四路信号，任一命中即算换手语境）：
     ① 「语句 / 参数名 / 字典键 / 字符串里有 ``turnover`` 或「换手」」；
     ② 「**函数名**或**函数 docstring**提到换手」；
     ③ 「**赋值目标名**里有**单边语义**（``one_way`` / ``oneway`` / ``single_side``
     / ``one`` / ``single`` / ``half`` / ``convert`` / 「单边」… ⇒ **成词**匹配，
     不做无边界的子串匹配 ⇒ ``half_weight``（「一半权重」）**不算**）」；
     ④ 「**整个文件**里出现过 ``turnover`` / 「换手」（**模块文本**兜底）」。
⑬ **全仓镜像对逐字一致**（``manu/`` ↔ ``manu/etf_rotation/`` 的同名模块）：
   * **递归**（``rglob("*.py")``）⇒ 子目录里的镜像文件也算数；
   * **任一侧缺文件**都报红（⇒ 新增文件不许漏检）；
   * 根对**跳过**独立成对的子树（``engine`` / ``probes`` / ``llm`` / ``tests``）
     与**显式豁免**的过期副本（``manu/daily/`` ⇒ 活链只有 ``manu/etf_rotation/daily/``）。

🔴 **扫描入口统一**：⑨⑪⑫ 的判定都走「**扫一段源码文本**」的 helper
   （``_scan_turnover_ok_filters`` / ``_scan_table_cells_missing_label`` /
   ``_scan_handwritten_half_turnover``）⇒ 全仓守卫与**常驻变异反证**用例走**同一条**代码路径。
"""
from __future__ import annotations

import ast
import re
import sys
from pathlib import Path

import numpy as np
import pandas as pd
import pytest

REPO_ROOT = Path(__file__).resolve().parents[4]
MANU_DIR = REPO_ROOT / "manu"

#: 导入 ``config_v2``（唯一口径事实源）
_V2_DIR = Path(__file__).resolve().parents[1]
if str(_V2_DIR) not in sys.path:
    sys.path.insert(0, str(_V2_DIR))
import config_v2 as C  # noqa: E402

#: 允许保留「未封顶惩罚」的文件（理由见模块 docstring）
ALLOWED_UNCAPPED_PENALTY = {
    REPO_ROOT / "manu" / "etf_rotation" / "v2" / "config_v2.py",
    REPO_ROOT / "manu" / "tools" / "v2_rerank_val_objective.py",
}

#: 报告出口（必须并列披露单/双边）
REPORT_SURFACES = (
    REPO_ROOT / "manu" / "etf_rotation" / "v2" / "report.py",
    REPO_ROOT / "manu" / "tools" / "v2_render_walkforward_report.py",
)


def _scan_files() -> list[Path]:
    """全仓**生产** ``.py``（排除 tests / 备份 / ``__pycache__``）。"""
    out: list[Path] = []
    for path in MANU_DIR.rglob("*.py"):
        parts = path.parts
        if any(part == "__pycache__" for part in parts):
            continue
        if any(part.startswith("_backup_") for part in parts):
            continue
        if "tests" in parts or path.name.startswith("test_"):
            continue
        out.append(path)
    return sorted(out)


def _parse(path: Path) -> ast.Module:
    return ast.parse(path.read_text(encoding="utf-8"), filename=str(path))


def _uncapped_penalty_nodes(tree: ast.AST) -> list[str]:
    """AST 找「**第二份**换手惩罚实现」（两种形态）。

    * 形态 1：``C.TURNOVER_PENALTY_RATE * ...``（自己拿费率常量算）；
    * 形态 2：``0.05 * max(...)``（把费率**写死**）。

    🔴 口径：**只准调** ``config_v2.turnover_penalty()`` ⇒ 哪怕写成 inline
    ``min(CAP, RATE * max(...))`` 也算「第二份实现」⇒ 违规
    ⇒ 目的是保证**封顶语义只有一个事实源**，杜绝「改了一处、漏了另一处」。
    """
    hits: list[str] = []
    for node in ast.walk(tree):
        if not isinstance(node, ast.BinOp) or not isinstance(node.op, ast.Mult):
            continue
        left, right = node.left, node.right
        if isinstance(left, ast.Attribute) and left.attr == "TURNOVER_PENALTY_RATE":
            hits.append(f"line {node.lineno}: TURNOVER_PENALTY_RATE * ...（未封顶）")
            continue
        if (
            isinstance(left, ast.Constant)
            and isinstance(left.value, float)
            and abs(left.value - 0.05) < 1e-12
            and isinstance(right, ast.Call)
            and isinstance(right.func, ast.Name)
            and right.func.id == "max"
        ):
            hits.append(f"line {node.lineno}: 0.05 * max(...)（写死费率 + 未封顶）")
    return hits


def _gate_calls_missing_mode(tree: ast.AST) -> list[str]:
    """``apply_turnover_gate(...)`` 调用点缺 ``mode=`` 的行号。"""
    hits: list[str] = []
    for node in ast.walk(tree):
        if not isinstance(node, ast.Call):
            continue
        func = node.func
        name = (
            func.attr
            if isinstance(func, ast.Attribute)
            else (func.id if isinstance(func, ast.Name) else "")
        )
        if name != "apply_turnover_gate":
            continue
        if not any(kw.arg == "mode" for kw in node.keywords):
            hits.append(f"line {node.lineno}")
    return hits


#: 🔴 规则③ 的**窄豁免**：``check_turnover_target`` 的返回值是「**裁决**」
#:    （``{"ok", "annualized_turnover", "target", "message"}``），**不是换手记录**；
#:    且 gate 模式的返回结构被**刻意**冻结为 4 个键（供旧产物审计对表），
#:    其 ``message`` 里已经**含单边数值** ⇒ 不该逼它加键。
#:    ⚠️ 豁免**只针对这个函数** ⇒ ``metrics.summarize()``（真正的记录产出方）**仍受守卫**。
RULE3_EXEMPT_FUNCTIONS = frozenset({"check_turnover_target"})


def _dict_nodes(tree: ast.AST, skip_functions: frozenset[str]) -> list[ast.Dict]:
    """收集 dict 字面量，但**不进入**被豁免函数的子树。"""
    found: list[ast.Dict] = []

    class _Visitor(ast.NodeVisitor):
        def visit_FunctionDef(self, node: ast.FunctionDef) -> None:  # noqa: N802
            if node.name in skip_functions:
                return
            self.generic_visit(node)

        def visit_AsyncFunctionDef(self, node) -> None:  # noqa: N802
            if node.name in skip_functions:
                return
            self.generic_visit(node)

        def visit_Dict(self, node: ast.Dict) -> None:  # noqa: N802
            found.append(node)
            self.generic_visit(node)

    _Visitor().visit(tree)
    return found


def _turnover_dicts_missing_one_way(tree: ast.AST) -> list[str]:
    """同一个 dict 里落 ``annualized_turnover`` 却没有单边伴随键的行号。"""
    hits: list[str] = []
    for node in _dict_nodes(tree, RULE3_EXEMPT_FUNCTIONS):
        keys = [
            k.value
            for k in node.keys
            if isinstance(k, ast.Constant) and isinstance(k.value, str)
        ]
        if "annualized_turnover" in keys and "annualized_turnover_one_way" not in keys:
            hits.append(f"line {node.lineno}")
    return hits


# ----------------------------------------------------------------- ① 惩罚唯一实现
def test_全仓没有未封顶的换手惩罚实现() -> None:
    """🔴 除唯一实现与旧口径审计函数外，全仓不许再出现未封顶惩罚。"""
    violations: list[str] = []
    for path in _scan_files():
        if path in ALLOWED_UNCAPPED_PENALTY:
            continue
        hits = _uncapped_penalty_nodes(_parse(path))
        if hits:
            rel = path.relative_to(REPO_ROOT)
            violations.extend(f"{rel}: {h}" for h in hits)
    assert not violations, (
        "🔴 全仓出现**第二份**换手惩罚实现（⇒ 封顶语义失去唯一事实源，"
        "且极易退回「无上限 ⇒ 目标函数退化成最小化换手」的老坑）：\n  "
        + "\n  ".join(violations)
        + "\n  ⇒ 一律改调 `config_v2.turnover_penalty()`（封顶 "
        + "TURNOVER_PENALTY_CAP，唯一实现）。"
    )


# ----------------------------------------------------------------- ② 闸门模式显式
def test_每个换手闸门调用点都显式传了mode() -> None:
    """🔴 ``apply_turnover_gate`` 的默认值是旧行为 ``gate`` ⇒ 吃默认值 = 口径回退。"""
    violations: list[str] = []
    for path in _scan_files():
        hits = _gate_calls_missing_mode(_parse(path))
        if hits:
            rel = path.relative_to(REPO_ROOT)
            violations.extend(f"{rel}: {h}" for h in hits)
    assert not violations, (
        "🔴 换手闸门调用点缺 `mode=`（默认 = 旧硬闸门）：\n  " + "\n  ".join(violations)
    )


# ----------------------------------------------------------------- ③ 单/双边并列披露
def test_落盘方必须同时落单边换手率() -> None:
    """🔴 同一条记录里落双边换手 ⇒ 必须**同时**落单边换手（并列披露）。"""
    violations: list[str] = []
    for path in _scan_files():
        hits = _turnover_dicts_missing_one_way(_parse(path))
        if hits:
            rel = path.relative_to(REPO_ROOT)
            violations.extend(f"{rel}: {h}" for h in hits)
    assert not violations, (
        "🔴 落盘双边换手却没有同字典的单边伴随键（并列披露缺失）：\n  "
        + "\n  ".join(violations)
        + "\n  ⇒ 加 `\"annualized_turnover_one_way\": config_v2.one_way_turnover(...)`。"
    )


# ----------------------------------------------------------------- ④ 报告层并列披露
def test_报告出口并列披露单双边换手率() -> None:
    """🔴 两个报告出口都必须同时出现「年化换手（双边）」与「年化换手（单边）」。"""
    violations: list[str] = []
    for path in REPORT_SURFACES:
        if not path.exists():
            violations.append(f"{path.relative_to(REPO_ROOT)}: 文件不存在")
            continue
        text = path.read_text(encoding="utf-8")
        if "年化换手（双边）" not in text:
            violations.append(f"{path.relative_to(REPO_ROOT)}: 缺「年化换手（双边）」")
        if "年化换手（单边）" not in text:
            violations.append(f"{path.relative_to(REPO_ROOT)}: 缺「年化换手（单边）」")
    assert not violations, (
        "🔴 报告层未并列披露单/双边换手率：\n  " + "\n  ".join(violations)
    )


# ----------------------------------------------------------------- 守卫自证（变异反证）
def test_守卫本身有牙_坏代码必须被抓到() -> None:
    """🔴 变异反证：故意写一段坏代码 ⇒ 三条守卫必须**都**报出来。

    🔴 一个抓不住东西的守卫等于没有守卫 ⇒ 本条是守卫**自己的**回归。
    """
    bad = (
        "import config_v2 as C\n"
        "def f(turn):\n"
        "    penalty = 0.05 * max(0.0, turn - float(C.TURNOVER_TARGET))\n"
        '    return {"annualized_turnover": turn}\n'
    )
    tree = ast.parse(bad)
    assert _uncapped_penalty_nodes(tree), (
        "🔴 规则① 必须抓到「自己实现的换手惩罚」⇒ 否则守卫没牙"
    )
    assert _turnover_dicts_missing_one_way(tree), (
        "🔴 规则③ 必须抓到「落双边却没落单边」⇒ 否则守卫没牙"
    )
    bad_gate = "def g(result):\n    return apply_turnover_gate(result, 4.0)\n"
    assert _gate_calls_missing_mode(ast.parse(bad_gate)), (
        "🔴 规则② 必须抓到「闸门调用点没传 mode=」⇒ 否则守卫没牙"
    )


def test_守卫本身有牙_好代码必须放过() -> None:
    """🔴 反向：合规写法**不许**被误报（守卫不许瞎抓）。"""
    good = (
        "import config_v2 as C\n"
        "def f(turn):\n"
        "    penalty = C.turnover_penalty(turn)\n"
        '    return {"annualized_turnover": turn,'
        ' "annualized_turnover_one_way": C.one_way_turnover(turn)}\n'
        "def g(result):\n"
        "    return apply_turnover_gate(result, 4.0, mode=C.TURNOVER_TARGET_MODE)\n"
    )
    tree = ast.parse(good)
    assert not _uncapped_penalty_nodes(tree), "🔴 规则① 误报了合规写法"
    assert not _turnover_dicts_missing_one_way(tree), "🔴 规则③ 误报了合规写法"
    assert not _gate_calls_missing_mode(tree), "🔴 规则② 误报了合规写法"


# ----------------------------------------------------------------- ⑤ 目标函数描述串唯一化
def _objective_literals(tree: ast.AST) -> list[tuple[int, str]]:
    """落盘 ``"objective"`` 键里**写死的字符串常量**（必须来自唯一事实源）。"""
    hits: list[tuple[int, str]] = []
    for node in _dict_nodes(tree, frozenset()):
        for key, value in zip(node.keys, node.values):
            if (
                isinstance(key, ast.Constant)
                and isinstance(key.value, str)
                and key.value == "objective"
                and isinstance(value, ast.Constant)
                and isinstance(value.value, str)
            ):
                hits.append((node.lineno, value.value))
    return hits


def test_目标函数描述串必须来自唯一事实源() -> None:
    """🔴 同一句 objective 历史上在 3 个文件里各写一遍 ⇒ 改口径时漏改 ⇒ 落盘产物说假话。

    ⇒ 现在只允许写 ``config_v2.OBJECTIVE_DESCRIPTION`` 这一份；
    任何**写死的字符串**（哪怕只差一个字）都算违规。
    """
    violations: list[str] = []
    for path in _scan_files():
        for lineno, literal in _objective_literals(_parse(path)):
            if literal != C.OBJECTIVE_DESCRIPTION:
                rel = path.relative_to(REPO_ROOT)
                violations.append(f"{rel}: line {lineno}: {literal!r}")
    assert not violations, (
        "🔴 目标函数描述串**写死**在业务文件里（⇒ 换手口径改版时会悄悄变成假话）：\n  "
        + "\n  ".join(violations)
        + "\n  ⇒ 一律改用 `config_v2.OBJECTIVE_DESCRIPTION`。"
    )


def test_唯一描述串本身必须反映当前口径() -> None:
    """🔴 唯一描述串必须**写明**封顶、披露基准、双边口径三件事。"""
    desc = C.OBJECTIVE_DESCRIPTION
    assert f"{C.TURNOVER_PENALTY_CAP}" in desc, (
        f"🔴 唯一描述串必须写明惩罚封顶值 {C.TURNOVER_PENALTY_CAP}；实得 {desc!r}"
    )
    assert f"{C.TURNOVER_TARGET}" in desc, (
        f"🔴 唯一描述串必须写明换手基准 {C.TURNOVER_TARGET}；实得 {desc!r}"
    )
    assert "披露基准" in desc and "双边" in desc, (
        f"🔴 唯一描述串必须写明「披露基准」与「双边」口径；实得 {desc!r}"
    )


# ----------------------------------------------------------------- ⑥ 双边/单边行为级口径一致
def test_回测产物的双边与单边换手必须口径一致() -> None:
    """🔴 同一条记录里：单边必须 = 0.5 × 双边（口径自洽）。"""
    import backtest_v2 as BV

    dates = pd.to_datetime(["2024-01-01", "2024-01-02", "2024-01-03", "2024-01-04"])
    px = pd.DataFrame({"A": [10.0] * 4, "B": [20.0] * 4}, index=dates)
    res = BV.run_backtest_v2(
        px, {"2024-01-02": {"A": 1.0}}, commission=0.0, slippage=0.0
    )
    two = res["annualized_turnover"]
    one = res["annualized_turnover_one_way"]
    assert np.isfinite(two), f"🔴 正常回测的年化双边换手不该是 NaN（实得 {two!r}）"
    assert one == pytest.approx(0.5 * two, abs=1e-12), (
        f"🔴 单边换手必须 = 0.5 × 双边；实得 单边 {one!r} / 双边 {two!r}"
    )


def test_双边算不出来时单边也必须是NaN() -> None:
    """🔴 双边 NaN ⇒ 单边**也是 NaN**（不许 NaN 与 0.0 混搭在同一条记录里）。

    🔴 口径锚点：``metrics.summarize`` 的年数 = (净值点数 − 1) / periods_per_year
    ⇒ **单点净值** ⇒ 年数 = 0 ⇒ 双边年化换手 NaN ⇒ 单边必须**也是 NaN**。

    ⚠️ 注：``backtest_v2.run_backtest_v2`` 硬性要求 **≥2 个交易日**
    ⇒ 它的 NaN 分支在**公开 API 上不可达**（纯防御分支）
    ⇒ 故 NaN 一致性在 ``metrics.summarize`` 这层测（可达 ＋ 更根本）。
    """
    import metrics as M

    idx = pd.to_datetime(["2024-01-01"])
    nav = pd.Series([1.0], index=idx, name="nav")
    out = M.summarize(nav, None, None)
    two = out["annualized_turnover"]
    one = out["annualized_turnover_one_way"]
    assert np.isnan(two), f"🔴 单点净值 ⇒ 双边年化换手应是 NaN（实得 {two!r}）"
    assert np.isnan(one), (
        f"🔴 双边是 NaN ⇒ 单边**也必须是 NaN**（实得 {one!r}；"
        "0.0 会与 NaN 混搭 ⇒ 读者会误以为「换手为零」而不是「算不出来」）"
    )


def test_回测产物与指标汇总的单边口径必须一致() -> None:
    """🔴 ``backtest_v2`` 的结果 dict 与 ``metrics.summarize`` 的单边口径必须一致。"""
    import backtest_v2 as BV
    import metrics as M

    dates = pd.to_datetime(["2024-01-01", "2024-01-02", "2024-01-03", "2024-01-04"])
    px = pd.DataFrame({"A": [10.0] * 4, "B": [20.0] * 4}, index=dates)
    res = BV.run_backtest_v2(
        px, {"2024-01-02": {"A": 1.0}}, commission=0.0, slippage=0.0
    )
    summary = res.get("metrics") or {}
    two = res["annualized_turnover"]
    one = res["annualized_turnover_one_way"]
    if "annualized_turnover_one_way" in summary:
        assert summary["annualized_turnover_one_way"] == pytest.approx(one, abs=1e-12), (
            f"🔴 回测结果 dict 与 metrics 汇总的单边换手必须一致；"
            f"实得 {summary['annualized_turnover_one_way']!r} vs {one!r}"
        )
    assert one == pytest.approx(0.5 * two, abs=1e-12)
    assert M.summarize(res["nav"], None, res.get("turnover_by_date"))[
        "annualized_turnover_one_way"
    ] == pytest.approx(one, abs=1e-9), (
        "🔴 同一份净值+换手 ⇒ summarize 与回测结果 dict 的单边换手必须一致"
    )


# =============================================================================
# ⑦～⑪ 续三新增守卫（2026-11-18 第二轮：V1 baseline 口径正名 + 换手不许参与选参）
# =============================================================================
#: V1 回测链（``cost_assumption`` 落盘处）—— 必须并列落单/双边。
V1_COST_SURFACES = (
    REPO_ROOT / "manu" / "etf_rotation" / "backtest.py",
    REPO_ROOT / "manu" / "backtest.py",
)
#: 表格出口（列头必须带口径标注）。
TABLE_SURFACES = (
    REPO_ROOT / "manu" / "etf_rotation" / "v2" / "report.py",
    REPO_ROOT / "manu" / "tools" / "v2_render_walkforward_report.py",
    REPO_ROOT / "manu" / "tools" / "v2_walkforward_probe.py",
    REPO_ROOT / "manu" / "tools" / "gen_v2_scorecard.py",
    # 🔴 2026-11-18 补漏：归因探针也会落「换手」表（step1/2/3 的 csv/md）
    #    ⇒ 它一旦把「换手」列写成**表头**，就必须同时标明双边 / 单边。
    REPO_ROOT / "manu" / "tools" / "v2_val_attribution_probe.py",
    REPO_ROOT / "manu" / "etf_rotation" / "analyze.py",
    REPO_ROOT / "manu" / "analyze.py",
)


# ----------------------------------------------------------------- ⑦ V1 baseline 口径正名
def test_V1_baseline换手必须按双边正名并并列单边() -> None:
    """🔴 **V1 baseline 的 16.57× 是双边**（Σ|Δw| / 年）⇒ 键名必须正名，且并列落单边。

    🔴 为什么会错：``backtest.py`` 的 ``turnover = Σ|w_new − w_prev|`` = Σ|Δw|
       = **双边**（= 总成交额/组合净值）⇒ 它被装进键名
       ``annualized_one_way_turnover``（说单边）⇒ **数字被错标 2 倍**。
       算术自证：16.568 × 6.5bp × 2.716 年 ≈ 2.93% = V1 记录的成本拖累 ✓。
    """
    baseline = dict(C.V1_BASELINE)
    assert "annualized_one_way_turnover" not in baseline, (
        "🔴 V1 baseline 里还留着错标键 `annualized_one_way_turnover`"
        "（它的值是**双边**）⇒ 必须正名为 `annualized_turnover_two_way`"
    )
    assert "annualized_turnover_two_way" in baseline, "🔴 V1 baseline 缺双边换手键"
    assert "annualized_turnover_one_way" in baseline, "🔴 V1 baseline 缺单边换手键"
    two = float(baseline["annualized_turnover_two_way"])
    one = float(baseline["annualized_turnover_one_way"])
    assert one == pytest.approx(C.one_way_turnover(two), abs=1e-9), (
        f"🔴 V1 baseline 的单边换手必须 = 唯一实现 `one_way_turnover(双边)`；"
        f"实得 单边 {one!r} / 双边 {two!r}"
    )


# ----------------------------------------------------------------- ⑧ V1 链并列披露
def test_V1回测链必须并列落单双边换手() -> None:
    """🔴 V1 回测链的 ``cost_assumption`` 同 dict 必须并列落双边 + 单边。"""
    violations: list[str] = []
    for path in V1_COST_SURFACES:
        if not path.exists():
            violations.append(f"{path.relative_to(REPO_ROOT)}: 文件不存在")
            continue
        tree = _parse(path)
        found = False
        for node in ast.walk(tree):
            if not isinstance(node, ast.Dict):
                continue
            keys = [k.value for k in node.keys
                    if isinstance(k, ast.Constant) and isinstance(k.value, str)]
            if "annualized_turnover_two_way" not in keys:
                continue
            found = True
            if "annualized_turnover_one_way" not in keys:
                violations.append(
                    f"{path.relative_to(REPO_ROOT)}:{node.lineno} "
                    "落盘双边换手却缺同字典的单边键"
                )
        if not found:
            violations.append(
                f"{path.relative_to(REPO_ROOT)}: 找不到落盘 `annualized_turnover_two_way` 的 dict"
            )
    assert not violations, (
        "🔴 V1 回测链换手口径未并列披露：\n  " + "\n  ".join(violations)
    )


def test_V1回测链镜像文件必须逐字一致() -> None:
    """🔴 镜像文件对（``manu/backtest.py`` ↔ ``manu/etf_rotation/backtest.py``）必须一致。"""
    import hashlib

    digests = {}
    for path in V1_COST_SURFACES:
        digests[path.relative_to(REPO_ROOT)] = hashlib.md5(
            path.read_bytes()).hexdigest()
    assert len(set(digests.values())) == 1, (
        "🔴 V1 回测链镜像文件**内容不一致**（改一边忘了另一边）：\n  "
        + "\n  ".join(f"{k}: {v}" for k, v in digests.items())
    )


#: 🔴 全仓**镜像对**（``manu/`` ↔ ``manu/etf_rotation/`` 的同名模块）。
#:
#: 🔴 为什么这是**硬要求**（2026-11-18 续四实测踩到）：
#:    ``engine`` 是**命名空间包**（两个目录都**没有** ``__init__.py``）
#:    ⇒ ``manu/engine/`` 与 ``manu/etf_rotation/engine/`` 会**同时**进 ``engine.__path__``
#:    ⇒ 实跑加载的是**哪一份取决于 sys.path 顺序**
#:    ⇒ 改了一边忘了另一边 ⇒ 实跑跑的是**另一份代码**
#:    （本轮真踩：刹车修复只改在 ``manu/etf_rotation/engine/portfolio.py``，
#:      而 pytest 全量里实跑加载的是 ``manu/engine/portfolio.py``
#:      ⇒ 修复「看起来没生效」⇒ 白查一轮）。
#:    ⇒ 同名顶层模块（``config`` / ``report`` / ``analyze`` / ``backtest`` …）
#:      同样**先到先得** ⇒ 一旦漂移，症状是「改了没反应」或「单跑绿、全量红」。
MIRROR_PAIRS = (
    ("manu", "manu/etf_rotation"),
    ("manu/engine", "manu/etf_rotation/engine"),
    ("manu/probes", "manu/etf_rotation/probes"),
    ("manu/llm", "manu/etf_rotation/llm"),
    # 🔴 2026-11-18 补漏：``tests/`` 同样是**镜像对**（55 个文件，逐字一致）
    #    ⇒ 以前用 ``glob("*.py")`` 只扫**顶层** ⇒ 子目录里的镜像文件**看不见**
    #    ⇒ 现在改成递归（``rglob``）⇒ 这一对必须一起纳管。
    ("manu/tests", "manu/etf_rotation/tests"),
)

#: 🔴 **显式豁免**的镜像对（**不是**漏检）：{（源侧, 镜像侧）: 豁免原因}。
#:    ``manu/daily/`` 是**已登记的过期副本**（活链只有 ``manu/etf_rotation/daily/``；
#:    见 ``manu/memory/rules.md`` 与 ``manu/memory/memory.md``）
#:    ⇒ 这一对**本来就不同** ⇒ 放进镜像对会**立刻转红**
#:    ⇒ 豁免写成**显式常量 + 注释**（让「不比对」是**登记过的**，不是「碰巧没扫到」）。
MIRROR_EXEMPT_PAIRS = {
    ("manu/daily", "manu/etf_rotation/daily"): (
        "``manu/daily/`` 是**已登记的过期副本**（活链只有 ``manu/etf_rotation/daily/``）"
        "⇒ 这一对**本来就不同** ⇒ 显式豁免"
    ),
}


def _mirror_py_relpaths(root: Path, skip_names: frozenset[str]) -> set[Path]:
    """``root`` 下所有 ``.py`` 的**相对路径**（**递归**；跳过缓存 / 备份 / 指定子树）。"""
    out: set[Path] = set()
    if not root.is_dir():
        return out
    for path in root.rglob("*.py"):
        parts = path.relative_to(root).parts
        if any(part == "__pycache__" for part in parts):
            continue
        if any(part.startswith("_backup_") for part in parts):
            continue
        if parts[0] in skip_names:
            continue
        out.add(Path(*parts))
    return out


def _mirror_in_scope(src_dir: Path, dst_dir: Path, rel: Path) -> bool:
    """这个相对路径**在不在镜像面内**。

    🔴 口径：**父目录**在两侧都存在 ⇒ 才是镜像面。
       ⇒ ``manu/tools/``（``manu/etf_rotation/tools/`` **不存在**）**不是**镜像面
       ⇒ 不拿它报「缺文件」（那会转红 ⇒ 守卫失信 ⇒ 以后没人看它）；
       ⇒ 反过来：**同一层**里少一个文件（``manu/data/x.py`` 有、镜像侧没有）
       ⇒ 父目录两侧都在 ⇒ **在镜像面内** ⇒ 报红。
    """
    return (src_dir / rel.parent).is_dir() and (dst_dir / rel.parent).is_dir()


def _mirror_pair_drift(src_dir: Path, dst_dir: Path, skip_names: frozenset[str]) -> list[str]:
    """这一对镜像目录的**漂移**清单（内容不一致 / 任一侧缺文件）。"""
    import hashlib

    drift: list[str] = []
    src_files = _mirror_py_relpaths(src_dir, skip_names)
    dst_files = _mirror_py_relpaths(dst_dir, skip_names)
    for rel in sorted(src_files | dst_files):
        if not _mirror_in_scope(src_dir, dst_dir, rel):
            continue
        src_file, dst_dir_file = src_dir / rel, dst_dir / rel
        if not dst_dir_file.is_file():
            drift.append(f"{rel}：源侧有、**镜像侧缺文件**（新增文件不许漏检）")
            continue
        if not src_file.is_file():
            drift.append(f"{rel}：**源侧缺文件**、镜像侧有（新增文件不许漏检）")
            continue
        a = hashlib.md5(src_file.read_bytes()).hexdigest()
        b = hashlib.md5(dst_dir_file.read_bytes()).hexdigest()
        if a != b:
            drift.append(f"{rel}：内容不一致（{a[:8]} != {b[:8]}）")
    return drift


def _repo_mirror_skip_names(src_rel: str, dst_rel: str) -> frozenset[str]:
    """仓库内这一对镜像在递归扫描时**必须跳过**的子树名。"""
    names: set[str] = set()
    # ① **独立成对**的子树（``engine`` / ``probes`` / ``llm`` / ``tests``）
    #    ⇒ 由**自己那一对**负责比对 ⇒ 根对里必须跳过
    #    （否则会把 ``manu/etf_rotation/**`` 里的文件拿去和
    #      ``manu/etf_rotation/etf_rotation/**`` 比 ⇒ 全部「缺文件」⇒ 假红）。
    for other_src, _other_dst in MIRROR_PAIRS:
        parts = Path(other_src).parts
        if len(parts) == 2 and parts[0] == "manu":
            names.add(parts[1])
    # ② 镜像目录本身**嵌在源目录里**（``manu/etf_rotation`` ⊂ ``manu``）⇒ 源侧跳过它。
    src_parts, dst_parts = Path(src_rel).parts, Path(dst_rel).parts
    if len(dst_parts) == len(src_parts) + 1 and dst_parts[:-1] == src_parts:
        names.add(dst_parts[-1])
    # ③ **显式豁免**的对（过期副本）⇒ 两侧都跳过（根对的 src 是它们的祖先 ⇒ 也要跳过）。
    for e_src, e_dst in MIRROR_EXEMPT_PAIRS:
        for rel in (e_src, e_dst):
            rel_parts = Path(rel).parts
            for base in (src_parts, dst_parts):
                if len(rel_parts) > len(base) and rel_parts[: len(base)] == base:
                    names.add(rel_parts[len(base)])
                    break
    return frozenset(names)


def test_全仓镜像文件必须逐字一致() -> None:
    """🔴 所有镜像对必须**逐字一致**（改一边必须改另一边）。

    🔴 判定口径（2026-11-18 收紧）：
       * **递归**扫描（``rglob("*.py")``）⇒ 子目录里的镜像文件也算数；
       * **任一侧缺文件**都报红（⇒ 新增文件不许漏检）；
       * 根对**跳过**独立成对的子树（``engine`` / ``probes`` / ``llm`` / ``tests``）
         与**显式豁免**的过期副本（``manu/daily/``）。
    """
    violations: list[str] = []
    for src_rel, dst_rel in MIRROR_PAIRS:
        src_dir, dst_dir = REPO_ROOT / src_rel, REPO_ROOT / dst_rel
        if not src_dir.is_dir() or not dst_dir.is_dir():
            violations.append(f"{src_rel} ↔ {dst_rel}：镜像目录不存在（守卫失效）")
            continue
        skip = _repo_mirror_skip_names(src_rel, dst_rel)
        for item in _mirror_pair_drift(src_dir, dst_dir, skip):
            violations.append(f"{src_rel} ↔ {dst_rel}: {item}")
    assert not violations, (
        "🔴 镜像文件**内容不一致 / 缺文件**（改一边忘了另一边 ⇒ 实跑可能加载另一份）：\n  "
        + "\n  ".join(violations)
    )


def test_规则13_镜像对漂移必须被抓到() -> None:
    """🔴 规则⑬ 变异反证：镜像对的一侧**内容不同 / 缺文件** ⇒ 守卫必须报红。

    🔴 手法：**临时目录**里造一对镜像 ⇒ **不碰真源码**（不污染生产代码）。
    """
    import tempfile

    with tempfile.TemporaryDirectory() as tmp:
        root = Path(tmp)
        left, right = root / "left", root / "right"
        left.mkdir()
        right.mkdir()
        (left / "mod.py").write_text("VALUE = 1\n", encoding="utf-8")
        (right / "mod.py").write_text("VALUE = 1\n", encoding="utf-8")
        assert not _mirror_pair_drift(left, right, frozenset()), (
            "🔴 规则⑬ 内容一致时不该报漂移（守卫误报）"
        )
        # 🔴 故意让**镜像侧**内容不同 ⇒ 必须报红
        (right / "mod.py").write_text("VALUE = 2\n", encoding="utf-8")
        assert _mirror_pair_drift(left, right, frozenset()), (
            "🔴 规则⑬ 漏抓「镜像对内容漂移」⇒ 守卫没牙"
        )
        # 🔴 故意让**镜像侧少一个文件 + 多一个文件** ⇒ 必须报红（新增文件不许漏检）
        (right / "mod.py").unlink()
        (right / "new_mod.py").write_text("VALUE = 3\n", encoding="utf-8")
        assert _mirror_pair_drift(left, right, frozenset()), (
            "🔴 规则⑬ 漏抓「镜像侧缺文件 / 源侧缺文件」⇒ 守卫没牙"
        )


# ----------------------------------------------------------------- ⑨ 换手不许参与选参
#: 🔴 披露标记的名字（``turnover_ok``＝「换手是否达到披露基准」）。
_TURNOVER_OK_TOKEN = "turnover_ok"

#: 🔴 「以谓词身份消费候选」的调用：``filter`` / ``sorted(key=)`` / ``min`` / ``max``
#:    / ``any`` / ``all`` ⇒ 这些位置上的 ``turnover_ok`` 也是**参与选参**
#:    （过滤 / 排序 ⇒ 换手在暗处挑赢家）。
_PREDICATE_CALLS = frozenset({"filter", "sorted", "min", "max", "any", "all"})


def _contains_turnover_ok(node: ast.AST) -> bool:
    """这个 AST 子树里有没有 ``turnover_ok``（标识符 / 属性 / 字典键 / 字符串）。"""
    for sub in ast.walk(node):
        if isinstance(sub, ast.Name) and _TURNOVER_OK_TOKEN in sub.id:
            return True
        if isinstance(sub, ast.Attribute) and _TURNOVER_OK_TOKEN in sub.attr:
            return True
        if isinstance(sub, ast.arg) and _TURNOVER_OK_TOKEN in sub.arg:
            return True
        if isinstance(sub, ast.keyword) and sub.arg and _TURNOVER_OK_TOKEN in sub.arg:
            return True
        if (
            isinstance(sub, ast.Constant)
            and isinstance(sub.value, str)
            and _TURNOVER_OK_TOKEN in sub.value
        ):
            return True
    return False


def _turnover_ok_condition_nodes(tree: ast.AST) -> list[tuple[int, str, ast.AST]]:
    """全树里**处于条件 / 过滤位置**的表达式。

    🔴 覆盖（漏一个就是让「换手参与选参」溜过去的口子）：
       * ``ListComp`` / ``SetComp`` / ``GeneratorExp`` / ``DictComp`` 的
         ``generators[].ifs``（推导式的过滤条件**挂在 generator 上**
         ⇒ ``ListComp.ifs`` 根本不存在 ⇒ 只挂 ListComp 会漏掉另外三种写法）；
       * ``ast.If.test`` / ``ast.IfExp.test`` / ``ast.While.test`` / ``ast.Assert.test``；
       * 谓词调用（``filter`` / ``sorted(key=)`` / ``min`` / ``max`` / ``any`` / ``all``）
         的 lambda 谓词 ⇒ **排序**也是挑赢家。
    """
    out: list[tuple[int, str, ast.AST]] = []
    for node in ast.walk(tree):
        if isinstance(node, (ast.ListComp, ast.SetComp, ast.GeneratorExp, ast.DictComp)):
            for generator in node.generators:
                for cond in generator.ifs:
                    out.append((node.lineno, f"{type(node).__name__} 推导的过滤条件", cond))
        elif isinstance(node, ast.If):
            out.append((node.lineno, "if 条件", node.test))
        elif isinstance(node, ast.IfExp):
            out.append((node.lineno, "if 表达式条件", node.test))
        elif isinstance(node, ast.While):
            out.append((node.lineno, "while 条件", node.test))
        elif isinstance(node, ast.Assert):
            out.append((node.lineno, "assert 条件", node.test))
        elif isinstance(node, ast.Call):
            fname = getattr(node.func, "id", None) or getattr(node.func, "attr", None)
            if fname in _PREDICATE_CALLS:
                for arg in node.args:
                    if isinstance(arg, ast.Lambda):
                        out.append((node.lineno, f"{fname}() 的谓词", arg.body))
                for kw in node.keywords:
                    if kw.arg == "key" and isinstance(kw.value, ast.Lambda):
                        out.append((node.lineno, f"{fname}(key=) 的排序键", kw.value.body))
    return out


def _turnover_ok_filter_hits(tree: ast.AST) -> list[str]:
    """规则⑨ 命中行（``turnover_ok`` 出现在**条件 / 过滤**位置）。"""
    hits: list[str] = []
    for lineno, where, cond in _turnover_ok_condition_nodes(tree):
        if not _contains_turnover_ok(cond):
            continue
        hits.append(f"line {lineno}: {where}里出现 `turnover_ok`")
    return hits


def _scan_turnover_ok_filters(source: str) -> list[str]:
    """🔴 规则⑨ 的**扫描入口**（对任意源码文本判定）⇒ 常驻变异反证用例可直接喂内存样例。"""
    return _turnover_ok_filter_hits(ast.parse(source))


#: 🔴 选参链的**关键文件**（原⑨ 的扫描目标）⇒ 必须存在（删文件 = 绕过守卫 ⇒ 报红）。
_TURNOVER_FILTER_TARGETS = (
    REPO_ROOT / "manu" / "tools" / "v2_val_attribution_probe.py",
    REPO_ROOT / "manu" / "etf_rotation" / "v2" / "run_v2.py",
)


def test_换手达标标记不许参与选参过滤() -> None:
    """🔴 ``turnover_ok`` 只是**披露标记** ⇒ 不许拿它做**前置过滤**去选参。

    🔴 为什么要盯：2026-11-18 已把「≤4.0× 换手」从**硬拦截**降级为**披露基准**；
       若选参逻辑仍按 ``turnover_ok`` 过滤 ⇒ 换手仍在**暗处**主导选参
       （与「先废除硬拦截」的目的相反，且**不留痕**）。

    🔴 判定口径：**扫全树**里任意「**条件 / 过滤位置**」出现 ``turnover_ok`` 即违规
       （推导式 ``ifs`` / ``if`` / ``if 表达式`` / ``while`` / ``assert`` / 谓词调用）；
       ⇒ **披露语境**（把 ``turnover_ok`` 写进 dict / CSV / md 的**赋值**）**不算**违规。
    """
    violations: list[str] = []
    for path in _scan_files():
        hits = _scan_turnover_ok_filters(path.read_text(encoding="utf-8"))
        if hits:
            rel = path.relative_to(REPO_ROOT)
            violations.extend(f"{rel}: {h}" for h in hits)
    for path in _TURNOVER_FILTER_TARGETS:
        if not path.exists():
            violations.append(f"{path.relative_to(REPO_ROOT)}: 文件不存在（选参链被删 ⇒ 守卫失效）")
    assert not violations, (
        "🔴 换手达标标记参与了选参过滤（换手只作披露，不作选参依据）：\n  "
        + "\n  ".join(violations)
    )


# ----------------------------------------------------------------- ⑩ 口径裁决函数也必须显式传 mode
def test_换手裁决函数调用点必须显式传mode() -> None:
    """🔴 ``check_turnover_target`` 的默认值是旧硬闸门语义 ⇒ 调用点必须显式传 ``mode=``。"""
    violations: list[str] = []
    for path in _scan_files():
        for node in ast.walk(_parse(path)):
            if not isinstance(node, ast.Call):
                continue
            func = getattr(node.func, "id", None) or getattr(node.func, "attr", None)
            if func != "check_turnover_target":
                continue
            if not any(kw.arg == "mode" for kw in node.keywords):
                violations.append(f"{path.relative_to(REPO_ROOT)}:{node.lineno}")
    assert not violations, (
        "🔴 `check_turnover_target` 调用点缺 `mode=`（默认 = 旧硬闸门语义）：\n  "
        + "\n  ".join(violations)
    )


# ----------------------------------------------------------------- ⑪ 表格列头必须带口径
#: 判定「这个单元格是不是**在报一个换手数**」的标志串。
#: 🔴 只认**指标型**表述（年化换手 / 换手× / 换手（…） / 原始键名 / 裸列名「换手」），
#:    **不认**散文（「换手更少」「压换手」「换手口径」「换手定义」……）。
_TURNOVER_CELL_MARKERS = ("年化换手", "换手×", "换手（", "换手(", "annualized_turnover")


def _is_turnover_metric_cell(cell: str) -> bool:
    """这个表格单元格是不是**在报一个换手数**（⇒ 就必须标明单边/双边）。"""
    text = cell.strip()
    if text == "换手":
        return True
    return any(marker in text for marker in _TURNOVER_CELL_MARKERS)


def _cells_of_text(text: str) -> list[str]:
    """一段文本里的 **markdown 表格行**切出来的单元格。"""
    cells: list[str] = []
    for line in text.splitlines():
        stripped = line.strip()
        if stripped.startswith("|"):
            cells.extend(c.strip() for c in stripped.strip("|").split("|"))
    return cells


def _has_convention_label(cell: str) -> bool:
    """这个单元格有没有标明**双边 / 单边**口径。"""
    return ("双边" in cell) or ("单边" in cell)


def _table_cells_missing_label(tree: ast.AST) -> list[str]:
    """规则⑪ 命中行（表格里的「换手」单元格没有口径标签）。

    🔴 判定方式（AST，**不是**正则扫源码文本）：
       ① **markdown 行**字符串（``strip()`` 后以 ``|`` 开头）⇒ 按 ``|`` 切单元格；
       ② **列表式表格行**（``_md_table`` 的入参：≥2 个字符串元素的 list）⇒ 每个元素算一格。
       ⇒ 只看**表格语境**，正文散文里的「换手」二字**不算违规**。
    """
    hits: list[str] = []
    for node in ast.walk(tree):
        texts: list[str] = []
        if isinstance(node, ast.Constant) and isinstance(node.value, str):
            texts.append(node.value)
        elif isinstance(node, ast.JoinedStr):
            for part in node.values:
                if isinstance(part, ast.Constant) and isinstance(part.value, str):
                    texts.append(part.value)
        cells = [c for text in texts for c in _cells_of_text(text)]
        # ② 列表式表格行
        if isinstance(node, ast.List) and len(node.elts) >= 2:
            for elt in node.elts:
                if isinstance(elt, ast.Constant) and isinstance(elt.value, str):
                    cells.append(elt.value.strip())
        for cell in cells:
            if not cell:
                continue
            if not _is_turnover_metric_cell(cell):
                continue
            if _has_convention_label(cell):
                continue
            hits.append(f"line {getattr(node, 'lineno', 0)} 单元格「{cell}」未标口径")
    return hits


def _scan_table_cells_missing_label(source: str) -> list[str]:
    """🔴 规则⑪ 的**扫描入口**（对任意源码文本判定）⇒ 常驻变异反证用例可直接喂内存样例。"""
    return _table_cells_missing_label(ast.parse(source))


def test_换手表头必须标明单边或双边() -> None:
    """🔴 凡是**表头 / 表格单元格**里的「换手」列，必须同时标明**双边 / 单边**。

    🔴 为什么盯表头：正文里写「年化换手 14.10×」读者会**默认**按自己记的那个口径
       理解（本仓历史上就把双边值当单边读）⇒ 表头是唯一的**就地**提示点。

    🔴 判定方式（AST，**不是**正则扫源码文本）：
       ① **markdown 行**字符串（``strip()`` 后以 ``|`` 开头）⇒ 按 ``|`` 切单元格；
       ② **列表式表格行**（``_md_table`` 的入参：≥2 个字符串元素的 list）⇒ 每个元素算一格。
       ⇒ 只看**表格语境**，正文散文里的「换手」二字**不算违规**。
    """
    violations: list[str] = []
    for path in TABLE_SURFACES:
        if not path.exists():
            violations.append(f"{path.relative_to(REPO_ROOT)}: 文件不存在")
            continue
        hits = _scan_table_cells_missing_label(path.read_text(encoding="utf-8"))
        if hits:
            rel = path.relative_to(REPO_ROOT)
            violations.extend(f"{rel}: {h}" for h in hits)
    assert not violations, (
        "🔴 换手表头/单元格未标明单边/双边口径：\n  " + "\n  ".join(violations)
    )


# ----------------------------------------------------------------- ⑫ 单边换算唯一实现
#: 🔴 铁律来源：``manu/memory/rules.md`` §十四.112.4
#:    「⇒ 单边换算一律走 ``config_v2.one_way_turnover()``（**唯一实现**）⇒ 🔴 不许手写 ``* 0.5``」
#: 🔴 为什么要单独一条：规则① 只查「**第二份惩罚实现**」，**不查换算**
#:    ⇒ 手写 ``* 0.5`` 一样会口径分叉（双边值当单边用 ⇒ 数字差 2 倍）。
#: 豁免：① ``one_way_turnover`` 函数体本身（唯一实现本体）；
#:       ② ``TURNOVER_ONE_WAY_FACTOR`` 常量定义（``= 0.5`` 是**赋值**，不是乘法 ⇒ 天然不命中）。
_ONE_WAY_FACTOR_LITERAL = 0.5
#: 🔴 「换手语境」标志：标识符 / 参数名 / 字典键 / 字符串里出现 ``turnover`` 或「换手」。
_TURNOVER_CONTEXT_TOKENS = ("turnover", "换手")
#: 🔴 唯一实现函数名（豁免它的**函数体**）。
_ONE_WAY_IMPL_FUNCS = frozenset({"one_way_turnover"})
#: 🔴 单边换算系数常量名（豁免它的**定义**：``TURNOVER_ONE_WAY_FACTOR = 0.5``）。
_ONE_WAY_FACTOR_CONST = "TURNOVER_ONE_WAY_FACTOR"

#: 🔴 「单边语义」的**复合词**（按**完整片段链**匹配 ⇒ ``_one_way`` / ``to_one_way``
#:    / ``one_way_value`` 都算「单边」）。
_ONE_WAY_COMPOUND_SEGMENTS = (
    ("one", "way"),
    ("oneway",),
    ("single", "side"),
    ("singleside",),
    # 🔴 2026-11-18 主 agent 补：``one_side``（「单边」）也是 reviewer 点名的逃逸口
    #    ⇒ 之前只有 ``single_side`` ⇒ ``def one_side(x): return x * 0.5`` 会溜过去。
    ("one", "side"),
)
#: 🔴 「单边语义」的**单词**（必须**整个名字**就是它）
#:    ⇒ ``half_weight``（「一半权重」）**不算**单边语义（不做无边界子串匹配 ⇒ 不误伤）。
_ONE_WAY_SINGLE_SEGMENTS = frozenset({"one", "single", "half", "convert", "conv"})
#: 🔴 「单边语义」的中文口径词（中文没有词边界 ⇒ 直接子串匹配）。
_ONE_WAY_CHINESE_TOKENS = ("单边", "单侧", "换算")
#: 🔴 标识符切片段：``_`` / ``-`` 是分隔符，驼峰也切开（``oneWay`` ⇒ ``one`` / ``way``）。
_NAME_SEGMENT_RE = re.compile(r"[A-Z]?[a-z0-9]+|[A-Z]+(?![a-z])")


def _is_turnover_context_text(text: str) -> bool:
    """这段文本是不是「换手语境」的标志（含 ``turnover`` 或「换手」）。"""
    return any(token in text.lower() for token in _TURNOVER_CONTEXT_TOKENS)


def _name_segments(name: str) -> list[str]:
    """标识符 → **小写片段**列表（``_`` / ``-`` / 驼峰 都算分隔符）。"""
    segments: list[str] = []
    for chunk in re.split(r"[_\-]+", name):
        if not chunk:
            continue
        for piece in _NAME_SEGMENT_RE.findall(chunk):
            segments.append(piece.lower())
    return segments


def _has_one_way_semantic(name: str) -> bool:
    """这个名字有没有「**单边语义**」（**成词**匹配 ⇒ 不做无边界子串匹配）。

    🔴 为什么要有这一路信号：``def _one_way(x): return x * 0.5`` /
       ``def convert(t): return t * 0.5`` / ``one = two * 0.5``
       这三种写法**一个 ``turnover`` 字都没有** ⇒ 只靠「标识符含 turnover」会**漏放**
       ⇒ 等于给「第二份单边换算」留了后门。

    🔴 口径：
       * 复合词（``one_way`` / ``oneway`` / ``single_side``）按**完整片段链**匹配；
       * 单词（``one`` / ``single`` / ``half`` / ``convert``）必须**整个名字**就是它
         ⇒ ``half_weight``（「一半权重」）**不算** ⇒ 不误伤。
    """
    if not name:
        return False
    lowered = name.lower()
    if any(token in lowered for token in _ONE_WAY_CHINESE_TOKENS):
        return True
    segments = _name_segments(name)
    for chain in _ONE_WAY_COMPOUND_SEGMENTS:
        span = len(chain)
        for start in range(len(segments) - span + 1):
            if tuple(segments[start : start + span]) == chain:
                return True
    return len(segments) == 1 and segments[0] in _ONE_WAY_SINGLE_SEGMENTS


def _target_names(target: ast.AST) -> list[str]:
    """赋值目标里的名字（``Name`` / ``Attribute.attr`` / 解包目标）。"""
    if isinstance(target, ast.Name):
        return [target.id]
    if isinstance(target, ast.Attribute):
        return [target.attr]
    if isinstance(target, ast.Starred):
        return _target_names(target.value)
    if isinstance(target, (ast.Tuple, ast.List)):
        out: list[str] = []
        for elt in target.elts:
            out.extend(_target_names(elt))
        return out
    return []


def _arg_names(args: ast.arguments) -> list[str]:
    """函数参数名（参数也是**绑定** ⇒ 算「赋值目标名」）。"""
    out = [arg.arg for arg in [*args.posonlyargs, *args.args, *args.kwonlyargs]]
    if args.vararg is not None:
        out.append(args.vararg.arg)
    if args.kwarg is not None:
        out.append(args.kwarg.arg)
    return out


def _binding_names(node: ast.AST | None) -> list[str]:
    """这个节点**绑定**了哪些名字（赋值目标 / 函数名 / 参数名）。"""
    if node is None:
        return []
    names: list[str] = []
    if isinstance(node, ast.Assign):
        for target in node.targets:
            names.extend(_target_names(target))
    elif isinstance(node, ast.AnnAssign):
        names.extend(_target_names(node.target))
    elif isinstance(node, ast.AugAssign):
        names.extend(_target_names(node.target))
    elif isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
        names.append(node.name)
        names.extend(_arg_names(node.args))
    elif isinstance(node, ast.ClassDef):
        names.append(node.name)
    elif isinstance(node, ast.NamedExpr):
        names.extend(_target_names(node.target))
    elif isinstance(node, (ast.For, ast.AsyncFor)):
        names.extend(_target_names(node.target))
    elif isinstance(node, (ast.With, ast.AsyncWith)):
        for item in node.items:
            if item.optional_vars is not None:
                names.extend(_target_names(item.optional_vars))
    return names


def _has_turnover_marker(node: ast.AST) -> bool:
    """这个 AST 子树里有没有「换手」标志（标识符 / 属性 / 参数名 / 关键字 / 字符串）。"""
    for sub in ast.walk(node):
        if isinstance(sub, ast.Name) and _is_turnover_context_text(sub.id):
            return True
        if isinstance(sub, ast.Attribute) and _is_turnover_context_text(sub.attr):
            return True
        if isinstance(sub, ast.arg) and _is_turnover_context_text(sub.arg):
            return True
        if isinstance(sub, ast.keyword) and sub.arg and _is_turnover_context_text(sub.arg):
            return True
        if (
            isinstance(sub, ast.Constant)
            and isinstance(sub.value, str)
            and _is_turnover_context_text(sub.value)
        ):
            return True
    return False


def _parent_map(tree: ast.AST) -> dict[ast.AST, ast.AST]:
    """AST 的「子 → 父」映射（用来回溯某个表达式所在语句 / 函数）。"""
    parents: dict[ast.AST, ast.AST] = {}
    for node in ast.walk(tree):
        for child in ast.iter_child_nodes(node):
            parents[child] = node
    return parents


def _enclosing_stmt_and_func(
    parents: dict[ast.AST, ast.AST], node: ast.AST
) -> tuple[ast.stmt | None, ast.FunctionDef | ast.AsyncFunctionDef | None]:
    """回溯表达式所在的**最近语句**与**最近函数**（判「换手语境」的取样窗口）。"""
    stmt: ast.stmt | None = None
    func: ast.FunctionDef | ast.AsyncFunctionDef | None = None
    current = node
    while current in parents:
        current = parents[current]
        if stmt is None and isinstance(current, ast.stmt):
            stmt = current
        if func is None and isinstance(current, (ast.FunctionDef, ast.AsyncFunctionDef)):
            func = current
        if stmt is not None and func is not None:
            break
    return stmt, func


def _is_one_way_factor_definition(stmt: ast.stmt | None) -> bool:
    """这条语句是不是「单边换算系数常量」的**定义**（``TURNOVER_ONE_WAY_FACTOR = 0.5``）。"""
    if not isinstance(stmt, ast.Assign):
        return False
    for target in stmt.targets:
        name = None
        if isinstance(target, ast.Name):
            name = target.id
        elif isinstance(target, ast.Attribute):
            name = target.attr
        if name == _ONE_WAY_FACTOR_CONST:
            return True
    return False


def _is_half_literal(node: ast.AST) -> bool:
    """这个常量是不是**内联的** ``0.5`` 浮点字面量（常量名引用不算）。"""
    return (
        isinstance(node, ast.Constant)
        and isinstance(node.value, float)
        and abs(node.value - _ONE_WAY_FACTOR_LITERAL) < 1e-12
    )


def _handwritten_half_form(node: ast.AST) -> str | None:
    """这个表达式是不是「手写单边换算」⇒ 返回写法描述（``* 0.5`` / ``0.5 *`` / ``/ 2``）。

    🔴 为什么把 ``/ 2`` 也纳入：``turnover / 2`` 与 ``turnover * 0.5`` **完全等价**
       ⇒ 只抓乘法 ⇒ 等价写法从后门溜过去 ⇒ 口径照样会分叉。
    """
    if not isinstance(node, ast.BinOp):
        return None
    if isinstance(node.op, ast.Mult):
        if _is_half_literal(node.left):
            return "0.5 *"
        if _is_half_literal(node.right):
            return "* 0.5"
        # 🔴 2026-11-18 主 agent 补：``x * (1 / 2)`` 与 ``x * 0.5`` **完全等价**
        #    ⇒ 只认内联 ``0.5`` 字面量 ⇒ 等价写法从后门溜过去。
        for side, label in ((node.left, "* (1/2)"), (node.right, "(1/2) *")):
            if isinstance(side, ast.BinOp) and isinstance(side.op, ast.Div):
                num, den = side.left, side.right
                if (
                    isinstance(num, ast.Constant)
                    and num.value == 1
                    and isinstance(den, ast.Constant)
                    and isinstance(den.value, int)
                    and not isinstance(den.value, bool)
                    and den.value == 2
                ):
                    return label
        return None
    if isinstance(node.op, ast.Div):
        # 🔴 除数必须是 **int 2**（``2.0`` 不算 ⇒ 不误伤浮点写法；``True`` 也不算）
        right = node.right
        if (
            isinstance(right, ast.Constant)
            and isinstance(right.value, int)
            and not isinstance(right.value, bool)
            and right.value == 2
        ):
            return "/ 2"
    return None


def _handwritten_half_hits(tree: ast.AST, source: str = "") -> list[str]:
    """规则⑫ 命中行（**换手语境**里手写 ``* 0.5`` / ``0.5 *`` / ``/ 2``）。

    🔴 **换手语境**＝四路信号，任一命中即算（2026-11-18 收紧）：
       ① 「语句 / 参数名 / 字典键 / 字符串里出现 ``turnover`` 或「换手」」；
       ② 「函数名 / 函数 docstring 提到换手」；
       ③ 「**赋值目标名**（含函数名 / 参数名）里有**单边语义**」；
       ④ 「**模块文本**兜底：整个文件里出现过 ``turnover`` / 「换手」」。
    """
    parents = _parent_map(tree)
    module_turnover = _is_turnover_context_text(source)
    hits: list[str] = []
    for node in ast.walk(tree):
        form = _handwritten_half_form(node)
        if form is None:
            continue
        stmt, func = _enclosing_stmt_and_func(parents, node)
        if func is not None and func.name in _ONE_WAY_IMPL_FUNCS:
            continue  # 🔴 唯一实现本体 ⇒ 豁免
        if _is_one_way_factor_definition(stmt):
            continue  # 🔴 常量定义（``TURNOVER_ONE_WAY_FACTOR = 0.5``）⇒ 豁免
        context: list[str] = []
        if stmt is not None and _has_turnover_marker(stmt):
            context.append("语句")
        if func is not None:
            if _is_turnover_context_text(func.name):
                context.append("函数名")
            doc = ast.get_docstring(func)
            if doc and _is_turnover_context_text(doc):
                context.append("函数 docstring")
        # 🔴 信号③：赋值目标名 / 函数名 / 参数名里的**单边语义**
        if any(_has_one_way_semantic(name) for name in _binding_names(stmt)):
            context.append("单边语义（赋值目标）")
        if func is not None:
            func_names = [func.name, *_arg_names(func.args)]
            if any(_has_one_way_semantic(name) for name in func_names):
                context.append("单边语义（函数名）")
        # 🔴 信号④：**模块文本**兜底（整个文件里出现过 turnover / 换手）
        if module_turnover:
            context.append("模块文本")
        if not context:
            continue
        hits.append(f"line {node.lineno}: 手写 `{form}`（换手语境：{'/'.join(context)}）")
    return hits


def _scan_handwritten_half_turnover(source: str) -> list[str]:
    """🔴 规则⑫ 的**扫描入口**（对任意源码文本判定）⇒ 常驻变异反证用例可直接喂内存样例。"""
    return _handwritten_half_hits(ast.parse(source), source)


def test_单边换算不许手写_half() -> None:
    """🔴 单边换算必须走 ``config_v2.one_way_turnover()``（唯一实现）⇒ 不许手写 ``* 0.5``。

    🔴 判定口径：**换手语境**（四路信号，任一命中即算）
       里的 ``* 0.5`` / ``0.5 *`` / ``/ 2`` ⇒ 违规：
       ① 标识符 / 字典键 / 字符串里出现 ``turnover`` 或「换手」；
       ② 函数名 / 函数 docstring 提到换手；
       ③ **赋值目标名**（含函数名 / 参数名）里有**单边语义**
       （``one_way`` / ``one`` / ``half`` / ``convert`` … ⇒ **成词**匹配）；
       ④ **模块文本**兜底：整个文件里出现过 ``turnover`` / 「换手」；
       ⇒ **唯一实现内部**（``one_way_turnover`` 函数体）与
       **常量定义**（``TURNOVER_ONE_WAY_FACTOR = 0.5``）**不误报**；
       ⇒ 非换手语境的 ``* 0.5``（例如权重打折）**不误报**。
    """
    violations: list[str] = []
    for path in _scan_files():
        hits = _scan_handwritten_half_turnover(path.read_text(encoding="utf-8"))
        if hits:
            rel = path.relative_to(REPO_ROOT)
            violations.extend(f"{rel}: {h}" for h in hits)
    assert not violations, (
        "🔴 换手语境里手写了 `* 0.5`（⇒ 单边换算出现**第二份**实现 ⇒ 口径会分叉）：\n  "
        + "\n  ".join(violations)
        + "\n  ⇒ 一律改调 `config_v2.one_way_turnover()`（唯一实现）。"
    )


# =============================================================================
# 守卫自证：⑨⑪⑫ 的**常驻**变异反证（2026-11-18 补齐）
# =============================================================================
# 🔴 为什么必须是**常驻**用例：memory 里写「两条守卫都做了变异反证（改坏必转红）」，
#    但仓库里原先只有 ①②③ 的反证（``test_守卫本身有牙_*``）⇒ 那 ⑨⑪ 是一次性手工验证，
#    读起来却像「守卫自带反证」⇒ 本节把 ⑨⑪⑫ 的反证**固化成常驻用例**。
# 🔴 手法：内存里的源码样例 ⇒ 直接喂「**扫一段源码文本**」的入口
#    ⇒ **不必落盘**改仓库，也不会污染生产代码。

# ----------------------------------------------------------------- ⑨ 变异反证
#: 🔴 阳性反证样例：``turnover_ok`` 出现在**条件 / 过滤**位置 ⇒ 规则⑨ 必须抓到。
#:    （三种历史漏网写法 + 几种同族写法各一条）
_BAD_TURNOVER_FILTER_SAMPLES = {
    "列表推导 ListComp": (
        'cands = [r for r in rows if r["turnover_ok"]]\n'
    ),
    "字典推导 DictComp": (
        'cands = {r["name"]: r for r in rows if r["turnover_ok"]}\n'
    ),
    "for + if": (
        "kept = []\n"
        "for r in rows:\n"
        "    if r[\"turnover_ok\"]:\n"
        "        kept.append(r)\n"
    ),
    "集合推导 SetComp": (
        'cands = {r["name"] for r in rows if r["turnover_ok"]}\n'
    ),
    "生成器 GeneratorExp": (
        'cands = (r for r in rows if r["turnover_ok"])\n'
    ),
    "if 表达式 IfExp": (
        'best = r if r["turnover_ok"] else None\n'
    ),
    "filter 谓词": (
        'cands = filter(lambda r: r["turnover_ok"], rows)\n'
    ),
    "sorted 排序键": (
        'cands = sorted(rows, key=lambda r: not r["turnover_ok"])\n'
    ),
}

#: 🔴 阴性反证样例：``turnover_ok`` 只作**披露标记**（落 dict / CSV / md）⇒ 不许被抓。
_GOOD_TURNOVER_DISCLOSURE_SAMPLES = {
    "落 dict": (
        'row = {"turnover_ok": bool(turn <= 4.0), "score": 0.3}\n'
    ),
    "落 CSV 表头": (
        'fields = ["variant", "turnover_ok", "score"]\n'
    ),
    "落 md 表格": (
        'text = "| 变体 | 年化换手（双边） |\\n|---|---|\\n| A | 14.10× |\\n"\n'
    ),
    "取回披露值": (
        'flag = row["turnover_ok"]\n'
        'if row["score"] > 0:\n'
        '    row["turnover_ok"] = True\n'
    ),
}


def test_规则9_换手标记参与过滤必须被抓到() -> None:
    """🔴 规则⑨ 阳性反证：``turnover_ok`` 一旦出现在**条件 / 过滤**位置 ⇒ 必被抓到。"""
    for label, sample in _BAD_TURNOVER_FILTER_SAMPLES.items():
        hits = _scan_turnover_ok_filters(sample)
        assert hits, (
            f"🔴 规则⑨ 漏抓「{label}」这种用 `turnover_ok` 做过滤的写法 ⇒ 守卫没牙\n"
            f"样例源码：\n{sample}"
        )


def test_规则9_换手标记只作披露必须放过() -> None:
    """🔴 规则⑨ 阴性反证：``turnover_ok`` 只是**披露标记**（落 dict / CSV / md）⇒ 不许误报。"""
    for label, sample in _GOOD_TURNOVER_DISCLOSURE_SAMPLES.items():
        hits = _scan_turnover_ok_filters(sample)
        assert not hits, (
            f"🔴 规则⑨ 误报了「{label}」这种纯披露写法（换手只作披露）⇒ 守卫误报\n"
            f"命中：{hits}\n样例源码：\n{sample}"
        )


# ----------------------------------------------------------------- ⑪ 变异反证
#: 🔴 阳性反证样例：表格里的「换手」单元格**不带口径标签** ⇒ 规则⑪ 必须抓到。
_BAD_TABLE_CELL_SAMPLES = {
    "markdown 表头行": (
        'text = "| 变体 | 年化换手 | 超额 |\\n|---|---|---|\\n"\n'
    ),
    "md_table 列表行": (
        'row = ["年化换手", "超额"]\n'
    ),
    "裸列名「换手」": (
        'text = "| 变体 | 换手 | 超额 |\\n|---|---|---|\\n"\n'
    ),
}

#: 🔴 阴性反证样例：正文散文里的「换手」**没有**口径标签，但**不是表格** ⇒ 不许被抓。
_GOOD_PROSE_SAMPLES = {
    "正文散文": (
        'def narrative():\n'
        '    return "年化换手 14.10×"\n'
    ),
    "散文里的换手口径讨论": (
        'def narrative():\n'
        '    return "换手口径：单边 = 0.5 × 双边"\n'
    ),
}


def test_规则11_表格换手单元格未标口径必须被抓到() -> None:
    """🔴 规则⑪ 阳性反证：表格里的「换手」单元格不带单/双边标签 ⇒ 必被抓到。"""
    for label, sample in _BAD_TABLE_CELL_SAMPLES.items():
        hits = _scan_table_cells_missing_label(sample)
        assert hits, (
            f"🔴 规则⑪ 漏抓「{label}」这种没有口径标签的换手表头 ⇒ 守卫没牙\n"
            f"样例源码：\n{sample}"
        )


def test_规则11_正文散文里的换手不许被抓到() -> None:
    """🔴 规则⑪ 阴性反证：正文散文里的「年化换手 14.10×」**不是表头** ⇒ 不许误报。"""
    for label, sample in _GOOD_PROSE_SAMPLES.items():
        hits = _scan_table_cells_missing_label(sample)
        assert not hits, (
            f"🔴 规则⑪ 误报了「{label}」这种正文散文（表头才是就地提示点）⇒ 守卫误报\n"
            f"命中：{hits}\n样例源码：\n{sample}"
        )


# ----------------------------------------------------------------- ⑫ 变异反证
#: 🔴 阳性反证样例：**换手语境**里手写 ``* 0.5`` ⇒ 规则⑫ 必须抓到。
_BAD_HALF_SAMPLES = {
    "标识符换手语境": (
        "def convert(turnover):\n"
        "    return turnover * 0.5\n"
    ),
    "字典键换手语境": (
        "def render(cost_assumption):\n"
        "    return cost_assumption[\"annualized_turnover\"] * 0.5\n"
    ),
    "反向写法 0.5 *": (
        "def render(cost_assumption):\n"
        "    return 0.5 * cost_assumption[\"annualized_turnover\"]\n"
    ),
    "函数 docstring 换手语境": (
        "def _one_way(x):\n"
        '    """双边换手 → 单边换手。"""\n'
        "    return 0.5 * float(x)\n"
    ),
    # 🔴 2026-11-18 补漏：下面三种写法**一个 ``turnover`` 字都没有**
    #    ⇒ 光靠「标识符含 turnover」抓不到 ⇒ 靠「单边语义」信号抓。
    "函数名单边语义（_one_way）": (
        "def _one_way(x):\n"
        "    return x * 0.5\n"
    ),
    "函数名换算语义（convert）": (
        "def convert(t):\n"
        "    return t * 0.5\n"
    ),
    "赋值目标名单边语义（one）": (
        "one = two * 0.5\n"
    ),
    # 🔴 2026-11-18 主 agent 补：reviewer 点名的 ``def one_side(x)`` 逃逸口
    #    （之前词表只有 ``single_side`` ⇒ ``one_side`` 溜得过去）。
    "函数名单边语义（one_side）": (
        "def one_side(x):\n"
        "    return x * 0.5\n"
    ),
    # 🔴 2026-11-18 主 agent 补：``* (1 / 2)`` 与 ``* 0.5`` **完全等价**
    #    ⇒ 只认内联 0.5 字面量 ⇒ 等价写法从后门溜过去。
    "等价写法 * (1/2)（换手语境）": (
        "turnover = two * (1 / 2)\n"
    ),
    "等价写法 / 2（换手语境）": (
        "turnover = two / 2\n"
    ),
    # 🔴 等价写法 ``turnover / 2``（和 ``* 0.5`` 完全等价）⇒ 同样必须抓到。
    "除法写法（turnover / 2）": (
        "def to_one_way(turnover):\n"
        "    return turnover / 2\n"
    ),
    # 🔴 **模块文本**兜底：整个文件里提到「换手」⇒ 视作换手语境。
    "模块文本兜底（文件里提到换手）": (
        "# 这个模块在算年化换手\n"
        "def scale(value):\n"
        "    return value * 0.5\n"
    ),
}

#: 🔴 阴性反证样例：唯一实现内部 / 常量定义 / 非换手语境 ⇒ 不许被抓。
_GOOD_HALF_SAMPLES = {
    "唯一实现本体": (
        "TURNOVER_ONE_WAY_FACTOR = 0.5\n"
        "def one_way_turnover(turnover):\n"
        "    return float(turnover) * 0.5\n"
    ),
    "常量引用": (
        "TURNOVER_ONE_WAY_FACTOR = 0.5\n"
        "def one_way_turnover(turnover):\n"
        "    return float(TURNOVER_ONE_WAY_FACTOR * turnover)\n"
    ),
    "常量定义（写成 1 * 0.5）": (
        "TURNOVER_ONE_WAY_FACTOR = 1 * 0.5\n"
    ),
    "非换手语境": (
        "def half_weight(weight):\n"
        "    return weight * 0.5\n"
    ),
    # 🔴 2026-11-18 新增：普通的（非换手语境）``x * 0.5`` ⇒ 不许误报。
    "普通缩放（非换手语境）": (
        "def scale(value):\n"
        "    return value * 0.5\n"
    ),
}


def test_规则12_换手语境手写_half必须被抓到() -> None:
    """🔴 规则⑫ 阳性反证：换手语境里手写 ``* 0.5`` ⇒ 必被抓到。"""
    for label, sample in _BAD_HALF_SAMPLES.items():
        hits = _scan_handwritten_half_turnover(sample)
        assert hits, (
            f"🔴 规则⑫ 漏抓「{label}」这种手写 `* 0.5` 的单边换算 ⇒ 守卫没牙\n"
            f"样例源码：\n{sample}"
        )


def test_规则12_唯一实现与常量必须放过() -> None:
    """🔴 规则⑫ 阴性反证：唯一实现内部 / 常量定义 / 非换手语境 ⇒ 不许误报。"""
    for label, sample in _GOOD_HALF_SAMPLES.items():
        hits = _scan_handwritten_half_turnover(sample)
        assert not hits, (
            f"🔴 规则⑫ 误报了「{label}」（唯一实现 / 常量定义 / 非换手语境）⇒ 守卫误报\n"
            f"命中：{hits}\n样例源码：\n{sample}"
        )
