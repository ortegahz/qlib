"""🔴 2026-11-10 新增：架构迁移（canonical → 每次运行一个不可变快照目录）的两个副作用回归。

【背景】
每日链架构从「canonical 根目录全局共享文件」迁到「每次运行一个不可变快照目录
``<canonical>/snapshots/<YYYYMMDD_HHMMSS>/``」⇒ 快照**跑完即冻结**，往里写任何东西
都破了不变式。迁移暴露出两个副作用（主 agent 已修，本文件把它们钉死）：

  ① `verify_artifacts.py` 的自检日志原本写死 ``<run-root>/verify_artifacts.log``
     ⇒ run-root = 快照目录时，**自检日志被写进不可变快照**（每跑一次就污染一次快照）。
     修法：快照 ⇒ 日志落 canonical 根 ``verify_artifacts_<快照名>.log``；
     非快照 ⇒ 旧口径 ``<run-root>/verify_artifacts.log`` 不变；``--log-path`` 显式优先。
  ② `probe_llm_stage_repro.py` 的 P2 指纹表解析只认 ``<run-root>/_ms5_md5.txt``
     ⇒ 断言对象从 canonical 迁到快照后，指纹表（住在 canonical 根）**找不到** ⇒ P2 误红。
     修法：快照 run-root ⇒ 回落 canonical 根的指纹表（快照自己那份优先）。

外加 `manu/tools/regen_fingerprint_table.py` 的快照守卫：指纹表（**与它的备份**）
不许落进快照目录 ⇒ 命中即 rc=2 且**不写盘**。

【测试风格】
照抄本目录既有风格：被测模块用 `importlib` 按**路径**加载（`probes/` 与 `manu/tools/`
不在 `sys.path` 的默认位置）⇒ 见 `test_render_table_blocks.py`；
`sys.path` 注入沿用本目录 `conftest.py` 的做法（把 `manu/etf_rotation/` 挂进去）。

【变异反证】
每条断言都配一个**变异反证**测试：把源码里的关键判定改坏
（改的是 **tmp 目录里的源码副本**，绝不碰主 agent 的源文件）⇒ 对应断言必须转红
（`pytest.raises(AssertionError)`）⇒ 证明这些测试有牙，不是空跑。
"""

from __future__ import annotations

import hashlib
import importlib.util
import sys
from pathlib import Path

import pytest

#: `manu/etf_rotation/` 目录（照抄本目录 `conftest.py` 的做法：挂进 `sys.path`，
#: 让 `verify_artifacts` 能 `import report_fingerprint`、探针能 `import config`）
ETF_DIR = Path(__file__).resolve().parents[1]
if str(ETF_DIR) not in sys.path:
    sys.path.insert(0, str(ETF_DIR))

#: `manu/etf_rotation/probes/` 目录
PROBES_DIR = ETF_DIR / "probes"
#: `manu/tools/` 目录
TOOLS_DIR = ETF_DIR.parent / "tools"
#: 仓库根（`manu/etf_rotation/` 上溯两级）
REPO_ROOT = ETF_DIR.parent.parent

#: 快照目录的标志文件 —— **写字面量**（故意不用被测模块的常量：
#: 常量被改名/改值时，下面的快照夹具就不再被识别 ⇒ 测试必须转红）
SNAPSHOT_MANIFEST = "snapshot_manifest.json"
#: 指纹表文件名（canonical 根的「代码状态登记表」）
FINGERPRINT_TABLE = "_ms5_md5.txt"


def _load_module(name: str, path: Path):
    """按**路径**加载模块（被测模块不在 `sys.path` 的默认位置）。"""
    spec = importlib.util.spec_from_file_location(name, path)
    assert spec is not None and spec.loader is not None, f"加载不了模块：{path}"
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


VERIFY = _load_module("snapfx_verify_artifacts", ETF_DIR / "verify_artifacts.py")
PROBE = _load_module("snapfx_probe_llm_stage_repro", PROBES_DIR / "probe_llm_stage_repro.py")
REGEN = _load_module("snapfx_regen_fingerprint_table", TOOLS_DIR / "regen_fingerprint_table.py")


