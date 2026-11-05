#!/usr/bin/env bash
# ============================================================
# manu/tools/pull_reports.sh
#   把【服务器】跑出来的 Markdown 报告拉回【本地 host 工程】的 manu/report/。
#
#   为什么需要:
#     流水线在服务器上跑（重活上服务器，铁律二）⇒ 报告天然落在服务器的
#     $REMOTE_ROOT/manu/report/ ⇒ 但用户在 PyCharm 里打开的是 **host 工程**
#     ⇒ 报告必须【拉回 host】才看得到。
#     本机 SSHFS 挂载 /home/manu/mnt/pycharm_project_658d3fc4 是【只读】，
#     能读服务器但**推不回去**；pull 方向则可以直接用它读，不必再走 ssh。
#
#   用法:
#     bash manu/tools/pull_reports.sh              # 拉全部报告
#     bash manu/tools/pull_reports.sh --check      # 只比对 md5，不落盘
#     bash manu/tools/pull_reports.sh --list       # 只列服务器上有什么
#
#   判据: **md5 双侧比对**（与 sync_to_server.sh 同一把尺子）。
#   🔴 本工具只拉【报告】(manu/report/*.md)，不动任何代码。
# ============================================================
set -euo pipefail

SRC_REPO="${SRC_REPO:-/media/manu/1TB-Volume/workspace/qlib}"
SSH_HOST="${SSH_HOST:-huangzhe@192.168.99.40}"
SSH_PORT="${SSH_PORT:-32222}"
REMOTE_ROOT="${REMOTE_ROOT:-/tmp/pycharm_project_658d3fc4}"
# 本机 SSHFS 挂载点（只读镜像）；挂不上就退回 ssh + rsync
SSHFS_MIRROR="${SSHFS_MIRROR:-/home/manu/mnt/pycharm_project_658d3fc4}"

REMOTE_DIR="$REMOTE_ROOT/manu/report"
LOCAL_DIR="$SRC_REPO/manu/report"

CHECK_ONLY=0
LIST_ONLY=0
for arg in "$@"; do
  case "$arg" in
    --check) CHECK_ONLY=1 ;;
    --list)  LIST_ONLY=1 ;;
    *) echo "未知参数: $arg" >&2; exit 2 ;;
  esac
done

echo "== 服务器报告目录 : $SSH_HOST:$SSH_PORT$REMOTE_DIR"
echo "== 本地落点        : $LOCAL_DIR"

REMOTE_READ=""
if [ -d "$SSHFS_MIRROR/manu/report" ]; then
  REMOTE_READ="sshfs"
  SRC_DIR="$SSHFS_MIRROR/manu/report"
  echo "== 通道            : SSHFS 只读镜像（$SRC_DIR）"
else
  REMOTE_READ="ssh"
  SRC_DIR="$REMOTE_DIR"
  echo "== 通道            : ssh（SSHFS 镜像不可用）"
fi

if [ "$LIST_ONLY" = "1" ]; then
  if [ "$REMOTE_READ" = "sshfs" ]; then
    ls -la "$SRC_DIR"
  else
    ssh -F /dev/null -p "$SSH_PORT" "$SSH_HOST" "ls -la '$REMOTE_DIR'"
  fi
  exit 0
fi

remote_list() {
  if [ "$REMOTE_READ" = "sshfs" ]; then
    ( cd "$SRC_DIR" && find . -maxdepth 1 -name '*.md' -type f -printf '%P\n' | sort )
  else
    ssh -F /dev/null -p "$SSH_PORT" "$SSH_HOST" \
      "cd '$REMOTE_DIR' && find . -maxdepth 1 -name '*.md' -type f -printf '%P\n' | sort"
  fi
}
remote_md5() {
  local rel="$1"
  if [ "$REMOTE_READ" = "sshfs" ]; then
    md5sum "$SRC_DIR/$rel" | awk '{print $1}'
  else
    ssh -F /dev/null -p "$SSH_PORT" "$SSH_HOST" "md5sum '$REMOTE_DIR/$rel'" | awk '{print $1}'
  fi
}

FILES="$(remote_list)"
if [ -z "$FILES" ]; then
  echo "❌ 服务器上没有报告（$REMOTE_DIR 为空）" >&2
  exit 1
fi
echo "== 服务器报告      : $(echo "$FILES" | wc -l) 个"

if [ "$CHECK_ONLY" = "1" ]; then
  fail=0
  while IFS= read -r rel; do
    [ -z "$rel" ] && continue
    r="$(remote_md5 "$rel")"
    if [ -f "$LOCAL_DIR/$rel" ]; then
      l="$(md5sum "$LOCAL_DIR/$rel" | awk '{print $1}')"
    else
      l="<本地缺失>"
    fi
    if [ "$r" = "$l" ]; then
      echo "   ✅ $rel  md5=$r"
    else
      echo "   ❌ $rel  服务器=$r  本地=$l"
      fail=1
    fi
  done <<< "$FILES"
  [ "$fail" = "0" ] && echo "✅ 报告两侧 md5 完全一致" || { echo "❌ 报告两侧不一致" >&2; exit 1; }
  exit 0
fi

mkdir -p "$LOCAL_DIR"
while IFS= read -r rel; do
  [ -z "$rel" ] && continue
  if [ "$REMOTE_READ" = "sshfs" ]; then
    cp -f "$SRC_DIR/$rel" "$LOCAL_DIR/$rel.tmp" && mv -f "$LOCAL_DIR/$rel.tmp" "$LOCAL_DIR/$rel"
  else
    ssh -F /dev/null -p "$SSH_PORT" "$SSH_HOST" "cat '$REMOTE_DIR/$rel'" > "$LOCAL_DIR/$rel.tmp"
    mv -f "$LOCAL_DIR/$rel.tmp" "$LOCAL_DIR/$rel"
  fi
done <<< "$FILES"

echo "== md5 双侧比对 =="
fail=0
while IFS= read -r rel; do
  [ -z "$rel" ] && continue
  r="$(remote_md5 "$rel")"
  l="$(md5sum "$LOCAL_DIR/$rel" | awk '{print $1}')"
  if [ "$r" = "$l" ]; then
    echo "   ✅ $rel  md5=$r"
  else
    echo "   ❌ $rel  服务器=$r  本地=$l"
    fail=1
  fi
done <<< "$FILES"
[ "$fail" = "0" ] || { echo "❌ 报告落盘后 md5 不一致" >&2; exit 1; }
echo "✅ 已拉回 host：$LOCAL_DIR"
