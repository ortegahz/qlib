"""fetch_data.py —— **盘后数据增量管道**（ETF 行情 / W 只读 / qlib 二进制可选增量）。

【三条硬约束（都是踩过坑换来的）】
1. 🔴 **W（成分股权重）绝不重爬**：`manu/etf_rotation/data/fetch_etf_holdings.py`
   **没有只读开关**（它的 `--resume` 只跳过「fetch_date 已是今天」的 code，其余照爬）
   ⇒ 每日链路**只读**缓存 `etf_holdings.csv`；陈旧度**只披露、不拦**。
2. 🔴 **akshare 绝不装进 ql env** ⇒ 本模块**不 import akshare**，
   而是把一段 python 源码写到 `/mnt/data` 的临时脚本，用 `DC.AK_PY`（ak env）**子进程**执行。
3. 🔴 **不写共享的 canonical 数据集**：每日增量落**自己的** run root，
   由下游把 canonical + 增量**合并**成有效价格序列 ⇒ 每日链路可复现、且不污染研究链输入。

【qlib 二进制增量：默认关闭 + 带复权守卫】
qlib 的 cn_data 里 `features/<code>/<field>.day.bin` 存的是**逐日序列**（不是逐日一个文件），
第 1 个 float32 是「起始日历下标」，其后是各交易日的值（缺失写 NaN）。
spot 接口给的是**未复权价** ⇒ 复权因子错了会把整段历史改坏。
⇒ `update_qlib_daily_bar` 默认 `dry_run=True`（只报告不写盘）；真写时：
沿用上一交易日的 factor + 用 akshare 的**日涨跌幅**与 qlib 复权序列的日收益比对，
差异超阈值 ⇒ **该股剔除、不写脏数据**，并记进 `diverged` 列表上报。
"""
from __future__ import annotations

import importlib.util
import json
import subprocess
import sys
import time
from datetime import datetime, timedelta
from pathlib import Path
from typing import Sequence

import numpy as np
import pandas as pd

_HERE = Path(__file__).resolve().parent
if str(_HERE) not in sys.path:
    sys.path.insert(0, str(_HERE))

import daily_config as DC  # noqa: E402

#: 研究链根（`data/` 下的脚本**不是包**，只能按绝对路径加载）
_RESEARCH_ROOT = _HERE.parent
if str(_RESEARCH_ROOT) not in sys.path:
    sys.path.insert(0, str(_RESEARCH_ROOT))

#: 临时工作目录（铁律二：**不落 /tmp**，一律落 /mnt/data）
_WORK_ROOT = Path("/mnt/data")

#: canonical 行情文件的列（服务器 `etf_prices.csv` 实测：`date,code,open,high,low,close,volume,amount`）
_PRICE_COLS = ("date", "code", "open", "high", "low", "close", "volume", "amount")

#: qlib 字段 ← akshare 列 的对应关系。
#: 🔴 qlib 的 `adjclose` 存的是**未复权**收盘价、而 `close` 是**复权后**的价格
#:    ⇒ 两者都从 akshare 的**未复权** `close` 定标（各自用自己的 scale，保住各自的价格水位）。
_QLIB_FIELD_SOURCE = {
    "open": "open",
    "high": "high",
    "low": "low",
    "close": "close",
    "adjclose": "close",
    "volume": "volume",
    "amount": "amount",
}

#: qlib cn_data 的字段 bin（`features/<code>/<field>.day.bin`）
_QLIB_FIELDS = (
    "open", "high", "low", "close", "volume", "amount", "vwap", "factor", "change", "adjclose",
)

