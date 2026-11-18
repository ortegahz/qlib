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
# 🔴 2026-11-11 增补（A7~A9）：**每日链**的单实例锁（`daily/main.py::_RunLock`）
#    也要能治理「陈旧锁」—— 进程被 kill（SIGTERM/SIGKILL/OOM/断电）后，
#    锁文件会残留 ⇒ 后续跑批要么被误拦、要么看不出「是谁在跑」。
#    ⇒ 锁文件自带**持有者身份**（pid / host / 启动时刻 / 进程启动时刻），
#      ① 持锁进程已死 ⇒ 判**陈旧锁** ⇒ 接管 + WARNING「清理了陈旧锁」；
#      ② 持锁进程还活着 / 内核锁还被占 ⇒ **一律拒**（宁可误拦，也不误并发写）。
#    🔴 这三条用**每日链自己的锁路径**（scratch 内），**不碰**正式 `daily/.lock`。
#
# 本探针**只跑 scratch 目录**（不碰正式产物目录），不联网、不跑 LLM。
# 用法：bash manu/etf_rotation/probes/probe_run_lock.sh
# 退出码：0 = 全部断言通过；1 = 有断言失败
set -uo pipefail

PROJ="${PROJ_DIR:-/tmp/pycharm_project_658d3fc4}"
#: 🔴 **结果落点根**（单一事实源 = `manu/etf_rotation/daily/daily_config.py` 的
#:    `ETF_ROTATION_RESULTS_ROOT`）⇒ 产物根 = 结果根
RESULTS_ROOT="${ETF_ROTATION_RESULTS_ROOT:-/mnt/data/siping/runs/etf_rotation_results}"
RUN_ROOT="${RUN_ROOT:-$RESULTS_ROOT}"
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

# ---- A7：每日链 · 陈旧锁（持锁进程已死）⇒ 接管 + WARNING ------------------
# 🔴 造一个「被 kill 掉的持锁者」：起一个进程再 kill -9，留下它的 pid（已死）。
DAILY_LOCK_DIR="$SC/daily_lock"
mkdir -p "$DAILY_LOCK_DIR"
DAILY_LOCK="$DAILY_LOCK_DIR/.lock"

sleep 30 & DEAD_PID=$!
kill -9 "$DEAD_PID" 2>/dev/null; wait "$DEAD_PID" 2>/dev/null

#: 🔴 2026-11-11 增补：start_ticks 三种夹具模式（**PID 复用判定**要靠它）
#:    · `auto`  = 目标 pid 的**真实** /proc starttime（⇒ 判定 = 同一个进程 ⇒ alive）
#:    · `wrong` = 真实 starttime **+1**（⇒ 模拟「PID 被复用」⇒ 必须判 dead）
#:    · 其它    = 直接写死该数值（旧写法，start_ticks=1）
write_lock_record() {  # write_lock_record <锁文件> <pid> <state> [ticks 模式] [host 覆盖]
  "$QL_PY" - "$1" "$2" "$3" "${4:-1}" "${5:-}" <<'PY'
import json, socket, sys
from pathlib import Path
path, pid, state = Path(sys.argv[1]), int(sys.argv[2]), sys.argv[3]
mode = sys.argv[4] if len(sys.argv) > 4 and sys.argv[4] else "1"
host = sys.argv[5] if len(sys.argv) > 5 and sys.argv[5] else socket.gethostname()


def _proc_start_ticks(pid: int):
    """`/proc/<pid>/stat` 的 starttime（与 `daily/main.py::_proc_start_ticks` 同口径）。"""
    try:
        raw = Path(f"/proc/{pid}/stat").read_text(encoding="utf-8", errors="replace")
    except OSError:
        return None
    cut = raw.rfind(")")
    if cut < 0:
        return None
    fields = raw[cut + 2:].split()
    if len(fields) < 20:
        return None
    try:
        return int(fields[19])
    except ValueError:
        return None


if mode in ("auto", "wrong"):
    ticks = _proc_start_ticks(pid)
    if ticks is None:
        raise SystemExit(f"夹具不适用：读不到 pid={pid} 的 start_ticks（模式={mode}）")
    start_ticks = ticks if mode == "auto" else ticks + 1   # wrong = 故意错开 ⇒ 模拟 PID 复用
else:
    start_ticks = int(mode)
path.write_text(json.dumps({
    "pid": pid,
    "host": host,
    "started_at": "2026-11-11T00:00:00+08:00",
    "start_ticks": start_ticks,
    "script": "probe_run_lock.sh",
    "state": state,
}, ensure_ascii=False) + "\n", encoding="utf-8")
PY
}

