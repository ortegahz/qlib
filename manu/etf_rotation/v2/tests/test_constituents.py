"""🔴 V2 成分股 / 权重口径（对齐②）的回归测试。

**事故背景（为什么要钉这些）**：V1/V2 的穿透打分 ``E = W × S`` 里，``W``（成分股权重）
本地**只有一个快照日**（2026-11-04）⇒ 回测用 2026 的权重倒推历史。
这类结构性前视如果被**沉默**处理 ⇒ 报告里的「历史真实成分股」是假的。
所以本模块必须做到：

  ① 成分表能被**正确解析**（代码 → 权重，键值不许错位）；
  ② 快照日可枚举、**可指定**（默认取最新）；
  ③ ``weight_pct <= 0`` 的行**必须剔除**（负权重不是持仓）；
  ④ 表读不出东西时**直接报错**（不许返回空 dict 让下游算出 0/0）；
  ⑤ ``lookahead_bias_bound`` 的 ``is_true_history`` **恒为 False**
     （本地没有历史 PCF ⇒ 只��代理量，**禁止编造历史快照**）。

【变异反证】沿用本仓库既有写法：源码 → 内存改 → tmp_path → import 副本
⇒ 改坏关键判定 ⇒ 对应断言**必须转红**。⛔ **绝不落盘改真源码**。
"""

from __future__ import annotations

import importlib.util
import sys
from pathlib import Path

import pytest

#: `manu/etf_rotation/v2/` 目录（``constituents`` 用扁平导入 ``config_v2``）
_V2_DIR = Path(__file__).resolve().parents[1]
if str(_V2_DIR) not in sys.path:
    sys.path.insert(0, str(_V2_DIR))

import constituents  # noqa: E402

#: 被测模块源码（**只读**）
CONSTITUENTS_SOURCE = _V2_DIR / "constituents.py"

#: 🔴 夹具成分表（**刻意造出** 旧快照日 / 负权重 / 商品行 三种坑）
_FIXTURE_CSV = """etf_code,etf_name,stock_code,stock_name,weight_pct,source,fetch_date,holding_type
510300,沪深300ETF,600000.SH,浦发银行,40.0,tx,2026-11-04,stock
510300,沪深300ETF,000001.SZ,平安银行,20.0,tx,2026-11-04,stock
510300,沪深300ETF,300857.SZ,协创数据,15.0,tx,2026-11-04,stock
510300,沪深300ETF,600519.SH,贵州茅台,10.0,tx,2026-11-04,stock
510300,沪深300ETF,601398.SH,工商银行,5.0,tx,2026-11-04,stock
510300,沪深300ETF,000651.SZ,格力电器,3.0,tx,2026-11-04,stock
510300,沪深300ETF,600036.SH,招商银行,-2.0,tx,2026-11-04,stock
510500,中证500ETF,600000.SH,浦发银行,30.0,pcf,2026-11-04,stock
510500,中证500ETF,000001.SZ,平安银行,12.0,pcf,2026-11-04,stock
510500,中证500ETF,Au9999,黄金9999,105.0,pcf,2026-11-04,other
510300,沪深300ETF,600519.SH,贵州茅台,35.0,tx,2025-06-30,stock
"""

#: 510300 在 2026-11-04 快照里的**正权重**合计（40+20+15+10+5+3）
_WEIGHT_510300 = 93.0
#: 510500 在 2026-11-04 快照里的正权重合计（30+12+105）
_WEIGHT_510500 = 147.0


def _write_fixture(tmp_path: Path, text: str = _FIXTURE_CSV) -> Path:
    path = tmp_path / "etf_holdings.csv"
    path.write_text(text, encoding="utf-8")
    return path


def _mutated_module(tmp_path: Path, edits: list[tuple[str, str]], tag: str):
    """🔴 把源码**读出来 → 在内存里改 → 写到 tmp_path → import 副本**。"""
    text = CONSTITUENTS_SOURCE.read_text(encoding="utf-8")
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


# --------------------------------------------------------------------- ① 解析


