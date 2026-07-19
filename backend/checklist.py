"""
Two separate, deliberately non-overlapping evaluations:

1. evaluate_checklist() -- document COMPLETENESS. "What's missing or stale
   against what this household says it needs to document?" Informational;
   shown to the renter so they know what to add, but does not by itself
   flip readiness_status (matches the official pack's behavior: HH-003 and
   HH-006 are missing an employment_letter yet are still READY_TO_REVIEW).

2. evaluate_readiness() -- EVIDENCE QUALITY. Computes the annualized income
   from confirmed documents and flags specific, named problems
   (PAY_STUB_TOTAL_CONFLICT, GIG_INCOME_UNCORROBORATED,
   *_EXPIRED) that make the evidence itself untrustworthy for a human
   reviewer. Only these named reasons drive readiness_status to
   NEEDS_REVIEW; everything else is READY_TO_REVIEW. Never an eligibility
   determination -- see rules_engine.compare_income_to_limit for that
   boundary.
"""
import datetime

from . import rules_engine

EVENT_DATE = datetime.date.fromisoformat(rules_engine.RULES["event_date"])
CURRENCY_WINDOW_DAYS = rules_engine.RULES["document_currency_window_days"]

# doc_type -> the checklist item id it satisfies
DOC_TYPE_TO_ITEM = {
    "application_summary": "application_summary",
    "pay_stub": "pay_stub",
    "employment_letter": "employment_letter",
    "benefit_letter": "benefit_letter",
    "gig_statement": "gig_statement",
    # deliberately no doc_type maps to "gig_income_corroboration" -- this
    # program doesn't model a corroborating document type, so any household
    # reporting gig income will always show it missing (matches the
    # official pack's GIG_INCOME_UNCORROBORATED expectation).
}

# item_id -> (label, date_field_id, applies_to flag key or "always")
CHECKLIST_ITEMS = [
    {"item_id": "application_summary", "label": "Application summary", "date_field": "application_date", "applies_to": "always"},
    {"item_id": "pay_stub", "label": "Pay stub", "date_field": "pay_date", "applies_to": "has_wage_income"},
    {"item_id": "employment_letter", "label": "Employment letter", "date_field": "document_date", "applies_to": "has_wage_income"},
    {"item_id": "benefit_letter", "label": "Benefit letter", "date_field": "document_date", "applies_to": "has_benefit_income"},
    {"item_id": "gig_statement", "label": "Gig income statement", "date_field": "statement_month", "applies_to": "has_gig_income"},
    {"item_id": "gig_income_corroboration", "label": "Corroborating gig-income documentation (e.g. bank deposit records)", "date_field": None, "applies_to": "has_gig_income"},
]


def _parse_date(doc_type: str, field_id: str, value: str):
    if not value:
        return None
    value = value.strip()
    try:
        if field_id == "statement_month":  # "YYYY-MM"
            return datetime.date.fromisoformat(value + "-01")
        return datetime.date.fromisoformat(value)
    except ValueError:
        return None


def _to_float(value):
    if value is None:
        return None
    try:
        return float(str(value).replace("$", "").replace(",", "").strip())
    except ValueError:
        return None


def evaluate_checklist(docs: list, household_flags: dict, today: datetime.date = None):
    """docs: [{doc_type, fields: {field_id: value}}] for CONFIRMED docs only."""
    today = today or EVENT_DATE
    docs_by_item = {}
    for doc in docs:
        item_id = DOC_TYPE_TO_ITEM.get(doc["doc_type"])
        if item_id:
            docs_by_item.setdefault(item_id, []).append(doc)

    results = []
    for item in CHECKLIST_ITEMS:
        applies_to = item["applies_to"]
        applicable = True if applies_to == "always" else bool(household_flags.get(applies_to))
        if not applicable:
            results.append({"item_id": item["item_id"], "label": item["label"], "status": "not_applicable"})
            continue

        item_docs = docs_by_item.get(item["item_id"], [])
        if not item_docs:
            results.append({"item_id": item["item_id"], "label": item["label"], "status": "missing"})
            continue

        newest = None
        if item["date_field"]:
            for d in item_docs:
                dt = _parse_date(d["doc_type"], item["date_field"], d["fields"].get(item["date_field"]))
                if dt and (newest is None or dt > newest):
                    newest = dt
            if newest and (today - newest).days > CURRENCY_WINDOW_DAYS:
                results.append({
                    "item_id": item["item_id"], "label": item["label"], "status": "expired",
                    "most_recent_date": newest.isoformat(), "age_days": (today - newest).days,
                })
                continue

        results.append({
            "item_id": item["item_id"], "label": item["label"], "status": "satisfied",
            "most_recent_date": newest.isoformat() if newest else None,
        })

    return {"evaluated_on": today.isoformat(), "currency_window_days": CURRENCY_WINDOW_DAYS, "items": results}


