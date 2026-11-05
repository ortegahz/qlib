# ETF 轮动分析流水线（`manu/etf_rotation/daily/`）

## 0. 交付形态：**按需分析**（2026-11-05 用户拍板，当前唯一形态）

> 原话：「算了，咱们简化流程，你不用在服务器写定时触发程序了，你只要这套流程跑通，
> 我会在需要你分析的时候，通过窗口告诉你分析，你直接在窗口给我恢复报告就可以，也不用发邮件了，
> 我想了一下，交易日我都是在上班的。」

| 维度 | 当前形态 |
|:--|:--|
| **触发** | **用户按需**：在窗口说「分析」⇒ 主 agent 在服务器跑流程 |
| **交付** | **Markdown 报告直接回窗口**（默认落盘 `manu/report/report_<YYYYMMDD>_mode<A|B>.md`，同时打印到 stdout） |
| **定时触发** | ❌ **已取消**。2026-11-05 已把服务器用户 crontab 里的 **5 条**（3 流水线 + 2 看门狗）**全部卸载**，只剩系统默认两行（`SHELL` / `PATH`） |
| **邮件推送** | ❌ **不再是默认动作**。保留为**显式 opt-in**（`--send-email`）；通道仍通（见 §7） |
| **host 关机 / 会话交接** | ❌ **不再考虑**。服务器不关机，重活一律在服务器侧自主执行 |

🔴 **本文件描述的是「怎么跑」，不是「项目现在处于什么状态」** —— 后者属事实层，见 `manu/memory/memory.md`。
🔴 **不可协商约束见 `manu/memory/rules.md` §十五「每日流水线铁律」**（本文件只引用条目号，不复制铁律原文）。

---

## 1. 六阶段全流程（主线口径）

**月度调仓日（例如 2026-12-01 收盘后）执行的这一整套，才是真正意义上的「量化 ETF 轮动全流程」**：

```text
[阶段1 AkShare 盘后数据管道]
        │
        ▼
[阶段2 Qlib S层全市场Alpha推断 (LightGBM + Alpha158)]
        │
        ▼
[阶段3 ETF穿透聚合 (E = W × S) + 均线过滤 (MA20/MA60)]
        │
        ▼
[阶段4 TradingAgents 多智能体交叉辩论与情景裁决]
        │
        ▼
[阶段5 规则层约束对账 (等权/整手取整/货基过滤/滑点缓冲)]
        │
        ▼
[阶段6 实盘执行卡生成与次日开盘挂单]
```

| 阶段 | 做什么 | 主要程序 | 耗时 |
|:--|:--|:--|:--|
| **1 盘后数据定型** | 拉全市场 ~5000 只个股 + 候选 ETF 池日线；读**本地静态缓存的 W**；增量标准化写入本地 Qlib 二进制 `cn_data`（**无未来函数、日历对齐**） | `daily/fetch_data.py`（akshare env 子进程） | ~2 min |
| **2 S 层全市场 Alpha 推断** | Alpha158 因子矩阵 ＋ 预训练 LightGBM 截面排序模型 ⇒ 个股 Alpha 评分矩阵 **S**（落 `stock_scores.csv`） | `manu/etf_rotation/qlib_scores.py --mode infer` | **~27 min** |
| **3 穿透聚合 + 均线过滤** | `E = W × S`；算候选 ETF 与持仓的 **MA20**（进场准入）/ **MA60**（生命线止损）；**未站上 MA20 的直接从候选池剔除**；产出站上均线的 **Top-5 候选 ETF** | `manu/etf_rotation/analyze.py` | ~1~2 min |
| **4 TradingAgents 交叉辩论** | 注入 Top-5 候选 ＋ 实盘账户状态（总资产 30,035.57 / 现金 23,367.37 / 银行 ETF 7,700 股）；宏观基本面 Agent × 技术风控 Agent 交叉辩论 ⇒ 产出 `tradingagents_decision.md`（超配 / 低配 / 观望） | `manu/etf_rotation/llm/run_tradingagents.py`（＋ `build_ta_context.py`） | **~15 min**（实测 679.4~1083 s） |
| **5 规则层约束对账** | 等权 5 份（单只 ≤ **6,000 元**）；**100 股整手向下取整**；场内货基碎屑（一手 ~10,000 元 > 6,000 上限）**拦截不买、归现金**；强制预留 **200~300 元**现金缓冲 | `manu/etf_rotation/final_rebalance_plan.py` ＋ `reprice_node_day.py` | ~1 min |
| **6 执行卡 + 报告** | 产出执行卡 `execution_card_<YYYYMMDD>.json` ＋ **终端/窗口直出的 Markdown 报告**；次日 09:25 在券商 App 集合竞价 / 开盘**人工挂单** | `daily/main.py` ＋ `daily/agents_rebalance.py` | ~5 s |

