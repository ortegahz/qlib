#!/usr/bin/env bash
# ============================================================
# manu/tools/pull_from_server.sh
#   把【服务器工程】的 manu/ 拉回【本地 host 工程】，并做 md5 双侧比对。
#   这是 sync_to_server.sh 的**反向**通道（sync_to_server.sh 是 host→server 单向推送）。
#
#   用法:
#     bash manu/tools/pull_from_server.sh                      # 拉回 manu/ 全目录
#     bash manu/tools/pull_from_server.sh manu/etf_rotation     # 只拉指定子路径(可多个)
#     bash manu/tools/pull_from_server.sh --dry-run             # 只看会发生什么, 不落盘
#     bash manu/tools/pull_from_server.sh --check               # 只做 md5 双侧比对, 不拉
#     bash manu/tools/pull_from_server.sh --no-backup           # 跳过备份(不建议)
#
#   安全设计:
#   1. 覆盖前**必定**把 host 侧同名文件备份到 /tmp/ 下的时间戳目录（host 临时件放 /tmp）。
#   2. 拉回后自动 md5 双侧比对，逐文件给出 OK / DIFF 结论。
#   3. **绝不** git commit / git push / git checkout —— 回灌只动工作树，历史由人工裁决。
#   4. 服务器侧多出来的文件（host 没有的）默认**也会**拉回（这是回灌的目的）；
#      若要保守，用 --no-new 才跳过。
#   5. 目标根路径可覆盖：SRC_REPO / REMOTE_ROOT / SSH_HOST / SSH_PORT。
# ============================================================
set -euo pipefail

#: 🔴 2026-11-20：本地工程盘路径已变（旧 `/media/manu/1TB-Volume/workspace/qlib` **不再存在**）
#:    ⇒ 默认值改为**从脚本自身位置反推仓库根**（本文件在 `<repo>/manu/tools/` ⇒ 上溯两级到 `<repo>`）
#:    ⇒ 挂载点/盘符再变也不用改脚本；仍可用 env `SRC_REPO` 显式覆盖。
_SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
SRC_REPO="${SRC_REPO:-$(cd "$_SCRIPT_DIR/../.." && pwd)}"
SSH_HOST="${SSH_HOST:-huangzhe@192.168.99.40}"
SSH_PORT="${SSH_PORT:-32222}"
REMOTE_ROOT="${REMOTE_ROOT:-/tmp/pycharm_project_3b68a403}"
SSH_BASE=(-F /dev/null -p "$SSH_PORT" -o ConnectTimeout=15 -o ServerAliveInterval=30 -o ServerAliveCountMax=6)
RSYNC_RSH="ssh -F /dev/null -p $SSH_PORT"

DRY_RUN=0; CHECK_ONLY=0; NO_BACKUP=0; NO_NEW=0
PATHS=()
for arg in "$@"; do
  case "$arg" in
    --dry-run)   DRY_RUN=1 ;;
    --check)     CHECK_ONLY=1 ;;
    --no-backup) NO_BACKUP=1 ;;
    --no-new)    NO_NEW=1 ;;
    --*) echo "未知参数: $arg" >&2; exit 2 ;;
    *) PATHS+=("$arg") ;;
  esac
