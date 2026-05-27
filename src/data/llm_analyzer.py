"""
LLM Market Analyzer – uses DeepSeek (via Volcengine ARK) to assess Polymarket outcomes.

For each market the LLM receives:
  • The market question
  • Current YES / NO prices, volume, liquidity
  • Its world knowledge (training data, not real-time news)

It returns:
  • estimated_probability (0-1)
  • edge vs market price
  • confidence (0-1)
  • detailed reasoning
  • recommended action

Analysis results are cached to avoid redundant API calls.
"""
import json
import os
import hashlib
from datetime import datetime, timedelta
from typing import Dict, Optional, Tuple
from dataclasses import dataclass, asdict
from dotenv import load_dotenv

from openai import OpenAI

from src.core import config

# Ensure .env is loaded (config.py already does this, but guard for standalone use)
load_dotenv(os.path.join(os.path.dirname(os.path.dirname(os.path.dirname(__file__))), "trading_system", ".env"))

# ── Cache ────────────────────────────────────────────────────
CACHE_DIR = os.path.join(config.DATA_DIR, "llm_cache")
CACHE_TTL_HOURS = 6  # Re-analyze a market at most every 6 hours


@dataclass
class LLMAnalysis:
    """Result of an LLM market analysis."""
    market_id: str
    question: str
    timestamp: str
    # LLM outputs
    estimated_probability: float  # LLM's probability estimate for YES
    market_yes_price: float
    edge: float  # estimated_probability - market_yes_price
    confidence: str  # "high", "medium", "low"
    reasoning: str
    recommendation: str  # "BUY_YES", "BUY_NO", "HOLD"
    key_factors: list
    risks: list
    # Meta
    model: str
    knowledge_cutoff_note: str


def _cache_key(market_id: str) -> str:
    return os.path.join(CACHE_DIR, f"{market_id}.json")


def _load_cached(market_id: str) -> Optional[LLMAnalysis]:
    """Load cached analysis if still fresh."""
    path = _cache_key(market_id)
    if not os.path.exists(path):
        return None
    try:
        with open(path, "r") as f:
            data = json.load(f)
        ts = datetime.fromisoformat(data["timestamp"])
        if datetime.utcnow() - ts > timedelta(hours=CACHE_TTL_HOURS):
            return None  # stale
        return LLMAnalysis(**data)
    except Exception:
        return None


def _save_cache(analysis: LLMAnalysis):
    os.makedirs(CACHE_DIR, exist_ok=True)
    with open(_cache_key(analysis.market_id), "w") as f:
        json.dump(asdict(analysis), f, indent=2)


# ── Prompt ───────────────────────────────────────────────────

SYSTEM_PROMPT = """\
你是预测市场分析师，任务是评估 Polymarket 上事件结果的概率。

规则：
1. 保持校准：不确定时，概率应接近市场价格；只有在有明确理由时才明显偏离。
2. 使用基准率、历史先例与逻辑推理。
3. 明确区分“已知”和“未知”。
4. 你的训练数据有时间截断，可能缺少最新信息；相关时必须提示。
5. 不要编造事实；不知道就直说不知道。
6. 只有当与你场价差异 >= 10 个百分点时，才建议 BUY_YES 或 BUY_NO。

输出要求：
- 只输出合法 JSON（不要 markdown 代码块）。
- 字段名必须严格匹配下方 schema。
- confidence 只能是 high / medium / low（英文枚举）。
- recommendation 只能是 BUY_YES / BUY_NO / HOLD。
- reasoning、key_factors、risks、knowledge_cutoff_note 必须使用中文。

JSON schema:
{
  "estimated_probability": <float 0-1, YES 概率>,
  "confidence": "<high|medium|low>",
  "reasoning": "<2-3句中文分析>",
  "key_factors": ["<中文因素1>", "<中文因素2>", ...],
  "risks": ["<中文风险1>", "<中文风险2>", ...],
  "recommendation": "<BUY_YES|BUY_NO|HOLD>",
  "knowledge_cutoff_note": "<中文：可能缺失的最新信息>"
}
"""


def _build_user_prompt(question: str, analysis: Dict) -> str:
    yes_price = analysis.get("yes_price", 0)
    no_price = analysis.get("no_price", 0)
    volume = analysis.get("volume", 0)
    liquidity = analysis.get("liquidity", 0)
    category = analysis.get("category", "Unknown")
    days_to_resolution = analysis.get("days_to_resolution", "unknown")

    return f"""\
请分析这个 Polymarket 预测市场：

问题：{question}
分类：{category}
当前价格：YES = {yes_price:.3f} ({yes_price*100:.1f}%)  |  NO = {no_price:.3f} ({no_price*100:.1f}%)
成交量：${volume:,.0f}
流动性：${liquidity:,.0f}
距结算天数：{days_to_resolution}
今天日期：{datetime.utcnow().strftime("%Y-%m-%d")}

请给出你对 YES 的概率估计。
仅当你的估计与市场价差异 >= 10 个百分点时，才推荐 BUY_YES 或 BUY_NO；否则推荐 HOLD。
"""


# ── Core ─────────────────────────────────────────────────────

