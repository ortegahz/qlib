# 快照架构说明（单一无状态流水线 + 不可变快照目录）

> **线别**：ETF 轮动**每日链** `manu/etf_rotation/daily/`
> **日期**：2026-11-09 架构瘦身（本轮改造）
> **取代**：`manu/docs/mode_c_shadow_arena.md`（旧的「影子推演」设计文档，**已删除**）
> **契约**：`/tmp/qlib_snapshot_contract.md`（统一流水线 + 快照制）
> 🔴 **本文件是「设计口径」，不是「实跑结论」** —— 本轮改造的代码（`daily/main.py` /
> `daily/agents_rebalance.py` / `daily/snapshot.py` / `daily/daily_config.py`）**尚未实跑验证**，
> 凡涉及行为的表述一律以代码与探针为准；探针（`probes/probe_snapshot_contract.py`）跑通之前，
> 都只是「应当如此」。

---

## 1. 为什么：旧形态「根目录全局共享文件」的三宗罪

旧形态里，每日链把产物写进**根目录下的全局共享文件**（canonical `stock_scores.csv` /
`tradingagents_decision.md|.json` / `rebalance_recommendation.json`）。
这个选择看起来只是「图省事把结果放在固定路径」，实际埋了三颗雷：

### 1.1 第一宗罪：跨链路污染

同一个根目录被**多条链共享**：研究链（`run_pipeline.sh` → 生产基线）、每日链（决策链）、
以及已废除的旧影子推演链。
三者的产物**文件名相同或相近**、**写入路径相同** ⇒ 一条链跑错就可能覆盖另一条链的金标。

最贵的一次教训是影子推演：它要跑完整六阶段、产出完整执行卡，而它的产物根与生产根
在**设计上**是不同的目录 —— 但「不同」这件事**只由 `if mode == "C"` 的分支守**。
只要哪一处新增写文件时忘了带这个分支判断，影子跑一次就把生产金标覆盖掉了，
而且**覆盖之后没有告警**（因为两边文件名一模一样）。

### 1.2 第二宗罪：无法 diff 两份跑

因为产物落在**固定文件名**上，第二轮跑会把第一轮**同名覆盖**。
于是「今天上午 11:30 跑的那次和下午 15:05 跑的那次差在哪」这个问题**在旧形态下无解**：
盘中到盘后的信号演化、两次跑的 Alpha 排序变化、两次跑之间数据源被修正了多少 —— 全部丢失。

而这恰恰是投研里最常问的问题。旧形态下唯一能回答它的办法，是**人工把文件改名备份**，
再靠人肉 diff —— 不可复现、不可自动化、也谈不上「随时回看」。

### 1.3 第三宗罪：「不写全局文件」靠 if 分支守 ⇒ 漏一处就污染

这是第一宗罪的**根因**。旧形态下，「每日链不写全局文件」这个不变式**没有物理载体**，
它只存在于若干个 `if` 分支的判断里：

- 新增一个写文件的环节 ⇒ 作者必须**记得**手动加分支判断；
- 漏加 ⇒ 污染，且**大概率在深夜跑批时发生**；
- 分支判断散落在多个函数里 ⇒ 「这个函数会不会写全局文件」无法一眼看出，必须通读代码。

换句话说：旧形态把不变式写在**人的注意力**里，而不是写在**数据结构**里。
只要注意力不在线，不变式就会破。

### 1.4 新形态怎么一次性解决三条

**每次运行 = 一个独立目录**。目录名带时间戳、内容自包含、跑完即冻结：

- 目录隔离 ⇒ **跨链路污染**在物理上不可能（不同链写不同目录）；
- 目录只增不改 ⇒ **任意两个快照可以直接 diff**（`diff -r snap_A snap_B`）；
- 「写哪儿」由**一个参数**（快照对象）决定，而不是由一串 `if` 分支决定 ⇒ **漏一处就污染**从根上消失。

---

## 2. 新形态：一张流程图

