"""🔴 2026-11-19 新增：「产物落点双根化 ＋ 痕迹清零」验收闸门的守卫。

【背景】
`manu/memory/rules.md §十六` 第 7 条原文：「**验收方式（闸门 / 自检必须能断言，不许只写在文档里）**」，
并逐条点名三条**必须能断言**的条件：
  ① 本项目在服务器上的文件**全部位于** `<DATASET_ROOT>` **或** `<RESULTS_ROOT>` 之下；
  ② `/mnt/data` 根**没有** `_etf_daily_*`；`/mnt/data/siping/datasets/` 顶层**没有**本项目目录；
  ③ `/mnt/data/siping/runs/` 下**本项目专属**目录**只存在** `<RESULTS_ROOT>` **一个**
     （别人的 `nogmc_*` / `npu20ms_*` / `ashare_topk` / `CANDIDATES` / `_ms6` / `_ms7`
     **不计、也不许删**）；
另加 §十六 第 4 条 2026-11-19 夜补：「**`/tmp` 里的本项目件同样必须清零**」。
🔴 这几条此前**只有人守**（既有工具只覆盖**写入端常量**与**仓库内载体**，
**没有任何工具读回文件系统去断言「落点真的只剩双根」**）⇒ 落点漂了没有任何闸门能报。

【被测对象】
`manu/tools/check_artifact_placement.py`（**纯只读**闸门：不写、不删、不动任何被检查的文件）。
退出码：0 = 无缺陷；1 = 有缺陷；2 = 输入错误；``analyze()`` 返回 ``(defects, facts)``。

【测试风格】
照抄同目录 `test_record_carrier_guard.py`：被测模块用 ``importlib`` 按**路径**加载
（``manu/tools/`` 不在 ``sys.path`` 的默认位置）。
🔴 本文件有**两个镜像位置**（``manu/etf_rotation/tests/`` 与 ``manu/tests/``，必须**逐字节一致**）
⇒ **不许**用固定的 ``parents[N]`` 定位 ⇒ 改成向上搜索。

【沙箱怎么造（🔴 不许给被测工具加 CLI 参数 —— 那会削弱守卫）】
沙箱靠 **monkeypatch 工具的模块级常量**：
``MOUNT_ROOT`` / ``SIPING_ROOT`` / ``DATASETS_SHARED_ROOT`` / ``RUNS_SHARED_ROOT`` /
``TMP_ROOT`` / ``DEFAULT_DATASET_ROOT`` / ``DEFAULT_RESULTS_ROOT`` / ``ALLOWED_EXTERNAL_LITERALS``。
沙箱把真实布局映射成 ``<tmp>/mnt/data/...``（与 ``/mnt/data/...`` **同构**），
并**真的把** ``mnt/data`` / ``mnt/data/siping/runs`` 建出来
⇒ 服务器侧断言（A2/A3/A4/A5）在沙箱里**真的执行**（不许被「不适用」蒙过去）。
双根用 ``analyze()`` 的**参数**直接传沙箱路径（优先用参数，不只靠 env）。

【变异反证（守卫要有牙）】
M1 沙箱基线绿；M2 真实工程绿（不许 skip、不许弱化）；
M3 `/mnt/data` 根 `_etf_daily_*` ⇒ 报 A2；M4 `datasets/` 顶层本项目目录 ⇒ 报 A3；
M5 `datasets/qlib/cn_data_live` ⇒ 报 A3；M6 `runs/` 顶层第二个本项目目录 ⇒ 报 A4；
M7 **零误伤**：别人的链一条都不许报；M8 `/tmp` 痕迹 ⇒ 报 A5，但 `pytest-of-*` 与隐藏项**豁免**；
M9 双根自洽：合一 / 互相嵌套 / 落在散落旧位置 ⇒ 报 A1；
M10 owner 落点字面量越界 / 报告落点回仓库 ⇒ 报 A6；M11 双 env 派生被破坏 ⇒ 报 A6；
M12 工具**只读** ⇒ 沙箱树 + owner 文件的 md5 前后一字不变；
M13 **接线反证**（走**整闸门** `check()`）：假缺陷 ⇒ rc=1 ＋ stderr 点名；恢复 ⇒ rc=0；
M14 允许的那一个（`<RESULTS_ROOT>` 自己）**不许**被 A4 误报；
M8b **内容口径**：名字毫无归属证据、内容含 `etf_rotation` ⇒ 报 A5（删掉它 ⇒ 本轮核心修复就没牙）；
M8c **目录子项口径**：目录名无关、子项名命中归属证据 ⇒ 报 A5；
M8d **别的链零误伤**（共用词元 `manu` / `qlib` ⇒ 必须再取证才计本项目）；
M10c **裸 `/mnt/data`** 字面量 ⇒ 报 A6（§十六 第 2 条点名的历史违例原形）；
M15 **owner 清单不许被悄悄截断** ⇒ 必须与 `rules.md §十六` 正文解析出来的清单一致。
"""

from __future__ import annotations

import hashlib
import importlib.util
import os
import re
import subprocess
import sys
import time
from pathlib import Path

import pytest


def _find_tool() -> Path:
    """向上找 ``manu/tools/check_artifact_placement.py``。

    🔴 本文件有**两个镜像位置** ⇒ **不许**用固定的 ``parents[N]`` 定位 ⇒ 改成向上搜索。
    """
    for parent in Path(__file__).resolve().parents:
        candidate = parent / "manu" / "tools" / "check_artifact_placement.py"
        if candidate.is_file():
            return candidate
    raise AssertionError("找不到 manu/tools/check_artifact_placement.py")


def _find_project_dir() -> Path:
    """向上找工程根（含 ``manu/tools/`` 的那一层）。"""
    for parent in Path(__file__).resolve().parents:
        if (parent / "manu" / "tools" / "check_artifact_placement.py").is_file():
            return parent
    raise AssertionError("找不到工程根目录")


TOOL_PATH = _find_tool()
PROJECT_DIR = _find_project_dir()


def _load_tool():
    """按路径加载被测工具（``manu/tools/`` 不在默认 ``sys.path`` 上）。"""
    spec = importlib.util.spec_from_file_location("check_artifact_placement_under_test", TOOL_PATH)
    assert spec and spec.loader
    module = importlib.util.module_from_spec(spec)
    # 🔴 必须先注册进 ``sys.modules``：被测模块用了 ``@dataclass`` ⇒
    #    dataclasses 会回查 ``sys.modules[cls.__module__].__dict__`` ⇒ 不注册会直接 AttributeError。
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


TOOL = _load_tool()


def _md5(path: Path) -> str:
    hasher = hashlib.md5()
    hasher.update(path.read_bytes())
    return hasher.hexdigest()


def _tree_md5(root: Path) -> dict:
    """整棵树的逐文件 md5（测试用它证明「只读」）。"""
    return {
        str(item.relative_to(root)): _md5(item)
        for item in sorted(root.rglob("*"))
        if item.is_file()
    }


def _defect_checks(defects) -> set:
    return {defect.check for defect in defects}


def _reasons(defects) -> str:
    return " ｜ ".join(defect.reason for defect in defects)


