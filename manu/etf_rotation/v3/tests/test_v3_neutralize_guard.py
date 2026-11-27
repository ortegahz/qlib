"""🔴 V3 截面中性化（对齐③）的回归测试。

【本文件钉的判定】
  ① **残差与控制变量截面正交**（逐日、逐控制变量，数值断言 corr ≈ 0）；
  ② **样本不足的日期被跳过并计数**（残差保留原值，不许悄悄截断样本）；
  ③ 🔴 **行业中性化守卫**：常量为 False ⇒ 通过；**monkeypatch 改成 True ⇒ 必须 raise**
     （变异反证：这是「绝不行业中性化」的机器化 teeth）；
  ④ **全 NaN / 常数列自动丢弃**（秩 0 列会让最小二乘不稳）；
  ⑤ **总开关关掉 ⇒ 原样返回**；
  ⑥ 🔴 **批量面板路径 ≡ 逐特征路径**（``cross_sectional_residualize_panel``
     对拍 ``cross_sectional_residualize``）：掩码相同 ⇒ 值/NaN 位置/stats 全等；
     掩码不同 ⇒ 值/NaN 位置全等 + stats 与「事实源反推」的参考值相等。

【口径提醒】本测试只覆盖**纯 pandas 数值**，不 import qlib、不联网、不读 /mnt/data。
"""

from __future__ import annotations

import inspect
import sys
import time
from pathlib import Path

import numpy as np
import pandas as pd
import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import neutralize  # noqa: E402


def _synthetic(n_dates: int = 3, n_inst: int = 60, seed: int = 7):
    """合成「规模档 + 流动性 + 标签」三件套（🔴 全部本地生成，不碰服务器数据）。"""
    rng = np.random.default_rng(seed)
    dates = pd.bdate_range("2020-01-02", periods=n_dates)
    instruments = [f"SZ{600000 + i:06d}" for i in range(n_inst)]
    tier_values = np.array([[i % 4 for i in range(n_inst)] for _ in range(n_dates)])
    tier_panel = pd.DataFrame(tier_values, index=dates, columns=instruments)
    liquidity = pd.DataFrame(
        rng.normal(size=(n_dates, n_inst)), index=dates, columns=instruments
    )
    values = pd.DataFrame(
        1.5 * (tier_values == 0) - 0.8 * (tier_values == 1) + 2.0 * liquidity.to_numpy()
        + rng.normal(scale=0.3, size=(n_dates, n_inst)),
        index=dates,
        columns=instruments,
    )
    return tier_panel, liquidity, values


def test_残差与控制变量截面正交() -> None:
    """🔴 逐日 OLS 残差化 ⇒ 残差与每个控制变量**截面正交**。"""
    tier_panel, liquidity, values = _synthetic()
    controls = neutralize.build_controls(tier_panel, liquidity)
    residuals, stats = neutralize.cross_sectional_residualize(values, controls, min_size=50)
    assert stats["n_dates_processed"] == 3
    assert stats["n_dates_skipped"] == 0
    assert stats["n_controls_used"] == 4  # 3 个规模档哑变量 + 1 个流动性
    for date in values.index:
        block = controls.xs(date, level="date").reindex(values.columns)
        residual_row = residuals.loc[date].to_numpy(dtype="float64")
        # 截距 ⇒ 残差截面均值 ≈ 0
        assert abs(float(np.nanmean(residual_row))) < 1e-8
        for column in block.columns:
            control_row = block[column].to_numpy(dtype="float64")
            corr = float(np.corrcoef(control_row, residual_row)[0, 1])
            assert abs(corr) < 1e-8, (date, column, corr)


def test_样本不足的日期被跳过并计数() -> None:
    """🔴 有效样本 < min_size ⇒ 该日跳过（残差保留原值）并计数。"""
    tier_panel, liquidity, values = _synthetic()
    controls = neutralize.build_controls(tier_panel, liquidity)
    broken = values.copy()
    broken.iloc[1, 5:] = np.nan
    residuals, stats = neutralize.cross_sectional_residualize(broken, controls, min_size=50)
    assert stats["n_dates_skipped"] == 1
    assert stats["n_dates_processed"] == 2
    skipped_date = broken.index[1]
    pd.testing.assert_series_equal(
        residuals.loc[skipped_date], broken.loc[skipped_date], check_names=False
    )


