"""Policy gates for contact outreach actions."""


def classify_contact_for_outreach(contact):
    source_type = contact.get("source_type") or ""
    confidence = int(contact.get("confidence_score") or 0)
    evidence = (contact.get("evidence_text") or "").lower()
    do_not_contact = "do not contact" in evidence or "do-not-contact" in evidence
    guessed = source_type == "guessed_role_email"
    official_public = source_type in {
        "official_public_page",
        "mailto_public_page",
        "search_result_snippet",
        "brave_search",
        "serpapi",
        "google_alert_rss",
        "official_hiring_email",
    }

    if confidence >= 90 and official_public and not guessed and not do_not_contact:
        return "auto_send_allowed"
    if 70 <= confidence <= 89:
        return "draft_only"
    return "manual_review"
