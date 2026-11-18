"""🔴 V2 标签口径（对齐①）的回归测试。

**事故背景（为什么要钉这些）**：
V2 的对齐①要求「训练标签 = 未来 20 交易日超额收益」，规格原文明确
「取 **t+15 ~ t+25 均价**以消除单日收盘毛刺」。这��口径一旦错位
（尤其写成 qlib 的 ``Mean(Ref($vwap,-15)/$vwap-1,11)`` —— 它让分子分母**一起**
滚动，得到的是「15 日前的相对收益」），**整条训练线的监督信号就是错的**，
而指标**看上去仍然正常**（不崩、不 NaN）⇒ 必须钉死。

【本文件钉的判定】
  ① 标签 = **窗口均价 / P[t] - 1**；
  ② qlib 表达式必须是 ``Ref(Mean($vwap,11),-25)/$vwap-1``；
  ③ 窗口不完整 ⇒ NaN（不许用「已到样本末端」的半截窗口凑数）；
  ④ 基准项是**逐日常数** ⇒ CSRankNorm 之后**截面排名不变**（超额的实质在评估层）；
  ⑤ 🔴 **窗口均价口径 ≡ 日收益均值口径**（分母 P[t] 在窗口内固定）
     —— 这条**等价性**是规格文字歧义的「保险丝」：
     无论把「t+15~t+25 均价」读成「均价」还是「日收益的均值」，
     **训练信号完全一样** ⇒ 口径歧义不影响结论。

【变异反证】每条关键判定都配「源码 → 内存改 → tmp_path → import 副本」的变异测试
⇒ 改坏源码里的关键判定 ⇒ 对应断言**必须转红**。
⛔ **绝不落盘改真源码**（真源码是主 agent 的写集）。
"""

from __future__ import annotations

import importlib.util
import sys
from pathlib import Path

import numpy as np
import pandas as pd
import pytest

_V2_DIR = Path(__file__).resolve().parents[1]
if str(_V2_DIR) not in sys.path:
    sys.path.insert(0, str(_V2_DIR))

import config_v2 as C  # noqa: E402
import labels as L  # noqa: E402

LABELS_SOURCE = _V2_DIR / "labels.py"


def _mutated_module(tmp_path: Path, edits: list[tuple[str, str]], tag: str):
    text = LABELS_SOURCE.read_text(encoding="utf-8")
    for old, new in edits:
        assert old in text, f"变异锚点不存在（源码已变）：{old!r}"
        text = text.replace(old, new)
    target = tmp_path / f"{tag}.py"
    target.write_text(text, encoding="utf-8")
    name = f"mutant_labels_{tag}"
    spec = importlib.util.spec_from_file_location(name, target)
    module = importlib.util.module_from_spec(spec)
    sys.modules[name] = module
    spec.loader.exec_module(module)
    return module


@pytest.fixture()
def px() -> pd.DataFrame:
    """价格宽表：6 只票 × 60 个交易日（斜率不同 ⇒ 截面有区分度）。"""
    idx = pd.bdate_range("2024-01-02", periods=60)
    data = {}
    for i in range(6):
        data[f"sh60000{i}"] = (10.0 + i) * (1.0 + 0.001 * i) ** np.arange(len(idx))
    return pd.DataFrame(data, index=idx)


@pytest.fixture()
def volatile_px() -> pd.DataFrame:
    """**高波动**价格表（确定性伪随机游走）⇒ 口径差异测得出来。"""
    idx = pd.bdate_range("2024-01-02", periods=60)
    rng = np.random.default_rng(20261117)
    shocks = rng.normal(0.0, 0.08, size=(60, 6))
    data = {}
    for j in range(6):
        data[f"sh60000{j}"] = 10.0 * np.exp(np.cumsum(shocks[:, j]))
    return pd.DataFrame(data, index=idx)


def _manual_window_avg(px: pd.DataFrame) -> pd.DataFrame:
    """**独立**重算（纯 Python 循环，不用 numpy 堆叠）：窗口均价 / P[t] - 1。"""
    lo, hi = 15, 25
    vals = px.to_numpy(dtype="float64")
    n_t, n_n = vals.shape
    rows = []
    for t in range(n_t):
        row = []
        for j in range(n_n):
            if t + hi >= n_t or not np.isfinite(vals[t, j]):
                row.append(np.nan)
                continue
            window = [vals[t + k, j] for k in range(lo, hi + 1)]
            row.append(float(np.mean(window)) / vals[t, j] - 1.0)
        rows.append(row)
    return pd.DataFrame(rows, index=px.index, columns=px.columns)


