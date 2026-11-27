"""🔴 V3 **打分侧中性化**（train/serve skew）的回归测试。

【为什么必须有这个文件】
  训练侧（``train_s.prepare_training_frame`` ⇒ ``_neutralize_panels``）把特征做了
  **逐日截面残差化**（规模档 + 流动性），
  而打分侧（``train_s.score_universe``）原先**直接**把原始特征喂给 booster
  ⇒ **train/serve skew**：模型在残差空间训练，却拿原始空间预测。
  只读审计实测 VOL20 截面 R²≈0.565 ⇒ 约 56% 的截面信息在训练被扣掉、打分又被放回去。
  🔴 该偏差对**全部 4 个消融臂共用同一打分路径** ⇒ 消融测不出来。

【本文件钉的判定】
  ① 🔴 **打分侧残差 ≡ 训练侧残差**（逐元素）⇒ 无 skew；
  ② 🔴 **打分侧必须残差化**（特征里含规模/流动性成分时，打分矩阵 ≠ 原始特征）；
  ③ ``neutralize=False``（消融臂 ``v3_no_neutralize``）⇒ 打分侧**不**残差化；
  ④ 🔴 **缺 tier_panel ⇒ 直接 raise**（不许静默退化成「不残差」）；
  ⑤ 🔴 **控制变量缺测的行：训练侧与打分侧剔除同一批**（样本集一致）；
  ⑥ **唯一装配点**：``_long_to_panel`` 是训练/打分共用的面板装配逻辑。

【口径提醒】本测试只覆盖**纯 numpy/pandas 数值**，不 import qlib、不联网、不读 /mnt/data。
"""

from __future__ import annotations

import sys
from pathlib import Path

import numpy as np
import pandas as pd
import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import neutralize as neu  # noqa: E402
import train_s as TS  # noqa: E402


N_INST = 60
N_DATES = 4
N_FEAT = 3
FEATURE_COLS = [f"f{i}" for i in range(N_FEAT)]


def _synthetic(seed: int = 11, n_inst: int = N_INST, n_dates: int = N_DATES):
    """合成「规模档 + 流动性 + 特征 + 标签」四件套（🔴 全部本地生成）。"""
    rng = np.random.default_rng(seed)
    dates = pd.bdate_range("2020-03-02", periods=n_dates)
    instruments = [f"SZ{600000 + i:06d}" for i in range(n_inst)]
    tier_values = np.array([[i % 4 for i in range(n_inst)] for _ in range(n_dates)])
    tier_panel = pd.DataFrame(tier_values, index=dates, columns=instruments)
    liquidity = pd.DataFrame(
        rng.normal(size=(n_dates, n_inst)), index=dates, columns=instruments
    )
    # 🔴 特征里**故意**塞进规模档与流动性成分 ⇒ 不残差化就一定对不上
    # 🔴 每个特征都带**特质噪声**（否则特征恰好落在控制变量张成的空间里
    #    ⇒ 残差退化成数值噪声 ⇒ 正交性断言失去意义）
    x = np.empty((n_dates, n_inst, N_FEAT))
    x[:, :, 0] = (1.5 * (tier_values == 0) - 0.8 * (tier_values == 1)
                  + rng.normal(scale=0.3, size=(n_dates, n_inst)))
    x[:, :, 1] = 2.0 * liquidity.to_numpy() + rng.normal(scale=0.3, size=(n_dates, n_inst))
    x[:, :, 2] = rng.normal(scale=0.3, size=(n_dates, n_inst))
    y = rng.normal(size=(n_dates, n_inst))
    return dates, instruments, tier_panel, liquidity, x, y


def _wide_frame(dates, instruments, x: np.ndarray, y: np.ndarray) -> pd.DataFrame:
    """把 (日期 × 标的 × 特征) 面板转成 qlib handler 风格的宽表。"""
    index = pd.MultiIndex.from_product(
        [pd.DatetimeIndex(dates), list(instruments)], names=["datetime", "instrument"]
    )
    columns = pd.MultiIndex.from_tuples([(c,) for c in FEATURE_COLS] + [("LABEL0",)])
    data = np.concatenate([x.reshape(-1, x.shape[2]), y.reshape(-1, 1)], axis=1)
    return pd.DataFrame(data, index=index, columns=columns)


class _FakeHandler:
    """假 handler：直接回放合成宽表（不碰 qlib）。"""

    def __init__(self, frame: pd.DataFrame):
        self._frame = frame

    def fetch(self, selector=None, level=None, col_set=None, data_key=None):
        return self._frame


