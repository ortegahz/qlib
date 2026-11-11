r"""🔴 2026-11-11（第九轮续）：**交付件两处瑕疵**的钉死测试。

① **连词被当成名称**（`llm_final.py::_name_near`）
   事故（真机快照 `20261111_161126`）：决策件风险裁决节写
   「…516620、159207、563020 **以及** 515020/516310/512820 正式一票否决…」
   ⇒ `_name_near` 的**向左回看**兜底把连词「以及」当成了 515020 的名称
   ⇒ 交付件被否名单里出现「515020 | 以及」这种垃圾名。
   ⇒ 现在连词/连接词一律不算名称 ⇒ 返回空串（渲染层显示「—」，
      再由计划层用分析报告的 code→name 表回填 ⇒ 应显示「银行ETF华夏」）。

② **跨快照复用决策件 ⇒ 假 stale**（`final_rebalance_plan.py::_llm_staleness`）
   事故：`--skip-llm` 复用**另一个快照**的决策件时，新快照的 context 与决策件
   当初吃到的 context **只差「快照路径行」** ⇒ sha1 必然不同
   ⇒ 旧口径把「路径不同」当成「内容过期」⇒ 交付件谎报
   「LLM 第二意见已过期 ⇒ 不可作数」（实测把 512800 那条**真冲突**整条标成作废）。
   ⇒ 现在**逐字比对**上游 context（归一化快照目录后）⇒ 内容一致 ⇒ 不算过期；
      **任何非路径差异仍必须判 stale**（本文件用变异反证钉死）。

本文件自带夹具，不依赖服务器产物、不联网。
"""

from __future__ import annotations

import argparse
import hashlib
import importlib.util
import json
import pathlib
import sys
import types

_ETF_DIR = pathlib.Path(__file__).resolve().parents[1]
if str(_ETF_DIR) not in sys.path:
    sys.path.insert(0, str(_ETF_DIR))

_LLF_PATH = _ETF_DIR / "llm_final.py"
_FRP_PATH = _ETF_DIR / "final_rebalance_plan.py"


def _load_llm_final():
    spec = importlib.util.spec_from_file_location("_llf_polish_under_test", _LLF_PATH)
    module = importlib.util.module_from_spec(spec)
    assert spec.loader is not None
    spec.loader.exec_module(module)
    return module


def _load_frp():
    spec = importlib.util.spec_from_file_location("_frp_polish_under_test", _FRP_PATH)
    module = importlib.util.module_from_spec(spec)
    assert spec.loader is not None
    spec.loader.exec_module(module)
    return module


def _sha1(path: pathlib.Path) -> str:
    return hashlib.sha1(path.read_bytes()).hexdigest()


# ────────────────────────── ① 连词 ≠ 名称 ──────────────────────────


def test_name_near_does_not_return_connective():
    llf = _load_llm_final()
    sentence = (
        "配套动作：515450 条件性建仓至 16%；515650 暂缓（放量 1.80／分位 94% 双阈值触发）；"
        "516620、159207、563020 以及 515020/516310/512820 正式一票否决；"
    )
    code_pos = sentence.find("515020")
    assert code_pos >= 0
    got = llf._name_near(sentence, "515020", code_pos)
    assert got == "", f"连词「以及」被当成了名称：{got!r}"


def test_rejection_row_name_is_empty_not_connective(tmp_path):
    llf = _load_llm_final()
    md = (
        "## 六、最终交易决定\n\n"
        "配套动作：515450 条件性建仓至 16%；"
        "516620、159207、563020 以及 515020/516310/512820 正式一票否决；\n"
    )
    md_path = tmp_path / "tradingagents_decision.md"
    md_path.write_text(md, encoding="utf-8")
    rows = llf.extract_authority_rejections(str(md_path))
    by_code = {str(r.get("code")): r for r in rows}
    assert "515020" in by_code, f"515020 应在否决名单里：{sorted(by_code)}"
    assert by_code["515020"].get("name") != "以及", "名称里出现了连词「以及」"
    assert not (by_code["515020"].get("name") or "").strip(), (
        f"应取不到名称（空串），实际 {by_code['515020'].get('name')!r}"
    )


def test_name_near_still_returns_real_name():
    """变异反证：修法**不许**把真名称也一起毙掉。"""
    llf = _load_llm_final()
    sentence = "剔除 512800 银行ETF华宝"
    got = llf._name_near(sentence, "512800", sentence.find("512800"))
    assert got == "银行ETF华宝", got


