#!/usr/bin/env python3
"""Polymarket Trading Dashboard — Usage: ./dashboard [--once]"""
from __future__ import annotations
import json, os, re, sys, time, math, subprocess
from datetime import datetime, timezone, timedelta
from pathlib import Path

_PROJECT_ROOT = Path(__file__).resolve().parent.parent.parent; sys.path.insert(0, str(_PROJECT_ROOT))

from rich.live import Live
from rich.text import Text
from rich.console import Console, Group
from rich.panel import Panel
from rich import box
from src.core import config

TRADE_LOG = _PROJECT_ROOT / "data" / "trade_log.json"
CC = {"Hong Kong":"red","London":"blue","Amsterdam":"dark_orange","Paris":"magenta","Istanbul":"cyan","Manila":"yellow","Madrid":"green"}
CD = [("Hong Kong",0.98,"+0.05","🟢"),("London",0.62,"-0.12","🟢"),("Amsterdam",0.68,"+1.08","🟢"),("Paris",0.94,"+0.91","🟢"),("Istanbul",0.76,"+0.38","🟢"),("Manila",0.90,"+0.45","🟡"),("Madrid",0.67,"-0.42","🟡")]

def _load(): 
    with open(TRADE_LOG) as f: return [t for t in json.load(f).get("trades",[]) if t.get("status")=="open"]
def _bid(tok):
    import requests
    try: ob=requests.get(f"{config.CLOB_API}/book",params={"token_id":tok},timeout=5).json(); return max((float(b["price"]) for b in ob.get("bids",[])),default=0)
    except: return 0
def _usdc():
    try:
        bk=os.path.expanduser("/home/darren/share/polymarket/config/.env.txt.backup")
        with open(bk) as f: m=re.search(r"^\s*sk\s*=\s*([0-9a-fA-FxX]+)",f.read(),re.MULTILINE)
        pk=m.group(1).strip(); pk=pk if pk.startswith("0x") else "0x"+pk
        from py_clob_client_v2.client import ClobClient
        from py_clob_client_v2.clob_types import ApiCreds,BalanceAllowanceParams,AssetType
        c=ApiCreds(api_key="f785f79c-3119-1c24-3489-3ac27718b741",api_secret="uLbsEVrSw-wTNHC1X4wZ5tQuHzaeiy6xpuJrXAGbFX4=",api_passphrase="04949246bc4fe4326d25df889a4271c52299b7bea07a5b38ed8566e7566fb61a")
        cl=ClobClient(host="https://clob.polymarket.com",chain_id=137,key=pk,creds=c,signature_type=2,funder="0x1270215141EA0a2CdA89272722B2ac47DF6751A1")
        return int(cl.get_balance_allowance(params=BalanceAllowanceParams(asset_type=AssetType.COLLATERAL,signature_type=2))["balance"])/1e6
    except: return 0
def _status():
    try: sc=subprocess.run("pgrep -f edge_scanner",shell=True,capture_output=True).returncode==0
    except: sc=False
    try: wd=subprocess.run("pgrep -f stop_loss_watchdog",shell=True,capture_output=True).returncode==0
    except: wd=False
    n=datetime.now(timezone.utc); w=False
    for gh in [0,6,12,18]:
        if gh*60+30<=n.hour*60+n.minute<gh*60+90: w=True; break
    return sc,wd,w

