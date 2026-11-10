#!/usr/bin/env python
"""研究链 · **账户事实源导出器**（把账户口径落到**交付目录**里）。

【为什么必须有这个件（2026-11-10 真缺陷）】
`verify_artifacts.py`（8/16 回归自检）与 `probes/probe_report_spec.py`（H1）
都改成「**期望值只认交付件同目录的账户事实源**」：
`account_context.json`（账户事实源）→ 同目录 `account_snapshot.csv` 回落 →
**都没有 ⇒ FAIL（不许真空 PASS）**。

每日流水线（`daily/agents_rebalance.py`）本来就会把实盘账户口径写进快照目录
（`account_state.write_snapshot_csv()` + `write_account_json()`）⇒ 自洽。

🔴 **但研究链（`run_pipeline.sh`）从来没有把账户事实源写进交付目录** ⇒
`verify_artifacts.py` 的「账户对账」与「持仓诊断逐只齐全」两项**必然红**
（2026-11-10 服务器实跑实测：24 项里 FAIL 2 ⇒ 8/16 直接 `die` ⇒ 整条研究链 rc=1）。
⇒ 本脚本补上这一环：**研究链的交付目录也必须自带账户事实源**。

【口径纪律（与每日链严格对齐）】
* 期望值（现金 / 总资产 / 持仓集合）的**唯一来源** = **本链实际使用的那份账户基准**：
  - 研究链 ⇒ `manu/etf_rotation/account.py`（2026-11-04 口径：现金 15,518.87 元 /
    总资产 29,854.67 元 / 5 只持仓）
  - 每日链 ⇒ `manu/etf_rotation/daily/account_state.py`（2026-11-05 口径：1 只 512800）
  ⇒ **两链各自导出自己那一份**，绝不允许互相串味（串味 ⇒ 交付件混入无源结论）。
* 导出前跑**三道自校验**（与 `account.py::load_account_snapshot` 同口径）：
  ① 每行 `market_value == shares × price`；② `Σmarket_value + cash == total_assets`；
  ③ 持仓代码集合非空。
  ⇒ 任一不过 ⇒ **抛异常退出（退出码 2）**，绝不带病产物。

【用法】
```bash
python manu/etf_rotation/emit_account_context.py --out-dir <交付目录>
```
产物（写在 `<交付目录>` 里，与报告 / 决策 / 计划同目录）：
* `account_snapshot.csv`  —— 逐只持仓的原始口径（字节级复制基准 CSV）
* `account_context.json`  —— 账户事实源（`cash` / `total_assets` / `positions` / …）
"""
from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

_HERE = Path(__file__).resolve().parent
if str(_HERE) not in sys.path:
    sys.path.insert(0, str(_HERE))

import account  # noqa: E402  —— 研究链的账户基准（单一事实源）

#: 算术恒等式容差（元）
_TOLERANCE = 0.01
#: 事实源文件名（与 `verify_artifacts.py` / `probe_report_spec.py` 的常量一致）
ACCOUNT_CONTEXT_NAME = "account_context.json"
ACCOUNT_SNAPSHOT_NAME = "account_snapshot.csv"


def build_context(snapshot_path: str | Path | None = None) -> tuple[dict, Path]:
    """构造账户事实源 payload，并返回 (payload, 基准 CSV 路径)。"""
    source = Path(snapshot_path) if snapshot_path else Path(account.ACCOUNT_SNAPSHOT_CSV)
    df = account.load_account_snapshot(source)
    summary = account.account_summary(df)
    positions: list[dict] = []
    for record in df.to_dict(orient="records"):
        positions.append(
            {
                "code": str(record["code"]).zfill(6),
                "name": str(record.get("name") or ""),
                "shares": float(record["shares"]),
                "price": float(record["price"]),
                "market_value": float(record["market_value"]),
                "cost": float(record["cost"]),
                "pnl": float(record["pnl"]),
                "pnl_pct": float(record["pnl_pct"]),
            }
        )
    # ---- 自校验（三道）：任一不过 ⇒ 抛异常，绝不带病产物 --------------------
    if not positions:
        raise ValueError("账户基准里没有持仓 ⇒ 事实源不可导出（不许写空事实源）")
    worst = 0.0
    for item in positions:
        worst = max(worst, abs(item["shares"] * item["price"] - item["market_value"]))
    if worst >= _TOLERANCE:
        raise ValueError(f"账户基准不自洽：股数×现价 最大偏差 {worst:.4f} 元 ≥ {_TOLERANCE}")
    total_mv = sum(item["market_value"] for item in positions)
    diff = abs(total_mv + float(summary["cash"]) - float(summary["total_assets"]))
    if diff >= _TOLERANCE:
        raise ValueError(
            f"账户基准不自洽：Σ市值 {total_mv:.2f} + 现金 {summary['cash']:.2f} "
            f"≠ 总资产 {summary['total_assets']:.2f}（差 {diff:.4f}）"
        )
    payload = {
        "source": "manu/etf_rotation/account.py",
        "snapshot_date": account.ACCOUNT_SNAPSHOT_DATE,
        "currency": "CNY",
        "total_assets": float(summary["total_assets"]),
        "cash": float(summary["cash"]),
        "market_value": float(summary["market_value"]),
        "n_positions": int(summary["n_positions"]),
        "positions": positions,
        "cleared_positions": [],
        "target_positions": int(summary["target_positions"]),
        "target_position_weight": float(summary["target_position_weight"]),
        "target_position_amount": float(summary["target_position_amount"]),
    }
    return payload, source


def emit(out_dir: str | Path, snapshot_path: str | Path | None = None) -> tuple[Path, Path]:
    """把账户事实源写进 ``out_dir``；返回 (context_json, snapshot_csv)。"""
    out = Path(out_dir)
    out.mkdir(parents=True, exist_ok=True)
    payload, source = build_context(snapshot_path)
    csv_out = out / ACCOUNT_SNAPSHOT_NAME
    json_out = out / ACCOUNT_CONTEXT_NAME
    #: 账户快照 CSV：**字节级复制**基准 CSV（保证与报告用的是同一份原始口径）
    csv_out.write_bytes(Path(source).read_bytes())
    json_out.write_text(
        json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8"
    )
    return json_out, csv_out


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        description="导出研究链的账户事实源（account_context.json + account_snapshot.csv）"
    )
    parser.add_argument("--out-dir", required=True, help="交付目录（报告/决策/计划同目录）")
    parser.add_argument(
        "--account-snapshot",
        default=None,
        help="账户基准 CSV（缺省 = 研究链 account.ACCOUNT_SNAPSHOT_CSV）",
    )
    args = parser.parse_args(argv)
    try:
        json_out, csv_out = emit(args.out_dir, args.account_snapshot)
    except Exception as exc:  # noqa: BLE001 —— 事实源不可导出必须报出来，不许静默
        print(f"[FAIL] 账户事实源导出失败：{type(exc).__name__}: {exc}")
        return 2
    print(f"[PASS] 账户事实源已写出：{json_out}")
    print(f"[PASS] 账户快照已写出：{csv_out}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
