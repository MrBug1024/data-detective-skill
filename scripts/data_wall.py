"""Offline DATA evidence wall. Safe text rendering and bounded, paged cards."""
from __future__ import annotations
import json
from pathlib import Path
from run_contract import assess


def _card(row=None, **values):
    card = {'row': row, 'kind': 'result', 'status': None, 'result': None,
            'checks': [], 'missing': [], 'error': None, 'coverage': {},
            'rules': [], 'sources': [], 'field_mappings': [], 'fields': [],
            'evidence': None, 'matches': [], 'links': [], 'outcome': 'incomplete'}
    card.update(values)
    card['field_count'] = len(card['fields'])
    return card


def _wall_data(report):
    """Preserve every returned row/candidate; paginate DOM, not evidence."""
    cards, source_catalog = [], {}

    def source_ref(file, **metadata):
        for key, source in source_catalog.items():
            if source.get('file') == file and all(source.get(k) == v for k, v in metadata.items()):
                return key
        key = 'source_' + str(len(source_catalog) + 1)
        source_catalog[key] = {'file': file, **metadata}
        return key

    stage = report.get('analysis_stage')
    if stage == 'exact_extraction':
        source = source_ref(report.get('file', report.get('source_file')),
                            sha256=report.get('source_sha256'), sheet=report.get('sheet'))
        for record in report.get('rows', report.get('records', [])):
            location = {k: v for k, v in record.items() if k not in ('values', 'cells', 'raw_record')}
            location['source_ref'] = source
            cards.append(_card(record.get('row_num', record.get('line_num')), kind='source',
                status='extracted', fields=list(record.get('values', {})),
                sources=[location], evidence=record, outcome='extraction_complete'))
    elif stage == 'candidate_links':
        chain = report.get('chain', {})
        evidence = report.get('evidence', [])
        hashes = report.get('provenance', {}).get('source_sha256', {})
        locations = {location.get('node_id'): location for location in report.get('source_locations', [])}
        for node in chain.get('nodes', []):
            if node.get('node_type') == 'result':
                continue
            source = source_ref(node.get('source_file'), sha256=hashes.get(node.get('source_file')))
            location = dict(locations.get(node.get('node_id'),
                {k: node.get(k) for k in ('node_id', 'sheet_name', 'row_num', 'page_num', 'paragraph_num')}))
            location['source_ref'] = source
            cards.append(_card(node.get('row_num'), kind='candidate', status='candidate',
                fields=list(node.get('field_names', [])), sources=[location], evidence=node,
                matches=[item for item in evidence if item.get('node_id') == node.get('node_id')],
                links=[edge for edge in chain.get('edges', [])
                       if node.get('node_id') in (edge.get('from_node'), edge.get('to_node'))],
                outcome='candidates_only'))
    items = report.get('results')
    if not isinstance(items, list) and not cards:
        items = [{'row': report.get('result', {}).get('row'), 'status': 'processed', 'report': report}]
    for item in items or []:
        row = item.get('report', item)
        checks = row.get('checks', [])
        cards.append(_card(item.get('row'), status=row.get('status', item.get('status')),
                      result=row.get('result'), checks=checks,
                      fields=list(dict.fromkeys(check.get('result_field') for check in checks)),
                      missing=row.get('missing', []), error=row.get('error'),
                      coverage=row.get('coverage', {}), rules=row.get('rule_evidence', []),
                      sources=[{k: source.get(k) for k in ('source', 'unit_id', 'location')}
                               for source in row.get('source_rows', [])],
                      field_mappings=row.get('field_mappings', []), outcome=assess(row)['outcome']))
    return {'assessment': assess(report), 'stage': stage, 'source_catalog': source_catalog,
            'summary': report.get('summary', {}), 'coverage': report.get('coverage', {}),
            'result_reference': report.get('result_ref', report.get('result')),
            'missing': report.get('missing', []), 'error': report.get('error'), 'cards': cards,
            'issues': report.get('issues', []), 'conflicts': report.get('conflicts', [])}


def generate_data_wall(report, path):
    data = _wall_data(report)
    payload = json.dumps(data, ensure_ascii=False, allow_nan=False).replace('&', '\\u0026').replace('<', '\\u003c').replace('>', '\\u003e')
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(TEMPLATE.replace('/*DATA*/null', payload), encoding='utf-8')


