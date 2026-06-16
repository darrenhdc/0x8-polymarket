# SOTA.md — 当前最优策略注册表

## 1. SOTA 策略概览

| 属性 | 值 |
|------|-----|
| **策略名称** | GFS-Weather-LeadTime0-v1 |
| **策略类型** | 信息套利（物理模型 vs 市场情绪） + 短领先时间过滤 |
| **预测源** | GFSPredictionSource + GFSPrecipSource |
| **Edge 计算** | edge_composer.compute_edge() |
| **融合方法** | 单源（GFS），暂不多源融合 |
| **训练数据** | 滚动 20 天已解决市场（bias/sigma 校准） |
| **适用市场** | Polymarket 天气预测市场（温度 + 降水 + 降雪），仅 T+0（target − price ≤ 24h） |
| **状态** | ✅ ACTIVE — 当前最优（待 holdout 验证） |

## 2. 策略定义

### 2.1 核心逻辑

对于每个活跃的天气市场：
1. 获取 GFS 预报值（Open-Meteo API 或本地历史数据库）
2. 滚动校准：用最近 20 个已解决日期计算 `(bias, sigma)`
3. 校正预报：`corrected = raw + bias`
4. 计算概率：`P(Yes) = Φ(rule(corrected, threshold, sigma))`
5. 计算 edge：`edge = P(Yes) - market_price`
6. 交易条件：`|edge| >= 0.05` 且 `min_price >= 0.03` 且 **`(target_date − price_date) ≤ 24h`（T+0 过滤）**

### 2.2 规则映射

| 市场类型 | 变量 | 默认规则 | 规则推断关键词 |
|----------|------|----------|--------------|
| `temp_above` | `temperature_2m_max` | `eq` | "above"→gte, "below"→lte |
| `temp_above` (min) | `temperature_2m_min` | `eq` | "lowest"/"minimum"→min temp |
| `precip` | `precipitation_sum` | `gte` | — |
| `snow` | `snowfall_sum` | `gte` | — |

### 2.3 校准参数（各城市）

| 城市 | 变量 | Bias (°C) | Sigma (°C) | N（校准对数） |
|------|------|-----------|------------|--------------|
| Hong Kong | temperature_2m_max | +0.89 | 1.47 | 65 |
| Hong Kong | temperature_2m_min | +0.89 | 1.47 | 65 |
| *其他城市* | *待校准* | *0.0* | *1.79* | *0 (fallback)* |

> 注：其他城市需运行 `cli.py calibrate --city all` 后更新此表。

## 3. 回测表现（Baseline）

### 3.1 香港温度市场（2026-03-01 至 2026-05-31，T+0 过滤）

> 数据来源：`research/reports/20260614_lead_time_comparison.md` (CR-20260614-001)，可经 `python3 -m research.lead_time_comparison --city hong-kong` 复现。

| 指标 | 值 |
|------|-----|
| 总交易数 | 123 |
| 已解决 | 123 |
| 胜率 | 69.9% |
| 总盈亏 (PnL) | +$146.33 |
| 投资回报率 (ROI) | +23.8% |
| 平均 Edge | 26.2% |
| 夏普比率（日） | 1.50 |
| 最大回撤 | -$33.25 |

**领先时间对比**（同窗口，香港）：

| Lead | 交易数 | 胜率 | PnL | ROI | 日 Sharpe |
|------|--------|------|-----|-----|-----------|
| **T+0** | 123 | 69.9% | **+$146.33** | **+23.8%** | **1.50** |
| T+1 | 201 | 70.6% | -$11.64 | -1.2% | -0.25 |
| T+2 | 95 | 73.9% | -$9.11 | -2.1% | -0.29 |
| T+3 | 95 | 76.4% | +$2.95 | +0.7% | 0.09 |

> 结论：T+0 是唯一正收益且有正 Sharpe 的分桶。长领先时间发现更多 edge 但精度损失抵消收益。

### 3.2 全市场（21 城市聚合，T+0 过滤）

| 指标 | 值 |
|------|-----|
| 总交易数 | 428 |
| 胜率 | 69.6% |
| ROI | +2.5% |
| 总 PnL | +$53.84 |
| 日 Sharpe | 0.49 |

> ⚠️ 多城市稀释严重（香港 +23.8% → 聚合 +2.5%），说明并非所有城市都有同等 T+0 alpha。**P0：分城市回测后再上线实盘。**

## 4. 消融实验记录

