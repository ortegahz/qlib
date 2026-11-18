"""阶段 1「校验对象 + 披露口径」的 pytest（**完全离线**：自带夹具，不依赖服务器产物）。

【背景：2026-11-11 全量跑批的假警报】
上游快照日历没推进（末日 2026-11-09）⇒ 阶段 1 跳过换库；
但**自建增量**已把派生库 `cn_data_live` 派生到 2026-11-11 ⇒ S 层真的打到了信号日。
老口径 `effective = provider_path if swapped else new_dir` 校验的是**上游新快照**
（末日 2026-11-09）⇒ 报「数据滞后 / 阶段 1 未换库」**假警报**
（还会误触发 `--full` 的「有环节没跑成 ⇒ 退出码 1」fail-closed）。

【四个场景】
* **A 假警报**：换库 + 增量成功（派生库末日 == asof）⇒ 不许出现「滞后 / 未换库 / 拿不到」；
* **B 真降级**：被校验对象末日 < asof ⇒ **必须**仍有滞后告警（不许把真降级也消掉）；
* **C 没换库 + 增量成功**：校验对象是 `live_path`（**不是** `new_dir`）；
* **D 没换库 + 增量失败**：校验对象回退到 `new_dir`。

【夹具为什么能造假】
qlib provider 结构极简：`calendars/day.txt` + `instruments/all.txt` +
`features/<code>/<field>.day.bin` ⇒ 用 `tarfile` 现场造最小 tarball 即可；
自建增量那一层用**假 `stage1_rolling_increment` 模块**（塞进 `sys.modules`）
⇒ 不联网、不碰真库、不写 `/mnt/data`。
"""
from __future__ import annotations

import shutil
import sys
import tarfile
import types
from pathlib import Path

import numpy as np
import pytest

_DAILY_DIR = Path(__file__).resolve().parents[1]
if str(_DAILY_DIR) not in sys.path:
    sys.path.insert(0, str(_DAILY_DIR))

import stage1_dataset as SD  # noqa: E402

#: 信号日
_ASOF = "2026-11-11"
#: 上游快照日历（末日 2026-11-09 ⇒ **没推进到信号日**）
_CAL_UPSTREAM = ["2026-11-04", "2026-11-05", "2026-11-06", "2026-11-09"]
#: 现役基座日历（末日 2026-11-06 ⇒ 换库后推进到 2026-11-09）
_CAL_BASE = ["2026-11-04", "2026-11-05", "2026-11-06"]
#: 自建增量补齐的交易日（补完 ⇒ 派生库末日 == 信号日）
_DAYS_ADD = ["2026-11-10", "2026-11-11"]

_FIELDS = ("close", "volume", "factor")
_INSTRUMENTS = ("sh600000", "sz000001")


# =============================================================================
# 夹具（自带，不依赖服务器产物）
# =============================================================================
def _make_provider(root: Path, days: list[str]) -> Path:
    """造一个最小可用的 qlib provider 目录。"""
    (root / "calendars").mkdir(parents=True, exist_ok=True)
    (root / "calendars" / "day.txt").write_text("\n".join(days) + "\n", encoding="utf-8")

    (root / "instruments").mkdir(parents=True, exist_ok=True)
    (root / "instruments" / "all.txt").write_text(
        "\n".join(f"{inst}\t{days[0]}\t{days[-1]}" for inst in _INSTRUMENTS) + "\n",
        encoding="utf-8",
    )

    for idx, inst in enumerate(_INSTRUMENTS):
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


