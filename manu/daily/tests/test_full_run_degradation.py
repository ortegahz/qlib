"""🔴 2026-11-10 新增：`--full` 的「不遗漏任何一个环节」硬保证 ＋ 降级事实必须进交付面。

本轮独立审计落地了 4 处行为（这里逐条钉住）：

  F1 **W（成分股权重）陈旧度必须进交付面** —— W 是**只读缓存、不重爬**（重爬成分股
     ⇒ IP 封禁）⇒ 它的陈旧度若只进日志，「实时拉了数据」就是假象（E=W×S 里的 W
     可能还是 N 天前的快照）。⇒ manifest 有 ``w_holdings`` 阶段（stale/ok）
     ＋ 报告 notes 有 🔴 说明。

  F2 **「自建增量日」兜底失败必须进 manifest** —— 兜底失败 ⇒ qlib 基座停在旧末日
     ⇒ S 层只能打到旧数据 ⇒ 只进 notes（notes 只进日志）＝ **静默降级**。
     ⇒ manifest 有 ``stage1_increment`` 阶段（failed）＋ error 带上失败原因。

  F3 **`--full` 的「不遗漏任何一个环节」硬保证** —— manifest 里写着 failed、进程却
     退出 0 ⇒ 静默降级。⇒ ``--full`` 下失败 ⇒ **退出码 1** ＋ **不发信**
     （fail-closed）＋ manifest/heartbeat/报告三处都记 degraded。
     🔴 阶段 1 的失败（换库失败 / 增量兜底失败）也**必须**进这份清单。

  F4 **`--dry-run` 语义碰撞** —— CLI 的 ``--dry-run``＝「显式声明观察态」（观察态本来
     就是缺省 ⇒ **不改变任何行为**），与「不取数 / 不写产物 / 不调 LLM」的前置判定
     **同名不同义** ⇒ 库函数侧改名 ``preflight_only``。

【变异反证】
每条行为都配一个「把源码**读出来 → 在内存里改 → 写到 tmp_path → import 副本**」的
变异测试 ⇒ 改坏源码里的关键判定 ⇒ 对应断言**必须转红**。
⛔ **绝不落盘改真源码**（源文件是主 agent 的写集）。
"""

from __future__ import annotations

import importlib.util
import inspect
import json
import logging
import sys
from pathlib import Path
from types import SimpleNamespace

import pandas as pd
import pytest

import account_state
import agents_rebalance as AR
import calendar_gate
import fetch_data
import main as daily_main
import notify_email
import report
import snapshot as snap_mod
import stage1_dataset

#: `daily/` 目录（源文件真实位置 —— 只**读**，绝不写）
DAILY_DIR = Path(daily_main.__file__).resolve().parent
MAIN_SOURCE = DAILY_DIR / "main.py"
AR_SOURCE = DAILY_DIR / "agents_rebalance.py"

#: 本轮测试统一的业务日
ASOF = "2026-11-05"
ASOF_TAG = ASOF.replace("-", "")

#: 「全部环节都跑成」时的交付件清单（`_record_pipeline_stages` 按**件是否存在**判状态）
ALL_ARTIFACTS = (
    "etf_analysis_report.json",
    "rebalance_recommendation.json",
    "ta_context_meta.json",
    "final_rebalance_plan.json",
    f"execution_card_{ASOF_TAG}.json",
)


# =============================================================================
# 0. 工具：变异反证（源码 → 内存改 → tmp_path → import 副本）
# =============================================================================
def _mutated_module(tmp_path: Path, source: Path,
                    edits: list[tuple[str, str]], tag: str):
    """🔴 把源码**读出来 → 在内存里改 → 写到 tmp_path → import 副本**。

    ⛔ **绝不落盘改真源码**：仓库里的 ``main.py`` / ``agents_rebalance.py`` 是主 agent
    的写集，本测试只在 ``tmp_path`` 里造副本。

    参数
    ----
    edits:
        ``[(锚点原文, 替换成)]`` 列表；锚点原文**必须存在**于源码里（不存在就报错
        ⇒ 源码变了要同步改测试，不许悄悄放过）。
    """
    text = source.read_text(encoding="utf-8")
    for old, new in edits:
        assert old in text, f"变异锚点不存在（源码已变）：{old!r}"
        text = text.replace(old, new)
    target = tmp_path / f"{tag}.py"
    target.write_text(text, encoding="utf-8")

    saved_path = list(sys.path)
    name = f"mutant_{tag}"
    spec = importlib.util.spec_from_file_location(name, target)
    module = importlib.util.module_from_spec(spec)
    sys.modules[name] = module
    try:
        spec.loader.exec_module(module)
    finally:
        sys.path[:] = saved_path
    return module


