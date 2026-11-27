"""live_eval_attribution（事后系统有效性评估）端到端自测。

【为什么需要】
`manu/tools/live_eval_attribution.py` 是「四个评估维度」的**唯一**执行器。
只有真实快照 + 真实未来行情才能验它，而真实未来行情**现在还不存在**（asof 之后
还没跑过）⇒ 用**合成面板**把四个维度**逐个跑通**，保证脚本不是"永远报缺"的空壳。

🔴 本测试**只用合成数据** ⇒ 任何结论都**不代表真实策略表现**。

【镜像副本】
本文件在 ``manu/etf_rotation/tests/`` 与 ``manu/tests/`` **逐字一致**（镜像纪律）。
"""

from __future__ import annotations

import csv
import importlib.util
import json
import sys
from pathlib import Path

import pandas as pd
import pytest

def _find_repo_root() -> Path:
    """按**标记文件**找仓库根（不靠相对层数 ⇒ 镜像副本里也成立）。"""
    here = Path(__file__).resolve()
    for parent in here.parents:
        if (parent / "manu" / "tools" / "live_eval_attribution.py").is_file():
            return parent
    return here.parents[2]


REPO_ROOT = _find_repo_root()
TOOL_PATH = REPO_ROOT / "manu" / "tools" / "live_eval_attribution.py"

_spec = importlib.util.spec_from_file_location("live_eval_attr_under_test", TOOL_PATH)
LEA = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(LEA)


#: 9 只合成 ETF：前瞻收益（horizon=5）**按 rank 递减** ⇒ 三档单调性必然成立
#:    rank1..3 = 高档（+20%/+18%/+16%）；rank4..6 = 中档（+8%/+5%/+2%）；
#:    rank7..9 = 低档（-4%/-8%/-12%）
_RANK_RETURNS = [0.20, 0.18, 0.16, 0.08, 0.05, 0.02, -0.04, -0.08, -0.12]
#: 🔴 用**6 位数字代码**（真实 ETF 代码形态）—— 行情面板读进来会 ``zfill(6)``
_CODES = [f"6000{i:02d}" for i in range(9)]
#: 规则层选中 = rank 1、2；TA 否决 = rank 9（低分组 ⇒ TA 若拦对了就是立功）
_SELECTED = _CODES[:2]
_TA_REJECTED = _CODES[8]


def _make_prices(path: Path, *, n_days: int = 30, asof_index: int = 5,
                 horizon: int = 5) -> None:
    """造一份**合成**行情面板（asof 之后有足够窗口 ⇒ 前瞻收益可算）。"""
    dates = pd.bdate_range("2026-01-05", periods=n_days)
    rows = []
    for code, ret in zip(_CODES, _RANK_RETURNS):
        base = 1.0 + 0.01 * _CODES.index(code)
        for t, day in enumerate(dates):
            price = base
            if t >= asof_index:
                price = base * (1.0 + ret * (t - asof_index) / horizon)
            rows.append({"date": day.strftime("%Y-%m-%d"), "code": code,
                         "open": price, "high": price, "low": price,
                         "close": price, "volume": 1.0, "amount": 1.0})
    pd.DataFrame(rows).to_csv(path, index=False)


