# CMDS.md — qlib 量化研究工程 · 可运行命令

> 规约见 `manu/AGENTS.md`；铁律见 `manu/memory/rules.md`。
> **工作方式**：只改本地 `/media/manu/1TB-Volume/workspace/qlib/`，改完同步到服务器再跑。

## env（服务器）

```bash
ssh -F /dev/null -p 32222 huangzhe@192.168.99.40
source /home/huangzhe/anaconda3/etc/profile.d/conda.sh && conda activate ql
cd /tmp/pycharm_project_658d3fc4
```

> ⚠️ `ssh` 必须加 `-F /dev/null`（本机 ssh_config.d 权限不对）；服务器 sshd 在 **32222**，22 端口关闭。

## 安装 / 复装 qlib（服务器）

```bash
cd /tmp/pycharm_project_658d3fc4
SETUPTOOLS_SCM_PRETEND_VERSION=0.9.8 pip install -e ".[dev]"
```

> 服务器工程目录**无 `.git`** ⇒ 必须给 `SETUPTOOLS_SCM_PRETEND_VERSION=0.9.8`，
> 否则 setuptools_scm 探测不到版本而失败。

## 同步本地改动到服务器

```bash
cd /media/manu/1TB-Volume/workspace/qlib
bash manu/tools/sync_to_server.sh              # 同步 manu/ 全目录
bash manu/tools/sync_to_server.sh manu/memory  # 只同步子路径
bash manu/tools/sync_to_server.sh --dry-run    # 预演
bash manu/tools/sync_to_server.sh --check      # 只做 md5 双侧比对
```

## 冒烟验证

```bash
export PATH=/home/huangzhe/anaconda3/envs/ql/bin:$PATH
cd /tmp/pycharm_project_658d3fc4
python -c "import qlib; print(qlib.__version__, qlib.__file__)"   # → 0.9.8 + 工程目录
qlib.init()                                                        # → successfully initialized
qrun --help                                                        # → 用法
python -c "from qlib.contrib.model import LGBModel; print('ok')"   # → ok
```

## 数据集

```bash
# 官方 cn_data 因数据安全策略暂时下线；社区镜像：
wget https://github.com/chenditc/investment_data/releases/latest/download/qlib_bin.tar.gz
mkdir -p ~/.qlib/qlib_data/cn_data
tar -zxvf qlib_bin.tar.gz -C ~/.qlib/qlib_data/cn_data --strip-components=1
```

> 数据集路径**一律显式指定**（`qlib.init(provider_uri=...)`），不依赖默认值
> `~/.qlib/qlib_data/cn_data`。**下载数据属重活，需用户当轮授权。**

## 跑官方示例

```bash
export PATH=/home/huangzhe/anaconda3/envs/ql/bin:$PATH
cd /tmp/pycharm_project_658d3fc4
python examples/benchmarks/LGBModel/workflow_config_lightgbm_Alpha158.yaml
```

> 🔴 **回测/训练属重活，必须用户当轮明确授权**（铁律一）。
> 引用任何回测数字前必须先填满 `manu/memory/rules.md` §十二·12.1 的六项口径。

## A 股截面打分 + Top-K 选股（本工程自研流水线）

> 代码：`manu/experiments/ashare_topk_selection/`（config.py / run_pipeline.py / export_topk.py）
> 口径与结果：`manu/memory/ashare_topk_20261104.md`
> ⚠️ 训练 + 回测属**重活**，需用户当轮授权；产物落 `/mnt/data/siping/runs/ashare_topk/runs/<recorder_id>/`。