def evaluate_readiness(docs: list, today: datetime.date = None):
    """
    docs: [{doc_id, doc_type, fields: {field_id: value}}] for CONFIRMED docs only.

    Returns {annualized_income_usd, contributions, review_reasons, readiness_status}.
    Uses the annualization convention from rules_engine.RULES["annualization"]:
    regular_hours * hourly_rate * periods_per_year for wages (reported
    gross_pay is only used to cross-check for PAY_STUB_TOTAL_CONFLICT, never
    as the calculation basis), monthly_benefit * periods_per_year for
    benefits, gross_receipts * 12 for gig income (always flagged
    GIG_INCOME_UNCORROBORATED in this program, which models no corroborating
    document type).
    """
    today = today or EVENT_DATE
    periods = rules_engine.PERIODS_PER_YEAR
    review_reasons = set()
    contributions = []

    pay_stubs = [d for d in docs if d["doc_type"] == "pay_stub"]
    benefit_letters = [d for d in docs if d["doc_type"] == "benefit_letter"]
    gig_statements = [d for d in docs if d["doc_type"] == "gig_statement"]
    employment_letters = [d for d in docs if d["doc_type"] == "employment_letter"]

    if pay_stubs:
        def _pay_date(d):
            return d["fields"].get("pay_date") or ""
        primary = max(pay_stubs, key=_pay_date)
        hours = _to_float(primary["fields"].get("regular_hours"))
        rate = _to_float(primary["fields"].get("hourly_rate"))
        freq = (primary["fields"].get("pay_frequency") or "").strip().lower()
        if hours is not None and rate is not None and freq in periods:
            per_period = round(hours * rate, 2)
            annualized = round(per_period * periods[freq], 2)
            contributions.append({
                "doc_id": primary.get("doc_id"), "doc_type": "pay_stub",
                "formula": f"{hours:g} hrs x ${rate:,.2f}/hr x {periods[freq]} pay periods/year ({freq})",
                "annualized_usd": annualized,
            })

        for d in pay_stubs:
            h = _to_float(d["fields"].get("regular_hours"))
            r = _to_float(d["fields"].get("hourly_rate"))
            g = _to_float(d["fields"].get("gross_pay"))
            if h is not None and r is not None and g is not None and abs(round(h * r, 2) - g) > 0.01:
                review_reasons.add("PAY_STUB_TOTAL_CONFLICT")

    for d in benefit_letters:
        amt = _to_float(d["fields"].get("monthly_benefit"))
        freq = (d["fields"].get("benefit_frequency") or "").strip().lower()
        if amt is not None and freq in periods:
            contributions.append({
                "doc_id": d.get("doc_id"), "doc_type": "benefit_letter",
                "formula": f"${amt:,.2f} x {periods[freq]} ({freq})",
                "annualized_usd": round(amt * periods[freq], 2),
            })

    for d in gig_statements:
        gross = _to_float(d["fields"].get("gross_receipts"))
        if gross is not None:
            contributions.append({
                "doc_id": d.get("doc_id"), "doc_type": "gig_statement",
                "formula": f"${gross:,.2f} x 12 (one statement month annualized)",
                "annualized_usd": round(gross * 12, 2),
            })
            review_reasons.add("GIG_INCOME_UNCORROBORATED")

    def _currency_check(group, date_field, expired_reason, unverified_reason):
        """A doc TYPE (not each individual duplicate) is stale/unverified.
        Households often have more than one copy of the same document type
        (e.g. a rasterized pay stub alongside a clean re-scan); as long as
        ANY copy has a readable, current date, the type is fine. Only flag
        if every copy is unreadable (never confirmed with a date) or every
        readable copy is stale."""
        if not group:
            return
        dates = []
        any_readable = False
        for d in group:
            raw = d["fields"].get(date_field)
            if raw:
                any_readable = True
                dt = _parse_date("_", date_field, raw)
                if dt:
                    dates.append(dt)
        if not any_readable:
            review_reasons.add(unverified_reason)
        elif dates and (today - max(dates)).days > CURRENCY_WINDOW_DAYS:
            review_reasons.add(expired_reason)

    _currency_check(employment_letters, "document_date", "EMPLOYMENT_LETTER_EXPIRED", "EMPLOYMENT_LETTER_DATE_UNVERIFIED")
    _currency_check(pay_stubs, "pay_date", "PAY_STUB_EXPIRED", "PAY_STUB_DATE_UNVERIFIED")
    _currency_check(benefit_letters, "document_date", "BENEFIT_LETTER_EXPIRED", "BENEFIT_LETTER_DATE_UNVERIFIED")

    total = round(sum(c["annualized_usd"] for c in contributions), 2)
    readiness_status = "NEEDS_REVIEW" if review_reasons else "READY_TO_REVIEW"

    return {
        "annualized_income_usd": total,
        "contributions": contributions,
        "review_reasons": sorted(review_reasons),
        "readiness_status": readiness_status,
        "evaluated_on": today.isoformat(),
    }
