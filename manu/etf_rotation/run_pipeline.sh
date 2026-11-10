#!/usr/bin/env bash
# manu/etf_rotation/run_pipeline.sh —— **一键自动化流程**
#
#   qlib 全市场个股打分 → 导出个股日线 → ETF 分析报告
#   → TradingAgents 多智能体决策 → 最终调仓计划（下一节点）
#
# 用法（服务器）：
#
#   --reuse-decision：🔴 复用**本轮已落盘**的 TradingAgents 决策（**不重跑 LLM**）。
#     只在「LLM 已跑完、后续步骤 FAIL」时用；前置校验「决策件不早于报告/上下文」
#     ⇒ 保证三环同代（rules.md §15.4）；sha1 链仍由 8/16 `verify_artifacts` 复核。
#   bash manu/etf_rotation/run_pipeline.sh [--asof 2026-11-03] [--out-dir DIR]
#                                          [--skip-scores] [--skip-prices] [--skip-ta]
#                                          [--reuse-decision]
#                                          [--rebalance-every N] [--start YYYY-MM-DD]
#                                          [--provider-uri DIR]
#
#   --provider-uri：🔴 2026-11-09 新增 —— 指定本次打分用的 **qlib provider 目录**
#     （研究链默认只有上游基座 `cn_data`；阶段 1 自建的增量日在 `cn_data_live`）。
#     **默认空 = 上游基座（零回归，当前基线不漂）**；显式指定 ⇒ 研究链与每日链
#     吃**同一个**数据集（口径不分叉）⇒ 12-01 重建时不会静默停在 11-03 的旧分数。
#     等价环境变量：`ETF_ROTATION_QLIB_PROVIDER_URI`（脚本显式参数优先）。
#
# 口径（manu/memory/rules.md §十四）：
#   * 个股层不可跳过（E = W × S）；
#   * α（E 权重）固定 0.5，与动量各半；
#   * 池口径 e_covered（只允许能穿透的 ETF 入池）；
#   * 调仓节点 = 下一个自然月的第一个交易日；
#   * ETF 免印花税，佣金 1.5bp、滑点 5bp，整手 100 份。
#
# 步数（2026-11-05 07:2x，M-S3）：**16 步**（M-S2 的 15 步 + 16/16 节点日重算工具闸门）。
set -uo pipefail

ASOF=""
#: 🔴 **结果落点根**（可被环境变量 `ETF_ROTATION_RESULTS_ROOT` 覆盖 ⇒ 整棵树可搬家）
#:    单一事实源 = `manu/etf_rotation/daily/daily_config.py` 的同名常量。
#:    🔴 `RESULTS_ROOT_DEFAULT` 必须与 `rebuild_baseline.sh` 里的**同名变量逐字相同**
#:       （两份脚本靠它做「备份目录 == 重建目录」的交叉校验）。
RESULTS_ROOT_DEFAULT="/mnt/data/siping/runs/etf_rotation_results"
RESULTS_ROOT="${ETF_ROTATION_RESULTS_ROOT:-$RESULTS_ROOT_DEFAULT}"
OUT_DIR="$RESULTS_ROOT"
SKIP_SCORES=0
SKIP_PRICES=0
SKIP_TA=0
#: 🔴 2026-11-09 新增：**复用本轮已生成的 TradingAgents 决策**（不重跑 LLM）。
#:    用途：LLM 已跑完、但后续步骤（如 8/16 回归自检）FAIL ⇒ 只重跑 7~16 步，
#:    **不许重跑 25 分钟的 LLM**（决策件已落盘，重跑会换一代意见）。
#:    铁律：① 前置校验「决策件比报告/上下文新」⇒ 保证**三环同代**（rules.md §15.4）；
#:          ② sha1 链仍由 8/16 `verify_artifacts` 复核，链断照样 die。
REUSE_DECISION=0
#: 🔴 M-S4（2026-11-05 08:0x）：打分相位步长 / 起点 —— **同一个值同时传给打分器与预检**
#:    （否则守卫算 10 日栅格、打分器算 20 日栅格 ⇒ 守卫可能放行一个打不出分的日子）
REBALANCE_EVERY=""
GRID_START=""
#: 最终计划要嵌入的 LLM 决策文件；没有可信决策时指向不存在的路径（⇒ 不嵌 LLM 意见）
DECISION_ARG="$OUT_DIR/.no_llm_decision.md"
#: 🔴 2026-11-09 新增：qlib **provider 覆盖**（研究链 ↔ 每日链口径不分叉）。
#:    默认 **空** ⇒ `qlib_scores.py` 用 `C.PROVIDER_URI`（上游基座）⇒ **零回归**；
#:    显式指定（`--provider-uri DIR` 或 env `ETF_ROTATION_QLIB_PROVIDER_URI`）⇒ 用该目录。
PROVIDER_URI="${ETF_ROTATION_QLIB_PROVIDER_URI:-}"
#: 🔴 2026-11-09 新增：穿透分时间平滑。
#:    🔴 **分析链（analyze.py）与规则层（recommend.py）必须拿同一组参数**
#:    ⇒ 否则两条链的平滑分不同 ⇒ 选出不同的票 ⇒ 交付与规则不同源。
NO_SMOOTHING=0
SMOOTHING_METHOD_ARG=""
SMOOTHING_WINDOW_ARG=""
SMOOTHING_ALPHA_ARG=""
SMOOTHING_HISTORY_ARG=""

