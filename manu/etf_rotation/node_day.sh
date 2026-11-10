#!/usr/bin/env bash
# =============================================================================
# node_day.sh · 调仓节点日「一键执行链」（2026-11-05）
#
# 【定位】把专题档 §45.4 的「手工 6 步」收敛成一条命令：
#   节点 2026-12-01 的信号沿用 2026-11-03（12-01 不在打分相位栅格上），
#   节点当天**只重算股数/金额**，不重跑 S 层、不联网、不下载。
#
# 【五个阶段】
#   1) 预检：manu/etf_rotation/data/preflight_freshness.py（**不带 --asof**）
#      ⇒ 必须 exit 0，否则**立刻中止**并把预检日志尾部打出来。
#      （为什么不带 --asof：预检默认信号日 = 个股分数末日 = 2026-11-03；
#        带 --asof 2026-12-01 必然被打分栅格校验 S1 拦下 —— 那是**预期行为**，不是故障。）
#   2) 提醒：**只打印**，不执行任何下载（ETF 行情/成分/现货刷新 = 联网重活，需用户当轮授权）。
#   3) 重算：manu/etf_rotation/reprice_node_day.py --node-date <日期> --out-dir <产物目录>
#      ⇒ 「行情没覆盖节点日」由该脚本**硬拦**，本脚本只把报错原文转发出来。
#      ⇒ 总资产口径由 `--total-assets-basis {plan|node}` **透传**给该脚本（口径归它算）。
#   4) 清单：读 <产物目录>/node_reprice_<日期>.json 打印买卖清单（python 读 json，**不用 jq**）。
#   5) 摘要：打印产物路径 + 执行日（节点日 + 1 交易日）。
#
# 【硬约束（写死在本脚本里）】
#   * **不联网、不下载、不打分、不回测、不调 LLM**；
#   * **只写** reprice_node_day.py 自己的两个新文件（node_reprice_<日期>.json/.md），
#     且**已存在则拒绝覆盖**（fail-fast）；
#   * 临时文件落**唯一落点根**下的 `_work/`（**不落 /tmp**：tmpfs 会清 /tmp）；
#     无 nohup、无后台常驻。
#
# 【用法】
#   bash manu/etf_rotation/node_day.sh --node-date 2026-12-01
#   bash manu/etf_rotation/node_day.sh --node-date 2026-12-01 --dry-run
# =============================================================================
set -uo pipefail

# ---- 固定口径（路径 / 解释器）------------------------------------------------
QL_PY="/home/huangzhe/anaconda3/envs/ql/bin/python"
#: 🔴 **结果落点根**（可被环境变量 `ETF_ROTATION_RESULTS_ROOT` 覆盖 ⇒ 整棵树可搬家）
#:    单一事实源 = `manu/etf_rotation/daily/daily_config.py` 的同名常量。
RESULTS_ROOT="${ETF_ROTATION_RESULTS_ROOT:-/mnt/data/siping/runs/etf_rotation_results}"
#: 产物根 = 结果根（与 `daily_config.RESEARCH_RUN_ROOT` 同口径）
DEFAULT_RUN_ROOT="$RESULTS_ROOT"
#: 🔴 **数据集落点根**（只放**临时工作区**；与 `daily_config.OUTPUT_ROOT` 同口径）
OUTPUT_ROOT="${ETF_ROTATION_OUTPUT_ROOT:-/mnt/data/siping/datasets/manu/etf_rotation}"
#: 临时工作区 = 数据集根下的 `_work/`（与 `daily_config.WORK_ROOT` 同口径）
WORK_ROOT="$OUTPUT_ROOT/_work"
DEFAULT_REPO_ROOT="/tmp/pycharm_project_658d3fc4"

# ---- 参数默认值 --------------------------------------------------------------
NODE_DATE=""
RUN_ROOT="$DEFAULT_RUN_ROOT"
REPO_ROOT="$DEFAULT_REPO_ROOT"
TOTAL_ASSETS_BASIS="plan"
DRY_RUN=0