```text
                        ┌──────────────────────────────────────────────┐
                        │ 入口（唯一）                                  │
                        │ python manu/etf_rotation/daily/main.py         │
                        │        --asof <YYYY-MM-DD>                   │
                        │        [--full] [--live-trade] [--dry-run]    │
                        │        [--snapshot-root PATH]                │
                        └───────────────────┬──────────────────────────┘
                                            │
                                            ▼
              ┌─────────────────────────────────────────────────────────┐
              │ 阶段 1  盘后数据管道（AkShare 增量；W 只读；qlib 数据集可选刷新）│
              └───────────────────┬─────────────────────────────────────┘
                                  ▼
              ┌─────────────────────────────────────────────────────────┐
              │ 阶段 2  S 层 Alpha 推断（qlib LightGBM + Alpha158）        │
              │          ⇒ 本轮 stock_scores.csv（合并后的全历史）         │
              └───────────────────┬─────────────────────────────────────┘
                                  ▼
              ┌─────────────────────────────────────────────────────────┐
              │ 阶段 3  ETF 穿透聚合 E = W × S ＋ 均线过滤（MA20/MA60）      │
              │          ⇒ etf_analysis_report.json|.md                 │
              └───────────────────┬─────────────────────────────────────┘
                                  ▼
              ┌─────────────────────────────────────────────────────────┐
              │ 阶段 4  TradingAgents 交叉辩论与情景裁决                    │
              │          ⇒ tradingagents_decision.md|.json               │
              └───────────────────┬─────────────────────────────────────┘
                                  ▼
              ┌─────────────────────────────────────────────────────────┐
              │ 阶段 5  规则层约束对账（等权/整手/货基过滤/滑点缓冲）          │
              │          ⇒ final_rebalance_plan.json|.md                 │
              │          ⇒ node_reprice_<YYYYMMDD>.json                  │
              └───────────────────┬─────────────────────────────────────┘
                                  ▼
              ┌─────────────────────────────────────────────────────────┐
              │ 阶段 6  执行卡组装 ＋ 报告渲染                             │
              │          ⇒ execution_card_<YYYYMMDD>.json                │
              │          ⇒ report_<YYYYMMDD>.md                         │
              └───────────────────┬─────────────────────────────────────┘
                                  ▼
        ┌───────────────────────────────────────────────────────────────────┐
        │ 落盘：<SNAPSHOT_ROOT>/<YYYYMMDD_HHMMSS>/                         │
        │        （自包含 ＋ 跑完即冻结 ＋ snapshot_manifest.json 唯一索引） │
        └───────────────────────────────────────────────────────────────────┘
```

**要点**：整条链**无状态**。引擎不记住上一次跑的任何东西 ——
输入是**当场拉到**的数据，输出是**当场落的**一个目录。
两次跑 ⇒ 两个目录 ⇒ 可以 diff；一份快照 ⇒ 任何时候回看结果都不用重跑。

---

## 3. 快照目录内容（契约件名清单）

每次运行落一个目录 `<SNAPSHOT_ROOT>/<YYYYMMDD_HHMMSS>/`，内容为：

```
<SNAPSHOT_ROOT>/<YYYYMMDD_HHMMSS>/
  stock_scores.csv                # 本轮 S 层分数（合并后的全历史）
  stock_prices.csv                # 个股日线导出（阶段 2b）
  etf_analysis_report.json|.md    # 阶段 3
  rebalance_recommendation.json|.csv  # 规则层（**本轮现跑**，不从生产根复制）
  ta_research_context.md + ta_context_meta.json
  tradingagents_decision.md|.json # 阶段 4
  final_rebalance_plan.json|.md   # 阶段 5
  node_reprice_<YYYYMMDD>.json
  execution_card_<YYYYMMDD>.json # 阶段 6
  report_<YYYYMMDD>.md            # 交付报告副本
  heartbeat_<YYYYMMDD>.json
  snapshot_manifest.json          # 唯一权威索引
```

- `<SNAPSHOT_ROOT>` 缺省 = `daily_config.SNAPSHOT_ROOT`
  （env `ETF_ROTATION_SNAPSHOT_ROOT`，缺省值 `RESEARCH_RUN_ROOT/"snapshots"`），
  可用 CLI `--snapshot-root PATH` 覆盖；
- 目录名格式 `YYYYMMDD_HHMMSS`（`snapshot.RUN_ID_FORMAT`）⇒ **字典序 == 时间序**；
- 🔴 **同一秒内连跑两次 ⇒ 后者目录名加后缀**（`_02`），**绝不覆盖**已有快照；
- 🔴 `snapshot_manifest.json` 是快照的**唯一权威索引**：里面记 `stages`（每阶段状态）、
  `skipped`（被跳过的环节 ＋ 原因）、`notes`、`live_trade`（本轮实盘开关）、
  `artifacts`（每个件的字节数 ＋ sha1）。
- 🔴 **口径**：上面那份清单 = **契约 16 件 ＋ `snapshot_manifest.json`**；
  快照目录里**还会多出几个过程件**（`etf_prices_effective_<日期>.csv` / `account_snapshot.csv` /
  `account_context.json` / `stock_scores.csv.bak-<时间戳>`）⇒ **不是「只有 16 件」**。
  过程件**不在**契约校验范围内（不因它们变红），但**确实落在快照目录里**（`diff -r` 两份快照时会看到）。

