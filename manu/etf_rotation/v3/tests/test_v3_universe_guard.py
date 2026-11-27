"""🔴 V3 训练池 / 规模档（对齐②）的回归测试。

【本文件钉的判定】
  ① **代码转换口径与 V2 逐位一致**
     （`to_qlib_instrument` ⇔ `v2/constituents.py::_to_qlib_instrument`）；
  ② **并集 / ΣW 正确**（合成样本：重复行求和、转换失败剔除、空候选回退）；
  ③ **规模档是时变的**（临时 instruments 文件 ⇒ 同一只票不同年份换档）；
  ④ **档位最严优先**（同时在 csi300 与 csi500 ⇒ 记 0）；
  ⑤ ``write_instruments_file`` **可读回**；
  ⑥ **fail-closed**：文件缺失 ⇒ raise；池太小 ⇒ raise。

【变异反证】关键判定改坏 ⇒ 对应断言必须转红（🔴 **绝不落盘改真源码**）。
"""

from __future__ import annotations

import sys
from pathlib import Path

import pandas as pd
import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import universe  # noqa: E402
from conftest import load_v2_module  # noqa: E402

#: 🔴 **对拍样本**：带后缀 / 无后缀 / 港股 / 商品行 / 空白 / 小写 / 脏前缀
_CODE_SAMPLES = (
    "300857.SZ",
    "600000.SH",
    "688981.SH",
    "000001.SZ",
    "09988.BJ",
    "430047.BJ",
    "SHAU",
    "Au9999",
    "600000",
    "300857",
    "1234",
    "",
    "  600000.sh  ",
    "600000.sz",
)


def test_代码转换口径与V2逐位一致() -> None:
    """🔴 与 V2 **逐位一致**（V2 只读加载，绝不改它）。"""
    v2 = load_v2_module("v2_constituents_ref", "constituents.py")
    for code in _CODE_SAMPLES:
        assert universe.to_qlib_instrument(code) == v2._to_qlib_instrument(code), code
    # 🔴 关键口径单独钉死（读的人一眼能看到「无后缀 ⇒ 补市场前缀」这件事）
    assert universe.to_qlib_instrument("300857.SZ") == "SZ300857"
    assert universe.to_qlib_instrument("600000.SH") == "SH600000"
    assert universe.to_qlib_instrument("SHAU") is None
    assert universe.to_qlib_instrument("") is None
    assert universe.to_qlib_instrument(None) is None


def test_并集与ΣW正确() -> None:
    """合成样本：同 (ETF, 股票) 多行求和；转换失败不进 ΣW；instrument 置空。"""
    holdings = pd.DataFrame(
        {
            "etf_code": ["510300", "510300", "510300", "159915", "159915"],
            "stock_code": ["600000.SH", "600000.SH", "000001.SZ", "300857.SZ", "SHAU"],
            "weight_pct": [10.0, 5.0, 20.0, 7.5, 1.0],
        }
    )
    union_df, sum_w = universe.build_etf_constituent_union(["510300", "159915"], holdings)
    assert list(union_df.columns) == ["etf_code", "stock_code", "instrument", "weight_pct"]
    weights = dict(zip(union_df["stock_code"], union_df["weight_pct"]))
    assert weights["600000.SH"] == 15.0  # 🔴 重复行权重求和
    assert weights["000001.SZ"] == 20.0
    assert sum_w == {"SH600000": 15.0, "SZ000001": 20.0, "SZ300857": 7.5}
    # 🔴 商品行转换失败 ⇒ instrument 置空、**不计入 ΣW**
    row = union_df[union_df["stock_code"] == "SHAU"].iloc[0]
    assert row["instrument"] == ""
    assert "SHAU" not in sum_w
    assert union_df.attrs["used_fallback"] is False


def test_空候选ETF回退到全部成分() -> None:
    """🔴 ``etf_codes`` 为空 ⇒ 用 holdings 全部 ETF，并标注 ``used_fallback``。"""
    holdings = pd.DataFrame(
        {
            "etf_code": ["510300", "159915"],
            "stock_code": ["600000.SH", "300857.SZ"],
            "weight_pct": [10.0, 7.5],
        }
    )
    union_df, sum_w = universe.build_etf_constituent_union([], holdings)
    assert union_df.attrs["used_fallback"] is True
    assert union_df["etf_code"].nunique() == 2
    assert set(sum_w) == {"SH600000", "SZ300857"}