# ────────────────────────── ② 跨快照复用 ⇒ 假 stale ──────────────────────────


def _write_snapshot(root: pathlib.Path, snap_id: str, ctx_body: str) -> pathlib.Path:
    snap = root / snap_id
    snap.mkdir(parents=True, exist_ok=True)
    (snap / "ta_research_context.md").write_text(
        ctx_body.replace("<SNAP>", str(snap)), encoding="utf-8"
    )
    (snap / "etf_analysis_report.md").write_text("# 报告\n", encoding="utf-8")
    (snap / "etf_analysis_report.json").write_text(
        json.dumps({"meta": {"asof": "2026-11-09"}}, ensure_ascii=False), encoding="utf-8"
    )
    (snap / "ta_context_meta.json").write_text(
        json.dumps({"report_md_sha1": _sha1(snap / "etf_analysis_report.md")}, ensure_ascii=False),
        encoding="utf-8",
    )
    (snap / "tradingagents_decision.md").write_text(
        "# 六、最终交易决定\n\n512800 保留为第一大权益核心仓。\n", encoding="utf-8"
    )
    return snap


def _make_args(decision: pathlib.Path, report: pathlib.Path) -> argparse.Namespace:
    return argparse.Namespace(decision=str(decision), report=str(report))


def test_context_equivalent_only_ignores_snapshot_dir(tmp_path):
    frp = _load_frp()
    body = "上下文正文\n（完整报告：<SNAP>/etf_analysis_report.md）\n"
    a = _write_snapshot(tmp_path, "20260101_010101", body)
    b = _write_snapshot(tmp_path, "20260101_020202", body)
    assert frp._context_equivalent(a / "ta_research_context.md", b / "ta_research_context.md")
    # 变异：内容变了（分数/结论变了）⇒ 必须判不等价
    b2 = _write_snapshot(tmp_path, "20260101_030303", body.replace("上下文正文", "上下文正文（改过）"))
    assert not frp._context_equivalent(a / "ta_research_context.md", b2 / "ta_research_context.md")


def _link_decision(src_snap: pathlib.Path, dst_snap: pathlib.Path) -> pathlib.Path:
    """把**上游快照**的 context 记进**本快照**决策件的 meta（= 复用路径的真实形态）。"""
    src_ctx = src_snap / "ta_research_context.md"
    dec_json = dst_snap / "tradingagents_decision.json"
    dec_json.write_text(
        json.dumps(
            {
                "status": "ok",
                "meta": {
                    "context_sha1": _sha1(src_ctx),
                    "context_path": str(src_ctx),
                },
                "final_trade_decision": "512800 保留。",
            },
            ensure_ascii=False,
        ),
        encoding="utf-8",
    )
    return dec_json


def test_stale_false_when_context_content_identical_across_snapshots(tmp_path):
    frp = _load_frp()
    body = "上下文正文\n（完整报告：<SNAP>/etf_analysis_report.md）\n"
    src = _write_snapshot(tmp_path, "20260101_010101", body)
    dst = _write_snapshot(tmp_path, "20260101_020202", body)
    _link_decision(src, dst)
    out = frp._llm_staleness(_make_args(dst / "tradingagents_decision.md", dst / "etf_analysis_report.json"))
    assert out["llm_present"] is True
    assert out["stale"] is False, out
    assert out.get("context_source_snapshot") == "20260101_010101", out


def test_stale_true_when_context_content_changed(tmp_path):
    """变异反证：内容真变了 ⇒ **必须**仍判 stale（不许被「等价」放行）。"""
    frp = _load_frp()
    body = "上下文正文\n（完整报告：<SNAP>/etf_analysis_report.md）\n"
    src = _write_snapshot(tmp_path, "20260101_010101", body)
    dst = _write_snapshot(tmp_path, "20260101_020202", body.replace("上下文正文", "上下文正文（改过）"))
    _link_decision(src, dst)
    out = frp._llm_staleness(_make_args(dst / "tradingagents_decision.md", dst / "etf_analysis_report.json"))
    assert out["stale"] is True, out
    assert any("旧版 research_context" in r for r in out["reasons"]), out


