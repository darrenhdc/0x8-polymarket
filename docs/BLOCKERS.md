# 请求: Polymarket 天气预测交易系统 — 阻塞问题清单

## 背景
信息套利策略: GFS 物理模型 vs Polymarket 散户定价。
核心逻辑已跑通(T+0 回测 Hong Kong 68天 +$1,623, 胜率40.8%, ROI+45%)。
5个阻塞问题需要解决才能进入实盘。

## 阻塞 #1: T+0 回测变量bug (已修复, 需验证)
- 问题: weather_backtester.py 总是用 temperature_2m_max, 即使市场是 "lowest temperature"
- 修复: 添加了 infer_market_variable() 自动选 min/max; backfill 也拉 min 数据
- 验证: 修复后结果几乎相同(HK max/min 高度相关)
- 未完成: ERA5 的 temperature_2m_min 观测数据回填失败

## 阻塞 #2: 价格数据管线断了 (P0)
- 症状: 5/25之后的市场没有价格数据(price_history表)
- 原因: backfill pipeline 调用 PolymarketHistoryCollector 缺少 ingest_price_history()
- 影响: 无法回测新数据, 无法获取实时市场价格
- 需要: 修复或重写价格拉取逻辑

## 阻塞 #3: 缺少多步GFS预报 (P2)
- 现状: 所有35,310行 GFS 都是 lead_time=0h (当天预报)
- 缺失: T+1/T+2/T+3 等多步提前预报
- 影响: 回测只能模拟T+0, 无法验证"提前入场edge更大"假设
- Open-Meteo 历史 API 不支持多步回放
- 需要: 评估 NOAA GFS grib2 归档方案

## 阻塞 #4: 回测过度依赖单一 outlier (P1)
- 发现: $1,424 (88%总利润) 来自一笔交易 (5/6 24°C, Mkt=0.4%, GFS=22%)
- 去掉后总PnL = +$199 (ROI 5.5%)
- 建议: 跑 London (108天) + New York (80天) 回测做交叉验证

## 阻塞 #5: 订单簿太薄, 纸面交易不可行 (P3)
- CLOB: best bid=$0.01, best ask=$0.99, 几乎无真实交易
- Gamma API 不返回这些天气市场的价格
- 结论: 纸面交易暂时不可行, 回测更可靠

## 当前CLI (13个子命令)
backfill | signals | backtest | calibrate | coverage | performance | sota | status | walk-forward | factor-ic | trade | paper-status | paper-close

## 优先级
P0: 价格管线 (#2) — 基石
P1: 交叉验证 (#4) — 确认策略非运气
P2: 多步预报 (#3) — 解锁T+1回测
P3: 纸面交易 (#5) — 实盘前最后一步
