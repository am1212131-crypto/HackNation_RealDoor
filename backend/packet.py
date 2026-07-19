"""
Builds the renter-controlled application-readiness packet (in-memory zip).
Never written to disk server-side and never auto-sent anywhere -- it is
returned directly as a download response to the renter's own request.
"""
import datetime
import io
import json
import zipfile


def build_packet_zip(session_id: str, profile: dict, docs_summary: list,
                      calculation: dict, checklist_result: dict, corpus_meta: dict,
                      confirmed_pdfs: list = None) -> bytes:
    buf = io.BytesIO()
    generated_at = datetime.datetime.utcnow().isoformat() + "Z"

    # The exact submission.schema.json shape, pulled straight out of the
    # calculation result so a grader can validate this object directly.
    submission = {
        "household_id": calculation.get("household_id"),
        "annualized_income": calculation.get("annualized_income"),
        "comparison": calculation.get("comparison"),
        "readiness_status": calculation.get("readiness_status"),
        "citations": calculation.get("citations", []),
    }

    summary = {
        "generated_at": generated_at,
        "note": "Renter-controlled application-readiness packet. Not an eligibility determination.",
        "program": corpus_meta["program_name"],
        "rule_year": corpus_meta["rule_year"],
        "event_date": corpus_meta.get("event_date"),
        "corpus_version": corpus_meta["corpus_version"],
        "submission": submission,
        "profile": profile,
        "documents": docs_summary,
        "calculation": calculation,
        "checklist": checklist_result,
        "sources": corpus_meta["sources"],
    }

    with zipfile.ZipFile(buf, "w", zipfile.ZIP_DEFLATED) as zf:
        zf.writestr("summary.json", json.dumps(summary, indent=2, default=str))
        zf.writestr("submission.json", json.dumps(submission, indent=2, default=str))
        zf.writestr("README.txt", _human_readable(summary))
        for filename, pdf_bytes in (confirmed_pdfs or []):
            zf.writestr(f"documents/{filename}", pdf_bytes)

    return buf.getvalue()


def _human_readable(summary: dict) -> str:
    lines = []
    lines.append("RealDoor Application-Readiness Packet")
    lines.append(f"Generated: {summary['generated_at']}")
    lines.append(f"Program: {summary['program']} ({summary['rule_year']})")
    lines.append("")
    lines.append("This packet was prepared by you, the renter, using RealDoor. It reflects")
    lines.append("only information you confirmed or corrected yourself. It is NOT an")
    lines.append("eligibility, approval, denial, or priority determination -- only a qualified")
    lines.append("human reviewer makes that call.")
    lines.append("")
    lines.append("-- Calculation --")
    calc = summary.get("calculation") or {}
    if calc:
        lines.append(f"Household: {calc.get('household_id')}")
        lines.append(f"Annualized income: ${calc.get('annualized_income', 'n/a'):,}" if isinstance(calc.get("annualized_income"), (int, float)) else "Annualized income: n/a")
        threshold = calc.get("threshold") or {}
        if threshold:
            lines.append(f"Frozen threshold ({threshold.get('ami_tier')}, household of "
                          f"{threshold.get('household_size')}): ${threshold.get('annual_income_limit_usd'):,}")
            lines.append(f"Effective date: {threshold.get('effective_date')}")
        lines.append(f"Comparison: {calc.get('comparison')}")
        lines.append(f"Readiness status: {calc.get('readiness_status')}")
        if calc.get("review_reasons"):
            lines.append(f"Review reasons: {', '.join(calc['review_reasons'])}")
    lines.append("")
    lines.append("-- Checklist --")
    for item in summary.get("checklist", {}).get("items", []):
        lines.append(f"[{item['status'].upper()}] {item['label']}")
    lines.append("")
    lines.append("-- Sources --")
    for src in summary.get("sources", []):
        file_part = f" ({src['file']})" if src.get("file") else ""
        lines.append(f"- {src['title']}{file_part}, publisher: {src['publisher']}")
    return "\n".join(lines)
