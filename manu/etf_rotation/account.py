"""ETF 轮动线 · 用户**真实账户**口径（单一事实源）。

【为什么有这一个模块】
`config.ETF_ACCOUNT` 早期是「20 万假想资金」，只是凑一个像样的账户量级；
但用户是**小资金人工下单**，真实账户只有 3 万量级 ⇒ 换手金额、佣金下限、
单笔能执行的股数（ETF 最小 100 份）都必须按真实账户算，否则「买入 8,200 元」
这种建议在真实账户里根本执行不了。

【本模块的口径（全部来自用户本人提供的持仓截图，2026-11-04，不许改数）】
- 总资产 = 现金 + 持仓市值 ⇒ 两边必须对账，偏差 ≥ 0.01 元**直接抛异常**（不许静默通过）；
- 目标结构：**5 只等权**，每只占总资产 **20%**（≈ 5,970 元）；
- 持仓/成本/盈亏落在 `data/account_snapshot.csv`（UTF-8 BOM，含表头）。

【与 `config.py` 的关系】
`config.ETF_ACCOUNT` 必须与本模块的 `TOTAL_ASSETS` 保持一致（2026-11-04 已同步）；
本模块**不 import config**（保持独立、可单测），同步关系靠文档 + 下面的常量声明。

【用法】
    from account import account_summary, current_weights
    account_summary()      # 账户总览：总资产 / 现金 / 市值 / 浮亏 / 目标结构
    current_weights()      # {code: 占总资产权重} ⇒ 可直接喂给 recommend.py --holdings
"""
from __future__ import annotations

from pathlib import Path

import pandas as pd

# ---------------------------------------------------------------- 真实账户口径（单一事实源）
#: 总资产（元）= 现金 15,518.87 + 持仓市值 14,335.80
TOTAL_ASSETS = 29_854.67
#: 现金（元）
CASH = 15_518.87
#: 目标持仓只数（等权 5 只）
TARGET_POSITIONS = 5
#: 每只标的占总资产的目标权重（5 × 20% = 100%）
TARGET_POSITION_WEIGHT = 0.20
#: 每只标的的目标金额（元）：TOTAL_ASSETS × TARGET_POSITION_WEIGHT = 5,970.93 ⇒ 取整 5,970.0
TARGET_POSITION_AMOUNT = 5_970.0
#: 账户快照 CSV（用户真实持仓，2026-11-04）
ACCOUNT_SNAPSHOT_CSV = Path(__file__).resolve().parent / "data" / "account_snapshot.csv"
#: **账户快照日**（用户持仓截图日）—— `TOTAL_ASSETS` / `CASH` 的真实时点。
#: 🔴 注意：这**不是**信号日（信号日 = 2026-11-03）⇒ 「计划件口径」= **账户快照日口径**，
#:    「节点日口径」= 节点日价格口径 ⇒ 两者**不可混称**（M-S5 独立审计 IMPORTANT 修复）。
ACCOUNT_SNAPSHOT_DATE = "2026-11-04"

#: 对账容差（元）：市值 vs「股数 × 现价」、总资产 vs「现金 + 市值」都按此容差核对
_TOLERANCE = 0.01
#: cost/pnl 反推值与 CSV 值的容差（元）：CSV 只保留 2 位小数 ⇒ 允许 2 分钱的舍入差
_DERIVED_TOLERANCE = 0.02


