#!/usr/bin/env python3
"""快照**派生件**「就地确定性重生成」工具（2026-11-11 实证后定型）。

为什么只重生成派生层（**不是**整链重跑）
--------------------------------------
整链重跑（`daily/main.py` 重跑分析层）**不可字节复现**：5 日 EWMA 平滑的
历史依赖会随「快照目录数」变化 ⇒ 同一份 stock_scores.csv 也可能排出不同的
``recommendation_sha1`` / ``report_content_sha1`` ⇒ 已经交付的 LLM 决策件的
sha1 链（``report_md_sha1`` → ``report_content_sha1`` → ``context_sha1``）
会**断链** ⇒ 闸门必红、交付件不可复现。

⇒ 本工具**只碰派生层**（分析层字节一律不动）：
    1. ``final_rebalance_plan.py``  → 调仓计划（纯函数：report + decision + prices + universe + account）
    2. ``reprice_node_day.py``      → 节点日重算（纯函数：plan + prices + account）
    3. ``build_execution_card``     → 执行卡（纯函数：node + plan + account）
    4. ``daily/report.py``          → 交付面报告（纯函数：上面三件 + 快照事实）
    5. ``daily/snapshot.py``        → manifest 指纹（size + sha1 重算）

🔴 **自验证（fail-closed）**：动手之前先用**旧产物**重渲一遍交付面报告，
   必须与快照里那份**逐字节相等**；不相等 ⇒ 说明「重建的 result 不忠实」
   ⇒ **立即中止，不落任何盘**（宁可不动，也不落一份不可信的交付件）。

用法
----
    python manu/tools/regen_snapshot_derived.py --snapshot-dir <快照目录> [--dry-run]
"""
from __future__ import annotations

import argparse
import json
import re
import subprocess
import sys
from pathlib import Path

_HERE = Path(__file__).resolve().parent
_REPO = _HERE.parent.parent
_RESEARCH = _REPO / "manu" / "etf_rotation"
_DAILY = _RESEARCH / "daily"
for _p in (str(_DAILY), str(_RESEARCH)):
    if _p not in sys.path:
        sys.path.insert(0, _p)

import account_state  # noqa: E402
import daily_config as DC  # noqa: E402
import snapshot as snap_mod  # noqa: E402
import report as report_mod  # noqa: E402
import agents_rebalance as AR  # noqa: E402

#: 交付面报告里「流水线备注」小节标题（解析旧报告以还原 notes）
_NOTES_HEADING = "### 流水线备注"


# --------------------------------------------------------------------- 小工具


def _read_json(path: Path) -> dict:
    return json.loads(Path(path).read_text(encoding="utf-8"))


def _read_text(path: Path) -> str:
    return Path(path).read_text(encoding="utf-8")


def _write_json(path: Path, payload: object) -> None:
    Path(path).write_text(
        json.dumps(payload, ensure_ascii=False, indent=2, default=str) + "\n",
        encoding="utf-8",
    )


def _notes_from_report(md_text: str) -> list[str]:
    """从**已落盘的交付面报告**里还原 ``result["notes"]``（渲染是 ``- {note}`` 逐条）。"""
    match = re.search(rf"^{re.escape(_NOTES_HEADING)}\s*$", md_text, re.M)
    if not match:
        return []
    tail = md_text[match.end():]
    tail = re.split(r"^## ", tail, maxsplit=1, flags=re.M)[0]
    out: list[str] = []
    for line in tail.splitlines():
        stripped = line.strip()
        if stripped.startswith("- "):
            out.append(stripped[2:])
    return out


def _generated_at_from_report(md_text: str) -> str | None:
    match = re.search(r"^- 报告生成时间：(.+)$", md_text, re.M)
    return match.group(1).strip() if match else None


# --------------------------------------------------------------- result 重建