def _make_snapshot(root: Path, *, asof: str, with_fill: bool = True) -> None:
    """造一份**合成**快照（含 eval pack 三件）。"""
    root.mkdir(parents=True, exist_ok=True)
    n = len(_CODES)
    high_cut = max(1, round(n * 0.32))
    mid_cut = max(high_cut, round(n * 0.68))
    pool_rows = []
    for i, code in enumerate(_CODES):
        rank = i + 1
        tercile = "高" if rank <= high_cut else ("中" if rank <= mid_cut else "低")
        pool_rows.append({
            "date": asof, "code": code, "name": f"ETF{code}",
            "e_score": 1.0 - i * 0.1, "rank": rank, "final_score": 1.0 - i * 0.1,
            "selected": code in _SELECTED, "not_selected_reason": "",
            "coverage": 1.0, "coverage_ok": "True", "liquidity_ok": "True",
            "tradable": "True", "above_ma60": "True", "entry_ma_ok": "True",
            "exit_ma_ok": "True", "broken": "False", "mom20": 0.01, "mom60": 0.02,
            "mom120": 0.03, "momentum_score": 0.0, "n_constituents": 10,
            "n_constituents_scored": 10, "theme_id": "T", "is_defensive": "False",
            "tercile": tercile, "is_held_after": code in _SELECTED,
        })
    with (root / "eval_candidate_pool.csv").open("w", encoding="utf-8-sig", newline="") as fh:
        writer = csv.DictWriter(fh, fieldnames=list(pool_rows[0]))
        writer.writeheader()
        writer.writerows(pool_rows)

    ledger_rows = []
    for i, code in enumerate(_CODES):
        verdict = "否决" if code == _TA_REJECTED else "放行买入"
        ledger_rows.append({
            "code": code, "name": f"ETF{code}", "rule_rank": i + 1,
            "rule_score": 1.0 - i * 0.1, "rule_e_score": 1.0 - i * 0.1,
            "rule_selected": code in _SELECTED, "in_candidate_pool": True,
            "ta_verdict": verdict, "ta_verdict_source": "llm_final_decision.targets",
            "ta_confidence": "high", "ta_target_pct": 20.0, "ta_reason": "合成",
            "ta_final_parsed": True, "in_ta_screening_rejected": code == _TA_REJECTED,
            "screening_agrees": True, "disagreement_note": "",
            "divergent_views": "", "final_action": "买入" if code in _SELECTED else "",
            "final_weight": 0.2 if code in _SELECTED else "",
            "final_shares": 100 if code in _SELECTED else "",
            "promoted_as": "", "held_after": code in _SELECTED,
        })
    with (root / "eval_ta_shadow_ledger.csv").open("w", encoding="utf-8-sig", newline="") as fh:
        writer = csv.DictWriter(fh, fieldnames=list(ledger_rows[0]))
        writer.writeheader()
        writer.writerows(ledger_rows)

    fills_rows = [{
        "signal_date": asof, "execution_date": asof, "code": _CODES[0],
        "name": f"ETF{_CODES[0]}", "side": "买入", "planned_shares": 100,
        "planned_price": 1.0, "planned_amount": 100.0, "planned_fee": 5.0,
        "planned_slippage": 0.01,
        "fill_status": "已成交",
        "actual_shares": 100 if with_fill else "",
        "actual_price": 1.002 if with_fill else "",
        "actual_amount": 100.2 if with_fill else "",
        "actual_commission": 5.0 if with_fill else "",
        "actual_transfer_fee": 0.0 if with_fill else "",
        "note": "合成",
    }]
    with (root / "live_fills.csv").open("w", encoding="utf-8-sig", newline="") as fh:
        writer = csv.DictWriter(fh, fieldnames=list(fills_rows[0]))
        writer.writeheader()
        writer.writerows(fills_rows)

    (root / "cost_assumption.json").write_text(json.dumps({
        "layers": {"execution_card": {"min_commission": 5.0}},
        "disagreements": ["🔴 合成：执行卡 5.00 vs 节点件 2.26"],
        "per_order": [],
    }, ensure_ascii=False), encoding="utf-8")


