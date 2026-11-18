#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""**快照契约**闸门探针（S1~S10）—— 钉死「单一无状态流水线 ＋ 不可变快照目录」的不变式。

【为什么要有这个探针】
2026-11-09 架构瘦身把「按模式分诊 ＋ 影子推演」换成「**每次运行 = 一个快照目录**」。
旧形态把产物写进**根目录全局共享文件**（canonical ``stock_scores.csv`` /
``tradingagents_decision.*`` / ``rebalance_recommendation.json``）⇒ 跨链路污染、无法 diff、
「不写全局文件」只能靠 if 分支守。新形态的不变式（契约 §0）：

* **I1** 每次运行落一个新快照目录 ⇒ 同一秒两次也**绝不覆盖**；旧快照**逐字节不变**；
* **I2** 每日链**不写任何根目录全局文件** ⇒ canonical 只作一次性只读种子；
* **I3** 副作用只由 ``--live-trade`` 决定 ⇒ 缺省 = 观察态（执行卡**挂单禁用**、不发信）；
* **I4** **绝不静默降级** ⇒ 跳过的环节必须在 manifest 的 ``skipped`` 里登记；
* **I5** 数字纪律（缺失渲染「—」，不反推、不编造）。

【每条断言钉死了什么】
S1  每次 ``create_snapshot`` 产生**新**目录；同一秒两次 ⇒ 后者目录名加后缀、**绝不覆盖**；
    旧快照里的件**逐字节不变**（含 mtime）
S2  快照**自包含**：manifest 存在 ＋ ``artifacts`` 覆盖契约 §4 的件名清单（且无外链）
S3  ``resolve_scores_base`` 三种情形：有上一份快照 ⇒ 返回快照内 ``stock_scores.csv``；
    无快照但有 canonical ⇒ 返回 canonical；两者都无 ⇒ ``(None, 说明含「无」)``
S4  **不写根目录全局文件**：建快照 + 写件前后，``root_global_fingerprint`` 的
    size / sha1 / mtime **三项全等**
S5  manifest **完整披露**：``stages`` / ``skipped`` / ``notes`` / ``live_trade`` / ``artifacts``
    字段齐全；``set_skipped`` 登记的项出现在 ``skipped`` 里
S6  ``--mode`` **已废除** ⇒ 退出码 **2** ＋ stderr 迁移提示（指向 ``--full`` / ``--live-trade`` / 快照制）
S7  ``--live-trade`` 与 ``--dry-run`` **互斥** ⇒ 退出码 **2**
S8  ``resolve_stage_flags``：``--full`` 强制四阶段全开；被覆盖的 skip 登记进 ``skipped``
S9  执行卡：``live_trade=False`` ⇒ 卡片带**挂单禁用**标记；``live_trade=True`` ⇒ **不带**
S10 **变异反例组**：S1~S9 每条正向断言都配一条「把行为改坏 ⇒ 必须转红」的变异体
    （**运行时替换** / **内存内 AST 改写** ⇒ **不落盘改生产代码**）

【用法】
    ~/anaconda3/envs/uav/bin/python manu/etf_rotation/probes/probe_snapshot_contract.py
    （退出码 0 = 全绿，1 = 有 FAIL）

