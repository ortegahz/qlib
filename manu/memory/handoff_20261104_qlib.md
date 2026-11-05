# 交接记录 · qlib ETF 轮动线（2026-11-04 18:15）

> 本文件是 **host 侧独有**的交接记录（服务器侧没有），
> `morning_pullback_qlib.sh` 用 rsync（**不带 --delete**）回拉 `manu/`，因此本文件不会被覆盖。

## 一句话状态

host 18:30 关机**安全**：夜间链（qlib 线）在服务器上自主跑到 **2026-11-05 08:30**，
对话 thread `01a1a469-c1a0-7d22-8b3d-072a6b3e7cb2` 已 fork 到服务器，
host 断电不影响服务器侧任何进程。

## 服务器上的运行实体（host 关机后仍然活着）

| 实体 | 位置 | 说明 |
|:--|:--|:--|
| 夜间 supervisor | `bash /mnt/data/codex-overnight/server_overnight_loop.sh --session 01a1a469-… --until 2026-11-05 08:30 --cwd /tmp/pycharm_project_658d3fc4 --state-dir /mnt/data/codex-overnight-qlib` | `setsid nohup` 启动，ppid=1 |
| 夜间 agent | `codex exec --approve-for-me --add-dir /mnt/data resume --skip-git-repo-check 01a1a469-…` | turn #1 于 18:02 起跑 |
| 对话 rollout | `/mnt/data/.codex/sessions/2026/11/04/rollout-2026-11-04T10-35-56-01a1a469-c1a0-7d22-8b3d-072a6b3e7cb2.jsonl` | 服务器 CODEX_HOME=`/mnt/data/.codex` |
| 工作副本 | `/tmp/pycharm_project_658d3fc4`（服务器，**权威**） | host 的 SSHFS 镜像 `/home/manu/mnt/pycharm_project_658d3fc4` 是**只读** |
| 产物目录 | `/mnt/data/siping/runs/etf_rotation/` | 持久盘 7.4 T |

## 明早（host 9:00）一条命令收尾

```bash
bash /media/manu/1TB-Volume/workspace/handoff/overnight/morning_pullback_qlib.sh
```

它做 6 件事：停夜间 session → 备份 host 侧 `manu/` → rsync 服务器 `manu/` 回 host（服务器侧为准）
→ rsync 运行产物 → md5 双侧比对 → `handoff_from_server.sh --session 01a1a469-… --adopt
--cwd /media/manu/1TB-Volume/workspace/qlib`（**cwd 必须是 host 的 qlib 目录**，picker 按 cwd 精确匹配）。

## 18:15 前 host 已经替夜间 agent 做完的事

1. **个股日线导出**（补掉 5/20 日 RankIC 取不到的坑）：
   `/mnt/data/siping/runs/etf_rotation/stock_prices.csv`
   —— 3,900,683 行 / 116.9 MB / 721 个交易日 / 5,939 只 / 2023-11-10 ~ 2026-11-03（≤ asof，无未来函数）。
2. **分析报告已产出**：`etf_analysis_report.json`（1.43 MB）+ `.md`（32 KB），18:10。
   - `next_rebalance.target_date = 2026-12-01`（`in_calendar_verified=true`，还剩 20 个交易日）
   - RankIC（实数）：N=1 → 68 截面 / mean 0.0480 / ICIR 0.447 / t 3.686；
     N=5 → 68 / 0.0511 / 0.478 / 3.943；N=20 → 66 / 0.0633 / 0.565 / 4.593
3. **TradingAgents 补丁 b)~e) 已落服务器**（两处副本一致）：
   `default_config.py`（`TRADINGAGENTS_REASONING_EFFORT`→`reasoning_effort`）、
   `factory.py::build_llm_kwargs`（provider 专属未设时转发通用 `reasoning_effort`）、
   `openai_client.py`（`_MINIMAX_REASONING_MODEL = ^(?:minimax-)?m\d` 放行 M 系列）、
   `model_catalog.py`（`_MINIMAX_MODELS` 含 `MiniMax-M3.1-Flash`）。
   `TradingAgents/.env` 已放好（provider=minimax-cn / 模型 MiniMax-M3.1-Flash /
   `TRADINGAGENTS_REASONING_EFFORT=max` / `MAX_TOKENS=32768` / 输出中文），**key 不入库**。
4. **把 18:10 更新过的后续指令推到了服务器 state-dir**
   （`/mnt/data/codex-overnight-qlib/prompt_next.txt`，29824 B）—— 原先服务器上还是 18:02 的旧版，
   夜间 agent 第 2 轮起会用新版：合并 host 领先补丁 → S5 TradingAgents → S6 最终调仓建议 → S7 回写 memory。

## 唯一的 host/server 分叉（已在 §十四 处置）

