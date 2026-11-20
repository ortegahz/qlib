"""🔴 2026-11-19 新增：「回归证据是否覆盖当前 memory 四层」的守卫。

【背景】
`rules.md §十四.128` 第 6 条：**改 memory 四层必须同轮重跑规范口径回归**。
此前这条铁律**只有人守、没有机器闸门** ⇒ 守卫测试的绿灯可能覆盖的是**旧**的 memory
（实测：位次双口径改写发生在回归之后 ⇒ 守卫测试 M2 的断言字面串当场过期，
要到下一轮复跑才暴露）。本工具把它机器化。

【被测对象】
`manu/tools/check_memory_regression_freshness.py`（**纯只读**闸门，不写任何被检查的文件）。

【测试风格】
照抄同目录既有风格：被测模块用 ``importlib`` 按**路径**加载
（``manu/tools/`` 不在 ``sys.path`` 的默认位置）。
🔴 本文件有**两个镜像位置**（``manu/etf_rotation/tests/`` 与 ``manu/tests/``，
两者必须**逐字节一致**）⇒ **不许**用固定的 ``parents[N]`` 定位 ⇒ 改成向上搜索。

【变异反证（守卫有牙）】
M1 回归之后**改了** memory 内容 ⇒ 必须报「R-回归证据过期」；
M2 最新回归是**红的** ⇒ 必须报「R-回归红」（红灯不能当证据）；
M3 **没有**回归证据 ⇒ 必须 rc 2「不适用」，**不许**当成缺陷（host 无证据属正常）；
M4 旧日志（无证据头）＋ memory mtime 更新 ⇒ 必须报「R-回归证据过期」（mtime 回退口径）；
M5 旧日志（无证据头）＋ memory mtime 更旧 ⇒ 必须 **rc 0**（不许误伤）；
M6 🔴 **反例**：memory 文件被 touch（mtime 变新）但**内容没变** ⇒ 必须**仍然 rc 0**
   （内容口径优先于时间口径 ⇒ 不许因为「文件动过」就误报红）；
M7 证据**选取**必须按日志 mtime 取最新 ⇒ 旧的红日志不许盖掉新的绿日志；
M8 工具**只读** ⇒ 跑完四层 md5 必须一字不变。

【live 集成断言】
对**真实工程目录** ＋ **自造的证据**（证据头按真实四层 md5 现算）⇒ 断言 rc == 0
（**不许 skip、不许弱化**）；另有一条对**真实证据根**的交叉复核（自算 md5 与工具判定互证）。
"""

from __future__ import annotations

import hashlib
import importlib.util
import os
import shutil
import subprocess
import sys
from pathlib import Path

import pytest


def _find_tool() -> Path:
    """向上找 ``manu/tools/check_memory_regression_freshness.py``。

    🔴 本文件有**两个镜像位置** ⇒ **不许**用固定的 ``parents[N]`` 定位 ⇒ 改成向上搜索。
    """
    for parent in Path(__file__).resolve().parents:
        candidate = parent / "manu" / "tools" / "check_memory_regression_freshness.py"
        if candidate.is_file():
            return candidate
    raise AssertionError("找不到 manu/tools/check_memory_regression_freshness.py")


def _find_project_dir() -> Path:
    """向上找工程根（含 ``manu/tools/`` 的那一层）。"""
    for parent in Path(__file__).resolve().parents:
        if (parent / "manu" / "tools" / "check_memory_regression_freshness.py").is_file():
            return parent
    raise AssertionError("找不到工程根目录")


TOOL_PATH = _find_tool()
PROJECT_DIR = _find_project_dir()


def _load_tool():
    """按路径加载被测工具（``manu/tools/`` 不在默认 ``sys.path`` 上）。"""
    spec = importlib.util.spec_from_file_location("check_memory_regression_freshness_under_test", TOOL_PATH)
    assert spec and spec.loader
    module = importlib.util.module_from_spec(spec)
    # 🔴 必须先注册进 ``sys.modules``：被测模块用了 ``@dataclass`` ＋
    #    ``from __future__ import annotations`` ⇒ dataclasses 会回查
    #    ``sys.modules[cls.__module__].__dict__`` ⇒ 不注册会直接 AttributeError。
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