**模式 A / 模式 B 的差别 = 跑哪几段**（不是两套代码，是同一条链的两种截断）：

| 维度 | 模式 A（日常巡检） | 模式 B（月度调仓信号日） |
|:--|:--|:--|
| 触发 | **按需**（窗口喊分析），非信号日的交易日 | **按需**，信号日 **2026-12-01** 收盘后 ⇒ 执行日 **2026-12-02** 开盘 |
| 目的 | 监控 512800 是否跌破 MA60 | 全市场重排兵布阵，决定下月买卖清单 |
| 阶段 1 | ✅ 只取 ETF 日线（**不拉全市场个股**） | ✅ 全市场个股 ＋ ETF |
| 阶段 2 S 层打分 | ❌ **不跑**（27 min 且覆盖历史，日常坚决不跑） | ✅ **必跑**（LightGBM + Alpha158，全市场 Alpha 矩阵 S） |
| 阶段 3 | 只算**持仓侧** MA20/MA60 | 全候选池穿透 + 均线过滤 + Top-5 |
| 阶段 4 TradingAgents | ❌ **不跑**（省 API 配额与算力） | ✅ **必跑**（多 Agent 辩论） |
| 阶段 5 / 6 | 只出**一句话状态卡**（趋势完好 ⇒ 躺平） | 完整**调仓执行表格**（次日开盘照单挂单） |
| 耗时 | **< 1 分钟**（实测 1.2 s） | **约 35~45 分钟**（数据定型 + S 层 ~27 min + 辩论 ~15 min） |

🔴 **模式 A 的「构造性保证」**：模式 A 的执行路径里**根本没有**打分 / 选股 / LLM 的函数引用 ⇒ **物理上不可能**产出新仓位指令（不是靠 `if` 判断）。唯一例外是**持仓纪律直接触发**的**紧急平仓**（收盘价有效跌破 MA60，`BREAK_CONFIRM_RATIO = 0.0`）—— 它**不经过任何选股路径**，是止损而非选股产物。

🔴 **静默防守窗口**：`2026-11-06` ~ `2026-11-30`（`daily_config.SILENT_DEFENSE_START/END`）—— 期间**严禁**生成任何新仓位指令。

---

## 2. 数据管道：分层存储、频次解耦、严格冷却、主动降速

### 2.1 分层更新矩阵

| 数据层级 | 对应标的 / 数据源 | 真实披露 / 变动周期 | 系统设定更新规则与频率 | 存储形式 | 防封关键策略 |
|:--|:--|:--|:--|:--|:--|
| **ETF 成分股权重矩阵 W** | 候选池 ~250 只 ETF（AkShare） | 季报 / 半年报（1/4/7/10 月末） | **严格 TTL 冷却（45~60 天）**；未过期**直接读本地缓存，网络请求数 = 0** | `etf_holdings.csv` ＋ `meta.json` | 强制休眠 1~2 s、分批打散（每 25 只熔断 5~8 s），**绝不日常重复爬取** |
| **ETF 日线量价行情** | 候选池 ~250 只 ETF（AkShare） | 每个交易日盘后 | 每个交易日**增量追加**，只拉最近 N 天缺失切片 | `etf_prices.csv`（追加去重） | 批量接口请求，单次 30~60 s |
| **个股 Qlib 底层量价** | 全市场 ~5000 只 A 股 | 每个交易日盘后 | **调仓日前夜 / 当天**增量写入 | Qlib 二进制 `cn_data`（`.bin`） | 本地离线维护 / 合规滚动快照 Dump，**不做高频爬虫** |

### 2.2 防封五铁律（数据层）