def _mutated_module(tmp_path: Path, tag: str, src_path: Path, old: str, new: str):
    """把源码**副本**里的关键判定改坏 ⇒ 返回改坏后的模块（**不碰真源码**）。"""
    text = src_path.read_text(encoding="utf-8")
    assert old in text, f"变异点没找到（源码已变？）：{old!r}"
    mutated_path = tmp_path / f"{tag}{src_path.name}"
    mutated_path.write_text(text.replace(old, new, 1), encoding="utf-8")
    return _load_module(f"snapfx_mutated_{tag}", mutated_path)


def _make_snapshot(canonical: Path, stamp: str = "20261110_120000") -> Path:
    """搭一个 canonical 根 + 其下的不可变快照目录（有 manifest ⇒ 是快照）。"""
    snapshot = canonical / "snapshots" / stamp
    snapshot.mkdir(parents=True)
    (snapshot / SNAPSHOT_MANIFEST).write_text("{}\n", encoding="utf-8")
    return snapshot


def _assert_outside_snapshot(path: Path, snapshot: Path) -> None:
    """落点不许在不可变快照目录内。"""
    assert snapshot not in path.parents, (
        f"落点 {path} 落在不可变快照 {snapshot} 内 ⇒ 破了「跑完即冻结」不变式"
    )


def _assert_equals(actual, expected, what: str) -> None:
    assert actual == expected, f"{what}：实得 {actual}，期望 {expected}"


def _assert_rc(actual: int, expected: int, what: str) -> None:
    assert actual == expected, f"{what}：实得 rc={actual}，期望 rc={expected}"


def _assert_no_new_files(directory: Path, before: list[str]) -> None:
    now = sorted(p.name for p in directory.iterdir())
    assert now == sorted(before), f"目录 {directory} 的文件集变了：{sorted(before)} → {now}"


def _assert_main_log_placement(snapshot: Path, canonical: Path, rc: int, before: list[str]) -> None:
    """`verify_artifacts.main()` 端到端的落点断言（供变异反证复用）。"""
    assert rc != 0, "缺交付件 ⇒ 自检必须 FAIL（属正常）；rc=0 说明断言没生效"
    _assert_no_new_files(snapshot, before)
    log = canonical / f"verify_artifacts_{snapshot.name}.log"
    assert log.is_file(), f"canonical 根应有自检日志：{log}"


# ---------------------------------------------------------------- 修复 ①：自检日志不许写进快照


def test_snapshot_manifest_and_log_name_constants() -> None:
    """快照标志文件 / 日志文件名的契约（改了就红）。"""
    _assert_equals(VERIFY.SNAPSHOT_MANIFEST, SNAPSHOT_MANIFEST, "快照标志文件名")
    _assert_equals(VERIFY.LOG_NAME, "verify_artifacts.log", "自检日志文件名")
    _assert_equals(REGEN.SNAPSHOT_MANIFEST, SNAPSHOT_MANIFEST, "regen 的快照标志文件名")
    _assert_equals(REGEN.TABLE_NAME, FINGERPRINT_TABLE, "regen 的指纹表文件名")


def test_resolve_log_path_snapshot_lands_in_canonical_root(tmp_path: Path) -> None:
    """快照 run-root ⇒ 日志落 canonical 根（**不在**快照内）。"""
    canonical = tmp_path / "canonical"
    snapshot = _make_snapshot(canonical)
    resolved = VERIFY._resolve_log_path(snapshot)
    _assert_equals(resolved, canonical / f"verify_artifacts_{snapshot.name}.log", "快照的日志落点")
    _assert_outside_snapshot(resolved, snapshot)


def test_resolve_log_path_non_snapshot_keeps_legacy_location(tmp_path: Path) -> None:
    """非快照 run-root ⇒ 旧口径 ``<run-root>/verify_artifacts.log`` 不变。"""
    run_root = tmp_path / "plain_run_root"
    run_root.mkdir()
    resolved = VERIFY._resolve_log_path(run_root)
    _assert_equals(resolved, run_root / "verify_artifacts.log", "非快照的日志落点")


