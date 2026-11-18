"""🔴 V2 **ETF 层真实流动性约束接线**的回归测试（2026-11-17 夜补）。

**为什么要补这一段**：``liquidity.py`` 早就实现了「停牌 / 涨停 / 跌停」四个标记，
``etf_layer.build_targets`` 也早就留了 ``tradable`` 入参，``backtest_v2`` 也早就
实现了「不可交易 ⇒ 不换手、保持原仓」—— 但 **``run_v2.py`` 从来没把它们接上**
⇒ V2 的 ETF 层回测里**涨停照买、跌停照卖**，是**假成交**。

🔴 **买/卖两侧语义必须不同**（涨跌停的本质，也是最容易写错的地方）：

    ==========  ==========  ==========  ==============================
    情形        可买入      可卖出      物理含义
    ==========  ==========  ==========  ==============================
    停牌        ❌          ❌          没有成交，报单挂不上
    涨停(+10%)  ❌          ✅          买盘排队买不到；但卖单成交容易
    跌停(-10%)  ✅          ❌          卖盘排队卖不掉；但买盘能成交
    正常        ✅          ✅          ——
    ==========  ==========  ==========  ==============================

⇒ **可买** = 不停牌 且 不涨停；**可卖** = 不停牌 且 不跌停。

【变异反证】沿用本仓库既有写法：源码 → 内存改 → tmp_path → import 副本
⇒ 改坏关键判定 ⇒ 对应断言**必须转红**。⛔ **绝不落盘改真源码**。

🔴 本文件**只用自造夹具**（小 DataFrame）⇒ 不联网、不读真实行情、不跑重量级回测。
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

import backtest_v2 as BV  # noqa: E402
import run_v2 as RV  # noqa: E402

BACKTEST_SOURCE = _V2_DIR / "backtest_v2.py"
RUNV2_SOURCE = _V2_DIR / "run_v2.py"

#: 夹具日期（4 天，与 4 只票一一对应）
_DATES = pd.to_datetime(["2026-11-02", "2026-11-03", "2026-11-04", "2026-11-05"])


def _mutated_module(tmp_path: Path, source: Path, edits: list[tuple[str, str]], tag: str):
    """🔴 把源码**读出来 → 在内存里改 → 写到 tmp_path → import 副本**（真源码不动）。"""
    text = source.read_text(encoding="utf-8")
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


def _flags_frame() -> pd.DataFrame:
    """四个标记的长表夹具（**面板形态**：每个交易日都含全部 4 只票）。

    A 正常 / B 停牌 / C 涨停 / D 跌停。
    """
    rows = []
    for code, susp, up, down in (
        ("A", False, False, False),
        ("B", True, False, False),
        ("C", False, True, False),
        ("D", False, False, True),
    ):
        for day in _DATES:
            rows.append({
                "date": day, "code": code,
                "suspended": susp, "limit_up": up, "limit_down": down,
            })
    return pd.DataFrame(rows)


# ----------------------------------------------------------------- ① 买/卖语义


def test_涨停可买映射为假可卖映射为真() -> None:
    """涨停 ⇒ 买不进（可买 False）但卖得掉（可卖 True）。"""
    buy, sell = RV.etf_tradable_maps_from_flags(_flags_frame())
    day = "2026-11-02"
    assert buy[day]["C"] is False, "🔴 涨停票必须**不可买**"
    assert sell[day]["C"] is True, "🔴 涨停票必须**可卖**（卖单成交容易）"


def test_跌停可买映射为真可卖映射为假() -> None:
    """跌停 ⇒ 卖不掉（可卖 False）但买得进（可买 True）。"""
    buy, sell = RV.etf_tradable_maps_from_flags(_flags_frame())
    day = "2026-11-02"
    assert buy[day]["D"] is True, "🔴 跌停票必须**可买**"
    assert sell[day]["D"] is False, "🔴 跌停票必须**不可卖**"


def test_停牌买卖两侧都不可() -> None:
    """停牌 ⇒ 买不了也卖不了。"""
    buy, sell = RV.etf_tradable_maps_from_flags(_flags_frame())
    day = "2026-11-02"
    assert buy[day]["B"] is False
    assert sell[day]["B"] is False


def test_正常票买卖两侧都可() -> None:
    buy, sell = RV.etf_tradable_maps_from_flags(_flags_frame())
    day = "2026-11-02"
    assert buy[day]["A"] is True
    assert sell[day]["A"] is True


def test_映射键是年月日且逐日分组() -> None:
    buy, sell = RV.etf_tradable_maps_from_flags(_flags_frame())
    assert set(buy) == {"2026-11-02", "2026-11-03", "2026-11-04", "2026-11-05"}
    assert set(buy) == set(sell)
    assert set(buy["2026-11-02"]) == {"A", "B", "C", "D"}


def test_空表返回空映射不抛错() -> None:
    buy, sell = RV.etf_tradable_maps_from_flags(pd.DataFrame())
    assert buy == {} and sell == {}


# ----------------------------------------------------------------- ② 回测两侧语义


def _px_two_days() -> pd.DataFrame:
    """两天收盘价宽表：A/B/C 三只票。"""
    idx = pd.to_datetime(["2026-11-02", "2026-11-03"])
    return pd.DataFrame({"A": [10.0, 10.0], "B": [5.0, 5.0], "C": [2.0, 2.0]}, index=idx)


def test_目标里不可买的票不买_保持原仓() -> None:
    """买侧不可交易 ⇒ 不买（记 blocked=target_untradable），其余按比例缩放。"""
    px = _px_two_days()
    targets = {"2026-11-02": {"A": 0.5, "C": 0.5}}
    res = BV.run_backtest_v2(
        px, targets, tradable={"2026-11-02": {"C": False}},
        tradable_sell={"2026-11-02": {"C": True}},
        commission=0.0, slippage=0.0,
    )
    reasons = [r["reason"] for r in res["blocked"]]
    assert "target_untradable" in reasons
    blocked = [r for r in res["blocked"] if r["reason"] == "target_untradable"]
    assert blocked[0]["code"] == "C"


def test_持仓里不可卖的票不卖_保持原仓() -> None:
    """卖侧不可交易 ⇒ 不卖（记 blocked=sell_untradable）。"""
    px = _px_two_days()
    targets = {"2026-11-02": {"A": 1.0}}
    res = BV.run_backtest_v2(
        px, targets, initial_weights={"B": 1.0},
        tradable={"2026-11-02": {"B": True}},
        tradable_sell={"2026-11-02": {"B": False}},
        commission=0.0, slippage=0.0,
    )
    reasons = [r["reason"] for r in res["blocked"]]
    assert "sell_untradable" in reasons
    blocked = [r for r in res["blocked"] if r["reason"] == "sell_untradable"]
    assert blocked[0]["code"] == "B"
    assert blocked[0]["kept_weight"] == pytest.approx(1.0)


def test_涨停票作为持仓时应当卖得掉() -> None:
    """🔴 核心语义：涨停票在**卖侧**是可交易的 ⇒ 不得被记成 sell_untradable。"""
    px = _px_two_days()
    targets = {"2026-11-02": {"A": 1.0}}
    res = BV.run_backtest_v2(
        px, targets, initial_weights={"C": 1.0},
        tradable={"2026-11-02": {"C": False}},          # 涨停 ⇒ 买不进
        tradable_sell={"2026-11-02": {"C": True}},      # 但卖得掉
        commission=0.0, slippage=0.0,
    )
    reasons = [r["reason"] for r in res["blocked"]]
    assert "sell_untradable" not in reasons, (
        "🔴 涨停票被判成卖不掉 ⇒ 买/卖两侧语义被写反了"
    )


def test_不给卖出侧映射时退化复用买入侧_保持向后兼容() -> None:
    """不给 ``tradable_sell`` ⇒ 卖侧退回买侧（既有调用方行为逐字不变）。"""
    px = _px_two_days()
    targets = {"2026-11-02": {"A": 1.0}}
    res = BV.run_backtest_v2(
        px, targets, initial_weights={"B": 1.0},
        tradable={"2026-11-02": {"B": False}},
        commission=0.0, slippage=0.0,
    )
    reasons = [r["reason"] for r in res["blocked"]]
    assert "sell_untradable" in reasons, (
        "🔴 不给 tradable_sell 时卖侧应退回买侧 ⇒ 买不掉的票也卖不掉"
    )


# ----------------------------------------------------------------- ③ 接线


def test_run_v2回测同时传入买卖两套映射(monkeypatch: pytest.MonkeyPatch) -> None:
    """🔴 ``run_v2._backtest`` 必须把 **buy + sell** 两套映射都传下去。"""
    captured: dict = {}

    def _fake_run(prices, targets, **kwargs):
        captured.update(kwargs)
        return {"nav": pd.Series([1.0, 1.0], index=prices.index)}

    monkeypatch.setattr(BV, "run_backtest_v2", _fake_run)
    buy = {"2026-11-02": {"A": True}}
    sell = {"2026-11-02": {"A": False}}
    monkeypatch.setitem(RV._ETF_TRADABLE_CACHE, "maps", (buy, sell))
    px = _px_two_days()
    RV._backtest(px, {"2026-11-02": {"A": 1.0}}, {})
    assert captured.get("tradable") == buy
    assert captured.get("tradable_sell") == sell


def test_run_v2目标构造把可买映射传给etf层(monkeypatch: pytest.MonkeyPatch) -> None:
    """🔴 ``run_v2._build_targets_for`` 必须把 **可买** 映射传给 ``etf_layer.build_targets``。"""
    captured: dict = {}

    def _fake_build_targets(*args, **kwargs):
        captured.update(kwargs)
        return {}, pd.DataFrame()

    monkeypatch.setattr(RV.EL, "build_targets", _fake_build_targets)
    monkeypatch.setitem(RV._ETF_TRADABLE_CACHE, "maps",
                        ({"2026-11-02": {"A": False}}, {"2026-11-02": {"A": True}}))

    class _FakeArgs:
        provider_uri = ""
        signal_stride = 10

    out_dir = Path("/tmp/_v2_wiring_test")
    out_dir.mkdir(parents=True, exist_ok=True)
    scores = out_dir / "stock_scores_v2_fake.csv"
    scores.write_text("date,instrument,score\n2026-11-02,SH600000,0.1\n", encoding="utf-8")

    close = pd.DataFrame({"A": [1.0, 1.0]}, index=pd.to_datetime(["2026-11-02", "2026-11-03"]))
    amount = pd.DataFrame({"A": [1.0, 1.0]}, index=close.index)
    try:
        RV._build_targets_for(
            _FakeArgs(), out_dir, {"_model_name": "fake"},
            close, amount, {}, [pd.Timestamp("2026-11-02")],
        )
    except Exception:      # noqa: BLE001 —— 夹具不足导致的中途异常不算失败，只要映射传到了
        pass
    assert captured.get("tradable") == {"2026-11-02": {"A": False}}, (
        "🔴 可买映射没传到 etf_layer.build_targets ⇒ 流动性约束形同虚设"
    )
    import shutil
    shutil.rmtree(out_dir, ignore_errors=True)


# ----------------------------------------------------------------- ④ 变异反证


def test_变异反证_卖侧改回用买侧_涨停票必须被判卖不掉(tmp_path: Path) -> None:
    """把卖侧判定改回收盘买侧 ⇒ 涨停票会被误判成卖不掉 ⇒ 断言必须转红。"""
    mutant = _mutated_module(
        tmp_path, BACKTEST_SOURCE,
        [("        if sell_map.get(code, True):\n            continue",
          "        if tradable_map.get(code, True):\n            continue")],
        "bt_sell_uses_buy",
    )
    px = _px_two_days()
    res = mutant.run_backtest_v2(
        px, {"2026-11-02": {"A": 1.0}}, initial_weights={"C": 1.0},
        tradable={"2026-11-02": {"C": False}},
        tradable_sell={"2026-11-02": {"C": True}},
        commission=0.0, slippage=0.0,
    )
    reasons = [r["reason"] for r in res["blocked"]]
    assert "sell_untradable" in reasons, (
        "🔴 变异后涨停票仍被判成可卖 ⇒ 这条断言没有牙"
    )
    # 对照：**真源码**下，涨停票必须能卖（不得记 sell_untradable）
    real = BV.run_backtest_v2(
        px, {"2026-11-02": {"A": 1.0}}, initial_weights={"C": 1.0},
        tradable={"2026-11-02": {"C": False}},
        tradable_sell={"2026-11-02": {"C": True}},
        commission=0.0, slippage=0.0,
    )
    assert "sell_untradable" not in [r["reason"] for r in real["blocked"]]


def test_变异反证_可卖映射改用涨停_跌停票必须被判不可卖(tmp_path: Path) -> None:
    """把「可卖」写成「不停牌 且 不涨停」⇒ 跌停票会被误判成可卖 ⇒ 断言必须转红。"""
    mutant = _mutated_module(
        tmp_path, RUNV2_SOURCE,
        [("            c: bool((not s) and (not d)) for c, s, d in zip(codes, suspended, limit_down)",
          "            c: bool((not s) and (not d)) for c, s, d in zip(codes, suspended, limit_up)")],
        "runv2_sell_uses_limitup",
    )
    buy, sell = mutant.etf_tradable_maps_from_flags(_flags_frame())
    # 🔴 变异后：跌停票被判成**可卖**（错误行为）⇒ 这条断言必须成立
    assert sell["2026-11-02"]["D"] is True, (
        "🔴 变异没生效：跌停票仍被判成不可卖 ⇒ 这条断言没有牙"
    )
    # 对照：**真源码**下，跌停票必须不可卖
    _buy2, sell2 = RV.etf_tradable_maps_from_flags(_flags_frame())
    assert sell2["2026-11-02"]["D"] is False


def test_变异反证_回测不传卖出侧_涨停票必须被判卖不掉(tmp_path: Path) -> None:
    """把 ``tradable_sell=sell_map`` 删掉 ⇒ 卖出侧退回买侧 ⇒ 涨停票被误判 ⇒ 断言必须转红。"""
    mutant = _mutated_module(
        tmp_path, RUNV2_SOURCE,
        [("        tradable=buy_map,\n        tradable_sell=sell_map,\n", "        tradable=buy_map,\n")],
        "runv2_no_sell_map",
    )
    captured: dict = {}

    def _fake_run(prices, targets, **kwargs):
        captured.update(kwargs)
        return {"nav": pd.Series([1.0, 1.0], index=prices.index)}

    monkey = BV
    original = monkey.run_backtest_v2
    monkey.run_backtest_v2 = _fake_run
    try:
        mutant._ETF_TRADABLE_CACHE["maps"] = (
            {"2026-11-02": {"A": False}}, {"2026-11-02": {"A": True}},
        )
        px = _px_two_days()
        mutant._backtest(px, {"2026-11-02": {"A": 1.0}}, {})
    finally:
        monkey.run_backtest_v2 = original
    assert "tradable_sell" not in captured, (
        "🔴 变异后卖出侧映射没传下去 ⇒ 两侧语义退化成一张 ⇒ 这条断言没有牙"
    )
    # 对照：**真源码**必须把卖出侧映射传下去
    captured2: dict = {}

    def _fake_run2(prices, targets, **kwargs):
        captured2.update(kwargs)
        return {"nav": pd.Series([1.0, 1.0], index=prices.index)}

    original2 = BV.run_backtest_v2
    BV.run_backtest_v2 = _fake_run2
    try:
        RV._ETF_TRADABLE_CACHE["maps"] = (
            {"2026-11-02": {"A": False}}, {"2026-11-02": {"A": True}},
        )
        RV._backtest(px, {"2026-11-02": {"A": 1.0}}, {})
    finally:
        BV.run_backtest_v2 = original2
    assert captured2.get("tradable_sell") == {"2026-11-02": {"A": True}}
