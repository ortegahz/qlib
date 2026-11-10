#!/usr/bin/env bash
# manu/etf_rotation/run_gates.sh —— ETF 轮动线「全量闸门」一键复跑脚本
#
# 【是什么】
#   把已跑通的全量闸门固化成一条可复制粘贴的命令：26 个探针阶段 + 1 个可复现性段（repro），
#   逐项实跑、逐项记录退出码，最后打印汇总（各阶段退出码 + 断言 PASS 行数）。
#   本脚本只做「编排 + 记账」，自身不产出任何策略结论：
#   不联网、不调 LLM、不跑 akshare、不训练、不回测；重活都在被调用的子脚本里。
#
# 【怎么跑】（服务器工作副本 /tmp/pycharm_project_658d3fc4）
#   bash manu/etf_rotation/run_gates.sh                     # 全量（含 repro 段，耗时数分钟）
#   bash manu/etf_rotation/run_gates.sh --skip-repro         # 轻量版：跳过最重的 repro 段
#   bash manu/etf_rotation/run_gates.sh --run-root DIR --log FILE
#   bash manu/etf_rotation/run_gates.sh --help
#
# 【参数】
#   --repo-root PATH  仓库根（默认 /tmp/pycharm_project_658d3fc4）
#   --run-root PATH   产物根（默认 <结果根> = /mnt/data/siping/runs/etf_rotation_results）
#   --artifact-root PATH  闸门断言对象（交付件根）；缺省 = 最新完整快照，没有则回落 canonical
#   --log PATH        日志路径（默认 $RUN_ROOT/_gates_latest.log）
#   --skip-repro      跳过 probe_reproducibility 段（该段会重跑 analyze.py + recommend.py +
#                     final_rebalance_plan.py，耗时数分钟）
#   --help            打印用法后退出
#
# 【退出码含义】
#   0 = 全部阶段通过（probe_verify_guards 的 rc=2「不适用」不算失败）
#   1 = 至少一个阶段失败（任一阶段 rc≠0；probe_verify_guards 的 rc=2 除外）
#   2 = 用法/环境错误（参数非法、目录或 Python 解释器不存在）⇒ 阶段还没开始跑
#
# 【记账口径】
#   * 断言 PASS 行数 = 日志中带断言通过标记的行数（口径：grep -c '\[PASS\]'）。
#   * probe_verify_guards.py 的退出码 2 = 「不适用」（**不是通过**）⇒ 汇总里显式标注、不计失败。
#   * repro 段的临时目录建在临时工作区 `<数据集根>/_work/` 下（不落 /tmp），跑完必删（trap 兜底）。
#   * repro 段的结论 JSON 写进临时目录（不覆盖产物根里已有的同名文件）。

set -uo pipefail

REPO_ROOT="/tmp/pycharm_project_658d3fc4"
#: 🔴 **结果落点根**（可被环境变量 `ETF_ROTATION_RESULTS_ROOT` 覆盖 ⇒ 整棵树可搬家）
#:    产物根 = 结果根本身（与 `daily_config.RESEARCH_RUN_ROOT` 同口径）
RESULTS_ROOT="${ETF_ROTATION_RESULTS_ROOT:-/mnt/data/siping/runs/etf_rotation_results}"
RUN_ROOT="$RESULTS_ROOT"
#: 🔴 **数据集落点根**（只用来放**临时工作区**；与 `daily_config.OUTPUT_ROOT` 同口径）
DATASET_ROOT="${ETF_ROTATION_OUTPUT_ROOT:-/mnt/data/siping/datasets/manu/etf_rotation}"
WORK_ROOT="$DATASET_ROOT/_work"
#: 快照根（与 `daily_config.SNAPSHOT_ROOT` 同口径：产物根下的 `snapshots/`）
#: 🔴 2026-11-10：改为**参数解析之后**按最终 `--run-root` 重算（原先写在默认值旁边 ⇒
#:    `--run-root` 换了产物根却没跟着换 ⇒ 闸门去验**别处**的快照）。
SNAPSHOT_ROOT=""
#: 闸门断言对象（**交付件根**）：缺省 = 最新一份**完整快照**；`--artifact-root` 可显式指定。
#: 🔴 架构迁移后流水线**不再回写 canonical 根目录的交付件** ⇒ 闸门断言对象必须跟着走，
#:    否则永远在验「上一轮的旧产物」（架构迁移在闸门层没落地）。
ARTIFACT_ROOT=""
LOG=""
SKIP_REPRO=0
QL_PY="${QL_PY:-/home/huangzhe/anaconda3/envs/ql/bin/python}"
#: repro 段的信号日（M-S3 基准口径，照抄产物根 _run_scripts/_run_gates_ms2.sh）
ASOF="2026-11-03"

usage(){ sed -n '2,/^set -uo/p' "$0" | sed '$d'; }
die(){ echo "FATAL: $*" >&2; exit 2; }

