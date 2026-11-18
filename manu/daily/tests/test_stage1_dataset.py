"""`stage1_dataset.py` 的 pytest 自检（**完全离线**：不联网、不连服务器、不换真库）。

【覆盖清单（对应交付规格）】
1. `compare_calendars` 前缀一致 ⇒ `safe_to_swap=True`；
2. `compare_calendars` 前缀不一致 ⇒ `safe_to_swap=False` + `phase_shift=True`；
3. `swap_provider` 前缀不一致 ⇒ **抛异常**且**磁盘没被动过**；
4. `swap_provider` 成功 ⇒ 新库生效、旧库备份存在；
5. `dry_run=True` ⇒ **磁盘零改动**；
6. `extract_snapshot` 缺子目录 ⇒ 抛异常。

外加：`inspect_provider` 计数、`extract_snapshot` 正常路径、`fetch_snapshot` 复用下载器、
`verify_provider` 在 qlib 不可用时**抛异常**（不许静默跳过）、
`swap_provider` 换库中途失败 ⇒ **自动回滚**、`run_stage1` 离线全链路。

【为什么可以造假 provider】
qlib 的 provider 结构极简：`calendars/day.txt` + `features/<code>/<field>.day.bin`
（首 float32 = 起始日历下标）+ `instruments/all.txt` ⇒ 用 `tarfile` 现场造一个极小的
tarball 就能覆盖全部逻辑路径，不需要真数据集、不需要联网。
"""
from __future__ import annotations

import sys
import tarfile
from pathlib import Path

import numpy as np
import pytest

_DAILY_DIR = Path(__file__).resolve().parents[1]
if str(_DAILY_DIR) not in sys.path:
    sys.path.insert(0, str(_DAILY_DIR))

import stage1_dataset as SD  # noqa: E402

#: 现役库的模拟日历（10 个交易日）
_CAL_OLD = [
    "2026-11-02",
    "2026-11-03",
    "2026-11-04",
    "2026-11-05",
    "2026-11-06",
    "2026-11-09",
    "2026-11-10",
    "2026-11-11",
    "2026-11-12",
    "2026-11-13",
]
#: 新快照的模拟日历（旧 + 4 个交易日 ⇒ 严格前缀扩展）
_CAL_NEW = _CAL_OLD + ["2026-11-16", "2026-11-17", "2026-11-18", "2026-11-19"]
#: 前缀被破坏的日历（把第 3 天换掉 ⇒ 历史下标右移）
_CAL_BROKEN = list(_CAL_OLD)
_CAL_BROKEN[2] = "2026-11-05"

_FIELDS = ("close", "volume", "factor")
_INSTRUMENTS = ("sh600000", "sz000001")


def _make_provider(
    root: Path, days: list[str], instruments: tuple[str, ...] = _INSTRUMENTS
) -> Path:
    """造一个最小可用的 qlib provider 目录。"""
    (root / "calendars").mkdir(parents=True, exist_ok=True)
    (root / "calendars" / "day.txt").write_text("\n".join(days) + "\n", encoding="utf-8")

    (root / "instruments").mkdir(parents=True, exist_ok=True)
    (root / "instruments" / "all.txt").write_text(
        "\n".join(f"{inst}\t{days[0]}\t{days[-1]}" for inst in instruments) + "\n",
        encoding="utf-8",
    )

    for idx, inst in enumerate(instruments):
        inst_dir = root / "features" / inst
        inst_dir.mkdir(parents=True, exist_ok=True)
        for f_idx, field in enumerate(_FIELDS):
            values = np.arange(1, len(days) + 1, dtype="<f4") * (10 + f_idx + idx)
            arr = np.hstack([np.array([0], dtype="<f4"), values]).astype("<f4")
            arr.tofile(inst_dir / f"{field}.day.bin")
    return root


def _make_tarball(provider_root: Path, tar_path: Path, *, top: str = "qlib_bin") -> Path:
    """把 provider 目录打成 tar.gz（模拟 `qlib_bin.tar.gz`）。"""
    tar_path.parent.mkdir(parents=True, exist_ok=True)
    with tarfile.open(tar_path, "w:gz") as tf:
        tf.add(provider_root, arcname=top)
    return tar_path


def _snapshot_of(root: Path) -> set[str]:
    """目录快照（相对路径集合）⇒ 用来断言「磁盘零改动」。"""
    return {str(p.relative_to(root)) for p in root.rglob("*")}