class Sandbox:
    """沙箱的路径台账（全部落在 ``tmp_path`` 之下 ⇒ 跑完自动清理）。"""

    def __init__(self, root: Path) -> None:
        self.root = root
        #: 工程根（被检查方）
        self.project_dir = root / "project"
        #: 假服务器根（与真实 `/mnt/data` **同构**）
        self.mount_root = root / "mnt" / "data"
        self.siping_root = self.mount_root / "siping"
        self.datasets_root = self.siping_root / "datasets"
        self.runs_root = self.siping_root / "runs"
        #: 假 `/tmp`
        self.tmp_root = root / "tmp"
        #: 双根（合法形态：`datasets/<命名空间>/<项目>` ＋ `runs/` 的直接子目录）
        self.dataset_root = self.datasets_root / "manu" / "etf_rotation"
        self.results_root = self.runs_root / "etf_rotation_results"

    @property
    def owner_files(self) -> dict:
        """owner 清单（相对路径 ⇒ 沙箱工程里的绝对路径）。"""
        return {relative: self.project_dir / relative for relative in TOOL.OWNER_FILES}

    def owner_file(self, relative: str) -> Path:
        return self.project_dir / relative


def _daily_config_source(box: Sandbox, with_results_env: bool = True, extra: str = "") -> str:
    """合成 ``daily_config.py``：**必须**含两次 ``os.environ.get``（双 env 派生）。

    🔴 A6b 要求 ``daily_config.py`` 里查得到 ``ETF_ROTATION_OUTPUT_ROOT`` 与
       ``ETF_ROTATION_RESULTS_ROOT`` ⇒ 少了任一个 ⇒ 必须报「A6/双 env 派生被破坏」。
    """
    blocks = [
        "ETF_ROTATION_OUTPUT_ROOT = Path(\n"
        f'    os.environ.get("ETF_ROTATION_OUTPUT_ROOT", "{box.dataset_root}")\n'
        ")\n"
    ]
    if with_results_env:
        blocks.append(
            "ETF_ROTATION_RESULTS_ROOT = Path(\n"
            f'    os.environ.get("ETF_ROTATION_RESULTS_ROOT", "{box.results_root}")\n'
            ")\n"
        )
    return (
        '"""合成 owner 文件（沙箱专用）：只保留「双 env 派生」这条底线。"""\n'
        "import os\n"
        "from pathlib import Path\n"
        "\n"
        + "".join(blocks)
        + extra
    )


#: 合成 owner 文件（不含任何落点字面量 ⇒ 不许凭空造缺陷）
_OTHER_OWNER_SOURCE = (
    '"""合成 owner 文件（沙箱专用）：不含任何落点字面量。"""\n'
    "from pathlib import Path\n"
    "\n"
    "_HERE = Path(__file__).resolve().parent\n"
)


def _write_owner_files(box: Sandbox) -> None:
    """在沙箱工程里铺好 5 个 owner 文件（内容可被各用例就地改写）。"""
    for relative, path in box.owner_files.items():
        path.parent.mkdir(parents=True, exist_ok=True)
        if relative == TOOL.DUAL_ENV_FILE:
            path.write_text(_daily_config_source(box), encoding="utf-8")
        else:
            path.write_text(_OTHER_OWNER_SOURCE, encoding="utf-8")


@pytest.fixture()
def sandbox(tmp_path: Path, monkeypatch) -> Sandbox:
    """造一棵**自洽的假服务器 ＋ 假工程**，并把被测工具的路径常量指过去。

    🔴 服务器侧断言（A2/A3/A4/A5）只在 ``RUNS_SHARED_ROOT`` 与 ``MOUNT_ROOT``
       **都存在**时才执行 ⇒ 沙箱必须把这两层**真的建出来**（不许「不适用」蒙过去）。
    """
    box = Sandbox(tmp_path)
    for directory in (box.dataset_root, box.results_root, box.tmp_root):
        directory.mkdir(parents=True, exist_ok=True)
    _write_owner_files(box)

    monkeypatch.setattr(TOOL, "MOUNT_ROOT", str(box.mount_root))
    monkeypatch.setattr(TOOL, "SIPING_ROOT", str(box.siping_root))
    monkeypatch.setattr(TOOL, "DATASETS_SHARED_ROOT", str(box.datasets_root))
    monkeypatch.setattr(TOOL, "RUNS_SHARED_ROOT", str(box.runs_root))
    monkeypatch.setattr(TOOL, "TMP_ROOT", str(box.tmp_root))
    # 双根的**默认值**也指到沙箱 ⇒ owner 里的落点字面量仍算「合法落点」
    monkeypatch.setattr(TOOL, "DEFAULT_DATASET_ROOT", str(box.dataset_root))
    monkeypatch.setattr(TOOL, "DEFAULT_RESULTS_ROOT", str(box.results_root))
    monkeypatch.setattr(
        TOOL, "ALLOWED_EXTERNAL_LITERALS", (str(box.siping_root / "secrets" / "smtp.env"),)
    )
    return box


def _analyze(box: Sandbox, dataset_root=None, results_root=None):
    """跑被测工具（双根**用参数**直接传沙箱路径 ⇒ 不依赖 env）。"""
    return TOOL.analyze(
        str(box.project_dir),
        str(box.dataset_root if dataset_root is None else dataset_root),
        str(box.results_root if results_root is None else results_root),
    )


# --------------------------------------------------------------------------- #
# 基线：真实状态必须是绿的
# --------------------------------------------------------------------------- #
def test_m1_sandbox_baseline_is_green(sandbox: Sandbox) -> None:
    """M1：沙箱基线必须零缺陷（否则下面的变异反证没有意义）。"""
    defects, facts = _analyze(sandbox)
    assert not defects, _reasons(defects)
    assert facts.server_scope, "沙箱没进服务器侧断言 ⇒ A2/A3/A4/A5 根本没跑"
    assert facts.owner_files == len(TOOL.OWNER_FILES)
    assert facts.owner_dual_env_ok
    assert facts.datasets_namespace == "manu"


def test_m2_live_project_is_green() -> None:
    """M2：live 集成：真实工程目录必须 rc 0（不许 skip、不许弱化）。"""
    completed = subprocess.run(
        ["python3", str(TOOL_PATH), "--project-dir", str(PROJECT_DIR)],
        cwd=str(PROJECT_DIR),
        capture_output=True,
        text=True,
        check=False,
    )
    assert completed.returncode == 0, completed.stdout + completed.stderr


# --------------------------------------------------------------------------- #
# 变异反证：服务器侧断言（A2 / A3 / A4 / A5）
# --------------------------------------------------------------------------- #
def test_m3_legacy_dir_under_mount_root_must_be_flagged(sandbox: Sandbox) -> None:
    """M3：`/mnt/data` 根出现 `_etf_daily_*` ⇒ 必须报「A2/散落旧位置」。"""
    (sandbox.mount_root / "_etf_daily_20261105").mkdir(parents=True)
    defects, facts = _analyze(sandbox)
    assert defects, "根目录下的旧工作目录却没人报错 ⇒ 守卫没牙"
    assert "A2/散落旧位置" in _defect_checks(defects), _reasons(defects)
    assert facts.legacy_work_dirs == 1


