"""🔴 V3 **滚动重训**（季度节奏；expanding window）。

**为什么要有（用户 2026-11-26 目标文档第 ④ 条）**
低信噪比的量价表格数据上，**一次性固定的权重会随市场风格切换而老化** ⇒ 按季度
（``config_v3.ROLLING_RETRAIN_EVERY_N_TRADING_DAYS = 60`` 个交易日）**滚动重训**。

**切分口径（🔴 硬约束）**
* **expanding window**：训练段随时间**变长**（``train_start`` 恒为原训练段起点）；
* **embargo 照旧从「验证起点」倒推**：``train_end = 验证起点之前第 (embargo + 1) 个交易日``
  ⇒ 训练末样本与验证首日之间始终留够隔离带（标签窗口末端 = t+25 交易日 ⇒ embargo ≥ 25）；
* 每个窗口的训练段**必须**满足 ``assert_fold_discipline``（🔴 用 ``v3/splits.py`` 的口径）；
* 🔴 **绝不触碰** ``config_v3.BLIND_SEALED_WINDOW``（2024-01-01 ~ 2026-11-03）。

**依赖说明（诚实披露）**
``v3/splits.py`` 由另一位 agent 同步编写 ⇒ 本模块**优先**调用它的公开名字
（``effective_embargo_days`` / ``trading_days_before`` / ``assert_fold_discipline``）；
若该模块尚不可用，则退回**本地等价实现**（读同一份交易日历 + 本地红线断言），
并在返回的 manifest 里**如实标注**用了哪条路径（不许悄悄降级）。
"""
from __future__ import annotations

import dataclasses
import importlib.util
import json
import sys
from pathlib import Path
from typing import Any

import pandas as pd

_V3_DIR = Path(__file__).resolve().parent
_ADDED_PATH = str(_V3_DIR) not in sys.path
if _ADDED_PATH:
    sys.path.insert(0, str(_V3_DIR))
try:
    import config_v3 as C
except ModuleNotFoundError:  # pragma: no cover —— 仅当以包方式被 import 时走这条
    from . import config_v3 as C
finally:
    if _ADDED_PATH:
        try:
            sys.path.remove(str(_V3_DIR))
        except ValueError:  # pragma: no cover
            pass

__all__ = ["rolling_retrain_and_score", "rolling_windows"]

#: 🔴 滚动重训的产物子目录（🔴 每个窗口一个子目录 ⇒ 不互相覆盖）
ROLLING_SUBDIR = "rolling"


# ----------------------------------------------------------------- 加载工具
def _load_by_path(path: Path, module_name: str):
    """按**绝对路径**加载模块（🔴 避免与 V2 的同名模块在 ``sys.path`` 上撞车）。"""
    if module_name in sys.modules:
        return sys.modules[module_name]
    spec = importlib.util.spec_from_file_location(module_name, path)
    if spec is None or spec.loader is None:
        raise ImportError(f"🔴 无法加载模块：{path}")
    module = importlib.util.module_from_spec(spec)
    sys.modules[module_name] = module
    added = str(_V3_DIR) not in sys.path
    if added:
        sys.path.insert(0, str(_V3_DIR))
    try:
        spec.loader.exec_module(module)
    except Exception:
        sys.modules.pop(module_name, None)
        raise
    finally:
        if added:
            try:
                sys.path.remove(str(_V3_DIR))
            except ValueError:  # pragma: no cover
                pass
    return module


def _load_sibling(module_name: str):
    """加载 V3 的兄弟模块（缺失 ⇒ 中文异常；🔴 不静默降级到「假装有」）。"""
    path = _V3_DIR / f"{module_name}.py"
    if not path.is_file():
        raise ImportError(f"🔴 V3 兄弟模块缺失：{path}")
    return _load_by_path(path, f"manu_v3_{module_name}")


def _load_train_s():
    """加载 V3 的 ``train_s``（按路径 ⇒ 不会拿到 V2 的同名模块）。"""
    return _load_by_path(_V3_DIR / "train_s.py", "manu_v3_train_s")


