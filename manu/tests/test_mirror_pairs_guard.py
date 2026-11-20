"""🔴 2026-11-19 新增：「镜像纪律」（文件层 ＋ **git 层成对放行**）的守卫。

【背景】
`rules.md §十四.125`（用户亲定铁律）：
  ① **镜像对的白名单必须「成对」写**（`manu/<X>` 与 `manu/etf_rotation/<X>` 要么都进、要么都不进）；
  ② **镜像纪律必须验到「git 层」**，不只验「文件层」——文件层 `diff` 一致**不足以**证明成立。
🔴 此前这两条**只有人守**（每轮手工 `diff`/`cmp` 扫一遍）⇒ 而「白名单只放行了一半」这种形态
**本地永远测不出来**（两边都在本地）⇒ 提交后仓库里只剩一半 ⇒ **新克隆缺文件**，
镜像纪律在版本库里断了（§十四.125 记载的真缺陷【二十】即此形态；修过但**没有机器闸门** ⇒ 必然复发）。

【被测对象】
`manu/tools/check_mirror_pairs.py`（**纯只读**闸门，不写、不改任何被检查的文件）。

【测试风格】
照抄同目录既有风格：被测模块用 ``importlib`` 按**路径**加载
（``manu/tools/`` 不在 ``sys.path`` 的默认位置）。
🔴 本文件有**两个镜像位置**（``manu/etf_rotation/tests/`` 与 ``manu/tests/``，
两者必须**逐字节一致**）⇒ **不许**用固定的 ``parents[N]`` 定位 ⇒ 改成向上搜索。
🔴 夹具必须**自带 git 工作区**（`git check-ignore` 只在工作区里有意义）⇒ 沙箱里 `git init`。

【变异反证（守卫有牙）】
M1 改**一侧**内容 ⇒ 必须报「B-逐字一致」；
M2 删掉**白名单点名**的那一侧 ⇒ 必须报「A-成对存在」（【二十】那个真缺陷的形态）；
M3 只给**一侧**加 `!` 放行 ⇒ 必须报「D-白名单成对」（且**不许**连带报 C：两侧都「不被忽略」）；
M4 给**一侧**加忽略规则 ⇒ 必须报「C-入库状态成对」（提交后仓库里只有一半）；
M5 删掉**豁免目录** ⇒ 必须报「E-豁免清单陈旧」（不许让豁免变成逃逸口）；
M6 🔴 **反例**：`manu/daily/`（**已登记过期副本**）两侧**确实不一致** ⇒ 必须**零误伤**（rc 0）；
M7 工具**只读** ⇒ 跑完两侧 md5 必须一字不变。

【live 集成断言】
对**真实工程目录**跑工具 ⇒ 断言 rc == 0（**不许 skip、不许弱化**）。
"""

from __future__ import annotations

import hashlib
import importlib.util
import shutil
import subprocess
import sys
from pathlib import Path

import pytest


def _find_tool() -> Path:
    """向上找 ``manu/tools/check_mirror_pairs.py``。

    🔴 本文件有**两个镜像位置** ⇒ **不许**用固定的 ``parents[N]`` 定位 ⇒ 改成向上搜索。
    """
    for parent in Path(__file__).resolve().parents:
        candidate = parent / "manu" / "tools" / "check_mirror_pairs.py"
        if candidate.is_file():
            return candidate
    raise AssertionError("找不到 manu/tools/check_mirror_pairs.py")


def _find_project_dir() -> Path:
    """向上找工程根（含 ``manu/tools/`` 的那一层）。"""
    for parent in Path(__file__).resolve().parents:
        if (parent / "manu" / "tools" / "check_mirror_pairs.py").is_file():
            return parent
    raise AssertionError("找不到工程根目录")


TOOL_PATH = _find_tool()
PROJECT_DIR = _find_project_dir()


def _load_tool():
    """按路径加载被测工具（``manu/tools/`` 不在默认 ``sys.path`` 上）。"""
    spec = importlib.util.spec_from_file_location("check_mirror_pairs_under_test", TOOL_PATH)
    assert spec and spec.loader
    module = importlib.util.module_from_spec(spec)
    # 🔴 必须先注册进 ``sys.modules``：被测模块用了 ``@dataclass`` ＋
    #    ``from __future__ import annotations`` ⇒ dataclasses 会回查
    #    ``sys.modules[cls.__module__].__dict__`` ⇒ 不注册会直接 AttributeError。
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


