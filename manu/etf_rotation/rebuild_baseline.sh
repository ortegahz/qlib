#!/usr/bin/env bash
# manu/etf_rotation/rebuild_baseline.sh —— 🔴 **生产基线向前重建**（rules.md §十四.93 第 6 条口径）
#
#   用途：当生产产物根处于「半更新态」（研究链是新口径、TA 链是旧口径）时，
#         把整条链**从当前版本重建**一遍，使 sha1 链（报告→context→决策）与
#         LLM 最终裁决层重新自洽。
#
#   铁律口径：
#   * **只许改值，不许增删** —— asof 钉死（默认 2026-11-03）⇒ 节点日/执行日不变、
#     文件名不变（decision_options_20261105.* / decision_scenarios_20261105.*）
#     ⇒ 指纹表 `_ms5_md5.txt` 的 35 条登记项集合恒定。
#   * **先备份**当前产物 + 指纹表 ⇒ 可回滚。
#   * **不许手改指纹表** ⇒ 重生成（见第 5 步）。
#   * 本脚本**只重建产物**，不 commit、不 push、不改仓库代码。
#   * **备份与重建必须同目录** ⇒ `--out-dir` 会**透传**给 run_pipeline.sh（否则备份 A、重建 B）。
#
#   用法（服务器）：
#     bash manu/etf_rotation/rebuild_baseline.sh [--asof 2026-11-03] [--label 20261105]
#                                               [--out-dir <结果根>]
#                                               [--provider-uri DIR]
#
#   退出码：0 = 编排器与两个生成器全过；非 0 = 编排器失败（优先），其次生成器失败。
#
#   依赖：run_pipeline.sh（16 步生产编排器）已在服务器工作副本里。
set -uo pipefail

ASOF="2026-11-03"
LABEL="20261105"
#: 🔴 结果落点根（**唯一事实源** = `manu/etf_rotation/daily/daily_config.py` 的 `ETF_ROTATION_RESULTS_ROOT`）
#:    🔴 `RESULTS_ROOT_DEFAULT` 必须与 `run_pipeline.sh` 里的**同名变量逐字相同**
#:       （两份脚本靠它做「备份目录 == 重建目录」的交叉校验）。
RESULTS_ROOT_DEFAULT="/mnt/data/siping/runs/etf_rotation_results"
RESULTS_ROOT="${ETF_ROTATION_RESULTS_ROOT:-$RESULTS_ROOT_DEFAULT}"
DEFAULT_OUT_DIR="$RESULTS_ROOT"
OUT_DIR="${OUT_DIR:-$DEFAULT_OUT_DIR}"
# 「产物根是否被显式指定」——决定「默认值不一致」时是**报错中止**还是只告警
OUT_DIR_EXPLICIT=0
if [ -n "${OUT_DIR:-}" ] && [ "${OUT_DIR:-}" != "$DEFAULT_OUT_DIR" ]; then
  OUT_DIR_EXPLICIT=1
fi
PROJ="${PROJ_DIR:-/tmp/pycharm_project_658d3fc4}"
QL_PY="${QL_PY:-/home/huangzhe/anaconda3/envs/ql/bin/python}"
#: 🔴 2026-11-09 新增：qlib **provider 覆盖**（默认空 = 上游基座 ⇒ **零回归**）。
#:    显式指定时**原样透传**给 run_pipeline.sh ⇒ 重建用的数据源与每日链一致。
PROVIDER_URI="${ETF_ROTATION_QLIB_PROVIDER_URI:-}"

while [ $# -gt 0 ]; do
  case "$1" in
    --asof)  ASOF="$2"; shift 2 ;;
    --label) LABEL="$2"; shift 2 ;;
    --out-dir) OUT_DIR="$2"; OUT_DIR_EXPLICIT=1; shift 2 ;;
    --provider-uri) PROVIDER_URI="$2"; shift 2 ;;
    -h|--help) sed -n '2,23p' "$0"; exit 0 ;;
    *) echo "unknown arg: $1" >&2; exit 2 ;;
  esac
done

# ---- 0) 前置断言（必须早于任何 mkdir / 备份 / 重建）----------------------
#   🔴 为什么必须前置：下面 `mkdir -p "$BK"`（BK 在 OUT_DIR 里）会把**不存在的 OUT_DIR 顺带创建出来**
#      ⇒ 事后那句「out_dir 不存在」永远不可能触发 ⇒ 必须在任何 mkdir 之前断言。
if [ ! -d "$OUT_DIR" ]; then
  echo "FATAL: 产物根目录不存在：$OUT_DIR" >&2
  echo "       本脚本只重建**已存在的**生产目录（先备份 → 再重建）⇒ 不自动创建。" >&2
  exit 1
fi

