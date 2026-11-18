"""🔴 2026-11-09 新增：失败的运行**必须**留下带 manifest 的快照（P1-7）。

旧口径：`snap.finalize()` 只在成功路径调用 ⇒ `StageError` 路径留下**孤儿目录**
（半程产物 + 没有 `snapshot_manifest.json`）⇒ 无权威索引、无失败阶段记录。
"""

from __future__ import annotations

import json
import logging
from pathlib import Path

import main as daily_main
import snapshot as snap_mod


def test_failed_run_finalizes_snapshot_with_manifest(tmp_path: Path) -> None:
    snap = snap_mod.create_snapshot(tmp_path, asof="2026-11-05")
    snap.set_stage("calendar_gate", "ok")
    state = {"snapshot": snap, "run_dir": snap.path}
    log = logging.getLogger("t")

    daily_main._finalize_failed_snapshot(state, "fetch_data", RuntimeError("boom"), log)

    manifest = snap.path / "snapshot_manifest.json"
    assert manifest.is_file(), "失败快照必须留下 snapshot_manifest.json"
    payload = json.loads(manifest.read_text(encoding="utf-8"))
    stages = {e["name"]: e for e in payload["stages"]}
    assert stages["fetch_data"]["status"] == "failed", stages.get("fetch_data")
    assert stages["pipeline"]["status"] == "failed", stages.get("pipeline")
    assert "boom" in str(stages["fetch_data"].get("error"))


def test_finalize_without_snapshot_is_noop(tmp_path: Path) -> None:
    """没有快照对象时**不许**抛异常（早退路径：日历门就拒了）。"""
    log = logging.getLogger("t")
    daily_main._finalize_failed_snapshot({}, "calendar_gate", RuntimeError("x"), log)
    daily_main._finalize_failed_snapshot(None, "calendar_gate", RuntimeError("x"), log)


def test_main_failure_path_actually_finalizes(monkeypatch, tmp_path: Path) -> None:
    """🔴 接线：``main()`` 的 StageError 分支**必须**调封存（不只是 helper 本身对）。"""
    import daily_config as DC

    calls: list[str] = []

    def _boom(*a, **k):
        raise daily_main.StageError("fetch_data", RuntimeError("boom"))

    monkeypatch.setattr(daily_main, "_run_stages", _boom)
    monkeypatch.setattr(daily_main, "_finalize_failed_snapshot",
                        lambda state, stage, cause, log: calls.append(str(stage)))
    monkeypatch.setattr(DC, "LOCK_FILE", tmp_path / "lock")
    monkeypatch.setattr(DC, "REPORT_DIR", tmp_path / "report")
    monkeypatch.setattr(DC, "LOG_DIR", tmp_path / "logs")

    rc = daily_main.main(["--asof", "2026-11-05", "--snapshot-root", str(tmp_path / "snaps")])
    assert rc == 1, f"阶段失败 ⇒ 退出码必须是 1，实得 {rc}"
    assert calls == ["fetch_data"], f"失败分支必须封存快照，实得 {calls}"
