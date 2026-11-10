"""快照底座**按 asof 挑选**的回归测试（2026-11-10 真 bug）。

【为什么需要】
`snapshot.latest_snapshot` 原实现只按**目录名**排序取最新 ⇒ 目录名是**墙钟创建序**
（`RUN_ID_FORMAT = "%Y%m%d_%H%M%S"`），**不含 asof**
⇒ 补跑（asof = 过去的信号日）时可能拿到**别的信号日**的快照当底座：
- 底座 `stock_scores.csv` 可能含 **asof 之后**的行 ⇒ 快照藏未来行 ⇒ A9 门禁误红；
- `--skip-llm` 会复用别个信号日的 LLM 决策件 ⇒ 交付件嵌的是**另一个信号日**的意见。

⇒ 现在 `latest_snapshot(..., asof=...)` **优先**挑 manifest 里 asof 相同的那一份。
"""

from __future__ import annotations

import json
import sys
from pathlib import Path

DAILY_DIR = Path(__file__).resolve().parents[1]
if str(DAILY_DIR) not in sys.path:
    sys.path.insert(0, str(DAILY_DIR))

import snapshot as snap_mod  # noqa: E402


def _make_snapshot(root: Path, run_id: str, asof: str, *, scores: bool = True) -> Path:
    path = root / run_id
    path.mkdir(parents=True, exist_ok=True)
    payload = {"schema_version": 1, "run_id": run_id, "asof": asof}
    (path / snap_mod.MANIFEST_NAME).write_text(
        json.dumps(payload, ensure_ascii=False), encoding="utf-8"
    )
    if scores:
        (path / "stock_scores.csv").write_text(
            "code,date\n510300,2026-11-09\n", encoding="utf-8"
        )
    return path


def test_latest_snapshot_prefers_same_asof_even_if_older(tmp_path: Path) -> None:
    """🔴 核心回归：目录名更**旧**但 asof 相同的那份 ⇒ 必须被选中。"""
    root = tmp_path / "snapshots"
    _make_snapshot(root, "20261110_160000", asof="2026-11-10")  # 墙钟更晚
    want = _make_snapshot(root, "20261109_173133", asof="2026-11-09")

    got = snap_mod.latest_snapshot(root, asof="2026-11-09")

    assert got == want, f"必须挑 asof 相同的那份，实得 {got}"


def test_latest_snapshot_without_asof_keeps_newest_by_name(tmp_path: Path) -> None:
    """不传 asof ⇒ 保持旧口径（按目录名取最新）。"""
    root = tmp_path / "snapshots"
    _make_snapshot(root, "20261109_173133", asof="2026-11-09")
    new = _make_snapshot(root, "20261110_160000", asof="2026-11-10")

    got = snap_mod.latest_snapshot(root)

    assert got == new


def test_latest_snapshot_falls_back_when_no_asof_match(tmp_path: Path) -> None:
    """没有任何 asof 匹配 ⇒ 退回「按目录名取最新」（不炸、不返回 None）。"""
    root = tmp_path / "snapshots"
    _make_snapshot(root, "20261109_173133", asof="2026-11-09")
    _make_snapshot(root, "20261110_160000", asof="2026-11-10")

    got = snap_mod.latest_snapshot(root, asof="2026-11-05")

    assert got is not None and got.name == "20261110_160000"


def test_manifest_asof_tolerates_missing_or_broken(tmp_path: Path) -> None:
    """manifest 缺失 / 坏 JSON ⇒ 返回 None，**不许炸**。"""
    root = tmp_path / "snapshots"
    broken = root / "20261110_160000"
    broken.mkdir(parents=True)
    (broken / snap_mod.MANIFEST_NAME).write_text("{ 不是 json", encoding="utf-8")
    empty = root / "20261110_170000"
    empty.mkdir(parents=True)

    assert snap_mod.manifest_asof(broken) is None
    assert snap_mod.manifest_asof(empty) is None


def test_resolve_scores_base_picks_same_asof_snapshot(tmp_path: Path) -> None:
    """底座解析：必须挑 asof 相同的那份快照的 `stock_scores.csv`。"""
    root = tmp_path / "snapshots"
    _make_snapshot(root, "20261110_160000", asof="2026-11-10")
    want = _make_snapshot(root, "20261109_173133", asof="2026-11-09")

    base, note = snap_mod.resolve_scores_base(
        root, tmp_path / "canonical.csv", asof="2026-11-09"
    )

    assert base == want / "stock_scores.csv"
    assert "2026-11-09" in note


def test_resolve_scores_base_warns_on_mismatched_asof(tmp_path: Path) -> None:
    """没有同 asof 的快照 ⇒ 退回旧口径，但**必须披露**底座 asof 不一致。"""
    root = tmp_path / "snapshots"
    _make_snapshot(root, "20261110_160000", asof="2026-11-10")

    base, note = snap_mod.resolve_scores_base(
        root, tmp_path / "canonical.csv", asof="2026-11-05"
    )

    assert base is not None
    assert "不一致" in note
