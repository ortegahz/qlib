# 明早接手手册 · ETF 单一流水线通宵自跑（2026-11-09 夜）

> 本文件是**冷启动接手说明**：即使明天开的是全新会话（没有本轮对话上下文），
> 只读本文件 + 服务器上的 `summary.md` 就能完整接手、判绿、定位、修复、验收。

---

## 0. 一句话现状（截至 2026-11-09 17:40）

- 服务器上**通宵自跑已启动**（`setsid nohup`，PID 3533951，**与 host 关机无关**）。
- 进度：步骤 1/4「生产基线重建」跑到 `6/16 TradingAgents 决策`（LLM 调用中，已跑 12 分钟，上限 5400s）。
- 剩余：步骤 2（统一流水线实跑，含 LLM）→ 步骤 3（指纹表）→ 步骤 4（25 阶段全量闸门）。
- **host 18:00 关机不影响服务器**：任务在服务器自己跑完，明早只读总结即可。

---

## 1. 服务器坐标

```bash
ssh -F /dev/null -o StrictHostKeyChecking=no -p 32222 huangzhe@192.168.99.40
# 工程目录  : /tmp/pycharm_project_658d3fc4
# 产物根    : /mnt/data/siping/runs/etf_rotation
# 日志目录  : /mnt/data/siping/runs/etf_rotation/_overnight_20261109/
# ql 解释器 : /home/huangzhe/anaconda3/envs/ql/bin/python
# llm 解释器: /home/huangzhe/anaconda3/envs/llm/bin/python
```

**唯一入口（先看这个）**

```bash
cat /mnt/data/siping/runs/etf_rotation/_overnight_20261109/summary.md
```

---

## 2. 四个步骤与判定标准

| # | 步骤 | 命令（服务器） | 日志 | 绿的标准 |
|---|---|---|---|---|
| 1 | 生产基线重建（含 LLM） | `bash manu/etf_rotation/rebuild_baseline.sh --asof 2026-11-03 --out-dir /mnt/data/siping/runs/etf_rotation` | `step1_rebuild_baseline.log` | rc=0；日志出现 `16/16` 阶段全部完成；`tradingagents_decision.md` 存在且非空 |
| 2 | 统一流水线真实跑（观察态 + 全流程 + LLM） | `main.py --asof 2026-11-05 --full`（脚本内置 3 级降级） | `step2_pipeline/pipeline.log` | rc=0；`snapshots/<时间戳>/` 新目录，内含 `snapshot_manifest.json` + 14 个交付件 |
| 3 | 指纹表重生成 + dry-run 复验 | `manu/tools/regen_fingerprint_table.py --artifact-root ... [--dry-run]` | `step3_fingerprint.log` | 重生成 rc=0 **且** dry-run 复验 rc=0 |
| 4 | 全量闸门（25 阶段） | `nice -n 10 bash manu/etf_rotation/run_gates.sh --skip-repro` | `step4_gates.log` + `_gates_overnight_20261109.log` | rc=0；**注意：rc=2「不适用」≠ 通过** |

### 步骤 2 的三级降级（脚本自带，别重复手搓）

```
第 1 尝试：--full                                        （取数 + 换库 + S 层 + LLM）
  ↓ 失败 → 第 2 尝试：--skip-fetch --no-update-qlib-bin   （用现有数据；保留 S 层 + LLM）
  ↓ 失败 → 第 3 尝试：--skip-fetch --no-update-qlib-bin --no-refresh-scores
                                                          （沿用快照底座；保留 LLM）
```

### 判红的两个高发点

1. **`probe_lookahead`（L20 金标）**：金标必须与 canonical **同源**（同 asof、同样平滑口径）。
   改动前它红是因为「金标=平滑后、canonical 基线=平滑前」。步骤 1 重建基线就是为了修这个。
2. **`probe_snapshot_lookahead`**：若本轮没产出快照，它会判 rc=2「不适用」——
   **这不是通过**，必须确认步骤 2 真的产出了快照。

---

## 3. 红灯怎么办：只重跑出问题的那一段（不要从头重跑 70 分钟）

