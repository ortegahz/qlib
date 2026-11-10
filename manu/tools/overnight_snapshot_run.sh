#!/usr/bin/env bash
# ============================================================
# manu/tools/overnight_snapshot_run.sh
#   服务器端「通宵自跑」编排器：host 关机 / SSH 断线后**独立跑完**剩余全部工作。
#
#   为什么要有这个脚本（2026-11-09 实证）：
#     host 18:00 关机 ⇒ 本地会话结束 ⇒ 任何「我在这里盯着跑」的长活都会中断。
#     ⇒ 剩下的活必须**整体搬到服务器**，用 setsid+nohup 脱离 SSH，跑完自己落总结。
#
#   铁律：
#     * **不 commit / 不 push / 不改服务器代码**（只跑工程里的既有脚本）；
#     * **不碰别人的 session**：单核跑、nice 降优先级、不 kill 任何进程、
#       不写别人的目录（只写本任务自己的产物根与 logs）；
#     * **每步都留日志 + 退出码** ⇒ 明早只看一个 summary.md 就知道结果；
#     * 任何一步失败都**不中断整条链**（降级重试 / 继续下一步）⇒ 明早至少有结论。
#
#   用法（服务器）：
#     setsid nohup bash manu/tools/overnight_snapshot_run.sh \
#         > <结果根>/_overnight_20261110/nohup.log 2>&1 &
#
#   环境变量（都有缺省）：
#     PROJ_DIR / ETF_RUN_ROOT / ETF_ROTATION_RESULTS_ROOT / ETF_ROTATION_OUTPUT_ROOT
#     QL_PY / ASOF_PIPE / ASOF_BASE / RUN_TAG
# ============================================================
set -uo pipefail

PROJ="${PROJ_DIR:-/tmp/pycharm_project_658d3fc4}"
#: 🔴 **结果落点根**（单一事实源 = `manu/etf_rotation/daily/daily_config.py` 的
#:    `ETF_ROTATION_RESULTS_ROOT`）⇒ 产物 / 快照 / 报告 / 日志**留在服务器**，
#:    全部落在这棵树下（**不再拷回 host**）。
#:    `ETF_RUN_ROOT` 覆盖**保留**（既有调用方不破）。
RESULTS_ROOT="${ETF_ROTATION_RESULTS_ROOT:-/mnt/data/siping/runs/etf_rotation_results}"
RUN="${ETF_RUN_ROOT:-$RESULTS_ROOT}"
QL_PY="${QL_PY:-/home/huangzhe/anaconda3/envs/ql/bin/python}"
RUN_TAG="${RUN_TAG:-$(date +%Y%m%d)}"   #: 本轮标签（默认今天）⇒ 每次跑都留独立目录
LOG_DIR="$RUN/_overnight_$RUN_TAG"
LOG="$LOG_DIR/overnight.log"
SUMMARY="$LOG_DIR/summary.md"
#: 统一流水线的信号日（最新可用交易日；与今天 15:16 那次 Mode C 实跑同口径）
ASOF_PIPE="${ASOF_PIPE:-2026-11-09}"
#: 研究链基线的 asof（**不许动**：文件名/日历下标不变 ⇒ 指纹表登记项集合恒定）
ASOF_BASE="${ASOF_BASE:-2026-11-03}"

mkdir -p "$LOG_DIR" || exit 2
: > "$LOG"

say(){ printf '[%s] %s\n' "$(date '+%F %T')" "$*" | tee -a "$LOG"; }

declare -a STEP_NAME=() STEP_RC=() STEP_LOG=()
record(){ STEP_NAME+=("$1"); STEP_RC+=("$2"); STEP_LOG+=("${3:-—}"); }

write_summary(){
  {
    echo "# 通宵自跑汇总 · $(date '+%F %T')"
    echo
    echo "| # | 步骤 | 退出码 | 结论 | 日志 |"
    echo "| --- | --- | --- | --- | --- |"
    local i rc verdict
    for i in "${!STEP_NAME[@]}"; do
      rc="${STEP_RC[$i]}"
      if [ "$rc" = "0" ]; then verdict="✅ 通过"; else verdict="🔴 失败（详见日志）"; fi
      echo "| $((i+1)) | ${STEP_NAME[$i]} | $rc | $verdict | ${STEP_LOG[$i]} |"
    done
    echo
    echo "## 关键产物"
    echo
    echo "- 统一流水线快照根：\`$RUN/snapshots/\`（每个子目录 = 一次不可变快照，manifest 在 \`snapshot_manifest.json\`）"
    echo "- 交付面报告：\`$RESULTS_ROOT/reports/\`（文件名带**信号日**，如 \`report_<YYYYMMDD>.md\`）"
    echo "- 编排器日志：\`$LOG\`"
    echo "- 指纹表：\`$RUN/_ms5_md5.txt\`"
    echo "- 全量闸门日志：\`$RUN/_gates_overnight_$RUN_TAG.log\`"
    echo
    echo "## 口径声明"
    echo
    echo "- 本轮是**观察态**（未开 \`--live-trade\`）：执行卡**结构性挂单禁用**、**不发信**、**不下单**。"
    echo "- 基线重建（步骤 1）走的是**既有** \`rebuild_baseline.sh\`（自带备份 + 只许改值不改文件名）。"
    echo "- 全程未 commit / 未 push；服务器代码由本地 `sync_to_server.sh` rsync 同步（服务器侧**未手工改代码**）。"
  } > "$SUMMARY"
}

