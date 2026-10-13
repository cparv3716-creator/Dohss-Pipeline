import tempfile
import unittest
from pathlib import Path
from application_pipeline.cli import prepare_applications
from application_pipeline.enrichment import EnrichmentResult
from database import db
from tests.helpers import answer, verified, write_answers, write_profile


class CliPipelineTests(unittest.TestCase):
    def test_prepare_pipeline_end_to_end_without_network_or_submission(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            public_db = root / "public.db"
            private_db = root / "private" / "applications.db"
            profile = write_profile(
                root / "profile.yaml",
                skills=verified(["Python", "SQL"]),
                target_roles=verified(["Data Analyst"]),
                remote_preference=verified("remote"),
            )
            answers = write_answers(
                root / "answers.yaml",
                {
                    "FULL_NAME": answer("Test Candidate"),
                    "EMAIL": answer("candidate@example.test"),
                    "PHONE": answer("+00-0000000000"),
                },
            )
            config = {
                "database": {"path": str(public_db)},
                "applications": {
                    "profile_path": str(profile),
                    "answer_bank_path": str(answers),
                    "private_db_path": str(private_db),
                    "required_questions": ["FULL_NAME", "EMAIL", "PHONE"],
                    "notify_telegram": False,
                },
            }
            db.init_db(public_db)
            db.insert_job(
                public_db,
                {
                    "title": "Data Analyst", "company": "Example", "location": "Remote",
                    "description": "", "url": "https://example.test/careers/1", "source": "test",
                    "keyword_matched": "analyst", "salary": "", "posted_at": "2026-01-01",
                },
            )
            job = db.get_job_by_url(public_db, "https://example.test/careers/1")
            db.update_job_enrichment(
                public_db,
                job["id"],
                EnrichmentResult(
                    "ENRICHED",
                    {
                        "full_description": "Use Python and SQL to analyze data.",
                        "required_skills": ["Python", "SQL"],
                        "remote_type": "Remote",
                        "application_url": "https://example.test/careers/1/apply",
                        "ats_provider": "Custom/Company ATS",
                    },
                ),
            )
            # It is already enriched, so the real function has no network work.
            results, errors = prepare_applications(config, ["--no-discovery", "--limit", "1"])
            self.assertEqual(errors, [])
            self.assertEqual(len(results), 1)
            self.assertEqual(results[0]["application"]["status"], "READY_TO_APPLY")
            self.assertIsNone(results[0]["application"]["submitted_at"])
            self.assertTrue(private_db.exists())

    def test_public_and_private_database_paths_must_differ(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            profile = write_profile(root / "profile.yaml")
            config = {
                "database": {"path": str(root / "same.db")},
                "applications": {
                    "profile_path": str(profile),
                    "private_db_path": str(root / "same.db"),
                },
            }
            with self.assertRaisesRegex(ValueError, "must be separate"):
                prepare_applications(config, ["--no-discovery"])


if __name__ == "__main__":
    unittest.main()