#: akshare 子进程源码（在 **ak env** 里跑；父进程是 ql env，两边依赖互不污染）
_AK_WORKER_SOURCE = r'''
"""每日 ETF 日线增量抓取（在 **ak env** 里运行；由 fetch_data.py 通过子进程调用）。"""
import json
import random
import sys
import time
import traceback

import akshare as ak
import pandas as pd

#: akshare `fund_etf_hist_em` 的中文列 → 本项目的英文列
COL_MAP = {
    "日期": "date", "开盘": "open", "最高": "high", "最低": "low",
    "收盘": "close", "成交量": "volume", "成交额": "amount",
    "涨跌幅": "pct_chg", "换手率": "turnover",
}
OUT_COLS = [
    "code", "date", "open", "high", "low", "close", "volume", "amount", "ret",
]
#: 需要转成数值的列（akshare 给的是字符串）
NUM_COLS = ["open", "high", "low", "close", "volume", "amount"]


def fetch_one(code, start_date, end_date, max_retries, sleep_min, sleep_max):
    """抓单只 ETF 的日线（重试 + 退避）。返回 (df, error)。"""
    last = None
    for attempt in range(max_retries):
        try:
            df = ak.fund_etf_hist_em(
                symbol=code,
                period="daily",
                start_date=start_date,
                end_date=end_date,
                # 🔴 **必须** qfq：canonical `etf_prices.csv` 是研究链
                #    `data/fetch_etf_prices.py` 用 adjust="qfq" 抓的
                #    ⇒ 增量口径不一致 ⇒ 合并后价格会跳空（均线/破位全废）
                adjust="qfq",
            )
            if df is None or df.empty:
                last = "空数据"
            else:
                out = df.rename(columns=COL_MAP)
                out["code"] = str(code)
                for col in OUT_COLS:
                    if col not in out.columns:
                        out[col] = None
                for col in NUM_COLS:
                    out[col] = pd.to_numeric(out[col], errors="coerce")
                # 🔴 akshare 的 `涨跌幅` 是**百分数**（`1.23` 表示 1.23%）
                #    ⇒ 统一换算成小数列 `ret`，下游免得再猜单位
                out["ret"] = pd.to_numeric(out.get("pct_chg"), errors="coerce") / 100.0
                out["date"] = pd.to_datetime(out["date"]).dt.strftime("%Y-%m-%d")
                out = out.dropna(subset=["date"])
                return out[OUT_COLS], None
        except Exception as exc:  # noqa: BLE001 - 逐只记录失败原因，不让单只炸掉整轮
            last = f"{type(exc).__name__}: {exc}"
        if attempt < max_retries - 1:
            time.sleep(sleep_min + attempt * (sleep_max - sleep_min))
    return None, last


def main():
    spec = json.loads(sys.argv[1])
    codes = spec["codes"]
    frames, failed = [], []
    for i, code in enumerate(codes, 1):
        df, err = fetch_one(
            code, spec["start_date"], spec["end_date"],
            spec["max_retries"], spec["sleep_min"], spec["sleep_max"],
        )
        if df is None:
            failed.append({"code": code, "error": err})
        else:
            frames.append(df)
        if i % 25 == 0:
            print(f"[ak] {i}/{len(codes)}", flush=True)
        # 🔴 **每次请求之间都限速**（和研究链 `fetch_etf_prices.py` 同口径）：
        #    连续几百次请求不打 eastmoney ⇒ IP 封禁 ⇒ 每日链路直接瘫。
        time.sleep(random.uniform(spec["sleep_min"], spec["sleep_max"]))
    if frames:
        out = pd.concat(frames, ignore_index=True)
    else:
        out = pd.DataFrame(columns=OUT_COLS)
    out.to_csv(spec["out_path"], index=False, encoding="utf-8-sig")
    with open(spec["meta_path"], "w", encoding="utf-8") as fp:
        json.dump({"rows": int(len(out)), "failed": failed}, fp, ensure_ascii=False)
    print(f"[ak] 完成：{len(out)} 行，失败 {len(failed)} 只", flush=True)
    return 0


if __name__ == "__main__":
    try:
        sys.exit(main())
    except Exception:
        traceback.print_exc()
        sys.exit(1)
'''


# ---------------------------------------------------------------- 研究链模块加载
_REFRESH_POLICY = None


def _refresh_policy():
    """按**绝对路径**加载研究链的 `data/refresh_policy.py`（它不是包，不能 `import`）。"""
    global _REFRESH_POLICY
    if _REFRESH_POLICY is None:
        path = _RESEARCH_ROOT / "data" / "refresh_policy.py"
        spec = importlib.util.spec_from_file_location("etf_rotation_refresh_policy_data", path)
        if spec is None or spec.loader is None:  # pragma: no cover - 防御
            raise RuntimeError(f"研究链模块加载失败：`{path}`")
        module = importlib.util.module_from_spec(spec)
        sys.modules[spec.name] = module
        spec.loader.exec_module(module)
        _REFRESH_POLICY = module
    return _REFRESH_POLICY


# ---------------------------------------------------------------- 行情增量
#: A 股日线**定型时刻**（15:00 收盘 + 5 分钟数据源余量）
BAR_FINALIZE_HH = 15
BAR_FINALIZE_MM = 5


def drop_unfinalized_bars(df: pd.DataFrame, asof: str) -> tuple[pd.DataFrame, str | None]:
    """丢掉**还没定型**的当日 bar（🔴 防污染）。

    收盘前（< 15:05）akshare 可能返回当天的**盘中 / 未收盘** bar
    ⇒ 写进缓存会污染 MA20 / MA60 与破位判定 ⇒ 一律丢弃，并如实记日志。

    返回 `(过滤后的表, 说明)`；说明为 None 表示没丢过东西。
    """
    now = datetime.now().astimezone()
    if (now.hour, now.minute) >= (BAR_FINALIZE_HH, BAR_FINALIZE_MM):
        return df, None
    if df.empty or "date" not in df.columns:
        return df, None
    mask = df["date"].astype(str).str.slice(0, 10) == str(asof)
    n = int(mask.sum())
    if n == 0:
        return df, None
    note = (
        f"⏳ 当日 bar 尚未定型（现在 {now:%H:%M} < "
        f"{BAR_FINALIZE_HH:02d}:{BAR_FINALIZE_MM:02d}）⇒ 已丢弃 {n} 行 "
        f"asof={asof} 的未收盘数据（均线将用上一个交易日）"
    )
    return df[~mask].copy(), note


def _work_root() -> Path:
    """临时工作目录：优先 `/mnt/data`（铁律二）；写不了就退回**每日产物根**。

    🔴 **绝不**落 `/tmp`（tmpfs ⇒ 重启即失 ⇒ 增量白抓）。
    """
    for root in (_WORK_ROOT, Path(DC.RUN_ROOT)):
        try:
            root.mkdir(parents=True, exist_ok=True)
            return root
        except OSError:
            continue
    raise RuntimeError(f"临时工作目录都建不出来：{_WORK_ROOT} / {DC.RUN_ROOT}")


