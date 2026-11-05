#!/usr/bin/env bash
# manu/etf_rotation/run_gates.sh —— ETF 轮动线「全量闸门」一键复跑脚本
#
# 【是什么】
#   把已跑通的全量闸门固化成一条可复制粘贴的命令：19 个探针阶段 + 1 个可复现性段（repro），
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
#   --run-root PATH   产物根（默认 /mnt/data/siping/runs/etf_rotation）
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
#   * repro 段的临时目录建在 /mnt/data 下（不落 /tmp），跑完必删（trap 兜底）。
#   * repro 段的结论 JSON 写进临时目录（不覆盖产物根里已有的同名文件）。

set -uo pipefail

REPO_ROOT="/tmp/pycharm_project_658d3fc4"
RUN_ROOT="/mnt/data/siping/runs/etf_rotation"
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
    --log)        [ $# -ge 2 ] || die "--log 需要一个路径";       LOG="$2";       shift 2 ;;
    --skip-repro) SKIP_REPRO=1; shift ;;
    -h|--help)    usage; exit 0 ;;
    *) echo "unknown arg: $1" >&2; usage >&2; exit 2 ;;
  esac
done

[ -d "$REPO_ROOT" ] || die "仓库根不存在：$REPO_ROOT"
[ -d "$RUN_ROOT" ]  || die "产物根不存在：$RUN_ROOT"
[ -x "$QL_PY" ]     || die "找不到 ql 解释器：$QL_PY（可用环境变量 QL_PY 覆盖）"
[ -n "$LOG" ] || LOG="$RUN_ROOT/_gates_latest.log"