def _manual_daily_return_mean(px: pd.DataFrame) -> pd.DataFrame:
    """**独立**重算「日收益的均值」口径：mean_k( P[t+k]/P[t] - 1 )。"""
    lo, hi = 15, 25
    vals = px.to_numpy(dtype="float64")
    n_t, n_n = vals.shape
    rows = []
    for t in range(n_t):
        row = []
        for j in range(n_n):
            if t + hi >= n_t or not np.isfinite(vals[t, j]):
                row.append(np.nan)
                continue
            row.append(float(np.mean([vals[t + k, j] / vals[t, j] - 1.0
                                      for k in range(lo, hi + 1)])))
        rows.append(row)
    return pd.DataFrame(rows, index=px.index, columns=px.columns)


def _manual_qlib_rolling(px: pd.DataFrame) -> pd.DataFrame:
    """**独立**重算 qlib 滚动口径（= **错误**写法）：mean_j( P[t+15-j]/P[t-j] - 1 )。

    🔴 这就是 ``Mean(Ref($vwap,-15)/$vwap-1,11)`` 的真实语义：
       分子分母**一起**滚动 ⇒ 不是「未来 15~25 日」而是「15 日前的相对收益」。
    """
    lo, hi = 15, 25
    n_win = hi - lo + 1
    vals = px.to_numpy(dtype="float64")
    n_t, n_n = vals.shape
    rows = []
    for t in range(n_t):
        row = []
        for j in range(n_n):
            if t + hi >= n_t or not np.isfinite(vals[t, j]):
                row.append(np.nan)
                continue
            terms = []
            for j_off in range(n_win):
                k = lo + j_off
                base_idx = t - j_off
                if base_idx < 0 or not np.isfinite(vals[base_idx, j]):
                    terms = []
                    break
                terms.append(vals[t + k, j] / vals[base_idx, j] - 1.0)
            row.append(float(np.mean(terms)) if terms else np.nan)
        rows.append(row)
    return pd.DataFrame(rows, index=px.index, columns=px.columns)


# --------------------------------------------------------------------- ① 口径


