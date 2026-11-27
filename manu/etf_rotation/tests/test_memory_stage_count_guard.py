"""🔴 2026-11-19 新增：「闸门阶段数 / 位次」这类**计数类口径**的守卫。

【背景】
计数类口径是最容易**悄悄漂移**的一类事实（脚本加了阶段、注释忘了改、memory 抄错了数）
⇒ **没有任何报错**，只会**默默误导**。本轮实测四类真缺陷：

① `manu/etf_rotation/run_gates.sh` 头注释写「26 个探针阶段」/ 段内写「27 个探针阶段」
   ⇒ 实测 `grep -c '^run_stage '` = **28**；
② memory 四层把「26 阶段」当**现口径** ⇒ 实测 **28**；
③ memory 写「26 阶段绿 / 4 阶段红」⇒ 同一轮日志实测 **24 绿 / 4 红 / 1 跳过**（**抄错**）；
④ 位次漂移：`probe_rolling_increment`「第 20 阶段」实测第 **22** 位（另有 3 处）。

【被测对象】
`manu/tools/check_memory_stage_count.py`（**纯只读**闸门，不写任何被检查的文件）。

【测试风格】
照抄本目录既有风格：被测模块用 ``importlib`` 按**路径**加载
（``manu/tools/`` 不在 ``sys.path`` 的默认位置）。
🔴 本文件有**两个镜像位置**（``manu/etf_rotation/tests/`` 与 ``manu/tests/``，
两者必须**逐字节一致**）⇒ **不许**用固定的 ``parents[N]`` 定位 ⇒ 改成向上搜索。

【变异反证（守卫有牙）】
M1 改**现口径**锚点的数字（28 → 26）⇒ 必须报「A-现口径锚点」；
M2 改**执行位次**标注（27 → 26）⇒ 必须报「B-位次自洽」；
M3 改 `run_gates.sh` **头注释**的阶段数 ⇒ 必须报「C-脚本自述」；
M4 把工具里的**动态标签**改回写死 ⇒ 必须报「D-标签不许写死」；
M5 删掉一条**历史锚点** ⇒ 必须报「E-历史不许篡改」（不许靠删历史来「变绿」）；
M6 🔴 **反例**：历史锚点里的「26 阶段」**原样保留** ⇒ 必须**仍然 rc 0**
   （历史行不许被当成现口径，也不许被「订正」）；
M7 复合口径的「绿/红/跳过」与**日志实测**不符 ⇒ 必须报「F-日志对照」。

【live 集成断言】
对**真实工程目录**跑工具 ⇒ 断言 rc == 0（**不许 skip、不许弱化**）。
"""

from __future__ import annotations

import importlib.util
import shutil
import subprocess
import sys
from pathlib import Path

import pytest


def _find_tool() -> Path:
    """向上找 ``manu/tools/check_memory_stage_count.py``。

    🔴 本文件有**两个镜像位置** ⇒ **不许**用固定的 ``parents[N]`` 定位 ⇒ 改成向上搜索。
    """
    for parent in Path(__file__).resolve().parents:
        candidate = parent / "manu" / "tools" / "check_memory_stage_count.py"
        if candidate.is_file():
            return candidate
    raise AssertionError("找不到 manu/tools/check_memory_stage_count.py")


def _find_project_dir() -> Path:
    """向上找工程根（含 ``manu/tools/`` 的那一层）。"""
    for parent in Path(__file__).resolve().parents:
        if (parent / "manu" / "tools" / "check_memory_stage_count.py").is_file():
            return parent
    raise AssertionError("找不到工程根目录")


TOOL_PATH = _find_tool()
PROJECT_DIR = _find_project_dir()


def _load_tool():
    """按路径加载被测工具（``manu/tools/`` 不在默认 ``sys.path`` 上）。"""
    spec = importlib.util.spec_from_file_location("check_memory_stage_count_under_test", TOOL_PATH)
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
    "manu/etf_rotation/run_gates.sh",
    "manu/memory/memory.md",
    "manu/memory/memory_compact.md",
    "manu/memory/.slim_memory.md",
    "manu/memory/rules.md",
    "manu/docs/snapshot_architecture.md",
    "manu/etf_rotation/daily/README.md",
)


@pytest.fixture()
def sandbox(tmp_path: Path) -> Path:
    """把真实工程里被检查的文件复制一份到临时目录（之后只改副本）。"""
    for rel in _FIXTURE_FILES:
        source = PROJECT_DIR / rel
        if not source.is_file():
            continue
        target = tmp_path / rel
        target.parent.mkdir(parents=True, exist_ok=True)
        shutil.copy2(source, target)
    tools_dir = tmp_path / "manu" / "tools"
    tools_dir.mkdir(parents=True, exist_ok=True)
    for source in sorted((PROJECT_DIR / "manu" / "tools").glob("*.sh")):
        shutil.copy2(source, tools_dir / source.name)
    return tmp_path