while [ $# -gt 0 ]; do
  case "$1" in
    --asof)         ASOF="$2"; shift 2 ;;
    --out-dir)      OUT_DIR="$2"; shift 2 ;;
    --skip-scores)  SKIP_SCORES=1; shift ;;
    --skip-prices)  SKIP_PRICES=1; shift ;;
    --skip-ta)      SKIP_TA=1; shift ;;
    --reuse-decision) REUSE_DECISION=1; shift ;;
    --rebalance-every) REBALANCE_EVERY="$2"; shift 2 ;;
    --start)        GRID_START="$2"; shift 2 ;;
    --provider-uri) PROVIDER_URI="$2"; shift 2 ;;
    --no-smoothing)   NO_SMOOTHING=1; shift ;;
    --smoothing-method)   SMOOTHING_METHOD_ARG="$2"; shift 2 ;;
    --smoothing-window)   SMOOTHING_WINDOW_ARG="$2"; shift 2 ;;
    --smoothing-alpha)    SMOOTHING_ALPHA_ARG="$2"; shift 2 ;;
    --smoothing-history-days) SMOOTHING_HISTORY_ARG="$2"; shift 2 ;;
    -h|--help)      sed -n '2,16p' "$0"; exit 0 ;;
    *) echo "unknown arg: $1" >&2; exit 2 ;;
  esac
done

PROJ="${PROJ_DIR:-/tmp/pycharm_project_658d3fc4}"
QL_PY="${QL_PY:-/home/huangzhe/anaconda3/envs/ql/bin/python}"
LLM_PY="${LLM_PY:-/home/huangzhe/anaconda3/envs/llm/bin/python}"
TA_DIR="$PROJ/TradingAgents"
LOG="$OUT_DIR/pipeline.log"
mkdir -p "$OUT_DIR"

#: 🔴 平滑参数组：**同时**传给 analyze.py 与 recommend.py
SMOOTH_ARGS=()
[ "$NO_SMOOTHING" -eq 1 ] && SMOOTH_ARGS+=(--no-smoothing)
[ -n "$SMOOTHING_METHOD_ARG" ]   && SMOOTH_ARGS+=(--smoothing-method "$SMOOTHING_METHOD_ARG")
[ -n "$SMOOTHING_WINDOW_ARG" ]   && SMOOTH_ARGS+=(--smoothing-window "$SMOOTHING_WINDOW_ARG")
[ -n "$SMOOTHING_ALPHA_ARG" ]    && SMOOTH_ARGS+=(--smoothing-alpha "$SMOOTHING_ALPHA_ARG")
[ -n "$SMOOTHING_HISTORY_ARG" ]  && SMOOTH_ARGS+=(--smoothing-history-days "$SMOOTHING_HISTORY_ARG")

say() { printf '[%s] %s\n' "$(date '+%F %T')" "$*" | tee -a "$LOG"; }
die() { say "FATAL: $*"; exit 1; }

cd "$PROJ" || die "工作目录不存在：$PROJ（服务器路径）"
[ -x "$QL_PY" ]  || die "找不到 ql 解释器 $QL_PY"
say "=== pipeline start === asof=${ASOF:-auto} out_dir=$OUT_DIR"
START_TS="$(date '+%F %T')"

