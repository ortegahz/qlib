"""🔴 V2 成分股 / 权重 **append-only 按日归档** 的回归测试。

【事故背景（为什么要钉这些）】
akshare 的 ``fund_etf_holdings_tx`` / ``fund_etf_pcf`` **都没有历史 PCF**
⇒ 历史成分变更**不可观测**；V1 的 ``data/fetch_etf_holdings.py`` 又把 ``fetch_date``
写死成「今天」且**每次覆盖**同一张 ``etf_holdings.csv`` ⇒ 本地只剩 **1 个**快照日。
所以唯一诚实的做法是**从今天起按日 append 归档**，并把「此前无历史快照」如实披露。

【钉死的性质】
  ① 同一 asof 重复 append ⇒ **抛错**，且原文件**逐字节未变**；
  ② 不同日期连续 append ⇒ 两个文件都在，``available_dates`` **升序**；
  ③ ``snapshot_for_date`` 取 **≤ date** 的**最近**归档日（跨日 / 跨空档都要对）；
  ④ 早于最早归档日 ⇒ **抛错**，且错误信息含「不可得 / 禁止倒推」；
  ⑤ 写盘**中途**失败 ⇒ **不留半截文件**（原子写）；
  ⑥ 每份快照都有伴随 ``meta.json``（审计：这条快照是什么时候抓的）；
  ⑦ 归档根必须落在允许落点 ``/mnt/data/siping/datasets/manu/`` 之内。

【变异反证】沿用本仓库既有写法：源码 → 内存改 → tmp_path → import 副本
⇒ 改坏关键判定 ⇒ 对应断言**必须转红**。⛔ **绝不落盘改真源码**。
"""

from __future__ import annotations

import importlib.util
import json
import sys
from pathlib import Path

import pandas as pd
import pytest

#: `manu/etf_rotation/v2/` 目录（``holdings_archive`` 用扁平导入）
_V2_DIR = Path(__file__).resolve().parents[1]
if str(_V2_DIR) not in sys.path:
    sys.path.insert(0, str(_V2_DIR))

import fetch_holdings_archive as CLI  # noqa: E402
import holdings_archive  # noqa: E402

#: 被测模块源码（**只读**）
HOLDINGS_ARCHIVE_SOURCE = _V2_DIR / "holdings_archive.py"


#: 🔴 夹具成分行（两只 ETF、两个来源）
_ROWS = [
    {"etf_code": "510300", "stock_code": "600000.SH", "weight_pct": 40.0, "source": "tx"},
    {"etf_code": "510300", "stock_code": "000001.SZ", "weight_pct": 20.0, "source": "tx"},
    {"etf_code": "510500", "stock_code": "600000.SH", "weight_pct": 30.0, "source": "pcf"},
]


def _rows() -> list[dict]:
    return [dict(r) for r in _ROWS]


def _mutated_module(tmp_path: Path, edits: list[tuple[str, str]], tag: str):
    """🔴 把源码**读出来 → 在内存里改 → 写到 tmp_path → import 副本**。"""
    text = HOLDINGS_ARCHIVE_SOURCE.read_text(encoding="utf-8")
    for old, new in edits:
        assert old in text, f"变异锚点不存在（源码已变）：{old!r}"
        text = text.replace(old, new)
    target = tmp_path / f"{tag}.py"
    target.write_text(text, encoding="utf-8")
    name = f"mutant_{tag}"
    spec = importlib.util.spec_from_file_location(name, target)
    module = importlib.util.module_from_spec(spec)
    sys.modules[name] = module
    spec.loader.exec_module(module)
    return module


# ----------------------------------------------------------------- 性质检查（真模块 / 变异体共用）
def _check_duplicate_append_rejected(module, root: Path) -> None:
    """性质①：同一 asof append 两次 ⇒ 抛错 + 原文件**逐字节未变**。"""
    module.append_snapshot(_rows(), "2026-11-17", root=root)
    path = root / "2026-11-17.csv"
    before = path.read_bytes()
    raised = False
    try:
        module.append_snapshot(_rows(), "2026-11-17", root=root)
    except BaseException:
        raised = True
    assert raised, "🔴 同一 asof 重复 append 必须抛错（append-only，不许覆盖 / 不许追加重复行）"
    assert path.read_bytes() == before, "🔴 重复 append 之后原文件必须逐字节未变"


