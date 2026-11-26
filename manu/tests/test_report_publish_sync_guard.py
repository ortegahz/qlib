"""🔴 2026-11-26 新增：「**交付面报告 vs 快照真值报告**」一致性的**只读闸门**守卫。

【背景（本守卫的诞生原因：2026-11-26 实测事故）】
`manu/etf_rotation/daily/main.py` 落盘时**把同一份 markdown 写两处**：
① `<RESULTS_ROOT>/reports/report_<YYYYMMDD>.md`（**交付面** ⇒ 人读的那份）；
② `<快照>/report_<YYYYMMDD>.md`（**快照才是真值**）。
🔴 `manu/tools/regen_snapshot_derived.py`（**就地确定性重生成**）**只重写快照内**的派生件
⇒ 快照更新了、**交付面没更新** ⇒ 两个面向内容分叉。

🔴 **实测后果（11-25 那轮，11-26 修完真缺陷后）**：交付面那份仍是**修复前**的计划
（「买入 512890 2,800 股」＋「512800 维持」＝ TA 从未点名的标的被下单），
而快照真值已修成「买入 563020 4,300 股」＋「卖出 512800 2,000 股」
⇒ **人读的那份会引导下错单** ⇒ 必须机器化拦截。

【被测对象】
`manu/tools/check_report_publish_sync.py`（**纯只读**闸门，不写、不改、不删任何文件）。

【变异反证（守卫有牙）】
M1 沙箱基线绿（交付面 == 快照）⇒ rc 0；
M2 交付面内容被改 ⇒ 必须报「A 交付面与快照真值不一致」；
M3 快照内报告副本缺失 ⇒ 必须报「B 快照内缺报告副本」；
M4 交付面报告缺失 ⇒ 必须报「C 交付面缺报告」；
M5 同一 asof 有多个快照 ⇒ 必须以 ``finalized_at`` **最新**的那个为准；
M6 🔴 **反例**：交付面有报告但**找不到对应快照** ⇒ 只披露、**rc 0**（历史报告不算缺陷）；
M7 工具**只读** ⇒ 跑完整个结果根的 md5 必须一字不变；
M8 结果根不存在 ⇒ 退出码 **2**（用法/输入错误，不是「绿灯」）。

【live 集成断言】
对**真实结果根**跑工具 ⇒ 断言 rc == 0。
🔴 结果根**只存在于服务器**（本机没有）⇒ 本机跑这一条会 ``skip``；
    但**服务器回归会跑到同一个文件** ⇒ 那时这条 **不是 skip、是真跑**（不许弱化）。
"""

from __future__ import annotations

import hashlib
import importlib.util
import json
import os
import sys
from pathlib import Path

import pytest

RESULTS_ROOT = Path(
    os.environ.get("ETF_ROTATION_RESULTS_ROOT", "/mnt/data/siping/runs/etf_rotation_results")
)


def _find_tool() -> Path:
    """向上找 ``manu/tools/check_report_publish_sync.py``。

    🔴 本文件有**两个镜像位置**（``manu/etf_rotation/tests/`` 与 ``manu/tests/``）
    ⇒ **不许**用固定的 ``parents[N]`` 定位 ⇒ 改成向上搜索。
    """
    for parent in Path(__file__).resolve().parents:
        candidate = parent / "manu" / "tools" / "check_report_publish_sync.py"
        if candidate.is_file():
            return candidate
    raise AssertionError("找不到 manu/tools/check_report_publish_sync.py")


def _load_tool():
    path = _find_tool()
    spec = importlib.util.spec_from_file_location("check_report_publish_sync", path)
    assert spec and spec.loader
    module = importlib.util.module_from_spec(spec)
    # 🔴 必须先注册进 ``sys.modules``：被测模块用了 ``@dataclass`` ＋
    #    ``from __future__ import annotations`` ⇒ dataclasses 会回查
    #    ``sys.modules[cls.__module__].__dict__`` ⇒ 不注册会直接 AttributeError。
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


TOOL = _load_tool()


def _write_snapshot(root: Path, run_id: str, *, asof: str, finalized: str, body: str | None) -> Path:
    snap = root / "snapshots" / run_id
    snap.mkdir(parents=True, exist_ok=True)
    (snap / "snapshot_manifest.json").write_text(
        json.dumps({"run_id": run_id, "asof": asof, "finalized_at": finalized}, ensure_ascii=False),
        encoding="utf-8",
    )
    if body is not None:
        (snap / f"report_{asof.replace('-', '')}.md").write_text(body, encoding="utf-8")
    return snap


def _write_published(root: Path, asof: str, body: str) -> Path:
    reports = root / "reports"
    reports.mkdir(parents=True, exist_ok=True)
    path = reports / f"report_{asof.replace('-', '')}.md"
    path.write_text(body, encoding="utf-8")
    return path


@pytest.fixture()
def sandbox(tmp_path: Path) -> Path:
    root = tmp_path / "results"
    root.mkdir()
    return root