# ---- 0) 同 out-dir 实例守卫（flock） --------------------------------------
# 事故记录（2026-11-04 夜实测）：升权会话里的 setsid nohup 实例实际活到 21:23，
# 与 21:10 的前台实例并发写同一 out-dir ⇒ 第一次的决策文件被覆盖、无法复查。
# 修法：用 flock 做互斥，同一 out-dir 同时只允许一个流水线实例。
# flock 由内核托管，进程退出（含 kill -9）自动释放 ⇒ 不会留下死锁。
LOCK="$OUT_DIR/.pipeline.lock"
LOCK_INFO="$OUT_DIR/.pipeline.lock.info"
command -v flock >/dev/null 2>&1 || die "系统缺 flock（util-linux）⇒ 无法做同目录实例守卫，拒绝并发写"
[ -d "$OUT_DIR" ] || die "out-dir 不存在：$OUT_DIR"
exec 9>>"$LOCK" || die "无法打开锁文件：$LOCK"
if ! flock -n 9; then
  say "FATAL: 同一 out-dir 已有流水线实例在运行 ⇒ 拒绝并发写"
  if [ -f "$LOCK_INFO" ]; then sed 's/^/    /' "$LOCK_INFO" >&2; fi
  say "    逃生口：确认无实例在跑后，rm -f \"$LOCK\" 再重跑（flock 锁在内核里，删文件即换 inode ⇒ 旧锁失效）"
  exit 3
fi
printf 'pid=%s\nhost=%s\nasof=%s\nstarted=%s\nscript=%s\nstate=running\n' \
  "$$" "$(hostname)" "${ASOF:-auto}" "$START_TS" "$0" > "$LOCK_INFO"
# 锁 fd 9 **故意**让子进程继承：主脚本被杀时，孤儿子进程仍持锁 ⇒ 下一轮被拒（fail-safe）。
# 代价：子进程若长期存活会一直占锁 ⇒ 提供上面的 rm 逃生口。正常收尾时全部子进程都已退出 ⇒ 锁自动释放。
_flock_release() {
  if [ -f "$LOCK_INFO" ] && grep -q '^state=running$' "$LOCK_INFO" 2>/dev/null; then
    printf 'pid=%s\nhost=%s\nasof=%s\nstarted=%s\nscript=%s\nstate=finished\n' \
      "$$" "$(hostname)" "${ASOF:-auto}" "$START_TS" "$0" > "$LOCK_INFO"
  fi
}
trap _flock_release EXIT
say "实例锁已获取：$LOCK（pid=$$）"

# ---- 0.5) 数据新鲜度预检（在花一小时打分之前先失败）----------------------
# 🔴 M-R（2026-11-05 05:2x 实测）：一键流水线**只刷新个股分数**，
#    **从不刷新** ETF 行情 / 候选池 / W 快照 ⇒ 到下一个调仓节点时它们很可能还停在上一轮。
#    而特征层按「`date <= asof` 截断后取最后一条」算特征 ⇒ **行情够不到信号日时**，
#    它会拿**旧收盘价**冒充信号日特征 ⇒ **静默降级 ⇒ 假报告**。
#    ⇒ 这里先查一次，**不达标立刻 die**，别把一小时花在必然被拦下的打分上。
#    （权威版守卫在 analyze.py::_data_freshness_guard ⇒ 报告本身也不会带病产出。）
say "0.5/16 数据新鲜度预检（ETF 行情/候选池/W 快照 vs 信号日）"
PREFLIGHT_ARGS=()
[ -n "$ASOF" ] && PREFLIGHT_ARGS+=(--asof "$ASOF")
# 🔴 M-S4：**同一个**步长 / 起点同时喂给预检与打分器 ⇒ 守卫与打分器不可能口径漂移
[ -n "$REBALANCE_EVERY" ] && PREFLIGHT_ARGS+=(--rebalance-every "$REBALANCE_EVERY")
[ -n "$GRID_START" ] && PREFLIGHT_ARGS+=(--start "$GRID_START")
# 🔴 M-S4：M-S2 让预检输出从 20 行涨到 43 行 ⇒ `tail -12` 会把**处置指引**（怎么修）
#    整段截掉，而下面的 die 文案却写着「详见上面提示」⇒ 改成 tail -40。
"$QL_PY" manu/etf_rotation/data/preflight_freshness.py "${PREFLIGHT_ARGS[@]}" 2>&1 | tail -40 | tee -a "$LOG"
[ "${PIPESTATUS[0]}" -eq 0 ] || die "数据新鲜度预检不通过 ⇒ ETF 行情够不到信号日 ⇒ 拒绝继续（详见上面提示）"