#: 🔴 **被测实现目录**（默认 = 正式 `daily/` 目录）。
#:    负例用**变异体副本**（见 `build_mutant`）⇒ 断言对「被断言的分支」真的敏感。
IMPL_DIR="$PROJ/manu/etf_rotation/daily"

daily_lock_try() {  # daily_lock_try <锁文件> [实现目录]：退出码 0 = 拿到锁 / 3 = 被拒
  PROJ="$PROJ" IMPL_DIR="${2:-$IMPL_DIR}" "$QL_PY" - "$1" <<'PY'
import logging, os, sys
from pathlib import Path
logging.basicConfig(level=logging.INFO, format="%(levelname)s | %(name)s | %(message)s",
                    stream=sys.stdout)
sys.path.insert(0, os.environ["IMPL_DIR"])
from main import LockError, _RunLock
try:
    with _RunLock(Path(sys.argv[1])):
        print("ACQUIRED")
except LockError as exc:
    print(f"REJECTED: {exc}")
    raise SystemExit(3)
PY
}

write_lock_record "$DAILY_LOCK" "$DEAD_PID" running
LOG="$SC/a7.log"; daily_lock_try "$DAILY_LOCK" > "$LOG" 2>&1; RC=$?
if [ "$RC" -eq 0 ] && grep -q "清理了陈旧锁" "$LOG"; then
  record A7 "每日链 · 陈旧锁（持锁进程已死）可接管" 1 \
    "拿到锁（rc=0）且日志含「清理了陈旧锁」⇒ 残留锁不再永久误拦"
else
  record A7 "每日链 · 陈旧锁（持锁进程已死）可接管" 0 \
    "退出码=$RC（期望 0）；日志：$LOG"
fi

# ---- A8：每日链 · 活着的持锁者 ⇒ 仍被拒（fail-safe） ----------------------
sleep 30 & LIVE_PID=$!
#: 🔴 `exec sleep 8`（**不是** `sleep 8`）：持锁进程必须**就是 `$!` 本身** ⇒ `kill -9` 它
#:    ⇒ 内核锁**立刻**释放。（写成 `sleep 8` 时它是**子进程** ⇒ kill 掉父进程后它仍持着
#:    fd 9 ⇒ 锁还在 ⇒ A10~A12 会在 flock 那一步就被拒 ⇒ 压根走不到被断言的判定分支。）
#:    （A4/A5 的 `.pipeline.lock` 持锁者**故意**保留孤儿行为 —— A5 就在测它，别改。）
( flock 9; exec sleep 8 ) 9>>"$DAILY_LOCK" &
HOLDER2=$!
sleep 1
write_lock_record "$DAILY_LOCK" "$LIVE_PID" running
LOG="$SC/a8.log"; daily_lock_try "$DAILY_LOCK" > "$LOG" 2>&1; RC=$?
kill -9 "$HOLDER2" 2>/dev/null; wait "$HOLDER2" 2>/dev/null
kill -9 "$LIVE_PID" 2>/dev/null; wait "$LIVE_PID" 2>/dev/null
if [ "$RC" -eq 3 ]; then
  record A8 "每日链 · 活着的持锁者被拒（fail-safe）" 1 \
    "退出码=3 ⇒ 宁可误拦，也不误并发写"
else
  record A8 "每日链 · 活着的持锁者被拒（fail-safe）" 0 \
    "退出码=$RC（期望 3）；日志：$LOG"
fi

# ---- A9：记录说「持锁者已死」但内核锁仍被占 ⇒ 仍被拒（记录不能推翻内核） --
( flock 9; exec sleep 8 ) 9>>"$DAILY_LOCK" &   # 🔴 同 A8：kill 后内核锁必须**立刻**释放
HOLDER3=$!
sleep 1
write_lock_record "$DAILY_LOCK" "$DEAD_PID" running
LOG="$SC/a9.log"; daily_lock_try "$DAILY_LOCK" > "$LOG" 2>&1; RC=$?
kill -9 "$HOLDER3" 2>/dev/null; wait "$HOLDER3" 2>/dev/null
if [ "$RC" -eq 3 ]; then
  record A9 "每日链 · 记录说已死但内核锁被占 ⇒ 仍被拒" 1 \
    "退出码=3 ⇒ 内核锁说了算，记录不能推翻它"
