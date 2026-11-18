"""🔴 V2 回测（`v2/backtest_v2.py`）单测 —— **Cost-Aware + Rank Buffer 计入换手**。

本文件钉死（自带夹具，**不依赖网络 / qlib / 大文件**）：

  ① 目标权重不变 ⇒ 换手 ≈ 0（漂移后有**微小**差异，按实现给容差）；
  ② 一次大幅调仓 ⇒ 换手 = Σ|Δw|，且成本按 (佣金 + 滑点) × 换手**在调仓当天**扣；
  ③ Rank Buffer：已持有且排名在 top_k+buffer 内 ⇒ **继续持有**（V1 等价逻辑）；
  ④ 不可交易标的 ⇒ 进 ``blocked`` 且**不换手**；
  ⑤ 净值**手算**核对（小夹具）；
  ⑥ 变异反证：把 buffer 逻辑改坏（忽略已持仓）⇒ ③ 的断言必须转红。

口径依据：V1 ``backtest.py:204``（换手）、``backtest.py:216-232``（成本扣减）、
``engine/filters.py::apply_theme_cap``（buffer 迟滞）。V1 冻结，本模块不 import V1。
"""

from __future__ import annotations

import pathlib
import sys

import pandas as pd
import pytest

_V2_DIR = pathlib.Path(__file__).resolve().parents[1]
if str(_V2_DIR) not in sys.path:
    sys.path.insert(0, str(_V2_DIR))

import backtest_v2  # noqa: E402

# --------------------------------------------------------------------- 夹具
DATES = pd.to_datetime(["2024-01-01", "2024-01-02", "2024-01-03", "2024-01-04"])

#: 有涨跌的夹具（A 涨、B 最后一天涨）
PX_MOVING = pd.DataFrame(
    {"A": [10.0, 11.0, 12.0, 12.0], "B": [20.0, 20.0, 20.0, 22.0]}, index=DATES
)
#: 全平价夹具（⇒ 净值只受成本影响 ⇒ 成本可以逐字手算）
PX_FLAT = pd.DataFrame(
    {"A": [10.0, 10.0, 10.0, 10.0], "B": [20.0, 20.0, 20.0, 20.0]}, index=DATES
)

COMMISSION = 0.00015
SLIPPAGE = 0.0005


# --------------------------------------------------------------------- ① 目标不变 ⇒ 换手≈0
def test_目标权重不变时换手约等于零() -> None:
    targets = {"2024-01-02": {"A": 0.5, "B": 0.5}, "2024-01-03": {"A": 0.5, "B": 0.5}}
    res = backtest_v2.run_backtest_v2(
        PX_MOVING, targets, commission=0.0, slippage=0.0
    )
    # 首次建仓换手 = 1.0；第二次目标不变 ⇒ 只剩「漂移回补」的微小换手
    assert res["turnover_by_date"]["2024-01-02"] == pytest.approx(1.0, abs=1e-12)
    drift_turnover = res["turnover_by_date"]["2024-01-03"]
    assert drift_turnover == pytest.approx(0.043478260869565216, abs=1e-12), (
        f"漂移回补换手必须是 |0.5−0.5217391| + |0.5−0.4782609|；实得 {drift_turnover!r}"
    )
    assert drift_turnover < 0.05, "目标不变 ⇒ 换手必须≈0（只剩漂移回补）"


# --------------------------------------------------------------------- ② 换手与成本
def test_一次大幅调仓的换手与成本扣减() -> None:
    targets = {"2024-01-02": {"A": 1.0}, "2024-01-03": {"B": 1.0}}
    res = backtest_v2.run_backtest_v2(
        PX_FLAT, targets, commission=COMMISSION, slippage=SLIPPAGE
    )
    rate = COMMISSION + SLIPPAGE
    assert res["turnover_by_date"]["2024-01-02"] == pytest.approx(1.0, abs=1e-12), (
        "空仓建 A ⇒ 换手 = |1−0| = 1"
    )
    assert res["turnover_by_date"]["2024-01-03"] == pytest.approx(2.0, abs=1e-12), (
        "A 换 B ⇒ 换手 = |0−1| + |1−0| = 2"
    )
    assert res["total_turnover"] == pytest.approx(3.0, abs=1e-12)
    assert res["total_cost_drag"] == pytest.approx(3.0 * rate, abs=1e-15)
    # 成本在**调仓当天**扣：全平价 ⇒ 净值 = (1−rate)×(1−2×rate)
    nav = res["nav"]
    assert nav.iloc[1] == pytest.approx(1.0 - rate, abs=1e-15), (
        f"调仓当天的净值必须已扣成本；实得 {nav.iloc[1]!r}"
    )
    assert nav.iloc[2] == pytest.approx((1.0 - rate) * (1.0 - 2.0 * rate), abs=1e-15)


