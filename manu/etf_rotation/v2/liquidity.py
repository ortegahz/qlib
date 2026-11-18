"""🔴 V2 真实流动性约束（对齐②）：停牌 / 涨跌停 ⇒ 不可成交。

**这是 V2，与 V1 无关**：本模块不 import、不改写 V1 的 ``engine/weights.py`` /
``microstructure.py``（V1 冻结为 baseline）；V1 的池筛选里**没有**「当天能不能买」
这一层 ⇒ V2 补上（对齐②的第三条：真实流动性约束）。

🔴 **为什么必须补这一层（事故背景）**：
   V1 的穿透打分只问「成分股权重覆盖够不够」，不问「这只票今天能不能买到」。
   ⇒ 回测里会出现「选出一只停牌股 / 一字涨停的票」的假成交 ⇒ 收益被系统性高估
   （涨停买不到 ⇒ 实盘根本拿不到那个价格）。
   ⇒ V2 在**候选筛选层**加一道日频可交易性闸门。

判据（逐条写死，报告里照抄）：

  * **停牌**：``volume`` 非有限或 ``<= 0``；
  * **涨停**：``change >= 9.5%``（``config_v2.LIMIT_THRESHOLD``）⇒ **不可买入**
    （卖方惜售，涨停板上买不到）；
  * **跌停**：``change <= -9.5%`` ⇒ **不可卖出**（买方恐慌，跌停板上卖不掉）；
  * 🔴 **返回列 ``tradable`` 的含义 =「可买入」**（不是「双向可交易」）：
    ``tradable = (不停牌) and (不涨停)``。
    跌停股**仍可买**（跌停时卖压巨大，反而好买），但**卖不掉**
    ⇒ 需要卖出侧过滤的调用方，用 ``limit_down`` 标记**自行判定**
    （可卖出 = ``not suspended and not limit_down``）。
  * 🔴 **阈值 9.5% 是保守近似**：A 股主板涨跌停真值是 10%，9.5% 是把「接近板」
    的票也当成触及（宁可少买，不许假成交）；创业板/科创板 20% 涨跌停不在本阈值内
    （**已知局限**，报告里必须披露）。

数据来源（个股侧）：qlib ``D.features(instruments, ['$volume','$change'])``
（cn_data 的 ``features/<inst>/<field>.day.bin`` 里就有 ``$volume`` / ``$change``）。
数据来源（ETF 侧）：``etf_prices.csv``（长表：date, code, close, volume, amount）
⇒ ETF 侧**没有 change 字段** ⇒ 用 ``close`` 的日环比（``pct_change``）推导。
"""

from __future__ import annotations

import numpy as np
import pandas as pd

try:  # 扁平导入（v2 目录进 sys.path，与 labels.py / splits.py 同口径）
    import config_v2 as C
except ModuleNotFoundError:  # pragma: no cover - 仅当以包方式被 import 时走这条
    from . import config_v2 as C


#: 输出的四个布尔标记（顺序固定，调用方按名取用）
FLAG_COLUMNS = ("suspended", "limit_up", "limit_down", "tradable")

#: 字段别名 → 规范名（大小写不敏感、可带 ``$`` 前缀）
_FIELD_ALIASES = {
    "volume": "volume",
    "vol": "volume",
    "change": "change",
    "chg": "change",
    "pct_chg": "change",
    "close": "close",
    "amount": "amount",
}

#: 长表形态下的「标的列」候选名
_CODE_COLUMNS = ("code", "instrument", "stock_code", "etf_code", "symbol")

#: 长表形态下的「日期列」候选名
_DATE_COLUMNS = ("date", "datetime", "trade_date", "dt")


def _canon_field(name: object) -> str | None:
    """列名 → 规范字段名（``$volume`` / ``VOLUME`` / ``vol`` ⇒ ``volume``）；认不出 ⇒ ``None``。"""
    text = str(name).strip().lower().lstrip("$").strip()
    return _FIELD_ALIASES.get(text)


def _pick_column(columns, candidates) -> str | None:
    """按候选名（忽略大小写）从列里挑一个出来。"""
    lowered = {str(col).strip().lower(): col for col in columns}
    for candidate in candidates:
        if candidate in lowered:
            return lowered[candidate]
    return None


def _is_long_table(df: pd.DataFrame) -> bool:
    """是不是长表形态（有日期列 + 标的列 + 字段列）？"""
    has_date = _pick_column(df.columns, _DATE_COLUMNS) is not None
    has_code = _pick_column(df.columns, _CODE_COLUMNS) is not None
    has_field = any(_canon_field(col) for col in df.columns)
    return has_date and has_code and has_field