---

## 4. 三条硬不变式

> 契约里共有 I1~I5 五条；下面先展开**三条核心**（I1/I2/I3），
> I4（不静默降级）与 I5（数字纪律）作为配套不变式紧随其后。

### I1 每次运行 = 一个快照目录，跑完即冻结

- 每次运行落 `<SNAPSHOT_ROOT>/<YYYYMMDD_HHMMSS>/`，**跑完即冻结**：
  `finalize()` 之后目录内的文件不再被写；
- **绝不覆盖**：同一秒内连跑两次 ⇒ 后者目录名加后缀；
- ⇒ **旧快照逐字节不变**（可作为回归基线）；
- ⇒ **任意两份快照可以直接 diff**（盘中 vs 盘后、观察态 vs 实盘态、今天 vs 上周）。

### I1-b 快照不可变性的边界：什么**不许**落在快照里（2026-11-10 已修）

🔴 **「跑完即冻结」是硬不变式**，但它**只管快照目录内部**——
**快照目录以外的落点**（canonical 根、闸门日志、指纹表…）**不受它约束**，因为那些东西**根本不是某一次运行的产物**。

🔴 **两类东西必须留在 canonical 根，不许写进快照**（2026-11-10 已修，之前都破了这条）：

| 东西 | 它是什么 | 之前为什么会破 | 现在的落点 |
|:--|:--|:--|:--|
| **指纹表 `_ms5_md5.txt`** | **代码状态登记表**（md5 段登记仓库脚本 ＋ sha1 段登记 canonical 交付件基线） | 闸门把断言对象从 canonical 迁到「最新完整快照」后，P2 仍去 `<run-root>/_ms5_md5.txt` 找 ⇒ 在快照里必然找不到 ⇒ **P2 误红**（不是漂移，是「找不到文件」） | 恒在 **canonical 根**；闸门 P2 **回落解析**到 canonical（或 `--fingerprint-table` 显式）；**重生成器不许以快照为 `--artifact-root`**（会把 canonical 登记表静默改写成「这一份快照」的哈希 ⇒ 口径错位），且**备份落表旁边**，不许写进快照 |
| **自检日志 `verify_artifacts*.log`** | 闸门第 1 阶段 `verify_artifacts` 的**只读探针日志** | 脚本末尾 `write_text` 把日志写进**快照目录** ⇒ 改了已冻结快照的文件集 ⇒ 直接破了 I1 | 落点优先级 ① `--log-path` 显式 > ② run-root **不是**快照 ⇒ 旧口径 `<run-root>/verify_artifacts.log` > ③ run-root **是**快照 ⇒ **canonical 根**的 `verify_artifacts_<快照名>.log` |

⇒ 🔴 **铁律**：**任何**往 `<canonical>/snapshots/` 下面写东西的动作都是**违规**
（登记表 / 日志 / 备份 / 缓存，一概不许）⇒ 「非交付件」一律落 **canonical 根**。
⇒ 🔴 **配套**：失败轮**也必须封存 manifest**（`main.py::_finalize_failed_snapshot()`）⇒
失败轮同样是**可审计的快照**（不许「跑崩了 ⇒ 什么都没留下」）。

### I2 每日链不写任何根目录全局共享文件

- canonical `stock_scores.csv` / `tradingagents_decision.*` / `rebalance_recommendation.json`
  每日链**只读或不碰**；
- canonical **只允许**作为「**还没有任何快照时的一次性只读种子**」——
  即：从 canonical 复制进快照，读它，但**永远不写回**；
- ⇒ 「每日链会不会污染生产金标」这个问题，从**分支判断**变成了**目录归属**：
  只要写文件走的是快照对象，就不存在污染的可能。

### I3 副作用只由 `--live-trade` 决定

- **缺省 = 观察态**：执行卡**挂单禁用**、**不发信**；
- **`--live-trade` = 实盘态**：执行卡**可下单**、**可发信**；
- ⇒ 副作用面**只由一个开关**决定，不再由「跑的是哪条模式」决定。
  这是把旧「多链三分诊」收敛掉之后最重要的一条：**副作用是二维问题**，
  真正需要区分的只有「要不要动真格」，不是「跑几段」。

### 配套 I4：绝不静默降级

任何被跳过的环节都必须

