"""🔴 2026-11-20 新增：逐日 record 载体（`manu/memory/records/*.md`）的**表格结构闸门覆盖**守卫。

【背景 / 真缺陷】
2026-11-20 reviewer 复审 NIT 9 实测发现：`manu/tools/check_memory_tables.py` 的
**默认文件集**只有 `manu/memory/memory.md` ＋ `manu/memory/memory_compact.md`
⇒ `manu/memory/records/*.md`（逐日 record 载体，本轮新增的第三方事实源）
**不在任何闸门的覆盖范围内**。

为什么这是**真缺口**而不是「小事」：
  ① `run_gates.sh` 第 17 阶段（`check_memory_tables`）**不传 `--files`** ⇒ 只跑默认集
     ⇒ record 里的表格列数错位，**常规闸门跑不到**；
  ② record 是**几个月后复盘的事实源**（权重/股数/金额）⇒ 一旦列错位，
     「当前权重」会被读成「目标权重」这类**真值误读**，且**没有任何闸门会报**；
  ③ 修法必须**面向未来**：用 **glob 扫 `records/*.md`** 而不是写死今天的文件名
     ⇒ 每天新增的 record 自动受检，不需要改代码。

【被测对象】
`manu/tools/check_memory_tables.py`（**纯只读**闸门，不写、不改任何被检查的文件）。

【测试风格】
照抄同目录 `test_record_carrier_guard.py` 的风格：被测模块用 ``importlib`` 按**路径**加载
（``manu/tools/`` 不在 ``sys.path`` 的默认位置）。
🔴 本文件有**两个镜像位置**（``manu/etf_rotation/tests/`` 与 ``manu/tests/``，两者必须**逐字节一致**）
⇒ **不许**用固定的 ``parents[N]`` 定位 ⇒ 改成向上搜索。

【本文件钉死的 6 件事】
  P1 默认集**必须**覆盖 `records/*.md`（含未来新增文件 ⇒ glob 而非写死）；
  P2 默认集**不许**把 `.csv` / `*.md.bak.*` 备份收进表格闸门（不误伤）；
  P3 真实工程默认跑 ⇒ **rc 0** ＋ 输出里逐个点名 records 的每份 `.md`；
  P4 **变异反证 M1**：把一份 record 的表格改坏（多一列）⇒ 默认集**必须报红**（rc 1）＋ 输出点名该文件；
  P5 **变异反证 M2（反向证明）**：显式只传 `--files memory.md` ⇒ **不报**该缺陷
      ⇒ 证明「覆盖」确实来自**默认集**，不是别处蒙对的；
  P6 **变异反证 M3**：在 records 里放一份**未来日期**的坏 record ⇒ 默认集**必须报红**
      ⇒ 证明 glob 生效（每天新增的 record 自动受检，不需要改代码）；
  另：闸门**只读**（跑完文件 md5 一字不变）＋ records 目录缺失时**不炸**（优雅退化为 2 个主档）。

【live 集成断言】
对**真实工程目录**跑工具 ⇒ 断言 rc == 0（**不许 skip、不许弱化**）。
"""

from __future__ import annotations

import contextlib
import hashlib
import importlib.util
import io
import shutil
import sys
from pathlib import Path

import pytest


def _find_tool() -> Path:
    """向上找 ``manu/tools/check_memory_tables.py``。

    🔴 本文件有**两个镜像位置** ⇒ **不许**用固定的 ``parents[N]`` 定位 ⇒ 改成向上搜索。
    """
    for parent in Path(__file__).resolve().parents:
        candidate = parent / "manu" / "tools" / "check_memory_tables.py"
        if candidate.is_file():
            return candidate
    raise AssertionError("找不到 manu/tools/check_memory_tables.py")


def _find_project_dir() -> Path:
    """向上找工程根（含 ``manu/tools/`` 的那一层）。"""
    for parent in Path(__file__).resolve().parents:
        if (parent / "manu" / "tools" / "check_memory_tables.py").is_file():
            return parent
    raise AssertionError("找不到工程根目录")


