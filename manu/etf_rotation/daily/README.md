# ETF 轮动分析流水线（`manu/etf_rotation/daily/`）

## 0. 交付形态：**按需分析**（2026-11-05 用户拍板，当前唯一形态）

> 原话：「算了，咱们简化流程，你不用在服务器写定时触发程序了，你只要这套流程跑通，
> 我会在需要你分析的时候，通过窗口告诉你分析，你直接在窗口给我恢复报告就可以，也不用发邮件了，
> 我想了一下，交易日我都是在上班的。」

| 维度 | 当前形态 |
|:--|:--|
| **触发** | **用户按需**：在窗口说「分析」⇒ 主 agent 在服务器跑流程 |
| **交付** | **Markdown 报告直接回窗口**（快照目录 + `manu/report/report_<YYYYMMDD>.md` 一份副本，同时打印到 stdout） |
| **定时触发** | ❌ **已取消**。2026-11-05 已把服务器用户 crontab 里的 **5 条**（3 流水线 + 2 看门狗）**全部卸载**，只剩系统默认两行（`SHELL` / `PATH`） |
| **邮件推送** | ❌ **不再是默认动作**。保留为**显式 opt-in**（`--send-email`）；通道仍通（见 §7） |
| **host 关机 / 会话交接** | ❌ **不再考虑**。服务器不关机，重活一律在服务器侧自主执行 |
| **架构形态** | **单一无状态流水线 + 快照制**（2026-11-09 架构瘦身）：**无「模式」分诊**；每次运行落一个**不可变快照目录**；副作用只由 `--live-trade` 决定 |

🔴 **本文件描述的是「怎么跑」，不是「项目现在处于什么状态」** —— 后者属事实层，见 `manu/memory/memory.md`。
🔴 **本轮改造（2026-11-09 架构瘦身）尚未实跑验证** —— 本文涉及快照制的新行为一律是**设计口径**，
以代码与探针（`probes/probe_snapshot_contract.py`）为准。
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

**这条链现在是「单一流水线」**（2026-11-09 架构瘦身）：**没有**「模式」分诊（旧形态的三条分诊链已合并为一条）。
唯一的入口永远是 `daily/main.py`；**跑哪几段**由 `--full` ＋ skip 开关决定，
**要不要动真格**由 `--live-trade` 决定。

| 维度 | 缺省（观察态） | `--full`（强制全流程） |
|:--|:--|:--|
| 跑哪几段 | 按 skip 开关：取数 ✅ / S 层刷新 ✅（可关）/ qlib 数据集刷新 ✅（可关）/ 增量补齐 ✅（可关）/ LLM 辩论 ✅（可关） | **5 个语义开关全开**（取数 / S 层刷新 / qlib 换库 / 增量补齐 / LLM 辩论），与任何 skip 开关冲突时**以 `--full` 为准** |
| 目的 | 巡检 / 推演：看当日排序、规则层 vs LLM 分歧、信号演化 | 月度调仓信号日（如 2026-12-01 收盘后）全市场重排兵布阵 |
| 产物 | **快照目录**（自包含、跑完冻结） | **快照目录**（同上） |
| 下单 | ❌ 执行卡**挂单禁用** | ❌ 仍然**挂单禁用**（除非再加 `--live-trade`） |
| 发信 | ❌ 默认不发 | ❌ 默认不发（除非再加 `--live-trade` ＋ `--send-email`） |
| 耗时 | 视 skip 而定 | **约 35~45 分钟**（数据定型 + S 层 ~27 min + 辩论 ~15 min） |

🔴 **副作用只由 `--live-trade` 决定**：缺省 = 观察态（执行卡挂单禁用、不发信）；`--live-trade` = 实盘态（可下单、可发信）。
🔴 **不静默降级**：任何被跳过的环节都在 `snapshot_manifest.json` 的 `skipped` 里登记，**并且**在报告里披露。
🔴 **「有环节没跑成」不许静默出卡**：`--full` 下 ⇒ **退出码 1 ＋ 不发信 ＋ manifest `pipeline=degraded` ＋ heartbeat `degraded` ＋ 报告披露**（见 §3.1）。
🔴 **唯一例外**：**持仓纪律直接触发**的**紧急平仓**（收盘价有效跌破 MA60，`BREAK_CONFIRM_RATIO = 0.0`）——
它**不经过任何选股路径**，是止损而非选股产物。

🔴 **静默防守窗口**：`2026-11-06` ~ `2026-11-30`（`daily_config.SILENT_DEFENSE_START/END`）—— 期间**严禁**生成任何新仓位指令。

---

## 2. 数据管道：分层存储、频次解耦、严格冷却、主动降速

### 2.1 分层更新矩阵

