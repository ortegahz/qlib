"""🔴 回归测试：``manu/tools/v3_comparison_probe.py`` 的 **ETF 行情面板代码口径**。

**背景（2026-11-26 真缺陷）**：探针的 :func:`load_etf_market` 在 ``--etf-prices-csv``
分支用**裸** ``pd.read_csv(prices_csv)`` ⇒ ``code`` 列被 pandas 推断成 **int64**
⇒ 宽表列名变成整数（``510300`` 而非 ``'510300'``）⇒ 与 V2 的
``etf_layer.load_etf_prices``（``dtype={'code': str}`` ＋ ``str.zfill(6)``）**口径分叉**
⇒ ``etf_layer.build_targets`` 里 ``_row(col, code)`` 用 str 代码去 int 列名的特征宽表
里查 ⇒ **全部 miss** ⇒ ``amount20d``/``last_close``/``n_days`` 全 NaN
⇒ ``liquidity_ok`` 全 False ⇒ **池空** ⇒ 61 个目标全是 ``{}``
⇒ **指标③两臂同时全平**（年化 0.00%、换手 0.0000），而指标①②照常正常
（它们走 qlib 个股面板，不碰 ETF close 面板）。

本文件锁住三件事：

1. ``load_etf_market`` 的代码列名**必须是 str**（裸 ``read_csv`` 会得到 int）；
2. 它的列名集合与 **V2 的 ``load_etf_prices``** **完全一致**（一个事实源）；
3. ``_assert_str_code_columns`` 对 int 列名 **fail-closed**（抛错，不静默跑偏）。
"""

from __future__ import annotations

import importlib.util
import sys
from pathlib import Path

import numpy as np
import pandas as pd
import pytest

#: `manu/etf_rotation/v3/` 目录
V3_DIR = Path(__file__).resolve().parents[1]
#: `manu/tools/` 目录（探针所在处）
TOOLS_DIR = V3_DIR.parent.parent / "tools"


def _load_probe():
    """按**文件路径**加载探针模块（探针在 ``manu/tools/``，不在包内）。"""
    path = TOOLS_DIR / "v3_comparison_probe.py"
    spec = importlib.util.spec_from_file_location("v3_comparison_probe", path)
    if spec is None or spec.loader is None:  # pragma: no cover - 路径写错才会到这
        raise ImportError(f"🔴 无法加载探针模块：{path}")
    module = importlib.util.module_from_spec(spec)
    sys.modules["v3_comparison_probe"] = module
    spec.loader.exec_module(module)
    return module


@pytest.fixture()
def probe():
    return _load_probe()


@pytest.fixture()
def prices_csv(tmp_path: Path) -> Path:
    """一个**最小**的 ETF 行情长表（``code`` 是纯数字文本 ⇒ 裸 read_csv 会推成 int）。"""
    path = tmp_path / "etf_prices.csv"
    path.write_text(
        "date,code,open,high,low,close,volume,amount\n"
        "2019-01-02,510300,3.8,3.95,3.78,3.9,1000,3900000\n"
        "2019-01-02,159915,1.05,1.12,1.04,1.1,2000,2200000\n"
        "2019-01-03,510300,3.9,4.02,3.88,3.95,1100,4345000\n"
        "2019-01-03,159915,1.1,1.15,1.09,1.14,2200,2508000\n",
        encoding="utf-8",
    )
    return path


def test_bare_read_csv_would_infer_int(probe, prices_csv: Path) -> None:
    """🔴 前提核对：裸 ``read_csv`` 确实会把 ``code`` 推成 int64（缺陷的根因）。"""
    raw = pd.read_csv(prices_csv)
    assert raw["code"].dtype != object or not isinstance(raw["code"].iloc[0], str)


def test_load_etf_market_keeps_code_as_str(probe, prices_csv, monkeypatch) -> None:
    """✅ 修复后：宽表列名**必须是 str**（不再被推断成 int）。"""
    monkeypatch.setattr(probe.CON, "read_holdings_frame", lambda: pd.DataFrame())
    close, amount, names = probe.load_etf_market(str(prices_csv))
    assert sorted(close.columns) == ["159915", "510300"]
    assert all(isinstance(c, str) for c in close.columns)
    assert sorted(amount.columns) == ["159915", "510300"]
    assert all(isinstance(c, str) for c in amount.columns)
    assert names == {}


def test_load_etf_market_matches_v2_loader(probe, prices_csv, monkeypatch) -> None:
    """✅ 与 **V2 的 ``load_etf_prices``** 同一口径（一个事实源 ⇒ 列名集合逐字相等）。"""
    monkeypatch.setattr(probe.CON, "read_holdings_frame", lambda: pd.DataFrame())
    close, _, _ = probe.load_etf_market(str(prices_csv))
    # 🔴 用探针已持有的 V2 引用（``probe.EL``）——**不写** ``import etf_layer``：
    #    V3 目录下的源码一律不许 import V2 可执行模块（隔离契约 B2，见 v3/probes）。
    ref = probe.EL.load_etf_prices(str(prices_csv)).pivot_table(
        index="date", columns="code", values="close", aggfunc="last")
    assert list(close.columns) == list(ref.columns)
    assert list(close.index) == list(ref.index)


