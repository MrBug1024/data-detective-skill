"""Source-trace adapter for the original mosaic investigation wall.

The canvas shows one requested result row at a time. Complete field mappings,
rule citations and coverage remain in the offline artifact, without rendering
thousands of nodes or presenting a retrieval score as a confidence probability.
"""
from __future__ import annotations

import hashlib
import json
from pathlib import Path

from run_contract import assess
from lineage_graph import seed_reference
from wall_renderer import _TEMPLATE


def _id(value):
    return 'n_' + hashlib.sha256(str(value).encode('utf-8')).hexdigest()[:20]


def prepare_trace_wall(report):
    """Only presentation changes; preserve the report's evidence decisions."""
    views = []
    result_ref = report.get('result_ref', {'file': report.get('result_file', ''), 'sheet': report.get('result_sheet')})
    mappings = report.get('field_mappings', [])
    registry = report.get('source_records', {})
    target = report.get('target', Path(result_ref.get('file') or '数据溯源').name)
    if isinstance(target, dict):
        target = target.get('description') or json.dumps(target, ensure_ascii=False)
    result_rows = report.get('result_rows', [])
    is_query = report.get('analysis_stage') == 'evidence_search'
    is_seed = result_ref.get('kind') == 'document_lookup_seeds'
    if is_query:
        source_rows = result_rows or report.get('source_trace', {}).get('result_rows', [])
        aggregate = {}
        for source_row in source_rows:
            for match in source_row.get('source_matches', []):
                existing = aggregate.get(match.get('source_ref'))
                if not existing or match.get('status') == 'ambiguous':
                    aggregate[match.get('source_ref')] = match
        result_rows = [{'row_num': None, 'values': {'查询线索': target}, 'source_matches': list(aggregate.values()),
                        'document_lookup_seeds': [seed for r in source_rows for seed in r.get('document_lookup_seeds', [])]}]
    for result in result_rows:
        row = result['row_num']
        values = result.get('values', {})
        rid = _id(('result', row))
        nodes = [{'node_id': rid, 'node_type': 'result', 'label': '目标线索' if is_query else f'文档线索 {row}' if is_seed else f'结果 · 第 {row} 行',
                  'source_file': '' if is_seed else result_ref.get('file', ''), 'sheet_name': result_ref.get('sheet'),
                  'row_num': None if is_seed else row, 'field_names': list(values), 'field_values': list(values.values()),
                  'confidence': None}]
        edges, evidence, locations, source_ids = [], [], [], {}
        row_mappings = [dict(m) for m in mappings if is_query or m.get('result_row', m.get('row_num', m.get('row'))) == row]
        for match in result.get('source_matches', []):
            record = registry.get(match.get('source_ref'), {}) if isinstance(registry, dict) else {}
            item = {**record, **match}
            loc = dict(item.get('location', item.get('source_location', {})) or {})
            loc.setdefault('file', item.get('file', ''))
            loc.setdefault('sheet', item.get('sheet'))
            loc.setdefault('row', item.get('row_num', item.get('row')))
            sid = _id((loc.get('file'), loc.get('sheet'), loc.get('row'), item.get('source_ref')))
            source_ids[item.get('source_ref')] = sid
            source_values = item.get('values', {})
            nodes.append({'node_id': sid, 'node_type': 'source',
                          'label': f"{Path(loc['file']).name} · {loc.get('row', '?')}",
                          'source_file': loc['file'], 'sheet_name': loc.get('sheet'),
                          'row_num': loc.get('row'), 'confidence': None, 'source_ref': item.get('source_ref'),
                          'field_names': list(source_values), 'field_values': list(source_values.values()),
                          'raw_text_snippet': str(item.get('status', ''))})
            locations.append({'node_id': sid, **loc})
            keys = item.get('match_keys', item.get('keys', []))
            key_text = ', '.join(keys) if isinstance(keys, (list, dict)) else str(keys)
            explanation = f"{item.get('status', '已定位')}；连接字段：{key_text}"
            edges.append({'edge_id': _id((rid, sid)), 'from_node': sid, 'to_node': rid,
                          'edge_type': 'exact_id' if item.get('status') in ('located', 'unique', 'matched') else 'joint',
                          'relationship': 'source_record_correspondence', 'status': item.get('status'),
                          'score': None, 'evidence': explanation})
            evidence.append({'evidence_id': _id(('source', rid, sid)), 'node_id': sid,
                             'match_type': 'exact_id', 'source_location': loc, 'evidence': explanation,
                             'field_name': key_text, 'score': None})
            for mapping in row_mappings:
                mapping.setdefault('result_value', values.get(mapping.get('result_field')))
                mapping['sources'] = [({**source, 'source_location': loc,
                    'source_value': source_values.get(source.get('source_field'))}
                    if source.get('source_ref') == item.get('source_ref') else source)
                    for source in mapping.get('sources', [])]
        # Show the actual lookup path, including intermediate records. This is
        # a reconstructed key join, not an assertion of historical derivation.
        document_seeds = {seed.get('seed_ref') or seed_reference(seed): seed for seed in result.get('document_lookup_seeds', [])
                          if seed.get('evidence_id') and seed.get('can_seed_lookup') is True}
        for match in result.get('source_matches', []):
            sid = source_ids.get(match.get('source_ref'))
            for key in match.get('key_provenance', []):
                for origin in key.get('origins', []):
                    parent = source_ids.get(origin.get('origin'))
                    if not parent and origin.get('origin') in document_seeds:
                        seed = document_seeds[origin['origin']]
                        loc = seed.get('source_location', {})
                        parent = _id(('document_seed', origin['origin']))
                        source_ids[origin['origin']] = parent
                        nodes.append({'node_id': parent, 'node_type': 'source', 'label': '文档桥接 · ' + Path(loc.get('file') or '').name,
                            'source_file': loc.get('file', ''), 'row_num': loc.get('row'), 'sheet_name': loc.get('sheet'),
                            'field_names': [seed.get('identifier_namespace', '标识')], 'field_values': [seed.get('value')],
                            'raw_text_snippet': '带原文出处的查询线索；业务关系仍待核验', 'confidence': None})
                        locations.append({'node_id': parent, **loc})
                        evidence.append({'evidence_id': _id(('seed', parent)), 'node_id': parent,
                            'match_type': 'semantic', 'source_location': loc, 'score': None,
                            'evidence': '文档键用于精确检索，不等于历史派生证明', 'lookup_seed': seed})
                    if parent and parent != sid:
                        edges.append({'edge_id': _id(('join', parent, sid, key.get('source_field'))),
                            'from_node': parent, 'to_node': sid, 'edge_type': 'joint',
                            'dependency_type': 'INDIRECT', 'relationship': 'reconstructed_key_join',
                            'status': match.get('status'), 'score': None,
                            'evidence': f"连接：{origin.get('field')} → {key.get('source_field')}；{match.get('status')}"})
        views.append({'row_num': row, 'view_kind': 'query' if is_query else 'seed' if is_seed else 'result', 'chain': {'nodes': nodes, 'edges': edges},
                      'evidence': evidence, 'source_locations': locations, 'field_mappings': row_mappings,
                      'missing': result.get('missing', []), 'conflicts': result.get('conflicts', [])})
    rule_nodes, rule_evidence, rule_locations = [], [], []
    related = report.get('related_evidence', report.get('rule_evidence', []))
    for index, item in enumerate(related):
        loc = dict(item.get('source_location', item.get('location', {})) or {})
        if 'row' not in loc and 'row_num' in loc:
            loc['row'] = loc['row_num']
        sid = _id(('rule', item.get('evidence_id', index)))
        quote = item.get('quote', item.get('text', item.get('value', '')))
        is_rule = item.get('kind') == 'rule_candidate' or item.get('rule_applicability') == 'not_verified'
        associated = sorted({origin['result_row'] for term in item.get('matched_terms', [])
                             if term.get('identifier') for origin in term.get('origins', [])
                             if origin.get('result_row') is not None}) if item.get('kind') == 'identifier_mention' and not is_query else None
        rule_nodes.append({'node_id': sid, 'node_type': 'rule' if is_rule else 'source',
                           'label': f"{Path(loc.get('file') or item.get('file') or '原文证据').name} · {loc.get('row', loc.get('paragraph', ''))}",
                           'source_file': loc.get('file', ''), 'row_num': loc.get('row'),
                           'sheet_name': loc.get('sheet'), 'raw_text_snippet': quote,
                           'field_names': ['关联依据'], 'field_values': [item.get('reason', item.get('match_reason', '原文关联候选'))],
                           'confidence': None, 'associated_result_rows': associated})
        rule_locations.append({'node_id': sid, **loc})
        rule_evidence.append({**item, 'node_id': sid, 'match_type': 'rule' if is_rule else item.get('match_type', 'semantic'), 'kind': 'source_statement',
                              'value': quote, 'score': None, 'source_location': loc, 'associated_result_rows': associated})
    missing = list(report.get('missing', []))
    for issue in report.get('issues', []) + report.get('source_localization', {}).get('unmatched_sources', []):
        missing.append({**issue, 'reason': issue.get('message') or issue.get('reason') or issue.get('code', '')})
    translated = {'Source locations reconstructed': '已定位全部请求结果的来源记录。',
                  'Source localization needs follow-up': '已保留当前来源与原文证据，未决连接见字段、覆盖与后续动作。'}
    conclusions = [{**item, 'statement': translated.get(item.get('statement'), item.get('statement', ''))}
                   for item in report.get('conclusions', [])]
    assessment = assess(report)
    return {'analysis_stage': report.get('analysis_stage', 'source_trace'),
            'investigation_id': report.get('investigation_id', 'LINEAGE'),
            'target': target,
            'result_ref': result_ref, 'confidence': None, 'confidence_semantics': '逐项证据状态，不以匹配分代替正确率',
            'assessment': assessment, 'coverage': report.get('coverage', {}),
            'source_profiles': report.get('source_profiles', []),
            'source_localization': report.get('source_localization', assessment.get('source_localization', {})),
            'calculation_verification': report.get('calculation_verification', assessment.get('calculation_verification', {})),
            'rule_applicability': report.get('rule_applicability', assessment.get('rule_applicability', {})),
            'historical_lineage': report.get('historical_lineage', assessment.get('historical_lineage', {})),
            'entity_mentions': report.get('entity_mentions', []), 'bridge_candidates': report.get('bridge_candidates', []),
            'lookup_seeds': report.get('lookup_seeds', []),
            'next_actions': report.get('next_actions', []), 'missing': missing,
            'conflicts': report.get('conflicts', []), 'views': views,
            'rule_nodes': rule_nodes, 'rule_evidence': rule_evidence, 'rule_locations': rule_locations,
            'conclusions': conclusions}


