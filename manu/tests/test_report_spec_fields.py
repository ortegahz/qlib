"""🔴 2026-11-26 新增：报告规格探针 **M4（同源 α）** 与 **H2（持仓 MA60 诊断）** 的成对正/负例。

【背景：2026-11-26 全量跑批的闸门红了 2 条】
  ① **M4 —— 基线陈旧**：探针把 α **写死 0.5**（＝ V1 口径 ``config.ALPHA_SCORE_WEIGHT``）
     ⇒ V2 接缝（2026-11-18，冠军臂 ``pure_E_native``）把现役口径换成
     **α=1.0 / 动量=0.0** ⇒ 2026-11-26 跑批必红（实测 ``meta.params.alpha_weight = 1.0``）。
     ⇒ 修法：期望值改从 **V2 冻结件**取（每日链同一个加载口）⇒ **同源**；
       冻结件**读不到 ⇒ fail-closed 判 FAIL**（不确定 ≠ 可以放过）。
  ② **H2 —— 真缺陷**：持仓诊断的 ``ma60_ok`` 绑在 **exit 闸门**上 ⇒ V2 冻结件
     （``exit_ma_window = 0``）下**恒为 null** ⇒ 必红。
     ⇒ 修法（``analyze.py``）：``ma60_ok`` 改成**恒定 MA60 口径**实算的诊断值，
       ``ma20_ok`` **保留**。

【本文件钉住的事（每条都有「变异 ⇒ 必须转红」的成对负例）】
  M4-1 报告的 α / 动量 == 冻结件 ⇒ PASS；
  M4-2 **变异**：把**冻结件**里的 α 改掉 ⇒ 必须转红（证明 M4 真的在比冻结件）；
  M4-3 **变异**：把**报告**里的 α 改掉 ⇒ 必须转红；
  M4-4 冻结件**读不到** ⇒ 必须转红（fail-closed，不许真空 PASS）；
  M4-5 M4 的期望值**不许**写死 0.5，也**不许**取自 ``ALPHA_WEIGHT_GRID``（＝搜索空间）；
  M4-6 M4 / H2 在 ``run_checks`` 里**真的接上了**（不许只是没人调用的辅助函数）；
  H2-1 字段齐全 ⇒ PASS；
  H2-2 **变异**：删掉 ``ma60_ok`` ⇒ 必须转红；
  H2-3 **变异**：把 ``ma60_ok`` 置成 null ⇒ 必须转红（2026-11-26 实测的红就是这个形态）；
  A-1  ``analyze.py`` 的 ``ma60_ok`` 必须取自**恒定 MA60 map**（不许再绑回 exit 闸门），
      且 ``ma20_ok`` **不许**被删（既有消费方不许破）。

【为什么用内存夹具】落点根在服务器上（``/mnt/data``）⇒ 本机没有本轮真实产物；
  断言的**口径**与真实产物无关 ⇒ 自带夹具更稳、也更快（不依赖 ``/mnt/data``）。
"""

from __future__ import annotations

import ast
import importlib.util
import json
import sys

from pathlib import Path


def _find_repo_file(relative: str) -> Path:
    """向上搜索工程内的某个文件（**两个镜像位置** ⇒ 不许用固定的 ``parents[N]``）。"""
    for parent in Path(__file__).resolve().parents:
        candidate = parent / relative
        if candidate.is_file():
            return candidate
    raise AssertionError(f"找不到 {relative}")


def _load_probe():
    """按**路径**加载探针本体（``probes/`` 不是包）。"""
    path = _find_repo_file("manu/etf_rotation/probes/probe_report_spec.py")
    spec = importlib.util.spec_from_file_location("manu_probe_report_spec", path)
    if spec is None or spec.loader is None:
        raise AssertionError(f"无法加载探针：{path}")
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    try:
        spec.loader.exec_module(module)
    except Exception:
        sys.modules.pop(spec.name, None)
        raise
    return module, path


