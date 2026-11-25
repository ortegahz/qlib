#!/usr/bin/env bash
# 🔴 V2 闸门（切分 / embargo / 标签口径 / 红线 / 产物 / V1 冻结 / 目录洁癖）
#
# 用法（服务器）：
#   bash manu/etf_rotation/v2/run_gates_v2.sh --log <日志路径> [--skip-artifacts]
#
# 口径与 V1 的 `run_gates.sh` 对齐：
#   * 每个阶段一行 `EXITCODE <阶段>=<rc>`；
#   * 任一阶段 rc != 0 ⇒ 整体 rc != 0；
#   * **日志里不允许出现以 `[FAIL]` 开头的行**（主机验收脚本按这个口径判定）。
set -uo pipefail

LOG=""
SKIP_ARTIFACTS=0
while [ $# -gt 0 ]; do
  case "$1" in
    --log) LOG="$2"; shift 2 ;;
    --skip-artifacts) SKIP_ARTIFACTS=1; shift ;;
    *) echo "未知参数：$1" >&2; exit 2 ;;
  esac
done

PROJ="${PROJ_DIR:-/tmp/pycharm_project_3b68a403}"
QL_PY="${QL_PY:-/home/huangzhe/anaconda3/envs/ql/bin/python}"
V2_ROOT="$PROJ/manu/etf_rotation/v2"
LOG="${LOG:-/mnt/data/siping/runs/etf_rotation_results/v2/_gates_v2_$(date +%Y%m%dT%H%M%S).log}"
mkdir -p "$(dirname "$LOG")"
: > "$LOG"

export OMP_NUM_THREADS=4 MKL_NUM_THREADS=4 OPENBLAS_NUM_THREADS=4 NUMEXPR_NUM_THREADS=4
export PYTHONHASHSEED=0
if [ "$SKIP_ARTIFACTS" = "1" ]; then
  export ETF_V2_SKIP_ARTIFACT_CHECK=1
fi

cd "$PROJ" || exit 2

FAILN=0
run_stage(){
  local name="$1"; shift
  {
    echo ""
    echo "### $name"
  } >> "$LOG"
  "$@" >> "$LOG" 2>&1
  local rc=$?
  echo "EXITCODE $name=$rc" >> "$LOG"
  if [ "$rc" != "0" ]; then FAILN=$((FAILN+1)); fi
  return $rc
}

echo "=== V2 闸门启动 $(date '+%F %T')｜日志：$LOG ===" >> "$LOG"

run_stage probe_v2_split      "$QL_PY" "$V2_ROOT/probes/probe_v2_split.py"
run_stage probe_v2_redline    "$QL_PY" "$V2_ROOT/probes/probe_v2_redline.py"
run_stage probe_v2_artifacts  "$QL_PY" "$V2_ROOT/probes/probe_v2_artifacts.py"
run_stage pytest_v2           "$QL_PY" -m pytest "$V2_ROOT/tests" -q

# ---- 自检：日志里不许有以 [FAIL] 开头的行 ----
{
  echo ""
  echo "### 自检 · 日志零 [FAIL] 行"
} >> "$LOG"
NFAIL=$(grep -cE '^\[FAIL\]' "$LOG" || true)
NFAIL=${NFAIL:-0}
if [ "$NFAIL" = "0" ]; then
  echo "[PASS] 日志零 [FAIL] 行" >> "$LOG"
else
  echo "[FAIL] 日志出现 $NFAIL 行 [FAIL]" >> "$LOG"
  FAILN=$((FAILN+1))
  grep -nE '^\[FAIL\]' "$LOG" >> "$LOG"
fi

{
  echo ""
  echo "--- 总计 ---"
  echo "失败阶段数：$FAILN"
  echo "最终退出码：$FAILN"
} >> "$LOG"

echo "=== V2 闸门结束 $(date '+%F %T')｜失败阶段数=$FAILN｜日志=$LOG ==="
exit "$FAILN"
