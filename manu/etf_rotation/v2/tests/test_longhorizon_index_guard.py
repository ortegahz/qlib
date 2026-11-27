"""🔴 **长周期指数宇宙外推检验**（2015 ~ 2023）的守卫测试。

【本文件钉死（全部自带夹具 ⇒ 不联网 / 不读大数据 / 秒级）】

  ① **封存段守卫**：两个新文件（``fetch_sw_index_prices.py`` /
     ``v2_longhorizon_index_probe.py``）的源码里**不许出现 2024 及以后的日期字面量**
     ⇒ 从代码层面保证「V2 的 2024+ 单盲封存段没被用」；
  ② **特征同口径**：指数宇宙的 ``mom20/mom60/mom120`` 与 ``ma60/above_ma60``
     必须与 ``etf_layer.compute_features`` **逐位一致**（不许自己另发明动量定义）；
  ③ **选股等价**：本探针复刻的目标权重必须与 ``etf_layer.build_targets``
     （``alpha_weight=0 / momentum_weight=1``）**逐位一致**（合成面板对照）；
  ④ **窗口常量**：长周期窗口必须是 ``("2015-01-01", "2023-12-31")``，且预热窗口早于起点；
  ⑤ **抓数纪律**：申万宇宙 31 只、默认区间止于 2023-12-31、**单只失败不许中断整批**
     且失败必须记进 manifest。

⛔ 不联网、不跑真实回测、不读真实产物（只读两个新工具的源码 + 合成面板）。
"""

from __future__ import annotations

import importlib.util
import re
import sys
from pathlib import Path

import numpy as np
import pandas as pd
import pytest

REPO_ROOT = Path(__file__).resolve().parents[4]
_V2_DIR = Path(__file__).resolve().parents[1]
if str(_V2_DIR) not in sys.path:
    sys.path.insert(0, str(_V2_DIR))

import etf_layer as EL  # noqa: E402

FETCH_TOOL = REPO_ROOT / "manu" / "tools" / "fetch_sw_index_prices.py"
PROBE_TOOL = REPO_ROOT / "manu" / "tools" / "v2_longhorizon_index_probe.py"
NEW_FILES = (FETCH_TOOL, PROBE_TOOL)

#: 🔴 **2024+ 日期字面量**（完整日期 / 8 位紧凑写法都算；单独的年份不算字面量）
FORBIDDEN_DATE_PATTERNS = (
    re.compile(r"20(?:2[4-9]|[3-9]\d)\s*[-/.年]\s*\d{1,2}\s*[-/.月]\s*\d{1,2}"),
    re.compile(r"20(?:2[4-9]|[3-9]\d)\d{4}"),
)


def _load_module(path: Path, name: str):
    """按路径加载一个模块（``manu/tools/`` 不是包 ⇒ 用 importlib）。"""
    spec = importlib.util.spec_from_file_location(name, path)
    assert spec is not None and spec.loader is not None, f"🔴 加载不了 {path}"
    module = importlib.util.module_from_spec(spec)
    sys.modules[name] = module
    spec.loader.exec_module(module)
    return module


@pytest.fixture(scope="module")
def probe():
    """长周期探针模块。"""
    return _load_module(PROBE_TOOL, "v2_longhorizon_index_probe")


@pytest.fixture(scope="module")
def fetcher():
    """申万抓数模块（``akshare`` 是延迟导入 ⇒ 本地无 akshare 也能加载）。"""
    return _load_module(FETCH_TOOL, "fetch_sw_index_prices")


# --------------------------------------------------------------------- 夹具
def _synthetic_panel(n_days: int = 320, n_codes: int = 8, seed: int = 20261126):
    """合成指数面板（随机游走 ⇒ 确定性；**不碰真实行情**）。"""
    rng = np.random.default_rng(seed)
    dates = pd.bdate_range("2014-01-02", periods=n_days)
    codes = [f"8010{i:02d}" for i in range(n_codes)]
    drift = np.linspace(-0.0004, 0.0006, n_codes)
    data = {}
    for j, code in enumerate(codes):
        path = np.cumsum(rng.normal(drift[j], 0.012, n_days))
        data[code] = 1000.0 * np.exp(path)
    close = pd.DataFrame(data, index=dates)
    amount = close * 1e8
    return close, amount, codes