PRS, PROBE_PATH = _load_probe()
ANALYZE_PATH = _find_repo_file("manu/etf_rotation/analyze.py")


#: V2 冻结件的**真件形状**（2026-11-18 冠军臂 ``pure_E_native``）
FROZEN_PARAMS = {
    "smoothing_arm": "B_native_slow",
    "top_k": 5,
    "hold_buffer": 2,
    "rebalance_every_n_days": 20,
    "alpha_weight": 1.0,
    "momentum_weight": 0.0,
    "entry_ma_window": 0,
    "exit_ma_window": 0,
    "allow_theme_cap": False,
    "neg_score_gate": False,
}


def _write_frozen(tmp_path: Path, **overrides) -> Path:
    """写一份**形状同真件**的 V2 冻结件（内存夹具，不依赖 ``/mnt/data``）。"""
    params = dict(FROZEN_PARAMS)
    params.update(overrides)
    payload = {
        "params": params,
        "winner_arm": "pure_E_native",
        "frozen_at": "2026-11-18T11:05:53",
    }
    path = tmp_path / "frozen_params_wf.json"
    path.write_text(json.dumps(payload, ensure_ascii=False), encoding="utf-8")
    return path


def _holding_row(**overrides) -> dict:
    """一只持仓的诊断行（字段齐全 = H2 期望的形状）。"""
    row = {
        "code": "512800",
        "name": "银行ETF华宝",
        "shares": 7700,
        "market_value": 7091.7,
        "pnl_pct": 0.117854,
        "in_candidate_pool": True,
        "ma20_ok": True,
        "ma60_ok": True,
        "broken": False,
        "mom120": 0.17341772151898738,
    }
    row.update(overrides)
    return row


def _report(**overrides) -> dict:
    """最小报告夹具（只带 M4 / H2 用得到的字段）。"""
    report = {
        "meta": {
            "asof": "2026-11-26",
            "params": {
                "pool_mode": "e_covered",
                "alpha_weight": 1.0,
                "momentum_weight": 0.0,
                "target_n_holdings": 5,
                "hold_buffer": 2,
                "entry_ma_window": 0,
                "exit_ma_window": 0,
            },
        },
        "current_holdings_diagnosis": {
            "total_assets": 30464.29,
            "cash": 23372.59,
            "holdings": [_holding_row()],
        },
    }
    report.update(overrides)
    return report


# ------------------------------------------------------------------ M4（同源 α）
def test_m4_report_alpha_matches_frozen_params(tmp_path):
    """M4-1 正例：报告的 α / 动量 == V2 冻结件 ⇒ PASS。"""
    frozen, err = PRS.load_frozen_expectation(_write_frozen(tmp_path))
    assert frozen is not None, f"冻结件读不到：{err}"
    assert frozen["alpha_weight"] == 1.0
    assert frozen["momentum_weight"] == 0.0
    ok, detail = PRS._m4_alpha_weight_verdict(_report(), frozen)
    assert ok, detail


def test_m4_turns_red_when_frozen_alpha_is_mutated(tmp_path):
    """M4-2 变异反证：把**冻结件**里的 α 改掉 ⇒ M4 必须转红。"""
    frozen, err = PRS.load_frozen_expectation(_write_frozen(tmp_path))
    assert frozen is not None, f"冻结件读不到：{err}"
    mutated = dict(frozen)
    mutated["alpha_weight"] = 0.5
    ok, detail = PRS._m4_alpha_weight_verdict(_report(), mutated)
    assert not ok, f"变异后 M4 仍 PASS ⇒ 断言没在比冻结件：{detail}"


def test_m4_turns_red_when_report_alpha_is_mutated(tmp_path):
    """M4-3 变异反证：把**报告**里的 α 改掉 ⇒ M4 必须转红。"""
    frozen, err = PRS.load_frozen_expectation(_write_frozen(tmp_path))
    assert frozen is not None, f"冻结件读不到：{err}"
    report = _report()
    report["meta"]["params"]["alpha_weight"] = 0.5
    ok, detail = PRS._m4_alpha_weight_verdict(report, frozen)
    assert not ok, f"变异后 M4 仍 PASS ⇒ 断言写死了：{detail}"