def test_stale_true_when_source_context_unverifiable(tmp_path):
    """变异反证：上游 context 的 sha1 对不上 ⇒ 不许信「等价」⇒ 必须 stale。"""
    frp = _load_frp()
    body = "上下文正文\n（完整报告：<SNAP>/etf_analysis_report.md）\n"
    src = _write_snapshot(tmp_path, "20260101_010101", body)
    dst = _write_snapshot(tmp_path, "20260101_020202", body)
    dec_json = _link_decision(src, dst)
    payload = json.loads(dec_json.read_text(encoding="utf-8"))
    payload["meta"]["context_sha1"] = "0" * 40  # 伪造的上游 sha1
    dec_json.write_text(json.dumps(payload, ensure_ascii=False), encoding="utf-8")
    out = frp._llm_staleness(_make_args(dst / "tradingagents_decision.md", dst / "etf_analysis_report.json"))
    assert out["stale"] is True, out


def test_stale_true_when_source_context_missing(tmp_path):
    """变异反证：上游 context 文件不在了 ⇒ 验不了 ⇒ 必须 stale（不确定 ≠ 确定新鲜）。"""
    frp = _load_frp()
    body = "上下文正文\n（完整报告：<SNAP>/etf_analysis_report.md）\n"
    src = _write_snapshot(tmp_path, "20260101_010101", body)
    dst = _write_snapshot(tmp_path, "20260101_020202", body)
    _link_decision(src, dst)
    (src / "ta_research_context.md").unlink()
    out = frp._llm_staleness(_make_args(dst / "tradingagents_decision.md", dst / "etf_analysis_report.json"))
    assert out["stale"] is True, out


# ─────────────────── ③ 跨快照复用 ⇒ 上下文三件套必须跟着决策件走 ───────────────────


def _load_agents_rebalance():
    daily_dir = _ETF_DIR / "daily"
    if str(daily_dir) not in sys.path:
        sys.path.insert(0, str(daily_dir))
    spec = importlib.util.spec_from_file_location("_arb_polish_under_test", daily_dir / "agents_rebalance.py")
    module = importlib.util.module_from_spec(spec)
    assert spec.loader is not None
    spec.loader.exec_module(module)
    return module


def _report_json(path: pathlib.Path, asof: str = "2026-11-09") -> None:
    path.write_text(
        json.dumps({"meta": {"asof": asof, "generated_at": "2026-11-11T16:00:00+08:00"}},
                   ensure_ascii=False),
        encoding="utf-8",
    )


def _context_pair(snap: pathlib.Path, report_json: pathlib.Path) -> None:
    snap.mkdir(parents=True, exist_ok=True)
    (snap / "ta_research_context.md").write_text("上下文正文\n", encoding="utf-8")
    import report_fingerprint as _rf

    (snap / "ta_context_meta.json").write_text(
        json.dumps({"report_content_sha1": _rf.report_content_sha1_from_file(report_json)},
                   ensure_ascii=False),
        encoding="utf-8",
    )


def test_carry_context_trio_copies_when_contexts_equivalent(tmp_path):
    arb = _load_agents_rebalance()
    src = tmp_path / "src"
    dst = tmp_path / "dst"
    dst.mkdir()
    src.mkdir()
    body = "上下文正文\n（完整报告：<SNAP>/etf_analysis_report.md）\n"
    (src / "ta_research_context.md").write_text(body.replace("<SNAP>", str(src)), encoding="utf-8")
    (dst / "ta_research_context.md").write_text(body.replace("<SNAP>", str(dst)), encoding="utf-8")
    dec = _decision_json(src, _sha1(src / "ta_research_context.md"))
    ok, why = arb._carry_context_trio(src, dst, dst / "ta_research_context.md", dec)
    assert ok is True, why
    assert (dst / "ta_research_context.md").is_file()
    assert (dst / "ta_research_context.md").read_bytes() == (src / "ta_research_context.md").read_bytes()


def test_carry_context_trio_refuses_when_data_changed(tmp_path):
    """变异反证：来源上下文与本轮现算的**不等价** ⇒ 不许搬（链保持断口 ⇒ 闸门会红）。"""
    arb = _load_agents_rebalance()
    src = tmp_path / "src"
    dst = tmp_path / "dst"
    dst.mkdir()
    src.mkdir()
    (src / "ta_research_context.md").write_text("上下文正文（旧数据）\n", encoding="utf-8")
    (dst / "ta_research_context.md").write_text("上下文正文（新数据）\n", encoding="utf-8")
    dec = _decision_json(src, _sha1(src / "ta_research_context.md"))
    ok, why = arb._carry_context_trio(src, dst, dst / "ta_research_context.md", dec)
    assert ok is False
    assert "不等价" in why, why
    # 本快照现算的上下文**不许被来源的覆盖**
    assert (dst / "ta_research_context.md").read_text(encoding="utf-8") == "上下文正文（新数据）\n"


