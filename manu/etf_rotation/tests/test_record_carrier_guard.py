"""🔴 2026-11-19 新增：「记忆载体入库纪律 ＋ 落点纪律」的守卫。

【背景】
`rules.md §十四.122` 第 5 条（落点纪律）＋ `§十四.125` 铁律二（新增载体必须当场验「会不会被提交」）：
  ① `manu/memory/` 下**只许记忆文档 ＋ 逐日记录**（`.md` ＋ 逐日台账 `.csv`）
     ⇒ 出现 `.pkl` / 行情 `.csv` / 其他运行产物 = 违规（撞「版本库只放源」铁律十六）；
  ② **台账 `.csv` 入库前必须验「没被 `.gitignore` 静默误杀」**
     （上游全局 `*.csv` ⇒ 台账不入库 ⇒ 几个月后复盘的事实源随工作区清理一起消失）；
  ③ `.gitattributes` 的 `-text` 必须**成对**（否则行尾归一化会让镜像对两侧字节分叉）。
🔴 此前这些**只有人守**：既有闸门 `check_mirror_pairs.py` 只管**镜像对**
（`manu/<X>` ↔ `manu/etf_rotation/<X>`），而 `manu/memory/` **整棵树不在镜像体系里**
⇒ 「台账被全局 `*.csv` 静默忽略」这种形态**没有任何闸门能报**（真缺陷【十九】的复发点）；
且 `.gitattributes` **全仓没有任何工具检查**（`grep -rn gitattributes manu/tools/*.py` = 0 命中）
⇒ 铁律一「`-text` 同样要成对」也是**靠人记** ⇒ 复发即断链。

【被测对象】
`manu/tools/check_record_carrier.py`（**纯只读**闸门，不写、不改任何被检查的文件）。

【测试风格】
照抄同目录 `test_mirror_pairs_guard.py` 的风格：被测模块用 ``importlib`` 按**路径**加载
（``manu/tools/`` 不在 ``sys.path`` 的默认位置）。
🔴 本文件有**两个镜像位置**（``manu/etf_rotation/tests/`` 与 ``manu/tests/``，两者必须**逐字节一致**）
⇒ **不许**用固定的 ``parents[N]`` 定位 ⇒ 改成向上搜索。
🔴 夹具必须**自带 git 工作区**（`git check-ignore` / `git check-attr` 只在工作区里有意义）
⇒ 沙箱里 ``git init``。

【变异反证（守卫有牙）】
M1 给台账加忽略规则 ⇒ 必须报「A/载体被忽略」＋「A2/台账未显式放行」；
M2 去掉台账的显式放行（**侥幸形态**：没被忽略但也没登记）⇒ 必须报「A2/台账未显式放行」；
M3 去掉台账的 `-text` ⇒ 必须报「B/台账缺 -text」；
M4 只给镜像对**一侧**加 `-text` ⇒ 必须报「B/-text 未成对」；
M5 往 `manu/memory/` 塞 `.pkl` ⇒ 必须报「C/载体类型越界」；
M6 往 `records/` 塞行情 `.csv` ⇒ 必须报「C/载体类型越界」；
M7 往 `records/` 塞超大文件 ⇒ 必须报「D/单文件超限」；
M8 删掉台账 ⇒ 必须报「A2/台账缺失」；
M9 工具**只读** ⇒ 跑完记忆树/`.gitignore`/`.gitattributes` 的 md5 必须一字不变；
M10 不在 git 工作区 ⇒ **不适用**（rc 2），不许当成缺陷、不许当成绿；
M11 🔴 **反例**：逐日 `.md` 与 `archive/*.md.bak.*` 备份 ⇒ 必须**零误伤**（rc 0）。

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
    """向上找 ``manu/tools/check_record_carrier.py``。

    🔴 本文件有**两个镜像位置** ⇒ **不许**用固定的 ``parents[N]`` 定位 ⇒ 改成向上搜索。
    """
    for parent in Path(__file__).resolve().parents:
        candidate = parent / "manu" / "tools" / "check_record_carrier.py"
        if candidate.is_file():
            return candidate
    raise AssertionError("找不到 manu/tools/check_record_carrier.py")


def _find_project_dir() -> Path:
    """向上找工程根（含 ``manu/tools/`` 的那一层）。"""
    for parent in Path(__file__).resolve().parents:
        if (parent / "manu" / "tools" / "check_record_carrier.py").is_file():
            return parent
    raise AssertionError("找不到工程根目录")


TOOL_PATH = _find_tool()
PROJECT_DIR = _find_project_dir()

#: 沙箱里要复制的树
_MEMORY_TREE = "manu/memory"
#: 镜像对（用来验 `-text` 成对）
_PAIR_REL = "data/account_snapshot.csv"


def _load_tool():
    """按路径加载被测工具（``manu/tools/`` 不在默认 ``sys.path`` 上）。"""
    spec = importlib.util.spec_from_file_location("check_record_carrier_under_test", TOOL_PATH)
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


@pytest.fixture()
def sandbox(tmp_path: Path) -> Path:
    """把真实工程的「记忆树 ＋ 忽略/属性规则 ＋ 一对镜像」复制一份到临时目录。

    🔴 沙箱必须**自带 git 工作区**（`git check-ignore` / `git check-attr` 只在工作区里有意义）。
    """
    shutil.copy2(PROJECT_DIR / ".gitignore", tmp_path / ".gitignore")
    shutil.copy2(PROJECT_DIR / ".gitattributes", tmp_path / ".gitattributes")
    shutil.copytree(
        PROJECT_DIR / _MEMORY_TREE,
        tmp_path / _MEMORY_TREE,
        ignore=shutil.ignore_patterns("__pycache__", "*.pyc", ".pytest_cache"),
    )
    # 一对镜像（`-text` 成对检查的靶子）
    for side in ("manu/etf_rotation", "manu"):
        target = tmp_path / side / _PAIR_REL
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text("日期,代码\n2026-11-19,512800\n", encoding="utf-8")
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
    assert facts.memory_files > 0
    assert facts.ledger_whitelisted
    assert facts.ledger_text_unset


def test_live_project_is_green() -> None:
    """live 集成：真实工程目录必须 rc 0（不许 skip、不许弱化）。"""
    completed = subprocess.run(
        [sys.executable, str(TOOL_PATH), "--project-dir", str(PROJECT_DIR)],
        capture_output=True,
        text=True,
        check=False,
    )
    assert completed.returncode == 0, completed.stdout + completed.stderr


def test_live_ledger_is_whitelisted_and_text_unset() -> None:
    """live 事实断言：台账既被显式放行、也有 `-text`（登记制的两条底线）。"""
    _defects, facts = TOOL.analyze(str(PROJECT_DIR))
    assert facts.ledger_whitelisted, "逐日台账没有被 `.gitignore` 显式放行 ⇒ 不许靠侥幸"
    assert facts.ledger_text_unset, "逐日台账缺 `.gitattributes` 的 `-text`"
    assert facts.ignored_files == 0, "记忆树里有被忽略的文件"


# --------------------------------------------------------------------------- #
# 变异反证
# --------------------------------------------------------------------------- #
def test_m1_ignored_ledger_must_be_flagged(sandbox: Path) -> None:
    """M1：给台账加忽略规则 ⇒ 必须报红（真缺陷【十九】的复发形态）。"""
    gitignore = sandbox / ".gitignore"
    gitignore.write_text(
        gitignore.read_text(encoding="utf-8") + "\nmanu/memory/records/*.csv\n",
        encoding="utf-8",
    )
    defects, _ = TOOL.analyze(str(sandbox))
    assert defects, "台账被静默忽略却没人报错 ⇒ 守卫没牙"
    checks = _defect_checks(defects)
    assert "A/载体被忽略" in checks, _reasons(defects)
    assert "A2/台账未显式放行" in checks, _reasons(defects)


def test_m2_ledger_without_explicit_whitelist_must_be_flagged(sandbox: Path) -> None:
    """M2：台账**没被忽略但也没登记**（侥幸形态）⇒ 必须报「A2/台账未显式放行」。"""
    gitignore = sandbox / ".gitignore"
    kept = [
        line
        for line in gitignore.read_text(encoding="utf-8").splitlines()
        if line.strip() != "*.csv" and not line.strip().startswith("!manu/memory/records/*.csv")
    ]
    gitignore.write_text("\n".join(kept) + "\n", encoding="utf-8")
    defects, _ = TOOL.analyze(str(sandbox))
    assert defects, "台账没被登记却没人报错 ⇒ 守卫没牙"
    checks = _defect_checks(defects)
    assert "A2/台账未显式放行" in checks, _reasons(defects)
    assert "A/载体被忽略" not in checks, "台账这轮确实没被忽略 ⇒ 不该连带报 A：" + _reasons(defects)


def test_m3_ledger_without_text_attr_must_be_flagged(sandbox: Path) -> None:
    """M3：台账去掉 `-text` ⇒ 必须报「B/台账缺 -text」（CRLF 会改写指纹）。"""
    attributes = sandbox / ".gitattributes"
    kept = [
        line
        for line in attributes.read_text(encoding="utf-8").splitlines()
        if "manu/memory/records/*.csv" not in line
    ]
    attributes.write_text("\n".join(kept) + "\n", encoding="utf-8")
    defects, _ = TOOL.analyze(str(sandbox))
    assert defects, "台账缺 `-text` 却没人报错 ⇒ 守卫没牙"
    assert "B/台账缺 -text" in _defect_checks(defects), _reasons(defects)


def test_m4_text_attr_must_be_paired(sandbox: Path) -> None:
    """M4：镜像对只给一侧 `-text` ⇒ 必须报「B/-text 未成对」（两侧字节会分叉）。"""
    attributes = sandbox / ".gitattributes"
    kept = [
        line
        for line in attributes.read_text(encoding="utf-8").splitlines()
        if not line.strip().startswith("manu/data/account_snapshot.csv")
    ]
    attributes.write_text("\n".join(kept) + "\n", encoding="utf-8")
    defects, _ = TOOL.analyze(str(sandbox))
    assert defects, "`-text` 不成对却没人报错 ⇒ 守卫没牙"
    assert "B/-text 未成对" in _defect_checks(defects), _reasons(defects)


def test_m5_non_doc_file_in_memory_must_be_flagged(sandbox: Path) -> None:
    """M5：往 `manu/memory/` 塞 `.pkl` ⇒ 必须报「C/载体类型越界」（版本库只放源）。"""
    (sandbox / "manu" / "memory" / "weights.pkl").write_bytes(b"\x80\x04pkl")
    defects, _ = TOOL.analyze(str(sandbox))
    assert defects, "记忆树里混进运行产物却没人报错 ⇒ 守卫没牙"
    assert "C/载体类型越界" in _defect_checks(defects), _reasons(defects)


def test_m6_market_csv_in_records_must_be_flagged(sandbox: Path) -> None:
    """M6：往 `records/` 塞行情 `.csv` ⇒ 必须报「C/载体类型越界」（不许收编运行产物）。"""
    (sandbox / "manu" / "memory" / "records" / "quotes_20261119.csv").write_text(
        "日期,收盘\n2026-11-19,0.915\n", encoding="utf-8"
    )
    defects, _ = TOOL.analyze(str(sandbox))
    assert defects, "records/ 混进行情 csv 却没人报错 ⇒ 守卫没牙"
    assert "C/载体类型越界" in _defect_checks(defects), _reasons(defects)


def test_m7_oversize_record_must_be_flagged(sandbox: Path) -> None:
    """M7：往 `records/` 塞超大文件 ⇒ 必须报「D/单文件超限」（只写路径，不拷大产物）。"""
    target = sandbox / "manu" / "memory" / "records" / "big.md"
    target.write_bytes(b"x" * (TOOL.MAX_RECORD_FILE_BYTES + 1))
    defects, _ = TOOL.analyze(str(sandbox))
    assert defects, "records/ 混进超大文件却没人报错 ⇒ 守卫没牙"
    assert "D/单文件超限" in _defect_checks(defects), _reasons(defects)


def test_m8_missing_ledger_must_be_flagged(sandbox: Path) -> None:
    """M8：台账不见了 ⇒ 必须报「A2/台账缺失」（复盘的唯一结构化载体没了）。"""
    (sandbox / "manu" / "memory" / "records" / "etf_rotation_daily_ledger.csv").unlink()
    defects, _ = TOOL.analyze(str(sandbox))
    assert defects, "台账缺失却没人报错 ⇒ 守卫没牙"
    assert "A2/台账缺失" in _defect_checks(defects), _reasons(defects)


def test_m9_tool_is_read_only() -> None:
    """M9：工具只读 ⇒ 跑完记忆树 / `.gitignore` / `.gitattributes` 的 md5 必须一字不变。"""
    memory_root = PROJECT_DIR / "manu" / "memory"
    before_tree = _tree_md5(memory_root)
    before_gitignore = _md5(PROJECT_DIR / ".gitignore")
    before_attributes = _md5(PROJECT_DIR / ".gitattributes")
    TOOL.analyze(str(PROJECT_DIR))
    assert before_tree == _tree_md5(memory_root), "只读工具竟然改了记忆树"
    assert before_gitignore == _md5(PROJECT_DIR / ".gitignore"), "只读工具竟然改了 `.gitignore`"
    assert before_attributes == _md5(PROJECT_DIR / ".gitattributes"), "只读工具竟然改了 `.gitattributes`"


def test_m10_not_a_git_worktree_is_not_applicable(tmp_path: Path) -> None:
    """M10：不在 git 工作区 ⇒ **不适用**（rc 2），不许当成缺陷、不许当成绿。"""
    plain = tmp_path / "plain"
    (plain / "manu" / "memory" / "records").mkdir(parents=True)
    (plain / "manu" / "memory" / "records" / "etf_rotation_daily_ledger.csv").write_text(
        "日期\n2026-11-19\n", encoding="utf-8"
    )
    with pytest.raises(TOOL.NotApplicable):
        TOOL.analyze(str(plain))


def test_m11_memory_docs_must_not_be_flagged(sandbox: Path) -> None:
    """M11（反例）：逐日 `.md` 与 `archive/*.md.bak.*` 备份 ⇒ 必须**零误伤**（rc 0）。"""
    (sandbox / "manu" / "memory" / "records" / "20261120.md").write_text(
        "# 2026-11-20 记录\n", encoding="utf-8"
    )
    archive = sandbox / "manu" / "memory" / "archive"
    archive.mkdir(parents=True, exist_ok=True)
    (archive / "rules.md.bak.20261120000000").write_text("备份\n", encoding="utf-8")
    defects, _ = TOOL.analyze(str(sandbox))
    assert not defects, "记忆文档被误判成违规载体 ⇒ 守卫误伤：" + _reasons(defects)


# --------------------------------------------------------------------------- #
# 接线反证（`§十四.122` 第 7 条 ＋ 姊妹闸门 `test_record_ledger_guard.py` M15/M16 同体例）
# --------------------------------------------------------------------------- #
def _load_sync_module():
    """按路径加载 `manu/tools/sync_slim_memory.py`（四层同步闸门本体）。"""
    path = PROJECT_DIR / "manu" / "tools" / "sync_slim_memory.py"
    spec = importlib.util.spec_from_file_location("sync_slim_memory_under_test", path)
    assert spec and spec.loader
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


def test_m12_wiring_propagates_carrier_defects(monkeypatch) -> None:
    """M12：四层闸门必须真的接上载体闸门 ⇒ 载体坏了 ⇒ 四层闸门必须红（不许只靠人记）。

    🔴 用**假缺陷**验证接线会传红（**不碰真实台账/记忆树** ⇒ 不污染仓库文件）。
    """
    sync_mod = _load_sync_module()
    assert hasattr(sync_mod, "_check_record_carrier"), (
        "sync_slim_memory.py 没接载体闸门 ⇒ §十四.122 第 7 条只靠人记"
    )
    tools_root = str(PROJECT_DIR / "manu")
    if tools_root not in sys.path:
        sys.path.insert(0, tools_root)
    import tools.check_record_carrier as checker_mod  # noqa: PLC0415

    monkeypatch.setattr(
        checker_mod,
        "analyze",
        lambda *args, **kwargs: (
            [checker_mod.Defect("台账", 1, "Z-假缺陷", "接线反证用的假缺陷", "")],
            checker_mod.CarrierFacts(),
        ),
    )
    problems = sync_mod._check_record_carrier()
    assert problems, "载体有缺陷，四层闸门却没红 ⇒ 接线没生效"
    # 接线会把缺陷的 **reason** 拼进问题清单 ⇒ 这里断言 reason 文本（不是 check 标签）
    assert "接线反证用的假缺陷" in problems[0], problems[0]


def test_m12b_wiring_call_site_makes_the_whole_gate_red(monkeypatch, capsys) -> None:
    """M12b：🔴 **调用点**也必须被守住：载体有缺陷 ⇒ **整个 `check()` 闸门**必须 rc=1。

    🔴 为什么不能只测 `_check_record_carrier()`：**把 `check()` 里的调用行删掉**，
    函数本身照样返回问题清单 ⇒ 只测函数的那条**测不出调用点被摘掉**这一形态
    （本轮实测：摘掉调用行后仍 17 passed ⇒ 守卫没牙）。
    ⇒ 这里必须走**整闸门** `check()`，退出码必须是 1，且 stderr 必须点名。
    """
    sync_mod = _load_sync_module()
    tools_root = str(PROJECT_DIR / "manu")
    if tools_root not in sys.path:
        sys.path.insert(0, tools_root)
    import tools.check_record_carrier as checker_mod  # noqa: PLC0415

    monkeypatch.setattr(
        checker_mod,
        "analyze",
        lambda *args, **kwargs: (
            [checker_mod.Defect("台账", 1, "Z-假缺陷", "接线反证用的假缺陷", "")],
            checker_mod.CarrierFacts(),
        ),
    )
    exit_code = sync_mod.check()
    captured = capsys.readouterr()
    assert exit_code == 1, "载体有缺陷，整闸门却没红 ⇒ 接线调用点被摘掉了：%s" % captured.err
    assert "接线反证用的假缺陷" in captured.err, captured.err


def test_m13_wiring_is_green_on_live_project() -> None:
    """M13：真实工程上，四层闸门（**整闸门** `check()`）必须 rc=0。"""
    sync_mod = _load_sync_module()
    assert sync_mod.check() == 0, "真实工程上四层闸门竟然报红"


def test_m14_backup_shape_must_be_strict(sandbox: Path) -> None:
    """M14：备份只认「`archive/` ＋ `.md.bak.<戳>` ＋ 名字不含产物扩展名」这一种形态。

    🔴 收紧前 `x.pkl.md.bak.1`（子串匹配）会被当成记忆文档放行 ⇒ 伪装成备份的运行产物能进库。
    """
    archive = sandbox / "manu" / "memory" / "archive"
    archive.mkdir(parents=True, exist_ok=True)
    # 伪装：产物扩展名藏在备份名里
    (archive / "weights.pkl.md.bak.1").write_text("x", encoding="utf-8")
    defects, _ = TOOL.analyze(str(sandbox))
    assert defects, "伪装成备份的 `.pkl` 却没人报错 ⇒ 守卫没牙"
    assert "C/载体类型越界" in _defect_checks(defects), _reasons(defects)

    # 备份放错目录（不在 archive/）⇒ 同样越界
    shutil.rmtree(archive)
    (sandbox / "manu" / "memory" / "rules.md.bak.20261120000000").write_text("x", encoding="utf-8")
    defects, _ = TOOL.analyze(str(sandbox))
    assert defects, "备份放在 archive/ 之外却没人报错 ⇒ 守卫没牙"
    assert "C/载体类型越界" in _defect_checks(defects), _reasons(defects)

    # 合法形态 ⇒ 零误伤（archive/ 已在上一段删掉 ⇒ 这里重建；上一段的越界文件也要先清掉）
    (sandbox / "manu" / "memory" / "rules.md.bak.20261120000000").unlink()
    archive.mkdir(parents=True, exist_ok=True)
    (archive / "rules.md.bak.20261120000000").write_text("x", encoding="utf-8")
    defects, _ = TOOL.analyze(str(sandbox))
    assert not defects, "合法备份被误判成违规载体 ⇒ 守卫误伤：" + _reasons(defects)