```bash
export PATH=/home/huangzhe/anaconda3/envs/ql/bin:$PATH
cd /tmp/pycharm_project_658d3fc4

# 0) 数据体检（校准区间/股票池/字段，产出 JSON 报告）
python manu/experiments/ashare_topk_selection/inspect_dataset.py \
  --provider-uri /mnt/data/siping/datasets/qlib/cn_data

# 1) 训练 + 打分 + 回测（一步产出 label/model/pred + manifest.json）
python manu/experiments/ashare_topk_selection/run_pipeline.py

# 2) 导出「模型打分 Top-N 排名」（kind=model_score_ranking，非策略实际持仓）
python manu/experiments/ashare_topk_selection/export_topk.py \
  --run-dir /mnt/data/siping/runs/ashare_topk/runs/<recorder_id> --top 5

# 2b) test 段每个交易日的 Top-N CSV
python manu/experiments/ashare_topk_selection/export_topk.py \
  --run-dir /mnt/data/siping/runs/ashare_topk/runs/<recorder_id> --top 30 --all-dates
```

> 🔴 `export_topk.py` 的信号只含 **test 段**（`LGBModel.predict` 默认 `segment="test"`）。
> 🔴 引用回测数字前必须先填满 `manu/memory/rules.md` §十二·12.1 的六项口径。

## 换手 / 成本分解（只读产物，不重训）

```bash
# 🔴 回测必须钉 PYTHONHASHSEED=0：qlib 的 Position.get_stock_list 用 set()，
#    迭代序随进程随机 ⇒ 不钉则回测**跨进程不可复现**（实测同 arm 两进程差 3.65pp）。
#    详见 manu/memory/rules.md §12.10。
export PYTHONHASHSEED=0
cd /tmp/pycharm_project_658d3fc4

# 1) 换手率与成本对账（单 arm，基线 topk=30/n_drop=5）
python manu/experiments/ashare_topk_selection/turnover_analysis.py \
  --run-dir /mnt/data/siping/runs/ashare_topk/runs/<recorder_id>

# 2) 降换手 sweep（14 组 arm × valid/test 两段；选参只看 valid）
python manu/experiments/ashare_topk_selection/turnover_sweep.py \
  --run-dir /mnt/data/siping/runs/ashare_topk/runs/<recorder_id>

# 3) valid 段分年稳定性（不引用 test ⇒ 用于识别跨子区间稳健的参数）
python manu/experiments/ashare_topk_selection/valid_stability.py \
  --run-dir /mnt/data/siping/runs/ashare_topk/runs/<recorder_id>
```

> ⚠️ 这三个脚本都会**打确定性补丁**（`determinism.py`，运行期生效，上游源码零改动）。
> ⚠️ 脚本**只输出排序表与推荐**，**不会**自动改 `config.py`。

## 数据集下载（服务器直下，禁走 host 代理）

```bash
# 服务器上执行；GitHub release 的 302 链会挂死 ⇒ 走 api.github.com 取签名直链 + aria2c 多连接
python manu/tools/fetch_qlib_dataset.py --root /mnt/data/siping/datasets/qlib
```

> 来源与 md5 留痕在 `<root>/fetch_manifest.json`。

## ETF 轮动线（人工低频，3~5 只 ETF；服务器）

> 代码在 `manu/etf_rotation/`；铁律见 `manu/memory/rules.md §十四`；
> 事实与结论见 `manu/memory/etf_rotation_20261104.md`。
> ⚠️ **akshare 只能用 env `ak`**（`/home/huangzhe/anaconda3/envs/ak/bin/python`），
> **绝不能装进 `ql` env**（会把 numpy 降级、拖垮 qlib）。

```bash
# ---- 服务器（重活一律上服务器）----
ssh -F /dev/null -p 32222 huangzhe@192.168.99.40
cd /tmp/pycharm_project_658d3fc4
export PYTHONHASHSEED=0
PY=/home/huangzhe/anaconda3/envs/ql/bin/python
AK=/home/huangzhe/anaconda3/envs/ak/bin/python

# 1) ETF 数据底座（akshare，服务器直下，禁走 host 代理）—— 顺序不可交换
$AK manu/etf_rotation/data/fetch_etf_universe.py --stage all
$AK manu/etf_rotation/data/fetch_etf_prices.py    --resume
$AK manu/etf_rotation/data/fetch_etf_universe.py --stage finalize
$AK manu/etf_rotation/data/fetch_etf_holdings.py --resume

# 2) 全市场个股打分 S（上游；~35 min，产物 stock_scores.csv）
$PY manu/etf_rotation/qlib_scores.py --mode infer --out-dir /mnt/data/siping/runs/etf_rotation

# 3) 当期调仓建议（中文清单 + 金额）
$PY manu/etf_rotation/recommend.py --asof 2026-11-03 \
    --out-dir /mnt/data/siping/runs/etf_rotation

# 4) 回测：主口径 / 压力成本 / 消融
$PY manu/etf_rotation/backtest.py --tag base \
    --rebalance-every 2 --alpha-weight 0.5 --momentum-weight 0.5 --hold-buffer 2
$PY manu/etf_rotation/backtest.py --tag stress \
    --rebalance-every 2 --alpha-weight 0.5 --momentum-weight 0.5 --hold-buffer 2 \
    --commission 0.0005 --slippage 0.0020
# 消融：--alpha-weight 0.0（纯动量）/ 1.0（纯 E）；--rebalance-every 1（双周）/ 4（季频）
```

