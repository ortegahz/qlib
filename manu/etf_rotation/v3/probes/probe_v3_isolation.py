#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""V3 隔离探针 · **V3 绝不许改 V2 / 不许 import V1 冻结件 / 绝不开行业中性化**。

【每类断言钉死了什么】
A 类 · V2 冻结件 md5 逐文件比对（**fail-closed**）
  A1 清单文件可读且非空（读不到 ⇒ **FAIL**，不许「跳过」）
  A2 清单里每个文件的 md5 与**当前磁盘**一致（V2 是**现役**口径，V3 改了就是事故）

B 类 · V3 源码 AST 扫描（**不许** import 冻结件）
  B1 不许 import V1 冻结件（``config`` / ``backtest`` / ``qlib_scores`` / ``engine.*``）
  B2 不许 import V2 的可执行模块（``config_v2`` / ``etf_layer`` / ``backtest_v2`` / …）
  B3 不许出现指向 V2 目录的**路径字面量**（按路径 import 同样是违规）

C 类 · 口径常量红线
  C1 ``INDUSTRY_NEUTRALIZATION_ENABLED`` 恒为 ``False``（开了会让 ``E = W × S`` 塌缩）
  C2 损失函数口径：实验臂 ``lambdarank`` / 对照臂 ``mse``
  C3 🔴 **学习期处理器不含 CSRankNorm**（做了就把数值超额抹成排名 ⇒ 排序目标失真）

