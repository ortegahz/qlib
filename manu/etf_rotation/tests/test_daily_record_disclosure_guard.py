"""🔴 2026-11-20 新增：逐日人读记录（``manu/memory/records/<YYYYMMDD>.md``）的**三类关键披露**守卫。

【背景：为什么这三类披露必须有守卫（真实复发点）】
``manu/tools/append_daily_record.py`` 从快照产物生成逐日人读记录。
2026-11-20 给它加了**三类关键披露**（都是「开源数据接口事后拿不到、但复盘必须要」的口径）：

  ① **成分股快照 W 的陈旧度** —— 来自 ``snapshot_manifest.json`` 的 ``stages[]`` 里
     ``name == "w_holdings"`` 那一项（``status`` / ``max_fetch_date`` / ``stale_days`` / ``n_codes``）
     ⇒ 落成**头部一行** ＋ **§三 一行**。
     🔴 复发点：``w_holdings`` 的状态是 ``stale``，它**不属于** ``{failed, error, degraded}``
     ⇒ 旧的「失败/跳过环节」口径**永远不会**显示它 ⇒ 极易被下一次改工具时**静默删掉**。
  ② **费率口径分歧** —— 计划件 ``meta.commission_bp`` / ``meta.slippage_bp`` vs
     执行卡 ``constraints.commission_rate`` / ``constraints.slippage_rate``（**小数** ⇒ ×10000 换算成 bp）
     ⇒ 落成 §三 **四行** ＋ 一行 ``费率口径分歧``。
     🔴 复发点：两套费率**确实不一致**（实测 2026-11-18 / 2026-11-19 两个快照都是）
     ⇒ 只写一套 ⇒ 复盘算不出真实摩擦成本。
  ③ **现金流双口径** —— 计划件 ``cash_flow`` vs 执行卡 ``cash_flow``
     ⇒ 落成 §二 一张**对照表** ＋ 一行 ``cash_end 差额（执行卡 − 计划件）``。
     🔴 复发点：同一天、同一张计划，**两套 cash_flow** 期末现金可能不等
     （实测 2026-11-18 差 **−7.22 元**；2026-11-19 无交易 ⇒ 差 0.00）⇒ 只留一套 ⇒ 对不上账。

【本文件钉死的 7 件事】
  P1 ``w_holdings`` 披露（头部行 ＋ §三 行，**两处**都要在）
  P2 执行卡 ``constraints`` 的佣金 bp ＋ 滑点 bp 两行 ＋ ``费率口径分歧`` 行
  P3 「执行卡口径的现金流」对照表 ＋ ``cash_end 差额`` 行
  P4 ``w_holdings.status == "stale"`` ⇒ **滞后天数必须显式写出**（不许只写一个 ``stale``）
  P5 产物**没有** ``constraints`` ⇒ 该行写「缺」（不许整行省略、不许写 0）
  P6 产物**没有**执行卡 ``cash_flow`` ⇒ ``cash_end 差额`` 写「缺」（不许写 0）
  P7 真实工程的两份记录（``20261118.md`` / ``20261119.md``）⇒ **一件都不许缺**（不许 skip）

【本文件的写法】
* 全部断言都建立在一个**探测器** ``_missing_disclosures(md_text)`` 之上
  （返回「这段记录里缺了哪些必备披露」⇒ 空列表 = 一件不缺）。
* 照抄 ``manu/etf_rotation/tests/test_daily_record_ledger.py`` 的既有风格：
  被测模块用 ``importlib`` 按**路径**加载；🔴 **不许**用固定的 ``parents[N]`` 定位
  （本文件有**两个镜像位置**、必须**逐字一致**）⇒ 改成**向上搜索**。
* **变异反证**：M1 / M2 / M3 三段，改的是 **tmp 目录里的源码副本**，**绝不碰**仓库里的工具文件。
"""

from __future__ import annotations

import importlib.util
import json
import re
import sys
import uuid
from pathlib import Path

