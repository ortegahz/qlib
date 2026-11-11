"""run_qlib_score.py —— S 层（个股截面分数）的**新鲜度判定**与可选增量刷新。

【S 层是什么】
`manu/etf_rotation/qlib_scores.py --mode infer` 产出 `stock_scores.csv`
（列：`date, instrument, score, fwd_ret`）⇒ 每个打分日一个**全市场截面**。
下游的 `E = W × S`（个股穿透打分）直接消费它。

【🔴 三个必须知道的事实（都经实测，不是推测）】
1. **打分日是「相位栅格」**：交易日历下标 `i` 满足 `i % every_n == every_n-1`
   （`every_n = config.REBALANCE_EVERY_N_DAYS = 10`）。栅格的事实源是
   `manu/etf_rotation/data/signal_policy.py::score_grid_dates`（**不许**在这里另写相位）。
2. **栅格受 qlib 数据集覆盖末日封顶**：栅格由 `trading_calendar(SCORE_START_TIME, cal_end)`
   算出，而 `cal_end` 不会超过 qlib 数据集的末日（当前 **2026-11-03**）⇒
   **2026-11-04 及以后（含 2026-12-01）根本不在栅格上** ⇒ 用现有数据集**打不出**新分数。
   ⇒ 要打新 S **必须先重建 qlib 数据集**（重活 ⇒ 需用户当轮授权）。
3. 🔴 **`qlib_scores.py` 会整体覆盖 `stock_scores.csv`，不是增量**：
   它的落盘是 `scores.to_csv(out_dir/"stock_scores.csv")`，产物**只含 `--start ~ --end`
   栅格上的日期** ⇒ 拿它当增量用会**抹掉**已有的 69 个打分日。
   ⇒ 本模块**默认只读**；真要刷新，必须**输出到临时目录 → 合并回 canonical（先备份）**。

【本模块的职责边界】
- **判定**：S 层相对目标日期新不新鲜？栅格里缺哪几天？**如实报告**，不编数字。
- **可选刷新**：`refresh=True` 时才真的跑 `qlib_scores.py`，且必须走「临时目录 + 合并 + 备份」。
- **默认路径绝不触发刷新**（铁律一：禁擅自启动重活；`--mode infer` 实测 ≈27 分钟）。
"""
from __future__ import annotations

import importlib.util
import shutil
import sys
from pathlib import Path

import pandas as pd

_HERE = Path(__file__).resolve().parent
if str(_HERE) not in sys.path:
    sys.path.insert(0, str(_HERE))

import daily_config as DC  # noqa: E402

#: 研究链根（`manu/etf_rotation`）
_RESEARCH_DIR = _HERE.parent


def _load_signal_policy():
    """按绝对路径加载 `manu/etf_rotation/data/signal_policy.py`（研究链不是包）。"""
    path = _RESEARCH_DIR / "data" / "signal_policy.py"
    spec = importlib.util.spec_from_file_location("manu_daily_signal_policy", path)
    if spec is None or spec.loader is None:
        raise ImportError(f"无法加载信号策略模块：{path}")
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    # 🔴 必须先登记进 sys.modules 再 exec_module（@dataclass + future-annotations ⇒ dataclasses 会反查 sys.modules）
    try:
        spec.loader.exec_module(module)
    except Exception:
        sys.modules.pop(spec.name, None)
        raise
    return module


def _load_research_config():
    """加载研究链 `config.py`（拿 `REBALANCE_EVERY_N_DAYS` 等常量）。"""
    if str(_RESEARCH_DIR) not in sys.path:
        sys.path.insert(0, str(_RESEARCH_DIR))
    import config as C  # type: ignore

    return C


#: 研究链 `qlib_provider.py` 的模块级缓存（避免每次调用重复 exec 整条依赖链）
_PROVIDER_MODULE = None


def _load_qlib_provider():
    """加载研究链 `qlib_provider.py`（**provider 单一事实源**；按路径加载，非包）。"""
    global _PROVIDER_MODULE
    if _PROVIDER_MODULE is not None:
        return _PROVIDER_MODULE
    path = _RESEARCH_DIR / "qlib_provider.py"
    spec = importlib.util.spec_from_file_location("manu_daily_qlib_provider", path)
    if spec is None or spec.loader is None:
        raise ImportError(f"无法加载 provider 模块：{path}")
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    try:
        spec.loader.exec_module(module)
    except Exception:
        sys.modules.pop(spec.name, None)
        raise
    _PROVIDER_MODULE = module
    return module


