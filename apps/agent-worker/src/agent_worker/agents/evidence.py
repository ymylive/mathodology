"""Deterministic evidence-mining over Writer artifacts.

Two scans run between Writer.run_for() and Critic.review():
- `mine_sensitivity_evidence`: count parameters that appear with ±N% perturbation
  numbers; F/O-prize papers consistently report ≥3.
- `scan_anonymity_violations`: regex for common Chinese university / region /
  team names; instant disqualifier under MCM rules.

Output is converted into additional Critic criteria so the model is judged
against deterministic facts about its own body text, not just its self-report.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from mm_contracts import PaperDraft


# --- sensitivity evidence -------------------------------------------------

# Captures perturbation phrasing: "±10%", "+/- 20%", "5% increase", "±0.1"
# Used to find sentences that quantify a parameter sensitivity test.
_PERTURB_RE = re.compile(
    r"(?:[±]|\+\/?\-|\bplus\s+or\s+minus\s+|增减|变化|波动|扰动)\s*\d+(?:\.\d+)?\s*%?",
    re.IGNORECASE,
)

# Captures explicit pct change of an objective ("the objective fell by 4.3%").
_PCT_DELTA_RE = re.compile(
    r"\b\d+(?:\.\d+)?\s*%\b",
)

# Variants of "sensitivity analysis" headings in both languages.
_SENS_HEADING_RE = re.compile(
    r"\b(sensitivity\s+analys\w+|sensitivity\s+study|敏感性分析|灵敏度分析)\b",
    re.IGNORECASE,
)

# Markers for typical sensitivity figure ids (Coder catalog).
_SENS_FIGURE_TOKENS = (
    "tornado",
    "heatmap_sensitivity",
    "monte_carlo",
    "monte-carlo",
    "敏感性",
    "灵敏度",
)


@dataclass
class SensitivityFindings:
    """What deterministic scanning learned about the paper's sensitivity work."""

    has_sensitivity_section: bool
    perturb_mention_count: int
    parameter_count_estimate: int
    tornado_or_mc_referenced: bool
    sample_evidence: list[str] = field(default_factory=list)

    def passes_award_bar(self) -> bool:
        return (
            self.has_sensitivity_section
            and self.parameter_count_estimate >= 3
            and self.perturb_mention_count >= 3
        )


def mine_sensitivity_evidence(paper: PaperDraft) -> SensitivityFindings:
    """Quick scan: does the body show ≥3 distinct parameters perturbed with quantitative deltas?"""
    has_sens_section = False
    perturb_count = 0
    figure_refs = False
    samples: list[str] = []

    # Parameter-count proxy: catalog short variable tokens that appear near
    # a perturbation phrase. Conservative — over-count is fine, under-count
    # would let weak papers through.
    parameter_tokens: set[str] = set()

    # Patterns that explicitly bind a parameter to a perturbation phrase.
    # Each pattern's group(1) is the parameter token.
    _GREEK = "αβγδεζηθικλμνξορστυφχψω"
    _PARAM_PATTERNS = [
        # "perturbed α by ±10%" / "varying β over ±20%" / "increased X by 10%"
        re.compile(
            rf"(?:perturb(?:ed|ing)?|vary(?:ing)?|increas(?:e[ds]?|ing)|decreas(?:e[ds]?|ing)|chang(?:e[ds]?|ing))"
            rf"\s+([{_GREEK}]|[A-Za-z](?:_?[A-Za-z0-9]{{0,8}})?)"
            rf"\s+(?:by|of|over|at|with)?\s*{_PERTURB_RE.pattern}",
            re.IGNORECASE,
        ),
        # "α by ±10%" / "γ at ±20%" / "X_init by 5%"
        re.compile(
            rf"\b([{_GREEK}]|[A-Za-z]_[A-Za-z0-9]{{1,8}}|[A-Za-z]{{1,3}})\s+(?:by|at|over)\s*{_PERTURB_RE.pattern}",
            re.IGNORECASE,
        ),
        # "For α at ±10%" / "对 α 增减 10%"
        re.compile(
            rf"(?:For|对|参数)\s+([{_GREEK}]|[A-Za-z](?:_?[A-Za-z0-9]{{0,8}})?)"
            rf"[^.。!?\n]{{0,30}}{_PERTURB_RE.pattern}",
            re.IGNORECASE,
        ),
    ]

    _PARAM_BLACKLIST = {
        "the", "a", "an", "this", "that", "we", "for", "of", "by", "at",
        "in", "on", "to", "is", "are", "was", "were", "be", "been",
        "and", "or", "with", "from", "as", "it", "its", "all", "each",
    }

    for sec in paper.sections:
        title = sec.title or ""
        body = sec.body_markdown or ""
        if _SENS_HEADING_RE.search(title) or _SENS_HEADING_RE.search(body):
            has_sens_section = True
        perturb_hits = _PERTURB_RE.findall(body)
        perturb_count += len(perturb_hits)
        for pat in _PARAM_PATTERNS:
            for m in pat.finditer(body):
                param = m.group(1).strip(" $\\")
                if not param or param.lower() in _PARAM_BLACKLIST:
                    continue
                if len(param) > 30:
                    continue
                parameter_tokens.add(param)
                if len(samples) < 6:
                    samples.append(m.group(0).strip()[:200])
        if any(tok in body.lower() for tok in _SENS_FIGURE_TOKENS):
            figure_refs = True
        for fig_id_match in re.finditer(r"\[\[FIG:([a-z0-9_]+)\]\]", body):
            if any(tok in fig_id_match.group(1) for tok in _SENS_FIGURE_TOKENS):
                figure_refs = True

    return SensitivityFindings(
        has_sensitivity_section=has_sens_section,
        perturb_mention_count=perturb_count,
        parameter_count_estimate=len(parameter_tokens),
        tornado_or_mc_referenced=figure_refs,
        sample_evidence=samples,
    )