# =============================================================================
# ① inspect_provider
# =============================================================================
def test_inspect_provider_counts(tmp_path: Path) -> None:
    provider = _make_provider(tmp_path / "cn_data", _CAL_OLD)
    out = SD.inspect_provider(provider)
    assert out["ok"] is True
    assert out["calendar_first"] == "2026-11-02"
    assert out["calendar_max"] == "2026-11-13"
    assert out["n_days"] == len(_CAL_OLD)
    assert out["n_features"] == len(_INSTRUMENTS)
    assert out["n_instruments"] == len(_INSTRUMENTS)


def test_inspect_provider_missing_dir(tmp_path: Path) -> None:
    out = SD.inspect_provider(tmp_path / "nope")
    assert out["ok"] is False
    assert out["n_days"] == 0


# =============================================================================
# ② compare_calendars —— 相位闸门
# =============================================================================
def test_compare_calendars_prefix_identical_is_safe(tmp_path: Path) -> None:
    cur = _make_provider(tmp_path / "cur", _CAL_OLD)
    new = _make_provider(tmp_path / "new", _CAL_NEW)
    out = SD.compare_calendars(cur, new)
    assert out["prefix_identical"] is True
    assert out["phase_shift"] is False
    assert out["safe_to_swap"] is True
    assert out["new_days"] == ["2026-11-16", "2026-11-17", "2026-11-18", "2026-11-19"]
    assert out["current_max"] == "2026-11-13"
    assert out["new_max"] == "2026-11-19"


def test_compare_calendars_prefix_broken_is_unsafe(tmp_path: Path) -> None:
    cur = _make_provider(tmp_path / "cur", _CAL_OLD)
    new = _make_provider(tmp_path / "new", _CAL_BROKEN + ["2026-11-16"])
    out = SD.compare_calendars(cur, new)
    assert out["prefix_identical"] is False
    assert out["phase_shift"] is True
    assert out["safe_to_swap"] is False
    assert out["first_diff_index"] == 2
    assert out["first_diff_current"] == "2026-11-04"
    assert out["first_diff_new"] == "2026-11-05"


def test_compare_calendars_shorter_snapshot_is_unsafe(tmp_path: Path) -> None:
    """新快照比现役库还短 ⇒ 前缀不成立 ⇒ 拒绝（不许拿短的覆盖长的）。"""
    cur = _make_provider(tmp_path / "cur", _CAL_OLD)
    new = _make_provider(tmp_path / "new", _CAL_OLD[:6])
    out = SD.compare_calendars(cur, new)
    assert out["prefix_identical"] is False
    assert out["safe_to_swap"] is False
    assert out["current_len"] == 10
    assert out["new_len"] == 6


def test_compare_calendars_no_new_days_is_unsafe(tmp_path: Path) -> None:
    """前缀一致但没有新增交易日 ⇒ 换库收益为零 ⇒ 不换。"""
    cur = _make_provider(tmp_path / "cur", _CAL_OLD)
    new = _make_provider(tmp_path / "new", _CAL_OLD)
    out = SD.compare_calendars(cur, new)
    assert out["prefix_identical"] is True
    assert out["safe_to_swap"] is False
    assert "无新增交易日" in out["reason"]


# =============================================================================
# ②bis 日历自洽性（重复日 / 乱序）⇒ 拒绝换库
# =============================================================================
def test_compare_calendars_duplicate_days_is_unsafe(tmp_path: Path) -> None:
    """重复日 ⇒ qlib 的「下标 ↔ 日期」对应关系会错位 ⇒ 必须拒绝。"""
    cur = _make_provider(tmp_path / "cur", _CAL_OLD)
    dup = _CAL_OLD + ["2026-11-16", "2026-11-16"]
    new = _make_provider(tmp_path / "new", dup)
    out = SD.compare_calendars(cur, new)
    assert out["prefix_identical"] is True, "位置上前缀仍然一致"
    assert out["calendar_sane"] is False
    assert out["safe_to_swap"] is False
    assert "重复日" in out["reason"]


def test_compare_calendars_unsorted_is_unsafe(tmp_path: Path) -> None:
    cur = _make_provider(tmp_path / "cur", _CAL_OLD)
    unsorted_days = _CAL_OLD + ["2026-11-18", "2026-11-17"]
    new = _make_provider(tmp_path / "new", unsorted_days)
    out = SD.compare_calendars(cur, new)
    assert out["calendar_sane"] is False
    assert out["safe_to_swap"] is False
    assert "升序" in out["reason"]