def _pool_codes(universe_csv: Path | None = None) -> list[str]:
    """候选池代码列表（`etf_universe.csv`）。

    口径与研究链 `data/fetch_etf_prices.py` 一致：优先 `fetch_candidate`（含最终候选池），
    没有该列则退到 `in_universe`，都没有则用全表。
    """
    path = Path(universe_csv) if universe_csv else DC.ETF_UNIVERSE_CSV
    if not path.exists():
        return []
    df = pd.read_csv(path, dtype={"code": str})
    if "code" not in df.columns:
        return []
    df["code"] = df["code"].astype(str).str.zfill(6)
    for flag in ("fetch_candidate", "in_universe"):
        if flag in df.columns:
            mask = df[flag].astype(str).str.lower().isin(("true", "1", "yes"))
            sub = df.loc[mask, "code"].tolist()
            if sub:
                return sorted(set(sub))
    return sorted(set(df["code"].tolist()))


def fetch_etf_daily_bars(
    *,
    codes: Sequence[str] | None = None,
    end_date: str | None = None,
    ak_python: str | None = None,
    sleep_min: float = 0.2,
    sleep_max: float = 0.5,
    max_retries: int = 3,
    lookback_days: int = 15,
    out_csv: str | Path | None = None,
    dry_run: bool = False,
) -> dict:
    """抓 ETF 日线增量（**ak env 子进程**；父进程绝不 import akshare）。

    `lookback_days` = 回看**自然日**（默认 15）⇒ 覆盖最近一个交易日 + 冗余；
    实际只保留 `end_date`（含）及之前的数据。

    返回：`{"rows","n_codes","codes_ok","codes_failed","out_csv","elapsed_sec","dry_run"}`。
    `dry_run=True` ⇒ 只返回计划，**不联网、不起子进程**。

    🔴 单只失败**逐只记录**（`codes_failed`）但**整体不抛**；
    **全部**失败 ⇒ 抛异常（静默返回 0 行 ⇒ 下游会拿空行情算均线）。
    """
    t0 = time.time()
    codes = list(codes) if codes else _pool_codes()
    end = end_date or DC.NEXT_SIGNAL_DATE
    if dry_run:
        return {
            "rows": 0,
            "n_codes": len(codes),
            "codes_ok": [],
            "codes_failed": [],
            "out_csv": None,
            "elapsed_sec": 0.0,
            "dry_run": True,
            "note": f"dry-run：计划抓 {len(codes)} 只 ETF 的日线（回看 {lookback_days} 自然日）",
        }
    if not codes:
        raise RuntimeError(f"候选池为空（universe={DC.ETF_UNIVERSE_CSV}）⇒ 无从抓取")

    ak_py = ak_python or DC.AK_PY
    if not Path(ak_py).exists():
        raise RuntimeError(f"找不到 ak 环境解释器：{ak_py}")

    end_dt = datetime.strptime(end, "%Y-%m-%d")
    start = (end_dt - timedelta(days=int(lookback_days))).strftime("%Y%m%d")
    end_compact = end_dt.strftime("%Y%m%d")

    work_dir = _work_root() / f"_etf_daily_{end_compact}"
    work_dir.mkdir(parents=True, exist_ok=True)
    script = work_dir / "_ak_fetch_bars.py"
    script.write_text(_AK_WORKER_SOURCE, encoding="utf-8")
    raw_csv = work_dir / "bars_raw.csv"
    meta_json = work_dir / "bars_meta.json"
    spec = {
        "codes": codes,
        "start_date": start,
        "end_date": end_compact,
        "out_path": str(raw_csv),
        "meta_path": str(meta_json),
        "max_retries": int(max_retries),
        "sleep_min": float(sleep_min),
        "sleep_max": float(sleep_max),
    }
    proc = subprocess.run(
        [ak_py, str(script), json.dumps(spec)], capture_output=True, text=True, check=False
    )
    if proc.returncode != 0 or not raw_csv.exists():
        raise RuntimeError(
            f"ak 子进程失败（退出码 {proc.returncode}）：{(proc.stderr or '')[-500:]}"
        )
    df = pd.read_csv(raw_csv, dtype={"code": str})
    df["code"] = df["code"].astype(str).str.zfill(6)
    df["date"] = df["date"].astype(str).str.slice(0, 10)
    df = df[df["date"] <= end].copy()
    failed: list[dict] = []
    if meta_json.exists():
        try:
            failed = json.loads(meta_json.read_text(encoding="utf-8")).get("failed", [])
        except json.JSONDecodeError:
            failed = []
    if df.empty:
        first = failed[0] if failed else {"code": "?", "error": "无数据"}
        raise RuntimeError(f"全部 {len(failed)} 只 ETF 抓取失败（第一只：{first}）")

    out_path = Path(out_csv) if out_csv else (work_dir / "bars.csv")
    df.to_csv(out_path, index=False, encoding="utf-8-sig")
    return {
        "rows": int(len(df)),
        "n_codes": len(codes),
        "codes_ok": sorted(df["code"].unique().tolist()),
        "codes_failed": failed,
        "out_csv": str(out_path),
        "elapsed_sec": round(time.time() - t0, 1),
        "dry_run": False,
    }