def test_全NaN控制列被丢弃() -> None:
    """🔴 秩 0 列（全 NaN）必须丢弃，而不是让最小二乘不稳 / 整天跳过。"""
    tier_panel, liquidity, values = _synthetic()
    controls = neutralize.build_controls(tier_panel, liquidity)
    controls["dead_column"] = np.nan
    residuals, stats = neutralize.cross_sectional_residualize(values, controls, min_size=50)
    assert stats["n_dates_processed"] == 3
    assert stats["n_controls_dropped"] == 1
    assert stats["n_controls_used"] == 4
    assert residuals.notna().to_numpy().all()


def test_行业中性化守卫() -> None:
    """🔴 常量 False ⇒ 通过；monkeypatch 成 True ⇒ **必须 raise**（变异反证）。"""
    import config_v3

    assert config_v3.INDUSTRY_NEUTRALIZATION_ENABLED is False
    neutralize.assert_no_industry_neutralization()
    neutralize.C.INDUSTRY_NEUTRALIZATION_ENABLED = True
    try:
        with pytest.raises(AssertionError):
            neutralize.assert_no_industry_neutralization()
    finally:
        neutralize.C.INDUSTRY_NEUTRALIZATION_ENABLED = False


def test_总开关关掉原样返回() -> None:
    """🔴 ``enabled=False`` ⇒ 值原样返回，统计标 ``enabled=False``。"""
    tier_panel, liquidity, values = _synthetic()
    controls = neutralize.build_controls(tier_panel, liquidity)
    residuals, stats = neutralize.neutralize_frame(values, controls, enabled=False)
    pd.testing.assert_frame_equal(residuals, values)
    assert stats["enabled"] is False
    assert stats["n_dates_processed"] == 0
    residuals_on, stats_on = neutralize.neutralize_frame(values, controls, enabled=True)
    assert stats_on["enabled"] is True
    assert stats_on["n_dates_processed"] == 3
    assert not np.allclose(residuals_on.to_numpy(), values.to_numpy())


def test_流动性控制是N日均值的对数() -> None:
    """🔴 流动性控制 = log(过去 N 日成交额均值)；头部不足 N 日 ⇒ NaN。"""
    turnover = pd.DataFrame(
        [[1.0, 10.0], [2.0, 20.0], [3.0, 30.0], [4.0, 40.0]],
        index=pd.bdate_range("2020-01-02", periods=4),
        columns=["A", "B"],
    )
    control = neutralize.build_liquidity_control(turnover, lookback_days=2)
    # 🔴 窗口必须**凑满 2 日** ⇒ 第 0 行缺样本 ⇒ NaN
    assert control.iloc[0].isna().all()
    assert float(control.iloc[1]["A"]) == pytest.approx(np.log(1.5))
    assert float(control.iloc[1]["B"]) == pytest.approx(np.log(15.0))
    assert float(control.iloc[2]["A"]) == pytest.approx(np.log(2.5))
    assert float(control.iloc[3]["B"]) == pytest.approx(np.log(35.0))