TOOL = _load_tool()

#: 沙箱里要复制的两棵树
_MIRROR_TREE = "manu/etf_rotation"


def _md5(path: Path) -> str:
    hasher = hashlib.md5()
    hasher.update(path.read_bytes())
    return hasher.hexdigest()


@pytest.fixture()
def sandbox(tmp_path: Path) -> Path:
    """把真实工程复制一份到临时目录（之后只改副本）。

    🔴 沙箱必须**自带 git 工作区**（`git check-ignore` 只在工作区里有意义）。
    """
    shutil.copy2(PROJECT_DIR / ".gitignore", tmp_path / ".gitignore")
    shutil.copytree(
        PROJECT_DIR / _MIRROR_TREE,
        tmp_path / _MIRROR_TREE,
        ignore=shutil.ignore_patterns("__pycache__", "*.pyc", ".pytest_cache"),
    )
    # 镜像头：只复制「有镜像伙伴」的那一侧（其余文件与镜像纪律无关）
    head = tmp_path / "manu"
    head.mkdir(parents=True, exist_ok=True)
    for source in (tmp_path / _MIRROR_TREE).rglob("*"):
        if not source.is_file():
            continue
        relative = source.relative_to(tmp_path / _MIRROR_TREE)
        target = head / relative
        if target.exists():
            continue
        target.parent.mkdir(parents=True, exist_ok=True)
        shutil.copy2(source, target)
    # 豁免目录（已登记过期副本）单独复制一份
    daily = PROJECT_DIR / "manu" / "daily"
    if daily.is_dir():
        shutil.copytree(
            daily,
            head / "daily",
            ignore=shutil.ignore_patterns("__pycache__", "*.pyc", ".pytest_cache"),
            dirs_exist_ok=True,
        )
    completed = subprocess.run(
        ["git", "init", "-q"], cwd=str(tmp_path), capture_output=True, text=True, check=False
    )
    assert completed.returncode == 0, completed.stdout + completed.stderr
    return tmp_path


def _defect_checks(defects) -> set:
    return {defect.check for defect in defects}


def _reasons(defects) -> str:
    return " ｜ ".join(defect.reason for defect in defects)


# --------------------------------------------------------------------------- #
# 基线：真实状态必须是绿的
# --------------------------------------------------------------------------- #
def test_sandbox_baseline_is_green(sandbox: Path) -> None:
    """复制出来的基线必须 rc 0（否则下面的变异反证没有意义）。"""
    defects, facts = TOOL.analyze(str(sandbox))
    assert not defects, _reasons(defects)
    assert facts.pairs > 0


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
def test_m1_one_sided_content_drift_must_be_flagged(sandbox: Path) -> None:
    """M1：只改一侧内容 ⇒ 必须报红（改一边必须同步另一边）。"""
    target = sandbox / "manu" / "etf_rotation" / "run_gates.sh"
    assert target.is_file(), "夹具里没有 run_gates.sh ⇒ 变异打不到点上"
    target.write_text(target.read_text(encoding="utf-8") + "\n# 变异：只改了一侧\n", encoding="utf-8")
    defects, _ = TOOL.analyze(str(sandbox))
    assert defects, "镜像对漂移却没人报错 ⇒ 守卫没牙"
    assert "B-逐字一致" in _defect_checks(defects), _reasons(defects)


def test_m2_whitelisted_side_missing_must_be_flagged(sandbox: Path) -> None:
    """M2：删掉**白名单点名**的那一侧 ⇒ 必须报红（【二十】那个真缺陷的形态）。"""
    target = sandbox / "manu" / "data" / "account_snapshot.csv"
    assert target.is_file(), "夹具里没有 account_snapshot.csv ⇒ 变异打不到点上"
    target.unlink()
    defects, _ = TOOL.analyze(str(sandbox))
    assert defects, "白名单点名的一侧缺失却没人报错 ⇒ 守卫没牙"
    assert "A-成对存在" in _defect_checks(defects), _reasons(defects)