else
  record A9 "每日链 · 记录说已死但内核锁被占 ⇒ 仍被拒" 0 \
    "退出码=$RC（期望 3）；日志：$LOG"
fi

# ============================================================================
# 🔴 2026-11-11 增补（A10~A12 + 负例）：`_RunLock._classify_holder` 的**三条判定分支**
#
# 为什么必须补：审计实测 ⇒ `__enter__` 是「**先 flock，抢不到就拒**」
#   ⇒ 老的 A8（活锁）/ A9（记录说已死但内核锁被占）**都在 flock 这一步就被拒**
#   ⇒ 永远走不到 `_classify_holder` ⇒ 那三条判定分支实际**零覆盖**。
# 本段三条用例都**先释放内核锁**（不占 flock）⇒ 一定走到判定分支：
#   🔴 「释放」必须是真的：A8/A9 的持锁进程用 `exec sleep`（否则 kill 父进程后
#      孤儿 `sleep` 仍持 fd 9 ⇒ 锁还在 ⇒ 这三条会在 flock 就被拒 ⇒ 断言空转），
#      并由 `require_lock_free` 闸门在跑之前**实测**内核锁已空。
#   · **A10** 内核锁空 + 记录说持有者**还活着** ⇒ 必须**拒**（不许接管）
#   · **A11** 记录里的 `host` 是**别的机器** ⇒ 本机验不了 ⇒ 必须**拒**（保守）
#   · **A12** `pid` 活着但 `start_ticks` **对不上**（PID 复用）⇒ 必须判 **dead ⇒ 接管**
#
# 🔴 **负例纪律（rules.md §十四.52 / §十四.68：新增守卫必须自带负例）**：
#   负例必须**真的改到被断言的对象上** ⇒ 这里改的是 **`daily/main.py` 的副本**
#   （scratch 内的变异体；**正式文件一个字都不动**）⇒ 把被断言的那条分支去掉：
#     · `alive_branch` ⇒ 去掉「记录说还活着 ⇒ 拒绝」⇒ 变异体会**接管**（A10 断言转红）
#     · `cross_host`   ⇒ 把「跨机 ⇒ unknown」改成「跨机 ⇒ dead」⇒ 变异体会**接管**（A11 转红）
#     · `start_ticks`  ⇒ 去掉「start_ticks 对不上 ⇒ 判死」⇒ 变异体会**拒**（A12 转红）
#   ⇒ 每条断言都必须「在变异体上不成立」才算有牙（否则记 FAIL）。
# ============================================================================

#: 造一份「变异体」：daily 目录整份软链过来，只把 `main.py` 换成**定点变异**的副本。
build_mutant() {  # build_mutant <变异 id> <目标目录> ⇒ 0 = 造好 / 1 = 失败
  local mut_id="$1" dest="$2"
  rm -rf "$dest"; mkdir -p "$dest" || return 1
  for f in "$PROJ/manu/etf_rotation/daily/"*.py; do
    ln -sf "$f" "$dest/$(basename "$f")"
  done
  rm -f "$dest/main.py"
  cp "$PROJ/manu/etf_rotation/daily/main.py" "$dest/main.py" || return 1
  "$QL_PY" - "$dest/main.py" "$mut_id" <<'PY'
import re
import sys
from pathlib import Path

path, mut_id = Path(sys.argv[1]), sys.argv[2]
src = path.read_text(encoding="utf-8")
if mut_id == "alive_branch":
    # 去掉「记录说持有者还活着 ⇒ 拒绝并发跑批」这条守卫（留下 else 兜底）
    try:
        start = src.index("            elif verdict == self._ALIVE:")
        end = src.index("            else:", start)
    except ValueError:
        raise SystemExit("变异锚点未命中：alive_branch")
    out = (src[:start]
           + "            elif verdict == self._ALIVE:\n"
           + "                pass  # 变异体：去掉「还活着 ⇒ 拒绝」这条守卫\n"
           + src[end:])
elif mut_id == "cross_host":
    # 把「跨机 ⇒ unknown（本机验不了 ⇒ 保守继续拦）」改成「跨机 ⇒ dead（误接管）」
    out, n = re.subn(r"return self\._UNKNOWN(\s*)#\s*跨机",
                     r"return self._DEAD\1# 跨机（变异体：误判成已死）", src)
    if n != 1:
        raise SystemExit("变异锚点未命中：cross_host")
elif mut_id == "start_ticks":
    # 去掉「PID 被复用（start_ticks 对不上）⇒ 判死」
    out, n = re.subn(r"if now_ticks is not None and now_ticks != expected:",
                     "if False:  # 变异体：去掉 PID 复用判定", src)
    if n != 1:
        raise SystemExit("变异锚点未命中：start_ticks")
else:
    raise SystemExit(f"未知变异 id：{mut_id}")
path.write_text(out, encoding="utf-8")
print(f"变异完成：{mut_id} -> {path}")
PY
}

