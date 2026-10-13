from pathlib import Path

import yaml

from candidate.profile import PROFILE_FIELDS


def unknown():
    return {"value": None, "verified": False, "unknown": True, "source": ""}


def verified(value, source="test"):
    return {"value": value, "verified": True, "unknown": False, "source": source}


def write_profile(path: Path, **facts):
    profile = {name: unknown() for name in PROFILE_FIELDS}
    profile.update(facts)
    path.write_text(yaml.safe_dump({"version": 1, "profile": profile}, sort_keys=False), encoding="utf-8")
    return path


def write_answers(path: Path, answers):
    path.write_text(yaml.safe_dump({"version": 1, "answers": answers}, sort_keys=False), encoding="utf-8")
    return path


def answer(value, verified=True, allowed=True, sensitivity="private"):
    return {
        "value": value, "verified": verified, "auto_answer_allowed": allowed,
        "sensitivity": sensitivity, "source": "test", "confidence": 100 if verified else 0,
    }
