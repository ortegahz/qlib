"""🔴 **急跌刹车：组合净值 / 峰值状态**（每日链唯一事实源，2026-11-18）

【为什么必须有它 —— 审计缺陷「no_drawdown_brake」（high 级）】
  组合级回撤刹车在**研究链**里是实现了的：
    ``config.DRAWDOWN_BRAKE_THRESHOLD``（回撤阈值）
    → ``analyze.py`` / ``recommend.py`` 收 ``--nav`` / ``--nav-peak``
      ⇒ 组装 ``nav_state = {"nav", "peak", "in_brake", ...}``
    → ``engine/portfolio.py`` 的 ``brake_mode`` ⇒ **整个组合切防守资产**。
  但**每日链从不传这两个参数** ⇒ ``nav_state`` 恒为 ``None``
  ⇒ ``in_brake`` 恒为 ``False`` ⇒ **刹车永远不触发**（"急跌无刹车"）。
  ⇒ 本模块提供「净值序列 + 峰值」的**唯一事实源**，每日链据此传参。

【净值口径（诚实披露，不美化）】
  * ``nav`` = **账户总资产的重估净值** = 现金 + Σ(持仓股数 × asof 当日收盘价)
    —— 持仓来自 ``account_state.account_summary()``（实盘基准），
    价格来自 ETF 行情面板（asof 当日或之前最近一根）。
  * 某只持仓**取不到价格** ⇒ 用账户快照里的 ``market_value`` 兜底
    ⇒ 计入 ``n_unpriced`` 并**如实披露**（不许编价）。
  * ``peak`` = **历史净值序列的滚动最大值**（跨日持久化到
    ``daily_config.NAV_STATE_FILE``）⇒ 回撤 = ``nav / peak − 1``。
  * 🔴 **口径提醒（重要）**：本账户**现金占比很高**（默认口径 77.8%），
    ⇒ **账户级**回撤会被现金摊薄 ⇒ 账户回撤 15% 大致对应**权益仓位腰斩**。
    ⇒ 权益口径回撤（只算持仓市值）**同时**输出，供披露对照；
    **刹车阈值仍按账户级**（与研究链 ``analyze.py`` / ``recommend.py`` 的
    ``dd = nav / peak − 1`` 语义**完全一致** ⇒ 不改研究链口径）。

【不做的事】
  * 不改研究链 ``analyze.py`` / ``recommend.py`` / ``engine/portfolio.py`` 的刹车语义；
  * 不改账户基准常量（``daily_config.TOTAL_ASSETS`` 等）；
  * 不在**观察态**之外产生任何交易副作用（刹车只影响**建议**，执行卡照旧走）。
"""

from __future__ import annotations

import json
import time
from pathlib import Path

import daily_config as DC


# ----------------------------------------------------------------- 净值序列
def state_path() -> Path:
    """净值状态文件路径（默认 ``<RUN_ROOT>/_state/nav_history.json``）。"""
    return Path(DC.NAV_STATE_FILE)


def load_state(path: Path | None = None) -> dict:
    """读净值序列；读不到 / 坏掉 ⇒ 返回空序列（**不抛**，不阻断流水线）。"""
    p = Path(path) if path is not None else state_path()
    if not p.is_file():
        return {"points": {}, "updated_at": None, "source": p.name}
    try:
        data = json.loads(p.read_text(encoding="utf-8"))
    except Exception:
        return {"points": {}, "updated_at": None, "source": p.name, "note": "状态文件损坏 ⇒ 按空序列处理"}
    points = data.get("points")
    if not isinstance(points, dict):
        points = {}
    return {"points": {str(k): float(v) for k, v in points.items() if _is_finite(v)},
            "updated_at": data.get("updated_at"), "source": p.name}


def save_state(state: dict, path: Path | None = None) -> Path:
    """原子写净值序列。"""
    p = Path(path) if path is not None else state_path()
    p.parent.mkdir(parents=True, exist_ok=True)
    tmp = p.with_suffix(p.suffix + ".tmp")
    tmp.write_text(json.dumps(state, ensure_ascii=False, indent=2), encoding="utf-8")
    tmp.replace(p)
    return p