def _defect_checks(defects) -> set[str]:
    return {defect.check for defect in defects}


def _reasons(defects) -> str:
    return " ｜ ".join(defect.reason for defect in defects)


# --------------------------------------------------------------------------- #
# 基线：真实状态必须是绿的
# --------------------------------------------------------------------------- #
def test_sandbox_baseline_is_green(sandbox: Path) -> None:
    """复制出来的基线必须 rc 0（否则下面的变异反证没有意义）。"""
    defects = TOOL.analyze(str(sandbox))
    assert not defects, _reasons(defects)


def test_live_project_is_green() -> None:
    """live 集成：真实工程目录必须 rc 0（不许 skip、不许弱化）。"""
    completed = subprocess.run(
        [sys.executable, str(TOOL_PATH), "--project-dir", str(PROJECT_DIR)],
        capture_output=True,
        text=True,
        check=False,
    )
    assert completed.returncode == 0, completed.stdout + completed.stderr


# --------------------------------------------------------------------------- #
# 变异反证
# --------------------------------------------------------------------------- #
def test_m1_current_total_anchor_must_match(sandbox: Path) -> None:
    """M1：把现口径锚点的 28 改成 26 ⇒ 必须报红。"""
    target = sandbox / "manu" / "memory" / ".slim_memory.md"
    text = target.read_text(encoding="utf-8")
    assert "现口径：28 阶段" in text
    target.write_text(text.replace("现口径：28 阶段", "现口径：26 阶段"), encoding="utf-8")
    defects = TOOL.analyze(str(sandbox))
    assert defects, "改了现口径阶段数却没人报错 ⇒ 守卫没牙"
    assert "A-现口径锚点" in _defect_checks(defects), _reasons(defects)


def test_m2_position_annotation_must_match(sandbox: Path) -> None:
    """M2：把执行位次标注改错 ⇒ 必须报红。"""
    target = sandbox / "manu" / "memory" / "memory.md"
    text = target.read_text(encoding="utf-8")
    assert "第 27 位（执行位次；添加序号 26）" in text
    target.write_text(
        text.replace("第 27 位（执行位次；添加序号 26）", "第 26 位（执行位次；添加序号 26）"),
        encoding="utf-8",
    )
    defects = TOOL.analyze(str(sandbox))
    assert defects, "改错执行位次却没人报错 ⇒ 守卫没牙"
    assert "B-位次自洽" in _defect_checks(defects), _reasons(defects)


def test_m3_script_header_total_must_match(sandbox: Path) -> None:
    """M3：把脚本头注释的阶段数改错 ⇒ 必须报红。"""
    target = sandbox / "manu" / "etf_rotation" / "run_gates.sh"
    text = target.read_text(encoding="utf-8")
    assert "28 个阶段" in text
    target.write_text(text.replace("28 个阶段", "26 个阶段", 1), encoding="utf-8")
    defects = TOOL.analyze(str(sandbox))
    assert defects, "头注释阶段数陈旧却没人报错 ⇒ 守卫没牙"
    assert "C-脚本自述" in _defect_checks(defects), _reasons(defects)


def test_m4_hardcoded_tool_label_must_be_flagged(sandbox: Path) -> None:
    """M4：把**活闸门**动态标签改回写死的「活闸门（V2 · 4 阶段）」⇒ 必须报红。

    🔴 2026-11-27（活闸门切 V2）：编排器的闸门步现在有**两个**标签形态 ——
    ① `活闸门（V2 · ${V2_STAGES} 阶段）`（**活闸门** = 唯一活的入口）
    ② `V1 全量闸门（已弃用 · 归因留档 · ${GATES_STAGES} 阶段）`（**已弃用**）
    两者都**必须**动态派生阶段数 ⇒ M4/M4b 各钉一个。
    """
    target = sandbox / "manu" / "tools" / "server_full_pipeline_run.sh"
    text = target.read_text(encoding="utf-8")
    anchor = 'record "活闸门（V2 · ${V2_STAGES} 阶段）"'
    assert anchor in text, "活闸门动态标签不在编排器里 ⇒ 锚点过期（先订正锚点）"
    target.write_text(
        text.replace(anchor, 'record "活闸门（V2 · 4 阶段）"'),
        encoding="utf-8",
    )
    defects = TOOL.analyze(str(sandbox))
    assert defects, "写死标签却没人报错 ⇒ 守卫没牙"
    assert "D-标签不许写死" in _defect_checks(defects), _reasons(defects)


