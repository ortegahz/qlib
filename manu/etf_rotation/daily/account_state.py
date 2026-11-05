"""account_state.py —— **每日流水线的实盘账户基准**（单一事实源，2026-11-05 用户口径）。

【为什么与 `manu/etf_rotation/account.py` 分开】
`manu/etf_rotation/account.py` 是**研究链**的账户基准（2026-11-04 持仓截图，29,854.67 元），
它的常量被写进了已归档的交付件（`etf_analysis_report.json` 等）与指纹表
⇒ **改它会让研究链的可复现性闸门假性失配**（铁律〇之三：只做用户交代的事，不动无关基准）。

本模块是**每日流水线**注入的实盘基准（2026-11-05 用户口径）：

| 项 | 值 | 来源 |
|:--|--:|:--|
| 总资产 | **30,035.57 元** | 用户原话 |
| 可用现金 | **23,367.37 元** | 用户原话 |
| ETF 持仓市值 | **6,668.20 元** | 用户原话（= 总资产 − 现金，可对账） |
| 512800 银行ETF华宝 | **7,700 股**，成本 **0.8239 元/股** | 用户原话 |
| 159929 / 512690 / 513330 / 513770 | **已清仓归零** | 用户原话 |

【🔴 用户原话里有一处内部不自洽 —— 本模块按「可对账口径」取值并显式披露】
用户原话同时给了：市值 6,668.20 元、现价 **0.865** 元、浮盈 **+316.32 元 / +4.99%**。
- 按现价 0.865 算：市值 = 7,700 × 0.865 = **6,660.50** 元 ⇒ 总资产 = 23,367.37 + 6,660.50
  = **30,027.87** 元，与用户给的 **30,035.57** 元差 **7.70 元**（= 7,700 股 × 0.001 元）。
- 按市值 6,668.20 反推：现价 = 6,668.20 / 7,700 = **0.866** 元（整除，无残差）
  ⇒ 浮盈 = 6,668.20 − 6,344.03 = **+324.17 元**（+5.11%）。

⇒ 本模块取**可对账口径**（总资产 = 现金 + 市值 必须精确成立）：
现价 **0.866** / 浮盈 **+324.17 元 / +5.11%**。
这 7.70 元差额**不猜、不抹平**，写进 `DISCREPANCY_NOTE`，每次邮件的资产卡片都披露。

【口径纪律】
- 本模块**只读用户给的数**，不做任何行情推算（现价 0.866 是由「市值 ÷ 股数」反推，不是行情）；
- 每日行情刷新后，**市值按新收盘价重算**（那是 `agents_rebalance.py` 的活），本模块只管**基准**；
- 三道校验（与研究链 `account.py` 同口径）：① 每行 `market_value == shares × price`；
  ② `Σmarket_value + CASH == TOTAL_ASSETS`；③ `cost / pnl` 与 `pnl_pct` 反推自洽。
"""
from __future__ import annotations

import json
import sys
from pathlib import Path

import pandas as pd

_HERE = Path(__file__).resolve().parent
if str(_HERE) not in sys.path:
    sys.path.insert(0, str(_HERE))

import daily_config as DC  # noqa: E402

#: 对账容差（元）
_TOLERANCE = 0.01
#: cost/pnl 反推自洽容差（元）
_DERIVED_TOLERANCE = 0.02

#: 用户原话里的内部不自洽（本模块按可对账口径取值时的披露文本）
DISCREPANCY_NOTE = (
    "⚠️ 账户口径披露：用户提供的口径里，现价 0.865 元与市值 6,668.20 元"
    "**相差 7.70 元**（7,700 股 × 0.001 元）——按 0.865 元算总资产为 30,027.87 元，"
    "与给出的 30,035.57 元不符。本流水线取**可对账口径**"
    "（总资产 30,035.57 = 现金 23,367.37 + 市值 6,668.20，反推现价 0.866 元、"
    "浮盈 +324.17 元 / +5.11%）。该 7.70 元差额**未做任何抹平**，在此显式披露。"
)