TOOL = _load_tool()

#: 要复制进 fixture 的文件（相对工程根）
_FIXTURE_FILES = (
    "manu/memory/rules.md",
    "manu/memory/memory.md",
    "manu/memory/memory_compact.md",
    "manu/memory/.slim_memory.md",
)

#: 绿色摘要（照抄规范口径回归的真实形态）
_GREEN_SUMMARY = "1407 passed, 7 skipped, 32 warnings in 183.63s (0:03:03)"

#: 红色摘要（照抄「守卫字面串过期」那轮的真实形态）
_RED_SUMMARY = "1 failed, 1406 passed, 7 skipped, 32 warnings in 175.19s (0:02:55)"


def _md5(path: Path) -> str:
    hasher = hashlib.md5()
    hasher.update(path.read_bytes())
    return hasher.hexdigest()


@pytest.fixture()
def sandbox(tmp_path: Path) -> Path:
    """把真实工程里被检查的四层 memory 复制一份到临时目录（之后只改副本）。"""
    project = tmp_path / "project"
    for rel in _FIXTURE_FILES:
        source = PROJECT_DIR / rel
        target = project / rel
        target.parent.mkdir(parents=True, exist_ok=True)
        shutil.copy2(source, target)
    return project


def _make_evidence(regression_root: Path, project_dir: Path, summary: str = _GREEN_SUMMARY, name: str = "_regression_20261119_r10") -> Path:
    """造一份回归证据（带 ``[MEMORY-MD5]`` 证据头 ＋ 指定摘要）。"""
    log_dir = regression_root / name
    log_dir.mkdir(parents=True, exist_ok=True)
    log = log_dir / "pytest.log"
    header = TOOL.build_header(str(project_dir))
    log.write_text("%s\n%s\n" % (header, summary), encoding="utf-8")
    return log


def _defect_checks(defects) -> set:
    return {defect.check for defect in defects}


def _reasons(defects) -> str:
    return " ｜ ".join(defect.reason for defect in defects)


# --------------------------------------------------------------------------- #
# 基线：真实状态必须是绿的
# --------------------------------------------------------------------------- #
def test_sandbox_baseline_is_green(sandbox: Path, tmp_path: Path) -> None:
    """复制出来的基线 ＋ 覆盖当前字节的证据 ⇒ 必须 rc 0。"""
    regression_root = tmp_path / "regression"
    _make_evidence(regression_root, sandbox)
    analysis = TOOL.analyze(str(sandbox), str(regression_root))
    assert analysis.applicable
    assert not analysis.defects, _reasons(analysis.defects)


def test_live_project_with_matching_evidence_is_green(tmp_path: Path) -> None:
    """live 集成：真实工程目录 ＋ 现算证据头 ⇒ 必须 rc 0（不许 skip、不许弱化）。"""
    regression_root = tmp_path / "regression"
    _make_evidence(regression_root, PROJECT_DIR)
    analysis = TOOL.analyze(str(PROJECT_DIR), str(regression_root))
    assert analysis.applicable
    assert not analysis.defects, _reasons(analysis.defects)


def test_live_project_evidence_header_covers_current_memory(tmp_path: Path) -> None:
    """live 交叉复核：工具的「过期」判定必须与**自算** md5 比对一致。"""
    regression_root = tmp_path / "regression"
    _make_evidence(regression_root, PROJECT_DIR)
    analysis = TOOL.analyze(str(PROJECT_DIR), str(regression_root))
    header = analysis.evidence.header
    expected_stale = any(header.get(rel) != _md5(PROJECT_DIR / rel) for rel in _FIXTURE_FILES)
    reported_stale = "R-回归证据过期" in _defect_checks(analysis.defects)
    assert expected_stale == reported_stale, "工具判定与自算 md5 比对不一致 ⇒ 判定逻辑不可信"


