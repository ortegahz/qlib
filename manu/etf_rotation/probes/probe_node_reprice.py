#!/usr/bin/env python
"""节点日股数重算工具 · 回归探针（M-S3，2026-11-05 07:2x）。

【为什么要有这道闸】
M-S3 交付件 `reprice_node_day.py` 是「12-01 那天按当日收盘价重算整手股数」的落地件
（专题档 §45.4 第 5 步 (a) 路径）。它**只重算价格口径**，风险有三类：

* **静默用旧价冒充节点日价**：`load_prices` 取的是「≤ asof 的最后一根」⇒ 行情没到节点日时
  会拿旧价算出一份**看起来正常**的清单 ⇒ **假结果**；
* **口径漂移**：股数取整 / 费率 / 不可建仓 / 权重归现金 若与 `final_rebalance_plan.py` 不一致
  ⇒ 两份产���的股数对不上 ⇒ 用户不知道该信哪份；
* **越权**：覆盖既有交付件（⇒ sha1 链断）或改写 `data/account_snapshot.csv`
  （⇒ `account.py` 的「Σ市值+现金==总资产」对账炸掉）。

【口径】
* **必须带真负例**（§十四.36）：D1（节点日未覆盖）/ D2（日期非法）/ D3（计划件缺失）必须**真的报出来**。
* **语义断言**：工具**只重算股数/金额** ⇒ **目标权重必须逐条未变**（P9，最关键的一条）。

【副作用】**只读**：只写临时目录（跑完即删），**不写任何既有交付件**、不联网。

【用法】
    /home/huangzhe/anaconda3/envs/ql/bin/python \\
        manu/etf_rotation/probes/probe_node_reprice.py \\
        [--run-root <落点根>/runs]
"""
from __future__ import annotations

import argparse
import hashlib
import json
import shutil
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

import config as C  # noqa: E402

#: 临时目录（跑完即删；写在持久盘，不写 /tmp）
TMP_DIR = C.WORK_ROOT / "_probe_node_reprice_tmp"


class Checker:
    """极简断言收集器：记录 (id, 名称, 是否通过, 细节)。"""

    def __init__(self) -> None:
        self.items: list[tuple[str, str, bool, str]] = []

    def add(self, check_id: str, name: str, ok: bool, detail: str = "") -> None:
        """记一条断言结果。"""
        self.items.append((check_id, name, bool(ok), detail))

    @property
    def failed(self) -> list[tuple[str, str, bool, str]]:
        """失败的断言列表。"""
        return [it for it in self.items if not it[2]]


def _sha1(path: Path) -> str:
    """文件 sha1（读不动就返回空串；不编指纹）。"""
    try:
        return hashlib.sha1(path.read_bytes()).hexdigest()
    except OSError:
        return ""


def _snapshot(root: Path) -> dict[str, str]:
    """目录内「文件名 → sha1」快照（只收文件；子目录不展开）。"""
    out: dict[str, str] = {}
    if not root.exists():
        return out
    for p in sorted(root.iterdir()):
        if p.is_file():
            out[p.name] = _sha1(p)
    return out


#: 整手口径（LOT）的**唯一真源** = `final_rebalance_plan.LOT`（工具也从那儿 import）。
#: 🔴 2026-11-09 修：旧代码写 `getattr(C, "LOT_SIZE", 100)` ⇒ `config.py` **根本没有** LOT_SIZE
#:    ⇒ 恒为 fallback 100 ⇒ 上游一旦改 LOT，探针与工具口径**静默分叉**。
_LOT_CACHE: int | None = None


def _resolve_lot() -> int:
    """取整手份数：config.LOT_SIZE → final_rebalance_plan.LOT → 100。

    🔴 2026-11-09 修（critic 复核轮）：旧签名带一个**死参数** `ns_map`（**没有任何调用点传值**）
    ⇒ 「工具命名空间里的 LOT 优先」这条分支永远走不到 ⇒ 白留一个会误导人的入口。
    ⇒ 修法：删掉死参数与对应分支；整手真源仍是 `final_rebalance_plan.LOT`。
    """
    global _LOT_CACHE
    if _LOT_CACHE is not None:
        return _LOT_CACHE
    _cfg = getattr(C, "LOT_SIZE", None)
    if _cfg:
        _LOT_CACHE = int(_cfg)
        return _LOT_CACHE
    try:
        from final_rebalance_plan import LOT as _plan_lot  # noqa: PLC0415

        _LOT_CACHE = int(_plan_lot)
    except Exception:  # noqa: BLE001
        _LOT_CACHE = 100
    return _LOT_CACHE


