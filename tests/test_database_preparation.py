import json
import sqlite3
import tempfile
import unittest
from pathlib import Path

from application_pipeline.enrichment import EnrichmentResult
from application_pipeline.preparation import prepare_job
from application_pipeline.private_db import (
    init_private_db, list_applications, transition_application, upsert_application,
)
from candidate import AnswerBank, CandidateProfile
from database import db
from tests.helpers import answer, verified, write_answers, write_profile


OLD_JOB = {
    "title": "Data Analyst", "company": "Example", "location": "India",
    "description": "Analyze data", "url": "https://example.test/jobs/1",
    "source": "test", "keyword_matched": "analyst", "salary": "",
    "posted_at": "2026-01-01",
}


class DatabaseAndPreparationTests(unittest.TestCase):
    def test_backward_compatible_job_migration_preserves_rows(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "old.db"
            conn = sqlite3.connect(path)
            conn.execute("CREATE TABLE jobs (id INTEGER PRIMARY KEY, title TEXT NOT NULL, company TEXT NOT NULL, location TEXT, description TEXT, url TEXT UNIQUE, source TEXT, keyword_matched TEXT, salary TEXT, posted_at TEXT, found_at TEXT, is_notified INTEGER DEFAULT 0, is_relevant INTEGER DEFAULT 1)")
            conn.execute("INSERT INTO jobs (id,title,company,url) VALUES (7,'Legacy Role','Legacy Co','https://example.test/legacy')")
            conn.commit()
            conn.close()
            db.init_db(str(path))
            conn = sqlite3.connect(path)
            columns = {row[1] for row in conn.execute("PRAGMA table_info(jobs)")}
            row = conn.execute("SELECT title, company FROM jobs WHERE id=7").fetchone()
            conn.close()
            self.assertEqual(row, ("Legacy Role", "Legacy Co"))
            for name in ("full_description", "requirements", "ats_provider", "match_score", "last_enriched_at", "enrichment_error"):
                self.assertIn(name, columns)

    def test_existing_discovery_payload_remains_insertable(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = str(Path(tmp) / "pipeline.db")
            db.init_db(path)
            self.assertTrue(db.insert_job(path, OLD_JOB))
            self.assertFalse(db.insert_job(path, OLD_JOB))
            self.assertEqual(db.get_job_by_url(path, OLD_JOB["url"])["title"], "Data Analyst")

    def test_public_enrichment_does_not_write_candidate_fields(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = str(Path(tmp) / "pipeline.db")
            db.init_db(path)
            db.insert_job(path, OLD_JOB)
            job = db.get_job_by_url(path, OLD_JOB["url"])
            result = EnrichmentResult("ENRICHED", {"required_skills": ["SQL"], "ats_provider": "Custom/Company ATS"})
            db.update_job_enrichment(path, job["id"], result)
            conn = sqlite3.connect(path)
            row = conn.execute("SELECT required_skills, eligibility_status, match_score, application_status FROM jobs WHERE id=?", (job["id"],)).fetchone()
            conn.close()
            self.assertEqual(json.loads(row[0]), ["SQL"])
            self.assertEqual(row[1:], (None, None, None))

    def test_submitted_is_prohibited_by_api_and_database(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = str(Path(tmp) / "private.db")
            app = upsert_application(path, {"job_id": 1, "status": "READY_TO_APPLY"})
            with self.assertRaises(ValueError):
                transition_application(path, app["id"], "SUBMITTED")
            conn = sqlite3.connect(path)
            with self.assertRaises(sqlite3.IntegrityError):
                conn.execute("UPDATE applications SET status='SUBMITTED' WHERE id=?", (app["id"],))
            conn.close()

    def test_valid_state_transition_is_recorded(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = str(Path(tmp) / "private.db")
            app = upsert_application(path, {"job_id": 1, "status": "NEEDS_REVIEW"})
            transition_application(path, app["id"], "READY_TO_APPLY", "review complete")
            self.assertEqual(list_applications(path)[0]["status"], "READY_TO_APPLY")

    def test_preparation_ready_and_needs_review(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            profile = CandidateProfile.load(
                write_profile(root / "profile.yaml", skills=verified(["Python"]), target_roles=verified(["Analyst"]))
            )
            complete = AnswerBank.load(
                write_answers(root / "answers.yaml", {
                    "FULL_NAME": answer("Test Candidate"),
                    "EMAIL": answer("candidate@example.test"),
                    "PHONE": answer("+00-0000000000"),
                })
            )
            job = {"id": 1, "title": "Analyst", "company": "Example", "required_skills": ["Python"], "application_url": "https://example.test/apply", "ats_provider": "Custom/Company ATS"}
            result = prepare_job(profile, complete, job, str(root / "private.db"))
            self.assertEqual(result["application"]["status"], "READY_TO_APPLY")
            result2 = prepare_job(profile, AnswerBank.empty(), {**job, "id": 2}, str(root / "private.db"))
            self.assertEqual(result2["application"]["status"], "NEEDS_REVIEW")
            self.assertIn("EMAIL", result2["unanswered_questions"])


if __name__ == "__main__":
    unittest.main()
