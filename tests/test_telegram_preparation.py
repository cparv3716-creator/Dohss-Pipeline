import unittest
from types import SimpleNamespace
from unittest.mock import patch

from notifier.telegram import notify_prepared_applications


def result(eligibility="ELIGIBLE", status="READY_TO_APPLY"):
    return {
        "job": {
            "title": "Data Analyst", "company": "Example", "ats_provider": "Greenhouse",
            "application_url": "https://example.test/apply",
        },
        "eligibility": SimpleNamespace(status=eligibility),
        "match": SimpleNamespace(
            match_score=91, recommendation="APPLY",
            strengths=["Python"], gaps=["Domain knowledge"],
        ),
        "application": {"status": status},
    }


class TelegramPreparationTests(unittest.TestCase):
    def test_ineligible_jobs_are_not_notified(self):
        with patch("notifier.telegram._is_configured", return_value=True), patch("notifier.telegram._send_configured") as send:
            self.assertFalse(notify_prepared_applications({}, [result("INELIGIBLE", "SKIPPED")]))
            send.assert_not_called()

    def test_high_quality_summary_has_required_fields(self):
        with patch("notifier.telegram._is_configured", return_value=True), patch("notifier.telegram._send_configured", return_value=True) as send:
            self.assertTrue(notify_prepared_applications({}, [result()]))
            message = send.call_args.args[1]
            for value in ("Data Analyst", "Example", "91/100", "ELIGIBLE", "Greenhouse", "READY_TO_APPLY", "application link"):
                self.assertIn(value, message)


if __name__ == "__main__":
    unittest.main()