1. 在 `snapshot_manifest.json` 的 `skipped` 里**显式登记**（名字 ＋ 原因），**并且**
2. 在交付报告里**披露**。

⇒ 「这次为什么少跑了一段」永远有答案，且答案在两处（manifest ＋ 报告）都能查到。

🔴 **配套 I4-b（2026-11-10 补）：「降级事实」也必须进交付面，不许只进日志**（`--full` 的硬保证见 §6.1）：

| 降级事实 | manifest 阶段条目 | 报告 notes |
|:--|:--|:--|
| **W（成分股权重）陈旧** | `w_holdings` = `stale`（detail：`max_fetch_date` / `stale_days` / `mixed` / `n_codes`） | 🔴 W 快照日与滞后天数 |
| **「自建增量日」兜底失败** | `stage1_increment` = `failed`（detail：`days` / `live_dir` / `error`） | 🔴 上游覆盖不到信号日 ⇒ S 层只能打到旧数据 |
| **上游 qlib 快照没推进（换库零收益跳过）** | `stage1_dataset` = `ok`（detail：`calendar_max_before` / `calendar_max_after` / `new_days` / `swapped` / `downloaded`） | 不进 notes，但 manifest 里一眼看出「库没换」 |
| **qlib 数据集刷新整段失败** | `stage1_dataset` = `failed`（带 `error`） | 🔴 降级用旧数据集继续 |

⇒ 🔴 **只进日志 = 用户看不到 = 静默降级**：W **永不重爬**（铁律：重爬成分股会触发 IP 封禁）
⇒ 「实时拉了数据」覆盖的是**行情 ＋ qlib 数据集**，**不覆盖 W** ⇒ W 的陈旧度**必须**进交付面。

### 配套 I5：数字纪律

缺失值渲染成 **「—」**，**不反推、不编造**。
复用旧 S 层时必须披露实际打分日；节点日重算失败时**不许拿信号日旧价冒充节点日价**。

---

## 5. 副作用矩阵

| 维度 | 缺省（**观察态**） | `--live-trade`（**实盘态**） |
|:--|:--|:--|
| **执行卡是否可下单** | ❌ **不可**：卡片带**挂单禁用标记**（订单级也禁） | ✅ **可下单**：不带禁用标记 |
| **是否发信** | ❌ **不发**（`--send-email` 在观察态下不产生实盘通知语义；发信是实盘态的动作） | ✅ **可发信**（发信通道由 `--send-email` 显式开启，实盘态下才真正生效） |
| **节点日重算失败是否整轮崩** | ❌ **不崩**：显式降级继续 —— 节点日股数/金额**留空**（渲染成「—」），执行卡退化为「只有方向、无数值」的观察卡，**不可下单**；失败原因写进 manifest `notes` ＋ 报告披露 | ✅ **崩**：**fail-closed** —— 拿不到节点日清单就不许出实盘执行卡 ⇒ 整轮失败（退出码 1） |
| **落盘位置** | 快照目录（同一份） | 快照目录（同一份） |
| **共享 qlib 数据集**（`<落点根>/datasets/qlib`） | ⚠️ **也会换**（旧库自动进 `_backup`） | ⚠️ 同样会换 |
| **增量台账**（`_increment_ledger.jsonl`） | ⚠️ **会追加**（记录本轮补齐了哪些交易日） | ⚠️ 同样会追加 |
| **交付副本**（`manu/report/report_<日期>.md`） | ⚠️ **会写**（**同名覆盖**；真值在快照目录里） | ⚠️ 同样会写 |

🔴 **为什么后三项不由 `--live-trade` 门控**（口径澄清，2026-11-09 独立审计发现后补写）：
`--live-trade` 门控的是**资金侧副作用**——**挂单** ＋ **发信**。
而后三项是**数据准备与留痕**（把共享数据集换到最新、补齐交易日增量、把报告副本放进交付目录），
它们**不碰任何资金**；把它们也门控掉 ⇒ 观察态就拿不到与实盘态**同代**的数据，
推演结论会与实盘口径脱节 ⇒ 那才是更大的风险。
⇒ 但**必须写实**：观察态**不是**「什么都不写」，上表三行都是真实副作用。

**为什么节点日重算在两种状态下处置不同**（设计口径，迁移自旧 `rules.md §15.3.5`）：
观察态**本来就不下单** ⇒ 让它崩掉**没有任何收益**，只会让这一轮推演颗粒无收 ⇒ 降级继续但**明确披露**；
实盘态要出的是**能直接照着挂单的清单** ⇒ 数值缺失就是不可用 ⇒ 必须 fail-closed。
🔴 两种状态**都不许静默降级**：观察态的降级必须落到 `notes` ＋ 报告，实盘态的失败必须落失败报告 ＋ 非零退出码。