def load_account_snapshot(
    path: str | Path | None = None,
    cash: float | None = None,
    total_assets: float | None = None,
) -> pd.DataFrame:
    """加载并校验账户快照 CSV，返回归一化后的持仓表。

    返回列：`code, name, shares, price, market_value, pnl_pct, cost, pnl`。

    - `code` 统一补零到 6 位（``str.zfill(6)``）并转 ``str``；
    - `cost`（成本金额）/ `pnl`（金额盈亏）由 `market_value` 与 `pnl_pct` **反推**：
      ``cost = market_value / (1 + pnl_pct)``、``pnl = market_value - cost``；
      CSV 里若已有这两列则**以 CSV 为准**，但仍要校验它与反推值自洽；
    - `pnl_pct` 是**小数**口径（0.0608 表示 +6.08%）。

    校验不通过一律抛异常（**不许静默通过**）：
    1. 每行 `market_value` 与 `shares * price` 偏差 < 0.01 元；
    2. `market_value` 合计 + `CASH` 与 `TOTAL_ASSETS` 偏差 < 0.01 元；
    3. CSV 的 `cost`/`pnl` 与反推值偏差 < 0.02 元（CSV 写错盈亏率会在这里被拦住）。

    参数
    ----
    path:
        快照 CSV 路径；缺省用模块内的 `ACCOUNT_SNAPSHOT_CSV`。
    cash:
        校验 2（Σ市值 + 现金 = 总资产）用的现金；缺省用模块常量 `CASH`。
    total_assets:
        校验 2 用的总资产；缺省用模块常量 `TOTAL_ASSETS`。

    🔴 2026-11-05（每日流水线）：本模块是**研究链**的账户基准（2026-11-04 快照）。
        每日流水线用的是**实盘基准**（2026-11-05，30,035.57 元）⇒ 通过 `cash` /
        `total_assets` 参数**显式注入**，不污染本模块的常量
        （改常量会让已归档交付件与指纹表假性失配）。
    """
    csv_path = Path(path) if path is not None else ACCOUNT_SNAPSHOT_CSV
    if not csv_path.exists():
        raise FileNotFoundError(f"账户快照 CSV 不存在：{csv_path}")
    # utf-8-sig：兼容带 BOM（Excel 打开中文不乱码）与不带 BOM 的 UTF-8
    df = pd.read_csv(csv_path, encoding="utf-8-sig", dtype={"code": str})

    required = ("code", "name", "shares", "price", "market_value", "pnl_pct")
    missing = [col for col in required if col not in df.columns]
    if missing:
        raise ValueError(f"账户快照缺少必需列：{missing}（文件：{csv_path}）")
    df = df.copy()
    df["code"] = df["code"].astype(str).str.strip().str.zfill(6)
    for col in ("shares", "price", "market_value", "pnl_pct"):
        df[col] = pd.to_numeric(df[col], errors="raise")
    if df.empty:
        raise ValueError(f"账户快照为空：{csv_path}")

    # --- 校验 1：每行市值 = 股数 × 现价 -------------------------------------
    mv_diff = (df["market_value"] - df["shares"] * df["price"]).abs()
    bad_rows = df.loc[mv_diff >= _TOLERANCE]
    if not bad_rows.empty:
        detail = ", ".join(
            f"{row['code']}(市值 {row['market_value']:.2f} ≠ 股数×现价 "
            f"{row['shares'] * row['price']:.2f})"
            for _, row in bad_rows.iterrows()
        )
        raise ValueError(f"账户快照市值与「股数 × 现价」不符（容差 {_TOLERANCE} 元）：{detail}")

    # --- 校验 2：持仓市值合计 + 现金 = 总资产 --------------------------------
    total_mv = float(df["market_value"].sum())
    _cash = CASH if cash is None else float(cash)
    _total = TOTAL_ASSETS if total_assets is None else float(total_assets)
    total_diff = total_mv + _cash - _total
    if abs(total_diff) >= _TOLERANCE:
        raise ValueError(
            f"账户对账失败：持仓市值合计 {total_mv:.2f} + 现金 {_cash:.2f} = {total_mv + _cash:.2f}，"
            f"与总资产 {_total:.2f} 偏差 {total_diff:+.2f} 元"
        )

    # --- cost / pnl：反推，并与 CSV 对账 -------------------------------------
    if bool((df["pnl_pct"] <= -1.0).any()):
        raise ValueError("账户快照存在盈亏率 ≤ -100% 的行（成本为负，无意义）")
    # 🔴 2026-11-04 夜实测：`pnl_pct` 为空 ⇒ NaN ⇒ 与任何数比较都 False
    #    ⇒ 下面「cost 与市值/盈亏率不自洽」这条检查对该行**静默失效**
    #    ⇒ 少一个数即可绕过对账。盈亏率是必需输入，不是可选字段 ⇒ 缺失直接抛。
    _pnl_bad = ~df["pnl_pct"].apply(lambda v: v == v and abs(float(v)) != float("inf"))
    _pnl_missing = df.loc[_pnl_bad, "code"].tolist()
    if _pnl_missing:
        raise ValueError(
            f"账户快照存在缺失/非数值的盈亏率行（代码 {_pnl_missing}）"
            "⇒ pnl_pct 是必需输入，缺失即拒绝加载（不许静默跳过对账）"
        )
    derived_cost = df["market_value"] / (1.0 + df["pnl_pct"])
    derived_pnl = df["market_value"] - derived_cost

    if "cost" in df.columns:
        csv_cost = pd.to_numeric(df["cost"], errors="raise")
        present = csv_cost.notna()
        bad_rows = df.loc[present & ((csv_cost - derived_cost).abs() >= _DERIVED_TOLERANCE)]
        if not bad_rows.empty:
            detail = ", ".join(
                f"{row['code']}(CSV cost {row['cost']:.2f} ≠ 由市值/盈亏率反推 "
                f"{row['market_value'] / (1.0 + row['pnl_pct']):.2f})"
                for _, row in bad_rows.iterrows()
            )
            raise ValueError(f"账户快照 cost 与「市值 / 盈亏率」不自洽：{detail}")
        # CSV 为准；CSV 里留空的行用反推值补上
        df["cost"] = csv_cost.where(present, derived_cost)
    else:
        df["cost"] = derived_cost

    if "pnl" in df.columns:
        csv_pnl = pd.to_numeric(df["pnl"], errors="raise")
        present = csv_pnl.notna()
        bad_rows = df.loc[present & ((csv_pnl - derived_pnl).abs() >= _DERIVED_TOLERANCE)]
        if not bad_rows.empty:
            detail = ", ".join(
                f"{row['code']}(CSV pnl {row['pnl']:.2f} ≠ 由市值/盈亏率反推 "
                f"{row['market_value'] - row['market_value'] / (1.0 + row['pnl_pct']):.2f})"
                for _, row in bad_rows.iterrows()
            )
            raise ValueError(f"账户快照 pnl 与「市值 / 盈亏率」不自洽：{detail}")
        df["pnl"] = csv_pnl.where(present, derived_pnl)
    else:
        df["pnl"] = derived_pnl

    return df[
        ["code", "name", "shares", "price", "market_value", "pnl_pct", "cost", "pnl"]
    ].reset_index(drop=True)