import pytest


def _find_tool() -> Path:
    """向上找 ``manu/tools/append_daily_record.py``。

    🔴 本文件有**两个镜像位置**（``manu/etf_rotation/tests/`` 与 ``manu/tests/``，
    两者必须逐字一致）⇒ **不许**用固定的 ``parents[N]`` 定位 ⇒ 改成向上搜索
    ⇒ 两侧都能真正跑到被测工具（否则镜像侧直接 FileNotFoundError ⇒ 收集错误）。
    """
    for parent in Path(__file__).resolve().parents:
        candidate = parent / "manu" / "tools" / "append_daily_record.py"
        if candidate.is_file():
            return candidate
    raise FileNotFoundError("找不到 manu/tools/append_daily_record.py")


#: 被测工具的路径（``manu/tools/append_daily_record.py``）
TOOL_PATH = _find_tool()

#: 真实工程里的逐日记录目录（从**已搜到的工具路径**推 ⇒ 两个镜像位置都指同一处）
RECORDS_DIR = TOOL_PATH.parents[1] / "memory" / "records"

#: P7 要验的真实记录（**不许 skip、不许弱化**）
LIVE_RECORDS = ("20261118.md", "20261119.md")


def _load_tool():
    """按路径加载被测工具（``manu/tools/`` 不在 ``sys.path`` 默认位置）。"""
    spec = importlib.util.spec_from_file_location("append_daily_record_under_test", TOOL_PATH)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


TOOL = _load_tool()


# =============================================================================
# 探测器：这段逐日记录里**缺了哪些必备披露**（空列表 = 一件不缺）
# =============================================================================

#: ① 成分股快照 W 的两个落点（头部行 / §三 行）
_W_HEADER = "- 🔴 成分股快照 W（w_holdings）："
_W_ROW = "| 成分股快照 W（w_holdings） |"
#: ② 费率口径（执行卡 constraints 两行 ＋ 分歧行）
_FEE_COMMISSION_CARD = "| 佣金 bp（执行卡 constraints） |"
_FEE_SLIPPAGE_CARD = "| 滑点 bp（执行卡 constraints） |"
_FEE_DIVERGENCE = "| 费率口径分歧 |"
#: ③ 现金流双口径（对照表 ＋ 差额行）
_CASH_TABLE_HEADER = "| 项 | 计划件 | 执行卡 |"
_CASH_DIFF_ROW = "| cash_end 差额（执行卡 − 计划件） |"
_CASH_TABLE_KEYS = ("buy_amount", "buy_fees", "slippage", "cash_end")


def _missing_disclosures(md_text: str, *, stale_days: int | None = None) -> list[str]:
    """**探测器**：这段逐日记录里缺了哪些必备披露（空列表 = 一件不缺）。

    🔴 三个编号 = 三类披露；「头部行 / §三 行」分别单列 ⇒ 少一处就报一条。
    ``stale_days`` 给了 ⇒ 额外要求 md 里出现**那个具体的天数**（P4）；
    没给 ⇒ 只要求 md 里出现形如「滞后 N 天」的整数天数。
    """
    missing: list[str] = []

    # ---- ① 成分股快照 W 的陈旧度（头部行 ＋ §三 行，两处都要在）----
    if _W_HEADER not in md_text:
        missing.append("① w_holdings 头部披露行")
    if _W_ROW not in md_text:
        missing.append("① w_holdings §三表格行")

    # ---- ② 费率口径（执行卡 constraints 两行 ＋ 分歧行）----
    if _FEE_COMMISSION_CARD not in md_text:
        missing.append("② 执行卡 constraints 佣金 bp 行")
    if _FEE_SLIPPAGE_CARD not in md_text:
        missing.append("② 执行卡 constraints 滑点 bp 行")
    if _FEE_DIVERGENCE not in md_text:
        missing.append("② 费率口径分歧 行")

    # ---- ③ 现金流双口径（对照表 ＋ cash_end 差额行）----
    if _CASH_TABLE_HEADER not in md_text:
        missing.append("③ 执行卡口径现金流对照表")
    for key in _CASH_TABLE_KEYS:
        # 🔴 对照表行是**三列**（项 / 计划件 / 执行卡）⇒ 与 §二 计划件那张**两列**表区分开
        if not re.search(rf"^\|\s*{re.escape(key)}\s*\|[^|]*\|[^|]*\|", md_text, re.MULTILINE):
            missing.append(f"③ 执行卡口径现金流对照表 {key} 行")
    if _CASH_DIFF_ROW not in md_text:
        missing.append("③ cash_end 差额 行")

    # ---- ④ stale ⇒ 滞后天数必须**显式写出**（不许只写一个 stale 就完事）----
    if "stale" in md_text:
        if stale_days is not None:
            if f"滞后 {stale_days} 天" not in md_text:
                missing.append(f"④ w_holdings 滞后天数（{stale_days} 天）")
        elif not re.search(r"滞后\s*\d+\s*天", md_text):
            missing.append("④ w_holdings 滞后天数")

    return missing


