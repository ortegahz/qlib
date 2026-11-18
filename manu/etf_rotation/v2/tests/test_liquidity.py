"""🔴 V2 真实流动性约束（停牌 / 涨跌停）的回归测试。

**事故背景（为什么要钉这些）**：V1 的穿透打分只问「成分股权重覆盖够不够」，
**不问「这只票今天能不能买」** ⇒ 回测里会出现「选出一只停牌股 / 一字涨停的票」
的**假成交** ⇒ 收益被系统性高估（涨停根本买不到那个价）。
V2 补上这道闸门，判据必须钉死：

  ① ``volume <= 0``（或非有限）⇒ **停牌** ⇒ 不可买；
  ② ``change >= +9.5%`` ⇒ **涨停** ⇒ 不可买（卖方惜售）；
  ③ ``change <= -9.5%`` ⇒ **跌停** ⇒ 不可**卖**（但**仍可买**：
     跌停时卖压巨大反而好买 —— 这是 A 股的常识，也是 ``tradable`` 只表示
     「可买入」的原因；卖出侧过滤请用 ``limit_down`` 标记自行判定）；
  ④ 正常量价 ⇒ 可买；
  ⑤ **边界**：``|change|`` **恰好等于**阈值 ⇒ 判为**触及**涨跌停
     （``>=`` / ``<=`` 含等号；写成 ``>`` 会让「一字板」漏判 ⇒ 假成交回来）。

【变异反证】沿用本仓库既有写法：源码 → 内存改 → tmp_path → import 副本
⇒ 改坏关键判定 ⇒ 对应断言**必须转红**。⛔ **绝不落盘改真源码**。

🔴 本文件**只用自造夹具**（小 DataFrame）⇒ 不跑重量级 qlib 取数、不联网。
"""

from __future__ import annotations

import importlib.util
import sys
from pathlib import Path

import numpy as np
import pandas as pd
import pytest

#: `manu/etf_rotation/v2/` 目录（``liquidity`` 用扁平导入 ``config_v2``）
_V2_DIR = Path(__file__).resolve().parents[1]
if str(_V2_DIR) not in sys.path:
    sys.path.insert(0, str(_V2_DIR))

import liquidity  # noqa: E402

#: 被测模块源码（**只读**）
LIQUIDITY_SOURCE = _V2_DIR / "liquidity.py"

#: 🔴 涨跌停阈值（与 config_v2.LIMIT_THRESHOLD 同值；测试里显式写死 ⇒ 改配置要改这里）
_THRESHOLD = 0.095

_DATES = pd.to_datetime(["2026-11-02", "2026-11-03", "2026-11-04", "2026-11-05", "2026-11-06"])


def _long_stock_panel() -> pd.DataFrame:
    """长表夹具：A 正常 / B 停牌 / C 涨停 / D 跌停 / E 差一点涨停。"""
    return pd.DataFrame({
        "date": _DATES,
        "code": ["A", "B", "C", "D", "E"],
        "volume": [1000.0, 0.0, 500.0, 500.0, 500.0],
        "change": [0.01, 0.02, _THRESHOLD, -_THRESHOLD, _THRESHOLD - 0.0001],
    })


def _wide_stock_panel() -> pd.DataFrame:
    """宽表夹具（MultiIndex 列 (标的, 字段)）—— 与长表夹具同口径。"""
    volumes = {"A": [1000.0] * 5, "B": [0.0] * 5, "C": [500.0] * 5,
               "D": [500.0] * 5, "E": [500.0] * 5}
    changes = {"A": [0.01] * 5, "B": [0.02] * 5, "C": [_THRESHOLD] * 5,
               "D": [-_THRESHOLD] * 5, "E": [_THRESHOLD - 0.0001] * 5}
    data, columns = {}, []
    for code in volumes:
        columns.append((code, "$volume"))
        data[(code, "$volume")] = volumes[code]
        columns.append((code, "$change"))
        data[(code, "$change")] = changes[code]
    return pd.DataFrame(data, index=_DATES, columns=pd.MultiIndex.from_tuples(columns))


def _mutated_module(tmp_path: Path, edits: list[tuple[str, str]], tag: str):
    """🔴 把源码**读出来 → 在内存里改 → 写到 tmp_path → import 副本**。"""
    text = LIQUIDITY_SOURCE.read_text(encoding="utf-8")
    for old, new in edits:
        assert old in text, f"变异锚点不存在（源码已变）：{old!r}"
        text = text.replace(old, new)
    target = tmp_path / f"{tag}.py"
    target.write_text(text, encoding="utf-8")
    name = f"mutant_{tag}"
    spec = importlib.util.spec_from_file_location(name, target)
    module = importlib.util.module_from_spec(spec)
    sys.modules[name] = module
    spec.loader.exec_module(module)
    return module