def _check_point_in_time_is_past_only(module, root: Path) -> None:
    """性质③：``snapshot_for_date`` 只许取 **≤ date** 的最近归档日。"""
    module.append_snapshot(_rows(), "2026-11-10", root=root)
    module.append_snapshot(_rows(), "2026-11-17", root=root)
    got = module.snapshot_for_date("2026-11-13", root=root)
    assert got == "2026-11-10", (
        f"🔴 2026-11-13 的 point-in-time 快照必须是 2026-11-10（≤ date 的最近日），实际 {got}"
    )


def _check_failed_write_leaves_nothing(module, root: Path, monkeypatch) -> None:
    """性质⑤：写盘**中途**失败 ⇒ 不留半截文件（原子写）。"""

    def _boom(self, path_or_buf=None, *args, **kwargs):
        # 写一半内容再炸 ⇒ 模拟「写到一半失败」
        with open(str(path_or_buf), "w", encoding="utf-8") as fh:
            fh.write("fetch_date,etf_code\n2026-11-17,510300\n")
        raise RuntimeError("🔴 注入的写盘中途失败")

    monkeypatch.setattr(pd.DataFrame, "to_csv", _boom)
    with pytest.raises(RuntimeError):
        module.append_snapshot(_rows(), "2026-11-17", root=root)
    assert not (root / "2026-11-17.csv").exists(), "🔴 写盘中途失败后不许留下半截快照文件"
    assert not list(root.glob("*.tmp")), "🔴 写盘中途失败后不许留下临时文件"
    assert not (root / "meta" / "2026-11-17.json").exists(), "🔴 没落盘的快照不许留下 meta.json"


# ----------------------------------------------------------------- ① append
class TestAppend:
    """append-only 落盘。"""

    def test_首次append落盘且列与行数都对(self, tmp_path: Path) -> None:
        result = holdings_archive.append_snapshot(_rows(), "2026-11-17", root=tmp_path)
        assert result.path == tmp_path / "2026-11-17.csv"
        assert result.rows_written == len(_ROWS)
        df = pd.read_csv(result.path, dtype=str)
        assert list(df.columns) == ["fetch_date", "etf_code", "stock_code", "weight_pct", "source"]
        assert len(df) == len(_ROWS)
        assert set(df["fetch_date"]) == {"2026-11-17"}

    def test_同日重复append必须抛错且原文件逐字节未变(self, tmp_path: Path) -> None:
        _check_duplicate_append_rejected(holdings_archive, tmp_path)

    def test_重复append抛的是SnapshotExistsError(self, tmp_path: Path) -> None:
        holdings_archive.append_snapshot(_rows(), "2026-11-17", root=tmp_path)
        with pytest.raises(holdings_archive.SnapshotExistsError):
            holdings_archive.append_snapshot(_rows(), "2026-11-17", root=tmp_path)

    def test_不同日期连续append两个文件都在且日期升序(self, tmp_path: Path) -> None:
        holdings_archive.append_snapshot(_rows(), "2026-11-17", root=tmp_path)
        holdings_archive.append_snapshot(_rows(), "2026-11-18", root=tmp_path)
        assert (tmp_path / "2026-11-17.csv").exists()
        assert (tmp_path / "2026-11-18.csv").exists()
        assert holdings_archive.available_dates(tmp_path) == ["2026-11-17", "2026-11-18"]

    def test_空快照不许落盘(self, tmp_path: Path) -> None:
        with pytest.raises(ValueError):
            holdings_archive.append_snapshot([], "2026-11-17", root=tmp_path)
        assert not (tmp_path / "2026-11-17.csv").exists()

    def test_缺列必须报错(self, tmp_path: Path) -> None:
        with pytest.raises(ValueError):
            holdings_archive.append_snapshot(
                [{"etf_code": "510300", "stock_code": "600000.SH"}], "2026-11-17", root=tmp_path
            )

    def test_日期格式非法必须报错(self, tmp_path: Path) -> None:
        with pytest.raises(ValueError):
            holdings_archive.append_snapshot(_rows(), "2026/11/17", root=tmp_path)


