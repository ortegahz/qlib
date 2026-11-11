#!/usr/bin/env python
"""节点日**股数/金额重算**工具（M-S3，2026-11-05 07:1x）。

【定位：只重算「买多少股」，不重算「买什么」】
qlib 个股层（S）⇒ E=W×S ⇒ 规则层 + LLM 层 ⇒ **信号（名单 + 目标权重）** 已经定在
`final_rebalance_plan.json` 里了。本脚本**只做一件事**：
**把「目标股数 / 金额 / 现金流」按节点日（如 2026-12-01）的收盘价重新折算一遍**。

🔴 **为什么需要它（M-S2 实测引出的直接后果）**
下一调仓节点是 **2026-12-01**，但 **S 层在 12-01 根本不存在**
（qlib 数据集只到 2026-11-03，且 12-01 **不在打分相位栅格**上 ⇒ 见专题档 §45），
⇒ **12-01 当天拿不到新信号**，但**可以**用 12-01 的**价格**把既有信号的股数重算一遍。
交付件本来就写着「股数是按 2026-11-03 收盘价折算的**样例值**，须在 2026-12-01 收盘后
按当日收盘价重算整手股数」⇒ **本脚本就是那句话的落地件**。

【口径纪律（不许含糊）】
* **只重算价格口径**：目标权重、动作方向、理由、final_score **一律取自计划件**，本脚本不改；
* 🔴 **节点日收盘价 ≠ 执行日成交价**：执行日 = 节点日 + 1 交易日（`config.EXECUTION_LAG_DAYS=1`）
  ⇒ 用节点日价算出的股数是「**按节点日价折算**」，实际成交在执行日 ⇒ 产物必须写明；
* 🔴 **账户口径可能已过期**：计划件的持仓 / 现金来自 **<计划 asof> 的账户快照**
  ⇒ 产物必须写明快照日；可用 `--account` / `--cash` / `--total-assets` 覆盖；
* **绝不覆盖既有产物**：输出写 `node_reprice_<节点日>.json` / `.md`（新文件名）
  ⇒ 不触发 sha1 链、不触发 `probe_execution_date` 的 P11「样例 + 重算」断言；
* **绝不写回 `data/account_snapshot.csv`**（会破坏 `account.py` 的 Σ市值+现金==总资产 对账）。

【用法】
    /home/huangzhe/anaconda3/envs/ql/bin/python manu/etf_rotation/reprice_node_day.py \\
        --node-date 2026-12-01 \\
        [--plan <落点根>/runs/final_rebalance_plan.json] \\
        [--prices <落点根>/datasets/etf/etf_prices.csv] \\
        [--account manu/etf_rotation/data/account_snapshot.csv] \\
        [--cash 15518.87] [--total-assets 29854.67] \\
        [--out-dir <落点根>/runs]
"""
from __future__ import annotations

import argparse
import json
import math
import sys
from datetime import datetime
from pathlib import Path

import pandas as pd

#: 漂移方向的浮点容差（百分数）：|drift_pct| < 该值 ⇒ 视为**无漂移**（方向中性）
_DRIFT_EPS_PCT = 1e-9

ROOT = Path(__file__).resolve().parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

import account as A  # noqa: E402
import config as C  # noqa: E402
from final_rebalance_plan import (  # noqa: E402
    LOT,
    _f,
    _round_lot,
    load_account,
    load_prices,
)


def _parse_date(value: object) -> str | None:
    """把日期统一成 ``"YYYY-MM-DD"``；解析不出来返回 ``None``（**不编日期**）。"""
    if value is None:
        return None
    text = str(value).strip()[:10]
    for fmt in ("%Y-%m-%d", "%Y%m%d", "%Y/%m/%d"):
        try:
            return datetime.strptime(text, fmt).strftime("%Y-%m-%d")
        except ValueError:
            continue
    return None


def _now_iso() -> str:
    """本地时区的当前时间（秒级 ISO8601）。"""
    return datetime.now().astimezone().isoformat(timespec="seconds")


def _load_plan(path: Path) -> dict:
    """读计划件；缺必需段 ⇒ 抛中文异常（**不静默降级**）。"""
    if not path.exists():
        raise FileNotFoundError(f"计划件缺失：`{path}` 不存在 ⇒ 无法重算股数")
    plan = json.loads(path.read_text(encoding="utf-8"))
    for key in ("meta", "actions"):
        if key not in plan:
            raise ValueError(f"计划件 `{path}` 缺少必需段 `{key}` ⇒ 无法重算股数")
    if not plan["actions"]:
        raise ValueError(f"计划件 `{path}` 的 actions 为空 ⇒ 无法重算股数")
    return plan


def _fee_rate(plan: dict) -> float:
    """单边费率 = 佣金 + 滑点（ETF 免印花税 / 过户费）。

    口径与 `final_rebalance_plan.py` 一致：优先取计划件 `meta.params`，
    缺则退回 `config.ETF_COMMISSION` / `config.ETF_SLIPPAGE`。
    """
    params = (plan.get("meta") or {}).get("params") or {}
    commission_bp = _f(params.get("commission_bp"))
    slippage_bp = _f(params.get("slippage_bp"))
    if commission_bp is None:
        commission_bp = float(C.ETF_COMMISSION) * 10000.0
    if slippage_bp is None:
        slippage_bp = float(C.ETF_SLIPPAGE) * 10000.0
    return (commission_bp + slippage_bp) / 10000.0


def _resolve_price(
    act: dict,
    code: str,
    prices: dict[str, tuple[float, str]],
    node_date: str,
) -> tuple[float, str, str]:
    """解析某只标的在节点日用的价格，返回 ``(价格, 价格日期, 提示)``。

    口径（M-S3 原语义**逐字保留**，本函数只是把原 `_recompute` 内联的分支抽出来，
    供「节点日账户口径」复用，避免两处逻辑漂移）：
    * 行情里有该代码 ⇒ 取「≤ 节点日的最后一根」收盘价；
      若价格日期 ≠ 节点日（停牌/未上市）⇒ 提示「沿用最近一根」；
    * 行情里完全没有 ⇒ 沿用**计划件**价格并标注；
    * 两者都拿不到 ⇒ **抛异常**（不编数字）。
    """
    price: float | None = None
    price_date: str | None = None
    price_note = ""
    if code in prices:
        price, price_date = prices[code]
        if price_date != node_date:
            price_note = (
                f"节点日 {node_date} 当天该 ETF **无 bar**（停牌/未上市）"
                f" ⇒ 沿用最近一根 {price_date} 收盘价"
            )
    else:
        price_note = f"节点日 {node_date} 该 ETF **完全没有行情** ⇒ 沿用计划件价格并标注"
        price = _f(act.get("price"))
        price_date = act.get("price_date")
    if not price:
        raise ValueError(
            f"标的 {code} 在节点日 {node_date} 既无行情也无计划件价格 ⇒ 无法折算股数（不编数字）"
        )
    return price, price_date, price_note


def _parse_scenario(spec: str) -> dict[str, str]:
    """解析 `--scenario` 规格串 ⇒ ``{6 位代码: "A"|"B"}``。

    口径：只接受 `代码=A|B` 形态（逗号分隔）；代码统一 zfill(6)；
    字母只允许 **A = 规则层** / **B = LLM 最终裁决** ⇒ 其它一律抛中文异常
    （**不默认、不猜**，宁可拒绝重算）。

    🔴 本函数是**纯解析**，不碰行情/账户 ⇒ 非法输入在读盘前就被拦下。
    """
    out: dict[str, str] = {}
    for chunk in str(spec).split(","):
        item = chunk.strip()
        if not item:
            continue
        if "=" not in item:
            raise ValueError(
                f"情景规格片段 `{item}` 缺 `=`（应形如 `511880=B`）⇒ 拒绝重算"
            )
        raw_code, _, letter = item.partition("=")
        code = raw_code.strip().zfill(6)
        letter = letter.strip().upper()
        if not code.isdigit() or len(code) != 6:
            raise ValueError(
                f"情景规格里的代码 `{raw_code.strip()}` 不是 6 位数字 ⇒ 拒绝重算"
            )
        if letter not in ("A", "B"):
            raise ValueError(
                f"情景规格里 `{code}` 的选项 `{letter}` 非法"
                "（只允许 A=规则层 / B=LLM 最终裁决）⇒ 拒绝重算（不默认、不猜）"
            )
        if code in out and out[code] != letter:
            raise ValueError(
                f"情景规格里 `{code}` 出现两次且选项不同（{out[code]} vs {letter}）⇒ 拒绝重算"
            )
        out[code] = letter
    if not out:
        raise ValueError("情景规格为空 ⇒ 拒绝重算")
    return out