#: 基准持仓（已清仓标的**不列** —— 归零即不存在）
BASE_POSITIONS: tuple[dict, ...] = (
    {
        "code": DC.PRIMARY_HOLDING_CODE,
        "name": DC.PRIMARY_HOLDING_NAME,
        "shares": DC.PRIMARY_HOLDING_SHARES,
        "cost_price": DC.PRIMARY_HOLDING_COST,
    },
)

#: 已清仓归零的标的（保留名单 ⇒ 报告里能说清「这几只已经清掉了」）
CLEARED_POSITIONS: tuple[dict, ...] = (
    {"code": "159929", "name": "医药ETF"},
    {"code": "512690", "name": "酒ETF"},
    {"code": "513330", "name": "恒生互联网ETF"},
    {"code": "513770", "name": "港股互联网ETF"},
)


def derive_positions() -> pd.DataFrame:
    """按基准推导持仓表（**不联网、不读行情**）。

    返回列：`code, name, shares, price, market_value, pnl_pct, cost, pnl`。

    - `price` = `MARKET_VALUE / shares`（可对账口径 ⇒ 精确）；
    - `cost` = `shares × cost_price`（成本价是用户原话，不推算）；
    - `pnl` = `market_value − cost`；`pnl_pct` = `pnl / cost`。
    """
    rows: list[dict] = []
    for pos in BASE_POSITIONS:
        shares = float(pos["shares"])
        cost_price = float(pos["cost_price"])
        cost = shares * cost_price
        market_value = shares * (DC.MARKET_VALUE / shares) if shares else 0.0
        pnl = market_value - cost
        rows.append(
            {
                "code": str(pos["code"]),
                "name": str(pos["name"]),
                "shares": int(shares),
                "price": round(market_value / shares, 6) if shares else 0.0,
                "market_value": round(market_value, 2),
                "pnl_pct": round(pnl / cost, 6) if cost else 0.0,
                "cost": round(cost, 2),
                "pnl": round(pnl, 2),
            }
        )
    return pd.DataFrame(rows)


def validate_snapshot(df: pd.DataFrame) -> None:
    """三道校验（任一不过 ⇒ **抛异常**，不许静默通过）。"""
    if df.empty:
        raise ValueError("账户快照为空 ⇒ 实盘基准不可用，拒绝继续")
    required = ("code", "shares", "price", "market_value", "pnl_pct", "cost", "pnl")
    missing = [c for c in required if c not in df.columns]
    if missing:
        raise ValueError(f"账户快照缺少必需列：{missing}")

    mv_diff = (df["market_value"] - df["shares"] * df["price"]).abs()
    bad = df.loc[mv_diff >= _TOLERANCE]
    if not bad.empty:
        detail = ", ".join(
            f"{r['code']}(市值 {r['market_value']:.2f} ≠ 股数×现价 {r['shares'] * r['price']:.2f})"
            for _, r in bad.iterrows()
        )
        raise ValueError(f"账户快照市值与「股数 × 现价」不符（容差 {_TOLERANCE} 元）：{detail}")

    total_mv = float(df["market_value"].sum())
    diff = total_mv + DC.CASH - DC.TOTAL_ASSETS
    if abs(diff) >= _TOLERANCE:
        raise ValueError(
            f"账户对账失败：持仓市值合计 {total_mv:.2f} + 现金 {DC.CASH:.2f} = "
            f"{total_mv + DC.CASH:.2f}，与总资产 {DC.TOTAL_ASSETS:.2f} 偏差 {diff:+.2f} 元"
        )

    if bool((df["pnl_pct"] <= -1.0).any()):
        raise ValueError("账户快照存在盈亏率 ≤ -100% 的行（成本为负，无意义）")
    derived_cost = df["market_value"] / (1.0 + df["pnl_pct"])
    derived_pnl = df["market_value"] - derived_cost
    bad_cost = df.loc[(df["cost"] - derived_cost).abs() >= _DERIVED_TOLERANCE]
    if not bad_cost.empty:
        detail = ", ".join(
            f"{r['code']}(CSV cost {r['cost']:.2f} ≠ 反推 {derived_cost.loc[i]:.2f})"
            for i, (_, r) in enumerate(bad_cost.iterrows())
        )
        raise ValueError(f"账户快照 cost 与「市值 / 盈亏率」不自洽：{detail}")
    bad_pnl = df.loc[(df["pnl"] - derived_pnl).abs() >= _DERIVED_TOLERANCE]
    if not bad_pnl.empty:
        detail = ", ".join(
            f"{r['code']}(CSV pnl {r['pnl']:.2f} ≠ 反推 {derived_pnl.loc[i]:.2f})"
            for i, (_, r) in enumerate(bad_pnl.iterrows())
        )
        raise ValueError(f"账户快照 pnl 与「市值 / 盈亏率」不自洽：{detail}")