# ---------------------------------------------------------------- 落盘与合并
def _canonical_columns(canonical: Path) -> list[str] | None:
    """探测 canonical 行情文件的列集合；缺必需列 ⇒ 抛中文异常（不许猜列名）。"""
    if not canonical.exists():
        return None
    head = pd.read_csv(canonical, nrows=0)
    cols = list(head.columns)
    missing = [c for c in _PRICE_COLS if c not in cols]
    if missing:
        raise ValueError(
            f"canonical 行情文件 `{canonical}` 缺少必需列：{missing}（实际列：{cols}）"
        )
    return cols


def write_daily_prices(df: pd.DataFrame, out_dir: str | Path, asof: str) -> Path:
    """把当日增量写成 `<out_dir>/etf_prices_daily_<YYYYMMDD>.csv`（列与 canonical 对齐）。

    🔴 增量列**只留 canonical 的列**（akshare 的 `ret` 等中间列不落盘），
    否则合并时 canonical 那部分会整片变成 NaN。

    文件里是**这个 run 抓到的整段回看窗口**（默认 15 自然日，不只是 asof 那天）
    ⇒ 某天漏跑时，后一天的窗口能把它**补回来**（合并时按 `(code, date)` 去重）。
    """
    if "code" not in df.columns or "date" not in df.columns:
        raise ValueError(f"当日增量缺少 code/date 列（实际列：{list(df.columns)}）⇒ 不落盘")
    out = Path(out_dir)
    out.mkdir(parents=True, exist_ok=True)
    path = out / f"etf_prices_daily_{asof.replace('-', '')}.csv"
    cols = _canonical_columns(DC.ETF_PRICES_CSV) or list(_PRICE_COLS)
    data = df.copy()
    for col in cols:
        if col not in data.columns:
            data[col] = pd.NA
    data["code"] = data["code"].astype(str).str.zfill(6)
    data["date"] = data["date"].astype(str).str.slice(0, 10)
    data = data[list(cols)]
    data.to_csv(path, index=False, encoding="utf-8-sig")
    return path


def load_effective_prices(
    *,
    asof: str | None = None,
    run_root: str | Path | None = None,
    canonical_csv: str | Path | None = None,
) -> pd.DataFrame:
    """canonical `etf_prices.csv` + `<run_root>/*/etf_prices_daily_*.csv` 合并去重。

    同一 `(code, date)` 以**增量文件为准**（更新鲜）；按 `code, date` 升序。
    缺必需列 ⇒ 抛中文异常（**不许**静默用错列名算出假行情）。
    """
    frames: list[pd.DataFrame] = []
    canonical = Path(canonical_csv) if canonical_csv else DC.ETF_PRICES_CSV
    if canonical.exists():
        canonical_cols = _canonical_columns(canonical)
        frames.append(pd.read_csv(canonical, dtype={"code": str}))
        _ = canonical_cols
    base = Path(run_root) if run_root else DC.RUN_ROOT
    if base.exists():
        for path in sorted(base.glob("**/etf_prices_daily_*.csv")):
            frames.append(pd.read_csv(path, dtype={"code": str}))
    if not frames:
        return pd.DataFrame(columns=list(_PRICE_COLS))
    df = pd.concat(frames, ignore_index=True)
    missing = [c for c in ("code", "date") if c not in df.columns]
    if missing:
        raise ValueError(f"合并行情缺少必需列：{missing}（实际列：{list(df.columns)}）")
    df["code"] = df["code"].astype(str).str.zfill(6)
    df["date"] = df["date"].astype(str).str.slice(0, 10)
    df = df.drop_duplicates(subset=["code", "date"], keep="last")
    if asof:
        df = df[df["date"] <= asof]
    return df.sort_values(["code", "date"]).reset_index(drop=True)


# ---------------------------------------------------------------- W（只读）
def read_holdings_snapshot(path: str | Path | None = None, *, asof: str | None = None) -> dict:
    """**只读** W 缓存（`etf_holdings.csv`），返回新鲜度信息。

    🔴 **绝不**调用 `fetch_etf_holdings.py`（它没有只读开关 ⇒ 会重爬成分股 ⇒ IP 封禁）。
    陈旧度**只披露、不拦**（拦了每日链路就会因为「W 旧了」而停摆）。

    返回键：`path` / `exists` / `n_rows` / `n_codes` / `min_fetch_date` / `max_fetch_date` /
    `stale_days` / `stale` / `mixed` / `codes` / `note`。
    """
    path = Path(path) if path else DC.ETF_HOLDINGS_CSV
    if not path.exists():
        return {
            "path": str(path),
            "exists": False,
            "n_rows": 0,
            "n_codes": 0,
            "min_fetch_date": None,
            "max_fetch_date": None,
            "stale_days": None,
            "stale": False,
            "mixed": False,
            "codes": [],
            "note": "🔴 W 缓存不存在 ⇒ E=W×S 的穿透分算不出来（**只披露不拦**）",
        }
    df = pd.read_csv(path, dtype={"etf_code": str, "fetch_date": str})
    dates = _refresh_policy().holdings_snapshot_dates(df)
    min_fd = dates.get("min")
    max_fd = dates.get("max")
    base = (
        datetime.strptime(asof, "%Y-%m-%d")
        if asof
        else datetime.now().astimezone().replace(hour=0, minute=0, second=0, microsecond=0)
    )
    stale_days = None
    if max_fd:
        stale_days = (base.date() - datetime.strptime(str(max_fd), "%Y-%m-%d").date()).days
    codes = sorted(
        df["etf_code"].astype(str).str.zfill(6).unique().tolist()
    ) if "etf_code" in df.columns else []
    return {
        "path": str(path),
        "exists": True,
        "n_rows": int(len(df)),
        "n_codes": int(dates.get("n_codes") or 0),
        "min_fetch_date": min_fd,
        "max_fetch_date": max_fd,
        "stale_days": stale_days,
        "stale": bool(stale_days is not None and stale_days > DC.HOLDINGS_STALE_DAYS_DISCLOSE),
        "mixed": bool(dates.get("mixed")),
        "codes": codes,
        "note": (
            f"W 快照 {min_fd} ~ {max_fd}（滞后 {stale_days} 天，披露阈值 "
            f"{DC.HOLDINGS_STALE_DAYS_DISCLOSE} 天）—— **只披露不拦**（重爬成分股会被 IP 封禁）"
            if stale_days is not None
            else "W 快照日期读不出来"
        ),
    }