# ----------------------------------------------------------------- ② point-in-time
class TestPointInTime:
    """point-in-time 读取（只许向后看）。"""

    def test_取不晚于date的最近归档日(self, tmp_path: Path) -> None:
        holdings_archive.append_snapshot(_rows(), "2026-11-10", root=tmp_path)
        holdings_archive.append_snapshot(_rows(), "2026-11-17", root=tmp_path)
        assert holdings_archive.snapshot_for_date("2026-11-17", root=tmp_path) == "2026-11-17"
        assert holdings_archive.snapshot_for_date("2026-11-20", root=tmp_path) == "2026-11-17"

    def test_跨空档时取最近的那天(self, tmp_path: Path) -> None:
        holdings_archive.append_snapshot(_rows(), "2026-11-10", root=tmp_path)
        holdings_archive.append_snapshot(_rows(), "2026-11-17", root=tmp_path)
        # 11-13 在两次归档之间 ⇒ 取 11-10（不许取 11-17 那是未来）
        assert holdings_archive.snapshot_for_date("2026-11-13", root=tmp_path) == "2026-11-10"
        assert holdings_archive.snapshot_for_date("2026-11-16", root=tmp_path) == "2026-11-10"

    def test_早于最早归档日必须报错且提示不可得与禁止倒推(self, tmp_path: Path) -> None:
        holdings_archive.append_snapshot(_rows(), "2026-11-17", root=tmp_path)
        with pytest.raises(holdings_archive.SnapshotUnavailableError) as exc:
            holdings_archive.snapshot_for_date("2026-11-16", root=tmp_path)
        message = str(exc.value)
        assert "不可得" in message, message
        assert "禁止" in message and "倒推" in message, message

    def test_归档为空时读任何日期都报错(self, tmp_path: Path) -> None:
        with pytest.raises(holdings_archive.SnapshotUnavailableError) as exc:
            holdings_archive.snapshot_for_date("2026-11-16", root=tmp_path)
        assert "不可得" in str(exc.value)

    def test_load_point_in_time返回快照日与成分表(self, tmp_path: Path) -> None:
        holdings_archive.append_snapshot(_rows(), "2026-11-10", root=tmp_path)
        holdings_archive.append_snapshot(_rows(), "2026-11-17", root=tmp_path)
        day, df = holdings_archive.load_point_in_time("2026-11-13", root=tmp_path)
        assert day == "2026-11-10"
        assert list(df.columns) == ["fetch_date", "etf_code", "stock_code", "weight_pct", "source"]
        assert len(df) == len(_ROWS)

    def test_load_point_in_time与constituents口径可对接(self, tmp_path: Path) -> None:
        """归档表的 5 列 = ``constituents.load_holdings`` 要求的必需列 ⇒ 可直接喂给它。"""
        import constituents

        holdings_archive.append_snapshot(_rows(), "2026-11-17", root=tmp_path)
        path = holdings_archive.snapshot_path("2026-11-17", root=tmp_path)
        snapshot = constituents.load_holdings(path)
        assert snapshot.snapshot_date == "2026-11-17"
        assert snapshot.weights["510300"]["600000.SH"] == 40.0


# ----------------------------------------------------------------- ③ 原子性
class TestAtomicity:
    """写盘中途失败 ⇒ 不留半截文件。"""

    def test_写到一半失败不留半截文件(self, tmp_path: Path, monkeypatch) -> None:
        _check_failed_write_leaves_nothing(holdings_archive, tmp_path, monkeypatch)