# =============================================================================
# 1. 工具：把整条流水线打桩后跑一轮
# =============================================================================
def _install_stubs(monkeypatch, *, holdings: dict | None,
                   stage1: dict | None, stage1_raises: bool,
                   llm_status: str, node_reprice_ok: bool,
                   artifacts: tuple[str, ...], captured: dict) -> None:
    """把「取数 / 换库 / 行情 / 账户 / 计算 / 报告 / 发信」全部打桩。"""
    monkeypatch.setattr(calendar_gate, "is_trading_day", lambda *a, **k: True)

    fetch_payload = {
        "asof": ASOF,
        "n_rows": 3,
        "n_codes": 3,
        "holdings": dict(holdings or {}),
        "guard_note": "",
    }
    monkeypatch.setattr(fetch_data, "run_fetch", lambda **kw: dict(fetch_payload))

    def _fake_prices(**kw) -> pd.DataFrame:
        return pd.DataFrame({"code": ["510300"], "date": [ASOF], "close": [1.0]})

    monkeypatch.setattr(fetch_data, "load_effective_prices", _fake_prices)

    def _fake_stage1(**kw) -> dict:
        if stage1_raises:
            raise RuntimeError("Boom")
        return dict(stage1 or {})

    monkeypatch.setattr(stage1_dataset, "run_stage1", _fake_stage1)
    monkeypatch.setattr(
        account_state, "account_summary",
        lambda: {"total_assets": 1.0, "cash": 1.0, "positions": []},
    )

    def _fake_run_pipeline(**kw) -> dict:
        snap = kw.get("snapshot")
        run_path = Path(snap.path)
        run_path.mkdir(parents=True, exist_ok=True)
        for name in artifacts:
            (run_path / name).write_text("{}", encoding="utf-8")
        return {
            "live_trade": bool(kw.get("live_trade")),
            "asof": ASOF,
            "execution_date": None,
            "run_dir": str(run_path),
            "snapshot": {"root": str(snap.root), "path": str(run_path),
                         "run_id": str(snap.run_id)},
            "report_json": str(run_path / "etf_analysis_report.json"),
            "plan_json": str(run_path / "final_rebalance_plan.json"),
            "node_json": str(run_path / f"node_reprice_{ASOF_TAG}.json"),
            "card_json": str(run_path / f"execution_card_{ASOF_TAG}.json"),
            "card": {"orders": [], "post_trade": {"cash": 0.0}},
            "decision_json": str(run_path / "tradingagents_decision.json"),
            "decision_md": str(run_path / "tradingagents_decision.md"),
            "scores_path": str(run_path / "stock_scores.csv"),
            "prices_path": str(run_path / "etf_prices.csv"),
            "email_kind": "rebalance_card",
            "notes": ["ℹ️ 测试夹具"],
            "holding_discipline": None,
            "analysis_asof": ASOF,
            "scores_max_date": None,
            "score_grid_last": None,
            "qlib_dataset_max_date": None,
            "llm_decision_status": llm_status,
            "execution_lag_days": 1,
            "node_reprice_ok": node_reprice_ok,
            "node_reprice_error": "" if node_reprice_ok else "节点日价格拿不到",
            "generated_at": "2026-11-10T00:00:00+08:00",
            "preflight_only": False,
        }

    monkeypatch.setattr(AR, "run_pipeline", _fake_run_pipeline)

    def _fake_render_report(result, **kw) -> str:
        captured["result"] = dict(result)
        return "# 测试报告\n"

    monkeypatch.setattr(report, "render_report", _fake_render_report)

    def _fake_write_report(markdown: str, path) -> Path:
        out = Path(path)
        out.parent.mkdir(parents=True, exist_ok=True)
        out.write_text(markdown, encoding="utf-8")
        return out

    monkeypatch.setattr(report, "write_report", _fake_write_report)
    monkeypatch.setattr(
        notify_email, "render_rebalance_card",
        lambda **kw: SimpleNamespace(subject="s", html_body="<p/>", text_body="t"),
    )

    def _fake_send_email(*a, **kw) -> dict:
        captured["emails"].append({"args": a, "kwargs": kw})
        return {"sent": False, "path": None}

    monkeypatch.setattr(notify_email, "send_email", _fake_send_email)