def attach_trace_graph(report):
    """Expose the same navigable graph in JSON as in the mosaic, with shared nodes."""
    data = prepare_trace_wall(report)
    nodes, edges, evidence, locations = {}, {}, {}, {}
    for view in data['views']:
        for node in view['chain']['nodes']:
            nodes[node['node_id']] = node
        for edge in view['chain']['edges']:
            edges[edge['edge_id']] = edge
        for item in view['evidence']:
            evidence[item['evidence_id']] = item
        for location in view['source_locations']:
            locations[location['node_id']] = location
        rid = next(n['node_id'] for n in view['chain']['nodes'] if n['node_type'] == 'result')
        for node in data['rule_nodes']:
            if node.get('associated_result_rows') is not None and view['row_num'] not in node['associated_result_rows']:
                continue
            eid = _id((node['node_id'], rid))
            edges[eid] = {'edge_id': eid, 'from_node': node['node_id'], 'to_node': rid,
                          'edge_type': 'rule' if node['node_type'] == 'rule' else 'semantic',
                          'score': None, 'evidence': '原文关联候选；含义与适用性需核验'}
    for node in data['rule_nodes']:
        nodes[node['node_id']] = node
    for item in data['rule_evidence']:
        evidence[item.get('evidence_id', item['node_id'])] = item
    for location in data['rule_locations']:
        locations[location['node_id']] = location
    report['chain'] = {'nodes': list(nodes.values()), 'edges': list(edges.values()), 'confidence': None}
    report['evidence'] = list(evidence.values())
    report['source_locations'] = list(locations.values())
    from lineage_graph import build_dependencies
    report['lineage_dependencies'] = build_dependencies(report)
    return report