class TestParsing:
    """夹具成分表能被正确解析。"""

    def test_代码到权重的映射正确(self, tmp_path: Path) -> None:
        snapshot = constituents.load_holdings(_write_fixture(tmp_path))
        weights = snapshot.weights["510300"]
        assert weights["600000.SH"] == 40.0
        assert weights["000001.SZ"] == 20.0
        assert weights["300857.SZ"] == 15.0
        assert len(weights) == 6, f"🔴 510300 的成分数不对：{weights}"

    def test_快照日与来源计数正确(self, tmp_path: Path) -> None:
        snapshot = constituents.load_holdings(_write_fixture(tmp_path))
        assert snapshot.snapshot_date == "2026-11-04"
        # 🔴 source_counts 数的是**成分行数**（pcf 那 3 行含黄金 ETF 的商品行 Au9999）
        assert snapshot.source_counts == {"tx": 6, "pcf": 3}

    def test_权重非正的行被剔除(self, tmp_path: Path) -> None:
        """🔴 ``weight_pct = -2.0`` 的行不是持仓 ⇒ 必须剔除
        （留着会让总权重被抵消 ⇒ 覆盖率算错）。"""
        snapshot = constituents.load_holdings(_write_fixture(tmp_path))
        assert "600036.SH" not in snapshot.weights["510300"]
        assert sum(snapshot.weights["510300"].values()) == pytest.approx(_WEIGHT_510300)

    def test_默认取最新快照(self, tmp_path: Path) -> None:
        snapshot = constituents.load_holdings(_write_fixture(tmp_path))
        assert snapshot.snapshot_date == "2026-11-04"
        assert "600519.SH" in snapshot.weights["510300"]
        assert snapshot.weights["510300"]["600519.SH"] == 10.0


# --------------------------------------------------------------------- ② 快照日


class TestSnapshotDates:
    """多快照日的枚举与指定。"""

    def test_快照日升序去重(self, tmp_path: Path) -> None:
        dates = constituents.available_snapshot_dates(_write_fixture(tmp_path))
        assert dates == ["2025-06-30", "2026-11-04"]

    def test_可以指定历史快照日(self, tmp_path: Path) -> None:
        snapshot = constituents.load_holdings(_write_fixture(tmp_path),
                                             snapshot_date="2025-06-30")
        assert snapshot.snapshot_date == "2025-06-30"
        assert snapshot.weights["510300"] == {"600519.SH": 35.0}
        assert "510500" not in snapshot.weights

    def test_指定不存在的快照日必须报错(self, tmp_path: Path) -> None:
        with pytest.raises(ValueError):
            constituents.load_holdings(_write_fixture(tmp_path),
                                       snapshot_date="1999-01-01")


# --------------------------------------------------------------------- ③ 空表


class TestEmptyTable:
    """🔴 读不出东西 ⇒ 直接报错（不许返回空 dict 蒙混）。"""

    def test_零字节文件必须报错(self, tmp_path: Path) -> None:
        path = tmp_path / "empty.csv"
        path.write_text("", encoding="utf-8")
        with pytest.raises(ValueError):
            constituents.load_holdings(path)

    def test_只有表头的文件必须报错(self, tmp_path: Path) -> None:
        path = tmp_path / "header_only.csv"
        path.write_text(
            "etf_code,etf_name,stock_code,stock_name,weight_pct,source,fetch_date\n",
            encoding="utf-8",
        )
        with pytest.raises(ValueError):
            constituents.load_holdings(path)

    def test_全是非正权重的文件必须报错(self, tmp_path: Path) -> None:
        path = tmp_path / "non_positive.csv"
        path.write_text(
            "etf_code,etf_name,stock_code,stock_name,weight_pct,source,fetch_date\n"
            "510300,沪深300ETF,600000.SH,浦发银行,0.0,tx,2026-11-04\n",
            encoding="utf-8",
        )
        with pytest.raises(ValueError):
            constituents.load_holdings(path)


# --------------------------------------------------------------------- ④ 覆盖率


class TestCoverage:
    """覆盖率 = **已打分权重 / 总权重**（V1 同口径）。"""

    def test_按qlib代码匹配(self, tmp_path: Path) -> None:
        snapshot = constituents.load_holdings(_write_fixture(tmp_path))
        assert constituents.coverage("510300", snapshot, {"SZ000001"}) == pytest.approx(
            20.0 / _WEIGHT_510300
        )
        assert constituents.coverage("510300", snapshot, {"SZ300857"}) == pytest.approx(
            15.0 / _WEIGHT_510300
        )

    def test_按原始代码也能匹配(self, tmp_path: Path) -> None:
        snapshot = constituents.load_holdings(_write_fixture(tmp_path))
        assert constituents.coverage("510300", snapshot, {"000001.SZ"}) == pytest.approx(
            20.0 / _WEIGHT_510300
        )

    def test_商品行计入分母但永远打不上分(self, tmp_path: Path) -> None:
        """🔴 ``Au9999``（黄金 ETF 的商品行）认不出 qlib instrument
        ⇒ 永不计入已打分权重，但**照旧计入总权重**（与 V1 同口径）。"""
        snapshot = constituents.load_holdings(_write_fixture(tmp_path))
        assert constituents.coverage("510500", snapshot, {"SZ000001"}) == pytest.approx(
            12.0 / _WEIGHT_510500
        )

    def test_打分池为空则覆盖率为零(self, tmp_path: Path) -> None:
        snapshot = constituents.load_holdings(_write_fixture(tmp_path))
        assert constituents.coverage("510300", snapshot, set()) == 0.0

    def test_不在快照里的ETF覆盖率为零(self, tmp_path: Path) -> None:
        snapshot = constituents.load_holdings(_write_fixture(tmp_path))
        assert constituents.coverage("999999", snapshot, {"SZ000001"}) == 0.0