usage() {
  cat <<'EOF'
用法：bash manu/etf_rotation/node_day.sh --node-date YYYY-MM-DD [选项]

必填：
  --node-date YYYY-MM-DD      调仓节点日（如 2026-12-01）；不给 ⇒ 报错退出

可选：
  --run-root PATH             产物目录（默认 <落点根>/runs）
  --repo-root PATH            工程根目录（默认 /tmp/pycharm_project_658d3fc4）
  --total-assets-basis {plan|node}
                              总资产口径（默认 plan；**透传**给 reprice_node_day.py）
                                plan = 计划件口径（**账户快照日** 2026-11-04 的账户总资产）
                                node = 节点日口径（现金 + Σ 持仓股数 × 节点日收盘价）
  --dry-run                   只打印将要执行的命令，不真跑
  -h, --help                  打印本帮助

退出码：0 = 五个阶段全通过；1 = 任一阶段失败（fail-fast，中止后续阶段）。

说明：
  * 本脚本**不联网、不下载、不打分、不回测、不调 LLM**；
  * ETF 行情/成分/现货的刷新是**联网重活，需用户当轮授权**，本脚本只提醒、不执行；
  * 阶段 3 若报「ETF 行情覆盖不到节点日」⇒ 先刷数据，再重跑本脚本。
EOF
}

# ---- 参数解析（--opt value 与 --opt=value 两种写法都支持）-------------------
while [ "$#" -gt 0 ]; do
  case "$1" in
    --node-date)
      [ "$#" -ge 2 ] || { echo "🔴 --node-date 缺值" >&2; exit 1; }
      NODE_DATE="$2"; shift 2 ;;
    --node-date=*)
      NODE_DATE="${1#*=}"; shift ;;
    --run-root)
      [ "$#" -ge 2 ] || { echo "🔴 --run-root 缺值" >&2; exit 1; }
      RUN_ROOT="$2"; shift 2 ;;
    --run-root=*)
      RUN_ROOT="${1#*=}"; shift ;;
    --repo-root)
      [ "$#" -ge 2 ] || { echo "🔴 --repo-root 缺值" >&2; exit 1; }
      REPO_ROOT="$2"; shift 2 ;;
    --repo-root=*)
      REPO_ROOT="${1#*=}"; shift ;;
    --total-assets-basis)
      [ "$#" -ge 2 ] || { echo "🔴 --total-assets-basis 缺值" >&2; exit 1; }
      TOTAL_ASSETS_BASIS="$2"; shift 2 ;;
    --total-assets-basis=*)
      TOTAL_ASSETS_BASIS="${1#*=}"; shift ;;
    --dry-run)
      DRY_RUN=1; shift ;;
    -h|--help)
      usage; exit 0 ;;
    *)
      echo "🔴 未知参数：$1" >&2
      usage >&2
      exit 1 ;;
  esac
done

# ---- 参数校验（不给 --node-date ⇒ 直接报错退出）------------------------------
if [ -z "$NODE_DATE" ]; then
  echo "🔴 缺少必填参数 --node-date（YYYY-MM-DD，如 2026-12-01）" >&2
  usage >&2
  exit 1
fi
if ! printf '%s' "$NODE_DATE" | grep -Eq '^[0-9]{4}-[0-9]{2}-[0-9]{2}$'; then
  echo "🔴 --node-date 格式不对（要 YYYY-MM-DD）：$NODE_DATE" >&2
  exit 1
fi
if ! date -d "$NODE_DATE" +%F >/dev/null 2>&1; then
  echo "🔴 --node-date 不是合法日期：$NODE_DATE" >&2
  exit 1
fi
case "$TOTAL_ASSETS_BASIS" in
  plan|node) ;;
  *)
    echo "🔴 --total-assets-basis 只能是 plan 或 node（给的是：$TOTAL_ASSETS_BASIS）" >&2
    exit 1 ;;
esac
if [ ! -d "$REPO_ROOT" ]; then
  echo "🔴 工程根目录不存在：$REPO_ROOT（用 --repo-root 指定）" >&2
  exit 1
fi
if [ ! -x "$QL_PY" ]; then
  echo "🔴 找不到 ql 解释器：$QL_PY" >&2
  exit 1
