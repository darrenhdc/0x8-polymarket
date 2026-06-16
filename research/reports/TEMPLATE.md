# 候选报告模板（Candidate Report Template）

## 1. 报告元数据

| 字段 | 内容 |
|------|------|
| **报告编号** | CR-YYYYMMDD-NNN |
| **日期** | YYYY-MM-DD |
| **策略名称** | [简明命名，如 gfs_weather_bias_calib_v2] |
| **父策略** | [继承自哪个 SOTA，如 GFS-Weather-Baseline-v1] |
| **作者** | AI Agent |
| **状态** | 🔄 REVIEW / ✅ APPROVED / ❌ REJECTED |

## 2. 改动摘要

一句话描述本次改动的核心假设：
> [例如：在 GFS 基线模型上增加滚动 bias 校准，假设各城市 GFS 冷偏置稳定存在]

### 2.1 改动清单

| # | 改动点 | 影响范围 | 回测兼容性 |
|---|--------|----------|------------|
| 1 | [具体改动] | [文件/模块] | [是/否需要重新跑数据] |

## 3. 理论依据

### 3.1 为什么这个改动应该有 edge？

[解释假设背后的逻辑。例如：GFS 模型存在系统性冷偏置，因为……]

### 3.2 与当前 SOTA 的差异

| 维度 | 当前 SOTA | 本候选 |
|------|-----------|--------|
| [维度1] | [值] | [值] |

## 4. 消融实验设计

### 4.1 实验配置

```yaml
train_window: 18m
test_window: 3m
step_window: 3m
holdout: 12m
cities: [hong-kong, new-york, london]  # 或 all
variables: [temperature_2m_max, precipitation_sum]
min_edge: 0.05
amount_per_trade: 5.0
```

### 4.2 实验组

| 组名 | 描述 |
|------|------|
| **Baseline** | 当前 SOTA 配置 |
| **Candidate** | 本次改动 |
| **Ablation-1** | [如有额外消融] |

## 5. Walk-Forward 结果

### 5.1 整体表现

| 指标 | Baseline | Candidate | 提升 |
|------|----------|-----------|------|
| 总交易数 | — | — | — |
| 胜率 | — | — | — |
| ROI | — | — | — |
| Sharpe | — | — | — |
| 最大回撤 | — | — | — |
| Calmar | — | — | — |

### 5.2 分城市表现

| 城市 | 交易数 | 胜率 | ROI |
|------|--------|------|-----|
| Hong Kong | — | — | — |
| New York | — | — | — |

### 5.3 分变量表现

| 变量 | 交易数 | 胜率 | ROI |
|------|--------|------|-----|
| temperature_2m_max | — | — | — |
| precipitation_sum | — | — | — |

## 6. 因子 IC 分析

### 6.1 整体 IC

| 指标 | 值 |
|------|-----|
| IC_mean | — |
| IC_std | — |
| IC_sharpe (IR) | — |
| IC > 0 比例 | — |

### 6.2 分城市 IC

| 城市 | IC_mean | IC_std | IC_sharpe |
|------|---------|--------|-----------|
| — | — | — | — |

### 6.3 IC 衰减（按领先时间）

| 领先时间 | IC_mean |
|----------|---------|
| ≤ 24h | — |
| 24-48h | — |
| 48-72h | — |
| > 72h | — |

## 7. 风险评估

### 7.1 已知风险

| 风险 | 可能性 | 影响 | 缓解措施 |
|------|--------|------|----------|
| [风险1] | [高/中/低] | [高/中/低] | [措施] |

### 7.2 尾部风险

[描述极端情况下的表现，如 GFS 模型重大升级、市场关闭等]

## 8. 结论与建议

### 8.1 是否推荐替换 SOTA？

- [ ] 是 — 显著优于当前 SOTA，IC 和 walk-forward 均通过
- [ ] 否 — 未达到替换阈值
- [ ] 部分 — 仅在某些子集上有效，建议作为变体保留

### 8.2 后续行动

1. [行动1]
2. [行动2]

## 9. 附录

### 9.1 运行命令

```bash
# 复现 walk-forward
python3 -m research.walk_forward [参数]

# 复现 factor IC
python3 -m research.factor_ic [参数]
```

### 9.2 原始数据路径

- `data/weather_markets.db`
- `data/gfs_forecasts.db`
- `research/output/YYYYMMDD_*.csv`

---
**报告生成时间**: YYYY-MM-DD HH:MM UTC
**复现承诺**: 本报告所有结果可通过上述命令完全复现
