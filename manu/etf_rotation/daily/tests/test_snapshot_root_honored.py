"""🔴 2026-11-09 新增：`--snapshot-root` 必须**真的**被尊重（P1-6）。

旧口径：`agents_rebalance.run_pipeline` 里三处写死 ``DC.SNAPSHOT_ROOT``
⇒ CLI 传了 ``--snapshot-root PATH`` 也被忽略 ⇒ 自定义根下
「底座链」断裂（退回 canonical 一次性种子）、``--skip-llm`` 复用不到上一轮决策。
"""

from __future__ import annotations

from pathlib import Path

import agents_rebalance as AR
import snapshot as snap_mod


def test_run_pipeline_uses_snapshot_own_root(tmp_path: Path, monkeypatch) -> None:
    """S 层底座必须从**本轮快照自己的根**找，不是 `DC.SNAPSHOT_ROOT`。"""
    custom_root = tmp_path / "custom_snapshots"
    snap = snap_mod.create_snapshot(custom_root, asof="2026-11-05")

    seen: list[Path] = []

    seen_asof: list[str | None] = []

    def _fake_resolve(root, canonical_csv, *, exclude=None, asof=None):
        seen.append(Path(root))
        seen_asof.append(asof)
        return None, "测试夹具：无底座"

    monkeypatch.setattr(AR.snap_mod, "resolve_scores_base", _fake_resolve)
    monkeypatch.setattr(
        AR, "_resolve_analysis_asof",
        lambda **kw: {
            "notes": [],
            "analysis_asof": kw.get("signal_date"),
            "scores_max_date": None,
            "grid_last": None,
            "dataset_max_date": None,
        },
    )

    AR.run_pipeline(asof="2026-11-05", snapshot=snap, preflight_only=True)

    assert seen, "前置判定也必须解析 S 层底座"
    assert seen[0] == custom_root, f"底座必须从本轮快照的根找，实得 {seen[0]}"
    # 🔴 2026-11-10：底座必须**按 asof 挑**（不是按目录名＝墙钟创建序）
    assert seen_asof and all(v == "2026-11-05" for v in seen_asof), (
        f"底座解析必须带上本轮 asof，实得 {seen_asof}"
    )


def test_base_chain_calls_never_use_default_root() -> None:
    """底座链 / 决策复用这两类调用**不许**再用默认根常量。

    （`create_snapshot(DC.SNAPSHOT_ROOT, ...)` 那处是**缺省兜底**——调用方
    `main.py` 永远传本轮快照，所以保留是对的。）
    """
    src = Path(AR.__file__).read_text(encoding="utf-8")
    body = src.split("def run_pipeline(", 1)[1].split("\ndef ", 1)[0]
    for call in ("resolve_scores_base(", "latest_snapshot("):
        for chunk in body.split(call)[1:]:
            head = chunk.split(")", 1)[0]
            assert "DC.SNAPSHOT_ROOT" not in head, (
                f"{call} 仍在用默认根常量：{head!r}"
            )
