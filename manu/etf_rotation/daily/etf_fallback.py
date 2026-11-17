"""ETF 日线**兜底数据源**（东财 push2 按 IP 封禁时的旁路）。

🔴 **为什么需要这个模块**（2026-11-13 夜跑实测）：
   东财 `push2his.eastmoney.com` 会**按 IP 封禁**（`Push2RateLimitError`：
   「短时间内请求过多，当前 IP 被临时限流」）⇒ 296 只 ETF **全灭**
   ⇒ 整轮 fail-closed（`fetch_etf_daily_bars` 抛「全部 N 只抓取失败」）。
   本模块提供**两条互不依赖的旁路**，口径对齐 canonical：

   * **腾讯** `web.ifzq.gtimg.cn/appstock/app/fqkline/get?param=<sym>,day,,,N,qfq`
     ⇒ **qfq 日线**（canonical `etf_prices.csv` 就是 `fund_etf_hist_em(adjust="qfq")` 抓的）
       - `qfqday` 行格式：`[日期, 开, 收, 高, 低, 量(手)]`
         （🔴 **历史行 3 位小数**；**当日行被四舍五入到 2 位** ⇒ 当日必须用 `qt`）
       - `qt` 数组 ⇒ **当日 3 位精度**：
         `[3]现价 [4]昨收 [5]今开 [6]量(手) [30]时间 [33]高 [34]低 [35]"价/量(手)/额(元)" [37]额(万元)`
   * **新浪** `ak.fund_etf_hist_sina` ⇒ 历史日线**含成交额**（成交量单位 = **股** ⇒ ÷100 换手）
     ⚠️ 新浪**当日 bar 发布滞后**（实测 11-13 收盘后仍未出）⇒ 当日行只能靠腾讯 `qt`。

   **fail-closed 分工**：本模块**只取数与拼装**，**不判定口径**；
   口径校验（与 canonical 重叠日比对 + 成交额/成交量自洽）由父进程
   `fetch_data._validate_fallback_bars` 做 ⇒ 校验不过 ⇒ 该标的**整只丢弃**
   （绝不用坏数据凑数）。
"""
from __future__ import annotations

from typing import Any

#: 🔴 腾讯**有 WAF**（2026-11-13 夜跑实测）：`web.ifzq.gtimg.cn` 连续请求 ~150 次后
#:   开始返 **501 + waf 跳转页**（不是业务错 ⇒ `raise_for_status` 会误判成 501）。
#:   ⇒ 主用**不带 `web.` 的同源域名**（实测 200），另备两个端点做容错。
TENCENT_ENDPOINTS = (
    "https://ifzq.gtimg.cn/appstock/app/fqkline/get",
    "https://web.ifzq.gtimg.cn/appstock/app/newfqkline/get",
    "https://web.ifzq.gtimg.cn/appstock/app/fqkline/get",
)
HEADERS = {"User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64)"}

#: 与 `fetch_data._AK_WORKER_SOURCE` 的 `OUT_COLS` 同构（父进程按同名列合并）
OUT_COLS = [
    "code", "date", "open", "high", "low", "close", "volume", "amount", "ret",
]


def tencent_symbol(code: str) -> str:
    """6 位代码 ⇒ 腾讯 symbol（沪 `sh` / 深 `sz` / 北 `bj`）。"""
    code = str(code).zfill(6)
    if code[0] in "569":
        return "sh" + code
    if code[0] in "48":
        return "bj" + code
    return "sz" + code


def _to_float(value: Any) -> float | None:
    try:
        return float(value)
    except (TypeError, ValueError):
        return None


def _dashed(day: str) -> str:
    """`YYYYMMDD` / `YYYY-MM-DD` ⇒ `YYYY-MM-DD`。"""
    text = str(day).strip()[:10].replace("-", "")
    if len(text) != 8 or not text.isdigit():
        return ""
    return f"{text[0:4]}-{text[4:6]}-{text[6:8]}"


def fetch_tencent(code: str, count: int = 40) -> tuple[list, list | None]:
    """腾讯 qfq 日线 ⇒ (`qfqday` 行, `qt` 数组)。

    🔴 多端点容错：WAF 命中时先换域名/端点重试，再决定失败。
    """
    import requests

    symbol = tencent_symbol(code)
    last_err = ""
    for endpoint in TENCENT_ENDPOINTS:
        try:
            resp = requests.get(
                f"{endpoint}?param={symbol},day,,,{int(count)},qfq",
                headers=HEADERS,
                timeout=25,
            )
            if resp.status_code != 200:
                last_err = f"HTTP {resp.status_code}"
                continue
            node = ((resp.json() or {}).get("data") or {}).get(symbol) or {}
            rows = node.get("qfqday") or node.get("day") or []
            if not rows:
                last_err = "返回空日线"
                continue
            qt_node = node.get("qt")
            qt = None
            if isinstance(qt_node, dict):
                qt = qt_node.get(symbol)
            elif isinstance(qt_node, list) and len(qt_node) >= 2 and isinstance(qt_node[1], list):
                qt = qt_node[1]
            return list(rows), qt
        except Exception as exc:  # noqa: BLE001 - 换下一个端点再试
            last_err = f"{type(exc).__name__}: {exc}"
    raise RuntimeError(f"腾讯三个端点全失败：{last_err}")