while [ $# -gt 0 ]; do
  case "$1" in
    --repo-root)  [ $# -ge 2 ] || die "--repo-root 需要一个路径"; REPO_ROOT="$2"; shift 2 ;;
    --run-root)   [ $# -ge 2 ] || die "--run-root 需要一个路径";  RUN_ROOT="$2";  shift 2 ;;
    --artifact-root) [ $# -ge 2 ] || die "--artifact-root 需要一个路径"; ARTIFACT_ROOT="$2"; shift 2 ;;
    --log)        [ $# -ge 2 ] || die "--log 需要一个路径";       LOG="$2";       shift 2 ;;
    --skip-repro) SKIP_REPRO=1; shift ;;
    -h|--help)    usage; exit 0 ;;
    *) echo "unknown arg: $1" >&2; usage >&2; exit 2 ;;
  esac
done

#: 快照根跟着**最终**产物根走（`--run-root` 改了这里必须跟着改）
SNAPSHOT_ROOT="$RUN_ROOT/snapshots"

[ -d "$REPO_ROOT" ] || die "仓库根不存在：$REPO_ROOT"
[ -d "$RUN_ROOT" ]  || die "产物根不存在：$RUN_ROOT"
[ -x "$QL_PY" ]     || die "找不到 ql 解释器：$QL_PY（可用环境变量 QL_PY 覆盖）"
[ -n "$LOG" ] || LOG="$RUN_ROOT/_gates_latest.log"

#: repro 段临时目录：落**临时工作区**（`<数据集根>/_work/`）
#:    🔴 共享机 ⇒ 临时目录也只许落本项目的根之下（不落 /tmp、不落公共目录）
REPRO_TMP="$WORK_ROOT/_repro_tmp"
case "$REPRO_TMP" in
  /mnt/data/*) : ;;
  *) die "repro 临时目录必须落在 /mnt/data 下（当前：$REPRO_TMP）" ;;
esac

cleanup(){ if [ -n "${REPRO_TMP:-}" ] && [ -d "$REPRO_TMP" ]; then rm -rf "$REPRO_TMP"; fi; return 0; }
trap cleanup EXIT

cd "$REPO_ROOT" || die "进不去仓库根：$REPO_ROOT"
: > "$LOG" || die "日志写不进去：$LOG"

# =============================================================================
# 闸门断言对象（交付件根）解析
# =============================================================================
# 🔴 2026-11-10（架构迁移在闸门层的落地）：流水线**不再回写 canonical 根目录的交付件**
#    （每次运行落 `snapshots/<YYYYMMDD_HHMMSS>/`）⇒ 闸门断言对象默认取
#    `$SNAPSHOT_ROOT` 下**最新且完整**的快照目录。
#    「完整」= 同时存在 snapshot_manifest.json + etf_analysis_report.json + etf_analysis_report.md。
#    一个完整快照都找不到 ⇒ 回落 canonical（**显式打印**，不许静默）。
if [ -n "$ARTIFACT_ROOT" ]; then
  [ -d "$ARTIFACT_ROOT" ] || die "交付件根不存在：$ARTIFACT_ROOT"
  ARTIFACT_NOTE="显式指定（--artifact-root）"
else
  ARTIFACT_ROOT=""
  _snap_list=""
  for _d in "$SNAPSHOT_ROOT"/*/; do
    [ -d "$_d" ] || continue
    _snap_list="${_snap_list}${_d}"$'\n'
  done
  #: 目录名按 `YYYYMMDD_HHMMSS`（字典序 == 时间序）**倒序** ⇒ 第一个完整的就是最新的
  while IFS= read -r _d; do
    [ -n "$_d" ] || continue
    if [ -f "$_d/snapshot_manifest.json" ] && [ -f "$_d/etf_analysis_report.json" ] \
       && [ -f "$_d/etf_analysis_report.md" ]; then
      ARTIFACT_ROOT="${_d%/}"
      break
    fi
  done < <(printf '%s' "$_snap_list" | LC_ALL=C sort -r)
  if [ -n "$ARTIFACT_ROOT" ]; then
    ARTIFACT_NOTE="缺省 = 最新完整快照"
  else
    ARTIFACT_ROOT="$RUN_ROOT"
    ARTIFACT_NOTE="未找到完整快照 ⇒ 回落 canonical"
  fi
fi
echo "闸门断言对象（交付件根）：$ARTIFACT_ROOT（$ARTIFACT_NOTE）" | tee -a "$LOG"

declare -a RC_NAME=() RC_VAL=() RC_DESC=()

add_stage(){ RC_NAME+=("$1"); RC_VAL+=("$2"); RC_DESC+=("$3"); }

run_stage(){  # run_stage <名字> <说明> <命令...>
  local name="$1" desc="$2"; shift 2
  echo "### $name" >> "$LOG"
  ( "$@" ) >> "$LOG" 2>&1
  local rc=$?
  echo "EXITCODE $name=$rc" >> "$LOG"
  add_stage "$name" "$rc" "$desc"
}