1. **TTL 缓存短路**：`etf_holdings_meta.json` 记 `last_updated`；(Today − last_updated) < TTL ⇒ 直接读本地缓存，**网络请求 0 次**；
2. **请求抖动与速率限制**：单标的抓取间隔 `sleep(random.uniform(1.2, 2.5))`；**每连续 25 只熔断** `sleep(random.uniform(5.0, 8.0))`；
3. **指数退避重试**：超时 / 502 / 空返回 ⇒ 最多 **3 次**，间隔递增（3 s → 6 s → 12 s）；单只连续失败 ⇒ **跳过并保留上一期**，不崩主进程；
4. **只在盘后取数一次**：15:30 之后数据定型，**只拉一次**日线；**严禁盘中为监控价格反复调爬虫**；
5. **User-Agent 轮换 ＋ 边爬边落盘**：Header 池轮换桌面浏览器 UA；每爬完一只立即落盘，避免最后一只异常导致整批内存数据丢失。

🔴 **本工程现状**：W 是**只读**的（`daily/fetch_data.py::read_holdings_snapshot()`）—— 新鲜度**只披露、不拦**；
研究链的 `fetch_etf_holdings.py` **没有只读开关** ⇒ **每日链不得调用它**；真要刷新 W ＝ 重活，必须**用户当轮授权**。

### 2.3 Qlib 数据集刷新：滚动官方快照换库（不是逐日增量写）

阶段 1 的「写 qlib 二进制」在当前形态下**不逐日增量写**，而是**滚动官方快照换库**：

- **源**：`chenditc/investment_data@latest` ⇒ `qlib_bin.tar.gz`（**570 MB**，服务器下载 **114 秒**）；
- **现状**：服务器 `/mnt/data/siping/datasets/qlib/cn_data` **就是它解出来的**，日历覆盖 **2000-01-04 ~ 2026-11-03**；
- 🔴 **日历前缀一致性 fail-closed 校验**：换库前后**日历前缀必须逐日一致** ⇒ 前缀变了 ⇒ **打分栅格相位漂移**
  ⇒ **拒绝换库**（否则历史 S 层与新 S 层不可比，收益归因全部失真）；
- 单只个股的**逐日增量写**仍保留 `update_qlib_daily_bar()`，但**默认 dry-run** ＋ **复权守卫**（见 §5）。

---

## 3. CLI（按需分析口径）

```bash
python manu/etf_rotation/daily/main.py [选项]
```

| 选项 | 默认 | 语义 |
|:--|:--|:--|
| `--asof YYYY-MM-DD` | 本机当天 | 业务日期（服务器在 Asia/Shanghai） |
| `--mode A\|B\|auto` | `auto` | `A`＝日常巡检 / `B`＝调仓信号日全流程 / `auto`＝按日期分流 |
| `--report-md PATH` | `manu/report/report_<YYYYMMDD>_mode<A|B>.md` | Markdown 报告落盘路径（目录可用 `ETF_ROTATION_REPORT_DIR` 覆盖） |
| `--send-email` | **关** | 🔴 **显式**开启邮件推送（**默认不发**） |
| `--no-email` | 关 | 兼容保留；默认行为，等价于「不显式 `--send-email`」 |
| `--dry-run` | 关 | 干跑：不联网取数 ＋ 不写产物 |
| `--skip-fetch` | 关 | 跳过取数阶段（用现有数据） |
| `--skip-llm` | 关 | 模式 B 跳过 TradingAgents（省 API 配额） |
| `--refresh-scores` / `--no-refresh-scores` | 模式 B **开** | 是否重跑 S 层（**重活**，~27 min） |
| `--update-qlib-bin` / `--no-update-qlib-bin` | 模式 B **开** | 是否刷新 qlib 数据集（**重活**） |
| `--run-root` | `daily_config.RUN_ROOT` | 产物目录 |
| `--log-dir` | `daily_config.LOG_DIR` | 日志目录 |
| `--calendar` | `daily_config.TRADE_CALENDAR_CSV` | A 股交易日历 CSV |
| `--send-only` | 关 | 只发已持久化的邮件草稿（**按需补发**时用；不重跑计算） |
| `--wait-seconds` | `1800.0` | `--send-only` 时等待草稿出现的秒数 |

