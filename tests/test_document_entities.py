"""Typed mention recall, assertion precision and bounded lookup candidates."""
import json
from pathlib import Path
import sys
import time
import tracemalloc

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'scripts'))
from document_entities import extract_document_entities, identifier_namespace, scope_namespace
from entity_resolution import candidate_mention_links
from evidence_retrieval import explore_evidence, retrieve_evidence, build_lookup_seeds, evidence_from_located_records, _mark_located_records


SCENARIOS = [
    ('retail', '租户: north\n订单号 ORD-0001 对应 合同编号 CT-002。', [('order', 'ORD-0001'), ('contract', 'CT-002')]),
    ('laboratory', '样本号: S-00017 使用设备编号: DEV-009，质量12.500 mg。', [('sample', 'S-00017'), ('device', 'DEV-009')]),
    ('manufacturing', 'Device ID DEV-401 uses Batch No. LOT-00073.', [('device', 'DEV-401'), ('batch', 'LOT-00073')]),
    ('logistics', 'Tracking No: PKG-0088 for Order No. OR-0008.', [('shipment', 'PKG-0088'), ('order', 'OR-0008')]),
    ('procurement', 'Supplier ID SUP-22 references Invoice Number INV-0091.', [('supplier', 'SUP-22'), ('invoice', 'INV-0091')]),
    ('healthcare', '就诊流水号 VIS-00078 关联结算流水号 SET-0043。', [('visit', 'VIS-00078'), ('settlement', 'SET-0043')]),
    ('finance', 'Payment ID PAY-00091 references Contract ID CT-00087.', [('payment', 'PAY-00091'), ('contract', 'CT-00087')]),
    ('customer', 'Customer ID C-0001 placed Order ID O-0002.', [('customer', 'C-0001'), ('order', 'O-0002')]),
]


@pytest.mark.parametrize('domain,text,expected', SCENARIOS)
def test_multidomain_identifier_recall_and_exact_span(domain, text, expected):
    report = extract_document_entities(text, location={'file': domain + '.txt', 'char_offset': 75})
    ids = [m for m in report['mentions'] if m['entity_type'] == 'identifier']
    assert [(m['identifier_namespace'], m['raw_value']) for m in ids] == expected
    for mention in report['mentions']:
        selector = mention['selector']
        assert text[selector['start']:selector['end']] == selector['quote']['exact']
        assert selector['source_start'] == 75 + selector['start']
        assert mention['identity_verified'] is False
    assert report['bridges']
    assert all(b['identity_merge_allowed'] is False for b in report['bridges'])


@pytest.mark.parametrize('cue,status', [('不属于', 'negated'), ('据称', 'reported'), ('可能', 'uncertain')])
def test_nonaffirmative_claims_never_seed_lookups(cue, status):
    report = extract_document_entities(f'{cue} 订单号 ORD-0001 对应合同号 CT-0002。', location={'file': 'claims.txt'})
    assert all(m['assertion']['status'] == status for m in report['mentions'])
    assert not any(m['can_seed_lookup'] for m in report['mentions'])
    assert not any(b['can_seed_lookup'] for b in report['bridges'])


def test_negation_scope_does_not_leak_to_next_sentence():
    text = '订单号 O-001 不属于合同号 C-001。订单号 O-002 对应合同号 C-002。'
    report = extract_document_entities(text, location={'file': 'claims.txt'})
    assert [m['assertion']['status'] for m in report['mentions']] == ['negated', 'negated', 'affirmed', 'affirmed']
    assert [b['can_seed_lookup'] for b in report['bridges']] == [False, True]


def test_reused_value_different_types_never_enters_same_block():
    first = extract_document_entities('订单号: 000123', location={'file': 'order.txt'})
    second = extract_document_entities('样本号: 000123', location={'file': 'sample.txt'})
    linked = candidate_mention_links(first['mentions'] + second['mentions'])
    assert linked['pairs'] == []
    assert linked['coverage']['blocks'] == 2


