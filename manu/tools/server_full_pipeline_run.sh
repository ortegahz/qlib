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

PROJ="${PROJ_DIR:-/tmp/pycharm_project_3b68a403}"
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

#: 🔴 **2026-11-16 真机事故（必须钉在这里，不许依赖调用方的 shell）**：
#:    服务器工程目录 `/tmp/pycharm_project_3b68a403` **无 .git** ⇒ `import qlib` 会退到
#:    `setuptools_scm.get_version()` ⇒ **探测不到版本** ⇒ `LookupError`
#:    ⇒ 阶段 1 的 `verify_provider`（qlib 读回校验）直接炸 ⇒ **整条流水线 rc=1**
#:    （现象：阶段 1 把 11-16 的全市场 bar 都取齐了，最后一步读回校验才挂，白跑 75 分钟）。
#:    ⇒ 判据：`python -c "import qlib"` 在服务器上必须**免环境变量**也能过。
export SETUPTOOLS_SCM_PRETEND_VERSION="${SETUPTOOLS_SCM_PRETEND_VERSION:-0.9.8}"

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
    echo
    echo "## 快照速览（自动追加）"
    echo
    local latest f
    latest="$(ls -1dt "$RUN"/snapshots/*/ 2>/dev/null | head -1)"
    if [ -z "$latest" ]; then
      echo "- （本轮**没有**快照目录）"
    else
      echo "- 快照目录：\`$latest\`"
      echo "- 产物清单："
      for f in "$latest"*; do
        [ -e "$f" ] || continue
        echo "  - \`$(basename "$f")\`"
      done
    fi
  } > "$SUMMARY"

  # 🔴 2026-11-13：把汇总**同时**落到 `reports/` ⇒ 明早**一条命令**就能取到
  #    （以前只在编排日志目录里，明早要自己翻文件）。
  mkdir -p "$RESULTS_ROOT/reports" 2>/dev/null || true
  # 🔴 2026-11-13（独立审查 D-1）：文件名必须带**信号日**（`reports/` 目录既有口径 =
  #    `report_<YYYYMMDD>.md` 里的日期是 **asof**）⇒ 夜跑「运行日 ≠ 信号日」时，
  #    只看文件名**分不清**这份汇总对应哪个信号日。
  cp -f "$SUMMARY" "$RESULTS_ROOT/reports/full_run_${RUN_TAG}_asof${ASOF_PIPE}_summary.md" 2>/dev/null || true
}

trap 'write_summary' EXIT

cd "$PROJ" || { say "FATAL: 进不去 $PROJ"; exit 2; }
say "=== 完整生产链启动 === proj=$PROJ run_root=$RUN asof_pipe=$ASOF_PIPE asof_base=$ASOF_BASE"

# ---- 步骤 1：生产基线向前重建（含 LLM 决策；自带备份）-------------------
# 🔴 2026-11-13：新增 `SKIP_BASELINE=1` 开关（缺省 0 = 行为不变）。
#    理由：每日链对 canonical 根（研究链基线）是**只读**的；而 canonical 根若**已经**
#    用**当前**代码重建过（例：11:22 用修后解析器重建、决策层零漂移），
#    再跑一遍 `rebuild_baseline.sh` 只会**重复调一次 LLM**（20+ 分钟）、
#    并可能把已验证过的基线换成新的漂移版本 ⇒ 允许显式跳过。
if [ "${SKIP_BASELINE:-0}" = "1" ]; then
  say "=== 步骤 1/5：生产基线重建 —— **已显式跳过**（SKIP_BASELINE=1；canonical 根已是当前代码的产物）==="
  record "生产基线重建（含 LLM）" "0" "—（显式跳过：SKIP_BASELINE=1）"
else
  say "=== 步骤 1/5：生产基线重建（rebuild_baseline.sh，含 LLM 决策）==="
  bash manu/etf_rotation/rebuild_baseline.sh --asof "$ASOF_BASE" --out-dir "$RUN" \
       > "$LOG_DIR/step1_rebuild_baseline.log" 2>&1
  RC=$?
  record "生产基线重建（含 LLM）" "$RC" "step1_rebuild_baseline.log"
  say "   退出码=$RC"