def holdings_needing_refresh(
    path: str | Path | None = None, asof: str | None = None
) -> list[str]:
    """陈旧度超阈值的 etf_code（**仅供披露**，每日链路不会去爬它们）。

    🔴 **逐 code** 判定：W 缓存可能是**混合快照**（部分 code 抓成功、部分保留旧行）
    ⇒ 只看全局 max 会低估滞后。
    """
    target = Path(path) if path else DC.ETF_HOLDINGS_CSV
    if not target.exists():
        return []
    df = pd.read_csv(target, dtype={"etf_code": str, "fetch_date": str})
    if "fetch_date" not in df.columns or "etf_code" not in df.columns:
        return []
    df = df.copy()
    df["_fd"] = pd.to_datetime(df["fetch_date"], errors="coerce")
    df = df.dropna(subset=["_fd"])
    if df.empty:
        return []
    base = (
        datetime.strptime(asof, "%Y-%m-%d").date()
        if asof
        else datetime.now().astimezone().date()
    )
    latest = df.groupby(df["etf_code"].astype(str).str.zfill(6))["_fd"].max()
    stale = [
        code
        for code, day in latest.items()
        if (base - day.date()).days > DC.HOLDINGS_STALE_DAYS_DISCLOSE
    ]
    return sorted(stale)


# ---------------------------------------------------------------- qlib 二进制
def _read_qlib_bin(path: Path) -> tuple[int, np.ndarray]:
    """读 qlib 的 float32 bin ⇒ (起始日历下标, 值数组)。

    🔴 qlib 的 bin 布局：**第 1 个 float32 是「起始日历下标」**，其后才是各交易日的值
    （缺失写 NaN）⇒ 不能按「第 0 个是值」解析。
    """
    if not path.exists():
        return 0, np.empty(0, dtype="<f4")
    arr = np.fromfile(path, dtype="<f4")
    if arr.size == 0:
        return 0, np.empty(0, dtype="<f4")
    return int(arr[0]), arr[1:].copy()


def _write_qlib_bin(
    path: Path, start: int, values: np.ndarray, target_index: int, value: float
) -> None:
    """把 `value` 写到 bin 的第 `target_index` 个日历位置。

    对齐 `scripts/dump_bin.py` 的 UPDATE_MODE：末尾 ⇒ **追加** float32（O(1)）；
    中间/有缺口 ⇒ 重写整个文件（保留首元素「起始下标」）。
    """
    slot = target_index - start
    if slot < 0:
        raise ValueError(f"目标日历下标 {target_index} 小于 bin 起始下标 {start} ⇒ 不写")
    if slot == len(values):
        with path.open("ab") as fp:
            np.array([value], dtype="<f4").tofile(fp)
        return
    vals = values.astype("<f4", copy=True)
    if slot > len(vals):
        vals = np.concatenate([vals, np.full(slot - len(vals), np.nan, dtype="<f4")])
    vals[slot] = value
    tmp = path.with_name(path.name + ".tmp")
    np.hstack([np.array([start], dtype="<f4"), vals]).astype("<f4").tofile(tmp)
    tmp.replace(path)


def _qlib_code_dirs(provider: Path) -> dict[str, Path]:
    """`{6 位代码: features/<code_lowercase> 目录}`（qlib 的目录名是 `sh/sz/bj` + 代码）。"""
    out: dict[str, Path] = {}
    feats = provider / "features"
    if not feats.is_dir():
        return out
    for item in feats.iterdir():
        if not item.is_dir():
            continue
        name = item.name.lower()
        if len(name) >= 8 and name[-6:].isdigit():
            out[name[-6:]] = item
    return out


def _read_qlib_calendar(cal_path: Path) -> list[str]:
    """读 qlib 日历（`calendars/day.txt`，一行一个 `YYYY-MM-DD`）。"""
    if not cal_path.exists():
        return []
    return [line.strip() for line in cal_path.read_text(encoding="utf-8").splitlines() if line.strip()]


def _to_float(value) -> float:
    """宽松转 float（读不出来 ⇒ NaN）。"""
    try:
        out = float(value)
    except (TypeError, ValueError):
        return float("nan")
    return out if np.isfinite(out) else float("nan")


