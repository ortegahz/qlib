"""🔴 2026-11-09 新增：`probe_lookahead` 的**断言对象**可指向本轮快照（正本 §三第 2 条）。

口径：
  * 给了 ``--snapshot-root`` / ``--snapshot`` ⇒ 断言**快照内那一份**报告；
    找不到真快照 / 快照内没报告 ⇒ 退出码 **2「不适用」**（≠ 通过），
    **不许**悄悄回落成根目录那份还报「通过」；
  * 不给这两个参数 ⇒ 仍断言**根目录**那份（基线一致性护栏，保留不动）。
"""

from __future__ import annotations

import json
import sys
from pathlib import Path

from manu.etf_rotation.probes import probe_lookahead  # type: ignore  # noqa: E402


def _make_snapshot(root: Path, name: str, *, with_report: bool,
                   with_data: bool = True) -> Path:
    snap = root / name
    snap.mkdir(parents=True, exist_ok=True)
    (snap / "snapshot_manifest.json").write_text(
        json.dumps({"schema_version": 1, "run_id": name}, ensure_ascii=False),
        encoding="utf-8",
    )
    if with_report:
        (snap / "etf_analysis_report.json").write_text(
            json.dumps({"meta": {"asof": "2026-11-03"}}, ensure_ascii=False), encoding="utf-8"
        )
        (snap / "etf_analysis_report.md").write_text("# 空报告\n", encoding="utf-8")
    if with_data:
        # 🔴 2026-11-10：快照**自包含** ⇒ 个股分数 / 个股日线也在快照目录里
        #    （`probe_snapshot_contract.CONTRACT_ARTIFACTS` 16 件里就有这两个）
        (snap / "stock_scores.csv").write_text(
            "instrument,date,score\nSH600000,2026-11-03,1.0\n", encoding="utf-8"
        )
        (snap / "stock_prices.csv").write_text(
            "instrument,date,close\nSH600000,2026-11-03,10.0\n", encoding="utf-8"
        )
    return snap


def _run(monkeypatch, argv: list[str]) -> int:
    monkeypatch.setattr(sys, "argv", ["probe_lookahead.py", *argv])
    return probe_lookahead.main()


def test_snapshot_without_report_is_not_applicable(tmp_path, monkeypatch) -> None:
    """快照在、但**没有报告**（dry-run 快照）⇒ rc2「不适用」，不许回落根目录。"""
    root = tmp_path / "snapshots"
    _make_snapshot(root, "20261105_120000", with_report=False)
    rc = _run(monkeypatch, ["--snapshot-root", str(root)])
    assert rc == 2, f"必须是「不适用」(2)，实得 {rc}"


def test_no_snapshot_at_all_is_not_applicable(tmp_path, monkeypatch) -> None:
    """快照根下什么都没有 ⇒ rc2「不适用」。"""
    rc = _run(monkeypatch, ["--snapshot-root", str(tmp_path / "empty")])
    assert rc == 2, f"必须是「不适用」(2)，实得 {rc}"


def test_snapshot_with_report_is_asserted_not_skipped(tmp_path, monkeypatch, capsys) -> None:
    """快照里有报告 ⇒ 必须**进入断言**（不是 rc2）。

    合成报告内容不足以满足 L20 金标 ⇒ 断言会红（rc=1）——这是**预期**：
    本测试只证明「断言对象切换到了快照那份」，而不是「又回落成根目录那份」。
    """
    root = tmp_path / "snapshots"
    _make_snapshot(root, "20261105_120000", with_report=True)
    try:
        rc = _run(monkeypatch, ["--snapshot-root", str(root)])
    except (FileNotFoundError, ValueError, KeyError):
        # 🔴 **环境差异**（两端都会走到这里，必须两端都绿）：
        #    * 本机没有服务器的数据集（<落点根>/datasets/etf/…）⇒ 崩在读数据上；
        #    * 服务器**有**数据集 ⇒ 合成夹具只有 1 行个股分数 ⇒ 崩在「分数方差为 0」。
        #    **这是预期**：本测试只证明「断言对象切到了快照那份」（看 stdout 的
        #    「[快照口径]」两行），不要求合成夹具能撑起完整断言。
        rc = -1
    out = capsys.readouterr().out
    assert rc != 2, "有报告（且数据齐）的快照不许判「不适用」"
    assert "快照口径" in out, f"必须打印快照口径，实得 {out[:300]!r}"


def test_snapshot_missing_stock_scores_is_not_applicable(tmp_path, monkeypatch, capsys) -> None:
    """快照里**缺个股分数** ⇒ rc2「不适用」（≠ 通过），**不许**回落 canonical。

    🔴 2026-11-10：`--snapshot-root` 只切报告不切数据的 bug 就是这么红的
       （断言对象 = 快照那份，个股分数却读 canonical ⇒ asof 过滤后为空 ⇒ 崩）。
    ⇒ 快照缺数据件 ⇒ 判「不适用」，**不是**回落 canonical 再报「通过」。
    """
    root = tmp_path / "snapshots"
    _make_snapshot(root, "20261105_120000", with_report=True, with_data=False)
    # canonical 有一份**完整**的个股分数 ⇒ 万一回落就能跑下去 ⇒ 必须 rc2
    canonical = tmp_path / "canonical"
    canonical.mkdir()
    (canonical / "stock_scores.csv").write_text(
        "instrument,date,score\nSH600000,2026-11-03,1.0\n", encoding="utf-8"
    )
    (canonical / "stock_prices.csv").write_text(
        "instrument,date,close\nSH600000,2026-11-03,10.0\n", encoding="utf-8"
    )
    monkeypatch.setattr(probe_lookahead, "RUN_ROOT", canonical)
    called: list = []

    def _spy(asof, data_root=None):
        called.append(data_root)
        return {"n_decision_lookahead": 0, "items": [], "non_decision": []}

    monkeypatch.setattr(probe_lookahead, "independent_ledger", _spy)
    rc = _run(monkeypatch, ["--snapshot-root", str(root)])
    out = capsys.readouterr().out
    assert rc == 2, f"缺个股分数必须判「不适用」(2)，实得 {rc}"
    assert rc != 0, "「不适用」≠「通过」"
    assert called == [], f"不许回落 canonical 读个股分数，实得 {called}"
    assert "不适用" in out and "回落" in out
