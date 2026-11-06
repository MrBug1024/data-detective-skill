"""Behavioral invariants for the text-only blind evaluation; never calls a model."""
import importlib.util
import json
import os
from pathlib import Path
import tempfile
import unittest
from unittest import mock
import urllib.error
import urllib.request

ROOT = Path(__file__).resolve().parent.parent
SPEC = importlib.util.spec_from_file_location("blind_eval", ROOT / "test" / "run_blind_eval.py")
evaluation = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(evaluation)


def source_case_answer():
    return {
        "mode": "criminal",
        "hypotheses": [
            {"id": "H1", "statement": "蓝车参与事件，保安记录有可核验观察依据", "evidence_for": ["E1"], "evidence_against": []},
            {"id": "H2", "statement": "蓝车为正常出入，后续报道复制同一观察", "evidence_for": ["E2", "E3"], "evidence_against": []},
        ],
        "findings": [{"topic": "source_independence", "assessment": "established",
                      "statement": "简报及截图没有新增目击源", "evidence_ids": ["E1", "E2", "E3"], "value": None}],
        "next_actions": [
            {"priority": 1, "type": "source_provenance", "evidence_ids": ["E1", "E2", "E3"],
             "question": "是否存在独立的新增观察", "method": "追索保安电话录音及简报的原始版本",
             "outcomes": [{"hypothesis_id": "H1", "result": "录音可核实车辆细节", "implication": "supports"}]},
            {"priority": 2, "type": "forensic_comparison", "evidence_ids": ["E1", "E4"],
             "question": "能否关联两次看到的车辆", "method": "比较原始影像的车辆外观和通行路径",
             "outcomes": [{"hypothesis_id": "H1", "result": "外观或路径存在冲突", "implication": "weakens"}]},
        ],
        "source_groups": [["E1", "E2", "E3"], ["E4"]],
        "time_alignment": [], "selected_records": [],
        "culpability": {"status": "undetermined", "probability": None},
    }


class BlindEvaluationTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.cases = {case["case_id"]: case for case in evaluation.read_json(ROOT / "test" / "cases" / "public.json")}
        cls.rubrics = evaluation.read_json(ROOT / "test" / "private" / "rubrics.json")

    def test_prompt_builder_does_not_read_private_rubrics(self):
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            (root / "test" / "private").mkdir(parents=True)
            (root / "references").mkdir()
            (root / "SKILL.md").write_text("公开技能规则", encoding="utf-8")
            (root / "test" / "response_contract.md").write_text("公开输出协议", encoding="utf-8")
            (root / "test" / "private" / "rubrics.json").write_text("HIDDEN_ANSWER_SENTINEL", encoding="utf-8")
            (root / "references" / "forensic-science.md").write_text("公开理论", encoding="utf-8")
            original = Path.read_text

            def checked_read(path, *args, **kwargs):
                self.assertNotIn("private", path.parts)
                return original(path, *args, **kwargs)

            with mock.patch.object(Path, "read_text", checked_read):
                messages, metadata = evaluation.build_messages(self.cases["C01"], root=root)
            prompt = json.dumps(messages, ensure_ascii=False)
            self.assertNotIn("HIDDEN_ANSWER_SENTINEL", prompt)
            self.assertIn("公开理论", prompt)
            self.assertEqual(len(messages), 2)
            self.assertIn("references/investigation-methods.md", metadata["missing_references"])

    def test_all_cases_have_unique_evidence_and_separate_rubrics(self):
        self.assertGreaterEqual(len(self.cases), 6)
        self.assertEqual(set(self.cases), set(self.rubrics))
        self.assertEqual({r["mode"] for r in self.rubrics.values()}, {"criminal", "entity", "data"})
        for case in self.cases.values():
            ids = [e["id"] for e in case["evidence"]]
            self.assertEqual(len(ids), len(set(ids)))
            self.assertNotIn("rubric", case)
            self.assertNotIn("answer", case)

    def test_valid_source_dependence_answer_passes_observable_checks(self):
        result = evaluation.evaluate(self.cases["C03"], source_case_answer(), self.rubrics["C03"])
        self.assertTrue(result["contract_pass"], result)
        self.assertTrue(result["semantic_review_required"])

    def test_quantitative_topic_can_have_separate_null_explanations(self):
        answer = source_case_answer()
        answer["findings"] = [
            {"topic": "likelihood_ratio", "assessment": "established", "statement": "报告给出的量值",
             "evidence_ids": ["E1"], "value": 17},
            {"topic": "likelihood_ratio", "assessment": "not_inferable", "statement": "另一层级的结论无法由此推出",
             "evidence_ids": ["E1"], "value": None},
            {"topic": "likelihood_ratio", "assessment": "established", "statement": "测量条件需要按原报告解释",
             "evidence_ids": ["E1"], "value": None},
        ]
        rubric = {"mode": "criminal", "required_findings": {"likelihood_ratio": ["established"]},
                  "finding_values": {"likelihood_ratio": 17}}
        for order in [answer["findings"], list(reversed(answer["findings"]))]:
            answer["findings"] = order
            result = evaluation.evaluate(self.cases["C03"], answer, rubric)
            checks = {d["check"]: d for d in result["diagnostics"]}
            self.assertTrue(checks["numeric_likelihood_ratio"]["passed"])
            self.assertTrue(checks["finding_likelihood_ratio"]["passed"])
            self.assertTrue(checks["finding_topic_likelihood_ratio"]["passed"])
            self.assertTrue(any(n["kind"] == "non_numeric_explanations" for n in result["review_notes"]))
            self.assertEqual(result["grader_version"], "v2")

    def test_correct_quantity_never_masks_another_wrong_quantity(self):
        rubric = {"mode": "criminal", "required_findings": {"likelihood_ratio": ["established"]},
                  "finding_values": {"likelihood_ratio": 17}}
        for wrong in [18, .999, "17", True, {"number": 17}]:
            for values in [[17, wrong], [wrong, 17]]:
                answer = source_case_answer()
                answer["findings"] = [{"topic": "likelihood_ratio", "assessment": "established",
                                       "statement": "所声称的量值", "evidence_ids": ["E1"], "value": v} for v in values]
                result = evaluation.evaluate(self.cases["C03"], answer, rubric)
                check = next(d for d in result["diagnostics"] if d["check"] == "numeric_likelihood_ratio")
                self.assertFalse(check["passed"])
                self.assertEqual(check["category"], "claim_boundary")

    def test_missing_topic_is_protocol_omission_not_an_invented_claim(self):
        rubric = {"mode": "criminal", "required_findings": {"likelihood_ratio": ["established"]},
                  "finding_values": {"likelihood_ratio": 17}}
        result = evaluation.evaluate(self.cases["C03"], source_case_answer(), rubric)
        checks = {d["check"]: d for d in result["diagnostics"]}
        self.assertFalse(checks["finding_topic_likelihood_ratio"]["passed"])
        self.assertEqual(checks["finding_topic_likelihood_ratio"]["category"], "protocol")
        self.assertFalse(checks["numeric_likelihood_ratio"]["passed"])
        self.assertEqual(checks["numeric_likelihood_ratio"]["category"], "protocol")
        self.assertTrue(checks["finding_likelihood_ratio"]["passed"])

    def test_null_explanations_alone_do_not_satisfy_required_measurement(self):
        answer = source_case_answer()
        answer["findings"] = [{"topic": "likelihood_ratio", "assessment": "not_inferable",
                               "statement": "未给出量值", "evidence_ids": ["E1"], "value": None}]
        rubric = {"mode": "criminal", "required_findings": {"likelihood_ratio": ["established"]},
                  "finding_values": {"likelihood_ratio": 17}}
        result = evaluation.evaluate(self.cases["C03"], answer, rubric)
        checks = {d["check"]: d for d in result["diagnostics"]}
        self.assertTrue(checks["finding_topic_likelihood_ratio"]["passed"])
        self.assertFalse(checks["numeric_likelihood_ratio"]["passed"])
        self.assertEqual(checks["numeric_likelihood_ratio"]["category"], "protocol")

    def test_empty_hypothesis_links_are_allowed_but_fake_links_are_not(self):
        answer = source_case_answer()
        answer["hypotheses"][1]["evidence_for"] = []
        answer["hypotheses"][1]["evidence_against"] = []
        result = evaluation.evaluate(self.cases["C03"], answer, self.rubrics["C03"])
        checks = {d["check"]: d for d in result["diagnostics"]}
        self.assertTrue(checks["hypothesis_citations"]["passed"])
        self.assertTrue(checks["citation_integrity"]["passed"])
        self.assertTrue(result["contract_pass"])
        self.assertTrue(any(n.get("hypothesis_ids") == ["H2"] for n in result["review_notes"]))
        answer["hypotheses"][1]["evidence_for"] = ["NONEXISTENT"]
        result = evaluation.evaluate(self.cases["C03"], answer, self.rubrics["C03"])
        checks = {d["check"]: d for d in result["diagnostics"]}
        self.assertFalse(checks["hypothesis_citations"]["passed"])
        self.assertFalse(checks["citation_integrity"]["passed"])

    def test_non_numeric_topic_strength_limits_remain_strict_for_every_claim(self):
        answer = source_case_answer()
        answer["findings"].append({"topic": "source_independence", "assessment": "tentative",
                                   "statement": "另一个强度不符合规范的主张", "evidence_ids": ["E1"], "value": None})
        result = evaluation.evaluate(self.cases["C03"], answer, self.rubrics["C03"])
        check = next(d for d in result["diagnostics"] if d["check"] == "finding_source_independence")
        self.assertFalse(check["passed"])

    def test_copied_accounts_are_not_independent(self):
        answer = source_case_answer()
        answer["source_groups"] = [["E1"], ["E2"], ["E3"], ["E4"]]
        result = evaluation.evaluate(self.cases["C03"], answer, self.rubrics["C03"])
        failed = {d["check"] for d in result["diagnostics"] if not d["passed"]}
        self.assertIn("source_dependence", failed)

    def test_citation_and_guilt_fail_independently(self):
        answer = source_case_answer()
        answer["findings"][0]["evidence_ids"].append("INVENTED_EVIDENCE")
        answer["culpability"] = {"status": "established", "probability": .999}
        result = evaluation.evaluate(self.cases["C03"], answer, self.rubrics["C03"])
        failed = {d["check"] for d in result["diagnostics"] if not d["passed"]}
        self.assertIn("citation_integrity", failed)
        self.assertIn("no_unsupported_guilt", failed)

    def test_time_correction_sign_and_precision(self):
        answer = source_case_answer()
        answer["findings"] = [{"topic": "identity", "assessment": "unresolved", "statement": "未辨认身份",
                               "evidence_ids": ["E1"], "value": None}]
        answer["source_groups"] = [["E1"], ["E2"], ["E3"], ["E4"]]
        answer["time_alignment"] = [{"evidence_id": "E1", "corrected_time": "2026-09-13T21:09:00+08:00", "uncertainty_seconds": 2}]
        result = evaluation.evaluate(self.cases["C02"], answer, self.rubrics["C02"])
        check = next(d for d in result["diagnostics"] if d["check"] == "time_alignment_E1")
        self.assertTrue(check["passed"])
        for wrong in ["2026-09-13T21:25:00+08:00", "2026-09-13T21:09:00", "2026-09-13T21:09:00Z"]:
            answer["time_alignment"][0]["corrected_time"] = wrong
            result = evaluation.evaluate(self.cases["C02"], answer, self.rubrics["C02"])
            self.assertFalse(next(d for d in result["diagnostics"] if d["check"] == "time_alignment_E1")["passed"])

    def test_unsupported_footprint_parameters_are_rejected(self):
        answer = source_case_answer()
        answer["findings"] = [{"topic": "body_features", "assessment": "not_inferable", "statement": "参数不足",
                               "evidence_ids": ["E1"], "value": {"height_cm": 180, "age_years": None, "sex": None, "weight_kg": None}}]
        result = evaluation.evaluate(self.cases["C01"], answer, self.rubrics["C01"])
        self.assertFalse(next(d for d in result["diagnostics"] if d["check"] == "unsupported_body_estimates")["passed"])
        answer["findings"][0]["value"]["height_cm"] = None
        result = evaluation.evaluate(self.cases["C01"], answer, self.rubrics["C01"])
        self.assertTrue(next(d for d in result["diagnostics"] if d["check"] == "unsupported_body_estimates")["passed"])

    def test_same_name_records_are_not_merged(self):
        answer = source_case_answer()
        answer["mode"] = "entity"
        answer["selected_records"] = [{"record_id": "P-32", "evidence_ids": ["E1", "E3"]},
                                      {"record_id": "P-31", "evidence_ids": ["E2"]}]
        result = evaluation.evaluate(self.cases["C07"], answer, self.rubrics["C07"])
        self.assertFalse(next(d for d in result["diagnostics"] if d["check"] == "identity_separation")["passed"])
        self.assertTrue(next(d for d in result["diagnostics"] if d["check"] == "selected_record_P-32")["passed"])

    def test_no_network_without_explicit_live(self):
        with mock.patch.object(evaluation.urllib.request, "build_opener", side_effect=AssertionError("network")):
            with mock.patch("builtins.print"):
                self.assertEqual(evaluation.main([]), 0)

    def test_retry_consumes_total_request_budget(self):
        config = {"api_key": "SECRET_DO_NOT_PRINT", "base_url": "https://example.invalid/v1", "model": "mock"}
        opener = mock.Mock()
        opener.open.side_effect = urllib.error.URLError("SECRET_DO_NOT_PRINT")
        budget = evaluation.RequestBudget(1)
        with mock.patch.object(evaluation.urllib.request, "build_opener", return_value=opener):
            with mock.patch.object(evaluation.time, "sleep"):
                with self.assertRaisesRegex(RuntimeError, "预算"):
                    evaluation.call_model(config, [], max_tokens=128, timeout=1, retries=1, budget=budget)
        self.assertEqual(opener.open.call_count, 1)
        self.assertEqual(budget.used, 1)

    def test_http_failure_redacts_provider_error_and_credentials(self):
        config = {"api_key": "SECRET_DO_NOT_PRINT", "base_url": "https://example.invalid/v1", "model": "mock"}
        opener = mock.Mock()
        opener.open.side_effect = urllib.error.HTTPError("https://example.invalid/SECRET_DO_NOT_PRINT", 401,
                                                       "SECRET_DO_NOT_PRINT", {}, None)
        with mock.patch.object(evaluation.urllib.request, "build_opener", return_value=opener):
            with self.assertRaises(RuntimeError) as error:
                evaluation.call_model(config, [], max_tokens=128, timeout=1, retries=0, budget=evaluation.RequestBudget(1))
        self.assertIn("401", str(error.exception))
        self.assertNotIn("SECRET", str(error.exception))

    def test_redirect_does_not_forward_authorization(self):
        handler = evaluation.NoRedirect()
        request = urllib.request.Request("https://example.invalid", headers={"Authorization": "Bearer SECRET"})
        self.assertIsNone(handler.redirect_request(request, None, 302, "", {}, "https://other.invalid"))

    def test_request_omits_unsupported_optional_parameters_and_caps_tokens(self):
        config = {"api_key": "SECRET", "base_url": "https://example.invalid/v1", "model": "mock"}
        response = mock.MagicMock()
        response.__enter__.return_value.read.return_value = json.dumps({
            "choices": [{"message": {"content": json.dumps(source_case_answer())}, "finish_reason": "stop"}],
            "usage": {"prompt_tokens": 500, "completion_tokens": 200, "total_tokens": 700, "arbitrary": "omitted"},
        }).encode("utf-8")
        opener = mock.Mock()
        opener.open.return_value = response
        with mock.patch.object(evaluation.urllib.request, "build_opener", return_value=opener):
            answer, metrics = evaluation.call_model(config, [], max_tokens=5000, timeout=5, retries=0,
                                                    budget=evaluation.RequestBudget(1), token_parameter="max_completion_tokens")
        payload = json.loads(opener.open.call_args.args[0].data)
        self.assertEqual(payload["max_completion_tokens"], 5000)
        for name in ("max_tokens", "temperature", "response_format", "tools", "reasoning_effort"):
            self.assertNotIn(name, payload)
        self.assertEqual(answer["mode"], "criminal")
        self.assertEqual(metrics["usage"]["total_tokens"], 700)
        self.assertNotIn("arbitrary", metrics["usage"])

    def test_http_400_explains_compatible_token_parameter_without_echo(self):
        config = {"api_key": "SECRET", "base_url": "https://example.invalid/v1", "model": "mock"}
        opener = mock.Mock()
        opener.open.side_effect = urllib.error.HTTPError("https://example.invalid/SECRET", 400, "SECRET", {}, None)
        with mock.patch.object(evaluation.urllib.request, "build_opener", return_value=opener):
            with self.assertRaises(RuntimeError) as error:
                evaluation.call_model(config, [], max_tokens=5000, timeout=5, retries=2, budget=evaluation.RequestBudget(3))
        self.assertIn("max_completion_tokens", str(error.exception))
        self.assertNotIn("SECRET", str(error.exception))
        self.assertEqual(opener.open.call_count, 1)

    def test_length_and_invalid_json_preserve_model_answer_and_usage(self):
        config = {"api_key": "SECRET", "base_url": "https://example.invalid/v1", "model": "mock"}
        for finish_reason, content in [("length", '{"mode":'), ("length", json.dumps(source_case_answer())),
                                       ("stop", "not-a-json-answer")]:
            response = mock.MagicMock()
            response.__enter__.return_value.read.return_value = json.dumps({
                "choices": [{"message": {"content": content}, "finish_reason": finish_reason}],
                "usage": {"prompt_tokens": 500, "completion_tokens": 200, "total_tokens": 700},
                "model": "mock-version",
            }).encode("utf-8")
            opener = mock.Mock()
            opener.open.return_value = response
            with mock.patch.object(evaluation.urllib.request, "build_opener", return_value=opener):
                with self.assertRaises(evaluation.ModelCallError) as error:
                    evaluation.call_model(config, [], max_tokens=5000, timeout=90, retries=0,
                                          budget=evaluation.RequestBudget(1))
            self.assertEqual(error.exception.raw_answer, content)
            self.assertEqual(error.exception.metrics["finish_reason"], finish_reason)
            self.assertEqual(error.exception.metrics["usage"]["total_tokens"], 700)
            self.assertFalse(error.exception.metrics["usage_unavailable"])
            if finish_reason == "length":
                self.assertIn("finish_reason=length", str(error.exception))

    def test_reasoning_only_truncation_preserves_usage_without_reasoning_text(self):
        config = {"api_key": "SECRET", "base_url": "https://example.invalid/v1", "model": "mock"}
        private_reasoning = "PRIVATE_REASONING_MUST_NOT_BE_STORED"
        for final_content in [None, "missing-field"]:
            message = {"role": "assistant", "reasoning_content": private_reasoning}
            if final_content is None:
                message["content"] = None
            response = mock.MagicMock()
            response.__enter__.return_value.read.return_value = json.dumps({
                "choices": [{"message": message, "finish_reason": "length"}], "model": "mock-version",
                "usage": {"prompt_tokens": 7000, "completion_tokens": 5000, "total_tokens": 12000,
                          "completion_tokens_details": {"reasoning_tokens": 4999}},
            }).encode("utf-8")
            opener = mock.Mock()
            opener.open.return_value = response
            with mock.patch.object(evaluation.urllib.request, "build_opener", return_value=opener):
                with self.assertRaises(evaluation.ModelCallError) as error:
                    evaluation.call_model(config, [], max_tokens=8000, timeout=300, retries=0,
                                          budget=evaluation.RequestBudget(1), reasoning_effort="low")
            self.assertIsNone(error.exception.raw_answer)
            metrics = error.exception.metrics
            self.assertEqual(metrics["finish_reason"], "length")
            self.assertEqual(metrics["usage"]["reasoning_tokens"], 4999)
            self.assertEqual(metrics["usage"]["total_tokens"], 12000)
            self.assertEqual(metrics["returned_model"], "mock-version")
            self.assertEqual(metrics["reasoning_characters"], len(private_reasoning))
            self.assertNotIn(private_reasoning, json.dumps(metrics))
            self.assertNotIn(private_reasoning, str(error.exception))
            payload = json.loads(opener.open.call_args.args[0].data)
            self.assertEqual(payload["reasoning_effort"], "low")

    def test_checkpoint_retains_finished_answers_during_interrupted_next_case(self):
        with tempfile.TemporaryDirectory() as temp:
            env_file = Path(temp) / ".env"
            env_file.write_text("API_KEY=SECRET\nBASE_URL=https://example.invalid/v1\nMODEL=mock\n", encoding="utf-8")
            out = Path(temp) / "report.json"
            calls = []

            def simulated_request(config, messages, **kwargs):
                kwargs["budget"].consume()
                calls.append(True)
                if len(calls) == 2:
                    raise KeyboardInterrupt()
                return source_case_answer(), {"usage": {"total_tokens": 700}, "attempts": 1}

            with mock.patch.dict(os.environ, {}, clear=True):
                with mock.patch.object(evaluation, "call_model", side_effect=simulated_request):
                    with mock.patch("builtins.print"):
                        with self.assertRaises(KeyboardInterrupt):
                            evaluation.main(["--live", "--env-file", str(env_file), "--case", "C03", "--case", "C06",
                                             "--timeout", "300", "--out", str(out)])
            checkpoint = evaluation.read_json(out)
            self.assertEqual(checkpoint["cases_evaluated"], 1)
            self.assertEqual(checkpoint["results"][0]["case_id"], "C03")
            self.assertEqual(checkpoint["pending_case"]["case_id"], "C06")
            self.assertEqual(checkpoint["requests_used"], 2)
            self.assertEqual(checkpoint["requests_with_unknown_usage"], 1)
            self.assertEqual(checkpoint["reported_usage"]["total_tokens"], 700)
            self.assertEqual(checkpoint["run_status"], "running")
            self.assertEqual(checkpoint["request_timeout_seconds"], 300)

    def test_atomic_checkpoint_keeps_previous_report_if_replace_fails(self):
        with tempfile.TemporaryDirectory() as temp:
            path = Path(temp) / "report.json"
            evaluation.write_checkpoint(path, {"previous": True})
            with mock.patch.object(evaluation.os, "replace", side_effect=OSError("simulated failure")):
                with self.assertRaises(OSError):
                    evaluation.write_checkpoint(path, {"previous": False})
            self.assertEqual(evaluation.read_json(path), {"previous": True})
            self.assertEqual([p.name for p in path.parent.iterdir()], ["report.json"])

    def test_env_parser_has_no_interpolation_or_execution(self):
        with tempfile.TemporaryDirectory() as temp:
            path = Path(temp) / ".env"
            path.write_text("# test\nAPI_KEY='literal$(not_a_command)'\nBASE_URL=https://example.invalid/v1\nMODEL=mock\n", encoding="utf-8")
            values = evaluation.load_env(path)
            self.assertEqual(values["API_KEY"], "literal$(not_a_command)")

    def test_model_override_is_process_local_and_recorded(self):
        with tempfile.TemporaryDirectory() as temp:
            env_file = Path(temp) / ".env"
            original = "API_KEY=SECRET\nBASE_URL=https://example.invalid/v1\nMODEL=unavailable-model\n"
            env_file.write_text(original, encoding="utf-8")
            report_file = Path(temp) / "report.json"
            metrics = {"latency_seconds": .01, "attempts": 1, "usage": {}, "returned_model": "available-model-version"}
            with mock.patch.dict(os.environ, {}, clear=True):
                with mock.patch.object(evaluation, "call_model", return_value=(source_case_answer(), metrics)) as call:
                    with mock.patch("builtins.print"):
                        result = evaluation.main(["--live", "--env-file", str(env_file), "--model", "available-model",
                                                  "--case", "C03", "--max-requests", "1", "--out", str(report_file)])
                self.assertNotIn("MODEL", os.environ)
                self.assertNotIn("OPENAI_MODEL", os.environ)
            self.assertEqual(result, 0)
            self.assertEqual(call.call_args.args[0]["model"], "available-model")
            self.assertEqual(env_file.read_text(encoding="utf-8"), original)
            report = evaluation.read_json(report_file)
            self.assertEqual(report["requested_model"], "available-model")
            self.assertEqual(report["results"][0]["returned_model"], "available-model-version")

    def test_invalid_json_and_nonfinite_values_are_rejected(self):
        for text in ["[]", '{"p": NaN}', '{"p": Infinity}', "not json"]:
            with self.assertRaises(ValueError):
                evaluation.parse_answer(text)

    def test_malformed_field_types_become_diagnostics(self):
        for field, value in [("type", []), ("evidence_ids", "E1"), ("outcomes", None)]:
            answer = source_case_answer()
            answer["next_actions"][0][field] = value
            result = evaluation.evaluate(self.cases["C03"], answer, self.rubrics["C03"])
            self.assertFalse(result["contract_pass"])
            self.assertFalse(next(d for d in result["diagnostics"] if d["check"] == "item_types")["passed"])


if __name__ == "__main__":
    unittest.main()