# ---- 1) qlib 全市场个股打分（重活；模型已在本地，infer 模式） -------------
if [ "$SKIP_SCORES" -eq 0 ]; then
  say "1/16 qlib 个股打分（全市场，E=W×S 的 S 层）"
  SCORE_ARGS=()
  [ -n "$REBALANCE_EVERY" ] && SCORE_ARGS+=(--rebalance-every "$REBALANCE_EVERY")
  [ -n "$GRID_START" ] && SCORE_ARGS+=(--start "$GRID_START")
  # 🔴 provider 覆盖 + **披露**（数据集覆盖末日 / 未覆盖目标日告警）
  #    ⇒ 12-01 重建时能一眼看出「这次打的分是不是真的到 12-01 之前」。
  PROV_ARGS=(--asof "${ASOF:-}")
  if [ -n "$PROVIDER_URI" ]; then
    SCORE_ARGS+=(--provider-uri "$PROVIDER_URI")
    PROV_ARGS+=(--provider-uri "$PROVIDER_URI")
  fi
  PROV_LINE="$("$QL_PY" manu/etf_rotation/qlib_provider.py "${PROV_ARGS[@]}" 2>&1 | tail -1)"
  say "   provider：$PROV_LINE"
  if [ -n "$ASOF" ]; then
    "$QL_PY" manu/etf_rotation/qlib_scores.py --mode infer --out-dir "$OUT_DIR" --end "$ASOF" "${SCORE_ARGS[@]}" 2>&1 | tail -20 | tee -a "$LOG"
  else
    "$QL_PY" manu/etf_rotation/qlib_scores.py --mode infer --out-dir "$OUT_DIR" "${SCORE_ARGS[@]}" 2>&1 | tail -20 | tee -a "$LOG"
  fi
  [ "${PIPESTATUS[0]}" -eq 0 ] || say "WARN: 个股打分返回非零（继续，用已有 stock_scores.csv）"
else
  say "1/16 跳过个股打分（--skip-scores）"
fi
[ -f "$OUT_DIR/stock_scores.csv" ] || die "缺 $OUT_DIR/stock_scores.csv（个股层不可跳过）"

# ---- 2) 导出个股日线（给 N=5 / N=20 的 RankIC 用；≤ asof，无未来函数） ----
if [ "$SKIP_PRICES" -eq 0 ] && [ ! -f "$OUT_DIR/stock_prices.csv" ]; then
  say "2/16 导出个股日线（stock_prices.csv）"
  "$QL_PY" manu/etf_rotation/data/export_qlib_prices.py --out "$OUT_DIR/stock_prices.csv" 2>&1 | tail -10 | tee -a "$LOG"
else
  say "2/16 跳过个股日线导出（已存在或 --skip-prices）"
fi

# ---- 2.5) 账户事实源导出（交付目录自带口径 ⇒ 8/16 回归自检 / H1 有据可查）----
#    🔴 2026-11-10 真缺陷：verify_artifacts.py 的「账户对账」与「持仓诊断逐只齐全」
#    已改成「期望值只认**交付件同目录**的 account_context.json（回落 account_snapshot.csv），
#    都没有 ⇒ FAIL（不许真空 PASS）」⇒ 研究链的交付目录**必须自带**账户事实源，
#    否则这两项必然红、8/16 直接 die（实测 24 项 FAIL 2 ⇒ 整链 rc=1）。
#    口径 = 研究链自己的基准（manu/etf_rotation/account.py，2026-11-04），
#    绝不串到每日链的实盘基准（daily/account_state.py，2026-11-05）去。
say "2.5/16 导出账户事实源（account_context.json + account_snapshot.csv）"
"$QL_PY" manu/etf_rotation/emit_account_context.py --out-dir "$OUT_DIR" 2>&1 | tail -5 | tee -a "$LOG"
[ "${PIPESTATUS[0]}" -eq 0 ] || die "账户事实源导出失败（交付件将无账户口径 ⇒ 闸门必红）"

# ---- 3) ETF 分析报告 ------------------------------------------------------
say "3/16 生成 ETF 分析报告"
ANALYZE_ARGS=(--out-dir "$OUT_DIR" --stock-prices "$OUT_DIR/stock_prices.csv")
[ -n "$ASOF" ] && ANALYZE_ARGS+=(--asof "$ASOF")
#: 🔴 平滑参数**两链同组**（分析链 + 规则层）
ANALYZE_ARGS+=("${SMOOTH_ARGS[@]+"${SMOOTH_ARGS[@]}"}")
"$QL_PY" manu/etf_rotation/analyze.py "${ANALYZE_ARGS[@]}" 2>&1 | tail -20 | tee -a "$LOG"
[ "${PIPESTATUS[0]}" -eq 0 ] || die "analyze.py 失败（报告不可生成 ⇒ 不产出假计划）"
[ -f "$OUT_DIR/etf_analysis_report.json" ] || die "缺 etf_analysis_report.json"