---

## 6. `--full` 语义：强制所有环节开

### 6.0 统一口径（2026-11-10 定稿）

> 🔴 **本口径是** `manu/etf_rotation/daily/README.md` 与本文**共用**的唯一口径（旧的「四段全开」/「五者全开」两说已作废）。

`--full` = **强制走完全部环节**：**5 个语义开关全开** ——
**取数 `fetch`** / **S 层刷新 `refresh_scores`** / **qlib 换库 `update_qlib`** /
**增量补齐 `increment`** / **LLM 辩论 `llm`**；
同时**强制 `increment_dry_run = False`**（不许只做计划）。
⇒ 换句话说：**`--full` 覆盖 6 个 CLI 开关**：
`--skip-fetch`（取数）/ `--no-refresh-scores`（S 层）/ `--no-update-qlib-bin`（换库）/
`--no-increment` ＋ `--increment-dry-run`（增量）/ `--skip-llm`（LLM）。

🔴 2026-11-09 补：增量开关（`--no-increment` / `--increment-dry-run`）原先**不在** `--full` 管辖内
⇒ 「走全部流程」可以被这两个开关**悄悄**切掉 ⇒ 现在同样纳入 `--full` 的覆盖集合，
被覆盖时登记进 `skipped` **并在报告里披露**（见下方表格与 `daily/main.py::resolve_stage_flags`）。

| 情况 | 结果 |
|:--|:--|
| 只给 `--full` | 5 个语义开关**全部 True**，`increment_dry_run = False` |
| `--full` 与某个 skip 开关**冲突**（如同时给 `--full --skip-llm`） | 🔴 **以 `--full` 为准** ⇒ 该 skip **不生效**，环节照跑 |
| 被覆盖的 skip | 🔴 **必须**登记进 `snapshot_manifest.json` 的 `skipped`（name 取 `skip_fetch` / `skip_llm` / `no_refresh_scores` / `no_update_qlib_bin` / `no_increment` / `increment_dry_run`，reason 写「`--full` 强制全流程 ⇒ 覆盖该开关」）**并且在报告里披露**（报告「快照与运行口径」表的「本轮被覆盖的开关」行） |

🔴 **不许静默降级**：被 `--full` 覆盖的 skip 必须**显式披露**。
理由很实在 —— 用户同时给了 `--full --skip-llm` 时，他心里的意思多半是「以 full 为准」；
但如果系统**默默**照跑了 LLM（烧 API 配额、耗时 +15 分钟）或者**默默**跳过了（用户以为跑了），
两种都会让用户对这一轮的认知失真。所以：照跑，但**记账**。

**对应实现**：`daily/main.py::resolve_stage_flags(args)`（纯函数，可单测）返回：

```python
{
  "fetch": bool,              # 取数        ← --skip-fetch
  "refresh_scores": bool,     # S 层刷新    ← --no-refresh-scores
  "update_qlib": bool,        # qlib 换库  ← --no-update-qlib-bin
  "llm": bool,                # LLM 辩论    ← --skip-llm
  "increment": bool,          # 增量补齐    ← --no-increment
  "increment_dry_run": bool,  # 增量只做计划 ← --increment-dry-run（--full 下强制 False）
  "full": bool,
  "skipped": [{"name": str, "reason": str}, ...],
}
```

默认（未给 `--full`）时：`fetch = not skip_fetch`；`refresh_scores = args.refresh_scores is not False`；
`update_qlib = args.update_qlib_bin is not False`；`llm = not skip_llm`；
`increment = not no_increment`；`increment_dry_run = args.increment_dry_run`。

### 6.1 `--full` 的硬保证：「有环节没跑成」不许静默出卡（2026-11-10 补）

🔴 **旧口径的洞**：manifest 里明明写着某阶段 `failed`，进程却**照样退出 0**、照样出一份执行卡
⇒ 「有环节没跑成也照样交付」＝ **静默降级**（违反 I4）。

**现在的判据**（`daily/main.py::_record_pipeline_stages()` **返回失败阶段清单**，内部统一用
`_stage(name, status, **detail)` 登记 ⇒ `status == "failed"` 进清单）：