def _run_pipeline(monkeypatch, tmp_path: Path, *, main_mod=daily_main,
                  argv: list[str] | None = None, holdings: dict | None = None,
                  stage1: dict | None = None, stage1_raises: bool = False,
                  llm_status: str = "ok", node_reprice_ok: bool = True,
                  artifacts: tuple[str, ...] = ALL_ARTIFACTS) -> dict:
    """跑一轮**全打桩**的流水线，返回 {rc, snap, manifest, stages, notes, heartbeat}。"""
    captured: dict = {"result": None, "emails": []}
    _install_stubs(
        monkeypatch,
        holdings=holdings,
        stage1=stage1,
        stage1_raises=stage1_raises,
        llm_status=llm_status,
        node_reprice_ok=node_reprice_ok,
        artifacts=artifacts,
        captured=captured,
    )
    snapshot_root = tmp_path / "snapshots"
    args = main_mod.parse_args(argv if argv is not None else [
        "--asof", ASOF,
        "--snapshot-root", str(snapshot_root),
        "--report-md", str(tmp_path / "report.md"),
    ])
    log = logging.getLogger("t_full_run_degradation")
    log.addHandler(logging.NullHandler())
    state: dict = {}
    rc = main_mod._run_stages(args, ASOF, snapshot_root, log, state=state)

    snap = state["snapshot"]
    manifest = json.loads(
        (snap.path / "snapshot_manifest.json").read_text(encoding="utf-8")
    )
    heartbeat_path = snap.path / f"heartbeat_{ASOF_TAG}.json"
    heartbeat = json.loads(heartbeat_path.read_text(encoding="utf-8"))
    result = captured["result"] or {}
    return {
        "rc": rc,
        "snap": snap,
        "manifest": manifest,
        "stages": {str(e["name"]): e for e in manifest["stages"]},
        "notes": list(result.get("notes") or []),
        "heartbeat": heartbeat,
        "emails": captured["emails"],
        "result": result,
    }


def _record_stages(main_mod, tmp_path: Path, *, artifacts: tuple[str, ...],
                    llm_status: str = "ok", node_reprice_ok: bool = True,
                    full: bool = False):
    """直接调 ``_record_pipeline_stages`` ⇒ 返回 (失败清单, manifest 阶段表)。"""
    run_dir = tmp_path / "run"
    run_dir.mkdir(parents=True, exist_ok=True)
    for name in artifacts:
        (run_dir / name).write_text("{}", encoding="utf-8")
    snap = snap_mod.create_snapshot(tmp_path / "snaps", asof=ASOF)
    result = {
        "run_dir": str(run_dir),
        "llm_decision_status": llm_status,
        "node_reprice_ok": node_reprice_ok,
        "node_reprice_error": "" if node_reprice_ok else "节点日价格拿不到",
    }
    flags = {
        "fetch": True, "refresh_scores": True, "update_qlib": True,
        "llm": True, "increment": True, "increment_dry_run": False,
        "full": full, "skipped": [],
    }
    failed = main_mod._record_pipeline_stages(
        snap, result, flags=flags, refresh_scores=True
    )
    stages = {str(e["name"]): e for e in snap.manifest_payload()["stages"]}
    return failed, stages


# =============================================================================
# 2. ① W（成分股权重）陈旧度必须进 manifest
# =============================================================================
STALE_HOLDINGS = {
    "stale": True,
    "max_fetch_date": "2026-11-01",
    "stale_days": 7,
    "mixed": False,
    "n_codes": 3,
}


def test_w_holdings_stale_goes_to_manifest(monkeypatch, tmp_path: Path) -> None:
    """① W 陈旧 ⇒ manifest 必须有 ``w_holdings`` 阶段（status=stale）＋ 带上滞后天数。"""
    out = _run_pipeline(monkeypatch, tmp_path, holdings=STALE_HOLDINGS)

    stages = out["stages"]
    assert "w_holdings" in stages, f"manifest 必须有 w_holdings 阶段，实得 {list(stages)}"
    entry = stages["w_holdings"]
    assert entry["status"] == "stale", f"陈旧的 W 必须记 stale，实得 {entry}"
    assert entry["stale_days"] == 7, f"必须带上滞后天数，实得 {entry}"
    assert entry["max_fetch_date"] == "2026-11-01", entry
    assert entry["mixed"] is False, entry
    assert entry["n_codes"] == 3, entry


