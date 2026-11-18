"""研究链 · 账户事实源导出器的单元测试。

🔴 为什么要有这个测试（2026-11-10 真缺陷的回归护栏）：
`verify_artifacts.py`（8/16 回归自检）与 `probes/probe_report_spec.py`（H1）
都改成「期望值只认**交付件同目录**的账户事实源，缺失 ⇒ FAIL（不许真空 PASS）」。
⇒ 研究链的交付目录**必须自带** `account_context.json` + `account_snapshot.csv`
⇒ 导出器一旦坏掉，研究链的 8/16 必然红、整链 rc=1 ⇒ 必须有测试兜住。
"""
from __future__ import annotations

import json
import subprocess
import sys
from pathlib import Path

import pytest

_REPO_ROOT = Path(__file__).resolve().parents[3]
_EMITTER = _REPO_ROOT / "manu" / "etf_rotation" / "emit_account_context.py"
_ACCOUNT = _REPO_ROOT / "manu" / "etf_rotation" / "account.py"


def _run(out_dir: Path, *extra: str) -> subprocess.CompletedProcess:
    return subprocess.run(
        [sys.executable, str(_EMITTER), "--out-dir", str(out_dir), *extra],
        capture_output=True,
        text=True,
        cwd=str(_REPO_ROOT),
        check=False,
    )


def test_emits_context_and_snapshot(tmp_path: Path) -> None:
    result = _run(tmp_path)
    assert result.returncode == 0, result.stdout + result.stderr
    ctx_path = tmp_path / "account_context.json"
    csv_path = tmp_path / "account_snapshot.csv"
    assert ctx_path.is_file()
    assert csv_path.is_file()
    payload = json.loads(ctx_path.read_text(encoding="utf-8"))
    # ---- 事实源的必含字段（verify_artifacts / probe_report_spec 都依赖它们）----
    assert payload["total_assets"] == pytest.approx(29854.67)
    assert payload["cash"] == pytest.approx(15518.87)
    codes = [p["code"] for p in payload["positions"]]
    #: 顺序跟基准 CSV 一致（512800 / 159929 / 513330 / 513770 / 512690）
    assert codes == ["512800", "159929", "513330", "513770", "512690"]
    assert sorted(codes) == ["159929", "512690", "512800", "513330", "513770"]


def test_snapshot_csv_is_byte_identical_to_baseline(tmp_path: Path) -> None:
    _run(tmp_path)
    baseline = (_REPO_ROOT / "manu" / "etf_rotation" / "data" / "account_snapshot.csv").read_bytes()
    assert (tmp_path / "account_snapshot.csv").read_bytes() == baseline


def test_arithmetic_identity_holds(tmp_path: Path) -> None:
    _run(tmp_path)
    payload = json.loads((tmp_path / "account_context.json").read_text(encoding="utf-8"))
    total_mv = sum(float(p["market_value"]) for p in payload["positions"])
    assert total_mv + float(payload["cash"]) == pytest.approx(
        float(payload["total_assets"]), abs=0.01
    )
    for position in payload["positions"]:
        assert float(position["shares"]) * float(position["price"]) == pytest.approx(
            float(position["market_value"]), abs=0.01
        )


def test_missing_baseline_fails_closed(tmp_path: Path) -> None:
    """基准文件缺失 ⇒ **必须 FAIL（退出码 2）**，绝不许写出空事实源。"""
    result = _run(tmp_path, "--account-snapshot", str(tmp_path / "nope.csv"))
    assert result.returncode == 2
    assert "[FAIL]" in result.stdout
    assert not (tmp_path / "account_context.json").exists()