def _ak_return(code: str, day: str, bars: pd.DataFrame) -> float | None:
    """akshare 在 `day` 的**日收益**（小数口径）。

    akshare 的 `涨跌幅` 是**百分数**（`1.23` 表示 1.23%）⇒ 这里统一换算成小数。
    """
    rows = bars[(bars["code"] == code) & (bars["date"] == day)]
    if rows.empty:
        return None
    row = rows.iloc[0]
    if "ret" in row.index:
        value = _to_float(row["ret"])
        if np.isfinite(value):
            return value
    if "pct_chg" in row.index:
        # akshare 的 `涨跌幅` 是**百分数** ⇒ 一律除 100（不按大小猜单位）
        value = _to_float(row["pct_chg"])
        if np.isfinite(value):
            return value / 100.0
    prior = bars[(bars["code"] == code) & (bars["date"] < day)].sort_values("date")
    if prior.empty:
        return None
    prev_close = _to_float(prior.iloc[-1].get("close"))
    close = _to_float(row.get("close"))
    if not np.isfinite(prev_close) or prev_close == 0.0 or not np.isfinite(close):
        return None
    return close / prev_close - 1.0


def _qlib_return(
    cdir: Path, close_start: int, close_vals: np.ndarray, cal_pos: int
) -> tuple[float | None, str]:
    """qlib **复权序列**（`close × factor`）在 `cal_pos` 的日收益。

    优先用复权序列（qlib 的 `$close` 口径）；factor 缺失时退回未复权 close。
    """
    series = close_vals.astype("float64")
    source = "close"
    factor_start, factor_vals = _read_qlib_bin(cdir / "factor.day.bin")
    if factor_vals.size == close_vals.size and factor_start == close_start:
        series = series * factor_vals.astype("float64")
        source = "close×factor"
    upto = max(0, cal_pos - close_start + 1)
    seg = series[:upto]
    finite = np.nonzero(np.isfinite(seg))[0]
    if finite.size < 2:
        return None, source
    last, prev = int(finite[-1]), int(finite[-2])
    if seg[prev] == 0.0:
        return None, source
    return float(seg[last] / seg[prev] - 1.0), source


def _field_value(cdir: Path, field: str, cal_pos: int) -> float:
    """某个字段 bin 在 `cal_pos`（日历下标）上的值；缺失 ⇒ NaN。"""
    start, vals = _read_qlib_bin(cdir / f"{field}.day.bin")
    if vals.size == 0:
        return float("nan")
    pos = cal_pos - start
    if pos < 0 or pos >= vals.size:
        return float("nan")
    return float(vals[pos])


def _plan_one_code(
    code: str,
    row: pd.Series,
    bars: pd.DataFrame,
    cal: list[str],
    code_dirs: dict[str, Path],
    threshold: float,
) -> tuple[str, dict]:
    """算出单个标的的 qlib 写入计划。

    返回 `(status, payload)`：`status` ∈ `{"ok", "skip", "diverged"}`。
    """
    cdir = code_dirs.get(code)
    if cdir is None:
        return "skip", {"reason": "qlib 数据集里没有该标的（cn_data 只有个股）⇒ 跳过"}

    close_start, close_vals = _read_qlib_bin(cdir / "close.day.bin")
    if close_vals.size == 0:
        return "skip", {"reason": "qlib 里没有该标的的 close bin ⇒ 跳过"}
    finite = np.nonzero(np.isfinite(close_vals))[0]
    if finite.size == 0:
        return "skip", {"reason": "qlib 里该标的有效收盘价为空 ⇒ 跳过"}
    cal_pos = close_start + int(finite[-1])
    if cal_pos >= len(cal):
        return "skip", {"reason": f"qlib bin 的日历下标 {cal_pos} 超出现有日历 ⇒ 跳过"}
    d_prev = cal[cal_pos]

    prev_rows = bars[(bars["code"] == code) & (bars["date"] == d_prev)]
    if prev_rows.empty:
        return "skip", {"reason": f"缺 {d_prev} 的 akshare 行情 ⇒ 无法比对复权（宁可不写）"}
    prev = prev_rows.iloc[0]

    # ---- 复权守卫：上一交易日的 qlib 日收益 vs akshare 日涨跌幅 ----
    qlib_ret, qlib_src = _qlib_return(cdir, close_start, close_vals, cal_pos)
    ak_ret = _ak_return(code, d_prev, bars)
    if qlib_ret is None:
        return "skip", {"reason": "qlib 的复权序列算不出日收益 ⇒ 无法比对（宁可不写）"}
    if ak_ret is None:
        return "skip", {"reason": f"akshare 在 {d_prev} 没有涨跌幅 ⇒ 无法比对（宁可不写）"}
    diff = abs(qlib_ret - ak_ret)
    if diff > threshold:
        return "diverged", {
            "code": code,
            "date": d_prev,
            "qlib_ret": round(qlib_ret, 6),
            "ak_ret": round(ak_ret, 6),
            "diff": round(diff, 6),
            "threshold": threshold,
            "qlib_source": qlib_src,
            "reason": (
                f"复权偏差 {diff:.4f} > 阈值 {threshold}（{d_prev}：qlib {qlib_ret:.4%} vs "
                f"akshare {ak_ret:.4%}）⇒ 疑似除权除息 ⇒ **剔除、不写脏数据**"
            ),
        }

    # ---- 新一天的取值：每个字段按「qlib 上一日值 / akshare 上一日值」定标 ----
    values: dict[str, float] = {}
    for field, ak_field in _QLIB_FIELD_SOURCE.items():
        qlib_val = _field_value(cdir, field, cal_pos)
        ak_prev_val = _to_float(prev.get(ak_field))
        ak_val = _to_float(row.get(ak_field))
        if np.isfinite(qlib_val) and np.isfinite(ak_val) and np.isfinite(ak_prev_val) and ak_prev_val != 0.0:
            values[field] = float(ak_val * (qlib_val / ak_prev_val))
        else:
            values[field] = float("nan")
    factor_val = _field_value(cdir, "factor", cal_pos)
    values["factor"] = factor_val if np.isfinite(factor_val) else float("nan")
    ak_ret_today = _ak_return(code, str(row.get("date")), bars)
    values["change"] = ak_ret_today if ak_ret_today is not None else float("nan")
    vwap_val = _field_value(cdir, "vwap", cal_pos)
    if np.isfinite(vwap_val) and ak_ret_today is not None:
        values["vwap"] = float(vwap_val * (1.0 + ak_ret_today))
    else:
        values["vwap"] = float("nan")
    return "ok", {
        "code": code,
        "cdir": cdir,
        "d_prev": d_prev,
        "values": values,
        "qlib_ret": round(qlib_ret, 6),
        "ak_ret": round(ak_ret, 6),
        "diff": round(diff, 6),
    }