TEMPLATE = r'''<!doctype html>
<html lang="zh-CN"><head><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1">
<title>数据溯源 · 证据调查墙</title><style>
:root{color-scheme:light;--bg:#f1f5f9;--panel:#fff;--ink:#172b4d;--muted:#475569;--line:#cbd5e1;--blue:#1e40af}
*{box-sizing:border-box}body{margin:0;background:var(--bg);color:var(--ink);font:16px/1.6 system-ui,"Microsoft YaHei",sans-serif}
main{max-width:1440px;margin:auto;padding:32px}h1{font-size:30px;line-height:1.2;margin:4px 0 16px}h2{font-size:18px;margin:0}
.eyebrow{color:var(--blue);font-weight:650;font-size:13px;letter-spacing:.08em}p{margin:8px 0;color:var(--muted)}
.banner,.tile,.controls,.overview{background:var(--panel);border:1px solid var(--line);border-radius:12px;padding:20px}
.banner{border-left:5px solid var(--blue);margin:24px 0 16px}.banner strong{font-size:22px}.overview{margin-bottom:16px}
.controls{display:flex;gap:16px;flex-wrap:wrap;align-items:end;margin:16px 0}label{display:block;font-size:14px;font-weight:600}
input,select,button{font:inherit;padding:10px 12px;border:1px solid #94a3b8;border-radius:6px;background:#fff;color:var(--ink);min-height:44px}
input{width:min(440px,100%)}button,summary{cursor:pointer}button:hover{background:#e2e8f0}button:disabled{opacity:.5;cursor:default}
:focus-visible{outline:3px solid #1e40af;outline-offset:3px}.grid{display:grid;grid-template-columns:repeat(auto-fill,minmax(min(100%,350px),1fr));gap:16px}
.tile{border-top:4px solid #64748b;min-width:0}.tile.good{border-top-color:#047857}.tile.bad{border-top-color:#b91c1c}
.status{display:inline-block;font-size:13px;font-weight:650;color:#334155;background:#e2e8f0;padding:3px 8px;border-radius:5px;margin:10px 0}
.good .status{background:#d1fae5;color:#065f46}.bad .status{background:#fee2e2;color:#991b1b}
details{margin:12px 0 0;border-top:1px solid var(--line);padding-top:12px}summary{font-weight:600;min-height:44px;padding:8px 0}
pre{white-space:pre-wrap;overflow-wrap:anywhere;background:#f8fafc;padding:12px;font:13px/1.6 ui-monospace,Consolas,monospace;max-height:440px;overflow:auto}
.check{padding:9px 0;border-bottom:1px solid #e2e8f0;overflow-wrap:anywhere}.check small{display:block;color:var(--muted)}
.pager{display:flex;gap:16px;align-items:center;justify-content:center;margin:24px 0}.empty{padding:32px;text-align:center}
a{color:var(--blue);text-underline-offset:3px}.foot{font-size:14px;margin-top:24px}#count{margin-left:auto}
@media(max-width:600px){main{padding:20px 14px}h1{font-size:25px}.controls{display:block}.controls>div{margin-bottom:12px}input,select{width:100%}#count{display:block}}
</style></head><body><main>
<div class="eyebrow">DATA DETECTIVE / 可复核证据</div><h1>数据溯源调查墙</h1>
<p>查看请求范围内的字段核验、来源位置与未决问题。原值放在折叠详情中；折叠仅改变显示，HTML 文件仍含原文。</p>
<section class="banner" aria-label="运行结论"><strong id="headline"></strong><p id="action"></p><p>计算相符仅说明显式计划可重算。历史血缘仍需原公式、代码或执行日志支持。</p></section>
<details class="overview"><summary>范围、覆盖与缺口</summary><pre id="overview"></pre></details>
<section class="controls" aria-label="筛选证据"><div><label for="search">查找物理行号或字段</label><input id="search" type="search" placeholder="例如：金额、数量、12"></div>
<div><label for="filter">核验状态</label><select id="filter"><option value="all">全部</option><option value="verified_computation">计算相符</option><option value="mismatch">存在差异</option><option value="extraction_complete">已提取，待核验</option><option value="candidates_only">候选，待核验</option><option value="incomplete">未完成或证据不足</option><option value="no_matches">未找到记录</option><option value="execution_error">执行失败</option></select></div><span id="count" role="status" aria-live="polite"></span></section>
<section class="grid" id="cards" aria-label="逐行证据"></section>
<nav class="pager" aria-label="证据分页"><button id="prev">上一页</button><span id="page"></span><button id="next">下一页</button></nav>
<p class="foot">原始证据与完整字段：<a href="report.json">report.json</a> · Agent 运行反馈：<a href="status.json">status.json</a>。本页离线显示，不发送数据。</p>
</main><script>
'use strict';const D=/*DATA*/null;
const labels={verified_computation:'计算核验通过',mismatch:'发现数据差异',incomplete:'证据不足或核验未完成',candidates_only:'已找到候选，待核验',execution_error:'执行失败',extraction_complete:'定向提取完成，待核验',no_matches:'未找到匹配记录'};
const actions={deliver_recalculation_with_lineage_limitations:'可交付计算结果，并保留历史血缘与规则适用性的限制。',review_mismatches:'请检查差额与原始计算规则。',review_mismatches_and_remaining_gaps:'请检查差额、遗漏行及其余缺口。',inspect_coverage_and_missing_evidence:'请继续处理覆盖范围、未决字段与证据缺口。',verify_field_mapping_join_cardinality_and_recompute:'下一步：确认字段映射、连接基数，再实际重算。',repair_input_or_environment_then_retry:'请根据错误修复输入或运行环境后重试。',verify_extracted_evidence_and_recompute:'下一步：核验提取范围、连接与计算规则。',review_keys_and_source_scope:'请复核键值、表头与来源范围。'};
const $=id=>document.getElementById(id);$('headline').textContent=labels[D.assessment.outcome]||D.assessment.outcome;$('action').textContent=actions[D.assessment.agent_action]||D.assessment.agent_action;
$('overview').textContent=JSON.stringify({stage:D.stage,summary:D.summary,coverage:D.coverage,missing:D.missing,error:D.error,issues:D.issues,conflicts:D.conflicts,result_reference:D.result_reference},null,2);
let page=0;const size=24;let filtered=[];
function node(tag,text,cls){const n=document.createElement(tag);if(text!==undefined)n.textContent=String(text);if(cls)n.className=cls;return n}
function details(parent,label,value){if(value===undefined||value===null||(typeof value==='object'&&!Object.keys(value).length))return;const d=node('details');d.append(node('summary',label),node('pre',JSON.stringify(value,null,2)));parent.append(d)}
function render(){const q=$('search').value.trim().toLowerCase(),f=$('filter').value;filtered=D.cards.filter(c=>(f==='all'||c.outcome===f)&&(q===''||String(c.row).includes(q)||JSON.stringify(c.fields).toLowerCase().includes(q)));page=Math.min(page,Math.max(0,Math.ceil(filtered.length/size)-1));$('cards').replaceChildren();
for(const c of filtered.slice(page*size,(page+1)*size)){const good=c.outcome==='verified_computation',bad=['mismatch','execution_error'].includes(c.outcome);const el=node('article',undefined,'tile'+(good?' good':bad?' bad':''));const title=c.row==null?(c.kind==='candidate'?'候选来源证据':'任务证据'):(c.kind==='result'?'结果物理行 ':'来源物理行 ')+c.row;el.append(node('h2',title),node('span',labels[c.outcome]||c.status,'status'));
el.append(node('p',`${c.field_count} 个字段`));
details(el,'原始值与单元格证据',c.evidence);details(el,'候选匹配证据',c.matches);details(el,'候选连接',c.links);details(el,'全部字段检查',c.checks);details(el,'字段映射与未决项',c.field_mappings);details(el,'来源位置',c.sources.map(s=>({...((D.source_catalog||{})[s.source_ref]||{}),...s})));details(el,'规则依据',c.rules);details(el,'覆盖范围',c.coverage);details(el,'缺口',c.missing);details(el,'错误',c.error);$('cards').append(el)}
if(!filtered.length)$('cards').append(node('p','没有符合筛选条件的证据。','empty'));$('count').textContent=`${filtered.length} / ${D.cards.length} 条记录`;$('page').textContent=`${page+1} / ${Math.max(1,Math.ceil(filtered.length/size))}`;$('prev').disabled=page===0;$('next').disabled=(page+1)*size>=filtered.length;}
$('search').addEventListener('input',()=>{page=0;render()});$('filter').addEventListener('change',()=>{page=0;render()});$('prev').addEventListener('click',()=>{page--;render()});$('next').addEventListener('click',()=>{page++;render()});render();
</script></body></html>'''