def test_m4_project_dir_at_datasets_top_level_must_be_flagged(sandbox: Sandbox) -> None:
    """M4：`datasets/` 顶层出现本项目目录 ⇒ 必须报「A3/datasets 顶层本项目目录」。"""
    (sandbox.datasets_root / "etf").mkdir(parents=True)
    defects, facts = _analyze(sandbox)
    assert defects, "datasets/ 顶层多出本项目目录却没人报错 ⇒ 守卫没牙"
    assert "A3/datasets 顶层本项目目录" in _defect_checks(defects), _reasons(defects)
    assert facts.datasets_project_dirs == 1


def test_m5_legacy_qlib_subdir_must_be_flagged(sandbox: Sandbox) -> None:
    """M5：`datasets/qlib/cn_data_live`（§十六 点名的历史违例实例）⇒ 必须报 A3。"""
    (sandbox.datasets_root / "qlib" / "cn_data_live").mkdir(parents=True)
    defects, _ = _analyze(sandbox)
    assert defects, "历史违例实例却没人报错 ⇒ 守卫没牙"
    assert "A3/datasets 顶层本项目目录" in _defect_checks(defects), _reasons(defects)


def test_m6_second_project_dir_under_runs_must_be_flagged(sandbox: Sandbox) -> None:
    """M6：`runs/` 顶层出现**第二个**本项目目录 ⇒ 必须报「A4/runs 顶层多出本项目目录」。"""
    (sandbox.runs_root / "etf_rotation").mkdir(parents=True)
    defects, facts = _analyze(sandbox)
    assert defects, "runs/ 顶层多出本项目目录却没人报错 ⇒ 守卫没牙"
    assert "A4/runs 顶层多出本项目目录" in _defect_checks(defects), _reasons(defects)
    assert facts.runs_project_dirs == 1


def test_m7_other_chains_must_not_be_flagged(sandbox: Sandbox) -> None:
    """M7：**零误伤** —— 别人的链（§十六 点名的那几族）一条缺陷都不许报。"""
    for name in ("nogmc_focus", "ashare_topk", "npu20ms_x", "CANDIDATES", "_ms6", "_ms7"):
        (sandbox.runs_root / name).mkdir(parents=True)
    defects, facts = _analyze(sandbox)
    assert not defects, "别人的链被误判成违规 ⇒ 守卫误伤：" + _reasons(defects)
    assert facts.runs_other_chains == 6
    assert facts.runs_project_dirs == 0


def test_m8_tmp_trace_must_be_flagged_but_exemptions_must_not(sandbox: Sandbox) -> None:
    """M8：`/tmp` 里的本项目件必须报；`pytest-of-*` 与隐藏项**豁免**。"""
    (sandbox.tmp_root / "etf_rotation_scratch").mkdir(parents=True)
    (sandbox.tmp_root / "pytest-of-huangzhe").mkdir(parents=True)
    (sandbox.tmp_root / ".manu_known_hosts").write_text("x", encoding="utf-8")
    defects, facts = _analyze(sandbox)
    checks = _defect_checks(defects)
    assert "A5//tmp 痕迹未清零" in checks, _reasons(defects)
    flagged = [defect for defect in defects if defect.check == "A5//tmp 痕迹未清零"]
    assert len(flagged) == 1, _reasons(defects)
    assert "etf_rotation_scratch" in flagged[0].preview, flagged[0].preview
    assert facts.tmp_exempt_items == 1, "pytest 自己的 tmp 根被当成项目痕迹 ⇒ 误伤"


# --------------------------------------------------------------------------- #
# 变异反证：双根自洽（A1，跨主机都适用）
# --------------------------------------------------------------------------- #
def test_m9a_dual_root_merged_must_be_flagged(sandbox: Sandbox) -> None:
    """M9a：双根合一（数据集根 == 结果根）⇒ 必须报「A1/双根合一」。"""
    defects, _ = _analyze(sandbox, dataset_root=sandbox.dataset_root, results_root=sandbox.dataset_root)
    assert defects, "双根合一却没人报错 ⇒ 守卫没牙"
    assert "A1/双根合一" in _defect_checks(defects), _reasons(defects)


def test_m9b_dual_root_nested_must_be_flagged(sandbox: Sandbox) -> None:
    """M9b：双根互相嵌套 ⇒ 必须报「A1/双根互相嵌套」（清一个会连带另一个）。"""
    nested = sandbox.results_root / "datasets" / "manu" / "etf_rotation"
    defects, _ = _analyze(sandbox, dataset_root=nested)
    assert defects, "双根互相嵌套却没人报错 ⇒ 守卫没牙"
    assert "A1/双根互相嵌套" in _defect_checks(defects), _reasons(defects)


def test_m9c_dual_root_on_shared_top_level_must_be_flagged(sandbox: Sandbox) -> None:
    """M9c：双根之一落在共享顶层（== `MOUNT_ROOT`）⇒ 必须报「A1/双根落在散落旧位置」。"""
    defects, _ = _analyze(sandbox, dataset_root=sandbox.mount_root)
    assert defects, "双根落在散落旧位置却没人报错 ⇒ 守卫没牙"
    assert "A1/双根落在散落旧位置" in _defect_checks(defects), _reasons(defects)


# --------------------------------------------------------------------------- #
# 变异反证：owner 落点字面量（A6，静态）
# --------------------------------------------------------------------------- #
def test_m10a_owner_literal_out_of_bounds_must_be_flagged(sandbox: Sandbox, monkeypatch) -> None:
    """M10a：owner 里的落点字面量指向「散落旧位置」⇒ 必须报「A6/落点字面量越界」。

    🔴 沙箱把 ``/mnt/data`` 映射成 ``<tmp>/mnt/data``（**同构**）⇒ 注入的字面量
       ``<mount>/_etf_daily_x`` ≡ 真实的 ``/mnt/data/_etf_daily_x``；
       末尾再验一次**字面原形** ``/mnt/data/_etf_daily_x``（把 ``MOUNT_ROOT`` 指回真实根）。
    """
    daily_config = sandbox.owner_file(TOOL.DUAL_ENV_FILE)
    daily_config.write_text(
        _daily_config_source(sandbox, extra=f'SCATTERED = Path("{sandbox.mount_root}/_etf_daily_x")\n'),
        encoding="utf-8",
    )
    defects, _ = _analyze(sandbox)
    assert defects, "落点字面量越界却没人报错 ⇒ 守卫没牙"
    previews = {defect.preview for defect in defects if defect.check == "A6/落点字面量越界"}
    assert str(sandbox.mount_root / "_etf_daily_x") in previews, previews

    # 字面原形：`/mnt/data/_etf_daily_x`（把 ``MOUNT_ROOT`` 指回真实根，只读扫一眼）
    monkeypatch.setattr(TOOL, "MOUNT_ROOT", "/mnt/data")
    daily_config.write_text(
        _daily_config_source(sandbox, extra='SCATTERED = Path("/mnt/data/_etf_daily_x")\n'),
        encoding="utf-8",
    )
    defects, _ = _analyze(sandbox)
    previews = {defect.preview for defect in defects if defect.check == "A6/落点字面量越界"}
    assert "/mnt/data/_etf_daily_x" in previews, previews


