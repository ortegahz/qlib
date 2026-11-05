# `manu/etf_rotation/data/` —— ETF 原始数据抓取层

本目录只做一件事：**从公开数据源把 ETF 侧原始数据抓下来，落到服务器持久盘**。
不做特征、不做打分、不做回测（那些在 `manu/etf_rotation/engine/` 与 `manu/etf_rotation/recommend.py`）。

## 目录

| 文件 | 角色 |
| --- | --- |
| `fetch_etf_universe.py` | 阶段 1：抓全市场快照 → 选候选池 |
| `fetch_etf_prices.py` | 阶段 2：抓候选池的日线历史 |
| `fetch_etf_holdings.py` | 阶段 3：抓候选池的成分股权重矩阵 |
| `README.md` | 本文件（字段口径说明） |

## 落盘位置

| 产物 | 路径 |
| --- | --- |
| 4 个 CSV + 2 个 manifest | `/mnt/data/siping/datasets/etf/` |
| 运行日志 | `/mnt/data/siping/runs/etf_rotation/` |

## 执行环境（**硬约束**）

- 服务器 `huangzhe@192.168.99.40:32222`（ssh 必须加 `-F /dev/null`）。
- 解释器 **`/home/huangzhe/anaconda3/envs/ak/bin/python`**（独立 `ak` env：
  akshare 1.20.25 / pandas 3.1.0 / numpy 2.5.4）。
- **严禁**把 akshare 装进 `/home/huangzhe/anaconda3/envs/ql/`（qlib 的 env）。
- 数据一律服务器直下，host 不做任何代理转发。

## 复现命令

```bash
# 0) 本地改完先同步（铁律三：判据只能看 md5 双侧比对）
cd /media/manu/1TB-Volume/workspace/qlib
bash manu/tools/sync_to_server.sh manu/etf_rotation/data

# 1) 快照 + 候选池
ssh -F /dev/null -p 32222 huangzhe@192.168.99.40 \
  "cd /tmp/pycharm_project_658d3fc4 && \
   /home/huangzhe/anaconda3/envs/ak/bin/python manu/etf_rotation/data/fetch_etf_universe.py --stage all"

# 2) 日线历史（候选池的临时集合）
ssh -F /dev/null -p 32222 huangzhe@192.168.99.40 \
  "cd /tmp/pycharm_project_658d3fc4 && \
   /home/huangzhe/anaconda3/envs/ak/bin/python manu/etf_rotation/data/fetch_etf_prices.py --resume"

# 3) 用真实 20 日均额重算候选池（必须夹在 2 和 4 之间）
ssh -F /dev/null -p 32222 huangzhe@192.168.99.40 \
  "cd /tmp/pycharm_project_658d3fc4 && \
   /home/huangzhe/anaconda3/envs/ak/bin/python manu/etf_rotation/data/fetch_etf_universe.py --stage finalize"

# 4) 成分股权重
ssh -F /dev/null -p 32222 huangzhe@192.168.99.40 \
  "cd /tmp/pycharm_project_658d3fc4 && \
   /home/huangzhe/anaconda3/envs/ak/bin/python manu/etf_rotation/data/fetch_etf_holdings.py --resume"
```

> **执行顺序不可交换**：候选池的流动性门槛最终以「20 个交易日日均成交额」判定，
> 该值只有抓完行情才知道 ⇒ 必须 `prices` → `finalize` → `holdings`。

---

## 抓取纪律（三脚本共用）

1. **限速**：每次请求之间 `sleep` 0.2~0.5 s（随机），不打爆 eastmoney。
2. **重试**：每只 ETF 每数据源最多重试 3 次，指数退避 **1 / 2 / 4** 秒；
   仍失败 ⇒ 记入 manifest 的 `failed` 清单，**不中断整批**。
3. **断点续跑**：`--resume` 按已落盘 CSV 的 `code` 去重跳过。
4. **原子写**：先写 `.tmp` 再 `os.replace`，杜绝半截文件。
5. **噪声控制**：全程 `warnings.filterwarnings("ignore")`；akshare 的 tqdm 用
   `contextlib.redirect_stderr` 吞掉。
6. **docstring**：三脚本均在文件头写明数据源 / 字段含义 / 单位 / 复权方式 / 口径 / 抓取时间 / 执行机。

---

# 本次抓取实况（2026-11-04）

