"""Source admission and non-invention regressions for appearance specifications.

These are compiler invariants, not validation of witness memory or likeness.
No private blind-test rubrics, credentials, image tools or remote models are used.
"""
import copy
import json
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / 'scripts'))
from portrait import FEATURES, compile_portrait, run


def evidence(eid='E1', statement='眉毛平直。', *, kind='direct_witness',
             source=None, group=None, contamination='none_reported', parents=None):
    result = {'evidence_id': eid, 'kind': kind, 'source_id': source or 'W_' + eid,
              'dependency_group': group or 'D_' + eid, 'statement': statement,
              'contamination': contamination}
    if parents is not None:
        result['derived_from'] = parents
    return result


def observation(feature='eyebrows', value='平直', quote='眉毛平直', *, eid='E1',
                status='reported', side='unspecified'):
    return {'feature_id': feature, 'status': status, 'value': value,
            'quotes': [{'evidence_id': eid, 'quote': quote}], 'laterality': side}


def case(sources=None, observations=None):
    return {'case_id': 'PORTRAIT-UNIT',
            'evidence': [evidence()] if sources is None else sources,
            'observations': [observation()] if observations is None else observations}


def feature(report, fid):
    return next(item for item in report['features'] if item['feature_id'] == fid)


class TestAppearanceAdmission(unittest.TestCase):
    def test_supported_feature_remains_partial_and_image_is_not_claimed(self):
        report = compile_portrait(case())
        self.assertEqual(report['status'], 'specification_ready')
        self.assertEqual(report['analysis_stage'], 'portrait_specification')
        self.assertEqual(report['render_state'], 'not_generated')
        self.assertNotIn('rendered_images', report)
        self.assertIn('quote_entailment', report['review_required'])
        self.assertEqual(report['render_request']['mode'], 'feature_sheet')
        bindings = report['render_request']['feature_bindings']
        self.assertEqual([(b['feature_id'], b['value']) for b in bindings], [('eyebrows', '平直')])
        for fid in set(FEATURES) - {'eyebrows'}:
            self.assertEqual(feature(report, fid)['claims'], [], fid)
            self.assertIn(fid, report['render_request']['unknown_regions'])

    def test_scene_trace_cannot_be_promoted_into_face_feature(self):
        source = evidence(statement='足迹长28厘米，门锁被撬。', kind='crime_scene')
        report = compile_portrait(case([source], [observation('face_shape', '方脸', '足迹长28厘米')]))
        self.assertEqual(report['status'], 'insufficient_information')
        self.assertIsNone(report['render_request'])
        claim = feature(report, 'face_shape')['claims'][0]
        self.assertFalse(claim['render_eligible'])
        self.assertIn('not_direct_appearance_observation', claim['exclusion_reasons'])

    def test_no_observation_is_a_valid_insufficient_result(self):
        report = compile_portrait(case([evidence(statement='只听见脚步，未看到人。')], []))
        self.assertEqual(report['status'], 'insufficient_information')
        self.assertIsNone(report['render_request'])
        self.assertTrue(all(not f['claims'] for f in report['features']))
        self.assertTrue(report['clarification_questions'])

    def test_hair_and_glasses_do_not_authorize_filling_a_face(self):
        report = compile_portrait(case(
            [evidence(statement='短发，戴黑框眼镜；没看清五官。')],
            [observation('hair', '短发', '短发'), observation('glasses', '黑框眼镜', '戴黑框眼镜')]))
        self.assertEqual(feature(report, 'hair')['status'], 'reported')
        self.assertEqual(feature(report, 'glasses')['status'], 'reported')
        self.assertIsNone(report['render_request'])
        self.assertEqual(report['status'], 'insufficient_information')

    def test_indirect_or_contaminated_or_uncertain_observation_stays_out_of_render(self):
        cases = [(evidence(kind=k), observation()) for k in ('hearsay', 'prior_composite', 'other')]
        cases += [(evidence(contamination=c), observation()) for c in ('unknown', 'possible', 'known')]
        cases += [(evidence(), observation(status='uncertain'))]
        for source, obs in cases:
            with self.subTest(source=source, observation=obs):
                report = compile_portrait(case([source], [obs]))
                self.assertIsNone(report['render_request'])
                self.assertFalse(feature(report, 'eyebrows')['claims'][0]['render_eligible'])

    def test_occluded_and_unknown_features_have_no_claims(self):
        report = compile_portrait(case(observations=[observation(),
            {'feature_id': 'nose', 'status': 'not_visible'},
            {'feature_id': 'ears', 'status': 'unknown'}]))
        self.assertEqual(feature(report, 'nose')['status'], 'not_visible')
        for fid in ('nose', 'ears'):
            self.assertEqual(feature(report, fid)['claims'], [])
            self.assertIn(fid, report['render_request']['unknown_regions'])

    def test_unknown_must_not_hide_asserted_value_or_side(self):
        for extras in ({'value': '高鼻梁'}, {'quotes': [{'evidence_id': 'E1', 'quote': '眉毛'}]},
                       {'laterality': 'subject_left'}):
            with self.subTest(extras=extras), self.assertRaises(ValueError):
                compile_portrait(case(observations=[{'feature_id': 'nose', 'status': 'unknown', **extras}]))

    def test_quote_must_exist_in_its_referenced_source(self):
        sources = [evidence('E1', '眉毛平直。'), evidence('E2', '鼻梁较高。')]
        for obs in (observation('nose', '高鼻梁', '鼻梁较高', eid='E1'),
                    observation(quote='粗而浓的眉毛'), observation(eid='missing')):
            with self.subTest(observation=obs), self.assertRaises(ValueError):
                compile_portrait(case(sources, [obs]))

    def test_quotation_whitespace_normalization_does_not_rewrite_source(self):
        statement = '眉毛\r\n  平直。'
        report = compile_portrait(case([evidence(statement=statement)],
                                       [observation(quote='眉毛 平直')]))
        self.assertEqual(report['evidence'][0]['statement'], statement)
        self.assertEqual(feature(report, 'eyebrows')['claims'][0]['quotes'][0]['quote'], '眉毛 平直')

    def test_laterality_is_preserved_in_claim_and_render_binding(self):
        report = compile_portrait(case([evidence(statement='本人左眼外侧有一处短疤。')],
            [observation('visible_marks', '左眼外侧短疤', '本人左眼外侧有一处短疤', side='subject_left')]))
        self.assertEqual(feature(report, 'visible_marks')['claims'][0]['laterality'], 'subject_left')
        self.assertEqual(report['render_request']['feature_bindings'][0]['laterality'], 'subject_left')
        self.assertEqual(feature(report, 'eyes')['claims'], [])

    def test_conflicting_descriptions_are_retained_without_averaged_render(self):
        report = compile_portrait(case(
            [evidence('E1', '鼻梁很高。'), evidence('E2', '鼻梁低平。')],
            [observation('nose', '高鼻梁', '鼻梁很高'),
             observation('nose', '低平鼻梁', '鼻梁低平', eid='E2')]))
        self.assertEqual(report['status'], 'clarification_required')
        self.assertIn('nose', report['unresolved_conflicts'])
        claims = feature(report, 'nose')['claims']
        self.assertEqual({c['value'] for c in claims}, {'高鼻梁', '低平鼻梁'})
        self.assertTrue(all(not c['render_eligible'] for c in claims))
        self.assertIsNone(report['render_request'])
        self.assertTrue(any(q['feature_id'] == 'nose' for q in report['clarification_questions']))

    def test_opposite_sides_do_not_silently_become_a_bilateral_mark(self):
        report = compile_portrait(case(
            [evidence('E1', '左脸有疤。'), evidence('E2', '右脸有疤。')],
            [observation('visible_marks', '脸部疤痕', '左脸有疤', side='subject_left'),
             observation('visible_marks', '脸部疤痕', '右脸有疤', eid='E2', side='subject_right')]))
        self.assertIn('visible_marks', report['unresolved_conflicts'])
        self.assertEqual({c['laterality'] for c in feature(report, 'visible_marks')['claims']},
                         {'subject_left', 'subject_right'})
        self.assertIsNone(report['render_request'])

    def test_complementary_witness_features_stay_in_their_own_panels(self):
        supplied = case(
            [evidence('E1', '只记得眉毛平直。', source='W1'),
             evidence('E2', '只看清鼻梁较高。', source='W2')],
            [observation(), observation('nose', '较高鼻梁', '鼻梁较高', eid='E2')])
        supplied['render_mode'] = 'partial_composite'
        report = compile_portrait(supplied)
        request = report['render_request']
        self.assertEqual(request['mode'], 'feature_sheet')
        panels = {p['source_id']: p for p in request['source_panels']}
        self.assertEqual(set(panels), {'W1', 'W2'})
        for source, own_feature, own_eid, other_feature in (
                ('W1', 'eyebrows', 'E1', 'nose'), ('W2', 'nose', 'E2', 'eyebrows')):
            panel = panels[source]
            self.assertEqual([b['feature_id'] for b in panel['feature_bindings']], [own_feature])
            self.assertEqual(panel['feature_bindings'][0]['source_ids'], [source])
            self.assertEqual(panel['feature_bindings'][0]['evidence_ids'], [own_eid])
            self.assertIn(other_feature, panel['unknown_regions'])
            self.assertNotIn(own_feature, panel['unknown_regions'])

    def test_original_profile_context_reaches_render_request_and_prompt(self):
        source = evidence(statement='鼻梁较高。')
        source['context'] = '仅见本人左侧轮廓，约两米，路灯下，未见正面。'
        report = compile_portrait(case([source], [observation('nose', '较高鼻梁', '鼻梁较高')]))
        request = report['render_request']
        self.assertEqual(request['source_panels'][0]['observation_contexts'],
                         [{'evidence_id': 'E1', 'context': source['context']}])
        self.assertIn(source['context'], request['prompt'])