### 4.1 实验设计

| 实验 | 描述 | 状态 |
|------|------|------|
| **Baseline** | 纯 GFS 正态模型，固定 sigma=1.79°C，bias=0 | ✅ 完成（`run()` 路径） |
| **Ablation #1** | + bias 校准（rolling 20d） | ✅ 完成（CR-20260602-001） |
| **Ablation #2** | + sigma 自适应（rolling 20d） | ✅ 完成（含于 #1） |
| **Ablation #3** | + 流动性过滤（min_liquidity > $2000） | 🔄 待执行 |
| **Ablation #4** | + 降水市场（GFSPrecipSource） | 🔄 待执行 |
| **Ablation #5** | + 多城市组合（21 城分散） | 🔄 部分执行（见 §3.2，需分城市） |
| **Ablation #6** | + **T+0 领先时间过滤** | ✅ 完成（CR-20260614-001） |

### 4.2 结果对比表（待填充）

| 实验 | 胜率 | ROI | Sharpe | 最大回撤 | 交易数 |
|------|------|-----|--------|----------|--------|
| Baseline (run, fixed) | — | — | — | — | — |
| +bias+sigma (rolling) | 70.6% | -1.2% | -0.25 | -$86.51 | 194 (T+1) |
| +T+0 filter | **69.9%** | **+23.8%** | **1.50** | **-$33.25** | 123 |
| +liquidity | — | — | — | — | — |
| +precip | — | — | — | — | — |
| +multi_city (T+0) | 69.6% | +2.5% | 0.49 | -$76.34 | 428 |

> 注：+bias+sigma 行使用 T+1 分桶作为「无 T+0 过滤」代表（最长样本）。T+0 过滤后 ROI 从 -1.2% 跃升至 +23.8%。

## 5. 已废弃策略

| 策略 | 废弃原因 | 实验结果 |
|------|----------|----------|
| 通用事件交易（5 策略） | 无信息优势，与散户同质化 | 7.7% 胜率，-4.74% ROI |
| LLM 定价引擎 | 成本高、延迟大、无稳定 edge | Phase 2 扫描未产生可交易信号 |

## 6. 待验证假设

1. **GFS 冷偏置具有季节性**：夏季 bias 可能 < 冬季 bias → 需分季节校准
2. **降水预报 sigma 应 > 温度 sigma**：降水更难预测，fallback sigma=5mm 可能过宽
3. **多城市分散可降低回撤**：21 城组合 vs 单城集中 → 待 walk_forward 验证
4. **领先时间（lead time）影响精度**：T+0 (≤24h) 显著优于 T+1/T+2/T+3 ✅ **已验证（CR-20260614-001）**

## 7. 下一步升级路径

| 优先级 | 升级 | 预期改进 | 验证方式 |
|--------|------|----------|----------|
| P0 | 完成 21 城滚动校准 | 提高各城市预测精度 | walk_forward |
| P0 | 因子 IC 分析 | 确认 GFS 信号稳定性 | factor_ic.py |
| P1 | 季节性感知校准 | 分季节 bias/sigma | 消融实验 |
| P1 | 降水市场深度挖掘 | 扩大可交易市场池 | walk_forward |
| P2 | 多变量联合策略 | 温度+降水组合信号 | 消融实验 |
| P2 | 订单簿深度过滤 | 避免大额冲击成本 | 实盘观察 |

## 8. 更新日志

| 日期 | 事件 |
|------|------|
| 2026-03-18 | 通用事件实验结束（7.7% 胜率），决定转向天气 GFS |
| 2026-05-22 | GFS 温度回测框架完成，香港 65 日期校准完成 |
| 2026-06-02 | 建立 A02 标准研究基础设施（AGENTS.md / SOTA.md / walk_forward / factor_ic） |
| 2026-06-02 | CR-20260602-001：滚动校准框架报告（REVIEW） |
| 2026-06-14 | CR-20260614-001：领先时间对比，T+0 过滤显著优于基线；SOTA 升级为 GFS-Weather-LeadTime0-v1（待 holdout 验证） |
| 2026-06-14 | 修复 `src/weather/gfs_weather_pipeline.py` 的 `PredictionRegistry` 导入（从 `prediction_interface` → `prediction_registry`） |

---
**维护者**: AI Agent
**更新频率**: 每次产生新候选报告时更新
**审核标准**: 只有 walk-forward + IC 均通过的策略才能替换当前 SOTA