| 哪步红 | 只重跑这个 | 预计耗时 |
|---|---|---|
| 步骤 1 | `bash manu/etf_rotation/rebuild_baseline.sh --asof 2026-11-03 --out-dir <run_root>`（自带备份，幂等） | 10–90 分钟（取决于 LLM） |
| 步骤 1（**LLM 已成功、后续步骤 FAIL**） | 🔴 **加 `--reuse-decision`**：`run_pipeline.sh --reuse-decision` —— 复用本轮已落盘决策，**不重跑 25 分钟 LLM**（决策件已落盘，重跑会换一代意见；前置校验「决策比上下文新 + 报告内容指纹与登记一致」保证三环同代） | 2–8 分钟 |
| 步骤 2 | `cd /tmp/pycharm_project_658d3fc4 && /home/huangzhe/anaconda3/envs/ql/bin/python manu/etf_rotation/daily/main.py --asof 2026-11-05 --full` | 10–40 分钟 |
| 步骤 3 | `python manu/tools/regen_fingerprint_table.py --artifact-root <run_root>` 再 `--dry-run` 复验 | <1 分钟 |
| 步骤 4 | `nice -n 10 bash manu/etf_rotation/run_gates.sh --skip-repro` | 10–25 分钟 |

> 只有「步骤 4 红」且日志指向前序步骤的产物有问题时，才需要回头重跑前序步骤。

**跑之前先看总闸门日志**：`/mnt/data/siping/runs/etf_rotation/_gates_overnight_20261109.log`
（它按阶段列了每条断言的 PASS 行）。

### 时间表（每轮**只有 1 次** LLM 调用；ticker 512800 硬编码）

| 步骤 | 耗时 | 预计完成 |
|---|---|---|
| 1 基线重建 | LLM 约 25 分钟 + 10 步杂活约 5 分钟 | 17:25 起 ⇒ 约 **17:55** |
| 2 流水线实跑 | 取数 + S 层约 5 分钟 + LLM 约 25 分钟 + 余量 | 约 **18:30** |
| 3 指纹表 | < 1 分钟 | 约 **18:31** |
| 4 全量闸门（25 阶段） | 10–25 分钟 | 约 **18:55** |

⇒ **预计 18:40–19:15 全部收口**（host 18:00 关机不影响服务器）。
LLM 单次硬上限 5400s（90 分钟）；超时则该步判红并自动降级继续，不会卡死整条链。

---

## 4. 验收清单（全绿即可直接验收）

- [ ] `summary.md` 四步退出码全 0
- [ ] `snapshots/<最新>/snapshot_manifest.json` 存在，且**逐阶段状态**可见（`pipeline=ok`；
      被跳过的阶段应出现在 `skipped` 里，不许静默降级）
- [ ] 报告字段齐全：`raw_score` / `smoothed_score` / `score_stability_5d`；
      排行榜有「排序依据：5 日 EWMA 平滑分（当前 Alpha=0.35）」
- [ ] 执行卡**全部**「🔴 禁用 / 观察清单（不可下单）」；**邮件未发**
- [ ] canonical 全局文件（`stock_scores.csv` 等）**零改动**（观察态不改历史）
- [ ] `probe_snapshot_contract` 37/0；`probe_score_smoothing` 55/0；`probe_snapshot_lookahead` 正例 9/9 + 负例 5/5
- [ ] `pytest manu -q` ⇒ 125 passed / 0 failed
- [ ] 指纹表 dry-run 复验通过（登记路径集合未漂移）

---

## 5. 铁律（接手时必须继续遵守）

- **不 commit / 不 push / 不建分支**（用户明确要求）；host 仓库 HEAD 仍 `c03f2618`
- **本地优先**：只改 host `/media/manu/1TB-Volume/workspace/qlib/`；
  同步只用 `bash manu/tools/sync_to_server.sh`；判据**只能 md5 双侧比对**；**严禁直接改服务器代码**
- **不碰别人的 session**：服务器上有 Optuna 训练（4 卡）+ 4 个视频渲染 ⇒
  本任务只用 1~2 核、`nice -n 10`、**不 kill、不调优先级**；
  不碰 `/mnt/data/siping/runs/npu20ms_offcal`、`nogmc_t3`、`/mnt/data/siping/datasets/manu/...`