产物：`/mnt/data/siping/runs/etf_rotation/`
- `stock_scores.csv`（全市场个股分数，374,793 行 / 69 个信号日）
- `etf_rotation_backtest_<tag>.json`（含 `segment_report` / `n_failed_rebalances` / 偏差披露）
- `etf_rotation_nav_<tag>.csv`（日频 NAV：portfolio / benchmark / excess）
- `rebalance_recommendation.json|.csv`（当期清单 + 金额）

> 🔴 引用回测数字**必须同时给出**口径六项 + 样本量（调仓期数）+ **分段稳定性** + 选择偏差声明。

## ETF 自动化流水线（2026-11-04 新增：qlib → ETF 报告 → TradingAgents → 调仓建议）

# 服务器（重活一律上服务器；host 18:30 关机 ⇒ 夜间链在服务器自主跑）
cd /tmp/pycharm_project_658d3fc4

# 1) 全市场个股打分 S（上游；产物 /mnt/data/siping/runs/etf_rotation/stock_scores.csv）
export PYTHONHASHSEED=0
/home/huangzhe/anaconda3/envs/ql/bin/python manu/etf_rotation/qlib_scores.py --mode infer

# 2) ETF 分析报告（= 喂给 TradingAgents 的正文；JSON + MD）
/home/huangzhe/anaconda3/envs/ql/bin/python manu/etf_rotation/analyze.py \
    --asof 2026-11-03 --out-dir /mnt/data/siping/runs/etf_rotation

# 3) 当期调仓建议（真实持仓口径；--holdings 权重 = account.py::current_weights()）
/home/huangzhe/anaconda3/envs/ql/bin/python manu/etf_rotation/recommend.py --asof 2026-11-03 \
    --pool-mode e_covered --entry-ma-window 20 --exit-ma-window 60 \
    --holdings "512800:0.2254,159929:0.1117,513330:0.0760,513770:0.0373,512690:0.0298"

# 4) 真实账户口径自检（改数必须抛异常）
cd manu/etf_rotation && /home/huangzhe/anaconda3/envs/ql/bin/python account.py

# 5) TradingAgents（llm env；research_context 注入外部报告）
#    注意：reasoning_effort=max 时 max_tokens 必须 >= 32768，否则正文会被思考 token 吃光
export PATH="$HOME/.local/bin:$PATH"; export CODEX_HOME=/mnt/data/.codex
cd /tmp/pycharm_project_658d3fc4/TradingAgents && /home/huangzhe/anaconda3/envs/llm/bin/python <script>.py

# 同步到服务器（本地改完必跑；md5 双侧比对）
cd /media/manu/1TB-Volume/workspace/qlib && bash manu/tools/sync_to_server.sh manu