def test_swap_provider_rejects_insane_calendar(tmp_path: Path) -> None:
    cur = _make_provider(tmp_path / "cur", _CAL_OLD)
    new = _make_provider(tmp_path / "new", _CAL_OLD + ["2026-11-16", "2026-11-16"])
    before = _snapshot_of(tmp_path)
    with pytest.raises(RuntimeError) as excinfo:
        SD.swap_provider(cur, new, backup_dir=tmp_path / "backup", dry_run=False)
    assert "拒绝换库" in str(excinfo.value)
    assert _snapshot_of(tmp_path) == before


def test_move_path_refuses_existing_destination(tmp_path: Path) -> None:
    """目标已存在时必须报错（否则 shutil.move 会把源塞进目标 ⇒ 静默嵌套损坏）。"""
    src = _make_provider(tmp_path / "src", _CAL_OLD)
    dst = _make_provider(tmp_path / "dst", _CAL_NEW)
    with pytest.raises(FileExistsError):
        SD._move_path(src, dst)
    assert (dst / "calendars" / "day.txt").exists(), "目标不能被改动"


# =============================================================================
# ③ swap_provider —— 前缀不一致 ⇒ 抛异常 + 磁盘零改动
# =============================================================================
def test_swap_provider_rejects_broken_prefix_and_keeps_disk_intact(tmp_path: Path) -> None:
    cur = _make_provider(tmp_path / "cur", _CAL_OLD)
    new = _make_provider(tmp_path / "new", _CAL_BROKEN + ["2026-11-16"])
    backup = tmp_path / "backup"
    before = _snapshot_of(tmp_path)

    with pytest.raises(RuntimeError) as excinfo:
        SD.swap_provider(cur, new, backup_dir=backup, dry_run=False)
    assert "拒绝换库" in str(excinfo.value)

    assert _snapshot_of(tmp_path) == before, "换库被拒后磁盘必须原封不动"
    assert not backup.exists()


# =============================================================================
# ④ swap_provider 成功 ⇒ 新库生效、旧库备份存在
# =============================================================================
def test_swap_provider_success_moves_new_and_keeps_backup(tmp_path: Path) -> None:
    cur = _make_provider(tmp_path / "cur", _CAL_OLD)
    new = _make_provider(tmp_path / "new", _CAL_NEW)
    backup = tmp_path / "backup"

    out = SD.swap_provider(cur, new, backup_dir=backup, dry_run=False)
    assert out["swapped"] is True
    assert out["dry_run"] is False
    assert out["backup"] is not None

    # 新库已就位
    assert (cur / "calendars" / "day.txt").exists()
    assert SD._read_calendar(cur) == _CAL_NEW
    # 新库目录已被搬空（不再是 provider）
    assert not (new / "calendars").exists()
    # 旧库在备份里
    backup_path = Path(out["backup"])
    assert backup_path.exists()
    assert SD._read_calendar(backup_path) == _CAL_OLD


# =============================================================================
# ⑤ dry_run ⇒ 磁盘零改动
# =============================================================================
def test_swap_provider_dry_run_changes_nothing(tmp_path: Path) -> None:
    cur = _make_provider(tmp_path / "cur", _CAL_OLD)
    new = _make_provider(tmp_path / "new", _CAL_NEW)
    backup = tmp_path / "backup"
    before = _snapshot_of(tmp_path)

    out = SD.swap_provider(cur, new, backup_dir=backup, dry_run=True)
    assert out["swapped"] is False
    assert out["dry_run"] is True
    assert out["backup"] is None
    assert _snapshot_of(tmp_path) == before, "dry-run 必须磁盘零改动"
    assert not backup.exists()


# =============================================================================
# ⑥ extract_snapshot —— 缺子目录 ⇒ 抛异常
# =============================================================================
def test_extract_snapshot_missing_subdir_raises(tmp_path: Path) -> None:
    broken = tmp_path / "broken"
    (broken / "calendars").mkdir(parents=True)
    (broken / "calendars" / "day.txt").write_text("2026-11-02\n", encoding="utf-8")
    tar_path = _make_tarball(broken, tmp_path / "broken.tar.gz", top="qlib_bin")

    with pytest.raises(RuntimeError) as excinfo:
        SD.extract_snapshot(tar_path, tmp_path / "work")
    assert "provider 根目录" in str(excinfo.value)
    assert not (tmp_path / "work" / "broken").exists()