def _is_unbuildable_slot(a: dict, lot: int) -> bool:
    """「一手买不起」的**唯一判定口径**（与被测工具 `reprice_node_day.py` 同口径）。

    口径：目标金额 > 0 ＋ 当前 0 股 ＋ 价格 > 0 ＋ 目标金额 < 一手金额。
    （工具侧还要求 `delta_shares == 0`，但那是上述条件的推论 ⇒ 不重复写。）
    """
    amt = float(a.get("target_amount") or 0.0)
    price = float(a.get("price") or 0.0)
    cur = int(a.get("current_shares") or 0)
    if amt <= 0 or price <= 0 or cur != 0:
        return False
    return amt < price * lot


def _unbuildable_violations(payload: dict) -> list[str]:
    """不可建仓不变量（M-S4 审计补测）。

    口径：目标金额 > 0、当前 0 股、且一手金额 > 目标金额 的席位
    => 整手口径下根本建不了仓 => 必须 action=不可建仓、target_shares=0、
    target_weight_effective=0、weight_to_cash=target_weight。

    这是 2026-11-04「看着像满仓、实际 40%」事故的回归网：
    没有它，删掉「权重归现金」三行后探针仍全绿。
    """
    lot = _resolve_lot()
    bad: list[str] = []
    for a in payload.get("actions") or []:
        if not _is_unbuildable_slot(a, lot):
            continue
        prob: list[str] = []
        if a.get("action") != "不可建仓":
            prob.append(f"action={a.get('action')!r}")
        if int(a.get("target_shares") or 0) != 0:
            prob.append(f"target_shares={a.get('target_shares')}")
        if abs(float(a.get("target_weight_effective") or 0.0)) > 1e-12:
            prob.append(f"effective={a.get('target_weight_effective')}")
        wc = float(a.get("weight_to_cash") or 0.0)
        tw = float(a.get("target_weight") or 0.0)
        if abs(wc - tw) > 1e-12:
            prob.append(f"weight_to_cash={wc} != target_weight={tw}")
        if prob:
            bad.append(f"{a.get('code')}({';'.join(prob)})")
    return bad


def _weight_identity_violation(payload: dict) -> list[str]:
    """权重恒等式（M-S4 审计补测）。"""
    bad: list[str] = []
    landing = payload.get("landing") or {}
    actions = payload.get("actions") or []
    eff_sum = sum(float(a.get("target_weight_effective") or 0.0) for a in actions)
    planned = float(landing.get("planned_equity_weight") or 0.0)
    target_cash = float(landing.get("target_cash_weight") or 0.0)
    if abs(eff_sum - planned) > 1e-9:
        bad.append(f"sum_effective={eff_sum:.9f} != planned={planned:.9f}")
    if abs(target_cash - max(0.0, 1.0 - planned)) > 1e-9:
        bad.append(f"target_cash={target_cash:.9f} != max(0,1-planned)={max(0.0, 1.0 - planned):.9f}")
    if planned <= 1.0 and abs(planned + target_cash - 1.0) > 1e-9:
        bad.append(f"planned+target_cash={planned + target_cash:.9f} != 1")
    return bad


def _covered_node_date(prices_path: Path) -> str | None:
    """取「行情已覆盖的最近一个交易日」⇒ 用它做正例节点日。"""
    import pandas as pd

    if not prices_path.exists():
        return None
    df = pd.read_csv(prices_path, usecols=["date"])
    v = pd.to_datetime(df["date"], errors="coerce").max()
    return None if pd.isna(v) else v.strftime("%Y-%m-%d")