# --------------------------------------------------------------------------- #
# 变异反证
# --------------------------------------------------------------------------- #
def test_m1_memory_changed_after_regression_must_be_red(sandbox: Path, tmp_path: Path) -> None:
    """M1：回归之后改了 memory 内容 ⇒ 必须报红。"""
    regression_root = tmp_path / "regression"
    _make_evidence(regression_root, sandbox)
    target = sandbox / "manu" / "memory" / "memory.md"
    target.write_text(target.read_text(encoding="utf-8") + "\n（变异：改了内容）\n", encoding="utf-8")
    analysis = TOOL.analyze(str(sandbox), str(regression_root))
    assert analysis.defects, "改了 memory 却没人报错 ⇒ 守卫没牙"
    assert "R-回归证据过期" in _defect_checks(analysis.defects), _reasons(analysis.defects)


def test_m2_red_regression_must_be_red(sandbox: Path, tmp_path: Path) -> None:
    """M2：最新回归是红的 ⇒ 必须报红（红灯不能当证据）。"""
    regression_root = tmp_path / "regression"
    _make_evidence(regression_root, sandbox, summary=_RED_SUMMARY)
    analysis = TOOL.analyze(str(sandbox), str(regression_root))
    assert analysis.defects, "回归是红的却没人报错 ⇒ 守卫没牙"
    assert "R-回归红" in _defect_checks(analysis.defects), _reasons(analysis.defects)


def test_m3_no_evidence_is_not_applicable(sandbox: Path, tmp_path: Path) -> None:
    """M3：没有回归证据 ⇒ 不适用（rc 2），**不许**当成缺陷。"""
    regression_root = tmp_path / "regression"
    regression_root.mkdir(parents=True, exist_ok=True)
    analysis = TOOL.analyze(str(sandbox), str(regression_root))
    assert not analysis.applicable
    assert not analysis.defects, _reasons(analysis.defects)
    completed = subprocess.run(
        [sys.executable, str(TOOL_PATH), "--project-dir", str(sandbox), "--regression-root", str(regression_root)],
        capture_output=True,
        text=True,
        check=False,
    )
    assert completed.returncode == 2, completed.stdout + completed.stderr
    assert "NOT-APPLICABLE" in completed.stdout


def test_m4_legacy_log_uses_mtime_fallback(sandbox: Path, tmp_path: Path) -> None:
    """M4：旧日志（无证据头）＋ memory mtime 更新 ⇒ 必须报红。"""
    regression_root = tmp_path / "regression"
    log_dir = regression_root / "_regression_legacy"
    log_dir.mkdir(parents=True, exist_ok=True)
    log = log_dir / "pytest.log"
    log.write_text("%s\n" % _GREEN_SUMMARY, encoding="utf-8")
    target = sandbox / "manu" / "memory" / "rules.md"
    # 🔴 时间基准取**日志自身的 mtime**（复制来的 memory mtime 是旧值，
    #    直接 +600 秒未必越得过日志 ⇒ 测不到「mtime 更新」这一情形）。
    stamp = log.stat().st_mtime + 600
    os.utime(target, (stamp, stamp))
    analysis = TOOL.analyze(str(sandbox), str(regression_root))
    assert analysis.defects, "旧口径下改了 memory 却没人报错 ⇒ 守卫没牙"
    assert "R-回归证据过期" in _defect_checks(analysis.defects), _reasons(analysis.defects)


def test_m5_legacy_log_older_memory_is_green(sandbox: Path, tmp_path: Path) -> None:
    """M5（旧日志 ＋ memory 更旧）⇒ 必须 rc 0（不许误伤）。"""
    regression_root = tmp_path / "regression"
    log_dir = regression_root / "_regression_legacy"
    log_dir.mkdir(parents=True, exist_ok=True)
    log = log_dir / "pytest.log"
    log.write_text("%s\n" % _GREEN_SUMMARY, encoding="utf-8")
    analysis = TOOL.analyze(str(sandbox), str(regression_root))
    assert not analysis.defects, _reasons(analysis.defects)


