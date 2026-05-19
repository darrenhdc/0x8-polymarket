"""
run_gfs_backtest.py — 真实 GFS 历史回测入口脚本

用法:
  python run_gfs_backtest.py                            # 默认：北京+上海，最近30天
  python run_gfs_backtest.py --city beijing --days 60  # 指定城市 + 天数
  python run_gfs_backtest.py --city beijing --city tokyo --start 2025-07-01 --end 2025-07-31
  python run_gfs_backtest.py --all-cities --days 14    # 所有城市，最近14天

数据来源:
  GFS 预报 → historical-forecast-api.open-meteo.com (真实历史预报存档)
  实测气温 → archive-api.open-meteo.com (ERA5 再分析)
  不再使用 random.gauss() 蒙特卡洛
"""

import argparse
import sys
import os
from datetime import datetime, timedelta

sys.path.insert(0, os.path.dirname(__file__))

from backtester import Backtester
from gfs_weather_source import CITIES


def parse_args():
    parser = argparse.ArgumentParser(description="GFS 历史回测")
    parser.add_argument("--city",       action="append", dest="cities", default=None,
                        help="城市名称（可重复，e.g. --city beijing --city tokyo）")
    parser.add_argument("--all-cities", action="store_true",
                        help="对 CITIES 字典中所有城市回测")
    parser.add_argument("--start",      type=str, default=None,
                        help="开始日期 YYYY-MM-DD（默认：--days 天前）")
    parser.add_argument("--end",        type=str, default=None,
                        help="结束日期 YYYY-MM-DD（默认：昨天）")
    parser.add_argument("--days",       type=int, default=30,
                        help="回测天数（从今天往前，默认30）")
    parser.add_argument("--sigma",      type=float, default=0.7,
                        help="GFS 预报误差 σ（默认 0.7°C）")
    parser.add_argument("--threshold",  type=float, default=None,
                        help="统一温度阈值（°C）；未指定时按纬度自动选 30 或 35°C")
    parser.add_argument("--min-edge",   type=float, default=0.10,
                        help="最小 edge 阈值（默认 0.10 = 10%%）")
    return parser.parse_args()


def main():
    args = parse_args()

    # 日期范围
    end_dt   = datetime.fromisoformat(args.end)   if args.end   else datetime.utcnow() - timedelta(days=1)
    start_dt = datetime.fromisoformat(args.start) if args.start else end_dt - timedelta(days=args.days - 1)
    date_range = (start_dt.strftime("%Y-%m-%d"), end_dt.strftime("%Y-%m-%d"))

    # 城市列表
    if args.all_cities:
        cities = list(CITIES.keys())
    elif args.cities:
        cities = [c.lower() for c in args.cities]
        unknown = [c for c in cities if c not in CITIES]
        if unknown:
            print(f"[警告] 未知城市（忽略）: {unknown}")
            cities = [c for c in cities if c in CITIES]
    else:
        cities = ["beijing", "shanghai"]  # 默认

    # 如果用户指定了统一阈值，构造 questions 列表
    questions = None
    if args.threshold is not None:
        questions = []
        for city in cities:
            lat, lon = CITIES[city]
            questions.append({
                "market_id":    f"gfs_bt_{city}",
                "question":     f"Will temperature in {city.title()} exceed {args.threshold:.0f}C?",
                "market_price": 0.50,
                "threshold":    args.threshold,
                "lat":          lat,
                "lon":          lon,
            })
        cities = None  # run_gfs 收到 questions 时忽略 cities

    print("=" * 60)
    print("  GFS 历史回测（真实数据）")
    print("=" * 60)
    print(f"  日期范围: {date_range[0]} → {date_range[1]}")
    print(f"  城市:     {cities or [q['market_id'] for q in questions]}")
    print(f"  σ =       {args.sigma}°C")
    print(f"  min_edge: {args.min_edge:.0%}")
    print()

    bt = Backtester(min_edge=args.min_edge, min_confidence=0.60)
    report = bt.run_gfs(
        date_range = date_range,
        sigma      = args.sigma,
        cities     = cities,
        questions  = questions,
    )
    bt.print_report(report)


if __name__ == "__main__":
    main()
