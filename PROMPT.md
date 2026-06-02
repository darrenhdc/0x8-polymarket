你在 /Users/darrencui/defi_labs/A05_polymarket_trading 工作，Polymarket 预测市场交易系统。

## 会话初始化

读取以下文件了解上下文：
1. `docs/EXPERIMENT_SUMMARY.md` — 纸面交易实验结果
2. `docs/POLYMARKET_TRADING_TUTORIAL.md` — 中文教程
3. `src/core/config.py` — 全局配置
4. `src/data/weather_backtester.py` — 天气回测引擎
5. `src/data/prediction_interface.py` — 预测源接口
6. `src/weather/gfs_weather_source.py` — GFS 天气模型

然后输出会话启动摘要。

## 项目评估（来自 A02/A04 资深审查）

**成熟度：Level 3+（研究流水线 → 接近 Level 4）**
- 13,157 行 Python，12 个子包，统一 CLI，10 条风控规则，可插拔预测源设计
- 但缺乏 A02 标准的 AGENTS.md / SOTA.md / walk_forward / 候选报告体系

**两条交易线：**
- 通用事件交易（LLM + 5 策略）：❌ 已死亡。$5,000 纸面 7 天 -4.74%，胜率 7.7%。砍掉。
- 天气预测（GFS 模型）：✅ 有真实 edge。物理模型驱动的信息套利——GFS 预报 vs Polymarket 散户情绪。

## 任务 — 按优先级

### P0：建立研究基础设施（对标 A02 标准）

1. 创建 `AGENTS.md` — 项目规则 + 进度锚点 + 研究 SOP
2. 创建 `research/reports/SOTA.md` — 天气预测策略的 SOTA 注册表
3. 创建 `research/reports/TEMPLATE.md` — 候选报告模板
4. 创建 `research/walk_forward.py` — 天气回测的 WF 验证框架
   - 使用 `src/data/weather_backtester.py` 作为基础
   - 18m train / 3m test / 3m step / 12m holdout
   - 适配多城市（21 个城市独立 WF）
5. 创建 `research/factor_ic.py` — 对 GFS 预测因子计算滚动 IC
   - `IC = corr(GFS_prob, actual_outcome)` 滚动 20 天窗口
   - 计算 IC_mean / IC_std / Sharpe IC
   - 分城市、分变量（temperature / precipitation）单独算

### P1：产出一份候选报告

6. 对 GFS 天气模型本身做消融验证：
   - 基线：纯 GFS 正态模型（当前默认）
   - 消融#1：加 bias 校准（calibration.json）
   - 消融#2：加 sigma 自适应
   - 消融#3：加流动性过滤（min_liquidity）
7. 产出第一份候选报告：`research/reports/YYYYMMDD_gfs_weather_baseline.md`

### P2：运营基础设施

8. 创建 `monitor/heartbeat.py` — CLI/JSON/HTTP 健康检查
   - 检查 `trading_system/data/portfolio.json` 新鲜度
   - 检查 Polymarket API 可达性
   - 检查 Open-Meteo API 可达性
9. 创建 CLI 工具：
   - `./performance` — 读取 portfolio.json + trades.json，显示实时 PnL
   - `./sota` — 当前最优天气策略简报
   - `./archived` — 策略演化史

### P3：策略增强（P0-P2 完成后）

10. 多城市组合优化 — 21 个城市的风控分散
11. 温度 + 降水双变量联合策略
12. 季节性效应分析（GFS 在夏季/冬季的预测精度差异）

## 硬性约束

- 通用事件交易线（LLM + 5 策略）不要再投入时间——7.7% 胜率不值得修
- 不删除旧代码，移入 `_archived/` 作为参考
- 所有新研究遵循 A02 的 8 阶段 SOP
- GFS 模型 sigma=0.7°C 是初始假设——必须在回测中校准
- Paper trading 模式保持在 `trading_system/data/` 下运行
- 不要修改 `trading_system/.env` 中的私钥或 API 密钥

## 这个项目的独特价值

这是五个项目中唯一一个**不依赖价格技术分析**的策略方向。天气预测市场的信息优势来自物理模型（GFS）而非市场数据——这不是统计套利，是信息套利。GFS 是 NOAA 免费提供的全球预报模型，你的 edge 来自把物理预测转化为概率定价，而 Polymarket 上的散户在下注时没有这个模型。这是真正可持续的 alpha 来源——不会因为更多人来用就消失。
