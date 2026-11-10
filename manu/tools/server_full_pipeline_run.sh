#!/usr/bin/env bash
# ============================================================
# manu/tools/server_full_pipeline_run.sh
#   服务器端「一轮完整生产链」编排器（2026-11-10 起）。
#
#   与 overnight_snapshot_run.sh 的关键区别（**都是 2026-11-10 真 bug 换来的**）：
#     * 步骤 2 **只跑 --full**，**不降级**到 --skip-fetch：
#       行情缺口是**根因**问题（`drop_unfinalized_bars` 把 asof 当日的
#       **已收盘** bar 当成盘中数据丢掉 ⇒ 底座永远差一天）
#       ⇒ 降级重试只会拿**同一份缺行情的底座**再撞一次 fail-closed（纯烧 CPU）。
#       修好根因后重跑即可；真失败时重试的仍是 --full（换一次网络运气）。
#     * 每步独立日志 + 退出码；**任何一步失败都不中断整条链** ⇒ 明早只看 summary.md。
#
#   铁律：
#     * **不 commit / 不 push / 不改服务器代码**（只跑工程里的既有脚本）；
#     * **不碰别人的 session**：单核 + nice 降优先级、**不 kill 任何进程**、
#       不写别人的目录；
#     * 观察态：不开 --live-trade ⇒ 执行卡挂单禁用、不发信、不下单。
#
#   用法（服务器）：
#     setsid nohup bash manu/tools/server_full_pipeline_run.sh \
#         > <结果根>/_full_20261110/nohup.log 2>&1 &
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
#: 🔴 **数据集落点根**（只放数据集 + 临时工作区 `_work/`；与 `daily_config.OUTPUT_ROOT` 同口径）
OUTPUT_ROOT="${ETF_ROTATION_OUTPUT_ROOT:-/mnt/data/siping/datasets/manu/etf_rotation}"
WORK_ROOT="$OUTPUT_ROOT/_work"
QL_PY="${QL_PY:-/home/huangzhe/anaconda3/envs/ql/bin/python}"
RUN_TAG="${RUN_TAG:-$(date +%Y%m%d)}"
LOG_DIR="$RUN/_full_$RUN_TAG"
LOG="$LOG_DIR/full.log"
SUMMARY="$LOG_DIR/summary.md"
#: 统一流水线的信号日（最新可用交易日）
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
    echo "# 完整生产链汇总 · $(date '+%F %T')"
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
    echo "- 统一流水线快照根：\`$RUN/snapshots/\`（每个子目录 = 一次不可变快照）"
    echo "- 交付面报告：\`$RESULTS_ROOT/reports/\`（文件名带**信号日**，如 \`report_<YYYYMMDD>.md\`）"
    echo "- 编排器日志：\`$LOG\`"
    echo "- 指纹表：\`$RUN/_ms5_md5.txt\`"
    echo "- 全量闸门日志：\`$RUN/_gates_full_$RUN_TAG.log\`"
    echo
    echo "## 口径声明"
    echo
    echo "- 本轮是**观察态**（未开 \`--live-trade\`）：执行卡**结构性挂单禁用**、**不发信**、**不下单**。"
    echo "- 全程未 commit / 未 push；服务器代码由本地 \`sync_to_server.sh\` rsync 同步。"
  } > "$SUMMARY"
}

trap 'write_summary' EXIT

cd "$PROJ" || { say "FATAL: 进不去 $PROJ"; exit 2; }
say "=== 完整生产链启动 === proj=$PROJ run_root=$RUN asof_pipe=$ASOF_PIPE asof_base=$ASOF_BASE"

# ---- 步骤 1：生产基线向前重建（含 LLM 决策；自带备份）-------------------
say "=== 步骤 1/5：生产基线重建（rebuild_baseline.sh，含 LLM 决策）==="
bash manu/etf_rotation/rebuild_baseline.sh --asof "$ASOF_BASE" --out-dir "$RUN" \
     > "$LOG_DIR/step1_rebuild_baseline.log" 2>&1
RC=$?
record "生产基线重建（含 LLM）" "$RC" "step1_rebuild_baseline.log"
say "   退出码=$RC"