# =============================================================================
# 26 个探针阶段（顺序与基准脚本 _run_gates_ms2.sh 完全一致；M-S5 新增第 15 个 probe_node_basis；
#                       M-S6 新增第 16 个 probe_llm_stage_repro（LLM 阶段可复现闸门）、
#                       第 17 个 check_memory_tables（memory 表格结构检查器，**只读**）、
#                       第 18 个 probe_decision_scenarios（三项分歧组合矩阵闸门）与
#                       第 19 个 probe_node_scenarios（节点日分歧情景重算闸门）；
#                       2026-11-09 新增第 22 个 probe_qlib_provider（provider 单一事实源闸门））、
#                       第 23 个 probe_score_smoothing（穿透分时间平滑闸门）；
#                       2026-11-09 架构瘦身：**第 21 个阶段改成 probe_snapshot_contract
#                       （快照契约闸门）—— 旧的 Mode A/B/C 分诊阶段已废除）；
#                       2026-11-10 新增第 24 个 probe_snapshot_lookahead（快照结构 + 无未来函数闸门））
# =============================================================================

### verify_artifacts —— 交付产物完整性/指纹/内部一致性校验
run_stage verify_artifacts "交付产物完整性/指纹/内部一致性校验" \
  "$QL_PY" manu/etf_rotation/verify_artifacts.py --run-root "$ARTIFACT_ROOT"

### probe_report_spec —— 分析报告字段规格校验（必填键/类型/披露完整性）
run_stage probe_report_spec "分析报告字段规格校验（必填键/类型/披露完整性）" \
  "$QL_PY" manu/etf_rotation/probes/probe_report_spec.py --report "$ARTIFACT_ROOT/etf_analysis_report.json"

### probe_llm_parser —— LLM 决策文本解析器自检（各种输出形态 → 结构化字段）
# 🔴 这个探针**读环境变量** `ETF_RUN_ROOT`（没有命令行参数）⇒ 跑之前先导出。
export ETF_RUN_ROOT="$ARTIFACT_ROOT"
run_stage probe_llm_parser "LLM 决策文本解析器自检（输出形态 → 结构化字段）" \
  "$QL_PY" manu/etf_rotation/probes/probe_llm_parser.py

### probe_verify_guards —— 语义守卫/可追溯守卫自检（正例 + 变异负例）；rc=2 表示「不适用」
run_stage probe_verify_guards "语义守卫/可追溯守卫自检（正例 + 变异负例）；rc=2 = 不适用" \
  "$QL_PY" manu/etf_rotation/probes/probe_verify_guards.py --run-root "$ARTIFACT_ROOT" --project-dir "$REPO_ROOT"

### probe_plan_robustness —— 调仓计划生成器稳健性（输入扰动下的输出稳定性）
run_stage probe_plan_robustness "调仓计划生成器稳健性（输入扰动下的输出稳定性）" \
  "$QL_PY" manu/etf_rotation/probes/probe_plan_robustness.py \
  --plan "$ARTIFACT_ROOT/final_rebalance_plan.json" --md "$ARTIFACT_ROOT/final_rebalance_plan.md"

### probe_plan_arithmetic —— 调仓计划算术自洽（权重/份额/金额/费用）
run_stage probe_plan_arithmetic "调仓计划算术自洽（权重/份额/金额/费用）" \
  "$QL_PY" manu/etf_rotation/probes/probe_plan_arithmetic.py \
  --plan "$ARTIFACT_ROOT/final_rebalance_plan.json" --md "$ARTIFACT_ROOT/final_rebalance_plan.md"

### probe_execution_date —— 执行日/信号日/调仓节点的日期口径校验
run_stage probe_execution_date "执行日/信号日/调仓节点的日期口径校验" \
  "$QL_PY" manu/etf_rotation/probes/probe_execution_date.py \
  --plan "$ARTIFACT_ROOT/final_rebalance_plan.json" --md "$ARTIFACT_ROOT/final_rebalance_plan.md" \
  --report "$ARTIFACT_ROOT/etf_analysis_report.json"

### probe_microstructure —— ETF 微观结构口径校验（折溢价/跟踪误差代理/流动性）
run_stage probe_microstructure "ETF 微观结构口径校验（折溢价/跟踪误差代理/流动性）" \
  "$QL_PY" manu/etf_rotation/probes/probe_microstructure.py \
  --report "$ARTIFACT_ROOT/etf_analysis_report.json" --md "$ARTIFACT_ROOT/etf_analysis_report.md" \
  --stock-prices "$ARTIFACT_ROOT/stock_prices.csv"