#: 跑一次「每日链能不能拿到锁」⇒ 退出码（0 = 拿到 / 3 = 被拒）；日志落在 $3
case_run() {  # case_run <实现目录> <锁文件> <pid> <ticks 模式> <host> <日志>
  local impl="$1" lock="$2" pid="$3" ticks_mode="$4" host="$5" log="$6"
  write_lock_record "$lock" "$pid" running "$ticks_mode" "$host" || { echo 9; return; }
  daily_lock_try "$lock" "$impl" > "$log" 2>&1
  echo $?
}

#: 🔴 **前置闸门**：每日链内核锁必须**真的空** ⇒ A10~A12 断言的是「拿到内核锁**之后**」
#:    的判定分支；内核锁若还被占着，这三条会在 flock 那一步就被拒 ⇒ 断言空转，
#:    而且负例会**假绿**（「变异体没满足断言」只是因为压根没进判定分支）。
#:    等不到 ⇒ 直接退出探针（不出具空转结论）。
require_lock_free() {  # require_lock_free <谁需要它>
  local who="$1" i
  for i in $(seq 1 100); do
    if flock -n "$DAILY_LOCK" -c 'exit 0' 2>/dev/null; then return 0; fi
    sleep 0.1
  done
  echo "❌ 每日链内核锁未释放 ⇒ $who 走不到判定分支（探针夹具问题，不是实现问题）" >&2
  exit 1
}

#: 一个**活着**的进程（当作「记录里那个还活着的持有者」）
sleep 120 & ALIVE_PID=$!

# ---- A10：内核锁空 + 记录说持有者还活着 ⇒ 必须拒 --------------------------
require_lock_free "A10~A12"
LOG="$SC/a10.log"; RC="$(case_run "$IMPL_DIR" "$DAILY_LOCK" "$ALIVE_PID" auto "" "$LOG")"
if [ "$RC" -eq 3 ] && grep -q "锁记录显示持有者仍在运行" "$LOG"; then
  record A10 "每日链 · 内核锁空但记录说持有者还活着 ⇒ 仍被拒（不许接管）" 1 \
    "退出码=3 且日志含「锁记录显示持有者仍在运行」⇒ alive 分支生效（宁可误拦，也不误并发写）"
else
  record A10 "每日链 · 内核锁空但记录说持有者还活着 ⇒ 仍被拒（不许接管）" 0 \
    "退出码=$RC（期望 3）且日志需含「锁记录显示持有者仍在运行」；日志：$LOG"
fi

# ---- A11：记录里的 host 是别的机器 ⇒ 本机验不了 ⇒ 必须拒 ------------------
LOG="$SC/a11.log"; RC="$(case_run "$IMPL_DIR" "$DAILY_LOCK" "$ALIVE_PID" auto "probe-other-host" "$LOG")"
if [ "$RC" -eq 3 ] && grep -q "无法判定锁记录持有者是否已死" "$LOG"; then
  record A11 "每日链 · 记录里的 host 是别的机器 ⇒ 保守拒（不猜）" 1 \
    "退出码=3 且日志含「无法判定锁记录持有者是否已死」⇒ unknown 分支生效"
else
  record A11 "每日链 · 记录里的 host 是别的机器 ⇒ 保守拒（不猜）" 0 \
    "退出码=$RC（期望 3）且日志需含「无法判定锁记录持有者是否已死」；日志：$LOG"
fi

# ---- A12：pid 活着但 start_ticks 对不上（PID 复用）⇒ 判 dead ⇒ 接管 -------
LOG="$SC/a12.log"; RC="$(case_run "$IMPL_DIR" "$DAILY_LOCK" "$ALIVE_PID" wrong "" "$LOG")"
if [ "$RC" -eq 0 ] && grep -q "清理了陈旧锁" "$LOG"; then
  record A12 "每日链 · pid 活着但 start_ticks 对不上（PID 复用）⇒ 判死并接管" 1 \
    "退出码=0 且日志含「清理了陈旧锁」⇒ PID 复用分支生效（原持有者已死，可接管）"