def load_scores_meta(scores_csv: str | Path | None = None) -> dict:
    """读 `stock_scores.csv` 的元信息（**只读**，不做任何写操作）。

    返回：`{"path","n_rows","n_dates","dates","max_date","n_instruments","columns"}`。
    🔴 文件缺失 ⇒ 抛 `FileNotFoundError`（**不编数字**）。
    """
    path = Path(scores_csv) if scores_csv else DC.STOCK_SCORES_CSV
    if not path.exists():
        raise FileNotFoundError(
            f"个股分数文件不存在：{path} ⇒ S 层不可用（先跑 qlib_scores.py --mode infer）"
        )
    df = pd.read_csv(path)
    if "date" not in df.columns:
        raise ValueError(f"个股分数文件缺少 `date` 列：{path}（实际列：{list(df.columns)}）")
    df["date"] = df["date"].astype(str).str.slice(0, 10)
    dates = sorted(df["date"].unique().tolist())
    n_inst = int(df["instrument"].nunique()) if "instrument" in df.columns else 0
    return {
        "path": str(path),
        "n_rows": int(len(df)),
        "n_dates": len(dates),
        "dates": dates,
        "max_date": dates[-1] if dates else None,
        "n_instruments": n_inst,
        "columns": list(df.columns),
    }


def score_grid(
    *, calendar_path: str | Path | None = None, every_n: int | None = None,
    start: str | None = None, end: str | None = None,
) -> list[str]:
    """算打分日栅格（委托 `signal_policy.score_grid_dates` —— **唯一事实源**）。

    日历缺省用**本次选定的 provider** 的 `calendars/day.txt`
    （与研究链 `qlib_scores.trading_calendar` 同源）。

    🔴🔴 **口径同源（2026-11-06 09:2x 实测修复）**：相位口径是
    ``i % every_n == every_n - 1``，而 ``i`` 是**传入列表**的下标
    ⇒ **切片起点决定相位**！
    研究链（`qlib_scores.trading_calendar(C.SCORE_START_TIME, cal_end)`）从
    **`SCORE_START_TIME`（2023-06-01）**起算；每日链原来传的是**数据集全量日历
    （2000-01-04 起）** ⇒ 两条链的相位**差 2 个交易日**
    ⇒ 实测**共同日期只有 1 个**（末日例外）⇒ 「缺分数」判定全错
    ⇒ 每轮都会**误触发全量重打分**（21~27 分钟）＋ 报告误报「落后」。
    ⇒ 这里与研究链**逐位同源**：切片起点 = `SCORE_START_TIME`，
    切片终点 = `max(SCORE_END_TIME, end)`（研究链的 `cal_end` 规则）。
    """
    sp = _load_signal_policy()
    cfg = _load_research_config()
    every_n = int(every_n if every_n is not None else cfg.REBALANCE_EVERY_N_DAYS)
    if calendar_path:
        cal_path = Path(calendar_path)
    else:
        provider, _ = select_provider(end)
        cal_path = provider / "calendars" / "day.txt"
    cal_dates = sp.read_calendar_dates(cal_path)
    if not cal_dates:
        return []
    # 🔴 与研究链同源：切片起点 = SCORE_START_TIME，终点 = max(SCORE_END_TIME, end)
    slice_start = str(cfg.SCORE_START_TIME)
    slice_end = max(str(cfg.SCORE_END_TIME), end) if end else str(cfg.SCORE_END_TIME)
    cal_dates = [
        d
        for d in cal_dates
        if slice_start <= (d.isoformat() if hasattr(d, "isoformat") else str(d)) <= slice_end
    ]
    return sp.score_grid_dates(cal_dates, every_n, start=start, end=end)