### probe_lookahead —— 前视审计（决策前视项台账 + 实际消费日）；断言对象 = **根目录基线**
run_stage probe_lookahead "前视审计（决策前视项台账 + 实际消费日）｜断言对象=根目录基线" \
  "$QL_PY" manu/etf_rotation/probes/probe_lookahead.py \
  --report "$RUN_ROOT/etf_analysis_report.json" --md "$RUN_ROOT/etf_analysis_report.md"

### probe_canonical_immutability —— canonical 只读性（**跑真实流水线** + 三项指纹）；rc=2 = 不适用
# 🔴 2026-11-09（rules.md §15.3.3 / 独立审计 P1-9）：
#    「探针**必须能证明**：跑前后对 canonical 取 size+sha1+mtime 三项指纹 ⇒ 必须完全一致」
#    ⇒ 这里**真的跑一次** daily/main.py（观察态 + 四个 skip 开关 ⇒ 起不了重活），
#      跑前跑后各取一次指纹，逐文件比对。
run_stage probe_canonical_immutability "canonical 只读性（跑真实流水线 + size/sha1/mtime 三项指纹）；rc=2 = 不适用" \
  "$QL_PY" manu/etf_rotation/probes/probe_canonical_immutability.py --asof "$ASOF" --repo-root "$REPO_ROOT"

### probe_lookahead_snapshot —— 前视审计（**断言对象 = 本轮快照内那一份**）；rc=2 = 不适用
# 🔴 2026-11-09（正本 §三第 2 条）：上一条是**基线一致性护栏**（断言根目录那份，
#    保留不动）；本条才是「断言当前运行生成的快照目录」的那一份。
#    两条**都**要绿：基线不许漂，快照也不许有未来函数。
run_stage probe_lookahead_snapshot "前视审计｜断言对象=本轮快照内那一份；rc=2 = 不适用" \
  "$QL_PY" manu/etf_rotation/probes/probe_lookahead.py \
  --snapshot-root "$SNAPSHOT_ROOT"

### probe_data_freshness —— 数据新鲜度校验（行情最新日与滞后披露）
run_stage probe_data_freshness "数据新鲜度校验（行情最新日与滞后披露）" \
  "$QL_PY" manu/etf_rotation/probes/probe_data_freshness.py --run-root "$ARTIFACT_ROOT"

### probe_refresh_integrity —— 刷新链路完整性（预检提示命令可直接复制执行）
run_stage probe_refresh_integrity "刷新链路完整性（预检提示命令可直接复制执行）" \
  "$QL_PY" manu/etf_rotation/probes/probe_refresh_integrity.py --run-root "$ARTIFACT_ROOT"

### probe_signal_feasibility —— 信号可落地性校验（池/行情/可交易性硬拦项）
run_stage probe_signal_feasibility "信号可落地性校验（池/行情/可交易性硬拦项）" \
  "$QL_PY" manu/etf_rotation/probes/probe_signal_feasibility.py --run-root "$ARTIFACT_ROOT"

### probe_node_reprice —— 节点日重算工具链闸门
run_stage probe_node_reprice "节点日重算工具链闸门" \
  "$QL_PY" manu/etf_rotation/probes/probe_node_reprice.py --run-root "$ARTIFACT_ROOT"

### probe_node_basis —— 节点日「账户口径双基准」闸门（M-S5 新增，16 项）
run_stage probe_node_basis "节点日账户口径双基准闸门（plan/node 双口径 + 变异反证）" \
  "$QL_PY" manu/etf_rotation/probes/probe_node_basis.py --run-root "$ARTIFACT_ROOT"

### probe_run_lock —— 流水线实例守卫（flock）与退出码口径回归
run_stage probe_run_lock "流水线实例守卫（flock）与退出码口径回归" \
  env PROJ_DIR="$REPO_ROOT" RUN_ROOT="$RUN_ROOT" bash manu/etf_rotation/probes/probe_run_lock.sh

### probe_llm_stage_repro —— LLM 阶段可复现闸门（M-S6 新增，M-Q 轮重写为 12 项）
###   钉死四件事：① LLM 运行脚本在 git 仓库里常驻且**指纹已登记**（§十四.82）；
###   ② 交付决策 json 口径正确（provider/模型/effort/max_tokens）+ 研究上下文未过期；
###   ③ **零 LLM 调用**即可逐字节重渲出交付 md（`--render-only`）；
###   ④ 多空两方的**每张表块都能在交付 md 里逐字找到**（事实源 = 决策 json 的
###      `investment_debate`）且逐表块完整（行内 `|` 一致 / 行数 ≥ 5）——
###      M-N 事故：空头表整表消失 ⇒ 必须 FAIL。
###      🔴 **不跨侧比较表结构**（两家分析师的表本来就允许不同 ⇒ 那条等式是伪断言）。
###   🔴 指纹表（`_ms5_md5.txt`）是 **canonical 根**的「代码状态登记表」⇒ **不在快照里**
###      （快照的 16 交付件清单里没有它）⇒ 探针自己回落解析：
###      `<artifact-root>/_ms5_md5.txt` → `<canonical>/_ms5_md5.txt`；
###      也可用 `--fingerprint-table` 显式指定。
run_stage probe_llm_stage_repro "LLM 阶段可复现闸门（脚本常驻+指纹登记+渲染可复现+多空表逐字在件）" \
  "$QL_PY" manu/etf_rotation/probes/probe_llm_stage_repro.py --run-root "$ARTIFACT_ROOT"

