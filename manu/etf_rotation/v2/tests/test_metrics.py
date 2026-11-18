"""🔴 V2 评估指标（`v2/metrics.py`）单测。

本文件钉死（自带夹具，**不依赖网络 / qlib / 大文件**）：

  ① 年化 / MDD / Sharpe / Calmar 用**手算**的小样本核对（口径不许漂）；
  ② RankIC **样本不足 ⇒ 一律 None**（**不许编数字**）；
  ③ ``summarize`` 的换手汇总与成本拖累（= 换手 × (佣金 + 滑点)）逐字一致；
  ④ 变异反证：把 MDD 口径改坏 ⇒ ① 的断言必须转红（证明本测试有牙）。

口径依据：V1 ``backtest.py:297-314``（年化 / 回撤 / 波动）、``analyze.py::_aggregate_ic``
（IC 汇总）。V1 是冻结 baseline，本模块只按其口径复刻，**不 import V1**。
"""

from __future__ import annotations

import pathlib
import sys

import pytest

_V2_DIR = pathlib.Path(__file__).resolve().parents[1]
if str(_V2_DIR) not in sys.path:
    sys.path.insert(0, str(_V2_DIR))

import metrics  # noqa: E402
import pandas as pd  # noqa: E402

# --------------------------------------------------------------------- 夹具
#: 4 期收益序列：+10% / +10% / −10% / +10% ⇒ 净值 1 → 1.1 → 1.21 → 1.089 → 1.1979
#: （``periods_per_year=4`` ⇒ 1 年 ⇒ 年化 = 1.1979 − 1 = 0.1979，可手算核对）
NAV = pd.Series([1.0, 1.1, 1.21, 1.089, 1.1979])
RETS = NAV.pct_change().dropna()


def _check_mdd(nav: pd.Series) -> None:
    """MDD 断言助手（正常测试与变异反证共用 ⇒ 变异时**必须转红**）。"""
    assert metrics.max_drawdown(nav) == pytest.approx(-0.1, abs=1e-12), (
        f"MDD 必须 = 1.089/1.21 − 1 = −0.1；实得 {metrics.max_drawdown(nav)!r}"
    )


# --------------------------------------------------------------------- ① 手算核对
class TestHandCalculated:
    def test_年化_手算一致(self) -> None:
        got = metrics.annualized_return(NAV, periods_per_year=4)
        assert got == pytest.approx(0.1979, abs=1e-12), (
            f"年化必须是几何口径 1.1979^(4/4) − 1 = 0.1979；实得 {got!r}"
        )

    def test_最大回撤_手算一致(self) -> None:
        _check_mdd(NAV)

    def test_Sharpe_手算一致(self) -> None:
        # mean = 0.05；std(ddof=1) = 0.1 ⇒ 0.05/0.1 × √4 = 1.0
        got = metrics.sharpe_ratio(RETS, periods_per_year=4)
        assert got == pytest.approx(1.0, abs=1e-12), (
            f"Sharpe 必须是 mean(超额)/std(ddof=1) × √ppy = 1.0；实得 {got!r}"
        )

    def test_Calmar_手算一致(self) -> None:
        got = metrics.calmar_ratio(0.1979, -0.1)
        assert got == pytest.approx(1.979, abs=1e-12), (
            f"Calmar 必须是 0.1979/|−0.1| = 1.979；实得 {got!r}"
        )

    def test_样本不足返回NaN(self) -> None:
        """净值只有 1 个点 ⇒ 年化/Sharpe/Calmar 一律 NaN（**不许编**）。"""
        one = pd.Series([1.0])
        assert metrics.annualized_return(one) != metrics.annualized_return(one)  # NaN
        assert metrics.sharpe_ratio(one.pct_change().dropna()) != metrics.sharpe_ratio(
            one.pct_change().dropna()
        )
        assert metrics.calmar_ratio(0.1, 0.0) != metrics.calmar_ratio(0.1, 0.0)