# =============================================================================
# 夹具：造一个**最小可读**的快照目录（5 个 JSON；不依赖服务器产物）
# =============================================================================

# ---- 夹具里**真实写进产物**的值（断言全部由这些常量推导 ⇒ 不许编数字）----
PLAN_COMMISSION_BP = 1.5
PLAN_SLIPPAGE_BP = 5
CARD_COMMISSION_RATE = 0.0003        # ⇒ 3 bp
CARD_SLIPPAGE_RATE = 0.0001          # ⇒ 1 bp
PLAN_BUY_AMOUNT = 14145.8
PLAN_BUY_FEES = 9.19477
PLAN_CASH_END = 9212.37523
CARD_BUY_AMOUNT = 14145.8
CARD_BUY_FEES = 15.0
CARD_SLIPPAGE = 1.41458
CARD_CASH_END = 9205.15542
W_MAX_FETCH_DATE = "2026-11-04"
W_STALE_DAYS = 14
W_N_CODES = 279

# ---- 由上面的值**推导**出的期望（⇒ 期望值与夹具值天然一致，不许各写各的）----
CARD_COMMISSION_BP = round(CARD_COMMISSION_RATE * 10000, 6)      # 3.0
CARD_SLIPPAGE_BP = round(CARD_SLIPPAGE_RATE * 10000, 6)          # 1.0
EXPECTED_COMMISSION_CELL = f"{CARD_COMMISSION_BP:g}"              # "3"
EXPECTED_SLIPPAGE_CELL = f"{CARD_SLIPPAGE_BP:g}"                  # "1"
EXPECTED_CASH_END_DIFF = f"{CARD_CASH_END - PLAN_CASH_END:+.2f}"  # "-7.22"
EXPECTED_FEE_DIVERGENCE = (
    f"佣金：计划件 meta {PLAN_COMMISSION_BP:g} bp vs 执行卡 constraints {CARD_COMMISSION_BP} bp；"
    f"滑点：计划件 meta {PLAN_SLIPPAGE_BP:g} bp vs 执行卡 constraints {CARD_SLIPPAGE_BP} bp"
)


def _num_text(value) -> str:
    """产物里的数字 ⇒ 记录里的写法（整数值的 float **不带小数尾巴**：15.0 ⇒ ``15``）。"""
    if isinstance(value, float) and value.is_integer():
        return str(int(value))
    return str(value)