# ----------------------------------------------------------------- 日历
def _load_calendar(calendar_file: str | Path | None = None) -> list[str]:
    """读交易日历（升序去重的 ``"YYYY-MM-DD"`` 列表）。"""
    path = Path(calendar_file) if calendar_file is not None else Path(C.CALENDAR_FILE)
    if not path.is_file():
        raise FileNotFoundError(f"🔴 交易日历不存在：{path}")
    out: set[str] = set()
    for raw in path.read_text(encoding="utf-8").splitlines():
        line = raw.strip()
        if not line:
            continue
        try:
            out.add(pd.Timestamp(line).date().isoformat())
        except ValueError:
            continue
    if not out:
        raise ValueError(f"🔴 交易日历 {path} 里没有解析出任何合法日期")
    return sorted(out)


def _trading_days_before(day: str, n: int, calendar: list[str],
                         calendar_file: str | Path | None = None) -> str:
    """``day`` 之前第 ``n`` 个**交易日**（不含 ``day`` 本身）。

    🔴 **优先**用 ``v3/splits.py::trading_days_before``（单一事实源）；不可用时用本地日历实现。
    """
    if int(n) <= 0:
        raise ValueError(f"🔴 n 必须 ≥ 1（实得 {n}）")
    try:
        splits = _load_sibling("splits")
        fn = getattr(splits, "trading_days_before", None)
    except ImportError:
        fn = None
    if fn is not None:
        try:
            if calendar_file is None:
                value = fn(pd.Timestamp(day).date(), int(n))
            else:
                value = fn(pd.Timestamp(day).date(), int(n), calendar_file=calendar_file)
            return pd.Timestamp(value).date().isoformat()
        except (TypeError, FileNotFoundError):
            # 🔴 兄弟模块不可用 / 日历不在本机 ⇒ 退回本地日历实现（下方）
            pass
    prior = [d for d in calendar if d < str(day)]
    if len(prior) < int(n):
        raise ValueError(f"🔴 日历里 {day} 之前不足 {n} 个交易日")
    return prior[-int(n)]


def _effective_embargo_days() -> int:
    """实际 embargo（交易日）= max(规格下限, 标签窗口末端)。"""
    try:
        splits = _load_sibling("splits")
        return int(splits.effective_embargo_days())
    except (ImportError, AttributeError):
        return max(int(C.EMBARGO_DAYS), int(C.LABEL_HORIZON_DAYS))


# ----------------------------------------------------------------- 红线断言
def _assert_window_discipline(window: dict, calendar: list[str]) -> None:
    """🔴 本地红线断言（与 ``v3/splits.py::assert_fold_discipline`` 同口径的兜底版）。

    断言内容：顺序 / embargo 隔离带 / 标签窗口不跨界 / embargo 上界 / **绝不触碰单盲段**。
    """
    train_start = str(window["train_start"])
    train_end = str(window["train_end"])
    valid_start = str(window["valid_start"])
    valid_end = str(window["valid_end"])
    if not train_start < train_end:
        raise AssertionError(f"🔴 窗口 {window['window_id']}：训练段顺序异常")
    if not train_end < valid_start:
        raise AssertionError(
            f"🔴 窗口 {window['window_id']}：训练段末日 {train_end} 不早于验证段首日 {valid_start}"
        )
    if not valid_start <= valid_end:
        raise AssertionError(f"🔴 窗口 {window['window_id']}：验证段倒挂")
    if valid_end >= str(C.BLIND_SEALED_WINDOW[0]):
        raise AssertionError(
            f"🔴 窗口 {window['window_id']}：验证段末日 {valid_end} 触碰单盲封存段"
        )
    # 标签窗口不跨界：训练末样本的标签窗口结束日必须早于验证段首日
    label_end = _trading_days_before(train_end, int(C.LABEL_HORIZON_DAYS), calendar)
    if label_end >= valid_start:
        raise AssertionError(
            f"🔴 窗口 {window['window_id']} embargo 失效：训练末样本 {train_end} 的标签窗口"
            f"结束日 {label_end} 不早于验证段首日 {valid_start}"
        )
    # embargo 上界：训练末样本不得晚于「名义末日之前第 EMBARGO_DAYS 个交易日」
    raw = str(window.get("train_end_raw") or valid_start)
    upper = _trading_days_before(raw, int(C.EMBARGO_DAYS), calendar)
    if train_end > upper:
        raise AssertionError(
            f"🔴 窗口 {window['window_id']} embargo 不足：训练末样本 {train_end} 晚于"
            f"「{raw} 前第 {int(C.EMBARGO_DAYS)} 个交易日」（{upper}）"
        )