- **闸门不许改弱**：负例必须成对、自带夹具；「不适用」≠「通过」
- **memory 四层同轮更新**（mtime 不倒挂）；动了 `memory/` `rules` `CMDS` ⇒
  **必须重生成指纹表并 `--dry-run` 复验**
- 指纹表结构 = **固定路径集、只许改值、不许增删** ⇒ 改文件内容安全，**新增/删除登记路径会直接失败**

---

## 6. 已知遗留（本轮范围外，别误判成新 bug）

- `run_pipeline.sh` **从不传 `--scores`** ⇒ 步骤 3/4 读的是根目录那份，
  不是本轮快照里的。要修就补 `--scores "$OUT_DIR/stock_scores.csv"`。
- 5 日 EWMA 平滑已落地（`score_smoothing.py` + `config.py` 五常量 + `engine/portfolio.py`
  + `analyze.py`/`recommend.py` 同名 CLI + `run_pipeline.sh` 同组透传）。

---

## 7. 本轮编排器在哪

- host 源码：`manu/tools/overnight_snapshot_run.sh`（4 步编排 + 自动生成 `summary.md`）
- 服务器启动方式：`setsid nohup bash manu/tools/overnight_snapshot_run.sh > .../nohup.log 2>&1 &`
- 特点：**每步留日志 + 退出码**；任一步失败**不中断整条链** ⇒ 明早至少有结论。

---

## 8. 🔴 P0 未结缺陷（2026-11-09 17:50 定位，明天第一件事）

### 现象
步骤 1「生产基线重建」在 **14/16 `probe_llm_parser`** 失败 ⇒ `run_pipeline.sh` 退出码 1
⇒ 重建判红。日志：
`/mnt/data/siping/runs/etf_rotation/_ms5/_rebuild_20261109T172523.log`（第 556 行附近）

```
[FAIL] A20⑥ 决策 md 里写明「清仓/不买」的代码 ⇒ 计划里必须零目标 + 零方向（+ 部分卖出≠清仓）
   部分卖出方向违例：
   "512800: 原文写的是**部分卖出**（`512800`减持回笼约 **2），但计划方向='卖'（必须是「减」，**不是**清仓）"
   "512800: 原文写的是**部分卖出**（- `512800`最终减仓数量应按2026-12-01实际），但计划方向='卖'"
合计 76 项：PASS 75 / FAIL 1
```

### 根因（已定位到行）
`manu/etf_rotation/final_rebalance_plan.py`
- `:401` `_PARTIAL_SELL_RE = r"卖出\s*(?:约)?\s*[\d,]+(?:\.\d+)?\s*(?:股|份)"`
  ⇒ **只认「卖出约 N 股」**，**不认「减持…N 股」「减仓…N 股」** ⇒ 本轮 LLM 写的
  「`512800`减持回笼约 2,xxx 股」**匹配不上** ⇒ 没被判成「减」。
- `:1081` 兜底 `re.search(r"卖|清|不买|退出|清掉", direction)`
  ⇒ **「卖」是子串**，落在「减持」里 ⇒ 被当成**清仓 ⇒ target_pct 强制 0.0**。
- `:383` `_LLM_DIR_CELL` 词表**已含**「减持/减仓」，但 `_classify_dir_cell` 只在
  `_PARTIAL_SELL_RE` 命中时返回「减」；否则走「长词优先」⇒ 方向可能是「减持」或表格里的「卖出」。

### 建议修法（**明天做，必须带负例 + 不能弱化闸门**）
1. `_PARTIAL_SELL_RE` 扩成覆盖**减/减持/减仓 + 数量 + 股/份**：
   `r"(?:卖出|减持|减仓)\s*(?:约|大约)?\s*[\d,]+(?:\.\d+)?\s*(?:股|份)"`
   （「回笼约 2,xxx 股」这种带插入词的，建议再加 `[^，。；]{0,8}` 的容错）
2. `:1081` 的子串正则改成**不落在「减」字后**的判定：先判「减/减持/减仓」⇒ 走「减」分支
   （target_pct=None 或按数量折算），**再**判清仓；或把 `卖` 改成 `(?:^|[^减])卖`。