_STYLE = """
#tracebar{min-height:58px;display:flex;align-items:center;gap:10px;padding:10px 18px;
  background:var(--s1);border-bottom:1px solid var(--b2);flex-wrap:wrap}
#tracebar label{font-weight:700;color:var(--t2)}
#trace-row{background:var(--s2);color:var(--t1);padding:6px;border:1px solid var(--b3);border-radius:5px}
.trace-state{font-size:11px;padding:5px 9px;background:var(--s3);border-radius:5px;color:var(--t2)}
.trace-state.good{color:var(--green2)}
#workspace{height:calc(100vh - 154px)}
.mapping-wrap{padding:24px;overflow:auto;height:100%;line-height:1.7}
.mapping-wrap table{width:100%;border-collapse:collapse;font-size:12px;margin:16px 0}
.mapping-wrap td,.mapping-wrap th{text-align:left;border-bottom:1px solid var(--b2);padding:9px;vertical-align:top;overflow-wrap:anywhere}
.mapping-wrap th{color:var(--t2);background:var(--s1)}
.mapping-wrap details{margin:10px 0;padding:12px;background:var(--s2);border:1px solid var(--b2);border-radius:7px}
.mapping-wrap summary{cursor:pointer;color:var(--cyan2)}
.mapping-wrap pre{white-space:pre-wrap;overflow-wrap:anywhere;font:11px/1.7 monospace;margin-top:12px}
.mapping-wrap blockquote{padding:14px;border-left:3px solid var(--purple);white-space:pre-wrap;background:var(--s2)}
@media(max-width:1000px){#sidebar{width:200px}#detpanel{width:250px}.conf-arc-wrap{display:none}.trace-state{font-size:10px}}
@media(max-width:740px){#sidebar{width:155px}#detpanel{display:none}.tb-logo-text{display:none}#workspace{height:calc(100vh - 190px)}#tabbar{overflow-x:auto}}
"""

