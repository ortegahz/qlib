"""从 pred.pkl 导出「模型打分 Top-K 排名清单」。

语义（务必读，别误读产物）：
    - 产出的是**模型在某个截面日的打分排名**（kind=model_score_ranking），
      **不是策略的实际持仓组合**，也不是可直接执行的买卖清单。
    - pred.pkl 由 qlib 的 SignalRecord 生成（列为 `score`，index 为
      (instrument, datetime) 的 MultiIndex）；LGBModel.predict 默认只跑 test 段
      ⇒ **pred.pkl 里只有 test 段的信号**。

产物定位（rules.md §12.8：产物目录不得互相覆盖）：
    - 优先 `--run-dir`（显式指定 runs/<recorder_id> 目录）；
    - 不给则用 `config.latest_run_dir()`（runs/ 下最新的含 manifest.json 的子目录）。
    - **禁止**按 mtime 全盘 glob mlruns/**/artifacts/pred.pkl —— 会配错 run。

用法（服务器）：
    python manu/experiments/ashare_topk_selection/export_topk.py --top 5
    python manu/experiments/ashare_topk_selection/export_topk.py --top 30 --date 2024-12-31
    python manu/experiments/ashare_topk_selection/export_topk.py \
        --run-dir /mnt/data/siping/runs/ashare_topk/runs/<recorder_id> --top 5 --all-dates
"""
from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

import pandas as pd

sys.path.insert(0, str(Path(__file__).resolve().parent))
import config as C  # noqa: E402


# ------------------------------------------------------------------ 参数与定位


def parse_args() -> argparse.Namespace:
    p = argparse.ArgumentParser(
        description="导出模型打分的 Top-N 排名（pred.pkl 只含 test 段信号）",
    )
    p.add_argument("--top", type=int, default=C.TOPK_EXPORT_N, help="导出前 N 只")
    p.add_argument("--date", default=None, help="指定交易日（默认取 pred 里最后一个交易日）")
    p.add_argument(
        "--run-dir",
        default=None,
        help="显式指定产物目录 runs/<recorder_id>（含 manifest.json / pred.pkl）；"
             "不给则自动取最新 run",
    )
    p.add_argument("--out", default=None, help="清单 JSON 落盘路径（默认 <run_dir>/top{N}.json）")
    p.add_argument(
        "--all-dates",
        action="store_true",
        help="导出 **test 段每个交易日**的 Top-N（CSV：<run_dir>/top{N}_test_dates.csv）",
    )
    return p.parse_args()


def resolve_run_dir(explicit: str | None) -> Path:
    """定位本次要读的产物目录。

    优先 `--run-dir`；否则用 `config.latest_run_dir()`。
    绝不再按 mtime 全盘 glob —— 那会把不同 run 的 pred.pkl 配错。
    """
    if explicit:
        run_dir = Path(explicit).expanduser()
        if not run_dir.is_dir():
            raise FileNotFoundError(f"--run-dir 指定的目录不存在或不是目录: {run_dir}")
        return run_dir

    latest_fn = getattr(C, "latest_run_dir", None)
    if latest_fn is None:
        raise RuntimeError("config 里没有 latest_run_dir() —— 请确认 config.py 已是最新版本")
    run_dir = latest_fn()
    if run_dir is None:
        runs_dir = Path(getattr(C, "RUNS_DIR", Path(C.EXP_ROOT) / "runs"))
        raise FileNotFoundError(
            f"{runs_dir} 下没有含 manifest.json 的 run 目录 —— 先跑 run_pipeline.py"
        )
    return Path(run_dir)


def resolve_pred_pkl(run_dir: Path) -> Path:
    """在 run 目录里取 pred.pkl；缺失时给出可行动的报错。"""
    pred_path = run_dir / "pred.pkl"
    if pred_path.is_file():
        return pred_path
    if (run_dir / "manifest.json").is_file():
        raise FileNotFoundError(
            f"{run_dir} 有 manifest.json 但没有 pred.pkl —— run_pipeline 尚未产出预测信号"
        )
    raise FileNotFoundError(
        f"{run_dir} 下既没有 pred.pkl 也没有 manifest.json —— 该目录不是产物目录"
    )


def load_manifest(run_dir: Path) -> dict:
    """读取 run 目录里的 manifest.json（没有就返回空 dict，不算致命错误）。"""
    path = run_dir / "manifest.json"
    if not path.is_file():
        return {}
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise ValueError(f"{path} 读取失败: {exc}") from exc


# ------------------------------------------------------------------ 取截面与排序


def extract_cross_section(pred_df: pd.DataFrame, target_date: pd.Timestamp) -> pd.DataFrame:
    """取 target_date 的截面。

    🔴 必须用 `xs(..., level="datetime")`：`pred_df.loc[target_date]` 对 MultiIndex
    的标量 .loc **只查 level 0（instrument）**，查 datetime 会 KeyError。
    """
    daily = pred_df.xs(target_date, level="datetime")
    if daily.index.name != "instrument":
        daily = daily.rename_axis("instrument")
    return daily