### check_memory_tables —— memory 表格结构检查（M-S6 新增，只读；退出码 1 = 有列数错位）
###   🔴 判定基于「**未转义**竖线数」：正文里已转义的 `\|`（引述表格表头）**不算缺陷**；
###   未转义的 `|`（如代码段里的 `ls || true`）会把一行切成多格 ⇒ 表格渲染错位 ⇒ 必须 FAIL。
run_stage check_memory_tables "memory 表格结构检查（未转义竖线 ⇒ 列数错位检测）" \
  "$QL_PY" manu/tools/check_memory_tables.py --project-dir "$REPO_ROOT"

### probe_decision_scenarios —— 「三项分歧组合矩阵」产物闸门（M-S6 新增，10 项 = 9 正向 + 1 负例）
###   钉死四件事：① 生成器脚本在仓库里常驻；② 生成器跑通且 5 条自洽断言全 PASS；
###   ③ **两次运行逐字节相同**（幂等）；④ 与已交付的 decision_scenarios_*.json/md **逐字节一致**（漂移即 FAIL）。
###   另含 P6 **独立重算**（从 final_rebalance_plan.json 手推 8 情景现金/权益/总资产，不复用生成器函数）
###   与 D1 真负例（改坏 A/A/A 现金 ⇒ 必须检出）⇒ 闸门有牙，不是摆设。
run_stage probe_decision_scenarios "三项分歧组合矩阵闸门（生成器常驻+幂等+独立重算+交付件漂移检测）" \
  "$QL_PY" manu/etf_rotation/probes/probe_decision_scenarios.py --run-root "$RUN_ROOT"

### probe_node_scenarios —— 节点日「分歧情景」重算闸门（M-S7 新增，13 项 = 10 正向 + 3 负例）
###   钉死四件事：① 两个脚本常驻；② **默认（规则层）路径零回归**（产物无 scenario 字段）；
###   ③ 情景 A/A/A ≡ 规则层逐条一致、8 情景现金非负/整手合法/**独立重算**现金与破单席标记、幂等；
###   ④ 非法情景字母 / 未知代码 ⇒ 必须硬拦（负例 D1/D2），篡改现金 ⇒ 独立重算必须 FAIL（负例 D3）。
run_stage probe_node_scenarios "节点日分歧情景重算闸门（默认零回归+情景一致+独立重算+非法输入硬拦）" \
  "$QL_PY" manu/etf_rotation/probes/probe_node_scenarios.py --run-root "$RUN_ROOT"

### probe_rolling_increment —— 阶段 1「自建增量日」闸门（新增第 20 个探针阶段）
###   钉死五件事：① qlib bin 的**日历对齐**（追加/缺口补 NaN/跳写拒绝）；
###   ② **基座零改动**（派生目录是硬链接副本 ⇒ 写前必须 copy-on-write）；
###   ③ **守卫剔除不错位**（基准偏离的标的不写 ⇒ qlib 读出 NaN，不整体错位）；
###   ④ **写盘配方口径**（close=原始价×factor / volume=原始手数÷factor / amount=元÷1000
###      / vwap=后复权均价 / change=原始涨跌幅）；
###   ⑤ **fail-closed**（缺中间交易日的 bar ⇒ 抛错且磁盘零副作用；dry-run ⇒ 完全不落盘）。
###   🔴 合成数据集 + **不联网** + 不碰生产数据集。
run_stage probe_rolling_increment "阶段 1 自建增量日闸门（日历对齐+基座零改动+守卫+写盘配方+fail-closed）" \
  "$QL_PY" manu/etf_rotation/probes/probe_rolling_increment.py