【副作用声明】
纯内存 ＋ ``tempfile`` 夹具：**不联网、不读唯一落点根、不写任何生产目录**。
CLI 子进程跑在**临时目录**里（产物根 / 快照根 / 报告目录 / 锁文件全部重定向到临时目录），
SMTP 凭据一律指向不存在的路径 ⇒ **绝不可能发信**；且子进程只带「跳过取数 / 跳过 LLM /
不重算 S 层 / 不换库」四个开关 ⇒ **即使被测实现坏了也不可能起重活**。
"""
from __future__ import annotations

import ast
import contextlib
import importlib.util
import io
import json
import os
import subprocess
import sys
import tempfile
from datetime import datetime
from pathlib import Path
from typing import Any, Callable, Iterator

#: 仓库根（manu/etf_rotation/probes → 上溯三级）
REPO_ROOT = Path(__file__).resolve().parents[3]
#: 研究层根（manu/etf_rotation）
MODULE_DIR = Path(__file__).resolve().parents[1]
#: 每日链目录（manu/etf_rotation/daily）
DAILY_DIR = MODULE_DIR / "daily"
#: 被测源码
MAIN_PY = DAILY_DIR / "main.py"
AGENTS_PY = DAILY_DIR / "agents_rebalance.py"

if str(DAILY_DIR) not in sys.path:
    sys.path.insert(0, str(DAILY_DIR))

import snapshot as SNAP  # noqa: E402

#: 变异前抓住原始函数对象（还原 / 变异体内部调用都用它）
_ORIGINAL_WRITE_TEXT: Callable[..., Path] = SNAP.Snapshot.write_text

#: 夹具用的固定业务日（⇒ 输出逐字可复现）
FIXED_ASOF = "2026-11-09"
FIXED_STAMP = "20261109"
FIXED_NOW = datetime(2026, 11, 9, 12, 0, 0)

#: 契约 §4 的快照件名清单（``{stamp}`` = 业务日 ``YYYYMMDD``）
CONTRACT_ARTIFACTS: tuple[str, ...] = (
    "stock_scores.csv",
    "stock_prices.csv",
    "etf_analysis_report.json",
    "etf_analysis_report.md",
    "rebalance_recommendation.json",
    "rebalance_recommendation.csv",
    "ta_research_context.md",
    "ta_context_meta.json",
    "tradingagents_decision.md",
    "tradingagents_decision.json",
    "final_rebalance_plan.json",
    "final_rebalance_plan.md",
    "node_reprice_{stamp}.json",
    "execution_card_{stamp}.json",
    "report_{stamp}.md",
    "heartbeat_{stamp}.json",
)

#: 迁移提示的判定词（契约 §5 S6：提示里要有 ``--full`` 或 ``--live-trade`` 或「快照」）
HINT_TOKENS: tuple[str, ...] = ("--full", "--live-trade", "快照")

PASS: list[str] = []
FAIL: list[str] = []

#: 最近一次 CLI 子进程的实测细节（退出码等）⇒ 只给断言失败时看
LAST_CLI: dict[str, Any] = {}
#: 被测代码抛异常时的现场（⇒ 探针**报 FAIL** 而不是自己崩掉）
LAST_ERROR: dict[str, str] = {}


# ---------------------------------------------------------------- 断言记账


def check(num: str, desc: str, ok: bool, reason: str = "") -> None:
    """打一条断言结果。

    🔴 必须带 ``[PASS]`` / ``[FAIL]`` 字面量（``run_gates.sh`` 靠 ``grep -c '\\[PASS\\]'`` 计数）。
    """
    line = f"{num} {desc}"
    if ok:
        PASS.append(line)
        print(f"  [PASS] {line}")
    else:
        FAIL.append(line)
        print(f"  [FAIL] {line}：{reason}" if reason else f"  [FAIL] {line}")


def _section(title: str) -> None:
    """打一个小节标题。"""
    print(f"\n--- {title} ---")


def _run_guarded(label: str, fn: Callable[[], dict[str, bool]]) -> dict[str, bool]:
    """跑一段被测代码；抛异常 ⇒ 记现场并返回空结果（探针**报 FAIL**，不自己崩）。"""
    try:
        return fn()
    except Exception as exc:  # noqa: BLE001
        LAST_ERROR[label] = f"{type(exc).__name__}: {exc}"
        return {}


def _why(label: str) -> str:
    """断言失败时的原因文案（被测代码抛过异常 ⇒ 用异常现场）。"""
    return LAST_ERROR.get(label, "")


@contextlib.contextmanager
def _patched(obj: Any, **attrs: Any) -> Iterator[None]:
    """临时替换模块属性（用完还原）—— **运行时替换**式变异体。"""
    old = {key: getattr(obj, key) for key in attrs}
    for key, value in attrs.items():
        setattr(obj, key, value)
    try:
        yield
    finally:
        for key, value in old.items():
            setattr(obj, key, value)


def _load_module(name: str, path: Path) -> Any:
    """按路径加载模块（避免同名模块串味）。"""
    spec = importlib.util.spec_from_file_location(name, path)
    if spec is None or spec.loader is None:  # pragma: no cover - 防御
        raise RuntimeError(f"加载不了模块：{path}")
    module = importlib.util.module_from_spec(spec)
    sys.modules[name] = module
    spec.loader.exec_module(module)
    return module


def _contract_names() -> list[str]:
    """契约 §4 的件名清单（按业务日展开 ⇒ 排序输出，逐字可复现）。"""
    return sorted(name.format(stamp=FIXED_STAMP) for name in CONTRACT_ARTIFACTS)


def _file_state(root: Path) -> dict[str, tuple]:
    """目录下每个件的内容指纹 ＋ mtime（⇒ 证明「旧快照逐字节不变」）。"""
    out: dict[str, tuple] = {}
    if not root.is_dir():
        return out
    for item in sorted(root.rglob("*")):
        if not item.is_file():
            continue
        stat = item.stat()
        out[item.relative_to(root).as_posix()] = (
            SNAP.sha1_of(item),
            stat.st_size,
            stat.st_mtime_ns,
        )
    return out


# ---------------------------------------------------------------- AST 变异工具（内存内，不落盘）


def _parse(path: Path) -> ast.Module:
    """读源码并解析成 AST（**只读**，绝不写回）。"""
    return ast.parse(path.read_text(encoding="utf-8"))


def _unparse(tree: ast.AST) -> str:
    """把 AST 还原成源码（内存内 ⇒ 不落盘）。"""
    ast.fix_missing_locations(tree)
    return ast.unparse(tree)


def _inject_file_assignment(tree: ast.Module, path: Path) -> None:
    """在模块顶部注入 ``__file__ = <真实路径>``（让 ``python -c`` 跑变异体时路径解析正确）。

    🔴 必须插在 ``from __future__`` **之后**（否则 ``SyntaxError``）。
    """
    index = 0
    for pos, node in enumerate(tree.body):
        if isinstance(node, ast.ImportFrom) and node.module == "__future__":
            index = pos + 1
    assign = ast.Assign(
        targets=[ast.Name(id="__file__", ctx=ast.Store())],
        value=ast.Constant(value=str(path)),
    )
    tree.body.insert(index, assign)


def _mentions(node: ast.AST) -> set[str]:
    """一个 AST 子树里出现过的标识符 / 属性名 / 字符串常量。"""
    found: set[str] = set()
    for item in ast.walk(node):
        if isinstance(item, ast.Name):
            found.add(item.id)
        elif isinstance(item, ast.Attribute):
            found.add(item.attr)
        elif isinstance(item, ast.Constant) and isinstance(item.value, str):
            found.add(item.value)
    return found


def _mutate_rename_token(tree: ast.Module, token: str) -> bool:
    """把源码里所有**恰好等于** ``token`` 的字符串常量改名（⇒ 那个开关/键名失效）。"""
    hits = 0
    for node in ast.walk(tree):
        if isinstance(node, ast.Constant) and isinstance(node.value, str) and node.value == token:
            node.value = token + "-mutated"
            hits += 1
    return hits > 0


def _mutate_drop_conflict_check(tree: ast.Module, left: str, right: str) -> bool:
    """把「两个互斥开关同时给 ⇒ 报错」的分支整个换成**立即退出 0**。

    🔴 换成 ``raise SystemExit(0)`` 而不是直接删：既表达「不再以退出码 2 拒绝」，
    又**保证探针绝不触发流水线**（不取数、不打分、不调 LLM、不发信）。
    """
    for parent in ast.walk(tree):
        body = getattr(parent, "body", None)
        if not isinstance(body, list):
            continue
        for pos, node in enumerate(body):
            if not isinstance(node, ast.If):
                continue
            mentions = _mentions(node)
            if left in mentions and right in mentions:
                body[pos] = ast.copy_location(
                    ast.Raise(
                        exc=ast.Call(
                            func=ast.Name(id="SystemExit", ctx=ast.Load()),
                            args=[ast.Constant(value=0)],
                            keywords=[],
                        ),
                        cause=None,
                    ),
                    node,
                )
                return True
    return False


def _mutate_force_live_trade(tree: ast.Module, func_name: str) -> bool:
    """在目标函数体顶部插入 ``live_trade = True``（⇒ 观察态开关被架空）。"""
    for node in ast.walk(tree):
        if isinstance(node, ast.FunctionDef) and node.name == func_name:
            inject = ast.parse("live_trade = True").body[0]
            node.body.insert(0, inject)
            return True
    return False


def _exec_functions(source: str, names: tuple[str, ...], namespace: dict[str, Any],
                    origin: Path) -> dict[str, Any]:
    """把源码里指定的几个函数**单独**抽出来 exec 到 ``namespace`` 里（不重跑整个模块）。"""
    tree = ast.parse(source)
    picked = [
        node for node in tree.body
        if isinstance(node, ast.FunctionDef) and node.name in names
    ]
    if len(picked) != len(names):
        raise RuntimeError(f"源码里找不到函数：{names}")
    module = ast.Module(body=picked, type_ignores=[])
    ast.fix_missing_locations(module)
    exec(compile(module, str(origin), "exec"), namespace)  # noqa: S102
    return namespace


# ---------------------------------------------------------------- CLI 子进程


def _cli_env(tmp: Path) -> dict[str, str]:
    """子进程环境：**产物根 / 快照根 / 报告目录 / 锁文件全部重定向到临时目录**；
    SMTP 凭据指向不存在的路径 ⇒ **绝不可能真的发信**。"""
    env = dict(os.environ)
    env.update({
        "ETF_ROTATION_RUN_ROOT": str(tmp / "run"),
        "ETF_ROTATION_LOG_DIR": str(tmp / "logs"),
        "ETF_ROTATION_SNAPSHOT_ROOT": str(tmp / "snapshots"),
        "ETF_ROTATION_REPORT_DIR": str(tmp / "report"),
        "ETF_ROTATION_LOCK_FILE": str(tmp / "daily.lock"),
        "ETF_ROTATION_SMTP_SECRETS": str(tmp / "no_smtp.env"),
        "ETF_ROTATION_SMTP_HOST": "127.0.0.1",
        "ETF_ROTATION_SMTP_PORT": "1",
        "ETF_ROTATION_SMTP_TIMEOUT": "3",
        "ETF_ROTATION_REPO_ROOT": str(REPO_ROOT),
        "PYTHONDONTWRITEBYTECODE": "1",
    })
    return env


#: 子进程只带这四个「不干活」开关 ⇒ 即使被测实现坏了也**起不了重活**
SAFE_FLAGS: tuple[str, ...] = (
    "--skip-fetch",
    "--skip-llm",
    "--no-refresh-scores",
    "--no-update-qlib-bin",
)


def _run_cli(args: list[str], *, tmp: Path | str, source: str | None = None,
             timeout: int = 90) -> tuple[int, str]:
    """跑一次 CLI，返回 ``(退出码, stderr)``。

    ``source`` 给了 ⇒ 跑的是**变异体**源码（``python -c``），生产代码**不动**。
    """
    if source is None:
        cmd = [sys.executable, str(MAIN_PY), *args]
    else:
        cmd = [sys.executable, "-c", source, *args]
    try:
        proc = subprocess.run(
            cmd,
            capture_output=True,
            text=True,
            timeout=timeout,
            cwd=str(REPO_ROOT),
            env=_cli_env(Path(tmp)),
        )
    except subprocess.TimeoutExpired:
        return -1, "⏱ 超时"
    return proc.returncode, (proc.stderr or "")


def _strip_usage(stderr: str) -> str:
    """去掉 argparse 的 usage 段 ⇒ 只留真正的错误消息。

    🔴 必须去：argparse 的 usage 会**罗列全部选项**（含 ``--full``）⇒ 不去掉会造成假阳性。
    """
    kept: list[str] = []
    skipping = False
    for line in (stderr or "").splitlines():
        if line.strip().startswith("usage:"):
            skipping = True
            continue
        if skipping:
            if not line.strip() or line[:1].isspace():
                continue
            skipping = False
        kept.append(line)
    return "\n".join(kept)


def _has_migration_hint(stderr: str) -> bool:
    """stderr 里有没有迁移提示（指向 ``--full`` / ``--live-trade`` / 快照制）。"""
    text = _strip_usage(stderr)
    return any(token in text for token in HINT_TOKENS)


# ---------------------------------------------------------------- S1 ~ S9


def _s1_results() -> dict[str, bool]:
    """S1：每次 ``create_snapshot`` 产生**新**目录；同一秒两次 ⇒ 加后缀、绝不覆盖、旧件不变。"""
    with tempfile.TemporaryDirectory(prefix="probe_snap_s1_") as tmp:
        root = Path(tmp) / "snapshots"
        snap1 = SNAP.create_snapshot(root, asof=FIXED_ASOF, run_id="20261109_120000", now=FIXED_NOW)
        snap1.write_text("stock_scores.csv", "code,score\n510300,1.5\n")
        snap1.finalize()
        before = _file_state(snap1.path)
        snap2 = SNAP.create_snapshot(root, asof=FIXED_ASOF, run_id="20261109_120000", now=FIXED_NOW)
        snap2.write_text("stock_scores.csv", "code,score\n510300,9.9\n")
        snap2.finalize()
        after = _file_state(snap1.path)
        return {
            "S1a": snap1.path != snap2.path and snap1.path.is_dir() and snap2.path.is_dir(),
            "S1b": snap2.run_id != snap1.run_id and snap2.run_id.startswith(snap1.run_id),
            "S1c": bool(before) and before == after,
        }


def _s2_results() -> dict[str, bool]:
    """S2：快照**自包含**（manifest 存在 ＋ artifacts 覆盖契约 §4 件名清单 ＋ 无外链）。"""
    with tempfile.TemporaryDirectory(prefix="probe_snap_s2_") as tmp:
        root = Path(tmp) / "snapshots"
        snap = SNAP.create_snapshot(root, asof=FIXED_ASOF, run_id="20261109_120000", now=FIXED_NOW)
        for name in _contract_names():
            if name.endswith(".json"):
                snap.write_json(name, {"probe": True, "name": name})
            else:
                snap.write_text(name, f"# probe 夹具：{name}\n")
        manifest_path = snap.finalize()
        payload = json.loads(manifest_path.read_text(encoding="utf-8"))
        got = {str(item.get("name")) for item in payload.get("artifacts") or []}
        want = set(_contract_names())
        return {
            "S2a": manifest_path.is_file() and manifest_path.name == SNAP.MANIFEST_NAME,
            "S2b": want <= got,
            "S2c": not any(item.is_symlink() for item in snap.path.rglob("*")),
        }


def _s3_results() -> dict[str, bool]:
    """S3：``resolve_scores_base`` 的三种情形（有快照 / 只有 canonical / 都没有）。"""
    with tempfile.TemporaryDirectory(prefix="probe_snap_s3_") as tmp:
        base = Path(tmp)
        root = base / "snapshots"
        canonical = base / "canonical" / "stock_scores.csv"
        canonical.parent.mkdir(parents=True, exist_ok=True)
        canonical.write_text("code,score\n510300,1.0\n", encoding="utf-8")

        # 情形一：还没有任何快照，但有 canonical ⇒ 用 canonical 当一次性只读种子
        seed, _ = SNAP.resolve_scores_base(root, canonical)
        ok_seed = seed is not None and Path(seed) == canonical

        # 情形二：有了上一份快照 ⇒ 优先取快照里的 stock_scores.csv（链式口径）
        snap = SNAP.create_snapshot(root, asof=FIXED_ASOF, run_id="20261109_120000", now=FIXED_NOW)
        snap.write_text("stock_scores.csv", "code,score\n510300,2.0\n")
        chained, note = SNAP.resolve_scores_base(root, canonical)
        ok_chain = chained is not None and Path(chained) == snap.path / "stock_scores.csv"

        # 情形三：两者都没有 ⇒ (None, 说明里写明「无」)
        empty, note_empty = SNAP.resolve_scores_base(base / "empty_root", base / "missing.csv")
        ok_empty = empty is None and "无" in str(note_empty)

        return {
            "S3a": ok_chain and "快照" in str(note),
            "S3b": ok_seed,
            "S3c": ok_empty,
        }


def _s4_results() -> dict[str, bool]:
    """S4：**不写根目录全局文件**（建快照 + 写件 ⇒ canonical 指纹三项全等）。"""
    with tempfile.TemporaryDirectory(prefix="probe_snap_s4_") as tmp:
        base = Path(tmp)
        canonical = base / "canonical" / "stock_scores.csv"
        canonical.parent.mkdir(parents=True, exist_ok=True)
        canonical.write_text("code,score\n510300,1.0\n", encoding="utf-8")
        root = base / "snapshots"

        before = SNAP.root_global_fingerprint([canonical])[str(canonical)]
        snap = SNAP.create_snapshot(root, asof=FIXED_ASOF, run_id="20261109_120000", now=FIXED_NOW)
        snap.write_text("stock_scores.csv", "code,score\n510300,2.0\n")
        snap.write_json("execution_card_20261109.json", {"orders": []})
        snap.finalize()
        after = SNAP.root_global_fingerprint([canonical])[str(canonical)]

        return {
            "S4a": before.get("exists") is True and after.get("exists") is True,
            "S4b": before.get("bytes") == after.get("bytes"),
            "S4c": before.get("sha1") == after.get("sha1"),
            "S4d": before.get("mtime") == after.get("mtime"),
        }


def _s5_results() -> dict[str, bool]:
    """S5：manifest **完整披露**（字段齐全 ＋ ``set_skipped`` 登记项进 ``skipped``）。"""
    with tempfile.TemporaryDirectory(prefix="probe_snap_s5_") as tmp:
        root = Path(tmp) / "snapshots"
        snap = SNAP.create_snapshot(root, asof=FIXED_ASOF, live_trade=False,
                                    run_id="20261109_120000", now=FIXED_NOW)
        snap.set_stage("analyze", "ok", seconds=1.5)
        snap.set_skipped("skip_fetch", "`--full` 强制全流程 ⇒ 覆盖 --skip-fetch")
        snap.add_note("探针夹具备注")
        snap.write_text("stock_scores.csv", "code,score\n510300,1.0\n")
        payload = json.loads(snap.finalize().read_text(encoding="utf-8"))
        required = ("stages", "skipped", "notes", "live_trade", "artifacts")
        skipped_names = [str(item.get("name")) for item in payload.get("skipped") or []]
        stage_names = [str(item.get("name")) for item in payload.get("stages") or []]
        return {
            "S5a": all(key in payload for key in required),
            "S5b": "skip_fetch" in skipped_names,
            "S5c": payload.get("live_trade") is False,
            "S5d": "探针夹具备注" in [str(n) for n in payload.get("notes") or []],
            "S5e": "analyze" in stage_names,
        }


def _s6_results(source: str | None = None) -> dict[str, bool]:
    """S6：``--mode`` 被拒 ⇒ 退出码 2 ＋ stderr 迁移提示。"""
    with tempfile.TemporaryDirectory(prefix="probe_snap_s6_") as tmp:
        rc, err = _run_cli(["--mode", "B", *SAFE_FLAGS], tmp=tmp, source=source)
        LAST_CLI.clear()
        LAST_CLI.update({"rc": rc, "stderr": err})
        return {
            "S6": rc == 2 and _has_migration_hint(err),
            "S6_rc": rc == 2,
            "S6_hint": _has_migration_hint(err),
        }


def _s7_results(source: str | None = None) -> dict[str, bool]:
    """S7：``--live-trade`` 与 ``--dry-run`` 互斥 ⇒ 退出码 2。"""
    with tempfile.TemporaryDirectory(prefix="probe_snap_s7_") as tmp:
        rc, _err = _run_cli(["--live-trade", "--dry-run", *SAFE_FLAGS], tmp=tmp, source=source)
        return {"S7": rc == 2}


def _s8_results(module: Any | None = None) -> dict[str, bool]:
    """S8：``resolve_stage_flags``（``--full`` 强制四阶段全开 ＋ 被覆盖的 skip 进 ``skipped``）。"""
    main_mod = module if module is not None else _load_module("main", MAIN_PY)
    parse_args = getattr(main_mod, "parse_args", None)
    resolve = getattr(main_mod, "resolve_stage_flags", None)
    if parse_args is None or resolve is None:
        return {"S8": False, "S8a": False, "S8b": False, "S8c": False, "S8d": False}
    try:
        # 🔴 argparse 的 usage/报错走 stderr ⇒ 吞掉（探针输出必须干净、逐字可复现）
        with contextlib.redirect_stderr(io.StringIO()):
            args = parse_args(["--full", "--skip-fetch", "--skip-llm",
                               "--no-refresh-scores", "--no-update-qlib-bin"])
            flags = resolve(args)
            default_flags = resolve(parse_args([]))
    except SystemExit:
        return {"S8": False, "S8a": False, "S8b": False, "S8c": False, "S8d": False}
    except Exception:  # noqa: BLE001
        return {"S8": False, "S8a": False, "S8b": False, "S8c": False, "S8d": False}
    skipped_names = {str(item.get("name")) for item in flags.get("skipped") or []}
    want_skipped = {"skip_fetch", "skip_llm", "no_refresh_scores", "no_update_qlib_bin"}
    return {
        "S8": (
            bool(flags.get("fetch")) and bool(flags.get("refresh_scores"))
            and bool(flags.get("update_qlib")) and bool(flags.get("llm"))
            and flags.get("full") is True and want_skipped <= skipped_names
        ),
        "S8a": (bool(flags.get("fetch")) and bool(flags.get("refresh_scores"))
                and bool(flags.get("update_qlib")) and bool(flags.get("llm"))),
        "S8b": flags.get("full") is True,
        "S8c": want_skipped <= skipped_names,
        "S8d": (
            default_flags.get("full") is False
            and bool(default_flags.get("fetch")) and bool(default_flags.get("refresh_scores"))
            and bool(default_flags.get("update_qlib")) and bool(default_flags.get("llm"))
            and not (default_flags.get("skipped") or [])
        ),
    }


def _disabled_marker_keys(card: Any) -> set[str]:
    """卡上「挂单禁用」标记的键集合（**字段名以实现为准** ⇒ 多种写法都认）。"""
    if not isinstance(card, dict):
        return set()
    keys: set[str] = set()
    for key in ("order_enabled", "trade_enabled", "can_order"):
        if key in card and card[key] is False:
            keys.add(key)
    for key in ("disabled", "order_disabled", "observation_only"):
        if card.get(key) is True:
            keys.add(key)
    for key in ("watermark", "notice"):
        text = str(card.get(key) or "")
        if "挂单禁用" in text or "不可下单" in text:
            keys.add(key)
    return keys


def _card_disabled(card: Any) -> bool:
    """卡上是否带「挂单禁用」标记（卡片级 **或** 订单级都算）。"""
    if not isinstance(card, dict):
        return False
    if _disabled_marker_keys(card):
        return True
    orders = card.get("orders") or []
    if orders and all(
        (item.get("disabled") is True) or (item.get("order_enabled") is False)
        for item in orders
    ):
        return True
    return False


def _s9_results(builder: Callable[..., dict] | None = None) -> dict[str, bool]:
    """S9：执行卡的挂单禁用标记（观察态带、实盘态不带）。"""
    ar = _load_module("agents_rebalance", AGENTS_PY)
    build = builder if builder is not None else getattr(ar, "build_execution_card", None)
    if build is None:
        return {"S9": False, "S9a": False, "S9b": False, "S9c": False}
    actions = [{"code": "563020", "name": "探针夹具标的", "side": "buy",
                "target_amount": 10000.0, "price": 1.2}]
    observation = build(actions=actions, asof=FIXED_ASOF, live_trade=False)
    live = build(actions=actions, asof=FIXED_ASOF, live_trade=True)
    only_observation = _disabled_marker_keys(observation) - _disabled_marker_keys(live)
    return {
        "S9": _card_disabled(observation) and not _card_disabled(live),
        "S9a": _card_disabled(observation),
        "S9b": not _card_disabled(live),
        # 🔴 「两种卡**必须有差异**」⇒ 开关真的作用在卡上（不靠某个具体字段名）
        "S9c": bool(only_observation),
    }


# ---------------------------------------------------------------- S10 变异反证


def _mutant_source() -> tuple[str, dict[str, str]]:
    """造三份**内存内 AST 改写**的 main.py 变异体（不落盘改生产代码）。"""
    tree = _parse(MAIN_PY)

    # 变异 A：把 ``--mode`` 这个开关**改名** ⇒ 拒绝逻辑失效（CLI 不再识别它）
    tree_a = ast.parse(ast.unparse(tree))
    _mutate_rename_token(tree_a, "--mode")
    _inject_file_assignment(tree_a, MAIN_PY)
    source_a = _unparse(tree_a)

    # 变异 B：把「``--dry-run`` 与 ``--live-trade`` 互斥」的分支换成**立即退出 0**
    tree_b = ast.parse(ast.unparse(tree))
    if not _mutate_drop_conflict_check(tree_b, "dry_run", "live_trade"):
        return source_a, {}
    _inject_file_assignment(tree_b, MAIN_PY)
    source_b = _unparse(tree_b)

    # 变异 C：把 ``--full`` 这个开关**改名** ⇒ ``--full`` 强制全流程不可达
    tree_c = ast.parse(ast.unparse(tree))
    _mutate_rename_token(tree_c, "--full")
    _inject_file_assignment(tree_c, MAIN_PY)
    source_c = _unparse(tree_c)

    return source_a, {"conflict": source_b, "full": source_c}


def _mutated_s8_module(source: str) -> Any:
    """把「``--full`` 改名」的变异体抽成可调用模块（**只 exec 两个纯函数** ⇒ 不跑流水线）。"""
    main_mod = _load_module("main", MAIN_PY)
    namespace = dict(vars(main_mod))
    _exec_functions(source, ("parse_args", "resolve_stage_flags"), namespace, MAIN_PY)
    return type("MutantMain", (), {
        "parse_args": staticmethod(namespace["parse_args"]),
        "resolve_stage_flags": staticmethod(namespace["resolve_stage_flags"]),
    })


def _mutated_s9_builder(source: str) -> Callable[..., dict]:
    """把「``live_trade = True`` 注入」的变异体抽成可调用的 ``build_execution_card``。"""
    ar = _load_module("agents_rebalance", AGENTS_PY)
    namespace = dict(vars(ar))
    _exec_functions(source, ("build_execution_card",), namespace, AGENTS_PY)
    return namespace["build_execution_card"]


def _flipped(baseline: dict[str, bool], mutant: dict[str, bool]) -> tuple[bool, str]:
    """变异体是否把正向断言打红（至少一条 True ⇒ False）。"""
    flipped = sorted(key for key, value in baseline.items() if value and not mutant.get(key, False))
    if flipped:
        return True, f"变异体下 {'/'.join(flipped)} 由 True 转 False"
    still = sorted(key for key, value in baseline.items() if value and mutant.get(key, False))
    if still:
        return False, f"变异体下 {'/'.join(still)} 仍为 True（变异体无效 ⇒ 断言没牙）"
    return False, "基线里没有 True 的断言（对照无效）"


def _run_s10(baselines: dict[str, dict[str, bool]]) -> None:
    """S10：逐条把被测行为改坏 ⇒ 对应断言**必须转红**。"""
    source_a, sources = _mutant_source()

    # S1：目录名不加后缀 ⇒ 同名目录被覆盖
    with _patched(SNAP, _unique_run_id=lambda root, run_id: run_id):
        mutant = _run_guarded("S10a", _s1_results)
    ok, why = _flipped(baselines["S1"], mutant)
    check("S10a", "变异「快照目录名不加后缀（同一秒两次 ⇒ 覆盖旧快照）」⇒ S1 必须转红", ok, why)

    # S2：artifacts 漏报件名 ⇒ 清单不再覆盖契约 §4
    original_artifacts = SNAP.Snapshot.artifacts

    def _mutant_artifacts(self: Any) -> list[dict]:
        return [
            item for item in original_artifacts(self)
            if "rebalance_recommendation" not in str(item.get("name"))
        ]

    with _patched(SNAP.Snapshot, artifacts=_mutant_artifacts):
        mutant = _run_guarded("S10b", _s2_results)
    ok, why = _flipped(baselines["S2"], mutant)
    check("S10b", "变异「artifacts 漏报件名」⇒ S2 必须转红", ok, why)

    # S3：底座解析无视快照 ⇒ 永远用 canonical
    def _mutant_resolve_scores_base(snapshot_root: Any, canonical_csv: Any,
                                    *, exclude: Any = None) -> tuple[Any, str]:
        canonical = Path(canonical_csv)
        if canonical.is_file():
            return canonical, "变异体：无条件用 canonical"
        return None, "变异体：无底座"

    with _patched(SNAP, resolve_scores_base=_mutant_resolve_scores_base):
        mutant = _run_guarded("S10c", _s3_results)
    ok, why = _flipped(baselines["S3"], mutant)
    check("S10c", "变异「底座解析无视快照（永远用 canonical）」⇒ S3 必须转红", ok, why)

    # S4：写件时顺带写根目录全局文件 ⇒ canonical 指纹漂移
    def _mutant_write_text(self: Any, name: Any, text: str) -> Path:
        path = _ORIGINAL_WRITE_TEXT(self, name, text)
        canonical = Path(self.root).parent / "canonical" / "stock_scores.csv"
        canonical.parent.mkdir(parents=True, exist_ok=True)
        canonical.write_text(text, encoding="utf-8")
        return path

    with _patched(SNAP.Snapshot, write_text=_mutant_write_text):
        mutant = _run_guarded("S10d", _s4_results)
    ok, why = _flipped(baselines["S4"], mutant)
    check("S10d", "变异「写件时顺带写根目录全局文件」⇒ S4 必须转红", ok, why)

    # S5：``set_skipped`` 变成空操作 ⇒ 跳过项被静默吞掉
    with _patched(SNAP.Snapshot, set_skipped=lambda self, name, reason: {}):
        mutant = _run_guarded("S10e", _s5_results)
    ok, why = _flipped(baselines["S5"], mutant)
    check("S10e", "变异「set_skipped 空操作（跳过项被静默吞掉）」⇒ S5 必须转红", ok, why)

    # S6：``--mode`` 改名 ⇒ CLI 不再识别它 ⇒ 不再拒
    mutant = _run_guarded("S10f", lambda: _s6_results(source=source_a))
    ok, why = _flipped(baselines["S6"], mutant)
    check("S10f", "变异「--mode 开关改名（拒绝逻辑失效）」⇒ S6 必须转红", ok, why)

    # S7：互斥分支改成立即退出 0 ⇒ 不再以退出码 2 拒绝
    conflict_source = sources.get("conflict")
    if not conflict_source:
        check("S10g", "变异「--dry-run/--live-trade 互斥检查去掉」⇒ S7 必须转红",
              False, "变异体没命中互斥分支（源码结构与探针假设不符）")
    else:
        mutant = _run_guarded("S10g", lambda: _s7_results(source=conflict_source))
        ok, why = _flipped(baselines["S7"], mutant)
        check("S10g", "变异「--dry-run/--live-trade 互斥检查去掉」⇒ S7 必须转红", ok, why)

    # S8：``--full`` 改名 ⇒ 强制全流程不可达
    full_source = sources.get("full")
    if not full_source:
        check("S10h", "变异「--full 开关改名（强制全流程不可达）」⇒ S8 必须转红",
              False, "变异体没命中 --full")
    else:
        mutant = _run_guarded("S10h", lambda: _s8_results(module=_mutated_s8_module(full_source)))
        ok, why = _flipped(baselines["S8"], mutant)
        check("S10h", "变异「--full 开关改名（强制全流程不可达）」⇒ S8 必须转红", ok, why)

    # S9：把 ``live_trade`` 写死成 True ⇒ 观察态卡也不带禁用标记
    tree = _parse(AGENTS_PY)
    if not _mutate_force_live_trade(tree, "build_execution_card"):
        check("S10i", "变异「live_trade 写死成 True（观察态卡也不禁用）」⇒ S9 必须转红",
              False, "变异体没命中 build_execution_card")
    else:
        mutant = _run_guarded("S10i", lambda: _s9_results(builder=_mutated_s9_builder(_unparse(tree))))
        ok, why = _flipped(baselines["S9"], mutant)
        check("S10i", "变异「live_trade 写死成 True（观察态卡也不禁用）」⇒ S9 必须转红", ok, why)


# ---------------------------------------------------------------- 入口


def main() -> int:
    """跑全部断言并汇总（退出码 0 = 全绿，1 = 有 FAIL）。"""
    print("=== 快照契约闸门探针（S1~S10）===")

    baselines: dict[str, dict[str, bool]] = {}

    _section("S1 · 快照目录唯一性（同一秒两次 ⇒ 加后缀、绝不覆盖、旧件逐字节不变）")
    s1 = _run_guarded("S1", _s1_results)
    baselines["S1"] = s1
    check("S1a", "两次 create_snapshot 产生两个不同目录", s1.get("S1a", False), _why("S1"))
    check("S1b", "同一秒两次 ⇒ 后者目录名加后缀", s1.get("S1b", False), _why("S1"))
    check("S1c", "旧快照里的件逐字节不变（含 mtime）", s1.get("S1c", False), _why("S1"))

    _section("S2 · 快照自包含（manifest + artifacts 覆盖契约 §4 件名清单）")
    s2 = _run_guarded("S2", _s2_results)
    baselines["S2"] = s2
    check("S2a", "manifest 存在且文件名 = snapshot_manifest.json", s2.get("S2a", False), _why("S2"))
    check("S2b", "artifacts 覆盖契约 §4 的全部件名", s2.get("S2b", False), _why("S2"))
    check("S2c", "快照内无软链（自包含、可逐字节复现）", s2.get("S2c", False), _why("S2"))

    _section("S3 · 只读底座解析（有快照 / 只有 canonical / 都没有）")
    s3 = _run_guarded("S3", _s3_results)
    baselines["S3"] = s3
    check("S3a", "有上一份快照 ⇒ 返回快照内的 stock_scores.csv", s3.get("S3a", False), _why("S3"))
    check("S3b", "无快照但有 canonical ⇒ 返回 canonical（一次性只读种子）", s3.get("S3b", False), _why("S3"))
    check("S3c", "两者都无 ⇒ 返回 (None, 说明含「无」)", s3.get("S3c", False), _why("S3"))

    _section("S4 · 不写根目录全局文件（canonical 指纹三项全等）")
    s4 = _run_guarded("S4", _s4_results)
    baselines["S4"] = s4
    check("S4a", "canonical 文件建快照前后都在", s4.get("S4a", False), _why("S4"))
    check("S4b", "canonical 文件 size 不变", s4.get("S4b", False), _why("S4"))
    check("S4c", "canonical 文件 sha1 不变", s4.get("S4c", False), _why("S4"))
    check("S4d", "canonical 文件 mtime 不变", s4.get("S4d", False), _why("S4"))

    _section("S5 · manifest 完整披露（stages/skipped/notes/live_trade/artifacts）")
    s5 = _run_guarded("S5", _s5_results)
    baselines["S5"] = s5
    check("S5a", "manifest 五个必填字段齐全", s5.get("S5a", False), _why("S5"))
    check("S5b", "set_skipped 登记项出现在 skipped 里", s5.get("S5b", False), _why("S5"))
    check("S5c", "manifest 披露 live_trade（观察态 = False）", s5.get("S5c", False), _why("S5"))
    check("S5d", "manifest 披露 notes", s5.get("S5d", False), _why("S5"))
    check("S5e", "manifest 披露 stages", s5.get("S5e", False), _why("S5"))

    _section("S6 · --mode 已废除（退出码 2 + 迁移提示）")
    s6 = _run_guarded("S6", _s6_results)
    baselines["S6"] = s6
    check("S6", "--mode 被拒 ⇒ 退出码 2", s6.get("S6_rc", False),
          f"实测退出码 {LAST_CLI.get('rc')}（期望 2）" + _why("S6"))
    check("S6b", "stderr 含迁移提示（--full / --live-trade / 快照）", s6.get("S6_hint", False), _why("S6"))

    _section("S7 · --live-trade 与 --dry-run 互斥（退出码 2）")
    s7 = _run_guarded("S7", _s7_results)
    baselines["S7"] = s7
    check("S7", "--live-trade 与 --dry-run 同时给 ⇒ 退出码 2", s7.get("S7", False), _why("S7"))

    _section("S8 · resolve_stage_flags（--full 强制四阶段全开 + 被覆盖的 skip 进 skipped）")
    s8 = _run_guarded("S8", _s8_results)
    baselines["S8"] = s8
    check("S8a", "--full ⇒ fetch/refresh_scores/update_qlib/llm 四阶段全开", s8.get("S8a", False), _why("S8"))
    check("S8b", "flags['full'] 为 True", s8.get("S8b", False), _why("S8"))
    check("S8c", "被 --full 覆盖的四个 skip 开关都登记进 skipped", s8.get("S8c", False), _why("S8"))
    check("S8d", "缺省（无 --full）⇒ 四阶段全开、full=False、skipped 为空", s8.get("S8d", False), _why("S8"))

    _section("S9 · 执行卡挂单禁用标记（观察态带、实盘态不带）")
    s9 = _run_guarded("S9", _s9_results)
    baselines["S9"] = s9
    check("S9a", "live_trade=False ⇒ 卡片带挂单禁用标记", s9.get("S9a", False), _why("S9"))
    check("S9b", "live_trade=True ⇒ 卡片不带挂单禁用标记", s9.get("S9b", False), _why("S9"))
    check("S9c", "观察卡与实盘卡**有差异**（禁用标记只出现在观察卡上）",
          s9.get("S9c", False), _why("S9"))

    _section("S10 · 变异反证（把行为改坏 ⇒ 对应断言必须转红）")
    _run_s10(baselines)

    print(f"\n=== 结果：PASS {len(PASS)} / FAIL {len(FAIL)} ===")
    if FAIL:
        print("FAIL：" + "; ".join(FAIL))
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