def test_assert_str_code_columns_fails_closed(probe) -> None:
    """🔴 fail-closed：int 列名必须**抛错**（不许静默跑出一份全 0 的报告）。"""
    bad = pd.DataFrame([[1.0, 2.0]], columns=[510300, 159915])
    with pytest.raises(TypeError, match="必须是 str"):
        probe._assert_str_code_columns(bad, "close")
    ok = pd.DataFrame([[1.0, 2.0]], columns=["510300", "159915"])
    probe._assert_str_code_columns(ok, "close")  # 不抛错


# ----------------------------------------------------------------- 根因链回归
def _market_panel(code_as_int: bool, n_days: int = 220):
    """最小可用的 ETF 行情面板（close / amount 宽表，index=交易日）。"""
    dates = pd.bdate_range("2019-01-01", periods=n_days)
    codes = ["510300", "159915"]
    close = pd.DataFrame(
        {c: np.linspace(1.0, 2.0, n_days) for c in codes}, index=dates)
    amount = close * 1.0e7
    if code_as_int:
        mapping = {c: int(c) for c in codes}
        close = close.rename(columns=mapping)
        amount = amount.rename(columns=mapping)
    return close, amount


def test_feature_columns_are_reachable_by_str_code(probe) -> None:
    """🔴 根因链第一环：特征宽表的**列名**必须能被 str 代码命中。

    ``etf_layer.build_targets`` 的 ``_row(col, code)`` 用 **str 代码**去
    ``features[col].index`` 里查 ⇒ 列名是 int ⇒ **全部 miss** ⇒
    ``amount20d``/``last_close``/``n_days`` 全 NaN ⇒ ``liquidity_ok`` 全 False。
    """
    close_str, amount_str = _market_panel(code_as_int=False)
    feats_str = probe.EL.compute_features(close_str, amount_str)
    assert "510300" in feats_str["amount20d"].columns

    close_int, amount_int = _market_panel(code_as_int=True)
    feats_int = probe.EL.compute_features(close_int, amount_int)
    assert "510300" not in feats_int["amount20d"].columns


def test_build_targets_pool_is_nonempty_only_with_str_codes(probe) -> None:
    """🔴 根因链第二环（**本次事故的直接锁**）：**str 列名 ⇒ 池非空**。

    旧口径（int 列名）⇒ 标的池全空（目标全为 ``{}``）；
    新口径（str 列名）⇒ 池非空 ⇒ 端到端才有净值。
    """
    dates = pd.bdate_range("2019-01-01", periods=220)
    signal_day = dates[150]
    weights = {"510300": {"600000": 1.0}}
    scores = pd.DataFrame({"date": [signal_day], "instrument": ["600000"], "score": [1.0]})

    def _run(code_as_int: bool) -> dict:
        close, amount = _market_panel(code_as_int=code_as_int)
        feats = probe.EL.compute_features(close, amount)
        targets, _diag = probe.EL.build_targets(
            [signal_day], scores, weights, close, feats, names={},
            alpha_weight=1.0, momentum_weight=0.0, top_k=1, hold_buffer=0,
            entry_ma_window=60, exit_ma_window=60,
            min_coverage=0.5, min_amount_20d=0.0, min_history_days=60,
            tradable=None, allow_theme_cap=False, neg_score_gate=False,
            neg_score_gate_stat="median", neg_score_gate_scale=0.05,
            neg_score_gate_min_exposure=0.0, smoothing_arm=None,
            rebalance_signal_stride=1,
        )
        return targets

    new_targets = _run(code_as_int=False)
    assert any(v for v in new_targets.values()), "🔴 str 列名口径下标的池竟然全空"

    old_targets = _run(code_as_int=True)
    assert not any(v for v in old_targets.values()), (
        "🔴 int 列名口径下标的池竟然非空 ⇒ 本用例已失去判别力（口径可能又变了）"
    )


def test_assert_targets_not_empty_fails_closed(probe) -> None:
    """🔴 fail-closed 闸门：目标全空 ⇒ 抛错；**部分**为空 ⇒ 放行（不误伤合法空池）。"""
    with pytest.raises(RuntimeError, match="全为空|为空"):
        probe.assert_targets_not_empty("v3_full", {})
    with pytest.raises(RuntimeError, match="全为空"):
        probe.assert_targets_not_empty("v3_full", {"2019-01-09": {}, "2019-02-13": {}})
    probe.assert_targets_not_empty("v3_full", {"2019-01-09": {"510300": 1.0}, "2019-02-13": {}})