def _scenario_overrides(
    plan: dict,
    choices: dict[str, str],
) -> tuple[dict[str, dict], list[dict]]:
    """把「每项选 A 还是 B」翻译成**节点日重算用的目标覆盖**。

    口径（🔴 只认 LLM 裁决里的**显式语义**，绝不猜）：
    * **选项 A（规则层）** ⇒ **不覆盖**，沿用计划件自己的 `target_weight`；
    * **选项 B（LLM 最终裁决）** ⇒ 按 `llm_final_decision.targets[code]` 的字段分三类：
      - `lots` 非空 ⇒ **kind="shares"**：原文「买 N 手」⇒ 目标股数固定 = ``lots × LOT``（**与价格无关**）；
      - `target_pct == 0` ⇒ **kind="none"**：原文写明不建仓 / 不买；
      - 其余 ⇒ **kind="weight"**：原文「权重降至 X%」⇒ 目标权重 = ``target_pct``。

    返回 ``(覆盖字典, 溯源行列表)``；覆盖字典形如
    ``{"512800": {"kind": "weight", "weight_pct": 12.0, ...}}``。
    """
    llm_targets = ((plan.get("llm_final_decision") or {}).get("targets") or {})
    plan_codes = {str(a.get("code") or "").zfill(6) for a in plan.get("actions") or []}
    #: 规则层各席位的计划权重（🔴 **不许写死「等权 20%」** —— 幸存只数变 ⇒ 等权值会变）
    plan_weight_map = {
        str(a.get("code") or "").zfill(6): float(a.get("target_weight") or 0.0)
        for a in plan.get("actions") or []
    }
    overrides: dict[str, dict] = {}
    rows: list[dict] = []
    for code, letter in sorted(choices.items()):
        if code not in plan_codes:
            raise ValueError(
                f"情景规格里的 `{code}` 不在计划件的 actions 里 ⇒ 拒绝重算（不编数字）"
            )
        if letter == "A":
            rows.append({
                "code": code, "choice": "A", "kind": "rule",
                "kind_label": "规则层（不覆盖，沿用计划件 target_weight）",
                "basis": (f"计划件 actions[].target_weight"
                          f"（本轮规则层等权 {plan_weight_map.get(code, 0.0) * 100:.1f}%）"),
                "source": "final_rebalance_plan.json → actions[].target_weight",
            })
            continue
        target = llm_targets.get(code)
        if not target:
            raise ValueError(
                f"情景规格要求 `{code}` 采纳 LLM 裁决，但计划件的 "
                f"llm_final_decision.targets 里没有 `{code}` ⇒ 拒绝重算（不猜）"
            )
        lots = target.get("lots")
        target_pct = _f(target.get("target_pct"))
        if lots not in (None, ""):
            shares = int(lots) * LOT
            if shares % LOT != 0 or shares <= 0:
                raise ValueError(
                    f"LLM 裁决里 `{code}` 的 lots={lots} 折算成 {shares} 股不是整手 ⇒ 拒绝重算"
                )
            overrides[code] = {
                "kind": "shares",
                "shares": shares,
                "weight_pct": target_pct,
                "basis": f"LLM 最终裁决原文「买 {int(lots)} 手」× 整手 {LOT} ⇒ 固定 {shares} 股（与价格无关）",
                "source": "final_rebalance_plan.json → llm_final_decision.targets[].lots",
            }
            kind_label = f"LLM 买 {int(lots)} 手（固定 {shares} 股）"
        elif target_pct is not None and target_pct == 0:
            overrides[code] = {
                "kind": "none",
                "shares": 0,
                "weight_pct": 0.0,
                "basis": f"LLM 最终裁决：{target.get('note') or target.get('direction') or '不建仓'}",
                "source": "final_rebalance_plan.json → llm_final_decision.targets[].target_pct == 0",
            }
            kind_label = "LLM 不建仓（权重 0%）"
        else:
            if target_pct is None:
                raise ValueError(
                    f"LLM 裁决里 `{code}` 既没有 lots 也没有 target_pct ⇒ 拒绝重算（不猜）"
                )
            overrides[code] = {
                "kind": "weight",
                "shares": None,
                "weight_pct": float(target_pct),
                "basis": (
                    f"LLM 最终裁决目标权重 {float(target_pct):.4f}%"
                    f"（原文口径：{target.get('target_pct_source') or '未注明'}）"
                ),
                "source": "final_rebalance_plan.json → llm_final_decision.targets[].target_pct",
            }
            kind_label = f"LLM 目标权重 {float(target_pct):.4f}%"
        rows.append({
            "code": code, "choice": "B", "kind": overrides[code]["kind"],
            "kind_label": kind_label,
            "basis": overrides[code]["basis"],
            "source": overrides[code]["source"],
            "cap_pct": target.get("cap_pct"),
            "stop_loss": target.get("stop_loss"),
        })
    return overrides, rows