fi

# ---- 步骤 1.5：qlib 可用性预检（自愈）--------------------------------------
#: 🔴 **2026-11-16 实测事故**：服务器工程目录里的 **Cython 扩展**
#:    （`qlib/data/_libs/rolling*.so` / `expanding*.so`）与 `qlib/_version.py`
#:    被一次 `--delete` 同步误删（host 是 git 检出 ⇒ 没有这些构建产物）
#:    ⇒ `import qlib` 炸 ⇒ **阶段 2（S 层重算）才暴露** ⇒ 白跑取数 + 75 分钟预期。
#:    ⇒ 预检必须**在开跑前**做，且**能自愈**（重建扩展是确定性动作）。
say "=== 步骤 1.5/5：qlib 可用性预检（import qlib ＋ Cython 扩展）==="
_qlib_ok(){
  ( cd "$PROJ" && SETUPTOOLS_SCM_PRETEND_VERSION="$SETUPTOOLS_SCM_PRETEND_VERSION" \
      "$QL_PY" -c "import qlib; from qlib.data._libs import rolling, expanding" ) >/dev/null 2>&1
}
if _qlib_ok; then
  say "   ✅ qlib 可用（$( cd "$PROJ" && "$QL_PY" -c 'import qlib; print(qlib.__version__)' 2>/dev/null )）"
else
  say "   ⚠️ qlib 不可用（多半是 Cython 扩展 / _version.py 缺失）⇒ 现场重建"
  if "$QL_PY" -m pip install --no-deps --quiet cython >>"$LOG" 2>&1 \
     && ( cd "$PROJ" && SETUPTOOLS_SCM_PRETEND_VERSION="$SETUPTOOLS_SCM_PRETEND_VERSION" \
          "$QL_PY" setup.py build_ext --inplace ) >>"$LOG" 2>&1 \
     && _qlib_ok; then
    say "   ✅ 重建成功（qlib 可用）"
  else
    say "   🔴 **重建失败** ⇒ qlib 不可用 ⇒ 本轮无法开跑（详见 $LOG）"
    record "qlib 预检（重建自愈）" "2" "—（qlib 不可用且重建失败）"
    write_summary
    exit 2
  fi
fi

#: 🔴 **2026-11-16 实测事故**：服务器工程目录里的 **`TradingAgents/` 子项目整个被删**
#:    （`run_tradingagents.py --ta-dir $PROJ/TradingAgents` ⇒ `sys.path.insert` 指向空目录
#:    ⇒ `from tradingagents.default_config import DEFAULT_CONFIG` ⇒ `ModuleNotFoundError`
#:    ⇒ 阶段 4（多智能体辩论）炸 ⇒ 前面 23 分钟白跑）。
#:    ⇒ 同样**必须开跑前预检**（这次只需 1 秒；缺了则要 23 分钟才暴露）。
say "=== 步骤 1.6/5：TradingAgents 可用性预检（ta-dir ＋ import）==="
#: 🔴 2026-11-20：PyCharm 工程映射变更 ⇒ **TradingAgents 已是独立工程**，
#:    不再嵌在 qlib 工程目录里（`$PROJ/TradingAgents` 已不存在）⇒ 默认值改为独立工程根。
TA_DIR="${ETF_ROTATION_TA_DIR:-/tmp/pycharm_project_59a2a4f0}"
TA_PY="${ETF_ROTATION_LLM_PY:-/home/huangzhe/anaconda3/envs/llm/bin/python}"
if [ -d "$TA_DIR" ] && ( cd "$PROJ" && "$TA_PY" -c "import sys; sys.path.insert(0, '$TA_DIR'); from tradingagents.default_config import DEFAULT_CONFIG" ) >/dev/null 2>&1; then
  say "   ✅ TradingAgents 可用（$TA_DIR）"
else
  say "   🔴 **TradingAgents 不可用**（$TA_DIR 缺失或 import 失败）⇒ 阶段 4 必炸 ⇒ 本轮无法开跑"
  say "      修法：把 host 的 TradingAgents/ 同步到 $TA_DIR（含 .env），再重跑"
  record "TradingAgents 预检" "2" "—（$TA_DIR 缺失或 import 失败）"
  write_summary
  exit 2