@pytest.mark.parametrize('scope,first,second', [('tenant', 'north', 'south'), ('period', '2024', '2025'), ('revision', 'v1', 'v2')])
def test_reused_id_with_conflicting_scope_remains_conflict(scope, first, second):
    one = extract_document_entities(f'{scope}: {first}; Order ID O-001.', location={'file': 'one.txt'})
    two = extract_document_entities(f'{scope}: {second}; Order ID O-001.', location={'file': 'two.txt'})
    pair = candidate_mention_links(one['mentions'] + two['mentions'])['pairs'][0]
    assert pair['status'] == 'scope_conflict'
    assert pair['can_seed_lookup'] is False


def test_missing_scope_is_not_globally_unique_identifier():
    one = extract_document_entities('Order ID O-001.', location={'file': 'one.txt'})
    two = extract_document_entities('Order ID O-001.', location={'file': 'two.txt'})
    pair = candidate_mention_links(one['mentions'] + two['mentions'])['pairs'][0]
    assert pair['status'] == 'scope_unresolved'
    assert pair['identity_verified'] is False
    assert pair['can_seed_lookup'] is False


def test_same_name_is_not_an_entity_merge():
    one = extract_document_entities('客户姓名: 张三', location={'file': 'one.txt'})
    two = extract_document_entities('客户姓名: 张三', location={'file': 'two.txt'})
    assert all(m['entity_type'] == 'name' for m in one['mentions'] + two['mentions'])
    assert candidate_mention_links(one['mentions'] + two['mentions'])['pairs'] == []


def test_multiple_same_type_ids_make_cooccurrence_bridge_ambiguous():
    report = extract_document_entities('订单号 O-001 与订单号 O-002 对应合同号 C-001。', location={'file': 'many.txt'})
    assert len(report['bridges']) == 2
    assert not any(b['can_seed_lookup'] for b in report['bridges'])
    assert all(b['reason'] == 'multiple_identifiers_in_same_namespace' for b in report['bridges'])


def test_ocr_confidence_remains_uncertain_even_when_high():
    report = extract_document_entities('Order ID O-001 references Contract ID C-001.',
                                       location={'file': 'scan.pdf', 'page': 1}, ocr_confidence=0.999)
    assert report['mentions']
    assert all(m['ocr']['review_required'] for m in report['mentions'])
    assert not any(b['can_seed_lookup'] for b in report['bridges'])


def test_structured_fields_keep_pointers_units_and_conflicting_customer_context():
    def record(file, customer):
        return extract_document_entities('', location={'file': file, 'row': 2,
                'field_pointers': {'order_id': '/0/order_id', 'customer_id': '/0/customer_id'}},
                fields={'tenant': 'A', 'order_id': '000123', 'customer_id': customer, 'amount': '123.4500'})
    first, second = record('one.json', 'C-001'), record('two.json', 'C-002')
    assert first['mentions'][0]['raw_value'] == '000123'
    assert first['mentions'][0]['source_location']['json_pointer'] == '/0/order_id'
    pair = next(p for p in candidate_mention_links(first['mentions'] + second['mentions'])['pairs']
                if p['identifier_namespace'] == 'order')
    assert pair['status'] == 'context_conflict'
    assert pair['can_seed_lookup'] is False


def test_decimal_measure_and_calendar_precision_are_preserved():
    report = extract_document_entities('样本号 S-001，含量12.500 mg；记录日期2025-02-28；错误日期2025-02-31。')
    measure = next(m for m in report['mentions'] if m['entity_type'] == 'measure')
    assert measure['normalized_value'] == {'value': '12.500', 'unit': 'mg'}
    dates = [m for m in report['mentions'] if m['entity_type'] == 'date']
    assert dates[0]['normalized_value'] == '2025-02-28'
    assert dates[1]['normalized_value'] is None
    assert dates[1]['normalization'] == 'unresolved_calendar'


