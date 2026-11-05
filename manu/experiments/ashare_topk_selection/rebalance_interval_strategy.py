"""带「调仓周期」的 Top-K 策略（自研扩展层，只在 `manu/` 下）。

## 为什么需要它

qlib 原生 `TopkDropoutStrategy`（`qlib/contrib/strategy/signal_strategy.py:196-230`）
**每个交易日**都会算一次「卖哪些 / 买哪些」：
    last  = 当前持仓，按信号分数降序                        (:196)
    today = **不在 last 里**的候选中分数最高的
            `n_drop + topk - len(last)` 只                 (:199-202)
    comb  = (last ∪ today) 按分数降序                       (:215)
    sell  = last ∩ (comb 里分数最低的 n_drop 只)            (:219)
    buy   = today 的前 `len(sell) + topk - len(last)` 只    (:230)
⇒ 换手与 `n_drop` **同向**（`n_drop` 越大换手越高）。

⚠️ **「每天买 n_drop 只」不是恒等式**：买的数量是 `len(sell) + topk - len(last)`，
只有在**稳态**（持仓数 = `topk` 且实际卖出 `n_drop` 只）时才**恰好等于** `n_drop`；
一旦卖出的只数少于 `n_drop`（例如被 `hold_thresh` 挡掉、持仓数不足 `topk`），
买的只数会**多于** `n_drop` ⇒ 「买卖各 n_drop 只」只是稳态近似。

⚠️ `hold_thresh`（最短持有期）**只挡卖、不挡买**（源码 `:242` 的检查只在 sell 循环里）
⇒ 持仓数可能略高于 `topk`。

🔴 `n_drop=0` **不是「买入持有」而是退化臂**：`only_tradable=False` 分支里
`get_last_n(li, 0) = list(li)[-0:]` 返回**整个列表** ⇒ 每天清仓、次日全量重建。
本工程实测（test 段 2024-01-02~2026-11-03，`topk=30 / n_drop=5`）：
**稳态日均单边换手 0.1646、年化 41.1 倍**，显式费率把年化超额从 +6.40% 打到 +0.35%。

本模块提供两个**只改组合构造、不动信号与模型**的降换手手段：

1. `rebalance_every`：每 N 个交易日才调仓一次，其余交易日**零交易**（持仓不动）；
2. `hold_thresh`：最短持有期（qlib 原生参数，父类已支持）—— 持有不足 N 个 bar 的票不许卖。

## 不引入未来函数

调仓日仍然走父类逻辑，信号取**上一个交易日**的分数
（父类 `generate_trade_decision` 用 `get_step_time(trade_step, shift=1)`，
`qlib/backtest/utils.py:102` 的 shift=1 = 往前一个 bar）
⇒ 与原生策略**同口径**，不存在「用当天收盘信息选股再按当天收盘成交」。

## 用法

```python
from rebalance_interval_strategy import TopkRebalanceIntervalStrategy

strategy = TopkRebalanceIntervalStrategy(
    signal=signal_df,           # DataFrame: index=(datetime, instrument), 列含 'score'
    topk=30,
    n_drop=30,                  # n_drop=topk ⇒ 调仓日做「全量刷新到 top-K」
    hold_thresh=1,
    rebalance_every=10,          # 每 10 个交易日调一次仓
)
```
`qlib.backtest.backtest()` 的 `strategy` 形参支持**直接传实例**
（`qlib/backtest/__init__.py:209` → `init_instance_by_config(strategy, accept_types=BaseStrategy)`
命中 `qlib/utils/mod.py:215` 的 `isinstance(config, accept_types)` 分支）。
"""
from __future__ import annotations

from qlib.backtest.decision import TradeDecisionWO
from qlib.contrib.strategy.signal_strategy import TopkDropoutStrategy


class TopkRebalanceIntervalStrategy(TopkDropoutStrategy):
    """在 `TopkDropoutStrategy` 之上增加「每 N 个交易日才调仓一次」。

    参数
    ----
    rebalance_every:
        调仓间隔（交易日数）。`1` = 与原生策略完全一致（每天调仓）。
        `N > 1` 时，只在 ``trade_step % N == 1 % N`` 的交易日做决策，
        其余交易日返回空决策 ⇒ **零交易、持仓不变**。
        取 `1 % N` 作为相位，是为了让**第一次建仓仍落在 step 1**
        （step 0 没有前一日信号，父类 `shift=1` 会取到区间外），与原生策略对齐。

    其余参数（`topk` / `n_drop` / `method_sell` / `method_buy` / `hold_thresh` /
    `only_tradable` / `forbid_all_trade_at_limit`）语义与父类**完全一致**。
    """

    def __init__(self, rebalance_every: int = 1, **kwargs):
        rebalance_every = int(rebalance_every)
        if rebalance_every < 1:
            raise ValueError(f"rebalance_every 必须是 >= 1 的整数，收到 {rebalance_every!r}")
        super().__init__(**kwargs)
        self.rebalance_every = rebalance_every

    def generate_trade_decision(self, execute_result=None):
        """非调仓日直接返回空决策（不下单 ⇒ 不产生任何交易成本）。"""
        trade_step = self.trade_calendar.get_trade_step()
        phase = 1 % self.rebalance_every
        if trade_step % self.rebalance_every != phase:
            return TradeDecisionWO([], self)
        return super().generate_trade_decision(execute_result)