def render(cycle=0, interval=10, prev_time=None):
    trades=_load(); n=datetime.now(timezone.utc); nc=n+timedelta(hours=8); sc,wd,gw=_status(); usdc=_usdc()
    G,R,Y,C,DIM,BOLD = "green","red","yellow","cyan","dim","bold"
    lines=[]
    
    # Title bar
    s_col,s_lbl = (G,"●") if sc else (R,"○")
    wd_col,wd_lbl = (G,"●") if wd else (R,"○")
    gfs_col,gfs_lbl = (G,"⚡ACTIVE") if gw else ("dim","⏳idle")
    
    title_parts = [f"[{BOLD} cyan]📊 Polymarket[/{BOLD} cyan]"]
    title_parts.append(f"Scanner [{s_col}]{s_lbl}[/{s_col}]")
    title_parts.append(f"Watchdog [{wd_col}]{wd_lbl}[/{wd_col}]")
    title_parts.append(f"GFS [{gfs_col}]{gfs_lbl}[/{gfs_col}]")
    title_parts.append(f"│ 💵 [bold green]${usdc:,.2f}[/bold green]")
    title_parts.append(f"│ UTC {n.strftime('%H:%M')} [bold yellow]HKT {nc.strftime('%H:%M')}[/bold yellow]")
    lines.append("  ".join(title_parts))
    
    # City abbreviations
    city_abbr = "  ".join(f"[{CC[c]}]{c[:3]}[/{CC[c]}]" for c in ["Hong Kong","London","Amsterdam","Paris","Istanbul","Manila","Madrid"])
    lines.append(f"  {city_abbr}")
    lines.append("")
    
    # City monitor (2 per row)
    for i in range(0,len(CD),2):
        row = []
        for j in range(2):
            if i+j < len(CD):
                name,sig,bias,st = CD[i+j]; c = CC.get(name,"white")
                row.append(f"[{c}]{name:<10}[/{c}] [{DIM}]σ[/{DIM}]{sig:.2f} [{DIM}]b[/{DIM}]{bias} {st}")
        lines.append(f"  {'    '.join(row)}")
    lines.append("")
    
    # Positions
    if not trades:
        lines.append(f"  [{DIM}]📭 No open positions[/{DIM}]")
    else:
        for t in trades:
            tid=t["trade_id"]; o=t["order"]; e=t.get("edge",{}); tp=t.get("take_profit",{})
            tok=o["token_id"]; entry=o["price"]; sh=o["shares"]; cost=o["cost_usd"]
            mp=e.get("model_P_not") or e.get("model_P_eq") or 0.5
            tpp=tp.get("price",0); city=t["market"]["city"]; dt=t["market"]["date"]
            q=t["market"]["question"][:48]; bid=_bid(tok)
            pnl=(bid-entry)*sh; pp=(bid-entry)/entry*100 if entry else 0
            gap=mp-entry; cv=(bid-entry)/gap*100 if gap>0 else 0; sl=entry*0.80
            
            bl=20; prog=max(0,min(int((cv/50)*bl) if cv>0 else 0,bl))
            if prog>0:
                bar=f"[{G}]{'█'*prog}[/{G}]{'·'*(bl-prog)}"
            else:
                bar=f"[{DIM}]{'·'*bl}[/{DIM}]"
            
            pc = G if pnl>=0 else R
            cc_city = CC.get(city,"white")
            # Time to settlement (end of target date UTC)
            try:
                settle_dt = datetime.strptime(dt, "%Y-%m-%d").replace(tzinfo=timezone.utc) + timedelta(hours=30)
                hours_left = (settle_dt - n).total_seconds() / 3600
                if hours_left <= 0: tts = f"[red]⏰ SETTLING[/red]"
                elif hours_left < 1: tts = f"[red]⏰ {(hours_left*60):.0f}m[/red]"
                elif hours_left < 6: tts = f"[red]⏰ {hours_left:.0f}h[/red]"
                elif hours_left < 24: tts = f"[yellow]⏰ {hours_left:.0f}h[/yellow]"
                else: tts = f"[dim]⏰ {hours_left:.0f}h[/dim]"
            except: tts = ""

            lines.append(f"  [{BOLD} {cc_city}]{tid}[/{BOLD} {cc_city}] [{DIM}]{q}[/{DIM}]")
            lines.append(f"    [{DIM}]{city} | {dt} | BUY_NO {sh}sh | {tts}[/{DIM}]")
            lines.append(f"    Cost [{cc_city}]${cost:.2f}[/{cc_city}]  Entry ${entry:.3f}  Bid ${bid:.3f}  P(not) {mp:.1%}")
            lines.append(f"    TP ${tpp:.3f}  SL ${sl:.3f}  PnL [{pc}]${pnl:+.2f} ({pp:+.1f}%)[/{pc}]  Conv {cv:+.1f}% {bar}")
            lines.append("")
        
        pos_t=sum(_bid(t["order"]["token_id"])*t["order"]["shares"] for t in trades)
        pos_c=sum(t["order"]["cost_usd"] for t in trades); pos_p=pos_t-pos_c
        pc_s = G if pos_p>=0 else R
        lines.append(f"  [{BOLD}]📦 Bid ${pos_t:.2f}  💰 In ${pos_c:.2f}  💵 Cash ${usdc:,.2f}  🏦 Total ${usdc+pos_t:.2f}  📈 [{pc_s}]${pos_p:+.2f}[/{pc_s}]  🔄 {len(trades)} open[/{BOLD}]")
    
    lines.append("")
    prev_str = f"[bold yellow]last: {prev_time.strftime('%H:%M:%S')}[/bold yellow]" if prev_time else f"[bold yellow]last: {n.strftime('%H:%M:%S')}[/bold yellow]"
    lines.append(f"  [{DIM}]Ctrl+C quit | auto {interval}s | {prev_str} | #{cycle}[/{DIM}]")
    
    return Panel(Text.from_markup("\n".join(lines)), border_style=C, box=box.ROUNDED, padding=(0,1))

def main():
    import argparse
    p=argparse.ArgumentParser(); p.add_argument("--once",action="store_true"); p.add_argument("--interval",type=int,default=5); a=p.parse_args()
    console = Console(highlight=False)
    if a.once: console.print(render(interval=a.interval)); return
    prev = datetime.now(timezone.utc)
    with Live(render(0, a.interval, prev), console=console, refresh_per_second=4, screen=True) as live:
        cycle = 1
        while True:
            try:
                time.sleep(a.interval)
                prev = datetime.now(timezone.utc)
                live.update(render(cycle, a.interval, prev)); cycle += 1
            except KeyboardInterrupt: break

if __name__=="__main__": main()
