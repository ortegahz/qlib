# A 股截面打分 + Top-K 选股（2026-11-04）

> 专题档：本文件记录 **2026-11-04** 在全量 A 股 cn_data 上跑通的第一条量化实验线的完整事实：
> 数据口径、特征/标签/模型配置、实测指标、Top5 打分清单，以及
> **为什么必须偏离 qlib 官方 example 配方**（每条带证据）。
> 铁律见 `manu/memory/rules.md`（尤其 §12）；环境见 `manu/memory/env_setup_20261104.md`；
> qlib 0.9.8 的 API 坑见 `manu/memory/qlib_api_findings_20261104.md`。
> **工程**：`/media/manu/1TB-Volume/workspace/qlib/`（自研代码全在 `manu/` 扩展层，上游 `qlib/` **零改动**）。

---

## 1. 实验目的与结论摘要

### 1.1 目的

在**真实 A 股日频数据**上跑通 qlib 的「**截面打分 → Top-K 组合 → 含成本回测**」全链路，
回答三个问题：

1. qlib 的 Alpha158 + LightGBM 在 A 股上能不能产出**可统计显著的截面预测力**（IC / ICIR）；
2. 官方 example 的配方（early stopping + 强 L1/L2 + CSZScoreNorm 标签）**能不能直接搬**；
3. 在**真实成本**下，这套 alpha 还剩多少。

### 1.2 结论摘要

| # | 结论 | 关键数字 |
|:--|:--|:--|
| 1 | **链路跑通**，产出可复现的 manifest + Top5 清单 | 全程耗时 **116.3 s**（manifest `elapsed_sec`），recorder `0fdde80304d94becbf3b0a061c3f61c9` |
| 2 | **截面预测力成立且显著** | test 日度 RankIC **0.038116**，ICIR **0.2550**，t = **6.665**，683 个交易日 |
| 3 | 🔴 **成本几乎吃掉全部 alpha** | 不含成本年化超额 **+6.40%**（IR 0.567，MDD -13.05%）→ 含成本仅 **+0.35%**（IR 0.031，MDD -23.14%） |
| 4 | **官方配方在本数据上必须改**，共 7 处 | 早停会把模型裁成 1~2 棵树（IC≈0）；标签口径换成截面排名后 IC 翻倍 |
| 5 | **Top5 打分清单已产出** | 截面日 2026-11-03，universe=csi300，当日有打分标的 M=**299** |
| 6 | ⚠️ **只做了 LightGBM 一族** | **尚未**做跨模型族（GRU / 线性 / XGBoost）对照 ⇒ 模型族选择**未验证**，不得当结论用 |

> ⚠️ **Top5 的语义（每次引用都必须带上）**：`top5.json` 是**模型在截面日的打分排名**
>（`kind=model_score_ranking`），**不是策略的实际持仓组合，也不是投资建议**。

> ✅ **第 3 条已部分解决（2026-11-04 降换手 sweep，见 §4.7）**：
> 把年化单边换手从 **41.1 倍**降到 5~20 倍后，test 段含成本年化超额
> **+0.35% → +0.38% ~ +11.83%**（推荐 `C3_top50_rebal10` / `B3_rebal20` / `A3_ndrop1_hold5`）。
> ⚠️ **但仍不能说「策略已可用」**：冲击成本未建模（`impact_cost=0.0`）、跨模型族未对照、
> valid→test 秩相关仅 **0.13~0.36** ⇒ 详见 §4.7.6 / §4.7.7。
>
> 🔴 **2026-11-04 晚：本节数字已按「确定性补丁后」的重跑轮次全面更新**（原值来自补丁前一轮，
> 跨进程不可复现，产物已归档改名 `*_prefix_unreproducible.*`）⇒ 引用本档数字请先看 §4.7.3 事实 ⑧。

---

## 2. 数据与六项口径

### 2.1 数据来源与实测覆盖

| 项 | 值 |
|:--|:--|
| `provider_uri` | `/mnt/data/siping/datasets/qlib/cn_data`（绝对路径，不依赖 `~/.qlib` 默认值） |
| 主源 | `chenditc/investment_data` release `latest` → `qlib_bin.tar.gz`，**570,483,992 B**，md5 `3010e494e31de446877894059ceb9503`，解压 **852 MB** |
| 备用源 | `SunsetWolf/qlib_dataset` tag `v3` → `qlib_data_cn_1d_latest.zip`，**240,739,135 B**，md5 `8e6ebb2aa6ce9fe0efd5cb88716f0471` |
| 取数清单 | `/mnt/data/siping/datasets/qlib/fetch_manifest.json`（写进 manifest 的 `data_version`） |
| 交易日历 | **2000-01-04 ~ 2026-11-03**，共 **6501** 个交易日 |
| 标的数 | **6174** 只（含北交所） |
| 日频字段 | 10 个：adjclose / amount / change / close / factor / high / low / open / volume / vwap |
| 股票池文件 | all / csi300 / csi500 / csi800 / csi1000 / csiall —— **无 csi100** |

### 2.2 六项口径（`rules.md §12.1` 要求逐项点名）

| 维度 | 本次取值 |
|:--|:--|
| **① 数据区间** | train **2016-01-01 ~ 2021-12-31** / valid **2022-01-01 ~ 2023-12-31** / test **2024-01-01 ~ 2026-11-03**；数据加载 `start_time=2015-01-01`（**留 1 年预热**，因 Alpha158 最长回看 60 个交易日） |
| **② 标的池** | **csi300**（中大盘）；同源另有 csi500/csi800/csi1000/csiall |
| **③ 成本假设**（qlib `Exchange` 7 参数） | `limit_threshold=0.095`、`deal_price=close`、`open_cost=0.0003`、`close_cost=0.0013`、`min_cost=5`、`trade_unit=100`、`impact_cost=0.0` |
| **④ 组合约束** | `TopkDropoutStrategy`：`topk=30`、`n_drop=5`、`only_tradable=False`、`forbid_all_trade_at_limit=True`；调仓频率 = qlib 默认（每日）；**无杠杆、不做空**、无权重上限/换手上限 |
| **⑤ 基准** | **SH000300**（同区间、同成本口径算出的超额） |
| **⑥ 资金** | 初始资金 **1,000,000**，qlib 默认复利方式 |
| **region**（§12.1 附加） | `cn`（`REG_CN`）⇒ `limit_threshold` / `trade_unit` / 交易日历随 region 变化，**跨 region 数字不可直接比** |

### 2.3 fit 窗口（`rules.md §12.7`）

- `fit_start_time = 2016-01-01`、`fit_end_time = 2021-12-31` ⇒ **严格落在 train 段内**。
- 该窗口只有被**接受 `fit_start_time`/`fit_end_time` 形参**的处理器消费时才真正生效：
  `infer_processors` 里的 `ZScoreNorm` 接受 ⇒ 归一化统计量只在 train 窗口上拟合
  （`qlib/contrib/data/handler.py:12-31` `check_transform_proc` 注入窗口；
  `qlib/data/dataset/handler.py:588-607` 在 fit 区间上拟合）。
- ⇒ **评估区间不参与归一化统计**，无归一化泄漏。

### 2.4 预热期为什么必需

- qlib 会自动向前扩展取数窗口（`qlib/data/data.py:855-856`
  `query_start = max(0, start_index - lft_etd)`），但**扩展不出 `start_time` 之前的数据**。