def test_m6_header_wins_over_mtime(sandbox: Path, tmp_path: Path) -> None:
    """M6（反例）：memory 被 touch（mtime 变新）但内容没变 ⇒ 必须仍然 rc 0。"""
    regression_root = tmp_path / "regression"
    log = _make_evidence(regression_root, sandbox)
    target = sandbox / "manu" / "memory" / "memory.md"
    stamp = log.stat().st_mtime + 600
    os.utime(target, (stamp, stamp))
    analysis = TOOL.analyze(str(sandbox), str(regression_root))
    assert not analysis.defects, "只 touch 不改内容却报红 ⇒ 守卫误伤：" + _reasons(analysis.defects)


def test_m7_newest_evidence_selected_by_mtime(sandbox: Path, tmp_path: Path) -> None:
    """M7：证据必须按日志 mtime 取最新（旧的红日志不许盖掉新的绿日志）。"""
    regression_root = tmp_path / "regression"
    old_log = _make_evidence(regression_root, sandbox, summary=_RED_SUMMARY, name="_regression_old")
    new_log = _make_evidence(regression_root, sandbox, summary=_GREEN_SUMMARY, name="_regression_new")
    os.utime(old_log, (1000000, 1000000))
    os.utime(new_log, (2000000, 2000000))
    analysis = TOOL.analyze(str(sandbox), str(regression_root))
    assert analysis.evidence.path == str(new_log), "选错了证据：%s" % analysis.evidence.path
    assert not analysis.defects, _reasons(analysis.defects)


def test_m8_tool_is_read_only() -> None:
    """M8：工具只读 ⇒ 跑完四层 md5 必须一字不变。"""
    before = {rel: _md5(PROJECT_DIR / rel) for rel in _FIXTURE_FILES}
    TOOL.analyze(str(PROJECT_DIR), None)
    after = {rel: _md5(PROJECT_DIR / rel) for rel in _FIXTURE_FILES}
    assert before == after, "只读工具竟然改了 memory 文件"


def test_m9_emit_header_roundtrip(tmp_path: Path) -> None:
    """M9：``--emit-header`` 发射的头必须能被自己的解析器原样读回。"""
    completed = subprocess.run(
        [sys.executable, str(TOOL_PATH), "--emit-header", "--project-dir", str(PROJECT_DIR)],
        capture_output=True,
        text=True,
        check=False,
    )
    assert completed.returncode == 0, completed.stdout + completed.stderr
    header = TOOL.parse_header(completed.stdout)
    assert set(header) == set(_FIXTURE_FILES)
    for rel in _FIXTURE_FILES:
        assert header[rel] == _md5(PROJECT_DIR / rel)


def test_m10_missing_memory_file_is_input_error(tmp_path: Path) -> None:
    """M10：四层缺文件 ⇒ 输入错误（rc 2），不许当成「绿」。"""
    empty = tmp_path / "empty"
    empty.mkdir(parents=True, exist_ok=True)
    # 🔴 先造一份**必然存在**的证据 ⇒ 保证走到「读四层」这一步
    #    （否则在**没有**回归证据的机器上会提前走「不适用」分支，测不到这条）。
    regression_root = tmp_path / "regression"
    log_dir = regression_root / "_regression_20261119_r10"
    log_dir.mkdir(parents=True, exist_ok=True)
    (log_dir / "pytest.log").write_text("%s\n" % _GREEN_SUMMARY, encoding="utf-8")
    with pytest.raises(TOOL.InputError):
        TOOL.analyze(str(empty), str(regression_root))


def test_m11_summary_parsing() -> None:
    """M11：摘要解析必须数对（绿/红/跳过）。"""
    green = TOOL.parse_summary("%s\n" % _GREEN_SUMMARY)
    assert green is not None
    assert green.passed == 1407
    assert green.failed == 0
    assert green.skipped == 7
    assert green.is_green
    red = TOOL.parse_summary("%s\n" % _RED_SUMMARY)
    assert red is not None
    assert red.failed == 1
    assert red.passed == 1406
    assert not red.is_green
    assert TOOL.parse_summary("no tests ran\n") is None