def rank_cross_section(daily: pd.DataFrame) -> pd.DataFrame:
    """确定性排序：先按 score 降序，并列分数再按 instrument 字典序升序。

    `kind="stable"` 保证同分同序 ⇒ 结果可复现。
    """
    return daily.sort_values(by=["score", "instrument"], ascending=[False, True], kind="stable")


def boundary_tie(ranked: pd.DataFrame, top_n: int) -> tuple[bool, float | None, float | None]:
    """检查截断处（第 N 名 vs 第 N+1 名）是否存在并列分数。"""
    if len(ranked) <= top_n:
        return False, None, None
    score_in = float(ranked.iloc[top_n - 1]["score"])
    score_out = float(ranked.iloc[top_n]["score"])
    return score_in == score_out, score_in, score_out


def resolve_data_last_date(manifest: dict, fallback: pd.Timestamp) -> tuple[str, str]:
    """取「数据集最后一个交易日」：优先 manifest.data_stats，其次 pred 的最后交易日。

    返回 (日期字符串, 来源说明) —— 来源必须写进产物，便于对账。
    """
    stats = manifest.get("data_stats") or {}
    for seg in ("test", "valid", "train"):
        entry = stats.get(seg) or {}
        last_dt = entry.get("last_dt")
        if last_dt:
            return str(last_dt)[:10], f"manifest.data_stats.{seg}.last_dt"
    return str(pd.Timestamp(fallback).date()), "pred.pkl 最后一个交易日（manifest 无 data_stats）"


def build_caveat(target_date: pd.Timestamp, data_last_date: str, tie: bool) -> str:
    """生成口径说明：这是「模型打分排名」，且是否等于当前时点取决于数据新鲜度。"""
    today = pd.Timestamp.now().normalize()
    try:
        lag_days = (today - pd.Timestamp(data_last_date)).days
    except (ValueError, TypeError):
        lag_days = None
    if lag_days is None:
        fresh = "数据集最后交易日未知，无法判断数据新鲜度"
    elif lag_days <= 3:
        fresh = f"数据集最后交易日距当前仅 {lag_days} 天，可视为接近当前时点"
    else:
        fresh = (f"数据集最后交易日距当前已滞后 {lag_days} 天，"
                 "**不等于当前时点**的可交易结论")
    tie_note = "并列分数已按 instrument 字典序定序（可复现）" if tie else "排序无并列"
    return (
        "这是**模型在给定截面日的打分排名**（kind=model_score_ranking），"
        "不是策略的实际持仓组合，也不是可直接执行的买卖清单；"
        f"截面日 {target_date.date()}，数据集最后交易日 {data_last_date}，{fresh}；"
        f"{tie_note}。"
    )


# ------------------------------------------------------------------ 主流程