3. 决策 md 里**表格方向列**与**散文措辞**冲突时（表写「卖出」、散文写「减持」），
   解析器应以**保守口径**取「减」（不许从「有比例的减仓」推断成 0%）。
4. **配套负例**：在 `manu/etf_rotation/probes/probe_llm_parser.py` 加一条
   「原文写『减持 N 股』⇒ 方向必须 '减' 且 target 不为 0%」的正例 + 一条
   「把『减持』回退成『卖出』⇒ 断言必须转红」的变异负例。

### 重跑方式（**不要重跑 25 分钟 LLM**）
本轮 LLM **已成功**（`tradingagents_decision.md` / `.json` 已落盘且有 sha1）⇒
```bash
cd /tmp/pycharm_project_658d3fc4
bash manu/etf_rotation/run_pipeline.sh --asof 2026-11-03 --reuse-decision   # 不重跑 LLM
# 之后重跑重建收尾：指纹表重生成 + --dry-run 复验
```
⚠️ `--reuse-decision` 前置校验「决策比上下文新 + 报告内容指纹与登记一致」，
不满足会 die ⇒ 那就重跑完整 LLM（约 25 分钟）。

### 连带影响
- 步骤 1 判红 ⇒ **canonical 基线未按新口径重建** ⇒ `probe_lookahead` L20 金标
  仍可能红（这正是步骤 1 存在的理由）⇒ **步骤 4 闸门大概率仍红**。
- 步骤 2（统一流水线实跑）**不受此缺陷阻断**（它走 `daily/main.py` 新链路，
  不经 `run_pipeline.sh` 的 14/16 闸门）⇒ 快照产物大概率正常产出。

---

## 9. 🔴 独立代码审计待办（2026-11-09 17:50，reviewer 独立审计；工作区未被其改动）

审计结论：需求 1/3/5 = **PARTIAL**，需求 2/4 = PASS；架构正本 A/B = PASS、C = PARTIAL。
**以下均在本轮需求范围内**，明天按 P0→P2 顺序修：

### P0-1 `--send-only` 静默失效（相对 HEAD 的功能回归）
- `daily/main.py:601` 草稿写进 `<SNAPSHOT_ROOT>/<run_id>/email_draft_<date>.json`，
  而 `:316` / `:829` 的 `--send-only` 读 `mode_router.draft_path(asof, DC.RUN_ROOT)`
  ⇒ **两个路径永不相交** ⇒ 空等 `--wait-seconds`（默认 1800s）后 `:326 return 0`，
  **不报错、不发信、退出码 0**（静默假成功）。
- 修法：`--send-only` 改为在 `SNAPSHOT_ROOT` 下 glob 最新快照里的 `email_draft_<date>.json`；
  或 `_persist_draft` 同时写 `RUN_ROOT` 兼容位。同步修 `daily/README.md:368`。

### P0-2 「模式 A/B 分诊」概念未随 Mode C 一并删除（违反 `rules.md:1971`）
- `daily/mode_router.py:106` `route()` 仍返回 mode `"A"/"B"`；
  `daily/calendar_gate.py:61` `MODE_A/MODE_B` + `:283 resolve_mode()` + `:295 describe_mode()`
  （后者仍写「走模式 B：…下发执行卡」）；`daily/__init__.py:7,10,14` 模块表仍写「模式分流」。
- `daily/tests/test_agents_rebalance.py:272,281,305` 仍断言 `MR.route(...) == "A"/"B"`（实测 32 passed ⇒ 残留是活的）。
- 运行时无影响（流水线无调用点），但**需求 1「精简到只有 modelB」未收口**。
- 修法：删 `mode_router.route()`、`calendar_gate.MODE_A/MODE_B/resolve_mode/describe_mode`
  与对应测试；`mode_router` 只保留 `normalize_date`/`draft_path`；同步 `daily/__init__.py`。

### P1-3 `--full` 管不到增量开关（需求 3「全部流程」有洞）
- `daily/main.py:478-479` `do_increment=not args.no_increment` 直读 ⇒ `--no-increment` /
  `--increment-dry-run` **不受 `--full` 管辖**；`daily/README.md:143` 却写「增量日由 `--full` 口径触发」⇒ 自相矛盾。
