# qlib 0.9.8 API 实测坑（2026-11-04，全部有源码/真机证据）

> 定位：本文件只记**「文档/直觉与 0.9.8 实际行为不一致」**的点。
> 通用铁律见 `manu/memory/rules.md`；环境见 `manu/memory/env_setup_20261104.md`。
> 本 checkout = microsoft/qlib HEAD `54355232`（含安全加固 `fix(security): constrain config-driven code execution`），
> 服务器 env `ql` 装的是它的 **editable** 版本 ⇒ 以下行为以**本 checkout 源码**为准。

---

## A. 阻断级（不照做就直接跑不起来）

### A1. `D.features()` 参数顺序是 `(instruments, fields)`，**不是** `(expression, instrument)`
- 证据：`qlib/data/data.py:1164` `def features(self, instruments, fields, ...)`；
  `qlib/data/dataset/loader.py:223` 内部调用 `D.features(instruments, exprs, ...)`；
  官方文档 `docs/start/getdata.rst:86` 也是 `D.features(instruments, fields, ...)`。
- **踩坑现象**：按老写法 `D.features(["$close"], insts)` ⇒ `fields=list(insts)` 变成 `["market","filter_pipe"]`
  ⇒ 报 `ExpressionSyntaxError: Unsupported syntax in Qlib expression: Name`。
  这个报错信息**极具误导性**（看起来像表达式语法问题，实际是传参顺序错）。
- ✅ 正确：`D.features(insts, ["$close", "Ref($close, 5)"], start_time=..., end_time=..., freq="day")`。

### A2. `D.instruments()` **没有** `freq` 参数
- `qlib/data/data.py:1153` `def instruments(self, market="all", filter_pipe=None, start_time=None, end_time=None)`。
- 有 `freq` 的是 `D.list_instruments(instruments, start_time, end_time, freq="day", as_list=True)`。

### A3. `Operators` 注册表**只有 `qlib.init()` 之后**才有内容
- `qlib/data/ops.py:1672` `Operators = OpsWrapper()`；填充发生在 `register_all_ops(C)`，
  由 `qlib/config.py:505-510` 在 `Config.register()`（即 `qlib.init()`）时调用。
- **踩坑现象**：在 `qlib.init()` 之前查 `Operators.get_operator("Ref")` ⇒ `AttributeError: 不可用`，
  会误判成「0.9.8 不支持 Ref/Mean」。init 之后实测 **52 个算子**，`Ref/Mean/Std/Corr/Rsquare/If/Feature` 全部可用。
- ⚠️ `RANK`/`SUM` **未注册**（这两个不是 qlib 算子名）。

### A4. `qlib.init(exp_manager=...)` 的 kwargs **必须同时给 `uri` 和 `default_exp_name`**
- `qlib/config.py:239-246` 默认值含 `default_exp_name`；`Config.set` 是**整键浅替换**，
  只传 `uri` ⇒ `MLflowExpManager(uri=...)` 缺形参 ⇒ `TypeError: missing 1 required positional argument: 'default_exp_name'`。
- ✅ `kwargs: {"uri": ..., "default_exp_name": "ashare_topk_selection"}`。
- 或：完全不覆盖 `exp_manager`，改用 `R.start(experiment_name=..., uri=...)`（`uri` 形参存在，`qlib/workflow/__init__.py:46`）。

### A5. `data_key` 只能是 `raw` / `infer` / `learn`
- `qlib/data/dataset/handler.py:52-54` `DK_R="raw" DK_I="infer" DK_L="learn"`；
  `handler.py:422` `ATTR_MAP = {DK_R:"_data", DK_I:"_infer", DK_L:"_learn"}`。
- 写 `"__infer__"` ⇒ `KeyError`。

### A6. `Recorder` **没有** `save_metrics`
- 全仓 grep `save_metrics` = **0 命中**。可用的是 `log_params(**kw)` / `log_metrics(**kw)` / `set_tags(**kw)`。
- ⚠️ `set_tags` 签名是 `(**kwargs)`，传 dict ⇒ `TypeError: takes 1 positional argument but 2 were given`。
- ⚠️ `save_metrics` 若写在「回测之后、manifest 之前」，一次成功训练会**丢掉全部 manifest**。

### A7. `pred.pkl` 的 MultiIndex 是 `(instrument, datetime)`，标量 `.loc[date]` 会 KeyError
- `SignalRecord.generate` 存的是 `pred.to_frame("score")`（`qlib/workflow/record_temp.py:200-202`）。
- pandas 对 MultiIndex 的标量 `.loc[Timestamp]` **只查 level 0**（= instrument）。
- ✅ `pred_df.xs(date, level="datetime")`（`qlib/backtest/signal.py:58` 用的是 `convert_index_format`+`swaplevel`）。
- ⚠️ `LGBModel.predict(dataset, segment="test")` **默认只出 test 段**（`qlib/contrib/model/gbdt.py:93`），
  所以 `pred.pkl` 里**没有** train/valid 的预测。