# ---- 4) 规则层调仓建议（recommend.py；持仓权重自动从账户快照推导） --------
#    口径：e_covered 池 + 入场 MA20 / 持仓 MA60 破位过滤（铁律 14.9 / 14.10）
say "4/16 规则层调仓建议（recommend.py）"
HOLDINGS_ARG="$("$QL_PY" -c "import sys; sys.path.insert(0,'manu/etf_rotation'); import account; w=account.current_weights(); print(','.join(str(k)+':'+format(v,'.6f') for k,v in sorted(w.items())))" 2>/dev/null)"
if [ -z "$HOLDINGS_ARG" ]; then
  say "WARN: 账户快照读不到持仓权重 ⇒ recommend.py 按空持仓跑（不编造）"
fi
RECOMMEND_ARGS=(--out-dir "$OUT_DIR" --pool-mode e_covered --entry-ma-window 20 --exit-ma-window 60)
#: 🔴 与 analyze.py **同一组**（不同组 ⇒ 两链分叉 ⇒ 交付与规则不同源）
RECOMMEND_ARGS+=("${SMOOTH_ARGS[@]+"${SMOOTH_ARGS[@]}"}")
[ -n "$ASOF" ] && RECOMMEND_ARGS+=(--asof "$ASOF")
[ -n "$HOLDINGS_ARG" ] && RECOMMEND_ARGS+=(--holdings "$HOLDINGS_ARG")
"$QL_PY" manu/etf_rotation/recommend.py "${RECOMMEND_ARGS[@]}" 2>&1 | tail -10 | tee -a "$LOG"
[ "${PIPESTATUS[0]}" -eq 0 ] || say "WARN: recommend.py 返回非零（规则层建议缺失，继续走最终计划）"
[ -f "$OUT_DIR/rebalance_recommendation.json" ] || say "WARN: 缺 rebalance_recommendation.json"

# ---- 5) TradingAgents 多智能体决策（LLM 第二意见） -------------------------
#    🔴 口径（2026-11-04 夜实测三次失败后定稿，rules.md §十四.18）：
#       · 只用 Python API（cli/run.py 的 research_context 没接线）；
#       · 数据工具在脚本内**打桩**成「DATA_UNAVAILABLE」哨兵
#         （yfinance 被 Yahoo 限流 ⇒ 退避 2+4+8s 空转；alpha_vantage 无 key ⇒ 掀翻整张图）；
#       · 递归上限 60（调小会被 analyst 的工具轮耗尽 ⇒ GraphRecursionError）；
#       · effort=max ⇒ max_tokens 必须是 32768（否则思考 token 吃光正文）。
if [ "$SKIP_TA" -eq 0 ] && [ "$REUSE_DECISION" -eq 1 ]; then
  # ---- 复用本轮决策（不重跑 LLM）：**前置三件套 + 同代校验** ------------------
  say "5/16~6/16 复用本轮 TradingAgents 决策（--reuse-decision；**不重跑 LLM**）"
  [ -f "$OUT_DIR/tradingagents_decision.md" ] \
    || die "没有 $OUT_DIR/tradingagents_decision.md ⇒ 无可复用决策"
  [ -f "$OUT_DIR/tradingagents_decision.json" ] \
    || die "没有 $OUT_DIR/tradingagents_decision.json ⇒ 无可复用决策"
  _dec_status="$("$QL_PY" -c "import json,sys;print(json.load(open(sys.argv[1])).get('status',''))" \
      "$OUT_DIR/tradingagents_decision.json" 2>/dev/null)"
  [ "$_dec_status" = "ok" ] \
    || die "复用的决策 status=${_dec_status:-无} ⇒ 不是成功轮（不许凑数）"
  # ---- 同代校验（**内容指纹**，不靠 mtime）------------------------------
  # 🔴 2026-11-09 实测踩坑：先用「决策件比报告新」当同代判据 ⇒ **必然误判** ——
  #    步骤 3 每次都重写报告（内容逐字节一致）⇒ 重跑时报告 mtime 必然比决策新。
  #    ⇒ 改用**内容指纹**：决策件必须比上下文新（决策出自这份上下文），
  #      且报告内容指纹必须 == ta_context_meta.json 登记值（报告仍是同一代）。
  [ -f "$OUT_DIR/ta_context_meta.json" ] \
    || die "同代校验失败：缺 ta_context_meta.json（没有上下文登记 ⇒ 无法证明同代）"
  if [ "$OUT_DIR/tradingagents_decision.md" -ot "$OUT_DIR/ta_context_meta.json" ]; then
    die "同代校验失败：决策件比上下文旧 ⇒ 那是**上一代**意见（规则 §15.4：基线必须三环同代）"
  fi
  _chain_ok="$("$QL_PY" -c "