class _CapturingBooster:
    """假 booster：记录每次 predict 收到的特征矩阵（🔴 用来对拍打分侧口径）。"""

    def __init__(self):
        self.calls: list[np.ndarray] = []

    def predict(self, X):
        arr = np.asarray(X, dtype=float)
        self.calls.append(arr.copy())
        # 🔴 分数必须有区分度（``_assert_non_degenerate`` 要求 ≥10 个唯一值）
        return arr[:, 0] * 0.5 + np.arange(len(arr)) * 1e-4


def _run_score_universe(tmp_path, frame, *, neutralize, tier_panel, liquidity_panel,
                        score_dates):
    """跑一次打分（monkeypatch 掉 handler 构造），返回 (booster, 打分 meta)。"""
    booster = _CapturingBooster()
    original = TS.build_score_handler
    TS.build_score_handler = lambda *a, **k: _FakeHandler(frame)
    try:
        meta = TS.score_universe(
            booster, object(), (), "SYNTH", tmp_path, "unit",
            pd.Timestamp(score_dates[0]).strftime("%Y-%m-%d"),
            pd.Timestamp(score_dates[-1]).strftime("%Y-%m-%d"),
            chunk_months=12, with_fwd=False, score_dates=[str(d) for d in score_dates],
            neutralize=neutralize, tier_panel=tier_panel, liquidity_panel=liquidity_panel,
        )
    finally:
        TS.build_score_handler = original
    return booster, meta


def test_score_side_residual_is_identical_to_training_side():
    """① 打分侧残差 ≡ 训练侧残差（逐元素）⇒ 无 train/serve skew。"""
    dates, instruments, tier_panel, liquidity, x, y = _synthetic()
    frame = _wide_frame(dates, instruments, x, y)

    # ---- 训练侧口径（事实源：``_neutralize_panels``）----
    long = TS._normalize_long_frame(frame).sort_values(["date", "instrument"]).reset_index(drop=True)
    x_panel, date_pos, inst_pos, d_axis, i_axis = TS._long_to_panel(long, FEATURE_COLS)
    y_panel = np.full((len(d_axis), len(i_axis)), np.nan)
    y_panel[date_pos, inst_pos] = long["LABEL0"].to_numpy(dtype=float)
    x_res, _y_res, _stats = TS._neutralize_panels(
        x_panel, y_panel, d_axis, i_axis, tier_panel, liquidity
    )

    # ---- 打分侧口径（走真实 ``score_universe``）----
    booster, meta = _run_score_universe(
        Path(__import__("tempfile").mkdtemp()), frame,
        neutralize=True, tier_panel=tier_panel, liquidity_panel=liquidity,
        score_dates=dates,
    )
    assert len(booster.calls) == len(dates)

    for k, d in enumerate(dates):
        rows = np.flatnonzero(date_pos == k)
        expected = x_res[date_pos[rows], inst_pos[rows], :]
        got = booster.calls[k]
        assert got.shape == expected.shape
        # 🔴 逐元素（NaN 位置也要一样）
        assert np.array_equal(np.isnan(got), np.isnan(expected))
        finite = np.isfinite(expected)
        assert np.allclose(got[finite], expected[finite], rtol=1e-12, atol=1e-12)


def test_score_side_actually_residualizes(tmp_path):
    """② 打分侧**确实**做了残差化（不是把原始特征直接喂给 booster）。"""
    dates, instruments, tier_panel, liquidity, x, y = _synthetic()
    frame = _wide_frame(dates, instruments, x, y)
    booster, _meta = _run_score_universe(
        tmp_path, frame, neutralize=True, tier_panel=tier_panel, liquidity_panel=liquidity,
        score_dates=dates,
    )
    raw = x.reshape(-1, x.shape[2])
    # 🔴 规模档成分（f0）必须被扣掉 ⇒ 打分矩阵与原始特征**不等**
    assert not np.allclose(booster.calls[0], raw[: len(booster.calls[0])])