# 6) ETF 轮动 · 一键自动化流程（qlib 个股层 → ETF 报告 → TradingAgents → 调仓计划）
#    服务器跑；host 只改代码 + 同步（铁律〇：本地改完 bash manu/tools/sync_to_server.sh manu）
cd /tmp/pycharm_project_658d3fc4 && bash manu/etf_rotation/run_pipeline.sh --asof 2026-11-03
#    日常调仓（复用已有个股分数，不跑重活）：
#    bash manu/etf_rotation/run_pipeline.sh --skip-scores --skip-prices
#    分步：
#      /home/huangzhe/anaconda3/envs/ql/bin/python manu/etf_rotation/qlib_scores.py --mode infer --out-dir /mnt/data/siping/runs/etf_rotation
#      /home/huangzhe/anaconda3/envs/ql/bin/python manu/etf_rotation/analyze.py --asof 2026-11-03 \
#          --out-dir /mnt/data/siping/runs/etf_rotation --stock-prices /mnt/data/siping/runs/etf_rotation/stock_prices.csv
#      /home/huangzhe/anaconda3/envs/ql/bin/python manu/etf_rotation/final_rebalance_plan.py \
#          --out-dir /mnt/data/siping/runs/etf_rotation
#    产物：etf_analysis_report.{json,md} / tradingagents_decision.md / final_rebalance_plan.{json,md}

## 仓库整理与版本控制（host 本地，2026-11-05 新增）

> 规约见 `manu/memory/rules.md §十四.93`：**版本库只放「核心代码 + 文档 + 事实」，中间产物一律 ignore**。
> 判定问句：**「这个文件丢了，明天能不能重跑出来？」** 能 ⇒ 不入库；不能 ⇒ 入库。

```bash
cd /media/manu/1TB-Volume/workspace/qlib

# 1) 看当前状态（整理前先看，别盲删）
git status --porcelain
git diff --cached --numstat | wc -l        # 已暂存文件数

# 2) 暂存核心代码 + memory（**不 commit**：commit 需用户授权）
git add -A manu/ CMDS.md .gitignore .gitattributes
git diff --cached --name-only | wc -l       # 期望 90
git diff --cached --name-only | grep -E "__pycache__|\.pyc$|\.ruff_cache|_host_backup|^runs/|\.log$"   # 期望：空（未误收）

# 3) 复核「输入真值确实能入库」（**权威判据是 git add -n，不是 check-ignore 退出码**）
git add -n manu/etf_rotation/data/account_snapshot.csv   # 期望：add 'manu/etf_rotation/data/account_snapshot.csv'
git cat-file blob :manu/etf_rotation/data/account_snapshot.csv | cmp - manu/etf_rotation/data/account_snapshot.csv && echo "逐字节一致 ✓"

# 4) memory 表格结构自检（改了 memory 必跑）
/home/huangzhe/anaconda3/envs/ql/bin/python manu/tools/check_memory_tables.py

# 5) 全量闸门（改了 memory / CMDS.md 必跑：它们在指纹表 35 条登记项里）
#    服务器跑；日志：/mnt/data/siping/runs/etf_rotation/_mq3_final4.log
cd /tmp/pycharm_project_658d3fc4 && bash manu/etf_rotation/run_gates.sh
```

> 🔴 **`.gitignore` 顺序陷阱**：上游 `.gitignore:15` 有 `*.csv`（为 qlib 自身大数据文件设的）⇒
> 会**静默忽略** `manu/etf_rotation/data/account_snapshot.csv`（输入真值）⇒ 本工程在**末尾**补
> `!manu/etf_rotation/data/account_snapshot.csv`（白名单必须排在通配之后）。
> 🔴 **`.gitattributes` 陷阱**：该 csv 带 **BOM + CRLF**，没有 `-text` 时 git 会归一化行尾 ⇒
> checkout 后文件字节变了 ⇒ md5 双侧比对**假漂移** ⇒ 已用 `manu/etf_rotation/data/account_snapshot.csv -text` 钉住；
> 改完 `.gitattributes` 若 index 仍是旧版，须 `git rm --cached` 再 `git add`。
> 🔴 **指纹表**：`CMDS.md` 与 memory 四件套在 `/mnt/data/siping/runs/etf_rotation/_ms5_md5.txt` 的 35 条登记项里
> ⇒ 改动它们**必然**让闸门 P2 变红 ⇒ 按 `rules.md §十四.92` 第 7 条重生成（先备份 → 从当前良好版重建 → 断言登记项集合不变）。
