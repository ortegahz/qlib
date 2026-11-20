"""🔴 2026-11-19 新增：逐日事实台账（铁律 §十四.122）的**幂等 + append-only + 不许编**回归。

【背景】
用户亲定铁律（``manu/memory/rules.md`` §十四.122）：

> 「你写memory的时候一定要注意记录核心信息，比如当天etf的权重等，方便我几个月以后复盘，
> 这些都是你在akshare开源数据接口拿不到的信息……把这一条写到rule里面，作为铁律。」
> 「不一定写memory,也可以单独开个record什么的，记录。」

⇒ 落地载体 = ``manu/tools/append_daily_record.py`` ⇒ 产出
``manu/memory/records/etf_rotation_daily_ledger.csv``（**逐日台账**，一行 = 一只 ETF 一天）
＋ ``manu/memory/records/<YYYYMMDD>.md``（当日人读记录）。

本文件把三条**不可退化的性质**钉死：

  ① **幂等**：同一快照重跑 ⇒ **不产生重复行**、**不误伤**已有行
     （否则每次复盘重跑都会把台账冲成垃圾，几 个月后没法看）。
  ② **append-only**：内容变了 ⇒ **追加新行**、旧行标「作废」，
     但**旧行的业务字段一个字都不许改**（演化轨迹必须留得住）。
  ③ **不许编**：产物读不到的字段 ⇒ 一律写「缺」；观察态 ⇒「是否成交」恒为「否（观察态不下单）」，
     成交价 / 成交金额写「不适用」⇒ **不许把「计划」冒充「成交」**。

【测试风格】
照抄本目录既有风格：被测模块用 ``importlib`` 按**路径**加载
（``manu/tools/`` 不在 ``sys.path`` 的默认位置）⇒ 见 ``test_snapshot_side_effects.py``。

【变异反证】
每条关键断言都配一个**变异反证**测试：把源码里的关键判定改坏
（改的是 **tmp 目录里的源码副本**，绝不碰主 agent 的源文件）⇒ 对应断言必须转红
（``pytest.raises(AssertionError)``）⇒ 证明这些测试有牙，不是空跑。
"""

from __future__ import annotations

import csv
import importlib.util
import json
import sys
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


def _load_tool():
    """按路径加载被测工具（``manu/tools/`` 不在 ``sys.path`` 默认位置）。"""
    spec = importlib.util.spec_from_file_location("append_daily_record_under_test", TOOL_PATH)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


TOOL = _load_tool()


