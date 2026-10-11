"""Local paper-trading dashboard and narrowly scoped task controls."""

from __future__ import annotations

import json
import os
import secrets
import threading
from datetime import datetime, timezone
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import parse_qs, urlparse

from .auto5m import AutoFiveMinute
from .config import Config, load_runtime_settings, save_runtime_settings
from .dashboard_state import live_quote, scheduled_task, snapshot
from .provider import select_provider
from .scheduler_control import manage_task
from .store import database, get_market, now_iso


HTML = """<!doctype html><html lang="zh-CN"><head><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1"><title>Polycast · 模拟预测工作台</title>
<style>
:root{
  --sans:"Inter","SF Pro Text",-apple-system,BlinkMacSystemFont,"Segoe UI","PingFang SC","Microsoft YaHei",system-ui,sans-serif;
  --mono:ui-monospace,"SF Mono","Cascadia Mono","JetBrains Mono",Consolas,monospace;
  --bg:#f5f4f0;--surface:#fffffe;--surface-2:#efeee9;--line:#e3e1da;--line-strong:#cdcac1;
  --text:#17181a;--text-2:#54575e;--text-3:#878a91;
  --accent:#3654d1;--accent-soft:rgba(54,84,209,.09);--accent-text:#2c46b4;--on-accent:#ffffff;
  --up:#18805a;--up-soft:rgba(24,128,90,.11);--down:#c93a48;--down-soft:rgba(201,58,72,.09);
  --warn:#9c6410;--warn-soft:rgba(168,106,18,.12);--radius:10px;color-scheme:light;
}
[data-theme="dark"]{
  --bg:#0c0d0f;--surface:#141518;--surface-2:#1c1e22;--line:#25272c;--line-strong:#363940;
  --text:#edeef0;--text-2:#a5a9b1;--text-3:#6d717a;
  --accent:#8196ff;--accent-soft:rgba(129,150,255,.13);--accent-text:#a6b5ff;--on-accent:#0c0d0f;
  --up:#45c08f;--up-soft:rgba(69,192,143,.14);--down:#f2717c;--down-soft:rgba(242,113,124,.13);
  --warn:#e3ad55;--warn-soft:rgba(227,173,85,.13);color-scheme:dark;
}
*{box-sizing:border-box}
html{font-size:14px}
body{margin:0;font-family:var(--sans);color:var(--text);background:var(--bg);line-height:1.5;-webkit-font-smoothing:antialiased;font-variant-numeric:tabular-nums}
button,input,select{font:inherit;color:inherit}
a{color:var(--accent-text);text-decoration:none}a:hover{text-decoration:underline}
:focus-visible{outline:2px solid var(--accent);outline-offset:2px}
.app{display:grid;grid-template-columns:224px minmax(0,1fr);min-height:100vh}
.side{position:sticky;top:0;height:100vh;display:flex;flex-direction:column;gap:22px;padding:20px 12px 16px;border-right:1px solid var(--line);background:var(--surface)}
.brand{display:flex;align-items:center;gap:10px;padding:0 8px}
.brand svg{width:24px;height:24px;flex:none}
.brand b{font-size:16px;font-weight:650;letter-spacing:-.3px}
.brand .tag{margin-left:auto;font-size:10.5px;color:var(--text-3);border:1px solid var(--line-strong);border-radius:99px;padding:1px 8px;white-space:nowrap}
.nav{display:flex;flex-direction:column;gap:2px}
.nav button{display:flex;align-items:center;gap:10px;width:100%;border:0;background:transparent;color:var(--text-2);padding:8px 10px;border-radius:8px;cursor:pointer;text-align:left;font-size:13.5px;white-space:nowrap}
.nav svg{width:16px;height:16px;flex:none;stroke:currentColor;fill:none;stroke-width:1.6;stroke-linecap:round;stroke-linejoin:round}
.nav button:hover{background:var(--surface-2);color:var(--text)}
.nav button.active{background:var(--accent-soft);color:var(--accent-text);font-weight:600}
.nav .count{margin-left:auto;font-size:11px;color:var(--text-3);font-weight:500}
.nav button.active .count{color:inherit;opacity:.75}
.side-foot{margin-top:auto;display:flex;flex-direction:column;gap:12px;padding:0 8px}
.sync{display:flex;align-items:flex-start;gap:9px;font-size:11.5px;color:var(--text-3);line-height:1.5}
.sync::before{content:"";width:7px;height:7px;border-radius:50%;background:var(--up);margin-top:5px;flex:none;box-shadow:0 0 0 3px var(--up-soft)}
.sync.danger{color:var(--down)}.sync.danger::before{background:var(--down);box-shadow:0 0 0 3px var(--down-soft)}
.theme-toggle{border:1px solid var(--line);background:transparent;color:var(--text-2);padding:6px 10px;border-radius:8px;cursor:pointer;font-size:12px;text-align:left}
.theme-toggle:hover{border-color:var(--line-strong);color:var(--text)}
main{min-width:0;max-width:1460px;padding:26px clamp(18px,3.2vw,44px) 28px}
.page-head{display:flex;justify-content:space-between;align-items:flex-end;gap:16px;flex-wrap:wrap;margin-bottom:18px}
.page-head h1{margin:0;font-size:22px;font-weight:650;letter-spacing:-.4px}
.page-head p{margin:3px 0 0;color:var(--text-3);font-size:12.5px}
.search{background:var(--surface);border:1px solid var(--line);border-radius:8px;padding:8px 12px;min-width:min(300px,100%);font-size:13px}
.search::placeholder{color:var(--text-3)}.search:focus{outline:0;border-color:var(--accent);box-shadow:0 0 0 3px var(--accent-soft)}
.alerts{display:grid;gap:8px}.alerts:not(:empty){margin-bottom:16px}
.alert{padding:10px 14px;border-radius:var(--radius);font-size:12.5px;line-height:1.55}
.alert b{font-weight:650;margin-right:8px}
.alert.danger{background:var(--down-soft);color:var(--down)}.alert.info{background:var(--accent-soft);color:var(--accent-text)}.alert.warn{background:var(--warn-soft);color:var(--warn)}
.stats{display:grid;grid-template-columns:repeat(4,minmax(0,1fr));gap:12px;margin-bottom:20px}
.kpi{background:var(--surface);border:1px solid var(--line);border-radius:var(--radius);padding:14px 16px 13px;display:flex;flex-direction:column;gap:5px;min-width:0}
.kpi .label{display:flex;justify-content:space-between;align-items:center;gap:8px;font-size:11.5px;color:var(--text-3)}
.kpi strong{font-size:25px;font-weight:650;letter-spacing:-.6px;line-height:1.2;white-space:nowrap;overflow:hidden;text-overflow:ellipsis}
.kpi small{font-size:11.5px;color:var(--text-3);line-height:1.5}
.chip{font-size:11px;font-weight:600;padding:1px 8px;border-radius:99px;background:var(--surface-2);color:var(--text-2);white-space:nowrap}
.chip.good{background:var(--up-soft);color:var(--up)}.chip.danger{background:var(--down-soft);color:var(--down)}.chip.warn{background:var(--warn-soft);color:var(--warn)}
.versus{display:grid;grid-template-columns:auto 1fr auto;gap:5px 9px;align-items:center;font-size:11.5px;color:var(--text-2);margin-top:2px}
.versus i{display:block;height:5px;border-radius:99px;background:var(--surface-2);overflow:hidden}
.versus i::after{content:"";display:block;height:100%;width:var(--w,0%);border-radius:99px;background:var(--text-3)}
.versus i.model::after{background:var(--accent)}
.versus b{font-weight:600;color:var(--text)}
.dots{display:flex;flex-direction:column;gap:3px;margin-top:2px;font-size:11.5px;color:var(--text-2)}
.dots span{display:flex;align-items:center;gap:8px}
.dots span::before{content:"";width:6px;height:6px;border-radius:50%;background:var(--text-3);flex:none}
.dots .ok::before{background:var(--up)}.dots .bad::before{background:var(--down)}.dots .idle::before{background:var(--warn)}
.danger{color:var(--down)!important}.good{color:var(--up)!important}.warn{color:var(--warn)!important}.muted{color:var(--text-3)}
.cols{display:grid;grid-template-columns:repeat(2,minmax(0,1fr));gap:14px;align-items:start;margin-bottom:14px}
.cols>.stack{display:grid;gap:14px;min-width:0}.cols .panel{margin:0}
.panel{background:var(--surface);border:1px solid var(--line);border-radius:var(--radius);padding:18px 20px;margin-bottom:14px;min-width:0}
.panel h2{margin:0 0 10px;font-size:14px;font-weight:650}
.panel h3{margin:0 0 6px;font-size:14px;font-weight:600}
.line{display:flex;justify-content:space-between;gap:16px;padding:7px 0;border-bottom:1px solid var(--line);font-size:12.5px;color:var(--text-2)}
.line:last-child{border-bottom:0}
.line span:last-child{text-align:right;color:var(--text);font-weight:500;overflow-wrap:anywhere}
.note,.sub{color:var(--text-3);font-size:12px;line-height:1.6}
.note{margin:10px 0 0}p.sub{margin:8px 0 0}p.warn,p.danger{font-size:12.5px;margin:10px 0 0}
.value{font-size:22px;font-weight:650;letter-spacing:-.4px;margin:6px 0}
.pill{display:inline-block;font-size:11px;font-weight:600;padding:2px 9px;border-radius:99px;background:var(--accent-soft);color:var(--accent-text);white-space:nowrap}
.pill.ok{background:var(--up-soft);color:var(--up)}.pill.wait{background:var(--warn-soft);color:var(--warn)}
.copy{border:1px solid var(--line-strong);background:transparent;color:var(--text);padding:6px 12px;border-radius:8px;cursor:pointer;font-size:12.5px;font-weight:500;white-space:nowrap}
.copy:hover:not(:disabled){background:var(--surface-2)}.copy:active:not(:disabled){transform:translateY(1px)}
.copy:disabled{opacity:.45;cursor:default}
.copy.primary{background:var(--accent);border-color:var(--accent);color:var(--on-accent);font-weight:600}
.copy.primary:hover:not(:disabled){background:var(--accent);filter:brightness(1.08)}
.copy.primary:disabled{opacity:1}.market-foot .copy.primary:disabled{opacity:.45}
.commands{display:flex;flex-wrap:wrap;gap:8px;margin-top:14px;align-items:center}
.interval,.limit-input{border:1px solid var(--line-strong);border-radius:8px;padding:6px 10px;background:var(--surface);font-size:12.5px}
.limit-box{display:flex;align-items:center;gap:8px;flex-wrap:wrap;margin-top:12px;padding-top:12px;border-top:1px solid var(--line);font-size:12.5px;color:var(--text-2)}
.limit-input{width:88px}
.meter{margin:12px 0}.meter .top{display:flex;justify-content:space-between;gap:14px;font-size:12.5px;color:var(--text-2)}
.meter .top span:last-child{color:var(--text);font-weight:500}
.bar{height:5px;background:var(--surface-2);border-radius:99px;overflow:hidden;margin:7px 0 4px}
.bar i{display:block;height:100%;background:var(--up);border-radius:99px}.bar i.warn{background:var(--warn)}.bar i.danger{background:var(--down)}
.filters{display:flex;gap:6px;flex-wrap:wrap;margin-bottom:16px}
.filters button{background:transparent;color:var(--text-2);border:1px solid var(--line);border-radius:99px;padding:5px 13px;cursor:pointer;font-size:12.5px}
.filters button:hover{border-color:var(--line-strong);color:var(--text)}
.filters button.active{background:var(--text);color:var(--bg);border-color:var(--text);font-weight:600}
.filters button em{font-style:normal;opacity:.6;margin-left:6px;font-size:11px}
.grid{display:grid;grid-template-columns:repeat(auto-fill,minmax(310px,1fr));gap:14px}
.task-grid{display:grid;grid-template-columns:repeat(2,minmax(0,1fr));gap:14px;margin-bottom:14px;align-items:start}.task-grid .panel{margin:0}
.item{background:var(--surface);border:1px solid var(--line);border-radius:var(--radius);padding:16px;min-width:0;transition:border-color .15s ease}
.item:hover{border-color:var(--line-strong)}
.item p{font-size:12.5px;line-height:1.65;margin:8px 0;color:var(--text-2)}
.market-card{display:flex;flex-direction:column;gap:10px}
.market-top{display:flex;align-items:center;justify-content:space-between;gap:10px;font-size:11.5px;color:var(--text-3)}
.tag{font-size:11px;font-weight:600;color:var(--text-2);background:var(--surface-2);border-radius:6px;padding:2px 8px;white-space:nowrap}
.market-card h3{margin:0;font-size:14.5px;line-height:1.45}.market-card h3 a{color:var(--text)}
.market-card .question{margin:0;font-size:12.5px;color:var(--text-2)}
.prob{display:grid;gap:9px;margin:2px 0}
.prob-row{display:grid;grid-template-columns:minmax(64px,auto) 1fr auto;gap:10px;align-items:center;font-size:12.5px}
.prob-row .name{font-weight:600;overflow:hidden;text-overflow:ellipsis;white-space:nowrap;max-width:130px}
.prob-row .num{font-weight:650;min-width:50px;text-align:right}
.track{position:relative;height:8px;border-radius:99px;background:var(--surface-2)}
.track i{position:absolute;left:0;top:0;bottom:0;border-radius:99px;background:var(--up)}
.prob-row.second .track i{background:var(--down)}
.track.pending i{background:var(--text-3);opacity:.45}
.track u{position:absolute;top:-3px;bottom:-3px;width:2px;border-radius:2px;background:var(--text);transform:translateX(-1px)}
.legend{display:flex;justify-content:space-between;gap:10px;font-size:11.5px;color:var(--text-3)}
.legend u{display:inline-block;width:2px;height:9px;background:var(--text);border-radius:2px;margin:0 6px -1px 0;text-decoration:none}
.market-foot{margin-top:auto;display:flex;align-items:center;justify-content:space-between;gap:8px;flex-wrap:wrap;padding-top:12px;border-top:1px solid var(--line);font-size:11.5px;color:var(--text-3)}
.market-actions{display:flex;gap:6px;flex-wrap:wrap}.market-foot .copy{font-size:12px;padding:5px 10px}
.pred-head{display:flex;justify-content:space-between;align-items:flex-start;gap:12px}
.pred-head h3{margin:0;font-size:14.5px;line-height:1.45}
.pred-id{font-family:var(--mono);font-size:11px;color:var(--text-3)}
.pred-value{display:flex;align-items:baseline;gap:8px;margin:10px 0 4px}
.pred-value b{font-size:26px;font-weight:650;letter-spacing:-.6px}
details{margin-top:10px;border-top:1px solid var(--line);padding-top:10px}
summary{cursor:pointer;font-size:12.5px;color:var(--accent-text);font-weight:500;list-style:none}
summary::-webkit-details-marker{display:none}summary::after{content:" ＋"}details[open] summary::after{content:" －"}
.evidence-list{display:flex;flex-wrap:wrap;gap:6px;margin:6px 0 10px}
.evidence-list a,.evidence-list span{border:1px solid var(--line);border-radius:6px;padding:2px 8px;font-size:11.5px;color:var(--text-2)}
.evidence-list a:hover{border-color:var(--accent);color:var(--accent-text);text-decoration:none}
.event-steps{display:flex;gap:6px;flex-wrap:wrap;margin:10px 0}
.empty{color:var(--text-3);font-size:13px;padding:26px 0;text-align:center}
.table{overflow-x:auto;border:1px solid var(--line);border-radius:8px}
table{border-collapse:collapse;width:100%;min-width:760px;font-size:12.5px}
th,td{text-align:left;padding:9px 12px;border-bottom:1px solid var(--line);vertical-align:top}
th{color:var(--text-3);font-weight:500;font-size:11.5px;white-space:nowrap;background:var(--surface-2)}
td.num,th.num{text-align:right;white-space:nowrap}
tr:last-child td{border-bottom:0}tbody tr:hover td{background:var(--surface-2)}
.chart-container{background:var(--surface);border:1px solid var(--line);border-radius:var(--radius);padding:18px 20px;margin-bottom:14px}
.chart-header{display:flex;justify-content:space-between;align-items:flex-start;gap:10px;flex-wrap:wrap;margin-bottom:10px}
.chart-header h3{margin:0;font-size:14px;font-weight:650}.chart-header p{margin:2px 0 0}
.chart-badge{font-size:12px;font-weight:600;padding:2px 10px;border-radius:99px;background:var(--surface-2)}
.chart-badge.good{background:var(--up-soft)}.chart-badge.danger{background:var(--down-soft)}
.svg-chart{width:100%;height:160px;overflow:visible;display:block}
.spark{width:100%;height:30px;display:block;margin-top:4px;overflow:visible}
.runs-strip{margin-bottom:14px;padding:12px 14px;background:var(--surface-2);border-radius:8px}
.strip-top{display:flex;justify-content:space-between;margin-bottom:9px;font-size:12px;color:var(--text-2)}
.strip-bars{display:flex;gap:3px;height:22px}.strip-col{flex:1}
.strip-fill{width:100%;height:100%;border-radius:3px}.strip-fill.ok{background:var(--up);opacity:.8}.strip-fill.err{background:var(--down)}
.footer{margin:22px 0 0;text-align:center}
@media(max-width:1180px){.stats{grid-template-columns:repeat(2,minmax(0,1fr))}}
@media(max-width:900px){
  .app{grid-template-columns:1fr}
  .side{position:static;height:auto;flex-direction:row;align-items:center;gap:12px;padding:10px 14px;border-right:0;border-bottom:1px solid var(--line);flex-wrap:wrap}
  .nav{flex-direction:row;overflow-x:auto;flex:1 1 100%;order:3}.nav .count{display:none}
  .side-foot{margin:0 0 0 auto;flex-direction:row;align-items:center}.sync span{display:none}
  .cols,.task-grid{grid-template-columns:1fr}main{padding:18px 14px 24px}
}
@media(max-width:560px){.stats{grid-template-columns:1fr}.grid{grid-template-columns:1fr}}
</style></head><body>
<div class="app">
  <aside class="side">
    <div class="brand">
      <svg viewBox="0 0 24 24" aria-hidden="true"><rect width="24" height="24" rx="6.5" fill="var(--text)"/><path d="M6.5 15.5l3.4-4 2.9 2.4 4.7-5.6" fill="none" stroke="var(--bg)" stroke-width="2" stroke-linecap="round" stroke-linejoin="round"/></svg>
      <b>Polycast</b><span class="tag">仅模拟</span>
    </div>
    <nav id="tabs" class="nav" aria-label="工作台导航"></nav>
    <div class="side-foot">
      <div id="sync" class="sync">正在读取本地状态…</div>
      <button id="theme-btn" class="theme-toggle" type="button" aria-label="切换深色/浅色主题">切换主题</button>
    </div>
  </aside>
  <main>
    <div class="page-head">
      <div><h1 id="view-title"></h1><p id="view-sub"></p></div>
      <input id="search" class="search" type="search" placeholder="搜索市场、证据、记录" aria-label="搜索当前视图">
    </div>
    <div id="alerts" class="alerts"></div>
    <div id="stats" class="stats"></div>
    <div id="content"></div>
    <p class="note footer">概率为未经样本外校准的研究输出；模拟成交不代表真实可得收益。页面可管理本机计划任务，不提交真实订单。</p>
  </main>
</div>
<script src="/app.js" defer></script>
</body></html>"""