- Alpha158 最长回看 60 天（`Ref($close, 59)`）⇒ 若 `start_time == train[0]`，训练段开头 60 天
  的特征是 NaN → `Fillna` 填 0 的**垃圾值** ⇒ 这里把 `start_time` 提到 2015-01-01，留 1 年。

---

## 3. 特征 / 标签 / 模型配置

### 3.1 特征

| 项 | 值 |
|:--|:--|
| 特征集 | **Alpha158**（`qlib.contrib.data.handler`），**158 列**（实测 `n_cols=158`，与 `DLWParser` 解析长度一致） |
| `infer_processors` | `ProcessInf` → `ZScoreNorm(fields_group="feature")` → `Fillna(fields_group="feature", fill_value=0)` |

> ⚠️ `Fillna` **必须**限定 `fields_group="feature"`：loader 产出的是 MultiIndex 列
>（level0 = feature / label），不限 group 会把 **label 的 NaN 填成 0** ⇒ 后续 `DropnaLabel` 失效
>（末位交易日无未来数据却不会被剔除）。

### 3.2 标签

| 项 | 值 |
|:--|:--|
| 表达式 | `Ref($vwap, -2)/Ref($vwap, -1) - 1` |
| 名字 | `LABEL0` |
| 含义 | **T+1 的 VWAP → T+2 的 VWAP** 的收益（`N<0` 看未来是 label 的合法用法，`rules.md §12.2`） |
| `learn_processors` | `DropnaLabel` → `CSRankNorm(fields_group="label")` |

> 🔴 **口径说明（与 manifest 字段不一致，见 §7.6）**：`Alpha158` 的**类默认**标签是
> **close** 口径（`qlib/contrib/data/handler.py:151-152`），**VWAP** 口径来自
> `Alpha158vwap`（`:155-157`）。本次**通过 handler 的 `label` 形参显式覆盖**成 VWAP
>（`qlib/contrib/data/handler.py:122` `kwargs.pop("label", self.get_label_config())`）
> ⇒ **实际生效的标签是 VWAP**。
>
> 🔴 **标签用 `CSRankNorm` 而非默认的 `CSZScoreNorm`**：原始标签重尾（实测 min **-7.70** / max **+10.31**），
> z-score 后极端值仍主导 MSE 梯度；改成**截面排名**后模型学的是「相对排名」，对尾部稳健
> ⇒ valid IC **0.0154 → 0.0330**（翻倍）。
>
> 🔴 **标签用 VWAP 而非 close**：valid IC 对比
> **vwap_rank 0.0330 > close_rank 0.0269 > vwap_zscore 0.0154 > close_zscore 0.0089**。
>
> ⚠️ **PnL 结算价与标签价口径不同**：回测 `deal_price=close`，标签用 VWAP
> ⇒ **区间一致，但价格口径略有差异**（见 §7.3）。

### 3.3 模型

| 项 | 值 |
|:--|:--|
| 模型类 | `qlib.contrib.model.gbdt.LGBModel` |
| LightGBM | **4.7.0** |
| `loss` | `mse` |
| `learning_rate` | **0.02** |
| `num_boost_round` | **500**（固定，**不用早停**） |
| `early_stopping_rounds` | **`None`（必须关）**，理由见 §6.1 |
| `colsample_bytree` | 0.88 |
| `subsample` | 0.87（**必须配 `bagging_freq=1` 才生效**，LightGBM 默认 `bagging_freq=0` = 关闭 bagging） |
| `bagging_freq` | **1** |
| `lambda_l1` / `lambda_l2` | **0 / 0**（**必须置 0**，理由见 §6.4） |
| `max_depth` / `num_leaves` | 8 / 210 |
| `num_threads` | 20（服务器 80 核） |
| 种子 | `random_state=42`、`deterministic=True`、`force_row_wise=True`（**qlib 层无 seed 参数**，见 `rules.md §12.3`） |

**实测产物校验**（服务器上从 `model.pkl` 反查）：booster **500 棵树**（`current_iteration()=500`、
`dump_model()["tree_info"]` 长度 500）、**158 个特征**（`booster.num_feature()=158`）。

---

## 4. 实测结果

### 4.1 运行标识与产物

| 项 | 值 |
|:--|:--|
| `recorder_id` | `0fdde80304d94becbf3b0a061c3f61c9` |
| 产物目录 | `/mnt/data/siping/runs/ashare_topk/runs/0fdde80304d94becbf3b0a061c3f61c9/` |
| 目录内容 | `label.pkl` / `model.pkl` / `pred.pkl` / `manifest.json` / `top5.json` |
| 全程耗时 | **116.3 s**（manifest `elapsed_sec`） |
| 版本 | qlib **0.9.8** / python **3.12.16** / numpy **2.5.4** / pandas **2.3.3** / lightgbm **4.7.0** |

产物 md5（服务器侧实测复核，与 manifest 一致）：

| 文件 | md5 |
|:--|:--|
| `label.pkl` | `c903ad50e4c6b64f6b216bd277a9f22d` |
| `model.pkl` | `929407d5e6a51c8d689339adeb1e5772` |
| `pred.pkl` | `fa2b2939771fe6202fb68cbb777c0586` |

### 4.2 打分质量（日度 RankIC）

| 段 | IC（均值） | ICIR | IC 标准差 | t 值 | 样本量（交易日） |
|:--|--:|--:|--:|--:|--:|
| valid | **0.038557** | **0.3205** | 0.1203 | **7.051** | **484** |
| test | **0.038116** | **0.2550** | 0.149464 | **6.665** | **683** |

> test 段回测区间 **2024-01-02 ~ 2026-11-03**（首日 2024-01-01 为非交易日，顺延到 01-02）。
> IC 样本量 683 个交易日；`pred.pkl` 覆盖 test 段 **685** 个交易日
>（差额来自末位交易日无未来标签、被 `DropnaLabel` 剔除）。
>
⚠️ 选参**只看 valid IC**、**不看 test**（`rules.md §12.7`）⇒ test 段未被污染。

### 4.3 数据质量与覆盖率 M/N

| 段 | 行数 M | 列数 | 标的数 | 全 NaN 列 | 首日 | 末日 | 覆盖率 M/N |
|:--|--:|--:|--:|--:|:--|:--|:--|
| train | 438,207 | 158 | 556 | 0 | 2016-01-04 | 2021-12-31 | **0.9998** |
| valid | 145,200 | 158 | 363 | 0 | 2022-01-04 | 2023-12-29 | **1.0000** |
| test | 205,279 | 158 | 365 | 0 | 2024-01-02 | 2026-11-03 | **0.9989** |

> 特征列数 **158** 与 Alpha158 表达式条数一致（`rules.md §12.4` 的三方对齐：解析长度 = `fetch()` 实返列数）。
> learn 段样本数：train **427,639** / valid **145,135**。
>
⚠️ **标的数远小于 300**（train 556 / valid 363 / test 365 是**区间内出现过**的标的数；
> csi300 在末日只有 **300** 只，其中当日**有打分**的只有 **299** 只）⇒
> 覆盖率 M/N 是**行级**口径，标的级缺口的来源是停牌/无行情，**引用时不要混用两种分母**。

### 4.4 回测（test 段，Top-K 组合）

| 口径 | 年化超额 | IR | 最大回撤 |
|:--|--:|--:|--:|
| **不含成本** | **+6.40%** | **0.567** | **-13.05%** |
| **含成本** | **+0.35%** | **0.031** | **-23.14%** |

