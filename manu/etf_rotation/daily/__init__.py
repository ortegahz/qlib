"""manu.etf_rotation.daily —— **每日 ETF 轮动流水线**（每个 A 股交易日 15:30 自动执行）。

模块分工（模块名是用户口径，**不许改**）：

| 模块 | 职责 |
|:--|:--|
| `main.py` | 统一入口：日历门 → 取数 → 模式分流 → 打分/组合 → 邮件；异常兜底 + 失败告警 |
| `fetch_data.py` | 盘后增量取数（ETF 行情 / W 只读 / qlib 二进制可选增量） |
| `run_qlib_score.py` | S 层（个股截面分数）的新鲜度判定与**可选**增量刷新 |
| `agents_rebalance.py` | 模式 A（巡检）/ 模式 B（调仓决策卡）的组合算术与执行卡 |
| `notify_email.py` | `smtplib` + `email.mime` 发信（四套模板 + dry-run） |

辅助模块：`daily_config.py`（常量单一事实源）、`account_state.py`（实盘账户基准）、
`calendar_gate.py`（交易日历门，fail-closed）、`mode_router.py`（模式分流）。

入口（服务器工作副本 `/tmp/pycharm_project_658d3fc4`）：

```bash
/home/huangzhe/anaconda3/envs/ql/bin/python manu/etf_rotation/daily/main.py --asof 2026-11-06
```
"""
