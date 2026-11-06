# 盲测输出协议

这是统一的机器可读答卷格式，适用于所有案件；字段不适用时用空数组。它不提供案件答案。只依据给定证据，应用 skill，输出一个 JSON 对象，不要 Markdown 代码围栏。不要访问外部资料、工具或猜测隐藏的结局。所有案件为合成材料，不存在必须猜中的嫌疑人。

```json
{
  "mode": "criminal | data | entity",
  "hypotheses": [
    {"id": "H1", "statement": "可检验命题", "evidence_for": ["E1"], "evidence_against": []}
  ],
  "findings": [
    {"topic": "body_features | identity | motive | case_linkage | likelihood_ratio | source_independence | negative_evidence | record_linkage | other", "assessment": "established | tentative | unresolved | not_inferable", "statement": "结论及其边界", "evidence_ids": ["E1"], "value": null}
  ],
  "next_actions": [
    {"priority": 1, "type": "preserve_original | scene_documentation | forensic_comparison | clock_calibration | source_provenance | transfer_test | reference_sampling | record_reconciliation | identity_verification | route_check | independent_review", "evidence_ids": ["E1"], "question": "该行动要区分什么", "method": "对哪个对象做什么具体操作", "outcomes": [{"hypothesis_id": "H1", "result": "可观察结果", "implication": "supports | weakens | uninformative"}]}
  ],
  "source_groups": [["E1"]],
  "time_alignment": [
    {"evidence_id": "E1", "corrected_time": "YYYY-MM-DDTHH:MM:SS+HH:MM", "uncertainty_seconds": 0}
  ],
  "selected_records": [
    {"record_id": "原始证据中出现的记录ID", "evidence_ids": ["E1"]}
  ],
  "culpability": {"status": "undetermined | excluded | established", "probability": null}
}
```

- `source_groups` 表示具有共同上游来源的证据分组，不是赞成同一假说的证据分组。列出所有来源，每个证据 ID 至多出现一次；单个来源也列一个组。
- `findings.value` 只记录有依据的机器可读量：似然比用数值；身体特征用对象 `height_cm / age_years / sex / weight_kg`，不能估计的值为 `null`；其他结论可用 `null`。`assessment` 表示对当前命题能作何种判断，不能把证据存在等同于有罪。
- 时间校正用证据中的时区；保留测量不确定度，未提供精度时不要虚构一个小误差。只在已提供校准信息时输出校正时间。
- `selected_records` 用于数据记录溯源或明确实体身份的候选选择；证据不足时可为空。记录 ID 必须与证据材料一致。
- 调查行动按优先级排序，至多 4 项。应具备操作对象、方法及可观察结果。至少两项行动需要说明结果如何支持或削弱某个已列出的假说；不要只写“继续调查”。没有犯罪嫌疑推断任务时，`culpability` 保持 `undetermined`。
- 假说至少覆盖两个能解释现有材料的竞争解释；不要为凑数发明现场事实。