class LLMMarketAnalyzer:
    """Calls DeepSeek (via Volcengine ARK) to analyze prediction markets."""

    MODEL = os.getenv("LLM_MODEL", "deepseek-v4-pro")

    def __init__(self):
        api_key = os.getenv("ARK_API_KEY", "")
        base_url = os.getenv("ARK_BASE_URL", "https://ark.cn-beijing.volces.com/api/v3")
        if not api_key:
            raise RuntimeError("ARK_API_KEY not set in environment")
        self.client = OpenAI(api_key=api_key, base_url=base_url)
        self.call_count = 0

    def analyze(
        self, market_id: str, question: str, market_analysis: Dict
    ) -> Optional[LLMAnalysis]:
        """
        Analyze a single market. Returns cached result if fresh.
        """
        # Check cache first
        cached = _load_cached(market_id)
        if cached:
            return cached

        yes_price = market_analysis.get("yes_price", 0)

        try:
            response = self.client.chat.completions.create(
                model=self.MODEL,
                max_tokens=512,
                messages=[
                    {"role": "system", "content": SYSTEM_PROMPT},
                    {"role": "user", "content": _build_user_prompt(question, market_analysis)}
                ],
            )

            text = response.choices[0].message.content.strip()
            # Strip markdown code fences if present
            if text.startswith("```"):
                text = text.split("\n", 1)[1]
                if text.endswith("```"):
                    text = text[: text.rfind("```")]
                text = text.strip()

            data = json.loads(text)
            self.call_count += 1

            est_prob = float(data["estimated_probability"])
            edge = est_prob - yes_price

            analysis = LLMAnalysis(
                market_id=market_id,
                question=question,
                timestamp=datetime.utcnow().isoformat(),
                estimated_probability=est_prob,
                market_yes_price=yes_price,
                edge=round(edge, 4),
                confidence=data.get("confidence", "low"),
                reasoning=data.get("reasoning", ""),
                recommendation=data.get("recommendation", "HOLD"),
                key_factors=data.get("key_factors", []),
                risks=data.get("risks", []),
                model=self.MODEL,
                knowledge_cutoff_note=data.get("knowledge_cutoff_note", ""),
            )

            _save_cache(analysis)
            return analysis

        except json.JSONDecodeError as e:
            print(f"LLM returned invalid JSON for {market_id}: {e}")
            return None
        except Exception as e:
            print(f"LLM analysis error for {market_id}: {e}")
            return None

    def to_strategy_signal(
        self, analysis: LLMAnalysis
    ) -> Tuple[str, float, str]:
        """
        Convert LLM analysis into a trading signal compatible with StrategyEngine.
        Returns: (decision, confidence_score, reasoning)
        """
        if not analysis:
            return "HOLD", 0, "LLM 分析不可用"

        edge = analysis.edge
        abs_edge = abs(edge)
        rec = analysis.recommendation

        # Map LLM confidence to numeric score
        conf_map = {"high": 0.75, "medium": 0.60, "low": 0.45}
        base_conf = conf_map.get(analysis.confidence, 0.45)

        # Only trade if edge >= 10 percentage points
        MIN_EDGE = 0.10

        if abs_edge < MIN_EDGE:
            return "HOLD", 0, f"优势不足（{edge:+.1%}）。LLM 估计：{analysis.estimated_probability:.1%}，市场价：{analysis.market_yes_price:.1%}"

        # Build reasoning string
        reasoning = (
            f"LLM 估计 YES 概率为 {analysis.estimated_probability:.1%} "
            f"（市场价：{analysis.market_yes_price:.1%}，优势：{edge:+.1%}）。"
            f"{analysis.reasoning}"
        )

        if rec == "BUY_YES" and edge > 0:
            return "BUY_YES", base_conf, reasoning
        elif rec == "BUY_NO" and edge < 0:
            return "BUY_NO", base_conf, reasoning
        elif edge > MIN_EDGE:
            # LLM said HOLD but edge is large enough for YES
            return "BUY_YES", base_conf * 0.8, f"[覆盖] {reasoning}"
        elif edge < -MIN_EDGE:
            return "BUY_NO", base_conf * 0.8, f"[覆盖] {reasoning}"

        return "HOLD", 0, reasoning


# ── CLI test ─────────────────────────────────────────────────

if __name__ == "__main__":
    from .market_data import MarketData

    md = MarketData()
    analyzer = LLMMarketAnalyzer()

    # Grab a few active markets to test
    markets = md.scan_opportunities()[:3]
    for m in markets:
        q = m.get("question", "")
        mid = m.get("id", "")
        print(f"\n{'='*60}")
        print(f"Market: {q}")

        from src.strategies.decision_engine import MarketAnalyzer
        ma = MarketAnalyzer(md)
        analysis = ma.analyze_market(m)

        result = analyzer.analyze(mid, q, analysis)
        if result:
            signal = analyzer.to_strategy_signal(result)
            print(f"  LLM estimate: {result.estimated_probability:.1%}")
            print(f"  Market price: {result.market_yes_price:.1%}")
            print(f"  Edge: {result.edge:+.1%}")
            print(f"  Confidence: {result.confidence}")
            print(f"  Reasoning: {result.reasoning}")
            print(f"  Risks: {result.risks}")
            print(f"  Signal: {signal[0]} @ {signal[1]:.2f}")
            print(f"  Knowledge note: {result.knowledge_cutoff_note}")