def select_provider(asof: str | None = None) -> tuple[Path, str]:
    """选本次打分用的 qlib provider 目录 ⇒ `(目录, 理由)`。

    🔴 **2026-11-09 起：委托研究链层单一事实源 `manu/etf_rotation/qlib_provider.py`。**
    为什么要改：每日链原本**自带一份**「谁新用谁」规则、研究链又**另写一份**
    （`qlib_scores.py` 默认基座）⇒ 两条链口径会分叉（研究链永远停在基座末日）。
    ⇒ 规则提到研究链层 ⇒ 两条链共用同一实现、同一组环境变量名。

    **公开签名与语义逐位不变**（每日链零回归）：
    * 派生目录日历末日 **> 基座** ⇒ 用派生目录；
    * 否则用上游基座 `cn_data`（默认路径，零回归）；
    * 派生目录**永远不覆盖**基座 ⇒ 上游换库的前缀闸门不受影响；
    * `asof` 未被派生目录覆盖时，只在 reason 里**告警**，**不换数据源**。

    路径仍由每日链常量（`DC.QLIB_CN_DATA` / `DC.QLIB_CN_DATA_LIVE`）显式传入
    ⇒ 每日链的路径口径**不动**；与研究链默认值的一致性由
    `daily_config.cross_check_provider_paths()` 对账（防第二个字面量分叉）。
    """
    qp = _load_qlib_provider()
    return qp.select_provider(
        asof, base=Path(DC.QLIB_CN_DATA), live=Path(DC.QLIB_CN_DATA_LIVE)
    )