def _resolve_analysis(snap_dir: Path, asof: str) -> dict:
    """**打分日 / 栅格末日 / 数据集末日** ⇒ 复用流水线自己的判定函数（不另造口径）。

    🔴 为什么不从报告 JSON 里猜：这三个值来自**qlib 打分栅格 + 数据集覆盖末日**
       （报告 JSON 里没有权威字段）⇒ 必须走 ``_resolve_analysis_asof``。
       读不到 ⇒ 回落报告里的 ``stock_scores.date``（只影响一行披露，不影响任何数字）。
    """
    try:
        analysis = AR._resolve_analysis_asof(
            signal_date=asof, scores_path=snap_dir / "stock_scores.csv")
    except Exception as exc:  # pragma: no cover - 环境缺 qlib 日历时才走
        print(f"⚠️ 打分栅格读不到（{type(exc).__name__}: {exc}）⇒ 回落报告内的分数日期")
        return {"analysis_asof": asof, "scores_max_date": None,
                "grid_last": None, "dataset_max_date": None}
    return analysis


def _build_result(*, snap_dir: Path, manifest: dict, plan: dict, node: dict,
                  card: dict, report_json: dict, report_md: str,
                  generated_at: str | None, analysis: dict) -> dict:
    """按 ``agents_rebalance.run_pipeline`` 的返回契约重建 ``result`` dict。

    🔴 字段来源全部是**快照内已有件**（不重新计算、不编数字）。
    """
    meta = report_json.get("meta") or {}
    scores = report_json.get("stock_scores") or {}
    asof = str(manifest.get("asof") or meta.get("asof") or "")
    notes = _notes_from_report(report_md)
    result = {
        "live_trade": bool(manifest.get("live_trade", False)),
        "asof": asof,
        "execution_date": card.get("execution_date"),
        "run_dir": str(snap_dir),
        "snapshot": {
            "root": str(manifest.get("snapshot_root") or snap_dir.parent),
            "path": str(snap_dir),
            "run_id": str(manifest.get("run_id") or snap_dir.name),
        },
        "report_json": str(snap_dir / "etf_analysis_report.json"),
        "plan_json": str(snap_dir / "final_rebalance_plan.json"),
        "node_json": str(snap_dir / f"node_reprice_{asof.replace('-', '')}.json"),
        "card_json": str(snap_dir / f"execution_card_{asof.replace('-', '')}.json"),
        "card": card,
        "decision_json": str(snap_dir / "tradingagents_decision.json"),
        "decision_md": str(snap_dir / "tradingagents_decision.md"),
        "scores_path": str(snap_dir / "stock_scores.csv"),
        "prices_path": str(snap_dir / f"etf_prices_effective_{asof.replace('-', '')}.csv"),
        "email_kind": "rebalance_card",
        "notes": notes,
        "holding_discipline": None,
        "analysis_asof": analysis.get("analysis_asof") or asof,
        "scores_max_date": analysis.get("scores_max_date") or scores.get("date"),
        "score_grid_last": analysis.get("grid_last"),
        "qlib_dataset_max_date": analysis.get("dataset_max_date"),
        "llm_decision_status": "ok",
        "execution_lag_days": int(DC.EXECUTION_LAG_DAYS),
        "node_reprice_ok": True,
        "node_reprice_error": "",
        "generated_at": generated_at or "",
        "preflight_only": False,
        "score_smoothing": report_json.get("score_smoothing") or {},
        "ta_screening": plan.get("ta_screening") or {},
        "skipped_switches": [],
    }
    return result


def _render(result: dict, *, account: dict, asof: str) -> str:
    return report_mod.render_report(result, account=account, asof=asof,
                                    live_trade=bool(result.get("live_trade")))