def test_extract_snapshot_returns_provider_root(tmp_path: Path) -> None:
    provider = _make_provider(tmp_path / "src", _CAL_NEW)
    tar_path = _make_tarball(provider, tmp_path / "qlib_bin.tar.gz")
    root = SD.extract_snapshot(tar_path, tmp_path / "work")
    assert (root / "calendars" / "day.txt").exists()
    assert (root / "features" / "sh600000" / "close.day.bin").exists()
    assert (root / "instruments" / "all.txt").exists()
    assert SD._read_calendar(root) == _CAL_NEW


# =============================================================================
# ⑦ fetch_snapshot —— 必须复用 manu/tools 的下载器（不许另写一套）
# =============================================================================
class _FakeSource:
    def __init__(self, **kwargs) -> None:
        self.__dict__.update(kwargs)
        self.history: list[str] = []


class _FakeDownloader:
    SOURCES = {
        "chenditc": {
            "repo": "chenditc/investment_data",
            "tag": "latest",
            "asset": "qlib_bin.tar.gz",
            "note": "fake",
        }
    }
    Source = _FakeSource

    def __init__(self, ok: bool = True) -> None:
        self.ok = ok
        self.calls: list[tuple] = []

    def fetch(self, src, min_speed, speed_time, max_rounds, verify_only):  # noqa: ANN001
        self.calls.append((src, min_speed, speed_time, max_rounds, verify_only))
        dest = Path(src.dest_dir) / src.asset
        dest.parent.mkdir(parents=True, exist_ok=True)
        dest.write_bytes(b"hello")
        return self.ok

    def md5_of(self, path):  # noqa: ANN001
        return "0cc175b9c0f1b6a831c399e269772661"