def test_large_duplicate_block_does_not_materialize_quadratic_pairs():
    mentions = []
    for index in range(600):
        mentions.extend(extract_document_entities('Order ID O-001.', location={'file': f'{index}.txt'})['mentions'])
    tracemalloc.start()
    started = time.perf_counter()
    result = candidate_mention_links(mentions, max_pairs=25)
    seconds = time.perf_counter() - started
    _, peak = tracemalloc.get_traced_memory()
    tracemalloc.stop()
    assert result['coverage']['candidate_pairs'] == 600 * 599 // 2
    assert len(result['pairs']) == 25
    assert result['coverage']['complete'] is False
    assert peak < 8 * 1024 * 1024
    assert seconds < 5


def test_single_clause_many_ids_obeys_bridge_budget():
    text = ', '.join(f'Order ID O-{i:04d}' for i in range(100)) + ', ' + ', '.join(f'Contract ID C-{i:04d}' for i in range(100))
    report = extract_document_entities(text, max_bridges=10)
    assert len(report['mentions']) == 200
    assert len(report['bridges']) == 10
    assert report['coverage']['omitted_bridges'] == 9990


def test_no_result_target_search_exposes_only_eligible_lookup_seeds(tmp_path):
    yes = tmp_path / 'positive.txt'
    no = tmp_path / 'negative.txt'
    yes.write_text('Tenant: north; Order ID O-001 references Contract ID C-001.', encoding='utf-8')
    no.write_text('Order ID O-001 does not reference Contract ID C-999.', encoding='utf-8')
    report = explore_evidence('Order ID O-001', [yes, no])
    assert report['analysis_stage'] == 'evidence_search'
    assert len(report['related_evidence']) == 2
    assert [(s['identifier_namespace'], s['value']) for s in report['lookup_seeds']] == [('contract', 'C-001')]
    assert report['entity_mentions']
    assert report['coverage']['retrieval_complete'] is True


def test_bare_id_and_explicit_chinese_topic_search(tmp_path):
    source = tmp_path / 'notes.txt'
    source.write_text('订单号 ORD-000001：设备校准已完成。\n\n设备校准需保留记录。', encoding='utf-8')
    by_id = explore_evidence('ORD-000001', [source])
    by_topic = explore_evidence('设备校准', [source])
    assert by_id['coverage']['matched_units'] == 1
    assert by_topic['coverage']['matched_units'] == 2


def test_identifier_columns_preserve_namespace_and_case():
    assert identifier_namespace('order_id') == 'order'
    assert identifier_namespace('设备编号') == 'device'
    assert identifier_namespace('id') == 'unspecified'
    one = extract_document_entities('', fields={'order_id': 'Ab-001'}, location={'file': 'one.csv'})
    two = extract_document_entities('', fields={'order_id': 'AB-001'}, location={'file': 'two.csv'})
    assert candidate_mention_links(one['mentions'] + two['mentions'])['pairs'] == []


def test_json_narrative_field_keeps_pointer_and_bridges_record_id(tmp_path):
    source = tmp_path / 'order.json'
    source.write_text(json.dumps({'order_id': 'O-001', 'tenant': 'north',
                                 'notes': '合同号 C-001 对应此订单。'}, ensure_ascii=False), encoding='utf-8')
    report = explore_evidence('Tenant: north; Order ID O-001', [source])
    contract = next(m for m in report['entity_mentions'] if m['identifier_namespace'] == 'contract')
    assert contract['source_location']['json_pointer'] == '/notes'
    assert contract['selector']['offset_scope'] == 'decoded_field_value'
    assert contract['selector']['quote']['exact'] == 'C-001'
    assert contract['scope']['values']['tenant'] == 'north'
    assert [(seed['identifier_namespace'], seed['value']) for seed in report['lookup_seeds']] == [('contract', 'C-001')]


