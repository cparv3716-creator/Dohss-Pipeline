"""Hard-rule eligibility evaluation using verified candidate facts only."""
from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Mapping

from candidate.profile import CandidateProfile


ELIGIBLE = "ELIGIBLE"
INELIGIBLE = "INELIGIBLE"
UNKNOWN_REVIEW = "UNKNOWN_REVIEW"


@dataclass(frozen=True)
class EligibilityResult:
    status: str
    reasons: list[dict[str, Any]]

    def __post_init__(self) -> None:
        if self.status not in (ELIGIBLE, INELIGIBLE, UNKNOWN_REVIEW):
            raise ValueError(f"invalid eligibility status: {self.status}")


def _items(value: Any) -> list[Any]:
    if value is None:
        return []
    return value if isinstance(value, list) else [value]


def _names(value: Any) -> set[str]:
    names: set[str] = set()
    for item in _items(value):
        if isinstance(item, Mapping):
            name = item.get("name") or item.get("skill") or item.get("degree") or item.get("value")
        else:
            name = item
        if name not in (None, ""):
            text = str(name).strip().lower()
            parts = [part.strip() for part in text.replace(";", ",").split(",") if part.strip()]
            names.update(parts or [text])
    return names


def _reason(code: str, outcome: str, message: str, **details: Any) -> dict[str, Any]:
    return {"code": code, "outcome": outcome, "message": message, "details": details}


def _job_country(job: Mapping[str, Any]) -> str:
    explicit = str(job.get("country") or "").strip().lower()
    if explicit:
        return explicit
    location = str(job.get("location") or "").lower()
    aliases = (
        ("india", ("india", "delhi", "mumbai", "bengaluru", "bangalore", "chennai", "hyderabad", "pune", "kolkata")),
        ("united states", ("united states", "usa", "u.s.", "new york", "california", "texas")),
        ("united kingdom", ("united kingdom", "uk", "london", "england", "scotland")),
        ("canada", ("canada", "toronto", "vancouver")),
        ("australia", ("australia", "sydney", "melbourne")),
        ("singapore", ("singapore",)),
    )
    return next((country for country, names in aliases if any(name in location for name in names)), "")


def _authorization_for_country(value: Any, country: str) -> Any:
    if isinstance(value, Mapping):
        normalized = {str(key).strip().lower(): item for key, item in value.items()}
        return normalized.get(country)
    for item in _items(value):
        if isinstance(item, Mapping):
            item_country = str(item.get("country") or "").strip().lower()
            if item_country == country:
                return item.get("authorized", item.get("value"))
        elif str(item).strip().lower() == country:
            return True
    return None


