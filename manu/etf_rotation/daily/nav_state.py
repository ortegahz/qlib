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

【🔴 未来点必须剔除（2026-11-27 补真缺陷）】
  净值序列 ``points`` 是**只增不减**的 dict ⇒ 一旦写进一个「未来日期」的点，
  它会**永久留在**序列里（后面再大的回撤也删不掉）⇒ 两个后果：
    ① **污染净值序列评估**：任何基于该序列的评估（净值曲线 / 回撤 / 绩效口径）
       都会看到一个**未来日期**的观测 ⇒ 口径失真；
    ② **抬高历史峰值 ⇒ 钝化急跌刹车**：``peak = max(points.values())`` 取的是
       **全序列**最大值 ⇒ 一个被污染抬高的值会**永久抬高 peak**
       ⇒ 回撤被摊薄 ⇒ **急跌刹车该触发时不触发**（生产风控被削弱）。
  ⇒ 未来日期的净值在**定义上**不可能是真实观测 ⇒ ``prepare_daily_nav``
    一律**先剔除所有日期 > asof 的点**（**含账户快照种子点**），
    再做 seed / 写点 / 算 peak ⇒ 峰值口径**只由已发生的观测**决定。
  🔴 本条对应项目铁律**「不许拿生产当试验田」**：生产净值序列是**生产的唯一事实源**，
     测试一旦漏了重定向就会往里写点 ⇒ 生产层必须**自己**兜住「未来点不许进序列」，
     而不是只靠每个调用方都记得传 ``path=``。
"""

from __future__ import annotations

import json
import time
from datetime import datetime
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


def _parse_day(text: object) -> str | None:
    """把 ``YYYY-MM-DD`` 解析成可比较的日期键；不是合法日期 ⇒ ``None``（不参与剔除）。"""
    try:
        return datetime.strptime(str(text), "%Y-%m-%d").strftime("%Y-%m-%d")
    except (TypeError, ValueError):
        return None


def _is_future_day(day: object, asof: str) -> bool:
    """日期 ``day`` 是否**晚于** ``asof``（两者都必须是合法 ``YYYY-MM-DD``）。"""
    day_key = _parse_day(day)
    asof_key = _parse_day(asof)
    return bool(day_key and asof_key and day_key > asof_key)


def _split_future_points(points: dict[str, float],
                         asof: str) -> tuple[dict[str, float], list[str]]:
    """把净值序列拆成「(保留的点, 被剔除的未来点日期列表)」。

    🔴 未来点（日期 > ``asof``）**必须剔除**，两个后果见模块 docstring：
       污染净值序列评估；抬高 ``peak`` 钝化急跌刹车。
    只有**合法日期**且**严格晚于** ``asof`` 的点会被剔除；
    非法日期键（历史脏数据）保持原样，**不扩大改动面**。
    """
    asof_key = _parse_day(asof)
    kept: dict[str, float] = {}
    dropped: list[str] = []
    for key, value in points.items():
        key_day = _parse_day(key)
        if asof_key is not None and key_day is not None and key_day > asof_key:
            dropped.append(str(key))
        else:
            kept[str(key)] = value
    return kept, sorted(dropped)


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
                "threshold": threshold, "n_points": 0, "dropped_future_points": []}
    live = nav_from_account(account, prices_csv, asof)
    if live.get("nav") is None:
        notes.append(f"🛑 {live.get('note')}")
        return {"nav": None, "peak": None, "drawdown": None, "in_brake": False,
                "enabled": True, "reason": "nav_unavailable",
                "threshold": threshold, "n_points": 0, "dropped_future_points": []}
    state = load_state(path)
    points: dict[str, float] = dict(state.get("points") or {})
    # 🔴 剔除**未来点**（日期 > asof）—— 必须在 seed / 写点 / 算 peak **之前**。
    #    两个后果见模块 docstring：① 污染净值序列评估；② 抬高 peak 钝化急跌刹车。
    #    ⇒ 峰值口径只由「已发生」的观测决定（铁律「不许拿生产当试验田」）。
    points, dropped_future_points = _split_future_points(points, asof)
    # 🔴 种子：账户快照日当天用**快照总资产**做锚（避免第一天的峰值被人为压低）
    #    ⇒ 种子日受**同一条规则**约束：晚于 asof 的未来日期**不许种**。
    _seed = account.get("total_assets")
    _seed_date = str(DC.ACCOUNT_SNAPSHOT_DATE)
    _seed_is_future = _is_future_day(_seed_date, asof)
    if _is_finite(_seed) and _seed_date not in points and not _seed_is_future:
        points[_seed_date] = float(_seed)
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
        "dropped_future_points": list(dropped_future_points),
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
    # 🔴 未来点剔除必须**留痕**（返回值 + notes 双重披露）⇒ 不许悄悄改峰值口径。
    if dropped_future_points:
        notes.append(
            "🧹 **净值序列已剔除未来点**："
            + "、".join(dropped_future_points)
            + f"（晚于 asof={asof} ⇒ 未来日期的净值不可能是真实观测）"
            " ⇒ 已从峰值 / 回撤口径中移除（留着会抬高历史峰值 ⇒ 钝化急跌刹车）"
        )
    if _seed_is_future:
        notes.append(
            f"🧹 **账户快照种子点未写入净值序列**：{_seed_date} 晚于 asof={asof}"
            " ⇒ 未来日期的点不许进净值序列"
        )
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
