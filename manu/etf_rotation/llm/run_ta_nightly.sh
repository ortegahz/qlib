#!/usr/bin/env bash
# TradingAgents 夜间决策运行包装（夜间无人值守 / 受限审批环境用）
#   bash manu/etf_rotation/llm/run_ta_nightly.sh [ticker] [date]
# 需要 api.minimaxi.com 网络访问；产物落在 <run-root>/tradingagents_decision.{md,json}
set -uo pipefail
PROJ="${PROJ_DIR:-/tmp/pycharm_project_658d3fc4}"
LLM_PY="${LLM_PY:-/home/huangzhe/anaconda3/envs/llm/bin/python}"
RUN_ROOT="${RUN_ROOT:-/mnt/data/siping/runs/etf_rotation}"
TICKER="${1:-512800}"
ASOF="${2:-2026-11-03}"
cd "$PROJ" || exit 1
unset http_proxy https_proxy HTTP_PROXY HTTPS_PROXY
export PYTHONPATH=
# 🔴 输出必须自己落盘：受限审批环境里后台进程活不过一次调用，
#    stdout 若只进死会话则事后无法判断跑到哪一步 ⇒ 统一重定向到 run-root。
exec >"$RUN_ROOT/ta_run.log" 2>&1
echo "=== TA run start $(date '+%F %T') ticker=$TICKER asof=$ASOF ==="
"$LLM_PY" manu/etf_rotation/llm/run_tradingagents.py \
  --ticker "$TICKER" --date "$ASOF" --analysts news --run-root "$RUN_ROOT"
echo "TA exit=$?"
echo "=== TA run end $(date '+%F %T') ==="