def _flag_series(flags: pd.DataFrame, code: str, name: str) -> pd.Series:
    """取宽表形态下某标的的某标记（列 = MultiIndex（标记, 标的））的整列。"""
    return flags[(name, code)]


def _flag(flags: pd.DataFrame, code: str, name: str) -> bool:
    """取宽表形态下某标的的某标记（夹具里同一标的各日取值一致 ⇒ 取首日）。"""
    series = _flag_series(flags, code, name)
    assert bool(series.all()) or not bool(series.any()), (
        f"🔴 夹具里 {code} 的 {name} 各日不一致 ⇒ 断言不适用"
    )
    return bool(series.iloc[0])


# --------------------------------------------------------------------- ① 停牌


class TestSuspended:
    def test_成交量为零判为停牌且不可买(self) -> None:
        flags = liquidity.mark_stock_tradable(_wide_stock_panel())
        assert _flag(flags, "B", "suspended") is True, "🔴 volume=0 没被判成停牌"
        assert _flag(flags, "B", "tradable") is False, "🔴 停牌股被判成可买 ⇒ 假成交"

    def test_成交量非有限也判为停牌(self) -> None:
        panel = _wide_stock_panel()
        panel[("B", "$volume")] = np.nan
        flags = liquidity.mark_stock_tradable(panel)
        assert _flag(flags, "B", "suspended") is True
        assert _flag(flags, "B", "tradable") is False


# --------------------------------------------------------------------- ② 涨停


class TestLimitUp:
    def test_涨幅触顶判为涨停且不可买(self) -> None:
        flags = liquidity.mark_stock_tradable(_wide_stock_panel())
        assert _flag(flags, "C", "limit_up") is True, "🔴 +9.5% 没被判成涨停"
        assert _flag(flags, "C", "tradable") is False, "🔴 涨停股被判成可买 ⇒ 假成交"

    def test_边界_恰好等于阈值仍判为触及涨停(self) -> None:
        """🔴 ``>=`` 含等号 ⇒ 恰好 9.5% 判**触及**（写成 ``>`` 会让一字板漏判）。"""
        flags = liquidity.mark_stock_tradable(_wide_stock_panel())
        assert _flag(flags, "C", "limit_up") is True
        assert _flag(flags, "E", "limit_up") is False, "🔴 9.49% 被误判成涨停"


# --------------------------------------------------------------------- ③ 跌停


class TestLimitDown:
    def test_跌幅触底判为跌停(self) -> None:
        flags = liquidity.mark_stock_tradable(_wide_stock_panel())
        assert _flag(flags, "D", "limit_down") is True, "🔴 -9.5% 没被判成跌停"

    def test_跌停仍可买但不可卖(self) -> None:
        """🔴 A 股常识：**涨停买不到、跌停卖不掉**。
        ⇒ ``tradable``（= 可买入）在跌停时为 True；卖出侧请用 ``limit_down`` 自判。"""
        flags = liquidity.mark_stock_tradable(_wide_stock_panel())
        assert _flag(flags, "D", "tradable") is True
        sellable = (not _flag(flags, "D", "suspended")) and (not _flag(flags, "D", "limit_down"))
        assert sellable is False, "🔴 跌停股被判成可卖 ⇒ 假成交"

    def test_边界_恰好等于阈值仍判为触及跌停(self) -> None:
        flags = liquidity.mark_stock_tradable(_wide_stock_panel())
        assert _flag(flags, "D", "limit_down") is True
        assert _flag(flags, "E", "limit_down") is False


# --------------------------------------------------------------------- ④ 正常量价


class TestNormal:
    def test_正常量价可买(self) -> None:
        flags = liquidity.mark_stock_tradable(_wide_stock_panel())
        for name in ("suspended", "limit_up", "limit_down"):
            assert _flag(flags, "A", name) is False, f"🔴 正常票被判成了 {name}"
        assert _flag(flags, "A", "tradable") is True, "🔴 正常量价被判成不可买"

    def test_长表与宽表口径一致(self) -> None:
        long_flags = liquidity.mark_stock_tradable(_long_stock_panel())
        wide_flags = liquidity.mark_stock_tradable(_wide_stock_panel())
        for code in ["A", "B", "C", "D", "E"]:
            for name in liquidity.FLAG_COLUMNS:
                long_val = bool(long_flags.loc[long_flags["code"] == code, name].iloc[0])
                wide_vals = [bool(v) for v in _flag_series(wide_flags, code, name).tolist()]
                assert all(v == long_val for v in wide_vals), (
                    f"🔴 长表与宽表在 {code}/{name} 上口径不一致："
                    f"长表 {long_val} vs 宽表 {wide_vals}"
                )

    def test_长表返回原结构加四个标记列(self) -> None:
        panel = _long_stock_panel()
        flags = liquidity.mark_stock_tradable(panel)
        assert len(flags) == len(panel)
        for name in liquidity.FLAG_COLUMNS:
            assert name in flags.columns
            assert flags[name].dtype == bool

    def test_扁平宽表必须报错(self) -> None:
        """🔴 扁平宽表放不下 volume 与 change ⇒ 直接报错，不许猜。"""
        with pytest.raises(ValueError):
            liquidity.mark_stock_tradable(pd.DataFrame([[1.0, 2.0]], columns=["A", "B"]))