# --------------------------------------------------------------------- ① 封存段守卫
def test_1_源码不许出现2024及以后的日期字面量() -> None:
    """🔴 两个新文件里**不许**出现 2024+ 的日期字面量（封存段纪律）。"""
    for path in NEW_FILES:
        assert path.exists(), f"🔴 缺文件：{path}"
        src = path.read_text(encoding="utf-8")
        hits: list[str] = []
        for pattern in FORBIDDEN_DATE_PATTERNS:
            hits.extend(pattern.findall(src))
        assert not hits, (
            f"🔴 {path.name} 里出现了 2024+ 的日期字面量 {hits}"
            "⇒ V2 的 2024 起单盲封存段不得被本任务触碰"
        )


def test_1b_抓数与回测的默认区间都止于2023() -> None:
    """🔴 抓数默认 ``end`` 与回测默认窗口都必须落在 2023-12-31 及以前。"""
    fetcher = _load_module(FETCH_TOOL, "fetch_sw_index_prices_dup")
    assert fetcher.DEFAULT_START == "2014-01-01", fetcher.DEFAULT_START
    assert fetcher.DEFAULT_END == "2023-12-31", fetcher.DEFAULT_END
    probe = _load_module(PROBE_TOOL, "v2_longhorizon_index_probe_dup")
    assert probe.LONGHORIZON_WINDOW == ("2015-01-01", "2023-12-31"), probe.LONGHORIZON_WINDOW
    assert probe.WARMUP_START == "2014-01-01", probe.WARMUP_START


# --------------------------------------------------------------------- ② 特征同口径
def test_2_指数特征与etf_layer逐位一致(probe) -> None:
    """🔴 指数宇宙的特征必须与 ``etf_layer.compute_features`` **逐位一致**。"""
    close, amount, _codes = _synthetic_panel()
    mine = probe.build_index_features(close, amount)
    theirs = EL.compute_features(close, amount)
    for key in ("mom20", "mom60", "mom120", "ma60", "above_ma60", "amount20d"):
        assert key in mine, f"🔴 探针没产出特征 {key}"
        pd.testing.assert_frame_equal(mine[key], theirs[key])
    # 🔴 动量权重 / 趋势窗口也必须用 V2 的常量（不许自己另发明）
    assert EL.MOMENTUM_WEIGHTS == {20: 0.2, 60: 0.5, 120: 0.3}
    assert EL.MA_TREND_WINDOW == 60


# --------------------------------------------------------------------- ③ 选股等价
def test_3_目标权重与etf_layer_build_targets逐位一致(probe) -> None:
    """🔴 复刻的选股结果必须与 ``etf_layer.build_targets``（纯动量口径）**逐位一致**。"""
    close, amount, codes = _synthetic_panel()
    feats = EL.compute_features(close, amount)
    # 从第 130 个交易日起的信号栅格（保证 mom120 / ma60 都有值 ⇒ 不撞 NaN 边界）
    signal_days = [pd.Timestamp(d) for d in close.index[130::10]]

    # ---- V2 侧：同一块面板 + 假的穿透分 E（alpha_weight=0 ⇒ E 不影响排序分）----
    weights = {code: {f"SYN_{code}": 100.0} for code in codes}
    score_rows = []
    for day in signal_days:
        for j, code in enumerate(codes):
            score_rows.append({"date": day, "instrument": f"SYN_{code}",
                               "score": float(j) * 0.1})
    scores = pd.DataFrame(score_rows)
    names = {code: f"行业{code}" for code in codes}
    targets_v2, _diag_v2 = EL.build_targets(
        signal_days, scores, weights, close, feats, names=names,
        alpha_weight=0.0, momentum_weight=1.0,
        top_k=3, hold_buffer=2,
        entry_ma_window=0, exit_ma_window=0,
        min_coverage=0.0, min_amount_20d=0.0, min_history_days=0,
        allow_theme_cap=False, rebalance_signal_stride=2,
    )

    # ---- 探针侧：同一块面板、同一组参数 ----
    targets_probe, _diag_probe = probe.build_targets_index(
        signal_days, close, feats,
        top_k=3, hold_buffer=2,
        entry_ma_window=0, exit_ma_window=0,
        min_amount_20d=0.0, min_history_days=0,
        rebalance_signal_stride=2,
    )

    assert set(targets_probe) == set(targets_v2), (
        f"🔴 调仓日不一致：探针 {sorted(targets_probe)[:3]} vs V2 {sorted(targets_v2)[:3]}")
    for day, tw in targets_v2.items():
        assert targets_probe[day] == tw, (
            f"🔴 {day} 的目标权重不一致：探针 {targets_probe[day]} vs V2 {tw}")


