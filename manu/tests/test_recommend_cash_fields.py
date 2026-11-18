"""🔴 规则层交付件 `rebalance_recommendation.json` 必须**自证现金口径**（2026-11-11）。

事故（host 侧 `accept_20261111.py` 15 PASS / 2 FAIL 的 B1 项）：
`rebalance_recommendation.json` 里**查不到** `cash_reserve_ratio` / `equity_budget`
⇒ 验收脚本无法从**规则层自己的交付件**里核对「留 20% 现金」。

查证结论（**不是验收脚本口径过期**，而是产物真缺字段）：
  * `engine/portfolio.py` 的 `build_portfolio()` **已经算好了**
    `cash_weight` / `cash_reserve_ratio`（= `config.CASH_RESERVE_RATIO`）
    / `equity_budget`（= `1 - CASH_RESERVE_RATIO`）并放进返回的 `result`；
  * `analyze.py` 把它们写进了 `etf_analysis_report.json` 的 `recommendation` 节点；
  * 🔴 但 `recommend.py` 落盘时**只漏了这三个** ⇒ 两件交付物**自相矛盾**。

修法：把**引擎自己算出来的值**补进 `rebalance_recommendation.json`
（不是外部常量、不是反推、不是把断言改松）。

本文件钉死：
  ① 三个字段必须在 JSON 里，且数值 = 引擎口径（`config.CASH_RESERVE_RATIO` / `1 - 它`）；
  ② `equity_budget + cash_reserve_ratio == 1`（口径自洽）；
  ③ **变异负例**：把落盘里的字段改成错的 ⇒ 用例必须转红。
"""

from __future__ import annotations

import importlib.util
import json
import pathlib
import sys

import pandas as pd

_ETF_DIR = pathlib.Path(__file__).resolve().parents[1]
if str(_ETF_DIR) not in sys.path:
    sys.path.insert(0, str(_ETF_DIR))

_REC_PATH = _ETF_DIR / "recommend.py"


def _load_recommend():
    spec = importlib.util.spec_from_file_location("_rec_under_test", _REC_PATH)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


REC = _load_recommend()

import config as C  # noqa: E402


def _fake_result() -> dict:
    """一份**最小**的引擎返回（字段名与 `engine/portfolio.py` 一致）。"""
    return {
        "asof": "2026-11-09",
        "defensive_mode": False,
        "brake_mode": False,
        "pool_mode": "e_covered",
        "relaxed_absolute_momentum_filter": False,
        "n_selected": 2,
        "cash_weight": 0.2,
        "cash_reserve_ratio": float(C.CASH_RESERVE_RATIO),
        "equity_budget": round(1.0 - float(C.CASH_RESERVE_RATIO), 6),
        "score_smoothing": {"enabled": True, "method": "ewma"},
        "nav_state": None,
        "stats": {"n_universe": 100, "n_pass_liquidity": 50},
        "holdings": [
            {
                "code": "512800",
                "name": "银行ETF华宝",
                "target_weight": 0.4,
                "final_score": 1.1,
                "alpha_score": 0.4,
                "mom120": 0.12,
                "above_ma60": True,
                "vol20": 0.15,
                "coverage": 1.0,
            }
        ],
        "trades": [
            {
                "action": "买入",
                "code": "512800",
                "name": "银行ETF华宝",
                "target_weight": 0.4,
                "current_weight": 0.0,
                "delta_weight": 0.4,
                "amount": 12000.0,
            }
        ],
        "top_candidates": [],
    }


