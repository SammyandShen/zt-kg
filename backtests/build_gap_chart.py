#!/usr/bin/env python3
"""build_gap_chart.py — 把 next_day_gap_daily.csv 渲染成自包含 HTML 折线图。"""

from __future__ import annotations

import csv
import json
from pathlib import Path

HERE = Path(__file__).resolve().parent
SRC = HERE / "next_day_gap_daily.csv"
DST = HERE / "next_day_gap.html"


def f(v):
    return None if v == "" or v is None else round(float(v), 4)


rows = list(csv.DictReader(open(SRC, encoding="utf-8")))
data = [{
    "d": r["date"], "nd": r["next_date"], "n": int(r["n"]),
    "up": f(r["gap_up_ratio"]), "upm": f(r["gap_up_mean"]),
    "dn": f(r["gap_dn_ratio"]), "dnm": f(r["gap_dn_mean"]),
    "flat": f(r["flat_ratio"]), "om": f(r["open_mean_all"]),
    "un": f(r["under_ratio"]), "unc": f(r["under_close_mean"]),
    "cm": f(r["close_mean_all"]),
} for r in rows]

# 全期加权汇总（按事件数加权，与逐事件口径一致）
tot = sum(r["n"] for r in data)
def wavg(key, weight_key=None):
    num = den = 0.0
    for r in data:
        v = r[key]
        if v is None:
            continue
        w = r["n"] * (r[weight_key] / 100 if weight_key else 1)
        num += v * w
        den += w
    return num / den if den else None

summary = {
    "days": len(data), "events": tot,
    "up": wavg("up"), "upm": wavg("upm", "up"),
    "dn": wavg("dn"), "dnm": wavg("dnm", "dn"),
    "un": wavg("un"), "unc": wavg("unc", "un"),
    "om": wavg("om"), "cm": wavg("cm"),
    "start": data[0]["d"], "end": data[-1]["d"],
}