def test_resolve_log_path_explicit_wins_over_both(tmp_path: Path) -> None:
    """``--log-path`` 显式指定 ⇒ 优先于「快照/非快照」两条自动规则。"""
    explicit = tmp_path / "explicit" / "my_own.log"
    snapshot = _make_snapshot(tmp_path / "canonical")
    plain = tmp_path / "plain_run_root"
    plain.mkdir()
    _assert_equals(VERIFY._resolve_log_path(snapshot, str(explicit)), explicit, "快照 + 显式落点")
    _assert_equals(VERIFY._resolve_log_path(plain, str(explicit)), explicit, "非快照 + 显式落点")


def test_resolve_log_path_snapshot_under_odd_parent_uses_parent(tmp_path: Path) -> None:
    """快照的父目录**不叫** ``snapshots`` ⇒ canonical 取 ``run_root.parent``。"""
    weird = tmp_path / "not_named_snapshots"
    snapshot = weird / "20261110_120000"
    snapshot.mkdir(parents=True)
    (snapshot / SNAPSHOT_MANIFEST).write_text("{}\n", encoding="utf-8")
    resolved = VERIFY._resolve_log_path(snapshot)
    _assert_equals(resolved, weird / f"verify_artifacts_{snapshot.name}.log", "怪父目录下的日志落点")
    _assert_equals(resolved.parent, snapshot.parent, "canonical 根的取法")