| 数据层级 | 对应标的 / 数据源 | 真实披露 / 变动周期 | 系统设定更新规则与频率 | 存储形式 | 防封关键策略 |
|:--|:--|:--|:--|:--|:--|
| **ETF 成分股权重矩阵 W** | 候选池 ~250 只 ETF（AkShare） | 季报 / 半年报（1/4/7/10 月末） | **严格 TTL 冷却（45~60 天）**；未过期**直接读本地缓存，网络请求数 = 0** | `etf_holdings.csv` ＋ `meta.json` | 强制休眠 1~2 s、分批打散（每 25 只熔断 5~8 s），**绝不日常重复爬取** |
| **ETF 日线量价行情** | 候选池 ~250 只 ETF（AkShare） | 每个交易日盘后 | 每个交易日**增量追加**，只拉最近 N 天缺失切片 | `etf_prices.csv`（追加去重） | 批量接口请求，单次 30~60 s |
| **个股 Qlib 底层量价** | 全市场 ~5000 只 A 股 | 每个交易日盘后 | **调仓日前夜 / 当天**增量写入 | Qlib 二进制 `cn_data`（`.bin`） | 本地离线维护 / 合规滚动快照 Dump，**不做高频爬虫** |

🔴 **canonical 行情的刷新由研究链负责，不是每日链**：每日链对 `etf_prices.csv` **只读不写**，
自己只做**回看 15 自然日的增量补**（窗口常量在 `daily/fetch_data.py`）⇒ 🔴 **长期停跑会留空洞**：
若每日链连续停跑 **> 15 天**，canonical 的最大日与增量窗口起点之间会留下**永久空洞**
（`load_effective_prices` 只做 `(code, date)` 去重合并、**不检测中间断档**）⇒ 断档期行情**永远不会回来**。
⇒ 缓解只能靠**人工**：执行卡每行带「价格日」列 ⇒ 断档会在单价上暴露；
`probe_data_freshness` 能发现「最新行情日 < 信号日」，但**发现不了中间断档**。
🔴 这是**研究链 canonical 刷新节奏**的问题，不是每日链架构的问题。

### 2.2 防封五铁律（数据层）

1. **TTL 缓存短路**：`etf_holdings_meta.json` 记 `last_updated`；(Today − last_updated) < TTL ⇒ 直接读本地缓存，**网络请求 0 次**；
2. **请求抖动与速率限制**：单标的抓取间隔 `sleep(random.uniform(1.2, 2.5))`；**每连续 25 只熔断** `sleep(random.uniform(5.0, 8.0))`；
3. **指数退避重试**：超时 / 502 / 空返回 ⇒ 最多 **3 次**，间隔递增（3 s → 6 s → 12 s）；单只连续失败 ⇒ **跳过并保留上一期**，不崩主进程；
4. **只在盘后取数一次**：15:30 之后数据定型，**只拉一次**日线；**严禁盘中为监控价格反复调爬虫**；
5. **User-Agent 轮换 ＋ 边爬边落盘**：Header 池轮换桌面浏览器 UA；每爬完一只立即落盘，避免最后一只异常导致整批内存数据丢失。

🔴 **本工程现状**：W 是**只读**的（`daily/fetch_data.py::read_holdings_snapshot()`）—— 新鲜度**只披露、不拦**；
研究链的 `fetch_etf_holdings.py` **没有只读开关** ⇒ **每日链不得调用它**；真要刷新 W ＝ 重活，必须**用户当轮授权**。

### 2.2b 🔴 W（成分股权重）的陈旧度：**现在进 manifest 与报告**（2026-11-10 补）

🔴 **为什么必须披露**：W 来自**只读缓存** `etf_holdings.csv`（铁律：**绝不重爬** ——
重爬成分股会触发 IP 封禁）⇒ 它的陈旧度以前**只进日志**，报告 / manifest 只字未提
⇒ 「实时拉了数据」其实是**假象**：E = W × S 里的 **W 可能还是 N 天前的快照**，而用户无从判断穿透分用的是哪天的权重。

**现在的落点**（阶段 1 之后登记）：

| 落点 | 内容 |
|:--|:--|
| **manifest 阶段 `w_holdings`** | `status` = `stale` / `ok`；detail 带 `max_fetch_date`（W 快照日）/ `stale_days`（滞后天数）/ `mixed`（是否跨期混合）/ `n_codes`（成分只数） |
| **报告 notes** | 若 `stale` ⇒ 追加 🔴：**「W（成分股权重）是只读缓存、不重爬」＋「本轮 E=W×S 的 W 快照日 = YYYY-MM-DD（滞后 N 天）」＋「『实时拉取』覆盖的是行情 ＋ qlib 数据集，不覆盖 W」** |

🔴 **口径**：`stale` 阈值 = `daily_config.HOLDINGS_STALE_DAYS_DISCLOSE` = **5 天**（超过才标 `stale`），
**只披露、不拦**（W 陈旧**不阻止**本轮出卡；它进的是交付面，不是闸门）。

### 2.3 Qlib 数据集刷新：滚动官方快照换库（不是逐日增量写）

阶段 1 的「写 qlib 二进制」在当前形态下**不逐日增量写**，而是**滚动官方快照换库**：

- **源**：`chenditc/investment_data@latest` ⇒ `qlib_bin.tar.gz`（**570 MB**，服务器下载 **114 秒**）；
- **现状**：服务器 `<数据集根>/datasets/qlib/cn_data`（数据集根 = `/mnt/data/siping/datasets/manu/etf_rotation`，
  可用 `ETF_ROTATION_OUTPUT_ROOT` 覆盖）**就是它解出来的**，日历覆盖 **2000-01-04 ~ 2026-11-03**；