def _fake_sri_module(*, days_to_add: list[str], fail_at: str | None = None) -> types.ModuleType:
    """假 `stage1_rolling_increment`（**不联网、不写真库**）。

    * `missing_trade_days` ⇒ 直接返回 `days_to_add`；
    * `fetch_missing_bars` ⇒ 返回假 frame（`fail_at="fetch"` 时**故意炸** ⇒ 模拟兜底失败）；
    * `build_live_dataset` ⇒ 把基座复制成派生目录 + 追加交易日（**真落盘**，
      让派生库有**自己的日历** ⇒ 「覆盖判定用同一个目录的日历」可被真实验证）。
    """
    mod = types.ModuleType("stage1_rolling_increment")

    def missing_trade_days(base_max: str, target: str, calendar_csv=None):  # noqa: ANN001
        return list(days_to_add)

    def fetch_missing_bars(days, base_dir=None, out_dir=None, ak_python=None):  # noqa: ANN001
        if fail_at == "fetch":
            raise RuntimeError("假故障：抓 bar 失败（模拟兜底失败）")
        frames = {
            day: {
                "code": list(_INSTRUMENTS),
                "close": [10.0, 20.0],
                "volume": [1.0, 1.0],
            }
            for day in days
        }
        stats = {day: {"rows": len(_INSTRUMENTS), "source": "fake"} for day in days}
        return {"frames": frames, "stats": stats}

    def build_live_dataset(  # noqa: ANN001
        base_dir,
        live_dir,
        days,
        bars_by_day,
        *,
        dry_run: bool = True,
        ledger_path=None,
        source: str = "fake",
        tolerance: float = 0.0,
    ):
        base = Path(base_dir)
        live = Path(live_dir)
        base_days = SD._read_calendar(base)
        new_days = sorted(set(base_days) | set(days))
        if not dry_run:
            if live.exists():
                shutil.rmtree(live)
            shutil.copytree(base, live)
            (live / "calendars" / "day.txt").write_text(
                "\n".join(new_days) + "\n", encoding="utf-8"
            )
        return {
            "generated_at": "1970-01-01T00:00:00+0000",
            "base_dir": str(base),
            "live_dir": str(live),
            "base_calendar_max": base_days[-1],
            "live_calendar_max": new_days[-1],
            "days_added": [d for d in days if d not in set(base_days)],
            "source": source,
            "tolerance": tolerance,
            "dry_run": bool(dry_run),
            "per_day": [],
            "notes": [f"假派生：{d} 写入 {len(_INSTRUMENTS)} 行" for d in days],
        }

    mod.missing_trade_days = missing_trade_days
    mod.fetch_missing_bars = fetch_missing_bars
    mod.build_live_dataset = build_live_dataset
    return mod


def _install_fake_sri(monkeypatch: pytest.MonkeyPatch, mod: types.ModuleType) -> None:
    """把假模块塞进 `sys.modules`（`try_increment` 内部是**惰性 import** ⇒ 拦得住）。"""
    monkeypatch.setitem(sys.modules, "stage1_rolling_increment", mod)


def _fake_verify_factory(recorded: list[dict]):
    """替代真 qlib 的读回校验（host 上没装 qlib）⇒ **顺手记录被校验的目录**。"""

    def _fake_verify(provider_dir, probe_date, **kwargs):  # noqa: ANN001
        recorded.append({"dir": str(provider_dir), "probe": probe_date})
        days = SD._read_calendar(provider_dir)
        assert probe_date in days, f"probe_date={probe_date} 不在 {provider_dir} 的日历里"
        return {
            "ok": True,
            "checked": [{"instrument": "sh600000", "date": probe_date}],
            "notes": "fake",
        }

    return _fake_verify


def _run(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    *,
    base_days: list[str],
    snapshot_days: list[str],
    days_to_add: list[str],
    fail_at: str | None = None,
    dry_run: bool = False,
) -> dict:
    """跑一遍 `run_stage1`（离线）⇒ 返回 (结果, 被校验目录记录)。"""
    provider = _make_provider(tmp_path / "cn_data", base_days)
    src = _make_provider(tmp_path / "src", snapshot_days)
    dest = tmp_path / "dest"
    _make_tarball(src, dest / "qlib_bin.tar.gz")

    recorded: list[dict] = []
    monkeypatch.setattr(SD, "verify_provider", _fake_verify_factory(recorded))
    _install_fake_sri(
        monkeypatch, _fake_sri_module(days_to_add=days_to_add, fail_at=fail_at)
    )

    out = SD.run_stage1(
        asof=_ASOF,
        provider_dir=provider,
        work_root=tmp_path / "work",
        backup_dir=tmp_path / "backup",
        dest_dir=dest,
        do_fetch=False,
        do_swap=True,
        dry_run=dry_run,
        do_increment=True,
        increment_dry_run=False,
        live_dir=tmp_path / "cn_data_live",
    )
    out["_recorded"] = recorded
    out["_live_dir"] = str(tmp_path / "cn_data_live")
    out["_work_root"] = str(tmp_path / "work")
    return out