#: repro 段临时目录：跟基准脚本同名同位置（产物根的同级目录），**必须在 /mnt/data 下**
RUN_PARENT="$(cd "$RUN_ROOT/.." && pwd)" || die "取不到产物根的父目录：$RUN_ROOT"
REPRO_TMP="$RUN_PARENT/_repro_tmp"
case "$REPRO_TMP" in
  /mnt/data/*) : ;;
  *) die "repro 临时目录必须落在 /mnt/data 下（当前：$REPRO_TMP）" ;;
esac

cleanup(){ if [ -n "${REPRO_TMP:-}" ] && [ -d "$REPRO_TMP" ]; then rm -rf "$REPRO_TMP"; fi; return 0; }
trap cleanup EXIT

cd "$REPO_ROOT" || die "进不去仓库根：$REPO_ROOT"
: > "$LOG" || die "日志写不进去：$LOG"

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
# 19 个探针阶段（顺序与基准脚本 _run_gates_ms2.sh 完全一致；M-S5 新增第 15 个 probe_node_basis；
#                       M-S6 新增第 16 个 probe_llm_stage_repro（LLM 阶段可复现闸门）、
#                       第 17 个 check_memory_tables（memory 表格结构检查器，**只读**）、
#                       第 18 个 probe_decision_scenarios（三项分歧组合矩阵闸门）与
#                       第 19 个 probe_node_scenarios（节点日分歧情景重算闸门））
# =============================================================================

### verify_artifacts —— 交付产物完整性/指纹/内部一致性校验
run_stage verify_artifacts "交付产物完整性/指纹/内部一致性校验" \
  "$QL_PY" manu/etf_rotation/verify_artifacts.py --run-root "$RUN_ROOT"

### probe_report_spec —— 分析报告字段规格校验（必填键/类型/披露完整性）
run_stage probe_report_spec "分析报告字段规格校验（必填键/类型/披露完整性）" \
  "$QL_PY" manu/etf_rotation/probes/probe_report_spec.py --report "$RUN_ROOT/etf_analysis_report.json"

### probe_llm_parser —— LLM 决策文本解析器自检（各种输出形态 → 结构化字段）
run_stage probe_llm_parser "LLM 决策文本解析器自检（输出形态 → 结构化字段）" \
  "$QL_PY" manu/etf_rotation/probes/probe_llm_parser.py

### probe_verify_guards —— 语义守卫/可追溯守卫自检（正例 + 变异负例）；rc=2 表示「不适用」
run_stage probe_verify_guards "语义守卫/可追溯守卫自检（正例 + 变异负例）；rc=2 = 不适用" \
  "$QL_PY" manu/etf_rotation/probes/probe_verify_guards.py --run-root "$RUN_ROOT" --project-dir "$REPO_ROOT"

### probe_plan_robustness —— 调仓计划生成器稳健性（输入扰动下的输出稳定性）
run_stage probe_plan_robustness "调仓计划生成器稳健性（输入扰动下的输出稳定性）" \
  "$QL_PY" manu/etf_rotation/probes/probe_plan_robustness.py

### probe_plan_arithmetic —— 调仓计划算术自洽（权重/份额/金额/费用）
run_stage probe_plan_arithmetic "调仓计划算术自洽（权重/份额/金额/费用）" \
  "$QL_PY" manu/etf_rotation/probes/probe_plan_arithmetic.py \
  --plan "$RUN_ROOT/final_rebalance_plan.json" --md "$RUN_ROOT/final_rebalance_plan.md"

### probe_execution_date —— 执行日/信号日/调仓节点的日期口径校验
run_stage probe_execution_date "执行日/信号日/调仓节点的日期口径校验" \
  "$QL_PY" manu/etf_rotation/probes/probe_execution_date.py \
  --plan "$RUN_ROOT/final_rebalance_plan.json" --md "$RUN_ROOT/final_rebalance_plan.md" \
  --report "$RUN_ROOT/etf_analysis_report.json"

### probe_microstructure —— ETF 微观结构口径校验（折溢价/跟踪误差代理/流动性）
run_stage probe_microstructure "ETF 微观结构口径校验（折溢价/跟踪误差代理/流动性）" \
  "$QL_PY" manu/etf_rotation/probes/probe_microstructure.py \
  --report "$RUN_ROOT/etf_analysis_report.json" --md "$RUN_ROOT/etf_analysis_report.md"

### probe_lookahead —— 前视审计（决策前视项台账 + 实际消费日）
run_stage probe_lookahead "前视审计（决策前视项台账 + 实际消费日）" \
  "$QL_PY" manu/etf_rotation/probes/probe_lookahead.py \
  --report "$RUN_ROOT/etf_analysis_report.json" --md "$RUN_ROOT/etf_analysis_report.md"

### probe_data_freshness —— 数据新鲜度校验（行情最新日与滞后披露）
run_stage probe_data_freshness "数据新鲜度校验（行情最新日与滞后披露）" \
  "$QL_PY" manu/etf_rotation/probes/probe_data_freshness.py --run-root "$RUN_ROOT"

### probe_refresh_integrity —— 刷新链路完整性（预检提示命令可直接复制执行）
run_stage probe_refresh_integrity "刷新链路完整性（预检提示命令可直接复制执行）" \
  "$QL_PY" manu/etf_rotation/probes/probe_refresh_integrity.py --run-root "$RUN_ROOT"

### probe_signal_feasibility —— 信号可落地性校验（池/行情/可交易性硬拦项）
run_stage probe_signal_feasibility "信号可落地性校验（池/行情/可交易性硬拦项）" \
  "$QL_PY" manu/etf_rotation/probes/probe_signal_feasibility.py --run-root "$RUN_ROOT"

### probe_node_reprice —— 节点日重算工具链闸门
run_stage probe_node_reprice "节点日重算工具链闸门" \
  "$QL_PY" manu/etf_rotation/probes/probe_node_reprice.py --run-root "$RUN_ROOT"

### probe_node_basis —— 节点日「账户口径双基准」闸门（M-S5 新增，16 项）
run_stage probe_node_basis "节点日账户口径双基准闸门（plan/node 双口径 + 变异反证）" \
  "$QL_PY" manu/etf_rotation/probes/probe_node_basis.py --run-root "$RUN_ROOT"

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
run_stage probe_llm_stage_repro "LLM 阶段可复现闸门（脚本常驻+指纹登记+渲染可复现+多空表逐字在件）" \
  "$QL_PY" manu/etf_rotation/probes/probe_llm_stage_repro.py --run-root "$RUN_ROOT"

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
  echo "解释器：$QL_PY"
  echo "日志：$LOG"
  echo "repro 临时目录：$REPRO_TMP（跑完删除）"
  echo "--- 各阶段 ---"
  for i in "${!RC_NAME[@]}"; do
    n="${RC_NAME[$i]}"; v="${RC_VAL[$i]}"
    if [ "$n" = "probe_verify_guards" ] && [ "$v" = "2" ]; then
      printf 'STAGE %s=%s —— %s ｜判定：不适用（缺 LLM 决策层，不是通过；不计失败）\n' "$n" "$v" "${RC_DESC[$i]}"
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
echo "日志：$LOG"
echo "--- 日志尾部（tail -70） ---"
tail -70 "$LOG"
exit "$EXIT_CODE"