def _recompute(
    plan: dict,
    prices: dict[str, tuple[float, str]],
    node_date: str,
    total_assets: float,
    cash: float,
    current_shares: dict[str, int],
    fee_rate: float,
    overrides: dict[str, dict] | None = None,
) -> dict:
    """按节点日价重算每个 action 的股数 / 金额 / 费用，并算现金流。

    🔴 `overrides` 是 **M-S7 新增的可选参数**（默认 ``None`` ⇒ 行为与改造前**逐字节一致**）：
    形如 ``{"512800": {"kind": "weight", "weight_pct": 12.0}}`` 的**目标覆盖**，
    用于「用户拍板某项采纳 LLM 最终裁决」的节点日重算。
    覆盖语义（由 `_scenario_overrides` 从 `llm_final_decision.targets` 推导，不猜）：
    * ``kind="weight"`` ⇒ 目标权重换成 ``weight_pct%``，股数按节点日价重算；
    * ``kind="shares"`` ⇒ 目标股数**固定**（买 N 手 ⇒ 与价格无关），金额 = 股数 × 节点日价；
    * ``kind="none"``  ⇒ 不建仓（该席位权重归现金）。
    """
    actions: list[dict] = []
    for act in plan["actions"]:
        code = str(act.get("code") or "").zfill(6)
        plan_target_weight = _f(act.get("target_weight"), 0.0) or 0.0
        target_weight = plan_target_weight
        cur = int(current_shares.get(code, int(act.get("current_shares") or 0)))
        price, price_date, price_note = _resolve_price(act, code, prices, node_date)
        # 🔴 2026-11-11（审计缺陷 1 · **阻断**）：**被 TA 一票否决但仍在持仓**的行
        #    ⇒ 语义 =「保持现仓」⇒ **既不买也不卖**。
        #    ⚠️ 旧实现按 `target_weight × total_assets` 在**节点日价**上重算目标股数
        #      ⇒ 节点日价 ≠ 快照价 ⇒ delta ≠ 0 ⇒ 生成**卖出挂单**
        #      ⇒ 违反「TA 一票否决 ⇒ 不卖」这条红线（`--live-trade` 下会被当真）。
        #    ⇒ 修法：与 `final_rebalance_plan.py` 的 `_is_ta_hold` **同口径** ——
        #      目标股数 = **当前股数**、delta = 0、动作 =「维持」。
        _is_ta_hold = bool(act.get("ta_rejected")) and bool(act.get("held_not_touched"))
        if _is_ta_hold:
            target_amount = cur * price if price else 0.0
            target_shares = cur
            delta_shares = 0
            est_amount = 0.0
            est_fee = 0.0
        else:
            target_amount = target_weight * total_assets if total_assets else 0.0
            target_shares = _round_lot(target_amount / price) if target_amount > 0 else 0
            delta_shares = target_shares - cur
            est_amount = delta_shares * price
            est_fee = abs(est_amount) * fee_rate
        lot_note = ""
        if target_amount > 0 and price * LOT > target_amount:
            lot_note = (
                f"目标金额 {target_amount:.0f} 元 < 一手({LOT} 份 × {price:.3f} = "
                f"{price * LOT:.0f} 元) ⇒ 整手口径下本轮无法按目标权重建仓"
            )
        # ---- M-S7 情景覆盖：用户拍板「某项采纳 LLM 裁决」时改写目标 ----
        ov = (overrides or {}).get(code)
        scenario_fields: dict = {}
        action_label = act.get("action") or ""
        #: 🔴 情景模式 = 传了 `--scenario`（`overrides is not None`；全 A 情景 ⇒ 空字典但**不是 None**）
        #:    ⇒ 默认（纯规则层）路径**完全不受影响**（产物逐字节不变）。
        scenario_mode = overrides is not None
        if ov:
            kind = ov["kind"]
            if kind == "weight":
                target_weight = float(ov["weight_pct"]) / 100.0
                target_amount = target_weight * total_assets if total_assets else 0.0
                target_shares = _round_lot(target_amount / price) if target_amount > 0 else 0
            elif kind == "shares":
                target_shares = int(ov["shares"])
                target_amount = target_shares * price
                target_weight = (target_amount / total_assets) if total_assets else 0.0
            elif kind == "none":
                target_weight = 0.0
                target_amount = 0.0
                target_shares = 0
            else:
                raise ValueError(
                    f"情景覆盖类型非法：{kind!r}（{code}）⇒ 拒绝重算（不猜）"
                )
            lot_note = ""
            if target_amount > 0 and price * LOT > target_amount:
                lot_note = (
                    f"目标金额 {target_amount:.0f} 元 < 一手({LOT} 份 × {price:.3f} = "
                    f"{price * LOT:.0f} 元) ⇒ 整手口径下本轮无法按目标权重建仓"
                )
            if _is_ta_hold:
                # 🔴 情景覆盖**不得**推翻「TA 一票否决 ⇒ 保持现仓」⇒ 下面统一复位。
                target_amount = cur * price if price else 0.0
                target_shares = cur
                target_weight = plan_target_weight
            delta_shares = target_shares - cur
            est_amount = delta_shares * price
            est_fee = abs(est_amount) * fee_rate
            if kind == "none" and not _is_ta_hold:
                action_label = "不建仓"
            elif target_shares > cur:
                action_label = "买入"
            elif target_shares < cur:
                action_label = "卖出"
            else:
                action_label = "持有"
            # 🔴 2026-11-09 修（生产基线重建轮）：**「单席上限」是账户级 20%，不是规则层席位预算**。
            #    旧实现拿 `plan_target_weight × total_assets`（= 规则层**等权分配到该席位**的预算，
            #    本轮幸存 4 只 ⇒ 每只 25% ⇒ 预算 7,463.67 元）当上限 ⇒ 落地 24.78%（7,398 元）
            #    **不破** ⇒ `slot_breach` 恒 False ⇒ 产品的破单席标记与
            #    `make_decision_options.py` / `make_decision_scenarios.py` / `probe_node_scenarios.py`
            #    三处的「落地金额 > 单席上限（5,970.93 元 = 20% × 总资产）」口径**互相矛盾**
            #    ⇒ 闸门 `probe_node_scenarios` P8（独立重算破单席标记）真红。
            #    ⇒ 修法：上限口径与全仓统一到**账户级单席上限** `A.TARGET_POSITION_WEIGHT`。
            scenario_fields = {
                "plan_target_weight": plan_target_weight,
                "plan_action": act.get("action") or "",
                "weight_to_cash": (plan_target_weight if kind == "none" else 0.0),
                "scenario_override": {
                    "kind": kind,
                    "weight_pct": ov.get("weight_pct"),
                    "shares": ov.get("shares"),
                    "basis": ov.get("basis"),
                    "source": ov.get("source"),
                },
            }
        if scenario_mode:
            # 🔴 2026-11-09 修（生产基线重建轮）：情景模式下**每一行**都要带破单席判定 ——
            #    旧实现把 `slot_breach` 只写在**被覆盖的行**（`if ov:` 分支内）⇒
            #    **选项 A（规则层，不覆盖）的那一行根本没有这个字段** ⇒
            #    聚合件的破单席清单漏掉规则层席位 ⇒ 与「落地金额 > 单席上限（20% × 总资产）」
            #    的独立重算**对不上** ⇒ 闸门 `probe_node_scenarios` P8 真红。
            #    ⇒ 修法：情景模式下**全部行**统一按**账户级单席上限**判定
            #      （默认路径不受影响 ⇒ 零回归产物逐字节不变）。
            # 🔴 2026-11-09 修（critic 复核轮）：破单席判定改成**落地口径** ——
            #    旧实现拿 `target_amount`（= **计划**目标金额 = 目标权重 × 总资产）比上限，
            #    且**不看是否真的建了仓** ⇒ 「计划 25% 但一手买不起（落地 0 股）」的席位
            #    （典型：场内货基，一手 1.0 万 > 单席 5,970.93 元）会被标成破单席，
            #    而聚合件的 `positions`（落地持仓全集，`target_shares > 0`）里**没有它**
            #    ⇒ 探针 P8「独立重算」按落地持仓重算 ⇒ 重算不出这个席位 ⇒ 口径对不上（假红）。
            #    ⇒ 修法：破单席 ⇔ **落地持仓金额**（落地股数 × 节点日价）> 单席上限，
            #      宇宙 = 落地持仓（`target_shares > 0`）⇒ 与 `make_decision_options.py`
            #      / `make_decision_scenarios.py` / `make_node_scenarios.py`
            #      / `probe_node_scenarios.py` 五处**同口径**。
            slot_limit = total_assets * float(A.TARGET_POSITION_WEIGHT) if total_assets else 0.0
            landed_amount = float(target_shares) * float(price) if target_shares > 0 else 0.0
            slot_breach = bool(landed_amount > slot_limit + 0.005)
            scenario_fields.update({
                "slot_limit_amount": round(slot_limit, 6),
                "slot_landed_amount": round(landed_amount, 6),
                "slot_breach": slot_breach,
                "slot_breach_note": (
                    f"落地金额 {landed_amount:.2f} 元 > 单席上限 {slot_limit:.2f} 元"
                    f"（账户级单席权重 {A.TARGET_POSITION_WEIGHT * 100:.0f}% × 总资产）⇒ 破单席"
                    if slot_breach else ""
                ),
            })
        if _is_ta_hold:
            #: 🔴 动作强制「维持」（不被情景覆盖改写）
            action_label = "维持"
        _row = {
            "code": code,
            "name": act.get("name") or "",
            "action": act.get("action") or "",
            "qlib_action": act.get("qlib_action") or "",
            "final_score": act.get("final_score"),
            "reason": act.get("reason") or "",
            "target_weight": target_weight,
            "target_amount": round(target_amount, 6),
            "price": round(float(price), 6),
            "price_date": price_date,
            "price_note": price_note,
            "current_shares": cur,
            "plan_current_shares": int(act.get("current_shares") or 0),
            "target_shares": target_shares,
            "delta_shares": delta_shares,
            "est_amount": round(float(est_amount), 6),
            "est_fee": round(float(est_fee), 6),
            "plan_target_shares": act.get("target_shares"),
            "plan_price": act.get("price"),
            "plan_price_date": act.get("price_date"),
            "lot_note": lot_note,
            "target_weight_effective": target_weight,
            "weight_to_cash": 0.0,
        }
        if scenario_fields:
            #: 🔴 情景字段**只在情景模式下出现** ⇒ 默认（规则层）产物逐字节不变
            _row.update(scenario_fields)
            _row["action"] = action_label
        if _is_ta_hold:
            #: 🔴 2026-11-11（审计缺陷 1）：把 `ta_rejected` / `held_not_touched`
            #    **透传**给下游 —— `daily/agents_rebalance.py` 的执行卡分支
            #    （delta=0 / 动作=维持 / 不下单）**靠这个标记才生效**。
            #: 🔴 只在 True 时写 ⇒ 默认路径（无 TA 维持行）产物**逐字节不变**。
            _row["action"] = "维持"
            _row["ta_rejected"] = True
            _row["held_not_touched"] = True
        actions.append(_row)

    # ---- 「买不进的席位」权重归现金（口径同 final_rebalance_plan）----
    unbuildable: list[dict] = []
    for a in actions:
        if a["lot_note"] and a["delta_shares"] == 0 and a["current_shares"] == 0 \
                and a["target_amount"] > 0:
            a["action"] = "不可建仓"
            a["target_weight_effective"] = 0.0
            a["weight_to_cash"] = a["target_weight"]
            a["target_shares"] = 0
            unbuildable.append({
                "code": a["code"],
                "name": a["name"],
                "target_weight": a["target_weight"],
                "target_amount": a["target_amount"],
                "one_lot_cost": round(a["price"] * LOT, 2),
                "price": a["price"],
                "reason": a["lot_note"],
            })

    # ---- 现金流（口径同 final_rebalance_plan）----
    sells = [a for a in actions if a["delta_shares"] < 0]
    buys = [a for a in actions if a["delta_shares"] > 0]
    proceeds = sum(abs(a["est_amount"]) for a in sells)
    sell_fees = sum(a["est_fee"] for a in sells)
    buy_amount = sum(a["est_amount"] for a in buys)
    buy_fees = sum(a["est_fee"] for a in buys)
    available = cash + proceeds - sell_fees
    deficit = buy_amount + buy_fees - available
    surplus = max(0.0, -deficit)
    deficit = max(0.0, deficit)
    scaled: list[dict] = []
    if deficit > 0 and buys:
        buys.sort(key=lambda a: (a["final_score"] is not None, a["final_score"] or 0),
                  reverse=True)
        budget = available
        for a in buys:
            if budget <= 0:
                a["delta_shares"] = 0
                a["target_shares"] = a["current_shares"]
                a["est_amount"] = 0.0
                a["est_fee"] = 0.0
                a["action"] = "持有"
                a["reason"] = (a["reason"] + "；现金不足，本轮不建仓").strip("；")
                scaled.append({"code": a["code"], "name": a["name"], "cut": True})
                continue
            if a["est_amount"] + a["est_fee"] > budget:
                lots = int(budget / (a["price"] * (1 + fee_rate)) / LOT) * LOT
                new_amount = lots * a["price"]
                a["target_shares"] = a["current_shares"] + lots
                a["delta_shares"] = lots
                a["est_amount"] = new_amount
                a["est_fee"] = new_amount * fee_rate
                budget -= new_amount + a["est_fee"]
                if lots <= 0:
                    a["action"] = "持有"
                    a["reason"] = (a["reason"] + "；现金不足，本轮不建仓").strip("；")
                scaled.append({"code": a["code"], "name": a["name"], "cut": True})
        buy_amount = sum(a["est_amount"] for a in buys)
        buy_fees = sum(a["est_fee"] for a in buys)
        deficit = max(0.0, buy_amount + buy_fees - available)
        surplus = max(0.0, available - (buy_amount + buy_fees))

    if scaled and total_assets:
        cut_codes = {s["code"] for s in scaled}
        for a in actions:
            if a["code"] not in cut_codes:
                continue
            plan_w = float(a["target_weight"] or 0.0)
            after_value = (a["current_shares"] + (a["delta_shares"] or 0)) * a["price"]
            eff = after_value / total_assets
            a["target_weight_effective"] = eff
            a["weight_to_cash"] = max(0.0, plan_w - eff)

    invested_weight = float(sum(a["target_weight_effective"] for a in actions))
    target_cash_weight = float(max(0.0, 1.0 - invested_weight))
    cash_after = cash + proceeds - sell_fees - buy_amount - buy_fees

    # ---- 实际落地口径（与 final_rebalance_plan 同口径）----
    realized_equity = 0.0
    snapshot_mv = 0.0
    exec_mv = 0.0
    for a in actions:
        if a["target_shares"] and a["price"]:
            realized_equity += a["target_shares"] * a["price"]
        if a["current_shares"]:
            snapshot_mv += a["current_shares"] * a["price"]
            exec_mv += a["current_shares"] * a["price"]
    planned_equity = invested_weight * total_assets if total_assets else None
    lot_gap = (planned_equity - realized_equity) if total_assets else None
    equity_weight_realized = (realized_equity / total_assets) if total_assets else None
    cash_weight_realized = (cash_after / total_assets) if total_assets else None

    return {
        "actions": actions,
        "cash_flow": {
            "cash_begin": round(cash, 6),
            "sell_proceeds": round(proceeds, 6),
            "sell_fees": round(sell_fees, 6),
            "buy_amount": round(buy_amount, 6),
            "buy_fees": round(buy_fees, 6),
            "available_before_buy": round(available, 6),
            "deficit": round(deficit, 6),
            "surplus": round(surplus, 6),
            "headroom": round(surplus, 6),
            "cash_end": round(cash_after, 6),
            "feasible": bool(deficit <= 0.0),
            "scaled_down": scaled,
            "unbuildable_slots": unbuildable,
        },
        "landing": {
            "planned_equity_weight": invested_weight,
            "target_cash_weight": target_cash_weight,
            "target_cash_amount": (target_cash_weight * total_assets) if total_assets else None,
            "realized_equity_amount": round(realized_equity, 6),
            "realized_equity_weight": equity_weight_realized,
            "realized_cash_weight": cash_weight_realized,
            "lot_rounding_gap": lot_gap,
            "planned_equity_amount": planned_equity,
            "snapshot_market_value_at_node_price": round(snapshot_mv, 6),
        },
    }