def _append_qlib_calendar(cal_path: Path, day: str) -> bool:
    """把 `day` 追加到 qlib 日历（已存在 ⇒ 不重复追加）。"""
    lines = cal_path.read_text(encoding="utf-8").splitlines()
    if day in [line.strip() for line in lines]:
        return False
    with cal_path.open("a", encoding="utf-8") as fp:
        fp.write(day + "\n")
    return True


def _update_instrument_end(provider: Path, symbol: str, end: str) -> bool:
    """把 `instruments/all.txt` 里该标的的结束日推到 `end`（qlib update 模式同样会做）。

    不在文件里 ⇒ 返回 False（**不新建标的**：新建要造全套 bin + 改 all.txt，风险太高）。
    """
    path = provider / "instruments" / "all.txt"
    if not path.exists():
        return False
    lines = path.read_text(encoding="utf-8").splitlines()
    changed = False
    for i, line in enumerate(lines):
        parts = line.split("\t")
        if parts and parts[0].strip().upper() == symbol.upper() and len(parts) >= 3:
            if parts[2].strip() != end:
                parts[2] = end
                lines[i] = "\t".join(parts)
                changed = True
    if changed:
        path.write_text("\n".join(lines) + "\n", encoding="utf-8")
    return changed


def update_qlib_daily_bar(
    *,
    asof: str,
    bars_df: pd.DataFrame | None = None,
    provider_dir: str | Path | None = None,
    divergence_threshold: float | None = None,
    dry_run: bool = True,
) -> dict:
    """qlib 二进制增量（**默认 dry-run**：只报告，不写盘）。

    🔴 **复权守卫（硬要求）**：spot 接口给的是**未复权价** ⇒ 做法是
    ①沿用**上一交易日**的 factor 作为起点；②用 akshare 抓到的**日涨跌幅**与 qlib 已有
    复权序列算出的日收益比对；③差异 > `DC.FACTOR_DIVERGENCE_THRESHOLD` ⇒
    **该股剔除、不写脏数据**，并把 `(code, qlib_ret, ak_ret, diff)` 记进 `diverged`。

    写盘口径对齐 `scripts/dump_bin.py` 的 UPDATE_MODE：bin 是**逐日序列**
    （`features/<code>/<field>.day.bin`，首元素是起始日历下标），
    新的一天**追加** float32；日历 `calendars/day.txt` 追加当天（已存在则不重复追加）。

    返回：`{"dry_run","written","skipped","diverged","errors","calendar_appended","notes"}`。
    """
    provider = Path(provider_dir) if provider_dir else DC.QLIB_CN_DATA
    threshold = float(
        divergence_threshold if divergence_threshold is not None
        else DC.FACTOR_DIVERGENCE_THRESHOLD
    )
    result: dict = {
        "dry_run": bool(dry_run),
        "written": 0,
        "skipped": 0,
        "diverged": [],
        "errors": [],
        "calendar_appended": False,
        "notes": "",
    }
    notes: list[str] = []

    if not provider.exists():
        result["errors"].append(f"qlib 数据集目录不存在：{provider}")
        result["notes"] = "；".join(notes)
        return result

    bars = _normalize_bars(bars_df)
    if bars is None:
        notes.append("没有当日 bar ⇒ 不更新 qlib 二进制")
        result["notes"] = "；".join(notes)
        return result

    cal_path = provider / "calendars" / "day.txt"
    cal = _read_qlib_calendar(cal_path)
    if not cal:
        result["errors"].append(f"qlib 日历文件不存在或为空：{cal_path}")
        result["notes"] = "；".join(notes)
        return result

    today_rows = bars[bars["date"] == asof]
    if today_rows.empty:
        notes.append(f"bars 里没有 {asof} 的行 ⇒ 不更新 qlib 二进制")
        result["notes"] = "；".join(notes)
        return result

    if asof in cal:
        target_index = cal.index(asof)
        append_calendar = False
    elif asof > cal[-1]:
        target_index = len(cal)
        append_calendar = True
    else:
        result["errors"].append(
            f"{asof} 落在 qlib 日历 {cal[0]} ~ {cal[-1]} 的**空隙**里 ⇒ 不写"
        )
        result["notes"] = "；".join(notes)
        return result

    code_dirs = _qlib_code_dirs(provider)
    plans: list[dict] = []
    for code, group in today_rows.groupby("code", sort=True):
        status, payload = _plan_one_code(
            str(code), group.iloc[0], bars, cal, code_dirs, threshold
        )
        if status == "diverged":
            result["diverged"].append(payload)
            result["skipped"] += 1
            notes.append(str(payload.get("reason")))
        elif status == "skip":
            result["skipped"] += 1
            notes.append(f"{payload.get('code', code)}：{payload.get('reason')}")
        else:
            plans.append(payload)

    notes.append(
        f"（dry-run）计划写入 {len(plans)} 个标的的 {asof} bar；复权守卫阈值 {threshold:.3f}"
        if dry_run
        else f"写入 {len(plans)} 个标的的 {asof} bar；复权守卫阈值 {threshold:.3f}"
    )
    if not plans:
        notes.append("没有可安全写入的标的（qlib cn_data 只有个股 ⇒ ETF 增量不会落进 qlib）")

    for plan in plans:
        code = plan["code"]
        cdir: Path = plan["cdir"]
        try:
            for field in _QLIB_FIELDS:
                value = float(plan["values"].get(field, float("nan")))
                path = cdir / f"{field}.day.bin"
                start, values = _read_qlib_bin(path)
                if dry_run:
                    continue
                _write_qlib_bin(path, start, values, target_index, value)
            if not dry_run:
                _update_instrument_end(provider, cdir.name.upper(), asof)
            result["written"] += 1
        except Exception as exc:  # noqa: BLE001 - 单只写失败不许拖垮整轮
            result["skipped"] += 1
            result["errors"].append(f"{code} 写 qlib bin 失败：{type(exc).__name__}: {exc}")

    if dry_run:
        notes.append(
            "dry-run ⇒ `written` 是「**计划**写入」的个数，**没有写任何文件**"
            "（真写要显式传 dry_run=False）"
        )
        if append_calendar:
            notes.append(f"（dry-run）计划把 {asof} 追加到 qlib 日历")
    elif append_calendar and result["written"]:
        result["calendar_appended"] = _append_qlib_calendar(cal_path, asof)
        notes.append(f"qlib 日历已追加 {asof}" if result["calendar_appended"] else "qlib 日历已含该日")

    result["notes"] = "；".join(notes)
    return result