---

## B. 语义级（能跑，但会算错/退化）

### B1. `Alpha158` 的类默认标签是 **close**；VWAP 要显式覆盖（或改用 `Alpha158vwap`）
- 🔴 **2026-11-04 订正**：本条原先写成「`Alpha158` 内置标签是 VWAP」，**按源码是错的**。
  `qlib/contrib/data/handler.py`：
  - `Alpha158.get_label_config()` → `["Ref($close, -2)/Ref($close, -1) - 1"], ["LABEL0"]`（**close**）
  - `Alpha158vwap.get_label_config()` → `["Ref($vwap, -2)/Ref($vwap, -1) - 1"], ["LABEL0"]`（vwap）
- 两者**时点相同、价格字段不同**：都是 T+1 → T+2 的收益。
- 🔴 **覆写机制**：`Alpha158.__init__` 里 `kwargs.pop("label", self.get_label_config())`
  ⇒ 显式传 `label=(exprs, names)` 时**以传入值为准**；不传才回落到类默认值。
- 🔴 **踩坑：读类默认值 ≠ 实际生效值**。`handler.get_label_config()` 恒返回**类默认**（close），
  即使本次实际跑的是 VWAP ⇒ 用它写 manifest 会把口径记错。
  **实际生效口径**取 `handler.data_loader.fields["label"]`（loader 解析后的真实配置）。
  （本工程 `run_pipeline.py` 已据此修好；实证：`label.pkl` 逐值等于 VWAP 表达式，误差 0.0，
  而 close 口径差 1e-3~6e-3。）
- 交付时必须点名用的是哪个口径（本工程用 VWAP）。

### B2. loader 产出的是 **MultiIndex 列**（level0 = `feature` / `label`）
- `qlib/data/dataset/loader.py` `DLWParser.load()`：`pd.concat({grp: load_group_df(...)}, axis=1)`。
- ⇒ 凡是「只该作用在特征上」的处理器**必须**写 `fields_group="feature"`，
  否则 `get_group_columns`（`processor.py:18`）取不到列。
- ⚠️ **尤其 `Fillna`**：若不限定 group，会把 **label 的 NaN 填成 0**，
  于是后面的 `DropnaLabel` 失效 —— 末位交易日（无未来数据）本该被剔除的训练行会**混进训练集**。
- ⚠️ `infer` 段（单层列）与 `learn` 段（MultiIndex 列）的列结构不同：
  `infer_processors` 里不要写 `fields_group="feature"` 之外的组名。

### B3. `Alpha158` 的 `infer_processors` 默认是 **空列表**（`handler.py:105`）
- `Alpha360` 才用 `_DEFAULT_INFER_PROCESSORS = [ProcessInf, ZScoreNorm, Fillna]`（`handler.py:41-45`）。
- 官方 Alpha158+LGB 配方（`examples/benchmarks/LightGBM/workflow_config_lightgbm_Alpha158.yaml`）**不写** infer_processors。

### B4. `fit_start_time` / `fit_end_time` 只有被**接受这两个形参的处理器**消费时才生效
- `qlib/contrib/data/handler.py:12-31` `check_transform_proc`：仅当处理器的 `__init__` 形参里
  同时有 `fit_start_time` 和 `fit_end_time` 才注入窗口。
- `ZScoreNorm.__init__(fit_start_time, fit_end_time, fields_group=None)` ✅ 接受；
  `CSZScoreNorm`（标签截面标准化）❌ 不接受。
- ⇒ **不挂 ZScoreNorm 类处理器，fit_* 就是空断言**（此时没有泄漏，但保护也是假的）。

### B5. handler 的 `start_time` 必须**早于** train 段起点（预热期）
- qlib 会自动向前扩展取数窗口：`qlib/data/data.py:855-856`
  `lft_etd, rght_etd = expression.get_extended_window_size()`；
  `query_start = max(0, start_index - lft_etd)`。
- 但扩展不出 `start_time` 之前的数据 ⇒ Alpha158 最长回看 60 天（`Ref($close, 59)`），
  若 `start_time == TRAIN[0]`，训练段开头 60 天的特征是 NaN→`Fillna` 填 0 的**垃圾值**。
- ✅ 做法：`START_TIME` 比 `TRAIN[0]` 早 1 年。

---

## C. 训练侧（本工程实测，2026-11-04）