class TestLabelCaliber:
    def test_退出窗口必须是15到25(self) -> None:
        assert L.label_exit_window() == (15, 25)
        assert C.LABEL_EXIT_WINDOW == (15, 25)
        assert L.label_horizon_days() == 25

    def test_标签是窗口均价除以P_t减1(self, px: pd.DataFrame) -> None:
        out = L.forward_window_return(px)
        pd.testing.assert_frame_equal(
            out, _manual_window_avg(px), check_names=False, rtol=1e-12, atol=1e-12
        )

    def test_窗口均价口径与日收益均值口径恒等(self, volatile_px: pd.DataFrame) -> None:
        """🔴 规格文字歧义的「保险丝」：两种读法给出**同一个**训练信号。

        数学：mean_k(P[t+k]/P[t] - 1) = (mean_k P[t+k])/P[t] - 1（分母在窗口内固定）。
        ⇒ 无论把「均价」读成「均价」还是「日收益的均值」，结论不变。
        """
        out = L.forward_window_return(volatile_px)
        daily = _manual_daily_return_mean(volatile_px)
        diff = np.nanmax(np.abs(out.to_numpy(dtype=float) - daily.to_numpy(dtype=float)))
        assert diff < 1e-12, f"两种口径居然不等价（max diff={diff:.3e}）"

    def test_qlib滚动口径必须与本口径不同(self, volatile_px: pd.DataFrame) -> None:
        """反证：qlib 的 ``Mean(Ref($vwap,-15)/$vwap-1,11)``（分子分母一起滚动）
        与本口径**不同** ⇒ 证明「写成那个写法就错了」。"""
        out = L.forward_window_return(volatile_px)
        rolling = _manual_qlib_rolling(volatile_px)
        diff = np.nanmax(np.abs(out.to_numpy(dtype=float) - rolling.to_numpy(dtype=float)))
        assert diff > 1e-4, f"qlib 滚动口径与本口径居然相同（max diff={diff:.3e}）"

    def test_qlib表达式必须是RefMean25(self) -> None:
        expr = L.qlib_label_expression()
        assert expr == "Ref(Mean($vwap,11),-25)/$vwap-1"
        assert "Mean(Ref($vwap,-15)" not in expr
        assert "Ref($vwap, 0)" not in expr  # Ref(ATTR,0) 会被 qlib 告警不准

    def test_窗口不完整必须NaN(self, px: pd.DataFrame) -> None:
        out = L.forward_window_return(px)
        assert np.isnan(out.loc[px.index[-1], "sh600000"])
        assert np.isnan(out.loc[px.index[-25], "sh600000"])
        assert not np.isnan(out.loc[px.index[-26], "sh600000"])

    def test_基准项是逐日常数且不改变截面排名(self, px: pd.DataFrame) -> None:
        fwd = L.forward_window_return(px)
        bench = pd.Series(0.03, index=px.index)
        excess = L.excess_forward_return(fwd, bench)
        assert L.rank_invariance_under_daily_shift(fwd, excess)

    def test_逐日常数基准不改变排名(self, px: pd.DataFrame) -> None:
        """正向：基准项逐日常数（哪怕逐日不同）⇒ 当日截面排名逐位不变。"""
        fwd = L.forward_window_return(px)
        bench = pd.Series(np.linspace(0.0, 0.5, len(px.index)), index=px.index)
        excess = L.excess_forward_return(fwd, bench)
        assert L.rank_invariance_under_daily_shift(fwd, excess)

    def test_基准项在截面内变化时排名必须改变(self, px: pd.DataFrame) -> None:
        """反证：只有当偏移量在**截面内也变化**时，排名才会变 ⇒ 断言有牙。"""
        fwd = L.forward_window_return(px)
        rng = np.random.default_rng(20261117)
        bench_panel = pd.DataFrame(
            rng.normal(0.0, 0.05, size=fwd.shape), index=fwd.index, columns=fwd.columns
        )
        assert not L.rank_invariance_under_daily_shift(fwd, fwd + bench_panel)

    def test_标签口径卡字段齐全(self) -> None:
        card = L.label_caliber()
        for key in ("label_name", "price_field", "exit_window", "horizon_days",
                    "benchmark", "qlib_expression", "formula", "post_processing"):
            assert key in card, f"标签口径卡缺字段 {key}"
        assert card["exit_window"] == [15, 25]
        assert card["horizon_days"] == 25
        assert card["benchmark"] == "sh000300"


# --------------------------------------------------------------------- ② 变异反证


def test_变异反证_只用窗口首日价格代替窗口均价_结果必须不一致(tmp_path: Path) -> None:
    """把「窗口均价」退化成「只用 t+15 的价格」⇒ 结果必须变化（证明均价真被实现了）。"""
    mutant = _mutated_module(
        tmp_path,
        [("    exit_px = np.nanmean(stack, axis=0)", "    exit_px = stack[0]")],
        "first_only",
    )
    idx = pd.bdate_range("2024-01-02", periods=40)
    rng = np.random.default_rng(7)
    shocks = rng.normal(0.0, 0.08, size=(40, 2))
    df = pd.DataFrame(
        {c: 10.0 * np.exp(np.cumsum(shocks[:, j])) for j, c in enumerate(["a", "b"])},
        index=idx,
    )
    got = mutant.forward_window_return(df)["a"].to_numpy(dtype=float)
    want = _manual_window_avg(df)["a"].to_numpy(dtype=float)
    assert not np.allclose(got, want, equal_nan=True)


def test_变异反证_把qlib表达式改回错误写法_表达式必须不同(tmp_path: Path) -> None:
    mutant = _mutated_module(
        tmp_path,
        [("    return str(C.LABEL_EXPRESSION)", '    return "Mean(Ref($vwap,-15)/$vwap-1,11)"')],
        "bad_expr",
    )
    assert mutant.qlib_label_expression() != "Ref(Mean($vwap,11),-25)/$vwap-1"


def test_变异反证_窗口不完整不置NaN_末端必须被污染(tmp_path: Path) -> None:
    mutant = _mutated_module(
        tmp_path,
        [("    out = np.where(full & np.isfinite(base), out, np.nan)",
          "    out = np.nan_to_num(out, nan=0.0)")],
        "no_nan_tail",
    )
    idx = pd.bdate_range("2024-01-02", periods=40)
    p = np.arange(40, dtype=float)
    df = pd.DataFrame({"a": 10 * (1.001 ** p)}, index=idx)
    out = mutant.forward_window_return(df)
    assert out.iloc[-1, 0] == 0.0  # 变异体确实污染了末端