def test_m10b_report_literal_must_be_flagged(sandbox: Sandbox) -> None:
    """M10b：owner 里的落点字面量指向仓库内 ``manu/report/`` ⇒ 必须报「A6/报告落点回仓库」。"""
    report_dir = sandbox.project_dir / "manu" / "report"
    sandbox.owner_file(TOOL.DUAL_ENV_FILE).write_text(
        _daily_config_source(sandbox, extra=f'REPORT = Path("{report_dir}")\n'),
        encoding="utf-8",
    )
    defects, _ = _analyze(sandbox)
    assert defects, "报告落点回仓库却没人报错 ⇒ 守卫没牙"
    assert "A6/报告落点回仓库" in _defect_checks(defects), _reasons(defects)


def test_m11_dual_env_derivation_broken_must_be_flagged(sandbox: Sandbox) -> None:
    """M11：``daily_config.py`` 里少了 ``ETF_ROTATION_RESULTS_ROOT`` 的查表 ⇒ 必须报「A6/双 env 派生被破坏」。"""
    sandbox.owner_file(TOOL.DUAL_ENV_FILE).write_text(
        _daily_config_source(sandbox, with_results_env=False),
        encoding="utf-8",
    )
    defects, facts = _analyze(sandbox)
    assert defects, "双 env 派生被破坏却没人报错 ⇒ 守卫没牙"
    checks = _defect_checks(defects)
    assert "A6/双 env 派生被破坏" in checks, _reasons(defects)
    assert "A6/落点字面量越界" not in checks, "双 env 没了 ≠ 落点字面量越界：" + _reasons(defects)
    assert not facts.owner_dual_env_ok


# --------------------------------------------------------------------------- #
# 只读性
# --------------------------------------------------------------------------- #
def test_m12_tool_is_read_only(sandbox: Sandbox) -> None:
    """M12：工具只读 ⇒ 跑完沙箱树 ＋ owner 文件的 md5 必须一字不变（没写、没删）。"""
    before_tree = _tree_md5(sandbox.root)
    before_owner = {relative: _md5(path) for relative, path in sandbox.owner_files.items()}
    assert before_owner, "owner 文件没铺好 ⇒ 本用例失去意义"

    _analyze(sandbox)

    assert before_tree == _tree_md5(sandbox.root), "只读工具竟然改了沙箱树"
    after_owner = {relative: _md5(path) for relative, path in sandbox.owner_files.items()}
    assert before_owner == after_owner, "只读工具竟然改了 owner 文件"


# --------------------------------------------------------------------------- #
# 接线反证：落点闸门必须真的挂在「整闸门」上
# --------------------------------------------------------------------------- #
def _load_sync_module():
    """按路径加载 `manu/tools/sync_slim_memory.py`（落点闸门的接线处）。"""
    path = PROJECT_DIR / "manu" / "tools" / "sync_slim_memory.py"
    spec = importlib.util.spec_from_file_location("sync_slim_memory_under_test", path)
    assert spec and spec.loader
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


def _import_placement_checker():
    """按 ``sync_slim_memory.py`` 里的同一路径导入落点闸门（**同一个模块对象** ⇒ 补丁才生效）。"""
    tools_root = str(PROJECT_DIR / "manu")
    if tools_root not in sys.path:
        sys.path.insert(0, tools_root)
    import tools.check_artifact_placement as checker_mod  # noqa: PLC0415

    return checker_mod


def _isolate_memory_freshness_gate(sync_mod, tmp_path: Path, monkeypatch) -> None:
    """把「精简档新鲜度」一族**与落点闸门无关**的检查隔离开。

    🔴 为什么必须隔离：本用例要断言的是**落点闸门在整闸门里的调用点**
       （把 ``check()`` 里那一行换成 ``[]`` 仍必须能被抓出来 ⇒ 必须走**整闸门**）。
       而 ``check()`` 里还挂着「精简档新鲜度」一族子闸门，它们的红绿取决于
       ``manu/memory/`` 当前**有没有刷新过**（与本守卫无关，且会随别人改动漂移
       ⇒ 当前工作副本的精简档行数标注确实是陈旧的 ⇒ 直接断言 rc==0 会恒红）。
       ⇒ 这里把三份记忆档换成**合成副本**（旧 mtime）、把行数标注改成**现算**、
         并把其余子闸门 stub 成空清单 ⇒ 整闸门的红绿**只由落点闸门决定**。
    """
    synthetic = tmp_path / "synthetic_memory"
    synthetic.mkdir(parents=True, exist_ok=True)
    old = time.time() - 3600
    for name in ("memory.md", "memory_compact.md", "rules.md"):
        path = synthetic / name
        path.write_text("合成副本（与落点闸门无关）\n", encoding="utf-8")
        os.utime(path, (old, old))

    monkeypatch.setattr(sync_mod, "MASTER", synthetic / "memory.md")
    monkeypatch.setattr(sync_mod, "COMPACT", synthetic / "memory_compact.md")
    monkeypatch.setattr(sync_mod, "RULES", synthetic / "rules.md")
    # 行数标注改成**现算**（真实精简档的标注可能陈旧 ⇒ 与本用例无关）
    monkeypatch.setattr(sync_mod, "read_block", lambda _text: sync_mod.build_block())
    for name in (
        "_check_rules_index",
        "_check_stage_count",
        "_check_record_ledger",
        "_check_mirror_pairs",
        "_check_record_carrier",
    ):
        monkeypatch.setattr(sync_mod, name, lambda: [])


def test_m13_wiring_makes_the_whole_gate_red(monkeypatch, capsys, tmp_path: Path) -> None:
    """M13：🔴 **接线反证**：落点闸门有缺陷 ⇒ **整闸门** `check()` 必须 rc=1 且 stderr 点名。

    🔴 为什么不能只测 ``_check_artifact_placement()``：把 ``check()`` 里的调用行换成 ``[]``，
       函数本身照样返回问题清单 ⇒ 只测函数的那条**测不出调用点被摘掉**这一形态
       ⇒ 这里必须走**整闸门** ``check()``（用**假缺陷**验证 ⇒ 不碰真实文件）。
    """
    sync_mod = _load_sync_module()
    _isolate_memory_freshness_gate(sync_mod, tmp_path, monkeypatch)
    checker_mod = _import_placement_checker()
    original_analyze = checker_mod.analyze

    monkeypatch.setattr(
        checker_mod,
        "analyze",
        lambda *args, **kwargs: (
            [
                checker_mod.Defect(
                    "manu/etf_rotation/daily/daily_config.py",
                    1,
                    "Z-假缺陷",
                    "接线反证用的假缺陷",
                    "",
                )
            ],
            checker_mod.PlacementFacts(),
        ),
    )
    exit_code = sync_mod.check()
    captured = capsys.readouterr()
    assert exit_code == 1, "落点有缺陷，整闸门却没红 ⇒ 接线调用点被摘掉了：" + captured.err
    assert "接线反证用的假缺陷" in captured.err, captured.err

    # 恢复 ⇒ 整闸门必须回绿（真实工程上的落点闸门是干净的）
    monkeypatch.setattr(checker_mod, "analyze", original_analyze)
    exit_code = sync_mod.check()
    captured = capsys.readouterr()
    assert exit_code == 0, captured.err + captured.out


