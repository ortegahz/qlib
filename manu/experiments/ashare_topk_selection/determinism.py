"""qlib 回测的**跨进程确定性**补丁（自研扩展层，只在 `manu/` 下）。

## 问题（2026-11-04 实测发现）

qlib 的 `Position.get_stock_list()`（`qlib/backtest/position.py:422-424`）是：

```python
stock_list = list(set(self.position.keys()) - {"cash", "now_account_value", "cash_delay"})
return stock_list
```

**`set(...)` 的迭代顺序随 Python 字符串哈希随机化（`PYTHONHASHSEED`）逐进程变化**
（Python 默认每个进程随机种子）。

而 `TopkDropoutStrategy.generate_trade_decision` 里：
```python
last = pred_score.reindex(current_stock_list).sort_values(ascending=False).index
```
`current_stock_list` 来自 `get_stock_list()`；`sort_values` 默认非稳定排序
⇒ **当两只股票的打分完全相同时，谁排在前面取决于集合迭代顺序**
⇒ 卖/买的取舍逐进程可能不同 ⇒ **回测结果不可逐位复现**。

实测证据：同一 arm、同一份 `pred.pkl`、同一套 config，**两个独立进程跑出的 valid 段
年化超额最大差 3.65pp**（A5：4.41% vs 8.06%），而 10/14 个 arm 逐位相同
（说明只在「有同分影响取舍」的 arm 上才发散）。

## 本补丁

把 `Position.get_stock_list` 的返回顺序改成**字典序**（`sorted(...)`）：
上游 `qlib/` **源码零改动**（运行期打补丁，补丁代码在本模块内）。

- 幂等：重复调用只生效一次。

配合 `PYTHONHASHSEED=0` 一起用（见 `CMDS.md`）⇒ 同机同版本**逐位可复现**。

## 🔴 影响面（**别把它当成「只影响同分时的先后」**）

补丁改的是**持仓列表的顺序**，它影响两件事，**第二件会改变回测数值本身**：

1. **排序场景**：同分标的的先后（`sort_values` 默认非稳定排序）
   ⇒ 卖 / 买的**取舍**会变。
2. **卖单的执行顺序**（🔴 会改变数值本身）：
   - `qlib/contrib/strategy/signal_strategy.py:231` `for code in current_stock_list:`
     —— 卖单是**按 `current_stock_list` 的顺序**逐个入列的
     （`current_stock_list` 来自 `current_temp.get_stock_list()`，正是本补丁改掉的那个顺序）；
   - `qlib/backtest/executor.py:576-578`：默认 `trade_type=TT_SERIAL`
     ⇒ `order_it = orders`，**原样保留**策略给出的 `sell_order_list + buy_order_list` 顺序
     （`TT_PARAL` 分支 `qlib/backtest/executor.py:585`
     `order_it = sorted(orders, key=lambda order: -order.direction)` 是**稳定排序**
     ⇒ 同方向订单之间**同样保持原顺序**）；
   - `qlib/backtest/exchange.py:919-939` 现金约束与 `trade_unit` 取整是**逐单生效**的
     ⇒ 先成交的订单先占现金，后面的单可能被**裁剪成交金额** ⇒ 持仓路径变 ⇒ 收益 / 换手变；
   - 上游**自证**：`qlib/backtest/exchange.py:633-636` 的注释已写明
     「because the order of the set is not fixed, the trading order of the stock is different,
     so that the backtest results of the same parameter are different」。

## 🔴 数字口径（引用任何换手 / 收益数字时必须带）

- **打补丁后的换手 / 收益数字可能与打补丁前不同** ⇒ 两轮数字**不可直接混用、不可直接相减**；
  上面的 3.65pp 实测就是「顺序敏感」arm 的发散证据。
- **memory 里 manifest 基线那一轮的数字来自补丁前**（当时的 `run_pipeline.py` 没打补丁）；
  而 `turnover_analysis.py` / `turnover_sweep.py` 的数字来自**打补丁后**。
  （实测：`turnover_sweep.py` 的 `A0_baseline` 控制组在**打补丁后**精确复现了 manifest
  **补丁前**的数字 —— 毛超额相对误差 4.1e-15、净超额 8.0e-14、稳态日均单边换手 4.8e-05
  ⇒ 该组对顺序不敏感；但顺序敏感的 arm 实测可差 3.65pp。）
"""
from __future__ import annotations


def apply_deterministic_position_ordering() -> str:
    """给 `qlib.backtest.position.Position.get_stock_list` 打上确定性补丁。

    返回 "applied" / "already-applied" / "unavailable"（qlib 结构变化时如实返回，不静默）。
    """
    try:
        from qlib.backtest.position import Position
    except Exception:  # noqa: BLE001 —— qlib 结构变了就如实说，不假装打上了
        return "unavailable"

    if getattr(Position.get_stock_list, "_manu_deterministic", False):
        return "already-applied"

    original = Position.get_stock_list

    def get_stock_list(self) -> list[str]:
        # 原实现返回 set 迭代序（逐进程随机）⇒ 改成字典序：
        #   ① 消除同分标的取舍的随机性（排序场景）；
        #   ② 同时固定卖单的入列顺序 ⇒ 消除「先成交者先占现金 / trade_unit 逐单取整」
        #      带来的数值漂移 ⇒ 打补丁前后的换手/收益数字可能不同（见本模块 docstring）
        return sorted(original(self))

    get_stock_list._manu_deterministic = True  # type: ignore[attr-defined]
    get_stock_list.__doc__ = (  # type: ignore[attr-defined]
        "manu 确定性补丁：返回**字典序**的持仓列表（上游原实现是 set 迭代序，逐进程随机）。"
    )
    Position.get_stock_list = get_stock_list  # type: ignore[method-assign]
    return "applied"