# --------------------------------------------------------------------- ⑤ 前视偏差


class TestLookaheadBiasBound:
    """🔴 前视偏差只给**代理量**，``is_true_history`` 恒为 False。"""

    def test_恒为非真实历史(self, tmp_path: Path) -> None:
        snapshot = constituents.load_holdings(_write_fixture(tmp_path))
        bound = constituents.lookahead_bias_bound(snapshot)
        assert bound["is_true_history"] is False, (
            "🔴 本地只有单快照日（2026-11-04），历史成分不可观测 ⇒ is_true_history 恒为 False"
        )

    def test_必须含快照日与代理口径说明(self, tmp_path: Path) -> None:
        snapshot = constituents.load_holdings(_write_fixture(tmp_path))
        bound = constituents.lookahead_bias_bound(snapshot)
        assert bound["snapshot_date"] == "2026-11-04"
        assert bound["n_etfs"] == 2
        assert "代理" in bound["proxy_note"]
        assert "is_true_history" in bound["proxy_note"]

    def test_最大权重与前N大集中度(self, tmp_path: Path) -> None:
        snapshot = constituents.load_holdings(_write_fixture(tmp_path))
        bound = constituents.lookahead_bias_bound(snapshot)
        assert bound["max_weight_pct"] == pytest.approx(105.0)
        # n_candidates=2 ⇒ 510500 的前 2 大（105 + 30）占其总权重 ⇒ 上界
        assert bound["top_weight_concentration"] == pytest.approx(1.0)
        tight = constituents.lookahead_bias_bound(snapshot, n_candidates=2)
        assert tight["top_weight_concentration"] == pytest.approx(135.0 / _WEIGHT_510500)

    def test_口径披露文本可读(self) -> None:
        note = constituents.caliber_note()
        assert "2026-11-04" in note
        assert "历史" in note


# --------------------------------------------------------------------- ⑥ 变异反证


def test_变异反证_正权重过滤改成恒等_负权重行必须复活(tmp_path: Path) -> None:
    """把「只留正权重」改成恒等 ⇒ ``-2.0`` 的行必须回来 ⇒ 证明第①条断言有牙。"""
    mutant = _mutated_module(
        tmp_path,
        [("    return df.loc[weights > 0].copy()\n", "    return df.copy()\n")],
        "cons_no_weight_filter",
    )
    path = _write_fixture(tmp_path)
    snapshot = mutant.load_holdings(path)
    assert "600036.SH" in snapshot.weights["510300"], (
        "🔴 变异后负权重行仍未复活 ⇒ 这条断言没有牙"
    )


def test_变异反证_qlib代码归一被摘掉_按instrument匹配必须失效(tmp_path: Path) -> None:
    """把「归一成 qlib instrument」摘掉 ⇒ 按 instrument 匹配的覆盖率必须掉成 0
    ⇒ 证明「qlib 代码形态也能对上」这条断言有牙。"""
    mutant = _mutated_module(
        tmp_path,
        [("        instrument = _to_qlib_instrument(code)\n",
          "        instrument = None\n")],
        "cons_no_instrument_norm",
    )
    path = _write_fixture(tmp_path)
    snapshot = mutant.load_holdings(path)
    assert mutant.coverage("510300", snapshot, {"SZ300857"}) == 0.0, (
        "🔴 变异后 instrument 形态仍能匹配上 ⇒ 这条断言没有牙"
    )
    # 真模块：同一份数据必须匹配得上
    real = constituents.load_holdings(path)
    assert constituents.coverage("510300", real, {"SZ300857"}) == pytest.approx(
        15.0 / _WEIGHT_510300
    )