- 修法：把增量纳入 `--full` 覆盖集合并在 `skipped` 披露，或改文档明确 `--full` 不含增量。

### P1-4 `--send-only` 绕过整条流水线，`--full` 被静默忽略
- `daily/main.py:825-829` 的 `if args.send_only:` 先于 `_run_stages` ⇒ `resolve_stage_flags` 不被调用、
  无快照、无 manifest、退出码 0。修法：`--send-only` 与 `--full` 同时给出 ⇒ **退出码 2 拒绝**。

### P1-5 被 `--full` 覆盖的 skip 开关只进 manifest、**没进报告**
- `daily/main.py:403-405` `flags["skipped"]` 从未进入 `result["notes"]`；`daily/report.py:284-294`
  的「快照与运行口径」表无 skipped 行 ⇒ 违反 `rules.md:2001`。
- 修法：`flags["skipped"]` 拼进 `result["notes"]`，或在 `_provenance_rows` 加「本轮被覆盖的开关」行。

### P1-6 `--snapshot-root` 覆盖在底座链解析/决策件复用处被忽略（写死 `DC.SNAPSHOT_ROOT`）
- `daily/agents_rebalance.py:935,1019,1090` ⇒ 自定义根下链式底座断裂、退回 canonical 一次性种子。
- 修法：改用已有的 `snapshot.root`（`agents_rebalance.py:913`）。

### P1-7 失败的运行留下**没有 manifest 的快照目录**（孤儿目录）
- `daily/main.py:640` / `:841-843`：`snap.finalize()` 只在成功路径与非交易日分支调用；
  `StageError` 路径直接 `_handle_failure` ⇒ 半程产物无 `snapshot_manifest.json`，
  且 `probe_snapshot_lookahead.find_snapshot` 会跳过它 ⇒ 既无权威索引、也不被闸门选中。
- 修法：`except` 分支补 `snap.set_stage(stage,"failed")` + `snap.finalize()`。

### P1-8 L20 金标断言对象仍是根目录产物（正本 §三第 2 条要求改指快照）
- `probes/probe_lookahead.py:688` 默认 `--report RUN_ROOT/etf_analysis_report.json`；
  `run_gates.sh:142` 传 `$RUN_ROOT/...` ⇒ **本轮只换了金标数值、没换断言对象**。
- 修法：给 `run_gates.sh` 的 `probe_lookahead` 传快照内的 `--report/--md`，
  **保留根目录那一条作为基线一致性护栏**（不弱化闸门）。

### P1-9 S4 只证明「`Snapshot.write_text` 不碰 canonical」，没证明「流水线跑前后 canonical 三项指纹全等」
- `probes/probe_snapshot_contract.py:459-480` 用合成夹具直接调 snapshot API，**流水线一次没跑**；
  违反 `rules.md:1987`（「探针必须能证明：跑前后对 canonical 取 size+sha1+mtime 三项指纹」）。
- 修法：加一条真实 `main.py --skip-fetch --skip-llm --no-refresh-scores --no-update-qlib-bin`
  跑一次、前后取三项指纹的断言（需给探针一条安全夹具路径）。

### P1-10 观察态仍会改**共享 qlib 生产数据集** + 追加台账（需求 5 口径要改口径，不改代码）
- `daily/main.py:467-480` → `stage1_dataset.run_stage1(do_swap=True, dry_run=False, dest_dir=DC.STAGE1_DEST_DIR)`，
  `STAGE1_DEST_DIR=/mnt/data/siping/datasets/qlib`（`daily_config.py:192`）⇒ **动的是现役生产数据集**；
  另有 `_increment_ledger.jsonl`（`daily_config.py:203-208`）、`manu/report/` 交付副本。
- ⇒ `manu/docs/snapshot_architecture.md:201` 的副作用矩阵不实。
- 修法（二选一，**明早定**）：把换库纳入 `--live-trade` 门控，**或**在架构文档 §5 副作用矩阵 +
  `daily/README.md` 显式列出「观察态仍会换库」。

### P2（可与 P0/P1 同批做）
- `main.py:666,672,678`：`analyze`/`final_plan`/`execution_card` 三个阶段状态**恒为 `ok`**（取的是路径字符串）
  ⇒ 改为按件存在性判定（同 `:668` 的 `_has(...)`）。