def test_w_holdings_fresh_goes_to_manifest(monkeypatch, tmp_path: Path) -> None:
    """① W 不陈旧 ⇒ status=ok（不许把所有 W 都记成 stale）。"""
    holdings = dict(STALE_HOLDINGS, stale=False, stale_days=0)
    out = _run_pipeline(monkeypatch, tmp_path, holdings=holdings)

    entry = out["stages"]["w_holdings"]
    assert entry["status"] == "ok", f"新鲜的 W 必须记 ok，实得 {entry}"
    assert entry["stale_days"] == 0, entry


def test_w_stale_is_disclosed_in_report_notes(monkeypatch, tmp_path: Path) -> None:
    """④ W 陈旧 ⇒ 报告 notes 必须有 🔴 说明（含「只读缓存、不重爬」与滞后天数）。"""
    out = _run_pipeline(monkeypatch, tmp_path, holdings=STALE_HOLDINGS)

    notes = "\n".join(out["notes"])
    assert "🔴" in notes, f"必须有 🔴 说明，实得 {out['notes']}"
    assert "W（成分股权重）是只读缓存、不重爬" in notes, notes
    assert "滞后 **7 天**" in notes, notes
    assert "2026-11-01" in notes, notes


# =============================================================================
# 3. ② 「自建增量日」兜底失败必须进 manifest
# =============================================================================
FAILED_INCREMENT = {
    "increment": {
        "ok": False,
        "needed": True,
        "days": ["2026-11-06"],
        "entry": {},
        "notes": ["⚠️ 自建增量日失败：Boom"],
    },
    "calendar_max_before": "2026-11-04",
    "calendar_max_after": "2026-11-04",
    "new_days": [],
    "swapped": False,
    "downloaded": False,
    "notes": [],
}


def test_increment_failure_goes_to_manifest(monkeypatch, tmp_path: Path) -> None:
    """② 增量兜底失败 ⇒ manifest 有 ``stage1_increment``（failed）＋ error 带原因。"""
    out = _run_pipeline(monkeypatch, tmp_path, stage1=FAILED_INCREMENT)

    stages = out["stages"]
    assert "stage1_increment" in stages, f"manifest 必须有 stage1_increment，实得 {list(stages)}"
    entry = stages["stage1_increment"]
    assert entry["status"] == "failed", f"兜底失败必须记 failed，实得 {entry}"
    assert "Boom" in str(entry.get("error")), f"error 必须带失败原因，实得 {entry}"
    assert list(entry.get("days") or []) == ["2026-11-06"], entry


def test_stage1_dataset_detail_is_in_manifest(monkeypatch, tmp_path: Path) -> None:
    """② 换库细节必须挂进 manifest（读者一眼看出「库没换」）。

    🔴 口径说明：当前源码把细节**合并进** ``stage1_dataset`` 这一条 manifest 条目
    （**没有**单独的 ``stage1_dataset_detail`` 阶段）⇒ 这里按**源码实际**断言。
    """
    out = _run_pipeline(monkeypatch, tmp_path, stage1=FAILED_INCREMENT)

    entry = out["stages"]["stage1_dataset"]
    assert entry["status"] == "ok", entry
    assert entry["calendar_max_before"] == "2026-11-04", entry
    assert entry["calendar_max_after"] == "2026-11-04", entry
    assert list(entry.get("new_days") or []) == [], entry
    assert entry["swapped"] is False, entry
    assert entry["downloaded"] is False, entry


def test_increment_failure_is_disclosed_in_report_notes(monkeypatch, tmp_path: Path) -> None:
    """④ 增量兜底失败 ⇒ 报告 notes 必须有 🔴 说明。"""
    out = _run_pipeline(monkeypatch, tmp_path, stage1=FAILED_INCREMENT)

    notes = "\n".join(out["notes"])
    assert "🔴" in notes, out["notes"]
    assert "自建增量日" in notes, notes
    assert "Boom" in notes, notes


# =============================================================================
# 4. ③ `_record_pipeline_stages` 返回失败阶段清单
# =============================================================================
def test_record_pipeline_stages_returns_failed_list(tmp_path: Path) -> None:
    """③ 缺 ``etf_analysis_report.json`` ⇒ 返回值含 ``analyze``。"""
    failed, stages = _record_stages(
        daily_main, tmp_path,
        artifacts=tuple(a for a in ALL_ARTIFACTS if a != "etf_analysis_report.json"),
    )
    assert "analyze" in failed, f"缺件必须进失败清单，实得 {failed}"
    assert stages["analyze"]["status"] == "failed", stages["analyze"]