def test_m14_allowed_results_root_must_not_be_flagged(sandbox: Sandbox) -> None:
    """M14：**允许的那一个**：`<RESULTS_ROOT>` 自己（`runs/` 顶层的直接子目录）不许被 A4 报成违规。"""
    defects, facts = _analyze(sandbox)
    assert "A4/runs 顶层多出本项目目录" not in _defect_checks(defects), _reasons(defects)
    assert facts.runs_project_dirs == 0
    assert sandbox.results_root.parent == sandbox.runs_root


# --------------------------------------------------------------------------- #
# M8b/M8c/M8d：A5 的「内容口径」「目录子项口径」「别的链零误伤」
# --------------------------------------------------------------------------- #
def test_m8b_content_marker_must_be_flagged_even_when_name_has_no_token(sandbox: Sandbox) -> None:
    """M8b：🔴 **内容口径**：名字**毫无**归属证据、但**内容**含 `etf_rotation` 的件 ⇒ 必须报 A5。

    🔴 这正是本轮修掉的**假绿**真缺陷形态（实测漏检 **24** 件）：
       `v2verbose.log` / `seam_e2e_decision.json` / `write_memory_119.py` / `commit_v2.sh` …（名字与本项目毫无关系）。
    🔴 删掉这条判据 ⇒ 守卫全绿 ⇒ **本轮的核心修复就没有牙齿**。
    """
    (sandbox.tmp_root / "v2verbose.log").write_text(
        "run root = /mnt/data/siping/runs/etf_rotation_results\n", encoding="utf-8"
    )
    defects, _ = _analyze(sandbox)
    assert defects, "内容里写着本项目落点的件却没人报 ⇒ **内容口径**被摘掉了（守卫没牙）"
    assert "A5//tmp 痕迹未清零" in _defect_checks(defects), _reasons(defects)


def test_m8c_child_name_evidence_must_be_flagged(sandbox: Sandbox) -> None:
    """M8c：**目录子项口径**：目录名毫无归属证据、但**子项名**命中归属证据 ⇒ 必须报 A5。"""
    nested = sandbox.tmp_root / "scratch_dir"
    nested.mkdir()
    (nested / "etf_rotation_daily_ledger.csv").write_text("x", encoding="utf-8")
    defects, _ = _analyze(sandbox)
    assert defects, "目录里藏着本项目专属子项却没人报 ⇒ **目录口径**被摘掉了（守卫没牙）"
    assert "A5//tmp 痕迹未清零" in _defect_checks(defects), _reasons(defects)


def test_m8d_other_chain_must_not_be_flagged(sandbox: Sandbox) -> None:
    """M8d：🔴 **零误伤**：别的链的件 ⇒ 一条缺陷都不许报（越权＝事故）。

    🔴 同一用户还有一条**红外弱小无人机检测线**，与本链**共用同一个仓库与 `manu/` 扩展层**
       ⇒ `manu/memory` 是**多链共用**的词根 ⇒ 拿它当归属证据就会把**别人的链**的件判成违规。
    🔴 两种形态都要零误伤：
       ① **内容**只有共用标记（`manu/memory`）、没有链专属标记；
       ② **名字**命中共用词元（`manu_*`）、但没有任何归属证据（**必须再取证**才计本项目）。
    """
    (sandbox.tmp_root / "prompt_first.txt").write_text(
        "主索引：manu/memory/memory.md 与 manu/memory/rules.md\n", encoding="utf-8"
    )
    shared = sandbox.tmp_root / "manu_scratch"
    shared.mkdir()
    (shared / "notes.txt").write_text("别的链的普通笔记\n", encoding="utf-8")
    defects, _ = _analyze(sandbox)
    assert not defects, "别的链的件被误判成违规 ⇒ **越权**：" + _reasons(defects)


# --------------------------------------------------------------------------- #
# M10c：裸 `/mnt/data` 字面量（§十六 第 2 条点名的历史违例原形）
# --------------------------------------------------------------------------- #
def test_m10c_bare_mount_root_literal_must_be_flagged(sandbox: Sandbox) -> None:
    """M10c：🔴 **裸 `/mnt/data`** 字面量也必须报越界。

    🔴 独立审计抓出的真缺陷：A6 只判 `startswith(MOUNT_ROOT + "/")`
       ⇒ **恰好等于 `/mnt/data`** 的字面量（历史违例原形：`Path("/mnt/data")`）**根本进不了判定**
       ⇒ 闸门与守卫**同时假绿**。
    """
    daily_config = sandbox.owner_file(TOOL.DUAL_ENV_FILE)
    daily_config.write_text(
        _daily_config_source(sandbox, extra=f'WORK = Path("{sandbox.mount_root}")\n'),
        encoding="utf-8",
    )
    defects, _ = _analyze(sandbox)
    assert defects, "裸 `/mnt/data` 字面量没人报 ⇒ A6 漏掉了 §十六 点名的历史违例原形"
    assert "A6/落点字面量越界" in _defect_checks(defects), _reasons(defects)


# --------------------------------------------------------------------------- #
# M15：owner 清单不许被悄悄截断（事实源 ＝ `rules.md §十六` 正文）
# --------------------------------------------------------------------------- #
def _owner_list_from_rules() -> tuple:
    """从 `rules.md §十六` 的 owner 清单段落**解析**出那 5 个文件。

    🔴 事实源**只许一份** ＝ 铁律正文；工具里的常量必须与它**逐字一致**
       ⇒ 工具里再写一份清单就是「第二份事实源」（§十四.96 单一事实源纪律）。
    """
    rules = (PROJECT_DIR / "manu" / "memory" / "rules.md").read_text(encoding="utf-8")
    marker = "> 🔴 **落地位置（owner）清单**"
    start = rules.index(marker)
    block = rules[start:start + 4000]
    block = block[: block.index("⇒ 全部改成由")]
    found = []
    for match in re.finditer(r"manu/etf_rotation/[A-Za-z0-9_/]+\.py", block):
        if match.group(0) not in found:
            found.append(match.group(0))
    return tuple(found)


def test_m15_owner_file_list_must_match_rules_md() -> None:
    """M15：🔴 owner 清单**不许**被悄悄截断 —— 必须与 `rules.md §十六` 的清单**一致**。

    🔴 独立审计抓出的真缺陷：断言 `facts.owner_files == len(TOOL.OWNER_FILES)` 是**自指**
       （期望值取自**被检对象自身**）⇒ 把 `OWNER_FILES` 删掉一个，测试**仍然全绿**
       ⇒ §十六 的 owner 清单可被无声缩小、A6 的覆盖面随之缩小。
    """
    expected = _owner_list_from_rules()
    assert len(expected) == 5, "从 `rules.md §十六` 解析出的 owner 清单不是 5 条，解析口径坏了：%s" % (
        expected,
    )
    assert sorted(TOOL.OWNER_FILES) == sorted(expected), (
        "owner 清单与 `rules.md §十六` 不一致 ⇒ 事实源被改了（不许第二份事实源）：%s vs %s"
        % (sorted(TOOL.OWNER_FILES), sorted(expected))
    )


