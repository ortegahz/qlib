"""🔴 V2 超参搜索 —— **物理红线：搜索只许在 val 段（2022–2023）做**。

时间线红线（用户权威规格 ``V2_SPEC_pasted_text.txt`` 第二层 + ``config_v2``）：

  * 训练 ``2016 ~ 2021``（末段按 embargo 裁剪，见 ``splits.py``）；
  * **验证 ``2022-01-01 ~ 2023-12-31``：所有超参搜索只许在这段做**；
  * 测试 ``2024-01-01 ~ 2026-11-03``：**单盲只跑一次**（见 ``evaluate.py``）。

🔴 **本模块的物理保证（不靠文档，靠代码）**：
  1. ``assert_val_only(start, end)``：窗口只要**不落在** val 段内 ⇒ **直接 raise ValueError**
     （含越界说明）；任何**触碰 2024-01-01 之后**的窗口一律拒收；
  2. ``search()``：**在调用 objective 之前**先 ``assert_val_only``，且 objective
     **只会**收到通过校验的 val 窗口 ⇒ objective 拿不到 test 段窗口；
  3. ``freeze_params()``：冻结时**再次**校验窗口 ⇒ 不可能把 test 段搜出来的参数冻结下来。

⚠️ 边界常量的**唯一事实源**就是本模块的 ``VAL_WINDOW`` / ``TEST_WINDOW``
（它们与 ``config_v2.VALID`` / ``config_v2.TEST`` 字面一致）⇒ 改边界 = 改红线，
测试里有**变异反证**盯着这件事。
"""

from __future__ import annotations

import datetime as _dt
import itertools
import json
import time
from pathlib import Path
from typing import Any, Callable, Mapping, Sequence

__all__ = [
    "VAL_WINDOW",
    "TEST_WINDOW",
    "assert_val_only",
    "search",
    "freeze_params",
]

#: 🔴 超参搜索**唯一合法**窗口（与 ``config_v2.VALID`` 字面一致）
VAL_WINDOW: tuple[str, str] = ("2022-01-01", "2023-12-31")
#: 🔴 测试段（**单盲**）：任何触碰它的搜索窗口都必须被拒
TEST_WINDOW: tuple[str, str] = ("2024-01-01", "2026-11-03")


# ----------------------------------------------------------------- 工具
def _to_date(value: Any, what: str) -> _dt.date:
    """把 ``"YYYY-MM-DD"`` / ``date`` / ``Timestamp`` 统一成 ``datetime.date``。

    解析失败 ⇒ raise ValueError（**不猜**日期，避免误判红线）。
    """
    if isinstance(value, _dt.datetime):
        return value.date()
    if isinstance(value, _dt.date):
        return value
    if isinstance(value, str):
        text = value.strip()
        try:
            return _dt.date.fromisoformat(text[:10])
        except ValueError as exc:  # noqa: PERF203
            raise ValueError(f"🔴 {what} 格式非法：{value!r}（应为 YYYY-MM-DD）") from exc
    raise ValueError(f"🔴 {what} 类型非法：{value!r}（应为 YYYY-MM-DD 字符串或 date）")


def _now_iso() -> str:
    """当前时间（ISO8601，秒级）——冻结文件里的「搜索时间」。"""
    return time.strftime("%Y-%m-%dT%H:%M:%S", time.localtime())