def _node_total_assets(
    plan: dict,
    prices: dict[str, tuple[float, str]],
    node_date: str,
    current_shares: dict[str, int],
    cash: float,
    holding_names: dict[str, str] | None = None,
) -> tuple[float, list[dict]]:
    """按**节点日价格**重算账户总资产：现金 + Σ(**账户全部持仓**股数 × 节点日价)。

    为什么要它（M-S5）：
    计划件里的 `total_assets` 是**账户快照日**（2026-11-04，`account.py` 常量）的值
    ⇒ 节点日价格变了以后，「目标金额 = 目标权重 × 总资产」若还用旧总资产，等权目标会**整体偏**。
    本函数给出**节点日口径**的总资产，并逐条记录用的价格、价格日期与价格来源（**不静默**）。

    🔴 2026-11-05（M-S5 独立审计 **BLOCKING** 修复）：旧实现只遍历 ``plan["actions"]``
       ⇒ **计划外持仓**（账户快照里有、计划件里没有的标的，例如临时多出来的一只 ETF）
       被**静默漏掉** ⇒ node 口径总资产算小 ⇒ 目标金额/股数整体偏 ⇒ 交易清单错，
       而产物仍宣称公式是「现金 + Σ(持仓股数 × 节点日收盘价)」⇒ **披露与实现不符**。
       现在遍历 ``current_shares`` **全量**；计划外持仓在明细里用 ``in_plan_actions=False``
       标出，并在 `basis_summary` / 风险披露里**显式登记**（不静默）。
       价格：节点日行情优先；无行情 ⇒ 计划件价格；两者都没有 ⇒ **抛异常**（不编数字）。

    返回 ``(总资产, 逐条持仓明细)``。
    """
    holdings: list[dict] = []
    total = float(cash)
    plan_by_code = {str(a.get("code") or "").zfill(6): a for a in plan["actions"]}
    #: 遍历顺序 = 计划件 actions 顺序在前、**计划外持仓**按代码排序在后
    #: （计划件顺序保持不变 ⇒ 默认口径产物逐字节不变；计划外持仓**不再被静默漏掉**）
    ordered: list[str] = []
    for a in plan["actions"]:
        c = str(a.get("code") or "").zfill(6)
        if c and c not in ordered:
            ordered.append(c)
    for c in sorted(current_shares):
        if c not in ordered:
            ordered.append(c)
    names = holding_names or {}
    for code in ordered:
        act = plan_by_code.get(code, {})
        shares = int(current_shares.get(code, int(act.get("current_shares") or 0)))
        if shares <= 0:
            continue
        price, price_date, note = _resolve_price(act, code, prices, node_date)
        market_value = shares * float(price)
        total += market_value
        holdings.append({
            "code": code,
            "name": act.get("name") or names.get(code) or "",
            "shares": shares,
            "price": round(float(price), 6),
            "price_date": price_date,
            "price_is_node_date": (price_date == node_date),
            "price_source": "节点日行情" if code in prices else "计划件价格（节点日无行情）",
            "price_note": note,
            "market_value": round(market_value, 6),
            "in_plan_actions": code in plan_by_code,
        })
    return total, holdings