def _normalize_bars(bars_df: pd.DataFrame | None) -> pd.DataFrame | None:
    """统一 bars 的列（code 补零、date 截断到 `YYYY-MM-DD`）；空表 ⇒ None。"""
    if bars_df is None or getattr(bars_df, "empty", True):
        return None
    df = bars_df.copy()
    if "code" not in df.columns or "date" not in df.columns:
        raise ValueError(f"bars 缺少 code/date 列（实际列：{list(df.columns)}）")
    df["code"] = df["code"].astype(str).str.zfill(6)
    df["date"] = df["date"].astype(str).str.slice(0, 10)
    return df


# ---------------------------------------------------------------- 编排
def run_fetch(
    asof: str | None = None,
    *,
    out_dir: str | Path | None = None,
    dry_run: bool = False,
    update_qlib: bool = False,
    codes: Sequence[str] | None = None,
    ak_python: str | None = None,
) -> dict:
    """盘后取数编排：行情增量 → 落盘 → W 只读披露 →（可选）qlib 二进制。

    返回：`{"asof","daily_prices_csv","n_rows","holdings","qlib","guard_note","elapsed_sec"}`。
    任一步失败 ⇒ **抛异常**（由 `main.py` 兜底发失败告警），不许静默。
    """
    t0 = time.time()
    asof = asof or datetime.now().astimezone().date().isoformat()
    out = Path(out_dir) if out_dir else (DC.RUN_ROOT / asof.replace("-", ""))
    out.mkdir(parents=True, exist_ok=True)

    bars_info = fetch_etf_daily_bars(
        codes=codes, end_date=asof, ak_python=ak_python, dry_run=dry_run
    )
    daily_csv = None
    n_rows = 0
    guard_note: str | None = None
    raw: pd.DataFrame | None = None
    if not dry_run:
        raw = pd.read_csv(bars_info["out_csv"], dtype={"code": str})
        raw, guard_note = drop_unfinalized_bars(raw, asof)
        n_rows = int(len(raw))
        daily_csv = str(write_daily_prices(raw, out, asof))

    holdings = read_holdings_snapshot(asof=asof)
    if update_qlib:
        qlib = update_qlib_daily_bar(asof=asof, bars_df=raw, dry_run=False)
    else:
        qlib = {
            "dry_run": True,
            "written": 0,
            "skipped": 0,
            "diverged": [],
            "errors": [],
            "calendar_appended": False,
            "notes": "未开启 qlib 二进制增量（--update-qlib-bin 才开）",
        }
    return {
        "asof": asof,
        "daily_prices_csv": daily_csv,
        "n_rows": n_rows,
        "holdings": holdings,
        "qlib": qlib,
        "guard_note": guard_note,
        "elapsed_sec": round(time.time() - t0, 1),
    }


if __name__ == "__main__":  # pragma: no cover - 手工自检
    print(json.dumps(read_holdings_snapshot(), ensure_ascii=False, indent=2))
