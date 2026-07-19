"""
Versioned rule corpus + deterministic math for the Hack-Nation RealDoor
challenge (LIHTC / Section 42), frozen to the official organizer starter
pack: Boston-Cambridge-Quincy, MA-NH HMFA, FY2026 MTSP limits, scored at the
60% AMI tier, event date 2026-07-18.

Every answer returned by this module carries: confirmed value used,
threshold, formula, source, and effective date. When the requested household
size is not published, or a required confirmed input is missing, the engine
ABSTAINS (comparison="no_frozen_threshold") rather than guessing or
interpolating. This module never labels a renter "eligible" or "not
eligible" -- see abstention_rules in the corpus.
"""
import json
import os
import re

_HERE = os.path.dirname(os.path.abspath(__file__))
_RULES_PATH = os.path.join(_HERE, "..", "data", "rules_boston_lihtc_2026.json")

with open(_RULES_PATH, "r", encoding="utf-8") as f:
    RULES = json.load(f)

_VALID_TIERS = list(RULES["income_limits_annual"]["tiers"].keys())
_SRC_BY_ID = {s["id"]: s for s in RULES["sources"]}
PERIODS_PER_YEAR = RULES["annualization"]["periods_per_year"]
SCORED_TIER = RULES["scored_ami_tier"]


class Abstain(Exception):
    def __init__(self, reason):
        self.reason = reason
        super().__init__(reason)


def annualize(amount: float, frequency: str) -> float:
    frequency = (frequency or "").strip().lower()
    if frequency not in PERIODS_PER_YEAR:
        raise ValueError(f"Unsupported frequency: {frequency!r}")
    if amount is None or amount < 0:
        raise ValueError("Amount must be non-negative")
    return round(float(amount) * PERIODS_PER_YEAR[frequency], 2)


def get_income_limit(household_size: int, ami_tier: str = None):
    ami_tier = str(ami_tier or SCORED_TIER)
    if ami_tier not in _VALID_TIERS:
        raise Abstain(
            f"The {ami_tier}% AMI tier is not published in this frozen corpus. "
            f"Published tiers: {', '.join(_VALID_TIERS)}%."
        )
    if not household_size or not (1 <= household_size <= 8):
        raise Abstain(
            "The published table covers household sizes 1-8. This household size "
            "is outside that range; a human reviewer must confirm the applicable limit."
        )
    limit = RULES["income_limits_annual"]["tiers"][ami_tier][household_size - 1]
    src = _SRC_BY_ID[RULES["income_limits_annual"]["source_id"]]
    return {
        "household_size": household_size,
        "ami_tier": f"{ami_tier}%",
        "annual_income_limit_usd": limit,
        "source": {"title": src["title"], "file": src["file"], "publisher": src["publisher"], "url": src.get("url")},
        "effective_date": RULES["effective_date"],
        "median_family_income_usd": RULES["median_family_income"],
    }


def compare_to_threshold(annual_income: float, threshold: float) -> str:
    """Matches starter/src/calculate.py::compare_to_threshold exactly."""
    if annual_income < 0 or threshold < 0:
        raise ValueError("Values must be non-negative")
    return "below_or_equal" if annual_income <= threshold else "above"


def compare_income_to_limit(confirmed_annual_income: float, household_size: int, ami_tier: str = None):
    """Deterministic comparison against the frozen 60% threshold (or whichever
    tier is requested). Never returns an eligibility determination -- only the
    submission-schema comparison enum (below_or_equal / above /
    no_frozen_threshold) plus the full threshold detail for citation."""
    try:
        limit_info = get_income_limit(household_size, ami_tier)
    except Abstain:
        return {
            "confirmed_annual_income_usd": round(confirmed_annual_income, 2) if confirmed_annual_income is not None else None,
            "comparison": "no_frozen_threshold",
            "threshold": None,
            "disclaimer": (
                "No frozen threshold applies to this household size/tier combination. "
                "This is not an eligibility determination either way."
            ),
        }

    limit = limit_info["annual_income_limit_usd"]
    comparison = compare_to_threshold(confirmed_annual_income, limit)
    return {
        "confirmed_annual_income_usd": round(confirmed_annual_income, 2),
        "threshold": limit_info,
        "formula": "annualized_income <= frozen_threshold",
        "comparison": comparison,
        "gap_usd": round(limit - confirmed_annual_income, 2),
        "disclaimer": (
            "This is a factual comparison of the confirmed annualized income to the frozen "
            f"{limit_info['ami_tier']} threshold for a household of {household_size}, effective "
            f"{limit_info['effective_date']}. It is not an eligibility, approval, denial, or "
            "priority determination -- a qualified human makes that call."
        ),
    }


