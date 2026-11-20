"""🔴 2026-11-19 新增：编排器步骤 5 的「实例锁守卫」**必须与每日链同口径**。

【背景：2026-11-19 真机复盘抓到的真缺陷】
`manu/tools/server_full_pipeline_run.sh` 的步骤 5 有一段守卫：清理 ``_work/`` 之前先
试探**流水线实例锁**是否被持有 ⇒ 被持有就说明**还有活实例在用 ``_work/``** ⇒ 一律不许清。

🔴 **缺陷**：守卫试探的锁路径写的是 ``$RUN/.lock``（= ``<RESULTS_ROOT>/.lock``），
而每日链**真正用的**实例锁在 ``<RESULTS_ROOT>/daily/.lock``
（单一事实源 = ``manu/etf_rotation/daily/daily_config.py::LOCK_FILE``）
⇒ 守卫**恒判空闲** ⇒ 并发跑时会把**另一个活实例正在用的 ``_work/``** 删掉
⇒ 复现 **2026-11-16** 那次事故（``ak`` 子进程落盘失败 → 整条链 rc=1）。

本文件把这个守卫钉死：**编排器试探的锁路径必须与 ``daily_config.LOCK_FILE`` 同口径**。

【测试风格】
被测对象 = 一个 **bash 脚本** ⇒ 用**文本解析 + 真实 bash 求值**两条腿验：
① 文本层：脚本里不许再出现 ``$RUN/.lock`` 这种与每日链不同口径的写法；
② 求值层：把脚本里的路径表达式**抽出来在真 bash 里展开** ⇒ 结果必须等于
   ``daily_config.LOCK_FILE``（默认环境）。

【变异反证】
把脚本里的守卫改回旧写法（``$RUN/.lock``）⇒ 断言必须转红（证明测试有牙）。
"""

from __future__ import annotations

import os
import re
import subprocess
import sys
from pathlib import Path

import pytest

#: `manu/etf_rotation/` 目录（照抄本目录 `conftest.py` 的做法）
ETF_DIR = Path(__file__).resolve().parents[1]
if str(ETF_DIR) not in sys.path:
    sys.path.insert(0, str(ETF_DIR))
#: ``daily_config`` 在 ``manu/etf_rotation/daily/`` ⇒ 也要挂进去
if str(ETF_DIR / "daily") not in sys.path:
    sys.path.insert(0, str(ETF_DIR / "daily"))

#: 🔴 本文件有**两个镜像位置**（``manu/etf_rotation/tests/`` 与 ``manu/tests/``，
#: 两者必须逐字一致）⇒ **不许**用固定的 ``parents[N]`` 定位 ⇒ 改成向上搜索
#: ⇒ 两侧都能真正跑到被测脚本（否则镜像侧 FileNotFoundError ⇒ 又一批收集错误）。
def _find_orchestrator() -> Path:
    for parent in Path(__file__).resolve().parents:
        candidate = parent / "manu" / "tools" / "server_full_pipeline_run.sh"
        if candidate.is_file():
            return candidate
    raise FileNotFoundError("找不到 manu/tools/server_full_pipeline_run.sh")


#: 被测编排器脚本
ORCHESTRATOR = _find_orchestrator()


def _read_orchestrator() -> str:
    assert ORCHESTRATOR.is_file(), f"找不到编排器脚本：{ORCHESTRATOR}"
    return ORCHESTRATOR.read_text(encoding="utf-8")


def _daily_lock_file() -> Path:
    """每日链的实例锁（单一事实源 = ``daily_config.LOCK_FILE``）。"""
    import daily_config as dc

    return dc.LOCK_FILE


def _resolve_instance_lock(script_text: str) -> str:
    """在真 bash 里展开脚本里的 ``INSTANCE_LOCK`` 取值（只抽这一行 + 其依赖行）。"""
    lines = script_text.splitlines()
    picked: list[str] = []
    for line in lines:
        stripped = line.strip()
        if stripped.startswith("DAILY_RUN_ROOT=") or stripped.startswith("INSTANCE_LOCK="):
            picked.append(stripped)
    assert picked, "脚本里找不到 INSTANCE_LOCK / DAILY_RUN_ROOT 定义"

    prelude = (
        'RESULTS_ROOT="${ETF_ROTATION_RESULTS_ROOT:-/mnt/data/siping/runs/etf_rotation_results}"\n'
        'RUN="${ETF_RUN_ROOT:-$RESULTS_ROOT}"\n'
        + "\n".join(picked)
        + '\nprintf "%s" "$INSTANCE_LOCK"\n'
    )
    env = {k: v for k, v in os.environ.items() if not k.startswith("ETF_ROTATION_")}
    proc = subprocess.run(
        ["bash", "-c", prelude],
        capture_output=True,
        text=True,
        env=env,
        check=True,
    )
    return proc.stdout.strip()


def test_编排器实例锁必须与每日链LOCK_FILE同口径() -> None:
    """🔴 守卫试探的锁路径必须**就是**每日链用的那个锁。"""
    resolved = _resolve_instance_lock(_read_orchestrator())
    assert resolved == str(_daily_lock_file()), (
        f"🔴 编排器步骤 5 的实例锁守卫路径与每日链不一致：\n"
        f"  编排器 = {resolved}\n"
        f"  每日链 = {_daily_lock_file()}\n"
        f"  ⇒ 守卫会恒判空闲 ⇒ 并发跑时会删掉别的活实例正在用的 _work/（2026-11-16 事故）"
    )


def test_脚本里不许再出现旧的错口径锁路径() -> None:
    """🔴 文本层兜底：``$RUN/.lock`` 与每日链不同口径 ⇒ 直接判红。"""
    text = _read_orchestrator()
    offenders = [
        line.strip()
        for line in text.splitlines()
        if re.search(r'INSTANCE_LOCK=.*\$RUN/\.lock\b', line)
    ]
    assert not offenders, f"🔴 实例锁守卫又写回了错口径的路径：{offenders}"


def test_变异反证_改回旧写法_断言必须转红() -> None:
    """🔴 变异反证：把守卫改回 ``$RUN/.lock`` ⇒ 两条断言都必须失败。

    🔴 手法：改的是 **内存里的文本副本**，绝不碰真脚本。
    """
    original = _read_orchestrator()
    mutated = original.replace(
        'DAILY_RUN_ROOT="${ETF_ROTATION_RUN_ROOT:-$RESULTS_ROOT/daily}"\n'
        'INSTANCE_LOCK="${ETF_ROTATION_LOCK_FILE:-$DAILY_RUN_ROOT/.lock}"',
        'INSTANCE_LOCK="${ETF_ROTATION_LOCK_FILE:-$RUN/.lock}"',
    )
    assert mutated != original, "变异没生效（源码结构变了？⇒ 测试要跟着改）"

    with pytest.raises(AssertionError):
        resolved = _resolve_instance_lock(mutated)
        assert resolved == str(_daily_lock_file())

    offenders = [
        line.strip()
        for line in mutated.splitlines()
        if re.search(r'INSTANCE_LOCK=.*\$RUN/\.lock\b', line)
    ]
    assert offenders, "（变异后文本层也应当判红）"