def _run_recommend(tmp_path: pathlib.Path, monkeypatch, result: dict) -> dict:
    """跑一遍 `recommend.main()`（重活全部打桩）⇒ 返回落盘的 JSON。"""
    scores_csv = tmp_path / "scores.csv"
    pd.DataFrame(
        [{"instrument": "sh600000", "date": "2026-11-09", "score": 1.0}]
    ).to_csv(scores_csv, index=False)

    monkeypatch.setattr(
        REC, "load_prices",
        lambda *a, **k: pd.DataFrame({"date": pd.to_datetime(["2026-11-09"]), "close": [1.0]}),
    )
    monkeypatch.setattr(
        REC, "load_universe",
        lambda *a, **k: pd.DataFrame({"code": ["512800"], "name": ["银行ETF华宝"]}),
    )
    monkeypatch.setattr(
        REC, "load_holdings",
        lambda *a, **k: pd.DataFrame({"etf_code": ["512800"], "weight": [0.4]}),
    )
    monkeypatch.setattr(REC, "build_portfolio", lambda *a, **k: result)

    argv = [
        "recommend.py",
        "--asof", "2026-11-09",
        "--scores", str(scores_csv),
        "--out-dir", str(tmp_path),
    ]
    monkeypatch.setattr(sys, "argv", argv)
    assert REC.main() == 0
    return json.loads((tmp_path / "rebalance_recommendation.json").read_text(encoding="utf-8"))


# ---------------------------------------------------------------- 断言（可复用 ⇒ 变异负例能真跑）

#: 数值容差
TOL = 1e-6


def _check_cash_caliber(out: dict) -> None:
    """规则层交付件的现金口径**必须**自证且与配置一致。"""
    for key in ("cash_weight", "cash_reserve_ratio", "equity_budget"):
        assert key in out, f"rebalance_recommendation.json 缺 {key} ⇒ 交付件不自证现金口径"
        assert isinstance(out[key], (int, float)), f"{key} 不是数值（实测 {type(out[key]).__name__}）"
    assert abs(float(out["cash_reserve_ratio"]) - float(C.CASH_RESERVE_RATIO)) <= TOL, (
        f"cash_reserve_ratio = {out['cash_reserve_ratio']}（期望 {C.CASH_RESERVE_RATIO}）"
    )
    assert abs(float(out["equity_budget"]) - (1.0 - float(C.CASH_RESERVE_RATIO))) <= TOL, (
        f"equity_budget = {out['equity_budget']}（期望 {1.0 - float(C.CASH_RESERVE_RATIO)}）"
    )
    assert abs(float(out["cash_weight"]) - float(C.CASH_RESERVE_RATIO)) <= TOL, (
        f"cash_weight = {out['cash_weight']}（期望 {C.CASH_RESERVE_RATIO}）"
    )
    total = float(out["equity_budget"]) + float(out["cash_reserve_ratio"])
    assert abs(total - 1.0) <= TOL, f"权益预算 + 现金预留 = {total}（应为 1.0）"


def test_recommendation_json_discloses_cash_caliber(tmp_path, monkeypatch):
    """三个现金口径字段必须在规则层交付件里，且数值 = 引擎/配置口径。"""
    out = _run_recommend(tmp_path, monkeypatch, _fake_result())
    _check_cash_caliber(out)


def test_cash_caliber_is_self_consistent(tmp_path, monkeypatch):
    """`equity_budget + cash_reserve_ratio == 1`（口径自洽 ⇒ 不可能两个都错还自洽）。"""
    out = _run_recommend(tmp_path, monkeypatch, _fake_result())
    total = float(out["equity_budget"]) + float(out["cash_reserve_ratio"])
    assert abs(total - 1.0) <= TOL, f"权益预算 + 现金预留 = {total}（应为 1.0）"


def test_mutation_wrong_cash_caliber_is_caught(tmp_path, monkeypatch):
    """变异：引擎返回的现金口径是错的 ⇒ **断言必须真的报红**（否则本测试是摆设）。"""
    bad = _fake_result()
    bad["cash_reserve_ratio"] = 0.0
    bad["equity_budget"] = 1.0
    bad["cash_weight"] = 0.0
    out = _run_recommend(tmp_path, monkeypatch, bad)
    try:
        _check_cash_caliber(out)
    except AssertionError:
        return
    raise AssertionError("变异体竟然通过了现金口径断言 ⇒ 断言没在核对数值")
