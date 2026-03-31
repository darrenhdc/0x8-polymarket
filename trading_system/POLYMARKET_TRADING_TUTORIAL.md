# Polymarket 交易系统中文教程

## 目录

1. [系统概述](#系统概述)
2. [快速开始](#快速开始)
3. [策略机制详解](#策略机制详解)
4. [风险管理](#风险管理)
5. [系统架构](#系统架构)
6. [使用指南](#使用指南)

---

## 系统概述

这是一个基于 AI 的 Polymarket 预测市场自动化交易系统，采用**模拟交易**模式，使用虚拟资金进行交易。

### 核心特点

- 🤖 **多策略 AI 决策引擎** - 5 种不同策略综合判断
- 📊 **实时仪表盘** - Flask Web 界面监控持仓和盈亏
- 🔄 **自动价格更新** - 后台每 60 秒更新持仓价格
- 🛡️ **严格风控** - 止损止盈、仓位限制、敏感话题过滤
- 📝 **决策日志** - 完整记录所有交易决策和理由

### 初始资金配置

- 初始资金：$5,000 虚拟美元
- 最大单仓位：$500
- 最大总风险敞口：$2,500
- 最大持仓数：10 个

---

## 快速开始

### 1. 查看当前状态

```bash
python agent.py --status
```

### 2. 运行一次交易周期

```bash
python agent.py --once
```

### 3. 持续运行交易代理

```bash
python agent.py --interval 300  # 每 5 分钟运行一次
```

### 4. 启动价格更新器

```bash
python updater.py  # 每 60 秒更新价格
```

### 5. 启动仪表盘

```bash
python dashboard.py  # 访问 http://localhost:5001
```

---

## 策略机制详解

系统采用**5 种策略**综合评估市场，每种策略输出独立的交易信号和置信度，最终选择置信度最高的决策。

### 策略 1：错误定价套利 (Mispricing)

**文件位置**: `decision_engine.py` 第 113-134 行

**核心思想**: 寻找 YES + NO ≠ 1.0 的市场，利用定价错误套利。

```python
# 如果价差 > 5%，可能存在机会
if spread > 0.05:
    if yes_price < no_price and yes_price < 0.45:
        return "BUY_YES", 0.60, "错误定价检测：YES 被低估"
    elif no_price < yes_price and no_price < 0.45:
        return "BUY_NO", 0.60, "错误定价检测：NO 被低估"
```

**触发条件**:
- YES + NO 价格总和偏离 1.0 超过 5%
- 被低估的一方价格 < 0.45

**置信度**: 0.60

---

### 策略 2：动量策略 (Momentum)

**文件位置**: `decision_engine.py` 第 136-157 行

**核心思想**: 寻找高流动性、低价格的市场，捕捉潜在上涨动能。

```python
# 高流动性 + 低价格 = 潜在非对称上涨空间
if liquidity > 5000 and volume > 5000:
    if MIN_PRICE <= yes_price < 0.35:
        return "BUY_YES", 0.55, "动量：低 YES 价格，高流动性"
```

**触发条件**:
- 流动性 > $5,000
- 24 小时交易量 > $5,000
- 价格在 0.10 - 0.35 之间

**置信度**: 0.55

**优化说明**:
- 最低价格阈值设为 0.10（10%），避免极低概率市场
- 从 OpenAI (3.9%) 和 GTA VI (2.4%) 的止损损失中学习

---

### 策略 3：流动性套利 (Liquidity Arbitrage)

**文件位置**: `decision_engine.py` 第 159-182 行

**核心思想**: 寻找订单簿价差小、深度好的市场。

```python
# 寻找紧凑价差和良好深度
if yes_spread_pct < 0.03 and yes_ask < 0.40:
    return "BUY_YES", 0.52, "流动性套利：价差紧凑，卖价低"
```

**触发条件**:
- 买卖价差 < 3%
- 卖价 < 0.40

**置信度**: 0.52

---

### 策略 4：情绪反向策略 (Sentiment Contrarian)

**文件位置**: `decision_engine.py` 第 184-209 行

**核心思想**: 当市场极度一边倒时，考虑反向操作。

```python
# 如果市场极度自信（>90%），考虑反向仓位
# 但只有当失败者至少有 10% 概率时
if yes_price > 0.90 and no_price >= MIN_CONTRARIAN_PRICE:
    return "BUY_NO", 0.50, "反向：市场对 YES 过度自信"
```

**触发条件**:
- 一方价格 > 0.90（市场极度自信）
- 另一方价格 ≥ 0.10（至少 10% 概率）

**置信度**: 0.50

**风险管理**:
- 最低反向价格 0.10，避免极低概率的"黑天鹅"陷阱

---

### 策略 5：非对称价值策略 (Asymmetric Value)

**文件位置**: `decision_engine.py` 第 211-238 行

**核心思想**: 寻找价格适中、流动性良好的被低估市场。

```python
# 寻找非对称机会
# 如果 YES 在最佳区间（15-35%），可能被低估
if MIN_PRICE <= yes_price < 0.35:
    return "BUY_YES", 0.55, "价值：YES 似乎被低估"
```

**触发条件**:
- 流动性 > $5,000
- 交易量 > $1,000
- 价格在 0.15 - 0.35 之间

**置信度**: 0.55

---

### 策略综合评估

**文件位置**: `decision_engine.py` 第 240-259 行

所有策略并行运行，选择**置信度最高**的决策：

```python
for strategy in self.strategies:
    decision, confidence, reasoning = strategy(analysis)
    if confidence > best_confidence:
        best_decision = decision
        best_confidence = confidence
        best_reasoning = reasoning
```

**执行阈值**: 置信度 ≥ 0.50 才会执行交易

---

## 风险管理

### 1. 仓位限制

**配置文件**: `config.py`

| 参数 | 值 | 说明 |
|------|-----|------|
| `MAX_POSITIONS` | 10 | 最大持仓数量 |
| `MAX_POSITION_SIZE` | $500 | 单仓位最大金额 |
| `MAX_TOTAL_EXPOSURE` | $2,500 | 总风险敞口上限 |
| `MIN_TRADE_SIZE` | $10 | 最小交易金额 |

### 2. 风险分层仓位管理

**文件位置**: `agent.py` 第 91-103 行

根据入场价格动态调整仓位大小：

```python
RISK_TIERS = {
    'safe': {      # 入场价 ≥ 0.35
        'min_price': 0.35,
        'max_position': 500,      # 全仓
        'stop_loss': 0.15
    },
    'medium': {    # 0.15 ≤ 入场价 < 0.35
        'min_price': 0.15,
        'max_position': 350,      # 70% 仓位
        'stop_loss': 0.15
    },
    'risky': {     # 0.10 ≤ 入场价 < 0.15
        'min_price': 0.10,
        'max_position': 200,      # 40% 仓位
        'stop_loss': 0.10
    }
}
```

### 3. 止损止盈

**文件位置**: `decision_engine.py` 第 334-354 行

| 参数 | 值 | 说明 |
|------|-----|------|
| `STOP_LOSS_PERCENT` | 15% | 正常仓位止损 |
| `LOW_PROB_STOP_LOSS` | 10% | 低概率仓位止损 |
| `TAKE_PROFIT_PERCENT` | 30% | 止盈 |
| `LOW_PROB_THRESHOLD` | 0.15 | 低概率阈值 |

```python
# 动态止损基于入场价格
if position.avg_price < config.LOW_PROB_THRESHOLD:
    stop_loss = config.LOW_PROB_STOP_LOSS  # 低概率仓位 10% 止损
else:
    stop_loss = config.STOP_LOSS_PERCENT     # 正常仓位 15% 止损

if pnl_pct <= -stop_loss * 100:
    decision_type = "SELL"
    confidence = 0.85
    reasoning = f"止损触发：{pnl_pct:.1f}% 损失"
elif pnl_pct >= config.TAKE_PROFIT_PERCENT * 100:
    decision_type = "SELL"
    confidence = 0.80
    reasoning = f"止盈触发：{pnl_pct:.1f}% 收益"
```

### 4. 止损后冷却期

**配置**: `STOPPED_OUT_COOLDOWN_HOURS = 24`

被止损的市场在 24 小时内不会重新入场，避免情绪性反复交易。

### 5. 敏感话题过滤

**文件位置**: `decision_engine.py` 第 304-319 行

系统自动过滤涉及敏感话题的市场：

```python
SENSITIVE_KEYWORDS = [
    "xi jinping", "xi", "jinping",
    "china", "chinese",
    "taiwan",
    "ccp", "communist",
    "politburo",
    "xijinping"
]
```

---

## 系统架构

### 核心模块

```
trading_system/
├── agent.py              # 主交易代理（入口）
├── decision_engine.py    # AI 决策引擎（5 种策略）
├── portfolio.py          # 投资组合管理
├── market_data.py        # 市场数据获取
├── trade_executor.py     # 交易执行
├── config.py             # 配置参数
├── dashboard.py          # Flask Web 仪表盘
├── updater.py            # 后台价格更新器
└── data/
    ├── portfolio.json    # 投资组合状态
    ├── trades.json       # 交易记录
    ├── decisions.json    # 决策历史
    └── stopped_out.json  # 止损市场记录
```

### 数据流

```
1. agent.py 启动
   ↓
2. market_data.py 扫描市场
   ↓
3. decision_engine.py 分析并决策
   ├─ 5 种策略并行评估
   ├─ 选择置信度最高的决策
   └─ 检查持仓的止损/止盈
   ↓
4. trade_executor.py 执行交易
   ↓
5. portfolio.py 更新状态
   ↓
6. dashboard.py 展示结果
```

---

## 使用指南

### 查看投资组合状态

```bash
python agent.py --status
```

输出示例：
```
============================================================
   POLYMARKET AI TRADING AGENT
   Paper Trading Mode
============================================================

----------------------------------------
PORTFOLIO STATUS
----------------------------------------
  Cash:           $3,123.73
  Positions:      6
  Position Value: $1,841.27
  Total Value:    $4,965.00
  Total P&L:      $-35.00 (-0.70%)
  Total Exposure: $1,750.00
  Trades:         12 (Win rate: 8.3%)
----------------------------------------

----------------------------------------
OPEN POSITIONS
----------------------------------------
  [YES] Will Ukraine qualify for the 2026 FIFA...
       Tokens: 1000.00 @ $0.2750
       Cost: $275.00, Value: $275.00
       P&L: $+0.00 (+0.0%)
  ...
```

### 运行一次完整周期

```bash
python agent.py --once
```

这会：
1. 更新所有持仓价格
2. 打印当前状态
3. 扫描市场机会
4. 分析并决策
5. 执行交易
6. 保存状态

### 启动持续交易

```bash
python agent.py --interval 300  # 每 5 分钟
```

### 启动价格更新器（单独进程）

```bash
python updater.py
```

每 60 秒自动更新所有持仓价格。

### 启动 Web 仪表盘

```bash
python dashboard.py
```

访问 `http://localhost:5001` 查看实时状态。

### 手动关闭敏感持仓

如果需要手动关闭某个持仓：

```bash
python remove_sensitive_position.py
```

---

## 市场选择标准

**配置文件**: `config.py` 第 29-32 行

| 参数 | 值 | 说明 |
|------|-----|------|
| `MIN_LIQUIDITY` | $2,000 | 最低流动性 |
| `MIN_VOLUME_24H` | $500 | 最低 24 小时交易量 |
| `MAX_END_DATE_DAYS` | 365 | 最长到期时间（天） |
| `MARKET_ANALYSIS_COUNT` | 50 | 每周期分析市场数 |

---

## 决策日志

所有决策都保存在 `data/decisions.json`，包含：

- `decision_id`: 决策 ID
- `timestamp`: UTC 时间戳
- `market_id`: 市场 ID
- `market_question`: 市场问题
- `decision`: 决策类型（BUY_YES/BUY_NO/SELL/HOLD）
- `confidence`: 置信度 (0-1)
- `reasoning`: 决策理由
- `market_data`: 市场数据分析
- `portfolio_state`: 投资组合状态
- `executed`: 是否已执行
- `result`: 执行结果

---

## 常见问题

### Q: 这是真实交易吗？

A: 不是。这是**模拟交易系统**，使用虚拟资金，不会产生真实盈亏。

### Q: 如何调整策略参数？

A: 编辑 `config.py` 文件修改配置，或直接修改 `decision_engine.py` 中的策略逻辑。

### Q: 系统为什么不交易？

A: 可能原因：
1. 置信度阈值未达到（< 0.50）
2. 已达最大持仓数（10 个）
3. 总风险敞口已达上限（$2,500）
4. 没有符合条件的市场

### Q: 如何重置投资组合？

A: 删除 `data/portfolio.json` 文件，系统会自动创建新的组合。

---

## 总结

这个交易系统的核心优势：

1. ✅ **多策略融合** - 5 种策略互补，避免单一策略偏差
2. ✅ **严格风控** - 多层风险保护，从实战损失中学习优化
3. ✅ **完整记录** - 所有决策可追溯，便于策略优化
4. ✅ **实时监控** - Web 仪表盘直观展示状态
5. ✅ **敏感过滤** - 自动规避不适宜交易的话题

**记住**: 这是一个学习和研究工具，预测市场 inherently 具有高度不确定性，请谨慎使用！