**执行机**：`test-Super-Server`（`huangzhe@192.168.99.40:32222`），
解释器 `/home/huangzhe/anaconda3/envs/ak/bin/python`（Python 3.12.16）
**抓取窗口**：2026-11-04 15:49 ~ 16:32 (UTC+8)　**行情日期**：2026-11-04

| 产物 | 行数 | 覆盖 | 备注 |
| --- | --- | --- | --- |
| `etf_spot.csv` | 1664 | 全市场 ETF | 38 列（akshare 原 37 + `exchange`）；SH 913 / SZ 751 |
| `etf_universe.csv` | 1664 | `in_universe` = 250 | `fetch_candidate` = 296（抓行情的临时集合） |
| `etf_prices.csv` | 306656 | 296 只 | 覆盖最终 250 只的 **250/250 = 100%** |
| `etf_holdings.csv` | 26831 | **235 / 250 = 94.0%** | `tx` 25621 行 / `pcf` 1210 行 |

## 门槛与筛选结果

| 项 | 值 |
| --- | --- |
| 过流动性门槛（全市场口径） | 529 只 |
| 过规模门槛 | 861 只 |
| 过类型门槛 | 1664 只（`LOF`/`分级`/`退` 关键词在 spot 里 **0 命中**，故无一被剔除） |
| 过 120 交易日门槛 | 292 只（296 只抓过行情，其中 **4 只不足 120 日被挡掉**） |
| `needs_review`（申赎状态非「开放」） | 7 只 |
| 入选候选池的最小 20 日均成交额 | 131,753,342 元（≈ 1.32 亿元） |
| 入选候选池的交易日数范围 | 181 ~ 1656 天 |

## 候选池类型分布（含防守资产）

| `fund_type_bucket` | 全市场 1664 | `in_universe` 250 |
| --- | --- | --- |
| `equity_cn`（指数型-股票） | 1533 | 188 |
| `cross_border`（指数型-海外股票） | 87 | 45 |
| `money_market`（货币型-普通货币） | 27 | 4 |
| `commodity`（指数型-其他：黄金/有色/能源化工/豆粕） | 17 | 13 |

## 权重和校验

- 每只 ETF 的 `weight_pct` 求和：**234 / 235 只落在 95~105**（99.6%）。
- `tx` 源 220 只：权重和全部 = 100.0000。
- `pcf` 源 15 只：min 97.111 / median 100.016 / max 105.224（14/15 在 95~105）。
- 唯一越界：**`518880` 黄金ETF华安 = 105.2236**（单一商品标的 `Au9999`，
  申购替代金额含溢价所致；**如实保留，未做归一化**）。

## 抓取耗时

| 阶段 | 只数 | 总耗时 | 平均每只 |
| --- | --- | --- | --- |
| `prices`（296 只 × 3 年日线，全量） | 296 | **244.9 s (4.08 min)** | **0.83 s** |
| `holdings`（250 只成分股，全量） | 250 | **411.1 s (6.85 min)** | **1.64 s** |
| `holdings --resume`（只补 15 只缺口） | 15 | 72.5 s | 4.84 s |

⇒ **后续若要扩池**：价格侧极便宜（~0.8 s/只 ⇒ 1000 只也只要 ~14 min），
成本大头在成分股（~1.6 s/只 ⇒ 1000 只约 27 min）。**扩到 500~600 只完全在可接受范围内。**

## 未能抓到权重的 15 只

| 类别 | 只数 | 代码 | 原因 |
| --- | --- | --- | --- |
| `no_holdings` | 8 | `511880` `511990` `511660` `159001`（货币 ETF）+ `159934` `159937` `159831` `159834`（黄金 ETF） | 两个源都只返回「申赎现金」占位行 ⇒ 无 A 股成分（货币 ETF 常态；部分商品 ETF 的 PCF 同样不披露实物篮子） |
| `failed` | 7 | `159655` `159577` `159866` `159980` `159981` `159985`（境外 / 商品标的）+ `159382` | 前 6 只：akshare 的 tx 解析因境外代码格式报错、且 PCF 的 `申购替代金额` 全为 0 ⇒ 无法还原权重；`159382` 两个源都报 `证券数量应为非负数`（上游 PCF 原始数据有负数量，akshare 直接抛错） |

---

# 产物字段说明