JS = r"""'use strict';
const $=id=>document.getElementById(id),names={overview:'总览',markets:'市场',predictions:'AI 分析',ledger:'模拟账本',validation:'三事件验证',scheduler:'计划任务'};
const subtitles={overview:'账户、模型、行情采集与风控的当前状态',markets:'观察池内的市场，AI 概率与同期盘口对照',predictions:'每次预测的概率、依据与独立审查',ledger:'模拟订单、逐次决策与资金预占',validation:'链路验收与已结算事件的预测质量',scheduler:'本机计划任务、循环审计与官方禁入窗口'};
const icons={overview:'M4 4h7v7H4zM13 4h7v4h-7zM13 11h7v9h-7zM4 14h7v6H4z',markets:'M5 20V10M12 20V4M19 20v-7',predictions:'M12 3l2.2 5.6L20 11l-5.8 2.4L12 19l-2.2-5.6L4 11l5.8-2.4z',ledger:'M6 3h12v18l-3-2-3 2-3-2-3 2zM9 8h6M9 12h6',validation:'M12 21a9 9 0 100-18 9 9 0 000 18zM8.5 12.2l2.4 2.4 4.6-5',scheduler:'M12 21a9 9 0 100-18 9 9 0 000 18zM12 7.5V12l3 2'};
let state=null,view='overview',query='',categoryFilter='all',selectedMarketId=null,quoteBusy=false,schedulerBusy=false,schedulerMessage='',modelBusy=false,modelMessage='';const liveQuotes={},analysisMessages={},openDetails=new Set();
const el=(tag,cls,text)=>{const n=document.createElement(tag);if(cls)n.className=cls;if(text!==undefined&&text!==null)n.textContent=String(text);return n};
const add=(parent,...nodes)=>{nodes.forEach(n=>parent.append(n));return parent};
const fmt=(v,d=2)=>v===null||v===undefined?'—':Number(v).toLocaleString('zh-CN',{minimumFractionDigits:d,maximumFractionDigits:d});
const pct=v=>v===null||v===undefined?'—':fmt(v*100,1)+'%';
const when=v=>{if(!v)return '—';const d=new Date(v);return isNaN(d)?String(v):d.toLocaleString('zh-CN',{hour12:false})};
const clock=v=>{const d=new Date(v);return isNaN(d)?'—':d.toLocaleTimeString('zh-CN',{hour12:false})};
const signed=(v,d=2)=>(v>=0?'+':'−')+'$'+fmt(Math.abs(v),d);
const show=v=>v===null||v===undefined||v===''?'—':String(v),matches=(...values)=>!query||values.some(v=>String(v??'').toLowerCase().includes(query));
const pill=(text,kind='')=>el('span','pill '+kind,text),chip=(text,kind='')=>el('span','chip '+kind,text),empty=text=>el('div','empty',text),panel=title=>add(el('section','panel'),el('h2','',title));
const ext=(url,label)=>{const a=el('a','',label);try{const u=new URL(url);if(u.protocol!=='https:')return el('span','',label);a.href=u.href;a.target='_blank';a.rel='noopener noreferrer'}catch{return el('span','',label)}return a};
const row=(left,right)=>add(el('div','line'),el('span','',left),typeof right==='string'?el('span','',right):right);
const svgNS='http://www.w3.org/2000/svg';
const sEl=(tag,attrs={})=>{const e=document.createElementNS(svgNS,tag);for(const [k,v] of Object.entries(attrs))e.setAttribute(k,v);return e};
const icon=d=>{const s=sEl('svg',{viewBox:'0 0 24 24','aria-hidden':'true'});s.append(sEl('path',{d}));return s};
const providerName=()=>state.model.provider==='deepseek'?'DeepSeek':'Codex';
function remaining(end){
 const ms=new Date(end)-Date.now();if(isNaN(ms))return '—';if(ms<=0)return '已截止';
 const minutes=ms/60000;if(minutes<1)return '不足 1 分钟后截止';if(minutes<90)return Math.round(minutes)+' 分钟后截止';
 if(minutes<2880)return Math.round(minutes/60)+' 小时后截止';return '截止 '+new Date(end).toLocaleDateString('zh-CN');
}
function initTheme(){
  const saved=localStorage.getItem('polycast_theme')||(window.matchMedia('(prefers-color-scheme: dark)').matches?'dark':'light');
  document.documentElement.setAttribute('data-theme',saved);
  const btn=$('theme-btn');if(btn)btn.textContent=saved==='dark'?'切换到浅色':'切换到深色';
}
function toggleTheme(){
  const curr=document.documentElement.getAttribute('data-theme')==='dark'?'light':'dark';
  document.documentElement.setAttribute('data-theme',curr);
  localStorage.setItem('polycast_theme',curr);
  const btn=$('theme-btn');if(btn)btn.textContent=curr==='dark'?'切换到浅色':'切换到深色';
}
function settledCurve(){
 // The state carries only recent orders, so anchor the curve on the account's full realized P&L.
 const a=state.account,init=(a&&typeof a.initial_cash==='number'&&a.initial_cash>0)?a.initial_cash:1000,steps=[];
 [...(state.orders||[])].sort((x,y)=>(x.id||0)-(y.id||0)).forEach(o=>{
  if(o.status==='settled'&&typeof o.settlement==='number'&&!isNaN(o.settlement))steps.push({t:when(o.created_at),pnl:o.shares*o.settlement-o.total_cost})});
 const realized=typeof a.realized_pnl==='number'?a.realized_pnl:steps.reduce((sum,s)=>sum+s.pnl,0);
 let value=init+realized-steps.reduce((sum,s)=>sum+s.pnl,0);
 const pts=[{t:steps.length&&Math.abs(value-init)>1e-9?'更早的记录':'初始本金',val:value}];
 steps.forEach(s=>{value+=s.pnl;pts.push({t:s.t,val:value})});
 return {init,realized,pts};
}
function sparkline(values){
 const svg=sEl('svg',{viewBox:'0 0 200 30',class:'spark',preserveAspectRatio:'none'});if(values.length<2)return svg;
 const lo=Math.min(...values),hi=Math.max(...values),span=hi-lo||1;
 const d=values.map((v,i)=>(i?'L':'M')+(200*i/(values.length-1)).toFixed(1)+','+(27-24*(v-lo)/span).toFixed(1)).join(' ');
 svg.append(sEl('path',{d,fill:'none',stroke:'var(--accent)','stroke-width':'1.6','stroke-linejoin':'round','stroke-linecap':'round','vector-effect':'non-scaling-stroke'}));
 return svg;
}
function kpi(label,value,kind,badge,...extra){
 const card=el('div','kpi'),top=el('div','label');add(top,el('span','',label));if(badge)add(top,badge);
 add(card,top,el('strong',kind||'',value));extra.forEach(n=>{if(n)add(card,n)});return card;
}
function renderAlerts(){
 const box=$('alerts');box.replaceChildren();if(!state)return;
 const notice=(kind,title,text)=>add(box,add(el('div','alert '+kind),el('b','',title),document.createTextNode(text)));
 ((state.risk||{}).halts||[]).forEach(h=>notice('danger','风控停机',show(h.reason)+'（'+when(h.triggered_at)+' 起）：新的模拟入场已全部暂停。核对账本与盘口后，在命令行执行 risk-resume 并写明原因才会恢复。'));
 if((state.settings||{}).btc5m_auto_enabled===false)notice('info','五分钟自动预测已暂停','行情采集与结算核对继续，可在「计划任务」恢复。');
 if(state.model.pause_until&&new Date(state.model.pause_until)>new Date())notice('warn',providerName()+' 调用暂停','至 '+when(state.model.pause_until)+'；市场采集与结算仍继续。');
}
function renderStats(){
 const s=$('stats');s.replaceChildren();s.hidden=!state||view!=='overview';if(s.hidden)return;
 const a=state.account,r=state.risk||{},v=state.validation,ev=v.evaluation||{},m=state.model,five=state.btc5m_worker||{},task=state.scheduler||{};
 const curve=settledCurve(),equity=r.snapshot?r.snapshot.equity:curve.pts[curve.pts.length-1].val,diff=equity-curve.init;
 add(s,kpi('模拟净值','$'+fmt(equity),'',chip(signed(diff),diff>=0?'good':'danger'),sparkline(curve.pts.map(p=>p.val)),
  el('small','','现金 $'+fmt(a.cash_unreserved)+' · 已实现 '+signed(curve.realized))));
 const mb=ev.risk_group_weighted_pre_model_matched_model_brier,kb=ev.risk_group_weighted_pre_model_market_brier,have=typeof mb==='number'&&typeof kb==='number';
 const gap=have?mb-kb:0,versus=el('div','versus');
 if(have)for(const [name,val,cls] of [['模型',mb,'model'],['盘口',kb,'']]){
  const bar=el('i',cls);bar.style.setProperty('--w',(val/Math.max(mb,kb,1e-9)*100).toFixed(1)+'%');add(versus,el('span','',name),bar,el('b','',fmt(val,4)))}
 add(s,kpi('预测误差 Brier · 越低越好',have?(gap>0?'+':'−')+fmt(Math.abs(gap),4):'—',have?(gap>0?'danger':'good'):'',
  have?chip(gap>0?'落后盘口':'优于盘口',gap>0?'danger':'good'):null,have?versus:null,
  el('small','',String(ev.complete_pre_model_matched_events||0)+' 个严格配对的已结算事件')));
 const used=m.calls_today||0,limit=m.daily_limit||0,ratio=limit?used/limit:0,bar=el('div','bar'),fill=el('i',ratio>=1?'danger':ratio>=.8?'warn':'');
 fill.style.width=Math.min(100,ratio*100)+'%';add(bar,fill);
 add(s,kpi('模型调用 · UTC 今日',used+' / '+limit,'',chip(providerName()),bar,el('small','',m.model_version||'模型未知')));
 const halted=(r.halts||[]).length>0,cycleOn=task.available&&task.state!=='Disabled',dots=el('div','dots');
 add(dots,el('span',five.collector_fresh?'ok':'bad','行情采集 '+(five.collector_fresh?'正常':'异常')),
  el('span',cycleOn?'ok':'idle','定时循环 '+(task.available?show(task.state):'不可用')),
  el('span',halted?'bad':'ok','风控 '+(halted?'停机中':'正常')));
 const healthy=five.collector_fresh&&cycleOn&&!halted;
 add(s,kpi('运行状态',halted?'入场已停':healthy?'运行正常':'需要关注',halted?'danger':healthy?'good':'warn',
  chip(v.completed+' / '+v.target+' 事件验收',v.achieved?'good':'warn'),dots));
}
function meter(title,used,limit,description){const wrap=el('div','meter'),top=add(el('div','top'),el('span','',title),el('span','',`$${fmt(used)} / $${fmt(limit)}`));const bar=el('div','bar'),fill=el('i',used>=limit?'danger':used>=limit*.8?'warn':'');fill.style.width=Math.min(100,Math.max(0,used/limit*100))+'%';add(bar,fill);add(wrap,top,bar,el('div','sub',description));return wrap}
async function changeProvider(provider){
 if(modelBusy)return;modelBusy=true;modelMessage='正在切换模型…';render();
 try{const response=await fetch('/api/provider',{method:'POST',headers:{'Content-Type':'application/json'},
  body:JSON.stringify({provider,token:state.scheduler_token})});
  if(!response.ok)throw Error('切换失败（HTTP '+response.status+'）');
  const res=await response.json();state.model=res.model;if(res.settings)state.settings=res.settings;
  modelMessage='已切换至 '+(provider==='deepseek'?'DeepSeek':'Codex')
 }catch(err){modelMessage=err.message}finally{modelBusy=false;render()}
}
async function saveLimit(provider,limit){
 if(modelBusy)return;
 if(!limit||limit<1){modelMessage='调用上限必须为正整数';render();return}
 modelBusy=true;modelMessage='正在保存调用上限…';render();
 try{const response=await fetch('/api/settings',{method:'POST',headers:{'Content-Type':'application/json'},
  body:JSON.stringify({provider,daily_limit:limit,token:state.scheduler_token})});
  if(!response.ok)throw Error('保存失败（HTTP '+response.status+'）');
  const res=await response.json();state.model=res.model;if(res.settings)state.settings=res.settings;
  modelMessage='已将 '+(provider==='deepseek'?'DeepSeek':'Codex')+' 每日调用上限设置为 '+limit+' 次'
 }catch(err){modelMessage=err.message}finally{modelBusy=false;render()}
}
function overview(){
 const root=document.createDocumentFragment(),r=state.risk,a=state.account,v=state.validation;
 const account=panel('账户与风控');
 add(account,row('交易手续费后模拟盈亏',signed(a.realized_pnl)),row('当前未结算持仓成本','$'+fmt(a.open_cost)),row('意图预占','$'+fmt(a.reserved_cost)),
  meter('全部持仓 + 预占',r.exposure,r.total_limit,'上限按初始资金计算；模拟订单成本和未完成意图计入'),
  meter('最大单市场占用',Math.max(0,...Object.values(r.by_market)),r.market_limit,'按初始资金计算'),
  meter('最大风险组占用',Math.max(0,...Object.values(r.by_group)),r.event_limit,'同一 Fed 政策风险组跨会议合并'));
 if(r.snapshot){const x=r.snapshot,loss=Math.max(0,(x.day_start_equity-x.equity)/x.day_start_equity),dd=Math.max(0,(x.high_water_equity-x.equity)/x.high_water_equity);
  add(account,row('审慎估值净值','$'+fmt(x.equity)),row('日内亏损',pct(loss)+' / '+pct(r.daily_loss_limit)),row('高水位回撤',pct(dd)+' / '+pct(r.drawdown_limit)))}
 if(r.halts.length)add(account,el('p','danger','风险停机：'+r.halts.map(h=>h.reason).join('、')));
 const model=panel('模型与费用'),costs=state.model_costs||{},hasCosts=costs.recorded_cost_lower_usd!=null;
 add(model,row('运行模式','仅模拟；没有真实下注入口'),row('当前预测通道',providerName()),row('具体模型',state.model.model_version||'未知'),
  row('模型调用',state.model.calls_today+' / '+state.model.daily_limit+' 次（UTC 今日）'),row('模型暂停至',when(state.model.pause_until)),
  row('已记录模型费估计',hasCosts?'$'+fmt(costs.recorded_cost_lower_usd,6)+' – $'+fmt(costs.recorded_cost_upper_usd,6):'待记录'),
  row('费用未知的调用',String(costs.unpriced_calls||0)+' / '+String(costs.total_calls||0)),row('包含全部运行成本的净收益','待核算'));
 const providerControls=el('div','commands');for(const [key,label] of [['codex','Codex'],['deepseek','DeepSeek']]){
  const b=el('button','copy'+(key===state.model.provider?' primary':''),label);b.type='button';b.disabled=modelBusy||key===state.model.provider||(key==='deepseek'&&!state.model.deepseek_configured);b.addEventListener('click',()=>changeProvider(key));add(providerControls,b)
 }add(model,providerControls);
 const limitBox=el('div','limit-box');add(limitBox,el('span','','DeepSeek 每日调用上限'));
 const limitInp=el('input','limit-input');
 const dsLimit=(state.settings&&state.settings.max_deepseek_calls_per_utc_day)||(state.model.provider==='deepseek'?state.model.daily_limit:24);
 limitInp.type='number';limitInp.min='1';limitInp.max='2000';limitInp.value=String(dsLimit);
 limitInp.setAttribute('aria-label','DeepSeek 每日调用上限');
 const limitBtn=el('button','copy','保存上限');limitBtn.type='button';limitBtn.disabled=modelBusy;
 limitBtn.addEventListener('click',()=>saveLimit('deepseek',Number(limitInp.value)));
 add(limitBox,limitInp,limitBtn);add(model,limitBox);
 if(!state.model.deepseek_configured)add(model,el('p','note','DeepSeek 密钥尚未在本机配置。'));
 if(modelMessage)add(model,el('p','sub',modelMessage));
 add(model,el('p','note','模型费按已保存 token 与峰谷价格估计，尚未核对账单。历史、Codex 与超时费用未知，不按零费用计算。'));
 const task=state.scheduler,auto=panel('自动运行');
 add(auto,row('最近状态同步',when(state.generated_at)));
 if(task.available){
  add(auto,row('计划任务',task.state),row('下次执行',when(task.next_run)),row('最近任务退出码',String(task.last_result)),row('最近循环审计',state.runs.length?state.runs[0].status:'暂无'),row('无窗口启动',task.background?'已配置':'需修复'));
 }else add(auto,el('p','warn','计划任务状态不可用：'+show(task.reason)));
 const five=state.btc5m_worker||{};add(auto,row('BTC 5分钟自动分析',five.status||'未知'),row('Chainlink 采集器',five.collector_alive?'运行中':'未运行'),row('Chainlink 数据',five.collector_fresh?'新鲜':(five.collector_data_age_seconds==null?'尚无数据':'过期 '+five.collector_data_age_seconds+' 秒')),row('最近处理市场',five.last_market_id||'暂无'));
 if(five.collector_warning)add(auto,el('p','warn','五分钟行情：'+five.collector_warning));
 if(five.last_error)add(auto,el('p','warn','五分钟后台：'+five.last_error));
 if(task.available){const open=el('button','copy','管理计划任务');open.type='button';open.addEventListener('click',()=>{view='scheduler';render()});add(auto,add(el('div','commands'),open))}
 const basis=state.forecast_basis;let checks=null;
 if(basis){const counts=basis.counts||{};checks=panel('五分钟输入检查');add(checks,row('五分钟输入有效期',String(basis.max_forecast_age_seconds)+' 秒'),row('盘口变化弃权阈值',fmt(basis.max_mid_change*100,1)+' 个百分点'),row('近 24 小时候选报价检查',String(basis.checks)+' 次 / '+String(basis.unique_predictions)+' 个预测'),row('变化 / 过期 / 无效',String(counts.drifted||0)+' / '+String(counts.expired||0)+' / '+String(counts.invalid||0)),el('p','note','报价变化和输入过期会阻止沿用旧概率。阈值是工程保护，收益增益仍待前瞻验证。'+(basis.truncated?'统计已截断为最近 5000 次检查。':'')))}
 const micro=state.microstructure||{},venues=micro.venues||{},publicData=panel('同步市场数据');
 if(micro.causal_feature_records!=null)add(publicData,row('实验输入可用快照',String(micro.causal_complete_records||0)+' / '+String(micro.causal_feature_records||0)),row('实验事件输入已封存',String(micro.causal_frozen_events||0)));
 add(publicData,row('公开采集器',micro.alive?(micro.state_fresh?'运行中':'状态待刷新'):(micro.previous_status==='storage_limit'?'存储达到上限':'未运行')),row('Polymarket / Coinbase',show((venues.polymarket||{}).status)+' / '+show((venues.coinbase||{}).status)),row('同期盘口快照',String(micro.paired_feature_records||0)+' / '+String(micro.feature_records||0)),row('原始数据占用',fmt((micro.storage_bytes||0)/1024**3,3)+' / '+fmt((micro.storage_limit_bytes||0)/1024**3,1)+' GiB'));
 if(micro.last_error)add(publicData,el('p','warn',micro.last_error));
 const archival=micro.archive||{};if(archival.enabled)add(publicData,row('已校验压缩归档',String(archival.archived_segments||0)+' 段'),row('压缩节省空间',fmt((archival.bytes_saved||0)/1024**3,3)+' GiB'));
 if(archival.last_error)add(publicData,el('p','warn','归档检查：'+archival.last_error));
 const utcClock=micro.public_clock||{};if(utcClock.status)add(publicData,row('公开 UTC 时间检查',utcClock.status==='venues_agree'?'两场所区间一致':'未通过'),row('UTC − 本机时钟',utcClock.offset_ms==null?'未知':fmt(utcClock.offset_ms/1000,3)+' 秒'),row('时钟区间半宽',utcClock.uncertainty_ms==null?'未知':fmt(utcClock.uncertainty_ms,0)+' 毫秒'));
 const missing=micro.missing_book_counts||{},gaps=micro.gap_counts||{},qualityNames={reported_top_mismatch:'盘口顶价不一致',unordered_depth:'深度更新时间异常',invalid_change:'深度数量异常',future_source_clock:'行情时钟异常',awaiting_snapshot:'等待完整盘口',reconnect:'连接恢复中',stale_receipt:'盘口过期',future_receipt:'接收时钟异常',future_source:'行情时间待确认',one_sided_book:'单边盘口',crossed_book:'买卖价交叉',awaiting_connection:'等待连接',invalid_trade:'逐笔成交异常'};const qualityText=values=>Object.entries(values).map(([k,v])=>(qualityNames[k]||'其他异常')+' '+v).join('、')||'暂无';if(Object.keys(missing).length||Object.keys(gaps).length)add(publicData,row('本次连接深度异常',qualityText(gaps)),row('本次快照缺失原因',qualityText(missing)));
 add(publicData,el('p','note','同一进程记录到账时间和原始消息；缺口或过期数据不生成有效特征，时钟检查未通过时不冻结新样本。只压缩已关闭日志，解压哈希一致后才移除原始副本。快照数不代表独立事件数，也不证明预测盈利；参考价仍为未独立验签的代理。'));
 const val=panel('三事件验收进度');add(val,el('div','value',v.completed+' / '+v.target),el('p','note','每个独立事件须在官方首个结果公开前完成经审查预测和模拟决策，并在所有相关市场完成有来源的官方结算。定时循环次数不计为事件轮次。三轮是运行链路验收，不足以证明策略盈利。'));
 if(!v.achieved)add(val,el('p','warn','等待实际事件结算。'));
 const left=el('div','stack'),right=el('div','stack');add(left,account,auto,val);add(right,model);if(checks)add(right,checks);add(right,publicData);
 add(root,add(el('div','cols'),left,right));return root
}
async function refreshQuote(id){if(quoteBusy)return;quoteBusy=true;selectedMarketId=id;try{const response=await fetch('/api/quote?market_id='+encodeURIComponent(id),{cache:'no-store'});if(!response.ok)throw Error('HTTP '+response.status);liveQuotes[id]=await response.json()}catch(err){liveQuotes[id]={captured_at:new Date().toISOString(),quotes:[],reason:'盘口读取失败：'+err.message}}finally{quoteBusy=false;if(view==='markets')render()}}
async function requestAnalysis(id){
 analysisMessages[id]='已提交审计预测…';render();
 try{const response=await fetch('/api/analyze',{method:'POST',headers:{'Content-Type':'application/json'},
  body:JSON.stringify({market_id:id,token:state.scheduler_token})});
  if(!response.ok)throw Error('审计启动失败（HTTP '+response.status+'）');
  const result=await response.json();state.analysis_jobs=state.analysis_jobs||[];
  state.analysis_jobs=state.analysis_jobs.filter(x=>x.market_id!==id).concat(result.job);
  analysisMessages[id]='审计已排队，结果会自动刷新';
 }catch(err){analysisMessages[id]=err.message}finally{render()}
}
function probRow(label,ai,mid,second){
 const line=el('div','prob-row'+(second?' second':'')),track=el('div','track'+(ai===null?' pending':'')),value=ai===null?mid:ai;
 if(value!==null){const fill=el('i');fill.style.width=Math.min(100,Math.max(0,value*100))+'%';add(track,fill)}
 if(ai!==null&&mid!==null){const mark=el('u');mark.style.left=Math.min(100,Math.max(0,mid*100))+'%';mark.title='同期盘口 '+pct(mid);add(track,mark)}
 return add(line,el('span','name',label),track,el('span','num',value===null?'—':pct(value)));
}
function markets(){
 const root=document.createDocumentFragment(),filters=el('div','filters'),grid=el('div','grid');
 const kinds={crypto5m:'BTC · 5分钟',crypto:'加密货币 · 1小时',daily:'日常赛事',macro:'政策宏观'};
 for(const [key,label] of [['all','全部'],...Object.entries(kinds)]){
  const b=el('button',categoryFilter===key?'active':'',label);b.type='button';
  add(b,el('em','',String(key==='all'?state.markets.length:state.markets.filter(m=>m.category===key).length)));
  b.addEventListener('click',()=>{categoryFilter=key;render()});add(filters,b)
 }
 let count=0;
 for(const m of state.markets){
  if(categoryFilter!=='all'&&m.category!==categoryFilter)continue;
  if(!matches(m.question,m.event_title,m.id,m.category))continue;
  count++;
  const card=el('article','item market-card'),hasAI=m.probability!==null&&!m.abstain&&m.review_approved,mid=m.p_yes_mid;
  const top=add(el('div','market-top'),el('span','tag',kinds[m.category]||'市场'),el('span','',remaining(m.end_at)));top.lastChild.title='截止 '+when(m.end_at);
  add(card,top,add(el('h3'),ext('https://polymarket.com/event/'+encodeURIComponent(m.event_slug),m.event_title)));
  if(m.category==='macro')add(card,el('p','question',m.question));
  add(card,add(el('div','prob'),probRow(m.yes_label,hasAI?m.probability:null,mid,false),probRow(m.no_label,hasAI?1-m.probability:null,mid===null?null:1-mid,true)));
  const status=m.abstain?'AI 弃权':m.probability===null?'待分析':m.review_approved?'AI 预测':'未通过审查',legend=el('div','legend');
  add(legend,el('span','',status+(!hasAI&&mid!==null?' · 条形为同期盘口':'')));
  if(hasAI&&mid!==null)add(legend,add(el('span'),el('u'),document.createTextNode('同期盘口 '+pct(mid))));
  add(card,legend);
  const q=liveQuotes[m.id];
  if(q){add(card,el('div','sub','盘口 '+when(q.captured_at)+(q.reason?' · '+q.reason:'')));
   q.quotes.forEach(x=>add(card,row((x.side==='YES'?m.yes_label:m.no_label)+' 买入',
    '均价 '+fmt(x.avg_ask,4)+' · 成本后优势 '+(x.edge===null?'—':pct(x.edge))))) }
  const job=(state.analysis_jobs||[]).find(x=>x.market_id===m.id),active=job&&['queued','running'].includes(job.status);
  if(job&&job.status==='error')add(card,el('p','warn','审计失败：'+job.error));
  else if(job&&job.status==='done')add(card,el('p','sub','审计完成 · '+(job.abstain?'AI 弃权':job.review_approved?'已通过独立审查':'审查未通过')));
  else if(active)add(card,el('p','sub','审计预测正在'+(job.status==='queued'?'排队':'执行')+'…'));
  else if(analysisMessages[m.id])add(card,el('p','sub',analysisMessages[m.id]));
  const foot=el('div','market-foot'),button=el('button','copy','查看盘口');button.type='button';
  button.addEventListener('click',()=>refreshQuote(m.id));
  const audit=el('button','copy primary',active?'审计中…':'立即进行审计预测');audit.type='button';
  audit.disabled=Boolean(active)||new Date(m.end_at)<=new Date();
  audit.addEventListener('click',()=>requestAnalysis(m.id));
  add(foot,el('span','','24h 成交 $'+fmt(m.volume_24h,0)),add(el('div','market-actions'),button,audit));add(card,foot);add(grid,card)
 }
 add(root,filters,count?grid:empty('当前分类没有可观察市场。'));return root
}
function evidenceLinks(items){const box=el('div','evidence-list');if(!items.length)return add(box,el('span','','无'));items.forEach(x=>add(box,x.url?ext(x.url,x.title):el('span','',x.title)));return box}
function predictions(){
 const root=document.createDocumentFragment(),grid=el('div','grid');let count=0;
 for(const x of state.predictions){
  if(!matches(x.question,x.event_title,x.reasoning,x.market_id))continue;count++;
  const card=el('article','item'),approved=!x.abstain&&x.review.approved===true;
  add(card,add(el('div','pred-head'),el('h3','',x.event_title),el('span','pred-id','#'+x.id)));
  if(x.question!==x.event_title)add(card,el('p','',x.question));
  add(card,add(el('div','pred-value'),el('b',approved?'':'muted',x.abstain?'弃权':approved?pct(x.probability):'未通过'),
   el('span','sub',x.abstain?'证据不足以给出概率':approved?x.yes_label:'独立审查未通过')),
   el('div','sub','预测 '+when(x.as_of)+' · 审查 '+when(x.reviewed_at)+' · '+(x.model_version||'模型未记录')));
  const more=el('details');more.open=openDetails.has(x.id);
  more.addEventListener('toggle',()=>{if(more.open)openDetails.add(x.id);else openDetails.delete(x.id)});
  add(more,el('summary','','推理、依据与审查'),el('p','',x.reasoning),el('div','sub','支持依据'),evidenceLinks(x.supports),el('div','sub','反证'),evidenceLinks(x.counters),
   el('p','sub','关键假设：'+(x.assumptions.join('；')||'无')),el('p','sub','独立审查：'+show(x.review.reason)),el('p','sub','重新评估触发：'+show(x.review_trigger)));
  add(card,more);add(grid,card)
 }
 add(root,count?grid:empty('没有匹配的预测。'));return root
}
function table(headers,rows){
 const wrap=el('div','table'),t=el('table'),thead=el('thead'),tr=el('tr'),body=el('tbody');
 const numeric=headers.map((_,i)=>rows.length>0&&rows.every(values=>/^(?:[-+−]?\$?[\d,.]+%?|—)$/.test(String(values[i]??''))));
 headers.forEach((h,i)=>add(tr,el('th',numeric[i]?'num':'',h)));add(thead,tr);
 rows.forEach(values=>{const rr=el('tr');values.forEach((v,i)=>add(rr,el('td',numeric[i]?'num':'',v)));add(body,rr)});
 add(t,thead,body);return add(wrap,t)
}
function equityChart(){
 const wrap=el('div','chart-container'),curve=settledCurve(),pts=curve.pts.length>1?curve.pts:[...curve.pts,{t:'当前',val:curve.pts[0].val}];
 const diff=curve.realized,pctGain=curve.init>0?diff/curve.init*100:0;
 add(wrap,add(el('div','chart-header'),
  add(el('div'),el('h3','','模拟净值走势'),el('p','sub','初始本金 $'+fmt(curve.init)+' · 累计已实现盈亏 '+signed(diff)+' · 曲线为最近 '+(curve.pts.length-1)+' 笔已结算订单')),
  el('span','chart-badge '+(diff>=0?'good':'danger'),signed(diff)+'（'+(diff>=0?'+':'')+pctGain.toFixed(2)+'%）')));
 const vals=pts.map(p=>p.val);let minV=Math.min(...vals,curve.init),maxV=Math.max(...vals,curve.init);
 if(maxV-minV<1e-9){minV-=5;maxV+=5}else{const span=maxV-minV;minV-=span*.1;maxV+=span*.1}
 const W=760,H=160,padL=56,padR=14,padT=12,padB=24,plotW=W-padL-padR,plotH=H-padT-padB,range=maxV-minV;
 const yOf=v=>padT+plotH-(v-minV)/range*plotH,xOf=i=>padL+plotW*i/(pts.length-1);
 const svg=sEl('svg',{viewBox:`0 0 ${W} ${H}`,class:'svg-chart'});
 for(let i=0;i<=3;i++){
  const value=minV+range*(3-i)/3,y=(padT+plotH*i/3).toFixed(1),label=sEl('text',{x:padL-8,y:(Number(y)+3.5).toFixed(1),'text-anchor':'end',fill:'var(--text-3)','font-size':'10'});
  label.textContent='$'+fmt(value,0);svg.append(sEl('line',{x1:padL,y1:y,x2:W-padR,y2:y,stroke:'var(--line)'}),label);
 }
 const base=yOf(curve.init).toFixed(1);svg.append(sEl('line',{x1:padL,y1:base,x2:W-padR,y2:base,stroke:'var(--text-3)','stroke-dasharray':'2,4'}));
 const lineD=pts.map((p,i)=>(i?'L':'M')+xOf(i).toFixed(1)+','+yOf(p.val).toFixed(1)).join(' ');
 svg.append(sEl('path',{d:lineD,fill:'none',stroke:'var(--accent)','stroke-width':'2','stroke-linejoin':'round','stroke-linecap':'round'}));
 const last=pts.length-1;svg.append(sEl('circle',{cx:xOf(last).toFixed(1),cy:yOf(pts[last].val).toFixed(1),r:'3.5',fill:'var(--accent)'}));
 for(const [index,anchor] of [[0,'start'],[last,'end']]){
  const label=sEl('text',{x:xOf(index).toFixed(1),y:String(H-5),'text-anchor':anchor,fill:'var(--text-3)','font-size':'10'});label.textContent=pts[index].t;svg.append(label)}
 add(wrap,svg);return wrap;
}
function runsHealthBar(runs){
 const box=el('div','runs-strip');
 const okCount=runs.filter(r=>r.status==='success'||!r.error_count).length;
 add(box,add(el('div','strip-top'),el('span','','最近 20 轮循环健康度'),el('span','sub',`${okCount} 正常 / ${runs.length} 轮次`)));
 const bars=el('div','strip-bars');
 [...runs].reverse().forEach(r=>{
  const isOk=r.status==='success'||(r.error_count===0);
  const bar=el('div','strip-col');
  const fill=el('div','strip-fill '+(isOk?'ok':'err'));
  bar.title=`${when(r.started_at)}: ${r.status} (检查:${r.paper_checks}, 成交:${r.paper_fills}, 错误:${r.error_count})`;
  add(bar,fill);add(bars,bar);
 });
 add(box,bars);return box;
}
function settlementLabel(x){
 if(x.status==='settled')return '已核验结算';
 if(x.status!=='open')return x.status;
 if(x.end_at&&new Date(x.end_at)>new Date())return '模拟持仓中';
 const reasons={twap_boundary_missing:'缺少开盘/收盘 TWAP',source_conflict:'结算来源结果不一致',corroboration_missing:'等待参考来源结果',official_result_pending:'等待官方有效结果',contract_mismatch:'市场规则核对未通过',confirmation_pending:'等待结果确认',external_data_error:'结算来源暂不可用'};
 return '待核验 · '+(reasons[x.settlement_check_reason]||'等待结算检查');
}
function ledger(){const root=document.createDocumentFragment(),orders=state.orders.filter(x=>matches(x.question,x.market_id,x.side)),decisions=state.decisions.filter(x=>matches(x.question,x.market_id,x.reason)),intents=state.intents.filter(x=>matches(x.question,x.market_id,x.status));const op=panel('模拟订单');add(op,orders.length?table(['ID','市场','方向','份额','均价','总成本','成交时成本后优势','状态','时间'],orders.map(x=>[x.id,x.question,x.side,fmt(x.shares),fmt(x.avg_price,4),'$'+fmt(x.total_cost),pct(x.expected_edge),settlementLabel(x),when(x.created_at)])):empty('尚无模拟订单。'));const dp=panel('逐次模拟决策审计');add(dp,decisions.length?table(['ID','市场','预测','结果','原因','检查时间'],decisions.map(x=>[x.id,x.question,x.prediction_id||'—',x.status,x.reason,when(x.checked_at)])):empty('尚无模拟决策记录。'));const ip=panel('意图与预占');add(ip,intents.length?table(['ID','市场','方向','最大预占','状态','原因','时间'],intents.map(x=>[x.id,x.question,x.side,'$'+fmt(x.max_total_cost),x.status,show(x.failure_reason),when(x.created_at)])):empty('尚无交易意图。'));add(root,equityChart(),op,dp,ip);return root}
function validation(){const root=document.createDocumentFragment(),v=state.validation,p=panel('三次独立事件验收');add(p,el('p','note','完整事件：所有相关市场均有公开结果之前的独立审查预测，至少一次有记录的模拟下注/跳过决策，且所有结果有来源地结算。一个 Fed 会议的多个市场只计一次。'),el('div','value',v.completed+' / '+v.target));const grid=el('div','grid');for(const x of v.events){if(!matches(x.title,x.event_id))continue;const c=el('article','item');add(c,el('h3','',x.title),el('div','sub','事件 '+x.event_id+' · 截止 '+when(x.end_at)));const steps=el('div','event-steps');add(steps,pill('审查预测 '+(x.prediction?'完成':'待完成'),x.prediction?'ok':'wait'),pill('模拟决策 '+(x.paper_decision?'完成':'待完成'),x.paper_decision?'ok':'wait'),pill('官方结算 '+(x.settlement?'完成':'待完成'),x.settlement?'ok':'wait'));add(c,steps,el('div','sub',`已结算 ${x.resolved_count}/${x.market_count} 个市场 · 决策 ${when(x.decision_at)}`),pill(x.complete?'计入验收':'尚未计入',x.complete?'ok':'wait'));add(grid,c)}add(p,grid.children.length?grid:empty('尚无事件。'));const e=panel('已结算事件的预测质量'),r=v.evaluation;add(e,row('完整已评分事件',String(r.complete_scored_events)),row('前置盘口配对完整事件',String(r.complete_pre_model_matched_events)),row('模型 Brier（前置盘口同样本）',show(r.risk_group_weighted_pre_model_matched_model_brier)),row('市场 Brier（前置盘口同样本）',show(r.risk_group_weighted_pre_model_market_brier)),row('具备盘口基准的完整事件（含旧口径）',String(r.complete_matched_events)),row('配对组均模型 Brier（含旧口径）',show(r.risk_group_weighted_matched_model_brier)),row('配对组均市场 Brier（含旧口径）',show(r.risk_group_weighted_market_brier)),row('具备输入代理的完整事件',String(r.complete_proxy_matched_events)),row('模型 Brier（代理配对）',show(r.risk_group_weighted_proxy_matched_model_brier)),row('简单代理 Brier（同样本）',show(r.risk_group_weighted_proxy_brier)),row('模拟已实现盈亏','$'+fmt(r.realized_paper_pnl)),el('p','note','Brier 越低越好。旧盘口是在模型审查后采集，正式前瞻比较只使用模型调用前的盘口；历史旧口径单列为诊断。价格代理来自交给 AI 的冻结输入，不能视为独立数据源。'));add(root,p,e);return root}
function calibrationShadowPanel(){
 const p=panel('前瞻校准观察'),s=state.calibration_shadow||{},counts=s.status_counts||{};
 const status=s.status==='running'?(s.fresh?'运行中':'状态过期'):(s.status==='error'?'需要检查':'尚未开启');
 add(p,row('观察状态',status),row('登记时间',when(s.registered_at)),row('已保存预测',String(s.frozen_events||0)),
  row('候选概率已生成',String(counts.candidate||0)),row('同版本训练不足',String((counts.insufficient_training||0)+(counts.insufficient_outcome_coverage||0))),
  row('待核验结果',String(s.pending_events||0)),row('结束前未保存的预测事件',String((s.coverage||{}).ended_without_record||0)));
 const a=s.resolved_raw_market||{},b=s.resolved_calibrated_market||{},av=a.brier||{},bv=b.brier||{};
 add(p,table(['相同核验事件','事件数','原始 AI Brier','校准候选 Brier','同期盘口 Brier'],[
  ['原始预测与盘口',a.events||0,show(av.model),'—',show(av.market)],
  ['同时具备校准候选',b.events||0,show(bv.model),show(bv.calibrated),show(bv.market)]]),
  el('p','note','仅比较事件结束前实际保存、随后完成结果核验的概率。校准训练需至少 20 个同模型、同提示版本的历史事件，起止间隔至少 60 分钟；Brier 越低越好。候选只作研究，未用于自动入场。'));
 if((s.invalid_records||0)+(s.malformed_records||0)>0)add(p,el('p','warn','部分记录或保存时间未通过核验，已排除评分。'));
 return p
}
function providerScores(){
 const metrics=state.validation.evaluation.by_provider||{},p=panel('模型通道样本');
 for(const [key,label] of [['codex','Codex'],['deepseek','DeepSeek'],['unknown','未归属'],['mixed','混合']]){
  const item=metrics[key]||{};
  if((key==='unknown'||key==='mixed')&&!item.complete_scored_events)continue;
  add(p,row(label+' · 完整评分事件',String(item.complete_scored_events||0)),
    row(label+' · Brier',show(item.risk_group_weighted_brier)));
 }
 add(p,el('p','note','仅统计每个事件结算前最后一次合格预测。通道覆盖的事件可能不同，不能直接比较模型优劣。'));
 const versions=state.validation.evaluation.by_model||{},models=panel('具体模型样本');
 for(const key of ['deepseek-flash','deepseek-v4-pro']){
  const item=versions[key]||{};
  add(models,row(key+' · 完整评分事件',String(item.complete_scored_events||0)),
    row(key+' · 严格配对模型 / 盘口 Brier',show(item.risk_group_weighted_pre_model_matched_model_brier)+' / '+show(item.risk_group_weighted_pre_model_market_brier)));
 }
 add(models,el('p','note','仅用结果公布前最后一次经审查预测归属模型；两个版本可能覆盖不同事件，不能当作同事件对照。'));
 const shadow=state.validation.evaluation.rejected_five_minute_shadow||{},diagnostic=panel('未通过审查的五分钟预测 · 研究评分');
 add(diagnostic,row('已结算研究事件',String(shadow.events||0)),row('严格盘口配对事件',String(shadow.paired_events||0)),
   row('模型 / 市场 Brier（同样本）',show(shadow.paired_model_brier)+' / '+show(shadow.paired_market_brier)));
 const cohorts=[];
 for(const [model,prompts] of Object.entries(shadow.by_model_and_prompt||{}))for(const [prompt,item] of Object.entries(prompts)){
  cohorts.push([model,prompt,item.events,item.paired_events,show(item.paired_model_brier),show(item.paired_market_brier)]);
 }
 if(cohorts.length)add(diagnostic,table(['模型','预测版本','事件','配对事件','模型 Brier','市场 Brier'],cohorts));
 add(diagnostic,el('p','note','这些是原始分析未弃权、但独立审查拒绝的概率，仅用于检查拒绝原因与模型质量。拒绝样本存在选择偏差，不计入三事件验收、合格预测或交易收益；不同模型和版本分别列出。'));
 return add(document.createDocumentFragment(),p,models,diagnostic);
}
async function schedulerAction(action,interval_minutes,target='cycle'){
 if(schedulerBusy)return;schedulerBusy=true;schedulerMessage='正在更新计划任务…';render();
 try{const response=await fetch('/api/scheduler',{method:'POST',headers:{'Content-Type':'application/json'},
  body:JSON.stringify({action,interval_minutes,target,token:state.scheduler_token})});
  if(!response.ok)throw Error('操作失败（HTTP '+response.status+'）');
  const result=await response.json();state.scheduler=result.scheduler;state.realtime_scheduler=result.realtime_scheduler;if(result.settings)state.settings=result.settings;schedulerMessage='已更新 · '+when(new Date().toISOString());
 }catch(err){schedulerMessage=err.message}finally{schedulerBusy=false;render()}
}
function scheduler(){
 const root=document.createDocumentFragment(),task=state.scheduler,p=panel('定时模拟循环');
 if(task.available){
  add(p,row('状态',task.state),row('后台无窗口',task.background?'已配置':'未配置'),
    row('运行间隔',show(task.interval_minutes)+' 分钟'),row('下次计划',when(task.next_run)),
    row('上次启动',when(task.last_run)),row('上次结果码',String(task.last_result)),
    row('重复期限',when(task.repetition_end)),row('错过轮次',String(task.missed_runs)));
  const controls=el('div','commands');
  for(const [action,label] of [['enable','启用'],['disable','暂停'],['run','立即运行一次']]){
   const b=el('button','copy',label);b.type='button';b.disabled=schedulerBusy;
   b.addEventListener('click',()=>schedulerAction(action));add(controls,b)
  }
  const select=el('select','interval');select.setAttribute('aria-label','计划任务运行间隔');
  for(const minutes of [15,30,60]){const option=el('option','',minutes+' 分钟');option.value=String(minutes);
   option.selected=Number(task.interval_minutes)===minutes;add(select,option)}
  const save=el('button','copy','保存间隔');save.type='button';save.disabled=schedulerBusy;
  save.addEventListener('click',()=>schedulerAction('interval',Number(select.value)));
  add(controls,select,save);add(p,controls);
  if(schedulerMessage)add(p,el('p','sub',schedulerMessage));
  add(p,el('p','note','“立即运行一次”只启动模拟循环。任务结果请结合下方循环审计查看。'))
 }else add(p,el('p','warn','无法读取计划任务：'+show(task.reason)));
 const realtime=state.realtime_scheduler||{},live=panel('五分钟实时采集');
 if(realtime.available){
  add(live,row('Windows 任务',realtime.task_name||'PolycastRealtime'),row('状态',realtime.state),
   row('后台无窗口',realtime.background?'已配置':'未配置'),row('启动方式','当前用户登录后持续运行'),
   row('恢复检查',realtime.interval_minutes?realtime.interval_minutes+' 分钟':'未配置'),row('下次触发',when(realtime.next_run)),
   row('自动预测',(state.settings||{}).btc5m_auto_enabled===false?'已暂停':'已启用'),
   row('上次触发',when(realtime.last_run)),row('任务结果',realtime.state==='Running'?'正在运行':(realtime.last_result===0?'正常结束':String(realtime.last_result))));
  const controls=el('div','commands');
  for(const [action,label] of [['pause_predictions','暂停自动预测'],['resume_predictions','恢复自动预测'],['enable','启用自动恢复'],['disable','停用自动启动'],['run','立即启动']]){
   const b=el('button','copy',label);b.type='button';b.disabled=schedulerBusy;
   b.addEventListener('click',()=>schedulerAction(action,null,'realtime'));add(controls,b)
  }
  add(live,controls,el('p','note','暂停自动预测会停止新自动分析和模拟入场，保留行情采集与结算核对；正在进行的分析可完成，手动审计仍可用。停用自动启动只影响后续触发。每分钟检查启动，运行中忽略重复触发；退出登录、关机或休眠会中断采集。'));
 }else add(live,empty('实时任务未配置或暂不可读：'+show(realtime.reason)+'。安装方式见 InstallRealtime.ps1。'));
 const runs=state.runs.filter(x=>matches(x.status,x.id,x.started_at)),rp=panel('循环审计 · 最近 20 次');
 if(runs.length)add(rp,runsHealthBar(runs));
 add(rp,runs.length?table(['ID','开始','结束','状态','市场','新证据','分析','模拟检查','成交','错误'],runs.map(x=>
  [x.id,when(x.started_at),when(x.finished_at),x.status,show(x.watchlist_markets),show(x.new_evidence_versions),
   show(x.analysis_attempts),show(x.paper_checks),show(x.paper_fills),show(x.error_count)])):empty('暂无运行记录。'));
 const cal=panel('官方发布时间禁入窗口');
 add(cal,state.calendar.length?table(['来源','事件','开始','结束'],state.calendar.map(x=>
  [x.source,x.name,when(x.starts_at),when(x.ends_at)])):empty('暂无后续官方窗口。'));
 const ev=panel('最近可用证据'),list=el('div','grid');
 state.evidence.filter(x=>matches(x.title,x.source)).forEach(x=>{const c=el('article','item');
  add(c,el('div','sub',x.source+' · '+when(x.published_at)),add(el('h3'),ext(x.url,x.title)));add(list,c)});
 const tasks=el('div','task-grid');add(tasks,p,live);
 add(ev,list.children.length?list:empty('暂无匹配证据。'));add(root,tasks,rp,cal,ev);return root
}
const renderers={overview,markets,predictions,ledger,
 validation:()=>{const root=document.createDocumentFragment();add(root,calibrationShadowPanel(),validation(),providerScores());return root},scheduler};
function renderTabs(){
 const nav=$('tabs');nav.replaceChildren();
 const counts=state?{markets:state.markets.length,predictions:state.stats.predictions,ledger:(state.account||{}).open_orders?state.account.open_orders+' 持仓':undefined,validation:state.validation.completed+'/'+state.validation.target}:{};
 Object.entries(names).forEach(([key,name])=>{
  const b=el('button',key===view?'active':'');b.type='button';b.setAttribute('aria-current',key===view?'page':'false');
  add(b,icon(icons[key]),el('span','',name));if(counts[key]!==undefined)add(b,el('span','count',counts[key]));
  b.addEventListener('click',()=>{view=key;render()});add(nav,b)})
}
function render(){renderTabs();$('view-title').textContent=names[view];$('view-sub').textContent=subtitles[view];renderAlerts();renderStats();if(state)$('content').replaceChildren(renderers[view]())}
async function refresh(){try{const response=await fetch('/api/state',{cache:'no-store'});if(!response.ok)throw Error('HTTP '+response.status);state=await response.json();$('sync').replaceChildren(el('span','','已同步 '+clock(state.generated_at)+' · 每 15 秒更新'));$('sync').className='sync';render();if(selectedMarketId&&view==='markets')refreshQuote(selectedMarketId)}catch(err){$('sync').replaceChildren(el('span','','状态读取失败 · '+err.message+' · 将自动重试'));$('sync').className='sync danger'}}
$('search').addEventListener('input',e=>{query=e.target.value.trim().toLowerCase();render()});
const tb=$('theme-btn');if(tb)tb.addEventListener('click',toggleTheme);initTheme();
render();refresh();setInterval(refresh,15000);
"""


