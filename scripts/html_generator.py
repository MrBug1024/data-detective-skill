"""
html_generator.py — 马赛克调查墙HTML报告生成器
生成可交互的调查墙：链路图、证据高亮、节点详情、置信度可视化
"""
import json
import os
from typing import Dict, Any
from linker import DataChain


WALL_TEMPLATE = r"""<!DOCTYPE html>
<html lang="zh-CN">
<head>
<meta charset="UTF-8">
<meta name="viewport" content="width=device-width, initial-scale=1.0">
<title>🔍 数据链路侦探 · 马赛克调查墙</title>
<style>
  :root {
    --bg: #0d1117; --surface: #161b22; --surface2: #21262d;
    --border: #30363d; --text: #c9d1d9; --muted: #8b949e;
    --accent: #58a6ff; --green: #3fb950; --yellow: #d29922;
    --red: #f85149; --purple: #bc8cff; --orange: #e3b341;
    --result: #1f6feb; --source: #238636; --rule: #9d5fe8;
    --exact: #3fb950; --direct: #58a6ff; --fuzzy: #d29922;
    --semantic: #bc8cff; --joint: #e3b341;
  }
  * { box-sizing: border-box; margin: 0; padding: 0; }
  body { background: var(--bg); color: var(--text); font-family: 'JetBrains Mono', 'Fira Code', monospace; font-size: 13px; }
  
  /* TOP BAR */
  .topbar { background: var(--surface); border-bottom: 1px solid var(--border); padding: 12px 20px;
    display: flex; align-items: center; gap: 16px; position: sticky; top: 0; z-index: 100; }
  .topbar .logo { font-size: 18px; font-weight: 700; color: var(--accent); }
  .topbar .chain-id { color: var(--muted); font-size: 11px; }
  .confidence-badge { padding: 4px 12px; border-radius: 20px; font-weight: 700; font-size: 14px; }
  .conf-high { background: rgba(63,185,80,.2); color: var(--green); border: 1px solid var(--green); }
  .conf-mid { background: rgba(210,153,34,.2); color: var(--yellow); border: 1px solid var(--yellow); }
  .conf-low { background: rgba(248,81,73,.2); color: var(--red); border: 1px solid var(--red); }

  /* LAYOUT */
  .container { display: grid; grid-template-columns: 300px 1fr 340px; gap: 0; height: calc(100vh - 53px); overflow: hidden; }
  .panel { overflow-y: auto; border-right: 1px solid var(--border); }
  .panel-title { padding: 10px 14px; font-size: 11px; font-weight: 700; text-transform: uppercase;
    letter-spacing: 1px; color: var(--muted); border-bottom: 1px solid var(--border); background: var(--surface); position: sticky; top: 0; z-index: 10; }

  /* NODE CARDS */
  .node-card { margin: 8px; border-radius: 8px; border: 1px solid var(--border); overflow: hidden; cursor: pointer; transition: all .15s; }
  .node-card:hover { border-color: var(--accent); transform: translateY(-1px); box-shadow: 0 4px 12px rgba(0,0,0,.4); }
  .node-card.active { border-color: var(--accent); box-shadow: 0 0 0 2px rgba(88,166,255,.3); }
  .node-header { padding: 8px 10px; display: flex; align-items: center; gap: 8px; }
  .node-type-result { background: rgba(31,111,235,.3); }
  .node-type-source { background: rgba(35,134,54,.3); }
  .node-type-rule { background: rgba(157,95,232,.3); }
  .node-badge { font-size: 10px; padding: 2px 8px; border-radius: 10px; font-weight: 700; }
  .badge-result { background: var(--result); color: #fff; }
  .badge-source { background: var(--source); color: #fff; }
  .badge-rule { background: var(--rule); color: #fff; }
  .node-label { font-size: 12px; font-weight: 600; flex: 1; white-space: nowrap; overflow: hidden; text-overflow: ellipsis; }
  .conf-bar-wrap { padding: 4px 10px 8px; }
  .conf-bar { height: 4px; border-radius: 2px; background: var(--border); position: relative; }
  .conf-bar-fill { height: 100%; border-radius: 2px; transition: width .3s; }
  .conf-text { font-size: 10px; color: var(--muted); margin-top: 2px; }
  .node-fields { padding: 4px 10px 8px; }
  .field-row { display: flex; gap: 4px; align-items: flex-start; margin-bottom: 3px; font-size: 11px; }
  .field-name { color: var(--muted); white-space: nowrap; min-width: 80px; }
  .field-value { color: var(--text); word-break: break-all; }
  .field-value.highlight { color: var(--green); background: rgba(63,185,80,.1); padding: 0 4px; border-radius: 3px; }

  /* CENTER — CHAIN CANVAS */
  .chain-canvas { padding: 0; position: relative; background: var(--bg); display: flex; flex-direction: column; }
  #canvas-svg { width: 100%; flex: 1; }
  .canvas-hint { padding: 8px 16px; font-size: 11px; color: var(--muted); border-top: 1px solid var(--border); background: var(--surface); }

  /* RIGHT — DETAIL PANEL */
  .detail-panel { padding: 0; }
  .detail-section { padding: 12px 14px; border-bottom: 1px solid var(--border); }
  .detail-section h3 { font-size: 11px; text-transform: uppercase; letter-spacing: 1px; color: var(--muted); margin-bottom: 8px; }
  .evidence-item { background: var(--surface2); border-radius: 6px; padding: 8px; margin-bottom: 6px; border-left: 3px solid; }
  .evidence-exact { border-color: var(--exact); }
  .evidence-direct { border-color: var(--direct); }
  .evidence-fuzzy { border-color: var(--fuzzy); }
  .evidence-semantic { border-color: var(--semantic); }
  .evidence-joint { border-color: var(--joint); }
  .evidence-rule { border-color: var(--purple); }
  .evidence-text { font-size: 11px; color: var(--muted); margin-top: 4px; line-height: 1.5; }
  .location-chip { display: inline-flex; align-items: center; gap: 4px; background: var(--surface2); border: 1px solid var(--border);
    padding: 3px 8px; border-radius: 12px; font-size: 10px; color: var(--muted); margin: 2px; cursor: pointer; }
  .location-chip:hover { border-color: var(--accent); color: var(--accent); }

  /* ALERTS */
  .alert { margin: 8px; border-radius: 6px; padding: 8px 10px; font-size: 11px; }
  .alert-warning { background: rgba(210,153,34,.1); border: 1px solid rgba(210,153,34,.4); color: var(--yellow); }
  .alert-error { background: rgba(248,81,73,.1); border: 1px solid rgba(248,81,73,.4); color: var(--red); }
  .alert-info { background: rgba(88,166,255,.1); border: 1px solid rgba(88,166,255,.4); color: var(--accent); }

  /* EDGE TOOLTIP */
  .edge-tooltip { position: fixed; background: var(--surface); border: 1px solid var(--border); border-radius: 6px;
    padding: 8px 12px; font-size: 11px; pointer-events: none; z-index: 200; max-width: 280px; display: none; box-shadow: 0 4px 16px rgba(0,0,0,.5); }

  /* HYPOTHESIS */
  .hypo-card { background: var(--surface2); border-radius: 6px; padding: 10px; margin-bottom: 8px; border: 1px solid var(--border); }
  .hypo-status { font-size: 10px; padding: 2px 8px; border-radius: 10px; display: inline-block; margin-bottom: 6px; }
  .status-tentative { background: rgba(210,153,34,.2); color: var(--yellow); }
  .status-confirmed { background: rgba(63,185,80,.2); color: var(--green); }
  .status-refuted { background: rgba(248,81,73,.2); color: var(--red); }

  /* SCROLLBAR */
  ::-webkit-scrollbar { width: 6px; height: 6px; }
  ::-webkit-scrollbar-track { background: var(--bg); }
  ::-webkit-scrollbar-thumb { background: var(--border); border-radius: 3px; }
  ::-webkit-scrollbar-thumb:hover { background: var(--muted); }

  /* MATCH TYPE LEGEND */
  .legend { display: flex; flex-wrap: wrap; gap: 8px; padding: 8px 14px; border-bottom: 1px solid var(--border); }
  .legend-item { display: flex; align-items: center; gap: 4px; font-size: 10px; color: var(--muted); }
  .legend-dot { width: 8px; height: 8px; border-radius: 50%; }
</style>
</head>
<body>

<div class="topbar">
  <div class="logo">🔍 数据链路侦探</div>
  <div style="flex:1">
    <div style="font-size:12px;font-weight:600;">结果文件: <span id="result-file" style="color:var(--accent)"></span> · 第 <span id="result-row" style="color:var(--accent)"></span> 行</div>
    <div class="chain-id">链路ID: <span id="chain-id"></span></div>
  </div>
  <div>
    <div style="font-size:10px;color:var(--muted);margin-bottom:2px;text-align:center;">整体置信度</div>
    <div class="confidence-badge" id="conf-badge"></div>
  </div>
</div>

<div class="container">
  <!-- LEFT: NODE LIST -->
  <div class="panel">
    <div class="panel-title">📌 链路节点 (<span id="node-count">0</span>)</div>
    <div id="node-list"></div>
    <div class="panel-title" style="margin-top:4px;">⚠️ 缺失 / 冲突</div>
    <div id="issues-list"></div>
  </div>

  <!-- CENTER: CHAIN CANVAS -->
  <div class="chain-canvas">
    <div class="legend" id="legend"></div>
    <svg id="canvas-svg" viewBox="0 0 800 600"></svg>
    <div class="canvas-hint">💡 点击节点查看详情 · 悬停边查看证据 · 各节点颜色代表类型</div>
  </div>

  <!-- RIGHT: DETAIL -->
  <div class="panel detail-panel">
    <div class="panel-title">🧩 节点详情 & 证据</div>
    <div id="detail-content">
      <div class="detail-section">
        <p style="color:var(--muted);font-size:11px;">← 点击左侧或图中节点查看详细证据</p>
      </div>
      <div class="detail-section">
        <h3>💡 暂疑结论</h3>
        <div id="hypotheses-list"></div>
      </div>
    </div>
  </div>
</div>

<div class="edge-tooltip" id="edge-tooltip"></div>

<script>
const CHAIN_DATA = __CHAIN_DATA__;

const matchColors = {
  exact_id: '#3fb950', direct: '#58a6ff', joint: '#e3b341',
  fuzzy: '#d29922', semantic: '#bc8cff', rule: '#9d5fe8',
  indirect: '#79c0ff', inferred: '#ffa657'
};
const nodeColors = { result: '#1f6feb', source: '#238636', rule: '#9d5fe8', intermediate: '#6e7681' };

let activeNodeId = null;

function init() {
  const chain = CHAIN_DATA;
  document.getElementById('result-file').textContent = chain.result_ref.file.split(/[\\/]/).pop();
  document.getElementById('result-row').textContent = chain.result_ref.row;
  document.getElementById('chain-id').textContent = chain.chain_id;

  const conf = chain.confidence;
  const badge = document.getElementById('conf-badge');
  badge.textContent = (conf * 100).toFixed(1) + '%';
  badge.className = 'confidence-badge ' + (conf >= 0.7 ? 'conf-high' : conf >= 0.4 ? 'conf-mid' : 'conf-low');

  renderNodeList(chain.nodes);
  renderIssues(chain.missing, chain.conflicts);
  renderGraph(chain);
  renderHypotheses(chain.hypotheses);
  renderLegend();
}

function renderNodeList(nodes) {
  const container = document.getElementById('node-list');
  container.innerHTML = nodes.map(n => {
    const conf = n.confidence;
    const barColor = conf >= 0.7 ? 'var(--green)' : conf >= 0.4 ? 'var(--yellow)' : 'var(--red)';
    const fields = n.field_names.slice(0, 4).map((fn, i) => {
      const fv = (n.field_values[i] || '').toString().substring(0, 60);
      return `<div class="field-row"><span class="field-name">${escHtml(fn)}:</span><span class="field-value">${escHtml(fv)}</span></div>`;
    }).join('');
    const typeBadge = n.node_type === 'result' ? 'badge-result' : n.node_type === 'rule' ? 'badge-rule' : 'badge-source';
    const headerClass = n.node_type === 'result' ? 'node-type-result' : n.node_type === 'rule' ? 'node-type-rule' : 'node-type-source';
    return `
    <div class="node-card" id="card-${n.node_id}" onclick="selectNode('${n.node_id}')">
      <div class="node-header ${headerClass}">
        <span class="node-badge ${typeBadge}">${n.node_type.toUpperCase()}</span>
        <span class="node-label" title="${escHtml(n.label)}">${escHtml(n.label)}</span>
      </div>
      <div class="conf-bar-wrap">
        <div class="conf-bar"><div class="conf-bar-fill" style="width:${conf*100}%;background:${barColor}"></div></div>
        <div class="conf-text">置信度 ${(conf*100).toFixed(1)}%</div>
      </div>
      <div class="node-fields">${fields}</div>
    </div>`;
  }).join('');
}

function renderIssues(missing, conflicts) {
  const container = document.getElementById('issues-list');
  let html = '';
  if (!missing.length && !conflicts.length) {
    html = '<div class="alert alert-info" style="margin:8px">✅ 无缺失或冲突</div>';
  }
  missing.forEach(m => {
    html += `<div class="alert alert-warning">⚠️ <strong>缺失</strong> ${escHtml(m.reason)}<br><small>${escHtml(m.file || m.field || '')}</small></div>`;
  });
  conflicts.forEach(c => {
    html += `<div class="alert alert-error">❌ <strong>冲突</strong> ${escHtml(c.reason)}</div>`;
  });
  container.innerHTML = html;
}

function renderHypotheses(hypotheses) {
  const container = document.getElementById('hypotheses-list');
  if (!hypotheses.length) {
    container.innerHTML = '<p style="color:var(--muted);font-size:11px">暂无假设</p>';
    return;
  }
  container.innerHTML = hypotheses.map(h => `
    <div class="hypo-card">
      <span class="hypo-status status-${h.status}">${h.status === 'tentative' ? '🔶 暂疑' : h.status === 'confirmed' ? '✅ 已确认' : '❌ 已否定'}</span>
      <div style="font-size:11px;color:var(--text);margin-bottom:4px">${escHtml(h.statement)}</div>
      <div style="font-size:10px;color:var(--muted)">置信度: ${(h.confidence*100).toFixed(1)}%</div>
      ${h.counter_evidence && h.counter_evidence.length ? `<div style="font-size:10px;color:var(--red);margin-top:4px">反例: ${h.counter_evidence.slice(0,2).map(escHtml).join('; ')}</div>` : ''}
    </div>
  `).join('');
}

function renderLegend() {
  const items = Object.entries(matchColors).map(([type, color]) =>
    `<div class="legend-item"><div class="legend-dot" style="background:${color}"></div>${type}</div>`
  ).join('');
  document.getElementById('legend').innerHTML = items;
}

function renderGraph(chain) {
  const svg = document.getElementById('canvas-svg');
  const W = svg.parentElement.clientWidth || 800;
  const H = 560;
  svg.setAttribute('viewBox', `0 0 ${W} ${H}`);

  const nodes = chain.nodes;
  const edges = chain.edges;

  // Simple force-like layout: result center-top, sources spread below
  const positions = {};
  const resultNode = nodes.find(n => n.node_type === 'result');
  const sourceNodes = nodes.filter(n => n.node_type !== 'result');

  if (resultNode) positions[resultNode.node_id] = { x: W / 2, y: 100 };

  sourceNodes.forEach((n, i) => {
    const total = sourceNodes.length;
    const spread = Math.min(W - 120, total * 160);
    const startX = (W - spread) / 2 + 60;
    const x = total === 1 ? W/2 : startX + (spread / Math.max(total-1,1)) * i;
    const y = n.node_type === 'rule' ? 450 : 320 + (i % 2) * 50;
    positions[n.node_id] = { x, y };
  });

  let svgHTML = `
    <defs>
      <marker id="arr" markerWidth="8" markerHeight="8" refX="6" refY="3" orient="auto">
        <path d="M0,0 L0,6 L8,3 z" fill="#58a6ff" opacity="0.7"/>
      </marker>
      <filter id="glow">
        <feGaussianBlur stdDeviation="3" result="coloredBlur"/>
        <feMerge><feMergeNode in="coloredBlur"/><feMergeNode in="SourceGraphic"/></feMerge>
      </filter>
    </defs>
    <rect width="${W}" height="${H}" fill="var(--bg)"/>
  `;

  // Draw edges
  edges.forEach((e, idx) => {
    const from = positions[e.from_node];
    const to = positions[e.to_node];
    if (!from || !to) return;
    const color = matchColors[e.edge_type] || '#58a6ff';
    const mx = (from.x + to.x) / 2;
    const my = (from.y + to.y) / 2 - 30;
    const esc = (s) => s.replace(/&/g,'&amp;').replace(/</g,'&lt;').replace(/>/g,'&gt;').replace(/"/g,'&quot;');
    svgHTML += `
      <path d="M${from.x},${from.y} Q${mx},${my} ${to.x},${to.y}"
        stroke="${color}" stroke-width="2" fill="none" opacity="0.7"
        stroke-dasharray="${e.edge_type === 'fuzzy' || e.edge_type === 'semantic' ? '6,3' : 'none'}"
        marker-end="url(#arr)"
        onmouseenter="showEdgeTooltip(event,'${esc(e.edge_type)}','${esc(e.evidence.substring(0,120))}','${(e.score*100).toFixed(1)}%')"
        onmouseleave="hideEdgeTooltip()"
        style="cursor:pointer"/>
      <text x="${mx}" y="${my-4}" text-anchor="middle" font-size="10" fill="${color}" opacity="0.9">${e.edge_type}</text>
    `;
  });

  // Draw nodes
  nodes.forEach(n => {
    const pos = positions[n.node_id];
    if (!pos) return;
    const color = nodeColors[n.node_type] || '#6e7681';
    const label = n.label.substring(0, 20);
    const conf = n.confidence;
    const r = n.node_type === 'result' ? 42 : 36;
    svgHTML += `
      <g onclick="selectNode('${n.node_id}')" style="cursor:pointer" id="g-${n.node_id}">
        <circle cx="${pos.x}" cy="${pos.y}" r="${r}" fill="${color}" fill-opacity="0.25" stroke="${color}" stroke-width="2"/>
        <circle cx="${pos.x}" cy="${pos.y}" r="${r * conf}" fill="${color}" fill-opacity="0.15"/>
        <text x="${pos.x}" y="${pos.y-4}" text-anchor="middle" font-size="11" fill="#fff" font-weight="600">${escHtmlSvg(label)}</text>
        <text x="${pos.x}" y="${pos.y+12}" text-anchor="middle" font-size="10" fill="${color}">${(conf*100).toFixed(0)}%</text>
        ${n.is_rule ? `<text x="${pos.x}" y="${pos.y+26}" text-anchor="middle" font-size="9" fill="#9d5fe8">📋 规则</text>` : ''}
      </g>
    `;
  });

  svg.innerHTML = svgHTML;
}

function selectNode(nodeId) {
  activeNodeId = nodeId;
  document.querySelectorAll('.node-card').forEach(c => c.classList.remove('active'));
  const card = document.getElementById('card-' + nodeId);
  if (card) { card.classList.add('active'); card.scrollIntoView({ block: 'nearest' }); }

  const chain = CHAIN_DATA;
  const node = chain.nodes.find(n => n.node_id === nodeId);
  if (!node) return;

  const relEdges = chain.edges.filter(e => e.from_node === nodeId || e.to_node === nodeId);
  const relEvidence = chain.evidence.filter(e => e.node_id === nodeId);
  const location = chain.source_locations.find(l => l.node_id === nodeId);

  let html = `
    <div class="detail-section">
      <h3>📍 节点信息</h3>
      <div style="font-size:12px;font-weight:700;color:var(--accent);margin-bottom:8px">${escHtml(node.label)}</div>
      <div style="font-size:11px;color:var(--muted);margin-bottom:8px">
        类型: <span style="color:var(--text)">${node.node_type}</span> ·
        置信度: <span style="color:${node.confidence>=.7?'var(--green)':node.confidence>=.4?'var(--yellow)':'var(--red)'}">${(node.confidence*100).toFixed(1)}%</span>
      </div>
  `;

  if (location) {
    html += `<div style="margin-bottom:8px">`;
    if (location.file) html += `<span class="location-chip" title="${escHtml(location.file)}">📁 ${escHtml(location.file.split(/[\\/]/).pop())}</span>`;
    if (location.sheet) html += `<span class="location-chip">📊 ${escHtml(location.sheet)}</span>`;
    if (location.row) html += `<span class="location-chip">🔢 第${location.row}行</span>`;
    if (location.page) html += `<span class="location-chip">📄 第${location.page}页</span>`;
    if (location.paragraph) html += `<span class="location-chip">¶ 第${location.paragraph}段</span>`;
    html += `</div>`;
  }

  // Fields table
  html += `<div style="background:var(--surface2);border-radius:6px;overflow:hidden;margin-bottom:8px">`;
  node.field_names.forEach((fn, i) => {
    const fv = (node.field_values[i] || '').toString().substring(0, 100);
    const isMatch = relEvidence.some(e => e.field_name === fn);
    html += `<div class="field-row" style="padding:4px 8px;${i%2?'background:var(--surface)':''}">
      <span class="field-name" style="min-width:100px">${escHtml(fn)}:</span>
      <span class="field-value ${isMatch ? 'highlight' : ''}">${escHtml(fv)}</span>
    </div>`;
  });
  html += `</div>`;

  if (node.raw_text_snippet) {
    html += `<div style="font-size:10px;color:var(--muted);background:var(--surface2);padding:8px;border-radius:6px;line-height:1.5;white-space:pre-wrap;word-break:break-all">${escHtml(node.raw_text_snippet.substring(0,400))}</div>`;
  }
  html += `</div>`;

  // Edges
  if (relEdges.length) {
    html += `<div class="detail-section"><h3>🔗 关联边 (${relEdges.length})</h3>`;
    relEdges.forEach(e => {
      const color = matchColors[e.edge_type] || '#58a6ff';
      html += `<div class="evidence-item evidence-${e.edge_type}" style="border-color:${color}">
        <div style="display:flex;align-items:center;gap:8px;margin-bottom:4px">
          <span style="font-size:10px;background:${color}22;color:${color};padding:2px 8px;border-radius:10px;font-weight:700">${e.edge_type}</span>
          <span style="font-size:11px;color:var(--text);font-weight:600">${(e.score*100).toFixed(1)}%</span>
        </div>
        <div class="evidence-text">${escHtml(e.evidence)}</div>
      </div>`;
    });
    html += `</div>`;
  }

  // Evidence
  if (relEvidence.length) {
    html += `<div class="detail-section"><h3>🔬 证据片段 (${relEvidence.length})</h3>`;
    relEvidence.forEach(ev => {
      html += `<div class="evidence-item evidence-${ev.match_type}">
        <div style="font-size:11px;margin-bottom:4px">
          <span style="color:var(--muted)">字段:</span> <span style="color:var(--accent)">${escHtml(ev.field_name)}</span>
        </div>
        <div style="font-size:11px">
          <span style="color:var(--muted)">结果值:</span> <span style="color:var(--green)">${escHtml(ev.value)}</span>
          <span style="color:var(--muted);margin:0 6px">→</span>
          <span style="color:var(--muted)">匹配值:</span> <span style="color:var(--yellow)">${escHtml(ev.matched_value)}</span>
        </div>
        <div style="font-size:10px;color:var(--muted);margin-top:4px">📍 ${escHtml(ev.source_location)}</div>
      </div>`;
    });
    html += `</div>`;
  }

  html += `<div class="detail-section"><h3>💡 暂疑结论</h3><div id="hypotheses-list"></div></div>`;

  document.getElementById('detail-content').innerHTML = html;
  renderHypotheses(chain.hypotheses);
}

function showEdgeTooltip(e, type, evidence, score) {
  const t = document.getElementById('edge-tooltip');
  t.innerHTML = `<div style="font-weight:700;color:${matchColors[type]||'#58a6ff'};margin-bottom:4px">${escHtml(type)} · ${escHtml(score)}</div><div>${escHtml(evidence)}</div>`;
  t.style.display = 'block';
  t.style.left = (e.clientX + 12) + 'px';
  t.style.top = (e.clientY - 10) + 'px';
}
function hideEdgeTooltip() { document.getElementById('edge-tooltip').style.display = 'none'; }

function escHtml(s) {
  return String(s || '').replace(/&/g,'&amp;').replace(/</g,'&lt;').replace(/>/g,'&gt;').replace(/"/g,'&quot;');
}
function escHtmlSvg(s) {
  return String(s || '').replace(/&/g,'&amp;').replace(/</g,'&lt;').replace(/>/g,'&gt;');
}

window.addEventListener('load', init);
window.addEventListener('resize', () => { if (CHAIN_DATA) renderGraph(CHAIN_DATA); });
</script>
</body>
</html>
"""


def generate_html(chain: DataChain, output_path: str):
    """生成马赛克调查墙HTML"""
    chain_json = json.dumps(chain.to_dict(), ensure_ascii=False, default=str).replace('<', '\\u003c').replace('>', '\\u003e').replace('&', '\\u0026')
    html = WALL_TEMPLATE.replace('__CHAIN_DATA__', chain_json)

    with open(output_path, 'w', encoding='utf-8') as f:
        f.write(html)
    print(f"[INFO] Mosaic wall saved to: {output_path}")