### C1. 🔴 qlib 的 early stopping 在本工程会**把模型裁成 1~2 棵树**，预测退化
- `qlib/contrib/model/gbdt.py:78-86`：`valid_sets=ds` 传的是 **[train, valid]**，
  且 `lgb.early_stopping(stopping_rounds)` 默认 `first_metric_only=False` ⇒ 同时盯 train/valid 两个指标。
- LightGBM 早停触发后**按最早的最优迭代裁剪模型**，`Booster.predict` 默认也只用 `best_iteration`。
- 实测（Alpha158 + csi300）：valid l2 在第 1~2 轮之后**单调变差** ⇒ `best_iteration` = 1~2
  ⇒ 预测只剩几个不同取值，截面排名无意义（test IC ≈ 0）。
- ✅ 本工程做法：**关掉 early stopping**（`early_stopping_rounds=None`），固定轮数，
  用 λ1/λ2 + 学习率控制过拟合；轮数按 **valid IC** 选，**不看 test**。
- ⚠️ 这不是 qlib 官方配方（官方用 `early_stopping_rounds=50`），偏离理由已记录在
  `manu/memory/ashare_topk_20261104.md`。

### C2. LightGBM 的 `subsample` 默认是**空操作**
- `bagging_freq` 默认 0 = 关闭 bagging ⇒ `bagging_fraction/subsample` 不生效。
- 要真做行采样必须显式 `bagging_freq=1`。

### C3. `dump_model()` 默认只导到 `best_iteration`
- 统计「模型到底建了多少棵树」要用 `booster.current_iteration()`（**是方法，要加括号**）
  或 `dump_model(num_iteration=booster.current_iteration())`。
- 早期用 `dump_model()["tree_info"]` 数树会**低估**模型规模，差点误判成「模型没训练」。

---

## D. 数据获取（本工程网络实测）

### D1. 🔴 服务器到 `github.com/.../releases/latest/download/...` 的 302 链会**挂死**
- 实测：裸 curl 15s **0 字节**、`code=000`；aria2c `0B/0B CN:1` 无限空转。
- 但：`api.github.com` 秒回；`release-assets.githubusercontent.com` 直连 0.12s 可达；PyPI 23MB/s。

### D2. ✅ 解法：API 解析资产 → 直连签名 URL
```python
# 1) 取资产元信息（含精确字节数）
GET https://api.github.com/repos/{repo}/releases/latest        # 或 /releases/tags/{tag}
# 2) 取签名直链（不加 -L，自己抓 Location）
GET https://api.github.com/repos/{repo}/releases/assets/{asset_id}
    Accept: application/octet-stream        →  302 Location: https://release-assets.githubusercontent.com/...
```
- 签名直链有效期约 1h（`se=` 参数）⇒ 需重解析续传。
- 工具：`manu/tools/fetch_qlib_dataset.py`（API 解析 + aria2c 16 连接续传 + 尺寸校验 + manifest）。

### D3. `aria2c` 1.35 **没有** `--lowest-speed-limit` / `--lowest-speed-time`（那是 curl 的选项）
- 误加会让 aria2c **直接以 rc=28 退出**，表面看是「下载不动」，实际是参数非法空转 13 轮。
- 停滞检测改由外层轮次比较字节数完成。

### D4. 速度量级（服务器实测）
- curl 单连接：~64 KB/s（ETA 1h45m）
- aria2c 16 连接 + 签名直链：**~1.6~5 MiB/s**（544 MiB 用时 ~2 分钟）

---

## E. 本工程数据实测（2026-11-04，chenditc 全量）

| 项 | 值 |
|:--|:--|
| 来源 | `chenditc/investment_data` release `latest`，`qlib_bin.tar.gz` |
| md5 | `3010e494e31de446877894059ceb9503` |
| 压缩 / 解压 | 570,483,992 B (544 MiB) / 852 MB |
| 归档顶层 | `qlib_bin/{calendars,features,instruments}` ⇒ 解压要 `--strip-components=1` |
| 交易日历 | **2000-01-04 ~ 2026-11-03，6501 个交易日** |
| 标的 | 6174 只（含北交所），10 个日频字段 |
| 字段 | adjclose / amount / change / close / factor / high / low / open / volume / vwap |
| 股票池文件 | all / csi300 / csi500 / csi800 / csi1000 / csiall（**无 csi100**） |
| csi300 末日成分 | 300 只 |
| instruments/*.txt 格式 | TSV **无表头**：`symbol\tstart\tend` |

> ⚠️ `qlib` 官方文档 `scripts/README.md` 写 `--strip-components=2` —— **已过时**，
> chenditc 的 tar 只有一层 `qlib_bin/`，正确的是 `--strip-components=1`。