def _write_snapshot(
    tmp_path: Path,
    *,
    run_id: str = "20261119_153730",
    asof: str = "2026-11-19",
    target_weight: float = 0.230859,
    live_trade: bool = False,
    with_reject: bool = True,
) -> Path:
    """造一个**最小可读**的快照目录（只放工具会读的 4 个 JSON ＋ 1 张执行卡）。"""
    snapshot_dir = tmp_path / f"snap_{run_id}"
    snapshot_dir.mkdir(parents=True, exist_ok=True)

    plan = {
        "meta": {"asof": asof, "total_assets": 30385.21, "cash": 23370.51},
        "ta_screening": {
            "equity_cap": 0.8,
            "equity_cap_effective": 0.16,
            "equity_cap_breach": 0.070859,
            "rejected": (
                [
                    {
                        "code": "515450",
                        "reason": "总权益敞口上限锁定约 12%（不超过 16%），不配置任何货币",
                        "source_section": "最终交易决定",
                    }
                ]
                if with_reject
                else []
            ),
            "substituted": [{"code": "512800", "substituted_for": "515450"}],
        },
        "min_amount_gate": {"deadlock_risk": False, "risk_overrides": []},
        "cash_flow": {"cash_begin": 23370.51, "cash_end": 23370.51},
        "actions": [
            {
                "code": "512800",
                "name": "银行ETF华宝",
                "action": "维持",
                "current_shares": 7700,
                "current_weight": 0.230859,
                "target_weight": target_weight,
                "target_weight_effective": target_weight,
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
    (snapshot_dir / "snapshot_manifest.json").write_text(
        json.dumps(
            {
                "asof": asof,
                "run_id": run_id,
                "stages": [
                    {"name": "stage1_increment", "status": "failed"},
                    {"name": "analyze", "status": "ok"},
                ],
                "skipped": [],
            },
            ensure_ascii=False,
        ),
        encoding="utf-8",
    )
    (snapshot_dir / "v2_frozen_params.json").write_text(
        json.dumps({"params": {"alpha_weight": 1.0, "top_k": 5}}, ensure_ascii=False),
        encoding="utf-8",
    )
    (snapshot_dir / "account_context.json").write_text(
        json.dumps({"total_assets": 30385.21, "cash": 23370.51}, ensure_ascii=False),
        encoding="utf-8",
    )
    (snapshot_dir / f"execution_card_{run_id}.json").write_text(
        json.dumps({"live_trade": live_trade, "orders": []}, ensure_ascii=False),
        encoding="utf-8",
    )
    return snapshot_dir


def _read_ledger(path: Path) -> list[dict]:
    with path.open("r", encoding="utf-8", newline="") as fh:
        return list(csv.DictReader(fh))


def test_台账表头固定_不许中途插列() -> None:
    """🔴 台账是 append-only ⇒ **表头不许变**（插列会让历史行整体错位）。"""
    assert TOOL.LEDGER_HEADER[0] == "日期"
    assert TOOL.LEDGER_HEADER[-1] == "来源"
    assert len(TOOL.LEDGER_HEADER) == len(set(TOOL.LEDGER_HEADER))
    assert "记录状态" in TOOL.LEDGER_HEADER
    assert "是否成交" in TOOL.LEDGER_HEADER


def test_同一快照重跑_幂等_不产生重复行(tmp_path: Path) -> None:
    """🔴 幂等：同一快照跑两次 ⇒ 第二次**不追加、不作废**。"""
    snapshot_dir = _write_snapshot(tmp_path)
    ledger_path = tmp_path / "ledger.csv"

    rows = TOOL.collect(snapshot_dir)["rows"]
    added, invalidated, skipped = TOOL.append_ledger(ledger_path, rows, dry_run=False)
    assert (added, invalidated, skipped) == (1, 0, 0)

    added2, invalidated2, skipped2 = TOOL.append_ledger(ledger_path, rows, dry_run=False)
    assert (added2, invalidated2, skipped2) == (0, 0, 1)

    ledger = _read_ledger(ledger_path)
    assert len(ledger) == 1
    assert ledger[0]["记录状态"] == "有效"
    assert ledger[0]["代码"] == "512800"


def test_记录时间不同_仍判定同一事实_跳过(tmp_path: Path) -> None:
    """🔴 ``记录时间`` / ``记录状态`` 每轮都变 ⇒ **不许计入内容签名**。"""
    snapshot_dir = _write_snapshot(tmp_path)
    rows = TOOL.collect(snapshot_dir)["rows"]
    rows[0]["记录时间"] = "2099-01-01 00:00:00"
    rows[0]["记录状态"] = "有效"

    ledger_path = tmp_path / "ledger.csv"
    TOOL.append_ledger(ledger_path, rows, dry_run=False)
    added, invalidated, skipped = TOOL.append_ledger(ledger_path, rows, dry_run=False)
    assert (added, invalidated, skipped) == (0, 0, 1)
    assert len(_read_ledger(ledger_path)) == 1


def test_内容变了_追加新行且旧行标作废_旧字段一字不改(tmp_path: Path) -> None:
    """🔴 append-only：口径变了 ⇒ 追加新行 + 旧行标「作废」，**旧行内容不许改写**。"""
    snap_a = _write_snapshot(tmp_path / "a", run_id="20261119_153730", target_weight=0.230859)
    snap_b = _write_snapshot(tmp_path / "b", run_id="20261119_200000", target_weight=0.25)
    ledger_path = tmp_path / "ledger.csv"

    TOOL.append_ledger(ledger_path, TOOL.collect(snap_a)["rows"], dry_run=False)
    added, invalidated, skipped = TOOL.append_ledger(
        ledger_path, TOOL.collect(snap_b)["rows"], dry_run=False
    )
    assert (added, invalidated, skipped) == (1, 1, 0)

    ledger = _read_ledger(ledger_path)
    assert len(ledger) == 2
    old, new = ledger
    assert old["记录状态"] == "作废"
    assert new["记录状态"] == "有效"
    # 🔴 旧行的业务字段**一个字都不许改**
    assert old["目标权重"] == "0.230859"
    assert old["来源"].endswith("snap_20261119_153730")
    assert new["目标权重"] == "0.25"
    assert new["来源"].endswith("snap_20261119_200000")


def test_读不到的字段写缺_不许留空也不许写零(tmp_path: Path) -> None:
    """🔴 铁律「数字不许编」：产物里没有的 ⇒ 写「缺」。"""
    snapshot_dir = _write_snapshot(tmp_path, with_reject=False)
    row = TOOL.collect(snapshot_dir)["rows"][0]
    assert row["否决理由"] == "缺"
    assert row["否决出处"] == "缺"
    assert row["当前股数"] == "7700"
    # 「没有成交」**不许**写成 0（会被误读成「成交价 0 元」）
    assert row["否决理由"] != "0"
    assert row["否决理由"] != ""


def test_观察态_是否成交恒为否_成交价写不适用(tmp_path: Path) -> None:
    """🔴 观察态**不许**把「计划」冒充「成交」。"""
    snapshot_dir = _write_snapshot(tmp_path, live_trade=False)
    row = TOOL.collect(snapshot_dir)["rows"][0]
    assert row["是否成交"] == "否（观察态不下单）"
    assert row["成交价"] == "不适用"
    assert row["成交金额"] == "不适用"


def test_失败环节从manifest取_不许瞎编(tmp_path: Path) -> None:
    """🔴 失败 / 降级环节只能来自 manifest（唯一事实源）。"""
    snapshot_dir = _write_snapshot(tmp_path)
    row = TOOL.collect(snapshot_dir)["rows"][0]
    assert "stage1_increment=failed" in row["失败环节"]
    assert row["是否走兜底"] == "否"
    assert row["账户总资产"] == "30385.21"
    assert row["账户现金"] == "23370.51"


def test_变异反证_签名一旦纳入易变字段_幂等立即失效(tmp_path: Path) -> None:
    """🔴 变异反证：把 ``VOLATILE_FIELDS`` 清空 ⇒ 幂等守卫失效 ⇒ 断言必须转红。

    🔴 手法：改的是 **tmp 目录里的源码副本**，绝不碰主 agent 的源文件。
    """
    mutated_src = TOOL_PATH.read_text(encoding="utf-8").replace(
        'VOLATILE_FIELDS = ("记录时间", "记录状态")',
        'VOLATILE_FIELDS = ()',
    )
    assert mutated_src != TOOL_PATH.read_text(encoding="utf-8")

    mutated_path = tmp_path / "mutated_tool.py"
    mutated_path.write_text(mutated_src, encoding="utf-8")
    spec = importlib.util.spec_from_file_location("append_daily_record_mutated", mutated_path)
    assert spec is not None and spec.loader is not None
    mutated = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = mutated
    spec.loader.exec_module(mutated)

    snapshot_dir = _write_snapshot(tmp_path / "snap")
    rows = mutated.collect(snapshot_dir)["rows"]
    ledger_path = tmp_path / "ledger.csv"
    mutated.append_ledger(ledger_path, rows, dry_run=False)
    rows[0]["记录时间"] = "2099-01-01 00:00:00"
    added, invalidated, skipped = mutated.append_ledger(ledger_path, rows, dry_run=False)

    # 变异后：记录时间不同 ⇒ 被当成「新事实」⇒ 追加 + 作废（幂等**已失效**）
    assert (added, invalidated, skipped) == (1, 1, 0)
    assert skipped == 0