_UI = r"""
const traceLabels={located:'已定位',partial:'部分定位',not_found:'未找到',ambiguous:'存在歧义',evidence_only:'已找到原文证据',
  passages_found:'原文已定位',no_passages_found:'未找到原文',candidate_ambiguous:'多个字段候选',
  not_checked:'未核算',not_verified:'待核验',not_proven:'未提供执行凭据',exact:'原值相同',equal:'原值相同',
  mapped:'已映射',format_equivalent:'格式等价',unresolved:'待解释',conflicting:'存在差异',
  inferred_value_match:'值对应候选',numeric_residual:'数值残差',candidate:'候选',unique:'唯一对应'};
function stateText(value){const k=typeof value==='object'?(value?.status||'not_checked'):value;return traceLabels[k]||k||'未评估'}
function traceInit(){
  const pick=document.getElementById('trace-row');
  REPORT.views.forEach((v,i)=>{const o=document.createElement('option');o.value=i;o.textContent=v.view_kind==='query'?'目标线索':v.view_kind==='seed'?'文档线索 '+v.row_num:'结果第 '+v.row_num+' 行';pick.appendChild(o)});
  pick.addEventListener('change',()=>traceSelect(REPORT.views[Number(pick.value)]?.row_num));
  const run=document.getElementById('trace-run');run.textContent=({completed:'本次已完成',partial:'本次需补充',failed:'本次执行失败'}[REPORT.assessment.state]||'待评估');
  run.style.color=REPORT.assessment.state==='failed'?'var(--red2)':REPORT.assessment.state==='completed'?'var(--green2)':'var(--amber2)';
  document.getElementById('trace-source').textContent='来源定位 · '+stateText(REPORT.source_localization);
  document.getElementById('trace-source').classList.toggle('good',REPORT.source_localization?.status==='located');
  document.getElementById('trace-calc').textContent='计算 · '+stateText(REPORT.calculation_verification);
  document.getElementById('trace-rule').textContent='规则/文档 · '+REPORT.rule_evidence.length+' 条关联 / '+stateText(REPORT.rule_applicability);
  document.getElementById('trace-scope').textContent=REPORT.analysis_stage==='evidence_search'?'线索证据检索':'覆盖 '+REPORT.views.length+' 个结果行';
  traceSelect(REPORT.views[0]?.row_num,false);
}
function traceSelect(row,refresh=true){
  const v=REPORT.views.find(v=>v.row_num===row)||{chain:{nodes:[],edges:[]},evidence:[],source_locations:[],field_mappings:[]};
  const rid=v.chain.nodes.find(n=>n.node_type==='result')?.node_id;
  const documents=REPORT.rule_nodes.filter(n=>n.associated_result_rows==null||n.associated_result_rows.includes(row));
  const docids=new Set(documents.map(n=>n.node_id));
  D={...REPORT,...v,chain:{nodes:[...v.chain.nodes,...documents],edges:[...v.chain.edges,
    ...documents.map(n=>({from_node:n.node_id,to_node:rid,edge_type:n.node_type==='rule'?'rule':'semantic',score:null,evidence:'原文关联；含义与适用性待核验'}))]},
    evidence:[...v.evidence,...REPORT.rule_evidence.filter(e=>docids.has(e.node_id))],source_locations:[...v.source_locations,...REPORT.rule_locations.filter(e=>docids.has(e.node_id))],
    missing:[...REPORT.missing,...(v.missing||[])],conflicts:[...REPORT.conflicts,...(v.conflicts||[])]};
  if(refresh){initTop();initSidebar();initEvidence();initTimeline();initConclusion();NP={};hl_node=null;layout();
    document.getElementById('det').textContent='点击节点查看本行对应的原始字段和位置。';}
  const badge=document.getElementById('tbadge');badge.textContent=stateText(REPORT.source_localization);
  badge.className='tb-badge '+(REPORT.source_localization?.status==='located'?'badge-confirmed':'badge-tentative');
  traceMappings(v);
}
function traceMappings(view){
  const root=document.getElementById('trace-details');root.replaceChildren();
  const heading=document.createElement('h2');heading.textContent=view.view_kind==='query'?'线索与实体证据':view.view_kind==='seed'?'文档线索对应记录':'逐字段溯源 · 结果第 '+(view.row_num??'—')+' 行';root.appendChild(heading);
  const note=document.createElement('p');note.textContent='来源记录定位、字段值对应和计算规则分别记录。点击证据详情可查看原始单元格与映射依据。';root.appendChild(note);
  const table=document.createElement('table');const tr=document.createElement('tr');
  ['结果字段','对照状态','定位 / 原值'].forEach(t=>{const th=document.createElement('th');th.textContent=t;tr.appendChild(th)});table.appendChild(tr);
  (view.field_mappings||[]).forEach(m=>{
    const r=document.createElement('tr');
    [m.result_field||m.field||'',stateText(m.status)].forEach(t=>{const td=document.createElement('td');td.textContent=t;r.appendChild(td)});
    const td=document.createElement('td');const det=document.createElement('details');const s=document.createElement('summary');
    const sources=m.sources||[];s.textContent=sources.length?('查看 '+sources.length+' 个对应来源'):'查看字段解释';
    const pre=document.createElement('pre');pre.textContent=JSON.stringify(m,null,2);det.append(s,pre);td.appendChild(det);r.appendChild(td);table.appendChild(r);
  });root.appendChild(table);
  const rh=document.createElement('h2');rh.textContent='关联规则与文档原文';root.appendChild(rh);
  (D.evidence||[]).filter(e=>e.kind==='source_statement').forEach(e=>{const d=document.createElement('details');const s=document.createElement('summary');s.textContent=locText(e.source_location);
    const q=document.createElement('blockquote');q.textContent=e.quote||e.value||e.text||'';
    const pre=document.createElement('pre');pre.textContent=JSON.stringify(e,null,2);d.append(s,q,pre);root.appendChild(d)});
  const cov=document.createElement('details');const cs=document.createElement('summary');cs.textContent='覆盖范围、表头识别与后续动作';
  const cp=document.createElement('pre');cp.textContent=JSON.stringify({coverage:REPORT.coverage,source_profiles:REPORT.source_profiles,next_actions:REPORT.next_actions,historical_lineage:REPORT.historical_lineage,entity_mentions:REPORT.entity_mentions,bridge_candidates:REPORT.bridge_candidates,lookup_seeds:REPORT.lookup_seeds},null,2);
  cov.append(cs,cp);root.appendChild(cov);
}
"""


