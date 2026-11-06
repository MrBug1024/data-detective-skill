"""Explicit LR arithmetic, never an estimator of evidential strength.

Inputs must come from externally validated, applicable models. Duplicate dependency
groups are rejected rather than multiplying correlated evidence. Conditional
independence must be justified by the caller, not inferred by this module.
"""
import math


def _probability(value):
    if isinstance(value, bool) or not isinstance(value, (int, float)) or not math.isfinite(value) or not 0 < value <= 1:
        raise ValueError("条件概率必须有限且处于 (0,1]；零频数须由验证模型处理，不能当无限证据")


def likelihood_ratio(p_e_h1, p_e_h2):
    _probability(p_e_h1)
    _probability(p_e_h2)
    ratio = p_e_h1 / p_e_h2
    if not math.isfinite(ratio):
        raise ValueError("LR超出浮点表示范围，使用combine_likelihoods返回log_lr")
    return ratio


def combine_likelihoods(items, *, conditional_independence_justification, prior_h1=None):
    """Two exhaustive, exclusive hypotheses only if a posterior is requested."""
    if not items or not conditional_independence_justification.strip():
        raise ValueError("需要模型输入及条件独立性依据；无法确立时分别报告 LR")
    groups, evidence_ids, log_lr = set(), set(), 0.0
    for item in items:
        group = item.get("dependency_group")
        eid = item.get("evidence_id")
        if not group or group in groups or not eid or eid in evidence_ids:
            raise ValueError("缺失或重复的来源组/证据ID，禁止重复计权")
        if not item.get("model_source", "").strip():
            raise ValueError("缺少可核查的条件概率模型来源")
        groups.add(group)
        evidence_ids.add(eid)
        # Log-domain arithmetic avoids underflow of long products.
        _probability(item["p_e_h1"])
        _probability(item["p_e_h2"])
        log_lr += math.log(item["p_e_h1"]) - math.log(item["p_e_h2"])
    result = {"log_lr": log_lr, "evidence_ids": sorted(evidence_ids),
              "conditional_independence_justification": conditional_independence_justification,
              "posterior_h1": None}
    if prior_h1 is not None:
        if (isinstance(prior_h1, bool) or not isinstance(prior_h1, (int, float))
                or not math.isfinite(prior_h1) or not 0 < prior_h1 < 1):
            raise ValueError("先验必须处于 (0,1)，仅适用于互斥且穷尽的两个命题")
        log_odds = math.log(prior_h1) - math.log1p(-prior_h1) + log_lr
        posterior = (1 / (1 + math.exp(-log_odds)) if log_odds >= 0
                     else math.exp(log_odds) / (1 + math.exp(log_odds)))
        result.update(prior_h1=prior_h1, posterior_h1=posterior)
    return result