### probe_snapshot_contract —— **快照契约闸门**（第 21 个探针阶段）
###   钉死 **2026-11-09 架构瘦身** 的快照制不变式（单一无状态流水线 ＋ 不可变快照目录）：
###   ① **S1** 每次 `create_snapshot` 产生**新**目录（同一秒两次 ⇒ 后者加后缀、**绝不覆盖**；
###      旧快照里的件**逐字节不变**）；
###   ② **S2** 快照**自包含**（manifest 存在 ＋ `artifacts` 覆盖契约 §4 的件名清单 ＋ 无软链）；
###   ③ **S3** `resolve_scores_base` 三种情形（有上一份快照 ⇒ 取快照内 `stock_scores.csv` /
###      无快照但有 canonical ⇒ 取 canonical / 两者都无 ⇒ `(None, 说明含「无」)`）；
###   ④ **S4** **不写根目录全局文件**（建快照 + 写件前后，canonical 的 size/sha1/mtime **三项全等**）；
###   ⑤ **S5** manifest **完整披露**（`stages`/`skipped`/`notes`/`live_trade`/`artifacts` 齐全，
###      `set_skipped` 登记项必须出现在 `skipped` 里 ⇒ 绝不静默降级）；
###   ⑥ **S6** `--mode` **已废除** ⇒ 退出码 **2** ＋ stderr 迁移提示（`--full`/`--live-trade`/快照制）；
###   ⑦ **S7** `--live-trade` 与 `--dry-run` **互斥** ⇒ 退出码 **2**；
###   ⑧ **S8** `resolve_stage_flags`（`--full` 强制四阶段全开；被覆盖的 skip 登记进 `skipped`）；
###   ⑨ **S9** 执行卡**挂单禁用**标记（观察态带 / 实盘态不带 ⇒ 副作用只由 `--live-trade` 决定）；
###   ⑩ **S10** **变异反证**（S1~S9 每条正向断言都配一条「把行为改坏 ⇒ 必须转红」的变异体
###      ⇒ 闸门有牙；变异体走**运行时替换 / 内存内 AST 改写** ⇒ **不落盘改生产代码**）。
###   🔴 纯内存 ＋ `tempfile` 夹具；CLI 子进程跑在**临时目录**里（产物根/快照根/报告目录/锁文件
###   全部重定向）＋ SMTP 凭据指向不存在的路径 ⇒ **不联网、不读生产数据、不写任何生产目录、
###   绝不可能发信**。
run_stage probe_snapshot_contract "快照契约闸门（目录唯一+自包含+只读底座+不写全局文件+完整披露+--mode已废除+观察/实盘互斥+--full强制全流程+执行卡挂单禁用+变异反证）" \
  "$QL_PY" manu/etf_rotation/probes/probe_snapshot_contract.py

### probe_qlib_provider —— **qlib provider 单一事实源闸门**（新增第 22 个探针阶段）
###   钉死**两链不分叉**：① 路径唯一性（基座 = 个股线 `PROVIDER_URI`；派生 = <root>/cn_data_live；
###   env 三件套覆盖）；② **谁新用谁**规则（派生末日 > 基座 ⇒ 用派生；== / < / 读不到 ⇒ 回基座）；
###   ③ fail-safe（空日历 / 目录缺失 ⇒ 退回基座，**不猜**）；④ `asof` 未覆盖只**告警**不换源；
###   ⑤ 每日链**委托**共享模块（AST + 运行时逐字等价）且路径**对账**无第二个字面量；
###   ⑥ 两条**变异反证**（永远用基座 / 派生不比基座新也用派生 ⇒ 必须失败 ⇒ 闸门有牙）。
###   🔴 纯函数 + 临时目录夹具；**不联网、不读生产数据集、不写生产目录**。
run_stage probe_qlib_provider "qlib provider 单一事实源闸门（路径唯一+谁新用谁+fail-safe+每日链委托+路径对账+变异反证）" \
  "$QL_PY" manu/etf_rotation/probes/probe_qlib_provider.py

### probe_score_smoothing —— **穿透分时间平滑闸门**（新增第 23 个探针阶段）
###   钉死**平滑语义不许漂**：① **脉冲假数据**（前 4 日平稳 + 第 5 日暴涨 3 倍 ⇒
###   平滑分必须显著低于裸分、且不许在首日冲上 Top 1）；② EWMA / 滚动均值**手算**逐位比对；
###   ③ **LOCF 阶梯**（观测点稀疏时按交易日轴前向填充 ⇒ 取最近观测）；
###   ④ 冷启动/缺数据 ⇒ 平滑退化成**单日瞬态分**且打 `WARNING: Fallback to transient score`；
###   ⑤ 稳定性算不出来 ⇒ **None**（不许写 0）；⑥ 非法参数抛**中文**异常；
###   ⑦ 源码静态检查（无 tab / snake_case / 中文 docstring / **禁 fillna(0)**）；
###   ⑧ 三条**变异反证**（不平滑 / 去掉 LOCF / 变异体自身抛错 ⇒ 必须失败 ⇒ 闸门有牙）。
###   🔴 纯函数 + 内存假数据夹具；**不联网、不读生产数据、不写任何目录**。
run_stage probe_score_smoothing "穿透分时间平滑闸门（脉冲压噪+EWMA/滚动手算+LOCF+冷启动回退+稳定性None+变异反证）" \
  "$QL_PY" manu/etf_rotation/probes/probe_score_smoothing.py