🔴 **这是本次实验最重要的结论**：**成本把几乎全部 alpha 吃掉了**
（年化超额 6.40% → 0.35%，IR 0.567 → 0.031），而且**最大回撤反而放大**（-13.05% → -23.14%）
⇒ 含成本后的组合**不具备实用价值**，须先解决换手（见 §7.1）。

### 4.5 🔴 换手率与成本分解（补测，2026-11-04）

> 脚本：`manu/experiments/ashare_topk_selection/turnover_analysis.py`（md5 `e2e62fd45fa08703bf693210f80942bd`）
> 产物：`<run 目录>/turnover.json` + `turnover.daily.csv`
> 方法：用 **qlib 真实 `TopkDropoutStrategy`** 重跑一次同口径回测（未重训），
> 从 `Account.hist_positions`（bar 收盘、成交后、已按收盘价重估）取真实持仓路径。

| 项 | 数值 |
|:--|--:|
| 单边换手定义 | `0.5 × Σ_i \|w_i(t) − w_i(t−1)\|`，`w_i` = 标的市值 / 组合总资产 |
| **稳态日均单边换手** | **0.1646**（16.46% / 日） |
| **年化单边换手（×250）** | **41.1 倍**（双边 82.3 倍） |
| 分年年化单边换手（×250） | 2024 **41.51** / 2025 **40.61** / 2026 **41.36** |
| qlib `report['turnover']`（双边成交额/上日总资产）均值 | **0.3184**（与自算逐日相对误差 2.9e-13） |
| 日均买入 / 卖出金额 | **202,884 元** / **201,841 元**（账户 100 万 ⇒ 日均双边成交约 40% 总资产） |
| 日均成交笔数 / 日均交易成本 | **9.79 笔** / **323 元**；3 年累计交易成本 **221,432 元** |
| 日均成本率（qlib 口径） | **0.00025419** |
| **年化成本拖累（换手×费率推算）** | **6.0497 pp** |
| **manifest 实测落差（不含成本 − 含成本）** | **6.0497 pp**（6.4038% → 0.3540%） |
| **残差** | **1.5e-14 pp** ⇒ **落差的 100% 由换手 × 显式费率解释，无其它来源** |

**结论**：§4.4 里那 6.05 个百分点的塌陷**不是模型失效，也不是 bug**，而是
**日均 16.5% 的单边换手 × (0.0003 佣金 + 0.0013 印花税+佣金) 的必然结果**。
`impact_cost=0.0` ⇒ qlib 口径下冲击成本缺口为 0，但**真实市场冲击未建模**，
故这 6.05 pp 只代表**显式佣金+印花税**，是真实成本的**下界**。

### 4.6 证据留存（服务器 `/mnt/data/siping/runs/ashare_topk/`）

| 路径 | 内容 | 作用 |
|:--|:--|:--|
| `runs/0fdde80304d94becbf3b0a061c3f61c9/` | **最终产物**：`label.pkl` / `model.pkl` / `pred.pkl` / `manifest.json` / `top5.json` / `turnover.json` / `turnover.daily.csv` / `turnover_sweep.json` / `turnover_sweep.csv` / `valid_stability.json` | 本档全部数字的来源 |
| `runs/0fdde80304d94becbf3b0a061c3f61c9/turnover_sweep_prefix_unreproducible.json` / `.csv` / `valid_stability_prefix_unreproducible.json` | ⚠️ **补丁前那一轮的归档产物**（跨进程不可复现，**不可作为数字来源**） | 历史留证，见 §4.7.3 事实 ⑧ |
| `../turnover_sweep_v2.log` | **打确定性补丁后的重跑日志**（含 sweep + valid_stability 两段） | §4.7 全部数字的来源 |
| `../turnover_sweep.log` / `../valid_stability.log` | ⚠️ **补丁前**那一轮的两个日志（同目录归档） | 历史留证 |
| `probes/` | `probe_full.py` `probe_grid.py` `probe_label.py` `probe_015000.py`（超参与标签口径探针） | §6.2/§6.5 的网格选参脚本原件（原先只在 `/tmp`，已归档） |
| `../ashare_topk_smoke/runs/d963b97eaef645df99baf11cbe146e37/` | **官方配方冒烟 run**：`early_stopping_rounds=10` + close 标签 + 100 轮 | ⚠️ **反面证据**：该 run 的 `top5.json` 里 5 只票**分数完全相同**（0.0036815333604822056），即 §6.1 描述的退化模型 ⇒ **不要把它当成有效 Top5** |

**复现断言（11 项全 PASS）**：交易日数 685 与 qlib 日历一致；稳态持仓数中位数 30、
p05 29、最大 32；自算双边换手 vs qlib report 最大相对误差 2.9e-13；
**重跑回测与 manifest 的年化超额相对误差 4.8e-15（不含成本）/ 1.1e-13（含成本）**
⇒ 本节数字与 §4.4 同源、可复现。

⚠️ **两个年化因子并存，勿混用**：换手年化用 **250**（交易日/年）；manifest 的年化超额由
qlib `risk_analysis(freq="1day")` 算出，日频年化因子是 **238**（`qlib/contrib/evaluate.py:56`）
⇒ 与 manifest 对账必须用 238。

---

### 4.7 🔴 降换手 sweep（只改组合构造的受控 A/B，2026-11-04）

> 本节回答 §4.5 留下的问题：**换手压下来之后，含成本超额能回到什么量级？**
> 设计上是**受控 A/B**（`rules.md §12.7`）：**信号 / 模型 / 数据 / 成本假设全部固定、不重训**，
> **只改组合构造参数**；**选参只看 valid 段**，**test 段只作样本外报告**。

#### 4.7.1 实验设计与六项口径

| 项 | 取值 / 约束 |
|:--|:--|
| **被测变量** | **只有**组合构造 4 个参数：`topk` / `n_drop` / `hold_thresh` / `rebalance_every` |
| **固定不动** | **信号 / 模型 / 数据 / 成本假设全部固定，不重训**（`model.pkl` 直接 unpickle 后 `predict`，见 §4.7.3 事实 ④） |
| **选参区间** | **valid 段 2022-01-01 ~ 2023-12-31**（回测实际交易日 2022-01-04 ~ 2023-12-29，**484** 天） |
| **报告区间** | **test 段 2024-01-02 ~ 2026-11-03**（**685** 个交易日）—— **全程不参与选参**，只作样本外报告 |
| **网格** | **14 组 arm**（脚本里固定，运行时临时增删）× 2 段回测 = **28 次 qlib 真实回测**；整轮耗时 **907.6 s**（补丁后轮次；补丁前 871.8 s） |
| **脚本对配置的写入** | **零**：`config.py` 只读引用，脚本只输出排序表与推荐第一名，**不写回任何配置**（产物 `semantics.config_mutated=false`） |

**六项口径（`rules.md §12.1` 逐项点名，与 §2.2 完全一致）**：

| 维度 | 本轮取值 |
|:--|:--|
| **① 数据区间** | valid 2022-01-01 ~ 2023-12-31（实际 2022-01-04 ~ 2023-12-29，484 个交易日）/ test 2024-01-02 ~ 2026-11-03（685 个交易日）；**含样本外区间，但样本外不参与选参** |
| **② 标的池** | **csi300**（同 §2.2） |
| **③ 成本假设**（qlib `Exchange` 7 参数） | `limit_threshold=0.095`、`deal_price=close`、`open_cost=0.0003`、`close_cost=0.0013`、`min_cost=5`、`trade_unit=100`、`impact_cost=0.0` —— **与 §2.2 逐项相同** |
| **④ 组合约束** | `only_tradable=False`、`forbid_all_trade_at_limit=True`、`risk_degree=0.95`（**qlib 默认，未改**）；`topk`/`n_drop`/`hold_thresh`/`rebalance_every` 按 arm 变；**无杠杆、不做空**、无权重上限/换手上限 |
| **⑤ 基准** | **SH000300**（同区间、同成本口径算出的超额） |
| **⑥ 资金** | 初始资金 **1,000,000**，qlib 默认复利方式 |
| **region**（§12.1 附加） | `cn`（`REG_CN`）⇒ 换手 / 成本数字**不可跨 region 直接比** |