| 文件 | host md5 | server md5 | 差异 |
|:--|:--|:--|:--|
| `manu/etf_rotation/analyze.py` | `a133b97c…` | `dc492aaa…` | server 多了 **akshare 双日历合并**（推 2026-12-01 必需）；host 多了 **`_realized_score_dates` 防未来函数**（RankIC 只统计已实现信号日，并写 `rank_ic.causality`） |

处置：**只移植 host 的 `_realized_score_dates` 一个特性进服务器版**，禁止整份覆盖
（会回归掉日历合并）。host 那份已放到服务器 `/mnt/data/codex-overnight-qlib/host_delta/analyze_host_rankic_causality.py`
（只读参考），并在 prompt 里点名要求移植 + 重跑一次 analyze.py。
回拉时以**服务器侧为准**合并回 host（`rules.md §十四`）。

## 夜间 agent 剩余三个里程碑（明早应能看到产物）

- `tradingagents_decision.md`（LLM 层：多智能体辩论结论）
- `final_rebalance_plan.json` + `final_rebalance_plan.md`（动作 / 目标股数 / 金额 / 理由，节点 2026-12-01）
- `manu/memory/etf_rotation_20261104.md` 等三份 memory 同步更新

## 未获授权、明确禁止的重活（夜间 agent 不得做）

全量重训模型、完整回测网格、数据集重建、下载新数据、改上游 `qlib/`。

## 18:19 追加：S5 TradingAgents 的两个坑（host 实测，非推测）

| 坑 | 现象 | 处置 |
|:--|:--|:--|
| `selected_analysts=()` | `ValueError: at least one analyst must be selected`（`graph/analyst_execution.py:70`） | **必须至少选一个 analyst**；原 prompt 里「不选 analyst」的退路不存在 |
| 服务器 yfinance 限流 | `YFRateLimitError: Too Many Requests`，在 `tools_market` 任务里**直接把整张图打崩** | 所有工具包 try/except 兜底；analyst 层改用不依赖 yfinance 的 `news` |

已验证可用：LLM（minimax-cn / MiniMax-M3.1-Flash / reasoning_effort=max / max_tokens=32768）真实调用成功；
`build_llm_kwargs` → `{'reasoning_effort':'max','max_tokens':32768}`；
`research_context` 在 `propagate()` 与 `create_initial_state()` 都存在，
`research_manager` + 三个 risk debator 都用 `render_research_context(...)` 渲染 ⇒ qlib 报告全文进辩论。

host 已把可用脚本写好并在服务器启动：
`/mnt/data/codex-overnight-qlib/host_delta/run_tradingagents_etf.py`
（自动截断注入报告 → 跑辩论 → 落 `tradingagents_decision.md`），
日志 `/mnt/data/siping/runs/etf_rotation/s5_run.log`，
实测结论 `/mnt/data/codex-overnight-qlib/host_delta/S5_RECIPE.md`。

## 18:29 追加：S6（最终调仓计划）已由 host 提前跑通并落盘

新增脚本 `manu/etf_rotation/final_rebalance_plan.py`（host 写 → 单文件推到服务器 → 服务器实跑通过），
产物 `/mnt/data/siping/runs/etf_rotation/final_rebalance_plan.{json(15.3KB),md(7.6KB)}`。

实跑结果（asof 2026-11-03，节点 **2026-12-01**，总资产 29,854.67 / 期初现金 15,518.87）：

| 代码 | 名称 | 动作 | 股数 | 金额（元） | 理由 |
|:--|:--|:--|--:|--:|:--|
| 159929 | 医药ETF汇添富 | 卖出 | -2500 | -3335.00 | 流动性不达标 |
| 512690 | 酒ETF鹏华 | 卖出 | -2000 | -890.00 | 120 日动量 -4.62% ≤ 0 |
| 513330 | 恒生互联网ETF华夏 | 卖出 | -7000 | -2268.00 | E 算不出（无 A 股成分）且非防守资产 |
| 513770 | 港股互联网ETF华宝 | 卖出 | -3500 | -1113.00 | 同上 + 同赛道重复持仓 |
| 512800 | 银行ETF华宝 | 卖出 | -900 | -786.60 | 入选，但现 7700 股 > 目标 6800 股（20% 权重） |
| 563020 | 红利低波ETF易方达 | **买入** | +4900 | +5963.30 | 入选本期组合 |
| 511660/511880/511990 | 货币 ETF ×3 | 持有 | 0 | 0 | 目标 5971 元 < 一手（100 份 × ≈100 元 ≈ 10000 元）⇒ 整手口径建不了仓 |

现金流：期初 15,518.87 → 卖出回款 8,392.60 → 买入 5,963.30 → 费用 ≈9.33 → **期末 17,938.84**。

### 两个踩过的坑（已修）

