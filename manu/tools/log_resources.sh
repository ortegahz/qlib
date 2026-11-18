#!/usr/bin/env bash
# 🔴 资源读数记录器（>5 分钟任务开跑前必须调用一次）
# 用法： bash manu/tools/log_resources.sh "<阶段标签>" [追加文件]
set -uo pipefail
TAG="${1:-untagged}"
LOG="${2:-/mnt/data/siping/runs/etf_rotation_results/codex_overnight/RESOURCE_LOG.md}"
mkdir -p "$(dirname "$LOG")"
{
  echo ""
  echo "## ${TAG}"
  echo ""
  echo "- 时间：$(date '+%F %T')"
  echo "- 线程钉：OMP/MKL/OPENBLAS/NUMEXPR=4"
  echo ""
  echo '```'
  echo "\$ free -m"
  free -m
  echo ""
  echo "\$ uptime"
  uptime
  echo ""
  echo "\$ nproc"
  nproc
  echo ""
  echo "\$ iostat -x 1 2"
  iostat -x 1 2 2>/dev/null || echo "(iostat 不可用)"
  echo ""
  echo "\$ nvidia-smi"
  nvidia-smi 2>/dev/null || echo "(nvidia-smi 在本容器不可用)"
  echo ""
  echo "\$ ps -eLf | wc -l"
  ps -eLf 2>/dev/null | wc -l
  echo '```'
} >> "$LOG"
echo "[OK] 资源读数已写入 $LOG （阶段：$TAG）"