def test_negative_json_note_cannot_bridge_record_id():
    report = extract_document_entities('order_id=O-001 | notes=不是合同号 C-999',
                                       location={'file': 'notes.json', 'json_pointer': '/0'},
                                       fields={'order_id': 'O-001', 'notes': '不是合同号 C-999'})
    assert report['bridges']
    assert not any(bridge['can_seed_lookup'] for bridge in report['bridges'])


def test_bare_order_field_is_not_assumed_to_be_business_identifier():
    report = extract_document_entities('', fields={'order': 2, 'sample': 'blood', 'device': 'pump'})
    assert report['mentions'] == []


def test_common_words_ending_in_id_are_not_identifier_labels():
    assert identifier_namespace('invalid') is None
    assert identifier_namespace('grid') is None
    assert identifier_namespace('video') is None
    assert identifier_namespace('update_id') == 'field:updateid'


def test_scope_header_changes_on_later_line_without_leaking_backwards():
    text = 'Tenant: north\nOrder ID O-001 references Contract ID C-001.\nTenant: south\nOrder ID O-001 references Contract ID C-002.'
    report = extract_document_entities(text, location={'file': 'multi-tenant.log'})
    ids = [m for m in report['mentions'] if m['entity_type'] == 'identifier']
    assert [m['scope']['values']['tenant'] for m in ids] == ['north', 'north', 'south', 'south']
    pair = next(p for p in candidate_mention_links(ids)['pairs'] if p['identifier_namespace'] == 'order')
    assert pair['status'] == 'scope_conflict'


def test_two_scopes_in_same_line_remain_ambiguous():
    report = extract_document_entities('Tenant: north; Tenant: south; Order ID O-001 references Contract ID C-001.')
    assert all(m['scope']['ambiguous']['tenant'] == ['north', 'south'] for m in report['mentions'])
    assert not any(b['can_seed_lookup'] for b in report['bridges'])


@pytest.mark.parametrize('text,expected', [
    ('租约 LX-27 的付款单是 PM-46。', [('rental', 'LX-27'), ('payment', 'PM-46')]),
    ('送货单 DN-16 所列批次为 BT-25。', [('delivery', 'DN-16'), ('batch', 'BT-25')]),
    ('论文 P-18 使用实验 EX-45。', [('publication', 'P-18'), ('experiment', 'EX-45')]),
    ('For order OR-27 the confirmed receipt is RC-36.', [('order', 'OR-27'), ('receipt', 'RC-36')]),
])
def test_natural_object_labels_and_link_verbs(text, expected):
    report = extract_document_entities(text)
    assert [(m['identifier_namespace'], m['raw_value']) for m in report['mentions']] == expected
    assert report['bridges'][0]['can_seed_lookup'] is True


def test_custom_literal_aliases_work_in_query_text_fields_and_scope(tmp_path):
    config = {'entity_aliases': {'work_item': ['作业卡', 'job_key'], 'recipe': ['配方卡', 'recipe_key']},
              'scope_aliases': {'facility': ['实验站', 'facility_key']}}
    assert identifier_namespace('job_key', config=config) == 'work_item'
    assert scope_namespace('facility_key', config) == 'facility'
    source = tmp_path / 'handoff.txt'
    source.write_text('实验站: Z-A\n作业卡 JOB-007 对应配方卡 FORM-011。', encoding='utf-8')
    report = explore_evidence('作业卡 JOB-007', [source], config=config)
    seed = report['lookup_seeds'][0]
    assert (seed['identifier_namespace'], seed['value']) == ('recipe', 'FORM-011')
    assert seed['scope']['values'] == {'facility': 'Z-A'}
    assert seed['provenance'][0]['to']['selector']['quote']['exact'] == 'FORM-011'
    assert seed['anchor']['value'] == 'JOB-007'