def qt_bar(qt: Any) -> dict | None:
    """腾讯 `qt` ⇒ **当日** bar（3 位精度 + 成交额）；字段缺失 ⇒ `None`。"""
    if not isinstance(qt, list) or len(qt) < 35:
        return None
    stamp = str(qt[30] or "") if len(qt) > 30 else ""
    if len(stamp) < 8 or not stamp[:8].isdigit():
        return None
    day = f"{stamp[0:4]}-{stamp[4:6]}-{stamp[6:8]}"
    amount = None
    triple = qt[35] if isinstance(qt[35], str) else ""
    if triple.count("/") == 2:
        amount = _to_float(triple.split("/")[2])
    if amount is None and len(qt) > 37:
        wan = _to_float(qt[37])
        amount = wan * 10000.0 if wan is not None else None
    bar = {
        "date": day,
        "open": _to_float(qt[5]),
        "close": _to_float(qt[3]),
        "prev_close": _to_float(qt[4]),
        "high": _to_float(qt[33]),
        "low": _to_float(qt[34]),
        "volume": _to_float(qt[6]),
        "amount": amount,
    }
    if any(bar[key] is None for key in ("open", "close", "high", "low", "volume")):
        return None
    return bar


def fetch_sina(code: str):
    """新浪 ETF 历史日线（**含成交额**；成交量单位 = 股）。"""
    import akshare as ak

    frame = ak.fund_etf_hist_sina(symbol=tencent_symbol(code))
    if frame is None or frame.empty:
        return None
    frame = frame.copy()
    frame["date"] = frame["date"].astype(str).str.slice(0, 10)
    return frame


def fetch_one_fallback(code: str, start_date: str, end_date: str) -> tuple[Any, str | None]:
    """兜底取单只 ETF 的日线 ⇒ `(df, err)`；`df` 列 = `OUT_COLS`。

    🔴 **只取数、不判定口径**：校验在父进程（canonical 重叠日 + 额量自洽）。
    """
    import pandas as pd

    code = str(code).zfill(6)
    start = _dashed(start_date)
    end = _dashed(end_date)
    if not start or not end:
        return None, f"日期区间非法：{start_date}~{end_date}"

    try:
        rows, qt = fetch_tencent(code)
    except Exception as exc:  # noqa: BLE001 - 兜底失败 ⇒ 交回父进程 fail-closed
        return None, f"腾讯取数失败：{type(exc).__name__}: {exc}"

    bars: dict[str, dict] = {}
    for row in rows:
        if not isinstance(row, (list, tuple)) or len(row) < 6:
            continue
        day = str(row[0])[:10]
        open_ = _to_float(row[1])
        close_ = _to_float(row[2])
        high = _to_float(row[3])
        low = _to_float(row[4])
        volume = _to_float(row[5])
        if close_ is None or volume is None:
            continue
        bars[day] = {
            "date": day, "open": open_, "high": high, "low": low,
            "close": close_, "volume": volume, "amount": None,
        }
    if not bars:
        return None, "腾讯日线为空"

    # 当日：qfqday 的当日行只有 2 位小数 ⇒ 用 qt 的 3 位精度覆盖（含成交额）
    bar = qt_bar(qt)
    if bar:
        bars[bar["date"]] = {**bars.get(bar["date"], {}), **bar}

    # 成交额：新浪历史（**当日 bar 新浪还没发** ⇒ 当日行的成交额来自腾讯 qt）
    try:
        sina = fetch_sina(code)
    except Exception as exc:  # noqa: BLE001
        sina = None
        print(f"[fallback] {code} 新浪历史失败：{type(exc).__name__}: {exc}", flush=True)
    if sina is not None:
        for _, row in sina.iterrows():
            day = str(row.get("date", ""))[:10]
            target = bars.get(day)
            if target is None or target.get("amount") is not None:
                continue
            amount = _to_float(row.get("amount"))
            if amount is None:
                continue
            target["amount"] = amount
            volume_shares = _to_float(row.get("volume"))
            if volume_shares is not None and volume_shares > 0:
                target["volume"] = volume_shares / 100.0  # 股 → 手（与 canonical 同口径）

    ordered = [bars[day] for day in sorted(bars)]
    # `ret` = 涨跌幅（小数）——用前一根 bar 的收盘作基准（窗口外的前值也在序列里）
    prev_close = None
    for item in ordered:
        item["ret"] = (item["close"] / prev_close - 1.0) if prev_close else None
        prev_close = item["close"]
    window = [item for item in ordered if start <= item["date"] <= end]
    if not window:
        return None, f"窗口内无数据（{start}~{end}）"
    frame = pd.DataFrame(window)
    frame.insert(0, "code", code)
    for col in OUT_COLS:
        if col not in frame.columns:
            frame[col] = None
    return frame[OUT_COLS], None