def _basis_comparison(
    actions: list[dict],
    plan_total_assets: float,
    node_total_assets: float,
    effective_total_assets: float,
) -> list[dict]:
    """逐条对比「计划件口径」与「节点日口径」总资产下的目标股数，并给出**跨档阈值**。

    跨档阈值口径：目标股数 n = floor(目标权重 × 总资产 / 价格 / 整手) × 整手
    ⇒ n 保持不变的总资产区间 = [n×价格/权重, (n+整手)×价格/权重)
    ⇒ 漂移越过区间边界，股数就会跨一手（买或卖）。
    """
    rows: list[dict] = []
    for a in actions:
        weight = float(a.get("target_weight") or 0.0)
        price = _f(a.get("price"))
        if weight <= 0 or not price:
            continue
        shares_plan = _round_lot(weight * plan_total_assets / price)
        shares_node = _round_lot(weight * node_total_assets / price)
        shares_eff = int(a.get("target_shares") or 0)
        lo = shares_eff * price / weight
        hi = (shares_eff + LOT) * price / weight
        rows.append({
            "code": a["code"],
            "name": a["name"],
            "price": round(float(price), 6),
            "target_weight": weight,
            "shares_plan_basis": shares_plan,
            "shares_node_basis": shares_node,
            "shares_effective": shares_eff,
            "shares_differ_plan_vs_node": bool(shares_plan != shares_node),
            "total_assets_window_lo": round(lo, 6),
            "total_assets_window_hi": round(hi, 6),
            "drift_to_window_lo_pct": (
                round((lo - effective_total_assets) / effective_total_assets * 100, 6)
                if effective_total_assets else None
            ),
            "drift_to_window_hi_pct": (
                round((hi - effective_total_assets) / effective_total_assets * 100, 6)
                if effective_total_assets else None
            ),
        })
    return rows


def _basis_summary(
    rows: list[dict],
    plan_total_assets: float,
    node_total_assets: float,
    effective_total_assets: float,
    requested_basis: str,
    effective_source: str,
    node_holdings: list[dict] | None = None,
) -> dict:
    """汇总双口径差异 + 「第一个跨档阈值」（离 0 最近的漂移幅度）。

    🔴 2026-11-05（M-S5 独立审计 MINOR 修复）——**方向语义**：
       漂移为**负**（总资产在往下走）时，先被跨掉的是**下边界 `lo`**；
       漂移为**正**时先跨掉的是**上边界 `hi`**。
       旧实现把两侧混在一起取「最小绝对值」⇒ 会报出一个**漂移方向上永远碰不到**的阈值。
       现在按 `drift_pct` 的符号**只在漂移方向上**取最近阈值；
       漂移恰为 0（两口径相等）时两侧都算（`direction="both"`）。
    """
    drift_abs = node_total_assets - plan_total_assets
    drift_pct = (drift_abs / plan_total_assets * 100) if plan_total_assets else None
    # 🔴 浮点容差：node_total 与 plan_total 是**同一笔钱**时，浮点误差会让 drift_pct 变成
    #    ±1e-13（例：29854.670000000002 vs 29854.67）⇒ 若按符号判方向会把「无漂移」
    #    误判成「向上漂移」⇒ 报出的跨档阈值方向**整个反过来**。
    #    ⇒ 漂移幅度 < _DRIFT_EPS_PCT（1e-9 %）一律视为**无漂移**（direction="both"）。
    direction = "both"
    if drift_pct is not None and drift_pct < -_DRIFT_EPS_PCT:
        direction = "down"
    elif drift_pct is not None and drift_pct > _DRIFT_EPS_PCT:
        direction = "up"
    side_allowed = {
        "down": ("drift_to_window_lo_pct",),
        "up": ("drift_to_window_hi_pct",),
        "both": ("drift_to_window_lo_pct", "drift_to_window_hi_pct"),
    }[direction]
    tripwires: list[tuple[float, str, str, float]] = []
    for r in rows:
        for key in side_allowed:
            value = r.get(key)
            if value is None:
                continue
            if abs(value) > 0:
                tripwires.append((abs(value), r["code"], key, value))
    tripwires.sort()
    first = tripwires[0] if tripwires else None
    off_plan = [h for h in (node_holdings or []) if not h.get("in_plan_actions")]
    return {
        "requested_basis": requested_basis,
        "effective_source": effective_source,
        "plan_total_assets": round(plan_total_assets, 6),
        "node_total_assets": round(node_total_assets, 6),
        "effective_total_assets": round(effective_total_assets, 6),
        "drift_abs": round(drift_abs, 6),
        "drift_pct": (
            round(drift_pct, 6) if drift_pct is not None else None
        ),
        "tripwire_direction": direction,
        "n_off_plan_holdings": len(off_plan),
        "off_plan_holdings": [
            {"code": h.get("code"), "name": h.get("name") or "",
             "shares": h.get("shares"), "market_value": h.get("market_value")}
            for h in off_plan
        ],
        "n_rows": len(rows),
        "n_shares_differ_plan_vs_node": sum(
            1 for r in rows if r["shares_differ_plan_vs_node"]
        ),
        "first_tripwire_pct": round(first[0], 6) if first else None,
        "first_tripwire_code": first[1] if first else None,
        "first_tripwire_side": first[2] if first else None,
        "first_tripwire_drift_pct": round(first[3], 6) if first else None,
    }


