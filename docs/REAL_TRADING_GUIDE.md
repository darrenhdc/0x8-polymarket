# Polymarket 真实交易分步指南

## ⚠️ 重要警告

**当前系统仅支持模拟交易（Paper Trading），不支持真实资金交易！**

本文档说明如何将系统修改为支持真实资金交易，以及使用真实资金前的所有必要准备。

---

## 📋 前置条件

### 1. Polymarket 账户准备
- [ ] 在 [polymarket.com](https://polymarket.com) 注册账户
- [ ] 完成 KYC 身份验证
- [ ] 存入 USDC 或其他支持的加密货币
- [ ] 获取 API 密钥和密钥

### 2. 技术准备
- [ ] Python 3.9+ 环境
- [ ] Git 仓库本地克隆
- [ ] 虚拟环境设置
- [ ] 依赖包安装（`pip install -r requirements.txt`）

### 3. 风险准备
- [ ] 只使用您能承受损失的资金
- [ ] 制定资金管理计划
- [ ] 设定最大单笔损失限额
- [ ] 准备手动干预预案

---

## 🔐 步骤 1：获取 Polymarket API 凭证

### 1.1 访问 Polymarket 开发者设置

1. 登录 [polymarket.com](https://polymarket.com)
2. 进入账户设置 → API Keys
3. 创建新的 API Key
4. 保存以下信息（不要与他人分享）：
   - API Key
   - API Secret
   - Passphrase

### 1.2 创建环境变量文件

在项目根目录创建 `.env` 文件：

```bash
# .env 文件
POLYMARKET_API_KEY="your_api_key_here"
POLYMARKET_API_SECRET="your_api_secret_here"
POLYMARKET_PASSPHRASE="your_passphrase_here"

# 交易模式设置
PAPER_TRADING=false  # 设置为 false 启用真实交易

# 安全设置
MAX_DAILY_LOSS=100.0  # 单日最大损失限额
MAX_POSITION_SIZE=50.0  # 单笔最大仓位
```

### 1.3 安装环境变量支持

```bash
pip install python-dotenv
```

---

## 🔧 步骤 2：修改配置文件

### 2.1 更新 config.py

在 `config.py` 开头添加环境变量支持：

```python
"""
Polymarket AI Trading System Configuration
"""
import os
from dotenv import load_dotenv

# 加载环境变量
load_dotenv()

# 交易模式
PAPER_TRADING = os.getenv('PAPER_TRADING', 'true').lower() == 'true'

# API 凭证（仅真实交易需要）
POLYMARKET_API_KEY = os.getenv('POLYMARKET_API_KEY', '')
POLYMARKET_API_SECRET = os.getenv('POLYMARKET_API_SECRET', '')
POLYMARKET_PASSPHRASE = os.getenv('POLYMARKET_PASSPHRASE', '')

# 安全限额
MAX_DAILY_LOSS = float(os.getenv('MAX_DAILY_LOSS', '100.0'))
```

### 2.2 调整风险参数（针对真实交易）

修改 `config.py` 中的交易参数：

```python
# 更保守的真实交易参数
INITIAL_CAPITAL = 500.0  # 从 $5000 降低到 $500 开始
MAX_POSITION_SIZE = 50.0  # 单笔降低到 $50
MAX_TOTAL_EXPOSURE = 250.0  # 总风险降低到 $250
MIN_TRADE_SIZE = 10.0
MAX_POSITIONS = 5  # 降低持仓数限制
```

---

## 📝 步骤 3：实现真实交易执行器

### 3.1 创建真实交易 API 客户端

创建新文件 `polymarket_api.py`：

```python
"""
Polymarket Real Trading API Client
"""
import os
import time
import hmac
import hashlib
import base64
import requests
from typing import Dict, Optional
from datetime import datetime
from dotenv import load_dotenv

load_dotenv()


class PolymarketTradingAPI:
    """Polymarket 真实交易 API 客户端"""

    def __init__(self):
        self.api_key = os.getenv('POLYMARKET_API_KEY')
        self.api_secret = os.getenv('POLYMARKET_API_SECRET')
        self.passphrase = os.getenv('POLYMARKET_PASSPHRASE')
        self.base_url = "https://api.polymarket.com"
        self.session = requests.Session()

    def _generate_signature(self, timestamp: str, method: str,
                          request_path: str, body: str = "") -> str:
        """生成 API 请求签名"""
        message = timestamp + method + request_path + body
        hmac_obj = hmac.new(
            self.api_secret.encode('utf-8'),
            message.encode('utf-8'),
            hashlib.sha256
        )
        return base64.b64encode(hmac_obj.digest()).decode('utf-8')

    def _get_headers(self, method: str, request_path: str, body: str = "") -> Dict:
        """生成带签名的请求头"""
        timestamp = str(int(time.time() * 1000))
        signature = self._generate_signature(timestamp, method, request_path, body)

        return {
            'OK-ACCESS-KEY': self.api_key,
            'OK-ACCESS-SIGN': signature,
            'OK-ACCESS-TIMESTAMP': timestamp,
            'OK-ACCESS-PASSPHRASE': self.passphrase,
            'Content-Type': 'application/json'
        }

    def get_balance(self) -> Optional[Dict]:
        """获取账户余额"""
        request_path = "/api/v1/account/balances"
        headers = self._get_headers("GET", request_path)

        try:
            response = self.session.get(
                self.base_url + request_path,
                headers=headers,
                timeout=30
            )
            response.raise_for_status()
            return response.json()
        except Exception as e:
            print(f"获取余额失败: {e}")
            return None

    def place_order(self, market_id: str, token_id: str,
                   side: str, size: float, price: float) -> Optional[Dict]:
        """
        下单

        Args:
            market_id: 市场 ID
            token_id: 代币 ID
            side: "BUY" 或 "SELL"
            size: 代币数量
            price: 价格（0.0-1.0）
        """
        request_path = "/api/v1/orders"

        order_data = {
            'marketId': market_id,
            'tokenId': token_id,
            'side': side,
            'size': str(size),
            'price': str(price),
            'type': 'LIMIT'
        }

        body = json.dumps(order_data)
        headers = self._get_headers("POST", request_path, body)

        try:
            response = self.session.post(
                self.base_url + request_path,
                headers=headers,
                json=order_data,
                timeout=30
            )
            response.raise_for_status()
            return response.json()
        except Exception as e:
            print(f"下单失败: {e}")
            return None

    def cancel_order(self, order_id: str) -> bool:
        """取消订单"""
        request_path = f"/api/v1/orders/{order_id}"
        headers = self._get_headers("DELETE", request_path)

        try:
            response = self.session.delete(
                self.base_url + request_path,
                headers=headers,
                timeout=30
            )
            response.raise_for_status()
            return True
        except Exception as e:
            print(f"取消订单失败: {e}")
            return False

    def get_open_orders(self, market_id: str = None) -> Optional[Dict]:
        """获取未成交订单"""
        request_path = "/api/v1/orders"
        params = {}
        if market_id:
            params['marketId'] = market_id

        headers = self._get_headers("GET", request_path)

        try:
            response = self.session.get(
                self.base_url + request_path,
                headers=headers,
                params=params,
                timeout=30
            )
            response.raise_for_status()
            return response.json()
        except Exception as e:
            print(f"获取订单失败: {e}")
            return None


import json
```

### 3.2 修改 trade_executor.py 支持真实交易

更新 `trade_executor.py` 中的 `TradeExecutor` 类：

```python
# 在文件顶部添加
from polymarket_api import PolymarketTradingAPI

class TradeExecutor:
    """Executes and tracks trades (paper or real)"""

    def __init__(self, portfolio: PortfolioManager):
        self.portfolio = portfolio
        self.market_data = MarketData()
        self.trades: List[Trade] = self._load_trades()
        self.stopped_out: Dict[str, str] = self._load_stopped_out()

        # 真实交易 API（仅在非模拟模式时初始化）
        self.real_api = None
        if not config.PAPER_TRADING:
            self.real_api = PolymarketTradingAPI()
            print("⚠️  真实交易模式已启用！")
        else:
            print("📝  模拟交易模式")

    # ... 其他方法保持不变 ...

    def execute_buy(self, market_id: str, market_question: str,
                    outcome: str, amount_usd: float,
                    reasoning: str, confidence: float) -> Optional[Trade]:
        """执行买单（模拟或真实）"""

        # ... 原有的模拟交易逻辑 ...

        # 如果是真实交易模式
        if not config.PAPER_TRADING and self.real_api:
            print(f"⚠️  执行真实买单: ${amount_usd:.2f}")

            # 获取代币 ID
            market = self.market_data.scanner.api.get_market_by_id(market_id)
            # ... 解析代币 ID ...

            # 真实下单（此处需要完善）
            # real_order = self.real_api.place_order(...)

            # 警告：此处仅为示例，实际使用前需要充分测试！
            print("⚠️  真实交易功能尚未完全实现，请先在测试网充分测试！")
            return None

        # ... 原有的模拟交易执行 ...
```

---

## 🧪 步骤 4：测试流程

### 4.1 始终先进行模拟交易测试

```bash
# 确保 PAPER_TRADING=true
python agent.py --once
```

### 4.2 使用极小金额进行真实测试

1. 修改配置使用极小金额：
   - MAX_POSITION_SIZE = 5.0
   - MAX_TOTAL_EXPOSURE = 25.0

2. 运行一次并验证：
   ```bash
   python agent.py --once
   ```

3. 检查 Polymarket 网站确认订单

### 4.3 监控和验证

- [ ] 验证余额正确更新
- [ ] 验证订单正确提交
- [ ] 验证成交后持仓正确
- [ ] 验证止损功能正常工作

---

## 🛡️ 安全措施（必须）

### 5.1 每日损失限制

```python
# 在 config.py 添加
DAILY_LOSS_FILE = os.path.join(DATA_DIR, "daily_loss.json")

def check_daily_loss_limit(current_loss: float) -> bool:
    """检查是否超过每日损失限额"""
    if current_loss >= MAX_DAILY_LOSS:
        print(f"⚠️  每日损失限额已达到: ${MAX_DAILY_LOSS}")
        return False
    return True
```

### 5.2 紧急停止按钮

创建 `emergency_stop.py`：

```python
#!/usr/bin/env python3
"""
紧急停止脚本 - 立即停止所有交易并平仓
"""
import os
import sys
import json

sys.path.insert(0, os.path.dirname(__file__))

from portfolio import PortfolioManager

def emergency_stop():
    """执行紧急停止"""
    print("=" * 60)
    print("⚠️  EMERGENCY STOP - 紧急停止")
    print("=" * 60)

    pm = PortfolioManager()

    print(f"\n当前持仓: {len(pm.portfolio.positions)} 个")
    print(f"当前现金: ${pm.portfolio.cash:.2f}")

    # 创建停止标记文件
    stop_file = os.path.join(os.path.dirname(__file__), "STOP_TRADING")
    with open(stop_file, 'w') as f:
        f.write("EMERGENCY STOP ACTIVATED")

    print("\n✅ 已创建停止标记")
    print("   请手动在 Polymarket 网站平仓")
    print("\n要恢复交易，删除 STOP_TRADING 文件")

if __name__ == "__main__":
    emergency_stop()
```

---

## 📊 监控清单

### 持续监控

- [ ] 每小时检查一次持仓盈亏
- [ ] 每日查看总盈亏
- [ ] 每周回顾交易表现
- [ ] 每月评估策略有效性

### 异常检查

- [ ] 检查是否有未成交订单
- [ ] 检查是否有错误日志
- [ ] 检查 API 连接状态
- [ ] 检查余额是否正确

---

## ⚡ 快速参考

### 启动命令

```bash
# 模拟交易（推荐先测试）
PAPER_TRADING=true python agent.py --once

# 真实交易（小心！）
PAPER_TRADING=false python agent.py --once

# 查看状态
python agent.py --status

# 紧急停止
python emergency_stop.py
```

### 重要文件

| 文件 | 用途 |
|------|------|
| `.env` | API 密钥和配置 |
| `config.py` | 交易参数 |
| `data/portfolio.json` | 投资组合状态 |
| `data/trades.json` | 交易历史 |
| `STOP_TRADING` | 紧急停止标记 |

---

## ⚠️ 最后的警告

1. **只使用您能承受损失的资金**
2. **始终先在模拟模式充分测试**
3. **从小额开始，逐步增加**
4. **随时准备手动干预**
5. **定期备份所有数据**
6. **不要让系统完全无人监控**

---

**祝交易顺利，风险可控！** 🎯