def test_conflicting_alias_namespaces_are_rejected():
    with pytest.raises(ValueError, match='multiple namespaces'):
        extract_document_entities('Order O-3', entity_aliases={'made_up_role': ['order']})


def test_retracted_literal_repetition_keeps_negative_source_span():
    text = 'Order A-17 might use payment PM-39. The reviewer ruled out PM-39; it belongs to another order.'
    report = extract_document_entities(text, location={'file': 'review.txt', 'char_offset': 20})
    matches = [mention for mention in report['mentions'] if mention['raw_value'] == 'PM-39']
    assert [mention['assertion']['status'] for mention in matches] == ['uncertain', 'negated']
    assert matches[1]['typing_reference'] == matches[0]['mention_id']
    assert matches[1]['selector']['source_start'] == 20 + text.rindex('PM-39')
    assert not any(bridge['can_seed_lookup'] for bridge in report['bridges'])


def test_explicit_chinese_denial_cannot_seed_correspondence():
    packet = extract_document_entities('租约 LS-19 与发票 IV-73 看似有关，但核对后没有对应关系。')
    assert {m['raw_value'] for m in packet['mentions']} == {'LS-19', 'IV-73'}
    assert all(m['assertion']['status'] == 'negated' for m in packet['mentions'])
    assert not any(b['can_seed_lookup'] for b in packet['bridges'])


def test_retrieval_continues_to_later_paragraph_with_explicit_provenance(tmp_path):
    notes = tmp_path / 'transfers.txt'
    notes.write_text('租约 LX-27 对应付款单 PM-46。\n\n付款单 PM-46 对应发票 IV-53。', encoding='utf-8')
    target = {'rows': [{'row_num': 7, 'values': {'rental_id': 'LX-27'}}]}
    report = retrieve_evidence(target, [notes])
    seeds = build_lookup_seeds(target, report['evidence'])
    final = next(seed for seed in seeds if seed['value'] == 'IV-53')
    assert final['result_row'] == 7 and len(final['provenance']) == 2
    assert len(report['evidence']) == 2
    assert report['coverage']['document_expansion_complete'] is True
    assert final['provenance'][0]['evidence_id'] != final['provenance'][1]['evidence_id']
    assert all(step['source_sha256'] == report['sources'][0]['source_sha256'] for step in final['provenance'])


def test_scope_propagates_across_two_clauses_without_losing_leading_zero(tmp_path):
    notes = tmp_path / 'research.txt'
    notes.write_text('本记录属于实验室 Z-B。论文 P-18 使用实验 EX-45。实验 EX-45 的样本编号为 00073。', encoding='utf-8')
    report = explore_evidence('论文 P-18', [notes])
    final = next(seed for seed in report['lookup_seeds'] if seed['value'] == '00073')
    assert final['scope']['values']['lab'] == 'Z-B'
    assert len(final['provenance']) == 2
    assert any(m['raw_value'] == 'Z-B' and m['identifier_namespace'] == 'lab' for m in report['entity_mentions'])


def test_scope_conflict_prevents_cross_document_bridge(tmp_path):
    first, second = tmp_path / 'first.txt', tmp_path / 'second.txt'
    first.write_text('Tenant: north\nOrder O-25 references Contract C-84.', encoding='utf-8')
    second.write_text('Tenant: south\nContract C-84 references Invoice I-98.', encoding='utf-8')
    report = explore_evidence('Order O-25', [first, second])
    assert {seed['value'] for seed in report['lookup_seeds']} == {'C-84'}
    assert len(report['related_evidence']) == 2  # Conflicting passage remains reviewable.


def test_document_hop_budget_reports_unexpanded_frontier(tmp_path):
    notes = tmp_path / 'chain.txt'
    notes.write_text('Order O-25 references Contract C-84.\n\nContract C-84 references Invoice I-98.', encoding='utf-8')
    report = explore_evidence('Order O-25', [notes], config={'evidence': {'max_document_hops': 1}})
    assert report['coverage']['retrieval_complete'] is False
    assert report['coverage']['truncated'] is True
    assert any(issue['code'] == 'document_hop_budget' for issue in report['issues'])