## 1. `etf_spot.csv` —— 全市场场内 ETF 实时快照

- **来源**：`ak.fund_etf_spot_em()`（东方财富），**原样落盘** 37 列，额外补 1 列 `exchange`。
- **行数**：全市场场内 ETF（实测 1664 只）。
- **行粒度**：1 只 ETF = 1 行。

| 列 | 含义 | 单位 |
| --- | --- | --- |
| `代码` | 6 位交易代码 | — |
| `名称` | ETF 简称 | — |
| `最新价` | 最新成交价 | 元/份 |
| `IOPV实时估值` | 盘中参考净值 | 元/份 |
| `基金折价率` | 折价率（负=溢价） | % |
| `涨跌额` / `涨跌幅` | 当日涨跌 | 元 / % |
| `成交量` | 成交量 | **手**（1 手 = 100 份） |
| `成交额` | 当日成交金额 | **元** |
| `开盘价`/`最高价`/`最低价`/`昨收`/`振幅` | 当日行情 | 元 / % |
| `换手率` | 换手率 | % |
| `量比`/`委比`/`外盘`/`内盘` | 盘口指标 | 倍 / % |
| `主力净流入-净额` … `小单净流入-净占比` | 资金流（10 列） | 元 / % |
| `现手`/`买一`/`卖一` | 盘口 | 手 / 元 |
| `最新份额` | 场内份额 | 份 |
| `流通市值` / `总市值` | 规模 | **元** |
| `数据日期` | 行情日期（`YYYY-MM-DD`） | — |
| `更新时间` | 快照时间戳 | — |
| **`exchange`** | **新增列**：`代码` 首位派生 → `SH`(5/6/9) / `SZ`(1/2/3) / `BJ`(4/8) | — |

> ⚠️ **单位提醒**：`成交额`、`流通市值`、`总市值` 都是 **元**。
> 「5000 万成交额」= `5e7` 元；「2 亿规模」= `2e8` 元。

---

## 2. `etf_universe.csv` —— 可交易候选池

- **来源**：`etf_spot.csv` + `ak.fund_name_em()`（细粒度基金类型，ETF 100% 覆盖）
  + `ak.fund_etf_category_ths(symbol="ETF基金")`（粗类型 + 申购/赎回状态）。
- **行粒度**：1 只 ETF = 1 行（**全市场 1664 行都在**，用 `in_universe` 标记是否入选）。

### 筛选口径（硬门槛 = doc2「过滤网 1」）

| 门槛 | 判据 | 列 |
| --- | --- | --- |
| 流动性 | 近 20 个交易日 **日均成交额 ≥ 5000 万元**（`5e7` 元） | `pass_liquidity` |
| 规模 | `流通市值 ≥ 2 亿元`（`2e8` 元） | `pass_size` |
| 上市时长 | 交易日数 ≥ **120**（供 120 日动量） | `pass_history` |
| 类型 | 名称含 `LOF`/`分级`/`退` ⇒ 剔除；**其余类型一律保留** | `pass_type` |
| 候选池规模 | 按 20 日均成交额降序，**上限 250 只** | `in_universe` / `rank` |

### 必需列

| 列 | 含义 | 单位 |
| --- | --- | --- |
| `code` | 6 位交易代码 | — |
| `name` | ETF 简称 | — |
| `exchange` | SH / SZ / BJ | — |
| `fund_type` | 基金类型（细粒度，来自 `fund_name_em`） | — |
| `avg_amount_20d` | **20 个交易日日均成交额** | **元** |
| `float_mcap` | 流通市值 | **元** |
| `last_price` | 最新价 | 元/份 |
| `liq_source` | `avg_amount_20d` 的来源：`prices`（真实 20 日均）/ `spot`（退回首日成交额） | — |
| `n_days_history` | 抓到的交易日数 | 天 |
| `pass_liquidity` | 是否过流动性门槛 | bool |
| `pass_size` | 是否过规模门槛 | bool |
| `in_universe` | 是否进入最终候选池 | bool |
| `fetch_date` | 抓取日期 | `YYYY-MM-DD` |

### 附加诊断列