def generate_trace_wall(report, output_path):
    data = prepare_trace_wall(report)
    payload = json.dumps(data, ensure_ascii=False, default=str, allow_nan=False).replace('<', '\\u003c').replace('>', '\\u003e').replace('&', '\\u0026')
    html = _TEMPLATE.replace('const D = /*__REPORT__*/null;', 'const REPORT = ' + payload + ';\nlet D=REPORT;')
    # Guard the adapter against an upstream template change instead of emitting
    # an attractive but empty page.
    if 'const REPORT = ' not in html:
        raise ValueError('Mosaic template payload marker changed')
    html = html.replace('</style>', _STYLE + '\n</style>', 1)
    html = html.replace('<!-- TABBAR -->', '''<div id="tracebar"><label for="trace-row">追踪行</label><select id="trace-row"></select>
<span class="trace-state" id="trace-run"></span><span class="trace-state" id="trace-source"></span><span class="trace-state" id="trace-calc"></span>
<span class="trace-state" id="trace-rule"></span><span class="trace-state" id="trace-scope"></span></div>
<!-- TABBAR -->''', 1)
    html = html.replace('<div id="tabbar">', '<div id="tabbar"><div class="tb" data-t="mapping" onclick="sw(\'mapping\')">📋 字段与规则</div>', 1)
    html = html.replace('<!-- timeline pane -->', '<div class="pane" id="pane-mapping"><div class="mapping-wrap" id="trace-details"></div></div>\n<!-- timeline pane -->', 1)
    html = html.replace('  initTop(); initSidebar();', '  traceInit(); initTop(); initSidebar();', 1)
    html = html.replace('initConclusion(); initCanvas(); buildLegend();', 'initConclusion(); initCanvas(); buildLegend(); traceSelect(REPORT.views[0]?.row_num);', 1)
    html = html.replace('</script>', _UI + '\n</script>', 1)
    html = html.replace("const stMap={tentative:", "const stMap={located:['ct-conf','✓ 来源已定位'],partial:['ct-tent','部分已定位'],evidence_only:['ct-tent','原文已找到'],ambiguous:['ct-tent','关联有歧义'],not_found:['ct-tent','暂未定位'],tentative:", 1)
    # Keep zero/false source values visible in the inherited renderer.
    html = html.replace("n.field_values?.[i]||''", "n.field_values?.[i]??''")
    output = Path(output_path)
    output.parent.mkdir(parents=True, exist_ok=True)
    output.write_text(html, encoding='utf-8')
    return str(output.resolve())