def test_node_reprice_failure_is_not_hard_failure(tmp_path: Path) -> None:
    """③ ``node_reprice`` 失败是**设计内降级** ⇒ 不进硬失败清单（但 manifest 仍记 failed）。"""
    failed, stages = _record_stages(
        daily_main, tmp_path, artifacts=ALL_ARTIFACTS, node_reprice_ok=False,
    )
    assert "node_reprice" not in failed, f"设计内降级不许算硬失败，实得 {failed}"
    assert stages["node_reprice"]["status"] == "failed", stages["node_reprice"]


def test_full_counts_missing_tradingagents(tmp_path: Path) -> None:
    """③ ``--full`` 且 LLM 状态 != ok ⇒ 返回值含 ``tradingagents``（有环节没跑）。"""
    failed, _ = _record_stages(
        daily_main, tmp_path, artifacts=ALL_ARTIFACTS,
        llm_status="absent", full=True,
    )
    assert "tradingagents" in failed, f"--full 下 LLM 没跑必须算失败，实得 {failed}"


def test_non_full_does_not_count_missing_tradingagents(tmp_path: Path) -> None:
    """③ 非 ``--full`` ⇒ LLM 状态 != ok **不**进硬失败清单（日常 cron 语义不变）。"""
    failed, _ = _record_stages(
        daily_main, tmp_path, artifacts=ALL_ARTIFACTS,
        llm_status="absent", full=False,
    )
    assert "tradingagents" not in failed, f"非 --full 不许越权判失败，实得 {failed}"


# =============================================================================
# 5. ③ `--full` 的硬保证：退出码 / 不发信 / degraded 三处披露
# =============================================================================
def test_full_with_failed_stage_returns_exit_code_1(monkeypatch, tmp_path: Path) -> None:
    """③ ``--full`` + 有环节没跑成 ⇒ 退出码 1（fail-closed）。"""
    out = _run_pipeline(
        monkeypatch, tmp_path,
        argv=["--asof", ASOF, "--full", "--snapshot-root", str(tmp_path / "snapshots"),
              "--report-md", str(tmp_path / "report.md")],
        stage1=FAILED_INCREMENT,
    )
    assert out["rc"] == 1, f"--full 有环节没跑成必须退出码 1，实得 {out['rc']}"


def test_non_full_with_failed_stage_returns_0_but_degraded(monkeypatch, tmp_path: Path) -> None:
    """③ 非 ``--full`` ⇒ 退出码仍是 0，但 manifest / heartbeat / 报告**三处**都记 degraded。"""
    out = _run_pipeline(
        monkeypatch, tmp_path,
        stage1=FAILED_INCREMENT,
        llm_status="absent",
    )
    assert out["rc"] == 0, f"非 --full 保持退出码 0，实得 {out['rc']}"
    assert out["stages"]["pipeline"]["status"] == "degraded", out["stages"]["pipeline"]
    assert out["heartbeat"]["status"] == "degraded", out["heartbeat"]
    assert "stage1_increment" in list(out["stages"]["pipeline"].get("failed_stages") or [])


def test_full_fail_closed_does_not_send_email(monkeypatch, tmp_path: Path) -> None:
    """③ ``--full`` + 有环节没跑成 ⇒ **不发信**（不许拿缺件交付件触发实盘通知）。"""
    out = _run_pipeline(
        monkeypatch, tmp_path,
        argv=["--asof", ASOF, "--full", "--live-trade", "--send-email",
              "--snapshot-root", str(tmp_path / "snapshots"),
              "--report-md", str(tmp_path / "report.md")],
        stage1=FAILED_INCREMENT,
    )
    assert out["emails"] == [], f"--full 缺件时绝不许发信，实得 {out['emails']}"
    assert out["rc"] == 1, out["rc"]