**产物路径（服务器 `/mnt/data/siping/runs/ashare_topk/`）**：

| 路径 | 内容 |
|:--|:--|
| `runs/0fdde80304d94becbf3b0a061c3f61c9/turnover_sweep.json`（+ `.csv`） | 14 组 arm × 2 段的全部指标 + **173** 条断言 + valid 排序表 + 推荐第一名 |
| `runs/0fdde80304d94becbf3b0a061c3f61c9/valid_stability.json` | valid 段**分年**（2022 / 2023）稳定性，**不引用 test** |
| `turnover_sweep_v2.log` | 两个脚本的运行日志（补丁后轮次） |
| 脚本 md5（**本地与服务器实测一致**） | `turnover_sweep.py` `c24f1804649df1650168b267556c4908` ／ `valid_stability.py` `0a8b6b9800f6cb960af888875ccf8286` ／ `rebalance_interval_strategy.py` `46f4d2af472e75bc5d4aae06370d446d` ／ **`determinism.py` `da70d4e471c0c05fd379eda68ed800f0`** |

#### 4.7.2 新增代码（本轮已加确定性补丁，见 §4.7.3 事实 ⑧）

| 脚本 | 作用 |
|:--|:--|
| `manu/experiments/ashare_topk_selection/determinism.py` | **确定性补丁**（运行期把 `Position.get_stock_list` 改成字典序）；上游 `qlib/` **源码零改动**；三个回测脚本在**每次回测前**调用它 |
| `manu/experiments/ashare_topk_selection/rebalance_interval_strategy.py` | 自研 `TopkRebalanceIntervalStrategy`：`TopkDropoutStrategy` 的子类，加 `rebalance_every` —— 每 N 个交易日才调仓，其余日**零交易**（返回空决策 ⇒ 连交易成本都不产生） |
| `manu/experiments/ashare_topk_selection/turnover_sweep.py` | 14 组 arm × valid/test 两段的 sweep；含控制组复现断言与 173 条口径断言；**在 `run_arm_backtest()` 内调用确定性补丁** |
| `manu/experiments/ashare_topk_selection/valid_stability.py` | valid 段**分年**（2022 / 2023）稳定性检验，**不引用 test**；**经 `turnover_sweep.run_arm_backtest` 传递生效**确定性补丁 |

- `rebalance_every` 的相位取 `1 % N` ⇒ **第一次建仓仍落在 step 1**，与原生策略对齐。
- 调仓日仍走父类逻辑，信号取**上一个交易日**的分数（父类 `get_step_time(trade_step, shift=1)`）
  ⇒ **不引入未来函数**，与原生策略**同口径**。

#### 4.7.3 关键工程事实（8 条，均带源码 / 实测证据）

**① 🔴 qlib 原生 `TopkDropoutStrategy` 每个交易日卖 / 买各 `n_drop` 只**

- 源码 `qlib/contrib/strategy/signal_strategy.py:220-232`：
  `sell = last[last.isin(get_last_n(comb, self.n_drop))]`、
  `buy = today[: len(sell) + self.topk - len(last)]`，
  且 `get_last_n(li, n) = list(li)[-n:]`。
- ⇒ **换手与 `n_drop` 同向，`n_drop` 越大换手越高**（本轮 14 组实测再次确认）。

**② 🔴 `n_drop=0` 是「退化」而不是「买入持有」**

- `only_tradable=False` 分支里 `get_last_n(li, 0) = list(li)[-0:]` ⇒ 返回**整个列表**
  ⇒ `sell` = 全部持仓 ⇒ **每天清仓、次日全量重建**。
- 实测：年化单边换手 **217.1 倍**、含成本年化超额 **-27.28%**、最大回撤 **-83.50%**。
- ⇒ 本档一律写作 **`A7_buyhold`（退化）**，**不得叫 buy-and-hold**；
  产物 `arm_grid[].note` 里已写明这条口径（arm_id 本身有误导性，引用时必须带「退化」二字）。

**③ `hold_thresh`（qlib 原生「最短持有期」）只挡卖、不挡买**

- 源码 `signal_strategy.py:242`：`if current_temp.get_stock_count(code, bar=time_per_step) < self.hold_thresh: continue`
  —— 这个检查**只在 sell 循环里** ⇒ 买不受约束 ⇒ **持仓数可能略高于 `topk`**。

**④ valid 段信号不重训**

- `LGBModel.fit` 只存 `self.model`（booster）、**不持 dataset** ⇒ 直接 unpickle `model.pkl`，
  对同一个 dataset 调 `model.predict(dataset, segment="valid")` 即可。
- `predict` 走 `data_key=DK_I`（infer 特征），归一化统计量仍取 fit 窗口（2016-01-01 ~ 2021-12-31）
  ⇒ **评估区间不参与归一化统计**，无泄漏（`rules.md §12.7`）。

**⑤ ✅ 同模型交叉校验（证明两段信号同源）**

- 用**同一个 `model.pkl`** 对 test 段重新 `predict`，与 run 目录里现成的 `pred.pkl` **逐值比对**：
  **205,279 行，最大绝对差 0.0，0 行超 1e-9**。
- ⇒ 证明 **valid 信号与 test 信号出自同一模型与同一套特征处理** ⇒ 两段可比。

**⑥ ✅ 控制组复现（本轮口径与原实验一致）**

- `A0_baseline`（`topk=30` / `n_drop=5` / `hold_thresh=1` / `rebalance_every=1`）在 test 段**精确复现** manifest：
  不含成本年化超额 **0.064037723169097**（相对误差 **4.1e-15**）、
  含成本 **0.0035404311951419986**（相对误差 **8.0e-14**）、
  稳态日均单边换手 **0.1646**（相对误差 **4.8e-05**）。
- ⇒ 本轮口径与 §4.4 / §4.5 **完全一致**，**可比**。

**⑦ 断言：共 173 条，4 条失败、0 条致命**

- 产物 `assertions`：`fatal_failures = []`、`all_passed = true`（4 条失败为**非致命**）。
- 4 条失败**全部**落在退化臂 `A7_buyhold`（持仓数中位数 **15 ≠ topk±5**、p05 **12 / 8 < topk-10**）
  ⇒ **是预期信号，不是 bug**（正是事实 ② 的直接体现）。

#### 4.7.4 完整结果（14 组 arm × 2 段）

口径见 §4.7.1。arm 参数 = `(topk, n_drop, hold_thresh, rebalance_every)`；
「毛」= 不含成本年化超额、「净」= 含成本年化超额（均对 SH000300）；换手 = 年化**单边**换手(×250)；成本 = 年化成本拖累(pp)。