| 环节 | 判定 |
|:--|:--|
| 各阶段 | 按**件是否存在**（或阶段自报状态）登记 `ok` / `skipped` / `failed` / `reused` / `absent` |
| `tradingagents` | `--full` **强制 LLM 真跑** ⇒ 状态**只可能是 `ok`**；其它状态（`skipped` / `absent` / `reused` / `unknown`）**一律算失败** |
| `node_reprice` | 🟢 **设计内降级**，**不计入硬失败集**：观察态退化成「只有方向、无数值」的观察卡 ＋ 报告披露；实盘态直接崩（fail-closed） |

**有失败时的五处披露**：

| 落点 | 表现 |
|:--|:--|
| **退出码** | `--full` 下 **1**（fail-closed）｜🔴 **非 `--full` 仍返回 0**（日常 cron 语义不变），但状态一律照记 degraded |
| **发信** | `--full` 下**不发信**（不许拿「有环节缺件」的交付件触发实盘通知/报单） |
| **manifest** | `pipeline` 阶段 = `"degraded"`，带 `failed_stages` 清单 |
| **heartbeat** | `status = "degraded"` |
| **报告** | notes 追加 🔴「本轮有环节没跑成：…（详见 `snapshot_manifest.json` 的 `stages`）」 |

⇒ 🔴 **非 `--full` 的降级同样要披露**（退出码 0 ≠ 无降级）：manifest `pipeline=degraded` ＋
heartbeat `degraded` ＋ 报告 notes 三处**照样写**。

🔴 **口径与每日链 README §3.1 一致**。

---

## 7. S 层底座的链式口径

S 层是全链的**唯一重活**（约 27 分钟量级）。它需要一份「历史分数底座」来把新分数**合并**进去。
新形态下底座的取用顺序是**链式**的：

```text
有上一份快照？
  ├─ 有 ⇒ 用那份快照里的 stock_scores.csv（只读；复制进本轮快照）
  │        ⇒ 每日链自成一链（快照链），永远不碰 canonical
  └─ 没有 ⇒ 用 canonical stock_scores.csv 作**一次性只读种子**
              （复制进本轮快照；**不写回** canonical）
                └─ 两者都没有 ⇒ 无底座 ⇒ S 层全量重算
```

实现：`daily/snapshot.py::resolve_scores_base(snapshot_root, canonical_csv, *, exclude=None)`
⇒ 返回 `(底座路径, 说明)`；底座路径为 `None` 时说明里写明「无底座」。

🔴 **三条硬约束**：

1. **优先上一份快照** —— 每日链的分数链是**快照链**，不是「canonical 链」；
2. canonical **只在还没有任何快照时**当一次性只读种子用一次（冷启动兜底）；
3. 🔴 **canonical 永远不被每日链写** —— 不追加、不合并、不回写、不覆盖。

`exclude` 参数用于把「本轮刚建的空快照」排除在候选之外（否则会拿自己当底座）。

---

## 8. 与研究链（`run_pipeline.sh`）的职责边界

| | **研究链** `manu/etf_rotation/run_pipeline.sh` | **每日链** `manu/etf_rotation/daily/main.py` |
|:--|:--|:--|
| **角色** | **基线生产方** ＋ **23 阶段闸门的被测对象** | **决策链** |
| **干什么** | 生产 canonical 基线产物（`stock_scores.csv`、穿透报告、决策件、规则层推荐件），供研究、复现、闸门、指纹表使用 | 现场拉数 → 内存计算 → 落一份**不可变快照**，供人读、供人 diff、供执行 |
| **触发** | 研究/重建基线时（**重活**，需授权） | **按需**（用户在窗口喊「分析」） |
| **写 canonical 吗** | ✅ **写**（它就是 canonical 的生产方） | ❌ **不写**（只读或不碰） |
| **产物形态** | 根目录全局文件（**被研究链与闸门钉死**，不宜改形态） | **快照目录** `<SNAPSHOT_ROOT>/<YYYYMMDD_HHMMSS>/` |
| **闸门** | `run_gates.sh`（23 阶段）探的是**研究链产物** | `probes/probe_snapshot_contract.py`（S1~S10）探的是**快照契约** |

🔴 **边界要点**：

- **每日链不再生产 canonical** ⇒ canonical 的唯一生产方是研究链 ⇒
  「谁改了我的基线」这个问题不再有歧义；
- **每日链不从生产根复制规则层推荐件**（`rebalance_recommendation.json|.csv` 是**本轮现跑**的）
  ⇒ 报告里的规则层结论与本轮快照里的其它件**必然同代**；
- 两条链**纪律同源**（铁律〇/一/三/十四）但**产物形态不同** ——
  研究链的产物形态被闸门与指纹表钉死，**不动**；每日链换成快照制。

---

