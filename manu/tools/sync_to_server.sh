#!/usr/bin/env bash
# ============================================================
# manu/tools/sync_to_server.sh
#   把【本地 host 工程】的 manu/ 同步到【服务器工程目录】，并做 md5 双侧比对。
#
#   用法:
#     bash manu/tools/sync_to_server.sh              # 同步 manu/ 全目录
#     bash manu/tools/sync_to_server.sh manu/memory   # 只同步指定子路径(可多个)
#     bash manu/tools/sync_to_server.sh --dry-run    # 只看会发生什么,不落盘
#     bash manu/tools/sync_to_server.sh --check      # 只做 md5 双侧比对,不同步
#
#   为什么不用 SSHFS 镜像:
#     本机 SSHFS 挂载 /home/manu/mnt/pycharm_project_658d3fc4 是【只读 ro】(实测),
#     写不进去 => 它只能【读服务器】,不能【推服务器】。
#     推送通道 = 本脚本的 rsync over ssh。
#
#   为什么不用 PyCharm 自动上传:
#     PyCharm 的 SFTP 自动上传(deployment.xml, autoUpload=Always)在【PyCharm 里改文件】时生效,
#     但 Agent 在【命令行】改的文件 PyCharm 不感知 => 仍需本脚本兜底。
#     二者不冲突: PyCharm 改的会先自动传, 本脚本再补齐 Agent 改的。
# ============================================================
set -euo pipefail

# ---------- 环境（本工程实测值）----------
SRC_REPO="${SRC_REPO:-/media/manu/1TB-Volume/workspace/qlib}"
SSH_HOST="${SSH_HOST:-huangzhe@192.168.99.40}"
SSH_PORT="${SSH_PORT:-32222}"     # 服务器 sshd 在 32222;22 端口是关闭的
REMOTE_ROOT="${REMOTE_ROOT:-/tmp/pycharm_project_658d3fc4}"
# 本机 /etc/ssh/ssh_config.d/20-systemd-ssh-proxy.conf 权限不对,
# 直接跑 ssh 会报 "Bad owner or permissions" => 必须 -F /dev/null 绕过系统 ssh_config。
SSH_BASE=(-F /dev/null -p "$SSH_PORT" -o ConnectTimeout=15 -o ServerAliveInterval=30 -o ServerAliveCountMax=6)
RSYNC_RSH="ssh -F /dev/null -p $SSH_PORT"

DRY_RUN=0
CHECK_ONLY=0
PATHS=()

for arg in "$@"; do
  case "$arg" in
    --dry-run) DRY_RUN=1 ;;
    --check)   CHECK_ONLY=1 ;;
    --*)       echo "未知参数: $arg" >&2; exit 2 ;;
    *)         PATHS+=("$arg") ;;
  esac
done
[ ${#PATHS[@]} -eq 0 ] && PATHS=("manu")

echo "== 源仓库 : $SRC_REPO"
echo "== 服务器 : $SSH_HOST:$SSH_PORT$REMOTE_ROOT"
echo "== 同步路径: ${PATHS[*]}"

# ---------- 前置：源路径必须存在 ----------
for p in "${PATHS[@]}"; do
  if [ ! -e "$SRC_REPO/$p" ]; then
    echo "❌ 本地不存在: $SRC_REPO/$p" >&2
    exit 1
  fi
done

# ---------- 排除项 ----------
EXCLUDES=(--exclude='__pycache__/' --exclude='*.pyc' --exclude='.pytest_cache/'
          --exclude='.ipynb_checkpoints/' --exclude='*.swp' --exclude='.DS_Store')

# ---------- md5 清单（本地侧） ----------
md5_local() {
  # 用与 rsync 相同的排除规则算，保证两侧样本集一致
  local rel="$1"
  ( cd "$SRC_REPO" && find "$rel" -type f \
      ! -path '*/__pycache__/*' ! -name '*.pyc' ! -path '*/.pytest_cache/*' \
      ! -path '*/.ipynb_checkpoints/*' ! -name '*.swp' ! -name '.DS_Store' \
      -print0 | LC_ALL=C sort -z | xargs -0 -r md5sum )
}

# ---------- 同步 ----------
if [ "$CHECK_ONLY" -eq 0 ] && [ "$DRY_RUN" -eq 0 ]; then
  echo "== [1/3] rsync 推送 =="
  for p in "${PATHS[@]}"; do
    rsync -az --rsh="$RSYNC_RSH" "${EXCLUDES[@]}" \
      "$SRC_REPO/$p" "$SSH_HOST:$REMOTE_ROOT/$(dirname "$p")/"
  done
  echo "   推送完成"
else
  echo "== [1/3] 跳过推送$( [ "$DRY_RUN" -eq 1 ] && echo '（dry-run）' )$( [ "$CHECK_ONLY" -eq 1 ] && echo '（check-only）' )=="
  if [ "$DRY_RUN" -eq 1 ]; then
    for p in "${PATHS[@]}"; do
      echo "--- 将发生的变化: $p ---"
      rsync -azn --itemize-changes --rsh="$RSYNC_RSH" "${EXCLUDES[@]}" \
        "$SRC_REPO/$p" "$SSH_HOST:$REMOTE_ROOT/$(dirname "$p")/"
    done
  fi
fi

# ---------- md5 双侧比对（铁律：同步判据只能靠 md5 双侧比对） ----------
echo "== [2/3] md5 双侧比对 =="
FAIL=0
for p in "${PATHS[@]}"; do
  local_sum=$(md5_local "$p")
  remote_sum=$(ssh "${SSH_BASE[@]}" "$SSH_HOST" "cd '$REMOTE_ROOT' && find '$p' -type f \
      ! -path '*/__pycache__/*' ! -name '*.pyc' ! -path '*/.pytest_cache/*' \
      ! -path '*/.ipynb_checkpoints/*' ! -name '*.swp' ! -name '.DS_Store' \
      -print0 | LC_ALL=C sort -z | xargs -0 -r md5sum" 2>/dev/null | grep -vE 'WARNING|store now|openssh.com|hostfile_replace|update_known')

  n_local=$(echo "$local_sum" | grep -c . || true)
  n_remote=$(echo "$remote_sum" | grep -c . || true)

  if [ "$n_local" -eq 0 ]; then
    echo "   ⚠️  $p : 本地无匹配文件，跳过"
    continue
  fi

  if [ "$local_sum" = "$remote_sum" ]; then
    echo "   ✅ $p : $n_local 个文件两侧 md5 完全一致"
  else
    echo "   ❌ $p : 不一致（本地 $n_local 个 / 服务器 $n_remote 个）"
    diff <(echo "$local_sum") <(echo "$remote_sum") | head -30 || true
    FAIL=1
  fi
done

# ---------- 结论 ----------
echo "== [3/3] 结论 =="
if [ "$FAIL" -eq 0 ]; then
  echo "✅ 已同步：本地与服务器字节一致"
else
  echo "❌ 存在不一致，请检查上面的 diff"
fi
exit $FAIL