def ensure_scores_fresh(
    *,
    asof: str,
    scores_csv: str | Path | None = None,
    calendar_path: str | Path | None = None,
    refresh: bool = False,
    out_dir: str | Path | None = None,
    dry_run: bool = True,
    merge: bool = True,
) -> dict:
    """判定 S 层相对 `asof` 的新鲜度；`refresh=True` 时才**可选**地重跑打分。

    返回 dict：
    - `action`：`"reuse"`（直接沿用）/ `"skip_heavy"`（需要刷新但未授权）/ `"refreshed"`；
    - `max_score_date`：现有 S 的最大打分日；
    - `analysis_asof`：**建议给 `analyze.py --asof` 的日期**
      = min(现有 S 最大日, 栅格里 ≤ asof 的最后一天)；
    - `missing_grid_dates`：栅格里 ≤ asof 但 S 里没有的日期（**可能非空**，如实报）；
    - `stale`：S 是否落后于最近一个应打分日；
    - `notes`：中文说明（会进邮件/日志）；
    - `merged_into` / `backup`：仅 `refresh=True` **且 `merge=True`** 时有值。

    🔴 `merge=False`：打分**只落本次目录**，**严禁合并回 canonical** `stock_scores.csv`
    ⇒ canonical 的哈希**保持不变**（本函数会实测并披露）。
    🔴 **2026-11-10 订正**：真正保证「canonical 不被写」的是**另一套机制**：
       `agents_rebalance._refresh_scores` 只写**本快照内**的 `scores_path`，
       并在前后对 canonical `stock_scores.csv` 做 md5 对照取证；
       本函数的 `merge` 参数**只在本函数自己的 `__main__` 里以默认 `True` 使用**
       ⇒ 每日流水线**不经过本函数** ⇒ 它是**预留位**，别当成每日链的现行开关。
    """
    meta = load_scores_meta(scores_csv)
    provider, provider_reason = select_provider(asof)
    grid = score_grid(calendar_path=calendar_path, end=asof)
    grid_in_window = [d for d in grid if d <= asof]

    max_score = meta["max_date"]
    missing = [d for d in grid_in_window if d > (max_score or "")]

    # 建议的信号日：现有 S 最大日 与 栅格最后一天 取较小（保守）
    grid_last = grid_in_window[-1] if grid_in_window else None
    if max_score and grid_last:
        analysis_asof = max_score if max_score <= grid_last else grid_last
    else:
        analysis_asof = max_score or grid_last

    notes: list[str] = []
    notes.append(f"ℹ️ 打分数据源：{provider}（{provider_reason}）")
    if not missing:
        notes.append(f"S 层新鲜：现有 S 覆盖到 {max_score}，栅格内无需补的日子。")
    else:
        notes.append(
            f"🔴 S 层落后：栅格上 ≤ {asof} 的打分日里，{missing} 尚无分数"
            f"（现有 S 末日 = {max_score}）。"
        )

    result = {
        "action": "reuse",
        "asof": asof,
        "max_score_date": max_score,
        "analysis_asof": analysis_asof,
        "n_grid_dates": len(grid_in_window),
        "missing_grid_dates": missing,
        "stale": bool(missing),
        "scores_csv": meta["path"],
        "n_rows": meta["n_rows"],
        "notes": notes,
        "merged_into": None,
        "backup": None,
    }

    if not missing:
        return result

    if not refresh:
        result["action"] = "skip_heavy"
        sp = _load_signal_policy()
        provider_max = sp.calendar_max_date(provider / "calendars" / "day.txt")
        notes.append(
            f"🔴 重跑 S 层需要**扩展 qlib 数据集覆盖**（当前数据源覆盖末日 = {provider_max}，"
            f"打分日 {asof} 需要覆盖到它）⇒ 属重活且未获当轮授权 ⇒ "
            f"**本次沿用最近一次 S**，不重打分、不覆盖 stock_scores.csv。"
            f"（阶段 1 的「自建增量日」可把派生目录补到 {asof}："
            f"`stage1_rolling_increment.py --day {asof}`）"
        )
        return result

    # ---- 真刷新：临时目录 → 合并回 canonical（先备份）----------------------
    import subprocess

    scores_path = Path(meta["path"])
    work_dir = Path(out_dir) if out_dir else (DC.RUN_ROOT / asof.replace("-", "") / "_score_refresh")
    work_dir.mkdir(parents=True, exist_ok=True)
    cmd = [
        DC.QL_PY,
        str(_RESEARCH_DIR / "qlib_scores.py"),
        "--mode", "infer",
        "--start", missing[0],
        "--end", asof,
        "--out-dir", str(work_dir),
        "--provider-uri", str(provider),
    ]
    notes.append(f"🔴 正在重跑 S 层（{missing[0]} ~ {asof}），这一步实测约 27 分钟……")
    proc = subprocess.run(cmd, capture_output=True, text=True, check=False)
    new_csv = work_dir / "stock_scores.csv"
    if proc.returncode != 0 or not new_csv.exists():
        result["action"] = "skip_heavy"
        notes.append(
            f"🔴 S 层刷新失败（退出码 {proc.returncode}）⇒ 沿用原 S。"
            f"stderr 尾部：{(proc.stderr or '')[-500:]}"
        )
        return result

    new_df = pd.read_csv(new_csv)
    new_df["date"] = new_df["date"].astype(str).str.slice(0, 10)
    if not merge:
        # 🔴 **不合并模式：不合并回 canonical**（canonical 逐字节不变）
        import hashlib

        digest = hashlib.md5(scores_path.read_bytes()).hexdigest()
        result.update({
            "action": "scored_no_merge",
            "merged_into": None,
            "backup": None,
            "scores_output": str(new_csv),
            "canonical_md5": digest,
            "n_new_rows": int(len(new_df)),
        })
        notes.append(
            f"🧪 **不合并模式（merge=False）**：S 层只落独立目录 {new_csv}"
            f"（{len(new_df)} 行）⇒ **未合并回 canonical**；canonical md5 = {digest}"
        )
        return result
    old_df = pd.read_csv(scores_path)
    old_df["date"] = old_df["date"].astype(str).str.slice(0, 10)
    keep = old_df[~old_df["date"].isin(set(new_df["date"]))]
    merged = pd.concat([keep, new_df], ignore_index=True)
    merged = merged.sort_values(["date", "instrument"]).reset_index(drop=True)

    backup = scores_path.with_suffix(f".csv.bak_{asof.replace('-', '')}")
    shutil.copy2(scores_path, backup)
    merged.to_csv(scores_path, index=False, encoding="utf-8-sig")

    result.update(
        {
            "action": "refreshed",
            "merged_into": str(scores_path),
            "backup": str(backup),
            "max_score_date": str(new_df["date"].max()),
            "stale": False,
            "missing_grid_dates": [],
            "n_rows": int(len(merged)),
        }
    )
    notes.append(f"✅ S 层已刷新并**合并**回 {scores_path}（旧文件备份到 {backup.name}）。")
    return result


if __name__ == "__main__":  # pragma: no cover - 手工自检
    import json

    print(json.dumps(ensure_scores_fresh(asof=DC.NEXT_SIGNAL_DATE), ensure_ascii=False, indent=2))