import json, sys
sys.path.insert(0, 'manu/etf_rotation')
from report_fingerprint import report_content_sha1_from_file
_meta = json.load(open(sys.argv[1], encoding='utf-8'))
_now = report_content_sha1_from_file(sys.argv[2])
_ref = _meta.get('report_content_sha1')
print('OK' if (_ref and _now and _ref == _now) else 'MISMATCH')
" "$OUT_DIR/ta_context_meta.json" "$OUT_DIR/etf_analysis_report.json" 2>/dev/null)"
  [ "$_chain_ok" = "OK" ] \
    || die "同代校验失败：报告内容指纹与 ta_context_meta.json 登记值不一致（$_chain_ok）⇒ 决策件是**上一代**意见"
  say "     ⇒ 同代校验通过（决策比上下文新 + 报告内容指纹与登记一致）｜sha1 链由 8/16 复核"
  DECISION_ARG="$OUT_DIR/tradingagents_decision.md"
elif [ "$SKIP_TA" -eq 0 ]; then
  say "5/16 构造 TradingAgents 上下文（压缩版 research_context）"
  "$QL_PY" manu/etf_rotation/llm/build_ta_context.py --run-root "$OUT_DIR" 2>&1 | tail -5 | tee -a "$LOG"
  [ "${PIPESTATUS[0]}" -eq 0 ] || die "build_ta_context.py 失败（上下文没生成 ⇒ 不许拿旧上下文做 TA）"

  say "6/16 TradingAgents 决策（llm env；MiniMax-M3.1-Flash / effort=max / max_tokens=32768）"
  [ -d "$TA_DIR" ] || say "WARN: 没有 $TA_DIR ⇒ 跳过 TradingAgents（最终计划仍可生成，只是没有 LLM 第二意见）"
  if [ -d "$TA_DIR" ] && [ -x "$LLM_PY" ]; then
    ( cd "$PROJ" && PYTHONPATH= "$LLM_PY" manu/etf_rotation/llm/run_tradingagents.py \
        --ticker 512800 --date "${ASOF:-2026-11-03}" --analysts news \
        --run-root "$OUT_DIR" ) 2>&1 | tail -20 | tee -a "$LOG"
    [ -f "$OUT_DIR/tradingagents_decision.md" ] || die "TradingAgents 没产出决策文件 ⇒ 不用旧决策凑数"
    # 🔴 F5（2026-11-04 21:0x E2E 首跑实测）：决策文件**存在** ≠ 决策**成功** ——
    #    run_tradingagents.py 失败时照样会写一份「**未完成**」占位 md
    #    ⇒ 旧判据（只看文件存在）会放行，占位文件被当成有效 LLM 意见一路流到最终计划。
    #    ⇒ 修法：以结构化件的 status 为权威口径，status!=ok 直接 die（宁可不产出，不产出假结果）。
    DEC_STATUS="$("$QL_PY" -c "import json,sys;print(json.load(open(sys.argv[1])).get('status',''))" \
        "$OUT_DIR/tradingagents_decision.json" 2>/dev/null)"
    [ "$DEC_STATUS" = "ok" ] || die "TradingAgents 本轮**未完成**（status=${DEC_STATUS:-无}）⇒ 不用失败占位凑数（见 $OUT_DIR/tradingagents_decision.md）"
    DECISION_ARG="$OUT_DIR/tradingagents_decision.md"
  fi
else
  say "5/16~6/16 跳过 TradingAgents（--skip-ta）"
  # 🔴 跳过 TA 时**绝不能**把上一轮遗留的决策混进本轮计划
  #    （2026-11-04 夜实测：--skip-ta 重跑后，最终计划里嵌的是 18:42 的旧 LLM 意见，
  #     而那轮 context 里的风险披露还是旧版 ⇒ 交付件混了两个时间点的来源）。
  DECISION_ARG="$OUT_DIR/.no_llm_decision.md"
  say "     ⇒ 跳过 TA ⇒ 最终计划**不嵌入任何 LLM 意见**（只保留规则层结论）"
fi

# ---- 7) 最终调仓计划 ------------------------------------------------------
say "7/16 合成最终调仓计划"
"$QL_PY" manu/etf_rotation/final_rebalance_plan.py \
  --report  "$OUT_DIR/etf_analysis_report.json" \
  --decision "$DECISION_ARG" \
  --out-dir "$OUT_DIR" 2>&1 | tail -30 | tee -a "$LOG"
[ "${PIPESTATUS[0]}" -eq 0 ] || die "final_rebalance_plan.py 失败（最终计划不可生成）"