def _render_md(payload: dict) -> str:
    """把重算结果渲染成中文 markdown。"""
    meta = payload["meta"]
    cf = payload["cash_flow"]
    land = payload["landing"]
    lines: list[str] = []
    add = lines.append
    add(f"# 节点日股数重算 · {meta['node_date']}")
    add("")
    add(f"- 生成时间：{meta['generated_at']}")
    add(f"- **节点日**：{meta['node_date']}（调仓节点；**执行日 = 节点日 + 1 交易日** = "
        f"{meta.get('execution_date') or '（日历覆盖不到，未计算）'}）")
    add(f"- **信号来源**：计划件 `{meta['plan_path']}`（信号日 {meta['plan_asof']}）"
        f" ⇒ **本次只重算股数/金额，不改信号**（名单与目标权重原样沿用）")
    add(f"- 价格基准：`{meta['prices_path']}` 的 **close**，取 {meta['node_date']} 及之前的最后一根")
    add(f"- 账户口径：持仓 {meta['account_source']}；现金 **{meta['cash']:.2f}** 元、"
        f"总资产 **{meta['total_assets']:.2f}** 元（{meta['account_note']}）")
    add(f"- **总资产口径**：{meta.get('basis_note') or '（读不到）'}")
    add(f"- 费率：佣金 {meta['commission_bp']:.1f} bp ＋ 滑点 {meta['slippage_bp']:.1f} bp "
        f"⇒ 单边 {meta['fee_rate'] * 10000:.1f} bp（ETF 免印花税 / 过户费）；整手 {LOT} 份")
    _sc = meta.get("scenario")
    if _sc:
        add(f"- 🔴 **分歧情景模式**：**{_sc['label']}** —— 本清单按用户拍板的选项重算目标"
            f"（**不是**纯规则层；规则层原值见各行「规则层权重」列）")
        add("")
        add("| 分歧项 | 选项 | 语义 | 依据 |")
        add("|:--|:--:|:--|:--|")
        for _r in _sc.get("rows", []):
            add(f"| {_r['code']} | {_r['choice']} | {_r['kind_label']} | {_r['basis']} |")
    add("")
    add("> ⚠️ **节点日收盘价 ≠ 执行日成交价**：股数是「按节点日价折算」，实际在**执行日收盘**成交。")
    add("> ⚠️ **这是「按节点日价的重算值」，不是新的信号**：S 层（个股分数）在节点日不存在，"
        "信号仍是计划件里那一份（专题档 §45）。")
    add("")
    add("## 一、按节点日价折算的买卖清单")
    add("")
    add("| 代码 | 名称 | 动作 | 当前股数 | 节点日价 | 价日期 | 目标权重 | 目标金额 | "
        "目标股数 | 变动股数 | 金额 | 费用 | 备注 |")
    add("|:--|:--|:--|--:|--:|:--|--:|--:|--:|--:|--:|--:|:--|")
    for a in payload["actions"]:
        note = a["lot_note"] or a["price_note"] or ""
        add(f"| {a['code']} | {a['name']} | {a['action']} | {a['current_shares']} | "
            f"{a['price']:.3f} | {a['price_date']} | {a['target_weight'] * 100:.1f}% | "
            f"{a['target_amount']:.2f} | {a['target_shares']} | {a['delta_shares']:+d} | "
            f"{a['est_amount']:+.2f} | {a['est_fee']:.2f} | {note} |")
    add("")
    add("## 二、与计划件（信号日价）的差异")
    add("")
    add("| 代码 | 名称 | 计划目标股数 | 节点日目标股数 | 差异 | 计划价 | 节点日价 | 价差 |")
    add("|:--|:--|--:|--:|--:|--:|--:|--:|")
    for d in payload["diff_vs_plan"]:
        add(f"| {d['code']} | {d['name']} | {d['plan_target_shares']} | "
            f"{d['node_target_shares']} | {d['shares_diff']:+d} | "
            f"{d['plan_price']} | {d['node_price']} | {d['price_diff_pct']:+.2f}% |")
    add("")
    add("## 三、总资产口径对照（plan vs node）")
    add("")
    _bs = payload.get("basis_summary") or {}
    add(f"- 生效口径：**{_bs.get('effective_source')}** ｜ 生效总资产 "
        f"**{_bs.get('effective_total_assets')}** 元")
    add(f"- 计划件口径 **{_bs.get('plan_total_assets')}** 元 vs 节点日口径 "
        f"**{_bs.get('node_total_assets')}** 元 ⇒ 差额 **{_bs.get('drift_abs')}** 元 "
        f"（**{_bs.get('drift_pct')}%**）")
    add(f"- 两口径目标股数**不一致**的标的数：**{_bs.get('n_shares_differ_plan_vs_node')}**"
        f"（共比较 {_bs.get('n_rows')} 条）")
    if _bs.get("first_tripwire_pct") is not None:
        add(f"- 第一个跨档阈值：**{_bs.get('first_tripwire_code')}**（总资产漂移 "
            f"**{_bs.get('first_tripwire_drift_pct')}%** ⇒ 股数跨一手）")
    else:
        add("- 第一个跨档阈值：**无**（本次无标的会因总资产漂移而跨档）")
    add("")
    add("| 代码 | 名称 | 节点日价 | 目标权重 | plan 口径股数 | node 口径股数 | 生效股数 | "
        "两口径是否不同 | 持仓区间下限 | 持仓区间上限 |")
    add("|:--|:--|--:|--:|--:|--:|--:|:--|--:|--:|")
    for r in payload.get("basis_comparison") or []:
        add(f"| {r['code']} | {r['name']} | {r['price']:.3f} | "
            f"{r['target_weight'] * 100:.1f}% | {r['shares_plan_basis']} | "
            f"{r['shares_node_basis']} | {r['shares_effective']} | "
            f"{'是' if r['shares_differ_plan_vs_node'] else '否'} | "
            f"{r['total_assets_window_lo']:.2f} | {r['total_assets_window_hi']:.2f} |")
    add("")
    add("## 四、现金流与实际落地")
    add("")
    add(f"- 期初现金 **{cf['cash_begin']:.2f}** ｜ 卖出回款 **{cf['sell_proceeds']:.2f}** "
        f"（费用 {cf['sell_fees']:.2f}）｜ 买入 **{cf['buy_amount']:.2f}** "
        f"（费用 {cf['buy_fees']:.2f}）")
    add(f"- 可用资金 **{cf['available_before_buy']:.2f}** ｜ 缺口 **{cf['deficit']:.2f}** "
        f"｜ 富余 **{cf['surplus']:.2f}** ⇒ {'✅ 可执行' if cf['feasible'] else '🔴 有缺口'}")
    add(f"- 期末现金 **{cf['cash_end']:.2f}**")
    add(f"- 实际落地权益 **{land['realized_equity_amount']:.2f} 元 / "
        f"{land['realized_equity_weight'] * 100:.2f}%**（计划权益权重 "
        f"{land['planned_equity_weight'] * 100:.2f}%）｜ 整手取整缺口 "
        f"**{(land['lot_rounding_gap'] or 0):.2f} 元**")
    if cf["unbuildable_slots"]:
        add("")
        add("**整手买不起（权重归现金）**：")
        for u in cf["unbuildable_slots"]:
            add(f"- {u['code']} {u['name']}：目标 {u['target_amount']:.2f} 元 < 一手 "
                f"{u['one_lot_cost']:.2f} 元 ⇒ 不可建仓")
    add("")
    add("## 五、风险披露")
    add("")
    for r in payload["risk_disclosure"]:
        add(f"- {r}")
    add("")
    add("---")
    add("")
    add("> ℹ️ 本产物由 `manu/etf_rotation/reprice_node_day.py` 生成（**只重算股数/金额**）。")
    add("> ℹ️ **不构成投资建议**。")
    return "\n".join(lines) + "\n"


