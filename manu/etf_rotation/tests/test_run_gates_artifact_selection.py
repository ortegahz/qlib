"""回归测试：闸门「断言对象（交付件根）」的**选取逻辑**（D-2 修复，2026-11-11）。

🔴 缺陷背景
    `run_gates.sh` 的「最新完整快照」判据只看 `snapshot_manifest.json` +
    `etf_analysis_report.json` + `etf_analysis_report.md` 三件 ⇒
    **`--skip-llm` 复跑产生的快照也会被选中**当闸门断言对象 ⇒
    结构上过不了 LLM 链（`verify_artifacts` 三件套断言红 + `probe_llm_stage_repro` 崩）
    ⇒ **不是代码回归，是断言对象选错了**（判据不许为刷绿而放宽 ⇒ 链不可校验就该判 FAIL）。

    修法：选取逻辑抽到 `manu/etf_rotation/lib/select_artifact_root.sh`（可被 pytest 单独回归），
    两级判据：
        ① **LLM 完整快照**（含 `ta_research_context.md` / `ta_context_meta.json` /
           `tradingagents_decision.json`）⇒ **优先取最新**；
        ② 一个都没有 ⇒ 回落「最新基础完整快照」（此时 LLM 类阶段会如实判红 ⇒ 正确语义）；
        ③ 再没有 ⇒ 回落 canonical（显式打印）。
"""

from __future__ import annotations

import subprocess
from pathlib import Path

ETF_DIR = Path(__file__).resolve().parents[1]
SCRIPT = ETF_DIR / "lib" / "select_artifact_root.sh"


def _mk_snapshot(root: Path, name: str, *, full: bool = True) -> Path:
    """造一个快照目录；`full=False` ⇒ 只有基础三件（模拟 `--skip-llm` 快照）。"""
    d = root / name
    d.mkdir(parents=True)
    (d / "snapshot_manifest.json").write_text("{}", encoding="utf-8")
    (d / "etf_analysis_report.json").write_text("{}", encoding="utf-8")
    (d / "etf_analysis_report.md").write_text("# report", encoding="utf-8")
    if full:
        (d / "ta_research_context.md").write_text("ctx", encoding="utf-8")
        (d / "ta_context_meta.json").write_text("{}", encoding="utf-8")
        (d / "tradingagents_decision.json").write_text("{}", encoding="utf-8")
    return d


def _select(snapshot_root: Path, canonical: Path) -> tuple[str, str]:
    out = subprocess.run(
        ["bash", str(SCRIPT), str(snapshot_root), str(canonical)],
        capture_output=True,
        text=True,
        check=True,
    )
    lines = out.stdout.splitlines()
    assert len(lines) >= 2, f"选取脚本输出不足两行：{out.stdout!r}"
    return lines[0], lines[1]


def test_picks_newest_llm_full_snapshot(tmp_path: Path) -> None:
    snap_root = tmp_path / "snapshots"
    canonical = tmp_path / "canonical"
    canonical.mkdir()
    old = _mk_snapshot(snap_root, "20261111_100742", full=True)
    new = _mk_snapshot(snap_root, "20261111_122825", full=True)

    root, note = _select(snap_root, canonical)

    assert root == str(new)
    assert root != str(old)
    assert "LLM 完整" in note


def test_skips_newer_basic_only_snapshot(tmp_path: Path) -> None:
    """🔴 D-2 核心回归：**更新的 `--skip-llm` 快照不得**抢走断言对象。"""
    snap_root = tmp_path / "snapshots"
    canonical = tmp_path / "canonical"
    canonical.mkdir()
    full = _mk_snapshot(snap_root, "20261111_100742", full=True)
    _mk_snapshot(snap_root, "20261111_121218", full=False)  # 更新、但缺 LLM 三件套

    root, note = _select(snap_root, canonical)

    assert root == str(full)
    assert "缺三件套" not in note  # 选到了完整件 ⇒ 不该出现回落告警
    assert "LLM 完整" in note


def test_falls_back_to_newest_basic_when_no_llm_snapshot(tmp_path: Path) -> None:
    snap_root = tmp_path / "snapshots"
    canonical = tmp_path / "canonical"
    canonical.mkdir()
    _mk_snapshot(snap_root, "20261111_100742", full=False)
    basic_new = _mk_snapshot(snap_root, "20261111_121218", full=False)

    root, note = _select(snap_root, canonical)

    assert root == str(basic_new)
    assert "缺三件套" in note  # 必须**显式**告知：LLM 类阶段会如实判红


def test_falls_back_to_canonical_when_nothing_complete(tmp_path: Path) -> None:
    snap_root = tmp_path / "snapshots"
    snap_root.mkdir()
    canonical = tmp_path / "canonical"
    canonical.mkdir()
    partial = snap_root / "20261111_122825"
    partial.mkdir()
    (partial / "snapshot_manifest.json").write_text("{}", encoding="utf-8")

    root, note = _select(snap_root, canonical)

    assert root == str(canonical)
    assert "回落 canonical" in note


def test_mutation_missing_decision_json_falls_back_to_basic(tmp_path: Path) -> None:
    """变异负例：拆掉 `tradingagents_decision.json` ⇒ 该快照**不再算 LLM 完整**。"""
    snap_root = tmp_path / "snapshots"
    canonical = tmp_path / "canonical"
    canonical.mkdir()
    broken = _mk_snapshot(snap_root, "20261111_122825", full=True)
    (broken / "tradingagents_decision.json").unlink()
    older_full = _mk_snapshot(snap_root, "20261111_100742", full=True)

    root, _note = _select(snap_root, canonical)

    assert root == str(older_full)
    assert root != str(broken)


def test_script_is_synced_into_run_gates() -> None:
    """`run_gates.sh` 必须**调用**抽出来的选取脚本（不许两份判据各自漂移）。"""
    gates = (ETF_DIR / "run_gates.sh").read_text(encoding="utf-8")
    assert "lib/select_artifact_root.sh" in gates