退出码：``0`` = 全过；``1`` = 有 FAIL。
"""
from __future__ import annotations

import ast
import hashlib
import json
import sys
from pathlib import Path

_V3_DIR = Path(__file__).resolve().parents[1]
if str(_V3_DIR) not in sys.path:
    sys.path.insert(0, str(_V3_DIR))

import config_v3 as C  # noqa: E402

PASSED: list[str] = []
FAILED: list[str] = []

#: 🔴 **V1 冻结件**的模块名片段（V1 已被 v2/probes/v1_frozen_md5.json 锁死）
V1_BANNED_COMPONENTS = ("config", "backtest", "qlib_scores", "engine")
#: 🔴 **V2 的可执行模块**名（V2 = 现役口径；V3 只许**只读参考**，不许 import）
V2_BANNED_MODULES = (
    "config_v2", "etf_layer", "backtest_v2", "metrics", "run_v2", "hyperopt",
    "evaluate", "report", "smoothing", "constituents", "liquidity", "attribution",
    "holdings_archive", "fetch_holdings_archive",
)
#: 指向 V2 目录的路径字面量（🔴 动态拼接 ⇒ 本文件自身不会误命中）
V2_PATH_MARK = "etf_rotation" + "/" + "v2"


def check(name: str, ok: bool, detail: str = "") -> None:
    """记录并打印一条判定（``[PASS]`` / ``[FAIL]``）。"""
    line = f"[PASS] {name}" + (f"｜{detail}" if detail else "")
    if ok:
        PASSED.append(line)
    else:
        line = f"[FAIL] {name}" + (f"｜{detail}" if detail else "")
        FAILED.append(line)
    print(line, flush=True)


def project_root() -> Path:
    """项目根（= 含 ``manu/etf_rotation/v2/config_v2.py`` 的那一层）。"""
    for candidate in [_V3_DIR, *_V3_DIR.parents]:
        if (candidate / "manu" / "etf_rotation" / "v2" / "config_v2.py").is_file():
            return candidate
    raise FileNotFoundError("🔴 找不到项目根（含 manu/etf_rotation/v2/config_v2.py 的那一层）")


def frozen_manifest_path() -> Path:
    """V2 冻结件 md5 清单的落点（🔴 **V3 自己目录**下的那一份 ⇒ 随 V3 目录一起 rsync）。

    🔴 **fail-closed**：清单读不到 ⇒ 判据失效 ⇒ 直接 FAIL（不许回落到别的目录蒙混过关）。
    """
    return _V3_DIR / "probes" / "v2_frozen_md5.json"


def md5_of(path: Path) -> str:
    """算文件 md5。"""
    h = hashlib.md5()
    with path.open("rb") as fh:
        for chunk in iter(lambda: fh.read(1 << 20), b""):
            h.update(chunk)
    return h.hexdigest()


def v3_sources() -> list[Path]:
    """V3 包下的全部 ``.py``（含 ``probes/`` 与 ``tests/``）。"""
    return sorted(_V3_DIR.rglob("*.py"))


# ----------------------------------------------------------------- A 类
def probe_v2_frozen_md5() -> None:
    """逐文件比对 V2 冻结件 md5（🔴 fail-closed：清单读不到 ⇒ FAIL）。"""
    path = frozen_manifest_path()
    try:
        manifest = json.loads(path.read_text(encoding="utf-8"))
    except Exception as exc:  # noqa: BLE001 —— 清单读不到 = 判据失效 = FAIL
        check("A1 V2 冻结件清单可读", False, f"读取失败：{exc!r}（{path}）")
        return
    files = manifest.get("files")
    if not isinstance(files, dict) or not files:
        check("A1 V2 冻结件清单可读", False, f"清单里没有 files（{path}）")
        return
    check("A1 V2 冻结件清单可读", True, f"{len(files)} 个文件")
    root = project_root()
    for rel, expected in files.items():
        target = root / rel
        if not target.is_file():
            check(f"A2 {rel}", False, "文件缺失（V2 冻结件不许删除）")
            continue
        actual = md5_of(target)
        check(f"A2 {rel}", actual == expected,
              f"md5={actual}" if actual == expected else f"期望 {expected}，实测 {actual}")


# ----------------------------------------------------------------- B 类
def _imported_names(tree: ast.AST) -> list[str]:
    """抽出所有 ``import`` 的模块名（``from X import a`` 取 ``X`` 与 ``a``）。"""
    out: list[str] = []
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            out.extend(alias.name for alias in node.names)
        elif isinstance(node, ast.ImportFrom):
            if node.module:
                out.append(node.module)
            out.extend(alias.name for alias in node.names)
    return out


def _docstring_nodes(tree: ast.AST) -> set[int]:
    """找出所有**文档字符串**节点（模块 / 函数 / 类的 docstring）。"""
    out: set[int] = set()
    for node in ast.walk(tree):
        if isinstance(node, (ast.Module, ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)):
            body = getattr(node, "body", None)
            if body and isinstance(body[0], ast.Expr) and isinstance(body[0].value, ast.Constant) \
                    and isinstance(body[0].value.value, str):
                out.add(id(body[0].value))
    return out


def _code_string_literals(tree: ast.AST) -> list[str]:
    """**代码里**的字符串字面量（🔴 排除 docstring —— 文档里提到 V2 路径不算违规）。"""
    docs = _docstring_nodes(tree)
    return [
        node.value
        for node in ast.walk(tree)
        if isinstance(node, ast.Constant) and isinstance(node.value, str) and id(node) not in docs
    ]


def probe_no_frozen_imports() -> None:
    """AST 扫 V3 源码 ⇒ 不许 import V1 冻结件 / V2 可执行模块。"""
    hits_v1: list[str] = []
    hits_v2: list[str] = []
    for path in v3_sources():
        try:
            tree = ast.parse(path.read_text(encoding="utf-8"))
        except SyntaxError as exc:  # pragma: no cover
            hits_v1.append(f"{path.name}: 语法错误 {exc}")
            continue
        for name in _imported_names(tree):
            parts = name.split(".")
            if any(p in V1_BANNED_COMPONENTS for p in parts):
                hits_v1.append(f"{path.name}: {name}")
            if "v2" in parts or name in V2_BANNED_MODULES:
                hits_v2.append(f"{path.name}: {name}")
    check("B1 V3 源码不 import V1 冻结件", not hits_v1,
          "无" if not hits_v1 else "；".join(hits_v1))
    check("B2 V3 源码不 import V2 可执行模块", not hits_v2,
          "无" if not hits_v2 else "；".join(hits_v2))

    marks: list[str] = []
    for path in v3_sources():
        if path.resolve() == Path(__file__).resolve():
            continue  # 🔴 探针自身含清单路径 ⇒ 自身豁免
        try:
            tree = ast.parse(path.read_text(encoding="utf-8"))
        except SyntaxError:  # pragma: no cover
            continue
        for literal in _code_string_literals(tree):
            if V2_PATH_MARK in literal:
                marks.append(f"{path.name}: {literal[:60]}")
    check("B3 V3 源码不含指向 V2 目录的路径字面量（docstring 除外）", not marks,
          "无" if not marks else "；".join(marks))


# ----------------------------------------------------------------- C 类
def probe_config_redlines() -> None:
    """口径常量红线（🔴 任何一条被改 ⇒ V3 的对齐实验立刻失去意义）。"""
    check("C1 行业中性化恒为 False", C.INDUSTRY_NEUTRALIZATION_ENABLED is False,
          f"INDUSTRY_NEUTRALIZATION_ENABLED={C.INDUSTRY_NEUTRALIZATION_ENABLED}")
    check("C2 损失函数口径", str(C.MODEL_OBJECTIVE) == "lambdarank"
          and str(C.CONTROL_OBJECTIVE) == "mse",
          f"实验臂={C.MODEL_OBJECTIVE} / 对照臂={C.CONTROL_OBJECTIVE}")
    classes = [str(p.get("class", "")) for p in C.LABEL_LEARN_PROCESSORS]
    check("C3 学习期处理器不含 CSRankNorm", "CSRankNorm" not in classes,
          f"学习期链={classes}（做了 CSRankNorm 就会把数值超额抹成排名）")


def main() -> int:
    """探针入口（退出码 0 = 全过）。"""
    print("=== probe_v3_isolation · V3 隔离（V2 冻结件 / V1 冻结 import / 口径红线）===")
    probe_v2_frozen_md5()
    probe_no_frozen_imports()
    probe_config_redlines()
    print()
    print(f"合计 {len(PASSED) + len(FAILED)} 项：PASS {len(PASSED)} / FAIL {len(FAILED)}")
    if FAILED:
        print()
        for line in FAILED:
            print(line)
    return 1 if FAILED else 0


if __name__ == "__main__":
    raise SystemExit(main())