def _is_finite(value: object) -> bool:
    try:
        f = float(value)  # type: ignore[arg-type]
    except (TypeError, ValueError):
        return False
    return f == f and abs(f) != float("inf")


# ----------------------------------------------------------------- 净值计算
def _latest_close_by_code(prices_csv: Path | None, asof: str) -> dict[str, float]:
    """ETF 行情面板里「每只代码在 asof（含）之前的最后一根收盘价」。"""
    if prices_csv is None:
        return {}
    p = Path(prices_csv)
    if not p.is_file():
        return {}
    import pandas as pd

    df = pd.read_csv(p, dtype={"code": str}, usecols=["date", "code", "close"])
    df["code"] = df["code"].astype(str).str.zfill(6)
    df["date"] = pd.to_datetime(df["date"])
    df = df[df["date"] <= pd.Timestamp(asof)].sort_values("date")
    if df.empty:
        return {}
    return {str(code): float(val) for code, val in df.groupby("code")["close"].last().items()}


def nav_from_account(account: dict, prices_csv: Path | None, asof: str) -> dict:
    """🔴 由账户上下文 + 行情面板算出**asof 当日的账户重估净值**。

    返回 ``{"nav", "cash", "market_value", "equity_nav", "n_priced", "n_unpriced",
    "source", "note"}``；``nav`` 为 ``None`` 表示**算不出来**（⇒ 刹车不启用）。
    """
    cash = account.get("cash")
    positions = list(account.get("positions") or [])
    closes = _latest_close_by_code(prices_csv, asof)
    market_live = 0.0
    market_snapshot = 0.0
    n_priced = 0
    n_unpriced = 0
    for pos in positions:
        code = str(pos.get("code") or "").strip().zfill(6)
        shares = pos.get("shares")
        snap_mv = pos.get("market_value")
        price = closes.get(code)
        try:
            shares_f = float(shares or 0.0)
        except (TypeError, ValueError):
            shares_f = 0.0
        try:
            snap_mv_f = float(snap_mv or 0.0)
        except (TypeError, ValueError):
            snap_mv_f = 0.0
        market_snapshot += snap_mv_f
        if price is not None and _is_finite(price) and shares_f > 0:
            market_live += shares_f * float(price)
            n_priced += 1
        else:
            market_live += snap_mv_f          # 🔴 取不到价 ⇒ 用快照市值兜底（不编价）
            n_unpriced += 1
    cash_f = None
    try:
        cash_f = float(cash) if cash is not None else None
    except (TypeError, ValueError):
        cash_f = None
    if cash_f is None and not positions:
        return {"nav": None, "cash": None, "market_value": None, "equity_nav": None,
                "n_priced": 0, "n_unpriced": 0, "source": "unavailable",
                "note": "账户上下文既无现金也无持仓 ⇒ 算不出净值 ⇒ 刹车不启用"}
    cash_f = cash_f if cash_f is not None else 0.0
    nav = cash_f + market_live
    total_assets = account.get("total_assets")
    try:
        total_assets_f = float(total_assets) if total_assets is not None else None
    except (TypeError, ValueError):
        total_assets_f = None
    note = (
        f"🔴 净值口径 = 现金 {cash_f:.2f} + 持仓重估 {market_live:.2f}"
        f"（{n_priced} 只按 asof 收盘价重估 / {n_unpriced} 只用快照市值兜底）"
    )
    if total_assets_f is not None and abs(nav - total_assets_f) > 0.01:
        note += f"｜与账户快照总资产 {total_assets_f:.2f} 相差 {nav - total_assets_f:+.2f} 元"
    return {
        "nav": nav, "cash": cash_f, "market_value": market_live,
        "equity_nav": market_live, "n_priced": n_priced, "n_unpriced": n_unpriced,
        "source": "account_marked_to_market", "note": note,
    }