def test_main_writes_log_to_canonical_root_not_into_snapshot(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    """端到端：拿临时快照目录当 ``--run-root`` 跑一次 ⇒ 日志落 canonical 根，快照里不多东西。"""
    canonical = tmp_path / "canonical"
    snapshot = _make_snapshot(canonical)
    before = sorted(p.name for p in snapshot.iterdir())
    monkeypatch.setattr(sys, "argv", ["verify_artifacts.py", "--run-root", str(snapshot)])
    rc = VERIFY.main()
    out = capsys.readouterr().out
    _assert_main_log_placement(snapshot, canonical, rc, before)
    assert "verify_artifacts.log" not in [p.name for p in snapshot.iterdir()]
    assert "自检日志落点" in out, f"自检输出里应写明日志落点，实得：{out}"


def test_mutation_log_path_into_snapshot_turns_assertion_red(tmp_path: Path) -> None:
    """变异反证 ①：canonical 根算错（日志落回快照）⇒ 落点断言必须转红。"""
    canonical = tmp_path / "canonical"
    snapshot = _make_snapshot(canonical)
    mutated = _mutated_module(
        tmp_path,
        "logpath_into_snapshot",
        ETF_DIR / "verify_artifacts.py",
        '    canonical = run_root.parent.parent if run_root.parent.name == "snapshots" else run_root.parent',
        "    canonical = run_root",
    )
    mutated_resolved = mutated._resolve_log_path(snapshot)
    assert mutated_resolved.parent == snapshot, "变异点没生效（日志没落回快照）"
    with pytest.raises(AssertionError):
        _assert_outside_snapshot(mutated_resolved, snapshot)


def test_mutation_main_log_into_snapshot_turns_e2e_red(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    """变异反证 ②：端到端跑变异版 ⇒ 「日志不在快照里」这条端到端断言必须转红。"""
    canonical = tmp_path / "canonical"
    snapshot = _make_snapshot(canonical)
    before = sorted(p.name for p in snapshot.iterdir())
    mutated = _mutated_module(
        tmp_path,
        "logpath_into_snapshot",
        ETF_DIR / "verify_artifacts.py",
        '    canonical = run_root.parent.parent if run_root.parent.name == "snapshots" else run_root.parent',
        "    canonical = run_root",
    )
    monkeypatch.setattr(sys, "argv", ["verify_artifacts.py", "--run-root", str(snapshot)])
    rc = mutated.main()
    capsys.readouterr()
    assert mutated._resolve_log_path(snapshot).parent == snapshot, "变异点没生效"
    with pytest.raises(AssertionError):
        _assert_main_log_placement(snapshot, canonical, rc, before)


def test_mutation_explicit_log_path_ignored_turns_assertion_red(tmp_path: Path) -> None:
    """变异反证 ③：显式 ``--log-path`` 被忽略 ⇒ 「显式优先」断言必须转红。"""
    snapshot = _make_snapshot(tmp_path / "canonical")
    explicit = tmp_path / "explicit.log"
    mutated = _mutated_module(
        tmp_path,
        "explicit_ignored",
        ETF_DIR / "verify_artifacts.py",
        "    if explicit:\n        return Path(explicit)",
        "    if explicit:\n        return run_root / LOG_NAME",
    )
    mutated_resolved = mutated._resolve_log_path(snapshot, str(explicit))
    assert mutated_resolved != explicit, "变异点没生效（显式落点被忽略了）"
    with pytest.raises(AssertionError):
        _assert_equals(mutated_resolved, explicit, "显式落点")


# ---------------------------------------------------------------- 修复 ②：P2 指纹表解析


def test_resolve_fingerprint_path_snapshot_prefers_canonical_table(tmp_path: Path) -> None:
    """快照 run-root + canonical 根有指纹表 ⇒ 返回 canonical 那份。"""
    canonical = tmp_path / "canonical"
    snapshot = _make_snapshot(canonical)
    table = canonical / FINGERPRINT_TABLE
    table.write_text("# canonical 根的指纹表\n", encoding="utf-8")
    _assert_equals(PROBE._resolve_fingerprint_path(snapshot), table, "指纹表落点")


def test_resolve_fingerprint_path_snapshot_own_table_wins(tmp_path: Path) -> None:
    """快照里自己也有指纹表 ⇒ 优先快照自己的（run-root 直读优先于 canonical 回落）。"""
    canonical = tmp_path / "canonical"
    snapshot = _make_snapshot(canonical)
    (canonical / FINGERPRINT_TABLE).write_text("# canonical\n", encoding="utf-8")
    own = snapshot / FINGERPRINT_TABLE
    own.write_text("# snapshot\n", encoding="utf-8")
    _assert_equals(PROBE._resolve_fingerprint_path(snapshot), own, "指纹表落点")


def test_resolve_fingerprint_path_explicit_wins(tmp_path: Path) -> None:
    """``--fingerprint-table`` 显式指定 ⇒ 优先于自动解析。"""
    snapshot = _make_snapshot(tmp_path / "canonical")
    explicit = tmp_path / "explicit_ms5.txt"
    explicit.write_text("# 显式指定\n", encoding="utf-8")
    _assert_equals(PROBE._resolve_fingerprint_path(snapshot, str(explicit)), explicit, "显式指纹表落点")


def test_resolve_fingerprint_path_missing_falls_back_and_check_fails(tmp_path: Path) -> None:
    """哪儿都没有指纹表 ⇒ 回落 ``run_root/_ms5_md5.txt``，且 P2 判 FAIL、文案说明「不在快照里」。"""
    canonical = tmp_path / "canonical"
    snapshot = _make_snapshot(canonical)
    resolved = PROBE._resolve_fingerprint_path(snapshot)
    _assert_equals(resolved, snapshot / FINGERPRINT_TABLE, "兜底指纹表落点")
    ok, detail = PROBE._check_fingerprint(snapshot, REPO_ROOT)
    assert ok is False, "指纹表不存在 ⇒ P2 必须 FAIL"
    assert "不在快照里" in detail, f"报错文案须说明指纹表不在快照里，实得：{detail}"


def test_mutation_fingerprint_canonical_candidate_dropped_turns_assertion_red(tmp_path: Path) -> None:
    """变异反证 ④：canonical 回落候选被去掉 ⇒ 「返回 canonical 那份」必须转红。"""
    canonical = tmp_path / "canonical"
    snapshot = _make_snapshot(canonical)
    table = canonical / FINGERPRINT_TABLE
    table.write_text("# canonical 根的指纹表\n", encoding="utf-8")
    mutated = _mutated_module(
        tmp_path,
        "fingerprint_no_canonical",
        PROBES_DIR / "probe_llm_stage_repro.py",
        "    if run_root.parent.name == SNAPSHOT_DIR_NAME:",
        '    if run_root.parent.name == "__never__":',
    )
    mutated_resolved = mutated._resolve_fingerprint_path(snapshot)
    assert mutated_resolved != table, "变异点没生效（canonical 候选还在）"
    with pytest.raises(AssertionError):
        _assert_equals(mutated_resolved, table, "指纹表落点")


def test_mutation_fingerprint_explicit_ignored_turns_assertion_red(tmp_path: Path) -> None:
    """变异反证 ⑤：显式指纹表被忽略 ⇒ 「显式优先」必须转红。"""
    snapshot = _make_snapshot(tmp_path / "canonical")
    explicit = tmp_path / "explicit_ms5.txt"
    explicit.write_text("# 显式指定\n", encoding="utf-8")
    mutated = _mutated_module(
        tmp_path,
        "fingerprint_explicit_ignored",
        PROBES_DIR / "probe_llm_stage_repro.py",
        "    if explicit:\n        return Path(explicit)",
        "    if explicit:\n        return run_root / FINGERPRINT_FILE",
    )
    mutated_resolved = mutated._resolve_fingerprint_path(snapshot, str(explicit))
    assert mutated_resolved != explicit, "变异点没生效（显式落点被忽略了）"
    with pytest.raises(AssertionError):
        _assert_equals(mutated_resolved, explicit, "显式指纹表落点")


# ---------------------------------------------------------------- 修复 ③：指纹表重生成的快照守卫


def test_regen_refuses_to_land_table_in_snapshot(tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
    """``--artifact-root`` 指向快照 ⇒ 退出码 2，且快照目录里**没有任何新文件**。"""
    canonical = tmp_path / "canonical"
    snapshot = _make_snapshot(canonical)
    before = sorted(p.name for p in snapshot.iterdir())
    rc = REGEN.main(["--artifact-root", str(snapshot), "--repo-root", str(REPO_ROOT)])
    out = capsys.readouterr().out
    _assert_rc(rc, 2, "指纹表落点在快照里 ⇒ 必须 rc=2")
    assert "[FAIL]" in out, f"失败提示须是中文 [FAIL]，实得：{out}"
    assert "快照" in out, f"失败提示须点明「快照」，实得：{out}"
    _assert_no_new_files(snapshot, before)


def test_regen_guard_blocks_explicit_table_inside_snapshot(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    """守卫 ① 单独验证：artifact-root 不是快照，但 ``--table`` 指向快照里 ⇒ 同样 rc=2。"""
    canonical = tmp_path / "canonical"
    snapshot = _make_snapshot(canonical)
    table_in_snapshot = snapshot / FINGERPRINT_TABLE
    table_in_snapshot.write_text("# 快照里不该有的指纹表\n", encoding="utf-8")
    before = sorted(p.name for p in snapshot.iterdir())
    rc = REGEN.main(
        ["--artifact-root", str(canonical), "--table", str(table_in_snapshot), "--repo-root", str(REPO_ROOT)]
    )
    out = capsys.readouterr().out
    _assert_rc(rc, 2, "指纹表落点在快照里 ⇒ 必须 rc=2")
    assert "[FAIL]" in out, f"失败提示须是中文 [FAIL]，实得：{out}"
    _assert_no_new_files(snapshot, before)


def test_regen_allows_canonical_table(tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
    """``--table`` 指向 canonical 根的指纹表 ⇒ **不触发**快照守卫（不再是 rc=2）。"""
    canonical = tmp_path / "canonical"
    canonical.mkdir(parents=True)
    table = canonical / FINGERPRINT_TABLE
    table.write_text("# 只有注释、没有登记项 ⇒ 解析失败，但**不是**快照守卫\n", encoding="utf-8")
    rc = REGEN.main(
        ["--artifact-root", str(canonical), "--table", str(table), "--repo-root", str(REPO_ROOT)]
    )
    out = capsys.readouterr().out
    assert rc != 2, f"canonical 根的指纹表不该触发快照守卫，实得 rc={rc}"
    assert "不可变快照" not in out, f"不该报快照守卫的错，实得：{out}"


def _build_regen_fixture(tmp_path: Path) -> tuple[Path, Path, Path]:
    """搭一套能让 `regen_fingerprint_table` 跑通的重生成夹具。

    返回 ``(repo_root, canonical 根, 指纹表路径)``；sha1 段的相对路径基准是
    ``<canonical>/deliverables``（**故意与指纹表不在同一目录** ⇒ 才能验证
    「备份落 table.parent，而不是落 artifact_root」）。
    """
    repo_root = tmp_path / "repo"
    repo_root.mkdir()
    (repo_root / "src.py").write_text("print('x')\n", encoding="utf-8")
    canonical = tmp_path / "canonical"
    deliverables = canonical / "deliverables"
    deliverables.mkdir(parents=True)
    (deliverables / "out.json").write_text("{}\n", encoding="utf-8")
    table = canonical / FINGERPRINT_TABLE
    md5 = hashlib.new("md5", (repo_root / "src.py").read_bytes()).hexdigest()
    sha1 = hashlib.new("sha1", (deliverables / "out.json").read_bytes()).hexdigest()
    table.write_text(f"{md5}  src.py\n{sha1}  out.json\n", encoding="utf-8")
    return repo_root, canonical, table


def test_regen_backup_lands_next_to_table_not_in_artifact_root(tmp_path: Path) -> None:
    """备份文件落 ``table.parent``（不是 ``artifact_root``）⇒ 快照目录不会被写脏。"""
    repo_root, canonical, table = _build_regen_fixture(tmp_path)
    deliverables = canonical / "deliverables"
    rc = REGEN.main(
        ["--artifact-root", str(deliverables), "--table", str(table), "--repo-root", str(repo_root)]
    )
    _assert_rc(rc, 0, "夹具本身应能跑通重生成")
    backups = sorted(p.name for p in canonical.glob("_backup_preFPRegen_*"))
    assert len(backups) == 1, f"备份应落在表旁边（canonical 根），实得 {backups}"
    assert backups[0].endswith(FINGERPRINT_TABLE), f"备份名应以指纹表名收尾，实得 {backups[0]}"
    assert not list(deliverables.glob("_backup_preFPRegen_*")), "备份不许落进 artifact_root"


def test_mutation_regen_snapshot_guard_disabled_turns_assertion_red(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    """变异反证 ⑥：快照守卫失效 ⇒ 「rc=2 且不写盘」必须转红。"""
    canonical = tmp_path / "canonical"
    snapshot = _make_snapshot(canonical)
    before = sorted(p.name for p in snapshot.iterdir())
    mutated = _mutated_module(
        tmp_path,
        "regen_no_guard",
        TOOLS_DIR / "regen_fingerprint_table.py",
        "    return (path / SNAPSHOT_MANIFEST).is_file()",
        "    return False",
    )
    rc = mutated.main(["--artifact-root", str(snapshot), "--repo-root", str(REPO_ROOT)])
    capsys.readouterr()
    assert rc != 2, "变异点没生效（快照守卫还在）"
    with pytest.raises(AssertionError):
        _assert_rc(rc, 2, "指纹表落点在快照里 ⇒ 必须 rc=2")
    _assert_no_new_files(snapshot, before)


def test_mutation_regen_backup_into_artifact_root_turns_assertion_red(tmp_path: Path) -> None:
    """变异反证 ⑦：备份落点改回 ``artifact_root`` ⇒ 「备份落表旁边」必须转红。"""
    repo_root, canonical, table = _build_regen_fixture(tmp_path)
    deliverables = canonical / "deliverables"
    mutated = _mutated_module(
        tmp_path,
        "regen_backup_in_artifact_root",
        TOOLS_DIR / "regen_fingerprint_table.py",
        '    backup = table.parent / f"_backup_preFPRegen_{stamp}_{TABLE_NAME}"',
        '    backup = artifact_root / f"_backup_preFPRegen_{stamp}_{TABLE_NAME}"',
    )
    rc = mutated.main(
        ["--artifact-root", str(deliverables), "--table", str(table), "--repo-root", str(repo_root)]
    )
    assert rc == 0, f"变异点没生效（重生成本身应仍成功），实得 rc={rc}"
    assert list(deliverables.glob("_backup_preFPRegen_*")), "变异点没生效（备份没落进 artifact_root）"
    with pytest.raises(AssertionError):
        assert not list(deliverables.glob("_backup_preFPRegen_*")), "备份不许落进 artifact_root"