def test_carry_context_trio_refuses_when_source_missing(tmp_path):
    arb = _load_agents_rebalance()
    src = tmp_path / "src"
    dst = tmp_path / "dst"
    dst.mkdir()
    ok, why = arb._carry_context_trio(src, dst, dst / "ta_research_context.md")
    assert ok is False
    assert "缺 ta_research_context.md" in why, why


def test_daily_chain_calls_carry_context_trio_in_reuse_branch():
    """静态断言：复用分支**必须**调用 `_carry_context_trio`（不许被后续重构悄悄删掉）。"""
    import ast

    src = (_ETF_DIR / "daily" / "agents_rebalance.py").read_text(encoding="utf-8")
    tree = ast.parse(src)
    called = False
    for node in ast.walk(tree):
        if isinstance(node, ast.Call):
            fn = node.func
            name = getattr(fn, "attr", None) or getattr(fn, "id", None)
            if name == "_carry_context_trio":
                called = True
    assert called, "daily 链的复用分支不再调用 _carry_context_trio ⇒ sha1 链会断"


def _decision_json(snap: pathlib.Path, context_sha1: str) -> pathlib.Path:
    p = snap / "tradingagents_decision.json"
    p.write_text(
        json.dumps({"status": "ok", "meta": {"context_sha1": context_sha1}}, ensure_ascii=False),
        encoding="utf-8",
    )
    return p


def test_carry_context_trio_refuses_when_decision_context_mismatch(tmp_path):
    """变异反证：来源 context 与来源决策件**对不上** ⇒ 不许搬（它自己就是断链的）。"""
    arb = _load_agents_rebalance()
    src = tmp_path / "src"
    dst = tmp_path / "dst"
    dst.mkdir()
    report = tmp_path / "etf_analysis_report.json"
    _report_json(report)
    _context_pair(src, report)
    dec = _decision_json(src, "0" * 40)
    ok, why = arb._carry_context_trio(src, dst, dst / "ta_research_context.md", dec)
    assert ok is False
    assert "对不上" in why, why
    assert not (dst / "ta_research_context.md").exists()


def test_carry_context_trio_accepts_self_consistent_source(tmp_path):
    arb = _load_agents_rebalance()
    src = tmp_path / "src"
    dst = tmp_path / "dst"
    dst.mkdir()
    report = tmp_path / "etf_analysis_report.json"
    _report_json(report)
    _context_pair(src, report)
    # 🔴 2026-11-11（审计 P1-2 · fail-closed）：本轮快照**必须**有 ta_research_context.md
    #    （生产链里 build_ta_context 先跑 ⇒ 必然存在）⇒ 本轮 context 缺失 ⇒ **不搬**
    #    （拿不到「本轮数据没变」的证明 ⇒ fail-closed）。
    #    ⇒ 这里补上本轮 context，让本用例测「来源自洽 **且** 本轮 context 等价 ⇒ 接受」，
    #    「本轮 context 缺失 ⇒ 不搬」由 tests/test_round3_context_guards.py 钉死。
    (dst / "ta_research_context.md").write_text("上下文正文\n", encoding="utf-8")
    dec = _decision_json(src, _sha1(src / "ta_research_context.md"))
    ok, why = arb._carry_context_trio(src, dst, dst / "ta_research_context.md", dec)
    assert ok is True, why
    assert (dst / "ta_research_context.md").is_file()


def test_snapshots_with_file_lists_newest_first(tmp_path):
    from daily import snapshot as _snap

    for name in ("20260101_010101", "20260101_020202", "20260101_030303"):
        d = tmp_path / name
        d.mkdir()
    (tmp_path / "20260101_020202" / "tradingagents_decision.json").write_text("{}", encoding="utf-8")
    (tmp_path / "20260101_030303" / "tradingagents_decision.json").write_text("{}", encoding="utf-8")
    got = [p.name for p in _snap.snapshots_with_file(tmp_path, "tradingagents_decision.json")]
    assert got == ["20260101_030303", "20260101_020202"], got
    latest = _snap.latest_snapshot_with_file(tmp_path, "tradingagents_decision.json")
    assert latest is not None and latest.name == "20260101_030303"