def _build_window_spec(splits_mod: Any, window: dict):
    """把窗口字典装成 ``v3/splits.py::FoldSpec``（🔴 按 dataclass 字段名映射，缺字段即报错）。"""
    fields = {f.name for f in dataclasses.fields(splits_mod.FoldSpec)}
    payload = {
        # 🔴 滚动窗口**不是** config_v3.WALK_FORWARD_FOLDS 里的折 ⇒ fold_id 必须为 None
        #    （折号非空时 assert_fold_discipline 会要求验证段与配置逐字一致 ⇒ 必然报红）
        "fold_id": None,
        "start_time": str(C.START_TIME),
        "train_start": str(window["train_start"]),
        "train_end_raw": str(window.get("train_end_raw") or window["valid_start"]),
        "train_end": str(window["train_end"]),
        "valid_start": str(window["valid_start"]),
        "valid_end": str(window["valid_end"]),
        "test_start": str(window["valid_start"]),
        "test_end": str(window["valid_end"]),
        "fit_start": str(window["train_start"]),
        "fit_end": str(window["train_end"]),
        "embargo_days": int(window["embargo_days"]),
    }
    required = ("train_start", "train_end", "valid_start", "valid_end", "fit_start", "fit_end")
    missing = [k for k in required if k not in fields]
    if missing:
        raise RuntimeError(
            f"🔴 v3/splits.py 的 FoldSpec 缺少字段 {missing} ⇒ 无法构造滚动窗口规格"
        )
    spec = splits_mod.FoldSpec(**{k: v for k, v in payload.items() if k in fields})
    # 🔴 折号被配置表校验 ⇒ 滚动窗口统一用 fold_id=None（窗口号记在 window 字典里）
    if getattr(spec, "fold_id", None) is not None:
        spec = dataclasses.replace(spec, fold_id=None)
    return spec


# ----------------------------------------------------------------- 窗口切分
def rolling_windows(spec, every_n_trading_days: int | None = None,
                    calendar_file: str | Path | None = None) -> list[dict]:
    """把 ``spec`` 的**验证段**按 ``every_n_trading_days`` 个交易日切成滚动重训窗口。

    返回
    ----
    ``[{'window_id': 1, 'train_start': ..., 'train_end': ..., 'train_end_raw': ...,
        'valid_start': ..., 'valid_end': ..., 'n_valid_days': ..., 'embargo_days': ...}, ...]``

    🔴 **expanding window**：``train_start`` 恒为原 spec 的训练段起点（训练段随时间变长）；
       ``train_end`` = 「本窗口验证起点之前第 (embargo + 1) 个交易日」（embargo 照旧倒推）。
    """
    n = int(C.ROLLING_RETRAIN_EVERY_N_TRADING_DAYS
            if every_n_trading_days is None else every_n_trading_days)
    if n <= 0:
        raise ValueError(
            f"🔴 every_n_trading_days 必须为正整数交易日（实得 {every_n_trading_days!r}）"
        )
    calendar = _load_calendar(calendar_file)
    valid_start = str(spec.valid_start)
    valid_end = str(spec.valid_end)
    days = [d for d in calendar if valid_start <= d <= valid_end]
    if not days:
        raise RuntimeError(
            f"🔴 验证段 {valid_start} ~ {valid_end} 内没有交易日 ⇒ 无法切滚动窗口"
        )
    embargo = _effective_embargo_days()
    train_start = str(spec.train_start)

    windows: list[dict] = []
    for i in range(0, len(days), n):
        chunk = days[i:i + n]
        window_valid_start = chunk[0]
        train_end = _trading_days_before(window_valid_start, embargo + 1, calendar,
                                         calendar_file=calendar_file)
        window = {
            "window_id": len(windows) + 1,
            "train_start": train_start,
            "train_end": train_end,
            "train_end_raw": window_valid_start,
            "valid_start": window_valid_start,
            "valid_end": chunk[-1],
            "n_valid_days": len(chunk),
            "embargo_days": embargo,
        }
        _assert_window_discipline(window, calendar)
        windows.append(window)
    return windows