# ----------------------------------------------------------------- 四维度
def test_four_dimensions_end_to_end(tmp_path: Path) -> None:
    """🔴 合成面板下四个维度**全部算得出来**（不是空壳）。"""
    snap = tmp_path / "snap"
    prices = tmp_path / "prices.csv"
    _make_prices(prices)
    # asof 必须落在行情面板里（合成面板第 6 个交易日）
    asof = pd.bdate_range("2026-01-05", periods=30)[5].strftime("%Y-%m-%d")
    _make_snapshot(snap, asof=asof)

    out_dir = tmp_path / "out"
    rc = LEA.main([
        "--snapshot", str(snap), "--prices", str(prices),
        "--horizon", "5", "--n-sims", "500", "--out-dir", str(out_dir),
    ])
    assert rc == 0
    report = (out_dir / "live_eval_report.md").read_text(encoding="utf-8")
    payload = json.loads((out_dir / "live_eval_report.json").read_text(encoding="utf-8"))
    avail = payload["results"][0]["available"]

    # ① 等权池基准：选中(+19%) 跑赢 池(+5%)
    base = avail["baseline"]
    assert base["available"] is True
    assert base["pool_equal_weight_fwd"] == pytest.approx(0.05, abs=1e-6)
    assert base["selected_equal_weight_fwd"] == pytest.approx(0.19, abs=1e-6)
    assert base["excess_selected_minus_pool"] > 0

    # ① 蒙特卡洛：实际组合应排进前 10%
    mc = avail["monte_carlo"]
    assert mc["available"] is True
    assert mc["actual_percentile"] is not None
    assert mc["passes_top10"] is True

    # ② 三档单调性：高 > 中 > 低
    mono = avail["monotonicity"]
    assert mono["monotone_high_mid_low"] is True
    assert mono["mean_fwd_return"]["高"] > mono["mean_fwd_return"]["中"] > mono["mean_fwd_return"]["低"]
    assert "噪声地板" in report

    # ③ TA 消融：留下组 > 否决组 ⇒ TA 立功
    ta = avail["ta_ablation"]
    assert ta["available"] is True
    assert ta["ta_helped"] is True
    assert ta["rejected_mean_fwd"] < ta["kept_mean_fwd"]

    # ④ 实盘实测滑点：成交价 1.002 vs 参考价 1.0 ⇒ +20 bp
    fric = avail["friction"]
    assert fric["measured_available"] is True
    assert fric["measured_fills"][0]["slippage_bp"] == pytest.approx(20.0, abs=1e-6)

    assert "① 等权池基准" in report
    assert "③ TA 消融" in report


def test_missing_forward_window_reports_gap(tmp_path: Path) -> None:
    """🔴 行情面板没覆盖 asof 之后 ⇒ **如实报缺**，不许编数。"""
    snap = tmp_path / "snap"
    prices = tmp_path / "prices.csv"
    _make_prices(prices)
    asof = pd.bdate_range("2026-01-05", periods=30)[5].strftime("%Y-%m-%d")
    _make_snapshot(snap, asof=asof)

    # 只给 asof 当天一根 bar ⇒ 前瞻收益全 NaN
    tiny = tmp_path / "tiny.csv"
    pd.DataFrame([{"date": asof, "code": c, "close": 1.0} for c in _CODES]).to_csv(
        tiny, index=False)

    out_dir = tmp_path / "out2"
    rc = LEA.main([
        "--snapshot", str(snap), "--prices", str(tiny), "--horizon", "5",
        "--out-dir", str(out_dir),
    ])
    assert rc == 0
    report = (out_dir / "live_eval_report.md").read_text(encoding="utf-8")
    assert "算不出来" in report
    assert "🔴" in report


def test_missing_eval_pack_is_skipped(tmp_path: Path) -> None:
    """缺 eval pack 的快照 ⇒ 不评估、**不崩**。"""
    snap = tmp_path / "bare"
    snap.mkdir()
    prices = tmp_path / "prices.csv"
    _make_prices(prices)
    out_dir = tmp_path / "out3"
    rc = LEA.main([
        "--snapshot", str(snap), "--prices", str(prices), "--out-dir", str(out_dir),
    ])
    # 🔴 "没有可评估的期数" 是**合法结论**（数据不足），不是输入错误 ⇒ 退出码 0
    assert rc == 0
    report = (out_dir / "live_eval_report.md").read_text(encoding="utf-8")
    assert "没有可评估的期数" in report
    assert "缺 eval_candidate_pool.csv" in report
