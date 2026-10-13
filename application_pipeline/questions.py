"""Application-question classification and verified answer retrieval."""
from __future__ import annotations

import re
from dataclasses import dataclass
from typing import Optional

from candidate.answer_bank import AnswerBank, AnswerEntry
from candidate.profile import CandidateProfile


@dataclass(frozen=True)
class ClassifiedQuestion:
    question_type: str
    confidence: int
    skill: str = ""
    alternatives: tuple[str, ...] = ()
    free_text: bool = False


PATTERNS = (
    ("FIRST_NAME", r"\b(first|given) name\b"),
    ("LAST_NAME", r"\b(last|family|surname) name\b"),
    ("FULL_NAME", r"\b(full|legal|your) name\b"),
    ("PREFERRED_NAME", r"\bpreferred name\b|\bname you go by\b"),
    ("EMAIL", r"\be-?mail\b"),
    ("PHONE", r"\b(phone|mobile|telephone)\b"),
    ("CURRENT_CITY", r"\b(current (city|location)|where do you (live|reside))\b"),
    ("COUNTRY", r"\b(country|country of residence)\b"),
    ("STATE_PROVINCE", r"\b(state|province|region)\b"),
    ("POSTAL_CODE", r"\b(postal|zip) code\b"),
    ("LINKEDIN", r"\blinked\s*in\b"),
    ("GITHUB", r"\bgithub\b"),
    ("PORTFOLIO", r"\b(portfolio|personal website)\b"),
    ("CGPA", r"\b(cgpa|gpa|grade point)\b"),
    ("GRADUATION_YEAR", r"\b(graduat(e|ion) (year|date)|year of passing)\b"),
    ("UNIVERSITY", r"\b(university|college|institution)\b"),
    ("DEGREE", r"\b(degree|field of study|major)\b"),
    ("HIGHEST_EDUCATION", r"\b(highest|maximum) (education|qualification)\b"),
    ("CURRENT_COMPANY", r"\b(current|present) (company|employer)\b"),
    ("CURRENT_TITLE", r"\b(current|present) (title|job title|position)\b"),
    ("CURRENT_CTC", r"\b(current (ctc|salary|compensation))\b"),
    ("EXPECTED_CTC", r"\b(expected|desired) (ctc|salary|compensation)\b"),
    ("WORK_AUTHORIZATION", r"\b(authori[sz]ed to work|work authori[sz]ation|legally work)\b"),
    ("VISA_SPONSORSHIP", r"\b(visa|sponsor(ship)?)\b"),
    ("RELOCATION", r"\b(relocat(e|ion)|move to)\b"),
    ("NOTICE_PERIOD", r"\b(notice period|how soon can you join)\b"),
    ("AVAILABILITY", r"\b(available|availability|start date|when can you (start|join))\b"),
    ("WHY_COMPANY", r"\bwhy (this|our|the) company\b|\bwhy .*company\b"),
    ("WHY_ROLE", r"\bwhy (this|the) role\b|\bwhy .*position\b"),
    ("PROJECT_EXAMPLE", r"\b(project|built|developed)\b.*\b(example|describe|tell)\b|\bdescribe .*project\b"),
    ("LEADERSHIP_EXAMPLE", r"\b(leadership|led a team|managed a team)\b"),
    ("WHY_ROLE", r"\bwhy should we hire you\b|\bwhat makes you (a )?(good|strong) fit\b"),
    ("PRONOUNS", r"\bpronouns?\b"),
    ("DEMOGRAPHIC_SELF_ID", r"\b(gender|race|ethnicity|veteran|disability|self[- ]identif)\b"),
    ("CONSENT", r"\b(consent|privacy notice|terms and conditions|electronic signature|certif(?:y|ication))\b"),
)
SKILL_EXPERIENCE = re.compile(
    r"(?:how many\s+)?years?(?: of)?(?: experience)?(?: have you)?(?: worked|working|using|with|in)?\s+(?:with\s+)?([A-Za-z][A-Za-z0-9+#. -]{0,40})",
    re.I,
)


def _skill_from_experience_question(text: str) -> str:
    patterns = (
        r"how many\s+years?(?:\s+of)?\s+(.+?)\s+experience\b",
        r"years?(?:\s+of)?\s+experience\s+(?:do you have\s+)?(?:with|using|in)\s+(.+?)(?:[?.,]|$)",
        r"worked\s+with\s+(.+?)(?:[?.,]|$)",
    )
    for pattern in patterns:
        match = re.search(pattern, text, re.I)
        if match:
            return match.group(1).strip(" ?.,")
    match = SKILL_EXPERIENCE.search(text)
    if not match:
        return ""
    return re.split(r"[?.,]|\bfor\b|\bexperience\b", match.group(1), maxsplit=1, flags=re.I)[0].strip()


def classify_question(question: str) -> ClassifiedQuestion:
    text = " ".join(str(question or "").split())
    lower = text.lower()
    if "year" in lower and any(word in lower for word in ("experience", "worked", "working", "using")):
        skill = _skill_from_experience_question(text)
        if not skill or skill.lower() in ("experience", "work experience"):
            return ClassifiedQuestion("YEARS_OF_EXPERIENCE", 85)
        return ClassifiedQuestion("SKILL_EXPERIENCE", 92, skill=skill)
    for question_type, pattern in PATTERNS:
        if re.search(pattern, lower, flags=re.I):
            alternatives = ("AVAILABILITY",) if question_type == "NOTICE_PERIOD" and "join" in lower else ()
            return ClassifiedQuestion(
                question_type, 90, alternatives=alternatives,
                free_text=question_type in ("WHY_COMPANY", "WHY_ROLE", "PROJECT_EXAMPLE", "LEADERSHIP_EXAMPLE"),
            )
    return ClassifiedQuestion("OTHER", 20, free_text=True)


def answer_question(question: str, bank: AnswerBank, profile: CandidateProfile) -> tuple[ClassifiedQuestion, Optional[AnswerEntry]]:
    classified = classify_question(question)
    answer = bank.lookup(classified.question_type, skill=classified.skill, profile=profile)
    if answer is None:
        for alternative in classified.alternatives:
            answer = bank.lookup(alternative, profile=profile)
            if answer:
                break
    return classified, answer


class FreeTextAnswerProvider:
    """Interface for future grounded free-text generation; no default generator."""

    def generate(self, question: str, verified_candidate: dict, job_description: str) -> str:
        raise NotImplementedError("No free-text provider is configured; manual review is required")