def test_m3_whitelist_must_be_paired(sandbox: Path) -> None:
    """M3：只给一侧加 `!` 放行 ⇒ 必须报「D-白名单成对」（且**不许**连带报 C）。"""
    gitignore = sandbox / ".gitignore"
    gitignore.write_text(
        gitignore.read_text(encoding="utf-8") + "\n!manu/etf_rotation/run_gates.sh\n",
        encoding="utf-8",
    )
    defects, _ = TOOL.analyze(str(sandbox))
    assert defects, "白名单只放行一侧却没人报错 ⇒ 守卫没牙"
    checks = _defect_checks(defects)
    assert "D-白名单成对" in checks, _reasons(defects)
    assert "C-入库状态成对" not in checks, "两侧都「不被忽略」⇒ 不该连带报 C：" + _reasons(defects)


def test_m4_ignore_status_must_be_paired(sandbox: Path) -> None:
    """M4：给一侧加忽略规则 ⇒ 必须报「C-入库状态成对」（提交后仓库里只有一半）。"""
    gitignore = sandbox / ".gitignore"
    gitignore.write_text(
        gitignore.read_text(encoding="utf-8")
        + "\nmanu/etf_rotation/data/account_snapshot.csv\n",
        encoding="utf-8",
    )
    defects, _ = TOOL.analyze(str(sandbox))
    assert defects, "两侧入库状态不一致却没人报错 ⇒ 守卫没牙"
    assert "C-入库状态成对" in _defect_checks(defects), _reasons(defects)


def test_m5_exempt_list_must_still_exist(sandbox: Path) -> None:
    """M5：豁免目录被删掉 ⇒ 必须报「E-豁免清单陈旧」（不许让豁免变成逃逸口）。"""
    daily = sandbox / "manu" / "daily"
    assert daily.is_dir(), "夹具里没有豁免目录 manu/daily ⇒ 变异打不到点上"
    shutil.rmtree(daily)
    defects, _ = TOOL.analyze(str(sandbox))
    assert defects, "豁免清单陈旧却没人报错 ⇒ 守卫没牙"
    assert "E-豁免清单陈旧" in _defect_checks(defects), _reasons(defects)


def test_m6_exempt_stale_copy_must_not_be_flagged(sandbox: Path) -> None:
    """M6（反例）：`manu/daily/`（**已登记过期副本**）两侧**确实不一致** ⇒ 必须仍然 rc 0。"""
    target = sandbox / "manu" / "daily" / "main.py"
    assert target.is_file(), "夹具里没有 manu/daily/main.py ⇒ 变异打不到点上"
    target.write_text(target.read_text(encoding="utf-8") + "\n# 变异：过期副本本来就与镜像不一致\n", encoding="utf-8")
    defects, _ = TOOL.analyze(str(sandbox))
    assert not defects, "已登记过期副本被误判成镜像漂移 ⇒ 守卫误伤：" + _reasons(defects)


def test_m7_tool_is_read_only() -> None:
    """M7：工具只读 ⇒ 跑完两侧 md5 必须一字不变。"""
    left = PROJECT_DIR / "manu" / "run_gates.sh"
    right = PROJECT_DIR / "manu" / "etf_rotation" / "run_gates.sh"
    before = (_md5(left), _md5(right))
    TOOL.analyze(str(PROJECT_DIR))
    assert before == (_md5(left), _md5(right)), "只读工具竟然改了镜像文件"


def test_m8_not_a_git_worktree_is_not_applicable(tmp_path: Path) -> None:
    """M8：不在 git 工作区 ⇒ **不适用**（rc 2），不许当成缺陷、不许当成绿。"""
    plain = tmp_path / "plain"
    (plain / "manu" / "etf_rotation").mkdir(parents=True)
    (plain / "manu" / "etf_rotation" / "foo.py").write_text("x = 1\n", encoding="utf-8")
    (plain / "manu" / "foo.py").write_text("x = 1\n", encoding="utf-8")
    with pytest.raises(TOOL.NotApplicable):
        TOOL.analyze(str(plain))