def account_summary(snapshot: pd.DataFrame | None = None) -> dict:
    """账户总览（口径全部来自真实账户）。

    返回键：
    - `total_assets` / `cash` / `market_value`：总资产、现金、持仓市值（元）；
    - `cash_weight` / `market_value_weight`：现金与持仓占总资产的比例；
    - `n_positions`：持仓只数；
    - `total_cost` / `total_pnl` / `total_pnl_pct`：总成本、总盈亏（元）、总盈亏率；
    - `target_positions` / `target_position_weight` / `target_position_amount`：目标结构
      （5 只等权，每只 20% ≈ 5,970 元）；
    - `per_position_pnl_pct`：`{code: 盈亏率}`（小数口径，如 0.0608 = +6.08%）。

    会校验 `total_assets == cash + market_value`（偏差 ≥ 0.01 元抛异常）。

    参数
    ----
    snapshot:
        已加载的持仓表；缺省用 `load_account_snapshot()` 读默认 CSV。
    """
    df = load_account_snapshot() if snapshot is None else snapshot
    market_value = float(df["market_value"].sum())
    cash = CASH
    total_assets = TOTAL_ASSETS
    if abs(total_assets - (cash + market_value)) >= _TOLERANCE:
        raise ValueError(
            f"账户对账失败：总资产 {total_assets:.2f} ≠ 现金 {cash:.2f} + 市值 {market_value:.2f}"
        )
    total_cost = float(df["cost"].sum())
    total_pnl = float(df["pnl"].sum())
    total_pnl_pct = total_pnl / total_cost if total_cost else float("nan")
    per_position_pnl_pct = {
        str(row["code"]): float(row["pnl_pct"]) for _, row in df.iterrows()
    }
    return {
        "total_assets": total_assets,
        "cash": cash,
        "market_value": market_value,
        "cash_weight": cash / total_assets,
        "market_value_weight": market_value / total_assets,
        "n_positions": int(len(df)),
        "total_cost": total_cost,
        "total_pnl": total_pnl,
        "total_pnl_pct": total_pnl_pct,
        "target_positions": TARGET_POSITIONS,
        "target_position_weight": TARGET_POSITION_WEIGHT,
        "target_position_amount": TARGET_POSITION_AMOUNT,
        "per_position_pnl_pct": per_position_pnl_pct,
    }


def current_weights(snapshot: pd.DataFrame | None = None) -> dict[str, float]:
    """每只持仓占总资产的权重（**不含现金**）。

    直接可用于 `recommend.py --holdings`，例如：
        ",".join(f"{code}:{w:.6f}" for code, w in current_weights().items())

    参数
    ----
    snapshot:
        已加载的持仓表；缺省用 `load_account_snapshot()` 读默认 CSV。
    """
    df = load_account_snapshot() if snapshot is None else snapshot
    return {
        str(row["code"]): float(row["market_value"]) / TOTAL_ASSETS
        for _, row in df.iterrows()
    }


if __name__ == "__main__":
    summary = account_summary()
    print(f"总资产 {summary['total_assets']:.2f} 元（现金 {summary['cash']:.2f} / "
          f"市值 {summary['market_value']:.2f}）")
    print(f"持仓 {summary['n_positions']} 只，总盈亏 {summary['total_pnl']:+.2f} 元 "
          f"({summary['total_pnl_pct'] * 100:+.2f}%)")
    print(f"目标结构：{summary['target_positions']} 只等权，每只 "
          f"{summary['target_position_weight'] * 100:.0f}% ≈ "
          f"{summary['target_position_amount']:.2f} 元")
    for code, weight in current_weights().items():
        print(f"  {code} 权重 {weight * 100:.2f}%")
