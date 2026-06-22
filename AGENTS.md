# AGENTS.md — Polymarket Weather Trading System

## 1. 项目背景

这是一个**信息套利型**预测市场交易系统，核心 edge 来自：
- **GFS（全球预报系统）物理模型** → 免费、公开、可验证的天气预报
- **Polymarket 散户定价** → 情绪驱动、缺乏物理模型、可预测偏差

> 区别于价格技术分析策略。天气预测的 alpha 来自物理模型信息优势，不会因为更多交易者使用而消失。

## 2. 项目结构

```
A05_polymarket_trading/
├── AGENTS.md                     ← 本文件（编码规范 + 构建步骤）
├── cli.py                        ← 统一 CLI 入口
├── PROMPT.md                     ← 任务指南 + 项目评估
├── requirements.txt              ← Python 依赖
│
├── src/                          ← 源代码
│   ├── core/                     ← 通用事件交易核心（已归档，仅参考）
│   │   ├── config.py             ← 全局配置
│   │   ├── agent.py              ← 通用事件交易代理（DEPRECATED）
│   │   ├── portfolio.py          ← 投资组合管理
│   │   └── ...
│   ├── data/                     ← 数据层（天气 + 通用）
│   │   ├── database.py           ← SQLite schema
│   │   ├── weather_backtester.py ← 天气回测引擎
│   │   ├── prediction_interface.py ← 可插拔预测源接口
│   │   ├── edge_composer.py      ← Edge 计算 + 多源融合
│   │   ├── gfs_prediction.py     ← GFS 预测源（温度 + 降水）
│   │   ├── geocoding.py          ← 城市 → 坐标映射
│   │   └── ...
│   ├── weather/                  ← 天气专用模块
│   │   ├── gfs_weather_source.py ← GFS 数据采集
│   │   ├── gfs_weather_pipeline.py ← 天气数据流水线
│   │   └── run_gfs_backtest.py   ← GFS 回测 runner
│   ├── scripts/                  ← 运维脚本
│   │   ├── generate_daily_signals.py ← 每日信号生成
│   │   ├── backfill_all_cities.py ← 城市数据回填
│   │   ├── coverage_report.py    ← 数据覆盖报告
│   │   └── ...
│   ├── strategies/               ← 策略层（通用事件策略已归档）
│   └── risk/                     ← 风控层
│
├── research/                     ← 研究基础设施（A02 标准）
│   ├── walk_forward.py           ← 滚动回测验证
│   ├── factor_ic.py              ← 因子 IC 分析
│   └── reports/                  ← 候选报告存档
│       ├── SOTA.md               ← 当前最优策略注册表
│       ├── TEMPLATE.md           ← 候选报告模板
│       └── YYYYMMDD_*.md         ← 具体候选报告
│
├── monitor/                      ← 运营监控
│   └── heartbeat.py              ← 健康检查 + 每日摘要
│
├── _archived/                    ← 归档代码（不删除，仅参考）
│   └── 通用事件交易策略说明.md
│
├── data/                         ← 运行时数据
│   ├── weather_markets.db        ← 天气市场 SQLite
│   ├── gfs_forecasts.db          ← GFS 预报 SQLite
│   ├── signal_history.db         ← 信号历史
│   └── calibration.json          ← GFS 偏差/方差校准
│
└── docs/                           ← 文档
    ├── EXPERIMENT_SUMMARY.md     ← 通用事件实验（7.7% 胜率）
    ├── POLYMARKET_TRADING_TUTORIAL.md ← 中文教程（含通用策略）
    └── REAL_TRADING_GUIDE.md     ← 真实交易指南
```

## 3. 战略方向（2026-06-02 更新）

**砍掉通用事件线**。原因：
- 实验结果：$5,000 纸面交易 7 天，胜率 7.7%，ROI -4.74%
- 5 种启发式策略（动量、反向、错误定价等）在预测市场无效
- LLM 定价无信息优势，与散户情绪同质化

**聚焦天气 GFS 线**。原因：
- 物理模型驱动的信息套利 → 真正可持续的 alpha
- GFS 是 NOAA 免费提供的高质量全球预报
- Polymarket 天气市场散户缺乏物理模型，存在系统性定价偏差

## 4. 构建步骤

```bash
# 1. 安装依赖
pip install -r requirements.txt

# 2. 初始化数据库
python3 -c "from src.data.database import init_all; init_all()"

# 3. 数据回填（所有 21 个城市）
python3 cli.py backfill --city all --start 2026-01-01

# 4. 生成今日信号
python3 cli.py signals

# 5. 运行回测
python3 cli.py backtest --city hong-kong --start 2026-03-01 --end 2026-05-31

# 6. 校准 GFS 偏差
python3 cli.py calibrate --city all

# 7. 滚动回测验证（研究级）
python3 -m research.walk_forward --city hong-kong --start 2026-01-01 --end 2026-05-31

# 8. 因子 IC 分析
python3 -m research.factor_ic --city all --start 2026-01-01 --end 2026-05-31
```