def test_fetch_snapshot_reuses_downloader(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    fake = _FakeDownloader(ok=True)
    monkeypatch.setattr(SD, "_load_downloader", lambda: fake)
    out = SD.fetch_snapshot(tmp_path / "dest")
    assert out["ok"] is True
    assert out["bytes"] == 5
    assert out["md5"] == "0cc175b9c0f1b6a831c399e269772661"
    assert Path(out["path"]).name == "qlib_bin.tar.gz"
    # 确实调的是那份下载器的 fetch（参数口径对齐 CLI 默认值）
    assert len(fake.calls) == 1
    src = fake.calls[0][0]
    assert src.repo == "chenditc/investment_data"
    assert src.asset == "qlib_bin.tar.gz"


def test_fetch_snapshot_failure_raises(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    monkeypatch.setattr(SD, "_load_downloader", lambda: _FakeDownloader(ok=False))
    with pytest.raises(RuntimeError) as excinfo:
        SD.fetch_snapshot(tmp_path / "dest")
    assert "失败" in str(excinfo.value)


def test_fetch_snapshot_unknown_source_raises(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    monkeypatch.setattr(SD, "_load_downloader", lambda: _FakeDownloader(ok=True))
    with pytest.raises(KeyError):
        SD.fetch_snapshot(tmp_path / "dest", source="not_exists")


# =============================================================================
# ⑧ verify_provider —— qlib 不可用 ⇒ 抛异常（不许静默跳过）
# =============================================================================
def test_verify_provider_raises_when_qlib_unavailable(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    provider = _make_provider(tmp_path / "cn_data", _CAL_OLD)
    monkeypatch.setitem(sys.modules, "qlib", None)  # import qlib ⇒ ImportError
    with pytest.raises(RuntimeError) as excinfo:
        SD.verify_provider(provider, "2026-11-13")
    assert "qlib 不可用" in str(excinfo.value)


# =============================================================================
# ⑨ swap_provider 回滚保险
# =============================================================================
def test_swap_provider_rolls_back_on_failure(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """第二步（新库 → 现役）失败 ⇒ 旧库必须自动回滚到原位。"""
    cur = _make_provider(tmp_path / "cur", _CAL_OLD)
    new = _make_provider(tmp_path / "new", _CAL_NEW)
    backup = tmp_path / "backup"

    real_move = SD._move_path
    calls = {"n": 0}

    def flaky_move(src: Path, dst: Path) -> None:
        calls["n"] += 1
        if calls["n"] == 2:
            raise OSError("模拟磁盘故障")
        real_move(src, dst)

    monkeypatch.setattr(SD, "_move_path", flaky_move)
    with pytest.raises(RuntimeError) as excinfo:
        SD.swap_provider(cur, new, backup_dir=backup, dry_run=False)
    assert "换库失败" in str(excinfo.value)
    assert "旧库已回滚到原位" in str(excinfo.value)
    assert SD._read_calendar(cur) == _CAL_OLD


def test_swap_provider_cleans_partial_target_on_rollback(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """跨盘 move 半失败 ⇒ 活库位置留下残缺目录 ⇒ 回滚必须先清干净再搬回旧库。"""
    cur = _make_provider(tmp_path / "cur", _CAL_OLD)
    new = _make_provider(tmp_path / "new", _CAL_NEW)
    backup = tmp_path / "backup"

    real_move = SD._move_path
    calls = {"n": 0}

    def half_broken_move(src: Path, dst: Path) -> None:
        calls["n"] += 1
        if calls["n"] == 2:
            # 模拟「拷了一半」的残缺目标
            dst.mkdir(parents=True, exist_ok=True)
            (dst / "calendars").mkdir(exist_ok=True)
            (dst / "calendars" / "day.txt").write_text("2026-11-09\n", encoding="utf-8")
            raise OSError("模拟跨盘 move 中断")
        real_move(src, dst)

    monkeypatch.setattr(SD, "_move_path", half_broken_move)
    with pytest.raises(RuntimeError) as excinfo:
        SD.swap_provider(cur, new, backup_dir=backup, dry_run=False)
    assert "旧库已回滚到原位" in str(excinfo.value)
    assert SD._read_calendar(cur) == _CAL_OLD
    assert (cur / "calendars" / "day.txt").read_text().strip() != "2026-11-09"


# =============================================================================
# ⑩ run_stage1 —— 离线全链路
# =============================================================================
def _fake_verify(provider_dir: Path, probe_date: str, **kwargs) -> dict:  # noqa: ANN003
    """替代真 qlib 的读回校验（host 上没装 qlib；真校验在服务器上跑）。"""
    days = SD._read_calendar(provider_dir)
    assert probe_date in days, f"probe_date={probe_date} 不在 {provider_dir} 的日历里"
    return {
        "ok": True,
        "checked": [{"instrument": "sh600000", "date": probe_date}],
        "notes": "fake",
    }


def test_run_stage1_default_is_dry_run() -> None:
    """铁律一：默认 dry-run ⇒ 真换库必须显式 dry_run=False。"""
    import inspect

    sig = inspect.signature(SD.run_stage1)
    assert sig.parameters["dry_run"].default is True
    assert sig.parameters["do_fetch"].default is True
    assert sig.parameters["do_swap"].default is True
    # swap_provider 同理
    swap_sig = inspect.signature(SD.swap_provider)
    assert swap_sig.parameters["dry_run"].default is True


def test_run_stage1_dry_run_offline(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    provider = _make_provider(tmp_path / "cn_data", _CAL_OLD)
    src = _make_provider(tmp_path / "src", _CAL_NEW)
    dest = tmp_path / "dest"
    _make_tarball(src, dest / "qlib_bin.tar.gz")
    monkeypatch.setattr(SD, "verify_provider", _fake_verify)

    before = _snapshot_of(provider)
    out = SD.run_stage1(
        asof="2026-11-19",
        provider_dir=provider,
        work_root=tmp_path / "work",
        backup_dir=tmp_path / "backup",
        dest_dir=dest,
        do_fetch=False,
        do_swap=True,
        dry_run=True,
    )
    assert out["downloaded"] is False
    assert out["swapped"] is False
    assert out["verified"] is True
    assert out["dry_run"] is True
    assert out["calendar_max_before"] == "2026-11-13"
    assert out["calendar_max_after"] == "2026-11-19"
    assert out["new_days"] == ["2026-11-16", "2026-11-17", "2026-11-18", "2026-11-19"]
    assert _snapshot_of(provider) == before, "dry-run 必须**活库**零改动"
    assert SD._read_calendar(provider) == _CAL_OLD, "dry-run 后活库仍是旧日历"
    assert not (tmp_path / "backup").exists(), "dry-run 不许产生备份"



def test_run_stage1_swap_offline(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    provider = _make_provider(tmp_path / "cn_data", _CAL_OLD)
    src = _make_provider(tmp_path / "src", _CAL_NEW)
    dest = tmp_path / "dest"
    _make_tarball(src, dest / "qlib_bin.tar.gz")
    monkeypatch.setattr(SD, "verify_provider", _fake_verify)

    out = SD.run_stage1(
        asof="2026-11-19",
        provider_dir=provider,
        work_root=tmp_path / "work",
        backup_dir=tmp_path / "backup",
        dest_dir=dest,
        do_fetch=False,
        do_swap=True,
        dry_run=False,
    )
    assert out["swapped"] is True
    assert out["verified"] is True
    assert SD._read_calendar(provider) == _CAL_NEW
    assert list((tmp_path / "backup").glob("cn_data_*")), "旧库必须留备份"


def test_run_stage1_broken_prefix_raises(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    provider = _make_provider(tmp_path / "cn_data", _CAL_OLD)
    src = _make_provider(tmp_path / "src", _CAL_BROKEN + ["2026-11-16"])
    dest = tmp_path / "dest"
    _make_tarball(src, dest / "qlib_bin.tar.gz")
    monkeypatch.setattr(SD, "verify_provider", _fake_verify)

    before = _snapshot_of(provider)
    with pytest.raises(RuntimeError) as excinfo:
        SD.run_stage1(
            asof="2026-11-16",
            provider_dir=provider,
            work_root=tmp_path / "work",
            backup_dir=tmp_path / "backup",
            dest_dir=dest,
            do_fetch=False,
            do_swap=True,
            dry_run=False,
        )
    assert "拒绝换库" in str(excinfo.value)
    assert _snapshot_of(provider) == before, "拒绝换库后活库必须原封不动"
    assert SD._read_calendar(provider) == _CAL_OLD
    assert not (tmp_path / "backup").exists()


def test_run_stage1_asof_not_covered_is_disclosed(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """asof 超出快照末日 ⇒ 披露「数据滞后」，但校验仍按末日跑（不许静默）。"""
    provider = _make_provider(tmp_path / "cn_data", _CAL_OLD)
    src = _make_provider(tmp_path / "src", _CAL_NEW)
    dest = tmp_path / "dest"
    _make_tarball(src, dest / "qlib_bin.tar.gz")
    monkeypatch.setattr(SD, "verify_provider", _fake_verify)

    out = SD.run_stage1(
        asof="2026-12-01",
        provider_dir=provider,
        work_root=tmp_path / "work",
        backup_dir=tmp_path / "backup",
        dest_dir=dest,
        do_fetch=False,
        do_swap=True,
        dry_run=True,
    )
    joined = " ".join(out["notes"])
    assert "2026-12-01" in joined and "数据滞后" in joined


# =============================================================================
# ⑪ _load_downloader —— 导入期不许炸
# =============================================================================
def test_load_downloader_imports_cleanly() -> None:
    """回归（2026-11-05 服务器事故）：`@dataclass` + `from __future__ import annotations`
    的模块用 `importlib` 加载时，**必须先登记进 `sys.modules`**，
    否则 `dataclasses` 反查 `sys.modules[cls.__module__].__dict__` 拿到 `None`
    ⇒ `AttributeError: 'NoneType' object has no attribute '__dict__'`。
    """
    dl = SD._load_downloader()
    assert "chenditc" in dl.SOURCES
    assert dl.Source.__module__ == "manu_fetch_qlib_dataset"
    assert sys.modules["manu_fetch_qlib_dataset"] is dl


# =============================================================================
# ⑫ 失败必须 fail-closed，且异常消息带 traceback（不许再吞栈）
# =============================================================================
def test_run_stage1_failure_carries_traceback(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """下载器加载失败 ⇒ 抛异常（类型不变）+ 消息里带完整 traceback + 原始报错。"""

    def _boom_loader():
        raise RuntimeError("boom: 模拟下载器加载失败")

    provider = _make_provider(tmp_path / "cn_data", _CAL_OLD)
    monkeypatch.setattr(SD, "_load_downloader", _boom_loader)

    with pytest.raises(RuntimeError) as excinfo:
        SD.run_stage1(
            asof="2026-11-13",
            provider_dir=provider,
            work_root=tmp_path / "work",
            backup_dir=tmp_path / "backup",
            dest_dir=tmp_path / "dest",
            do_fetch=True,
            do_swap=True,
            dry_run=True,
        )
    message = str(excinfo.value)
    assert "boom: 模拟下载器加载失败" in message, "原始报错必须留在消息里"
    assert "Traceback (most recent call last)" in message, "消息里必须带 traceback"
    assert "_boom_loader" in message, "traceback 必须指到真正的出错位置"