def load_snapshot() -> pd.DataFrame:
    """返回已校验的实盘持仓表。"""
    df = derive_positions()
    validate_snapshot(df)
    return df


def write_snapshot_csv(path: str | Path) -> Path:
    """把实盘基准写成账户快照 CSV（供 `analyze.py --account-snapshot` /
    `final_rebalance_plan.py --account` 消费）。

    🔴 **每次运行都重新生成** ⇒ 基准只有一个真值（本模块），不会与仓库里的 CSV 分叉。
    """
    out = Path(path)
    out.parent.mkdir(parents=True, exist_ok=True)
    df = load_snapshot()
    df.to_csv(out, index=False, encoding="utf-8-sig", lineterminator="\r\n")
    return out


def account_summary() -> dict:
    """实盘账户总览（数字全部来自本模块的基准 + 已校验的持仓表）。"""
    df = load_snapshot()
    market_value = float(df["market_value"].sum())
    total_cost = float(df["cost"].sum())
    total_pnl = float(df["pnl"].sum())
    return {
        "snapshot_date": DC.ACCOUNT_SNAPSHOT_DATE,
        "total_assets": DC.TOTAL_ASSETS,
        "cash": DC.CASH,
        "market_value": market_value,
        "cash_weight": DC.CASH / DC.TOTAL_ASSETS,
        "market_value_weight": market_value / DC.TOTAL_ASSETS,
        "n_positions": int(len(df)),
        "total_cost": total_cost,
        "total_pnl": total_pnl,
        "total_pnl_pct": total_pnl / total_cost if total_cost else float("nan"),
        "positions": df.to_dict(orient="records"),
        "cleared_positions": [dict(p) for p in CLEARED_POSITIONS],
        "discrepancy_note": DISCREPANCY_NOTE,
    }


def write_account_json(path: str | Path) -> Path:
    """把**实盘账户口径**导出成 JSON（阶段 4 喂给 TradingAgents 的 Portfolio Manager）。

    🔴 为什么必须有这个件（2026-11-05 正确性 bug）：
    `run_tradingagents.py` 默认读**研究链**的 `manu/etf_rotation/account.py`
    （2026-11-04 快照：现金 15,518.87 元 / 5 只持仓）
    ⇒ Portfolio Manager 拿的是**用户根本没持有的组合** ⇒ 交付件混入无源结论
    （历史实测编出过「目标 15%」「止损 0.804」这类假论据）。
    ⇒ 每日流水线在阶段 4 之前把**实盘口径**（本模块）导出成 JSON，
    再用 `--account-json` 传给 TA ⇒ TA 拿到的与模式 A 报告里的账户卡片**完全一致**。

    JSON 字段（**必含** 现金 / 总资产 / 持仓（代码 · 股数 · 成本））：
        `cash` / `total_assets` / `positions[{code, name, shares, cost, ...}]`
        / `cleared_positions`（已清仓名单）/ `snapshot_date` / `source`。
    """
    out = Path(path)
    out.parent.mkdir(parents=True, exist_ok=True)
    summary = account_summary()
    payload = {
        "source": "manu/etf_rotation/daily/account_state.py",
        "snapshot_date": summary["snapshot_date"],
        "currency": "CNY",
        "total_assets": summary["total_assets"],
        "cash": summary["cash"],
        "market_value": summary["market_value"],
        "positions": summary["positions"],
        "cleared_positions": summary["cleared_positions"],
        "discrepancy_note": summary["discrepancy_note"],
    }
    out.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")
    return out


if __name__ == "__main__":  # pragma: no cover - 手工自检
    print(json.dumps(account_summary(), ensure_ascii=False, indent=2))