- 🔴 **日历前缀一致性 fail-closed 校验**：换库前后**日历前缀必须逐日一致** ⇒ 前缀变了 ⇒ **打分栅格相位漂移**
  ⇒ **拒绝换库**（否则历史 S 层与新 S 层不可比，收益归因全部失真）；
- 单只个股的**逐日增量写**仍保留 `update_qlib_daily_bar()`，但**默认 dry-run** ＋ **复权守卫**（见 §5）。

### 2.3b 🆕 自建增量日兜底（上游滞后时的第二条腿；2026-11-05 新增）

🔴 **为什么需要**：实测（2026-11-05）上游快照的日历末日 **2026-11-03**，比信号日滞后 **2 天**
⇒ 阶段 2 的 S 层**只能打到数据集末日** ⇒ 报告出现「打分日 ≠ 信号日」。
⇒ 换库链路本身**没问题**（它正确地判定「无新增交易日 ⇒ 不换库」），滞后在**上游数据**。

- 🔴 **解法不是改网格相位**：`data/signal_policy.py::score_grid_dates` 有**末日例外**（末位交易日必进栅格）
  ⇒ **只要数据集日历覆盖到信号日**，`qlib_scores --end <信号日>` 就会产出该日分数
  ⇒ 唯一要做的是**把数据集覆盖推到信号日**；
- **工具**：`stage1_rolling_increment.py` —— 用 AkShare **全市场当日快照**自建缺失交易日，
  写到**派生目录** `cn_data_live`（基座 `cn_data` 的**硬链接派生**）；
- 🔴 **只写派生目录**：自建日写进 `cn_data` ⇒ `compare_calendars` 的**日历前缀闸门永远拒绝换库**；
  🔴 **写前 copy-on-write**（断硬链接）⇒ 否则追加写会改到基座 inode；
- 🔴 **基准守卫**：D−1 的 qlib **原始**收盘（close/factor）vs AkShare 原始前收，偏离 > **2%** ⇒ **剔除**；
  停牌/无量 ⇒ 留 NaN；数据集里没有 ⇒ **不新建标的**；
- 🔴 **默认 dry-run**（真写要 `--apply` / 流水线 `--increment-apply`）⇒ 磁盘零改动；
- ✅ **历史缺口也能补（2026-11-05 19:2x 实测）**：**搜狐 `hisHq` 批量通道** —— **8 只/请求**、
  **一次请求覆盖整段区间** ⇒ 全市场 5575 只可延展标的 ≈ **698 个请求**；口径与东财**逐位吻合**
  （不复权原始价 / 成交量=手 / 成交额=万元）；沪深 + 北交所 920xxx 全覆盖
  ⇒ **「当周/当月个股增量」口径可落地**（🔴 上一轮「只能补当天」的结论**已作废**）；
  🔴 实时快照 `stock_zh_a_spot_em`（5927 只 / 59 请求 / 67.2 秒）用于**当天**；
- 🔴 **两个静默失败坑**：搜狐 `start/end` 必须**无横线**（带横线 ⇒ 返回空表不报错）｜
  `str.isdigit()` 对 `YYYY-MM-DD` 恒 False ⇒ 日历被静默清空 ⇒ 全部标的被守卫剔除；
- **写盘口径**（2026-11-05 实测逐位核对，最大相对误差 **7.9e-5**）：
  `close/open/high/low = 原始价 × f_new`；`volume = 原始手数 ÷ f_new`；`amount = 成交额 ÷ 1000`；
  `vwap = 成交额/(手数×100) × f_new`；`change = 原始涨跌幅`；**除权日 `f_new = f_prev × hfq(t)/hfq(t−1)`**；
- **闸门**：`probes/probe_rolling_increment.py`（**18 项**，含 2 个真负例 + 3 个批量源不变量）⇒ `run_gates.sh` 第 20 阶段。
- ✅ **真实构建与实机验收（2026-11-05 18:37 → 11-06 09:0x）**：基座末日 2026-11-03 ⇒ 补 **11-04 + 11-05**
  ⇒ 派生目录 **`cn_data_live`**（852 MB，**基座零改动**）；四项验收全过：
  ① 派生日历末日 = **2026-11-05**｜② **`D.features` qlib 真读得到 11-05**（含北交所）｜
  ③ bin 长度自洽（0 条超日历末日）｜④ **打分栅格末位 = 2026-11-05** ⇒ `--end 2026-11-05` 会打到该日
  ⇒ **「数据集末日落后于信号日」在实机上被关闭**；
- **验收脚本**：`probes/_verify_live_dataset.py`（**只读**，四项验收可随时复跑）。
- 🟢 **增量日由 `--full` 口径触发**：`daily/main.py --asof <date> --full` ⇒ 阶段 1 会**把派生目录补到信号日**
  （只写 `cn_data_live`、基座零改动、可随时重建）⇒ 阶段 2 的 S 层因此**能打到信号日**；
  不需要 S 层的轮次可以 `--no-update-qlib-bin` 关掉增量（否则白跑 700 个请求没意义）；`--increment-dry-run` 只看计划。