def test_规模档时变与最严优先(tmp_path: Path) -> None:
    """🔴 同一只票**不同年份换档**；同时命中多档 ⇒ 取最严。"""
    files = {
        "csi300": tmp_path / "csi300.txt",
        "csi500": tmp_path / "csi500.txt",
        "csi1000": tmp_path / "csi1000.txt",
    }
    files["csi300"].write_text("SH600000\t2010-01-01\t2015-12-31\n", encoding="utf-8")
    files["csi500"].write_text(
        "SH600000\t2016-01-01\t2020-12-31\nSH600001\t2016-01-01\t2020-12-31\n",
        encoding="utf-8",
    )
    files["csi1000"].write_text("SH600000\t2021-01-01\t2030-12-31\n", encoding="utf-8")
    dates = ["2014-06-30", "2018-06-29", "2024-06-28"]
    panel = universe.size_tier_panel(["SH600000", "SH600001"], dates, files=files)
    assert list(panel.columns) == ["SH600000", "SH600001"]
    assert panel.loc["2014-06-30", "SH600000"] == 0  # csi300
    assert panel.loc["2018-06-29", "SH600000"] == 1  # csi500
    assert panel.loc["2024-06-28", "SH600000"] == 2  # csi1000
    assert panel.loc["2018-06-29", "SH600001"] == 1
    assert panel.loc["2014-06-30", "SH600001"] == 3  # 不在任何档 ⇒ 基准档
    assert panel.loc["2024-06-28", "SH600001"] == 3


def test_规模档最严优先(tmp_path: Path) -> None:
    """🔴 同时在 csi300 与 csi500 ⇒ 记 0（csi300 更严）。"""
    files = {
        "csi300": tmp_path / "csi300.txt",
        "csi500": tmp_path / "csi500.txt",
        "csi1000": tmp_path / "csi1000.txt",
    }
    files["csi300"].write_text("SH600000\t2010-01-01\t2030-12-31\n", encoding="utf-8")
    files["csi500"].write_text("SH600000\t2010-01-01\t2030-12-31\n", encoding="utf-8")
    files["csi1000"].write_text("", encoding="utf-8")
    panel = universe.size_tier_panel(["SH600000"], ["2020-01-02"], files=files)
    assert panel.loc["2020-01-02", "SH600000"] == 0


def test_规模档空股票池不报错() -> None:
    """🔴 空 instruments ⇒ 空面板，不报错。"""
    panel = universe.size_tier_panel([], ["2020-01-02"])
    assert panel.empty
    assert list(panel.columns) == []


def test_写instruments文件可读回(tmp_path: Path) -> None:
    """🔴 写出的文件能被读回（qlib 可读格式：一行一个代码）。"""
    target = tmp_path / "nested" / "instruments.txt"
    written = universe.write_instruments_file(target, ["SH600000", "SZ300857"])
    assert written == target
    assert target.read_text(encoding="utf-8").splitlines() == ["SH600000", "SZ300857"]


def test_候选池列名容忍与缺失即报错(tmp_path: Path) -> None:
    """🔴 列名差异（code / fund_code）都能读；文件缺失 ⇒ raise。"""
    for column in ("code", "fund_code", "etf_code", "symbol"):
        path = tmp_path / f"pool_{column}.csv"
        path.write_text(f"{column},name\n510300,沪深300ETF\n159915,创业板ETF\n", encoding="utf-8")
        assert universe.load_etf_universe(path) == ["159915", "510300"]
    with pytest.raises(FileNotFoundError):
        universe.load_etf_universe(tmp_path / "不存在.csv")


def test_候选池标志列口径(tmp_path: Path) -> None:
    """🔴 与每日链同口径：优先 ``fetch_candidate``，退到 ``in_universe``，再退到全表。"""
    path = tmp_path / "pool.csv"
    path.write_text(
        "code,fetch_candidate,in_universe\n510300,True,True\n159915,False,True\n"
        "511880,False,False\n",
        encoding="utf-8",
    )
    assert universe.load_etf_universe(path) == ["510300"]
    path.write_text("code,in_universe\n510300,True\n159915,False\n", encoding="utf-8")
    assert universe.load_etf_universe(path) == ["510300"]
    path.write_text("code\n510300\n159915\n", encoding="utf-8")
    assert universe.load_etf_universe(path) == ["159915", "510300"]