# --------------------------------------------------------------------- ② RankIC
class TestRankIc:
    def test_截面不足返回None(self) -> None:
        """只有 1 个有效截面 ⇒ ic / icir / t_stat 全 None（**不许编数字**）。"""
        out = metrics.rank_ic(["2024-01-01"] * 3, [1, 2, 3], [1, 2, 3])
        assert out["ic"] is None, f"截面不足时 ic 必须是 None；实得 {out['ic']!r}"
        assert out["icir"] is None, f"截面不足时 icir 必须是 None；实得 {out['icir']!r}"
        assert out["t_stat"] is None, f"截面不足时 t_stat 必须是 None；实得 {out['t_stat']!r}"
        assert out["n_cross_sections"] == 1

    def test_手算一致(self) -> None:
        """3 个截面：Spearman 分别为 1.0 / 0.5 / −0.5 ⇒ ic = 1/3、icir、t 逐字核对。"""
        dates = ["2024-01-01"] * 3 + ["2024-01-02"] * 3 + ["2024-01-03"] * 3
        pred = [1, 2, 3, 1, 2, 3, 1, 2, 3]
        fwd = [1, 2, 3, 1, 3, 2, 3, 1, 2]
        out = metrics.rank_ic(dates, pred, fwd)
        assert out["n_cross_sections"] == 3
        assert out["ic"] == pytest.approx(1.0 / 3.0, abs=1e-12), (
            f"ic 必须是 (1.0 + 0.5 − 0.5)/3 = 1/3；实得 {out['ic']!r}"
        )
        assert out["icir"] == pytest.approx(0.4364357804719848, abs=1e-12), (
            f"icir 必须是 mean/std(ddof=1) = 0.43643578；实得 {out['icir']!r}"
        )
        assert out["t_stat"] == pytest.approx(0.7559289460184544, abs=1e-12), (
            f"t 必须是 mean/(std/√n) = 0.75592895；实得 {out['t_stat']!r}"
        )

    def test_返回键固定(self) -> None:
        out = metrics.rank_ic(["2024-01-01"] * 2, [1, 2], [1, 2])
        assert set(out) == {"ic", "icir", "t_stat", "n_cross_sections"}


# --------------------------------------------------------------------- ③ summarize
class TestSummarize:
    def test_换手与成本拖累一致(self) -> None:
        turnover = {"2024-01-02": 0.5, "2024-01-03": 0.25}
        out = metrics.summarize(
            NAV, turnover=turnover, commission=0.00015, slippage=0.0005
        )
        assert out["total_turnover"] == pytest.approx(0.75, abs=1e-12), (
            f"总换手必须是各期换手之和 0.75；实得 {out['total_turnover']!r}"
        )
        assert out["total_cost_drag"] == pytest.approx(
            0.75 * (0.00015 + 0.0005), abs=1e-15
        ), (
            f"成本拖累必须是 换手 × (佣金 + 滑点) = 0.75 × 0.00065；实得 {out['total_cost_drag']!r}"
        )
        # 年数 = (净值点数 − 1) / 252 = 4/252
        assert out["annualized_turnover"] == pytest.approx(
            0.75 / (4.0 / 252.0), abs=1e-12
        )
        assert set(out) == {
            "annualized_return",
            "max_drawdown",
            "sharpe",
            "calmar",
            "total_turnover",
            "annualized_turnover",
            # 🔴 2026-11-18 换手口径改版：单边换手（= 0.5 × 双边）与年化成本拖累
            "annualized_turnover_one_way",
            "total_cost_drag",
            "annualized_cost_drag",
        }, f"汇总键必须固定；实得 {sorted(out)!r}"

    def test_给了基准才有超额键(self) -> None:
        flat_bench = pd.Series(1.0, index=NAV.index)
        out = metrics.summarize(NAV, benchmark_nav=flat_bench, periods_per_year=4)
        assert "excess_vs_benchmark" in out
        assert out["excess_vs_benchmark"] == pytest.approx(0.1979, abs=1e-12), (
            "基准恒为 1 ⇒ 相对超额 = 组合自身的年化 0.1979"
        )
        # 相对（几何）口径：基准恒为组合的 1/1.05 ⇒ 比值恒为 1.05 ⇒ 超额年化 = 0
        out2 = metrics.summarize(NAV, benchmark_nav=NAV / 1.05, periods_per_year=4)
        assert out2["excess_vs_benchmark"] == pytest.approx(0.0, abs=1e-12), (
            "等比基准的相对超额必须是 0（几何口径）"
        )

    def test_无换手输入时成本拖累为零(self) -> None:
        out = metrics.summarize(NAV)
        assert out["total_turnover"] == 0.0
        assert out["total_cost_drag"] == 0.0


# --------------------------------------------------------------------- ④ 变异反证
def test_变异反证_MDD口径改坏必须转红(monkeypatch: pytest.MonkeyPatch) -> None:
    """把 MDD 口径改坏（永远返回 0）⇒ ① 的 MDD 断言必须**转红**（证明本测试有牙）。"""
    monkeypatch.setattr(metrics, "max_drawdown", lambda nav: 0.0)
    with pytest.raises(AssertionError):
        _check_mdd(NAV)
