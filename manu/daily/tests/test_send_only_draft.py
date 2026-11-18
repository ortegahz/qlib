"""🔴 2026-11-09 新增：`--send-only` 的草稿定位（快照化改造带进来的回归）。

事故：`--send-only` 读 ``<run_root>/<YYYYMMDD>/email_draft_<YYYYMMDD>.json``，
而草稿现在写在**快照目录** ``<SNAPSHOT_ROOT>/<run_id>/`` ⇒ 两个路径永不相交
⇒ 空等 ``--wait-seconds``（默认 1800s）后**退出码 0 却没发信**（静默假成功）。

另钉一条：`--send-only` 与 `--full` 互斥（`--send-only` 整条流水线不跑
⇒ `--full` 被静默忽略）。
"""

from __future__ import annotations

import json
from pathlib import Path

import main as daily_main


def _write_draft(directory: Path, day: str = "20261105") -> Path:
    directory.mkdir(parents=True, exist_ok=True)
    path = directory / f"email_draft_{day}.json"
    path.write_text(
        json.dumps(
            {
                "asof": "2026-11-05",
                "kind": "daily",
                "subject": "S",
                "html_body": "<p>H</p>",
                "text_body": "H",
                "to": ["a@example.com"],
            },
            ensure_ascii=False,
        ),
        encoding="utf-8",
    )
    return path


def test_draft_found_in_snapshot_root(tmp_path: Path) -> None:
    """① 草稿在快照根 ⇒ 必须找到快照里那一份（哪怕旧目录里没有）。"""
    snap_root = tmp_path / "snapshots"
    run_root = tmp_path / "run_root"
    expected = _write_draft(snap_root / "20261105_120000")
    got = daily_main._resolve_draft("2026-11-05", snap_root, run_root)
    assert got == expected, f"必须命中快照里的草稿，实得 {got}"


def test_latest_snapshot_wins(tmp_path: Path) -> None:
    """② 多份快照 ⇒ 取**最新**那份（目录名 == 时间戳）。"""
    snap_root = tmp_path / "snapshots"
    _write_draft(snap_root / "20261105_120000")
    newest = _write_draft(snap_root / "20261105_180000")
    got = daily_main._resolve_draft("2026-11-05", snap_root, tmp_path / "run_root")
    assert got == newest, f"必须取最新快照的草稿，实得 {got}"


def test_legacy_dir_fallback(tmp_path: Path) -> None:
    """③ 旧按日目录里的草稿仍要能兜底找到（改造前的老产物）。"""
    snap_root = tmp_path / "snapshots"
    snap_root.mkdir(parents=True, exist_ok=True)
    legacy = _write_draft(tmp_path / "run_root" / "20261105")
    got = daily_main._resolve_draft("2026-11-05", snap_root, tmp_path / "run_root")
    assert got == legacy, f"必须回落��旧目录的草稿，实得 {got}"


def test_no_draft_returns_candidate_without_raising(tmp_path: Path) -> None:
    """④ 都没有 ⇒ 返回候选路径（供等待循环轮询），**不许抛异常**。"""
    snap_root = tmp_path / "snapshots"
    snap_root.mkdir(parents=True, exist_ok=True)
    got = daily_main._resolve_draft("2026-11-05", snap_root, tmp_path / "run_root")
    assert got is not None and got.name == "email_draft_20261105.json"


def test_send_only_with_full_is_rejected(capsys) -> None:
    """⑤ `--send-only` + `--full` ⇒ 退出码 2（不许静默忽略 `--full`）。"""
    rc = daily_main.main(["--asof", "2026-11-05", "--send-only", "--full"])
    assert rc == 2, f"必须退出码 2，实得 {rc}"
    err = capsys.readouterr().err
    assert "--send-only" in err and "--full" in err, f"必须给出迁移提示，实得 {err!r}"