- `main.py:589`：日志打的是快照**目录**而非报告副本文件路径 ⇒ 改成 `snap.file(f"report_{asof.replace('-','')}.md")`。
- `agents_rebalance.py:687,728`：`_refresh_scores` 的备份件 `stock_scores.csv.bak-<stamp>` 留在快照里不清理
  ⇒ 快照体积翻倍 ⇒ 合并成功后删除或移到快照外临时目录。
- `main.py:367`：`import mode_router` 在 `_run_stages` 内未被使用 ⇒ 删。
- `agents_rebalance.run_pipeline(dry_run=True)`（前置判定）与 CLI `--dry-run`（观察态、照样落快照）
  **同名不同义** ⇒ 建议把 CLI 的改名或加注释。

---

## 8b. 🔴 P0 根因·精确版（2026-11-09 17:53 实取服务器决策 md 后定位）

### 权威输入（`/mnt/data/siping/runs/etf_rotation/tradingagents_decision.md`）
- L42 **表格**：`| **512800** | **减** | **约15%** | 保留唯一正E…降低单一银行行业风险 |`
  ⇒ **裁决本身是「减仓到 15%」，不是清仓**
- L55 `- 512800减持约**2,600份**，保留约**5,100份**…约占总资产14.93%；`
- L57 `- 卖出512800预计回笼约**2,272.40元**；` ← **「预计回笼」是收益预估句，不是卖出指令**

### 三个代码点（都能在本地单测里钉死）
1. `manu/etf_rotation/llm_final.py:207` 规则②
   `(?:卖出|卖掉|减掉)\s*(?:约)?\s*[\d,]+(?:\.\d+)?\s*(?:股|份)`
   ⇒ **不认「减持/减仓 + 数量」** ⇒ L55「减持约2,600份」走不到规则②。
2. `manu/etf_rotation/llm_final.py:218` 规则④ 裸 `卖出|卖掉` ⇒ **卖 + 0.0**
   ⇒ L57「卖出512800**预计回笼**约2,272.40元」被当成**清仓指令**。
   🔴 **「预计回笼/预计回收」是收益预估句，不是动作** ⇒ 必须排除。
3. `manu/etf_rotation/final_rebalance_plan.py:1081`
   `re.search(r"卖|清|不买|退出|清掉", direction)` ⇒ **「卖」是子串**，落在「减持」里
   ⇒ 减仓方向被兜底强制 `target_pct = 0.0`。
   （同文件 `:401` `_PARTIAL_SELL_RE` 也只认「卖出约 N 股」，不认「减持约 N 份」）

### 修法（明天做；必须带正例 + 变异负例，不许弱化闸门）
1. **规则②扩词**：`llm_final.py:207` ⇒
   `(?:卖出|卖掉|减掉|减持|减仓)\s*(?:约)?\s*[\d,]+(?:\.\d+)?\s*(?:股|份)`
   ⇒ L55 判成「减」。`final_rebalance_plan.py:401` `_PARTIAL_SELL_RE` 同步扩。
2. **规则④加负向护栏**：`llm_final.py:218` 命中前先排除收益预估句
   （含「预计回笼 / 预计回收 / 回笼约 / 预计可得」）⇒ 这类句子**不给方向、不给 target**
   （宁可留 None，不猜）。
3. **`:1081` 子串正则**：先判「减/减持/减仓」⇒ 走「减」分支；或把 `卖` 写成 `(?:^|[^减])卖`。
4. **配套断言**（`manu/etf_rotation/probes/probe_llm_parser.py`）：
   - 正例「原文写『减持约 N 份，保留约 M 份』⇒ 方向必须 '减'、target **不为 0**」
   - 变异负例「把『减持』回退成『卖出』⇒ 断言必须**转红**」
   - 正例「卖出 X **预计回笼** N 元」⇒ **不得**产生 target=0 的卖出指令
5. **验收**：`probe_llm_parser.py` 现有 76 项 + 新增项 **全 PASS**；
   再 `run_pipeline.sh --asof 2026-11-03 --reuse-decision` 重跑（**不重跑 25 分钟 LLM**）。

