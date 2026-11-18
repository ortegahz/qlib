#!/usr/bin/env bash
# 🔴 2026-11-11（D-2 修复）：闸门「断言对象（交付件根）」的**选取逻辑**（可独立测试）
#
# 用法：  bash select_artifact_root.sh <snapshot_root> <canonical_run_root>
# 输出：  第一行 = 选中的交付件根目录
#         第二行 = 选取说明 NOTE
#
# 选取规则（**两级**，判据不许为刷绿而放宽）：
#   1) **LLM 完整快照**（优先）：目录里同时有
#        snapshot_manifest.json + etf_analysis_report.json + etf_analysis_report.md
#        + ta_research_context.md + ta_context_meta.json + tradingagents_decision.json
#   2) **基础完整快照**（回落）：只有 manifest + report.json + report.md
#        ⚠️ 这种快照**没有 LLM 三件套**（典型来源：`--skip-llm` 复跑）
#        ⇒ LLM 类阶段（verify_artifacts / probe_llm_stage_repro）会如实判红 / 判不适用
#        ⇒ **这是正确语义**（链不可校验 ≠ 通过）
#   3) 都找不到 ⇒ 回落 canonical，并**显式打印**（不许静默）
#
# 目录名按 `YYYYMMDD_HHMMSS`（字典序 == 时间序）**倒序** ⇒ 第一个命中的就是最新的。

_sel_is_full() {
  [ -f "$1/snapshot_manifest.json" ] && [ -f "$1/etf_analysis_report.json" ] \
    && [ -f "$1/etf_analysis_report.md" ] \
    && [ -f "$1/ta_research_context.md" ] && [ -f "$1/ta_context_meta.json" ] \
    && [ -f "$1/tradingagents_decision.json" ]
}

_sel_is_basic() {
  [ -f "$1/snapshot_manifest.json" ] && [ -f "$1/etf_analysis_report.json" ] \
    && [ -f "$1/etf_analysis_report.md" ]
}

_sel_snapshot_list() {
  local _d
  for _d in "$1"/*/; do
    [ -d "$_d" ] || continue
    printf '%s\n' "${_d%/}"
  done | LC_ALL=C sort -r
}

select_artifact_root() {
  local snapshot_root="$1" canonical_root="$2"
  local _root="" _note="" _d
  if [ ! -d "$snapshot_root" ]; then
    printf '%s\n%s\n' "$canonical_root" "未找到快照根（$snapshot_root 不存在）⇒ 回落 canonical"
    return 0
  fi
  while IFS= read -r _d; do
    [ -n "$_d" ] || continue
    if _sel_is_full "$_d"; then _root="$_d"; break; fi
  done < <(_sel_snapshot_list "$snapshot_root")
  if [ -n "$_root" ]; then
    _note="缺省 = 最新 **LLM 完整**快照（含三件套）"
  else
    while IFS= read -r _d; do
      [ -n "$_d" ] || continue
      if _sel_is_basic "$_d"; then _root="$_d"; break; fi
    done < <(_sel_snapshot_list "$snapshot_root")
    if [ -n "$_root" ]; then
      _note="⚠️ 无 LLM 完整快照 ⇒ 回落 = 最新**基础完整**快照（缺三件套，LLM 类阶段将如实判红）"
    fi
  fi
  if [ -z "$_root" ]; then
    _root="$canonical_root"
    _note="未找到完整快照 ⇒ 回落 canonical"
  fi
  printf '%s\n%s\n' "$_root" "$_note"
}

# 直接执行（而非被 source）时，打印结果
if [ "${BASH_SOURCE[0]}" = "${0}" ]; then
  select_artifact_root "$1" "$2"
fi