# ---- 步骤 2：统一流水线真实跑（--full：取数 + 换库 + S 层 + LLM）----------
say "=== 步骤 2/5：统一流水线真实跑（--full，asof=$ASOF_PIPE，观察态，LLM 开）==="
mkdir -p "$LOG_DIR/step2_pipeline"
run_full(){
  say "   → $1"
  nice -n 10 "$QL_PY" manu/etf_rotation/daily/main.py --asof "$ASOF_PIPE" --full \
      >> "$LOG_DIR/step2_pipeline/pipeline.log" 2>&1
  local rc=$?
  say "   退出码=$rc"
  return $rc
}
run_full "第 1 尝试：--full"
RC=$?
if [ "$RC" -ne 0 ]; then
  say "   ⚠️ 第 1 次 --full 未过 ⇒ 重试一次（**仍用 --full**：行情缺口是根因，不能降级）"
  run_full "第 2 尝试：--full（重试）"
  RC=$?
fi
record "统一流水线真实跑（--full，含 LLM）" "$RC" "step2_pipeline/pipeline.log"

# ---- 步骤 3：指纹表重生成 + --dry-run 复验 -------------------------------
say "=== 步骤 3/5：指纹表重生成 + --dry-run 复验 ==="
"$QL_PY" manu/tools/regen_fingerprint_table.py --artifact-root "$RUN" \
     > "$LOG_DIR/step3_fingerprint.log" 2>&1
RC1=$?
"$QL_PY" manu/tools/regen_fingerprint_table.py --artifact-root "$RUN" --dry-run \
     >> "$LOG_DIR/step3_fingerprint.log" 2>&1
RC2=$?
record "指纹表重生成（重生成 + dry-run 复验）" "$(( RC1 != 0 || RC2 != 0 ))" "step3_fingerprint.log"
say "   重生成退出码=$RC1｜dry-run 复验退出码=$RC2"

# ---- 步骤 4：全量闸门（单核 + nice；不碰别人 session）--------------------
say "=== 步骤 4/5：全量闸门（run_gates.sh --skip-repro）==="
nice -n 10 bash manu/etf_rotation/run_gates.sh --skip-repro \
     --log "$RUN/_gates_full_$RUN_TAG.log" > "$LOG_DIR/step4_gates.log" 2>&1
RC=$?
record "全量闸门（26 阶段）" "$RC" "step4_gates.log / _gates_full_$RUN_TAG.log"
say "   退出码=$RC"

# ---- 步骤 5：产物留在结果根 + 清理本项目临时/中间产物 ----------------------
#   🔴 用户口径（2026-11-10 变更）：产物 / 快照 / 报告 / 日志**留在服务器**的结果根
#      `$RESULTS_ROOT` 之下 ⇒ **不再拷回 host**（本步骤已删掉「拷回 host + 取回命令」）。
#   🔴 共享训练机 ⇒ 清理**只许删本项目的路径**（临时工作区 + 本项目编排锁），
#      **绝不 kill / pkill 任何进程**（别人的 session 不许动）。
say "=== 步骤 5/5：产物留在结果根 + 清理本项目临时/中间产物 ==="

#: 5a. 清理**临时/中间产物**（只清本项目的路径）：
#:     🔴 守卫三重：① 目标必须在本项目落点根（临时工作区 / 结果根）之下
#:                   ② 路径不得含 `..`
#:                   ③ **绝不** kill / pkill 任何进程（共享机 ⇒ 别人的 session 不许动）
cleanup_project_tmp(){
  local target="$1"
  case "$target" in
    "$WORK_ROOT"/*|"$RESULTS_ROOT"/*) : ;;
    *) return 1 ;;
  esac
  case "$target" in
    *'..'*) return 1 ;;
  esac
  [ -e "$target" ] || return 0
  rm -rf "$target"
  return 0
}
CLEANED=0
for _t in "$WORK_ROOT/"* ; do
  [ -e "$_t" ] || continue
  if cleanup_project_tmp "$_t"; then
    CLEANED=$((CLEANED+1))
  else
    say "   ⚠️ 跳过（不在本项目落点根下）：$_t"
  fi
done
#: 本项目的编排锁（只删自己那个 ⇒ 不影响别人的进程）
cleanup_project_tmp "$RUN/.pipeline.lock" && CLEANED=$((CLEANED+1))
cleanup_project_tmp "$RUN/.pipeline.lock.info" && CLEANED=$((CLEANED+1))
say "   已清理本项目临时/中间产物：$CLEANED 项（**只删本项目根下的路径**；未 kill 任何进程）"

#: 5b. 本轮产物**留在服务器结果根**（不再拷回 host）：
say "   本轮产物留在：$RESULTS_ROOT（快照：$RUN/snapshots/ ｜ 编排日志：$LOG_DIR）"
record "产物留在结果根 + 清理临时产物" 0 "step5_stay_on_server.log"

say "=== 完整生产链结束 === 总结见 $SUMMARY"
exit 0
