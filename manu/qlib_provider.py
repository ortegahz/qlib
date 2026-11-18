#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""qlib_provider.py —— **qlib 数据集 provider 的单一事实源**（研究链层）。

【为什么要有这个模块（2026-11-09 实测）】
本工程有**两条链**都吃 qlib 数据集：

| | 研究链 `manu/etf_rotation/*.py` | 每日链 `manu/etf_rotation/daily/*` |
|:--|:--|:--|
| 谁挑数据源 | `qlib_scores.py --provider-uri`（默认 `C.PROVIDER_URI` = 上游基座） | `daily/run_qlib_score.py::select_provider()`（**谁新用谁**） |
| 派生目录可见性 | 手动传 `--provider-uri` 才看得见 | 自动（`cn_data_live`） |

⇒ 两条链**各写各的 provider 逻辑** ⇒ **口径会分叉**：研究链永远用基座（末日 2026-11-03），
每日链用派生（末日 2026-11-05）⇒ 12-01 重建时研究链的穿透打分会**静默停在 11-03 的旧值**。
⇒ 本模块把「provider 路径 + 选择规则」提到**研究链层**，两条链都从这儿取 ⇒ **单一事实源**。

【选择规则（**谁新用谁**，与每日链原有语义逐位一致）】
* 派生目录 `cn_data_live` 的日历末日 **> 基座** `cn_data` 的末日 ⇒ 用派生目录
  （派生 = 基座 + 阶段 1 自建增量日 ⇒ 它的末日天然 ≥ 基座）；
* 派生目录**不存在 / 日历读不出 / 末日不比基座新** ⇒ 用上游基座（**零回归**）；
* 派生目录**永远不覆盖**基座（基座是上游快照，禁写）⇒ 上游换库的前缀闸门不受影响。

【硬纪律】
* **纯读**：只读 `calendars/day.txt`，不写任何文件、不联网、不起 qlib 运行时；
* **不许编数字**：读不到日历 ⇒ 退回基座并在 reason 里说清楚，**不猜**末日；
* 所有路径都可被环境变量覆盖（服务器 cron 环境极简 ⇒ 必须能注入）。

【环境变量】
    ETF_ROTATION_QLIB_CN_DATA_LIVE   派生目录（默认 <dataset_root>/cn_data_live）
    ETF_ROTATION_QLIB_DATASET_ROOT   数据集根（默认 <落点根>/datasets/qlib）
    ETF_ROTATION_QLIB_PROVIDER_URI   **provider 覆盖**（研究链 `qlib_scores.py --provider-uri` 同名变量）

【用法】
    python -c "import sys; sys.path.insert(0,'manu/etf_rotation'); import qlib_provider as qp; print(qp.select_provider('2026-12-01'))"