# ---- 8) 回归自检（只读；口径漂移 / 账不平 / 决策过期 ⇒ 直接 FAIL 退出） ----
say "8/16 回归自检（基准 12.11% 探针 + 账户对账 + sha1 链 + 交付件一致性）"
"$QL_PY" manu/etf_rotation/verify_artifacts.py --run-root "$OUT_DIR" 2>&1 | tee -a "$LOG"
[ "${PIPESTATUS[0]}" -eq 0 ] || die "回归自检 FAIL ⇒ 产物不可交付（见 $OUT_DIR/verify_artifacts.log）"
# ---- 9) 算术自洽闸门（只读；M-H：计划权重 ≠ 实际落地权重 ⇒ 数字必须对得上） ----
say "9/16 算术自洽闸门（实际落地权益 / 整手取整缺口 / 价格基差 / 现金流恒等式）"
"$QL_PY" manu/etf_rotation/probes/probe_plan_arithmetic.py \
    --plan "$OUT_DIR/final_rebalance_plan.json" \
    --md   "$OUT_DIR/final_rebalance_plan.md" 2>&1 | tee -a "$LOG"
[ "${PIPESTATUS[0]}" -eq 0 ] || die "算术自洽 FAIL ⇒ 产物不可交付（见 $LOG）"
# ---- 10) 信号日/执行日闸门（只读；M-J：12-01 是**信号日**，执行日 = 12-02） ----
say "10/16 信号日 vs 执行日闸门（信号日 / 执行日 / 倒计时起算点）"
"$QL_PY" manu/etf_rotation/probes/probe_execution_date.py \
    --plan   "$OUT_DIR/final_rebalance_plan.json" \
    --md     "$OUT_DIR/final_rebalance_plan.md" \
    --report "$OUT_DIR/etf_analysis_report.json" 2>&1 | tee -a "$LOG"
[ "${PIPESTATUS[0]}" -eq 0 ] || die "信号日/执行日口径 FAIL ⇒ 产物不可交付（见 $LOG）"
# ---- 11) ETF 微观结构披露闸门（只读；M-K：风险披露必须带实测数字 + 指针不许指 §11.2） ----
say "11/16 ETF 微观结构披露闸门（折溢价 / 流动性分层 / 跟踪误差代理 + 独立重算比对）"
"$QL_PY" manu/etf_rotation/probes/probe_microstructure.py \
    --report "$OUT_DIR/etf_analysis_report.json" \
    --md     "$OUT_DIR/etf_analysis_report.md" 2>&1 | tee -a "$LOG"
[ "${PIPESTATUS[0]}" -eq 0 ] || die "微观结构披露闸门 FAIL ⇒ 产物不可交付（见 $LOG）"
# ---- 12/14) 前视审计闸门（只读；M-L：无未来函数台账 + W 快照前视 E 上界 + 排名稳健性） ----
# 🔴 本闸门守三件事（见 manu/etf_rotation/lookahead.py 与 probes/probe_lookahead.py）：
#    ① 输入日期台账里**决策前视恰 1 项**且必须是 W 成分快照（不许漏、也不许凑数）；
#    ② E 值与两档误差上界与**独立重算**逐只一致（不信任产物自述）；
#    ③ 排名稳健性「余量 ≤ 0 ⇒ 名单不变」的结论与文案必须**自洽**。
#    口径：**文件含未来行 ≠ 前视** —— 判据是「实际被消费的那部分数据的日期 > asof」。
say "12/16 前视审计闸门（无未来函数台账 + W 快照前视 E 上界 + 排名稳健性）"
"$QL_PY" manu/etf_rotation/probes/probe_lookahead.py \
    --report "$OUT_DIR/etf_analysis_report.json" \
    --md     "$OUT_DIR/etf_analysis_report.md" 2>&1 | tee -a "$LOG"