- 🔴 **打分栅格口径必须同源**（`rules.md §十四.95`）：相位 `i % every_n == every_n - 1` 的 `i` 是
  **传入列表下标** ⇒ **切片起点决定相位** ⇒ 每日链 `score_grid` 与研究链
  `qlib_scores.trading_calendar(SCORE_START_TIME, cal_end)` **必须同起点**；
  否则「缺分数」判定全错 ⇒ 每轮**误触发 21~27 分钟全量重打分** ＋ 报告误报「落后」。
  ⇒ 同源闸门在 `probes/probe_rolling_increment.py` 的 **G1/G2**（含变异反证）。

### 2.3c 🔴 降级事实现在进交付面（manifest ＋ 报告，2026-11-10 补）

🔴 **旧口径的洞**：阶段 1 的两处降级**只进日志** ⇒ 用户看到的报告/manifest 里**只字未提**
⇒ **整轮悄悄沿用旧 qlib 库、照样出执行卡** ＝ **静默降级**（违反铁律 I4「绝不静默降级」）。

| 事实 | manifest 阶段条目 | 报告 notes |
|:--|:--|:--|
| **「自建增量日」兜底失败** | `stage1_increment` = `failed`（detail 带 `days` / `live_dir` / `error`） | 🔴 追加：**上游 qlib 快照覆盖不到信号日 ⇒ qlib 基座停在旧末日 ⇒ S 层只能打到旧数据**（含失败原因） |
| **上游 qlib 快照没推进（换库零收益跳过）** | `stage1_dataset` = `ok`，detail 带 `calendar_max_before` / `calendar_max_after` / `new_days` / `swapped` / `downloaded` | （不进 notes，但 manifest 里一眼能看出「库没换」） |
| **qlib 数据集刷新整段失败** | `stage1_dataset` = `failed`（带 `error`） | 🔴 追加：**本轮降级用旧数据集继续 ⇒ S 层只能打到旧数据集的覆盖末日** |
| **W 陈旧** | `w_holdings` = `stale` | 🔴 追加 W 快照日与滞后天数（见 §2.2b） |

🔴 **口径**：这几条**都不改**「本轮跑没跑」的判定 —— 它们进的是**交付面**（manifest ＋ 报告），
让「有环节没跑成 / 数据不新鲜」**用户看得见**；`--full` 下的**硬失败**判据见 §3.1。

### 2.4 🆕 快照制：每次运行 = 一个不可变快照目录（2026-11-09 架构瘦身）

**旧的「根目录全局共享文件」形态已经废除**。每日链不再写任何根目录全局文件
（canonical `stock_scores.csv` / `tradingagents_decision.*` / `rebalance_recommendation.json`），
改为**每次运行落一个独立目录**：

```text
<SNAPSHOT_ROOT>/<YYYYMMDD_HHMMSS>/
  # ── 契约 16 件（= probes/probe_snapshot_contract.py::CONTRACT_ARTIFACTS）──
  stock_scores.csv  stock_prices.csv  etf_analysis_report.json|.md
  rebalance_recommendation.json|.csv  ta_research_context.md  ta_context_meta.json
  tradingagents_decision.md|.json  final_rebalance_plan.json|.md
  node_reprice_<YYYYMMDD>.json  execution_card_<YYYYMMDD>.json
  report_<YYYYMMDD>.md  heartbeat_<YYYYMMDD>.json
  # ── 权威索引 ──
  snapshot_manifest.json          # 唯一权威索引（stages / skipped / notes / live_trade / artifacts）
  # ── 过程件（不在契约 16 件里，但确实落在快照目录内）──
  etf_prices_effective_<YYYYMMDD>.csv   # 本轮合并后的行情底座（canonical + 当日增量）
  account_snapshot.csv                   # 账户快照（本轮实盘口径）
  account_context.json                   # 账户上下文（喂给 LLM 的那份）
  stock_scores.csv.bak-<时间戳>          # S 层底座备份（合并前原样留存）
```

🔴 **口径**：快照 = **契约 16 件 ＋ `snapshot_manifest.json` ＋ 若干过程件**（**不是**「只有 16 件」）。
契约清单只看 `probe_snapshot_contract.py::CONTRACT_ARTIFACTS`（正好 16 条）；上面列的**过程件**由流水线顺带写出，
**不在契约校验范围内**（⇒ 契约闸门不因它们变红），但**确实在快照目录里**（⇒ `diff -r` 两份快照时会看到它们）。

- `<SNAPSHOT_ROOT>` 缺省 = `daily_config.SNAPSHOT_ROOT`（env `ETF_ROTATION_SNAPSHOT_ROOT`），
  可用 `--snapshot-root PATH` 覆盖；