def test_target_text_bridge_actually_queries_next_structured_source(tmp_path):
    notes, table = tmp_path / 'links.txt', tmp_path / 'invoices.csv'
    notes.write_text('Order O-25 references Contract C-84.\n\nContract C-84 references Invoice I-98.', encoding='utf-8')
    table.write_text('invoice_id,amount\nI-98,405.00\nI-99,405.00\n', encoding='utf-8')
    report = explore_evidence('Order O-25', [notes, table], cache_dir=tmp_path / 'cache')
    positives = [(Path(match['file']).name, match['row_num']) for row in report['result_rows']
                 for match in row['source_matches'] if match['status'] == 'located']
    assert ('invoices.csv', 2) in positives
    assert ('invoices.csv', 3) not in positives
    assert report['source_trace']['result_ref']['kind'] == 'document_lookup_seeds'


def test_explicit_typed_target_queries_structured_records_without_fake_file(tmp_path):
    orders = tmp_path / 'orders.csv'
    orders.write_text('order_id,amount\nO-25,405.00\nO-26,405.00\n', encoding='utf-8')
    report = explore_evidence('Order O-25', [orders], cache_dir=tmp_path / 'cache')
    matches = [match for row in report['result_rows'] for match in row['source_matches']]
    assert [(m['row_num'], m['status']) for m in matches] == [(2, 'located')]
    assert report['query_seeds'][0]['source_location']['kind'] == 'user_query'
    assert report['source_trace']['result_file'] is None
    assert all(row['row_num_kind'] == 'lookup_target_index' for row in report['result_rows'])


def test_lookup_closure_does_not_return_anchor_or_reverse_shorter_path(tmp_path):
    notes = tmp_path / 'cycle.txt'
    notes.write_text('Order O-25 references Contract C-84.\n\nContract C-84 references Invoice I-98.\n\nInvoice I-98 references Order O-25.', encoding='utf-8')
    report = explore_evidence('Order O-25', [notes])
    assert {seed['value'] for seed in report['lookup_seeds']} == {'C-84', 'I-98'}
    assert all(len(seed['provenance']) == 1 for seed in report['lookup_seeds'])


def test_question_is_uncertain_without_polluting_prior_affirmative_clause(tmp_path):
    notes = tmp_path / 'questions.txt'
    notes.write_text('订单 OR-51 对应发票 IV-27。有人问“订单 OR-51 对应的是发票 IV-28 吗？”这只是待答问题。', encoding='utf-8')
    report = explore_evidence('订单 OR-51', [notes])
    assert {seed['value'] for seed in report['lookup_seeds']} == {'IV-27'}
    question = next(mention for mention in report['entity_mentions'] if mention['raw_value'] == 'IV-28')
    assert question['assertion']['status'] == 'uncertain'


def test_same_source_correction_retracts_previous_pair_and_keeps_new_pair(tmp_path):
    notes = tmp_path / 'revision.txt'
    notes.write_text('订单 OR-51 对应发票 IV-27。\n\n复核更正：撤回上一段的对应关系。订单 OR-51 实际对应发票 IV-28，发票 IV-27 属于订单 OR-52。', encoding='utf-8')
    report = explore_evidence('订单 OR-51', [notes])
    assert {seed['value'] for seed in report['lookup_seeds']} == {'IV-28'}
    prior = next(e for e in report['related_evidence'] if e['source_location']['paragraph'] == 1)
    assert all(m['assertion']['status'] == 'retracted' for m in prior['entity_mentions'])
    assert all(m['original_assertion']['status'] == 'affirmed' for m in prior['entity_mentions'])
    assert prior['source_revisions'][0]['selector']['exact'].startswith('撤回上一段')
    assert prior['source_revisions'][0]['source_location']['paragraph'] == 2