# ----------------------------------------------------------------- 对外入口
def prepare_daily_nav(*, asof: str, account: dict, prices_csv: Path | None,
                      notes: list[str] | None = None,
                      path: Path | None = None) -> dict:
    """🔴 每日链入口：算净值 → 更新序列 → 给出**峰值 / 回撤 / 是否刹车**。

    返回 ``{"nav", "peak", "drawdown", "in_brake", "enabled", "reason", ...}``。
    ``nav`` 为 ``None`` ⇒ 不传 ``--nav`` / ``--nav-peak``（刹车保持关闭并披露）。
    """
    notes = notes if notes is not None else []
    threshold = float(DC.DRAWDOWN_BRAKE_THRESHOLD)
    if not bool(DC.DRAWDOWN_BRAKE_ENABLED):
        notes.append(
            "🛑 **急跌刹车已被配置关闭**"
            f"（`daily_config.DRAWDOWN_BRAKE_ENABLED=False`）⇒ 不传 --nav/--nav-peak"
        )
        return {"nav": None, "peak": None, "drawdown": None, "in_brake": False,
                "enabled": False, "reason": "disabled_by_config",
                "threshold": threshold, "n_points": 0}
    live = nav_from_account(account, prices_csv, asof)
    if live.get("nav") is None:
        notes.append(f"🛑 {live.get('note')}")
        return {"nav": None, "peak": None, "drawdown": None, "in_brake": False,
                "enabled": True, "reason": "nav_unavailable",
                "threshold": threshold, "n_points": 0}
    state = load_state(path)
    points: dict[str, float] = dict(state.get("points") or {})
    # 🔴 种子：账户快照日当天用**快照总资产**做锚（避免第一天的峰值被人为压低）
    _seed = account.get("total_assets")
    if _is_finite(_seed) and str(DC.ACCOUNT_SNAPSHOT_DATE) not in points:
        points[str(DC.ACCOUNT_SNAPSHOT_DATE)] = float(_seed)
    points[str(asof)] = float(live["nav"])
    peak = max(points.values()) if points else float(live["nav"])
    peak_date = max(points, key=lambda k: points[k])
    state = {"points": points, "updated_at": time.strftime("%Y-%m-%dT%H:%M:%S"),
             "threshold": threshold, "source": str(path or state_path())}
    try:
        save_state(state, path)
    except OSError as exc:                  # 🔴 写不进去 ⇒ 只披露，不阻断
        notes.append(f"⚠️ 净值序列写盘失败（{exc}）⇒ 本轮刹车按**内存峰值**计算")
    drawdown = float(live["nav"]) / float(peak) - 1.0 if peak > 0 else 0.0
    in_brake = drawdown <= -threshold
    out = {
        "nav": float(live["nav"]), "peak": float(peak), "peak_date": str(peak_date),
        "drawdown": drawdown, "in_brake": bool(in_brake),
        "enabled": True, "reason": "ok", "threshold": threshold,
        "n_points": len(points), "asof": str(asof),
        "equity_nav": live.get("equity_nav"),
        "equity_drawdown_vs_peak": None,
        "note": live.get("note"),
        "nav_source": live.get("source"),
        "n_priced": live.get("n_priced"), "n_unpriced": live.get("n_unpriced"),
    }
    # 🔴 权益口径回撤（只披露，不参与刹车判定）
    _eq_peak = max((float(v) for v in points.values()), default=0.0)
    if _eq_peak > 0 and live.get("equity_nav") is not None:
        out["equity_drawdown_vs_peak"] = float(live["equity_nav"]) / _eq_peak - 1.0
    if in_brake:
        notes.append(
            f"🚨 **急跌刹车已触发**：账户净值 {live['nav']:.2f} / 历史峰值 {peak:.2f}"
            f"（{peak_date}）⇒ 回撤 {drawdown:+.2%} ≤ 阈值 -{threshold:.0%}"
            " ⇒ 规则层**整个组合切防守资产**（黄金 / 货币 ETF）"
        )
    else:
        notes.append(
            f"✅ 急跌刹车检查：净值 {live['nav']:.2f} / 峰值 {peak:.2f}（{peak_date}）"
            f" ⇒ 回撤 {drawdown:+.2%}（阈值 -{threshold:.0%}）⇒ 未触发"
        )
    return out