# --------------------------------------------------------------------------- #
# 🔴🔴 2026-11-20 第 45 轮独立复审补的守卫（M16~M23）
#   复审抓出的**真缺陷**（每条都配**变异反证** ⇒ 删掉判据 ⇒ 本组必红）：
#     M16 A4b 深度 2 命中必报（原先**零守卫覆盖** ⇒ 可被静默删除）；
#     M17 「不适用」时 rc 仍 0 **且 stdout 必出现「不适用」**（原先**零守卫覆盖**）；
#     M18 A1 的四条子判据各有一条（原先**零守卫覆盖**）；
#     M19 双根**不许落 `/tmp` 之下**（原先**零守卫覆盖** ⇒ 产物可直接落 `/tmp`）；
#     M20 **A1b**：命名空间根下本项目专属目录**只许** `<DATASET_ROOT>` 一个（原先没实现）；
#     M21 **A6 全量 AST**：裸串 / `os.getenv` 形式的落点字面量也必须报
#         （原先只认 `Path(...)` 与 `os.environ.get(...)` ⇒ `_WORK_ROOT = "/mnt/data"` **隐形**）；
#     M22 **A5 目录内容口径**：目录**里的**本项目件必须报（原先只判顶层普通文件 ⇒ 假绿）；
#     M23 **命名空间根无条件豁免** ＋ 多链共享目录**零误伤**（越权防线）。
# --------------------------------------------------------------------------- #
def test_m16a_nested_project_dir_under_shared_runs_dir_must_be_flagged(sandbox: Sandbox) -> None:
    """M16a：**A4b 深度 2**：共享目录的**下一层**藏着本项目专属目录 ⇒ 必须报。

    🔴 复审实证：把整个 A4b 块删掉 ⇒ 本组**全绿** ⇒ 该判据**零守卫覆盖**。
    """
    nested = sandbox.runs_root / "codex_overnight" / "codex-overnight-etf20261113"
    nested.mkdir(parents=True, exist_ok=True)
    defects, facts = _analyze(sandbox)
    assert "A4b/共享目录里藏着本项目专属目录" in _defect_checks(defects), _reasons(defects)
    assert facts.runs_nested_project_dirs == 1, facts.runs_nested_project_dirs


def test_m16b_shared_runs_dir_with_only_other_chains_must_not_be_flagged(sandbox: Sandbox) -> None:
    """M16b：**零误伤**：共享目录里只有**别的链**的目录 ⇒ A4b **不许**报。"""
    other = sandbox.runs_root / "codex_overnight" / "AVTrack_infer_20261119"
    other.mkdir(parents=True, exist_ok=True)
    defects, facts = _analyze(sandbox)
    assert "A4b/共享目录里藏着本项目专属目录" not in _defect_checks(defects), _reasons(defects)
    assert facts.runs_nested_project_dirs == 0


def test_m17_not_applicable_must_be_spelled_out_and_not_red(monkeypatch, capsys) -> None:
    """M17：🔴 「不适用」必须**显式说出来**（`§十四.117` 第 3 条：不许无声变绿）。

    🔴 复审实证：把 `sync_slim_memory.py` 里那段「不适用」提示删掉 ⇒ 本组**全绿**
       ⇒ 该提示**零守卫覆盖** ⇒ 「不许无声变绿」这条纪律**没有牙齿**。
    """
    sync_mod = _load_sync_module()
    checker_mod = _import_placement_checker()
    monkeypatch.setattr(
        checker_mod,
        "analyze",
        lambda *args, **kwargs: ([], checker_mod.PlacementFacts(server_scope=False)),
    )
    problems = sync_mod._check_artifact_placement()
    captured = capsys.readouterr()
    assert problems == [], "「不适用」**不许**折算成缺陷（`§十四.117`）：%s" % (problems,)
    assert "不适用" in captured.out, "「不适用」必须**显式说出来**，不许无声变绿：" + captured.out


def test_m18a_repo_under_double_root_must_be_flagged(sandbox: Sandbox) -> None:
    """M18a：A1 子判据①：**仓库落在双根内** ⇒ 必须报。"""
    inner = sandbox.dataset_root / "proj"
    inner.mkdir(parents=True, exist_ok=True)
    defects, _facts = TOOL.analyze(str(inner), str(sandbox.dataset_root), str(sandbox.results_root))
    assert "A1/仓库落在双根内" in _defect_checks(defects), _reasons(defects)


def test_m18b_double_root_under_repo_must_be_flagged(sandbox: Sandbox) -> None:
    """M18b：A1 子判据②：**双根落在仓库内** ⇒ 必须报。"""
    defects, _facts = _analyze(
        sandbox,
        dataset_root=sandbox.project_dir / "ds",
        results_root=sandbox.project_dir / "rs",
    )
    assert "A1/双根落在仓库内" in _defect_checks(defects), _reasons(defects)


def test_m18c_results_root_wrong_depth_must_be_flagged(sandbox: Sandbox) -> None:
    """M18c：A1 子判据③：`<RESULTS_ROOT>` 落在 `runs/` 之下但**不是直接子目录** ⇒ 必须报。"""
    defects, _facts = _analyze(
        sandbox, results_root=sandbox.runs_root / "sub" / "etf_rotation_results"
    )
    assert "A1/结果根层级不对" in _defect_checks(defects), _reasons(defects)


def test_m18d_dataset_root_wrong_depth_must_be_flagged(sandbox: Sandbox) -> None:
    """M18d：A1 子判据④：`<DATASET_ROOT>` 落在 `datasets/` 之下但**不是「命名空间/项目」两层** ⇒ 必须报。"""
    defects, _facts = _analyze(sandbox, dataset_root=sandbox.datasets_root / "etf_rotation")
    assert "A1/数据集根层级不对" in _defect_checks(defects), _reasons(defects)


def test_m19_double_root_under_tmp_must_be_flagged(sandbox: Sandbox) -> None:
    """M19：🔴 **双根不许落 `/tmp` 之下**（否则与第 4 条「`/tmp` 痕迹清零」自相矛盾）。"""
    defects, _facts = _analyze(
        sandbox,
        dataset_root=sandbox.tmp_root / "ds",
        results_root=sandbox.runs_root / "etf_rotation_results",
    )
    assert "A1/双根落在 /tmp 之下" in _defect_checks(defects), _reasons(defects)


def test_m20a_second_project_dir_in_namespace_must_be_flagged(sandbox: Sandbox) -> None:
    """M20a：🔴 **A1b**：命名空间根下**第二个**本项目专属目录 ⇒ 必须报。

    🔴 复审实证：`datasets/manu/etf_rotation_v2/` ⇒ 旧闸门 **0 缺陷**（第 1 条
       「本项目文件**全部位于**双根之下」在**命名空间内部**从来没实现过）。
    """
    second = sandbox.datasets_root / "manu" / "etf_rotation_v2"
    second.mkdir(parents=True, exist_ok=True)
    (second / "etf_daily.csv").write_text("a,b\n1,2\n", encoding="utf-8")
    defects, facts = _analyze(sandbox)
    assert "A1b/命名空间里多出本项目目录" in _defect_checks(defects), _reasons(defects)
    assert facts.datasets_namespace_project_dirs == 1, facts.datasets_namespace_project_dirs