fi

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
# 🔴 2026-11-13：新增 `INITIAL_WAIT_SECONDS`（缺省 0 = 不等，保持旧行为）。
#    东财按 IP 限流时原文明确写着「**等待期间不要重复调用行情接口，否则会延长限流**」
#    ⇒ 允许把「**静默等待**」放在**第一次尝试之前**（2026-11-13 下午实测：
#    第一次尝试 15:45 立刻撞限流 → 退避 600s 后第二次 16:03 仍全挂
#    ⇒ 限流窗口比 10 分钟长 ⇒ 必须「先静默、后开跑」）。
INITIAL_WAIT_SECONDS="${INITIAL_WAIT_SECONDS:-0}"
if [ "$INITIAL_WAIT_SECONDS" -gt 0 ]; then
  say "   ⏳ **限流静默等待 ${INITIAL_WAIT_SECONDS}s**（等待期**不碰行情接口**）…"
  sleep "$INITIAL_WAIT_SECONDS"
fi

# 🔴 2026-11-13：新增 `RETRY_MAX_ATTEMPTS`（缺省 1 = 旧行为：只重试一次）。
#    `RETRY_BACKOFF_SECONDS` 缺省 600s（沿用）。
#    🔴 判定必须**只看本次尝试**的日志（多次尝试写在同一个 pipeline.log 里
#       ⇒ 全文 grep 会把**上一次**的限流痕迹当成本次的 ⇒ 误判成「该退避」）。
RETRY_MAX_ATTEMPTS="${RETRY_MAX_ATTEMPTS:-1}"
RETRY_BACKOFF_SECONDS="${RETRY_BACKOFF_SECONDS:-600}"
#: 🔴 2026-11-16：首轮时 `step2_pipeline/pipeline.log` **还不存在**
#:    ⇒ `wc -l < 文件` 的**重定向**失败（shell 自己报错，`2>/dev/null` 管不到它）
#:    ⇒ 日志里常驻一条 "No such file or directory" 假告警 ⇒ 先把文件**建出来**。
PIPELINE_LOG="$LOG_DIR/step2_pipeline/pipeline.log"
mkdir -p "$(dirname "$PIPELINE_LOG")"
[ -f "$PIPELINE_LOG" ] || : > "$PIPELINE_LOG"
LOG_MARK="$(wc -l < "$PIPELINE_LOG" 2>/dev/null || echo 0)"
ATTEMPT=1
run_full "第 ${ATTEMPT} 尝试：--full"
RC=$?
while [ "$RC" -ne 0 ] && [ "$ATTEMPT" -lt "$RETRY_MAX_ATTEMPTS" ]; do
  # 🔴 2026-11-13（独立审查 D-2）：**退避只对限流生效**。
  #    限流的恢复要时间（等待期重复调用只会延长限流）⇒ 该等；
  #    但若是 LLM/解析侧的真错 ⇒ 等再久也只是再调一次 LLM、还多产一份快照 ⇒ 直接判失败。
  if tail -n "+$((LOG_MARK+1))" "$LOG_DIR/step2_pipeline/pipeline.log" 2>/dev/null \
       | grep -q "Push2RateLimitError"; then
    ATTEMPT=$((ATTEMPT+1))
    LOG_MARK="$(wc -l < "$LOG_DIR/step2_pipeline/pipeline.log" 2>/dev/null || echo 0)"
    say "   ⚠️ 第 $((ATTEMPT-1)) 次 --full 未过（**限流**）⇒ 等 ${RETRY_BACKOFF_SECONDS}s 后重试（第 ${ATTEMPT}/${RETRY_MAX_ATTEMPTS} 次；**仍用 --full**：行情缺口是根因，不能降级；等待期不碰行情接口）"
    sleep "$RETRY_BACKOFF_SECONDS"
    run_full "第 ${ATTEMPT} 尝试：--full（限流退避后重试）"
    RC=$?
  else
    say "   ⚠️ 第 ${ATTEMPT} 次 --full 未过（**非限流**）⇒ **不再重跑**（真错重跑只会再调一次 LLM）；详见 step2_pipeline/pipeline.log"
    break
  fi