# --------------------------------------------------------------------- 主流程


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="快照派生件就地确定性重生成")
    parser.add_argument("--snapshot-dir", required=True, help="快照目录（绝对路径）")
    parser.add_argument("--dry-run", action="store_true",
                        help="只做自验证（用旧产物重渲报告并与盘上那份逐字节比对）")
    parser.add_argument("--accept-render-change", action="store_true",
                        help=("🔴 显式承认「**渲染层代码有意变更**」：自验证逐字节不等时"
                              "**不再中止**（仍打印差异摘要）⇒ 允许落盘。"
                              "只许在**确实改了渲染层**（如 daily/report.py）时使用；"
                              "误用会把「重建不忠实」的真事故放过去 ⇒ 用完必须"
                              "**不带该开关再跑一次**（此时自验证必须通过）。"))
    args = parser.parse_args(argv)

    snap_dir = Path(args.snapshot_dir).resolve()
    if not snap_dir.is_dir():
        print(f"❌ 快照目录不存在：{snap_dir}", file=sys.stderr)
        return 2

    manifest_path = snap_dir / snap_mod.MANIFEST_NAME
    manifest = _read_json(manifest_path)
    asof = str(manifest.get("asof") or "")
    stamp = asof.replace("-", "")
    report_json = _read_json(snap_dir / "etf_analysis_report.json")
    report_md_path = snap_dir / f"report_{stamp}.md"
    report_md = _read_text(report_md_path) if report_md_path.is_file() else ""
    plan = _read_json(snap_dir / "final_rebalance_plan.json")
    node = _read_json(snap_dir / f"node_reprice_{stamp}.json")
    card = _read_json(snap_dir / f"execution_card_{stamp}.json")
    account = account_state.account_summary()

    # ---- 步骤 0：自验证（用旧产物重渲 ⇒ 必须与盘上那份逐字节相等）---------
    analysis = _resolve_analysis(snap_dir, asof)
    old_result = _build_result(
        snap_dir=snap_dir, manifest=manifest, plan=plan, node=node, card=card,
        report_json=report_json, report_md=report_md,
        generated_at=_generated_at_from_report(report_md), analysis=analysis,
    )
    rebuilt = _render(old_result, account=account, asof=asof)
    if rebuilt != report_md:
        if not args.accept_render_change:
            print("❌ **自验证失败**：用旧产物重渲的交付面报告与盘上那份**不一致**"
                  " ⇒ 重建的 result 不忠实 ⇒ 中止（不落任何盘）。\n"
                  "   （若确系**渲染层代码有意变更**，请显式加 --accept-render-change "
                  "重跑，并在事后**不带该开关再跑一次**验证。）", file=sys.stderr)
            _diff_head(report_md, rebuilt)
            return 1
        print("⚠️  自验证逐字节不等，但已显式承认「渲染层代码有意变更」"
              "（--accept-render-change）⇒ **继续落盘**；差异摘要如下：")
        _diff_head(report_md, rebuilt)
    else:
        print(f"✅ 自验证通过：用旧产物重渲的交付面报告与盘上那份**逐字节相等**"
              f"（{len(report_md)} 字符）")

    if args.dry_run:
        print("🧪 --dry-run：自验证已过，未落任何盘。")
        return 0

    # ---- 步骤 1：调仓计划（纯函数）-----------------------------------------
    prices = snap_dir / f"etf_prices_effective_{stamp}.csv"
    universe = Path(DC.ETF_UNIVERSE_CSV)
    account_csv = snap_dir / "account_snapshot.csv"
    plan_cmd = [
        DC.QL_PY, str(_RESEARCH / "final_rebalance_plan.py"),
        "--report", str(snap_dir / "etf_analysis_report.json"),
        "--decision", str(snap_dir / "tradingagents_decision.md"),
        "--prices", str(prices),
        "--universe", str(universe),
        "--account", str(account_csv),
        "--out-dir", str(snap_dir),
    ]
    print("▶ 1/4 final_rebalance_plan.py …")
    _run(plan_cmd)

    # ---- 步骤 2：节点日重算（纯函数）---------------------------------------
    node_cmd = [
        DC.QL_PY, str(_RESEARCH / "reprice_node_day.py"),
        "--node-date", asof,
        "--plan", str(snap_dir / "final_rebalance_plan.json"),
        "--prices", str(prices),
        "--account", str(account_csv),
        "--cash", f"{DC.CASH}",
        "--total-assets", f"{DC.TOTAL_ASSETS}",
        "--total-assets-basis", "plan",
        "--out-dir", str(snap_dir),
    ]
    print("▶ 2/4 reprice_node_day.py …")
    _run(node_cmd)

    # ---- 步骤 3：执行卡 -----------------------------------------------------
    print("▶ 3/4 执行卡（build_execution_card）…")
    plan = _read_json(snap_dir / "final_rebalance_plan.json")
    node = _read_json(snap_dir / f"node_reprice_{stamp}.json")
    actions = node.get("actions") or []
    exec_date = (node.get("meta") or {}).get("execution_date") or card.get("execution_date")
    new_card = AR.build_execution_card(
        actions=actions,
        account=account,
        asof=asof,
        execution_date=exec_date,
        node_json=node,
        plan_json=plan,
        live_trade=bool(manifest.get("live_trade", False)),
        node_reprice_ok=True,
        node_reprice_error="",
    )
    card_path = AR.persist_execution_card(new_card, snap_dir, asof)
    card = _read_json(card_path)

    # ---- 步骤 4：交付面报告 + manifest 指纹 --------------------------------
    print("▶ 4/4 交付面报告 + manifest 指纹 …")
    new_result = _build_result(
        snap_dir=snap_dir, manifest=manifest, plan=plan, node=node, card=card,
        report_json=report_json, report_md=report_md,
        generated_at=_generated_at_from_report(report_md), analysis=analysis,
    )
    markdown = _render(new_result, account=account, asof=asof)
    report_md_path.write_text(markdown, encoding="utf-8")

    snap = snap_mod.Snapshot(snap_dir.parent, asof=asof,
                             run_id=str(manifest.get("run_id") or snap_dir.name),
                             live_trade=bool(manifest.get("live_trade", False)))
    snap._stages = list(manifest.get("stages") or [])
    snap._skipped = list(manifest.get("skipped") or [])
    snap._notes = list(manifest.get("notes") or [])
    snap.finalize()

    # ---- 摘要 --------------------------------------------------------------
    print("\n===== 重生成摘要 =====")
    print(f"快照目录：{snap_dir}")
    print(f"ta_screening.rejected = {plan.get('ta_screening', {}).get('rejected')}")
    for act in plan.get("actions") or []:
        print(f"  · {act.get('code')} {act.get('name')} | 方向={act.get('direction')}"
              f" | 目标={act.get('target_pct')}% | 动作={act.get('action')}")
    cash = (plan.get("cash_flow") or {})
    print(f"现金预留比例 = {cash.get('cash_reserve_ratio')}")
    print(f"执行卡订单数 = {len(card.get('orders') or [])}｜挂单禁用 = {card.get('disabled')}")
    print("✅ 派生层重生成完成（分析层字节未动）")
    return 0


def _run(cmd: list[str]) -> None:
    proc = subprocess.run(cmd, capture_output=True, text=True)
    if proc.returncode != 0:
        print(proc.stdout[-4000:], file=sys.stderr)
        print(proc.stderr[-4000:], file=sys.stderr)
        raise SystemExit(f"❌ 子命令失败（rc={proc.returncode}）：{' '.join(cmd)}")
    tail = (proc.stdout or "").strip().splitlines()[-3:]
    for line in tail:
        print(f"    {line}")


def _diff_head(old: str, new: str) -> None:
    old_lines = old.splitlines()
    new_lines = new.splitlines()
    print("--- 首个差异位置 ---", file=sys.stderr)
    for i in range(max(len(old_lines), len(new_lines))):
        a = old_lines[i] if i < len(old_lines) else "<缺行>"
        b = new_lines[i] if i < len(new_lines) else "<缺行>"
        if a != b:
            print(f"  行 {i + 1}", file=sys.stderr)
            print(f"    盘上：{a[:220]}", file=sys.stderr)
            print(f"    重渲：{b[:220]}", file=sys.stderr)
            break


if __name__ == "__main__":
    raise SystemExit(main())