- 🔴 **跑完即冻结**：同一秒内连跑两次 ⇒ 后者目录名加后缀，**绝不覆盖**；旧快照**逐字节不变**；
- ⇒ **任意两份快照可以直接 diff**（盘中 vs 盘后、今天 vs 上周）；
- 🔴 **canonical 永远不被每日链写**：它只在「**还没有任何快照时**」当**一次性只读种子**用一次。

📖 **完整架构说明**（为什么废除旧形态、三条硬不变式、副作用矩阵、`--full` 语义、S 层底座链式口径、
与研究链的职责边界、迁移对照表）：**`manu/docs/snapshot_architecture.md`**。

---

## 3. CLI（单一流水线口径）

```bash
python manu/etf_rotation/daily/main.py [选项]
```

🔴 **入口只有一个**。**没有**「模式」分诊 —— 那是 2026-11-09 已废除的旧形态。
跑哪几段 = `--full` ＋ skip 开关；要不要动真格 = `--live-trade`。

| 选项 | 默认 | 语义 |
|:--|:--|:--|
| `--asof YYYY-MM-DD` | 本机当天 | 业务日期（服务器在 Asia/Shanghai） |
| `--full` | 关 | 🔴 **强制走完全部环节**：**5 个语义开关全开**（取数 `fetch` / S 层刷新 `refresh_scores` / qlib 换库 `update_qlib` / 增量补齐 `increment` / LLM 辩论 `llm`）⇒ **覆盖 6 个 CLI 开关**（`--skip-fetch` / `--no-refresh-scores` / `--no-update-qlib-bin` / `--no-increment` / `--increment-dry-run` / `--skip-llm`）。与任何 skip 开关冲突时**以 `--full` 为准**；被覆盖的 skip **必须**登记进 manifest 的 `skipped` **并**在报告披露（**不许静默降级**）。🔴 **硬保证**：有环节没跑成 ⇒ 退出码 1 ＋ 不发信 ＋ manifest `pipeline=degraded` ＋ heartbeat `degraded` ＋ 报告披露（见 §3.1） |
| `--live-trade` | **关** | 🔴 **唯一**实盘副作用开关：执行卡**可下单** ＋ **可发信**。缺省 = 观察态（挂单禁用、不发信） |
| `--dry-run` | 关 | **显式声明「观察态」**（与 `--live-trade` **互斥** ⇒ 同时给 ⇒ **退出码 2**）。🔴 **观察态本来就是缺省行为 ⇒ 本开关不改变任何行为**：跑完照样落快照、照样出报告、照样写执行卡（带挂单禁用标记）。⚠️ **别把它当成「不产出产物」的预演** —— 内部那个「前置判定（不取数 / 不写产物 / 不调 LLM）」参数**不叫 dry-run**，叫 **`preflight_only`** |
| `--snapshot-root PATH` | `daily_config.SNAPSHOT_ROOT` | 快照根目录（env `ETF_ROTATION_SNAPSHOT_ROOT`）；每次运行落 `<root>/<YYYYMMDD_HHMMSS>/` |
| `--report-md PATH` | `manu/report/report_<YYYYMMDD>.md` | Markdown 报告落盘路径（目录可用 `ETF_ROTATION_REPORT_DIR` 覆盖） |
| `--send-email` | **关** | 🔴 **显式**开启邮件推送（**默认不发**；实盘语义需配合 `--live-trade`） |
| `--no-email` | 关 | 兼容保留；默认行为，等价于「不显式 `--send-email`」 |
| `--skip-fetch` | 关 | 跳过取数阶段（用现有数据） |
| `--skip-llm` | 关 | 跳过 TradingAgents 辩论（省 API 配额） |
| `--refresh-scores` / `--no-refresh-scores` | **开** | 是否重跑 S 层（**重活**，~27 min）；`--full` 会强制为开 |
| `--update-qlib-bin` / `--no-update-qlib-bin` | **开** | 是否刷新 qlib 数据集（**重活**）；`--full` 会强制为开 |
| `--run-root` | `daily_config.RUN_ROOT` | 产物目录 |
| `--log-dir` | `daily_config.LOG_DIR` | 日志目录 |
| `--calendar` | `daily_config.TRADE_CALENDAR_CSV` | A 股交易日历 CSV |
| `--send-only` | 关 | 只发已持久化的邮件草稿（**按需补发**时用；不重跑计算） |
| `--wait-seconds` | `1800.0` | `--send-only` 时等待草稿出现的秒数 |
| `--no-increment` / `--increment-dry-run` / `--increment-apply` | 见 §2.3b | 阶段 1 自建增量日的写入口径 |
| `--llm-timeout` / `--reasoning-effort` | 见 `main.py` | LLM 阶段参数 |
| ~~`--mode`~~ | — | 🔴 **已删除**（连参数本身都已删除，旧取值一并作废）。仍传 ⇒ **退出码 2** ＋ stderr 打印迁移提示（指向 `--full` / `--live-trade` / 快照制） |

**默认行为**：渲染 Markdown 报告 ⇒ 落盘 `manu/report/report_<YYYYMMDD>.md` **＋** 快照目录内一份 `report_<YYYYMMDD>.md`
⇒ **同时打印到 stdout**（主 agent 直接把 stdout 贴回窗口）。**默认不发邮件。**
🔴 **快照才是真值**：`manu/report/` 下那份是**交付副本**，跑完即冻结的快照目录才是唯一权威。