def run_checks(run_root: Path) -> list[tuple[str, str, bool, str]]:
    """跑全部断言，返回 (id, 名称, 是否通过, 细节) 列表。"""
    checker = Checker()
    py = sys.executable
    tool = ROOT / "reprice_node_day.py"
    plan_path = run_root / "final_rebalance_plan.json"
    prices_path = Path(str(C.ETF_PRICES_CSV))
    src = tool.read_text(encoding="utf-8")

    # ============================================================ D 组：真负例
    # ---- D1（真负例）：节点日未被行情覆盖 ⇒ 必须硬拦 ----
    far_future = "2099-12-31"
    try:
        r1 = subprocess.run([py, str(tool), "--node-date", far_future,
                             "--out-dir", str(TMP_DIR)],
                            capture_output=True, text=True, timeout=300)
        out1 = (r1.stdout or "") + (r1.stderr or "")
        ok_d1 = r1.returncode != 0 and "覆盖不到节点日" in out1
        d_d1 = f"退出码={r1.returncode}（期望 ≠0）｜含「覆盖不到节点日」={'覆盖不到节点日' in out1}"
    except Exception as exc:  # noqa: BLE001
        ok_d1, d_d1 = False, f"子进程异常：{exc!r}"
    checker.add("D1", "真负例：节点日未被行情覆盖 ⇒ 必须硬拦（不许用旧价冒充）", ok_d1, d_d1)

    # ---- D2（真负例）：日期非法 ⇒ 必须报错 ----
    try:
        r2 = subprocess.run([py, str(tool), "--node-date", "不是日期",
                             "--out-dir", str(TMP_DIR)],
                            capture_output=True, text=True, timeout=300)
        out2 = (r2.stdout or "") + (r2.stderr or "")
        ok_d2 = r2.returncode != 0 and "节点日" in out2
        d_d2 = f"退出码={r2.returncode}（期望 ≠0）"
    except Exception as exc:  # noqa: BLE001
        ok_d2, d_d2 = False, f"子进程异常：{exc!r}"
    checker.add("D2", "真负例：节点日非法 ⇒ 必须报错（不编日期）", ok_d2, d_d2)

    # ---- D3（真负例）：计划件缺失 ⇒ 必须报错 ----
    try:
        r3 = subprocess.run([py, str(tool), "--node-date", "2026-11-04",
                             "--plan", "/nonexistent/plan.json",
                             "--out-dir", str(TMP_DIR)],
                            capture_output=True, text=True, timeout=300)
        out3 = (r3.stdout or "") + (r3.stderr or "")
        ok_d3 = r3.returncode != 0 and ("计划件" in out3)
        d_d3 = f"退出码={r3.returncode}（期望 ≠0）｜含「计划件」={'计划件' in out3}"
    except Exception as exc:  # noqa: BLE001
        ok_d3, d_d3 = False, f"子进程异常：{exc!r}"
    checker.add("D3", "真负例：计划件缺失 ⇒ 必须报错（不静默降级）", ok_d3, d_d3)

    # ============================================================ 正例：跑一次重算
    node_date = _covered_node_date(prices_path)
    if node_date is None:
        checker.add("P1", "重算工具正例跑通（节点日 = 行情最新日）", False, "读不到行情最新日")
        return checker.items

    # ============================================================ 合成夹具工装
    # 🔴 2026-11-09（Mode C v2 独立审计）修：**P10 原本是「空过」**——
    #    它直接拿生产计划当夹具，而本轮生产计划里**一个不可建仓席位都没有**
    #    （P10 明文输出「0 个不可建仓席位全部合规」）⇒ 断言覆盖 = 0 ⇒ 闸门名存实亡。
    #    ⇒ 这里造**合成夹具**：挑一个「0 股 + 正目标权重」的席位，把目标权重压到
    #      「一手买不起」⇒ **保证**存在不可建仓席位 ⇒ P10 / D4 都能在确定的
    #      不可建仓席位上验证守卫（**断言条数不变**，只加强判定与披露）。
    def _make_unbuildable_fixture(plan_obj: dict) -> tuple[dict, str | None]:
        """造出「不可建仓席位」⇒ (改写后的计划件, 夹具代码)。

        ⚠️ **会就地改写 plan_obj**（改的是 `target_weight`）。

        优先用计划里现成的「0 股 + 正目标权重」席位，把目标权重压到 1e-5
        ⇒ 目标金额 = 1e-5 × total_assets，只要 total_assets < price×LOT×1e5 就必然买不起；
        计划里**没有**这种席位时（例如纯减仓轮）**追加一个合成席位**（价格 1.0）
        ⇒ 夹具**不依赖生产计划恰好有新建仓** ⇒ 不会在无新建仓的轮次变成假警报。
        """
        _acts = plan_obj.setdefault("actions", [])
        for _a in _acts:
            if int(_a.get("current_shares") or 0) != 0:
                continue
            if float(_a.get("target_weight") or 0.0) <= 0:
                continue
            _a["target_weight"] = 0.00001
            return plan_obj, str(_a.get("code") or "")
        _acts.append({
            "code": "999998",
            "name": "SYNTH_UNBUILDABLE",
            "action": "买入",
            "target_weight": 0.00001,
            "current_shares": 0,
            "target_shares": 0,
            "price": 1.0,
            "price_date": node_date,
            "final_score": 1.0,
            "reason": "probe 合成夹具：不可建仓席位",
        })
        return plan_obj, "999998"

    def _count_unbuildable(payload: dict) -> int:
        """不可建仓席位计数（与 `_unbuildable_violations` **同一判定口径**）。"""
        _lot = _resolve_lot()
        return sum(1 for _a in payload.get("actions") or [] if _is_unbuildable_slot(_a, _lot))

    def _exec_tool(src_text: str, mod_name: str) -> dict:
        """把工具源码在内存里 exec 成一个命名空间（不落盘、不改任何文件）。"""
        _ns: dict = {"__name__": mod_name, "__file__": str(tool)}
        exec(compile(src_text, str(tool), "exec"), _ns)  # noqa: S102
        return _ns

    def _run_fixture(ns_map: dict) -> tuple[dict, str | None, list[str]]:
        """用给定源码命名空间跑一次合成夹具 ⇒ (结果, 夹具代码, 违例)。"""
        _p = ns_map["_load_plan"](plan_path)
        _p, _code = _make_unbuildable_fixture(_p)
        _prices = ns_map["load_prices"](str(prices_path), node_date)
        _meta = _p.get("meta") or {}
        _shares = {
            str(a.get("code") or "").zfill(6): int(a.get("current_shares") or 0)
            for a in (_p.get("actions") or [])
        }
        _res = ns_map["_recompute"](
            _p, _prices, node_date,
            float(_meta.get("total_assets") or 0.0),
            float(_meta.get("cash") or 0.0),
            _shares, ns_map["_fee_rate"](_p),
        )
        return _res, _code, _unbuildable_violations(_res)

    #: 原版源码命名空间（P10 与 D4 共用；exec 失败 ⇒ P10 明确判红，不静默跳过）
    orig_ns: dict | None = None
    orig_ns_err = ""
    try:
        orig_ns = _exec_tool(src, "_orig")
    except Exception as exc:  # noqa: BLE001
        orig_ns_err = f"原版源码 exec 异常：{exc!r}"

    if TMP_DIR.exists():
        shutil.rmtree(TMP_DIR, ignore_errors=True)
    TMP_DIR.mkdir(parents=True, exist_ok=True)
    # M-S4 审计补测：跑之前先给 run-root 拍「文件名 -> sha1」快照
    # => 跑完能实证工具没有覆盖既有交付件、也没有往 run-root 里塞新文件
    run_root_before = _snapshot(run_root)
    try:
        rp = subprocess.run([py, str(tool), "--node-date", node_date,
                             "--plan", str(plan_path),
                             "--prices", str(prices_path),
                             "--out-dir", str(TMP_DIR)],
                            capture_output=True, text=True, timeout=600)
        ok_run = rp.returncode == 0
        detail_run = f"节点日={node_date} 退出码={rp.returncode}"
        if not ok_run:
            detail_run += f"｜stderr 尾：{(rp.stderr or '')[-200:]}"
    except Exception as exc:  # noqa: BLE001
        ok_run, detail_run = False, f"子进程异常：{exc!r}"
    checker.add("P1", "重算工具正例跑通（节点日 = 行情最新日）", ok_run, detail_run)

    stem = f"node_reprice_{node_date.replace('-', '')}"
    jpath = TMP_DIR / f"{stem}.json"
    mpath = TMP_DIR / f"{stem}.md"
    payload = {}
    if jpath.exists():
        try:
            payload = json.loads(jpath.read_text(encoding="utf-8"))
        except Exception as exc:  # noqa: BLE001
            payload = {}
            checker.add("P2", "重算产物 JSON 可解析", False, f"解析失败：{exc!r}")
    if not payload:
        checker.add("P2", "重算产物 JSON 可解析", False, f"文件缺失或不可解析：{jpath}")
        return checker.items
    checker.add("P2", "重算产物 JSON 可解析", True, f"{jpath.name}（{len(payload.get('actions', []))} 条 action）")

    actions = payload.get("actions") or []
    cf = payload.get("cash_flow") or {}
    md_text = mpath.read_text(encoding="utf-8") if mpath.exists() else ""

    # ---- P3：整手取整（目标股数必须是 100 的整数倍）----
    bad_lot = [a["code"] for a in actions
                if int(a.get("target_shares") or 0) % int(C.LOT_SIZE if hasattr(C, "LOT_SIZE") else 100) != 0]
    checker.add("P3", "目标股数必须是整手（100 份的整数倍）", not bad_lot,
                f"不合规：{bad_lot}" if bad_lot else f"{len(actions)} 条全部合规")

    # ---- P4：整手向下取整 ⇒ 实际金额不得超目标金额 ----
    over = []
    for a in actions:
        amt = float(a.get("target_amount") or 0.0)
        val = int(a.get("target_shares") or 0) * float(a.get("price") or 0.0)
        if amt > 0 and val > amt + 1e-6:
            over.append(f"{a['code']}({val:.2f}>{amt:.2f})")
    checker.add("P4", "整手向下取整 ⇒ 实际金额不得超目标金额", not over,
                f"超额的：{over}" if over else f"{len(actions)} 条全部未超")

    # ---- P5：现金恒等式 ----
    cash_begin = float(cf.get("cash_begin") or 0.0)
    cash_end = float(cf.get("cash_end") or 0.0)
    expect = (cash_begin + float(cf.get("sell_proceeds") or 0.0)
              - float(cf.get("sell_fees") or 0.0)
              - float(cf.get("buy_amount") or 0.0)
              - float(cf.get("buy_fees") or 0.0))
    diff = abs(cash_end - expect)
    checker.add("P5", "现金恒等式：期末现金 = 期初 + 回款 − 卖出费 − 买入 − 买费", diff < 0.01,
                f"期末 {cash_end:.4f} vs 期望 {expect:.4f}｜差 {diff:.6f}")

    # ---- P6：价日期不得晚于节点日（不许用未来价）----
    late = [f"{a['code']}({a.get('price_date')})" for a in actions
            if str(a.get("price_date") or "") > node_date]
    checker.add("P6", "价格日期不得晚于节点日（不许用未来价）", not late,
                f"越界：{late}" if late else f"全部 ≤ {node_date}")

    # ---- P7：目标权重必须逐条未变（工具只重算股数，不改信号）----
    plan = {}
    if plan_path.exists():
        try:
            plan = json.loads(plan_path.read_text(encoding="utf-8"))
        except Exception:  # noqa: BLE001
            plan = {}
    plan_w = {str(a.get("code") or "").zfill(6): a.get("target_weight")
              for a in (plan.get("actions") or [])}
    changed = []
    for a in actions:
        code = str(a.get("code") or "")
        if code in plan_w:
            if abs(float(a.get("target_weight") or 0.0) - float(plan_w[code] or 0.0)) > 1e-12:
                changed.append(code)
    checker.add("P7", "🔴 目标权重必须逐条未变（工具只重算股数/金额，不改信号）",
                not changed,
                f"被改动的：{changed}" if changed else
                f"{len(actions)} 条目标权重与计划件逐条一致")

    # ---- P8：md 必须含两条关键披露 ----
    need = ("节点日收盘价 ≠ 执行日成交价", "不是新的信号")
    missing = [k for k in need if k not in md_text]
    checker.add("P8", "md 必须写明「节点日价≠执行日成交价」与「不是新的信号」", not missing,
                f"缺：{missing}" if missing else "两条披露齐备")

    # ---- P9：风险披露非空且含账户口径提示 ----
    risks = payload.get("risk_disclosure") or []
    checker.add("P9", "风险披露非空且含「账户口径可能已过期」", bool(risks)
                and any("账户口径" in r for r in risks),
                f"{len(risks)} 条")

    # ---- P10：不可建仓席位必须「权重归现金」（M-S4 审计补测）----
    bad_unbuild = _unbuildable_violations(payload)
    n_unbuild = _count_unbuildable(payload)
    # 🔴 2026-11-09 加固：生产侧「0 个不可建仓席位」⇒ P10 覆盖为 0 ⇒ **空过**。
    #    ⇒ 判定改成两段同时成立：① 生产计划零违例 ② **合成夹具**在原版源码下零违例
    #      （夹具保证真的存在一个不可建仓席位 ⇒ 这条断言**有牙**）。
    fixture_ok = False
    fixture_detail = orig_ns_err or "合成夹具未执行"
    if orig_ns is not None:
        try:
            f_res, f_code, f_bad = _run_fixture(orig_ns)
            f_n = _count_unbuildable(f_res)
            if f_code is None or f_n < 1:
                # 🔴 夹具**没造出来** ≠ 守卫失效 ⇒ 文案必须分开（否则值班人会去查被测代码）
                fixture_ok = False
                fixture_detail = (
                    f"夹具**未构造成功**（代码={f_code}，不可建仓席位 {f_n} 个）"
                    "⇒ 覆盖=0，**这不是守卫结论**（计划里既无「0 股 + 正目标权重」席位，"
                    "合成席位也未生效）"
                )
            else:
                fixture_ok = bool(not f_bad)
                fixture_detail = (
                    f"合成夹具 {f_code}：不可建仓席位 {f_n} 个｜原版源码违例 {len(f_bad)} 条（期望 0）"
                )
        except Exception as exc:  # noqa: BLE001
            fixture_ok = False
            fixture_detail = f"合成夹具执行异常：{exc!r}"
    prod_detail = (
        f"生产计划 {n_unbuild} 个不可建仓席位，违例 {len(bad_unbuild)}：{bad_unbuild}"
        if bad_unbuild else f"生产计划 {n_unbuild} 个不可建仓席位，违例 0 条"
    )
    checker.add("P10", "不可建仓席位：0 股 + 一手买不起 => 必须判不可建仓且权重归现金",
                (not bad_unbuild) and fixture_ok,
                f"{prod_detail}｜{fixture_detail}")

    # ---- P11：权重恒等式（计划权益 / 计划现金 / 落地权益）----
    bad_weight = _weight_identity_violation(payload)
    landing = payload.get("landing") or {}
    checker.add("P11", "权重恒等式：Σ有效权重=计划权益、计划现金=1-计划权益",
                not bad_weight,
                f"违例：{bad_weight}" if bad_weight else
                f"planned={landing.get('planned_equity_weight')}｜"
                f"target_cash={landing.get('target_cash_weight')}｜"
                f"realized={landing.get('realized_equity_weight')}")

    # ============================================================ S 组：静态回归断言
    # M-S4 审计订正：这一条原来是「if False else」死代码（条件表达式优先级问题）
    #   => 真正的检查分支从未执行，闸门名存实亡（实测：把输出名改成
    #      final_rebalance_plan 覆盖交付件，断言照样 PASS）。
    #   => 改成**运行实证**：(a) out-dir 里只有约定的两个文件；
    #      (b) run-root 里没有新增文件；(c) run-root 里既有文件 sha1 全部未变。
    run_root_after = _snapshot(run_root)
    added = sorted(set(run_root_after) - set(run_root_before))
    changed = sorted(k for k in set(run_root_before) & set(run_root_after)
                      if run_root_before[k] != run_root_after[k])
    tmp_files = sorted(p.name for p in TMP_DIR.iterdir()) if TMP_DIR.exists() else []
    expect_tmp = sorted([f"{stem}.json", f"{stem}.md"])
    s1_ok = (not added) and (not changed) and (tmp_files == expect_tmp)
    checker.add("S1", "写边界（运行实证）：只写 node_reprice_* 两个文件，不碰既有交付件",
                s1_ok,
                f"out-dir 文件={tmp_files}（期望 {expect_tmp}）｜"
                f"run-root 新增={added}｜run-root 被改={changed}")

    # 🔴 M-S3 实测修（§十四.36：断言写错 = 假阳性/假阴性）：
    #    首版用**整串 replace** 剥 help 文案 ⇒ 实际文本末尾是 `口径")` 而非 `口径"` ⇒ 没剥掉 ⇒ 误报；
    #    二版用正则剥 `help="..."` ⇒ 仍被**模块 docstring**里的两处说明命中 ⇒ 又误报。
    #    ⇒ 修法：**用 AST 找真正的写操作**（write_text / write_bytes / to_csv / open(写模式)）
    #    ⇒ 语义级判定，docstring 与注释天然不参与 ⇒ 不会误报，也不会漏报真写入。
    import ast as _ast

    _tree = _ast.parse(src)
    _writes: list[str] = []
    for _node in _ast.walk(_tree):
        if not isinstance(_node, _ast.Call):
            continue
        _fn = _node.func
        _name = (_fn.attr if isinstance(_fn, _ast.Attribute)
                 else (_fn.id if isinstance(_fn, _ast.Name) else ""))
        if _name in {"write_text", "write_bytes", "to_csv"}:
            _writes.append(_ast.get_source_segment(src, _node) or _name)
            continue
        if _name == "open":
            _mode = None
            if len(_node.args) >= 2 and isinstance(_node.args[1], _ast.Constant):
                _mode = _node.args[1].value
            for _kw in _node.keywords:
                if _kw.arg == "mode" and isinstance(_kw.value, _ast.Constant):
                    _mode = _kw.value.value
            if isinstance(_mode, str) and any(ch in _mode for ch in "wax+"):
                _writes.append(_ast.get_source_segment(src, _node) or "open(...)")
    _acct_writes = [w for w in _writes if "account" in w.lower()]
    checker.add("S2", "不许改写 data/account_snapshot.csv（AST 找真正的写操作）",
                not _acct_writes,
                f"涉及 account 的写操作：{_acct_writes}" if _acct_writes
                else f"写操作共 {len(_writes)} 处，均不涉及 account："
                     + "｜".join(w[:40] for w in _writes))

    checker.add("S3", "口径单一事实源：复用 final_rebalance_plan 的 LOT / _round_lot / load_prices / load_account",
                all(k in src for k in ("LOT,", "_round_lot,", "load_account,", "load_prices,")),
                f"LOT={('LOT,' in src)}｜_round_lot={('_round_lot,' in src)}｜"
                f"load_prices={('load_prices,' in src)}｜load_account={('load_account,' in src)}")

    checker.add("S4", "源码必须写明「节点日收盘价 ≠ 执行日成交价」这条披露",
                "节点日收盘价 ≠ 执行日成交价" in src, "披露文案在源码中")

    # ---- D4（真负例 · 变异测试）：删掉「权重归现金」两行后，P10 必须报错 ----
    #    M-S4 审计订正：原来的 16 项断言对「不可建仓 / 权重归现金」**零覆盖**
    #    （实测：删掉那三行，探针仍 16/16 PASS）。
    #    => 这里做**变异测试**：在内存里改坏源码再跑一遍，断言新检查会抓住它。
    # 🔴 2026-11-09 修（Mode C v2 独立审计后补）：旧实现**直接拿生产计划当夹具** ⇒
    #    本轮生产计划里**一个不可建仓席位都没有**（P10 实测「0 个不可建仓席位全部合规」）
    #    ⇒ 变异删掉的那两行**从未被触发** ⇒ 变异等价于**空操作**
    #    ⇒ P10 自然抓不到违例 ⇒ 负例**无牙**（探针把「守卫生效」误报成 FAIL）。
    #    ⇒ 修法（**加强**，断言条数不变）：
    #      ① **合成夹具**：从生产计划里挑一个「0 股 + 正目标权重」的席位，
    #         把目标权重压到「一手买不起」⇒ **保证**存在不可建仓席位；
    #      ② **成对对照**：原版源码跑同一夹具必须**零违例**、变异版必须**有违例**
    #         （一正一反 ⇒ 比原来单向变异更强，且**不再依赖生产计划恰好有**不可建仓席位）。
    mut_lines = [
        '            a["target_weight_effective"] = 0.0\n',
        '            a["weight_to_cash"] = a["target_weight"]\n',
    ]
    mut_src = src
    for line in mut_lines:
        mut_src = mut_src.replace(line, "")
    mut_applied = (mut_src != src)
    ok_d4 = False
    d4_detail = ""
    if not mut_applied:
        d4_detail = "变异没生效：源码里找不到待删的两行（=> 探针该跟着升级）"
    else:
        # ---- 对照组：原版源码 + 同一夹具 ⇒ 必须零违例（夹具本身合规）----
        try:
            ns0 = orig_ns if orig_ns is not None else _exec_tool(src, "_orig")
            o_res, o_code, o_bad = _run_fixture(ns0)
            o_n = _count_unbuildable(o_res)
        except Exception as exc:  # noqa: BLE001
            ok_d4 = False
            d4_detail = f"对照组（原版源码）执行异常：{exc!r}"
            ns0 = None
        if ns0 is not None:
            if o_code is None or o_n < 1:
                # 🔴 夹具没造出来 ⇒ **不是**守卫结论（文案必须与「守卫失效」分开）
                ok_d4 = False
                d4_detail = (
                    f"夹具**未构造成功**（代码={o_code}，原版不可建仓席位 {o_n} 个）"
                    "⇒ 覆盖=0，**这不是守卫结论**"
                )
            else:
                try:
                    # ---- 变异组：删掉「权重归现金」两行 ⇒ 必须有违例 ----
                    ns = _exec_tool(mut_src, "_mutant")
                    m_res, m_code, m_bad = _run_fixture(ns)
                    m_n = _count_unbuildable(m_res)
                    m_planned = (m_res.get("landing") or {}).get("planned_equity_weight")
                    ok_d4 = bool(
                        o_n >= 1 and not o_bad      # 夹具确实造出了不可建仓席位，且原版合规
                        and m_n >= 1 and m_bad      # 变异版同样有该席位，且 P10 抓到违例
                    )
                    d4_detail = (
                        f"夹具代码 {o_code}（不可建仓席位：原版 {o_n} 个 / 变异版 {m_n} 个）｜"
                        f"原版违例 {len(o_bad)} 条（期望 0）｜"
                        f"变异后 planned_equity_weight={m_planned}｜"
                        f"P10 抓到 {len(m_bad)} 条违例（期望 ≥1）：{m_bad[:3]}"
                    )
                except Exception as exc:  # noqa: BLE001
                    ok_d4 = False
                    d4_detail = f"变异组（变异源码）执行异常：{exc!r}"
    checker.add("D4", "真负例（变异）：删掉「权重归现金」=> P10 必须报错（闸门不许是摆设）",
                ok_d4, d4_detail)

    return checker.items


def main() -> int:
    """跑探针；退出码 0 = 全部断言通过，1 = 有断言失败。"""
    ap = argparse.ArgumentParser(description="节点日股数重算工具 · 回归探针")
    ap.add_argument("--run-root", default=str(C.ETF_RUN_ROOT))
    args = ap.parse_args()

    items = run_checks(Path(args.run_root))
    print("=" * 78)
    print("节点日股数重算工具 · 回归探针（M-S3）")
    print("=" * 78)
    for check_id, name, ok, detail in items:
        print(f"[{'PASS' if ok else 'FAIL'}] {check_id} {name}")
        print(f"        {detail}")
    failed = [it for it in items if not it[2]]
    shutil.rmtree(TMP_DIR, ignore_errors=True)
    print("-" * 78)
    print(f"合计 {len(items)} 项：PASS {len(items) - len(failed)} / FAIL {len(failed)}")
    if failed:
        print("❌ 节点日重算工具失效 ⇒ 12-01 当天的股数会算错或污染账户快照")
        return 1
    print("✅ 节点日重算工具全绿（含 4 个真负例）")
    return 0


if __name__ == "__main__":
    sys.exit(main())