# =============================================================================
# 场景 A（假警报）：换库 + 增量成功 + 派生库末日 == asof ⇒ 不许有「滞后 / 未换库」
# =============================================================================
def test_scenario_a_false_alarm_is_gone(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """换库（上游推进到 11-09）+ 自建增量（派生到 11-11）⇒ **不许**报滞后/未换库。"""
    out = _run(
        monkeypatch,
        tmp_path,
        base_days=_CAL_BASE,
        snapshot_days=_CAL_UPSTREAM,
        days_to_add=_DAYS_ADD,
    )
    assert out["swapped"] is True
    assert out["swap_skipped"] is False
    # 🔴 增量必须真的回传「派生库覆盖 asof」这个事实
    assert out["increment"]["derived_ok"] is True
    assert out["increment"]["derived_covers_asof"] is True
    assert out["increment"]["derived_calendar_max"] == _ASOF
    # 🔴 校验对象 = 下游 S 层真正会用的那一份（派生库）
    assert out["validated_dir"] == out["_live_dir"]
    assert out["validated_source"] == "自建增量派生库"
    assert out["covers_asof"] is True
    assert out["validated_calendar_max"] == _ASOF
    # 🔴 读回校验（verify_provider）拿到的也必须是派生库
    assert out["_recorded"] and out["_recorded"][0]["dir"] == out["_live_dir"]
    # 🔴 假警报字样一个都不许出现
    joined = " ".join(out["notes"])
    for bad in ("滞后", "未换库", "拿不到"):
        assert bad not in joined, f"假警报字样 {bad!r} 不该出现：\n{joined}"
    # 🔴 口径要能一眼看出校验的是哪一份（路径写进 info 行）
    assert out["_live_dir"] in joined
    assert "校验对象" in joined


# =============================================================================
# 场景 B（真降级）：被校验对象末日 < asof ⇒ 滞后告警必须还在
# =============================================================================
def test_scenario_b_true_degradation_is_kept(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """没换库 + 增量**失败** ⇒ 校验对象末日 11-09 < asof ⇒ **必须**披露滞后。"""
    out = _run(
        monkeypatch,
        tmp_path,
        base_days=_CAL_UPSTREAM,
        snapshot_days=_CAL_UPSTREAM,
        days_to_add=_DAYS_ADD,
        fail_at="fetch",
    )
    assert out["swap_skipped"] is True
    assert out["increment"]["derived_ok"] is False
    assert out["increment"]["derived_covers_asof"] is False
    assert out["covers_asof"] is False
    joined = " ".join(out["notes"])
    assert "数据滞后" in joined, f"真降级必须披露滞后：\n{joined}"
    assert "未换库" in joined, f"真降级必须保留「未换库」披露：\n{joined}"
    assert "2026-11-09" in joined, "必须说清楚只能打到哪一天"


def test_scenario_b_derived_not_covering_asof_keeps_alarm(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """没换库 + 增量**成功但没覆盖 asof**（派生库末日 11-10）⇒ 滞后告警必须还在。"""
    out = _run(
        monkeypatch,
        tmp_path,
        base_days=_CAL_UPSTREAM,
        snapshot_days=_CAL_UPSTREAM,
        days_to_add=["2026-11-10"],
    )
    assert out["increment"]["derived_ok"] is True
    assert out["increment"]["derived_covers_asof"] is False
    assert out["validated_dir"] == out["_live_dir"]
    assert out["covers_asof"] is False
    joined = " ".join(out["notes"])
    assert "数据滞后" in joined, f"派生库没覆盖 asof ⇒ 必须披露滞后：\n{joined}"
    assert "未换库" in joined, "派生库没覆盖 asof ⇒ 必须保留「未换库」披露"
    assert "拿不到" in joined, "真降级必须保留「拿不到」的披露口径"


# =============================================================================
# 场景 C（没换库 + 增量成功）：校验对象是 live_path（**不是** new_dir）
# =============================================================================
def test_scenario_c_validates_live_dir_not_new_snapshot(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """上游没推进（跳过换库）+ 自建增量派生到 asof ⇒ 校验**派生库**，不是上游新快照。"""
    out = _run(
        monkeypatch,
        tmp_path,
        base_days=_CAL_UPSTREAM,
        snapshot_days=_CAL_UPSTREAM,
        days_to_add=_DAYS_ADD,
    )
    assert out["swapped"] is False
    assert out["swap_skipped"] is True
    assert out["increment"]["derived_covers_asof"] is True
    # 🔴 校验对象路径字段
    assert out["validated_dir"] == out["_live_dir"]
    assert out["validated_source"] == "自建增量派生库"
    # 🔴 不是上游新快照（解压暂存目录）
    assert not Path(out["validated_dir"]).is_relative_to(Path(out["_work_root"]))
    assert out["covers_asof"] is True
    # 🔴 假警报字样不许出现
    joined = " ".join(out["notes"])
    for bad in ("滞后", "未换库", "拿不到"):
        assert bad not in joined, f"假警报字样 {bad!r} 不该出现：\n{joined}"
    # 🔴 「上游没推进」的披露必须**改写**成「仍可打到信号日」
    assert "上游快照日历未推进" in joined
    assert "仍可打到信号日" in joined
    assert out["_live_dir"] in joined


# =============================================================================
# 场景 D（没换库 + 增量失败）：校验对象回退到 new_dir
# =============================================================================
def test_scenario_d_falls_back_to_new_snapshot(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """没换库 + 增量失败 ⇒ 校验对象回退到**上游新快照**（新解压的那一份）。"""
    out = _run(
        monkeypatch,
        tmp_path,
        base_days=_CAL_UPSTREAM,
        snapshot_days=_CAL_UPSTREAM,
        days_to_add=_DAYS_ADD,
        fail_at="fetch",
    )
    assert out["swap_skipped"] is True
    assert out["increment"]["derived_ok"] is False
    assert out["validated_dir"] != out["_live_dir"]
    assert out["validated_source"] == "上游新快照（本轮没有换库）"
    assert Path(out["validated_dir"]).is_relative_to(Path(out["_work_root"]))
    assert SD._read_calendar(out["validated_dir"])[-1] == "2026-11-09"
    assert out["covers_asof"] is False


# =============================================================================
# try_increment 的派生事实回传（单元级）
# =============================================================================
def test_try_increment_reports_derived_coverage(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """`try_increment` 必须回传「派生库是否覆盖 asof」⇒ 调用方才能选对校验对象。"""
    base = _make_provider(tmp_path / "cn_data", _CAL_UPSTREAM)
    live = tmp_path / "cn_data_live"
    _install_fake_sri(monkeypatch, _fake_sri_module(days_to_add=_DAYS_ADD))

    out = SD.try_increment(
        asof=_ASOF,
        base_dir=base,
        live_dir=live,
        work_root=tmp_path / "work",
        dry_run=False,
    )
    assert out["ok"] is True
    assert out["derived_ok"] is True
    assert out["derived_covers_asof"] is True
    assert out["derived_calendar_max"] == _ASOF
    # 🔴 派生库自己的日历就是事实源
    assert SD._read_calendar(live)[-1] == _ASOF


def test_try_increment_reports_not_covering(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """派生库没覆盖 asof ⇒ 必须**如实**回传 False（不许乐观假设）。"""
    base = _make_provider(tmp_path / "cn_data", _CAL_UPSTREAM)
    live = tmp_path / "cn_data_live"
    _install_fake_sri(monkeypatch, _fake_sri_module(days_to_add=["2026-11-10"]))

    out = SD.try_increment(
        asof=_ASOF,
        base_dir=base,
        live_dir=live,
        work_root=tmp_path / "work",
        dry_run=False,
    )
    assert out["ok"] is True
    assert out["derived_covers_asof"] is False
    assert out["derived_calendar_max"] == "2026-11-10"