def _wide_from_multiindex(df: pd.DataFrame) -> dict[str, pd.DataFrame]:
    """``MultiIndex`` 列（(标的, 字段) 或 (字段, 标的)）⇒ ``{规范字段名: 宽表}``。

    🔴 **为什么要自动判方向**：qlib ``D.features`` 返回的列是**字段**，
    而按 (标的, 字段) 拼出来的宽表列是 (标的, 字段) ⇒ 两种方向都出现过，
    写死一个方向就会有一边读成 NaN（静默失真）。这里逐列**探测**字段落在哪一层。
    """
    if not isinstance(df.columns, pd.MultiIndex):
        raise ValueError("🔴 期望 MultiIndex 列（(标的, 字段)），实际是普通列")
    field_level = None
    for level in range(df.columns.nlevels):
        hits = sum(1 for value in df.columns.get_level_values(level)
                   if _canon_field(value) is not None)
        if hits:
            field_level = level
            break
    if field_level is None:
        raise ValueError(
            f"🔴 MultiIndex 列里找不到 volume / change 字段（列：{list(df.columns)[:8]}）"
        )
    other_level = 1 - field_level
    buckets: dict[str, dict[object, pd.Series]] = {}
    for key, series in df.items():
        field = _canon_field(key[field_level])
        if field is None:
            continue
        buckets.setdefault(field, {})[key[other_level]] = series
    out: dict[str, pd.DataFrame] = {}
    for field, mapping in buckets.items():
        wide = pd.DataFrame(mapping)
        wide = wide.reindex(columns=sorted(mapping, key=str))
        out[field] = wide
    return out


def _flags_from_panels(volume: pd.DataFrame, change: pd.DataFrame,
                        limit_threshold: float) -> dict[str, pd.DataFrame]:
    """由「成交量宽表 + 涨跌幅宽表」算出四个布尔标记（wide 形态）。"""
    # 🔴 DataFrame 级的 to_numeric 不接受 DataFrame ⇒ 必须逐列 apply
    vol = volume.apply(pd.to_numeric, errors="coerce").astype("float64")
    chg = change.apply(pd.to_numeric, errors="coerce").astype("float64")
    suspended = ~(np.isfinite(vol.to_numpy()) & (vol.to_numpy() > 0))
    suspended = pd.DataFrame(suspended, index=vol.index, columns=vol.columns)
    limit_up = (chg >= limit_threshold).fillna(False)
    limit_down = (chg <= -limit_threshold).fillna(False)
    tradable = (~suspended) & (~limit_up)
    return {
        "suspended": suspended,
        "limit_up": limit_up,
        "limit_down": limit_down,
        "tradable": tradable,
    }


def _pack_wide(flags: dict[str, pd.DataFrame]) -> pd.DataFrame:
    """把四个标记宽表打包成 ``MultiIndex`` 列（**标记, 标的**）的 DataFrame。

    🔴 第 0 层是**标记**（顺序固定 = ``FLAG_COLUMNS``）、第 1 层是**标的**
    ⇒ 调用方取 ``flags["tradable"]`` 直接得到「日期 × 标的」的可买宽表。
    """
    out = pd.concat({name: flags[name] for name in FLAG_COLUMNS}, axis=1)
    out.columns = out.columns.set_names(["flag", "instrument"])
    return out


def _append_flags_long(df: pd.DataFrame, flags: dict[str, pd.Series]) -> pd.DataFrame:
    """把四个标记**并回**长表（同行同序 ⇒ 调仓明细可直接带着标记走）。"""
    out = df.copy()
    for name in FLAG_COLUMNS:
        out[name] = flags[name].to_numpy()
    return out


