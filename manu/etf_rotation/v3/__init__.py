"""🔴 **V3**：S 层「对齐式重构」**研发分支**（2026-11-26 起）。

**V3 是什么**：一条**研发分支**，用来验证「把 S 层打分子系统与月频下游对齐」能不能
换来端到端收益。🔴 **V3 不是现役口径**：**V2 仍是现役**（用户 2026-11-26 原话：
「注意 v2 还保留，我目前在用」）。

**硬隔离契约**（与 `v2/__init__.py` 同款，方向反过来 —— 这次 V3 是被隔离的那一方）

1. 本包**不 import、不改写** V1 的 ``manu/etf_rotation/{config,backtest,qlib_scores}.py``
   ＋ ``manu/etf_rotation/engine/*``（V1 已被 ``v2/probes/v1_frozen_md5.json`` 锁死）。
2. 本包**不 import、不改写** V2 的任何文件；只在「端到端验收」时**只读**调用 V2 的
   ETF 层（``etf_layer.build_targets`` / ``backtest_v2.run_backtest_v2`` /
   ``metrics.summarize``）⇒ V2 的文件与产物**原样保留**。
3. 本包**只写** ``<RESULTS_ROOT>/v3/`` ⇒ V1 根目录 / ``v2/`` / ``reports/`` **零改动**。
4. 本包**不改** ``manu/etf_rotation/daily/``（每日链）—— V3 接每日链须**用户当轮授权**。

**版本标签**：:data:`config_v3.V3_MODEL_VERSION_TAG`（``"V3"``）写进产物 manifest；
现役 V2 的标签是 ``"V2"`` ⇒ snapshot 必须能区分两者（铁律见
``manu/memory/rules.md §十四.140``）。

**单盲纪律**：``2024-01-01 ~ 2026-11-03`` 是**单盲封存段**（V2 已消耗）⇒ V3 的任何
对照实验**一律不许触碰**（`config_v3.BLIND_SEALED_WINDOW`）。
"""

from __future__ import annotations

__all__ = ["V3_VERSION", "V3_LABEL", "V3_MODEL_VERSION_TAG", "V2_MODEL_VERSION_TAG"]

V3_VERSION = "v3"
V3_LABEL = "ETF 穿透评分 V3 对齐版（研发分支）"
V3_MODEL_VERSION_TAG = "V3"
V2_MODEL_VERSION_TAG = "V2"