def evaluate_eligibility(profile: CandidateProfile, job: Mapping[str, Any]) -> EligibilityResult:
    reasons: list[dict[str, Any]] = []
    failures = 0
    unknowns = 0

    def fail(code: str, message: str, **details: Any) -> None:
        nonlocal failures
        failures += 1
        reasons.append(_reason(code, INELIGIBLE, message, **details))

    def unknown(code: str, message: str, **details: Any) -> None:
        nonlocal unknowns
        unknowns += 1
        reasons.append(_reason(code, UNKNOWN_REVIEW, message, **details))

    def passed(code: str, message: str, **details: Any) -> None:
        reasons.append(_reason(code, ELIGIBLE, message, **details))

    required_years = job.get("required_experience_min")
    if required_years not in (None, ""):
        candidate_years = profile.verified_value("years_of_experience")
        if candidate_years is None:
            unknown("EXPERIENCE_UNKNOWN", "Candidate experience is not verified", required=float(required_years))
        else:
            try:
                if float(candidate_years) < float(required_years):
                    fail("EXPERIENCE_TOO_LOW", f"Requires {float(required_years):g}+ years experience", required=float(required_years), candidate=float(candidate_years))
                else:
                    passed("EXPERIENCE_MET", "Verified experience meets the minimum", required=float(required_years), candidate=float(candidate_years))
            except (TypeError, ValueError):
                unknown("EXPERIENCE_INVALID", "Experience could not be compared")

    required_skills = _names(job.get("required_skills"))
    if required_skills:
        if not profile.fact("skills").verified and not profile.fact("programming_languages").verified and not profile.fact("software_tools").verified:
            unknown("SKILLS_UNKNOWN", "Candidate skill inventory is not verified", required=sorted(required_skills))
        else:
            candidate_skills = set().union(
                _names(profile.verified_value("skills", [])),
                _names(profile.verified_value("programming_languages", [])),
                _names(profile.verified_value("software_tools", [])),
            )
            missing = sorted(required_skills - candidate_skills)
            if missing:
                # A verified resume-derived inventory proves listed skills, but
                # is not an explicit declaration that every omitted skill is
                # absent. Keep omissions unknown unless negative evidence is
                # represented in the profile.
                unknown("MANDATORY_SKILLS_UNVERIFIED", "Mandatory skills are not present in verified candidate evidence", missing=missing)
            else:
                passed("MANDATORY_SKILLS_MET", "All parsed mandatory skills are verified", matched=sorted(required_skills))

    education = str(job.get("education_requirement") or "").lower()
    if education:
        degrees = _names(profile.verified_value("degrees"))
        education_value = str(profile.verified_value("education", "")).lower()
        known = profile.fact("degrees").verified or profile.fact("education").verified
        degree_tokens = {
            "bachelor": ("bachelor", "b.a", "b.s", "bsc", "btech", "b.tech"),
            "master": ("master", "m.a", "m.s", "msc", "mtech", "m.tech", "mba"),
            "phd": ("phd", "ph.d", "doctorate"),
        }
        required_level = next((level for level, tokens in degree_tokens.items() if any(token in education for token in tokens)), "")
        combined = " ".join(degrees) + " " + education_value
        if not known:
            unknown("EDUCATION_UNKNOWN", "Candidate education is not verified", requirement=job.get("education_requirement"))
        elif required_level and not any(token in combined for token in degree_tokens[required_level]):
            fail("DEGREE_REQUIREMENT_NOT_MET", "Verified education does not show the required degree", requirement=job.get("education_requirement"))
        else:
            passed("EDUCATION_MET", "Verified education is compatible with the parsed requirement")

    employment = str(job.get("employment_type") or "").lower()
    allowed_employment = _names(profile.verified_value("employment_type"))
    if employment and employment != "unknown" and profile.fact("employment_type").verified:
        unrestricted = bool(allowed_employment & {"any", "all", "both", "no preference"})
        if allowed_employment and not unrestricted and employment not in allowed_employment:
            fail("EMPLOYMENT_TYPE_EXCLUDED", "Job employment type is outside candidate preferences", job_type=employment)
        else:
            passed("EMPLOYMENT_TYPE_MET", "Employment type matches verified preferences")

    remote_type = str(job.get("remote_type") or "").lower()
    remote_pref = str(profile.verified_value("remote_preference", "")).lower()
    if remote_pref in ("remote only", "remote-only", "remote_only") and remote_type not in ("remote", "unknown", ""):
        fail("REMOTE_PREFERENCE_CONFLICT", "Candidate is verified as remote-only", job_arrangement=remote_type)
    elif remote_type not in ("", "unknown") and not profile.fact("remote_preference").verified:
        unknown("WORK_ARRANGEMENT_UNKNOWN", "Candidate work-arrangement preference is not verified", job_arrangement=remote_type)

    job_location = str(job.get("location") or "").strip().lower()
    preferred_locations = _names(profile.verified_value("preferred_locations"))
    relocation = profile.verified_value("relocation_preferences")
    if job_location and remote_type != "remote" and preferred_locations:
        location_match = any(place in job_location or job_location in place for place in preferred_locations)
        relocation_allowed = relocation is True or str(relocation).lower() in ("yes", "true", "willing")
        if not location_match and profile.fact("relocation_preferences").verified and not relocation_allowed:
            fail("LOCATION_CONSTRAINT", "Job location is outside verified preferences and relocation is not allowed", location=job.get("location"))
        elif not location_match and not profile.fact("relocation_preferences").verified:
            unknown("RELOCATION_UNKNOWN", "Relocation preference is unknown", location=job.get("location"))

    description = str(job.get("full_description") or job.get("description") or "").lower()
    authorization_required = bool(job.get("work_authorization_required")) or any(
        phrase in description
        for phrase in ("must be authorized to work", "legally authorized to work", "right to work")
    )
    if authorization_required:
        country = _job_country(job)
        authorization = profile.verified_value("work_authorization")
        if authorization is None or not profile.fact("work_authorization").verified:
            unknown("WORK_AUTHORIZATION_UNKNOWN", "Required work authorization is not verified", country=country or "unknown")
        elif not country:
            unknown("JOB_COUNTRY_UNKNOWN", "Work authorization is required but the job country could not be determined")
        else:
            authorized = _authorization_for_country(authorization, country)
            if authorized is True or str(authorized).lower() in ("yes", "true", "authorized"):
                passed("WORK_AUTHORIZATION_MET", "Verified work authorization matches the job country", country=country)
            elif authorized is False or str(authorized).lower() in ("no", "false", "not authorized"):
                fail("WORK_AUTHORIZATION_MISSING", "Candidate is not authorized to work in the job country", country=country)
            else:
                unknown("WORK_AUTHORIZATION_COUNTRY_UNKNOWN", "Authorization for the job country is not verified", country=country)
    if any(phrase in description for phrase in ("no visa sponsorship", "unable to sponsor", "will not sponsor")):
        sponsorship = profile.verified_value("visa_sponsorship_requirements")
        if sponsorship is None:
            unknown("SPONSORSHIP_NEED_UNKNOWN", "Job does not sponsor visas but candidate requirement is unknown")
        elif sponsorship is True or str(sponsorship).lower() in ("yes", "required", "true"):
            fail("VISA_SPONSORSHIP_UNAVAILABLE", "Candidate requires sponsorship but the job states it is unavailable")
        else:
            passed("VISA_SPONSORSHIP_MET", "Verified candidate facts do not require sponsorship")

    preferences = profile.verified_value("application_preferences", {})
    if isinstance(preferences, Mapping):
        excluded_companies = _names(preferences.get("excluded_companies"))
        excluded_keywords = _names(preferences.get("excluded_keywords"))
        company = str(job.get("company") or "").lower()
        haystack = f"{job.get('title', '')} {description}".lower()
        if company in excluded_companies:
            fail("COMPANY_EXCLUDED", "Company is on the candidate's verified exclusion list", company=job.get("company"))
        matched_keywords = sorted(keyword for keyword in excluded_keywords if keyword in haystack)
        if matched_keywords:
            fail("KEYWORD_EXCLUDED", "Job matches candidate-defined exclusions", keywords=matched_keywords)

    if failures:
        return EligibilityResult(INELIGIBLE, reasons)
    if unknowns:
        return EligibilityResult(UNKNOWN_REVIEW, reasons)
    return EligibilityResult(ELIGIBLE, reasons or [_reason("NO_HARD_CONFLICTS", ELIGIBLE, "No hard eligibility conflicts found")])
