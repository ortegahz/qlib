#!/usr/bin/env bash
# manu/etf_rotation/probes/probe_run_lock.sh
#
# 探针：run_pipeline.sh 的「同 out-dir 实例守卫（flock）」+ 「退出码口径」回归。
#
# 背景（2026-11-04 夜两次实测）：
#   · 事故一：升权会话的 setsid nohup 实例活到 21:23，与 21:10 的前台实例
#     **并发写同一 out-dir** ⇒ 第一次的决策文件被覆盖、无法复查。
#     ⇒ 修法：run_pipeline.sh 用 flock 做同 out-dir 互斥（fail-safe：宁可误拦，不误并发写）。
#   · 事故二：--skip-ta 模式下 tradingagents_decision.md 根本不存在，
#     而 `ls` 只要有一个 operand 缺失就返回 2 ⇒ pipefail 把 2 传播成脚本退出码
#     ⇒ 自检明明 20/20 PASS，脚本却 exit 2。
#     ⇒ 修法：末尾 ls 不致命 + 显式 exit 0（真失败走 die ⇒ exit 1）。
#
# 本探针**只跑 scratch 目录**（不碰正式产物目录），不联网、不跑 LLM。
# 用法：bash manu/etf_rotation/probes/probe_run_lock.sh
# 退出码：0 = 全部断言通过；1 = 有断言失败
set -uo pipefail

PROJ="${PROJ_DIR:-/tmp/pycharm_project_658d3fc4}"
RUN_ROOT="${RUN_ROOT:-/mnt/data/siping/runs/etf_rotation}"
QL_PY="${QL_PY:-/home/huangzhe/anaconda3/envs/ql/bin/python}"
STAMP="$(date '+%Y%m%dT%H%M%S')"
SC="$RUN_ROOT/_probe_lock_$STAMP"
OUT_JSON="$RUN_ROOT/probe_run_lock.json"
SELFCHECK_LOG="$RUN_ROOT/probe_run_lock_selfcheck.log"

FAIL=0
declare -a CHECK_ID=() CHECK_NAME=() CHECK_OK=() CHECK_DETAIL=()

record() {  # record <id> <name> <0|1> <detail>
  CHECK_ID+=("$1"); CHECK_NAME+=("$2"); CHECK_OK+=("$3"); CHECK_DETAIL+=("$4")
  if [ "$3" -eq 1 ]; then
    printf '[PASS] %s %s\n        %s\n' "$1" "$2" "$4"
  else
    printf '[FAIL] %s %s\n        %s\n' "$1" "$2" "$4"
    FAIL=$((FAIL + 1))
  fi
}

cleanup() {
  # 留证据：自检日志尾（+ 结论 json 由正文写）；scratch 大文件（1.6MB 报告）不留
  if [ -f "$SC/a1.log" ]; then tail -40 "$SC/a1.log" > "$SELFCHECK_LOG" 2>/dev/null; fi
  rm -rf "$SC"
}
trap cleanup EXIT

echo "=============================================================================="
echo "ETF 轮动线 · 流水线实例守卫 / 退出码探针"
echo "scratch: $SC"
echo "=============================================================================="

# ---- 准备 scratch（只放外部输入，符号链接到正式目录） --------------------
mkdir -p "$SC" || { echo "无法创建 scratch：$SC"; exit 1; }
for f in stock_scores.csv stock_prices.csv \
         etf_rotation_backtest_v4_prefer0.json etf_rotation_backtest_v4_prefer1.json \
         probe_cluster_legacy.json probe_cluster_quality.json; do
  [ -e "$RUN_ROOT/$f" ] || { echo "缺输入：$RUN_ROOT/$f"; exit 1; }
  ln -s "$RUN_ROOT/$f" "$SC/$f"
done

run_pipeline() {  # 用法：run_pipeline <日志文件>
  bash "$PROJ/manu/etf_rotation/run_pipeline.sh" \
    --asof 2026-11-03 --out-dir "$SC" \
    --skip-scores --skip-prices --skip-ta > "$1" 2>&1
}

count_match() {  # count_match <正则> <文件>；grep 无匹配时返回 0 而不是多打一行
  local n
  n="$(grep -c -E "$1" "$2" 2>/dev/null || true)"
  echo "${n:-0}"
}

# ---- A1：--skip-ta 正常跑 ⇒ 退出码 0（不是 2） ----------------------------
LOG="$SC/a1.log"; run_pipeline "$LOG"; RC=$?
if [ "$RC" -eq 0 ]; then
  record A1 "--skip-ta 流水线退出码为 0" 1 \
    "退出码=$RC（修复前是 2：ls 缺 tradingagents_decision.md ⇒ pipefail 传播 2）"