### probe_snapshot_lookahead —— **快照结构 + 无未来函数闸门**（新增第 24 个探针阶段）
###   🔴 **断言对象 = 本轮运行生成的快照目录**（不是产物根里的旧件）—— 这是用户架构正本
###   §三 第 2 条「把断言目标改成当前运行生成的快照目录」的直接落地：
###   ① 三处 asof 必须一致（manifest / report.meta / 前视审计 / inputs）；
###   ② 决策前视**只能 0 项或 1 项**且台账逐条自洽（🔴 不许写死「恰 1 项」——
###      asof 推过 W 快照日就是 0 项 ⇒ 写死会误红）；
###   ③ 决策前视**有则唯一且必是** W 成分快照（有效日 > asof），无则 blocking 为空；
###   ④ 非前视决策项的实际消费日 ≤ asof（唯一例外 = **已实测惰性**的成交额兜底）；
###   ⑤ W 快照**若晚于 asof** ⇒ 必须结构性不可修（只有 1 个快照日 + 不可观测理由）；
###   ⑥ 风险披露含 w_snapshot_bias 且带**实测数字**；
###   ⑦ 快照**自包含**（manifest 登记的件都在、无符号链接）+ 行情文件最晚日期 ≤ asof；
###   ⑧ **五条负例自检**（改坏 ⇒ 对应断言必须转红 ⇒ 断言不许空过）。
###   🔴 找不到真实快照 ⇒ 退出码 **2 = 不适用**（**≠ 通过**，不计失败但也不计通过）。
run_stage probe_snapshot_lookahead "快照结构+无未来函数闸门（断言对象=本轮快照；asof相对判定+5条负例）" \
  "$QL_PY" manu/etf_rotation/probes/probe_snapshot_lookahead.py

# =============================================================================
# repro 段：重跑 analyze.py → recommend.py → final_rebalance_plan.py → 比对基线
# =============================================================================
if [ "$SKIP_REPRO" -eq 1 ]; then
  echo "### probe_reproducibility" >> "$LOG"
  echo "SKIPPED probe_reproducibility —— --skip-repro：跳过 analyze.py + recommend.py + final_rebalance_plan.py 重跑段" >> "$LOG"
  echo "EXITCODE probe_reproducibility=SKIPPED" >> "$LOG"
  add_stage "probe_reproducibility" "SKIPPED" "重跑产物与基线逐字节比对（--skip-repro：已跳过）"
else
  ### probe_reproducibility —— 重跑三件套并与基线逐字节比对（最重的阶段，耗时数分钟）
  echo "### probe_reproducibility" >> "$LOG"
  rm -rf "$REPRO_TMP"; mkdir -p "$REPRO_TMP" || { echo "FATAL: 建不了临时目录：$REPRO_TMP" >> "$LOG"; }
  rc_a=1; rc_r=1; rc_f=1; rc_p=1
  HOLD="$("$QL_PY" -c "import sys; sys.path.insert(0,'manu/etf_rotation'); import account; w=account.current_weights(); print(','.join(str(k)+':'+format(v,'.6f') for k,v in sorted(w.items())))" 2>>"$LOG")"
  rc_h=$?
  if [ "$rc_h" -ne 0 ]; then
    echo "FATAL: 取当前持仓权重失败（rc=$rc_h）⇒ repro 段判失败" >> "$LOG"
  else
    PYTHONHASHSEED=0 "$QL_PY" manu/etf_rotation/analyze.py --out-dir "$REPRO_TMP" --asof "$ASOF" \
      --stock-prices "$RUN_ROOT/stock_prices.csv" >> "$LOG" 2>&1
    rc_a=$?
    PYTHONHASHSEED=0 "$QL_PY" manu/etf_rotation/recommend.py --out-dir "$REPRO_TMP" --asof "$ASOF" \
      --pool-mode e_covered --entry-ma-window 20 --exit-ma-window 60 --holdings "$HOLD" >> "$LOG" 2>&1
    rc_r=$?
    PYTHONHASHSEED=0 "$QL_PY" manu/etf_rotation/final_rebalance_plan.py \
      --report "$REPRO_TMP/etf_analysis_report.json" --decision "$RUN_ROOT/tradingagents_decision.md" \
      --out-dir "$REPRO_TMP" >> "$LOG" 2>&1
    rc_f=$?
    PYTHONHASHSEED=0 "$QL_PY" manu/etf_rotation/probes/probe_reproducibility.py \
      --baseline "$RUN_ROOT" --rerun "$REPRO_TMP" --path-map "$REPRO_TMP=$RUN_ROOT" \
      --out "$REPRO_TMP/probe_reproducibility.json" >> "$LOG" 2>&1
    rc_p=$?
  fi
  echo "EXITCODE repro_stage analyze=$rc_a recommend=$rc_r final=$rc_f" >> "$LOG"
  echo "EXITCODE probe_reproducibility=$rc_p" >> "$LOG"
  repro_stage_rc=0
  [ "$rc_a" -eq 0 ] || repro_stage_rc=1
  [ "$rc_r" -eq 0 ] || repro_stage_rc=1
  [ "$rc_f" -eq 0 ] || repro_stage_rc=1
  add_stage "repro_stage" "$repro_stage_rc" "重跑 analyze.py + recommend.py + final_rebalance_plan.py"
  add_stage "probe_reproducibility" "$rc_p" "重跑产物与基线逐字节比对（可复现性）"