## 5. 编码规范

### 5.1 Python 风格
- **类型注解**：所有公共函数必须使用 `from __future__ import annotations` + 类型提示
- **Docstring**：Google 风格，模块顶部写使用示例
- **导入顺序**：`__future__` → 标准库 → 第三方 → 本项目（绝对导入 `from src.data…`）
- **无 `scipy` 依赖**：正态 CDF 用 `math.erf` 实现，保持轻量

### 5.2 数据库规范
- SQLite + `PRAGMA journal_mode=WAL`
- 表名：snake_case，主键显式声明
- 外键约束启用
- 时间戳统一使用 `YYYY-MM-DD`（date）或 ISO-8601（datetime）

### 5.3 配置规范
- 所有可调整参数在 `src/core/config.py` 中声明默认值
- 可被 `.env` 文件覆盖（通过 `python-dotenv`）
- 真实交易凭证**只读环境变量**，绝不写入磁盘（除 keystore 外）

### 5.4 回测规范
- **必须**使用 `weather_backtester.run_standard()`（pluggable PredictionSource）
- **禁止**修改 `weather_backtester.run()`（保留向后兼容）
- 滚动校准：每交易日使用前 N 个已解决市场对重新估计 bias/sigma
- 最小校准对数：`MIN_CALIB_PAIRS = 5`

### 5.5 研究规范（A02 SOP）
1. 任何新策略必须有 **walk-forward 验证**（18m train / 3m test / 3m step）
2. 任何新因子必须有 **IC 分析**（滚动 20 天窗口，IC_mean / IC_std / IC_sharpe）
3. 任何策略升级必须有 **消融实验**（baseline → +bias → +sigma → +filter）
4. 结果写入 `research/reports/YYYYMMDD_*.md`，按 `TEMPLATE.md` 格式
5. 只有 walk-forward + IC 均通过的策略才能进入 `SOTA.md`

## 6. 关键常量

| 参数 | 值 | 说明 |
|------|-----|------|
| `DEFAULT_SIGMA_TEMP` | 1.79°C | GFS 温度预报残差标准差 |
| `DEFAULT_BIAS_TEMP` | +0.89°C | GFS 冷偏置（香港） |
| `MIN_CALIB_PAIRS` | 5 | 滚动校准最小样本数 |
| `DEFAULT_CALIB_WINDOW` | 20 | 滚动校准窗口天数 |
| `MIN_EDGE` | 0.05 | 最小可交易 edge |
| `MAX_SANE_EDGE` | 0.60 | edge  sanity 上限 |
| `STOP_LOSS_PERCENT` | 0.15 | 正常仓位止损 |
| `LOW_PROB_STOP_LOSS` | 0.10 | 低概率仓位止损 |
| `MIN_DIST_SIGMA_EQ` | 0.5 | 点概率交易最小 GFS-threshold 距离（σ） |
| `MIN_EDGE_NEAR_THRESHOLD` | 0.25 | GFS 中心靠近 threshold (<1σ) 时的最低 edge |
| `MIN_CALIB_PAIRS_RELIABLE` | 100 | 可靠校准最少样本数 |

## 6.5 交易前检查清单（硬性约束，2026-06-22 加入）

### 规则 A：点概率距离约束

**点概率市场（rule=eq，「Will it be X°C?」）必须满足距离要求。**

| GFS校正值距threshold | 最低 edge | 是否允许交易 |
|----------------------|-----------|-------------|
| < 0.5σ | — | ❌ **禁止**（噪声主导） |
| 0.5σ – 1.0σ | ≥ 25% | ⚠️ 需人工确认 |
| 1.0σ – 1.5σ | ≥ 15% | ✅ 正常 |
| > 1.5σ | ≥ 8% | ✅ 最佳区间 |

> **原因**：T003 教训——GFS 中心 30.84°C 距 threshold 31°C 仅 0.1σ，edge +11.6% 完全被 bias/sigma 误差淹没。
>
> 距离 = `abs(gfs_corrected − threshold) / sigma`

### 规则 B：同日同城 neg-risk 市场联合分析

**同一 neg-risk market group 内多笔仓位 = 必须做联合 payoff 矩阵。**

1. 列出所有可能的实际温度（至少覆盖 GFS 中心 ±2σ 范围内每个整数 °C）
2. 对每个温度计算每笔仓位的 PnL + 总 PnL
3. 确认：**最坏情况概率 < 最好情况概率**（否则不是对冲，是集中押注）
4. 确认：EV 最可能区间（概率 > 50% 的区间）净 PnL ≥ 0
5. 将 payoff 矩阵写入 trade_log.json 的 `rationale` 字段

> **原因**：T005+T006 教训——BUY_NO 30°C + BUY_YES 32°C 看似互补，实际是押注 32°C 的杠杆仓位。最坏情况（30°C, −$9.95）概率 24.7% > 最好情况（32°C, +$53.24）概率 23.7%。