else
  record A1 "--skip-ta 流水线退出码为 0" 0 "退出码=$RC（期望 0）；日志：$LOG"
fi

# ---- A2：自检全 PASS ----------------------------------------------------
N_PASS="$(count_match '^\[PASS\]' "$LOG")"
N_FAIL="$(count_match '^\[FAIL\]' "$LOG")"
if [ "$N_FAIL" -eq 0 ] && [ "$N_PASS" -ge 20 ]; then
  record A2 "--skip-ta 自检全 PASS" 1 "PASS $N_PASS / FAIL $N_FAIL（期望 FAIL=0、PASS≥20）"
else
  record A2 "--skip-ta 自检全 PASS" 0 "PASS $N_PASS / FAIL $N_FAIL（期望 FAIL=0、PASS≥20）"
fi

# ---- A3：正常收尾后锁已释放 ---------------------------------------------
if flock -n "$SC/.pipeline.lock" -c 'exit 0' 2>/dev/null; then
  record A3 "正常收尾后锁已释放" 1 "独立进程能拿到 .pipeline.lock ⇒ 无死锁"
else
  record A3 "正常收尾后锁已释放" 0 "锁仍被占用 ⇒ 有孤儿进程继承了 fd 9"
fi

# ---- A4：并发第二个实例 ⇒ 退出码 3 + 拒绝消息 ---------------------------
( flock 9; sleep 8 ) 9>>"$SC/.pipeline.lock" &
HOLDER=$!
sleep 1
LOG="$SC/a4.log"; run_pipeline "$LOG"; RC=$?
if [ "$RC" -eq 3 ] && grep -q "拒绝并发写" "$LOG"; then
  record A4 "并发实例被拒绝" 1 "退出码=3 且日志含「拒绝并发写」"
else
  record A4 "并发实例被拒绝" 0 "退出码=$RC（期望 3）；日志：$LOG"
fi

# ---- A5：孤儿持锁 ⇒ 仍被拒（fail-safe） ---------------------------------
kill -9 "$HOLDER" 2>/dev/null; wait "$HOLDER" 2>/dev/null; sleep 1
LOG="$SC/a5.log"; run_pipeline "$LOG"; RC=$?
if [ "$RC" -eq 3 ]; then
  record A5 "孤儿持锁时仍然拒绝（fail-safe）" 1 "退出码=3 ⇒ 宁可误拦，也不误并发写"
else
  record A5 "孤儿持锁时仍然拒绝（fail-safe）" 0 "退出码=$RC（期望 3）"
fi

# ---- A6：逃生口（rm 锁文件）后恢复 --------------------------------------
rm -f "$SC/.pipeline.lock"
LOG="$SC/a5b.log"; run_pipeline "$LOG"; RC=$?
if [ "$RC" -eq 0 ]; then
  record A6 "逃生口（rm 锁文件）后恢复" 1 "退出码=0 ⇒ 删锁文件换 inode ⇒ 旧锁失效"
else
  record A6 "逃生口（rm 锁文件）后恢复" 0 "退出码=$RC（期望 0）；日志：$LOG"
fi

# ---- 写 json 结论 ------------------------------------------------------
{
  printf '['
  for i in "${!CHECK_ID[@]}"; do
    [ "$i" -gt 0 ] && printf ','
    if [ "${CHECK_OK[$i]}" -eq 1 ]; then P=true; else P=false; fi
    printf '{"id":"%s","name":"%s","passed":%s,"detail":"%s"}' \
      "${CHECK_ID[$i]}" "${CHECK_NAME[$i]}" "$P" "${CHECK_DETAIL[$i]}"
  done
  printf ']'
} > "$SC/.checks.json"

CHECKS_JSON="$(cat "$SC/.checks.json")" "$QL_PY" - "$OUT_JSON" "$STAMP" "$FAIL" <<'PY'
import json, os, socket, sys
from datetime import datetime

out_path, stamp, n_fail = sys.argv[1], sys.argv[2], int(sys.argv[3])
checks = json.loads(os.environ.get("CHECKS_JSON", "[]"))
payload = {
    "generated_at": datetime.now().isoformat(timespec="seconds"),
    "host": socket.gethostname(),
    "scratch": f"_probe_lock_{stamp}",
    "n_checks": len(checks),
    "n_fail": n_fail,
    "checks": checks,
}
with open(out_path, "w", encoding="utf-8") as fh:
    json.dump(payload, fh, ensure_ascii=False, indent=2)
print(f"已写出 {out_path}")
PY

echo "------------------------------------------------------------------------------"
if [ "$FAIL" -eq 0 ]; then
  echo "合计 6 项：PASS 6 / FAIL 0"
  exit 0
fi
echo "合计 6 项：有 $FAIL 项 FAIL"
exit 1