# ----------------------------------------------------------------- 批量路径对拍
def _synthetic_panel(
    n_dates: int = 6,
    n_inst: int = 60,
    n_features: int = 5,
    seed: int = 11,
    same_mask: bool = True,
):
    """合成控制变量与一个 ``(日期 × 标的 × 特征)`` 值面板（🔴 全部本地生成）。

    * ``same_mask=True``  ⇒ 各特征的 NaN 掩码**完全相同**（真实面板的常态）；
    * ``same_mask=False`` ⇒ 掩码各不相同（逐特征挖洞 / 某日整列 NaN / 整列全 NaN）；
    * 面板里**故意**埋了四种边界：某日有效样本 < min_size、某日**完全没有控制变量**、
      常数控制列、全 NaN 控制列 ⇒ 把所有口径分支都逼出来。
    """
    rng = np.random.default_rng(seed)
    dates = pd.bdate_range("2021-03-01", periods=n_dates)
    instruments = [f"SZ{600000 + i:06d}" for i in range(n_inst)]
    tier_values = np.array([[i % 4 for i in range(n_inst)] for _ in range(n_dates)])
    tier_panel = pd.DataFrame(tier_values, index=dates, columns=instruments)
    liquidity = pd.DataFrame(
        rng.normal(size=(n_dates, n_inst)), index=dates, columns=instruments
    )
    values = rng.normal(size=(n_dates, n_inst, n_features))
    values[rng.random((n_dates, n_inst)) < 0.1, :] = np.nan  # 各特征共享的缺格
    if not same_mask:
        values[0, :5, 1] = np.nan      # 只在特征 1 上挖洞（⇒ 掩码不同）
        values[2, :, 2] = np.nan        # 特征 2 在某日整列 NaN
        values[:, :, 3] = np.nan        # 特征 3 整列 NaN（任何日期都无有效样本）
    controls = neutralize.build_controls(tier_panel, liquidity)
    controls["dead_column"] = np.nan   # 🔴 全 NaN 控制列 ⇒ 必须丢弃
    controls["const_column"] = 1.0     # 🔴 常数控制列 ⇒ 必须丢弃（与截距共线）
    values[-2, n_inst // 2 :, :] = np.nan       # 倒数第二日：有效样本 < min_size
    controls = controls.drop(index=dates[-1], level="date")  # 最后一日：完全没有控制变量
    return values, dates, instruments, controls


def _old_path(values_3d, dates, instruments, controls, min_size):
    """🔴 **事实源路径**：逐特征调用 ``cross_sectional_residualize``（旧函数不许改）。"""
    residuals: list[np.ndarray] = []
    stats_list: list[dict] = []
    for feature in range(values_3d.shape[2]):
        panel = pd.DataFrame(values_3d[:, :, feature], index=dates, columns=instruments)
        residual, stats = neutralize.cross_sectional_residualize(
            panel, controls, min_size=min_size
        )
        residuals.append(residual.to_numpy(dtype=float))
        stats_list.append(stats)
    return np.stack(residuals, axis=2), stats_list


def _panel_reference_stats(values_3d, dates, instruments, controls, min_size, old_stats):
    """用**事实源**（旧函数）反推面板口径的参考统计（🔴 不看新实现的任何内部量）。

    探针法：单日面板 + ``min_size=1`` ⇒ 精确拿到该（日期, 特征）的有效样本数与有效行掩码
    （单日面板的 keep 列集与整面板一致 ⇒ 口径不变）；再用**并集**口径汇总。
    """
    n_dates, n_inst, n_features = values_3d.shape
    n_valid = np.zeros((n_dates, n_features), dtype=int)
    masks = np.zeros((n_dates, n_inst, n_features), dtype=bool)
    keep_counts: dict[int, int] = {}
    for day in range(n_dates):
        for feature in range(n_features):
            panel = pd.DataFrame(values_3d[:, :, feature], index=dates, columns=instruments)
            residual, stats = neutralize.cross_sectional_residualize(
                panel.iloc[[day]], controls, min_size=1
            )
            if stats["n_dates_processed"] == 1:
                n_valid[day, feature] = int(stats["n_samples_total"])
                masks[day, :, feature] = ~np.isnan(residual.to_numpy(dtype=float)[0])
                keep_counts.setdefault(day, int(stats["n_controls_used"]))
    processed = n_valid >= int(min_size)
    processed_days = [day for day in range(n_dates) if bool(processed[day].any())]
    union_total = 0
    for day in processed_days:
        union_total += int(masks[day][:, processed[day]].any(axis=1).sum())
    n_controls_total = int(old_stats["n_controls_total"])
    used = [keep_counts[day] for day in processed_days]
    dropped = [n_controls_total - keep_counts[day] for day in processed_days]
    return {
        "enabled": True,
        "n_dates_processed": len(processed_days),
        "n_dates_skipped": n_dates - len(processed_days),
        "n_dates_no_controls": int(old_stats["n_dates_no_controls"]),
        "n_controls_used": neutralize._median_int(used),
        "n_controls_dropped": neutralize._median_int(dropped),
        "n_controls_total": n_controls_total,
        "n_samples_total": union_total,
        "min_cross_section_size": int(min_size),
    }


def test_批量路径与逐特征路径等价_同掩码() -> None:
    """🔴 各特征 NaN 掩码相同 ⇒ 批量路径与事实源**逐元素等价**（值 + NaN 位置 + stats）。"""
    values, dates, instruments, controls = _synthetic_panel(same_mask=True)
    min_size = 50
    old_residuals, old_stats = _old_path(values, dates, instruments, controls, min_size)
    new_residuals, new_stats = neutralize.cross_sectional_residualize_panel(
        values, dates, instruments, controls, min_size=min_size
    )
    assert new_residuals.shape == values.shape
    # 🔴 NaN 位置必须**完全一致**（含「跳过 ⇒ 保留原值」的那些格）
    assert np.array_equal(np.isnan(old_residuals), np.isnan(new_residuals))
    finite = np.isfinite(old_residuals)
    assert finite.any()
    assert np.allclose(old_residuals[finite], new_residuals[finite], rtol=1e-8, atol=1e-12)
    # 🔴 掩码相同 ⇒ 面板统计与事实源统计**逐键相等**
    assert new_stats == old_stats[0]
    assert old_stats[0]["n_dates_no_controls"] == 1  # 最后一日没有控制变量


def test_批量路径与逐特征路径等价_异掩码() -> None:
    """🔴 掩码各不相同（整列 NaN / 逐特征挖洞 / 样本不足 / 无控制变量 / 常数列）。"""
    values, dates, instruments, controls = _synthetic_panel(same_mask=False)
    min_size = 50
    old_residuals, old_stats = _old_path(values, dates, instruments, controls, min_size)
    new_residuals, new_stats = neutralize.cross_sectional_residualize_panel(
        values, dates, instruments, controls, min_size=min_size
    )
    # ---- 逐元素等价 ----
    assert np.array_equal(np.isnan(old_residuals), np.isnan(new_residuals))
    finite = np.isfinite(old_residuals)
    assert finite.any()
    assert np.allclose(old_residuals[finite], new_residuals[finite], rtol=1e-8, atol=1e-12)
    # ---- 「跳过 ⇒ 保留原值」的三种情形：新旧路径都必须原样保留 ----
    assert np.array_equal(new_residuals[-1], values[-1], equal_nan=True)  # 无控制变量
    assert np.array_equal(new_residuals[-2], values[-2], equal_nan=True)  # 有效样本不足
    assert np.array_equal(new_residuals[:, :, 3], values[:, :, 3], equal_nan=True)  # 整列 NaN
    assert np.array_equal(old_residuals[:, :, 3], values[:, :, 3], equal_nan=True)
    # ---- stats：与「事实源反推」的参考值相等 ----
    reference = _panel_reference_stats(
        values, dates, instruments, controls, min_size, old_stats[0]
    )
    assert new_stats == reference
    # 🔴 各特征 stats 确实不同（否则这个用例就白测了）
    assert len({stats["n_samples_total"] for stats in old_stats}) > 1


def test_旧函数仍是事实源() -> None:
    """🔴 旧函数不许删/不许改语义：签名、返回类型与新函数并存。"""
    assert hasattr(neutralize, "cross_sectional_residualize")
    assert hasattr(neutralize, "cross_sectional_residualize_panel")
    parameters = list(inspect.signature(neutralize.cross_sectional_residualize).parameters)
    assert parameters == ["values", "controls", "min_size"]
    values, dates, instruments, controls = _synthetic_panel()
    panel = pd.DataFrame(values[:, :, 0], index=dates, columns=instruments)
    residual, stats = neutralize.cross_sectional_residualize(panel, controls, min_size=50)
    assert isinstance(residual, pd.DataFrame)
    assert list(residual.columns) == list(instruments)
    assert stats["n_controls_total"] == 6  # 4 个正常控制 + 1 全 NaN + 1 常数


def test_批量路径显著快于逐特征路径() -> None:
    """🔴 性能守卫：口径等价的前提下，批量路径必须显著快于逐特征路径。"""
    values, dates, instruments, controls = _synthetic_panel(
        n_dates=20, n_inst=200, n_features=40, seed=5
    )
    min_size = 50
    start = time.perf_counter()
    _old_path(values, dates, instruments, controls, min_size)
    old_seconds = time.perf_counter() - start
    start = time.perf_counter()
    neutralize.cross_sectional_residualize_panel(
        values, dates, instruments, controls, min_size=min_size
    )
    new_seconds = time.perf_counter() - start
    print(
        f"\n[perf] 逐特征 {old_seconds:.3f}s vs 批量 {new_seconds:.3f}s "
        f"⇒ 加速 {old_seconds / max(new_seconds, 1e-9):.1f}×"
    )
    assert new_seconds * 3 < old_seconds