def test_non_full_with_failed_stage_does_not_send_email(monkeypatch, tmp_path: Path) -> None:
    """🔴 非 ``--full``（cron 口径）+ ``--live-trade --send-email`` + 阶段失败
    ⇒ **同样不许发信**（独立审计发现的口子：原先条件带 ``and flags.get("full")``
    ⇒ 非 full 会带着 degraded 交付件发出**实盘**通知）；但退出码仍保持 0。"""
    out = _run_pipeline(
        monkeypatch, tmp_path,
        argv=["--asof", ASOF, "--live-trade", "--send-email",
              "--snapshot-root", str(tmp_path / "snapshots"),
              "--report-md", str(tmp_path / "report.md")],
        stage1=FAILED_INCREMENT,
    )
    assert out["emails"] == [], f"有环节缺件时绝不许发信（任何模式），实得 {out['emails']}"
    assert out["rc"] == 0, f"非 --full 保持退出码 0，实得 {out['rc']}"
    notes = "\n".join(out["notes"])
    assert "不发信" in notes, notes


def test_failed_stage_names_are_disclosed_in_report_notes(monkeypatch, tmp_path: Path) -> None:
    """④ 报告 notes 必须点名**失败阶段名**（读者不看 manifest 也能知道缺了什么）。"""
    out = _run_pipeline(
        monkeypatch, tmp_path,
        argv=["--asof", ASOF, "--full", "--snapshot-root", str(tmp_path / "snapshots"),
              "--report-md", str(tmp_path / "report.md")],
        stage1=FAILED_INCREMENT,
    )
    notes = "\n".join(out["notes"])
    assert "🔴" in notes, out["notes"]
    assert "stage1_increment" in notes, notes
    assert "硬失败" in notes and "退出码 1" in notes and "不发信" in notes, notes


# =============================================================================
# 6. ③（补）阶段 1 的失败也必须进 `--full` 的硬失败清单
# =============================================================================
def test_stage1_increment_failure_joins_failed_stages(monkeypatch, tmp_path: Path) -> None:
    """🔴 阶段 1（增量兜底失败）必须**合并进** ``failed_stages``（否则 --full 仍退出 0）。"""
    out = _run_pipeline(
        monkeypatch, tmp_path,
        argv=["--asof", ASOF, "--full", "--snapshot-root", str(tmp_path / "snapshots"),
              "--report-md", str(tmp_path / "report.md")],
        stage1=FAILED_INCREMENT,
    )
    failed = list(out["stages"]["pipeline"].get("failed_stages") or [])
    assert "stage1_increment" in failed, f"增量兜底失败必须进硬失败清单，实得 {failed}"
    assert out["rc"] == 1, f"--full 下必须退出码 1，实得 {out['rc']}"


def test_stage1_dataset_exception_joins_failed_stages(monkeypatch, tmp_path: Path) -> None:
    """🔴 阶段 1（换库抛异常）也必须**合并进** ``failed_stages``。"""
    out = _run_pipeline(
        monkeypatch, tmp_path,
        argv=["--asof", ASOF, "--full", "--snapshot-root", str(tmp_path / "snapshots"),
              "--report-md", str(tmp_path / "report.md")],
        stage1_raises=True,
    )
    failed = list(out["stages"]["pipeline"].get("failed_stages") or [])
    assert "stage1_dataset" in failed, f"换库失败必须进硬失败清单，实得 {failed}"
    assert out["stages"]["stage1_dataset"]["status"] == "failed"
    assert out["rc"] == 1, f"--full 下必须退出码 1，实得 {out['rc']}"


# =============================================================================
# 7. ⑤ `preflight_only` 改名生效
# =============================================================================
def test_run_pipeline_signature_has_no_dry_run() -> None:
    """⑤ ``run_pipeline`` 签名里**不许**再有 ``dry_run``（与 CLI 的 ``--dry-run`` 语义碰撞）。"""
    params = inspect.signature(AR.run_pipeline).parameters
    assert "dry_run" not in params, f"库函数侧不许再叫 dry_run，实得 {list(params)}"
    assert "preflight_only" in params, f"必须叫 preflight_only，实得 {list(params)}"
    assert params["preflight_only"].default is False, params["preflight_only"]


def test_run_pipeline_result_uses_preflight_only_key(monkeypatch, tmp_path: Path) -> None:
    """⑤ 前置判定分支的结果 dict 用 ``preflight_only`` 键（**没有** ``dry_run`` 键）。"""
    snap = snap_mod.create_snapshot(tmp_path / "snaps", asof=ASOF)
    monkeypatch.setattr(
        AR, "_resolve_analysis_asof",
        lambda **kw: {"notes": [], "analysis_asof": kw.get("signal_date"),
                      "scores_max_date": None, "grid_last": None, "dataset_max_date": None},
    )
    monkeypatch.setattr(
        AR.snap_mod, "resolve_scores_base", lambda *a, **k: (None, "测试夹具：无底座")
    )

    result = AR.run_pipeline(asof=ASOF, snapshot=snap, preflight_only=True)

    assert "preflight_only" in result, f"结果 dict 必须有 preflight_only 键，实得 {list(result)}"
    assert result["preflight_only"] is True, result.get("preflight_only")
    assert "dry_run" not in result, f"结果 dict 不许再有 dry_run 键，实得 {list(result)}"