### 规则 C：校准质量分级

| 等级 | 样本数 | 覆盖季节 | 允许 lead time | 最低 edge |
|------|--------|---------|---------------|-----------|
| 🟢 可靠 | ≥ 100 | 跨季节 ≥ 2 | 所有 | 标准阈值 |
| 🟡 可交易 | 20–99 | 单季节 | T+0 优先 | ×1.5 |
| 🔴 禁止 | < 20 | — | 不允许交易 | — |

**当前校准状态：**
- 🟢 HK T+0: 791 pairs (HKO, 跨年) — 可靠
- 🟡 HK T+1: 73 pairs (spring-only) — edge 阈值 ×1.5 → ≥18% 才能交易
- 🟡 HK T+2: 73 pairs (spring-only) — 同上，且 ≤48h lead 不推荐
- 🟢 London T+0/T+1: 408 pairs (PM settlement, 跨季节) — 可靠

> **原因**：T001/T002/T003 全用弱校准（73 pairs），log 标注 WARNING 但照下单。T002 赢了是运气，T003 输了是必然。

### 规则 D：BUY_YES 低概率仓位止损

BUY_YES 方向（买「会发生」），如果 Yes 价格 < 0.15（低概率事件）：
- 止损设为 **−100%**（接受归零）→ 仓位必须 ≤ `LOW_PROB_STOP_LOSS` × 总资金
- 禁止对同一 neg-risk group 同时开 ≥ 2 个 BUY_YES
- 每笔低概率 BUY_YES 必须在 rationale 中标注「接受归零风险」

> **原因**：T006 BUY_YES 32°C @ $0.09 有 +14.7% edge，但 23.7% 胜率意味着每 4-5 次才中一次。配合 T005 形成了 30°C 双杀风险。

## 7. 安全与合规

- **纸面交易默认**：`PAPER_TRADING=true`，真实交易需显式 `.env` 设置
- **私钥绝不落地**：`POLYMARKET_PRIVATE_KEY` 只通过环境变量或交互式输入
- **敏感话题过滤**：自动跳过涉及政治敏感关键词的市场
- **日亏损上限**：真实交易每日亏损不超过 `$MAX_DAILY_LOSS`

## 7.5 交易日志（必须遵守）

**每笔下注必须记录到 `data/trade_log.json`，下注前后都要维护。**

- **下注前**：追加一条新 entry（`status: "open"`），填入所有 edge 计算参数（gfs_raw/bias/sigma/corrected/model_P/market_ask/edge/rationale）
- **结算后**：填充 `pnl_usd` / `resolved_at` / `resolution`，status 改为 `"resolved"`
- **如提前平仓**：写入 `exit` 对象（含平仓价、净 PnL），同时在 `liquidation_outcome` 字段记录结算结果（赢/输对照），注满 forward-test 参考
- **每次下注只会新增一条记录，不删除、不覆盖已有数据**
- 日志结构见 `data/trade_log.json` meta 注释
- 这是硬性约束——用于 PnL 审计、策略归因、未来 analysis。不记录 = 承认交易不可审计。

## 8. 沟通规范（必须遵守）

- **回复语言：简体中文（简体中文）**。所有面向用户的回复（对话、状态报告、edge 表、风险提示）必须用简体中文。
- **技术术语保留英文原文**：edge, backtest, Sharpe, PnL, lead time, order book, bid/ask, token_id, GFS, CLOB, FOK/GTC, bias, sigma 等。
- **代码、文件路径、命令行、变量名**：保持原样（不翻译）。
- **数字优先**：用户偏好直接结论——先给数字/yes-no/日期，再给方法论。
- **诚实标注不确定性**：数据不可信（geofence、stale、小样本）时，明确说"置信度低"并附上置信区间或范围。
- **绝不自动下单**：任何真实订单前必须等用户明确确认（"yes, place these" 或类似）。
- **不啰嗦流程**：用户讨厌 process-talk，要 results。

## 9. 常见操作速查

| 操作 | 命令 |
|------|------|
| 查看今日信号 | `python3 cli.py signals` |
| 查看指定城市 | `python3 cli.py signals --city hong-kong` |
| 运行回测 | `python3 cli.py backtest --city all --start 2026-03-01` |
| 校准偏差 | `python3 cli.py calibrate --city all` |
| 数据覆盖报告 | `python3 cli.py coverage` |
| 滚动回测 | `python3 -m research.walk_forward` |
| 因子 IC | `python3 -m research.factor_ic` |
| 健康检查 | `python3 -m monitor.heartbeat` |
| 性能监控 | `python3 cli.py performance` |
| 查看 SOTA | `python3 cli.py sota` |

---
**最后更新**: 2026-06-22
**系统版本**: Polymarket Weather Trading System v2.1 (GFS-only + trade guards)
