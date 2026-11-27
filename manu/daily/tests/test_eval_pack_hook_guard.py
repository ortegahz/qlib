"""🔴 守卫：**每日链必须把「评估就绪包」接进跑批**（hook 不许被静默拆掉/挪位/改成硬失败）。

【为什么必须有这条守卫（2026-11-27 审计发现）】
  「事后系统有效性评估」的四维度输入，靠的是**每个快照目录里那 6 件机读件**
  （``eval_candidate_pool.csv`` / ``eval_ta_shadow_ledger.csv`` / ``live_fills.csv``
  / ``cost_assumption.json`` / ``nav_history_snapshot.csv`` / ``eval_pack_manifest.json``）。
  它们由 ``manu/etf_rotation/daily/eval_pack.py::write_eval_pack`` 落盘，
  而**唯一的调用点是** ``daily/main.py`` 里那段 hook
  （``import eval_pack as EVAL_PACK`` → ``EVAL_PACK.write_eval_pack(snap.path, ...)``）。

  ⇒ 那段 hook **一旦被人删掉/挪到冻结之后**：跑批**照样全绿**（不报错、不失败），
    但**从那一天起所有新快照都不再带评估料** ⇒ 评估线**静默断链**
    ⇒ 等几个月后才发现「怎么没数据可算」。
  ⇒ 本守卫把四件事**钉死**：① hook 真的被调用 ② 在**同一函数内**的 ``snap.finalize()``
    **之前** ③ 被 ``try/except`` 包住（fail-soft） ④ 失败状态**不是** ``failed``
    （``failed`` 会污染每日台账的「失败/降级环节」栏）。

【为什么用 AST 而不是字符串窗口】
  实测：字符串窗口法会被**邻近代码**骗过（别处的 ``except``、别处的 ``"absent"``、
  另一个代码路径里的 ``snap.finalize()``）⇒ 变异反证**抓不住**「挪位/去兜底/改状态」。
  ⇒ 本文件按 **AST** 判定：hook 调用点 → 最近的 ``ast.Try`` 祖先 → 同一函数内的
    ``snap.finalize()`` 调用 ⇒ 全部按**结构**取证，不靠文本巧合。

【为什么本文件自带绝对路径定位】
  镜像纪律要求本文件**成对存在**：活链 ``manu/etf_rotation/daily/tests/`` ＋
  镜像 ``manu/daily/tests/``（**已登记的过期副本**）⇒ 裸 ``import main`` 在镜像侧
  会 ImportError ⇒ 本文件按**绝对路径**读**活链**的 ``main.py`` **源码**做静态判定，
  两处行为一致。
"""

from __future__ import annotations

import ast
from pathlib import Path


def _repo_root() -> Path:
    """从本文件**向上**找仓库根（含 ``manu/etf_rotation/daily/main.py`` 的那层）。

    🔴 本文件有**两个镜像位置** ⇒ **不许**用固定的 ``parents[N]`` 定位。
    """
    for parent in Path(__file__).resolve().parents:
        if (parent / "manu" / "etf_rotation" / "daily" / "main.py").is_file():
            return parent
    raise RuntimeError("定位仓库根失败：找不到 manu/etf_rotation/daily/main.py")


REPO_ROOT = _repo_root()
MAIN_PY = REPO_ROOT / "manu" / "etf_rotation" / "daily" / "main.py"

#: hook 的方法名（``EVAL_PACK.write_eval_pack(...)``）
METHOD = "write_eval_pack"
#: 快照冻结方法名（``snap.finalize()``）
FINALIZE = "finalize"


# ----------------------------------------------------------------- AST 工具
def _parse_main(source: str) -> ast.Module:
    return ast.parse(source)


def _attr_chain(node: ast.AST) -> list[str]:
    """``a.b.c`` ⇒ ``["a","b","c"]``；不是属性链 ⇒ ``[]``。"""
    parts: list[str] = []
    cur = node
    while isinstance(cur, ast.Attribute):
        parts.append(cur.attr)
        cur = cur.value
    if isinstance(cur, ast.Name):
        parts.append(cur.id)
    else:
        return []
    return list(reversed(parts))


def _parents(tree: ast.AST) -> dict[ast.AST, ast.AST]:
    out: dict[ast.AST, ast.AST] = {}
    for node in ast.walk(tree):
        for child in ast.iter_child_nodes(node):
            out[child] = node
    return out


def _hook_calls(tree: ast.Module) -> list[ast.Call]:
    """全部 ``*.write_eval_pack(...)`` 调用点。"""
    out: list[ast.Call] = []
    for node in ast.walk(tree):
        if isinstance(node, ast.Call) and isinstance(node.func, ast.Attribute) \
                and node.func.attr == METHOD:
            out.append(node)
    return out


def _enclosing(tree: ast.Module, node: ast.AST) -> ast.AST | None:
    """向上找最近的 ``ast.FunctionDef`` / ``ast.AsyncFunctionDef`` 祖先。"""
    parents = _parents(tree)
    cur = parents.get(node)
    while cur is not None:
        if isinstance(cur, (ast.FunctionDef, ast.AsyncFunctionDef)):
            return cur
        cur = parents.get(cur)
    return None