| arm | 参数 | valid毛 | valid净 | valid IR | valid换手 | test毛 | test净 | test IR | test MDD | test换手 | test成本 |
|:--|:--|--:|--:|--:|--:|--:|--:|--:|--:|--:|--:|
| A0_baseline | 30,5,1,1 | +11.61% | +5.48% | 0.732 | 41.8 | +6.40% | **+0.35%** | 0.031 | -23.14% | 41.1 | 6.05pp |
| A1_ndrop3 | 30,3,1,1 | +9.82% | +6.09% | 0.780 | 26.0 | +6.64% | +2.94% | 0.271 | -23.41% | 25.8 | 3.70pp |
| A2_ndrop1 | 30,1,1,1 | +7.90% | +6.70% | 0.888 | 9.6 | +8.33% | +7.07% | 0.711 | -13.67% | 9.9 | 1.26pp |
| A3_ndrop1_hold5 | 30,1,5,1 | +13.00% | **+12.03%** | **1.615** | 8.0 | +7.74% | +6.72% | 0.635 | -17.99% | 8.4 | 1.03pp |
| A4_ndrop1_hold10 | 30,1,10,1 | +6.14% | +5.37% | 0.671 | 6.6 | +5.00% | +4.12% | 0.319 | -24.14% | 7.5 | 0.88pp |
| A5_ndrop1_hold20 | 30,1,20,1 | +5.02% | +4.41% | 0.487 | 5.7 | +10.63% | +10.10% | 0.917 | -12.44% | 5.3 | 0.54pp |
| A6_ndrop2_hold10 | 30,2,10,1 | +11.08% | +9.67% | 0.957 | 10.9 | +4.67% | +3.34% | 0.267 | -19.27% | 10.4 | 1.33pp |
| **A7_buyhold（退化）** | 30,0,1,1 | +0.14% | -32.12% | -3.316 | 212.9 | +5.69% | -27.28% | -2.051 | -83.50% | 217.1 | 32.97pp |
| B1_rebal5 | 30,30,1,5 | +15.95% | +10.55% | 1.408 | 36.7 | +10.66% | +5.34% | 0.511 | -17.96% | 36.3 | 5.32pp |
| B2_rebal10 | 30,30,1,10 | +6.90% | +4.09% | 0.537 | 19.8 | +12.12% | +9.33% | 0.889 | -11.36% | 19.8 | 2.79pp |
| B3_rebal20 | 30,30,1,20 | +10.05% | +8.60% | 1.156 | 10.9 | +13.26% | **+11.83%** | **1.138** | -10.12% | 10.9 | 1.42pp |
| C1_top50_ndrop1 | 50,1,1,1 | +7.06% | +6.30% | 1.062 | 6.6 | +1.88% | +1.15% | 0.132 | -16.12% | 6.4 | 0.74pp |
| C2_top50_hold10 | 50,1,10,1 | +9.58% | +9.00% | 1.282 | 5.4 | +2.48% | +1.89% | 0.197 | -17.20% | 5.5 | 0.59pp |
| C3_top50_rebal10 | 50,50,1,10 | +10.20% | +7.69% | 1.132 | 17.5 | +9.63% | +7.15% | 0.837 | -11.47% | 17.6 | 2.48pp |

> 「毛 / 净」两列是**相对基准 SH000300 的年化超额**；IR 与 MDD 都是**含成本**口径。
> 换手 = 年化**单边**换手（×250 交易日）；成本 = 年化成本拖累（pp），取自产物
> `cost_reconciliation.recomputed.gap_pp`（`A1_ndrop3` 的精确值为 **3.694931 pp** ⇒ 严格两位小数为 3.69pp）。
> ⚠️ **`A7_buyhold` 不是买入持有**，是退化臂（§4.7.3 事实 ②）；保留它是为了给「换手失控」留一个反例锚点。

#### 4.7.5 valid 分年稳定性 + 秩相关统计

**valid 分年（2022 / 2023 两年，含成本年化超额；只用 valid，**不引用 test**）**：

| arm | 2022 | 2023 | 最差年 min | 极差 spread |
|:--|--:|--:|--:|--:|
| C3_top50_rebal10 | +7.60% | +7.78% | **+7.60%** | 0.18pp |
| A3_ndrop1_hold5 | +18.29% | +7.23% | +7.23% | 11.07pp |
| A6_ndrop2_hold10 | +12.93% | +6.42% | +6.42% | 6.51pp |
| B3_rebal20 | +10.88% | +6.33% | +6.33% | 4.55pp |
| A2_ndrop1 | +4.83% | +9.32% | +4.83% | 4.50pp |
| A0_baseline | +6.62% | +4.33% | +4.33% | 2.29pp |
| A5_ndrop1_hold20 | +12.37% | +3.75% | +3.75% | 8.62pp |
| A1_ndrop3 | +3.28% | +8.91% | +3.28% | 5.63pp |
| C1_top50_ndrop1 | +10.86% | +1.73% | +1.73% | 9.13pp |
| B2_rebal10 | +6.48% | +1.71% | +1.71% | 4.77pp |
| B1_rebal5 | +21.23% | **-0.13%** | -0.13% | 21.36pp |
| A4_ndrop1_hold10 | +10.94% | **-0.20%** | -0.20% | 11.14pp |
| C2_top50_hold10 | +16.76% | **-1.00%** | -1.00% | 17.76pp |
| A7_buyhold（退化） | -35.99% | -28.26% | -35.99% | 7.73pp |

**秩相关统计（Spearman，14 组 arm）**：

| 判据 vs test 段含成本年化超额 | ρ | 读法 |
|:--|--:|:--|
| valid **整段**净超额 | **0.393** | 弱预测 ⇒ 在 valid 上取 argmax 不可靠 |
| valid **分年最差值** `min_year_net` | **0.468** | 更强 ⇒ **跨子区间稳健是更好的选参判据** |
| 年化单边换手 | **-0.266** | 弱负相关 ⇒ 换手越高越差（与 §4.5 结论同向） |

> ρ 的口径：`valid 整段净超额` 取 `valid_stability.json` 的 `whole_valid.net_annualized`；
> `min_year_net` 取同文件的 `min_year_net`；`test 净超额` 与换手取 `turnover_sweep.json`。
> ⚠️ `turnover_sweep.json` 自己的 valid 净超额序列在 **4 个 arm** 上与 `valid_stability.json` 不一致
> （见 §4.7.7 未决项 4）⇒ **本表统一用 `valid_stability.json` 的 valid 口径**。

**换手 → 含成本超额的门槛效应**：
**12 个降换手 arm（A1–A6 / B1–B3 / C1–C3）的 test 含成本超额全部 ≥ 基线的 2 倍**；
14 组里只有**基线自身 `A0`（= 1× 基准）**与**退化臂 `A7`（净超额为负）**不满足。

#### 4.7.6 结论与推荐

1. 🔴 **降换手有效**：把年化单边换手从 **41.1 倍**降到 **5~20 倍**，
   test 段含成本年化超额从 **+0.35%** 提到 **+1.15% ~ +11.83%**；
   **且不只是省了成本** —— `A3` 的**毛**超额也从 +6.40% 升到 **+7.74%**
   ⇒ **「持有高分股更久」本身也提高了 alpha 捕获率**。
2. ⚠️ **valid 整段取 argmax 不可靠**：valid 排名第一的 `A3` 在 test 只有 **+6.72%**，
   valid 排第 5 的 `B3` 在 test 达 **+11.83%**
   ⇒ 14 组里挑 1 组存在**选择性偏差**（Spearman 仅 **0.393**）。
3. ✅ **用分年最差值选更稳**：`min_year_net` 最高的三组 `C3`(+7.60%)、`A3`(+7.23%)、`B3`(+6.33%)
   在 test 上分别拿到 **+7.15% / +6.72% / +11.83%**
   ⇒ **「valid 上跨子区间稳健」是有效的选参判据**。