TOOL_PATH = _find_tool()
PROJECT_DIR = _find_project_dir()

#: 记忆树里被本闸门覆盖的四层 ＋ 逐日 record 目录
_MEMORY_REL = "manu/memory"
_RECORDS_REL = "manu/memory/records"
#: memory **四层**（主档 / 精简档 / 铁律层 / slim 档）—— 2026-11-20 起全部纳入默认集
_FOUR_LAYERS = (
    "manu/memory/memory.md",
    "manu/memory/memory_compact.md",
    "manu/memory/rules.md",
    "manu/memory/.slim_memory.md",
)

#: 注入 record 的坏表格：表头 2 列、数据行 3 列 ⇒ 列数错位 ⇒ 必须报缺陷
_BAD_TABLE = "\n| 变异列A | 变异列B |\n|:--|:--|\n| 值1 | 值2 | 值3 |\n"


def _load_tool():
    """按路径加载被测工具（``manu/tools/`` 不在默认 ``sys.path`` 上）。"""
    spec = importlib.util.spec_from_file_location("check_memory_tables_under_test", TOOL_PATH)
    assert spec and spec.loader
    module = importlib.util.module_from_spec(spec)
    # 🔴 必须先注册进 ``sys.modules``：被测模块用了 ``@dataclass`` ＋
    #    ``from __future__ import annotations`` ⇒ dataclasses 会回查
    #    ``sys.modules[cls.__module__].__dict__`` ⇒ 不注册会直接 AttributeError。
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


def _run(project_dir: Path, extra: list[str] | None = None) -> tuple[int, str]:
    """跑闸门 ⇒ 返回 (退出码, 合并后的 stdout)。"""
    argv = ["--project-dir", str(project_dir)] + list(extra or [])
    buffer = io.StringIO()
    with contextlib.redirect_stdout(buffer):
        rc = TOOL.main(argv)
    return rc, buffer.getvalue()


@pytest.fixture()
def sandbox(tmp_path: Path) -> Path:
    """把「两份主档 ＋ records 目录」复制一份到临时目录（只含表格闸门关心的部分）。"""
    mem = tmp_path / "manu" / "memory"
    rec = mem / "records"
    rec.mkdir(parents=True)
    for layer in _FOUR_LAYERS:
        shutil.copy2(PROJECT_DIR / layer, mem / Path(layer).name)
    for item in sorted((PROJECT_DIR / _RECORDS_REL).glob("*.md")):
        shutil.copy2(item, rec / item.name)
    return tmp_path


def _records_md_names(project_dir: Path) -> list[str]:
    return sorted(item.name for item in (project_dir / _RECORDS_REL).glob("*.md"))


def _break_one_record(project_dir: Path) -> Path:
    """往一份 record 里注入坏表格 ⇒ 返回被破坏的文件。"""
    victim = sorted((project_dir / _RECORDS_REL).glob("*.md"))[0]
    with victim.open("a", encoding="utf-8") as handle:
        handle.write(_BAD_TABLE)
    return victim


# ============================ P1 / P2：默认集的覆盖口径 ============================


def test_default_targets_cover_records_dir() -> None:
    """P1：默认集必须含 records 下**每一份** `.md`（glob ⇒ 未来新增自动纳入）。"""
    targets = TOOL.default_targets(str(PROJECT_DIR))
    assert targets[: len(_FOUR_LAYERS)] == list(_FOUR_LAYERS)

    records_targets = [item for item in targets if item.startswith(_RECORDS_REL + "/")]
    assert records_targets, "records 目录里至少要有一份 .md（否则本守卫无意义）"
    assert sorted(Path(item).name for item in records_targets) == _records_md_names(PROJECT_DIR)


