# 候选报告：GFS 天气基线 + 滚动校准 (CR-20260602-001)

## 1. 报告元数据

| 字段 | 内容 |
|------|------|
| **报告编号** | CR-20260602-001 |
| **日期** | 2026-06-02 |
| **策略名称** | gfs_weather_rolling_calib_v1 |
| **父策略** | GFS-Weather-Baseline-v1 |
| **作者** | AI Agent |
| **状态** | 🔄 REVIEW |

## 2. 改动摘要

**核心假设**：GFS 预报存在城市级别的系统性偏置（如香港 GFS 偏冷 +0.89°C），通过滚动校准（rolling 20 天窗口）可以动态修正 bias 和 sigma，从而提高概率估计精度。

### 2.1 改动清单

| # | 改动点 | 影响范围 | 回测兼容性 |
|---|--------|----------|------------|
| 1 | 增加 rolling calibration | `src/data/gfs_prediction.py` | 需历史已解决市场数据 |
| 2 | 校准结果写入 prediction.extra | `src/data/prediction_interface.py` | 向后兼容 |
| 3 | run_standard() 使用 pluggable source | `src/data/weather_backtester.py` | 向后兼容 |

## 3. 理论依据

### 3.1 为什么这个改动应该有 edge？

GFS 是全球数值天气预报模型，分辨率约 13km。对于城市尺度（如香港 VHHH 站），GFS 存在系统性偏置：
- **城市热岛效应**：GFS 未完全解析城市微气候，导致夜间温度偏低
- **地形简化**：复杂地形（如香港山地）在 GFS 中被平滑化
- **海温强迫误差**：近海城市的海表温度强迫存在滞后

这些偏置在不同城市稳定存在（文献支持），因此可用历史已解决市场数据滚动估计 bias 和 residual sigma，将正态模型的均值和方差校准到本地气候统计。

### 3.2 与当前 SOTA 的差异

| 维度 | 当前 SOTA (Baseline) | 本候选 |
|------|----------------------|--------|
| Bias | 固定 0（无校准） | 滚动 20 天动态估计 |
| Sigma | 固定 1.79°C | 滚动 20 天动态估计 |
| 适用城市 | 仅香港有先验值 | 所有 21 城自动校准 |
| 回测方法 | run() hardcoded | run_standard() pluggable |

## 4. 消融实验设计

### 4.1 实验配置

```yaml
train_window: 18m
test_window: 3m
step_window: 3m
holdout: 12m
cities: [hong-kong, new-york, london]
variables: [temperature_2m_max]
min_edge: 0.05
amount_per_trade: 5.0
```

### 4.2 实验组

| 组名 | 描述 |
|------|------|
| **Baseline** | GFS raw，固定 bias=0, sigma=1.79 |
| **Candidate** | + rolling bias/sigma calibration (20d window) |

> **注意**：由于数据时间范围限制（2024-2026），上述 18m/3m/3m 配置可能产生较少 fold。实际运行时可缩短 train_window 至 3-6 个月。

## 5. Walk-Forward 结果（待执行）

> 执行以下命令填充：
> ```bash
> python3 -m research.walk_forward --city hong-kong --start 2025-01-01 --end 2026-05-31 --train-months 6 --test-months 2 --step-months 2
> ```

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
| London | — | — | — |

## 6. 因子 IC 分析（待执行）

> 执行以下命令填充：
> ```bash
> python3 -m research.factor_ic --city all --start 2025-01-01 --end 2026-05-31
> ```

### 6.1 整体 IC

| 指标 | 值 |
|------|-----|
| IC_mean | — |
| IC_std | — |
| IC_sharpe (IR) | — |
| IC > 0 比例 | — |

### 6.2 IC 衰减（按领先时间）

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
| 新城市 calibration N < 5，fallback 参数不准 | 高 | 中 | 仅交易 calibration N ≥ 5 的市场 |
| GFS 模型升级改变统计特性 | 中 | 高 | 缩短 calibration 窗口，快速适应 |
| 天气市场流动性不足 | 中 | 中 | 已有 min_liquidity > $2000 过滤 |
| 降水预报 sigma 过宽 | 高 | 中 | 单独校准降水 sigma，不共用温度参数 |

### 7.2 尾部风险

- **GFS 模型重大升级**（如 GFSv17）：可能导致历史 calibration 失效。缓解：监控 calibration N 的稳定性，突变时切换到更短窗口。
- **Polymarket 天气市场关闭**：单一平台风险。缓解：研究其他预测市场（Kalshi、Betfair）。
- **极端天气事件**（台风、热浪）：GFS 在极端事件上的误差分布可能与常态不同。缓解：在 calibration 中剔除 3-sigma 异常值。

## 8. 结论与建议

### 8.1 是否推荐替换 SOTA？

- [ ] 是 — 待 walk-forward 和 IC 结果填充后决定
- [ ] 否 — 当前为框架性报告，等待数据验证
- [ ] 部分 — 仅在有足够 calibration 数据的城市有效

### 8.2 后续行动

1. **执行 walk_forward.py**：验证 rolling calibration 在各城市的实际表现
2. **执行 factor_ic.py**：确认 GFS 信号的预测稳定性
3. **扩展降水市场**：GFSPrecipSource 已完成，需数据回填后验证
4. **21 城全面校准**：运行 `cli.py calibrate --city all` 生成各城市先验参数

## 9. 附录

### 9.1 运行命令

```bash
# 复现 walk-forward
python3 -m research.walk_forward --city hong-kong --start 2025-01-01 --end 2026-05-31 --train-months 6 --test-months 2 --step-months 2 --out-dir research/output/20260602_wf

# 复现 factor IC
python3 -m research.factor_ic --city all --start 2025-01-01 --end 2026-05-31 --out-dir research/output/20260602_ic

# 校准所有城市
python3 cli.py calibrate --city all --end 2026-05-31
```

### 9.2 原始数据路径

- `data/weather_markets.db` — 市场定义与价格历史
- `data/gfs_forecasts.db` — GFS 预报历史
- `data/calibration.json` — 校准参数输出
- `research/output/20260602_wf/` — walk-forward 结果
- `research/output/20260602_ic/` — IC 分析结果

---
**报告生成时间**: 2026-06-02 UTC
**复现承诺**: 本报告所有结果可通过上述命令完全复现