---

## 8c. ✅ P0 修复**已落地并本地验证**（2026-11-09 17:57，host 本地；**服务器尚未同步**）

### 已改代码（host 工作区，**未 commit / 未 push**）
| 文件 | 改动 |
|---|---|
| `manu/etf_rotation/llm_final.py` 规则② | 词表扩为 `(?:卖出|卖掉|减掉|减持|减仓)\s*(?:约)?\s*[\d,]+…(?:股\|份)` ⇒ L55「减持约2,600份」判成「减」 |
| `manu/etf_rotation/llm_final.py` 规则④ | 裸「卖出/卖掉」加**负向前瞻**：`(?![^。；\n]{0,40}?(?:回笼\|回款\|折现\|变现))` ⇒ 「卖出 X **预计回笼** N 元」是**收益预估句**，不再当卖出指令（「全部卖出/清仓」仍由规则①先判，不受影响） |
| `manu/etf_rotation/final_rebalance_plan.py` 零判定 | `re.search(r"卖\|…", direction)` ⇒ `re.search(r"(?:^\|[^减])卖\|清\|…", direction)` ⇒ 「卖」落在「减持」里不再被当清仓、减仓保持 `target_pct=None`（不猜） |
| `manu/etf_rotation/tests/test_llm_final_partial_sell.py`（**新增**） | 3 条断言：①「减持+数量」⇒ 方向「减」且 target≠0；②「预计回笼」句 ⇒ 不得产生 target=0；③**真清仓仍判「卖+0%」**（防误伤） |

### 本地验证证据（uav env）
- 新单测：**3 passed**
- 🔴 **变异负例已验**：把 `llm_final.py` 回退成旧版 ⇒ 新单测 **1 failed**，失败信息精确复现事故
  （`target_pct: 0.0, direction: '卖'`，note=「原文写明卖出」）⇒ **测试是 load-bearing 的，不是凑数**
- 恢复新代码 ⇒ 3 passed；`md5sum` 双向校验文件字节一致
- 全量回归：**128 passed**（原 125 + 新增 3），**0 failed**
- `probe_snapshot_contract` **37/0**、`probe_score_smoothing` **55/0** ⇒ 无回归

### 🔴 明早必做（顺序不能乱）
1. `bash manu/tools/sync_to_server.sh manu` 同步（**md5 双侧比对**为准）⇒
   `llm_final.py` / `final_rebalance_plan.py` 在指纹表 md5 段内 ⇒ **必须重生成指纹表**（步骤 3 会自动做）
2. 服务器上跑 `probe_llm_parser.py`（对着真实 artifacts）⇒ 现有 76 项 + 新增项 **全 PASS**
3. `bash manu/etf_rotation/run_pipeline.sh --asof 2026-11-03 --reuse-decision`
   ⇒ **不重跑 25 分钟 LLM**，只重跑 8~16 步 ⇒ 重建应转绿
4. 步骤 1 转绿后重跑步骤 4 全量闸门 ⇒ L20 金标大概率转绿（canonical 与金标同源）
5. ⚠️ **不要在服务器跑批期间同步代码**（步骤 2 正在跑 `daily/main.py`）⇒ 同步时机必须等步骤 2 结束

---

## 9b. ✅ P0-1 / P0-2 已修复并本地验证（2026-11-09 17:58，host 本地；**服务器尚未同步**）

### P0-1 `--send-only` 静默失效（相对 HEAD 的功能回归）—— **已修**
- 根因：草稿写在**快照目录**（`_persist_draft(draft, run_dir=snap.path, …)` ⇒
  `<SNAPSHOT_ROOT>/<run_id>/email_draft_<YYYYMMDD>.json`），
  而 `--send-only` 读 `mode_router.draft_path(asof, run_root)` ⇒
  `<run_root>/<YYYYMMDD>/email_draft_<YYYYMMDD>.json` ⇒ **两路径永不相交**
  ⇒ 空等 `--wait-seconds`（默认 1800s）后 **退出码 0 却没发信**（静默假成功）。