def test_m4_turns_red_when_report_momentum_is_mutated(tmp_path):
    """M4-3' 变异反证：动量权重脱离冻结件 ⇒ M4 必须转红。"""
    frozen, err = PRS.load_frozen_expectation(_write_frozen(tmp_path))
    assert frozen is not None, f"冻结件读入口：{err}"
    report = _report()
    report["meta"]["params"]["momentum_weight"] = 0.5
    ok, detail = PRS._m4_alpha_weight_verdict(report, frozen)
    assert not ok, f"变异后 M4 仍 PASS ⇒ 断言写死了：{detail}"


def test_m4_is_fail_closed_when_frozen_params_unreadable(tmp_path):
    """M4-4 变异反证：冻结件**读不到** ⇒ M4 必须转红（不许真空 PASS）。"""
    missing = tmp_path / "不存在的冻结件.json"
    frozen, err = PRS.load_frozen_expectation(missing)
    assert frozen is None, "读不到冻结件却返回了期望值 ⇒ fail-closed 失效"
    assert err, "读不到冻结件却没有错误说明 ⇒ 不许静默"
    ok, detail = PRS._m4_alpha_weight_verdict(_report(), frozen, err)
    assert not ok, f"冻结件缺失时 M4 仍 PASS ⇒ 真空 PASS：{detail}"


def test_m4_expectation_is_not_hardcoded_and_not_from_search_grid():
    """M4-5 守卫：M4 的期望值**不许**写死 0.5，也**不许**取自搜索空间 ``ALPHA_WEIGHT_GRID``。"""
    source = PROBE_PATH.read_text(encoding="utf-8")
    tree = ast.parse(source)
    func = next(
        node for node in ast.walk(tree)
        if isinstance(node, ast.FunctionDef) and node.name == "_m4_alpha_weight_verdict"
    )
    for node in ast.walk(func):
        # 🔴 只查**可执行代码**里的数值常量（docstring 里提到 0.5 是**说明文字**，不是判据）
        if isinstance(node, ast.Constant) and isinstance(node.value, (int, float)) \
                and not isinstance(node.value, bool):
            assert node.value != 0.5, (
                "M4 的判定里出现了写死的 0.5（基线陈旧的形态）"
            )
        if isinstance(node, ast.Name):
            assert node.id != "ALPHA_WEIGHT_GRID", "M4 不许取自搜索空间 ALPHA_WEIGHT_GRID"
        if isinstance(node, ast.Attribute):
            assert node.attr != "ALPHA_WEIGHT_GRID", "M4 不许取自搜索空间 ALPHA_WEIGHT_GRID"


def test_m4_and_h2_are_wired_into_run_checks(tmp_path):
    """M4-6 守卫：M4 / H2 真的在 ``run_checks`` 里生效（不许只是没人调用的辅助函数）。"""
    frozen, err = PRS.load_frozen_expectation(_write_frozen(tmp_path))
    assert frozen is not None, f"冻结件读不到：{err}"
    results = {cid: ok for cid, _n, ok, _d in PRS.run_checks(
        _report(), ["2026-11-26", "2026-12-01"], None, frozen, err,
    )}
    assert results.get("M4") is True, "M4 没接进 run_checks（或判定不通过）"
    assert results.get("H2") is True, "H2 没接进 run_checks（或判定不通过）"
    mutated = dict(frozen)
    mutated["alpha_weight"] = 0.5
    bad = {cid: ok for cid, _n, ok, _d in PRS.run_checks(
        _report(), ["2026-11-26", "2026-12-01"], None, mutated, err,
    )}
    assert bad.get("M4") is False, "run_checks 里的 M4 变异后仍 PASS ⇒ 断言写死了"