def _write_snapshot(
    tmp_path: Path,
    *,
    run_id: str = "20261118_163556",
    asof: str = "2026-11-18",
    with_constraints: bool = True,
    with_card_cash_flow: bool = True,
) -> Path:
    """造一个**最小可读**的快照目录（5 个 JSON ＋ 1 张执行卡）。

    三种情形：
      ① 默认 ⇒ 正常（含 ``w_holdings`` stale ＋ ``constraints`` ＋ 执行卡 ``cash_flow``）
      ② ``with_constraints=False`` ⇒ 产物**没有** ``constraints``
      ③ ``with_card_cash_flow=False`` ⇒ 产物**没有**执行卡 ``cash_flow``
    """
    snapshot_dir = tmp_path / f"snap_{run_id}"
    snapshot_dir.mkdir(parents=True, exist_ok=True)

    plan = {
        "meta": {
            "asof": asof,
            "total_assets": 30035.57,
            "cash": 23367.37,
            "target_n_holdings": 5,
            "hold_buffer": 2,
            "commission_bp": PLAN_COMMISSION_BP,
            "slippage_bp": PLAN_SLIPPAGE_BP,
        },
        "ta_screening": {
            "equity_cap": 0.8,
            "equity_cap_effective": 0.8,
            "equity_cap_breach": 0.0,
            "rejected": [],
            "substituted": [],
        },
        "min_amount_gate": {
            "enabled": True,
            "thresholds": {"存量微调": 2800.0, "新开仓": 3800.0, "清仓止损": 0.0},
            "skipped_count": 0,
            "deadlock_risk": False,
            "risk_overrides": [],
        },
        "cash_flow": {
            "cash_begin": 23367.37,
            "sell_proceeds": 0,
            "sell_fees": 0,
            "buy_amount": PLAN_BUY_AMOUNT,
            "buy_fees": PLAN_BUY_FEES,
            "cash_end": PLAN_CASH_END,
            "available_before_buy": 23367.37,
            "deficit": 0,
            "surplus": PLAN_CASH_END,
            "headroom": PLAN_CASH_END,
            "scaled_down": [],
            "feasible": True,
            "invested_weight_planned": 0.70201,
        },
        "actions": [
            {
                "code": "512800",
                "name": "银行ETF华宝",
                "action": "维持",
                "current_shares": 7700,
                "current_weight": 0.230859,
                "target_weight": 0.230859,
                "target_weight_effective": 0.230859,
                "target_amount": 7037.8,
                "target_shares": 7700,
                "est_amount": 0,
                "reason": "TA 一票否决 ⇒ 保持现仓不卖出",
            }
        ],
    }
    (snapshot_dir / "final_rebalance_plan.json").write_text(
        json.dumps(plan, ensure_ascii=False), encoding="utf-8"
    )

    manifest = {
        "asof": asof,
        "run_id": run_id,
        "created_at": f"{asof}T16:35:56",
        "finalized_at": f"{asof}T16:40:12",
        "stages": [
            {
                "name": "w_holdings",
                "status": "stale",
                "max_fetch_date": W_MAX_FETCH_DATE,
                "stale_days": W_STALE_DAYS,
                "n_codes": W_N_CODES,
            },
            {"name": "stage1_increment", "status": "ok"},
            {"name": "analyze", "status": "ok"},
        ],
        "skipped": [],
    }
    (snapshot_dir / "snapshot_manifest.json").write_text(
        json.dumps(manifest, ensure_ascii=False), encoding="utf-8"
    )

    (snapshot_dir / "v2_frozen_params.json").write_text(
        json.dumps({"params": {"alpha_weight": 1.0, "top_k": 5}}, ensure_ascii=False),
        encoding="utf-8",
    )

    (snapshot_dir / "account_context.json").write_text(
        json.dumps(
            {
                "snapshot_date": "2026-11-05",
                "total_assets": 30035.57,
                "cash": 23367.37,
                "market_value": 6668.2,
                "positions": [
                    {
                        "code": "512800",
                        "name": "银行ETF华宝",
                        "shares": 7700,
                        "price": 0.866,
                        "market_value": 6668.2,
                        "cost": 6440.5,
                        "pnl": 227.7,
                        "pnl_pct": 0.03535,
                    }
                ],
            },
            ensure_ascii=False,
        ),
        encoding="utf-8",
    )

    card = {
        "asof": asof,
        "run_id": run_id,
        "live_trade": False,
        "order_enabled": False,
        "execution_basis": "plan_only（节点日重算未完成）",
        "orders": [],
        "warnings": [],
    }
    if with_constraints:
        card["constraints"] = {
            "commission_rate": CARD_COMMISSION_RATE,
            "slippage_rate": CARD_SLIPPAGE_RATE,
        }
    if with_card_cash_flow:
        card["cash_flow"] = {
            "buy_amount": CARD_BUY_AMOUNT,
            "buy_fees": CARD_BUY_FEES,
            "slippage": CARD_SLIPPAGE,
            "cash_end": CARD_CASH_END,
        }
    (snapshot_dir / f"execution_card_{run_id}.json").write_text(
        json.dumps(card, ensure_ascii=False), encoding="utf-8"
    )
    return snapshot_dir


