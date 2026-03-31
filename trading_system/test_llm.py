"""Quick test: analyze 3 markets with DeepSeek LLM"""
from llm_analyzer import LLMMarketAnalyzer
from market_data import MarketData
from decision_engine import MarketAnalyzer

md = MarketData()
analyzer = LLMMarketAnalyzer()

markets = md.scan_opportunities()[:3]
print(f"Found {len(markets)} markets\n")

for i, m in enumerate(markets):
    q = m.get("question", "")
    mid = m.get("id", "")
    print("=" * 60)
    print(f"Market {i+1}: {q}")

    ma = MarketAnalyzer(md)
    analysis = ma.analyze_market(m)

    result = analyzer.analyze(mid, q, analysis)
    if result:
        signal = analyzer.to_strategy_signal(result)
        print(f"  LLM estimate:  {result.estimated_probability:.1%}")
        print(f"  Market price:  {result.market_yes_price:.1%}")
        print(f"  Edge:          {result.edge:+.1%}")
        print(f"  Confidence:    {result.confidence}")
        print(f"  Reasoning:     {result.reasoning}")
        print(f"  Risks:         {result.risks}")
        print(f"  Signal:        {signal[0]} @ {signal[1]:.2f}")
        print(f"  Knowledge note: {result.knowledge_cutoff_note}")
    else:
        print("  Analysis failed")
    print()

print(f"Total API calls: {analyzer.call_count}")