**默认行为**：渲染 Markdown 报告 ⇒ 落盘 `manu/report/report_<YYYYMMDD>_mode<A|B>.md` ⇒ **同时打印到 stdout**
（主 agent 直接把 stdout 贴回窗口）。**默认不发邮件。**

**常用组合**：

```bash
# ① 模式 A 日常巡检（按需；报告回窗口，不发信）
python manu/etf_rotation/daily/main.py --asof 2026-11-05

# ② 模式 B 调仓信号日全流程（2026-12-01 收盘后；S 层 + 辩论 + 执行卡）
python manu/etf_rotation/daily/main.py --asof 2026-12-01 --mode B

# ③ 模式 B 但跳过 LLM（省 API 配额）
python manu/etf_rotation/daily/main.py --asof 2026-12-01 --mode B --skip-llm

# ④ 干跑：不联网、不写产物（只验逻辑）
python manu/etf_rotation/daily/main.py --asof 2026-12-01 --dry-run

# ⑤ 显式发邮件（默认不发；按需）
python manu/etf_rotation/daily/main.py --asof 2026-11-05 --send-email
```

### 退出码

| 码 | 含义 |
|:--|:--|
| **0** | 成功（**含**「非交易日静默退出 0」） |
| **1** | 失败（异常 ⇒ 捕获 Traceback ⇒ 落盘失败报告） |
| **2** | 用法错误（argparse 未知参数）**或** 锁冲突（已有实例在跑） |

---

## 4. 报告输出（回窗口的东西）

默认落盘 `manu/report/report_<YYYYMMDD>_mode<A|B>.md`（服务器即 `/tmp/pycharm_project_658d3fc4/manu/report/`；可用 env `ETF_ROTATION_REPORT_DIR` 改目录，或用 `--report-md` 指定绝对路径；兜底才落 `<run_dir>/report_<日期>.md`），内容按模式分：

**模式 A**：账户卡片（总资产 30,035.57 / 现金占比 / 512800 现价与 MA60 安全余量）＋ MA20/MA60 明细表
＋ **行情披露日**（`close_date`，与 `asof` 不一致时显著告警）＋ 状态结论（「趋势完好，守纪律躺平」或破位告警）＋ 备注。

**模式 B**：执行卡买卖明细表（代码 / 名称 / 方向 / 目标股数 / 预估金额）＋ 调仓后仓位分布 ＋ 现金流 ＋ notes
＋ 风险披露 ＋ 🔴 **S 层数据日**（**必须披露 12-01 用的到底是哪个打分日** —— 复用旧 S 层时这是最容易被忽略的坑）。

🔴 **数字纪律**：报告里每个数字都必须来自**实际产物**；字段缺失渲染成 **「—」**，**不许反推、不许编**。

---

## 5. 重活开关（模式 B 默认开，日常坚决不开）

### `--refresh-scores`（重跑 S 层，约 27 分钟）

- S 层 = `manu/etf_rotation/qlib_scores.py --mode infer`，产出 `stock_scores.csv`（列：`date, instrument, score, fwd_ret`），下游 `E = W × S` 直接消费它。
- 🔴 **它的落盘是整体覆盖 `stock_scores.csv`，不是增量**；产物只含 `--start ~ --end` 栅格上的日期 ⇒ 拿它当增量用会**抹掉已有的打分日**。
- 因此 `daily/run_qlib_score.py` **默认只读**（只报告 S 层新鲜度与栅格缺哪几天）；真刷新走「**临时目录 → 先备份 canonical → 合并回 canonical**」，且须**用户当轮授权**（铁律一）。
- **打分日是相位栅格**（交易日历下标 `i % 10 == 9`，唯一事实源 `manu/etf_rotation/data/signal_policy.py::score_grid_dates`），栅格被 qlib 数据集覆盖末日封顶 ⇒ 想打新 S **必须先让数据集覆盖到目标日**。

### `--update-qlib-bin`（刷新 qlib 数据集）

- 走 §2.3 的**滚动官方快照换库** ＋ **日历前缀一致性 fail-closed 校验**；
- 单只个股增量写路径 `update_qlib_daily_bar()` **默认 dry-run**：真写前沿用上一交易日 factor，用 akshare **日涨跌幅**与 qlib 复权序列 **日收益**比对，差异超阈值（`FACTOR_DIVERGENCE_THRESHOLD = 0.02`）⇒ **该股剔除、不写脏数据**（记进 `diverged`）。
- 理由：qlib 二进制一旦写进脏复权数据，会**污染其后所有回测 / 打分**，且极难归因。