_DECISION_REQUEST_PATTERNS = [
    r"\bam i eligible\b",
    r"\bwill i qualify\b",
    r"\bdo i qualify\b",
    r"\bcan you approve\b",
    r"\bdecide for me\b",
    r"\bshould i be approved\b",
    r"\bwhat('| i)?s my score\b",
    r"\brank me\b",
    r"\bapproved? or den(y|ied)\b",
    r"\bmark (this|the) applicant (as )?(eligible|approved|denied)\b",
]

_CROSS_APPLICANT_PATTERNS = [
    r"\banother household\b",
    r"\bsomeone else'?s (income|documents?|data)\b",
    r"\bother applicant'?s\b",
    r"\bhh-0\d\d\b.*\bhh-0\d\d\b",  # mentions two different household ids
]

_VACANCY_PATTERNS = [
    r"\bunit available\b",
    r"\bavailable today\b",
    r"\bvacan(t|cy)\b",
    r"\bopen waitlist\b",
    r"\bcurrent rent\b",
]


def is_decision_request(question: str) -> bool:
    q = question.lower()
    return any(re.search(p, q) for p in _DECISION_REQUEST_PATTERNS)


def is_cross_applicant_request(question: str, requested_household_id: str = None, session_household_id: str = None) -> bool:
    q = question.lower()
    if any(re.search(p, q) for p in _CROSS_APPLICANT_PATTERNS):
        return True
    if requested_household_id and session_household_id and requested_household_id != session_household_id:
        return True
    return False


def is_vacancy_request(question: str) -> bool:
    q = question.lower()
    return any(re.search(p, q) for p in _VACANCY_PATTERNS)


def answer_rule_question(question: str, household_size: int = None, ami_tier: str = None):
    """Deterministic, retrieval-only Q&A over the frozen numeric corpus. No
    free-form generation: every answer is built from structured lookups so it
    is always citable and never hallucinated. Refuses (deflects) decisioning
    and cross-applicant requests; states the dataset limitation for vacancy
    questions."""
    if is_decision_request(question):
        return {
            "type": "refusal",
            "message": (
                "RealDoor doesn't decide eligibility, approve, deny, score, prioritize, or rank "
                "applicants. What I can do is show you the published rule, your confirmed income, "
                "and the deterministic comparison -- then a qualified human makes the actual "
                "determination."
            ),
        }

    if is_cross_applicant_request(question):
        return {
            "type": "refusal",
            "message": (
                "I can only work with the household you're currently signed in as. I won't look up "
                "or reveal another household's income, documents, or readiness status."
            ),
        }

    if is_vacancy_request(question):
        return {
            "type": "abstain",
            "message": (
                "The HUD LIHTC property dataset used here lists project locations only -- it is not "
                "a current vacancy, open-waitlist, or rent feed, so I can't tell you what's available "
                "today. Contact the property directly for current availability."
            ),
        }

    q = question.lower()

    size_match = re.search(r"household of (\d+)|family of (\d+)|(\d+)[- ]person", q)
    if not household_size and size_match:
        household_size = int(next(g for g in size_match.groups() if g))

    tier_match = re.search(r"(\d{2,3})\s*%", q)
    if not ami_tier and tier_match:
        ami_tier = tier_match.group(1)

    if not household_size and not ami_tier:
        return {"type": "route_to_rag"}

    ami_tier = ami_tier or SCORED_TIER

    if not household_size:
        return {
            "type": "abstain",
            "message": "I need a household size (1-8) to look up the frozen threshold. Please confirm it.",
        }

    try:
        result = get_income_limit(household_size, ami_tier)
        return {
            "type": "answer",
            "message": (
                f"For a household of {household_size} at {ami_tier}% AMI in the Boston-Cambridge-Quincy "
                f"HMFA, the {RULES['rule_year']} frozen threshold is ${result['annual_income_limit_usd']:,}, "
                f"effective {result['effective_date']}."
            ),
            "citation": result["source"],
            "data": result,
        }
    except Abstain as e:
        return {"type": "abstain", "message": str(e)}


def corpus_meta():
    return {
        "corpus_id": RULES["corpus_id"],
        "corpus_version": RULES["corpus_version"],
        "program_name": RULES["program_name"],
        "metro_area": RULES["metro_area"],
        "rule_year": RULES["rule_year"],
        "event_date": RULES["event_date"],
        "effective_date": RULES["effective_date"],
        "scored_ami_tier": SCORED_TIER,
        "document_currency_window_days": RULES["document_currency_window_days"],
        "sources": RULES["sources"],
        "valid_ami_tiers": _VALID_TIERS,
    }