# ----------------------------------------------------------------- 🔴 红线闸门
def assert_val_only(start: str, end: str) -> None:
    """🔴 **硬红线**：超参搜索窗口必须**完全落在** val 段内，否则直接抛错。

    参数
    ----
    start / end : 窗口起止（含端点），``"YYYY-MM-DD"``。

    拒收条件（任一命中即 raise ``ValueError``）：
      1. ``start > end``（窗口倒挂）；
      2. 窗口**触碰测试段**（``end >= 2024-01-01``）——单盲段只许跑一次，绝不许回头选参；
      3. 窗口越出 val 段（``start < 2022-01-01`` 或 ``end > 2023-12-31``），
         包括**误用训练段**（2016–2021）来选参。

    错误信息必须**说清越界在哪**（红线的可审计性）。
    """
    s = _to_date(start, "start")
    e = _to_date(end, "end")
    val_start = _to_date(VAL_WINDOW[0], "VAL_WINDOW[0]")
    val_end = _to_date(VAL_WINDOW[1], "VAL_WINDOW[1]")
    test_start = _to_date(TEST_WINDOW[0], "TEST_WINDOW[0]")

    if s > e:
        raise ValueError(f"🔴 搜索窗口非法：start={start} 晚于 end={end}（窗口倒挂）")
    if e >= test_start:
        raise ValueError(
            f"🔴 搜索窗口 {start} ~ {end} **触碰测试段**"
            f"（{TEST_WINDOW[0]} ~ {TEST_WINDOW[1]}）⇒ 超参搜索只许在 val 段"
            f"（{VAL_WINDOW[0]} ~ {VAL_WINDOW[1]}）做；测试段单盲只跑一次，不许回头选参"
        )
    if s < val_start or e > val_end:
        raise ValueError(
            f"🔴 搜索窗口 {start} ~ {end} 越界：必须完全落在 val 段"
            f"（{VAL_WINDOW[0]} ~ {VAL_WINDOW[1]}）内"
            f"（起点早于 {VAL_WINDOW[0]} = 用了训练段；终点晚于 {VAL_WINDOW[1]} = 用了测试段）"
        )


# ----------------------------------------------------------------- 网格搜索
def _grid_combinations(grid: Mapping[str, Sequence[Any]]) -> list[dict]:
    """把网格展开成**确定性顺序**的参数字典列表（键名排序 ⇒ 可复现）。"""
    if not grid:
        raise ValueError("🔴 搜索网格为空：没有任何超参可搜")
    keys = sorted(grid.keys())
    for key in keys:
        values = list(grid[key] or [])
        if not values:
            raise ValueError(f"🔴 搜索网格 {key} 的取值列表为空")
    combos = []
    for values in itertools.product(*(list(grid[k]) for k in keys)):
        combos.append(dict(zip(keys, values)))
    return combos


def search(
    objective: Callable[[dict, tuple[str, str]], Mapping[str, Any]],
    grid: Mapping[str, Sequence[Any]],
    val_window: tuple[str, str] = VAL_WINDOW,
    max_trials: int | None = None,
) -> dict:
    """在 **val 段内**做网格搜索（🔴 越界直接抛错，不会调 objective）。

    参数
    ----
    objective : ``objective(params, val_window) -> dict``
        必须返回 **dict** 且至少含 ``{"score": float}``（**越大越好**）。
        ``val_window`` 恒为通过 ``assert_val_only`` 校验的窗口 ⇒ objective 拿不到 test 段。
    grid : ``{超参名: [取值, ...]}``。
    val_window : 搜索窗口（默认 val 段）；**不在 val 段 ⇒ 直接 raise**。
    max_trials : 最多试几次；超了 ⇒ **截断**（按确定性顺序取前 N 个）并记录。

    返回
    ----
    ``{"best_params", "best_score", "trials", "val_window", "n_trials_total",
       "n_trials_run", "truncated"}``；
    ``trials`` 每条 = ``{"params", "score", "objective", "status"}``
    （失败条目 ``status="error"`` + ``"error"``，**不编分数**）。

    🔴 语义：
      * **先校验、再搜索**：``assert_val_only`` 在**任何** objective 调用之前执行；
      * 全部 trial 都失败 ⇒ raise RuntimeError（不返回空 best）。
    """
    window = (str(val_window[0]), str(val_window[1]))
    # 🔴 第一步就是红线闸门（在调 objective 之前）
    assert_val_only(window[0], window[1])

    combos = _grid_combinations(grid)
    total = len(combos)
    truncated = False
    if max_trials is not None and total > int(max_trials):
        combos = combos[: int(max_trials)]
        truncated = True

    trials: list[dict] = []
    best_params: dict | None = None
    best_score: float | None = None
    for params in combos:
        # 再校验一次（防御纵深：万一调用方在循环里改了窗口常量）
        assert_val_only(window[0], window[1])
        try:
            out = objective(params, window)
        except Exception as exc:  # noqa: BLE001 —— 失败必须**如实记录**，不许静默吞掉
            trials.append({"params": params, "score": None, "objective": None,
                           "status": "error", "error": repr(exc)})
            continue
        if not isinstance(out, dict) or "score" not in out:
            trials.append({"params": params, "score": None, "objective": None,
                           "status": "error",
                           "error": "objective 必须返回 dict 且含 'score' 键"})
            continue
        try:
            score = float(out["score"])
        except (TypeError, ValueError):
            trials.append({"params": params, "score": None, "objective": None,
                           "status": "error", "error": f"score 不可转 float：{out['score']!r}"})
            continue
        if score != score or score in (float("inf"), float("-inf")):
            trials.append({"params": params, "score": None, "objective": dict(out),
                           "status": "error", "error": f"score 非有限：{score!r}"})
            continue
        trials.append({"params": params, "score": score, "objective": dict(out),
                       "status": "ok"})
        if best_score is None or score > best_score:
            best_score = score
            best_params = dict(params)

    if best_params is None or best_score is None:
        raise RuntimeError(
            f"🔴 网格搜索全部失败（共 {len(trials)} 个 trial）⇒ 无 best_params 可冻结"
        )
    return {
        "best_params": best_params,
        "best_score": float(best_score),
        "trials": trials,
        "val_window": [window[0], window[1]],
        "n_trials_total": int(total),
        "n_trials_run": int(len(combos)),
        "truncated": bool(truncated),
    }