done
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
# 🔴 2026-11-19（`rules.md §十四.127`）：阶段数**不许手写** ⇒ 从单一事实源
#    `run_gates.sh` 的 `run_stage` 调用数**动态派生**（读不到就写「缺」，不许猜、不许写死）。
GATES_STAGES="$(grep -c '^run_stage ' manu/etf_rotation/run_gates.sh 2>/dev/null || true)"
[ -n "$GATES_STAGES" ] || GATES_STAGES="缺"
record "全量闸门（${GATES_STAGES} 阶段）" "$RC" "step4_gates.log / _gates_full_$RUN_TAG.log"
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
#: 🔴 **2026-11-16 实测事故**：并发跑时，本步骤把**另一个活实例**的 `_work/` 删了
#:    ⇒ 那个实例的 ak 子进程落盘失败（`Cannot save file into a non-existent directory`）
#:    ⇒ 整条链 rc=1。
#:    ⇒ 守卫：**流水线实例锁**（`daily_config.LOCK_FILE` = `$RUN_ROOT/.lock`，内核 flock）
#:    **被持有** ⇒ 说明还有活实例在用 `_work/` ⇒ **一律不许清 `_work`**。
#:    （本步骤跑在步骤 2 之后 ⇒ 自己这一轮早退出 ⇒ 锁空闲 ⇒ 正常清理不受影响。）
#: 🔴 2026-11-19 真缺陷修复：这里原来写的是 ``$RUN/.lock``（= ``<RESULTS_ROOT>/.lock``），
#:    而每日链的**实例锁**实际在 ``<RESULTS_ROOT>/daily/.lock``（见 ``daily_config.LOCK_FILE``）
#:    ⇒ 守卫**恒判空闲** ⇒ 并发跑时会把**另一个活实例正在用的 ``_work/``** 删掉
#:    ⇒ 复现 2026-11-16 那次事故（``ak`` 子进程落盘失败 → 整条链 rc=1）。
#:    ⇒ 现在**按每日链的真实路径**取（与 ``daily_config`` 同口径，支持环境变量覆盖）。
DAILY_RUN_ROOT="${ETF_ROTATION_RUN_ROOT:-$RESULTS_ROOT/daily}"
INSTANCE_LOCK="${ETF_ROTATION_LOCK_FILE:-$DAILY_RUN_ROOT/.lock}"
SKIP_WORK_CLEAN=0
if flock -n "$INSTANCE_LOCK" -c true 2>/dev/null; then
  say "   ℹ️ 流水线实例锁空闲（$INSTANCE_LOCK）⇒ 可清理 _work"
else
  say "   ⚠️ 流水线实例锁**被持有**（$INSTANCE_LOCK）⇒ 还有活实例在用 _work ⇒ **跳过 _work 清理**"
  SKIP_WORK_CLEAN=1
fi
if [ "$SKIP_WORK_CLEAN" -eq 0 ]; then
  for _t in "$WORK_ROOT/"* ; do
    [ -e "$_t" ] || continue
    if cleanup_project_tmp "$_t"; then
      CLEANED=$((CLEANED+1))
    else
      say "   ⚠️ 跳过（不在本项目落点根下）：$_t"
    fi
  done
fi
#: 本项目的编排锁（只删自己那个 ⇒ 不影响别人的进程）
cleanup_project_tmp "$RUN/.pipeline.lock" && CLEANED=$((CLEANED+1))
cleanup_project_tmp "$RUN/.pipeline.lock.info" && CLEANED=$((CLEANED+1))
say "   已清理本项目临时/中间产物：$CLEANED 项（**只删本项目根下的路径**；未 kill 任何进程）"

#: 5b. 本轮产物**留在服务器结果根**（不再拷回 host）：
say "   本轮产物留在：$RESULTS_ROOT（快照：$RUN/snapshots/ ｜ 编排日志：$LOG_DIR）"
record "产物留在结果根 + 清理临时产物" 0 "step5_stay_on_server.log"

say "=== 完整生产链结束 === 总结见 $SUMMARY"
exit 0