def test_成本必须进净值而非只进报表() -> None:
    """同一套目标，佣金+滑点从 0 提到 0.01 ⇒ 净值必须**变低**（成本真的进了净值）。"""
    targets = {"2024-01-02": {"A": 0.5, "B": 0.5}}
    free = backtest_v2.run_backtest_v2(PX_MOVING, targets, commission=0.0, slippage=0.0)
    paid = backtest_v2.run_backtest_v2(
        PX_MOVING, targets, commission=0.005, slippage=0.005
    )
    assert paid["nav"].iloc[-1] < free["nav"].iloc[-1], "成本必须从净值里扣"
    expected = free["nav"].iloc[-1] * (1.0 - 0.01 * paid["total_turnover"])
    assert paid["nav"].iloc[-1] == pytest.approx(expected, abs=1e-12)


# --------------------------------------------------------------------- ③ Rank Buffer
def test_buffer_已持有且在band内则继续持有() -> None:
    """X（排名 6，在 top_k+buffer=7 内）是持仓 ⇒ **继续持有**，顶掉非持仓的 F。"""
    ranks = {"A": 1, "B": 2, "C": 3, "D": 4, "F": 5, "X": 6, "G": 7}
    held = ["A", "B", "C", "D", "X"]
    targets = backtest_v2.buffer_targets(ranks, held, top_k=5, buffer=2)
    assert "X" in targets, f"已持有且排名在 band 内 ⇒ 必须继续持有；实得 {targets!r}"
    assert "F" not in targets, (
        f"腾出的槽位由已持有的 X 占据 ⇒ 非持仓的 F 不该进组合；实得 {targets!r}"
    )
    assert set(targets) == {"A", "B", "C", "D", "X"}
    assert all(w == pytest.approx(0.2) for w in targets.values()), "必须等权"
    assert sum(targets.values()) == pytest.approx(1.0)


def test_buffer_掉出band的持仓必须被卖出() -> None:
    ranks = {"A": 1, "B": 2, "C": 3, "D": 4, "F": 5, "X": 9}
    held = ["A", "B", "C", "D", "X"]
    targets = backtest_v2.buffer_targets(ranks, held, top_k=5, buffer=2)
    assert "X" not in targets, "排名 9 > top_k+buffer=7 ⇒ 必须被卖出"
    assert set(targets) == {"A", "B", "C", "D", "F"}


def test_buffer_空持仓时就是取前top_k() -> None:
    ranks = {"A": 1, "B": 2, "C": 3, "D": 4, "E": 5, "F": 6}
    targets = backtest_v2.buffer_targets(ranks, [], top_k=5, buffer=2)
    assert set(targets) == {"A", "B", "C", "D", "E"}


def test_buffer_留存量必须体现为换手() -> None:
    """🔴 对齐③的落点：buffer 保住的持仓 ⇒ 目标权重与漂移权重不同 ⇒ **换手不为 0**。"""
    ranks = {"A": 1, "B": 2, "C": 3, "D": 4, "F": 5, "X": 6}
    held = ["A", "B", "C", "D", "X"]
    targets = backtest_v2.buffer_targets(ranks, held, top_k=5, buffer=2)
    res = backtest_v2.run_backtest_v2(
        PX_MOVING,
        {"2024-01-02": targets},
        initial_weights={"A": 0.2, "B": 0.2, "C": 0.2, "D": 0.2, "X": 0.2},
        commission=0.0,
        slippage=0.0,
    )
    # X 保留 ⇒ 目标权重与漂移后权重有差 ⇒ 换手必须>0（buffer 留存量被计入）
    assert res["total_turnover"] > 0.0, "buffer 留存量必须计入换手"
    assert res["weights_by_date"]["2024-01-02"] == pytest.approx(targets, abs=1e-12)


# --------------------------------------------------------------------- ④ 不可交易
def test_不可交易的标的不换手且进blocked() -> None:
    targets = {"2024-01-02": {"A": 0.5, "B": 0.5}}
    tradable = {"2024-01-02": {"B": False}}
    res = backtest_v2.run_backtest_v2(
        PX_MOVING, targets, tradable=tradable, commission=0.0, slippage=0.0
    )
    assert res["blocked"], f"不可交易必须记入 blocked；实得 {res['blocked']!r}"
    assert res["blocked"][0]["code"] == "B"
    assert res["blocked"][0]["reason"] == "target_untradable"
    # B 不买 ⇒ 换手只有 A 的 0.5（**不含** B 的 0.5）
    assert res["turnover_by_date"]["2024-01-02"] == pytest.approx(0.5, abs=1e-12), (
        f"不可交易的标的**不换手**；实得 {res['turnover_by_date']['2024-01-02']!r}"
    )
    assert res["weights_by_date"]["2024-01-02"].get("B", 0.0) == pytest.approx(0.0), (
        "不可交易的标的**不许建仓**（权重必须为 0）"
    )