class TestPortraitProvenance(unittest.TestCase):
    def test_copy_transitive_dependency_and_repeated_witness_are_not_independent(self):
        sources = [evidence('E1', '眉毛平直。', source='W1', group='G1'),
                   evidence('E2', '眉毛平直。', source='W2', group='G2'),
                   evidence('E3', '转录：眉毛平直。', source='W3', group='G3', parents=['E2']),
                   evidence('E4', '再次陈述眉毛平直。', source='W1', group='G4')]
        observations = [observation(eid=e['evidence_id']) for e in sources]
        report = compile_portrait(case(sources, observations))
        groups = report['source_groups']
        self.assertEqual(len(groups), 1)
        self.assertEqual(set(groups[0]['evidence_ids']), {'E1', 'E2', 'E3', 'E4'})
        self.assertEqual(groups[0]['independence'], 'not_established')
        self.assertEqual(set(report['render_request']['feature_bindings'][0]['evidence_ids']),
                         {'E1', 'E2', 'E3', 'E4'})
        self.assertNotIn('confidence', report)

    def test_distinct_group_labels_do_not_claim_proven_independence(self):
        report = compile_portrait(case(
            [evidence('E1', '眉毛平直。'), evidence('E2', '此人眉毛平直。')],
            [observation(), observation(eid='E2')]))
        self.assertEqual(len(report['source_groups']), 2)
        self.assertTrue(all(g['independence'] == 'not_established' for g in report['source_groups']))

    def test_mixed_followup_does_not_make_its_initial_sources_mutually_dependent(self):
        supplied = case([
            evidence('E1', '当时眉毛平直。', source='W1'),
            evidence('E2', '我记得鼻梁高。', source='W2'),
            evidence('E3', '看过两份初访后，我觉得眉毛平直。', source='W1',
                     contamination='known', parents=['E1', 'E2'])],
            [observation(quote='眉毛平直'), observation(eid='E3', quote='眉毛平直')])
        report = compile_portrait(supplied)
        group_of = {eid: g['dependency_group'] for g in report['source_groups'] for eid in g['evidence_ids']}
        self.assertNotEqual(group_of['E1'], group_of['E2'])
        self.assertEqual(group_of['E1'], group_of['E3'])
        self.assertEqual({(x['derived_evidence_id'], x['upstream_evidence_id']) for x in report['dependency_links']},
                         {('E3', 'E1'), ('E3', 'E2')})
        followup = next(c for c in feature(report, 'eyebrows')['claims'] if c['evidence_ids'] == ['E3'])
        self.assertEqual(set(followup['dependency_groups']), {group_of['E1'], group_of['E2']})
        self.assertFalse(followup['render_eligible'])

    def test_duplicate_or_unknown_upstream_ids_are_rejected(self):
        for sources in ([evidence(), evidence()], [evidence(parents=['missing'])],
                        [evidence(parents=['E1'])]):
            with self.subTest(sources=sources), self.assertRaises(ValueError):
                compile_portrait(case(sources, []))

    def test_wrapping_a_contaminated_or_indirect_ancestor_does_not_make_it_clean(self):
        ancestors = [evidence('E0', '眉毛平直。', kind='prior_composite'),
                     evidence('E0', '眉毛平直。', contamination='known'),
                     evidence('E0', '眉毛平直。', kind='hearsay')]
        for ancestor in ancestors:
            with self.subTest(ancestor=ancestor):
                sources = [ancestor,
                           evidence('E1', '转录：眉毛平直。', parents=['E0']),
                           evidence('E2', '再次转录：眉毛平直。', kind='image_observation', parents=['E1'])]
                report = compile_portrait(case(sources, [observation(eid='E2')]))
                self.assertIsNone(report['render_request'])
                claim = feature(report, 'eyebrows')['claims'][0]
                self.assertFalse(claim['render_eligible'])
                self.assertEqual(set(claim['lineage_evidence_ids']), {'E0', 'E1', 'E2'})

    def test_identifiers_with_surrounding_whitespace_cannot_split_provenance(self):
        for field in ('evidence_id', 'source_id', 'dependency_group'):
            supplied = case()
            supplied['evidence'][0][field] = ' ' + supplied['evidence'][0][field] + ' '
            with self.subTest(field=field), self.assertRaises(ValueError):
                compile_portrait(supplied)

    def test_upstream_cycle_is_rejected(self):
        sources = [evidence('E1', parents=['E2']), evidence('E2', parents=['E1'])]
        with self.assertRaises(ValueError):
            compile_portrait(case(sources, []))

    def test_unexpected_prediction_fields_are_not_accepted(self):
        for supplied in ({**case(), 'guilt_probability': 0.9},
                         case(observations=[{**observation(), 'ethnicity': 'inferred'}]),
                         case(observations=[{**observation(), 'feature_id': 'personality'}])):
            with self.subTest(supplied=supplied), self.assertRaises(ValueError):
                compile_portrait(supplied)

    def test_compiler_does_not_mutate_source_material(self):
        source = case()
        before = copy.deepcopy(source)
        compile_portrait(source)
        self.assertEqual(source, before)

    def test_revision_cannot_rewrite_or_remove_original_evidence(self):
        original = compile_portrait(case())
        rewritten = case()
        rewritten['evidence'][0]['statement'] = '修订后：眉毛平直。'
        for supplied in (rewritten, case([], [])):
            with self.subTest(case=supplied), self.assertRaises(ValueError):
                compile_portrait(supplied, original)
        wrong_case = case()
        wrong_case['case_id'] = 'ANOTHER-CASE'
        with self.assertRaises(ValueError):
            compile_portrait(wrong_case, original)

    def test_revision_records_removed_claim_and_requires_reason(self):
        original = compile_portrait(case())
        revised_input = case(observations=[])
        with self.assertRaises(ValueError):
            compile_portrait(revised_input, original)
        revised_input['revision_reason'] = '复核原始采访后，确认眉部描述是采访者提示；暂不用于绘图。'
        revised = compile_portrait(revised_input, original)
        self.assertIsNone(revised['render_request'])
        self.assertEqual(revised['revision']['changed_features'], ['eyebrows'])
        self.assertEqual(revised['revision']['reason'], revised_input['revision_reason'])
        prior_claims = revised['revision']['removed_or_changed_claims']
        self.assertEqual(len(prior_claims), 1)
        self.assertEqual(prior_claims[0]['feature_id'], 'eyebrows')
        self.assertEqual(prior_claims[0]['claim']['value'], '平直')
        self.assertEqual(revised['evidence'], original['evidence'])
        self.assertEqual(len(revised['revision']['previous_sha256']), 64)

    def test_unchanged_revision_does_not_need_an_invented_reason(self):
        original = compile_portrait(case())
        updated = compile_portrait(case(), original)
        self.assertEqual(updated['revision']['changed_features'], [])
        self.assertEqual(updated['revision']['removed_or_changed_claims'], [])