def render(config: Config) -> str:
    """Return the static dashboard shell (keeps compatibility with older callers)."""
    return HTML


def make_server(config: Config, host: str, port: int,
                auto5m: AutoFiveMinute | None = None, microstructure=None) -> ThreadingHTTPServer:
    if host not in ("127.0.0.1", "localhost"):
        raise ValueError("Dashboard only binds to localhost")
    # One-time schema migration before read-only polling connections start.
    with database(config.database):
        pass
    scheduler_token = secrets.token_urlsafe(24)
    analysis_jobs: dict[str, dict] = {}
    jobs_lock = threading.Lock()

    def job_states() -> list[dict]:
        with jobs_lock:
            return [dict(item) for item in list(analysis_jobs.values())[-20:]]

    def run_analysis_job(market_id: str) -> None:
        from .cli import analyze, collect
        from .short_cycle import collect_short

        with jobs_lock:
            analysis_jobs[market_id].update(status="running", started_at=now_iso())
        try:
            with database(config.database) as conn:
                market = get_market(conn, market_id)
                category = market["category"]
                if category in ("crypto", "crypto5m", "daily"):
                    collect_short(conn, [dict(market)],
                                  config.data_dir / "chainlink_twap60.jsonl")
            if category == "macro":
                collect(config)
            result = analyze(config, market_id)
            outcome = {"status": "done", "result_status": result.get("status"),
                       "prediction_id": result.get("prediction_id"),
                       "abstain": result.get("abstain"),
                       "review_approved": result.get("review_approved"),
                       "finished_at": now_iso(), "error": None}
        except Exception as exc:
            outcome = {"status": "error", "finished_at": now_iso(),
                       "error": f"{type(exc).__name__}: {exc}"[:180]}
        with jobs_lock:
            analysis_jobs[market_id].update(outcome)

    class Handler(BaseHTTPRequestHandler):
        def _trusted_host(self) -> bool:
            return self.headers.get("Host", "") in (
                f"127.0.0.1:{self.server.server_port}",
                f"localhost:{self.server.server_port}")

        def do_GET(self) -> None:
            if not self._trusted_host():
                self.send_error(403, "Local host required")
                return
            path = urlparse(self.path).path
            if path in ("/", "/index.html"):
                data, content_type = HTML.encode("utf-8"), "text/html; charset=utf-8"
            elif path == "/app.js":
                data, content_type = JS.encode("utf-8"), "text/javascript; charset=utf-8"
            elif path == "/api/health":
                from .runtime import database_identity
                value = {"service": "polycast", "mode": "paper-only", "pid": os.getpid(),
                         "database_identity": database_identity(config)}
                data, content_type = json.dumps(value).encode(), "application/json; charset=utf-8"
            elif path == "/api/state":
                try:
                    value = snapshot(config)
                    value["scheduler_token"] = scheduler_token
                    value["btc5m_worker"] = (auto5m.state() if auto5m else {"status": "inactive"})
                    value["microstructure"] = (microstructure.state() if microstructure else {"status": "inactive"})
                    value["analysis_jobs"] = job_states()
                    data = json.dumps(value, ensure_ascii=False,
                                      allow_nan=False).encode("utf-8")
                except Exception as exc:
                    self.send_error(503, f"State unavailable: {type(exc).__name__}")
                    return
                content_type = "application/json; charset=utf-8"
            elif path == "/api/quote":
                market_ids = parse_qs(urlparse(self.path).query).get("market_id", [])
                if len(market_ids) != 1 or not market_ids[0].isdigit():
                    self.send_error(400, "One numeric market_id is required")
                    return
                try:
                    data = json.dumps(live_quote(config, market_ids[0]),
                                      ensure_ascii=False, allow_nan=False).encode("utf-8")
                except (KeyError, ValueError) as exc:
                    self.send_error(404, str(exc)[:100])
                    return
                content_type = "application/json; charset=utf-8"
            else:
                self.send_error(404)
                return
            self.send_response(200)
            self.send_header("Content-Type", content_type)
            self.send_header("Content-Length", str(len(data)))
            self.send_header("Cache-Control", "no-store")
            self.send_header("X-Content-Type-Options", "nosniff")
            self.send_header("Content-Security-Policy",
                             "default-src 'none'; script-src 'self'; connect-src 'self'; "
                             "style-src 'unsafe-inline'; base-uri 'none'; form-action 'none'")
            self.end_headers()
            self.wfile.write(data)

        def do_POST(self) -> None:
            path = urlparse(self.path).path
            if path not in ("/api/scheduler", "/api/provider", "/api/settings", "/api/analyze"):
                self.send_error(404)
                return
            origin = self.headers.get("Origin", "")
            allowed_origins = (f"http://127.0.0.1:{self.server.server_port}",
                               f"http://localhost:{self.server.server_port}")
            if not self._trusted_host() or origin not in allowed_origins:
                self.send_error(403, "Local page origin required")
                return
            if self.headers.get("Content-Type", "").split(";", 1)[0] != "application/json":
                self.send_error(415, "JSON required")
                return
            try:
                length = int(self.headers.get("Content-Length", "0"))
                if not 1 <= length <= 512:
                    raise ValueError("Invalid request size")
                payload = json.loads(self.rfile.read(length))
                if not isinstance(payload, dict) or not secrets.compare_digest(
                        str(payload.get("token", "")), scheduler_token):
                    self.send_error(403, "Invalid local control token")
                    return
                if path == "/api/analyze":
                    market_id = str(payload.get("market_id", ""))
                    if not market_id.isdigit():
                        raise ValueError("Numeric market ID required")
                    with database(config.database) as conn:
                        market = get_market(conn, market_id)
                        end = datetime.fromisoformat(market["end_at"].replace("Z", "+00:00"))
                        if end <= datetime.now(timezone.utc):
                            raise ValueError("Market has ended")
                    with jobs_lock:
                        current = analysis_jobs.get(market_id)
                        active = sum(job["status"] in ("queued", "running")
                                     for job in analysis_jobs.values())
                        if current and current["status"] in ("queued", "running"):
                            value = {"job": dict(current)}
                        elif active >= 3:
                            raise ValueError("Too many analysis jobs are already running")
                        else:
                            job = {"market_id": market_id, "status": "queued",
                                   "requested_at": now_iso(), "started_at": None,
                                   "finished_at": None, "error": None}
                            analysis_jobs[market_id] = job
                            value = {"job": dict(job)}
                            threading.Thread(target=run_analysis_job, args=(market_id,),
                                             daemon=True, name=f"audit-{market_id}").start()
                elif path == "/api/scheduler":
                    target = payload.get("target", "cycle")
                    action = payload.get("action")
                    if target == "realtime" and action in ("pause_predictions", "resume_predictions"):
                        save_runtime_settings(config.data_dir, {"btc5m_auto_enabled": action == "resume_predictions"})
                    elif target == "cycle":
                        manage_task(payload.get("action"), payload.get("interval_minutes"))
                    else:
                        manage_task(payload.get("action"), payload.get("interval_minutes"), target=target)
                    value = {"scheduler": scheduled_task(), "realtime_scheduler": scheduled_task("realtime"),
                             "settings": load_runtime_settings(config.data_dir)}
                elif path == "/api/provider":
                    if payload.get("provider"):
                        select_provider(config.data_dir, payload.get("provider"))
                    if payload.get("daily_limit") or payload.get("limit"):
                        lim = int(payload.get("daily_limit") or payload.get("limit"))
                        if lim < 1:
                            raise ValueError("Daily limit must be at least 1")
                        prov = payload.get("provider") or snapshot(config)["model"]["provider"]
                        k = "max_deepseek_calls_per_utc_day" if prov == "deepseek" else "max_model_calls_per_utc_day"
                        save_runtime_settings(config.data_dir, {k: lim})
                    value = {"model": snapshot(config)["model"], "settings": load_runtime_settings(config.data_dir)}
                else:
                    updates = {}
                    ds_lim = payload.get("max_deepseek_calls_per_utc_day")
                    if ds_lim is None and (payload.get("provider") == "deepseek" or "daily_limit" in payload):
                        ds_lim = payload.get("daily_limit") or payload.get("limit")
                    if ds_lim is not None:
                        lim = int(ds_lim)
                        if lim < 1:
                            raise ValueError("DeepSeek call limit must be at least 1")
                        updates["max_deepseek_calls_per_utc_day"] = lim
                    cdx_lim = payload.get("max_model_calls_per_utc_day")
                    if cdx_lim is None and payload.get("provider") == "codex":
                        cdx_lim = payload.get("daily_limit") or payload.get("limit")
                    if cdx_lim is not None:
                        lim = int(cdx_lim)
                        if lim < 1:
                            raise ValueError("Codex call limit must be at least 1")
                        updates["max_model_calls_per_utc_day"] = lim
                    if updates:
                        save_runtime_settings(config.data_dir, updates)
                    value = {"model": snapshot(config)["model"], "settings": load_runtime_settings(config.data_dir)}
                data = json.dumps(value, ensure_ascii=False).encode("utf-8")
            except (KeyError, ValueError, TypeError, RuntimeError, OSError) as exc:
                self.send_error(400, str(exc)[:180])
                return
            self.send_response(200)
            self.send_header("Content-Type", "application/json; charset=utf-8")
            self.send_header("Content-Length", str(len(data)))
            self.send_header("Cache-Control", "no-store")
            self.send_header("X-Content-Type-Options", "nosniff")
            self.end_headers()
            self.wfile.write(data)

    return ThreadingHTTPServer((host, port), Handler)


def serve(config: Config, host: str, port: int) -> None:
    from .microstructure import MicrostructureCapture
    worker = AutoFiveMinute(config)
    capture = MicrostructureCapture(config)
    server = make_server(config, host, port, worker, capture)
    worker.start()
    capture.start()
    print(f"Local paper dashboard: http://{host}:{server.server_port}/", flush=True)
    try:
        server.serve_forever()
    finally:
        capture.stop()
        worker.stop()
        server.server_close()