### 3.1 `--full` 的硬保证：「有环节没跑成」不许静默出卡（2026-11-10 补）

🔴 **旧口径的洞**：manifest 里明明写着某阶段 `failed`，进程却**照样退出 0**、照样出一份执行卡
⇒ 「有环节没跑成也照样交付」＝ **静默降级**（违反铁律 I4「绝不静默降级」）。

**现在的判据**（`daily/main.py::_record_pipeline_stages()` 返回**失败阶段清单**）：

| 环节 | 判定 |
|:--|:--|
| 各阶段状态 | 统一用 `_stage(name, status, **detail)` 登记进 manifest；`status == "failed"` ⇒ 进**失败阶段清单** |
| `tradingagents` | `--full` **强制 LLM 真跑** ⇒ 状态**只可能是 `ok`**；其它状态（`skipped` / `absent` / `reused` / `unknown`）**一律算失败** |
| `node_reprice` | 🟢 **设计内降级**，**不计入硬失败集**：观察态退化成「只有方向、无数值」的观察卡 ＋ 报告显著披露；实盘态直接崩（fail-closed） |

**有失败时的五处披露**（`--full` 下）：

| 落点 | 表现 |
|:--|:--|
| **退出码** | **1**（fail-closed）｜🔴 **非 `--full` 仍返回 0**（日常 cron 语义不变），但状态一律照记 degraded |
| **发信** | `--full` 下**不发信**（不许拿「有环节缺件」的交付件去触发实盘通知/报单） |
| **manifest** | `pipeline` 阶段 = `"degraded"`，带 `failed_stages` 清单 |
| **heartbeat** | `status = "degraded"` |
| **报告** | notes 追加 🔴「本轮有环节没跑成：…（详见 `snapshot_manifest.json` 的 `stages`）」 |

🔴 **本节与架构正本同口径**：`manu/docs/snapshot_architecture.md §6`。

### 用法示例

```bash
# ① 完整跑（缺省 = 观察态）：现场拉数 → 内存计算 → 落一个快照目录
#    副作用：无（执行卡挂单禁用、不发信）
python manu/etf_rotation/daily/main.py --asof 2026-12-01

# ② 强制全流程（--full）：取数 / S 层刷新 / qlib 换库 / 增量补齐 / LLM 辩论 —— 5 个语义开关全开
#    🔴 与 skip 开关冲突时以 --full 为准，被覆盖的 skip 会登记进 manifest 并在报告披露
#    🔴 硬保证：有环节没跑成 ⇒ 退出码 1 ＋ 不发信 ＋ manifest/heartbeat 记 degraded ＋ 报告披露
#    ⚠️ 含重活（S 层 ~27 min + 辩论 ~15 min）⇒ 必须用户当轮授权
python manu/etf_rotation/daily/main.py --asof 2026-12-01 --full

# ③ 观察态（显式声明）：与缺省同义，与 --live-trade 互斥
python manu/etf_rotation/daily/main.py --asof 2026-12-01 --dry-run

# ④ 实盘态：执行卡可下单、可发信
#    🔴 唯一实盘副作用开关；不想要发信就别加 --send-email
python manu/etf_rotation/daily/main.py --asof 2026-12-01 --full --live-trade

# ⑤ 指定快照根（默认 daily_config.SNAPSHOT_ROOT）
python manu/etf_rotation/daily/main.py --asof 2026-12-01 --snapshot-root <结果根>/snapshots

# ⑥ 显式发邮件（默认不发；按需）
python manu/etf_rotation/daily/main.py --asof 2026-11-05 --send-email

# ⑦ 只发已持久化的邮件草稿，不重跑任何计算
python manu/etf_rotation/daily/main.py --asof 2026-11-05 --send-only
```

🔴 **老命令会被拒**：`--mode`（连参数本身都已删除，旧取值一并作废）一律**退出码 2** ＋ stderr 迁移提示。
新旧对照表见 `manu/docs/snapshot_architecture.md` §9。

### 退出码

| 码 | 含义 |
|:--|:--|
| **0** | 成功（**含**「非交易日静默退出 0」） |
| **1** | 失败（异常 ⇒ 捕获 Traceback ⇒ 落盘失败报告）**或**（`--full` 下）**有环节没跑成** ⇒ fail-closed（不发信、manifest/heartbeat 记 degraded、报告披露；见 §3.1） |
| **2** | **用法或环境错误**：argparse 未知参数 / **`--mode` 被拒**（旧命令，打印迁移提示）/ `--live-trade` 与 `--dry-run` 互斥 / 锁冲突（已有实例在跑） |

---

## 4. 报告输出（回窗口的东西）

落盘**两处**，内容一致：