# --------------------------------------------------------------------- ⑤ 候选过滤


class TestFilterCandidates:
    def test_不可交易的候选被剔除并给出原因(self) -> None:
        kept, dropped = liquidity.filter_candidates_by_tradability(
            ["A", "B"], {"A": True, "B": False},
        )
        assert kept == ["A"]
        assert dropped == [{"code": "B", "reason": "untradable"}]

    def test_没有可交易数据的候选按不可交易处理(self) -> None:
        """🔴 没有行情 ⇒ 不许下单（保守口径）。"""
        kept, dropped = liquidity.filter_candidates_by_tradability(
            ["A", "Z"], {"A": True},
        )
        assert kept == ["A"]
        assert dropped == [{"code": "Z", "reason": "no_tradability_data"}]

    def test_关掉剔除时保留全部但照样给明细(self) -> None:
        kept, dropped = liquidity.filter_candidates_by_tradability(
            ["A", "B"], {"A": True, "B": False}, drop_on_untradable=False,
        )
        assert kept == ["A", "B"]
        assert [d["code"] for d in dropped] == ["B"]


# --------------------------------------------------------------------- ⑥ ETF 侧


class TestEtfSide:
    def test_ETF涨跌幅由收盘价推导并判涨停(self) -> None:
        etf = pd.DataFrame({
            "date": pd.to_datetime(["2026-11-02", "2026-11-03", "2026-11-04"]),
            "code": ["510300", "510300", "510300"],
            "close": [1.00, 1.10, 1.00],
            "volume": [100.0, 100.0, 100.0],
            "amount": [100.0, 110.0, 100.0],
        })
        flags = liquidity.mark_etf_tradable(etf)
        assert len(flags) == len(etf), "🔴 ETF 侧必须同结构返回"
        assert bool(flags.loc[1, "limit_up"]) is True, "🔴 ETF +10% 没被判成涨停"
        assert bool(flags.loc[1, "tradable"]) is False
        assert bool(flags.loc[2, "tradable"]) is True

    def test_ETF成交量为零判为停牌(self) -> None:
        etf = pd.DataFrame({
            "date": pd.to_datetime(["2026-11-02", "2026-11-03"]),
            "code": ["510300", "510300"],
            "close": [1.00, 1.00],
            "volume": [100.0, 0.0],
            "amount": [100.0, 0.0],
        })
        flags = liquidity.mark_etf_tradable(etf)
        assert bool(flags.loc[1, "suspended"]) is True
        assert bool(flags.loc[1, "tradable"]) is False


# --------------------------------------------------------------------- ⑦ 变异反证


def test_变异反证_阈值比较改成严格大于_边界票必须漏判(tmp_path: Path) -> None:
    """把 ``>=`` 改成 ``>`` ⇒ 恰好 9.5% 的票**不再**判为触及涨停
    ⇒ 证明第⑤条边界断言有牙。"""
    mutant = _mutated_module(
        tmp_path,
        [("    limit_up = (chg >= limit_threshold).fillna(False)\n",
          "    limit_up = (chg > limit_threshold).fillna(False)\n")],
        "liq_strict_gt",
    )
    flags = mutant.mark_stock_tradable(_wide_stock_panel())
    assert _flag(flags, "C", "limit_up") is False, (
        "🔴 变异后边界票仍被判成涨停 ⇒ 这条断言没有牙"
    )


def test_变异反证_可买入公式被写反_正常票必须变成不可买(tmp_path: Path) -> None:
    """把 ``tradable = 不停牌 且 不涨停`` 写反 ⇒ 正常票必须变成不可买
    ⇒ 证明第④条断言有牙。"""
    mutant = _mutated_module(
        tmp_path,
        [("    tradable = (~suspended) & (~limit_up)\n",
          "    tradable = suspended | limit_up\n")],
        "liq_tradable_flip",
    )
    flags = mutant.mark_stock_tradable(_wide_stock_panel())
    assert _flag(flags, "A", "tradable") is False, (
        "🔴 变异后正常票仍被判成可买 ⇒ 这条断言没有牙"
    )
    real = liquidity.mark_stock_tradable(_wide_stock_panel())
    assert _flag(real, "A", "tradable") is True
