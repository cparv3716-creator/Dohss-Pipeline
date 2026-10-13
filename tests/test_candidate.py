import tempfile
import unittest
from pathlib import Path

import yaml

from candidate import AnswerBank, CandidateProfile, ProfileValidationError
from tests.helpers import answer, unknown, verified, write_answers, write_profile


class CandidateProfileTests(unittest.TestCase):
    def test_example_profile_supports_resume_sections(self):
        profile = CandidateProfile.load(Path("candidate/profile.example.yaml"))
        for name in ("achievements", "competitions", "positions_of_responsibility", "coursework"):
            self.assertTrue(profile.fact(name).unknown)

    def test_unknown_personal_fact_stays_unknown(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = write_profile(Path(tmp) / "profile.yaml")
            profile = CandidateProfile.load(path)
            self.assertIsNone(profile.verified_value("email"))
            self.assertTrue(profile.fact("email").unknown)
            self.assertNotIn("email", profile.verified_facts())

    def test_unwrapped_values_are_rejected(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "profile.yaml"
            path.write_text(yaml.safe_dump({"version": 1, "profile": {"full_name": "Invented Name"}}), encoding="utf-8")
            with self.assertRaises(ProfileValidationError):
                CandidateProfile.load(path)

    def test_verified_and_unknown_cannot_both_be_true(self):
        with tempfile.TemporaryDirectory() as tmp:
            bad = {"value": "person@example.test", "verified": True, "unknown": True, "source": "test"}
            path = write_profile(Path(tmp) / "profile.yaml", email=bad)
            with self.assertRaises(ProfileValidationError):
                CandidateProfile.load(path)

    def test_answer_bank_requires_verified_and_allowed_for_auto_answer(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = write_answers(
                Path(tmp) / "answers.yaml",
                {"EMAIL": answer("person@example.test", allowed=False)},
            )
            bank = AnswerBank.load(path)
            entry = bank.lookup("EMAIL")
            self.assertIsNotNone(entry)
            self.assertFalse(entry.usable)

    def test_verified_profile_fallback_is_retrieved_but_not_auto_allowed(self):
        with tempfile.TemporaryDirectory() as tmp:
            profile = CandidateProfile.load(write_profile(Path(tmp) / "profile.yaml", email=verified("person@example.test")))
            entry = AnswerBank.empty().lookup("EMAIL", profile=profile)
            self.assertEqual(entry.value, "person@example.test")
            self.assertFalse(entry.auto_answer_allowed)


if __name__ == "__main__":
    unittest.main()