# ----------------------------------------------------------------- ④ 审计
class TestAudit:
    """伴随 meta.json（这条快照是什么时候抓的）。"""

    def test_meta记录抓取日期行数ETF数来源与脚本版本(self, tmp_path: Path) -> None:
        holdings_archive.append_snapshot(_rows(), "2026-11-17", root=tmp_path)
        meta = holdings_archive.snapshot_meta("2026-11-17", root=tmp_path)
        assert meta["fetch_date"] == "2026-11-17"
        assert meta["n_rows"] == len(_ROWS)
        assert meta["n_etfs"] == 2
        assert meta["sources"] == {"tx": 2, "pcf": 1}
        assert meta["generated_at"]
        assert meta["archive_version"] == holdings_archive.ARCHIVE_VERSION

    def test_meta文件就在归档根下(self, tmp_path: Path) -> None:
        holdings_archive.append_snapshot(_rows(), "2026-11-17", root=tmp_path)
        meta_path = tmp_path / "meta" / "2026-11-17.json"
        assert meta_path.exists()
        assert json.loads(meta_path.read_text(encoding="utf-8"))["fetch_date"] == "2026-11-17"

    def test_历史缺口披露必须出现不可得与禁止倒推(self, tmp_path: Path) -> None:
        holdings_archive.append_snapshot(_rows(), "2026-11-17", root=tmp_path)
        note = holdings_archive.history_gap_note(tmp_path)
        assert "不可得" in note and "禁止" in note and "倒推" in note
        assert "2026-11-17" in note

    def test_归档根必须落在允许落点内(self) -> None:
        """🔴 归档根**不许**落在 ``/mnt/data`` 根或其它越界位置。"""
        root = str(holdings_archive.ARCHIVE_ROOT)
        assert root.startswith("/mnt/data/siping/datasets/manu/"), root

    def test_权重写出去再读回来必须逐位相同(self, tmp_path: Path) -> None:
        """🔴 pandas **默认**的 CSV 浮点解析不是 round-trip 的（实测差 1 ULP）
        ⇒ 归档必须用 ``float_precision="round_trip"`` 读，否则权重会漂。"""
        rows = [{"etf_code": "510300", "stock_code": "600000.SH",
                 "weight_pct": 2.2276509007003513, "source": "tx"}]
        holdings_archive.append_snapshot(rows, "2026-11-17", root=tmp_path)
        _, df = holdings_archive.load_point_in_time("2026-11-17", root=tmp_path)
        assert float(df["weight_pct"].iloc[0]) == 2.2276509007003513


# ----------------------------------------------------------------- ⑤ CLI
class TestCli:
    """``fetch_holdings_archive.py`` 的落盘纪律。"""

    def test_正常抓取落盘且列对(self, tmp_path: Path, monkeypatch) -> None:
        monkeypatch.setattr(CLI, "_pool_codes", lambda *a, **k: ["510300"])
        monkeypatch.setattr(
            CLI, "fetch_rows",
            lambda codes, deadline: (pd.DataFrame(_ROWS), {"n_codes": 1, "n_ok": 1, "n_no_holdings": 0,
                                                          "n_failed": 0, "no_holdings": [], "failed": [],
                                                          "elapsed_s": 0.1}),
        )
        rc = CLI.main(["--asof", "2026-11-17", "--root", str(tmp_path)])
        assert rc == 0
        df = pd.read_csv(tmp_path / "2026-11-17.csv", dtype=str)
        assert list(df.columns) == ["fetch_date", "etf_code", "stock_code", "weight_pct", "source"]
        assert len(df) == len(_ROWS)

    def test_dry_run只打印统计量不落盘(self, tmp_path: Path, monkeypatch, capsys) -> None:
        monkeypatch.setattr(CLI, "_pool_codes", lambda *a, **k: ["510300"])
        monkeypatch.setattr(
            CLI, "fetch_rows",
            lambda codes, deadline: (pd.DataFrame(_ROWS), {"n_codes": 1, "n_ok": 1, "n_no_holdings": 0,
                                                          "n_failed": 0, "no_holdings": [], "failed": [],
                                                          "elapsed_s": 0.1}),
        )
        rc = CLI.main(["--asof", "2026-11-17", "--root", str(tmp_path), "--dry-run"])
        assert rc == 0
        assert not (tmp_path / "2026-11-17.csv").exists()
        assert "成分行" in capsys.readouterr().out

    def test_抓取失败必须报错且不落盘(self, tmp_path: Path, monkeypatch) -> None:
        def _boom(codes, deadline):
            raise RuntimeError("🔴 限流")

        monkeypatch.setattr(CLI, "_pool_codes", lambda *a, **k: ["510300"])
        monkeypatch.setattr(CLI, "fetch_rows", _boom)
        rc = CLI.main(["--asof", "2026-11-17", "--root", str(tmp_path)])
        assert rc == 1
        assert not (tmp_path / "2026-11-17.csv").exists()

    def test_超时必须退出且不落盘(self, tmp_path: Path, monkeypatch) -> None:
        def _timeout(codes, deadline):
            raise CLI.FetchTimeout("🔴 抓取超时")

        monkeypatch.setattr(CLI, "_pool_codes", lambda *a, **k: ["510300"])
        monkeypatch.setattr(CLI, "fetch_rows", _timeout)
        rc = CLI.main(["--asof", "2026-11-17", "--root", str(tmp_path)])
        assert rc == 3
        assert not (tmp_path / "2026-11-17.csv").exists()

    def test_未来归档日必须拒绝(self, tmp_path: Path, monkeypatch) -> None:
        monkeypatch.setattr(CLI, "_pool_codes", lambda *a, **k: ["510300"])
        rc = CLI.main(["--asof", "2999-01-01", "--root", str(tmp_path)])
        assert rc == 2
        assert not list(tmp_path.glob("*.csv"))

    def test_同一天第二次抓取必须报错且不覆盖(self, tmp_path: Path, monkeypatch) -> None:
        monkeypatch.setattr(CLI, "_pool_codes", lambda *a, **k: ["510300"])
        monkeypatch.setattr(
            CLI, "fetch_rows",
            lambda codes, deadline: (pd.DataFrame(_ROWS), {"n_codes": 1, "n_ok": 1, "n_no_holdings": 0,
                                                          "n_failed": 0, "no_holdings": [], "failed": [],
                                                          "elapsed_s": 0.1}),
        )
        assert CLI.main(["--asof", "2026-11-17", "--root", str(tmp_path)]) == 0
        before = (tmp_path / "2026-11-17.csv").read_bytes()
        rc = CLI.main(["--asof", "2026-11-17", "--root", str(tmp_path)])
        assert rc == 4
        assert (tmp_path / "2026-11-17.csv").read_bytes() == before

    def test_默认总预算是60秒(self) -> None:
        assert CLI.DEFAULT_TIMEOUT_S == 60.0

    def test_预算明显不够时必须提前警告(self, tmp_path: Path, monkeypatch, capsys) -> None:
        """🔴 池子大、预算小 ⇒ **先喊一声**（否则白跑一轮、超时退出、今天的快照就废了）。"""
        monkeypatch.setattr(CLI, "_pool_codes", lambda *a, **k: ["510300", "510500", "159919"])

        def _timeout(codes, deadline):
            raise CLI.FetchTimeout("🔴 抓取超时")

        monkeypatch.setattr(CLI, "fetch_rows", _timeout)
        rc = CLI.main(["--asof", "2026-11-17", "--root", str(tmp_path), "--timeout", "1"])
        assert rc == 3
        assert not (tmp_path / "2026-11-17.csv").exists()
        assert "预算" in capsys.readouterr().err