def test_default_targets_cover_all_four_memory_layers() -> None:
    """P1b：默认集必须含 **memory 四层**（含铁律层 rules.md 与 slim 档）。

    🔴 此前只有 memory.md ＋ memory_compact.md 受检 ⇒ rules.md / .slim_memory.md 的表格
    列数错位**没有任何闸门能报** ⇒ 与 records 同类的复发点。
    """
    targets = TOOL.default_targets(str(PROJECT_DIR))
    for layer in _FOUR_LAYERS:
        assert layer in targets, f"默认集漏掉了 {layer}"


def test_default_targets_exclude_non_markdown_and_backups() -> None:
    """P2：`.csv`（台账）与 `*.md.bak.*`（备份）不许进表格闸门。"""
    targets = TOOL.default_targets(str(PROJECT_DIR))
    records_targets = [item for item in targets if item.startswith(_RECORDS_REL + "/")]
    assert not any(item.endswith(".csv") for item in records_targets)
    assert not any(".bak" in item for item in records_targets)


def test_default_targets_tolerate_missing_records_dir(tmp_path: Path) -> None:
    """records 目录不存在时**不炸** ⇒ 优雅退化为两份主档。"""
    (tmp_path / "manu" / "memory").mkdir(parents=True)
    targets = TOOL.default_targets(str(tmp_path))
    assert targets == list(_FOUR_LAYERS)


# ============================ P3：live 集成（真实工程） ============================


def test_real_project_default_run_is_green_and_names_every_record() -> None:
    """P3：真实工程默认跑 ⇒ rc 0 ＋ 输出逐个点名 records 的每份 `.md`。"""
    rc, out = _run(PROJECT_DIR)
    assert rc == 0, out
    for name in _records_md_names(PROJECT_DIR):
        assert f"{_RECORDS_REL}/{name}" in out, f"默认集漏掉了 {name}"
    for layer in _FOUR_LAYERS:
        assert layer in out, f"默认集漏掉了 {layer}"
    assert "缺陷 0" in out


# ============================ P4 / P5 / P6：变异反证 ============================


def test_mutant_broken_record_table_is_caught_by_default_set(sandbox: Path) -> None:
    """P4（M1）：record 表格被改坏 ⇒ 默认集**必须报红**（rc 1）并点名该文件。"""
    victim = _break_one_record(sandbox)
    rc, out = _run(sandbox)
    assert rc == 1, out
    assert "[DEFECT]" in out
    assert victim.name in out


def test_mutant_explicit_files_only_does_not_cover_records(sandbox: Path) -> None:
    """P5（M2 反向证明）：只传 `--files memory.md` ⇒ **不报** records 的缺陷。

    ⇒ 证明 P4 的「报红」确实来自**默认集覆盖 records**，不是别处蒙对的。
    """
    _break_one_record(sandbox)
    rc, out = _run(sandbox, ["--files", "manu/memory/memory.md"])
    assert rc == 0, out
    assert "[DEFECT]" not in out


def test_mutant_future_dated_record_is_auto_covered(sandbox: Path) -> None:
    """P6（M3）：records 里放一份**未来日期**的坏 record ⇒ 默认集**必须报红**。

    ⇒ 证明 glob 生效：**每天新增的 record 自动受检**，不需要改代码。
    """
    future = sandbox / _RECORDS_REL / "20990101.md"
    future.write_text(
        "| 列A | 列B |\n|:--|:--|\n| 值1 | 值2 | 值3 |\n", encoding="utf-8"
    )
    rc, out = _run(sandbox)
    assert rc == 1, out
    assert "20990101.md" in out


# ============================ 只读性 ============================


def test_gate_is_read_only(sandbox: Path) -> None:
    """闸门**只读**：跑完（绿灯与红灯各一次）所有文件 md5 一字不变。"""
    before = _tree_md5(sandbox)
    _run(sandbox)
    _break_one_record(sandbox)
    _run(sandbox)
    assert _tree_md5(sandbox) != before  # 变异确实改动了文件（否则下面的断言无意义）
    after_mutation = _tree_md5(sandbox)
    _run(sandbox)
    assert _tree_md5(sandbox) == after_mutation