def test_m4b_hardcoded_v1_label_must_be_flagged(sandbox: Path) -> None:
    """M4b：把 **V1（已弃用）** 标签写死 ⇒ 同样必须报红（不许只护活闸门）。"""
    target = sandbox / "manu" / "tools" / "server_full_pipeline_run.sh"
    text = target.read_text(encoding="utf-8")
    anchor = 'record "V1 全量闸门（已弃用 · 归因留档 · ${GATES_STAGES} 阶段）"'
    assert anchor in text, "V1 归因留档标签不在编排器里 ⇒ 锚点过期（先订正锚点）"
    target.write_text(
        text.replace(
            anchor,
            'record "V1 全量闸门（已弃用 · 归因留档 · 28 阶段）"',
        ),
        encoding="utf-8",
    )
    defects = TOOL.analyze(str(sandbox))
    assert defects, "写死 V1 标签却没人报错 ⇒ 守卫没牙"
    assert "D-标签不许写死" in _defect_checks(defects), _reasons(defects)


def test_m5_historical_anchor_must_survive(sandbox: Path) -> None:
    """M5：删掉一条历史锚点 ⇒ 必须报红（不许靠删历史来「变绿」）。"""
    target = sandbox / "manu" / "memory" / "memory.md"
    text = target.read_text(encoding="utf-8")
    snippet = "自动化 **16 步流水线 + 21 阶段闸门**"
    assert snippet in text
    target.write_text(text.replace(snippet, "（历史行被删）"), encoding="utf-8")
    defects = TOOL.analyze(str(sandbox))
    assert defects, "历史锚点被删却没人报错 ⇒ 守卫没牙"
    assert "E-历史不许篡改" in _defect_checks(defects), _reasons(defects)


def test_m6_historical_numbers_must_not_be_flagged(sandbox: Path) -> None:
    """M6（反例）：历史锚点里的旧数字**原样保留** ⇒ 必须仍然 rc 0。"""
    defects = TOOL.analyze(str(sandbox))
    assert not defects, _reasons(defects)
    text = (sandbox / "manu" / "memory" / "memory.md").read_text(encoding="utf-8")
    assert "21 阶段闸门" in text, "历史行被误删 ⇒ 守卫误伤历史"


def test_m7_composite_counts_must_match_log(sandbox: Path, tmp_path: Path) -> None:
    """M7：复合口径的「绿/红/跳过」与日志实测不符 ⇒ 必须报红。"""
    log = tmp_path / "gates.log"
    log.write_text(
        "STAGE a=0 —— 绿\n"
        "STAGE b=1 —— 红\n"
        "STAGE c=SKIPPED —— 跳过\n",
        encoding="utf-8",
    )
    defects = TOOL.analyze(str(sandbox), gates_log=str(log))
    assert defects, "绿/红/跳过与日志不符却没人报错 ⇒ 守卫没牙"
    assert "F-日志对照" in _defect_checks(defects), _reasons(defects)


def test_m8_log_parsing_counts_entries() -> None:
    """M8：日志解析本身要能数对（绿/红/跳过/不适用/总条目）。"""
    log_text = (
        "STAGE a=0 —— 绿\n"
        "STAGE b=1 —— 红\n"
        "STAGE c=SKIPPED —— 跳过\n"
        "STAGE d=2 —— 不适用\n"
        "STAGE e=0 —— 绿\n"
    )
    facts = TOOL.parse_gates_log(log_text)
    assert facts.entries == 5
    assert facts.green == 2
    assert facts.red == 1
    assert facts.skipped == 1
    assert facts.not_applicable == 1


def test_m9_stage_measurement_is_source_of_truth() -> None:
    """M9：阶段总数必须从 ``run_gates.sh`` 实测（不许写死）。"""
    script = "run_stage a \"x\"\nrun_stage b \"y\"\nrun_stage probe_c \"z\"\n"
    facts = TOOL.measure_run_gates(script)
    assert facts.total == 3
    assert facts.probe_count == 1
    assert facts.stage_names == ["a", "b", "probe_c"]
    assert facts.position_of("b") == 2
    assert facts.position_of("nope") is None


def test_m10_missing_script_is_input_error() -> None:
    """M10：``run_gates.sh`` 读不到 ⇒ 必须是**用法/输入错误**（退出码 2），不是「无缺陷」。"""
    with pytest.raises(TOOL.InputError):
        TOOL.analyze("/nonexistent-project-dir-for-stage-count-guard")