def sensitivity_criteria(findings: SensitivityFindings) -> list[str]:
    """Convert findings into appendable Critic criteria strings."""
    crits: list[str] = []
    if not findings.has_sensitivity_section:
        crits.append(
            "BLOCKING: No 'Sensitivity Analysis' / 敏感性分析 section detected in any "
            "section title or body. F-prize / O-prize papers must include this section."
        )
    if findings.parameter_count_estimate < 3:
        crits.append(
            f"BLOCKING: Detected only ~{findings.parameter_count_estimate} parameter(s) "
            "with quantitative perturbation tests. The Modeler's plan requires ≥3 — "
            "expand the Sensitivity section with at least 3 distinct parameters at ±10% and ±20%."
        )
    if findings.perturb_mention_count < 3:
        crits.append(
            f"Found only {findings.perturb_mention_count} ±N% perturbation mentions in the "
            "body. Award-level papers describe each parameter's effect with a concrete % change."
        )
    if not findings.tornado_or_mc_referenced:
        crits.append(
            "No tornado_sensitivity / heatmap_sensitivity / Monte Carlo figure referenced. "
            "The Modeler's validation plan asks for these — embed them with [[FIG:<id>]] and "
            "discuss the result in prose."
        )
    return crits


# --- anonymity scanner ----------------------------------------------------

# Universities frequently leaked in CUMCM / MCM violations. Conservative list
# focused on ones likely to appear verbatim in student-written context.
_UNIVERSITY_PATTERNS = [
    r"Jilin\s+University",
    r"Tsinghua\s+University",
    r"Peking\s+University",
    r"Fudan\s+University",
    r"Shanghai\s+Jiao\s*tong\s+University",
    r"Zhejiang\s+University",
    r"Beihang\s+University",
    r"University\s+of\s+Science\s+and\s+Technology\s+of\s+China",
    r"Nanjing\s+University",
    r"Xi[' ]?an\s+Jiao\s*tong\s+University",
    r"吉林大学",
    r"清华大学",
    r"北京大学",
    r"复旦大学",
    r"上海交通大学",
    r"浙江大学",
    r"北京航空航天大学",
    r"中国科学技术大学",
    r"南京大学",
    r"西安交通大学",
    r"华中科技大学",
    r"哈尔滨工业大学",
    r"中山大学",
    r"武汉大学",
    r"同济大学",
    r"东南大学",
    r"国防科技大学",
]

# Chinese province + tier-1 city names that hint at team origin.
_REGION_PATTERNS = [
    r"\b(?:Beijing|Shanghai|Shenzhen|Guangzhou|Chengdu|Wuhan|Nanjing|Xi[' ]?an|Hangzhou|Tianjin|Chongqing)\b",
    r"(?:北京|上海|深圳|广州|成都|武汉|南京|西安|杭州|天津|重庆|吉林省|辽宁省|山东省|江苏省|浙江省|广东省)",
]

# Common author-style intros that imply a real name follows.
_AUTHOR_PATTERNS = [
    r"\b(?:I am|My name is|We are students at|We the undersigned)\b",
    r"^\s*作者[:：]\s*\S+",
    r"^\s*指导教师[:：]\s*\S+",
]

_ALL_ANON_RES = [re.compile(p, re.IGNORECASE | re.MULTILINE) for p in (
    *_UNIVERSITY_PATTERNS,
    *_REGION_PATTERNS,
    *_AUTHOR_PATTERNS,
)]


@dataclass
class AnonymityFindings:
    violations: list[tuple[str, str]] = field(default_factory=list)
    # (section_title, matched_snippet)

    @property
    def has_violations(self) -> bool:
        return bool(self.violations)


def scan_anonymity_violations(paper: PaperDraft) -> AnonymityFindings:
    """Regex sweep for school / region / author identity leaks.

    Returns the first ~5 hits with context so Critic can be unambiguous about
    what to remove. Conservative — we'd rather flag for human review than
    miss a real DQ-causing leak.
    """
    finds: list[tuple[str, str]] = []
    fields_to_scan: list[tuple[str, str]] = [
        ("title", paper.title or ""),
        ("abstract", paper.abstract or ""),
    ]
    for sec in paper.sections:
        fields_to_scan.append((sec.title or "(section)", sec.body_markdown or ""))
    # Also scan references — schools leak there too.
    if getattr(paper, "references", None):
        joined_refs = "\n".join(paper.references)
        fields_to_scan.append(("references", joined_refs))

    for label, text in fields_to_scan:
        if not text:
            continue
        for pattern in _ALL_ANON_RES:
            m = pattern.search(text)
            if m:
                snippet = text[max(0, m.start() - 30) : m.end() + 30].replace("\n", " ")
                finds.append((label, snippet.strip()))
                if len(finds) >= 5:
                    return AnonymityFindings(violations=finds)
    return AnonymityFindings(violations=finds)


def anonymity_criteria(findings: AnonymityFindings) -> list[str]:
    if not findings.has_violations:
        return []
    bullets = "\n".join(
        f"  - in {label}: ...{snip}..." for label, snip in findings.violations[:5]
    )
    return [
        "BLOCKING (DISQUALIFICATION RISK): identity leak detected — under MCM/CUMCM rules, "
        "any school, region, author, or advisor name causes instant disqualification. "
        f"Remove the following matches:\n{bullets}"
    ]


__all__ = [
    "AnonymityFindings",
    "SensitivityFindings",
    "anonymity_criteria",
    "mine_sensitivity_evidence",
    "scan_anonymity_violations",
    "sensitivity_criteria",
]