## 9. 迁移说明：老命令行 `--mode` 会被拒

旧的「模式分诊」已被**废除**。`--mode` 参数**已删除**（连参数本身也不再保留）：

```bash
# ❌ 旧写法 —— 一律被拒
python manu/etf_rotation/daily/main.py --asof 2026-12-01 --mode 旧取值
# ⇒ 退出码 2，stderr 打印迁移提示（指向 --full / --live-trade / 快照制）
```

**新命令对照表**：

| 老命令（已废除） | 新命令 | 变化说明 |
|:--|:--|:--|
| `--mode`（旧「调仓节点」取值） | `--asof <date> --full` | 「跑全流程」现在由 `--full` 表达；**副作用**由 `--live-trade` 单独表达 |
| `--mode`（旧「调仓节点」取值）＋ `--skip-llm` | `--asof <date> --skip-llm` | 不再需要 `--mode`；`--skip-llm` 直接就是「跳过辩论」 |
| `--mode`（旧「日常巡检」取值） | `--asof <date>`（缺省） | 缺省就是「按需跑一趟」；不需要再声明「我这是巡检模式」 |
| `--mode auto`（旧「按日期分流」取值） | *不需要* | 单一流水线没有分流；跑哪几段由 `--full` ＋ skip 开关决定 |

> 旧的「影子推演」能力**没有消失，只是换了表达**：**「跑一遍但不动真格」= `--dry-run` / 缺省观察态**
> （执行卡结构性挂单禁用、不发信）；**「要可下单的执行卡」= `--live-trade`**（显式 opt-in 的唯一副作用开关）。

🔴 **被拒的行为**：`--mode` 仍被传入 ⇒ **退出码 2** ＋ stderr 打印迁移提示（不静默忽略）。
理由：静默忽略一个「用户以为生效了」的参数，是最典型的**假绿**；
必须让用户**当场看见**自己的老命令已经过时。

---

## 10. 不适用 / 边界（别装全）

本架构**只管每日链**。以下事情**不在**它范围内：

| 不适用 | 原因 / 归属 |
|:--|:--|
| **研究链产物的形态** | 研究链仍写 canonical 根目录全局文件 —— 那是**闸门与指纹表钉死**的基线形态，改它属于另一个议题 |
| **回测 / 训练 / 特征重建** | 全部**不在**每日链内；每日链只出**当期决策**，不出统计结论 |
| **节点日之后的执行** | 每日链只生成**执行卡**；**真实下单是人工动作**（券商 App 集合竞价 / 开盘），本工程不接券商接口 |
| **多进程 / 并发跑** | 同一快照根下的并发写**不在**本设计内；并发场景的隔离由调用方保证 |
| **快照的跨机迁移** | 快照目录自包含，**拷贝即迁移**；但**没有**内置的签名/校验协议（manifest 里的 sha1 只做**读时自检**，不是防篡改） |
| **历史快照的自动清理** | 本设计**只增不删**；保留策略（GC）是运维决策，**不在**代码里 |
| **实盘接口** | `--live-trade` 只放开**执行卡可下单** ＋ **可发信**两件事；**没有**任何自动报单通道 |
| **`--dry-run` 的语义** | `--dry-run` ＝ **显式声明「观察态」**，与 `--live-trade` **互斥**（同时给 ⇒ 退出码 2）。🔴 **观察态本来就是缺省行为 ⇒ 本开关不改变任何行为**：跑完照样落快照、照样出报告。⚠️ **别把它当成「不产出产物」的预演** —— 内部那个「前置判定（不取数 / 不写产物 / 不调 LLM）」参数**不叫 dry-run**，叫 **`preflight_only`**（2026-11-10 改名，消除同名不同义的暗门） |

---

## 11. 相关文档

- 每日链怎么跑：`manu/etf_rotation/daily/README.md`
- 可复制命令：`CMDS.md` §「每日 ETF 轮动流水线」
- 快照层实现：`manu/etf_rotation/daily/snapshot.py`（API 冻结：`Snapshot` / `create_snapshot` /
  `latest_snapshot` / `resolve_scores_base` / `root_global_fingerprint`）
- 常量单一事实源：`manu/etf_rotation/daily/daily_config.py`（`SNAPSHOT_ROOT`）
- 快照契约闸门：`manu/etf_rotation/probes/probe_snapshot_contract.py`（S1~S10）
- 铁律：`manu/memory/rules.md` §十五「每日流水线铁律」

---

## 12. 对用户架构正本「§三 改造代价评估」两条的落地口径（2026-11-09 收口）