# ----------------------------------------------------------------- 冻结
def freeze_params(params: dict, out_path: Any) -> Path:
    """把选定的参数**冻结**成 json（供单盲阶段**只读**引用）。

    入参两种写法都支持：
      * ``params`` = 参数字典（``best_score`` 可放在 ``params["best_score"]`` 或 ``params["score"]``）；
      * ``params`` = ``search()`` 的**整个返回**（含 ``best_params`` / ``best_score``）。

    文件内容（审计用）：``params`` / ``best_score`` / ``val_window`` / ``frozen_at`` / ``note``。

    🔴 冻结时**再次** ``assert_val_only`` ⇒ 不可能把 test 段搜出来的参数冻进单盲配置。
    写完后把文件权限设为 **只读（0444）** ⇒ 单盲阶段物理上不许改。
    """
    src = dict(params or {})
    if isinstance(src.get("best_params"), Mapping):
        best = dict(src.get("best_params") or {})
        score = src.get("best_score")
        window = list(src.get("val_window") or VAL_WINDOW)
    else:
        best = {k: v for k, v in src.items() if k not in ("best_score", "score")}
        score = src.get("best_score", src.get("score"))
        window = list(VAL_WINDOW)
    window = [str(window[0]), str(window[1])]
    # 🔴 冻结前的最后一道闸门
    assert_val_only(window[0], window[1])

    path = Path(out_path)
    path.parent.mkdir(parents=True, exist_ok=True)
    record = {
        "version": "v2",
        "params": best,
        "best_score": None if score is None else float(score),
        "val_window": window,
        "frozen_at": _now_iso(),
        "note": (
            "本文件由 v2/hyperopt.py::freeze_params 生成：参数**只**在 val 段"
            f"（{VAL_WINDOW[0]} ~ {VAL_WINDOW[1]}）搜索得到；"
            "测试段（单盲）只许读取本文件，不许再搜索。"
        ),
    }
    if path.exists():
        path.unlink()          # 只读文件重写：先删后写
    path.write_text(json.dumps(record, ensure_ascii=False, indent=2, sort_keys=True),
                    encoding="utf-8")
    path.chmod(0o444)          # 只读 ⇒ 单盲阶段物理上不许改
    return path