def _render(snapshot_dir: Path) -> str:
    """用被测工具渲染出当日人读记录（**只读产物**，不落盘）。"""
    return TOOL.render_markdown(TOOL.collect(snapshot_dir))


def _load_mutant(tmp_path: Path, source: str, tag: str):
    """把**变异后的源码副本**加载成模块（🔴 绝不碰仓库里的工具文件）。

    🔴 模块名带**唯一后缀** ⇒ 不与 ``sys.modules`` 里的缓存串味；
    🔴 **先** ``sys.modules[spec.name] = module`` **再** ``exec_module``
    （被测模块用了 ``@dataclass`` ＋ ``from __future__ import annotations``
    ⇒ 不注册会 AttributeError）。
    """
    module_name = f"append_daily_record_mutant_{tag}_{uuid.uuid4().hex}"
    mutant_path = tmp_path / f"mutant_{tag}.py"
    mutant_path.write_text(source, encoding="utf-8")
    spec = importlib.util.spec_from_file_location(module_name, mutant_path)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


# ---- 三段变异的**目标片段**（必须与工具源码逐字一致 ⇒ 下面 assert 唯一性）----
_M1_TARGET = """    lines.append(f"- 🔴 成分股快照 W（w_holdings）：{info['w_blob']}")"""

_M2_TARGET = """    lines.append(f"| 佣金 bp（执行卡 constraints） | {_fmt(info['card_commission_bp'])} |")
    lines.append(f"| 滑点 bp（执行卡 constraints） | {_fmt(info['card_slippage_bp'])} |")"""

_M3_TARGET = """    lines.append("| 项 | 计划件 | 执行卡 |")
    lines.append("| --- | ---: | ---: |")
    for key in ("buy_amount", "buy_fees", "slippage", "cash_end"):
        lines.append(
            f"| {key} | {_fmt_value(cash_flow.get(key))} | {_fmt_value(card_cash_flow.get(key))} |"
        )
    lines.append(
        f"| cash_end 差额（执行卡 − 计划件） | — | {_fmt_money(info['cash_end_diff'])} |"
    )"""


def _mutated_source(target: str, replacement: str, tag: str) -> str:
    """把工具源码里的 ``target`` 整段换成 ``replacement``（**只在副本上做**）。"""
    source = TOOL_PATH.read_text(encoding="utf-8")
    assert source.count(target) == 1, f"{tag} 目标片段在源码里必须**恰好出现一次**"
    mutated = source.replace(target, replacement)
    assert mutated != source, f"{tag} 变异必须真的改到东西"
    return mutated


# =============================================================================
# P1 ~ P4：三类披露必须在位
# =============================================================================