def test_m20b_own_dataset_root_must_not_be_flagged(sandbox: Sandbox) -> None:
    """M20b：允许的那一个（`<DATASET_ROOT>` 自己）**不许**被 A1b 报成违规。"""
    defects, facts = _analyze(sandbox)
    assert "A1b/命名空间里多出本项目目录" not in _defect_checks(defects), _reasons(defects)
    assert facts.datasets_namespace_project_dirs == 0


def test_m21a_bare_string_literal_must_be_flagged(sandbox: Sandbox) -> None:
    """M21a：🔴 **A6 全量 AST**：`_WORK_ROOT = "<MOUNT_ROOT>"` 这种**裸串赋值**必须报。

    🔴 复审实证：旧 A6 只认 `Path(...)` 与 `os.environ.get(..., 默认值)`
       ⇒ **`_WORK_ROOT = "/mnt/data"`（§十六 第 2 条点名的历史违例原形）完全隐形**
       ⇒ 把裸串改回去，闸门照样全绿。
    """
    target = sandbox.owner_file("manu/etf_rotation/daily/fetch_data.py")
    target.write_text(
        '"""沙箱 owner：裸串落点字面量（历史违例原形）。"""\n'
        "_WORK_ROOT = %r\n" % TOOL.MOUNT_ROOT,
        encoding="utf-8",
    )
    defects, _facts = _analyze(sandbox)
    assert "A6/落点字面量越界" in _defect_checks(defects), _reasons(defects)


def test_m21b_getenv_default_literal_must_be_flagged(sandbox: Sandbox) -> None:
    """M21b：**A6 全量 AST**：`os.getenv(<名字>, "<MOUNT_ROOT>")` 的默认值也必须报。"""
    target = sandbox.owner_file("manu/etf_rotation/daily/fetch_data.py")
    target.write_text(
        '"""沙箱 owner：`os.getenv` 默认值形式的落点字面量。"""\n'
        "import os\n"
        "_WORK_ROOT = os.getenv('ETF_ROTATION_WORK_ROOT', %r)\n" % TOOL.MOUNT_ROOT,
        encoding="utf-8",
    )
    defects, _facts = _analyze(sandbox)
    assert "A6/落点字面量越界" in _defect_checks(defects), _reasons(defects)


def test_m21c_docstring_mentioning_mount_root_must_not_be_flagged(sandbox: Sandbox) -> None:
    """M21c：**零误伤**：docstring 里**提到** `/mnt/data`（在讲禁令）**不许**被当成落点字面量。"""
    target = sandbox.owner_file("manu/etf_rotation/daily/fetch_data.py")
    target.write_text(
        '"""沙箱 owner：docstring 里提到 %s（**在讲禁令**，不是在写落点）。"""\n'
        "_WORK_ROOT = None\n" % TOOL.MOUNT_ROOT,
        encoding="utf-8",
    )
    defects, _facts = _analyze(sandbox)
    assert "A6/落点字面量越界" not in _defect_checks(defects), _reasons(defects)


def test_m22a_project_file_inside_tmp_dir_must_be_flagged(sandbox: Sandbox) -> None:
    """M22a：🔴 **A5 目录内容口径**：目录**里的**本项目件必须报（原先只判顶层普通文件 ⇒ 假绿）。

    🔴 复审实证：现场 `/tmp/inv/`（5 件）与 `/tmp/supplement.txt/`（1 件）
       **都不在**顶层普通文件口径里 ⇒ 旧闸门报「`/tmp` 本项目件 = 0」＝ **假绿**。
    """
    leak = sandbox.tmp_root / "inv"
    leak.mkdir(parents=True, exist_ok=True)
    (leak / "memory.md.cand").write_text(
        "落点：manu/etf_rotation 与 /mnt/data/siping/runs/etf_rotation_results\n",
        encoding="utf-8",
    )
    defects, facts = _analyze(sandbox)
    assert "A5//tmp 痕迹未清零" in _defect_checks(defects), _reasons(defects)
    assert facts.tmp_content_dir_items == 1, facts.tmp_content_dir_items


def test_m22b_bare_word_in_other_chain_dir_must_not_be_flagged(sandbox: Sandbox) -> None:
    """M22b：**零误伤**：别的链的目录里**只提到**裸词「etf_rotation」⇒ **不许**报（防越权）。"""
    other = sandbox.tmp_root / "other_chain_scratch"
    other.mkdir(parents=True, exist_ok=True)
    (other / "notes.txt").write_text("今天在聊 etf_rotation 这个话题\n", encoding="utf-8")
    defects, facts = _analyze(sandbox)
    assert "A5//tmp 痕迹未清零" not in _defect_checks(defects), _reasons(defects)
    assert facts.tmp_content_dir_items == 0


def test_m23a_namespace_container_with_other_chains_must_not_be_flagged(sandbox: Sandbox) -> None:
    """M23a：🔴 **越权防线**：命名空间根（`datasets/manu/`）里**别的链**的目录**不许**被报。

    🔴 复审实证：实测 `datasets/manu/` 有 **97** 条（`anti-uav*` / `arm_*` / `gmc_*` /
       `fpv_ir_*` / `AVTrack` …）⇒ 旧 A3 的「共用词元 ⇒ 再取证」规则会把**整棵命名空间**
       判成本项目目录（**越权**）⇒ 现在容器**无条件豁免**。
    """
    ns_root = sandbox.datasets_root / "manu"
    for name in ("anti-uav_20261119", "arm_train_v2", "gmc_data", "AVTrack"):
        (ns_root / name).mkdir(parents=True, exist_ok=True)
    defects, facts = _analyze(sandbox)
    assert "A3/datasets 顶层本项目目录" not in _defect_checks(defects), _reasons(defects)
    assert facts.datasets_project_dirs == 0


def test_m23b_other_chain_container_at_datasets_top_level_must_not_be_flagged(sandbox: Sandbox) -> None:
    """M23b：**零误伤**：`datasets/` 顶层的**别的链**的容器目录（`qlib/` 等）**不许**被报。"""
    other = sandbox.datasets_root / "qlib"
    other.mkdir(parents=True, exist_ok=True)
    # 🔴 注意：`cn_data_live` / `_stage1_work` 是 §十六 第 2 条**点名**的历史违例实例
    #   ⇒ 它们**必须**被报（M5 覆盖）⇒ 这里用**别的**子目录名做「零误伤」用例
    (other / "cn_data_other_chain").mkdir(parents=True, exist_ok=True)
    defects, _facts = _analyze(sandbox)
    assert "A3/datasets 顶层本项目目录" not in _defect_checks(defects), _reasons(defects)


def test_m23c_project_dir_at_datasets_top_level_must_still_be_flagged(sandbox: Sandbox) -> None:
    """M23c：**不许被豁免过头**：`datasets/` 顶层的**本项目**目录仍必须报。"""
    top = sandbox.datasets_root / "etf_rotation"
    top.mkdir(parents=True, exist_ok=True)
    defects, facts = _analyze(sandbox)
    assert "A3/datasets 顶层本项目目录" in _defect_checks(defects), _reasons(defects)
    assert facts.datasets_project_dirs == 1, facts.datasets_project_dirs