---

## 6. 密钥纪律（授权码）

**126 客户端授权码只写进服务器本地的 0600 文件，绝不进仓库、绝不进日志、绝不进邮件正文。**

| 项 | 值 |
|:--|:--|
| 授权码落点 | 服务器 `/mnt/data/siping/secrets/smtp.env`（权限必须 **0600**；可用 `ETF_ROTATION_SMTP_SECRETS` 覆盖） |
| 文件里写什么 | `SMTP_PASSWORD=<126 客户端授权码>`（读入后映射为 `ETF_ROTATION_SMTP_PASSWORD`），可选 `SMTP_USER` / `SMTP_HOST` / `SMTP_PORT` |
| SMTP | `smtp.126.com:465`，隐式 SSL；账号 `zxthz@126.com` |
| 权限检查 | `notify_email.load_secret_env()` 检查文件权限：**过宽（≠ 0600/0400）直接忽略该文件并报错** |
| 泄露防护 | 缺字段时**只报字段名、不报值**；日志器**只记非敏感信息**；`SmtpConfig.__repr__` 里 `password='***'` |
| 🚫 绝不 | 不写进仓库任何文件（含 `.env`、`.py`、文档）；不写进日志；不写进邮件主题 / 正文 / 附件 |

⚠️ **「网页登录密码」≠「客户端授权码」**：126 的 SMTP 必须用**客户端授权码**登录，用网页登录密码会报 **`535 authentication failed`**。
另注：126 只接受发件地址本身（`zxthz@126.com`），换成别的域名会报 `553 Local user only`。

---

## 7. 邮件通道：**按需 opt-in**（不再是默认动作）

- **默认不发**。要发必须显式 `--send-email`。
- 通道本身**仍然可用**：2026-11-05 已实测发信成功（用户截图确认收到）。
- 四套模板渲染在 `daily/notify_email.py`：日常正常 / 紧急破位 / 月度调仓卡 / 流水线失败告警。
- 邮件正文里的账户卡片口径在 `daily/account_state.py`。
- 凭据读取顺序：**环境变量（`ETF_ROTATION_SMTP_*` / `SMTP_*`）→ 0600 secrets 文件 → 模块默认**。
- 独立探针：`manu/tools/send_test_mail.py`（纯标准库，不依赖工程代码）。
- 📖 操作规程见 `manu/skills/qlib-smtp-mail/SKILL.md`。

---

## 8. 产物落盘位置

| 产物 | 路径 |
|:--|:--|
| 每日产物根 | `/mnt/data/siping/runs/etf_rotation/daily/`（`daily_config.RUN_ROOT`） |
| 当天目录 | `/mnt/data/siping/runs/etf_rotation/daily/<YYYYMMDD>/`（`mode_router.build_run_dir` 创建） |
| 运行日志 | `/mnt/data/siping/runs/etf_rotation/daily/logs/daily_run_YYYYMMDD.log`（格式 `时间 \| 级别 \| 阶段 \| 消息`） |
| **Markdown 报告** | `manu/report/report_<YYYYMMDD>_mode<A|B>.md`（**主交付件**；可 env / `--report-md` 改路径） |
| **拉回 host** | 🔴 流水线在**服务器**跑 ⇒ 报告天然落在服务器 `manu/report/`；**PyCharm 打开的是 host 工程** ⇒ 跑完必须 `bash manu/tools/pull_reports.sh` 拉回 host（md5 双侧比对），否则 host 上看不到报告 |
| 邮件草稿 | `<当天产物目录>/email_draft_<YYYYMMDD>.json`（含 `subject` / `html_body` / `text_body` / `to` / `kind` / `generated_at`） |
| 心跳 | `<当天产物目录>/heartbeat_<YYYYMMDD>.json`（`status` / `mode` / `updated_at` / `detail`） |
| 已发回执 | `<当天产物目录>/sent_<YYYYMMDD>T<HHMMSS>.json`（证明那封信**真的发出去了**） |
| 执行卡 | `<当天产物目录>/execution_card_<YYYYMMDD>.json` |
| 行情增量 | `<当天产物目录>/etf_prices_daily_YYYYMMDD.csv` |
| 有效行情底座 | `<当天产物目录>/etf_prices_effective_<YYYYMMDD>.csv`（canonical ＋ 当日增量合并，供两个模式共用） |
| dry-run 邮件 | `<当天产物目录>/<YYYYMMDD>T<HHMMSS>_<主题>.eml`（不发送，只落盘） |
| 锁文件 | `/mnt/data/siping/runs/etf_rotation/daily/.lock`（`daily_config.LOCK_FILE`，flock 单实例守卫） |