class TestPortraitIO(unittest.TestCase):
    def test_outputs_cannot_overwrite_source_or_each_other(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            source = root / 'case.json'
            source.write_text(json.dumps(case(), ensure_ascii=False), encoding='utf-8')
            original = source.read_bytes()
            for output, prompt in ((source, None), (root / 'report.json', root / 'report.json')):
                with self.subTest(output=output, prompt=prompt), self.assertRaises(ValueError):
                    run(source, output, prompt)
            self.assertEqual(source.read_bytes(), original)

    def test_unified_cli_writes_specification_and_prompt_without_image_claim(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            source, output, prompt = root / 'case.json', root / 'spec.json', root / 'prompt.txt'
            source.write_text(json.dumps(case(), ensure_ascii=False), encoding='utf-8-sig')
            process = subprocess.run([sys.executable, str(ROOT / 'scripts' / 'detective.py'),
                                      'portrait', '--case', str(source), '--out', str(output),
                                      '--prompt-out', str(prompt)], capture_output=True,
                                     text=True, encoding='utf-8')
            self.assertEqual(process.returncode, 0, process.stderr)
            report = json.loads(output.read_text(encoding='utf-8'))
            self.assertEqual(report['render_state'], 'not_generated')
            self.assertEqual(prompt.read_text(encoding='utf-8'), report['render_request']['prompt'])
            self.assertEqual(set(p.name for p in root.iterdir()), {'case.json', 'spec.json', 'prompt.txt'})


if __name__ == '__main__':
    unittest.main()