def test_P1_w_holdings披露_头部行与三段行两处都在(tmp_path: Path) -> None:
    """🔴 P1：``w_holdings`` 披露**两处**都要在（头部行 ＋ §三 行）。

    🔴 复发点：``w_holdings`` 状态是 ``stale`` ⇒ **不属于** ``{failed, error, degraded}``
    ⇒ 「失败/跳过环节」那行**永远不会**提到它 ⇒ 少一处就静默丢失了。
    """
    snapshot_dir = _write_snapshot(tmp_path)
    md = _render(snapshot_dir)

    assert _W_HEADER in md
    assert _W_ROW in md
    # 头部行与 §三 行的内容**必须一致**（同一个 ``w_blob``）
    header_line = next(line for line in md.splitlines() if line.startswith(_W_HEADER))
    table_line = next(line for line in md.splitlines() if line.startswith(_W_ROW))
    assert header_line == f"{_W_HEADER}stale｜快照日 {W_MAX_FETCH_DATE}｜滞后 {W_STALE_DAYS} 天｜{W_N_CODES} 只"
    assert f"stale｜快照日 {W_MAX_FETCH_DATE}" in table_line

    missing = _missing_disclosures(md)
    assert [item for item in missing if item.startswith("①")] == []


def test_P2_执行卡constraints费率两行与费率口径分歧行都在(tmp_path: Path) -> None:
    """🔴 P2：执行卡 ``constraints`` 的佣金 bp ＋ 滑点 bp 两行 ＋ ``费率口径分歧`` 行。

    🔴 复发点：计划件 1.5bp/5bp 与执行卡 constraints 3bp/1bp **确实不一致**
    ⇒ 只写一套 ⇒ 复盘算不出真实摩擦成本。
    """
    snapshot_dir = _write_snapshot(tmp_path)
    md = _render(snapshot_dir)

    assert f"| 佣金 bp（执行卡 constraints） | {EXPECTED_COMMISSION_CELL} |" in md
    assert f"| 滑点 bp（执行卡 constraints） | {EXPECTED_SLIPPAGE_CELL} |" in md
    assert f"| 费率口径分歧 | {EXPECTED_FEE_DIVERGENCE} |" in md
    # 计划件那套也必须留着（两套口径**都要留痕**）
    assert f"| 佣金 bp（计划件 meta） | {PLAN_COMMISSION_BP:g} |" in md
    assert f"| 滑点 bp（计划件 meta） | {PLAN_SLIPPAGE_BP:g} |" in md

    missing = _missing_disclosures(md)
    assert [item for item in missing if item.startswith("②")] == []


def test_P3_执行卡口径现金流对照表与cash_end差额行都在(tmp_path: Path) -> None:
    """🔴 P3：「执行卡口径的现金流」对照表 ＋ ``cash_end 差额`` 行都要在。

    🔴 复发点：同日两张 ``cash_flow`` 期末现金可能不等（实测 11-18 差 −7.22 元）
    ⇒ 只留一套 ⇒ 复盘对不上账。
    """
    snapshot_dir = _write_snapshot(tmp_path)
    md = _render(snapshot_dir)

    assert _CASH_TABLE_HEADER in md
    # 🔴 对照表**两列**的值必须分别来自计划件 / 执行卡（不许只留一套）
    for key, plan_text, card_text in (
        ("buy_amount", _num_text(PLAN_BUY_AMOUNT), _num_text(CARD_BUY_AMOUNT)),
        ("buy_fees", _num_text(PLAN_BUY_FEES), _num_text(CARD_BUY_FEES)),
        ("slippage", "缺", _num_text(CARD_SLIPPAGE)),
        ("cash_end", _num_text(PLAN_CASH_END), _num_text(CARD_CASH_END)),
    ):
        assert f"| {key} | {plan_text} | {card_text} |" in md
    assert f"| cash_end 差额（执行卡 − 计划件） | — | {EXPECTED_CASH_END_DIFF} |" in md

    missing = _missing_disclosures(md)
    assert [item for item in missing if item.startswith("③")] == []