trap 'write_summary' EXIT

cd "$PROJ" || { say "FATAL: 进不去 $PROJ"; exit 2; }
say "=== 通宵自跑启动 === proj=$PROJ run_root=$RUN asof_pipe=$ASOF_PIPE asof_base=$ASOF_BASE"

# ---- 步骤 1：生产基线向前重建（含 TradingAgents 决策；自带备份）----------
say "=== 步骤 1/4：生产基线重建（rebuild_baseline.sh，含 LLM 决策）==="
bash manu/etf_rotation/rebuild_baseline.sh --asof "$ASOF_BASE" --out-dir "$RUN" \
     > "$LOG_DIR/step1_rebuild_baseline.log" 2>&1
RC=$?
record "生产基线重建（含 LLM）" "$RC" "step1_rebuild_baseline.log"
say "   退出码=$RC"

# ---- 步骤 2：统一流水线真实跑（观察态 + 全流程 + LLM）--------------------
say "=== 步骤 2/4：统一流水线真实跑（asof=$ASOF_PIPE，观察态，LLM 开）==="
mkdir -p "$LOG_DIR/step2_pipeline"
run_pipe(){
  local desc="$1"; shift
  say "   → $desc"
  "$QL_PY" manu/etf_rotation/daily/main.py --asof "$ASOF_PIPE" "$@" \
      >> "$LOG_DIR/step2_pipeline/pipeline.log" 2>&1
  local rc=$?
  say "   退出码=$rc"
  return $rc
}
run_pipe "第 1 尝试：--full（取数 + 换库 + S 层 + LLM）" --full
RC=$?
if [ "$RC" -ne 0 ]; then
  say "   ⚠️ --full 未过 ⇒ 降级（跳过取数/换库，用现有数据；保留 S 层 + LLM）"
  run_pipe "第 2 尝试：--skip-fetch --no-update-qlib-bin" \
      --skip-fetch --no-update-qlib-bin
  RC=$?
fi
if [ "$RC" -ne 0 ]; then
  say "   ⚠️ 仍未过 ⇒ 再降级（不重跑 S 层，沿用快照底座；保留 LLM）"
  run_pipe "第 3 尝试：--skip-fetch --no-update-qlib-bin --no-refresh-scores" \
      --skip-fetch --no-update-qlib-bin --no-refresh-scores
  RC=$?
fi
record "统一流水线真实跑（含 LLM）" "$RC" "step2_pipeline/pipeline.log"

# ---- 步骤 3：指纹表重生成 + --dry-run 复验 -------------------------------
say "=== 步骤 3/4：指纹表重生成 + --dry-run 复验 ==="
"$QL_PY" manu/tools/regen_fingerprint_table.py --artifact-root "$RUN" \
     > "$LOG_DIR/step3_fingerprint.log" 2>&1
RC1=$?
"$QL_PY" manu/tools/regen_fingerprint_table.py --artifact-root "$RUN" --dry-run \
     >> "$LOG_DIR/step3_fingerprint.log" 2>&1
RC2=$?
record "指纹表重生成（重生成 + dry-run 复验）" "$(( RC1 != 0 || RC2 != 0 ))" "step3_fingerprint.log"
say "   重生成退出码=$RC1｜dry-run 复验退出码=$RC2"

# ---- 步骤 4：全量闸门（单核 + nice；不碰别人 session）--------------------
say "=== 步骤 4/4：全量闸门（run_gates.sh --skip-repro）==="
nice -n 10 bash manu/etf_rotation/run_gates.sh --skip-repro \
     --log "$RUN/_gates_overnight_$RUN_TAG.log" > "$LOG_DIR/step4_gates.log" 2>&1
RC=$?
record "全量闸门（26 阶段）" "$RC" "step4_gates.log / _gates_overnight_$RUN_TAG.log"
say "   退出码=$RC"

say "=== 通宵自跑结束 === 总结见 $SUMMARY"
exit 0