def _tree_digest(root: Path) -> dict[str, str]:
    out: dict[str, str] = {}
    for path in sorted(p for p in root.rglob("*") if p.is_file()):
        out[str(path.relative_to(root))] = hashlib.md5(path.read_bytes()).hexdigest()
    return out


def test_m1_baseline_green(sandbox: Path) -> None:
    """M1：交付面与快照逐字节一致 ⇒ rc 0、零缺陷。"""
    _write_snapshot(sandbox, "20261125_180119", asof="2026-11-25", finalized="2026-11-25T18:01:19+08:00", body="OK\n")
    _write_published(sandbox, "2026-11-25", "OK\n")
    result = TOOL.analyze(str(sandbox))
    assert result.ok, result.as_dict()
    assert result.checked == 1


def test_m2_published_drift_must_be_flagged(sandbox: Path) -> None:
    """M2：交付面被改 ⇒ 必须报 A。"""
    _write_snapshot(sandbox, "20261125_180119", asof="2026-11-25", finalized="2026-11-25T18:01:19+08:00", body="买入 563020 4300\n")
    _write_published(sandbox, "2026-11-25", "买入 512890 2800\n")
    result = TOOL.analyze(str(sandbox))
    assert not result.ok
    codes = [d.code for d in result.defects]
    assert "A" in codes, result.as_dict()
    assert TOOL.main(["--results-root", str(sandbox)]) == 1


def test_m3_snapshot_report_missing_must_be_flagged(sandbox: Path) -> None:
    """M3：快照内报告副本缺失 ⇒ 必须报 B。"""
    _write_snapshot(sandbox, "20261125_180119", asof="2026-11-25", finalized="2026-11-25T18:01:19+08:00", body=None)
    _write_published(sandbox, "2026-11-25", "任何内容\n")
    result = TOOL.analyze(str(sandbox))
    assert not result.ok
    assert "B" in [d.code for d in result.defects], result.as_dict()


def test_m4_published_report_missing_must_be_flagged(sandbox: Path) -> None:
    """M4：交付面报告缺失 ⇒ 必须报 C。"""
    _write_snapshot(sandbox, "20261125_180119", asof="2026-11-25", finalized="2026-11-25T18:01:19+08:00", body="OK\n")
    result = TOOL.analyze(str(sandbox))
    assert not result.ok
    assert "C" in [d.code for d in result.defects], result.as_dict()


def test_m5_latest_snapshot_wins(sandbox: Path) -> None:
    """M5：同一 asof 两个快照 ⇒ 以 ``finalized_at`` 最新的那个为准。"""
    _write_snapshot(sandbox, "20261125_180119", asof="2026-11-25", finalized="2026-11-25T18:01:19+08:00", body="旧\n")
    _write_snapshot(sandbox, "20261125_200000", asof="2026-11-25", finalized="2026-11-26T09:15:16+08:00", body="新\n")
    _write_published(sandbox, "2026-11-25", "新\n")
    result = TOOL.analyze(str(sandbox))
    assert result.ok, result.as_dict()

    _write_published(sandbox, "2026-11-25", "旧\n")
    result = TOOL.analyze(str(sandbox))
    assert not result.ok
    assert "A" in [d.code for d in result.defects], result.as_dict()


def test_m6_published_without_snapshot_is_only_disclosed(sandbox: Path) -> None:
    """M6（反例）：交付面有报告但没有对应快照 ⇒ 只披露、**rc 0**。"""
    _write_published(sandbox, "2026-11-17", "历史报告\n")
    result = TOOL.analyze(str(sandbox))
    assert result.ok, result.as_dict()
    assert any("找不到对应快照" in note for note in result.notes), result.as_dict()


def test_m7_tool_is_read_only(sandbox: Path) -> None:
    """M7：工具只读 ⇒ 跑完整个结果根的 md5 必须一字不变。"""
    _write_snapshot(sandbox, "20261125_180119", asof="2026-11-25", finalized="2026-11-25T18:01:19+08:00", body="OK\n")
    _write_published(sandbox, "2026-11-25", "别的\n")
    before = _tree_digest(sandbox)
    TOOL.main(["--results-root", str(sandbox)])
    after = _tree_digest(sandbox)
    assert before == after


def test_m8_missing_results_root_is_usage_error(sandbox: Path) -> None:
    """M8：结果根不存在 ⇒ 退出码 2（**不许**当成绿灯）。"""
    assert TOOL.main(["--results-root", str(sandbox / "nope")]) == 2


def test_m9_live_results_root_is_green() -> None:
    """M9：live 集成 —— **真实结果根**必须 rc 0。

    🔴 结果根**只存在于服务器** ⇒ 本机 skip；**服务器回归会真跑这一条**。
    """
    if not RESULTS_ROOT.is_dir():
        pytest.skip(f"结果根不在本机（{RESULTS_ROOT}）⇒ 服务器回归时真跑")
    assert TOOL.main(["--results-root", str(RESULTS_ROOT)]) == 0
