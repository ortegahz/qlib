"""🔴 V3 验收指标模块的守卫测试（**纯合成数据** ⇒ 本机可跑，不依赖 qlib / 服务器）。"""

from __future__ import annotations

import sys
from pathlib import Path

import numpy as np
import pandas as pd
import pytest

_V3_DIR = Path(__file__).resolve().parents[1]
if str(_V3_DIR) not in sys.path:
    sys.path.insert(0, str(_V3_DIR))

import config_v3 as C  # noqa: E402
import evaluate_s as EV  # noqa: E402


def _make_scores(rng: np.random.Generator, n_dates: int = 12, n_inst: int = 60):
    dates = pd.bdate_range("2020-01-01", periods=n_dates * 10, freq="C")[::10][:n_dates]
    insts = [f"SH{600000 + i:06d}" for i in range(n_inst)]
    rows = []
    for d in dates:
        vals = rng.normal(size=n_inst)
        for inst, v in zip(insts, vals):
            rows.append({"date": d, "instrument": inst, "score": v})
    return pd.DataFrame(rows), dates, insts


def test_rank_ic_series_抓出完美正相关():
    rng = np.random.default_rng(7)
    scores, dates, insts = _make_scores(rng)
    fwd = EV.scores_to_panel(scores) * 0.01 + 0.02
    ic = EV.rank_ic_series(scores, fwd, min_names=30)
    assert len(ic) == len(dates)
    assert np.allclose(ic.to_numpy(), 1.0, atol=1e-9), f"完美相关应得 IC=1，实际 {ic.to_numpy()}"


def test_rank_ic_series_负相关反证():
    rng = np.random.default_rng(7)
    scores, dates, insts = _make_scores(rng)
    fwd = EV.scores_to_panel(scores) * -0.01 + 0.02
    ic = EV.rank_ic_series(scores, fwd, min_names=30)
    assert np.allclose(ic.to_numpy(), -1.0, atol=1e-9)


def test_rank_ic_series_样本不足的日期被跳过():
    rng = np.random.default_rng(7)
    scores, dates, insts = _make_scores(rng)
    fwd = EV.scores_to_panel(scores) * 0.01
    ic = EV.rank_ic_series(scores, fwd, min_names=10_000)
    assert len(ic) == 0


def test_icir_stats_常数序列判为不显著():
    s = pd.Series([0.05] * 20)
    st = EV.icir_stats(s, rolling_window=5)
    assert st["n"] == 20
    assert st["std_ic"] == pytest.approx(0.0, abs=1e-12)
    assert not np.isfinite(st["icir"])


def test_icir_stats_零序列不炸():
    st = EV.icir_stats(pd.Series([0.0] * 5))
    assert st["n"] == 5
    assert st["win_rate"] == 0.0


def test_top_decile_excess_合成上涨行情():
    """分数 = 真收益 ⇒ Top 组合应当跑赢基准（费后仍为正）。"""
    rng = np.random.default_rng(11)
    n_days = 40
    dates = pd.bdate_range("2020-01-01", periods=n_days)
    insts = [f"SH{600000 + i:06d}" for i in range(50)]
    drift = rng.normal(0.0005, 0.01, size=len(insts))
    px = pd.DataFrame(100.0 * np.exp(np.cumsum(np.tile(drift, (n_days, 1)), axis=0)),
                      index=dates, columns=insts)
    bench = pd.Series(100.0 * np.exp(np.cumsum(np.full(n_days, 0.0001))), index=dates)
    rows = []
    for i in range(0, n_days - 10, 10):
        d = dates[i]
        for inst, p0 in zip(insts, px.loc[d]):
            rows.append({"date": d, "instrument": inst, "score": px.loc[d, inst]})
    scores = pd.DataFrame(rows)
    out = EV.top_decile_excess(scores, px, bench, universe=set(insts),
                               top_fraction=0.2, commission=0.0, slippage=0.0, min_names=20)
    assert out["n_periods"] > 0
    assert out["mean_excess_net"] > 0, out


def test_top_decile_excess_成本确实扣掉():
    rng = np.random.default_rng(3)
    scores, dates, insts = _make_scores(rng, n_dates=8, n_inst=40)
    dates_px = pd.bdate_range("2019-12-01", periods=200)
    px = pd.DataFrame(100.0, index=dates_px, columns=insts)
    bench = pd.Series(100.0, index=dates_px)
    base = EV.top_decile_excess(scores, px, bench, universe=set(insts), min_names=20)
    costly = EV.top_decile_excess(scores, px, bench, universe=set(insts), min_names=20,
                                  commission=0.001, slippage=0.002)
    assert costly["mean_excess_net"] < base["mean_excess_net"]
    assert costly["total_cost_drag"] > 0


def test_end_to_end_metrics_口径自洽():
    idx = pd.bdate_range("2020-01-01", periods=253)
    nav = pd.Series(np.linspace(1.0, 1.2, len(idx)), index=idx)
    m = EV.end_to_end_metrics(nav, None, turnover=pd.Series([0.1] * len(idx)), periods_per_year=252)
    assert m["annualized_return"] > 0
    assert m["max_drawdown"] == pytest.approx(0.0, abs=1e-12)
    assert m["total_turnover"] == pytest.approx(0.1 * len(idx), rel=1e-9)
    assert m["annualized_turnover_one_way"] == pytest.approx(m["annualized_turnover"] * 0.5)


def test_end_to_end_metrics_回撤为负():
    idx = pd.bdate_range("2020-01-01", periods=100)
    nav = pd.Series(np.concatenate([np.linspace(1.0, 1.5, 50), np.linspace(1.5, 0.9, 50)]), index=idx)
    m = EV.end_to_end_metrics(nav, None, None)
    assert m["max_drawdown"] < -0.3