# ----------------------------------------------------------------- ⑥ 变异反证
def test_变异反证_把重复append改成覆盖写_必须转红(tmp_path: Path) -> None:
    """把 append-only 的存在性检查去掉 ⇒ 同一 asof 会**覆盖** ⇒ 性质①必须转红。"""
    mutant = _mutated_module(
        tmp_path,
        [("    if path.exists():  # 🔴 append-only：同一 asof 只许写一次",
          "    if False:  # 🔴 变异：去掉 append-only 保护 ⇒ 变成覆盖写")],
        "ha_overwrite",
    )
    with pytest.raises(AssertionError):
        _check_duplicate_append_rejected(mutant, tmp_path / "mutant")


def test_变异反证_把point_in_time改成取不早于date的最近日_必须转红(tmp_path: Path) -> None:
    """把「≤ date 的最近日」改成「≥ date 的最近日」⇒ 会用**未来**快照 ⇒ 性质③必须转红。"""
    mutant = _mutated_module(
        tmp_path,
        [("    candidates = [d for d in days if d <= target]  # 🔴 point-in-time：只许取 ≤ date",
          "    candidates = [d for d in days if d >= target]  # 🔴 变异：改成取 ≥ date")],
        "ha_future_pit",
    )
    with pytest.raises(AssertionError):
        _check_point_in_time_is_past_only(mutant, tmp_path / "mutant")


def test_变异反证_把原子写退化成直接写_必须转红(tmp_path: Path, monkeypatch) -> None:
    """把「先写 .tmp 再 os.replace」退化成「直接写目标文件」⇒ 失败会留半截文件 ⇒ 性质⑤必须转红。"""
    mutant = _mutated_module(
        tmp_path,
        [('        df.to_csv(csv_tmp, index=False, encoding="utf-8")',
          "        df.to_csv(path, index=False, encoding=\"utf-8\")")],
        "ha_direct_write",
    )
    with pytest.raises(AssertionError):
        _check_failed_write_leaves_nothing(mutant, tmp_path / "mutant", monkeypatch)