fi
cd "$REPO_ROOT" || { echo "🔴 进不去工程根目录：$REPO_ROOT" >&2; exit 1; }
for _f in manu/etf_rotation/data/preflight_freshness.py manu/etf_rotation/reprice_node_day.py; do
  [ -f "$_f" ] || { echo "🔴 缺文件：$REPO_ROOT/$_f" >&2; exit 1; }
done

# ---- 临时目录（落落点根的 _work/，不落 /tmp；退出时清掉）--------------------
mkdir -p "$WORK_ROOT" || {
  echo "🔴 建不了临时工作目录（$WORK_ROOT）" >&2; exit 1; }
TMP_DIR="$(mktemp -d "$WORK_ROOT/_node_day_XXXXXX")" || {
  echo "🔴 建不了临时目录（$WORK_ROOT/_node_day_XXXXXX）" >&2; exit 1; }
trap 'rm -rf "$TMP_DIR"' EXIT

# ---- 各阶段退出码（任一非 0 ⇒ 最终退出码 1）---------------------------------
RC_PREFLIGHT=0
RC_REPRICE=0
RC_LIST=0
RC_SUMMARY=0

say() { printf '\n========== %s ==========\n' "$*"; }

printf '节点日 %s ｜ 产物目录 %s ｜ 工程根目录 %s ｜ 总资产口径 %s ｜ %s\n' \
  "$NODE_DATE" "$RUN_ROOT" "$REPO_ROOT" "$TOTAL_ASSETS_BASIS" \
  "$([ "$DRY_RUN" -eq 1 ] && echo 'dry-run（不真跑）' || echo '实跑')"

# =============================================================================
# 阶段 1：数据新鲜度预检（**不带 --asof**；失败 ⇒ 立刻中止）
# =============================================================================
say "阶段 1/5 · 数据新鲜度预检（不带 --asof）"
if [ "$DRY_RUN" -eq 1 ]; then
  printf '  将执行：%s manu/etf_rotation/data/preflight_freshness.py\n' "$QL_PY"
else
  "$QL_PY" manu/etf_rotation/data/preflight_freshness.py > "$TMP_DIR/preflight.log" 2>&1
  RC_PREFLIGHT=$?
  if [ "$RC_PREFLIGHT" -ne 0 ]; then
    echo "🔴 预检不通过（退出码 $RC_PREFLIGHT）⇒ 中止（不再重算、不打印清单）"
    echo "---------------- 预检日志尾部（tail -40）----------------"
    tail -40 "$TMP_DIR/preflight.log"
    echo "--------------------------------------------------------"
    exit 1
  fi
  tail -n 25 "$TMP_DIR/preflight.log"
  echo "  ✅ 预检通过（退出码 0）"
fi

# =============================================================================
# 阶段 2：联网刷新提醒（**只打印，不执行任何下载**）
# =============================================================================
say "阶段 2/5 · 联网刷新提醒（本阶段不执行任何下载）"
cat <<'EOF'
  🔴 ETF 行情 / 成分快照 / 现货候选池 的刷新**需要联网**（akshare 环境）
     ⇒ 属**重活，需用户当轮授权**；本脚本**不执行**任何下载（无 akshare 调用、无网络请求）。
  ℹ️ 若阶段 3 报「ETF 行情覆盖不到节点日」⇒ **先刷数据，再重跑本脚本**。
EOF
PX_MAX=""
if [ "$DRY_RUN" -eq 0 ] && [ -f "$TMP_DIR/preflight.log" ]; then
  PX_MAX="$(grep -E 'ETF 行情' "$TMP_DIR/preflight.log" | head -1 | sed -E 's/.*最新\/快照 = *([0-9]{4}-[0-9]{2}-[0-9]{2}).*/\1/' || true)"