#   🔴 run_pipeline.sh 的默认 OUT_DIR 必须与本脚本默认一致：
#      不一致 ⇒ 用户以为「重建的是本脚本默认目录」，内层却按**它自己的**默认目录重建
#      ⇒ 备份与重建落在两个目录（本脚本要防的事故）。
#      用户**显式**指定 --out-dir 时两边都用这一个目录 ⇒ 只告警不拦。
SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" 2>/dev/null && pwd || true)"
PIPE_SH="$SCRIPT_DIR/run_pipeline.sh"
if [ ! -f "$PIPE_SH" ]; then
  echo "FATAL: 找不到 run_pipeline.sh：$PIPE_SH" >&2
  exit 1
fi
#   🔴 2026-11-10：默认 OUT_DIR 改成「落点根 + /runs」派生 ⇒ 不再是**纯字面量**
#      ⇒ 交叉校验改成**逐字比对落点根的默认值字面量**（两份脚本同一个字面量
#      ⇒ 同一个 env 变量解析后必然同路径 ⇒ 「备份目录 == 重建目录」仍然成立）。
#   grep 取值要稳健：允许行首空格；**注释行行首是 # ⇒ 天然不匹配**；
#   只认「字面量路径」（值里含 $ ⇒ 是变量默认值不是字面量 ⇒ 解析不出就中止，不猜）。
PIPE_DEF_ROOT="$(grep -Eo '^[[:space:]]*RESULTS_ROOT_DEFAULT="?[^"[:space:]$]+"?[[:space:]]*$' "$PIPE_SH" 2>/dev/null \
                 | head -1 | sed -E 's/^[[:space:]]*RESULTS_ROOT_DEFAULT="?([^"]+)"?[[:space:]]*$/\1/' || true)"
if [ -z "$PIPE_DEF_ROOT" ]; then
  echo "FATAL: 解析不到 run_pipeline.sh 的结果根默认值（RESULTS_ROOT_DEFAULT）：$PIPE_SH" >&2
  echo "       ⇒ 无法核对「备份目录 == 重建目录」⇒ 中止（宁可不重建，也不错位重建）。" >&2
  exit 1
fi
#   run_pipeline.sh 的 OUT_DIR 必须**由结果根派生**（不许又写一份字面量）
if ! grep -Eq '^[[:space:]]*OUT_DIR="\$RESULTS_ROOT"[[:space:]]*$' "$PIPE_SH"; then
  echo "FATAL: run_pipeline.sh 的 OUT_DIR 不是由结果根派生：$PIPE_SH" >&2
  echo "       ⇒ 无法核对「备份目录 == 重建目录」⇒ 中止。" >&2
  exit 1
fi
if [ "$PIPE_DEF_ROOT" != "$RESULTS_ROOT_DEFAULT" ]; then
  if [ "$OUT_DIR_EXPLICIT" -eq 0 ]; then
    echo "FATAL: run_pipeline.sh 结果根默认值（$PIPE_DEF_ROOT）≠ 本脚本默认（$RESULTS_ROOT_DEFAULT）" >&2
    echo "       ⇒ 默认路径下「备份」与「重建」可能落在两个目录 ⇒ 中止。" >&2
    echo "       处置：显式传 --out-dir <生产目录>（本脚本会把同一个目录透传给 run_pipeline.sh）。" >&2
    exit 1
  fi
  echo "⚠️  run_pipeline.sh 结果根默认值（$PIPE_DEF_ROOT）≠ 本脚本默认（$RESULTS_ROOT_DEFAULT）；" >&2
  echo "    本次用显式指定的 $OUT_DIR（备份与重建同目录 ⇒ 无错位）。" >&2
fi

TS="$(date +%Y%m%dT%H%M%S)"
BK="$OUT_DIR/_backup_preRebuild_${TS}"
LOG="$OUT_DIR/_ms5/_rebuild_${TS}.log"
mkdir -p "$OUT_DIR/_ms5"
say() { printf '[%s] %s\n' "$(date '+%F %T')" "$*" | tee -a "$LOG"; }

say "=== 重建开始 === asof=$ASOF label=$LABEL out_dir=$OUT_DIR"

# ---- 1) 备份（规则 §十四.93 第 6 条：先备份）--------------------------
say "1/5 备份产物 + 指纹表 → $BK"
mkdir -p "$BK"
cd "$OUT_DIR" || { say "FATAL: out_dir 不存在"; exit 1; }
for f in etf_analysis_report.json etf_analysis_report.md \
         ta_research_context.md ta_context_meta.json \
         tradingagents_decision.md tradingagents_decision.json \
         rebalance_recommendation.json rebalance_recommendation.csv \
         final_rebalance_plan.json final_rebalance_plan.md \
         "decision_options_${LABEL}.json" "decision_options_${LABEL}.md" \
         "decision_scenarios_${LABEL}.json" "decision_scenarios_${LABEL}.md" \
         _ms5_md5.txt; do
  if [ -f "$f" ]; then cp -p "$f" "$BK/"; else say "   ⚠️ 缺失（跳过备份）: $f"; fi