"""
from __future__ import annotations

import importlib.util
import os
import sys
from pathlib import Path

#: 研究链根（`manu/etf_rotation`）
_RESEARCH_DIR = Path(__file__).resolve().parent

#: 环境变量名（**两条链共用同一组变量名** ⇒ 杜绝「第二个字面量」分叉）
ENV_CN_DATA_LIVE = "ETF_ROTATION_QLIB_CN_DATA_LIVE"
ENV_DATASET_ROOT = "ETF_ROTATION_QLIB_DATASET_ROOT"
ENV_PROVIDER_URI = "ETF_ROTATION_QLIB_PROVIDER_URI"

def _load_by_path(path: Path, name: str):
    """按**绝对路径**加载模块（研究链不是包；同名 `config.py` 有两个 ⇒ 必须按路径）。"""
    spec = importlib.util.spec_from_file_location(name, path)
    if spec is None or spec.loader is None:
        raise ImportError(f"无法加载模块：{path}")
    module = importlib.util.module_from_spec(spec)
    sys.modules[name] = module
    # 🔴 必须先登记进 sys.modules 再 exec_module（@dataclass + future-annotations ⇒ dataclasses 会反查 sys.modules）
    try:
        spec.loader.exec_module(module)
    except Exception:
        sys.modules.pop(name, None)
        raise
    return module


def _load_config():
    """研究链 `config.py`（⇒ `C.PROVIDER_URI` / `C.STOCK.PROVIDER_URI`）。"""
    return _load_by_path(_RESEARCH_DIR / "config.py", "manu_etf_rotation_config")


def _load_signal_policy():
    """研究链 `data/signal_policy.py`（交易日历读法的唯一事实源）。"""
    return _load_by_path(
        _RESEARCH_DIR / "data" / "signal_policy.py", "manu_etf_rotation_signal_policy"
    )


# ---------------------------------------------------------------- 路径（单一事实源）
def dataset_root() -> Path:
    """qlib 数据集根目录。

    🔴 缺省值**从研究链 `config.py` 的落点根派生**（`ETF_ROTATION_OUTPUT_ROOT`），
    而不是在这里再写第二份字面量 ⇒ 两条链永远指向同一棵产物树。
    """
    env = os.environ.get(ENV_DATASET_ROOT)
    if env:
        return Path(env).expanduser()
    return Path(_load_config().ETF_ROTATION_OUTPUT_ROOT) / "datasets" / "qlib"


def base_dir() -> Path:
    """上游基座目录（**只读**；默认 = 个股线 config 的 `PROVIDER_URI`）。"""
    return Path(str(_load_config().PROVIDER_URI)).expanduser()


def live_dir() -> Path:
    """派生目录（阶段 1 自建增量日；**默认永不覆盖基座**）。"""
    env = os.environ.get(ENV_CN_DATA_LIVE)
    if env:
        return Path(env).expanduser()
    return dataset_root() / "cn_data_live"


def provider_uri_override() -> Path | None:
    """环境变量显式指定的 provider 覆盖（研究链 `qlib_scores.py` 同名变量）。"""
    env = os.environ.get(ENV_PROVIDER_URI)
    return Path(env).expanduser() if env else None


# ---------------------------------------------------------------- 日历
def calendar_path(provider: Path | str) -> Path:
    """provider 的交易日历路径（`calendars/day.txt`）。"""
    return Path(provider) / "calendars" / "day.txt"


def calendar_max_date(provider: Path | str | None) -> str | None:
    """provider 的**数据集覆盖末日**（`YYYY-MM-DD`）；读不到 ⇒ `None`（**不猜**）。"""
    if provider is None:
        return None
    return _load_signal_policy().calendar_max_date(calendar_path(provider))


# ---------------------------------------------------------------- 选择规则
def select_provider(
    asof: str | None = None,
    *,
    base: Path | str | None = None,
    live: Path | str | None = None,
) -> tuple[Path, str]:
    """选本次打分用的 qlib provider 目录 ⇒ `(目录, 理由)`。

    规则（🔴 **谁新用谁**，与每日链原有语义逐位一致）：
    * 派生目录 `cn_data_live` 的日历末日 **> 基座** ⇒ 用派生目录；
    * 否则用上游基座（**零回归**）。

    参数：
    * `asof`：目标日（如 `2026-12-01`）⇒ 若派生目录末日 **未覆盖** 它，
      在 reason 里加**告警**（**不换数据源** —— 「没覆盖到目标日」由报告 notes 披露）；
    * `base` / `live`：显式指定目录（每日链用它把口径钉在自己的常量上 ⇒ 行为不变）。

    🔴 派生目录**永远不覆盖**基座 ⇒ 上游换库的前缀闸门不受影响。
    """
    sp = _load_signal_policy()
    base_path = Path(base) if base is not None else base_dir()
    live_path = Path(live) if live is not None else live_dir()
    base_max = sp.calendar_max_date(calendar_path(base_path))
    live_max = (
        sp.calendar_max_date(calendar_path(live_path)) if live_path.exists() else None
    )
    if live_max is None:
        return base_path, f"用上游基座（末日 {base_max}；派生目录不存在）"
    # 🔴 **谁新用谁**（派生目录 = 基座 + 自建增量 ⇒ 它的末日 ≥ 基座末日）
    #    ⇒ 即使它**没覆盖到目标日**，也比基座新 ⇒ S 层的最大打分日更大 ⇒ 仍该用派生目录
    #    （「没覆盖到目标日」这件事由报告 notes 披露，不是换数据源的理由）。
    if base_max is None or live_max > base_max:
        note = "（阶段 1 自建增量日）"
        if asof and live_max < asof:
            note += f"；⚠️ 派生目录末日 {live_max} **未覆盖**目标 {asof}"
        return live_path, f"用派生目录{note}：末日 {live_max} > 基座 {base_max}"
    return base_path, f"用上游基座（末日 {base_max}；派生目录末日 {live_max} 不比基座新）"


def describe(provider: Path | str) -> str:
    """一行摘要（provider 路径 + 数据集覆盖末日）—— 供脚本/日志直接打印。"""
    provider_path = Path(provider)
    max_date = calendar_max_date(provider_path)
    return f"{provider_path}（数据集覆盖末日 {max_date or '读不到'}）"


def _cli() -> int:
    """命令行入口：打印「本次该用哪个 provider + 数据集覆盖末日」（供 shell 脚本披露）。"""
    import argparse

    parser = argparse.ArgumentParser(description="打印本次该用的 qlib provider")
    parser.add_argument("--asof", default=None, help="目标日（如 2026-12-01）")
    parser.add_argument("--provider-uri", default=None, help="显式 provider 目录（覆盖环境变量）")
    args = parser.parse_args()
    asof = args.asof or None
    override = Path(args.provider_uri) if args.provider_uri else provider_uri_override()
    if override is not None:
        provider = override
        reason = "显式指定（--provider-uri / ETF_ROTATION_QLIB_PROVIDER_URI）"
    else:
        provider, reason = select_provider(asof)
    max_date = calendar_max_date(provider)
    print(f"{provider}（数据集覆盖末日 {max_date or '读不到'}）—— {reason}")
    if asof and max_date and max_date < asof:
        print(f"⚠️ provider 覆盖末日 {max_date} **未覆盖**目标 {asof} ⇒ 该目标日打不出分数")
    return 0


if __name__ == "__main__":
    raise SystemExit(_cli())