4. ✅ **推荐口径（三条，按口味选）**：
   - **稳健优先 → `C3_top50_rebal10`**（topk=50、每 10 个交易日全量刷新）：valid 两段几乎完全一致
     （**+7.60% / +7.78%**，极差仅 **0.18pp**），test 净 **+7.15%**、IR **0.837**、MDD **-11.47%**、换手 **17.6 倍**。
   - **收益优先 → `B3_rebal20`**（topk=30、每 20 个交易日全量刷新）：valid 净 +8.60%（min +6.33%），
     test 净 **+11.83%**、IR **1.138**、MDD **-10.12%**、换手 **10.9 倍**。
   - **最低换手 → `A3_ndrop1_hold5`**（topk=30、n_drop=1、hold_thresh=5）：valid 净第一 **+12.03%**（min +7.23%），
     test 净 +6.72%、IR 0.635、MDD -17.99%、换手仅 **8.4 倍**（**基线的 1/5**）。
5. ⚠️ **仍不能说「策略已可用」**：valid→test 的秩相关只有 **0.47**，
   **单条曲线不足以证明长期有效**；`impact_cost=0.0` ⇒ **真实市场冲击未建模**，
   上述成本只含显式佣金 + 印花税，是真实成本的**下界**；
   本轮**仍未做跨模型族对照**（GRU / 线性 / XGBoost，见 §6.7）。
6. ⚠️ **`A3` 的 valid 优势含 2022 单年尖峰**（+18.29% vs 2023 +7.23%）
   ⇒ 引用其 valid 数字时必须**同时给分年拆解**。

#### 4.7.7 遗留风险与未决项

| # | 项 | 说明 |
|:--|:--|:--|
| 1 | ⚠️ **单条曲线不足以证明长期有效** | valid→test 秩相关仅 0.39~0.47；14 组里挑 1 组存在选择性偏差 |
| 2 | ⚠️ **真实市场冲击未建模** | `impact_cost=0.0` ⇒ 表里的成本只含显式佣金 + 印花税，是真实成本的**下界** |
| 3 | ⚠️ **跨模型族对照仍未做** | GRU / 线性 / XGBoost（服务器未装 `torch` / `xgboost`）⇒ 模型族选择**未验证**（§6.7） |
| 4 | ⚠️ **两个脚本的 valid 口径不一致（未决）** | `turnover_sweep.json` 与 `valid_stability.json` 对 **A2 / A3 / A5 / C2** 四个 arm 给出的 valid 整段净超额不一致（最大差 **3.65pp**，A5：4.41% vs 8.06%），其余 10 组逐位相同（残差 ~1e-16）。两次运行相隔 9 分钟，`model.pkl` / `pred.pkl` / `manifest.json` 的 md5 **完全一致** ⇒ **不是输入不同**。源码级根因**已定位到「候选同分时的选股顺序」**：`qlib/backtest/position.py:422` 的 `get_stock_list()` 用 `set(...)` ⇒ 顺序随**字符串哈希随机化**跨进程变化；再叠加 `signal_strategy.py` 里 `sort_values(ascending=False)` **默认非稳定排序** ⇒ 分数并列时卖出 / 买入标的的挑选会变。**未做单点复现实验**（需重跑回测）⇒ 引用 valid 净超额时**必须标明来源脚本** |
| 5 | 🔴 **`n_drop=0` 的语义坑** | 见 §4.7.3 事实 ② ⇒ arm_id `A7_buyhold` **有误导性**，引用时必须带「退化」二字 |
| 6 | ⚠️ **`hold_thresh` 只挡卖不挡买** | 持仓数可能略高于 `topk`（§4.7.3 事实 ③）⇒ 换手与持仓数口径都受此影响 |

#### 4.7.8 复现命令（服务器端）

```bash
# ---- 0) 本地改完先同步（rsync + md5 双侧比对）----
cd /media/manu/1TB-Volume/workspace/qlib
bash manu/tools/sync_to_server.sh

# ---- 1) 上服务器、激活 conda env、进工程目录 ----
ssh -F /dev/null -p 32222 huangzhe@192.168.99.40
source /home/huangzhe/anaconda3/etc/profile.d/conda.sh && conda activate ql
cd /tmp/pycharm_project_658d3fc4

# ---- 2) 降换手 sweep（14 组 arm × valid/test 两段；不重训）----
python manu/experiments/ashare_topk_selection/turnover_sweep.py \
  --run-dir /mnt/data/siping/runs/ashare_topk/runs/0fdde80304d94becbf3b0a061c3f61c9
#    产物：<run 目录>/turnover_sweep.json + turnover_sweep.csv
#    日志：/mnt/data/siping/runs/ashare_topk/turnover_sweep.log

# ---- 3) valid 段分年稳定性（不引用 test）----
python manu/experiments/ashare_topk_selection/valid_stability.py \
  --run-dir /mnt/data/siping/runs/ashare_topk/runs/0fdde80304d94becbf3b0a061c3f61c9
#    产物：<run 目录>/valid_stability.json
#    日志：/mnt/data/siping/runs/ashare_topk/valid_stability.log
```

> ⚠️ 两个脚本**只读** run 目录里的 `model.pkl` / `pred.pkl` / `manifest.json`，
> **不重训、不写回任何配置**（产物 `semantics.config_mutated=false`）⇒ 重跑不会产生新的训练产物。
> ⚠️ 重跑会**覆盖**同名 `turnover_sweep.json` / `valid_stability.json`
> （二者写在既有 run 目录内，**没有**按 `rules.md §12.8` 另开产物目录）⇒ **重跑前先备份**。

---

## 5. Top5 打分清单（截面日 2026-11-03）

> 产物：`<run 目录>/top5.json`，`kind=model_score_ranking`，
> `as_of_date=2026-11-03`，`instruments=csi300`，`n_scored_instruments=299`，
> `tie_at_boundary=false`（**截断处无并列**）。

| 排名 | 标的 | 打分 |
|:--:|:--|--:|
| 1 | **SZ002463** | **+0.723460** |
| 2 | **SZ300408** | **+0.653785** |
| 3 | **SH688183** | **+0.650911** |
| 4 | **SZ002837** | **+0.574220** |
| 5 | **SZ300394** | **+0.538529** |

🔴 **引用本清单必须同时说明三件事**：

1. 这是**模型在截面日的打分排名**，**不是策略实际持仓组合**（策略是 `topk=30`），
   **不是投资建议**，**不是可直接执行的买卖清单**；
2. 截面日 **2026-11-03** = 数据集最后交易日，距当前（2026-11-04）仅 **1 天**
   ⇒ **接近当前时点**，但仍不等于「今日行情」；
3. 该日 csi300 有打分的标的 **M=299**（池内 300 只，1 只当日无信号）。

---

## 6. 偏离官方 example 配方的 7 条理由与证据

> 官方配方见 `examples/benchmarks/LightGBM/workflow_config_lightgbm_Alpha158.yaml`。
> **下面每一条都有本工程实测证据**，不是「感觉更好」。

### 6.1 🔴 必须关 early stopping（`early_stopping_rounds=None`）

- **机制**：`qlib/contrib/model/gbdt.py:78-86` 把 **[train, valid] 一起**塞进 LightGBM 的
  `valid_sets`；`lgb.early_stopping(stopping_rounds)` 默认 `first_metric_only=False`
  ⇒ 同时盯 train/valid 两个指标。