1. `target_weight=0`（卖出）时 `target_amount=0` 被 `if price and target_amount` 判成 falsy
   ⇒ `target_shares=None` ⇒ **卖出回款没进现金核算**。改成 `is not None` 判断。
2. 货币 ETF 单价 100 元/份 ⇒ 1 手 ≈ 10,000 元 > 目标 5,971 元 ⇒ 整手口径下建不了仓。
   脚本现在**显式标注** `lot_note`，不静默变成 0 股，也不为了凑 5 只去拆整手约束。

### 硬约束（不许为了好看去改）

整手 100 股 / 佣金 1.5bp / 滑点 5bp / ETF 免印花税免过户费 / 目标权重 20%（约 5,971 元）。

## 18:26 追加：自动化入口 + 回测引用 bug

**自动化入口**：`manu/etf_rotation/run_pipeline.sh`（一条命令跑完 5 步）
`qlib 个股打分 → 导出个股日线 → ETF 分析报告 → TradingAgents 决策 → 最终调仓计划`；
日常调仓用 `--skip-scores --skip-prices` 复用已有产物（重活默认不跑）。

**Bug（已备补丁，等夜间 agent 落地）**：`analyze.py::_load_backtest` 用
`sorted(glob(...))[-1]` 按文件名字母序取最后一个 ⇒ 选中 18:09 那个 **408 字节的残缺
`etf_rotation_backtest_v4_prefer1.json`** ⇒ 报告误判「回测产物不可得」，
实际完整产物在同目录（年化 21.49% / 基准 12.11% / 超额 +8.37% / MDD -28.19% / 换手 19.07 倍）。
补丁：`/mnt/data/codex-overnight-qlib/host_delta/fix_backtest.py`
（遍历候选 + 校验 `portfolio_net`/`benchmark_510300`/`excess_vs_510300` 三键 + 跳过残缺）。

## 18:28 追加：TradingAgents（S5）的四道墙 —— 全部实测定位

| # | 报错 | 根因 | 解法 |
|:--|:--|:--|:--|
| ① | `ValueError: at least one analyst must be selected` | `graph/analyst_execution.py:70` 强制 ≥1 analyst | 选 `news`（不依赖 yfinance） |
| ② | `YFRateLimitError: Too Many Requests`（崩在 `tools_market`） | 服务器 Yahoo 限流，行情类 analyst 取数把整张图打崩 | 工具包 try/except 兜底 |
| ③ | `AlphaVantageNotConfiguredError` | `get_news` 无可用 vendor（无 key + Yahoo 限流） | **不用申请 key**：`S5_FAST_STUB=1` 跳过外部数据源 |
| ④ | `GraphRecursionError: Recursion limit of 6` | 绕过了 `propagator.get_graph_args()` ⇒ 用 LangGraph 默认上限 6 | 走 `ta.propagate()`（内部自动 `max_recur_limit=100`，见 `default_config.py:125`） |

统一解法（已成脚本）：`/mnt/data/codex-overnight-qlib/host_delta/run_tradingagents_etf.py`
—— 工具兜底 + fast-stub 开关 + `propagate()` 路径 + 自动落 `tradingagents_decision.md`。
看门狗 `/mnt/data/codex-overnight-qlib/host_delta/watch_and_refresh2.sh`：
**只接受不含「未完成 / Traceback」且含「最终交易决策」的决策文件**，自动重跑 S6 并入计划；
失败文档一律不并进最终计划（已实测拦截成功）。

## 08:53~09:0x 回拉验收（2026-11-05 早）

`morning_pullback_qlib.sh` 已在 **08:53:04** 跑完（证据：host 侧 `manu/_host_backup_20261105T085304` 存在）。
本节是**主 agent 第一手复核**，不是转述脚本输出。

### 结论

| 项 | 结果 |
|:--|:--|
| 对话已回到本地 | ✅ thread `01a1a469-c1a0-7d22-8b3d-072a6b3e7cb2` 在 host 可续 |
| 服务器副本 | 保留未删：`/mnt/data/.codex/sessions/2026/11/04/rollout-…01a1a469….jsonl` = **142,937,871 B**（mtime 08:36），作备份 |
| host 副本 | `/home/manu/.codex/sessions/2026/11/04/rollout-…01a1a469….jsonl`，**143,753,337 B**（服务器部分 35,771 条 + 本地续写 150 条，会继续涨） |
| 原 host 副本（撤销用） | 同目录 `.bak.20261105-085324` / `.bak.20261105-085328`，各 **19,808,716 B** / 7,835 条 |
| `state_5.sqlite` 行 | `cwd=/media/manu/1TB-Volume/workspace`、`history_mode=paginated`、`archived=0`、`title`/`preview`/`first_user_message` 已回填 |
| 历史索引 | `thread_history_1.sqlite`：**110 turns / 10,381 items**（与服务器**完全一致**） |
| 锚点校验 | **110/110** start 锚点指向正确 `ordinal`；end 锚点为**开区间**（指向下一条记录）—— 与服务器同口径 |
| 逐记录语义比对 | 35,771 条中 **仅 61 条（0.17%）** 有差异，且**全部**是嵌入文本里的路径串 |
| trust 条目 | `config.toml` 已含 `/media/manu/1TB-Volume/workspace` 与 `…/qlib` 两个 `trusted` |
| 端到端 | `read_thread` 能读出服务器侧 **M-S7 / M-S6** 回合全文 ⇒ 索引真的可用 |
| 服务器残留 | 单次触发器 `/tmp/codex-handoff-go` **已解除**；无残留 `codex exec` 进程（只剩 app-server daemon） |