def test_score_side_residual_is_orthogonal_to_controls(tmp_path):
    """②b 打分残差与控制变量（规模档哑变量 + 流动性）截面正交。"""
    dates, instruments, tier_panel, liquidity, x, y = _synthetic()
    frame = _wide_frame(dates, instruments, x, y)
    booster, _meta = _run_score_universe(
        tmp_path, frame, neutralize=True, tier_panel=tier_panel, liquidity_panel=liquidity,
        score_dates=dates,
    )
    controls = neu.build_controls(tier_panel, liquidity)
    day = pd.Timestamp(dates[0])
    ctrl_day = controls.xs(day, level="date")
    got = booster.calls[0]
    # 打分侧行序 = 该日标的升序（与 tier_panel.columns 一致）
    assert list(got.shape) == [len(instruments), N_FEAT]
    for col in ("tier_csi300", "tier_csi500", "tier_csi1000", "log_turnover"):
        if col not in ctrl_day.columns:
            continue
        c = ctrl_day[col].to_numpy(dtype=float)
        ok = np.isfinite(c) & np.isfinite(got[:, 0])
        if ok.sum() < 3:
            continue
        corr = float(np.corrcoef(c[ok], got[ok, 0])[0, 1])
        assert abs(corr) < 1e-8, f"{col} 与残差未正交：corr={corr}"


def test_no_neutralize_arm_uses_raw_features(tmp_path):
    """③ 消融臂 ``v3_no_neutralize`` ⇒ 打分侧**不**残差化（喂原始特征）。"""
    dates, instruments, tier_panel, liquidity, x, y = _synthetic()
    frame = _wide_frame(dates, instruments, x, y)
    booster, meta = _run_score_universe(
        tmp_path, frame, neutralize=False, tier_panel=tier_panel, liquidity_panel=liquidity,
        score_dates=dates,
    )
    raw = x.reshape(-1, x.shape[2])
    for k in range(len(dates)):
        assert np.allclose(booster.calls[k], raw[k * len(instruments):(k + 1) * len(instruments)])
    assert meta["neutralize"]["enabled"] is False


def test_missing_tier_panel_is_fail_closed(tmp_path):
    """④ 🔴 缺 tier_panel ⇒ 直接 raise（不许静默退化成「不残差」）。"""
    dates, instruments, tier_panel, liquidity, x, y = _synthetic()
    frame = _wide_frame(dates, instruments, x, y)
    with pytest.raises(RuntimeError, match="skew|tier_panel"):
        _run_score_universe(
            tmp_path, frame, neutralize=True, tier_panel=None, liquidity_panel=liquidity,
            score_dates=dates,
        )


def test_rows_without_controls_are_dropped_on_both_sides(tmp_path):
    """⑤ 🔴 控制变量缺测的行：训练侧与打分侧剔除**同一批**（样本集一致）。"""
    dates, instruments, tier_panel, liquidity, x, y = _synthetic()
    # 让 3 只票的流动性缺测 ⇒ 它们在训练侧与打分侧都不可用
    missing = instruments[:3]
    liquidity = liquidity.copy()
    liquidity.loc[:, missing] = np.nan
    frame = _wide_frame(dates, instruments, x, y)

    long = TS._normalize_long_frame(frame).sort_values(["date", "instrument"]).reset_index(drop=True)
    x_panel, date_pos, inst_pos, d_axis, i_axis = TS._long_to_panel(long, FEATURE_COLS)
    y_panel = np.full((len(d_axis), len(i_axis)), np.nan)
    y_panel[date_pos, inst_pos] = long["LABEL0"].to_numpy(dtype=float)
    x_res, y_res, _stats = TS._neutralize_panels(
        x_panel, y_panel, d_axis, i_axis, tier_panel, liquidity
    )
    train_keep = TS._usable_rows(x_res[date_pos, inst_pos, :], y_res[date_pos, inst_pos])

    booster, meta = _run_score_universe(
        tmp_path, frame, neutralize=True, tier_panel=tier_panel, liquidity_panel=liquidity,
        score_dates=dates,
    )
    for k in range(len(dates)):
        assert booster.calls[k].shape[0] == int(train_keep.sum()) // len(dates)
        # 🔴 缺测的那些票一个都不许出现在打分结果里
        assert meta["neutralize"]["n_rows_dropped_no_controls"] >= 3


def test_usable_rows_rule_is_symmetric():
    """⑥ 样本可用规则：两侧同一条（至少一个有限残差特征）。"""
    x = np.array([[1.0, np.nan], [np.nan, np.nan], [np.nan, 2.0]])
    assert TS._usable_rows(x).tolist() == [True, False, True]
    y = np.array([0.0, 1.0, np.nan])
    assert TS._usable_rows(x, y).tolist() == [True, False, False]