# --------------------------------------------------------------------- ④ 窗口常量
def test_4_长周期窗口常量与预热(probe) -> None:
    """🔴 窗口常量必须钉死：评估 2015-01-01 ~ 2023-12-31，预热早于起点。"""
    assert probe.LONGHORIZON_WINDOW == ("2015-01-01", "2023-12-31")
    start, end = probe.LONGHORIZON_WINDOW
    assert pd.Timestamp(probe.WARMUP_START) < pd.Timestamp(start), (
        "🔴 预热窗口必须早于评估起点（动量 120 日 + MA60 需要预热）")
    assert pd.Timestamp(end) < pd.Timestamp("2024-01-01"), "🔴 评估窗口不得越过 2024 封存段"
    # 🔴 冻结参数必须与 V2 冠军臂同值
    assert probe.TOP_K == 5
    assert probe.HOLD_BUFFER == 2
    assert probe.CADENCE == 20
    assert probe.COMMISSION == 0.00015
    assert probe.SLIPPAGE == 0.0005
    assert probe.PERIODS_PER_YEAR == 250
    # 🔴 指数宇宙没有穿透分 E ⇒ 排序分必须是纯动量（且必须在报告里披露）
    assert probe.ALPHA_WEIGHT == 0.0
    assert probe.MOMENTUM_WEIGHT == 1.0
    # 🔴 buffer 高原面必须覆盖 0/1/2/3/5
    assert tuple(probe.BUFFER_GRID) == (0, 1, 2, 3, 5)


# --------------------------------------------------------------------- ⑤ 抓数纪律
def test_5_抓数宇宙与失败披露(fetcher) -> None:
    """🔴 宇宙 31 只 + 默认区间止于 2023 + **单只失败不许中断整批**。"""
    assert len(fetcher.SW_L1_UNIVERSE) == 31, len(fetcher.SW_L1_UNIVERSE)
    assert all(len(c) == 6 and c.isdigit() for c in fetcher.SW_L1_UNIVERSE)
    assert len(set(fetcher.SW_L1_UNIVERSE.values())) == 31, "🔴 行业名不许重复"

    class _FakeAk:
        """假 akshare：一只好、一只炸 ⇒ 验证「单只失败不中断整批」。"""

        def __init__(self) -> None:
            self.calls: list[str] = []

        def index_hist_sw(self, symbol: str = "", period: str = "day"):
            self.calls.append(symbol)
            if symbol == "801040":
                raise RuntimeError("模拟源站限流")
            return pd.DataFrame({
                "代码": [symbol, symbol],
                "日期": ["2023-12-28", "2023-12-29"],
                "收盘": [100.0, 101.0],
                "开盘": [99.0, 100.0],
                "最高": [101.0, 102.0],
                "最低": [98.0, 99.0],
                "成交量": [1.0, 2.0],
                "成交额": [3.0, 4.0],
            })

    fake = _FakeAk()
    good, err = fetcher.fetch_one(fake, "801010", "2014-01-01", "2023-12-31", 0)
    assert err == ""
    assert good is not None and len(good) == 2
    assert list(good.columns) == ["date", "code", "open", "high", "low", "close",
                                  "volume", "amount"], list(good.columns)

    bad, err2 = fetcher.fetch_one(fake, "801040", "2014-01-01", "2023-12-31", 0)
    assert bad is None, "🔴 失败必须返回 None（不许半截数据）"
    assert "RuntimeError" in err2, err2

    # 🔴 2024+ 的数据必须被截断（源站只给全历史 ⇒ 截断是唯一的防线）
    class _LateAk:
        def index_hist_sw(self, symbol: str = "", period: str = "day"):
            return pd.DataFrame({
                "代码": [symbol, symbol, symbol],
                "日期": ["2023-12-29", "2024-01-02", "2024-06-28"],
                "收盘": [1.0, 2.0, 3.0], "开盘": [1.0, 2.0, 3.0],
                "最高": [1.0, 2.0, 3.0], "最低": [1.0, 2.0, 3.0],
                "成交量": [1.0, 2.0, 3.0], "成交额": [1.0, 2.0, 3.0],
            })

    late, _ = fetcher.fetch_one(_LateAk(), "801010", "2014-01-01", "2023-12-31", 0)
    assert late is not None
    assert list(late["date"]) == ["2023-12-29"], list(late["date"])