### 🔴 那 61 条差异到底是什么（不是内容丢失）

`handoff_from_server.sh` 的 `SERVER_CWD` **默认值是 `/tmp/pycharm_project_10ae9e2e`**（上一轮 ultralytics 交接的目录），
回拉时它被当作 `--replace` 的源整体替换成 host cwd ⇒ **早期回合里那段真实存在过的历史路径**
（`/tmp/pycharm_project_10ae9e2e`）被一并改写成了 `…/workspace/qlib`。

⇒ 影响面：**仅路径文本的呈现**，命令语义、输出内容、消息正文一字未失；
但翻阅早期回合时会看到「当年在 10ae9e2e 干的事」被写成了 qlib 目录。**知悉即可，不需修**。

### 代码 / 产物同步核对

| 范围 | 服务器 | host | md5 比对 |
|:--|:--|:--|:--|
| `manu/`（全树） | 132 文件 | 132 文件重叠 | **132/132 全同，0 不同**（host 另有 121 个自有文件，含本记录档） |
| `runs/etf_rotation/` | 562 文件 | 470 文件重叠 | **470/470 全同，0 不同** |
| ~~host `runs/etf_rotation/`~~ | 562 文件 | ⚠️ **「470 文件」已作废** | **2026-11-05**：host `runs/`（170M）**已移出仓库**（移到 `/tmp/qlib_host_runs_moved_*`，**非删除**）⇒ 上表 host 侧数字是**历史快照**；**权威副本在服务器** `/mnt/data/siping/runs/etf_rotation/`（582 文件、更新 3 小时）。详见 `etf_rotation_20261104.md §52.3` |

服务器独有的 92 个文件 = **4 个** lock/pycache + **53 个** `_backup_*` 备份 + **35 个** `.sh`/`.py`/`.out`
⇒ `morning_pullback_qlib.sh` 第 4 步 rsync 只放行 `json/md/csv/log/txt` ⇒ `.sh`/`.py` 被**按设计排除**，
不是同步失败。`stock_prices.csv`（117 MB）同样按设计排除，可用导出脚本复现。

### ⚠️ 一处遗留（下次交接前请改）

`handoff/.handoff_session` 仍写着**上一轮**的对象：

```
SESSION=01a1a5b5-cab9-7ef0-b566-b4a033b8afdb
CWD=/tmp/pycharm_project_10ae9e2e
```

若将来跑 `prepare_host_repo.sh`（它读这个文件来 arm 触发器），会 arm 到**错的 thread**。
且该文件指向的是**服务器侧较旧的副本** ⇒ 真触发的话会把旧内容盖到本 thread 上。
⇒ 交接前请先改写或删除它。

### 复核命令

```bash
# 状态行 + 历史索引
cp ~/.codex/state_5.sqlite* ~/.codex/thread_history_1.sqlite* /tmp/tcheck2/
python3 - <<'PY'
import sqlite3; TID="01a1a469-c1a0-7d22-8b3d-072a6b3e7cb2"
con=sqlite3.connect("/tmp/tcheck2/state_5.sqlite")
print(con.execute("select cwd,history_mode,archived from threads where id=?",(TID,)).fetchone())
con.close()
con=sqlite3.connect("/tmp/tcheck2/thread_history_1.sqlite")
print("turns",con.execute("select count(*) from thread_turns where thread_id=?",(TID,)).fetchone()[0],
      "items",con.execute("select count(*) from thread_items where thread_id=?",(TID,)).fetchone()[0])
PY

# 服务器侧只读核对（注意本机 ssh_config 有权限问题，必须 -F /dev/null）
ssh -F /dev/null -p 32222 huangzhe@192.168.99.40 \
  'ls -l /tmp/codex-handoff-go 2>/dev/null || echo NO_TRIGGER; ls -l /mnt/data/.codex/sessions/2026/11/04/rollout-*01a1a469*.jsonl'
```