fi

# =============================================================================
# 汇总
# =============================================================================
FAILED=0; NA_COUNT=0; SKIPPED_COUNT=0; FAILED_STAGES=""
for i in "${!RC_NAME[@]}"; do
  n="${RC_NAME[$i]}"; v="${RC_VAL[$i]}"
  case "$v" in ''|*[!0-9]*) SKIPPED_COUNT=$((SKIPPED_COUNT + 1)); continue ;; esac
  [ "$v" -eq 0 ] && continue
  if [ "$n" = "probe_verify_guards" ] && [ "$v" -eq 2 ]; then
    NA_COUNT=$((NA_COUNT + 1)); continue
  fi
  # 🔴 2026-11-09：退出码 2 = 「本阶段自判**不适用**」（不是通过）—— 通用化：
  #    除 probe_verify_guards（缺 LLM 决策层）外，probe_snapshot_lookahead
  #    在「本轮没跑出真实快照」时也会返回 2 ⇒ 同样**不计失败，但绝不计入通过**。
  if [ "$v" -eq 2 ]; then
    NA_COUNT=$((NA_COUNT + 1)); continue
  fi
  FAILED=$((FAILED + 1))
  FAILED_STAGES="${FAILED_STAGES}${FAILED_STAGES:+, }$n=$v"
done
EXIT_CODE=0
[ "$FAILED" -eq 0 ] || EXIT_CODE=1

#: 阶段实跑产生的断言通过行数（不含下面「各探针合计行」的回抄）
STAGE_PASS_COUNT="$(grep -c '\[PASS\]' "$LOG" || true)"

{
  echo "=== 汇总 ==="
  echo "仓库根：$REPO_ROOT"
  echo "产物根：$RUN_ROOT"
  echo "闸门断言对象（交付件根）：$ARTIFACT_ROOT"
  echo "解释器：$QL_PY"
  echo "日志：$LOG"
  echo "repro 临时目录：$REPRO_TMP（跑完删除）"
  echo "--- 各阶段 ---"
  for i in "${!RC_NAME[@]}"; do
    n="${RC_NAME[$i]}"; v="${RC_VAL[$i]}"
    if [ "$v" = "2" ]; then
      case "$n" in
        probe_verify_guards)      why="缺 LLM 决策层" ;;
        probe_snapshot_lookahead) why="本轮没跑出真实快照（或快照结构不符）" ;;
        probe_lookahead_snapshot) why="本轮没跑出真实快照（或快照内没报告）" ;;
        probe_canonical_immutability) why="环境不具备（无 canonical 输入 / 找不到 main.py）" ;;
        *)                        why="该阶段自判不适用" ;;
      esac
      printf 'STAGE %s=%s —— %s ｜判定：不适用（%s，不是通过；不计失败）\n' "$n" "$v" "${RC_DESC[$i]}" "$why"
    else
      printf 'STAGE %s=%s —— %s\n' "$n" "$v" "${RC_DESC[$i]}"
    fi
  done
} >> "$LOG"

echo "=== 各探针合计行 ===" >> "$LOG"
grep -E "合计|共 *[0-9]+ *项|PASS.*项" "$LOG" | tail -30 >> "$LOG"

#: 断言 PASS 行数（口径：日志中带断言通过标记的行数；与回抄后的日志一致）
PASS_COUNT="$(grep -c '\[PASS\]' "$LOG" || true)"

{
  echo "--- 总计 ---"
  echo "断言 PASS 行数：$PASS_COUNT（口径：日志中带断言通过标记的行数）"
  echo "阶段实跑产生的 PASS 行数：$STAGE_PASS_COUNT（不含「各探针合计行」回抄）"
  echo "不适用阶段数：$NA_COUNT"
  echo "跳过阶段数：$SKIPPED_COUNT"
  echo "失败阶段数：$FAILED${FAILED_STAGES:+（$FAILED_STAGES）}"
  echo "最终退出码：$EXIT_CODE"
} >> "$LOG"

echo "=== 闸门结论 ==="
echo "退出码：$EXIT_CODE（0=全过 / 1=有阶段失败）｜断言 PASS 行数：$PASS_COUNT｜失败阶段：$FAILED｜不适用：$NA_COUNT｜跳过：$SKIPPED_COUNT"
echo "闸门断言对象（交付件根）：$ARTIFACT_ROOT"
echo "日志：$LOG"
echo "--- 日志尾部（tail -70） ---"
tail -70 "$LOG"
exit "$EXIT_CODE"