用户正本把改造拆成两处历史包袱。本节写清**各自落到哪、凭什么**（避免明早/下轮再猜）。

### 12.1 「破除根目录硬编码路径」⇒ 已落地

| 正本诉求 | 落地形态 | 证据 |
| --- | --- | --- |
| 输出路径从参数动态传入 | `daily/snapshot.py::create_snapshot(root, …)` ⇒ **每次运行一个带时间戳的目录**；`daily_config.SNAPSHOT_ROOT`（env `ETF_ROTATION_SNAPSHOT_ROOT`） | `probe_snapshot_contract` S1 / S2 |
| 取消全局追加逻辑 | 每日链**不写**任何根目录全局共享文件（canonical `stock_scores.csv` 只作**一次性只读种子**） | S4（canonical 三项指纹前后全等：exists/bytes/sha1/mtime） |
| 内存计算、不共享状态 | S 层**底座链式**：底座优先取**上一份快照**的分数，其次才取 canonical 只读种子；本轮分数**只落本快照** | `resolve_scores_base()` ＋ S3a/S3b/S3c 三情形 |
| 快照自包含、可 diff 两份跑 | 快照内**含全部交付件**（契约 §4 件名清单），无符号链接、无外链 | S2a/S2b/S2c |

### 12.2 「重构测试闸门」⇒ **部分落地 + 明确剩余项**（不遮丑）

正本建议：把 `probe_lookahead L20` 这类**写死旧文件期望值**的金标探针，改成断言
「**当前运行生成的快照目录**的结构有效性与无未来函数」，并删掉单文件哈希校验。

本轮**实际**做法与理由（**不是**把金标删掉来图绿）：

1. 🔴 **L20 断言的不是字节哈希，是业务值**（`final_gap` / `swap_threshold_realistic` /
   `margin_realistic` 与「|final_gap| 最小的一对」是谁）⇒ 它是**基线一致性护栏**，
   不是「死盯某个 csv 的哈希」⇒ 直接删掉 = 弱化闸门（违反铁律「闸门不许改弱」）。
2. 🔴 2026-11-09 实测定性：L20 变红的根因是 **canonical 基线（09:57 生成、平滑前口径）
   与金标（平滑后口径）不同源**，**不是**产品算错（当前代码实跑临时目录**命中**金标）。
   ⇒ 正确处置 = **先重建基线、再更新金标、最后重跑闸门**（见 `rules.md §十五.4`）。
3. ✅ 「断言对象 = 当前运行生成的快照」这条**已经大面积覆盖**，只是断言载体是新探针：
   * 结构有效性 / 自包含 / 不写全局文件 / manifest 完整披露 ⇒ `probe_snapshot_contract`
     **S1 / S2 / S3 / S4 / S5**（含 9 条变异反证，S10）；
   * 无未来函数 ⇒ `probe_lookahead`（金标 + 10 条负例自检）、`probe_data_freshness`、
     `probe_signal_feasibility`、`probe_rolling_increment`（阶段 1 自建增量日的守卫）。
4. 🔴 **明确剩余项（下一轮做，不在 2026-11-09 收口里）**：
   新增一条**直接断言「本轮快照目录」**的探针阶段（而不是断言产物根里的旧件）：
   读快照内 `etf_analysis_report.json` ⇒ 断言
   ① `meta.asof` == 快照 manifest 的 `asof`；
   ② `lookahead_audit` 的**决策前视恰 1 项**且该项 `fixable=False`（= W 成分快照，结构性不可修）；
   ③ `risk_disclosure` 含 `w_snapshot_bias` 且带实测数字；
   ④ 快照内**所有**日期型输入 ≤ `asof`（无未来函数）。
   ⇒ ✅ **2026-11-09 已落地**：`manu/etf_rotation/probes/probe_snapshot_lookahead.py`
   （正例 **A1~A9** ＋ 负例自检 **N1~N5**）已接进 `run_gates.sh` 的**第 24 个探针阶段**
   （`probe_snapshot_lookahead`）。🔴 判定口径做成 **asof 相对**（不写死「决策前视恰 1 项」）：
   asof 推过 W 快照日就是 0 项 ⇒ 换 asof 不会误红；**没有真实快照时该阶段退出码 2 = 不适用**（≠「通过」，不计失败）。
   ⇒ 2026-11-09 通宵自跑产出的那一份快照，就是它的**真实夹具**（本地已用两套合成夹具
   —— asof=2026-11-03 有 1 项前视 / asof=2026-11-05 有 0 项前视 —— 验过 **正例 9/9 ＋ 负例 5/5**）。