done
echo "$BK" > "$OUT_DIR/_ms5/_last_rebuild_backup_dir.txt"
say "   备份完成：$(ls -1 "$BK" | wc -l) 个文件"

# ---- 2) 跑生产编排器（16 步；含 sha1 链自检 + 全部只读闸门）----------
#   * --skip-scores：不重跑 S 层（重活 ≈27min；canonical stock_scores.csv 沿用 ⇒ asof 不漂）
#   * --skip-prices：stock_prices.csv 已存在
#   * **不传 --skip-ta** ⇒ 真跑 TradingAgents（LLM 第二意见）
say "2/5 跑 run_pipeline.sh（16 步，含 LLM 决策）"
cd "$PROJ" || { say "FATAL: 工作目录不存在：$PROJ"; exit 1; }
# 🔴 provider 透传（默认空 ⇒ 基座 ⇒ 零回归；显式指定 ⇒ 与每日链同源）
PIPE_PROVIDER_ARGS=()
[ -n "$PROVIDER_URI" ] && PIPE_PROVIDER_ARGS+=(--provider-uri "$PROVIDER_URI")
[ -n "$PROVIDER_URI" ] && say "   provider 覆盖：$PROVIDER_URI"
bash manu/etf_rotation/run_pipeline.sh --asof "$ASOF" --out-dir "$OUT_DIR" \
     --skip-scores --skip-prices "${PIPE_PROVIDER_ARGS[@]}" \
     2>&1 | tee -a "$LOG"
PIPE_RC="${PIPESTATUS[0]}"
say "   run_pipeline.sh 退出码 = $PIPE_RC"

# ---- 3) 重生成「分歧对照件」（分歧项个数与情景数由产物动态决定；----------
#        幂等；标签固定 ⇒ 文件名不变）------------------------------------
say "3/5 重生成 decision_options / decision_scenarios（--asof-label $LABEL）"
"$QL_PY" manu/etf_rotation/make_decision_options.py \
    --run-root "$OUT_DIR" --out-dir "$OUT_DIR" --asof-label "$LABEL" 2>&1 | tee -a "$LOG"
OPT_RC="${PIPESTATUS[0]}"
say "   make_decision_options 退出码 = $OPT_RC"
"$QL_PY" manu/etf_rotation/make_decision_scenarios.py \
    --run-root "$OUT_DIR" --out-dir "$OUT_DIR" --asof-label "$LABEL" 2>&1 | tee -a "$LOG"
SCN_RC="${PIPESTATUS[0]}"
say "   make_decision_scenarios 退出码 = $SCN_RC"

# ---- 4) 重建后 sha1（与指纹表登记值逐条对照用）-------------------------
say "4/5 重建后产物 sha1"
cd "$OUT_DIR"
for f in etf_analysis_report.json etf_analysis_report.md \
         ta_research_context.md ta_context_meta.json \
         tradingagents_decision.md tradingagents_decision.json \
         rebalance_recommendation.json final_rebalance_plan.json final_rebalance_plan.md \
         "decision_options_${LABEL}.json" "decision_options_${LABEL}.md" \
         "decision_scenarios_${LABEL}.json" "decision_scenarios_${LABEL}.md"; do
  if [ -f "$f" ]; then sha1sum "$f"; else echo "MISSING  $f"; fi
done | tee -a "$LOG"

# ---- 5) 收尾：退出码语义（编排器失败优先，其次生成器）------------------
FINAL_RC=0
FAIL_ITEM=""
if [ "${PIPE_RC:-0}" -ne 0 ]; then
  FINAL_RC="${PIPE_RC:-0}"; FAIL_ITEM="run_pipeline.sh"
elif [ "${OPT_RC:-0}" -ne 0 ]; then
  FINAL_RC="${OPT_RC:-0}"; FAIL_ITEM="make_decision_options.py"
elif [ "${SCN_RC:-0}" -ne 0 ]; then
  FINAL_RC="${SCN_RC:-0}"; FAIL_ITEM="make_decision_scenarios.py"
fi

say "5/5 重建收尾（指纹表重生成与全量闸门另跑）"
if [ "$FINAL_RC" -eq 0 ]; then
  say "   编排器 + 2 个生成器 退出码全为 0 ⇒ 本轮重建通过"
else
  say "   🔴 本轮重建**未通过**：$FAIL_ITEM 退出码 = $FINAL_RC（编排器失败优先，其次生成器）"
fi
say "=== 重建结束 === 日志=$LOG 备份=$BK"
exit "$FINAL_RC"