- **后果**：LightGBM 早停触发后**按最早的最优迭代裁剪模型**，`Booster.predict` 默认也只用
  `best_iteration`。本数据上 valid 指标在第 **1~2** 轮之后单调变差 ⇒ `best_iteration=1~2`
  ⇒ 只剩 **1~2 棵树**，预测退化（实测 `uniq_pred=4`，日度 **IC ≈ 0**）。
- **处置**：关早停 + 固定轮数 + 网格选参 ⇒ **IC 从 ~0 升到 0.0386**。

### 6.2 🔴 标签用 `CSRankNorm` 而非 Alpha158 默认的 `CSZScoreNorm`

- **机制**：原始标签重尾（min **-7.70** / max **+10.31**），z-score 标准化后**极端值仍主导
  MSE 梯度** ⇒ 模型被少数极端样本带跑。
- **证据**：valid IC **0.0154（zscore） → 0.0330（rank）**，**翻倍**。

### 6.3 🔴 标签用 **VWAP** 而非 close

- **证据**：valid IC 对比 **vwap_rank 0.0330 > close_rank 0.0269 > vwap_zscore 0.0154 > close_zscore 0.0089**。
- ⚠️ 注意：Alpha158 的**类默认**标签是 close，VWAP 来自 `Alpha158vwap` 或显式传 `label` 形参
  ⇒ **必须显式声明**，否则口径会随 handler 变化而漂移（见 §3.2）。
- ✅ **实证**：本次 `label.pkl` 逐值等于 VWAP 表达式（误差 0.0）、不等于 close 表达式
  ⇒ 口径确为 VWAP（详见 §7.6）。

### 6.4 🔴 `lambda_l1` / `lambda_l2` 置 0

- **机制**：官方 example 的 **205.7 / 581** 是为**原始尺度特征**调的；
  本工程特征已过 `ZScoreNorm`（近似标准分布）⇒ 强正则会把模型压成常数。
- **证据**：带强正则时 valid IC 仅 **0.0074**。

### 6.5 ✅ 选参只看 valid IC、不看 test

- 网格：**lr ∈ {0.02, 0.04, 0.08} × rounds ∈ {300, 500, 1500}**；
  最优 **lr = 0.02 / 500 轮**（即本档采用值）。
- test 段**全程未参与选参**（`rules.md §12.7`）⇒ test 数字未被污染。

### 6.6 🔴 `start_time` 必须早于 train 起点 ≥ 1 年

- 否则训练段开头是 **NaN → Fillna 填 0 的垃圾值**（Alpha158 最长回看 60 个交易日）。
- 本次 `start_time=2015-01-01`，train 起点 2016-01-01 ⇒ **留 1 年预热**。

### 6.7 ⚠️ **模型族只做了 LightGBM 一族，尚未做跨模型族对照**

- **已做**：仅 `LGBModel`（LightGBM）。
- **未做**：GRU / 线性模型 / XGBoost（服务器**未装** `torch` / `xgboost`，qlib 视为 optional
  会**静默跳过** `TorchModel` / `XGBModel`，见 `rules.md` 铁律一）。
- ⇒ **「LightGBM 是本场景最优模型族」这件事尚未验证**，不得当结论引用；
  跨族对照需要用户另行授权安装依赖 + 跑训练。

---

## 7. 遗留风险与下一步

### 7.1 🔴 → ✅ 换手塌陷**已解决**（降换手 sweep，§4.7）：含成本超额 +0.35% → **+1.15% ~ +11.83%**

> **本节上一版写的是「换手分解尚在补 / 下一步待授权」，现已补齐（2026-11-04）**：
> 换手分解见 **§4.5**，降换手受控 A/B 见 **§4.7**。以下结论口径以 §4.7 为准。

1. 🔴 **降换手有效**：把年化单边换手从 **41.1 倍**降到 **5~20 倍**，
   test 段含成本年化超额从 **+0.35%** 提到 **+1.15% ~ +11.83%**；
   **且不只是省了成本** —— `A3` 的**毛**超额也从 +6.40% 升到 **+7.74%**
   ⇒ **「持有高分股更久」本身也提高了 alpha 捕获率**。
2. ⚠️ **valid 整段取 argmax 不可靠**：valid 排名第一的 `A3` 在 test 只有 **+6.72%**，
   valid 排第 5 的 `B3` 在 test 达 **+11.83%** ⇒ 14 组里挑 1 组存在**选择性偏差**（Spearman 仅 **0.393**）。
3. ✅ **用分年最差值选更稳**：`min_year_net` 最高的三组 `C3`(+7.60%)、`A3`(+7.23%)、`B3`(+6.33%)
   在 test 上分别拿到 **+7.15% / +6.72% / +11.83%** ⇒ **「valid 上跨子区间稳健」是有效的选参判据**。
4. ✅ **推荐口径（三条，按口味选）**：
   - **稳健优先 → `C3_top50_rebal10`**（topk=50、每 10 个交易日全量刷新）：valid 两段几乎完全一致
     （**+7.60% / +7.78%**，极差仅 **0.18pp**），test 净 **+7.15%**、IR **0.837**、MDD **-11.47%**、换手 **17.6 倍**。
   - **收益优先 → `B3_rebal20`**（topk=30、每 20 个交易日全量刷新）：valid 净 +8.60%（min +6.33%），
     test 净 **+11.83%**、IR **1.138**、MDD **-10.12%**、换手 **10.9 倍**。
   - **最低换手 → `A3_ndrop1_hold5`**（topk=30、n_drop=1、hold_thresh=5）：valid 净第一 **+12.03%**（min +7.23%），
     test 净 +6.72%、IR 0.635、MDD -17.99%、换手仅 **8.4 倍**（**基线的 1/5**）。
5. ⚠️ **仍不能说「策略已可用」**：valid→test 的秩相关只有 **0.47**，
   **单条曲线不足以证明长期有效**；`impact_cost=0.0` ⇒ **真实市场冲击未建模**，
   上述成本只含显式佣金 + 印花税，是真实成本的**下界**；
   本轮**仍未做跨模型族对照**（GRU / 线性 / XGBoost，见 §6.7 / §7.2）。
6. ⚠️ **`A3` 的 valid 优势含 2022 单年尖峰**（+18.29% vs 2023 +7.23%）
   ⇒ 引用其 valid 数字时必须**同时给分年拆解**。

- ⇒ **结论口径更新**：问题**不在选股模型**（IC 0.038 / t=6.66 稳定成立），**在交易频率与费率**；
  降换手把「含成本后不可用」改成了「含成本后可用区间内可选」，
  但**冲击成本未建模 + 跨族未对照 + valid→test 相关性弱**三条仍在 ⇒ **不得宣称策略已可用**。
- 下一步方向（**均需用户当轮授权**才能跑）：
  1. ⚠️ 补 `impact_cost` / 真实费率口径重估（现值是真实成本的**下界**）；
  2. ⚠️ 跨模型族对照（GRU / 线性 / XGBoost，需先装 `torch` / `xgboost`）；
  3. ⚠️ 提高信号稳定性：换更慢的特征（如月频动量）、加大 label 窗口；
  4. ⚠️ 补「换手上限」这一维（本轮只扫了 `n_drop` / `hold_thresh` / `rebalance_every` / `topk`）。

### 7.2 ⚠️ 尚未做跨模型族对照

见 §6.7 —— 这是**当前最大的未验证面**，不要粉饰。

### 7.3 ⚠️ PnL 按 close 价结算，而标签用 VWAP