# ------------------------------------------------------------------ H2（持仓 MA60）
def test_h2_passes_when_all_required_fields_present():
    """H2-1 正例：字段齐全 ⇒ PASS。"""
    ok, detail = PRS._h2_holdings_fields_verdict(_report())
    assert ok, detail


def test_h2_turns_red_when_ma60_ok_key_removed():
    """H2-2 变异反证：删掉 ``ma60_ok`` ⇒ H2 必须转红。"""
    report = _report()
    report["current_holdings_diagnosis"]["holdings"][0].pop("ma60_ok")
    ok, detail = PRS._h2_holdings_fields_verdict(report)
    assert not ok, f"变异后 H2 仍 PASS ⇒ 断言写死了：{detail}"


def test_h2_turns_red_when_ma60_ok_is_null():
    """H2-3 变异反证：``ma60_ok`` 置成 null ⇒ H2 必须转红（2026-11-26 实测的红就是这个形态）。"""
    report = _report()
    report["current_holdings_diagnosis"]["holdings"][0]["ma60_ok"] = None
    ok, detail = PRS._h2_holdings_fields_verdict(report)
    assert not ok, f"变异后 H2 仍 PASS ⇒ 断言只查键不查值：{detail}"


def test_h2_accepts_false_ma60_ok_as_a_real_value():
    """H2-4 守卫：``ma60_ok = False``（真·破位）是**有效值** ⇒ 不许判红。"""
    report = _report()
    report["current_holdings_diagnosis"]["holdings"][0]["ma60_ok"] = False
    report["current_holdings_diagnosis"]["holdings"][0]["broken"] = True
    ok, detail = PRS._h2_holdings_fields_verdict(report)
    assert ok, f"把 False 当成缺失 ⇒ 口径错：{detail}"


# ------------------------------------------------------------------ analyze.py 静态口径
def test_analyze_ma60_ok_is_not_bound_to_exit_gate():
    """A-1 守卫：``analyze.py`` 的 ``ma60_ok`` 取自**恒定 MA60 map**，不许绑回 exit 闸门。"""
    source = ANALYZE_PATH.read_text(encoding="utf-8")
    tree = ast.parse(source)
    func = next(
        node for node in ast.walk(tree)
        if isinstance(node, ast.FunctionDef) and node.name == "_diagnose_holdings"
    )
    assigns = [
        node for node in ast.walk(func)
        if isinstance(node, ast.Assign)
        and any(isinstance(t, ast.Name) and t.id == "ma60_ok" for t in node.targets)
    ]
    assert assigns, "analyze.py 里找不到 ma60_ok 的赋值 ⇒ 持仓诊断不产这个字段了"
    segments = [ast.get_source_segment(source, node) or "" for node in assigns]
    assert any("ma60_map" in seg for seg in segments), (
        f"ma60_ok 不再取自恒定 MA60 map（segments={segments}）⇒ 缺陷复发"
    )
    assert not any("exit_ma" in seg for seg in segments), (
        f"ma60_ok 又绑回 exit 闸门（segments={segments}）⇒ V2 冻结件下会恒为 null"
    )


def test_analyze_keeps_ma20_ok_field():
    """A-2 守卫：``ma20_ok`` **不许**被删（既有消费方不许破）。"""
    source = ANALYZE_PATH.read_text(encoding="utf-8")
    tree = ast.parse(source)
    func = next(
        node for node in ast.walk(tree)
        if isinstance(node, ast.FunctionDef) and node.name == "_diagnose_holdings"
    )
    keys = {
        key.value
        for node in ast.walk(func)
        if isinstance(node, ast.Dict)
        for key in node.keys
        if isinstance(key, ast.Constant) and isinstance(key.value, str)
    }
    assert "ma20_ok" in keys, "持仓诊断不再产出 ma20_ok（既有字段被删了）"
    assert "ma60_ok" in keys, "持仓诊断不产出 ma60_ok"