| 列 | 含义 |
| --- | --- |
| `fund_type_coarse` | 同花顺粗类型（`股票型`/`债券型`/`货币型`/`其他`） |
| `fund_type_bucket` | 防守资产分桶：`equity_cn` / `cross_border` / `bond` / `money_market` / `commodity` / `other`（**仅供参考，不参与筛选**） |
| `fund_type_src` | 类型来自 `fund_name_em` 还是 `category_ths` |
| `purchase_status` / `redeem_status` | 申购 / 赎回状态（`开放`/`暂停`/`限制大额`/空） |
| `pass_type` | 是否过类型门槛 |
| `pass_history` | 是否过 120 交易日门槛 |
| `needs_review` | 申赎状态异常（暂停 / 限制大额）⇒ **保留但需人工复核** |
| `exclude_reason` | 剔除原因（如 `名称含'退'`） |
| `fetch_candidate` | 是否进入「抓历史行情」的临时集合（按当日成交额排序取前 300，给 finalize 重排留余量） |
| `rank` | 最终候选池内按 20 日均额降序的排名 |
| `rank_by_spot_amount` | 按快照当日成交额降序的全市场排名 |
| `spot_amount` | 快照当日成交额 | **元** |
| `total_mcap` | 总市值 | **元** |
| `turnover_rate` | 换手率 | % |
| `last_price_date` | 该 ETF 最后一条行情日期（查数据是否陈旧） | `YYYY-MM-DD` |

> **两阶段口径**：
> `--stage universe` 用快照当日成交额近似日均（`liq_source=spot`），先定出 300 只的临时抓取集合；
> `--stage finalize` 读 `etf_prices.csv` 算真实 20 日均额与交易日数，按真实值重排并截断到 250 只。

---

## 3. `etf_holdings.csv` —— 成分股权重矩阵 W 的原始料

- **主源**：`ak.fund_etf_holdings_tx(symbol=<code>)`（腾讯）⇒ `source=tx`，
  `weight_pct` 直接取接口的 `市值占比`（已归一到 0~100）。
- **备源**：`ak.fund_etf_pcf(symbol=<code>)`（基金公司申赎清单）⇒ `source=pcf`，
  PCF 无「最新价」列 ⇒ 单价 = `申购替代金额 / 证券数量`，
  `weight_pct` = `证券数量 × 单价 / 申赎单位净值 × 100`。
- **行粒度**：1 条成分 = 1 行。

| 列 | 含义 | 单位 |
| --- | --- | --- |
| `etf_code` | 所属 ETF 代码 | — |
| `etf_name` | 所属 ETF 简称 | — |
| `stock_code` | 成分证券代码 + `.SH`/`.SZ`/`.BJ` 后缀 | — |
| `stock_name` | 成分证券简称 | — |
| `weight_pct` | 占 ETF 净值的百分比，**数值本身即 0~100 的百分数**（如 `3.9464`） | % |
| `source` | `tx` / `pcf` | — |
| `fetch_date` | 抓取日期 | `YYYY-MM-DD` |
| `stock_exchange` | SH / SZ / BJ / 空（境外或商品标的） | — |
| `holding_type` | `stock` / `bond` / `other` | — |
| `security_qty` | 成分数量 | 股 / 份 |
| `price_used` | 采用的单价 | 元 |
| `mkt_value` | 成分市值（`数量 × 单价`） | 元 |
| `weight_pct_recalc` | 用 `数量×单价` 重新归一后的权重（交叉校验用） | % |

> **后缀优先级**：接口的 `市场代码`（101→SH, 102→SZ, 103→BJ）优先；
> 接口缺失时才回落代码前缀规则（`6`→SH，`4`/`8`→BJ，其余→SZ）。
> 境外 / 商品标的（如 `Au9999`）无 A 股后缀，保留原码。

### `etf_holdings_manifest.json`

每只 ETF 一条：成分数 `n_holdings`、权重和 `weight_sum` / `weight_sum_recalc`、
抓取来源 `source`、状态 `status`、错误信息、耗时 `elapsed_s`。

`status` 三态：

| status | 含义 |
| --- | --- |
| `ok` | 抓到 ≥1 条**带有效权重**的成分 |
| `no_holdings` | 两个源都只返回「申赎现金」占位行 ⇒ 该 ETF 本身没有股票成分（典型：货币 ETF） |
| `failed` | 抓取报错，**或**两个源都返回了成分行但**没有可用估值**（见限制 3） |