# =============================================================================
# 8. 变异反证：改坏源码里的关键判定 ⇒ **原断言必须转红**
# =============================================================================
# 反证口径：把源码**读出来 → 在内存里改 → 写到 tmp_path → import 副本**，
# 用副本跑同一个场景，然后把**原测试里的那条断言**原样套上去
# ⇒ 必须抛 AssertionError（= 原测试转红）。
# ⛔ 全程**不落盘改真源码**（源文件是主 agent 的写集）。
def test_mutation_w_stale_judgement_broken(monkeypatch, tmp_path: Path) -> None:
    """变异反证 ①：``"stale" if holdings.get("stale") else "ok"`` → 恒 ``"ok"``
    ⇒ 原断言 ``status == "stale"`` 必须转红。"""
    mutant = _mutated_module(
        tmp_path, MAIN_SOURCE,
        [('"stale" if holdings.get("stale") else "ok"', '"ok"')],
        "main_w_stale_broken",
    )
    out = _run_pipeline(monkeypatch, tmp_path, main_mod=mutant, holdings=STALE_HOLDINGS)

    assert "w_holdings" in out["stages"], "变异必须只改判定、不改阶段登记"
    with pytest.raises(AssertionError):
        assert out["stages"]["w_holdings"]["status"] == "stale"


def test_mutation_increment_failure_judgement_broken(monkeypatch, tmp_path: Path) -> None:
    """变异反证 ②：``"ok" if inc_ok else "failed"`` → 恒 ``"ok"``
    ⇒ 原断言 ``status == "failed"`` 必须转红。"""
    mutant = _mutated_module(
        tmp_path, MAIN_SOURCE,
        [('"ok" if inc_ok else "failed"', '"ok"')],
        "main_increment_broken",
    )
    out = _run_pipeline(monkeypatch, tmp_path, main_mod=mutant, stage1=FAILED_INCREMENT)

    assert "stage1_increment" in out["stages"], "变异必须只改判定、不改阶段登记"
    with pytest.raises(AssertionError):
        assert out["stages"]["stage1_increment"]["status"] == "failed"


def test_mutation_node_reprice_exclusion_broken(tmp_path: Path) -> None:
    """变异反证 ③a：「排除 node_reprice」→ 不排除 ⇒ 原断言必须转红。"""
    mutant = _mutated_module(
        tmp_path, MAIN_SOURCE,
        [('hard_failed = [name for name in failed if name != "node_reprice"]',
          "hard_failed = list(failed)")],
        "main_node_exclusion_broken",
    )
    failed, stages = _record_stages(
        mutant, tmp_path, artifacts=ALL_ARTIFACTS, node_reprice_ok=False,
    )
    assert stages["node_reprice"]["status"] == "failed", "manifest 登记不受影响"
    with pytest.raises(AssertionError):
        assert "node_reprice" not in failed


def test_mutation_full_tradingagents_judgement_broken(tmp_path: Path) -> None:
    """变异反证 ③b：「--full 且 LLM 状态 != ok ⇒ 算失败」→ 恒假 ⇒ 原断言必须转红。"""
    mutant = _mutated_module(
        tmp_path, MAIN_SOURCE,
        [('if flags.get("full") and ta_status != "ok":', "if False:")],
        "main_full_ta_broken",
    )
    failed, _ = _record_stages(
        mutant, tmp_path, artifacts=ALL_ARTIFACTS, llm_status="absent", full=True,
    )
    with pytest.raises(AssertionError):
        assert "tradingagents" in failed


def test_mutation_record_pipeline_stages_returns_empty(tmp_path: Path) -> None:
    """变异反证 ③c：``return hard_failed`` → ``return []`` ⇒ 原断言必须转红。"""
    mutant = _mutated_module(
        tmp_path, MAIN_SOURCE, [("return hard_failed", "return []")],
        "main_returns_empty",
    )
    failed, stages = _record_stages(
        mutant, tmp_path,
        artifacts=tuple(a for a in ALL_ARTIFACTS if a != "etf_analysis_report.json"),
    )
    assert stages["analyze"]["status"] == "failed", "manifest 登记不受影响"
    with pytest.raises(AssertionError):
        assert "analyze" in failed