fi
if [ -n "$PX_MAX" ]; then
  if [[ "$PX_MAX" < "$NODE_DATE" ]]; then
    echo "  🔴 当前 ETF 行情最新只到 $PX_MAX，**覆盖不到节点日 $NODE_DATE**"
    echo "     ⇒ **先刷数据，再重跑本脚本**（下列命令是联网重活，需你当轮授权后手工执行）："
    echo "       /home/huangzhe/anaconda3/envs/ak/bin/python manu/etf_rotation/data/fetch_etf_prices.py --resume"
    echo "       /home/huangzhe/anaconda3/envs/ak/bin/python manu/etf_rotation/data/fetch_etf_holdings.py --resume"
    echo "       /home/huangzhe/anaconda3/envs/ak/bin/python manu/etf_rotation/data/fetch_etf_universe.py --force"
  else
    echo "  ✅ ETF 行情最新 $PX_MAX，已覆盖节点日 $NODE_DATE（阶段 3 仍会再做一次硬校验）"
  fi
else
  echo "  ℹ️ 未能从预检输出解析出「ETF 行情最新日」⇒ 是否覆盖节点日交由阶段 3 的硬拦判定。"
fi

# =============================================================================
# 阶段 3：节点日股数重算（行情没覆盖节点日 ⇒ 由 reprice_node_day.py 硬拦）
# =============================================================================
say "阶段 3/5 · 节点日股数重算（reprice_node_day.py）"
PLAN_PATH="$RUN_ROOT/final_rebalance_plan.json"
OUT_JSON="$RUN_ROOT/node_reprice_${NODE_DATE//-/}.json"
OUT_MD="$RUN_ROOT/node_reprice_${NODE_DATE//-/}.md"

# 🔴 「绝不覆盖既有产物」：目标文件已存在 ⇒ fail-fast（不覆盖、不静默改写）
EXISTING=""
for _f in "$OUT_JSON" "$OUT_MD" "$RUN_ROOT/node_reprice_${NODE_DATE}.json" "$RUN_ROOT/node_reprice_${NODE_DATE}.md"; do
  if [ -e "$_f" ]; then EXISTING="$_f"; break; fi
done
if [ -n "$EXISTING" ]; then
  echo "🔴 目标产物已存在：**拒绝覆盖**（既有交付件一律不改）"
  echo "     已存在：$EXISTING"
  echo "     处置：① 确认要重跑就手工删掉该文件；② 或用 --run-root 指向一个新目录另存。"
  exit 1
fi

# ---- 总资产口径：plan（默认）｜ node ⇒ **透传**给 reprice_node_day.py ---------
# 🔴 口径算法归上游所有（node = 现金 + Σ(持仓股数 × 节点日收盘价)）⇒ 本脚本**不自己重算**，
#    也不用 --total-assets 去覆盖它 ⇒ 免得两处算法漂移（单一事实源）。
REPRICE_ARGS=(--node-date "$NODE_DATE" --plan "$PLAN_PATH" --out-dir "$RUN_ROOT"
              --total-assets-basis "$TOTAL_ASSETS_BASIS")

if [ "$DRY_RUN" -eq 1 ]; then
  printf '  将执行：%s manu/etf_rotation/reprice_node_day.py' "$QL_PY"
  printf ' %s' "${REPRICE_ARGS[@]}"
  printf '\n'
else
  "$QL_PY" manu/etf_rotation/reprice_node_day.py "${REPRICE_ARGS[@]}" > "$TMP_DIR/reprice.log" 2>&1
  RC_REPRICE=$?
  if [ "$RC_REPRICE" -ne 0 ]; then
    echo "🔴 重算失败（退出码 $RC_REPRICE）⇒ 中止（不打印清单）"
    echo "---------------- 重算报错原文 ----------------"
    cat "$TMP_DIR/reprice.log"
    echo "--------------------------------------------"
    exit 1
  fi
  cat "$TMP_DIR/reprice.log"
  echo "  ✅ 重算完成（退出码 0）"
fi

# =============================================================================
# 阶段 4：打印买卖清单（python 读 json，不用 jq）
# =============================================================================
say "阶段 4/5 · 买卖清单"
if [ "$DRY_RUN" -eq 1 ]; then
  echo "  将执行：$QL_PY（内联脚本）读取 $OUT_JSON 并打印买卖清单 / 期末现金 / 实际落地权益"
else
  if [ ! -f "$OUT_JSON" ]; then
    echo "🔴 重算产物不存在：$OUT_JSON ⇒ 中止" >&2
    exit 1
  fi
  "$QL_PY" - "$OUT_JSON" > "$TMP_DIR/list.log" 2>&1 <<'PY'