else
  record A12 "每日链 · pid 活着但 start_ticks 对不上（PID 复用）⇒ 判死并接管" 0 \
    "退出码=$RC（期望 0）且日志需含「清理了陈旧锁」；日志：$LOG"
fi

kill -9 "$ALIVE_PID" 2>/dev/null; wait "$ALIVE_PID" 2>/dev/null

# ---- A10N/A11N/A12N：负例（变异体）⇒ 对应断言必须**不成立**（否则断言没牙）--
neg_case() {  # neg_case <负例 id> <变异 id> <用例名> <期望退出码> <期望日志关键词> <正例日志>
  local neg_id="$1" mut_id="$2" case_name="$3" want_rc="$4" want_kw="$5" ref_log="$6"
  local mut_dir="$SC/mut_$mut_id" mut_log="$SC/$neg_id.log" rc mut_kw
  if ! build_mutant "$mut_id" "$mut_dir" > "$SC/$neg_id.build.log" 2>&1; then
    record "$neg_id" "负例「$case_name」⇒ 变异体（去掉被断言的分支）必须**不**满足该断言" 0 \
      "变异体造不出来（正式文件未改动）；详见 $SC/$neg_id.build.log"
    return
  fi
  #: 用**同一个场景**跑变异体（锁记录重新写一遍 ⇒ 与正例完全同源）
  case "$mut_id" in
    alive_branch) rc="$(case_run "$mut_dir" "$DAILY_LOCK" "$ALIVE_PID2" auto "" "$mut_log")" ;;
    cross_host)   rc="$(case_run "$mut_dir" "$DAILY_LOCK" "$ALIVE_PID2" auto "probe-other-host" "$mut_log")" ;;
    start_ticks)  rc="$(case_run "$mut_dir" "$DAILY_LOCK" "$ALIVE_PID2" wrong "" "$mut_log")" ;;
  esac
  mut_kw="（无）"
  grep -q "锁记录显示持有者仍在运行" "$mut_log" 2>/dev/null && mut_kw="锁记录显示持有者仍在运行"
  grep -q "无法判定锁记录持有者是否已死" "$mut_log" 2>/dev/null && mut_kw="无法判定锁记录持有者是否已死"
  grep -q "清理了陈旧锁" "$mut_log" 2>/dev/null && mut_kw="清理了陈旧锁"
  if [ "$rc" -eq "$want_rc" ] && grep -q "$want_kw" "$mut_log" 2>/dev/null; then
    record "$neg_id" "负例「$case_name」⇒ 变异体（去掉被断言的分支）必须**不**满足该断言" 0 \
      "变异体实测仍满足该断言（rc=$rc，含「$want_kw」）⇒ 断言写死了，对该分支不敏感"
  else
    record "$neg_id" "负例「$case_name」⇒ 变异体（去掉被断言的分支）必须**不**满足该断言" 1 \
      "变异体（$mut_id）实测 rc=$rc、日志关键词=$mut_kw ≠ 正例期望（rc=$want_rc /「$want_kw」）" \
      "⇒ 去掉该分支后断言必红 ⇒ 断言有牙"
  fi
}

# 🔴 正例跑完后 ALIVE_PID 已被 kill ⇒ 负例要用**另一个活进程**重造记录
sleep 120 & ALIVE_PID2=$!
#: 🔴 负例同样必须**真的进**判定分支 ⇒ 内核锁为空是前提（同上，否则负例会假绿）
require_lock_free "A10N~A12N"
neg_case A10N alive_branch "A10 · 内核锁空但记录说还活着 ⇒ 拒" 3 "锁记录显示持有者仍在运行" "$SC/a10.log"
neg_case A11N cross_host   "A11 · 记录 host 是别的机器 ⇒ 拒" 3 "无法判定锁记录持有者是否已死" "$SC/a11.log"
neg_case A12N start_ticks  "A12 · PID 复用 ⇒ 判死并接管" 0 "清理了陈旧锁" "$SC/a12.log"
kill -9 "$ALIVE_PID2" 2>/dev/null; wait "$ALIVE_PID2" 2>/dev/null

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
N_CHECKS="${#CHECK_ID[@]}"
if [ "$FAIL" -eq 0 ]; then
  echo "合计 $N_CHECKS 项：PASS $N_CHECKS / FAIL 0"
  exit 0
fi
echo "合计 $N_CHECKS 项：有 $FAIL 项 FAIL"
exit 1
