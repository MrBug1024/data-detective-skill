"""
wall_renderer.py — 马赛克调查墙渲染器 v3
数据溯源交互布局：暗夜主题 + Canvas力导向图 + 交互面板 + 时间线 + 证据库
"""
import json, os

def generate_wall(report: dict, output_path: str):
    payload = json.dumps(report, ensure_ascii=False, default=str).replace('<', '\\u003c').replace('>', '\\u003e').replace('&', '\\u0026')
    # 模板中是 /*__REPORT__*/null — 把整个 /*__REPORT__*/null 替换掉
    html = _TEMPLATE.replace("/*__REPORT__*/null", payload)
    with open(output_path, "w", encoding="utf-8") as f:
        f.write(html)
    print(f"[INFO] Wall → {output_path}")

_TEMPLATE = r"""<!DOCTYPE html>
<html lang="zh-CN">
<head>
<meta charset="UTF-8">
<meta name="viewport" content="width=device-width,initial-scale=1">
<title>数据溯源 · 马赛克调查墙</title>
<style>
*{box-sizing:border-box;margin:0;padding:0}
:root{
  --bg:#07090f;--s1:#0d1117;--s2:#131820;--s3:#1a2130;
  --b1:#1f2937;--b2:#263040;--b3:#2e3a50;
  --t1:#e2e8f0;--t2:#94a3b8;--t3:#4a5568;
  --red:#ef4444;--red2:#fca5a5;--amber:#f59e0b;--amber2:#fde68a;
  --green:#10b981;--green2:#6ee7b7;--blue:#3b82f6;--blue2:#93c5fd;
  --purple:#8b5cf6;--purple2:#c4b5fd;--cyan:#06b6d4;--cyan2:#67e8f9;
  --pink:#ec4899;--orange:#f97316;
  --nc-result:#1d4ed8;--nc-source:#065f46;--nc-rule:#4c1d95;--nc-anomaly:#7f1d1d;
  --ec-exact:#10b981;--ec-direct:#3b82f6;--ec-joint:#ec4899;
  --ec-fuzzy:#f59e0b;--ec-semantic:#8b5cf6;--ec-rule:#06b6d4;
  --ec-temporal:#f97316;--ec-entity:#a855f7;--ec-inferred:#64748b;
}
html,body{height:100%;overflow:hidden;background:var(--bg);color:var(--t1);
  font-family:'SF Pro Display',-apple-system,'Segoe UI',sans-serif;font-size:13px}

/* scanline */
body::after{content:'';position:fixed;inset:0;pointer-events:none;z-index:9999;
  background:repeating-linear-gradient(180deg,transparent 0,transparent 1px,
  rgba(0,0,0,.03) 1px,rgba(0,0,0,.03) 2px);animation:scanroll 8s linear infinite}
@keyframes scanroll{from{background-position:0 0}to{background-position:0 100px}}

/* ── TOPBAR ── */
#topbar{height:56px;background:rgba(13,17,23,.97);border-bottom:1px solid var(--b2);
  display:flex;align-items:center;padding:0 18px;gap:14px;position:relative;z-index:200;
  backdrop-filter:blur(16px)}
.tb-logo{display:flex;align-items:center;gap:10px;white-space:nowrap}
.tb-logo-icon{width:32px;height:32px;border-radius:8px;
  background:linear-gradient(135deg,#1d4ed8,#7c3aed);
  display:flex;align-items:center;justify-content:center;font-size:16px;
  box-shadow:0 0 12px rgba(59,130,246,.4)}
.tb-logo-text{font-size:15px;font-weight:800;letter-spacing:1.5px;color:var(--t1);
  text-transform:uppercase}
.tb-sep{width:1px;height:28px;background:var(--b2)}
.tb-case{flex:1;min-width:0}
.tb-title{font-size:13px;font-weight:700;white-space:nowrap;overflow:hidden;
  text-overflow:ellipsis;color:var(--t1)}
.tb-meta{font-size:10px;color:var(--t3);margin-top:2px;font-family:monospace}
.tb-right{display:flex;align-items:center;gap:12px;flex-shrink:0}
.tb-conf{display:flex;align-items:center;gap:8px}
.conf-arc-wrap{position:relative;width:44px;height:44px;flex-shrink:0}
.conf-arc-wrap svg{transform:rotate(-90deg)}
.conf-val{position:absolute;inset:0;display:flex;align-items:center;
  justify-content:center;font-size:10px;font-weight:800;font-family:monospace}
.tb-badge{padding:4px 12px;border-radius:20px;font-size:10px;font-weight:700;
  letter-spacing:.8px;text-transform:uppercase;white-space:nowrap}
.badge-tentative{background:rgba(245,158,11,.12);color:var(--amber);
  border:1px solid rgba(245,158,11,.3)}
.badge-confirmed{background:rgba(16,185,129,.12);color:var(--green);
  border:1px solid rgba(16,185,129,.3)}
.badge-refuted{background:rgba(239,68,68,.12);color:var(--red);
  border:1px solid rgba(239,68,68,.3)}

/* ── TABBAR ── */
#tabbar{height:40px;background:var(--s1);border-bottom:1px solid var(--b1);
  display:flex;align-items:flex-end;padding:0 14px;gap:1px;z-index:100}
.tb{padding:7px 14px;font-size:11px;font-weight:700;color:var(--t3);cursor:pointer;
  border-radius:6px 6px 0 0;border:1px solid transparent;border-bottom:none;
  letter-spacing:.5px;user-select:none;transition:all .15s;white-space:nowrap}
.tb:hover{color:var(--t2);background:var(--s2)}
.tb.on{color:var(--blue2);background:var(--s2);border-color:var(--b2)}
.tb-n{display:inline-flex;align-items:center;justify-content:center;
  width:17px;height:17px;border-radius:50%;font-size:9px;font-weight:800;
  margin-left:5px;background:var(--b2);color:var(--t3);transition:all .15s}
.tb.on .tb-n{background:var(--blue);color:#fff}

/* ── LAYOUT ── */
#workspace{display:flex;height:calc(100vh - 96px);overflow:hidden}
#sidebar{width:272px;flex-shrink:0;border-right:1px solid var(--b1);
  overflow-y:auto;background:var(--s1);display:flex;flex-direction:column}
#main{flex:1;overflow:hidden;position:relative}
#detpanel{width:306px;flex-shrink:0;border-left:1px solid var(--b1);
  overflow-y:auto;background:var(--s1)}

.ph{padding:9px 12px;font-size:10px;font-weight:700;letter-spacing:1.2px;
  color:var(--t3);text-transform:uppercase;border-bottom:1px solid var(--b1);
  background:rgba(7,9,15,.6);position:sticky;top:0;z-index:10;
  display:flex;align-items:center;gap:8px;backdrop-filter:blur(4px)}
.ph-dot{width:5px;height:5px;border-radius:50%}

/* ── NODE CARDS ── */
.nc{margin:5px 7px;border-radius:8px;border:1px solid var(--b1);
  background:linear-gradient(160deg,var(--s2),var(--s3));
  cursor:pointer;transition:all .18s;overflow:hidden;position:relative}
.nc::before{content:'';position:absolute;left:0;top:0;bottom:0;width:3px}
.nc.type-result::before{background:var(--blue)}
.nc.type-source::before{background:var(--green)}
.nc.type-rule::before{background:var(--purple)}
.nc.type-anomaly::before{background:var(--red)}
.nc:hover{border-color:var(--b3);transform:translateX(3px);
  box-shadow:0 4px 16px rgba(0,0,0,.4)}
.nc.sel{border-color:var(--blue);
  box-shadow:0 0 0 1px rgba(59,130,246,.25),0 4px 20px rgba(0,0,0,.4)}
.nc-top{padding:8px 10px 6px 14px;display:flex;align-items:center;gap:8px}
.nc-icon{font-size:14px;flex-shrink:0}
.nc-label{font-size:11px;font-weight:700;flex:1;white-space:nowrap;
  overflow:hidden;text-overflow:ellipsis;color:var(--t1)}
.nc-tag{font-size:9px;font-weight:700;padding:2px 7px;border-radius:10px;
  letter-spacing:.4px;text-transform:uppercase;flex-shrink:0}
.tag-result{background:rgba(29,78,216,.25);color:#93c5fd}
.tag-source{background:rgba(6,95,70,.25);color:#6ee7b7}
.tag-rule{background:rgba(76,29,149,.25);color:#c4b5fd}
.tag-anomaly{background:rgba(127,29,29,.25);color:#fca5a5}
.nc-pct{font-size:10px;font-weight:800;font-family:monospace;flex-shrink:0}
.nc-prog{height:2px;margin:0 14px 6px;background:var(--b1);border-radius:1px}
.nc-prog-fill{height:100%;border-radius:1px;transition:width .5s ease}
.nc-fields{padding:0 14px 8px}
.nf{display:flex;gap:6px;font-size:10px;line-height:1.5}
.nfn{color:var(--t3);min-width:52px;flex-shrink:0;overflow:hidden;
  text-overflow:ellipsis;white-space:nowrap}
.nfv{color:var(--t2);word-break:break-all}
.nfv.hl{color:var(--green2);font-weight:700}

/* ── PANES ── */
.pane{display:none;height:100%}
.pane.on{display:flex}
#pane-graph{flex-direction:row}
#pane-timeline,#pane-evidence,#pane-conclusion{
  overflow-y:auto;padding:20px 24px;background:var(--s1)}

/* ── CANVAS ── */
#cvswrap{flex:1;position:relative;overflow:hidden;background:var(--bg)}
#cvs{position:absolute;inset:0;width:100%;height:100%}
.cvc{position:absolute;bottom:14px;right:14px;display:flex;flex-direction:column;gap:6px}
.cvbtn{width:30px;height:30px;border-radius:7px;border:1px solid var(--b2);
  background:rgba(13,17,23,.85);color:var(--t2);font-size:14px;cursor:pointer;
  display:flex;align-items:center;justify-content:center;
  backdrop-filter:blur(6px);transition:all .15s}
.cvbtn:hover{border-color:var(--blue);color:var(--blue2)}
#legend{position:absolute;bottom:14px;left:14px;background:rgba(7,9,15,.88);
  border:1px solid var(--b2);border-radius:8px;padding:7px 10px;
  display:flex;flex-wrap:wrap;gap:6px;max-width:380px;backdrop-filter:blur(8px)}
.lg{display:flex;align-items:center;gap:5px;font-size:9px;color:var(--t3)}
.lg-dot{width:7px;height:7px;border-radius:50%;flex-shrink:0}
.lg-line{width:18px;height:2px;border-radius:1px;flex-shrink:0}
.lg-dash{width:18px;border-top:2px dashed;flex-shrink:0}

/* ── EDGE TOOLTIP ── */
#etip{position:fixed;background:rgba(13,17,23,.96);border:1px solid var(--b3);
  border-radius:8px;padding:9px 13px;font-size:11px;pointer-events:none;
  z-index:999;max-width:280px;display:none;
  box-shadow:0 12px 40px rgba(0,0,0,.7);backdrop-filter:blur(10px)}
#etip .et{font-weight:700;margin-bottom:3px}
#etip .es{font-family:monospace;font-size:10px}
#etip .ev{color:var(--t3);line-height:1.5;margin-top:3px;font-size:10px}

/* ── DETAIL PANEL ── */
.dp{padding:11px 13px;border-bottom:1px solid var(--b1)}
.dp h3{font-size:9px;font-weight:700;letter-spacing:1.2px;color:var(--t3);
  text-transform:uppercase;margin-bottom:9px}
.dp-title{font-size:14px;font-weight:800;color:var(--t1);margin-bottom:3px}
.dp-sub{font-size:11px;color:var(--t3);margin-bottom:10px}
.chips{display:flex;flex-wrap:wrap;gap:4px;margin-bottom:9px}
.chip{display:inline-flex;align-items:center;gap:4px;padding:3px 8px;
  border-radius:10px;font-size:10px;background:var(--s3);
  border:1px solid var(--b2);color:var(--t3);font-family:monospace}
.fbox{background:var(--bg);border-radius:6px;overflow:hidden;margin-bottom:9px}
.fr{display:flex;gap:8px;padding:4px 8px;font-size:10px}
.fr:nth-child(odd){background:rgba(255,255,255,.02)}
.fn{color:var(--t3);min-width:72px;flex-shrink:0}
.fv{color:var(--t2);word-break:break-all}
.fv.m{color:var(--green2);font-weight:700}
.rawbox{font-size:10px;color:var(--t3);background:var(--bg);
  border-radius:6px;padding:8px;line-height:1.6;word-break:break-all;
  font-family:'JetBrains Mono',monospace;max-height:90px;overflow-y:auto;
  border:1px solid var(--b1)}

/* evidence / edge items */
.evcard{border-radius:7px;padding:8px 10px;margin-bottom:6px;
  border-left:3px solid;background:var(--s2)}
.evhead{display:flex;align-items:center;gap:8px;margin-bottom:5px}
.evbadge{font-size:9px;font-weight:700;padding:2px 7px;border-radius:10px;
  letter-spacing:.4px;text-transform:uppercase}
.evpct{font-family:monospace;font-size:11px;font-weight:700}
.evtext{font-size:10px;color:var(--t3);line-height:1.5}
.evvals{display:flex;align-items:center;gap:6px;font-size:10px;margin-top:4px;
  font-family:monospace}
.evfrom{color:var(--amber2)}.evarr{color:var(--t3)}.evto{color:var(--green2)}

/* ── TIMELINE ── */
.tl-wrap{max-width:680px}
.tl-head{font-size:13px;font-weight:800;color:var(--blue2);
  letter-spacing:1px;text-transform:uppercase;margin-bottom:20px}
.tl-item{display:flex;gap:12px;margin-bottom:16px;position:relative}
.tl-item::before{content:'';position:absolute;left:8px;top:22px;
  width:1px;bottom:-16px;background:var(--b2)}
.tl-item:last-child::before{display:none}
.tl-dot{width:17px;height:17px;border-radius:50%;border:2px solid;
  flex-shrink:0;margin-top:3px;background:var(--bg);position:relative;z-index:1}
.tl-body{flex:1;min-width:0}
.tl-time{font-size:10px;color:var(--t3);font-family:monospace;margin-bottom:3px}
.tl-event{font-size:12px;color:var(--t1);line-height:1.5}
.tl-src{font-size:10px;color:var(--t3);margin-top:3px}
.tl-ents{display:flex;flex-wrap:wrap;gap:4px;margin-top:4px}
.tl-ent{font-size:9px;padding:1px 6px;border-radius:8px;
  background:rgba(59,130,246,.1);color:var(--blue2);border:1px solid rgba(59,130,246,.2)}

/* ── EVIDENCE TAB ── */
.ev-wrap{max-width:680px}
.ev-head{font-size:13px;font-weight:800;color:var(--blue2);
  letter-spacing:1px;text-transform:uppercase;margin-bottom:16px}
.ev-grid{display:grid;gap:8px}

/* ── CONCLUSION ── */
.cc-wrap{max-width:680px}
.sec-head{font-size:12px;font-weight:800;letter-spacing:1px;
  text-transform:uppercase;margin:0 0 12px;padding-bottom:6px;
  border-bottom:1px solid var(--b2)}
.ccard{border-radius:9px;padding:13px;margin-bottom:10px;
  border:1px solid var(--b2);background:var(--s2);
  transition:border-color .15s}
.ccard:hover{border-color:var(--b3)}
.chead{display:flex;align-items:center;gap:9px;margin-bottom:8px}
.cstmt{font-size:12px;font-weight:700;color:var(--t1);line-height:1.55;margin-bottom:5px}
.cpct{font-family:monospace;font-size:11px;font-weight:700}
.cbar{height:3px;background:var(--b1);border-radius:2px;margin:6px 0}
.cbar-f{height:100%;border-radius:2px;transition:width .6s ease}
.cdetail{font-size:10px;color:var(--t3);line-height:1.5;margin-top:4px}
.ctag{font-size:9px;padding:2px 8px;border-radius:10px;font-weight:700;
  text-transform:uppercase;letter-spacing:.4px}
.ct-tent{background:rgba(245,158,11,.12);color:var(--amber);border:1px solid rgba(245,158,11,.25)}
.ct-conf{background:rgba(16,185,129,.12);color:var(--green);border:1px solid rgba(16,185,129,.25)}
.ct-ref{background:rgba(239,68,68,.12);color:var(--red);border:1px solid rgba(239,68,68,.25)}

/* anomaly */
.acard{border-radius:8px;padding:10px 12px;margin-bottom:8px;
  background:rgba(127,29,29,.12);border:1px solid rgba(239,68,68,.2)}
.atag{font-size:9px;font-weight:700;padding:2px 8px;border-radius:10px;
  background:rgba(239,68,68,.15);color:var(--red2);text-transform:uppercase;
  letter-spacing:.4px;margin-bottom:5px;display:inline-block}
.adesc{font-size:11px;color:var(--t2);line-height:1.45}
.aloc{font-size:10px;color:var(--t3);margin-top:3px;font-family:monospace}

/* relation table */
.rtable{width:100%;border-collapse:collapse;font-size:11px;margin-top:8px}
.rtable th{padding:6px 9px;text-align:left;font-size:9px;color:var(--t3);
  text-transform:uppercase;letter-spacing:.8px;
  border-bottom:1px solid var(--b2);background:var(--bg)}
.rtable td{padding:6px 9px;border-bottom:1px solid var(--b1);color:var(--t2)}
.rtable tr:hover td{background:rgba(255,255,255,.02);color:var(--t1)}

/* alerts */
.alert{border-radius:7px;padding:8px 11px;font-size:11px;
  margin-bottom:6px;display:flex;align-items:flex-start;gap:8px;line-height:1.45}
.al-ok{background:rgba(16,185,129,.07);border:1px solid rgba(16,185,129,.2);color:var(--green2)}
.al-warn{background:rgba(245,158,11,.07);border:1px solid rgba(245,158,11,.2);color:var(--amber2)}
.al-err{background:rgba(239,68,68,.07);border:1px solid rgba(239,68,68,.2);color:var(--red2)}
.al-info{background:rgba(59,130,246,.07);border:1px solid rgba(59,130,246,.2);color:var(--blue2)}

/* scrollbar */
::-webkit-scrollbar{width:4px;height:4px}
::-webkit-scrollbar-track{background:transparent}
::-webkit-scrollbar-thumb{background:var(--b2);border-radius:2px}
</style>
</head>
<body>

<!-- TOPBAR -->
<div id="topbar">
  <div class="tb-logo">
    <div class="tb-logo-icon">🔍</div>
    <div class="tb-logo-text">Detective</div>
  </div>
  <div class="tb-sep"></div>
  <div class="tb-case">
    <div class="tb-title" id="tt"></div>
    <div class="tb-meta">CASE <span id="tid" style="color:var(--cyan2)"></span> &nbsp;·&nbsp; <span id="ttype" style="color:var(--purple2)"></span></div>
  </div>
  <div class="tb-right">
    <div id="tbadge" class="tb-badge badge-tentative">TENTATIVE</div>
    <div class="conf-arc-wrap">
      <svg width="44" height="44" viewBox="0 0 44 44">
        <circle cx="22" cy="22" r="18" fill="none" stroke="var(--b2)" stroke-width="3"/>
        <circle id="carc" cx="22" cy="22" r="18" fill="none" stroke="var(--green)"
          stroke-width="3" stroke-linecap="round"
          stroke-dasharray="113.1" stroke-dashoffset="113.1"
          style="transition:all 1s ease"/>
      </svg>
      <div class="conf-val" id="cval"></div>
    </div>
  </div>
</div>

<!-- TABBAR -->
<div id="tabbar">
  <div class="tb on" data-t="graph" onclick="sw('graph')">🕸 关系图<span class="tb-n" id="bn-graph">0</span></div>
  <div class="tb" data-t="timeline" onclick="sw('timeline')">📅 时间线<span class="tb-n" id="bn-timeline">0</span></div>
  <div class="tb" data-t="evidence" onclick="sw('evidence')">🔬 证据库<span class="tb-n" id="bn-evidence">0</span></div>
  <div class="tb" data-t="conclusion" onclick="sw('conclusion')">💡 结论<span class="tb-n" id="bn-conclusion">0</span></div>
</div>

<!-- WORKSPACE -->
<div id="workspace">

  <!-- SIDEBAR -->
  <div id="sidebar">
    <div class="ph"><div class="ph-dot" style="background:var(--blue)"></div>
      调查节点<span style="margin-left:auto;font-size:10px;color:var(--t3)" id="ncount">0</span>
    </div>
    <div id="nlist"></div>
    <div class="ph" style="margin-top:4px">
      <div class="ph-dot" style="background:var(--red)"></div>缺失 / 冲突
    </div>
    <div id="ilist" style="padding:6px 7px"></div>
  </div>

  <!-- MAIN -->
  <div id="main">

    <!-- graph pane -->
    <div class="pane on" id="pane-graph">
      <div id="cvswrap">
        <canvas id="cvs"></canvas>
        <div class="cvc">
          <div class="cvbtn" onclick="rv()" title="复位">⌖</div>
          <div class="cvbtn" onclick="zi()" title="放大">+</div>
          <div class="cvbtn" onclick="zo()" title="缩小">−</div>
        </div>
        <div id="legend"></div>
      </div>
      <!-- detail panel inside graph pane -->
      <div id="detpanel">
        <div class="ph"><div class="ph-dot" style="background:var(--purple)"></div>节点详情</div>
        <div id="det">
          <div class="dp" style="padding:16px">
            <p style="color:var(--t3);font-size:11px;line-height:1.7">
              ← 点击左侧节点或图中节点<br>查看字段详情与证据片段
            </p>
          </div>
        </div>
        <div class="ph" style="margin-top:2px">
          <div class="ph-dot" style="background:var(--amber)"></div>调查结论
        </div>
        <div id="hyp" style="padding:7px"></div>
      </div>
    </div>

    <!-- timeline pane -->
    <div class="pane" id="pane-timeline">
      <div class="tl-wrap">
        <div class="tl-head">📅 事件时间线</div>
        <div id="tl-content"></div>
      </div>
    </div>

    <!-- evidence pane -->
    <div class="pane" id="pane-evidence">
      <div class="ev-wrap">
        <div class="ev-head">🔬 证据库</div>
        <div class="ev-grid" id="ev-content"></div>
      </div>
    </div>

    <!-- conclusion pane -->
    <div class="pane" id="pane-conclusion">
      <div class="cc-wrap">
        <div class="sec-head" style="color:var(--blue2)">💡 调查结论</div>
        <div id="cc-content"></div>
        <div class="sec-head" style="color:var(--amber2);margin-top:24px">⚠ 异常发现</div>
        <div id="an-content"></div>
        <div class="sec-head" style="color:var(--purple2);margin-top:24px">🕸 关系清单</div>
        <table class="rtable">
          <thead><tr><th>实体A</th><th>关系</th><th>实体B</th><th>置信度</th><th>来源</th></tr></thead>
          <tbody id="rel-body"></tbody>
        </table>
      </div>
    </div>

  </div>
</div>

<div id="etip"></div>

<script>
const D = /*__REPORT__*/null;

/* ── COLOURS ── */
const NC={result:'#3b82f6',source:'#10b981',rule:'#8b5cf6',anomaly:'#ef4444',intermediate:'#475569'};
const EC={
  exact_id:'#10b981',direct:'#3b82f6',joint:'#ec4899',
  fuzzy:'#f59e0b',semantic:'#8b5cf6',rule:'#06b6d4',
  temporal:'#f97316',entity:'#a855f7',inferred:'#64748b',indirect:'#60a5fa'
};
const EDASH={fuzzy:1,semantic:1,indirect:1,inferred:1,temporal:0};
const EICONS={result:'◎',source:'◉',rule:'◈',anomaly:'⚠',intermediate:'◦'};

function cc(v){return typeof v!=='number'?'#94a3b8':v>=.75?'#10b981':v>=.5?'#f59e0b':'#ef4444'}
function score(v){return typeof v==='number'&&Number.isFinite(v)?v.toFixed(3)+' 分':'未评估'}
function locText(loc){return typeof loc==='object'&&loc!==null?[loc.file,loc.sheet,loc.row?'Row:'+loc.row:'',loc.page?'Page:'+loc.page:'',loc.paragraph?'Para:'+loc.paragraph:''].filter(Boolean).join(' | '):loc||''}

/* ── INIT ── */
function nodes(){return D.chain?.nodes||D.nodes||[]}
function edges(){return D.chain?.edges||D.edges||[]}
function evs(){return D.evidence||[]}

window.addEventListener('load',()=>{
  if(!D){document.body.innerHTML='<p style="color:red;padding:20px">数据加载失败</p>';return}
  initTop(); initSidebar(); initTimeline(); initEvidence();
  initConclusion(); initCanvas(); buildLegend();
});

function initTop(){
  const t=D.target||D.result_ref?.file||'';
  document.getElementById('tt').textContent=t;
  document.getElementById('tid').textContent=D.investigation_id||D.chain_id||'—';
  document.getElementById('ttype').textContent='DATA LINEAGE';
  const cf=D.confidence;
  const arc=document.getElementById('carc');
  const C=2*Math.PI*18;
  setTimeout(()=>{
    arc.setAttribute('stroke-dashoffset',C*(1-(cf??0)));
    arc.setAttribute('stroke',cc(cf));
  },100);
  const cv=document.getElementById('cval');
  cv.textContent=score(cf);
  cv.title=D.confidence_semantics||'启发式字段匹配分，非概率';
  cv.style.color=cc(cf);
  const st=(D.conclusions||[])[0]?.status||(D.hypotheses||[])[0]?.status||'tentative';
  const b=document.getElementById('tbadge');
  b.textContent=D.analysis_stage==='evidence_packet_only'?'待研判':({tentative:'🔶 TENTATIVE',confirmed:'✅ CONFIRMED',refuted:'❌ REFUTED'}[st]||'待评估');
  b.className='tb-badge badge-'+st;
  // badges
  document.getElementById('bn-graph').textContent=nodes().length;
  document.getElementById('bn-timeline').textContent=(D.timeline||[]).length;
  document.getElementById('bn-evidence').textContent=evs().length;
  const nc=(D.conclusions||D.hypotheses||[]).length;
  document.getElementById('bn-conclusion').textContent=nc;
  document.getElementById('ncount').textContent=nodes().length;
}

/* ── SIDEBAR ── */
function initSidebar(){
  const ns=nodes(), es=evs();
  const hlFields=new Set(es.map(e=>e.field_name));
  document.getElementById('nlist').innerHTML=ns.map(n=>{
    const c=n.confidence,col=cc(c);
    const tp=n.node_type||'source';
    const fields=(n.field_names||[]).slice(0,4).map((fn,i)=>{
      const fv=String(n.field_values?.[i]||'').substring(0,48);
      const hl=hlFields.has(fn)?'hl':'';
      return `<div class="nf"><span class="nfn">${x(fn)}</span><span class="nfv ${hl}">${x(fv)}</span></div>`;
    }).join('');
    return `<div class="nc type-${tp}" id="nc-${n.node_id}" onclick="sel('${n.node_id}')">
      <div class="nc-top">
        <div class="nc-icon">${EICONS[tp]||'◉'}</div>
        <div class="nc-label" title="${x(n.label)}">${x(n.label||n.node_id)}</div>
        <div class="nc-tag tag-${tp}">${tp}</div>
        <div class="nc-pct" style="color:${col}">${score(c)}</div>
      </div>
      <div class="nc-prog"><div class="nc-prog-fill" style="width:${c*100}%;background:${col}"></div></div>
      <div class="nc-fields">${fields}</div>
    </div>`;
  }).join('');

  // issues
  const m=D.missing||[],cf=D.conflicts||[];
  let h='';
  if(!m.length&&!cf.length) h='<div class="alert al-ok"><span>✓</span>无缺失或冲突</div>';
  m.slice(0,4).forEach(i=>h+=`<div class="alert al-warn"><span>⚠</span><div><div>${x(i.reason||i.type||'')}</div><div style="font-size:9px;opacity:.7;margin-top:1px">${x(i.file||i.field||'')}</div></div></div>`);
  cf.slice(0,3).forEach(i=>h+=`<div class="alert al-err"><span>✕</span>${x(i.reason||'')}</div>`);
  document.getElementById('ilist').innerHTML=h;
}

/* ── TIMELINE ── */
function initTimeline(){
  const tl=D.timeline||[];
  const el=document.getElementById('tl-content');
  if(!tl.length){el.innerHTML='<p style="color:var(--t3);font-size:12px">暂无时间线数据</p>';return}
  const cols=['var(--blue)','var(--green)','var(--amber)','var(--purple)','var(--cyan)','var(--pink)'];
  el.innerHTML=tl.map((t,i)=>`
    <div class="tl-item">
      <div class="tl-dot" style="border-color:${cols[i%cols.length]}"></div>
      <div class="tl-body">
        <div class="tl-time">${x(t.time||'')}</div>
        <div class="tl-event">${x(t.event||t.description||t.content||'')}</div>
        ${t.source?`<div class="tl-src">📍 ${x(t.source)}</div>`:''}
        ${(t.entities||[]).length?`<div class="tl-ents">${t.entities.slice(0,4).map(e=>`<span class="tl-ent">${x(e)}</span>`).join('')}</div>`:''}
      </div>
    </div>`).join('');
}

/* ── EVIDENCE ── */
function initEvidence(){
  const ev=evs();
  const el=document.getElementById('ev-content');
  if(!ev.length){el.innerHTML='<p style="color:var(--t3);font-size:12px">暂无证据片段</p>';return}
  el.innerHTML=ev.map(e=>{
    const c=EC[e.match_type]||'#64748b';
    return `<div class="evcard" style="border-color:${c}">
      <div class="evhead">
        <span class="evbadge" style="background:${c}20;color:${c}">${x(e.match_type||e.kind||'?')}</span>
        <span class="evpct" style="color:${cc(e.score)}">${score(e.score)}</span>
        <span style="font-size:9px;color:var(--t3);margin-left:auto;font-family:monospace">${x(locText(e.source_location))}</span>
      </div>
      ${e.value||e.matched_value?`<div class="evvals"><span class="evfrom">${x(String(e.value||'').substring(0,50))}</span><span class="evarr"> → </span><span class="evto">${x(String(e.matched_value||'').substring(0,50))}</span></div>`:''}
      <div class="evtext">${x(e.field_name||'')}${e.evidence?' · '+x(e.evidence.substring(0,90)):''}${e.kind==='source_statement'?x(String(e.value||'').substring(0,800)):''}</div>
    </div>`;
  }).join('');
}

/* ── CONCLUSION ── */
function initConclusion(){
  const concs=D.conclusions||D.hypotheses||[];
  const el=document.getElementById('cc-content');
  el.innerHTML=concs.length?concs.map(cCard).join('')
    :'<p style="color:var(--t3);font-size:12px">暂无结论</p>';
  if(D.analysis_stage==='evidence_packet_only'){
    const cov=D.coverage||{};
    el.innerHTML='<div class="ccard"><div class="cstmt">证据整理已完成，待 Agent 执行竞争假说、反证与调查计划分析。</div><div class="cdetail">覆盖 '+x(cov.included_units??0)+' / '+x(cov.total_units??0)+' 条；遗漏 '+x(cov.omitted_units??0)+' 条；截断 '+x((cov.truncated_evidence_ids||[]).length)+' 条。哈希用于核对文件一致性，不证明内容真实。</div></div>'+el.innerHTML;
  }
  (D.investigation_plan||[]).forEach(p=>{
    el.innerHTML+='<div class="ccard"><div class="cstmt">'+x(p.action||p.description||p.task||'调查步骤')+'</div><div class="cdetail">'+x(p.purpose||p.discriminates||'')+'</div></div>';
  });
  // anomalies
  const an=D.anomalies||[];
  const ael=document.getElementById('an-content');
  ael.innerHTML=an.length?an.map(a=>`
    <div class="acard">
      <div class="atag">${x(a.type||'ANOMALY')} · ${a.severity||'?'}</div>
      <div class="adesc">${x(a.description||a.reason||'')}</div>
      <div class="aloc">${x(a.location||a.source_location||'')}</div>
    </div>`).join('')
    :'<p style="color:var(--t3);font-size:12px">未检测到异常</p>';
  // relations
  const rels=D.relationships||[];
  document.getElementById('rel-body').innerHTML=rels.length
    ?rels.slice(0,30).map(r=>`<tr>
      <td style="color:var(--blue2)">${x(r.entity_a||r.from||'')}</td>
      <td style="color:var(--purple2)">${x(r.relation_type||r.edge_type||'')}</td>
      <td style="color:var(--green2)">${x(r.entity_b||r.to||'')}</td>
      <td style="font-family:monospace;color:${cc(r.confidence)}">${score(r.confidence)}</td>
      <td style="color:var(--t3);font-size:10px">${x(r.source||'')}</td>
    </tr>`).join('')
    :'<tr><td colspan="5" style="color:var(--t3)">暂无关系数据</td></tr>';
  // hypotheses in side panel
  renderHyp(concs);
}

function cCard(c){
  const cf=c.confidence,col=cc(cf);
  const st=c.status||'tentative';
  const stMap={tentative:['ct-tent','🔶 暂疑'],confirmed:['ct-conf','✅ 已确认'],refuted:['ct-ref','❌ 已否定']};
  const [cls,lbl]=stMap[st]||stMap.tentative;
  return `<div class="ccard">
    <div class="chead">
      <span class="ctag ${cls}">${lbl}</span>
      <span class="cpct" style="color:${col};margin-left:auto">${score(cf)}</span>
    </div>
    <div class="cstmt">${x(c.statement||c.hypothesis||'')}</div>
    <div class="cbar"><div class="cbar-f" style="width:${cf*100}%;background:${col}"></div></div>
    ${c.reasoning?`<div class="cdetail">推理: ${x(c.reasoning)}</div>`:''}
    ${(c.counter_evidence||[]).length?`<div class="cdetail" style="color:var(--red2)">反例: ${c.counter_evidence.slice(0,2).map(x).join(' · ')}</div>`:''}
    ${(c.missing_evidence||c.missing_items||[]).length?`<div class="cdetail" style="color:var(--amber2)">待补充: ${(c.missing_evidence||c.missing_items||[]).slice(0,2).map(x).join(' · ')}</div>`:''}
  </div>`;
}

function renderHyp(concs){
  const el=document.getElementById('hyp');
  el.innerHTML=concs.length?concs.map(cCard).join('')
    :'<p style="font-size:11px;color:var(--t3);padding:10px 12px">暂无结论</p>';
}

/* ── NODE SELECT ── */
let selId=null;
function sel(id){
  selId=id;
  document.querySelectorAll('.nc').forEach(c=>c.classList.remove('sel'));
  const card=document.getElementById('nc-'+id);
  if(card){card.classList.add('sel');card.scrollIntoView({block:'nearest',behavior:'smooth'})}
  const ns=nodes(),es=edges(),ev=evs();
  const n=ns.find(x=>x.node_id===id);
  if(!n) return;
  const relEdges=es.filter(e=>e.from_node===id||e.to_node===id);
  const relEv=ev.filter(e=>e.node_id===id);
  const loc=(D.source_locations||[]).find(l=>l.node_id===id);
  const hl=new Set(relEv.map(e=>e.field_name));
  let h=`<div class="dp">
    <div class="dp-title">${x(n.label||n.node_id)}</div>
    <div class="dp-sub">${x((n.node_type||'').toUpperCase())} &nbsp;·&nbsp; <span style="color:${cc(n.confidence)};font-weight:800">${score(n.confidence)}</span></div>`;
  const l=loc||{};
  if(n.source_file||l.file){
    h+=`<div class="chips">
      ${n.source_file?`<span class="chip">📁 ${x(n.source_file.split(/[\\/]/).pop())}</span>`:''}
      ${l.sheet?`<span class="chip">📊 ${x(l.sheet)}</span>`:''}
      ${l.row?`<span class="chip">Row ${l.row}</span>`:''}
      ${l.page?`<span class="chip">P.${l.page}</span>`:''}
      ${l.paragraph?`<span class="chip">¶${l.paragraph}</span>`:''}
    </div>`;
  }
  if((n.field_names||[]).length){
    h+=`<div class="fbox">`;
    (n.field_names||[]).forEach((fn,i)=>{
      const fv=String(n.field_values?.[i]||'').substring(0,100);
      h+=`<div class="fr"><span class="fn">${x(fn)}</span><span class="fv ${hl.has(fn)?'m':''}">${x(fv)}</span></div>`;
    });
    h+=`</div>`;
  }
  if(n.raw_text_snippet)
    h+=`<div class="rawbox">${x(n.raw_text_snippet.substring(0,280))}</div>`;
  h+=`</div>`;
  if(relEdges.length){
    h+=`<div class="dp"><h3>关联边 (${relEdges.length})</h3>`;
    relEdges.forEach(e=>{
      const c=EC[e.edge_type]||'#64748b';
      h+=`<div class="evcard" style="border-color:${c}">
        <div class="evhead">
          <span class="evbadge" style="background:${c}20;color:${c}">${e.edge_type||'?'}</span>
          <span class="evpct" style="color:${cc(e.score)}">${score(e.score)}</span>
        </div>
        <div class="evtext">${x((e.evidence||'').substring(0,110))}</div>
      </div>`;
    });
    h+=`</div>`;
  }
  if(relEv.length){
    h+=`<div class="dp"><h3>证据片段 (${relEv.length})</h3>`;
    relEv.forEach(e=>{
      const c=EC[e.match_type]||'#64748b';
      h+=`<div class="evcard" style="border-color:${c}">
        <div class="evhead">
          <span class="evbadge" style="background:${c}20;color:${c}">${e.match_type||'?'}</span>
          <span class="evpct" style="color:${cc(e.score)}">${score(e.score)}</span>
        </div>
        <div class="evvals"><span class="evfrom">${x(String(e.value||'').substring(0,48))}</span><span class="evarr"> → </span><span class="evto">${x(String(e.matched_value||'').substring(0,48))}</span></div>
        <div class="evtext" style="margin-top:4px">📍 ${x(locText(e.source_location))}</div>
      </div>`;
    });
    h+=`</div>`;
  }
  document.getElementById('det').innerHTML=h;
  hl_node=id;
}

/* ── CANVAS ── */
let cvs,ctx,CW,CH;
let NP={},drag=null,doff={x:0,y:0},pan=null;
let ox=0,oy=0,sc=1,hl_node=null;
let animFrame=null;

function initCanvas(){
  cvs=document.getElementById('cvs');
  ctx=cvs.getContext('2d');
  resize2();
  window.addEventListener('resize',resize2);
  layout();
  cvs.addEventListener('mousedown',md);
  cvs.addEventListener('mousemove',mm);
  cvs.addEventListener('mouseup',mu);
  cvs.addEventListener('mouseleave',mu);
  cvs.addEventListener('wheel',mw,{passive:false});
  tick();
}
function resize2(){
  const r=cvs.parentElement.getBoundingClientRect();
  cvs.width=CW=r.width; cvs.height=CH=r.height;
}
function w2s(wx,wy){return{x:wx*sc+ox,y:wy*sc+oy}}
function s2w(sx,sy){return{x:(sx-ox)/sc,y:(sy-oy)/sc}}

function layout(){
  const ns=nodes(),es=edges();
  if(!ns.length) return;
  const res=ns.find(n=>n.node_type==='result');
  const srcs=ns.filter(n=>n.node_type==='source');
  const rules=ns.filter(n=>n.node_type==='rule');
  const rest=ns.filter(n=>n.node_type!=='result'&&n.node_type!=='source'&&n.node_type!=='rule');
  const cx=CW/2,cy=CH/2;
  if(res) NP[res.node_id]={x:cx,y:cy-CH*.22,vx:0,vy:0};
  const R1=Math.min(CW,CH)*.27;
  srcs.forEach((n,i)=>{
    const a=-Math.PI/2+(Math.PI*(i+1)/(srcs.length+1));
    NP[n.node_id]={x:cx+R1*Math.cos(a)+(Math.random()-.5)*30,
                   y:cy+R1*Math.sin(a)*.75+CH*.07+(Math.random()-.5)*30,vx:0,vy:0};
  });
  const R2=Math.min(CW,CH)*.34;
  rules.forEach((n,i)=>{
    const a=Math.PI*.2+Math.PI*.6*(i/Math.max(rules.length-1,1));
    NP[n.node_id]={x:cx+R2*Math.cos(a)+(Math.random()-.5)*25,
                   y:cy+R2*Math.sin(a)*.5+CH*.12+(Math.random()-.5)*25,vx:0,vy:0};
  });
  rest.forEach((n,i)=>{
    NP[n.node_id]={x:cx+(Math.random()-.5)*CW*.5,y:cy+(Math.random()-.5)*CH*.4,vx:0,vy:0};
  });
  for(let i=0;i<80;i++) force(ns,es);
}

function force(ns,es){
  const K=110;
  ns.forEach(a=>{
    ns.forEach(b=>{
      if(a.node_id===b.node_id) return;
      const pa=NP[a.node_id],pb=NP[b.node_id];
      if(!pa||!pb) return;
      const dx=pa.x-pb.x,dy=pa.y-pb.y;
      const d=Math.sqrt(dx*dx+dy*dy)||1;
      const f=K*K/d*.08;
      pa.vx+=dx/d*f; pa.vy+=dy/d*f;
    });
  });
  es.forEach(e=>{
    const pa=NP[e.from_node],pb=NP[e.to_node];
    if(!pa||!pb) return;
    const dx=pb.x-pa.x,dy=pb.y-pa.y;
    const d=Math.sqrt(dx*dx+dy*dy)||1;
    const f=(d-K)*.05;
    pa.vx+=dx/d*f; pa.vy+=dy/d*f;
    pb.vx-=dx/d*f; pb.vy-=dy/d*f;
  });
  ns.forEach(n=>{
    const p=NP[n.node_id];
    if(!p) return;
    p.vx+=(CW/2-p.x)*.003; p.vy+=(CH/2-p.y)*.003;
    p.x+=p.vx*.45; p.y+=p.vy*.45;
    p.vx*=.82; p.vy*=.82;
  });
}

function tick(){
  animFrame=requestAnimationFrame(tick);
  if(!ctx) return;
  ctx.clearRect(0,0,CW,CH);
  ctx.save();
  ctx.translate(ox,oy); ctx.scale(sc,sc);
  drawEdges(); drawNodes();
  ctx.restore();
}

function drawEdges(){
  const es=edges();
  es.forEach(e=>{
    const pa=NP[e.from_node],pb=NP[e.to_node];
    if(!pa||!pb) return;
    const col=EC[e.edge_type]||'#475569';
    const isHL=hl_node&&(e.from_node===hl_node||e.to_node===hl_node);
    const alpha=isHL?.95:(hl_node?.2:.65);
    const lw=isHL?2.5:1.5;
    ctx.save();
    ctx.globalAlpha=alpha;
    ctx.strokeStyle=col;
    ctx.lineWidth=lw;
    if(EDASH[e.edge_type]) ctx.setLineDash([5,4]);
    // curved bezier
    const mx=(pa.x+pb.x)/2,my=(pa.y+pb.y)/2-38;
    ctx.beginPath();
    ctx.moveTo(pa.x,pa.y);
    ctx.quadraticCurveTo(mx,my,pb.x,pb.y);
    ctx.stroke();
    ctx.setLineDash([]);
    // arrowhead
    const t=.88,ax=pa.x+(pb.x-pa.x)*t+(mx-pa.x)*2*(1-t)*t,
          ay=pa.y+(pb.y-pa.y)*t+(my-pa.y)*2*(1-t)*t;
    const ang=Math.atan2(pb.y-my,pb.x-mx);
    ctx.beginPath();
    ctx.moveTo(ax,ay);
    ctx.lineTo(ax-9*Math.cos(ang-.4),ay-9*Math.sin(ang-.4));
    ctx.lineTo(ax-9*Math.cos(ang+.4),ay-9*Math.sin(ang+.4));
    ctx.closePath();
    ctx.fillStyle=col; ctx.fill();
    // label when zoomed or highlighted
    if(isHL||sc>1.1){
      ctx.font='bold 9px monospace';
      ctx.textAlign='center';
      ctx.fillStyle=col;
      ctx.globalAlpha=Math.min(alpha,.8);
      ctx.fillText(e.edge_type||'',mx,my-5);
    }
    ctx.restore();
  });
}

function drawNodes(){
  const ns=nodes();
  ns.forEach(n=>{
    const p=NP[n.node_id];
    if(!p) return;
    const col=NC[n.node_type]||'#475569';
    const cf=n.confidence||0;
    const isHL=n.node_id===hl_node;
    const isSel=n.node_id===selId;
    const R=n.node_type==='result'?42:33;
    ctx.save();
    ctx.globalAlpha=isHL||isSel?1:(hl_node?.35:.9);
    // glow for highlighted
    if(isHL||isSel){
      const g=ctx.createRadialGradient(p.x,p.y,R*.5,p.x,p.y,R*2.5);
      g.addColorStop(0,col+'50'); g.addColorStop(1,'transparent');
      ctx.fillStyle=g; ctx.beginPath(); ctx.arc(p.x,p.y,R*2.5,0,Math.PI*2); ctx.fill();
    }
    // confidence ring track
    ctx.beginPath(); ctx.arc(p.x,p.y,R,0,Math.PI*2);
    ctx.strokeStyle=col+'30'; ctx.lineWidth=7; ctx.stroke();
    // confidence fill
    ctx.beginPath(); ctx.arc(p.x,p.y,R,-Math.PI/2,-Math.PI/2+Math.PI*2*cf);
    ctx.strokeStyle=col; ctx.lineWidth=3.5; ctx.stroke();
    // body gradient
    const g2=ctx.createRadialGradient(p.x-R*.25,p.y-R*.25,0,p.x,p.y,R);
    g2.addColorStop(0,col+'60'); g2.addColorStop(1,col+'18');
    ctx.fillStyle=g2; ctx.beginPath(); ctx.arc(p.x,p.y,R-3,0,Math.PI*2); ctx.fill();
    // border
    ctx.strokeStyle=isSel?col:'rgba(255,255,255,.15)';
    ctx.lineWidth=isSel?2:1;
    ctx.beginPath(); ctx.arc(p.x,p.y,R-3,0,Math.PI*2); ctx.stroke();
    // icon
    ctx.font=`${n.node_type==='result'?16:12}px sans-serif`;
    ctx.textAlign='center'; ctx.fillStyle='rgba(255,255,255,.8)';
    ctx.fillText(EICONS[n.node_type]||'◉',p.x,p.y-4);
    // label
    const lbl=(n.label||'').substring(0,14);
    ctx.font=`bold ${n.node_type==='result'?10:9}px 'SF Pro Display',sans-serif`;
    ctx.fillStyle='rgba(255,255,255,.95)';
    ctx.fillText(lbl,p.x,p.y+10);
    // conf text
    ctx.font='bold 9px monospace';
    ctx.fillStyle=col;
    ctx.fillText(score(n.confidence),p.x,p.y+23);
    ctx.restore();
  });
}

/* ── CANVAS EVENTS ── */
function nodeAt(wx,wy){
  for(const n of nodes().slice().reverse()){
    const p=NP[n.node_id]; if(!p) continue;
    const R=n.node_type==='result'?42:33;
    if((wx-p.x)**2+(wy-p.y)**2<R*R) return n;
  }
  return null;
}
function edgeAt(wx,wy){
  for(const e of edges()){
    const pa=NP[e.from_node],pb=NP[e.to_node]; if(!pa||!pb) continue;
    const mx=(pa.x+pb.x)/2,my=(pa.y+pb.y)/2-38;
    for(let t=0;t<=1;t+=.06){
      const bx=(1-t)**2*pa.x+2*(1-t)*t*mx+t**2*pb.x;
      const by=(1-t)**2*pa.y+2*(1-t)*t*my+t**2*pb.y;
      if((wx-bx)**2+(wy-by)**2<110) return e;
    }
  }
  return null;
}
function md(e){
  const {x,y}=s2w(e.offsetX,e.offsetY);
  const n=nodeAt(x,y);
  if(n){drag=n;doff={x:x-NP[n.node_id].x,y:y-NP[n.node_id].y}}
  else{pan={x:e.offsetX-ox,y:e.offsetY-oy}}
}
let _mmNode=null;
function mm(e){
  const tip=document.getElementById('etip');
  const {x,y}=s2w(e.offsetX,e.offsetY);
  if(drag&&NP[drag.node_id]){NP[drag.node_id].x=x-doff.x;NP[drag.node_id].y=y-doff.y;return}
  if(pan){ox=e.offsetX-pan.x;oy=e.offsetY-pan.y;return}
  const n=nodeAt(x,y);
  cvs.style.cursor=n?'pointer':'default';
  if(n){tip.style.display='none';
    if(n.node_id!==_mmNode){_mmNode=n.node_id;}
    return;
  }
  _mmNode=null;
  const ed=edgeAt(x,y);
  if(ed){
    cvs.style.cursor='crosshair';
    const col=EC[ed.edge_type]||'#94a3b8';
    tip.innerHTML=`<div class="et" style="color:${col}">${ed.edge_type||'?'}</div>
      <div class="es" style="color:${cc(ed.score)}">${score(ed.score)} 匹配</div>
      <div class="ev">${x2((ed.evidence||'').substring(0,160))}</div>`;
    tip.style.display='block';
    tip.style.left=(e.clientX+14)+'px';
    tip.style.top=(e.clientY-8)+'px';
  } else {tip.style.display='none'}
}
function mu(e){
  if(drag&&!pan){
    const {x,y}=s2w(e.offsetX,e.offsetY);
    const moved=(x-doff.x-NP[drag.node_id].x)**2+(y-doff.y-NP[drag.node_id].y)**2;
    if(moved<20) sel(drag.node_id);
  }
  drag=null;pan=null;
}
function mw(e){
  e.preventDefault();
  const f=e.deltaY>0?.88:1.14;
  ox=e.offsetX-(e.offsetX-ox)*f; oy=e.offsetY-(e.offsetY-oy)*f;
  sc=Math.max(.15,Math.min(5,sc*f));
}
function zi(){sc=Math.min(5,sc*1.2)}
function zo(){sc=Math.max(.15,sc/1.2)}
function rv(){ox=0;oy=0;sc=1}

/* ── LEGEND ── */
function buildLegend(){
  const items=[
    {t:'dot',col:'#3b82f6',lbl:'结果节点'},
    {t:'dot',col:'#10b981',lbl:'数据源'},
    {t:'dot',col:'#8b5cf6',lbl:'规则节点'},
    {t:'dot',col:'#ef4444',lbl:'异常节点'},
    {t:'line',col:'#10b981',lbl:'精确ID'},
    {t:'line',col:'#3b82f6',lbl:'直接匹配'},
    {t:'line',col:'#ec4899',lbl:'联合匹配'},
    {t:'dash',col:'#f59e0b',lbl:'模糊匹配'},
    {t:'dash',col:'#8b5cf6',lbl:'语义匹配'},
    {t:'line',col:'#f97316',lbl:'时序关联'},
  ];
  document.getElementById('legend').innerHTML=items.map(i=>`
    <div class="lg">
      ${i.t==='dot'?`<div class="lg-dot" style="background:${i.col};box-shadow:0 0 5px ${i.col}55"></div>`:''}
      ${i.t==='line'?`<div class="lg-line" style="background:${i.col}"></div>`:''}
      ${i.t==='dash'?`<div class="lg-dash" style="border-color:${i.col}"></div>`:''}
      ${i.lbl}
    </div>`).join('');
}

/* ── TAB SWITCH ── */
function sw(name){
  document.querySelectorAll('.tb').forEach(t=>t.classList.toggle('on',t.dataset.t===name));
  document.querySelectorAll('.pane').forEach(p=>p.classList.toggle('on',p.id==='pane-'+name));
  if(name==='graph') resize2();
}

/* ── UTILS ── */
function x(s){return String(s??'').replace(/&/g,'&amp;').replace(/</g,'&lt;').replace(/>/g,'&gt;').replace(/"/g,'&quot;').replace(/'/g,'&#39;')}
function x2(s){return x(s)}
</script>
</body>
</html>
"""