def main() -> int:
    """跑重算；退出码 0 = 产物已写出，1 = 失败（中文错误）。"""
    ap = argparse.ArgumentParser(description="节点日股数/金额重算（只重算价格口径）")
    ap.add_argument("--node-date", required=True, help="节点日（YYYY-MM-DD），如 2026-12-01")
    ap.add_argument("--plan", default=str(C.ETF_RUN_ROOT / "final_rebalance_plan.json"))
    ap.add_argument("--prices", default=str(C.ETF_PRICES_CSV))
    ap.add_argument("--account", default=None,
                    help="节点日的持仓快照 CSV（列同 account_snapshot.csv）；不给则用计划件口径")
    ap.add_argument("--cash", type=float, default=None, help="节点日现金（不给则用计划件口径）")
    ap.add_argument("--total-assets", type=float, default=None,
                    help="节点日总资产（不给则用计划件口径）")
    ap.add_argument("--total-assets-basis", choices=("plan", "node"), default="plan",
                    help="总资产口径：plan=计划件（信号日快照，默认）｜"
                         "node=节点日价格口径（现金 + Σ持仓×节点日收盘价）")
    ap.add_argument("--out-dir", default=str(C.ETF_RUN_ROOT))
    ap.add_argument("--scenario", default=None,
                    help="分歧情景规格：`代码=A|B` 逗号分隔（如 `511880=B,512800=A,563020=A`）；"
                         "A=规则层、B=LLM 最终裁决；不给则按计划件（纯规则层）重算")
    args = ap.parse_args()

    node_date = _parse_date(args.node_date)
    if node_date is None:
        raise ValueError(f"节点日无法解析（--node-date {args.node_date!r}）⇒ 拒绝重算（不编日期）")

    plan_path = Path(args.plan)
    plan = _load_plan(plan_path)
    plan_meta = plan.get("meta") or {}
    plan_asof = plan_meta.get("asof")

    # ---- M-S7：分歧情景（用户拍板「某项采纳 LLM 裁决」）⇒ 节点日重算的目标覆盖 ----
    # 🔴 不给 `--scenario` ⇒ **完全等价于改造前**（覆盖为空 ⇒ 产物逐字节不变）。
    scenario_choices = _parse_scenario(args.scenario) if args.scenario else None
    overrides: dict[str, dict] | None = None
    scenario_rows: list[dict] = []
    if scenario_choices:
        overrides, scenario_rows = _scenario_overrides(plan, scenario_choices)

    plan_total_assets = _f(plan_meta.get("total_assets"), 0.0)
    cash = args.cash if args.cash is not None else _f(plan_meta.get("cash"), 0.0)

    # ---- 当前持仓：默认取计划件；给了 --account 则用快照的股数 ----
    # 🔴 2026-11-05（M-S5 独立审计 IMPORTANT 修复）：计划件的 `total_assets` 来自
    #    `account.py` 常量（**账户快照日 2026-11-04**），**不是**信号日 2026-11-03
    #    ⇒ 旧标签「信号日账户快照」与事实不符 ⇒ 读者会误判 plan 档的时点。
    account_source = f"计划件口径（账户快照日 = {A.ACCOUNT_SNAPSHOT_DATE}）"
    account_note = (
        f"持仓 / 现金 / 总资产均来自计划件的账户口径"
        f"（**账户快照日 {A.ACCOUNT_SNAPSHOT_DATE}**；信号日 = {plan_asof}）"
    )
    current_shares: dict[str, int] = {}
    holding_names: dict[str, str] = {}
    for act in plan["actions"]:
        code = str(act.get("code") or "").zfill(6)
        current_shares[code] = int(act.get("current_shares") or 0)
        holding_names[code] = str(act.get("name") or "")
    if args.account:
        acc_path = Path(args.account)
        if not acc_path.exists():
            raise FileNotFoundError(f"持仓快照缺失：`{acc_path}` 不存在 ⇒ 无法重算股数")
        acc = load_account(str(acc_path))
        current_shares = {c: int(v.get("shares") or 0) for c, v in acc.items()}
        for _c, _v in acc.items():
            if str(_v.get("name") or ""):
                holding_names[_c] = str(_v.get("name") or "")
        account_source = f"`{acc_path}`（账户快照 CSV **不含日期字段**）"
        account_note = (
            f"**持仓股数**取自节点日快照 `{acc_path}`；**现金 / 总资产**"
            + ("取自 --cash / --total-assets" if (args.cash is not None
                                              or args.total_assets is not None)
               else f"仍取计划件口径（快照日 {plan_asof}）")
            + " ⇒ 两个口径**可能不一致**（若期间已交易），请自行核对"
        )

    prices_path = Path(args.prices)
    if not prices_path.exists():
        raise FileNotFoundError(f"ETF 行情缺失：`{prices_path}` 不存在 ⇒ 无法折算股数")
    prices = load_prices(str(prices_path), node_date)
    if not prices:
        raise ValueError(f"ETF 行情 `{prices_path}` 在 {node_date} 及之前**没有任何数据** ⇒ 无法折算股数")
    # 🔴 节点日必须**被行情覆盖**：否则 `load_prices` 会静默返回**旧价**
    #    （它取的是「≤ asof 的最后一根」）⇒ 拿 11-04 的价冒充 12-01 的价 ⇒ **假结果**。
    #    ⇒ 硬拦：行情最新日必须 ≥ 节点日（口径同 `analyze.py::_data_freshness_guard`）。
    _px = pd.read_csv(prices_path, usecols=["date"])
    _px_max = pd.to_datetime(_px["date"], errors="coerce").max()
    if pd.isna(_px_max):
        raise ValueError(f"ETF 行情 `{prices_path}` 的日期列**解析不出任何日期** ⇒ 无法折算股数")
    _px_max_s = _px_max.strftime("%Y-%m-%d")
    if _px_max_s < node_date:
        raise ValueError(
            f"🔴 **ETF 行情覆盖不到节点日**：`{prices_path}` 最新只到 {_px_max_s}，"
            f"而节点日是 {node_date}（差 {int((pd.Timestamp(node_date) - _px_max).days)} 个自然日）"
            f" ⇒ 折算出来的股数会用**旧价冒充节点日价** ⇒ **假结果** ⇒ 拒绝重算。\n"
            f"   处置：先刷新 ETF 行情（akshare 环境；**绝不往 ql env 装 akshare**）：\n"
            f"     /home/huangzhe/anaconda3/envs/ak/bin/python "
            f"manu/etf_rotation/data/fetch_etf_prices.py --resume\n"
            f"     （`--end` 留空 = 自动取「今天之前（含）的最近一个交易日」）"
        )

    # ---- 总资产口径（M-S5）：plan / node / 显式覆盖 三选一 ----
    # 🔴 node 口径 = 现金 + Σ(持仓股数 × 节点日收盘价) ⇒ 与「目标权重 × 总资产」同处一个时点，
    #    避免拿**信号日**的总资产去配**节点日**的目标金额（等权目标会整体偏）。
    node_total_assets, node_holdings = _node_total_assets(
        plan, prices, node_date, current_shares, cash, holding_names)
    #: 计划外持仓（账户里有、计划件里没有）⇒ **必须显式登记**，不许静默（审计 BLOCKING 修复）
    off_plan_holdings = [h for h in node_holdings if not h.get("in_plan_actions")]
    off_plan_codes = [str(h.get("code")) for h in off_plan_holdings]
    off_plan_mv = sum(float(h.get("market_value") or 0.0) for h in off_plan_holdings)
    requested_basis = args.total_assets_basis
    if args.total_assets is not None:
        total_assets = float(args.total_assets)
        effective_source = "explicit（--total-assets 显式覆盖）"
    elif requested_basis == "node":
        total_assets = node_total_assets
        effective_source = "node（节点日价格口径：现金 + Σ持仓×节点日收盘价）"
    else:
        total_assets = plan_total_assets
        effective_source = (
            f"plan（计划件口径：**账户快照日 {A.ACCOUNT_SNAPSHOT_DATE}** 的账户总资产，"
            "取 `account.py` 常量 TOTAL_ASSETS；≠ 信号日价格口径）"
        )
    if not total_assets or total_assets <= 0:
        raise ValueError(f"总资产口径无效（{total_assets}）⇒ 拒绝重算（不编数字）")
    if requested_basis == "node" and node_total_assets <= 0:
        raise ValueError(
            f"节点日口径总资产无效（{node_total_assets}）⇒ 拒绝重算（不编数字）"
        )
    basis_note = (
        f"总资产口径 = **{effective_source}**：生效值 {total_assets:.2f} 元；"
        f"计划件口径 {plan_total_assets:.2f} 元、节点日口径 {node_total_assets:.2f} 元，"
        f"差额 {node_total_assets - plan_total_assets:+.2f} 元"
        + (f"（{(node_total_assets - plan_total_assets) / plan_total_assets * 100:+.4f}%）"
           if plan_total_assets else "")
        + (f"；**计划外持仓 {len(off_plan_holdings)} 只**（{'、'.join(off_plan_codes)}，"
           f"节点日市值合计 {off_plan_mv:.2f} 元）**已计入**节点日口径总资产"
           if off_plan_holdings else "")
    )

    fee_rate = _fee_rate(plan)
    params = (plan_meta.get("params") or {})
    commission_bp = _f(params.get("commission_bp"))
    slippage_bp = _f(params.get("slippage_bp"))
    if commission_bp is None:
        commission_bp = float(C.ETF_COMMISSION) * 10000.0
    if slippage_bp is None:
        slippage_bp = float(C.ETF_SLIPPAGE) * 10000.0

    result = _recompute(plan, prices, node_date, total_assets, cash,
                        current_shares, fee_rate, overrides=overrides)

    # ---- 双口径对照（M-S5）：计划件口径 vs 节点日口径，目标股数差多少、阈值在哪 ----
    basis_rows = _basis_comparison(
        result["actions"], plan_total_assets, node_total_assets, total_assets)
    basis = _basis_summary(
        basis_rows, plan_total_assets, node_total_assets, total_assets,
        requested_basis, effective_source, node_holdings)

    # ---- 与计划件的差异 ----
    diff_vs_plan: list[dict] = []
    for a in result["actions"]:
        plan_price = _f(a["plan_price"])
        node_price = a["price"]
        price_diff_pct = (
            ((node_price - plan_price) / plan_price * 100.0) if plan_price else None
        )
        diff_vs_plan.append({
            "code": a["code"],
            "name": a["name"],
            "plan_target_shares": a["plan_target_shares"],
            "node_target_shares": a["target_shares"],
            "shares_diff": int(a["target_shares"]) - int(a["plan_target_shares"] or 0),
            "plan_price": a["plan_price"],
            "plan_price_date": a["plan_price_date"],
            "node_price": node_price,
            "node_price_date": a["price_date"],
            "price_diff_pct": price_diff_pct,
        })

    # ---- 执行日（节点日 + 1 交易日）：从合并日历推 ----
    execution_date = None
    try:
        sys.path.insert(0, str(ROOT / "data"))
        import signal_policy as SP

        cal_path = Path(str(getattr(C, "PROVIDER_URI", ""))) / "calendars" / "day.txt"
        extra = C.ETF_DATA_ROOT / "trade_calendar.csv"
        cal = set(SP.read_calendar_dates(cal_path))
        if extra.exists():
            df = pd.read_csv(extra, dtype=str)
            cal |= {d for d in (SP.parse_date(v) for v in df["date"].dropna()) if d}
        after = sorted(d for d in cal if d > SP.parse_date(node_date))
        if after:
            execution_date = after[0].isoformat()
    except Exception as exc:  # noqa: BLE001
        print(f"[WARN] 执行日推导失败（{exc!r}）⇒ 执行日记为「未计算」，不编日期")

    stale_notes = [a["price_note"] for a in result["actions"] if a["price_note"]]
    # qlib 数据集覆盖末日（用于「节点日有没有 S 层」的如实披露；读不到就不提）
    try:
        sys.path.insert(0, str(ROOT / "data"))
        import signal_policy as SP

        _cal = Path(str(getattr(C, "PROVIDER_URI", ""))) / "calendars" / "day.txt"
        _qlib_max = SP.calendar_max_date(_cal)
    except Exception:  # noqa: BLE001
        _qlib_max = None
    _s_note = (
        f"qlib 数据集只到 **{_qlib_max}** ⇒ 节点日 {node_date} 的 S 层"
        + ("**不存在**" if _qlib_max and _qlib_max < node_date else "是否可用取决于打分栅格")
        + "（专题档 §45）"
        if _qlib_max else "（qlib 数据集覆盖末日读不到）"
    )
    # ---- 双口径差异的成文披露（先拼好，避免在 f-string 里嵌表达式）----
    n_differ = int(basis["n_shares_differ_plan_vs_node"])
    if n_differ:
        _diff_text = f"**不一致**，涉及 {n_differ} 条标的"
    else:
        _diff_text = "**逐条一致**（本次切换口径不改变任何股数）"
    _tw_code = basis["first_tripwire_code"] or "（无：本次无标的会因漂移跨档）"
    _tw_drift = basis["first_tripwire_drift_pct"]
    _tw_text = (f"{_tw_code}（漂移 {_tw_drift:+.4f}% ⇒ 股数跨一手）"
                if _tw_drift is not None else _tw_code)
    _drift_pct = basis["drift_pct"]
    _drift_text = f"{_drift_pct:+.4f}%" if _drift_pct is not None else "（读不到）"
    _basis_risk = (
        f"🔴 **总资产口径（plan / node 双口径）**：{basis_note}。"
        f"两种口径下目标股数{_diff_text}；"
        f"总资产相对生效口径漂移 **{_drift_text}** 时，"
        f"第一个跨档阈值出现在 **{_tw_text}**。"
        f"⇒ **执行日应以 node 口径为准**（目标权重 × 总资产 需与节点日价格同一时点），"
        f"但前提是**账户在节点日之前没有额外交易**；若期间已交易，须先更新账户快照再重算。"
    )
    risk = [
        f"🔴 **节点日收盘价 ≠ 执行日成交价**：本清单的股数按 **{node_date} 收盘价**折算，"
        f"实际在执行日 **{execution_date or '（未算出）'} 收盘**成交 ⇒ 成交股数/金额与本表可能不同。",
        f"🔴 **信号未刷新**：本次**只重算股数与金额**，信号（名单 + 目标权重）仍来自计划件"
        f"（信号日 {plan_asof}）⇒ {_s_note} ⇒ 本表**不是**「按新信号」的清单。",
        f"🔴 **账户口径可能已过期**：{account_note}。",
        _basis_risk,
        f"🔴 **整手与费用口径**：整手 {LOT} 份、佣金 {commission_bp:.1f} bp + 滑点 "
        f"{slippage_bp:.1f} bp（单边 {fee_rate * 10000:.1f} bp）⇒ 股数取整缺口 "
        f"{(result['landing']['lot_rounding_gap'] or 0):.2f} 元自动留现金。",
    ]
    if off_plan_holdings:
        risk.append(
            f"🔴 **计划外持仓已计入总资产**：账户里有、计划件里没有的标的共 "
            f"**{len(off_plan_holdings)}** 只（{'、'.join(off_plan_codes)}），"
            f"节点日市值合计 **{off_plan_mv:.2f} 元**，**已计入**节点日口径总资产 "
            f"{node_total_assets:.2f} 元 ⇒ 若这些标的不在目标名单里，它们就是"
            "**未被处置的存量持仓**（本工具只重算股数，不生成卖出指令）⇒ 需人工确认。"
        )
    if stale_notes:
        risk.append(f"⚠️ **节点日行情不齐**：{len(stale_notes)} 只标的的价日期不是 {node_date}："
                    + "；".join(sorted(set(stale_notes))))
    if scenario_choices:
        _rows_txt = "；".join(
            f"{r['code']}={r['choice']}（{r['kind_label']}）" for r in scenario_rows)
        risk.append(
            f"🔴 **本清单是「分歧情景」口径，不是纯规则层**：已按用户拍板的 {_rows_txt} "
            f"改写目标 ⇒ 目标权重/目标股数**与计划件（规则层）不同**；"
            f"**规则层原值**同时记在每条动作的 `plan_target_weight` / `plan_action` 字段里，"
            f"便于逐条对照。"
        )
    risk.append("⚠️ 本工具**不产生新信号**、**不改账户快照**、**不覆盖既有交付件**"
                "（输出为新文件 `node_reprice_<节点日>.*`）。")

    if scenario_choices:
        _label = ",".join(f"{c}{scenario_choices[c]}" for c in sorted(scenario_choices))
        _slug = "_".join(f"{c}{scenario_choices[c]}" for c in sorted(scenario_choices))
        meta_scenario = {
            "label": _label,
            "slug": _slug,
            "choices": dict(sorted(scenario_choices.items())),
            "rows": scenario_rows,
            "n_overrides": len(overrides or {}),
            "note": ("选项 A = 规则层（不覆盖）；选项 B = LLM 最终裁决"
                     "（语义取自 llm_final_decision.targets：lots / target_pct）"),
        }
    else:
        meta_scenario = None

    payload = {
        "meta": {
            "generated_at": _now_iso(),
            "node_date": node_date,
            "execution_date": execution_date,
            "execution_lag_days": int(C.EXECUTION_LAG_DAYS),
            "plan_path": str(plan_path),
            "plan_asof": plan_asof,
            "prices_path": str(prices_path),
            "account_source": account_source,
            "account_note": account_note,
            "cash": cash,
            "total_assets": total_assets,
            "total_assets_basis": requested_basis,
            "total_assets_source": effective_source,
            "plan_total_assets": round(plan_total_assets, 6),
            "node_total_assets": round(node_total_assets, 6),
            "node_holdings": node_holdings,
            "basis_note": basis_note,
            "commission_bp": commission_bp,
            "slippage_bp": slippage_bp,
            "fee_rate": fee_rate,
            "lot_size": LOT,
            "scope": "只重算股数/金额（价格口径）；不改信号、不改目标权重、不改账户快照",
        },
        "cash_flow": result["cash_flow"],
        "landing": result["landing"],
        "actions": result["actions"],
        "diff_vs_plan": diff_vs_plan,
        "basis_comparison": basis_rows,
        "basis_summary": basis,
        "risk_disclosure": risk,
        "disclaimer": "本产物为模型与规则的输出，不构成投资建议。",
    }

    if scenario_choices:
        payload["meta"]["scenario"] = meta_scenario

    out_dir = Path(args.out_dir)
    out_dir.mkdir(parents=True, exist_ok=True)
    stem = f"node_reprice_{node_date.replace('-', '')}"
    if scenario_choices:
        stem = f"{stem}_sc_{meta_scenario['slug']}"
    json_path = out_dir / f"{stem}.json"
    md_path = out_dir / f"{stem}.md"
    json_path.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")
    md_path.write_text(_render_md(payload), encoding="utf-8")

    print(f"[OK] 节点日 {node_date}（执行日 {execution_date or '未算出'}）")
    print(f"[OK] 已写出 {json_path}")
    print(f"[OK] 已写出 {md_path}")
    print(f"[INFO] 期末现金 {result['cash_flow']['cash_end']:.2f} 元 ｜ "
          f"实际落地权益 {result['landing']['realized_equity_amount']:.2f} 元 "
          f"（{result['landing']['realized_equity_weight'] * 100:.2f}%）｜ "
          f"缺口 {result['cash_flow']['deficit']:.2f} 元")
    return 0


if __name__ == "__main__":
    sys.exit(main())