| 落点 | 路径 | 角色 |
|:--|:--|:--|
| **快照目录**（真值） | `<SNAPSHOT_ROOT>/<YYYYMMDD_HHMMSS>/report_<YYYYMMDD>.md` | 🔴 **权威副本**：跑完即冻结，与快照内其它件**同代** |
| **交付目录**（副本） | `manu/report/report_<YYYYMMDD>.md`（服务器即 `/tmp/pycharm_project_658d3fc4/manu/report/`；可用 env `ETF_ROTATION_REPORT_DIR` 改目录，或用 `--report-md` 指定绝对路径） | 便于**直接贴回窗口 / 拉回 host 看**的副本 |

🔴 **快照才是真值**：`manu/report/` 那份是**副本**；要复盘「当时到底算了什么」，看快照目录
（`snapshot_manifest.json` 里有每个件的 sha1 ＋ `stages` / `skipped` / `notes` / `live_trade`）。

内容按**是否实盘态**分：

- **观察态**（缺省 / `--dry-run`）：执行卡买卖明细表（代码 / 名称 / 方向 / 目标股数 / 预估金额）＋ 调仓后仓位分布 ＋ 现金流 ＋ notes
  ＋ 风险披露 ＋ 🔴 **S 层数据日**（**必须披露用的是哪个打分日** —— 复用旧 S 层时这是最容易被忽略的坑）
  ＋ 🔴 **本次跳过了哪些环节**（`skipped` 披露）；
- **实盘态**（`--live-trade`）：同上，且执行卡**带可下单语义**（无挂单禁用标记）。

🔴 **数字纪律**：报告里每个数字都必须来自**实际产物**；字段缺失渲染成 **「—」**，**不许反推、不许编**。

---

## 5. 重活开关（默认开；`--full` 强制开）

### `--refresh-scores`（重跑 S 层，约 27 分钟）

- S 层 = `manu/etf_rotation/qlib_scores.py --mode infer`，产出 `stock_scores.csv`（列：`date, instrument, score, fwd_ret`），下游 `E = W × S` 直接消费它。
- 🔴 **它的落盘是整体覆盖 `stock_scores.csv`，不是增量**；产物只含 `--start ~ --end` 栅格上的日期 ⇒ 拿它当增量用会**抹掉已有的打分日**。
- 因此 `daily/run_qlib_score.py` **默认只读**（只报告 S 层新鲜度与栅格缺哪几天）；
  真刷新须**用户当轮授权**（铁律一）。
- 🔴 **快照制下的落点**：刷新后的分数**只写进本轮快照**（`stock_scores.csv`），
  **不再合并回 canonical** ⇒ canonical 永远不被每日链写；
  下一轮的底座走**链式口径**：优先**上一份快照**的 `stock_scores.csv`，没有快照时才用 canonical 作一次性只读种子
  （详见 `manu/docs/snapshot_architecture.md` §7）。
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

### 8.1 快照目录（🔴 真值）

每次运行落**一个独立目录** `<SNAPSHOT_ROOT>/<YYYYMMDD_HHMMSS>/`，
**自包含**、**跑完即冻结**、**绝不覆盖**：

```text
<SNAPSHOT_ROOT>/<YYYYMMDD_HHMMSS>/
  # ── 契约 16 件（= probes/probe_snapshot_contract.py::CONTRACT_ARTIFACTS）──
  stock_scores.csv                # 本轮 S 层分数（合并后的全历史）
  stock_prices.csv                # 个股日线导出（阶段 2b）
  etf_analysis_report.json|.md    # 阶段 3
  rebalance_recommendation.json|.csv  # 规则层（本轮现跑）
  ta_research_context.md + ta_context_meta.json
  tradingagents_decision.md|.json # 阶段 4
  final_rebalance_plan.json|.md   # 阶段 5
  node_reprice_<YYYYMMDD>.json
  execution_card_<YYYYMMDD>.json # 阶段 6
  report_<YYYYMMDD>.md            # 交付报告副本
  heartbeat_<YYYYMMDD>.json
  # ── 权威索引 ──
  snapshot_manifest.json          # 唯一权威索引
  # ── 过程件（不在契约 16 件里）──
  etf_prices_effective_<YYYYMMDD>.csv   # 本轮合并后的行情底座
  account_snapshot.csv                   # 账户快照（本轮实盘口径）
  account_context.json                   # 账户上下文（喂 LLM 的那份）
  stock_scores.csv.bak-<时间戳>          # S 层底座备份（合并前原样留存）
```

- `<SNAPSHOT_ROOT>` 缺省 = `daily_config.SNAPSHOT_ROOT`（env `ETF_ROTATION_SNAPSHOT_ROOT`，
  缺省 `ETF_ROTATION_RESULTS_ROOT/"snapshots"`），可用 `--snapshot-root PATH` 覆盖；
- `snapshot_manifest.json` 里记 `stages` / `skipped` / `notes` / `live_trade` / `artifacts`（字节数 ＋ sha1）；
- 🔴 **canonical 永远不被每日链写**：只作为「还没有任何快照时的一次性只读种子」；
- 🔴 **口径**：快照 = **契约 16 件 ＋ `snapshot_manifest.json` ＋ 若干过程件**（**不是**「只有 16 件」）：
  契约校验只看那 16 条；过程件**确实落在快照目录里**（`diff -r` 两份快照时会看到），
  但**不在契约校验范围内**（不因它们变红）。