- 修法（`daily/main.py`）：新增 `_resolve_draft(asof, snapshot_root, run_root)`
  ⇒ **先快照根（按目录名倒序 = 时间倒序，取最新一份含草稿的快照）→ 回落旧按日目录**；
  等待循环**每轮重新解析**（等待期间新出的快照也能被看见）。
- 附带修 P1-4：`--send-only` 与 `--full` **互斥** ⇒ 退出码 **2**（原先 `--full` 被静默忽略）。
- 新增测试 `daily/tests/test_send_only_draft.py`（5 条：快照命中 / 最新优先 / 旧目录回落 /
  无草稿不抛异常 / `--send-only --full` ⇒ rc2）。

### P0-2 「模式 A/B 分诊」概念未删（需求 1 未收口）—— **已修**
- 删除：`daily/mode_router.py::route()` 与 `_calendar_days()`（随之成为死代码）；
  `daily/calendar_gate.py::MODE_A` / `MODE_B` / `resolve_mode()` / `describe_mode()`。
- 新增 `daily/calendar_gate.py::describe_day(date_str)`：**只给事实**
  （`date` / `is_silent_defense` / `is_signal_date` / `execution_date` / `reason`），
  **不再有 `mode` 键**；`reason` 文案也不再出现「走模式 A/B」。
- 清理残留字样：`agents_rebalance.py`（章节标题 + **报错文案**「模式 B 第 N 步」→「第 N 步」）、
  `calendar_gate.py` docstring/章节头、`daily_config.py`、`account_state.py`、
  `stage1_rolling_increment.py`、`daily/__init__.py` 模块表。
- 测试同步**换成新契约**（不是删测试）：
  `test_agents_rebalance.py` 新增「`mode_router`/`calendar_gate` **不许**再有 mode API」+
  「当日口径只给事实、**不带 mode 键**」；`test_calendar_gate.py` 同理替换旧的两条。
- 复查：全仓（排除 `manu/report/` 历史归档与 `manu/memory/` 历史叙事）**已无「模式 A/B」字样**；
  剩下的 README 提及都是在**说明该概念已废除**（有意保留）。

### 本地验证证据（uav env）
- `pytest manu -q` ⇒ **135 passed / 0 failed**（125 基线 + 3 解析器 + 5 send-only + 2 新契约）
- 🔴 **变异负例已验（两处都验）**：
  - 回退 `llm_final.py` ⇒ 解析器新单测 **1 failed**（精确复现 `target_pct: 0.0, direction: '卖'`）
  - 回退 `daily/main.py` ⇒ send-only 新单测 **4 failed**（`AttributeError: _resolve_draft`）
  ⇒ 两组新测试都是 **load-bearing** 的，不是凑数
- `probe_snapshot_contract` **37/0**、`probe_score_smoothing` **55/0**
- CLI 闸门：`--mode B` ⇒ rc2｜`--dry-run --live-trade` ⇒ rc2｜`--send-only --full` ⇒ rc2

### 🔴 明早同步顺序（**顺序错 ⇒ 指纹表必红**）
1. 等服务器步骤 2/3/4 跑完（**跑批期间不许同步**）
2. `bash manu/tools/sync_to_server.sh manu` ⇒ md5 双侧比对
   （本轮改动的文件：`llm_final.py`、`final_rebalance_plan.py`、`daily/main.py`、
   `daily/mode_router.py`、`daily/calendar_gate.py`、`daily/agents_rebalance.py`、
   `daily/daily_config.py`、`daily/account_state.py`、`daily/stage1_rolling_increment.py`、
   `daily/__init__.py`、`daily/tests/*`、新增 `daily/tests/test_send_only_draft.py`、
   新增 `manu/etf_rotation/tests/test_llm_final_partial_sell.py`）
3. **重生成指纹表**（改了被登记的文件 ⇒ 必须重生成 + `--dry-run` 复验）
4. 跑 `probe_llm_parser.py`（真实 artifacts）⇒ 全 PASS
5. `run_pipeline.sh --asof 2026-11-03 --reuse-decision` ⇒ 重建转绿（**不重跑 25 分钟 LLM**）
6. 重跑步骤 4 全量闸门（25 阶段）
7. memory 四层回写 ⇒ 若同步了 memory ⇒ **再重生成一次指纹表** + `--dry-run` 复验