def test_m18e_relative_raw_root_must_be_flagged(sandbox: Sandbox, monkeypatch) -> None:
    """M18e：A1 子判据⑤：**原始值**（env / CLI）是**相对路径** ⇒ 必须报。

    🔴 复审实证：原先这条判据是**死代码**（`_resolve_root` 恒 `abspath`
       ⇒ 「绝对路径」永远为真 ⇒ 删掉也不影响行为）⇒ 现在改成判**原始值**。
    """
    monkeypatch.setenv("ETF_ROTATION_OUTPUT_ROOT", "./relative_ds")
    # 🔴 必须**不传**显式双根 ⇒ 才走 env 分支（`_raw_root` 看的就是**原始值**）
    defects, _facts = TOOL.analyze(str(sandbox.project_dir))
    assert "A1/双根非绝对路径" in _defect_checks(defects), _reasons(defects)


def test_m20c_project_dir_in_undeclared_namespace_must_be_flagged(sandbox: Sandbox) -> None:
    """M20c：**A1b 扫所有命名空间**：本项目目录落在**未声明**的命名空间下 ⇒ 必须报。

    🔴 复审实证：A1b 若只扫「已声明的命名空间」⇒ `datasets/manu/etf_rotation` 这类
       **漏检** ⇒ 第 1 条「本项目文件**全部位于**双根之下」在命名空间之间也失守。
    """
    declared = sandbox.datasets_root / "xyz" / "etf_rotation"
    declared.mkdir(parents=True, exist_ok=True)
    stray = sandbox.datasets_root / "manu" / "etf_rotation_v2"
    stray.mkdir(parents=True, exist_ok=True)
    defects, facts = _analyze(sandbox, dataset_root=declared)
    assert "A1b/命名空间里多出本项目目录" in _defect_checks(defects), _reasons(defects)
    reported = [d.preview for d in defects if d.check == "A1b/命名空间里多出本项目目录"]
    assert any(preview.endswith("etf_rotation_v2") for preview in reported), reported
    assert facts.datasets_namespace_project_dirs == len(reported), facts.datasets_namespace_project_dirs


def test_m23d_shared_container_must_not_be_reported_as_project_dir(sandbox: Sandbox) -> None:
    """M23d：🔴 **越权防线（要有牙）**：多链共享的容器目录**本身**不许被报成「顶层本项目目录」。

    🔴 复审实证：旧 A3 的「共用词元 ⇒ 再取证（子项命中归属证据）」规则会把
       **整棵** `datasets/manu/`（实测 **97** 条，本项目只占 **1** 条）判成「本项目目录」
       ⇒ **越权**。⇒ 现在 A3 **只认名字** ⇒ 容器**豁免**；容器里的那个本项目目录
       由 **A1b** **精确**报告（报的是**子目录**，不是整棵容器）。
    """
    declared = sandbox.datasets_root / "xyz" / "etf_rotation"
    declared.mkdir(parents=True, exist_ok=True)
    ns_root = sandbox.datasets_root / "manu"
    for name in ("anti-uav_20261119", "arm_train_v2", "gmc_data", "AVTrack"):
        (ns_root / name).mkdir(parents=True, exist_ok=True)
    (ns_root / "etf_rotation_v2").mkdir(parents=True, exist_ok=True)
    defects, _facts = _analyze(sandbox, dataset_root=declared)
    checks = _defect_checks(defects)
    assert "A3/datasets 顶层本项目目录" not in checks, _reasons(defects)
    reported = [d.preview for d in defects if d.check == "A1b/命名空间里多出本项目目录"]
    assert reported, "容器里的本项目目录必须由 A1b **精确**报出来：" + _reasons(defects)
    # 🔴 关键：**报的是容器里的「子目录」，不是整棵容器** ⇒ 越权面归零
    assert not any(preview.rstrip("/").endswith("/manu") for preview in reported), reported
    assert any(preview.endswith("etf_rotation_v2") for preview in reported), reported


# --------------------------------------------------------------------------- #
# M24：登记制豁免（`TMP_EXEMPT_NAMES`）必须**精确**、且**不挡内容口径**
# --------------------------------------------------------------------------- #
def test_m24_registered_tmp_exemption_is_narrow_and_does_not_hide_content(sandbox: Sandbox) -> None:
    """M24：登记豁免只对**精确文件名**生效，且**只挡名字口径、不挡内容口径**。

    🔴 起因（2026-11-20 03:0x 实测）：运行环境（CLI / QA 守护进程）自有的线程 turn 日志
       ``/tmp/vq_etf.txt`` 被**名字口径**误报成「本项目件」（``etf`` **独立成词**）——
       但它**仓库零引用**、内容只有 turn 编号/时间戳、且**后台守护进程持续重写**
       ⇒ 「删了清零」这条路走不通 ⇒ 按 ``pytest-of-*`` 同性质**登记豁免**。
    🔴 豁免**必须**窄到**精确文件名**：近似名（``vq_etf_data.json``）**照样要报**。
    🔴 豁免**不得**变成「同名即隐身」的洞：豁免名里若真塞进本项目数据 ⇒ **照样报 A5**。
    """
    exempt_name = TOOL.TMP_EXEMPT_NAMES[0]
    assert TOOL.TMP_EXEMPT_NAMES == ("vq_etf.txt",), "豁免名单必须精确登记，不许扩成前缀"
    assert not exempt_name.startswith("pytest-of-"), "豁免名单不许混进前缀豁免族"

    # ① 无本项目内容的豁免名 ⇒ **不报**
    (sandbox.tmp_root / exempt_name).write_text(
        "[2026-11-20 02:34:59] === turn #51 end rc=0 ===\n", encoding="utf-8"
    )
    defects, facts = _analyze(sandbox)
    assert "A5//tmp 痕迹未清零" not in _defect_checks(defects), _reasons(defects)
    assert facts.tmp_exempt_items >= 1, "登记豁免的件必须计入豁免计数"

    # ② 豁免名里**塞进本项目内容** ⇒ **照样报 A5**（豁免不挡内容口径）
    (sandbox.tmp_root / exempt_name).write_text(
        "path=/mnt/data/siping/runs/etf_rotation_results/snapshots/x\n", encoding="utf-8"
    )
    defects, _facts = _analyze(sandbox)
    assert "A5//tmp 痕迹未清零" in _defect_checks(defects), (
        "豁免名里塞进本项目数据却没人报 ⇒ 豁免变成了「同名即隐身」的洞：" + _reasons(defects)
    )

    # ③ **近似名**（不在豁免名单）⇒ **照样报 A5**（豁免必须精确到文件名）
    (sandbox.tmp_root / exempt_name).unlink()
    (sandbox.tmp_root / "vq_etf_data.json").write_text("{}", encoding="utf-8")
    defects, _facts = _analyze(sandbox)
    assert "A5//tmp 痕迹未清零" in _defect_checks(defects), (
        "近似名被放过 ⇒ 豁免过宽：" + _reasons(defects)
    )