# ----------------------------------------------------------------- 滚动重训
def rolling_retrain_and_score(
    spec,
    out_dir: Path,
    model_name: str,
    arm: str = "v3",
    every_n_trading_days: int | None = None,
    provider_uri: str | None = None,
    universe_info: dict | None = None,
    neutralize: bool | None = None,
    center: str | None = None,
    calendar_file: str | Path | None = None,
    score_dates: list[str] | None = None,
) -> dict:
    """对每个滚动窗口**重训 + 在该窗口内打分** ⇒ 合并成一个 OOS 分数 CSV。

    🔴 每个窗口的训练段都过 ``assert_fold_discipline``（用 ``v3/splits.py`` 的口径；
       该模块不可用时用本地等价断言，manifest 里如实标注）。
    """
    train_s = _load_train_s()
    out_dir = Path(out_dir)
    out_dir.mkdir(parents=True, exist_ok=True)
    windows = rolling_windows(spec, every_n_trading_days, calendar_file=calendar_file)

    splits_mod = None
    try:
        splits_mod = _load_sibling("splits")
    except ImportError:
        splits_mod = None

    frames = []
    window_manifests = []
    explicit = sorted({str(d)[:10] for d in (score_dates or [])})
    skipped_windows: list[dict] = []
    for window in windows:
        # 🔴 **显式打分日**：按窗口切分（每个窗口只打自己那几天的分）
        #    ⇒ 两臂（V2 对照 / V3 实验）在**完全相同的信号日**上比较。
        window_dates = ([d for d in explicit
                         if str(window["valid_start"]) <= d <= str(window["valid_end"])]
                        if explicit else None)
        if explicit and not window_dates:
            skipped_windows.append({"window": window, "reason": "该窗口内没有显式打分日"})
            continue
        if splits_mod is not None and hasattr(splits_mod, "assert_fold_discipline"):
            window_spec = _build_window_spec(splits_mod, window)
            splits_mod.assert_fold_discipline(window_spec)
        else:
            window_spec = _SimpleSpec(window)
        w_dir = out_dir / ROLLING_SUBDIR / f"window_{int(window['window_id']):03d}"
        manifest = train_s.train_and_score(
            window_spec,
            out_dir=w_dir,
            model_name=model_name,
            arm=arm,
            provider_uri=provider_uri,
            universe_info=universe_info,
            neutralize=neutralize,
            center=center,
            score_start=window["valid_start"],
            score_end=window["valid_end"],
            rolling=window,
            score_dates=window_dates,
        )
        window_manifests.append({
            "window": window,
            "manifest_path": str(w_dir / f"train_manifest_v3_{model_name}.json"),
            "objective": manifest.get("objective"),
            "n_samples": manifest.get("n_samples"),
            "n_features": manifest.get("n_features"),
        })
        csv_path = Path(manifest["outputs"]["stock_scores_csv"])
        frames.append(pd.read_csv(csv_path, parse_dates=["date"]))

    scores = pd.concat(frames, ignore_index=True) if frames else pd.DataFrame()
    # 🔴 **信号日逐日相等**（两臂可比性的地基）：显式打分日清单里的**每一个**日期
    #    都必须真的被打过分 —— 落在**所有**滚动窗口之外的日期会被静默丢弃
    #    ⇒ 两臂调仓日历就此不同 ⇒ 端到端指标不可比，而全程无报错。
    if explicit and not scores.empty:
        got = {str(pd.Timestamp(d).date()) for d in scores["date"].unique()}
        missing = [d for d in explicit if d not in got]
        if missing:
            raise RuntimeError(
                f"🔴 显式打分日有 {len(missing)} 个没有被打分：{missing[:8]}…"
                "（落在所有滚动窗口之外 ⇒ 两臂调仓日历不同 ⇒ 端到端不可比）⇒ 拒绝执行"
            )
    if not scores.empty:
        scores = scores.sort_values(["date", "instrument"]).reset_index(drop=True)
        merged_path = out_dir / C.scores_filename(model_name)
        scores.to_csv(merged_path, index=False, encoding="utf-8-sig")
    else:
        merged_path = out_dir / C.scores_filename(model_name)

    result = {
        "model_name": model_name,
        "arm": arm,
        "objective": str(C.MODEL_OBJECTIVE) if arm == "v3" else str(C.CONTROL_OBJECTIVE),
        "mode": "expanding_window_rolling_retrain",
        "every_n_trading_days": int(
            C.ROLLING_RETRAIN_EVERY_N_TRADING_DAYS
            if every_n_trading_days is None else every_n_trading_days
        ),
        "embargo_days": _effective_embargo_days(),
        "n_windows": len(windows),
        "n_windows_scored": len(window_manifests),
        "n_windows_skipped": len(skipped_windows),
        "skipped_windows": skipped_windows,
        "score_dates_source": ("explicit（🔴 与对照臂同一批信号日）" if explicit
                               else "signal_policy.score_grid_dates（栅格相位）"),
        "windows": windows,
        "window_manifests": window_manifests,
        "fold_discipline_source": (
            "v3/splits.py::assert_fold_discipline" if splits_mod is not None
            else "🔴 本地等价断言（v3/splits.py 不可用）"
        ),
        "oos_window": list(C.WALK_FORWARD_OOS_WINDOW),
        "blind_sealed_window": list(C.BLIND_SEALED_WINDOW),
        "n_rows": len(scores),
        "n_score_dates": int(scores["date"].nunique()) if not scores.empty else 0,
        "outputs": {"stock_scores_csv": str(merged_path)},
        "scores_md5": train_s.md5_of(merged_path) if merged_path.is_file() else None,
        "caliber_card": C.v3_caliber_card(),
    }
    manifest_path = out_dir / f"rolling_manifest_v3_{model_name}.json"
    manifest_path.write_text(json.dumps(result, ensure_ascii=False, indent=2, default=str),
                             encoding="utf-8")
    print(f"[OK] 滚动重训 manifest 已写出：{manifest_path}")
    return result