HTML = """<!doctype html>
<html lang="zh-CN"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1">
<title>涨停次日开盘行为 · 最近一年</title>
<style>
:root{color-scheme:light dark;
 --surface-1:#fcfcfb; --plane:#f9f9f7;
 --ink:#0b0b0b; --ink-2:#52514e; --muted:#898781;
 --grid:#e1e0d9; --axis:#c3c2b7; --border:rgba(11,11,11,.10);
 --s1:#2a78d6; --s2:#eb6834; --s3:#1baf7a;}
@media (prefers-color-scheme:dark){:root:where(:not([data-theme=light])){
 --surface-1:#1a1a19; --plane:#0d0d0d;
 --ink:#fff; --ink-2:#c3c2b7; --muted:#898781;
 --grid:#2c2c2a; --axis:#383835; --border:rgba(255,255,255,.10);
 --s1:#3987e5; --s2:#d95926; --s3:#199e70;}}
:root[data-theme=dark]{--surface-1:#1a1a19;--plane:#0d0d0d;--ink:#fff;--ink-2:#c3c2b7;
 --grid:#2c2c2a;--axis:#383835;--border:rgba(255,255,255,.10);
 --s1:#3987e5;--s2:#d95926;--s3:#199e70;}
*{box-sizing:border-box}
body{margin:0;background:var(--plane);color:var(--ink);
 font:14px/1.55 system-ui,-apple-system,"PingFang SC","Segoe UI",sans-serif;}
.wrap{max-width:1080px;margin:0 auto;padding:32px 20px 64px}
h1{font-size:22px;margin:0 0 6px;letter-spacing:-.01em}
.sub{color:var(--ink-2);font-size:13px;margin:0 0 24px}
.tiles{display:grid;grid-template-columns:repeat(auto-fit,minmax(150px,1fr));gap:10px;margin-bottom:26px}
.tile{background:var(--surface-1);border:1px solid var(--border);border-radius:10px;padding:12px 14px}
.tile .k{font-size:11px;color:var(--muted);letter-spacing:.02em}
.tile .v{font-size:24px;margin-top:3px;letter-spacing:-.02em}
.tile .x{font-size:11px;color:var(--ink-2);margin-top:2px}
.bar{display:flex;gap:14px;align-items:center;flex-wrap:wrap;margin-bottom:14px;
 font-size:12px;color:var(--ink-2)}
.bar label{display:flex;gap:6px;align-items:center;cursor:pointer;user-select:none}
.panel{background:var(--surface-1);border:1px solid var(--border);border-radius:10px;
 padding:14px 14px 6px;margin-bottom:14px;position:relative}
.ptitle{font-size:14px;margin:0 0 2px}
.pnote{font-size:11.5px;color:var(--muted);margin:0 0 6px}
.legend{display:flex;gap:14px;font-size:12px;color:var(--ink-2);margin:0 0 4px}
.legend i{display:inline-block;width:10px;height:10px;border-radius:2px;margin-right:5px;vertical-align:-1px}
svg{display:block;width:100%;height:auto;overflow:visible}
.tip{position:absolute;pointer-events:none;opacity:0;transition:opacity .1s;
 background:var(--surface-1);border:1px solid var(--border);border-radius:8px;
 padding:8px 10px;font-size:12px;box-shadow:0 6px 20px rgba(0,0,0,.14);z-index:5;
 white-space:nowrap;font-variant-numeric:tabular-nums}
.tip b{font-weight:600}
.tip .row{display:flex;justify-content:space-between;gap:16px;color:var(--ink-2)}
.tip .row span:last-child{color:var(--ink)}
table{border-collapse:collapse;width:100%;font-size:12px;font-variant-numeric:tabular-nums}
th,td{padding:5px 8px;border-bottom:1px solid var(--grid);text-align:right;white-space:nowrap}
th:first-child,td:first-child{text-align:left}
th{color:var(--muted);font-weight:500;position:sticky;top:0;background:var(--surface-1)}
details{background:var(--surface-1);border:1px solid var(--border);border-radius:10px;padding:12px 14px}
summary{cursor:pointer;font-size:13px;color:var(--ink-2)}
.tblwrap{max-height:420px;overflow:auto;margin-top:10px}
.foot{color:var(--muted);font-size:11.5px;margin-top:18px;line-height:1.7}
</style></head><body><div class="wrap">
<h1>涨停次日开盘行为 · 最近一年</h1>
<p class="sub" id="sub"></p>
<div class="tiles" id="tiles"></div>
<div class="bar">
  <label><input type="checkbox" id="smooth"> 5 日平滑（移动平均）</label>
  <label><input type="checkbox" id="mean" checked> 显示全期均值参考线</label>
</div>
<div id="charts"></div>
<details><summary>查看按天数据表（253 个交易日）</summary>
<div class="tblwrap"><table id="tbl"></table></div></details>
<p class="foot" id="foot"></p>
</div>
<script>
const DATA = __DATA__, S = __SUMMARY__;
const pct = (v,d=1)=> v==null? '—' : (v>=0?'':'') + v.toFixed(d) + '%';
const sgn = (v,d=2)=> v==null? '—' : (v>0?'+':'') + v.toFixed(d) + '%';

document.getElementById('sub').textContent =
  `样本：${S.start} ~ ${S.end}，${S.days} 个交易日、${S.events.toLocaleString()} 个涨停事件（沪深主板 + 创业板，`
  + `已排除 ST / 退市整理 / 次日停牌）。基准为涨停日收盘价（前复权）。`;

const TILES = [
  ['次日高开比例', pct(S.up,1), '高开溢价均值 ' + sgn(S.upm)],
  ['次日低开比例', pct(S.dn,1), '低开亏损均值 ' + sgn(S.dnm)],
  ['全天不及前收比例', pct(S.un,1), '该子集收盘亏损均值 ' + sgn(S.unc)],
  ['次日开盘均值', sgn(S.om), '次日收盘均值 ' + sgn(S.cm)],
];
document.getElementById('tiles').innerHTML = TILES.map(t =>
  `<div class="tile"><div class="k">${t[0]}</div><div class="v">${t[1]}</div><div class="x">${t[2]}</div></div>`).join('');

const PANELS = [
  {title:'① 次日开盘方向占比', note:'当日涨停股中，次日开盘价高于/低于涨停日收盘价的家数占比。虚线为全期均值',
   unit:'%', series:[{k:'up',name:'高开比例',c:'var(--s1)'},{k:'dn',name:'低开比例',c:'var(--s2)'}]},
  {title:'② 次日开盘溢价 / 亏损均值', note:'高开股的平均高开幅度、低开股的平均低开幅度（相对涨停日收盘价）。虚线为全期均值',
   unit:'%', zero:true, series:[{k:'upm',name:'高开溢价均值',c:'var(--s1)'},{k:'dnm',name:'低开亏损均值',c:'var(--s2)'}]},
  {title:'③ 次日全天价格都不及前收的比例', note:'次日最高价 < 涨停日收盘价的家数占比（当日买入无论何时都亏）。虚线为全期均值',
   unit:'%', series:[{k:'un',name:'全天不及前收比例',c:'var(--s3)'}]},
  {title:'④ 该部分股票的亏损均值', note:'上述子集的次日收盘价相对涨停日收盘价的平均跌幅；子集为空的交易日断线。虚线为全期均值',
   unit:'%', zero:true, series:[{k:'unc',name:'收盘亏损均值',c:'var(--s3)'}]},
];

const W=920, H=250, M={t:12,r:56,b:26,l:44};
const iw=W-M.l-M.r, ih=H-M.t-M.b;

function ma(arr,w){return arr.map((_,i)=>{
  const s=arr.slice(Math.max(0,i-w+1),i+1).filter(v=>v!=null);
  return s.length? s.reduce((a,b)=>a+b,0)/s.length : null;});}

function render(){
  const smooth=document.getElementById('smooth').checked;
  const showMean=document.getElementById('mean').checked;
  document.getElementById('charts').innerHTML = PANELS.map((p,pi)=>{
    const cols = p.series.map(s=>{
      const raw = DATA.map(r=>r[s.k]);
      return smooth? ma(raw,5) : raw;});
    const flat = cols.flat().filter(v=>v!=null);
    let lo=Math.min(...flat), hi=Math.max(...flat);
    if(p.zero){lo=Math.min(lo,0);hi=Math.max(hi,0);}
    const pad=(hi-lo)*0.10||1; lo-=pad; hi+=pad;
    const X=i=> M.l + (DATA.length<2?0:i*iw/(DATA.length-1));
    const Y=v=> M.t + ih - (v-lo)/(hi-lo)*ih;

    // y 网格
    const ticks=[]; const step=niceStep((hi-lo)/4);
    for(let t=Math.ceil(lo/step)*step; t<=hi; t+=step) ticks.push(+t.toFixed(6));
    const grid=ticks.map(t=>`<line x1="${M.l}" x2="${M.l+iw}" y1="${Y(t)}" y2="${Y(t)}"
      stroke="${Math.abs(t)<1e-9&&p.zero?'var(--axis)':'var(--grid)'}" stroke-width="1"/>
      <text x="${M.l-8}" y="${Y(t)+4}" text-anchor="end" fill="var(--muted)" font-size="11">${t}${p.unit}</text>`).join('');

    // x 月份刻度（间距不足则跳过，避免叠字）
    let last='', lastX=-1e9; const xt=[];
    DATA.forEach((r,i)=>{const m=r.d.slice(0,7); if(m!==last){last=m;
      if(X(i)-lastX < 40) return; lastX=X(i);
      xt.push(`<text x="${X(i)}" y="${H-M.b+16}" text-anchor="middle" fill="var(--muted)" font-size="10.5">${r.d.slice(2,4)}/${r.d.slice(5,7)}</text>`);}});

    const paths=p.series.map((s,si)=>{
      const vals=cols[si]; let d='',open=false;
      vals.forEach((v,i)=>{ if(v==null){open=false;return;}
        d += (open?'L':'M') + X(i).toFixed(1) + ' ' + Y(v).toFixed(1) + ' '; open=true;});
      return `<path d="${d}" fill="none" stroke="${s.c}" stroke-width="2"
        stroke-linejoin="round" stroke-linecap="round"/>`;}).join('');

    const means=showMean? p.series.map((s,si)=>{
      const m=S[s.k];   // 全期事件加权均值，与顶部指标卡同口径
      if(m==null||m<lo||m>hi) return '';
      return `<line x1="${M.l}" x2="${M.l+iw}" y1="${Y(m)}" y2="${Y(m)}" stroke="${s.c}"
        stroke-width="1.5" stroke-dasharray="4 4" opacity=".55"/>
        <text x="${M.l+iw+8}" y="${Y(m)+4}" fill="${s.c}" font-size="11"
        font-variant-numeric="tabular-nums">${m>0&&p.zero?'+':''}${m.toFixed(1)}${p.unit}</text>`;}).join(''):'';

    const legend = p.series.length>1 ? `<div class="legend">` + p.series.map(s=>
      `<span><i style="background:${s.c}"></i>${s.name}</span>`).join('') + `</div>` : '';

    const hover = p.series.map((s,si)=>`<circle class="hp" data-si="${si}" r="4.5" fill="${s.c}"
      stroke="var(--surface-1)" stroke-width="2" opacity="0"/>`).join('');

    return `<div class="panel" data-pi="${pi}">
      <h2 class="ptitle">${p.title}</h2><p class="pnote">${p.note}</p>${legend}
      <svg viewBox="0 0 ${W} ${H}" preserveAspectRatio="none">
        ${grid}${xt}
        <line x1="${M.l}" x2="${M.l+iw}" y1="${M.t+ih}" y2="${M.t+ih}" stroke="var(--axis)" stroke-width="1"/>
        ${means}${paths}
        <line class="cross" y1="${M.t}" y2="${M.t+ih}" stroke="var(--axis)" stroke-width="1" opacity="0"/>
        ${hover}
        <rect class="hit" x="${M.l}" y="${M.t}" width="${iw}" height="${ih}" fill="transparent"/>
      </svg><div class="tip"></div></div>`;
  }).join('');
  wire(smooth);
}

function niceStep(x){const p=Math.pow(10,Math.floor(Math.log10(x)));const n=x/p;
  return (n<=1?1:n<=2?2:n<=2.5?2.5:n<=5?5:10)*p;}

function wire(smooth){
  document.querySelectorAll('.panel').forEach(panel=>{
    const pi=+panel.dataset.pi, p=PANELS[pi];
    const svg=panel.querySelector('svg'), tip=panel.querySelector('.tip');
    const cross=panel.querySelector('.cross'), pts=[...panel.querySelectorAll('.hp')];
    const cols=p.series.map(s=>{const raw=DATA.map(r=>r[s.k]); return smooth?ma(raw,5):raw;});
    const flat=cols.flat().filter(v=>v!=null);
    let lo=Math.min(...flat),hi=Math.max(...flat);
    if(p.zero){lo=Math.min(lo,0);hi=Math.max(hi,0);}
    const pad=(hi-lo)*0.10||1; lo-=pad; hi+=pad;
    const X=i=>M.l+i*iw/(DATA.length-1), Y=v=>M.t+ih-(v-lo)/(hi-lo)*ih;

    svg.addEventListener('pointermove',e=>{
      const b=svg.getBoundingClientRect();
      const x=(e.clientX-b.left)/b.width*W;
      let i=Math.round((x-M.l)/iw*(DATA.length-1));
      i=Math.max(0,Math.min(DATA.length-1,i));
      const r=DATA[i];
      cross.setAttribute('x1',X(i)); cross.setAttribute('x2',X(i)); cross.setAttribute('opacity','1');
      pts.forEach((c,si)=>{const v=cols[si][i];
        if(v==null){c.setAttribute('opacity','0');return;}
        c.setAttribute('cx',X(i)); c.setAttribute('cy',Y(v)); c.setAttribute('opacity','1');});
      tip.innerHTML = `<b>${r.d}</b> → 次日 ${r.nd}<br>`
        + `<div class="row"><span>涨停家数</span><span>${r.n}</span></div>`
        + p.series.map((s,si)=>{const v=cols[si][i];
            return `<div class="row"><span>${s.name}${smooth?'(MA5)':''}</span><span>${v==null?'—':(p.zero&&v>0?'+':'')+v.toFixed(2)}%</span></div>`;}).join('');
      tip.style.opacity='1';
      const px=(X(i)/W)*b.width;
      tip.style.left = Math.min(Math.max(px-70,4), b.width-176) + 'px';
      tip.style.top = '54px';
    });
    svg.addEventListener('pointerleave',()=>{
      tip.style.opacity='0'; cross.setAttribute('opacity','0');
      pts.forEach(c=>c.setAttribute('opacity','0'));});
  });
}

document.getElementById('smooth').addEventListener('change',render);
document.getElementById('mean').addEventListener('change',render);
render();

const COLS=[['d','涨停日'],['nd','次日'],['n','家数'],['up','高开%'],['upm','高开溢价均值%'],
  ['dn','低开%'],['dnm','低开亏损均值%'],['flat','平开%'],['om','开盘均值%'],
  ['un','全天不及前收%'],['unc','该子集收盘亏损均值%'],['cm','收盘均值%']];
document.getElementById('tbl').innerHTML =
  '<thead><tr>'+COLS.map(c=>`<th>${c[1]}</th>`).join('')+'</tr></thead><tbody>'
  + DATA.map(r=>'<tr>'+COLS.map(c=>{const v=r[c[0]];
      return `<td>${typeof v==='number'&&c[0]!=='n'? v.toFixed(2): (v==null?'—':v)}</td>`;}).join('')+'</tr>').join('')
  + '</tbody>';

document.getElementById('foot').innerHTML =
  '口径：涨停事件取同花顺涨停池（pool=zt），板块限沪主板 600/601/603/605、深主板 000/001/002/003、创业板 300/301；'
  + '价格取东财前复权日K。「次日」为交易日历上的下一个交易日，次日无 K 线（停牌/退市）的事件已剔除。'
  + '汇总数值按事件数加权，与逐事件口径一致；折线为逐日截面统计，单日样本 26~158 家。'
  + '<br>研究口径统计，非投资建议。';
</script></body></html>
"""

DST.write_text(
    HTML.replace("__DATA__", json.dumps(data, ensure_ascii=False))
        .replace("__SUMMARY__", json.dumps(summary, ensure_ascii=False)),
    encoding="utf-8")
print(f"✅ {DST}")