[ "${PIPESTATUS[0]}" -eq 0 ] || die "前视审计闸门 FAIL ⇒ 产物不可交付（见 $LOG）"
# ---- 13/14) LLM 层守卫探针（只读；守卫本身也必须**真的会拦人**） -------------
# 🔴 2026-11-05 01:3x（M-K 收尾）：本轮真缺陷就出在守卫上
#    （「当前权重」被伪造成目标值 ⇒ 语义守卫假阳性拦下交付）。
#    ⇒ 守卫必须**有自己的回归测试**：故意做坏的交付件必须被守卫 FAIL。
# 🔴 **适用性**：这两道闸门针对的是 **LLM 层语义**（方向-权重自洽 / 冲突表解析）。
#    `--skip-ta`（或 TA 没产出决策）⇒ 本轮**没有** LLM 层 ⇒ 探针**不适用**。
#    ⇒ 显式跳过并**明说「不适用 ≠ 通过」**，不许静默跳过，也不许把不适用记成 PASS。
if [ "$SKIP_TA" -eq 0 ] && [ -f "$OUT_DIR/tradingagents_decision.json" ] \
   && [ -f "$OUT_DIR/tradingagents_decision.md" ]; then
  say "13/16 守卫自检探针（语义守卫 / 可追溯守卫必须真的会拦人）"
  "$QL_PY" manu/etf_rotation/probes/probe_verify_guards.py \
      --run-root "$OUT_DIR" 2>&1 | tee -a "$LOG"
  _rc="${PIPESTATUS[0]}"
  # 🔴 2026-11-05 01:4x：退出码契约 0=通过 / 1=失败 / **2=不适用**
  #    ⇒ 2 既不是通过也不是失败 ⇒ 显式分流，**不许**把它报成「守卫已失效」。
  if [ "$_rc" -eq 2 ]; then
    say "     ⚠️ 守卫自检探针判定**不适用**（本轮无 LLM 决策层）⇒ **不适用 ≠ 通过**"
  elif [ "$_rc" -ne 0 ]; then
    die "守卫自检 FAIL ⇒ 守卫已失效（见 $LOG）"
  fi
  say "14/16 LLM 冲突表解析器回归探针（含「当前权重不得伪造」真负例）"
  "$QL_PY" manu/etf_rotation/probes/probe_llm_parser.py 2>&1 | tee -a "$LOG"
  [ "${PIPESTATUS[0]}" -eq 0 ] || die "解析器回归 FAIL ⇒ 冲突表不可信（见 $LOG）"
else
  say "13/16~14/16 **跳过** LLM 层守卫探针（--skip-ta 或本轮无 LLM 决策 ⇒ **不适用**；不适用 ≠ 通过）"
fi
# ---- 15/15) 信号日可行性闸门（只读；M-S2：守卫必须**真的会拦人**） -------------
# 🔴 M-S2（2026-11-05 06:3x 实测）：两个会让「到节点日跑流水线」白跑一小时 /
#    静默产出假结果的缺陷 —— ① `qlib_scores.py --end` 被 `SCORE_END_TIME` 静默截断；
#    ② 全链路**没有一处**判定「信号日打不打得出来」（12-01 既不在打分相位栅格上，
#    qlib 数据集也只到 11-03 ⇒ 白跑）。
#    ⇒ 这道闸门断言守卫仍然有效（含 4 个真负例）⇒ 修复不许被改回去。
say "15/16 信号日可行性闸门（打分日栅格 / qlib 数据集覆盖 / 真负例）"
"$QL_PY" manu/etf_rotation/probes/probe_signal_feasibility.py \
    --run-root "$OUT_DIR" 2>&1 | tee -a "$LOG"
[ "${PIPESTATUS[0]}" -eq 0 ] || die "信号日可行性闸门 FAIL ⇒ 节点日会白跑一小时或产出假结果（见 $LOG）"
# ---- 16/16) 节点日股数重算工具 · 闸门（只读；M-S3：口径必须与计划件一致） ----------
# 🔴 M-S3（2026-11-05 07:2x 实测）：「12-01 按当日收盘价重算整手股数」这句话
#    在交付件里写了很久，但**一直没有工具**⇒ 节点日那天只能靠人算。
#    ⇒ 新增 `reprice_node_day.py`（**只重算股数/金额，不改信号**）+ 本闸门
#    （整手 / 不超目标额 / 现金恒等式 / 价日期不许越界 / **目标权重逐条未变** / 3 个真负例）。
say "16/16 节点日股数重算工具闸门（整手 / 现金恒等式 / 权重未变 / 真负例）"
"$QL_PY" manu/etf_rotation/probes/probe_node_reprice.py \
    --run-root "$OUT_DIR" 2>&1 | tee -a "$LOG"
[ "${PIPESTATUS[0]}" -eq 0 ] || die "节点日重算工具闸门 FAIL ⇒ 节点日股数会算错或污染账户快照（见 $LOG）"
say "=== pipeline done === 产物：$OUT_DIR"
# 退出码修正（2026-11-04 21:42 实测）：--skip-ta 模式下 tradingagents_decision.md 根本不存在，
# 而 ls 只要有一个 operand 缺失就返回 2 ⇒ pipefail 把 2 传播成脚本退出码
# ⇒ 自检明明 20/20 PASS，脚本却 exit 2。修法：末尾 ls 不致命 + 显式 exit 0；
# 真正的失败路径走 die（exit 1），语义不受影响。
{
  ls -la "$OUT_DIR"/etf_analysis_report.* "$OUT_DIR"/final_rebalance_plan.* 2>/dev/null
  if [ "$SKIP_TA" -eq 0 ]; then ls -la "$OUT_DIR"/tradingagents_decision.md 2>/dev/null; fi
} | tee -a "$LOG" || true
exit 0