class _SimpleSpec:
    """``v3/splits.py`` 不可用时的**最小切分规格**（字段与 ``FoldSpec`` 同名同义）。"""

    def __init__(self, window: dict):
        self.fold_id = None  # 🔴 同上：滚动窗口不是 config 里的「折」
        self.start_time = str(C.START_TIME)
        self.train_start = str(window["train_start"])
        self.train_end_raw = str(window.get("train_end_raw") or window["valid_start"])
        self.train_end = str(window["train_end"])
        self.valid_start = str(window["valid_start"])
        self.valid_end = str(window["valid_end"])
        self.test_start = self.valid_start
        self.test_end = self.valid_end
        self.fit_start = self.train_start
        self.fit_end = self.train_end
        self.embargo_days = int(window["embargo_days"])

    def as_dict(self) -> dict:
        """转成可 JSON 序列化的字典。"""
        return {
            "fold_id": self.fold_id,
            "start_time": self.start_time,
            "train_start": self.train_start,
            "train_end_raw": self.train_end_raw,
            "train_end": self.train_end,
            "valid_start": self.valid_start,
            "valid_end": self.valid_end,
            "test_start": self.test_start,
            "test_end": self.test_end,
            "fit_start": self.fit_start,
            "fit_end": self.fit_end,
            "embargo_days": self.embargo_days,
        }