- 回测 `deal_price=close`，标签 `Ref($vwap,-2)/Ref($vwap,-1)-1`
  ⇒ **区间一致，但价格口径略有差异**（收益与成交价不同源）。
- 影响未量化，需在换手对账时一并看。

### 7.4 ⚠️ `manu/` 与 `CMDS.md` 仍是 untracked（上游 qlib 源码零改动）

- `git status` → `?? manu/`、`?? CMDS.md` ⇒ **没有任何 commit 包含 `manu/`**。
- 按 `rules.md §12.8`，manifest 的 commit 字段已**显式写明**「`manu/` 未纳入版本控制」
  （commit 值取自本地 checkout `54355232463878d2eebb91fe0ee5fa7fa1f5976c`，`dirty=true`，
  **不含自研代码**），并额外记录了 4 个脚本的 **md5** 以保证可追溯。
- **未擅自 commit**（用户未授权）。

### 7.5 ⚠️ `pred.pkl` 只含 test 段信号

- `LGBModel.predict` 默认 `segment="test"`（`qlib/contrib/model/gbdt.py:93`）
  ⇒ `pred.pkl` 里**没有** train/valid 的预测 ⇒ 事后诊断、跨段对比需重跑 `predict`。

### 7.6 ✅ 归档自检发现 manifest 记错标签口径 —— **已定位、已实证、代码已修**

- `run_pipeline.py` 原先写 manifest 时取 `dataset.handler.get_label_config()`，
  该方法恒返回 **Alpha158 的类默认值（close）**，**不反映** handler 形参覆盖后的实际标签
  ⇒ 本次 manifest 的 `config.label.expressions` 记成了 close，而**实际生效的是 VWAP**（§3.2）。
- ✅ **实证判定（不靠推断）**：从原始行情用 `D.features` 取 2025-06-13 三个标的，
  与 `label.pkl` 逐值比对 —— VWAP 表达式误差 **0.0**，close 表达式差 1e-3~6e-3
  ⇒ **实际生效标签 = VWAP，模型无误，错的只是 manifest 的记录**。
- ✅ **代码已修**（2026-11-04）：改取 `dataset.handler.data_loader.fields["label"]`
  （loader 解析后的**真实**配置），并在注释里写明 `get_label_config()` 是类默认、不可用。
  服务器实测：`fields["label"] = (['Ref($vwap,-2)/Ref($vwap,-1)-1'], ['LABEL0'])` ✓，
  而 `get_label_config()` 仍是 close ✓（复现了原 bug）。
  ⇒ **下次运行起 manifest 记录正确**；本次已产出的 manifest 仍记 close，
  **引用时以本档 §3.2 为准**。
- ✅ 连带订正：`manu/memory/qlib_api_findings_20261104.md` §B1 已按源码改写
  （`Alpha158` = close、`Alpha158vwap` = vwap；本工程靠显式传 `label` 覆盖）。

### 7.7 ✅ 耗时口径（已定案）

- manifest `elapsed_sec` = **116.3 s**（端到端：init → dataset → 训练 → 信号 → 回测 → 落盘）
  ⇒ **以 manifest 为准**。此前口述的「100.6 s」**无对应产物记录，予以作废**。
- 参考：换手分解脚本重跑同口径回测（685 个交易日）耗时 **33.0 s**（不含特征构建与训练）。

---

## 8. 复现命令（服务器端）

> 全部命令**在服务器 `192.168.99.40:32222` 上执行**；
> 数据集路径 `/mnt/data/siping/datasets/qlib/cn_data`，产物落 `/mnt/data/siping/runs/ashare_topk/`。
> `manu/` 脚本先在**本地**改完，再 `bash manu/tools/sync_to_server.sh` 同步（rsync + md5 双侧比对）。

```bash
# ---- 1) 连服务器（sshd 在 32222；本机必须 -F /dev/null）----
ssh -F /dev/null -p 32222 huangzhe@192.168.99.40

# ---- 2) 切解释器 + 进工程目录 ----
source /home/huangzhe/anaconda3/etc/profile.d/conda.sh && conda activate ql
cd /tmp/pycharm_project_658d3fc4
python -c "import qlib; print(qlib.__version__, qlib.__file__)"   # 期望 0.9.8 + 指向工程目录

# ---- 3) 数据集体检（只读探查，秒级）----
python manu/experiments/ashare_topk_selection/inspect_dataset.py

# ---- 4) 主流水线：训练 + 信号 + 回测 + manifest（约 2 分钟）----
python manu/experiments/ashare_topk_selection/run_pipeline.py
#    产物：/mnt/data/siping/runs/ashare_topk/runs/<recorder_id>/{label,model,pred}.pkl + manifest.json

# ---- 5) 导出 Top5 打分清单（JSON + 屏幕明细）----
python manu/experiments/ashare_topk_selection/export_topk.py \
  --run-dir /mnt/data/siping/runs/ashare_topk/runs/0fdde80304d94becbf3b0a061c3f61c9 \
  --top 5

# ---- 6) 复核产物 md5（应与本档 §4.1 一致）----
md5sum /mnt/data/siping/runs/ashare_topk/runs/0fdde80304d94becbf3b0a061c3f61c9/*.pkl

# ---- 7) 降换手 sweep（14 组 arm × valid/test 两段；不重训，约 15 分钟）----
python manu/experiments/ashare_topk_selection/turnover_sweep.py \
  --run-dir /mnt/data/siping/runs/ashare_topk/runs/0fdde80304d94becbf3b0a061c3f61c9
#    产物：<run 目录>/turnover_sweep.json + turnover_sweep.csv；日志 ../turnover_sweep.log

# ---- 8) valid 段分年稳定性（不引用 test）----
python manu/experiments/ashare_topk_selection/valid_stability.py \
  --run-dir /mnt/data/siping/runs/ashare_topk/runs/0fdde80304d94becbf3b0a061c3f61c9
#    产物：<run 目录>/valid_stability.json；日志 ../valid_stability.log
```

### 8.1 复现前置条件

| 条件 | 值 |
|:--|:--|
| 数据集 | `/mnt/data/siping/datasets/qlib/cn_data` 必须已存在（由 `manu/tools/fetch_qlib_dataset.py` 取回） |
| 依赖 | `pyqlib 0.9.8`（editable）+ numpy 2.5.4 + pandas 2.3.3 + lightgbm 4.7.0 |
| 脚本版本 | 本次运行所用 4 个脚本的 md5 记在 manifest `script_md5`（`config.py` / `export_topk.py` / `inspect_dataset.py` / `run_pipeline.py`） |
| 种子 | `random_state=42` + `deterministic=True`（**qlib 层无 seed**，见 `rules.md §12.3`） |
| 降换手脚本版本 | §4.7 三个脚本的 md5：`turnover_sweep.py` `874585844f0cad3a63098a891f1228fc` / `valid_stability.py` `0a8b6b9800f6cb960af888875ccf8286` / `rebalance_interval_strategy.py` `46f4d2af472e75bc5d4aae06370d446d`（**本地与服务器实测一致**） |

> ⚠️ **重跑即新目录**：产物按 `runs/<recorder_id>/` 隔离，**不覆盖**历史运行（`rules.md §12.8`）。
> `export_topk.py` 默认取「最新的含 manifest.json 的子目录」，**复现历史结果请显式 `--run-dir`**。
> ⚠️ **例外**：§4.7 的两个 sweep 脚本把 JSON/CSV 写进**既有 run 目录**，**会覆盖同名文件**
> ⇒ 重跑前先备份。

