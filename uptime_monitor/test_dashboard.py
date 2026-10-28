import json
import unittest
from unittest.mock import MagicMock, call, patch

from uptime_monitor.dashboard import (
    build_public_monitor_summary,
    lambda_handler,
)


class TestPublicMonitorSummary(unittest.TestCase):
    def test_builds_public_summary_without_exposing_url(self):
        monitor = {
            "url": "https://status.example.com/private/path?token=secret",
        }
        latest_result = {
            "is_healthy": True,
            "checked_at": "1793173843#72d7bc37-6e80-4a41-95dd-c509b3c8d6ee",
        }

        summary = build_public_monitor_summary(monitor, latest_result)

        self.assertEqual(
            summary,
            {
                "hostname": "status.example.com",
                "health_state": "healthy",
                "last_checked": 1793173843,
            },
        )
        self.assertEqual(
            set(summary),
            {"hostname", "health_state", "last_checked"},
        )
        self.assertNotIn("url", summary)
        self.assertNotIn("private", str(summary))
        self.assertNotIn("secret", str(summary))

    def test_builds_unknown_summary_when_no_result_exists(self):
        monitor = {"url": "http://service.example.org/health"}

        summary = build_public_monitor_summary(monitor, None)

        self.assertEqual(
            summary,
            {
                "hostname": "service.example.org",
                "health_state": "unknown",
                "last_checked": None,
            },
        )
        self.assertEqual(
            set(summary),
            {"hostname", "health_state", "last_checked"},
        )
        self.assertNotIn("url", summary)

    @patch.dict(
        "os.environ",
        {
            "MONITORS_TABLE": "TestMonitors",
            "CHECK_RESULTS_TABLE": "TestCheckResults",
        },
    )
    @patch("uptime_monitor.dashboard.get_latest_check_result")
    @patch("uptime_monitor.dashboard.boto3.resource")
    def test_handler_returns_safe_summaries_and_unknown_health(
        self,
        mock_resource,
        mock_get_latest,
    ):
        monitor_with_result = {
            "monitor_id": "mon-healthy",
            "url": "https://healthy.example.com/private?token=secret",
        }
        monitor_without_result = {
            "monitor_id": "mon-new",
            "url": "https://new.example.com/health",
        }
        monitors_table = MagicMock()
        monitors_table.scan.return_value = {
            "Items": [monitor_with_result, monitor_without_result],
        }
        results_table = MagicMock()
        mock_resource.return_value.Table.side_effect = [
            monitors_table,
            results_table,
        ]
        mock_get_latest.side_effect = [
            {
                "is_healthy": True,
                "checked_at": "1793173843#72d7bc37-6e80-4a41-95dd-c509b3c8d6ee",
            },
            None,
        ]

        response = lambda_handler({}, None)

        self.assertEqual(response["statusCode"], 200)
        self.assertEqual(response["headers"]["Content-Type"], "application/json")
        body = json.loads(response["body"])
        self.assertEqual(
            body,
            {
                "monitors": [
                    {
                        "hostname": "healthy.example.com",
                        "health_state": "healthy",
                        "last_checked": 1793173843,
                    },
                    {
                        "hostname": "new.example.com",
                        "health_state": "unknown",
                        "last_checked": None,
                    },
                ],
            },
        )
        self.assertNotIn("url", response["body"])
        self.assertNotIn("private", response["body"])
        self.assertNotIn("secret", response["body"])
        mock_get_latest.assert_has_calls(
            [
                call(results_table, "mon-healthy"),
                call(results_table, "mon-new"),
            ]
        )

    @patch.dict(
        "os.environ",
        {
            "MONITORS_TABLE": "TestMonitors",
            "CHECK_RESULTS_TABLE": "TestCheckResults",
        },
    )
    @patch("uptime_monitor.dashboard.get_latest_check_result", return_value=None)
    @patch("uptime_monitor.dashboard.boto3.resource")
    def test_handler_scans_all_monitor_pages(self, mock_resource, mock_get_latest):
        monitors_table = MagicMock()
        monitors_table.scan.side_effect = [
            {
                "Items": [
                    {
                        "monitor_id": "mon-1",
                        "url": "https://one.example.com",
                    },
                ],
                "LastEvaluatedKey": {"monitor_id": "mon-1"},
            },
            {
                "Items": [
                    {
                        "monitor_id": "mon-2",
                        "url": "https://two.example.com",
                    },
                ],
            },
        ]
        results_table = MagicMock()
        mock_resource.return_value.Table.side_effect = [
            monitors_table,
            results_table,
        ]

        response = lambda_handler({}, None)

        self.assertEqual(
            [summary["hostname"] for summary in json.loads(response["body"])["monitors"]],
            ["one.example.com", "two.example.com"],
        )
        self.assertEqual(
            monitors_table.scan.call_args_list,
            [
                call(),
                call(ExclusiveStartKey={"monitor_id": "mon-1"}),
            ],
        )
        self.assertEqual(mock_get_latest.call_count, 2)


if __name__ == "__main__":
    unittest.main()