def test_mutation_degrade_notes_not_merged(monkeypatch, tmp_path: Path) -> None:
    """变异反证 ④：把 ``+ degrade_notes`` 去掉 ⇒ 原断言（报告里有 W 说明）必须转红。"""
    mutant = _mutated_module(
        tmp_path, MAIN_SOURCE,
        [('result["notes"] = list(result.get("notes") or []) + degrade_notes',
          'result["notes"] = list(result.get("notes") or [])')],
        "main_no_degrade_notes",
    )
    out = _run_pipeline(monkeypatch, tmp_path, main_mod=mutant, holdings=STALE_HOLDINGS)
    notes = "\n".join(out["notes"])
    with pytest.raises(AssertionError):
        assert "W（成分股权重）是只读缓存、不重爬" in notes


def test_mutation_stage_failed_merge_dropped(monkeypatch, tmp_path: Path) -> None:
    """变异反证（补）：把 ``failed_stages`` 的**合并**去掉 ⇒ 原断言必须转红。"""
    mutant = _mutated_module(
        tmp_path, MAIN_SOURCE,
        [("failed_stages = list(dict.fromkeys(list(stage_failed) + list(failed_stages)))",
          "failed_stages = list(failed_stages)")],
        "main_no_stage_failed_merge",
    )
    out = _run_pipeline(
        monkeypatch, tmp_path, main_mod=mutant,
        argv=["--asof", ASOF, "--full", "--snapshot-root", str(tmp_path / "snapshots"),
              "--report-md", str(tmp_path / "report.md")],
        stage1=FAILED_INCREMENT,
    )
    failed = list(out["stages"]["pipeline"].get("failed_stages") or [])
    with pytest.raises(AssertionError):
        assert "stage1_increment" in failed
    assert out["rc"] == 0, "变异后 --full 仍退出 0 ⇒ 这正是本轮要钉死的洞"


def test_mutation_stage_failed_append_removed(monkeypatch, tmp_path: Path) -> None:
    """变异反证（补）：把 ``stage_failed.append("stage1_increment")`` 去掉
    ⇒ 原断言必须转红。"""
    mutant = _mutated_module(
        tmp_path, MAIN_SOURCE,
        [('stage_failed.append("stage1_increment")', "pass  # 变异：故意不登记")],
        "main_no_stage_failed_append",
    )
    out = _run_pipeline(
        monkeypatch, tmp_path, main_mod=mutant,
        argv=["--asof", ASOF, "--full", "--snapshot-root", str(tmp_path / "snapshots"),
              "--report-md", str(tmp_path / "report.md")],
        stage1=FAILED_INCREMENT,
    )
    failed = list(out["stages"]["pipeline"].get("failed_stages") or [])
    assert out["stages"]["stage1_increment"]["status"] == "failed", "manifest 登记不受影响"
    with pytest.raises(AssertionError):
        assert "stage1_increment" in failed


def test_mutation_preflight_only_renamed_back_to_dry_run(monkeypatch, tmp_path: Path) -> None:
    """变异反证 ⑤：把 ``preflight_only`` 全量改回 ``dry_run`` ⇒ 原断言必须转红。"""
    mutant = _mutated_module(
        tmp_path, AR_SOURCE, [("preflight_only", "dry_run")], "ar_dry_run_back",
    )
    params = inspect.signature(mutant.run_pipeline).parameters
    with pytest.raises(AssertionError):
        assert "dry_run" not in params
    with pytest.raises(AssertionError):
        assert "preflight_only" in params

    snap = snap_mod.create_snapshot(tmp_path / "snaps", asof=ASOF)
    monkeypatch.setattr(
        mutant, "_resolve_analysis_asof",
        lambda **kw: {"notes": [], "analysis_asof": kw.get("signal_date"),
                      "scores_max_date": None, "grid_last": None, "dataset_max_date": None},
    )
    monkeypatch.setattr(
        mutant.snap_mod, "resolve_scores_base", lambda *a, **k: (None, "测试夹具：无底座")
    )
    result = mutant.run_pipeline(asof=ASOF, snapshot=snap, dry_run=True)
    with pytest.raises(AssertionError):
        assert "dry_run" not in result
    with pytest.raises(AssertionError):
        assert result.get("preflight_only") is True
