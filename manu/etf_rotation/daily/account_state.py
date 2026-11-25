"""account_state.py —— **每日流水线的实盘账户基准**（单一事实源，2026-11-23 用户口径）。

【为什么与 `manu/etf_rotation/account.py` 分开】
`manu/etf_rotation/account.py` 是**研究链**的账户基准（2026-11-04 持仓截图，29,854.67 元），
它的常量被写进了已归档的交付件（`etf_analysis_report.json` 等）与指纹表
⇒ **改它会让研究链的可复现性闸门假性失配**（铁律〇之三：只做用户交代的事，不动无关基准）。

本模块是**每日流水线**注入的实盘基准（2026-11-23 用户口径）：

| 项 | 值 | 来源 |
|:--|--:|:--|
| 总资产 | **30,425.00 元** | 用户原话（11-23 收盘后券商首页截图，截图时刻 15:41） |
| 可用现金 | **23,371.80 元** | 用户原话（首页「可用」） |
| ETF 持仓市值 | **7,053.20 元** | 用户原话（首页「证券市值」＝ 总资产 − 现金，可对账） |
| 512800 银行ETF华宝 | **7,700 股**，成本 **0.8239 元/股** | 用户原话（持仓行） |
| 159929 / 512690 / 513330 / 513770 | **已清仓归零** | 用户原话（11-23 截图未列该名单 ⇒ 沿用 11-20 口径，持仓未变） |

【🔴 本次口径（2026-11-23）是「单一现价」口径 ⇒ 三项金额可精确对账】
用户 2026-11-23 收盘后给了**一张**券商首页截图，同一时点**只有一个**现价口径：
- 持仓行：现价 **0.916** 元 ⇒ 市值 7,700 × 0.916 = **7,053.20 元**（与首页「证券市值」**逐字一致**）；
- 首页「总资产」**30,425.00 元** ＝ 可用现金 **23,371.80 元** ＋ 证券市值 **7,053.20 元**（**精确成立**）；
- 证券仓位 **23.18%** ＝ 7,053.20 / 30,425.00 ✓；
- 当日盈亏 **−77.00 元（−0.25%）** ＝ 7,700 × (0.916 − 0.926) ⇒ **反推出 11-20 收盘价 0.926 元**
  （与 11-20 记录里的收盘价 0.926 **一致** ⇒ 跨日可对账）。

⇒ 🔴 **11-20 那份「金额／现价」披露随本次换口径整体作废**（不是被抹平，是被**新的一份自洽快照取代**）；
旧口径 30,501.07 / 23,370.87 / 7,130.20 **不再使用**。
🟡 **持仓名单本身不作废**：11-23 截图只列了 512800 一行，未列已清仓四只 ⇒ 清仓名单沿用 11-20 口径（持仓未变）。

【🔴 本次仍有一处（很小）的口径差 —— 如实披露，不抹平】
- 券商「持仓盈亏」显示 **+709.02 元（+11.18%）**；本模块按「股数 × 用户原话成本价 0.8239」推得
  7,053.20 − 6,344.03 = **+709.17 元（+11.18%）** ⇒ 差 **0.15 元**；
- 根因：券商真实成本价 ≈ 6,344.18 / 7,700 = **0.8239195**，界面按**四位小数**显示 0.8239 ⇒ 成本额被舍入
  ⇒ **不猜、不抹平**，本模块按用户原话成本价 0.8239 记账，差额在 `DISCREPANCY_NOTE` 里显式披露。
- 另：现金比 11-20 口径 **+0.93 元**（23,370.87 → 23,371.80），券商未说明原因（可能是零钱结息/费用结算）
  ⇒ **只记差异、不猜原因**。
- 另：用户截图的「11 月资产盈亏 +801.60 元」本模块**不用于记账**（口径不同：那是券商的月度口径，
  含本月已实现盈亏），只作为原话留档。

【口径纪律】
- 本模块**只读用户给的数**，不做任何行情推算（现价 0.916 是由「市值 ÷ 股数」反推，不是行情）；
- 每日行情刷新后，**市值按新收盘价重算**（那是 `agents_rebalance.py` 的活），本模块只管**基准**；
- 三道校验（与研究链 `account.py` 同口径）：① 每行 `market_value == shares * price`；
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

#: 用户原话与本模块记账口径的差异（**如实披露，不抹平**）
DISCREPANCY_NOTE = (
    "⚠️ 账户口径披露（2026-11-23 收盘后用户券商首页截图，截图时刻 15:41）："
    "**单一现价口径** —— 持仓行现价 **0.916 元**（7,700 股 ⇒ 市值 **7,053.20 元**）与首页「证券市值」"
    "**7,053.20 元**逐字一致；首页「总资产」**30,425.00 元** ＝ 可用现金 **23,371.80 元** ＋ "
    "证券市值 **7,053.20 元**（精确成立）；证券仓位实算 23.1823%（券商显示 23.18%，一致）；"
    "当日盈亏 −77.00 元（−0.25%）＝ "
    "7,700 × (0.916 − 0.926)，与 11-20 收盘价 0.926 一致 ⇒ 跨日可对账。"
    "本流水线取这套**可精确对账**的口径。"
    "**唯一残差**：券商「持仓盈亏」显示 +709.02 元（+11.18%），本模块按用户原话成本价 0.8239 推得 "
    "**+709.17 元**（+11.18%），差 **0.15 元** —— 根因是券商真实成本价 ≈ 0.8239195、界面按四位小数显示 0.8239，"
    "成本额被舍入。该 0.15 元差额**未做抹平**。"
    "另：现金比 11-20 口径 +0.93 元（23,370.87 → 23,371.80），券商未说明原因 ⇒ **只记差异、不猜原因**。"
    "另：用户截图「11 月资产盈亏 +801.60 元」本模块**不用于记账**（券商月度口径，含已实现盈亏），只留档。"
    "⚠️ **2026-11-20 那份「金额／现价」披露随本次换口径整体作废**"
    "（被本份自洽快照取代，不是被抹平）；**持仓名单不作废**（11-23 截图未列已清仓四只 ⇒ 沿用 11-20 口径）。"
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
    再用 `--account-json` 传给 TA ⇒ TA 拿到的与巡检报告里的账户卡片**完全一致**。

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
