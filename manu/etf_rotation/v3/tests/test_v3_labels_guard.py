"""🔴 V3 标签（对齐①·**数值层面中心化**）的回归测试。

【本文件钉的判定】
  ① **前瞻收益与 V2 逐位一致**（`forward_window_return` ⇔ `v2/labels.py`，
     🔴 只读加载 V2，绝不改它）；
  ② **中心化后截面均值 ≈ 0**（`mode='market_mean'`）—— 🔴 V3 与 V2 的核心差别；
  ③ **保留数值超额**（**不**转成排名）—— 中心化前后秩相关仍是 1，但数值变了；
  ④ ``rank_grades`` 的**取值范围**与**分布**正确；样本不足的日期整日记 0；
  ⑤ **口径卡字段与** `config_v3.v3_caliber_card()['label']` **对齐**。
"""

from __future__ import annotations

import sys
from pathlib import Path

import numpy as np
import pandas as pd
import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import config_v3  # noqa: E402
import labels_v3  # noqa: E402
from conftest import load_v2_module  # noqa: E402


def _price_panel(seed: int = 11) -> pd.DataFrame:
    """合成价格面板（含 NaN 缺口 ⇒ 顺便验证「窗口不完整 ⇒ NaN」）。"""
    rng = np.random.default_rng(seed)
    dates = pd.bdate_range("2020-01-02", periods=60)
    instruments = [f"SZ{600000 + i:06d}" for i in range(12)]
    values = 10.0 * np.exp(np.cumsum(rng.normal(scale=0.02, size=(60, 12)), axis=0))
    frame = pd.DataFrame(values, index=dates, columns=instruments)
    frame.iloc[5, 3] = np.nan
    frame.iloc[40, 7] = np.nan
    return frame


def test_前瞻收益与V2逐位一致() -> None:
    """🔴 与 V2 **逐位一致**（`check_exact=True` ⇒ 一个 bit 都不能差）。"""
    v2 = load_v2_module("v2_labels_ref", "labels.py")
    price = _price_panel()
    expected = v2.forward_window_return(price)
    actual = labels_v3.forward_window_return(price)
    pd.testing.assert_frame_equal(actual, expected, check_exact=True)
    # 🔴 换一个窗口也要一致（口径不许只在默认窗口上对齐）
    expected_5 = v2.forward_window_return(price, window=(5, 10))
    actual_5 = labels_v3.forward_window_return(price, window=(5, 10))
    pd.testing.assert_frame_equal(actual_5, expected_5, check_exact=True)


def test_窗口不完整即为NaN() -> None:
    """🔴 样本末端不足窗口 ⇒ NaN（不许用半截窗口凑数）。"""
    price = _price_panel()
    out = labels_v3.forward_window_return(price)
    assert out.iloc[-1].isna().all()
    assert out.iloc[-30].notna().any()


def test_市场均值中心化后截面均值约零() -> None:
    """🔴 ``mode='market_mean'`` ⇒ 减完当日截面均值 ≈ 0。"""
    fwd = labels_v3.forward_window_return(_price_panel())
    centered, stats = labels_v3.center_label(fwd, mode="market_mean")
    assert stats["mode"] == "market_mean"
    assert stats["n_dates"] == len(fwd.index)
    assert stats["mean_abs_shift"] > 0
    means = centered.mean(axis=1, skipna=True)
    assert float(np.nanmax(np.abs(means.to_numpy()))) < 1e-12


def test_基准中心化是逐日常数() -> None:
    """🔴 ``mode='benchmark'`` ⇒ 减基准的同窗口收益（逐日常数）。"""
    fwd = labels_v3.forward_window_return(_price_panel())
    bench = pd.Series(
        np.linspace(-0.01, 0.01, len(fwd.index)), index=fwd.index, name="bench"
    )
    centered, stats = labels_v3.center_label(fwd, mode="benchmark", bench_fwd=bench)
    assert stats["mode"] == "benchmark"
    expected = fwd.sub(bench, axis=0)
    pd.testing.assert_frame_equal(centered, expected)
    with pytest.raises(ValueError):
        labels_v3.center_label(fwd, mode="benchmark")


def test_中心化保留数值超额不做排名() -> None:
    """🔴 中心化**只减常数**：秩不变，但**数值超额保留**（这正是 V3 的核心差异点）。"""
    fwd = labels_v3.forward_window_return(_price_panel())
    centered, _ = labels_v3.center_label(fwd, mode="market_mean")
    # 逐日截面排名不变
    pd.testing.assert_frame_equal(fwd.rank(axis=1), centered.rank(axis=1))
    # 🔴 数值层面确实变了（超额没有被抹平成 0/1 排名）
    assert not np.allclose(fwd.to_numpy(), centered.to_numpy())
    assert float(np.nanmax(np.abs(centered.to_numpy()))) > 0


def test_rank_grades范围与分布() -> None:
    """🔴 grade ∈ [0, n_grades-1]；分箱均匀；样本不足的日期整日记 0。"""
    rising = [0.01 * (i + 1) for i in range(10)]
    values = pd.DataFrame(
        [rising, [0.02, 0.01] + [np.nan] * 8],
        index=["2020-01-02", "2020-01-03"],
        columns=[f"S{index:06d}" for index in range(10)],
    )
    grades = labels_v3.rank_grades(values, n_grades=5)
    assert grades.to_numpy().min() >= 0
    assert grades.to_numpy().max() <= 4
    assert grades.loc["2020-01-02"].tolist() == [0, 0, 1, 1, 2, 2, 3, 3, 4, 4]
    # 🔴 第二行只有 2 个有效样本 < 5 ⇒ 整行记 0
    assert grades.loc["2020-01-03"].tolist() == [0] * 10
    assert grades.attrs["n_dates_skipped"] == 1
    assert grades.attrs["n_grades"] == 5


def test_口径卡字段与配置对齐() -> None:
    """🔴 口径卡字段名必须与 ``config_v3.v3_caliber_card()['label']`` 对齐。"""
    card = labels_v3.label_caliber()
    expected = config_v3.v3_caliber_card()["label"]
    assert set(expected).issubset(set(card))
    assert card["exit_window"] == list(config_v3.LABEL_EXIT_WINDOW)
    assert card["horizon_days"] == config_v3.LABEL_HORIZON_DAYS
    assert card["centering"] == config_v3.LABEL_CENTERING
    assert card["benchmark"] == config_v3.BENCHMARK