def test_P4_stale必须显式写出滞后天数(tmp_path: Path) -> None:
    """🔴 P4：``w_holdings.status == "stale"`` ⇒ **滞后天数必须显式写出**。

    🔴 不许只写一个 ``stale`` 就完事（读者根本不知道 W 落后了多久 ⇒ 风险被稀释）。
    """
    snapshot_dir = _write_snapshot(tmp_path)
    md = _render(snapshot_dir)

    assert f"滞后 {W_STALE_DAYS} 天" in md
    assert _missing_disclosures(md, stale_days=W_STALE_DAYS) == []

    # 🔴 探测器自己有牙：把「滞后 N 天」抹掉（只剩一个 stale）⇒ 必须报缺 ④
    stripped = md.replace(f"｜滞后 {W_STALE_DAYS} 天", "")
    w_lines = [line for line in stripped.splitlines() if "w_holdings" in line]
    assert w_lines, "头部行与 §三 行都该还在"
    assert not any("滞后" in line for line in w_lines), "只剩一个 stale 不算披露"
    missing = _missing_disclosures(stripped, stale_days=W_STALE_DAYS)
    assert [item for item in missing if item.startswith("④")] != []


# =============================================================================
# P5 / P6：产物里**没有** ⇒ 写「缺」（不许省略、不许写 0）
# =============================================================================


def test_P5_产物没有constraints_该行写缺_不许省略也不许写零(tmp_path: Path) -> None:
    """🔴 P5：产物**没有** ``constraints`` ⇒ 该行写「缺」。

    🔴 不许**整行省略**（读者分不清「没有」还是「漏了」）；
    🔴 不许写 0（把「没有费率」冒充成「费率是 0」⇒ 复盘会把摩擦成本算成 0）。
    """
    snapshot_dir = _write_snapshot(tmp_path, with_constraints=False)
    md = _render(snapshot_dir)

    assert f"| 佣金 bp（执行卡 constraints） | 缺 |" in md
    assert f"| 滑点 bp（执行卡 constraints） | 缺 |" in md
    # 不许整行省略 ⇒ 探针不许报缺 ②
    assert [item for item in _missing_disclosures(md) if item.startswith("②")] == []
    # 不许写 0
    assert "| 佣金 bp（执行卡 constraints） | 0 |" not in md
    assert "| 滑点 bp（执行卡 constraints） | 0 |" not in md


def test_P6_产物没有执行卡cash_flow_cash_end差额写缺_不许写零(tmp_path: Path) -> None:
    """🔴 P6：产物**没有**执行卡 ``cash_flow`` ⇒ ``cash_end 差额`` 写「缺」。

    🔴 不许写 0（0 会被读成「两套口径正好相等」⇒ 掩盖了「根本没拿到执行卡口径」）。
    """
    snapshot_dir = _write_snapshot(tmp_path, with_card_cash_flow=False)
    md = _render(snapshot_dir)

    assert f"| cash_end 差额（执行卡 − 计划件） | — | 缺 |" in md
    # 不许整行省略 ⇒ 探针不许报缺 ③
    assert [item for item in _missing_disclosures(md) if item.startswith("③")] == []
    # 不许写 0
    assert "| cash_end 差额（执行卡 − 计划件） | — | 0 |" not in md
    assert "| cash_end 差额（执行卡 − 计划件） | — | +0.00 |" not in md


# =============================================================================
# P7：真实工程里的两份记录 ⇒ 一件都不许缺（**不许 skip、不许弱化**）
# =============================================================================


@pytest.mark.parametrize("filename", LIVE_RECORDS)
def test_P7_真实工程两份记录_必备披露一件都不许缺(filename: str) -> None:
    """🔴 P7：真实工程的两份逐日记录 ⇒ ``_missing_disclosures()`` **必须为空**。

    🔴 **不许 skip、不许弱化**：文件不在 ⇒ 直接失败（不许 ``pytest.skip``）。
    """
    record_path = RECORDS_DIR / filename
    assert record_path.is_file(), f"真实逐日记录不见了：{record_path}"
    md = record_path.read_text(encoding="utf-8")

    # 🔴 stale ⇒ 滞后天数**必须显式写出**（天数从文件里读出来，不许硬编）
    match = re.search(r"滞后\s*(\d+)\s*天", md)
    assert match is not None, f"{filename}：stale 却没显式写出滞后天数"
    stale_days = int(match.group(1))

    missing = _missing_disclosures(md, stale_days=stale_days)
    assert missing == [], f"{filename} 缺了这些披露：{missing}"