def mark_stock_tradable(panel: pd.DataFrame, limit_threshold: float | None = None) -> pd.DataFrame:
    """给个股面板打上「停牌 / 涨停 / 跌停 / 可买入」四个标记。

    参数
    ----
    panel : 个股面板，三种形态任选其一
        ① **MultiIndex 列**（推荐）：``columns = (标的, 字段)`` 或 ``(字段, 标的)``，
           字段须含 ``volume`` 与 ``change``（大小写不敏感、可带 ``$`` 前缀；
           qlib ``D.features`` 的原始输出 pivot 一下就是这个形态）；
        ② **长表**：含日期列（``date``）+ 标的列（``code``/``instrument``/…）
           + ``volume`` 列 + ``change`` 列；
        ③ **dict**：``{"volume": 宽表, "change": 宽表}``。
        🔴 **扁平宽表（只有一层列名）不支持**：一只标的一个格子里放不下
        ``volume`` 与 ``change`` 两个字段 ⇒ 传进来会**直接报错**，
        而不是猜哪个是哪个（猜错 ⇒ 涨跌停判据静默失效）。
    limit_threshold : 涨跌停阈值；``None`` ⇒ ``config_v2.LIMIT_THRESHOLD``（9.5%）

    返回
    ----
    ① ③ ⇒ ``MultiIndex`` 列（**标记, 标的**）的 DataFrame（index = 日期）；
    取出「日期 × 标的」的可买宽表：``flags["tradable"]``；
    ② ⇒ 原长表 + 四个标记列（同行同序）。

    🔴 **``tradable`` = 可买入**：``not suspended and not limit_up``。
    跌停 ⇒ ``limit_down=True`` 且**仍可买**（卖压巨大反而好买），
    但**卖不掉** ⇒ 卖出侧过滤请用 ``limit_down`` 标记自行判定。
    """
    threshold = float(C.LIMIT_THRESHOLD if limit_threshold is None else limit_threshold)
    if isinstance(panel, dict):
        volume = panel.get("volume")
        change = panel.get("change")
        if volume is None or change is None:
            raise ValueError("🔴 dict 形态必须同时给 'volume' 与 'change' 两张宽表")
        return _pack_wide(_flags_from_panels(volume, change, threshold))
    if not isinstance(panel, pd.DataFrame):
        raise TypeError(f"🔴 panel 必须是 DataFrame / dict，实得 {type(panel)!r}")
    if isinstance(panel.columns, pd.MultiIndex):
        buckets = _wide_from_multiindex(panel)
        if "volume" not in buckets or "change" not in buckets:
            raise ValueError(
                f"🔴 MultiIndex 列里必须同时有 volume 与 change，实得 {sorted(buckets)}"
            )
        return _pack_wide(_flags_from_panels(buckets["volume"], buckets["change"], threshold))
    if _is_long_table(panel):
        vol_col = _pick_column(panel.columns, ("volume", "vol"))
        chg_col = _pick_column(panel.columns, ("change", "chg", "pct_chg"))
        if vol_col is None or chg_col is None:
            raise ValueError(
                f"🔴 个股长表必须同时有 volume 与 change 列，实得 {list(panel.columns)}"
            )
        vol = pd.to_numeric(panel[vol_col], errors="coerce")
        chg = pd.to_numeric(panel[chg_col], errors="coerce")
        suspended = pd.Series(
            ~(np.isfinite(vol.to_numpy()) & (vol.to_numpy() > 0)), index=panel.index
        )
        limit_up = pd.Series((chg >= threshold).fillna(False).to_numpy(), index=panel.index)
        limit_down = pd.Series((chg <= -threshold).fillna(False).to_numpy(), index=panel.index)
        flags = {
            "suspended": suspended,
            "limit_up": limit_up,
            "limit_down": limit_down,
            # 🔴 tradable = 可买入 = 不停牌 且 不涨停（跌停仍可买，但卖不掉）
            "tradable": (~suspended) & (~limit_up),
        }
        return _append_flags_long(panel, flags)
    raise ValueError(
        "🔴 个股面板形态不支持：期望 MultiIndex 列（标的, 字段）/ 长表 / "
        "dict{'volume','change'}；扁平宽表放不下 volume 与 change 两个字段"
    )