def main() -> int:
    args = parse_args()

    # ---- 定位产物 ----
    try:
        run_dir = resolve_run_dir(args.run_dir)
        pred_path = resolve_pred_pkl(run_dir)
        manifest = load_manifest(run_dir)
    except (FileNotFoundError, RuntimeError, ValueError) as exc:
        print(f"❌ {exc}", file=sys.stderr)
        return 2

    print(f"[export] run 目录 = {run_dir}")
    print(f"[export] pred.pkl = {pred_path}")

    # ---- 读 pred ----
    try:
        pred_df = pd.read_pickle(pred_path)
    except Exception as exc:  # noqa: BLE001
        print(f"❌ 读取 {pred_path} 失败: {type(exc).__name__}: {exc}", file=sys.stderr)
        return 2

    if not isinstance(pred_df, pd.DataFrame):
        print(f"❌ {pred_path} 内容不是 DataFrame，而是 {type(pred_df).__name__}", file=sys.stderr)
        return 2
    print(f"[export] shape={pred_df.shape} columns={list(pred_df.columns)} "
          f"index_names={list(pred_df.index.names)}")

    # 🔴 断言 1：必须有 score 列（SignalRecord 把 Series 转成 'score' 列）
    if "score" not in pred_df.columns:
        print(f"❌ pred.pkl 没有 'score' 列，实际列: {list(pred_df.columns)}", file=sys.stderr)
        return 2
    # 🔴 断言 2：index 必须是含 datetime 层的 MultiIndex
    if not isinstance(pred_df.index, pd.MultiIndex) or "datetime" not in list(pred_df.index.names):
        print(f"❌ pred.pkl 的 index 不是含 'datetime' 层的 MultiIndex，"
              f"实际 index.names={list(pred_df.index.names)}", file=sys.stderr)
        return 2

    uniq_dates = pd.DatetimeIndex(sorted(pd.to_datetime(pred_df.index.get_level_values("datetime")).unique()))
    if len(uniq_dates) == 0:
        print("❌ pred.pkl 里没有任何交易日 —— 拒绝产出清单", file=sys.stderr)
        return 2

    # ---- 目标日期校验（不抛裸 KeyError）----
    if args.date:
        try:
            target_date = pd.Timestamp(args.date).normalize()
        except (ValueError, TypeError) as exc:
            print(f"❌ --date 解析失败: {args.date!r}（{exc}）", file=sys.stderr)
            return 2
        # 🔴 断言 3：目标日期必须在 pred 实际存在的交易日集合内
        if target_date not in uniq_dates:
            print(f"❌ {target_date.date()} 不是 pred 中的交易日。pred 覆盖 "
                  f"{uniq_dates[0].date()} ~ {uniq_dates[-1].date()}，共 {len(uniq_dates)} 个交易日",
                  file=sys.stderr)
            return 2
    else:
        target_date = uniq_dates[-1]

    print(f"[export] pred 覆盖 {uniq_dates[0].date()} ~ {uniq_dates[-1].date()}，"
          f"共 {len(uniq_dates)} 个交易日（pred 只含 test 段信号）")
    print(f"[export] 目标交易日: {target_date.date()}")

    # ---- 取截面 ----
    daily = extract_cross_section(pred_df, target_date)
    # 🔴 断言 4：截面非空（rules.md §12.4：禁止静默空数据）
    if daily.empty:
        print(f"❌ {target_date.date()} 截面为空 —— 拒绝产出清单", file=sys.stderr)
        return 2

    n_scored = len(daily)
    print(f"[export] 该日有打分的标的数 M = {n_scored}")

    ranked = rank_cross_section(daily)
    top = ranked.head(args.top)

    tie, score_in, score_out = boundary_tie(ranked, args.top)

    # ---- 打印明细 ----
    manifest_cfg = manifest.get("config") or {}
    instruments = manifest_cfg.get("instruments") or C.INSTRUMENTS
    print(f"\n=== {target_date.date()} Top-{args.top} 打分排名（universe={instruments}）===")
    for i, (inst, row) in enumerate(top.iterrows(), 1):
        print(f"{i:>2}. {str(inst):<12} score={float(row['score']):+.6f}")
    if tie:
        print(f"\n⚠️ 截断处存在并列分数：第 {args.top} 名 score={score_in:+.6f} 与"
              f"第 {args.top + 1} 名 score={score_out:+.6f} 相同"
              " —— 已按 instrument 字典序定序（可复现）")

    # ---- JSON 语义字段 ----
    data_last_date, data_last_date_source = resolve_data_last_date(manifest, uniq_dates[-1])
    caveat = build_caveat(target_date, data_last_date, tie)
    result = {
        "kind": "model_score_ranking",
        "as_of_date": str(target_date.date()),
        "data_last_date": data_last_date,
        "data_last_date_source": data_last_date_source,
        "top_n": args.top,
        "n_scored_instruments": n_scored,
        "instruments": instruments,
        "segments": manifest_cfg.get("segments") or {},
        "run_dir": str(run_dir),
        "recorder_id": manifest.get("recorder_id") or run_dir.name,
        "recorder_uri": manifest.get("recorder_uri"),
        "generated_at": manifest.get("generated_at"),
        "pred_pkl": str(pred_path),
        "pred_scope": "test",
        "pred_date_range": {
            "first": str(uniq_dates[0].date()),
            "last": str(uniq_dates[-1].date()),
            "n_trading_days": int(len(uniq_dates)),
        },
        "tie_at_boundary": bool(tie),
        "caveat": caveat,
        "picks": [
            {"rank": i, "instrument": str(inst), "score": float(row["score"])}
            for i, (inst, row) in enumerate(top.iterrows(), 1)
        ],
    }

    # ---- --all-dates：test 段每个交易日的 Top-N ----
    if args.all_dates:
        csv_path = run_dir / f"top{args.top}_test_dates.csv"
        csv_rows = []
        n_dates = 0
        for dt in uniq_dates:
            d = rank_cross_section(extract_cross_section(pred_df, dt))
            if d.empty:
                continue
            n_dates += 1
            for i, (inst, row) in enumerate(d.head(args.top).iterrows(), 1):
                csv_rows.append({"date": str(pd.Timestamp(dt).date()), "rank": i,
                                 "instrument": str(inst), "score": float(row["score"])})
        pd.DataFrame(csv_rows).to_csv(csv_path, index=False)
        print(f"\n[export] test 段每个交易日 Top-{args.top} 清单 CSV -> {csv_path}"
              f"（{n_dates} 个交易日，路径带 run 目录名，避免不同 run 互相覆盖）")
        result["all_dates_csv"] = {
            "path": str(csv_path),
            "scope": "test_segment_every_trading_day",
            "n_trading_days": int(n_dates),
            "note": "pred.pkl 只含 test 段信号（LGBModel.predict 默认 segment='test'），"
                    "故该 CSV 是 **test 段每个交易日**的 Top-N，不是全历史",
        }

    # ---- 落盘 JSON ----
    out_path = Path(args.out).expanduser() if args.out else run_dir / f"top{args.top}.json"
    out_path.write_text(json.dumps(result, ensure_ascii=False, indent=2), encoding="utf-8")
    print(f"[export] 清单 JSON -> {out_path}")
    print(f"[export] 口径: {caveat}")

    return 0


if __name__ == "__main__":
    raise SystemExit(main())