**模式 B 产物**（在当天目录下）：`account_snapshot.csv` · `etf_analysis_report.json` · `ta_research_context.md` ＋ `ta_context_meta.json`
· `tradingagents_decision.md` / `.json` · `final_rebalance_plan.json` · `node_reprice_<YYYYMMDD>.json`。

🔴 **每日流水线只读研究链产物**：`/mnt/data/siping/runs/etf_rotation/stock_scores.csv`（S 层）、
`/mnt/data/siping/datasets/etf/etf_holdings.csv`（W，**只读**）、`/mnt/data/siping/datasets/qlib/cn_data`（qlib 二进制）。

---

## 9. 模块职责一览

| 文件 | 职责 |
|:--|:--|
| `main.py` | 入口：六阶段编排、CLI、flock 守卫、退出码、报告渲染、失败兜底 |
| `daily_config.py` | **全部常量的单一事实源**（路径、账户口径、组合约束、均线窗口、节点日、SMTP、阈值） |
| `mode_router.py` | **唯一的模式判定 owner**（口径源 = `daily_config.NEXT_SIGNAL_DATE`）＋ 产物路径 ＋ `draft_path()` |
| `calendar_gate.py` | 交易日历门（**fail-closed**）＋ 模式解析（与 `mode_router` 同口径、刻意不互相 import） |
| `fetch_data.py` | 盘后增量：ETF 日线（shell out 到 akshare env）、**W 只读**、qlib 二进制可选增量（默认 dry-run） |
| `run_qlib_score.py` | S 层**新鲜度判定**（默认只读）＋ 可选刷新（临时目录 ＋ 合并 ＋ 备份） |
| `agents_rebalance.py` | 模式 A / 模式 B 的计算主体 ＋ 执行卡组装 |
| `notify_email.py` | 四套邮件模板 ＋ SMTP 客户端（0600 secrets 读取、权限守卫、dry-run 落 `.eml`） |
| `account_state.py` | **实盘账户基准**（30,035.57 元口径）＋ 三道校验 ＋ `DISCREPANCY_NOTE` |
| `tests/` | pytest 单测（`conftest.py` 把 `daily/` 挂进 `sys.path`） |

---

## 10. 调度与看门狗：**当前不部署**

- 🔴 **服务器 crontab 里已无本工程的任何条目**（2026-11-05 已卸载 5 条：3 流水线 ＋ 2 看门狗）；
- 🔴 **`install_schedule.sh` 已删除**（2026-11-05）：它唯一的用途是往用户 crontab 装那 5 条定时任务 ⇒ 取消定时后即死代码 ⇒ **删除**（避免后人误以为「定时是标准形态」）。
- ⚠️ **`manu/tools/etf_watchdog.py` 保留在仓库，但当前不部署**：它的用途是守护 cron（查 cron 三条 / 主程序 / 凭据 0600 / 当天心跳与已发回执）⇒ **cron 取消后它没有守护对象**。
  服务器持久盘 `/mnt/data/siping/apps/etf_watchdog.py` 的旧副本**保留未删**（无害）；若日后要恢复定时，可复用它。
- 按需形态下「谁通知谁」的答案是：**用户在窗口喊 → 主 agent 跑 → 报告回窗口**；失败由主 agent 在窗口内报错，不需要看门狗。

---

## 11. 相关文档

- 铁律：`manu/memory/rules.md` §十五「每日流水线铁律」。
- 事实与数字：`manu/memory/memory.md` §`2026-11-05 · 每日流水线` ＋ §`2026-11-05 · 交付形态改为「按需分析」`。
- 可复制命令：`CMDS.md` §「每日 ETF 轮动流水线」。
- 发信规程：`manu/skills/qlib-smtp-mail/SKILL.md`。
