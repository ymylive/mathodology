"""Tests for sensitivity evidence mining + anonymity scanning."""

from __future__ import annotations

from agent_worker.agents.evidence import (
    anonymity_criteria,
    mine_sensitivity_evidence,
    scan_anonymity_violations,
    sensitivity_criteria,
)
from mm_contracts import PaperDraft, PaperSection


def _draft(sections: list[tuple[str, str]], abstract: str = "abs", title: str = "T") -> PaperDraft:
    return PaperDraft(
        title=title,
        abstract=abstract,
        sections=[PaperSection(title=t, body_markdown=b) for t, b in sections],
        references=["[1] Smith 2020"],
        figure_refs=[],
    )


# --- sensitivity ----------------------------------------------------------


def test_sensitivity_passes_bar_when_three_params_perturbed() -> None:
    body = (
        "Sensitivity Analysis. We perturbed α by ±10% and observed a 4.3% change in objective.\n"
        "We perturbed β by ±20% (objective shifted 7.1%). For γ at ±10%, the result was 1.2%.\n"
        "See [[FIG:tornado_sensitivity]] for the tornado plot and Monte Carlo N=2000."
    )
    p = _draft([("Sensitivity Analysis", body)])
    findings = mine_sensitivity_evidence(p)
    assert findings.has_sensitivity_section
    assert findings.parameter_count_estimate >= 3
    assert findings.perturb_mention_count >= 3
    assert findings.tornado_or_mc_referenced
    assert findings.passes_award_bar()
    assert sensitivity_criteria(findings) == []


def test_sensitivity_misses_when_no_section() -> None:
    p = _draft([("Conclusion", "We did not analyze sensitivity.")])
    findings = mine_sensitivity_evidence(p)
    assert not findings.has_sensitivity_section
    crits = sensitivity_criteria(findings)
    assert any("BLOCKING" in c for c in crits)
    assert any("Sensitivity Analysis" in c for c in crits)


def test_sensitivity_misses_when_fewer_than_3_params() -> None:
    body = "Sensitivity Analysis. We perturbed α by ±10% and saw 2% change. That was it."
    p = _draft([("Sensitivity Analysis", body)])
    findings = mine_sensitivity_evidence(p)
    assert findings.has_sensitivity_section
    assert findings.parameter_count_estimate < 3
    crits = sensitivity_criteria(findings)
    assert any("only ~1 parameter" in c or "only ~2 parameter" in c or "≥3" in c for c in crits)


def test_sensitivity_finds_cn_heading() -> None:
    body = "我们对参数 α 增减 10% 进行扰动，目标变化 4.3%。对 β 增减 20%，变化 7.1%。对 γ 增减 10%，变化 1.5%。"
    p = _draft([("敏感性分析", body)])
    findings = mine_sensitivity_evidence(p)
    assert findings.has_sensitivity_section


# --- anonymity ------------------------------------------------------------


def test_anonymity_flags_chinese_university_name() -> None:
    p = _draft(
        [("Intro", "Our team from 吉林大学 has worked on this problem.")],
        abstract="Clean abstract.",
    )
    findings = scan_anonymity_violations(p)
    assert findings.has_violations
    assert any("吉林大学" in snip for _, snip in findings.violations)
    crits = anonymity_criteria(findings)
    assert any("DISQUALIFICATION" in c for c in crits)


def test_anonymity_flags_english_university_name_in_abstract() -> None:
    p = _draft([("body", "model.")], abstract="From Jilin University we studied...")
    findings = scan_anonymity_violations(p)
    assert findings.has_violations


def test_anonymity_flags_advisor_in_references() -> None:
    p = PaperDraft(
        title="T",
        abstract="abs",
        sections=[PaperSection(title="x", body_markdown="y")],
        references=["指导教师：张教授. 数学建模指南. 2021."],
        figure_refs=[],
    )
    findings = scan_anonymity_violations(p)
    assert findings.has_violations


def test_anonymity_clean_paper_passes() -> None:
    p = _draft(
        [("Body", "Team # 12345 modeled X with parameter α. No school mentioned.")],
        abstract="Clean: predicted error 3.2%, fuel saved 17%.",
    )
    findings = scan_anonymity_violations(p)
    assert not findings.has_violations
    assert anonymity_criteria(findings) == []


def test_anonymity_short_circuits_at_5_violations() -> None:
    body = "吉林大学 清华大学 北京大学 复旦大学 上海交通大学 浙江大学 (extra)"
    p = _draft([("Intro", body)])
    findings = scan_anonymity_violations(p)
    assert len(findings.violations) <= 5