顶层统计：`n_ok` / `n_no_holdings` / `n_failed`，
外加 `failed` 清单、`no_holdings` 清单、`per_code` 明细、总耗时、平均每只耗时。

---

## 4. `etf_prices.csv` —— 日线历史

- **来源**：`ak.fund_etf_hist_em(symbol=<code>, period="daily",
  start_date="20200101", end_date="20261104", adjust="qfq")`。
- **复权**：**前复权（qfq）** ⇒ open/high/low/close 都是前复权价，
  与盘口「最新价」同尺度；**不可**再与不复权序列混用。
- **区间**：`2020-01-01 ~ 2026-11-04`（参数 `--start` / `--end`）。
- **行粒度**：1 个交易日 = 1 行。

| 列 | 含义 | 单位 |
| --- | --- | --- |
| `date` | 交易日 | `YYYY-MM-DD` |
| `code` | ETF 代码 | — |
| `open`/`high`/`low`/`close` | 前复权价 | 元/份 |
| `volume` | 成交量 | **手**（1 手 = 100 份） |
| `amount` | 当日成交额 | **元** |

> `成交量` 单位实测：`Σ成交额 / (Σ成交量 × 100)` ≈ 区间均价 ⇒ 成交量是「手」。

### `etf_prices_manifest.json`

每只 ETF 一条：行数 `rows`、首末日期 `first_date`/`last_date`、成功/失败 `status`、
错误信息、耗时 `elapsed_s`；顶层另有 `failed` 清单、总耗时、平均每只耗时。

---

## 已知口径限制（如实告知）

1. **`weight_pct` 统一存 0~100 的百分数**（如 `3.9464`），不是 0~1 的比例；
   下游做矩阵运算时记得 `/100`。
2. **货币 ETF / 纯现金 ETF 没有真成分**：`fund_etf_pcf` 只返回「申赎现金」占位行
   （`SSXJ` / `159900` / 名称含 `申赎现金`），脚本剔除后判为 `no_holdings` 并记入 manifest，
   这是**正确行为**，不是 bug。
3. **境外 / 商品标的 ETF 拿不到权重（已知缺口）**：这类 ETF 的
   - `fund_etf_holdings_tx` 报 `境外证券代码需要对应的行情来源`（腾讯没有境外行情）；
   - `fund_etf_pcf` 的 `申购替代金额` 全为 `0`（基金公司不披露境外 / 商品标的的现金替代金额），
     `赎回替代金额` 也全为 `0`。

   ⇒ 两个源都**无法还原权重**，脚本判为 `failed`（错误信息里写明
   `no_value` 行数）**并把这些行从 CSV 中剔除**，不会把「权重和 = 0」的脏数据塞给下游。
   受影响的典型代码：`159655`（恒生科技，503 只境外成分）、`159980` / `159981` / `159985`
   （商品 / 期货 ETF）、`159866`。
   ⇒ **若下游需要这些 ETF 的权重矩阵，得另找数据源**（如基金公司官网 PCF 文件、
   `ak.fund_portfolio_hold_em` 季报持仓），本脚本不做第三源回退。
4. **商品 ETF 的「成分」是商品本身**（如黄金 ETF 的 `Au9999`），
   `holding_type=other`，不是 A 股股票。
5. **腾讯 `市值` 列与 `证券数量 × 最新价` 存在 ±1% 级差异**（少数成分的 `证券数量`
   与 `市值` 口径不同），故 `weight_pct` 以接口的 `市值占比` 为准，
   `weight_pct_recalc` 仅作交叉校验。
6. **PCF 源的权重和一般落在 95~98 而非 100**：申赎篮子含少量现金
   （`申赎单位净值 − Σ申购替代金额 = 现金差额`），归一后必然小于 100。
   个别（如黄金 ETF 518880）因替代金额含溢价会略超 100。
7. **`etf_spot.csv` 的 `成交额` 是「当日」额**，不是 20 日均值；
   只有 `liq_source=prices` 的行才是真正的 20 日均额。
8. **数据陈旧**：个别 ETF 可能停牌 / 退市导致最后行情日期早于 `2026-11-04`，
   用 `last_price_date` 列核查。
9. **PCF 源拿到的境外成分无法定价**：`stock_code` 无 `.SH/.SZ` 后缀
   （如 `A000660` = SK hynix），`holding_type=other`。
   下游若要用这些权重，必须自备境外行情。