# =============================================================================
# 变异反证：M1 / M2 / M3（改的是 **tmp 目录里的源码副本**，绝不碰仓库里的工具文件）
# =============================================================================


def test_变异M1_删掉w_holdings头部行_必须报缺一(tmp_path: Path) -> None:
    """🔴 变异反证 M1：删掉输出 ``w_holdings`` 头部行的那一行 ⇒ 必须报缺 ①。

    🔴 手法：改的是 **tmp 目录里的源码副本**，绝不碰主 agent 的源文件。
    """
    snapshot_dir = _write_snapshot(tmp_path)
    baseline = _missing_disclosures(_render(snapshot_dir))
    assert baseline == [], "对照组：未变异的记录必须一件不缺"

    mutated_src = _mutated_source(
        _M1_TARGET,
        "    pass  # M1 变异：删掉 w_holdings 头部披露行",
        "M1",
    )
    mutant = _load_mutant(tmp_path, mutated_src, "M1")
    md = mutant.render_markdown(mutant.collect(snapshot_dir))

    missing = _missing_disclosures(md)
    assert [item for item in missing if item.startswith("①")] != [], "M1 变异必须被探针抓到"
    assert _W_HEADER not in md


def test_变异M2_删掉执行卡constraints两行_必须报缺二(tmp_path: Path) -> None:
    """🔴 变异反证 M2：删掉输出执行卡 ``constraints`` 佣金/滑点那两行 ⇒ 必须报缺 ②。

    🔴 手法：改的是 **tmp 目录里的源码副本**，绝不碰主 agent 的源文件。
    """
    snapshot_dir = _write_snapshot(tmp_path)
    baseline = _missing_disclosures(_render(snapshot_dir))
    assert baseline == [], "对照组：未变异的记录必须一件不缺"

    mutated_src = _mutated_source(
        _M2_TARGET,
        "    pass  # M2 变异：删掉执行卡 constraints 的佣金 / 滑点两行",
        "M2",
    )
    mutant = _load_mutant(tmp_path, mutated_src, "M2")
    md = mutant.render_markdown(mutant.collect(snapshot_dir))

    missing = _missing_disclosures(md)
    assert [item for item in missing if item.startswith("②")] != [], "M2 变异必须被探针抓到"
    assert _FEE_COMMISSION_CARD not in md
    assert _FEE_SLIPPAGE_CARD not in md


def test_变异M3_删掉执行卡口径现金流对照表_必须报缺三(tmp_path: Path) -> None:
    """🔴 变异反证 M3：删掉「执行卡口径的现金流」对照表 ⇒ 必须报缺 ③。

    🔴 手法：改的是 **tmp 目录里的源码副本**，绝不碰主 agent 的源文件。
    """
    snapshot_dir = _write_snapshot(tmp_path)
    baseline = _missing_disclosures(_render(snapshot_dir))
    assert baseline == [], "对照组：未变异的记录必须一件不缺"

    mutated_src = _mutated_source(
        _M3_TARGET,
        "    pass  # M3 变异：删掉「执行卡口径的现金流」对照表",
        "M3",
    )
    mutant = _load_mutant(tmp_path, mutated_src, "M3")
    md = mutant.render_markdown(mutant.collect(snapshot_dir))

    missing = _missing_disclosures(md)
    assert [item for item in missing if item.startswith("③")] != [], "M3 变异必须被探针抓到"
    assert _CASH_TABLE_HEADER not in md
    assert _CASH_DIFF_ROW not in md