def mark_etf_tradable(prices: pd.DataFrame, limit_threshold: float | None = None) -> pd.DataFrame:
    """给 ETF 行情打上四个标记（**可买入**语义与个股侧一致）。

    参数
    ----
    prices : ETF 行情，两种形态
        ① **长表**（``etf_prices.csv`` 的原始形态）：``date`` + ``code``
           + ``close`` + ``volume``（+ ``amount``）⇒ **同结构返回**（加四列标记）；
        ② **MultiIndex 列**：``columns = (代码, 字段)``，字段含 ``close`` / ``volume``。
    limit_threshold : 涨跌停阈值；``None`` ⇒ ``config_v2.LIMIT_THRESHOLD``（9.5%）

    返回
    ----
    与输入**同结构**的 DataFrame（长表进长表出，宽表进宽表出）。

    🔴 ETF 行情表**没有 change 字段** ⇒ 涨跌幅由 ``close`` 的日环比推导
    （长表按代码分组求 ``pct_change``，宽表按行求 ``pct_change``）。
    🔴 ETF 的真涨跌停是 **10%**（跨境/商品 ETF 可到 20%）⇒ 本函数沿用统一的
    9.5% 阈值 ⇒ 对 20% 涨跌停品种**判不出**板（**已知局限**，报告须披露）。
    """
    threshold = float(C.LIMIT_THRESHOLD if limit_threshold is None else limit_threshold)
    if not isinstance(prices, pd.DataFrame):
        raise TypeError(f"🔴 prices 必须是 DataFrame，实得 {type(prices)!r}")
    if isinstance(prices.columns, pd.MultiIndex):
        buckets = _wide_from_multiindex(prices)
        if "close" not in buckets:
            raise ValueError("🔴 ETF 宽表必须有 close 字段")
        close = buckets["close"].apply(pd.to_numeric, errors="coerce").astype("float64")
        change = close.pct_change(axis=0)
        if "volume" in buckets:
            volume = buckets["volume"]
        else:
            # 没有成交量列 ⇒ 不判停牌（退化口径，报告里要说明）
            volume = pd.DataFrame(1.0, index=close.index, columns=close.columns)
        return _pack_wide(_flags_from_panels(volume, change, threshold))
    if _is_long_table(prices):
        code_col = _pick_column(prices.columns, _CODE_COLUMNS)
        close_col = _pick_column(prices.columns, ("close",))
        vol_col = _pick_column(prices.columns, ("volume", "vol"))
        if close_col is None or vol_col is None:
            raise ValueError("🔴 ETF 长表必须有 close 与 volume 列")
        date_col = _pick_column(prices.columns, _DATE_COLUMNS)
        # 🔴 先按（代码, 日期）排序再算日环比 ⇒ 不依赖调用方给的行序（乱序会算错收益率）
        order_keys = [code_col] if date_col is None else [code_col, date_col]
        ordered = prices.sort_values(order_keys, kind="stable")
        close = pd.to_numeric(prices[close_col], errors="coerce")
        vol = pd.to_numeric(prices[vol_col], errors="coerce")
        change = ordered.groupby(code_col, sort=False)[close_col].pct_change()
        change = change.reindex(prices.index)
        suspended = pd.Series(
            ~(np.isfinite(vol.to_numpy()) & (vol.to_numpy() > 0)), index=prices.index
        )
        limit_up = pd.Series((change >= threshold).fillna(False).to_numpy(), index=prices.index)
        limit_down = pd.Series((change <= -threshold).fillna(False).to_numpy(), index=prices.index)
        flags = {
            "suspended": suspended,
            "limit_up": limit_up,
            "limit_down": limit_down,
            # 🔴 tradable = 可买入 = 不停牌 且 不涨停（跌停仍可买，但卖不掉）
            "tradable": (~suspended) & (~limit_up),
        }
        return _append_flags_long(prices, flags)
    raise ValueError(
        "🔴 ETF 行情形态不支持：期望长表（date/code/close/volume）或 MultiIndex 列"
    )


def filter_candidates_by_tradability(candidates: list[str], tradable: dict[str, bool],
                                      drop_on_untradable: bool = True) -> tuple[list[str], list[dict]]:
    """按「可买入」过滤候选 ⇒ ``(保留列表, 不可交易明细)``。

    参数
    ----
    candidates : 候选标的（保持原顺序）
    tradable : ``{代码: 是否可买入}``（由 ``mark_*_tradable`` 的 ``tradable`` 列抽出）；
        **代码不在表里 ⇒ 视为不可交易**（没有行情就不许下单）
    drop_on_untradable : ``True`` ⇒ 不可交易的候选被**剔除**；
        ``False`` ⇒ 一律保留（只做披露，诊断用）

    返回
    ----
    ``(kept, dropped)``：``kept`` 是保留的候选；``dropped`` 是
    ``[{"code": ..., "reason": ...}]`` —— **不可交易候选的明细**
    （``drop_on_untradable=False`` 时它们仍留在 ``kept`` 里，明细照样给出来，
    便于「只诊断不剔除」的用法）。
    """
    kept: list[str] = []
    dropped: list[dict] = []
    for code in candidates:
        key = str(code)
        if key in tradable:
            ok = bool(tradable[key])
            reason = "untradable"
        else:
            ok = False
            reason = "no_tradability_data"
        if ok:
            kept.append(code)
            continue
        dropped.append({"code": code, "reason": reason})
        if not drop_on_untradable:
            kept.append(code)
    return kept, dropped
