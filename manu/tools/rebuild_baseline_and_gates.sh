#!/usr/bin/env bash
# ============================================================
# manu/tools/rebuild_baseline_and_gates.sh
#   服务器端「只重建基线 + 复跑闸门」编排器（**不重跑每日链流水线**）。
#
#   为什么要单独有这个脚本（2026-11-10 实证）：
#     `overnight_snapshot_run.sh` 的 4 步里，**步骤 2（每日链 --full）最贵**（约 1~3 小时）。
#     而 2026-11-10 上午实跑发现：研究链的**解析器**（`llm_final.extract_section`）
#     有真缺陷 ⇒ **步骤 1（基线重建）失败** ⇒ 基线产物与新代码不同源。
#     ⇒ 解析器修好后**只需要**重建基线（步骤 1）＋ 重生成指纹表（步骤 3）＋ 复跑闸门（步骤 4），
#       **步骤 2 的快照不受影响**（它是独立目录、自包含）⇒ 不必重跑那 1~3 小时。
#
#   铁律（与 overnight_snapshot_run.sh 一致）：
#     * **不 commit / 不 push / 不改服务器代码**（只跑工程里的既有脚本）；
#     * **不碰别人的 session**：单核跑、nice 降优先级、不 kill 任何进程；
#     * **每步都留日志 + 退出码** ⇒ 只看一个 summary.md 就知道结果；
#     * 任何一步失败都**不中断整条链** ⇒ 明早至少有结论。
#
#   用法（服务器）：
#     setsid nohup bash manu/tools/rebuild_baseline_and_gates.sh \
#         > <结果根>/_rebuild_20261110/nohup.log 2>&1 &
#
#   环境变量（都有缺省）：
#     PROJ_DIR / ETF_RUN_ROOT / ETF_ROTATION_RESULTS_ROOT / ETF_ROTATION_OUTPUT_ROOT
#     QL_PY / ASOF_BASE / RUN_TAG
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
#: 研究链基线的 asof（**不许动**：文件名/日历下标不变 ⇒ 指纹表登记项集合恒定）
ASOF_BASE="${ASOF_BASE:-2026-11-03}"
RUN_TAG="${RUN_TAG:-$(date +%Y%m%d)}"

LOG_DIR="$RUN/_rebuild_$RUN_TAG"
LOG="$LOG_DIR/rebuild.log"
SUMMARY="$LOG_DIR/summary.md"

mkdir -p "$LOG_DIR" || exit 2
: > "$LOG"

say(){ printf '[%s] %s\n' "$(date '+%F %T')" "$*" | tee -a "$LOG"; }

declare -a STEP_NAME=() STEP_RC=() STEP_LOG=()
record(){ STEP_NAME+=("$1"); STEP_RC+=("$2"); STEP_LOG+=("${3:-—}"); }

write_summary(){
  {
    echo "# 基线重建 + 闸门复跑 汇总 · $(date '+%F %T')"
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
    echo "## 口径声明"
    echo
    echo "- 本脚本**只重建研究链 canonical 基线**并复跑闸门；**不重跑**每日链流水线"
    echo "  （每日链快照自包含、互不影响 ⇒ 上一轮 \`--full\` 的快照仍有效）。"
    echo "- 全程未 commit / 未 push；服务器代码由本地 \`sync_to_server.sh\` rsync 同步。"
  } > "$SUMMARY"
}

trap 'write_summary' EXIT

cd "$PROJ" || { say "FATAL: 进不去 $PROJ"; exit 2; }
say "=== 基线重建 + 闸门复跑启动 === proj=$PROJ run_root=$RUN asof_base=$ASOF_BASE tag=$RUN_TAG"

# ---- 步骤 1：生产基线向前重建（含 TradingAgents 决策；自带备份）----------
say "=== 步骤 1/3：生产基线重建（rebuild_baseline.sh，含 LLM 决策）==="
bash manu/etf_rotation/rebuild_baseline.sh --asof "$ASOF_BASE" --out-dir "$RUN" \
     > "$LOG_DIR/step1_rebuild_baseline.log" 2>&1
RC=$?
record "生产基线重建（含 LLM）" "$RC" "step1_rebuild_baseline.log"
say "   退出码=$RC"

# ---- 步骤 2：指纹表重生成 + --dry-run 复验 -------------------------------
say "=== 步骤 2/3：指纹表重生成 + --dry-run 复验 ==="
"$QL_PY" manu/tools/regen_fingerprint_table.py --artifact-root "$RUN" \
     > "$LOG_DIR/step2_fingerprint.log" 2>&1
RC1=$?
"$QL_PY" manu/tools/regen_fingerprint_table.py --artifact-root "$RUN" --dry-run \
     >> "$LOG_DIR/step2_fingerprint.log" 2>&1
RC2=$?
record "指纹表重生成（重生成 + dry-run 复验）" "$(( RC1 != 0 || RC2 != 0 ))" "step2_fingerprint.log"
say "   重生成退出码=$RC1｜dry-run 复验退出码=$RC2"

# ---- 步骤 3：全量闸门（单核 + nice；不碰别人 session）--------------------
say "=== 步骤 3/3：全量闸门（run_gates.sh --skip-repro）==="
nice -n 10 bash manu/etf_rotation/run_gates.sh --skip-repro \
     --log "$RUN/_gates_rebuild_$RUN_TAG.log" > "$LOG_DIR/step3_gates.log" 2>&1
RC=$?
record "全量闸门（26 阶段）" "$RC" "step3_gates.log / _gates_rebuild_$RUN_TAG.log"
say "   退出码=$RC"

say "=== 结束 === 总结见 $SUMMARY"
exit 0