def _nearest_try(tree: ast.Module, node: ast.AST) -> ast.Try | None:
    """向上找最近的 ``ast.Try`` 祖先。"""
    parents = _parents(tree)
    cur = parents.get(node)
    while cur is not None:
        if isinstance(cur, ast.Try):
            return cur
        cur = parents.get(cur)
    return None


def _finalize_calls_in(func: ast.AST) -> list[ast.Call]:
    """某函数体内全部 ``snap.finalize()`` 调用（链尾是 ``finalize``）。"""
    out: list[ast.Call] = []
    for node in ast.walk(func):
        if isinstance(node, ast.Call) and isinstance(node.func, ast.Attribute) \
                and node.func.attr == FINALIZE:
            out.append(node)
    return out


def _main_source() -> str:
    return MAIN_PY.read_text(encoding="utf-8")


# =============================================================================
# ① hook 必须真的被调用
# =============================================================================
def test_每日链必须调用评估就绪包() -> None:
    """hook 调用点必须存在于 ``daily/main.py``（不许只剩 import）。"""
    tree = _parse_main(_main_source())
    calls = _hook_calls(tree)
    assert calls, (
        "🔴 `daily/main.py` 里没有 `*.write_eval_pack(...)` 调用"
        " ⇒ **每日链不再产出评估就绪包** ⇒ 快照静默不再带评估料 ⇒ 评估线断链"
    )


# =============================================================================
# ② hook 必须在（同一函数内的）快照冻结之前
# =============================================================================
def test_hook必须在快照冻结之前() -> None:
    """hook 与 ``snap.finalize()`` 必须落在**同一个函数**里，且 finalize 在后。

    🔴 为什么不按全文找 finalize：``main.py`` 里**另一个**代码路径（早期返回分支）
       也有 ``snap.finalize()``（行号更小）⇒ 全文比对会把「挪到冻结之后」这种违规放过。
    """
    tree = _parse_main(_main_source())
    calls = _hook_calls(tree)
    assert calls, "🔴 找不到 hook 调用点"
    hook = calls[0]
    func = _enclosing(tree, hook)
    assert func is not None, "🔴 hook 调用点不在任何函数内（结构异常）"
    finalizes = _finalize_calls_in(func)
    assert finalizes, (
        "🔴 hook 所在函数里**没有** `snap.finalize()` ⇒ 无法判定冻结顺序"
    )
    hook_line = getattr(hook, "lineno", 0)
    later = [f for f in finalizes if getattr(f, "lineno", 0) > hook_line]
    assert later, (
        "🔴 评估就绪包必须在本函数 `snap.finalize()` **之前**落盘"
        "（否则它不进 manifest 的 artifacts[] ⇒ 快照不自包含）"
    )


# =============================================================================
# ③ hook 必须 fail-soft（被 try/except 包住）
# =============================================================================
def test_hook必须fail_soft不阻断流水线() -> None:
    """hook 必须落在 ``try`` 块内，且该 ``try`` **有 except 兜底**。"""
    tree = _parse_main(_main_source())
    calls = _hook_calls(tree)
    assert calls, "🔴 找不到 hook 调用点"
    try_node = _nearest_try(tree, calls[0])
    assert try_node is not None, (
        "🔴 hook 必须在 `try:` 块内（**fail-soft**：它不影响任何交易决策）"
    )
    assert try_node.handlers, (
        "🔴 hook 的 `try:` **没有 except 兜底** ⇒ 过程件失败会**阻断整条跑批**"
    )


# =============================================================================
# ④ 失败状态不许用 failed（会污染每日台账失败栏）
# =============================================================================
def test_hook失败状态不许用failed() -> None:
    """``eval_pack`` 的 stage 状态必须是 ``ok`` / ``absent``，**不许** ``failed``。"""
    tree = _parse_main(_main_source())
    calls = _hook_calls(tree)
    assert calls, "🔴 找不到 hook 调用点"
    try_node = _nearest_try(tree, calls[0])
    assert try_node is not None and try_node.handlers, "🔴 找不到 hook 的 except 分支"
    statuses: list[str] = []
    for handler in try_node.handlers:
        for node in ast.walk(handler):
            if not isinstance(node, ast.Call):
                continue
            if not isinstance(node.func, ast.Attribute) or node.func.attr != "set_stage":
                continue
            args = list(node.args)
            if not args:
                continue
            first = args[0]
            if isinstance(first, ast.Constant) and first.value == "eval_pack":
                for arg in args[1:2]:
                    if isinstance(arg, ast.Constant) and isinstance(arg.value, str):
                        statuses.append(arg.value)
    assert statuses, "🔴 找不到 `set_stage(\"eval_pack\", ...)` 的状态登记"
    assert all(s != "failed" for s in statuses), (
        f"🔴 eval_pack 失败分支必须登记 `absent`（不许 `failed`）"
        f"⇒ 否则会把研究侧过程件缺失记成生产失败（污染每日台账）：实测 {statuses}"
    )