import json
import sys
import unicodedata
from pathlib import Path

path = Path(sys.argv[1])
data = json.loads(path.read_text(encoding="utf-8"))
meta = data.get("meta") or {}
cf = data.get("cash_flow") or {}
landing = data.get("landing") or {}
actions = data.get("actions") or []


def _w(text) -> int:
    """显示宽度（中文/全角算 2 列）。"""
    return sum(2 if unicodedata.east_asian_width(ch) in ("W", "F") else 1
               for ch in str(text))


def _pad(text, size, right=False) -> str:
    text = "" if text is None else str(text)
    gap = max(0, size - _w(text))
    return (" " * gap + text) if right else (text + " " * gap)


def _num(value, digits=2) -> str:
    if value is None:
        return "-"
    try:
        return f"{float(value):,.{digits}f}"
    except (TypeError, ValueError):
        return str(value)


def _pct(value) -> str:
    return "-" if value is None else f"{float(value) * 100:.2f}%"


def _signed(value, digits=2) -> str:
    if value is None:
        return "-"
    try:
        number = float(value)
    except (TypeError, ValueError):
        return str(value)
    return ("+" if number > 0 else "") + f"{number:,.{digits}f}"


def _signed_int(value) -> str:
    """股数：带符号的整数（不加千分位，与「当前/目标股数」列一致）。"""
    if value is None:
        return "-"
    try:
        number = int(value)
    except (TypeError, ValueError):
        return str(value)
    return f"+{number}" if number > 0 else str(number)


header = ["代码", "名称", "动作", "当前股数", "目标股数", "变化股数",
          "节点日价", "目标金额", "成交金额"]
rows = []
for a in actions:
    rows.append([
        a.get("code", ""),
        a.get("name", ""),
        a.get("action", ""),
        a.get("current_shares", ""),
        a.get("target_shares", ""),
        _signed_int(a.get("delta_shares")),
        _num(a.get("price"), 3),
        _num(a.get("target_amount")),
        _signed(a.get("est_amount")),
    ])
widths = [max(_w(header[i]), max([_w(r[i]) for r in rows], default=0))
          for i in range(len(header))]

print(f"节点日 {meta.get('node_date', '?')} 的买卖清单"
      f"（按节点日收盘价折算；信号日 = {meta.get('plan_asof', '?')}，计划件口径沿用）")
print("  " + " | ".join(_pad(header[i], widths[i]) for i in range(len(header))))
print("  " + "-+-".join("-" * widths[i] for i in range(len(header))))
for row in rows:
    print("  " + " | ".join(_pad(row[i], widths[i]) for i in range(len(header))))
print()
print(f"  期末现金：{_num(cf.get('cash_end'))} 元"
      f"（期初 {_num(cf.get('cash_begin'))} + 卖出回款 {_num(cf.get('sell_proceeds'))}"
      f" - 卖出费用 {_num(cf.get('sell_fees'))} - 买入 {_num(cf.get('buy_amount'))}"
      f" - 买入费用 {_num(cf.get('buy_fees'))}）")
print(f"  实际落地权益：{_num(landing.get('realized_equity_amount'))} 元 / "
      f"{_pct(landing.get('realized_equity_weight'))}"
      f"（计划权益权重 {_pct(landing.get('planned_equity_weight'))}，"
      f"实际现金 {_pct(landing.get('realized_cash_weight'))}）")
print(f"  整手取整缺口：{_num(landing.get('lot_rounding_gap'))} 元"
      f" ｜ 现金缺口：{_num(cf.get('deficit'))} 元"
      f"（{'可执行' if cf.get('feasible') else '有缺口'}）")
unbuildable = cf.get("unbuildable_slots") or []
if unbuildable:
    print("  整手买不起（权重归现金）："
          + "、".join(f"{u.get('code')} {u.get('name')}" for u in unbuildable))
PY
  RC_LIST=$?
  if [ "$RC_LIST" -ne 0 ]; then
    echo "🔴 清单打印失败（退出码 $RC_LIST）" >&2
    cat "$TMP_DIR/list.log" >&2
    exit 1
  fi
  cat "$TMP_DIR/list.log"