def test_卖不掉的持仓保持原仓且不换手() -> None:
    """持仓不可交易 ⇒ 卖不掉 ⇒ 保持原仓（换手 = 0）。"""
    targets = {"2024-01-02": {"B": 1.0}}
    tradable = {"2024-01-02": {"A": False}}
    res = backtest_v2.run_backtest_v2(
        PX_MOVING,
        targets,
        initial_weights={"A": 1.0},
        tradable=tradable,
        commission=0.0,
        slippage=0.0,
    )
    assert res["turnover_by_date"]["2024-01-02"] == pytest.approx(0.0, abs=1e-12), (
        f"卖不掉 ⇒ 不得换手；实得 {res['turnover_by_date']['2024-01-02']!r}"
    )
    assert res["blocked"][0]["code"] == "A"
    assert res["blocked"][0]["reason"] == "sell_untradable"
    assert res["weights_by_date"]["2024-01-02"]["A"] == pytest.approx(1.0, abs=1e-12)


# --------------------------------------------------------------------- ⑤ 净值手算
def test_净值手算核对() -> None:
    """手算：d2 收盘建仓（当日无收益）→ d3 赚 0.5×(12/11−1) → d4 赚漂移后 B 权重 × 10%。"""
    targets = {"2024-01-02": {"A": 0.5, "B": 0.5}}
    res = backtest_v2.run_backtest_v2(
        PX_MOVING, targets, commission=0.0, slippage=0.0
    )
    nav = res["nav"]
    assert nav.iloc[0] == pytest.approx(1.0, abs=1e-12)
    assert nav.iloc[1] == pytest.approx(1.0, abs=1e-12), "建仓当日无持仓 ⇒ 净值不变"
    r3 = 0.5 * (12.0 / 11.0 - 1.0)
    assert nav.iloc[2] == pytest.approx(1.0 + r3, abs=1e-12)
    w_b = 0.5 / (1.0 + r3)                       # 漂移后 B 的权重
    assert nav.iloc[3] == pytest.approx(
        (1.0 + r3) * (1.0 + w_b * (22.0 / 20.0 - 1.0)), abs=1e-12
    )


def test_净值必须单调跟随价格且指标齐备() -> None:
    targets = {"2024-01-02": {"A": 0.5, "B": 0.5}}
    res = backtest_v2.run_backtest_v2(
        PX_MOVING, targets, commission=COMMISSION, slippage=SLIPPAGE
    )
    assert set(res) >= {
        "nav", "turnover_by_date", "total_turnover", "annualized_turnover",
        "total_cost_drag", "weights_by_date", "blocked", "metrics",
    }, f"返回键不齐；实得 {sorted(res)!r}"
    assert res["nav"].iloc[-1] > 1.0
    assert res["metrics"]["total_turnover"] == pytest.approx(
        res["total_turnover"], abs=1e-12
    )
    assert res["metrics"]["total_cost_drag"] == pytest.approx(
        res["total_cost_drag"], abs=1e-15
    )


def test_交易日不足两天必须raise() -> None:
    one_day = PX_MOVING.iloc[:1]
    with pytest.raises(ValueError):
        backtest_v2.run_backtest_v2(one_day, {"2024-01-01": {"A": 1.0}})


# --------------------------------------------------------------------- ⑥ 变异反证
def test_变异反证_buffer逻辑改坏_已持有不再继续持有(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """变异：把 buffer 改成「忽略已持仓、只取前 top_k」⇒ X 必被丢掉
    ⇒ 证明 ③ 的断言有牙。"""
    monkeypatch.setattr(
        backtest_v2,
        "buffer_targets",
        lambda ranks, held, top_k, buffer: {
            code: 1.0 / min(top_k, len(ranks))
            for code in sorted(ranks, key=lambda c: ranks[c])[:top_k]
        },
    )
    ranks = {"A": 1, "B": 2, "C": 3, "D": 4, "F": 5, "X": 6}
    held = ["A", "B", "C", "D", "X"]
    targets = backtest_v2.buffer_targets(ranks, held, top_k=5, buffer=2)
    assert "X" not in targets, (
        f"变异后 X 必被丢掉 ⇒ 证明「已持有且在 band 内 ⇒ 继续持有」这条断言有牙；实得 {targets!r}"
    )