def test_withdrawal_without_repeated_identifier_still_blocks_previous_paragraph(tmp_path):
    notes = tmp_path / 'withdrawal.txt'
    notes.write_text('Order OR-51 references Invoice IV-27.\n\nWe retract the previous paragraph.', encoding='utf-8')
    report = explore_evidence('Order OR-51', [notes])
    assert report['lookup_seeds'] == []
    assert report['source_revisions']
    assert report['related_evidence'][0]['entity_mentions'][0]['assertion']['status'] == 'retracted'


def test_unlocated_structured_record_cannot_supply_its_own_missing_scope(tmp_path):
    notes, table = tmp_path / 'notes.txt', tmp_path / 'samples.csv'
    notes.write_text('Publication P-72 uses Sample ID S-51.', encoding='utf-8')
    table.write_text('sample_id,lab_id,metric\nS-51,LAB-C,8.25\n', encoding='utf-8')
    target = {'identifiers': [{'namespace': 'publication', 'value': 'P-72'}]}
    evidence = retrieve_evidence(target, [notes, table])['evidence']
    # Supply the unrelated row as an adversarial lexical hit as well.
    evidence += retrieve_evidence({'identifiers': [{'namespace': 'sample', 'value': 'S-51'}]}, [table])['evidence']
    seeds = build_lookup_seeds(target, evidence)
    assert [(seed['identifier_namespace'], seed['value']) for seed in seeds] == [('sample', 'S-51')]
    assert seeds[0]['scope']['values'] == {}


def test_already_located_record_note_exports_bridge_with_query_proof():
    match = {'file': 'records.json', 'sheet': None, 'row_num': 1, 'json_pointer': '/0',
             'source_sha256': 'a' * 64, 'source_ref': 'src_001', 'status': 'located', 'query_complete': True,
             'match_keys': ['order_id', 'tenant'], 'values': {'order_id': 'OR-51', 'tenant': 'north',
                                                          'notes': 'Contract CT-73 references this order.'},
             'field_pointers': {'order_id': '/0/order_id', 'tenant': '/0/tenant', 'notes': '/0/notes'}}
    evidence = evidence_from_located_records([{'source_matches': [match]}])
    assert len(evidence) == 1
    assert evidence[0]['coverage'] == {'scope': 'already_located_record_only', 'source_scan_claim': False}
    contract = next(m for m in evidence[0]['entity_mentions'] if m['identifier_namespace'] == 'contract')
    assert contract['source_location']['json_pointer'] == '/0/notes'
    seeds = build_lookup_seeds({'identifiers': [{'namespace': 'order', 'value': 'OR-51',
                              'scope': {'values': {'tenant': 'north'}, 'ambiguous': {}}}]}, evidence)
    assert [seed['value'] for seed in seeds] == ['CT-73']
    proof = seeds[0]['provenance'][0]['record_localization']
    assert proof['query_complete'] is True and proof['source_ref'] == 'src_001'
    assert proof['source_sha256'] == 'a' * 64
    assert proof['location']['json_pointer'] == '/0'
    _mark_located_records(evidence, {'result_rows': []})
    assert not evidence[0].get('record_localization')
    assert build_lookup_seeds({'identifiers': [{'namespace': 'order', 'value': 'OR-51'}]}, evidence) == []


def test_incomplete_or_ambiguous_record_query_cannot_export_bridge():
    base = {'file': 'records.csv', 'row_num': 2, 'source_sha256': 'b' * 64, 'source_ref': 'src_002',
            'values': {'order_id': 'OR-51', 'notes': 'Contract CT-73 references this order.'}}
    assert evidence_from_located_records([{**base, 'status': 'ambiguous', 'query_complete': True},
                                          {**base, 'status': 'located', 'query_complete': False}]) == []