fi

# =============================================================================
# 阶段 5：摘要（产物路径 + 执行日）
# =============================================================================
say "阶段 5/5 · 摘要"
if [ "$DRY_RUN" -eq 1 ]; then
  echo "  将执行：$QL_PY（内联脚本）读 $OUT_JSON 打印产物路径 + 执行日"
else
  "$QL_PY" - "$OUT_JSON" "$OUT_MD" > "$TMP_DIR/summary.log" 2>&1 <<'PY'
import json
import sys
from pathlib import Path

json_path, md_path = sys.argv[1], sys.argv[2]
meta = (json.loads(Path(json_path).read_text(encoding="utf-8")).get("meta") or {})


def _num(value) -> str:
    """金额：两位小数（上游是 float，直接打会带 1e-13 级噪声）。"""
    if value is None:
        return "（读不到）"
    try:
        return f"{float(value):,.2f}"
    except (TypeError, ValueError):
        return str(value)


node_date = meta.get("node_date") or "?"
execution_date = meta.get("execution_date")
lag = meta.get("execution_lag_days", 1)
print(f"  产物 JSON：{json_path}")
print(f"  产物 MD  ：{md_path}")
print(f"  节点日   ：{node_date}（信号沿用计划件 asof = {meta.get('plan_asof', '?')}）")
if execution_date:
    print(f"  执行日   ：{execution_date}（= 节点日 + {lag} 交易日）")
else:
    print(f"  执行日   ：产物里没有 execution date 字段 ⇒ 执行日请按 "
          f"`config.EXECUTION_LAG_DAYS={lag}` + 交易日历确认")
print(f"  总资产口径：{meta.get('total_assets_basis') or '（读不到）'}"
      f" ｜ 来源：{meta.get('total_assets_source') or meta.get('account_source') or '（读不到）'}")
print(f"  生效总资产：{_num(meta.get('total_assets'))} 元 ｜ 现金 {_num(meta.get('cash'))} 元"
      f" ｜ 双口径对照：计划件 {_num(meta.get('plan_total_assets'))} 元"
      f" / 节点日 {_num(meta.get('node_total_assets'))} 元")
print(f"  重算范围：{meta.get('scope', '?')}")
PY
  RC_SUMMARY=$?
  if [ "$RC_SUMMARY" -ne 0 ]; then
    echo "🔴 摘要打印失败（退出码 $RC_SUMMARY）" >&2
    cat "$TMP_DIR/summary.log" >&2
    exit 1
  fi
  cat "$TMP_DIR/summary.log"
fi

# =============================================================================
# 收尾：给用户看的操作提示 + 阶段退出码
# =============================================================================
say "给你的操作提示"
cat <<EOF
  · 这份清单是「**按节点日（$NODE_DATE）收盘价折算的股数**」，**不是成交指令**；
  · 实际成交在**执行日**（节点日 + 1 交易日）⇒ **执行日价格可能不同** ⇒ 成交股数/金额会变；
  · 下单前请按执行日的实际价格**重新核对**（尤其整手取整处在边界的标的）；
  · 本脚本**只**做了「预检 → 重算 → 打印」：没联网、没下载、没打分、没回测、没调 LLM；
  · 产物写在 $RUN_ROOT（**新文件名**，既有交付件一个都没动）。
EOF

say "阶段退出码"
printf '  阶段 1 预检 = %s\n  阶段 3 重算 = %s\n  阶段 4 清单 = %s\n  阶段 5 摘要 = %s\n' \
  "$RC_PREFLIGHT" "$RC_REPRICE" "$RC_LIST" "$RC_SUMMARY"
if [ "$DRY_RUN" -eq 1 ]; then
  echo "  （dry-run：以上命令**都没有真正执行**）"
fi
if [ "$RC_PREFLIGHT" -ne 0 ] || [ "$RC_REPRICE" -ne 0 ] \
   || [ "$RC_LIST" -ne 0 ] || [ "$RC_SUMMARY" -ne 0 ]; then
  echo "最终退出码：1"
  exit 1
fi
echo "最终退出码：0"
exit 0