### 8.2 交付副本与运行侧产物

| 产物 | 路径 | 角色 |
|:--|:--|:--|
| **Markdown 报告（副本）** | `manu/report/report_<YYYYMMDD>.md`（可 env `ETF_ROTATION_REPORT_DIR` / `--report-md` 改路径） | **交付副本**（真值在快照目录里） |
| **拉回 host** | 🔴 流水线在**服务器**跑 ⇒ 报告天然落在服务器 `manu/report/`；**PyCharm 打开的是 host 工程** ⇒ 跑完必须 `bash manu/tools/pull_reports.sh` 拉回 host（md5 双侧比对），否则 host 上看不到报告 | — |
| 运行日志 | `daily_config.LOG_DIR`（`<结果根>/logs/`）下 `daily_run_YYYYMMDD.log`（格式 `时间 \| 级别 \| 阶段 \| 消息`） | 运行侧 |
| 锁文件 | `daily_config.RUN_ROOT` 下 `.lock`（`daily_config.LOCK_FILE`，flock 单实例守卫） | 运行侧 |
| 邮件草稿 / 已发回执 / dry-run `.eml` | `daily_config.RUN_ROOT` 下（`email_draft_<YYYYMMDD>.json` / `sent_<YYYYMMDD>T<HHMMSS>.json` / `<YYYYMMDD>T<HHMMSS>_<主题>.eml`） | 邮件通道侧（**未纳入快照契约**） |

🔴 **每日流水线只读研究链产物**：`<结果根>/stock_scores.csv`（S 层 canonical，
**只读**，仅在无快照时当一次性种子）、`<数据集根>/datasets/etf/etf_holdings.csv`（W，**只读**）、
`<数据集根>/datasets/qlib/cn_data`（qlib 二进制）。

🔴 **两个落点根**（服务器 `huangzhe@192.168.99.40` 是共享机 ⇒ 本项目只许落这两棵树下）：
`<数据集根>` = `/mnt/data/siping/datasets/manu/etf_rotation`（env `ETF_ROTATION_OUTPUT_ROOT`）
⇒ 行情/权重/日历/qlib 数据集 + 临时工作区 `_work/`；
`<结果根>` = `/mnt/data/siping/runs/etf_rotation_results`（env `ETF_ROTATION_RESULTS_ROOT`）
⇒ 产物/快照/报告/日志（**留在服务器，不再拷回 host**）。

📖 快照制的完整架构说明见 `manu/docs/snapshot_architecture.md`。

---

## 9. 模块职责一览

| 文件 | 职责 |
|:--|:--|
| `main.py` | **唯一入口**：六阶段编排、CLI、flock 守卫、退出码、报告渲染、失败兜底（**无模式分流**） |
| `snapshot.py` | **快照层**：建快照目录 / 写件 / 登记 `stages`·`skipped`·`notes` / `finalize()` 写 manifest / 解析 S 层底座 / 根目录全局文件指纹（只读审计） |
| `daily_config.py` | **全部常量的单一事实源**（路径、账户口径、组合约束、均线窗口、节点日、SMTP、阈值）；**`SNAPSHOT_ROOT` 在此**（`SHADOW_ROOT` 已随影子推演链废除删除） |
| `calendar_gate.py` | 交易日历门（**fail-closed**） |
| `fetch_data.py` | 盘后增量：ETF 日线（shell out 到 akshare env）、**W 只读**（陈旧度由 `main.py` 登记进 `w_holdings` 阶段 ＋ 报告 notes）、qlib 二进制可选增量（默认 dry-run） |
| `run_qlib_score.py` | S 层**新鲜度判定**（默认只读）＋ 可选刷新 |
| `agents_rebalance.py` | **单一路径**的计算主体 ＋ 执行卡组装（**无模式分支**；观察态 / 实盘态由 `live_trade` 参数决定）；内部「前置判定」参数叫 **`preflight_only`**（**不叫 dry-run**） |
| `notify_email.py` | 四套邮件模板 ＋ SMTP 客户端（0600 secrets 读取、权限守卫、dry-run 落 `.eml`） |
| `account_state.py` | **实盘账户基准**（30,035.57 元口径）＋ 三道校验 ＋ `DISCREPANCY_NOTE` |
| `tests/` | pytest 单测（`conftest.py` 把 `daily/` 挂进 `sys.path`） |

🔴 **模式判定不再是每日链的一环**：「模式」分诊已废除（2026-11-09 架构瘦身），
`--mode` 被拒（退出码 2）；「跑哪几段」由 `--full` ＋ skip 开关决定，「要不要动真格」由 `--live-trade` 决定。

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
- **快照架构说明**：`manu/docs/snapshot_architecture.md`（为什么废除「根目录全局共享文件」、三条硬不变式、副作用矩阵、`--full` 语义、S 层底座链式口径、与研究链的职责边界、老命令迁移对照表）。
- 快照契约闸门：`manu/etf_rotation/probes/probe_snapshot_contract.py`（S1~S10）。