def test_成分股表只留正权重(tmp_path: Path) -> None:
    """🔴 ``weight_pct <= 0`` 的行必须剔除（负权重不是持仓）。"""
    path = tmp_path / "holdings.csv"
    path.write_text(
        "etf_code,stock_code,weight_pct\n510300,600000.SH,10.0\n"
        "510300,000001.SZ,-1.0\n510300,300857.SZ,0.0\n",
        encoding="utf-8",
    )
    frame = universe.load_holdings(path)
    assert list(frame.columns) == ["etf_code", "stock_code", "weight_pct"]
    assert frame["stock_code"].tolist() == ["600000.SH"]
    with pytest.raises(FileNotFoundError):
        universe.load_holdings(tmp_path / "不存在.csv")


def test_训练池只数不足即报错(monkeypatch: pytest.MonkeyPatch) -> None:
    """🔴 ``n_stocks < MIN_UNIVERSE_SIZE`` ⇒ raise（fail-closed）。"""
    import config_v3

    holdings = pd.DataFrame(
        {
            "etf_code": ["510300"],
            "stock_code": ["600000.SH"],
            "weight_pct": [10.0],
        }
    )
    monkeypatch.setattr(config_v3, "MIN_UNIVERSE_SIZE", 300)
    with pytest.raises(ValueError):
        universe.build_universe(["510300"], holdings, mode=config_v3.UNIVERSE_MODE_FULL_UNION)
    monkeypatch.setattr(config_v3, "MIN_UNIVERSE_SIZE", 1)
    result = universe.build_universe(["510300"], holdings, mode=config_v3.UNIVERSE_MODE_FULL_UNION)
    assert result["n_stocks"] == 1
    assert result["instruments"] == ["SH600000"]
    assert result["sum_w"] == {"SH600000": 10.0}
    assert result["mode"] == config_v3.UNIVERSE_MODE_FULL_UNION
    assert result["holdings_snapshot_date"] == config_v3.HOLDINGS_SNAPSHOT_DATE


def test_大中盘口径_并集与中证800_1000取交集(monkeypatch: pytest.MonkeyPatch, tmp_path) -> None:
    """🔴 **V3 主口径**（``etf_constituent_large``）：ETF 并集 ∩ 中证 800 ∪ 1000。

    ⇒ 用**临时 instruments 文件**（不读服务器路径）⇒ 本机可跑。
    """
    import config_v3

    inst = tmp_path / "csi800.txt"
    inst.write_text("SH600000\t2016-01-01\t2023-12-31\nSH600001\t2016-01-01\t2023-12-31\n",
                    encoding="utf-8")
    inst2 = tmp_path / "csi1000.txt"
    inst2.write_text("SZ600002\t2016-01-01\t2023-12-31\n", encoding="utf-8")
    monkeypatch.setattr(config_v3, "LARGE_CAP_INSTRUMENT_FILES",
                        {"csi800": inst, "csi1000": inst2})
    monkeypatch.setattr(config_v3, "MIN_UNIVERSE_SIZE", 1)
    holdings = pd.DataFrame(
        {
            "etf_code": ["510300", "510300", "510300"],
            "stock_code": ["600000.SH", "600002.SZ", "600003.SZ"],
            "weight_pct": [10.0, 5.0, 7.0],
        }
    )
    result = universe.build_universe(["510300"], holdings, mode=config_v3.UNIVERSE_MODE)
    assert result["mode"] == config_v3.UNIVERSE_MODE
    # SH600000 ∈ csi800、SH600002 ∈ csi1000 ⇒ 两者保留；SH600003 不在任何档 ⇒ 剔除
    assert result["instruments"] == ["SH600000", "SZ600002"]
    assert "SZ600003" not in result["instruments"]
    # 🔴 ΣW 只保留池内标的（被剔除的 SZ600003 不该出现在权重里）
    assert set(result["sum_w"]) == {"SH600000", "SZ600002"}