done
[ ${#PATHS[@]} -eq 0 ] && PATHS=("manu")

echo "== 源(server) = $REMOTE_ROOT   宿(host) = $SRC_REPO =="
for p in "${PATHS[@]}"; do echo "   路径: $p"; done

# ---------- 1) 服务器侧文件清单 + md5 ----------
LIST=$(mktemp); trap 'rm -f "$LIST"' EXIT
for p in "${PATHS[@]}"; do
  ssh "${SSH_BASE[@]}" "$SSH_HOST" "cd '$REMOTE_ROOT' && find '$p' -type f -not -path '*/__pycache__/*' -not -name '*.pyc' 2>/dev/null | sed 's|^\./||'" >> "$LIST"
done
sort -u -o "$LIST" "$LIST"
NTOTAL=$(wc -l < "$LIST")
echo "== 服务器侧待回灌文件数: $NTOTAL =="

# ---------- 2) 差异分析 ----------
CHANGED=0; NEW=0; SAME=0
declare -a DIFF_LIST=() NEW_LIST=()
# 服务器侧 md5 清单：一次 ssh 批量取回（逐文件 ssh 会慢到不可用）
RMD5=$(mktemp); trap 'rm -f "$LIST" "$RMD5"' EXIT
while IFS= read -r rel; do
  [ -n "$rel" ] || continue
  printf '%s\0' "$rel"
done < "$LIST" | ssh "${SSH_BASE[@]}" "$SSH_HOST" "cd '$REMOTE_ROOT' && xargs -0 md5sum 2>/dev/null" > "$RMD5" || true

while IFS= read -r rel; do
  [ -n "$rel" ] || continue
  hostf="$SRC_REPO/$rel"
  if [ ! -f "$hostf" ]; then
    NEW=$((NEW+1)); NEW_LIST+=("$rel"); continue
  fi
  rm_md5=$(awk -v k="$rel" '$2==k || $2=="./"k {print $1; exit}' "$RMD5")
  host_md5=$(md5sum "$hostf" | awk '{print $1}')
  if [ -z "$rm_md5" ]; then rm_md5="__MISSING__"; fi
  if [ "$rm_md5" = "$host_md5" ]; then SAME=$((SAME+1)); else CHANGED=$((CHANGED+1)); DIFF_LIST+=("$rel"); fi
done < "$LIST"
echo "   相同: $SAME   内容不同: $CHANGED   host 侧不存在(新增): $NEW =="

if [ ${#DIFF_LIST[@]} -gt 0 ]; then
  echo "-- 内容不同的文件（回灌会覆盖）--"
  printf '   %s\n' "${DIFF_LIST[@]}" | head -60
fi
if [ ${#NEW_LIST[@]} -gt 0 ]; then
  echo "-- host 侧不存在的新增文件（回灌会新建）--"
  printf '   %s\n' "${NEW_LIST[@]}" | head -60
fi

if [ "$CHECK_ONLY" -eq 1 ]; then echo "== --check 模式：只比对，不落盘 =="; exit 0; fi
if [ "$DRY_RUN" -eq 1 ]; then echo "== --dry-run 模式：不落盘 =="; exit 0; fi

# ---------- 3) 备份 host 侧将被覆盖的文件 ----------
if [ "$NO_BACKUP" -eq 0 ] && [ ${#DIFF_LIST[@]} -gt 0 ]; then
  BK="/tmp/pull_backups_$(date +%Y%m%d-%H%M%S)"
  mkdir -p "$BK"
  for rel in "${DIFF_LIST[@]}"; do
    mkdir -p "$BK/$(dirname "$rel")"
    cp -p "$SRC_REPO/$rel" "$BK/$rel"
  done
  echo "== 已备份 host 侧 $([ ${#DIFF_LIST[@]} -gt 0 ] && echo ${#DIFF_LIST[@]}) 个将被覆盖的文件到 $BK =="
fi

# ---------- 4) rsync 回灌 ----------
for p in "${PATHS[@]}"; do
  EXTRA=""
  [ "$NO_NEW" -eq 1 ] && EXTRA="--ignore-existing"
  echo "== rsync 回灌: $p =="
  # 🔴 2026-11-18 修（路径 bug）：源写作 `$REMOTE_ROOT/$p`（**无**尾部斜杠）而
  #    目标是 `$SRC_REPO/` ⇒ rsync 的语义是「把**目录本身**放进目标目录」
  #    ⇒ 拉 `manu/etf_rotation` 实际落到 **`$SRC_REPO/etf_rotation/`**
  #    （仓库根的野生目录！）而**不是** `$SRC_REPO/manu/etf_rotation/`
  #    ⇒ 回灌完 md5 双侧比对必然全 DIFF（比对的却是另一条路径）。
  #    修法：源加尾部斜杠（= 只同步**内容**），目标 = `$SRC_REPO/$p/`。
  #    另加 --checksum：免得「大小相同 + host mtime 更新」时被快速检查跳过。
  rsync -az --checksum --info=stats1 $EXTRA \
    -e "$RSYNC_RSH" \
    --exclude='__pycache__/' --exclude='*.pyc' \
    "$SSH_HOST:$REMOTE_ROOT/$p/" "$SRC_REPO/$p/" 2>&1 | tail -4
done

# ---------- 5) 回灌后 md5 双侧比对 ----------
echo "== 回灌后 md5 双侧比对 =="
BAD=0; CHECKED=0
while IFS= read -r rel; do
  [ -n "$rel" ] || continue
  hostf="$SRC_REPO/$rel"
  [ -f "$hostf" ] || continue
  rm_md5=$(awk -v k="$rel" '$2==k || $2=="./"k {print $1; exit}' "$RMD5")
  host_md5=$(md5sum "$hostf" | awk '{print $1}')
  CHECKED=$((CHECKED+1))
  if [ "$rm_md5" != "$host_md5" ]; then echo "   [DIFF] $rel  server=$rm_md5 host=$host_md5"; BAD=$((BAD+1)); fi
done < "$LIST"
echo "   已比对 $CHECKED 个文件，不一致 $BAD 个"
echo "== 完成（未做任何 git 操作）=="
exit $((BAD>0))
