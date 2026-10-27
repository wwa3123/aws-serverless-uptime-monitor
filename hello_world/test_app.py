import json
import unittest
from unittest.mock import MagicMock, patch
import os
import app

class TestLambdaValidation(unittest.TestCase):
    
    def assert_status_code(self, event, expected_code):
        """Helper to invoke the handler and check the status code."""
        response = app.lambda_handler(event, None)
        self.assertEqual(response.get('statusCode'), expected_code)

    def test_empty_body_returns_400(self):
        self.assert_status_code({}, 400)  # Missing 'body' key entirely
        self.assert_status_code({"body": None}, 400)

    def test_malformed_json_returns_400(self):
        self.assert_status_code({"body": '{"url": "https://example.com", "interval"}'}, 400)

    def test_json_array_returns_400(self):
        body = json.dumps([{"url": "https://example.com", "interval_seconds": 900}])
        self.assert_status_code({"body": body}, 400)

    def test_missing_url_returns_400(self):
        body = json.dumps({"interval_seconds": 900})
        self.assert_status_code({"body": body}, 400)

    def test_url_with_credentials_returns_400(self):
        body = json.dumps({"url": "https://user:pass@example.com", "interval_seconds": 900})
        self.assert_status_code({"body": body}, 400)

    def test_url_without_hostname_returns_400(self):
        body = json.dumps({"url": "https:///path/only", "interval_seconds": 900})
        self.assert_status_code({"body": body}, 400)

    def test_interval_boolean_returns_400(self):
        # json.dumps converts True to true, which parses as a boolean in Python
        body = json.dumps({"url": "https://example.com", "interval_seconds": True})
        self.assert_status_code({"body": body}, 400)

    def test_interval_string_returns_400(self):
        body = json.dumps({"url": "https://example.com", "interval_seconds": "900"})
        self.assert_status_code({"body": body}, 400)

    def test_interval_below_minimum_returns_400(self):
        body = json.dumps({"url": "https://example.com", "interval_seconds": 899})
        self.assert_status_code({"body": body}, 400)

    def test_interval_minimum_returns_201(self):
        with patch.dict(os.environ, {"MONITORS_TABLE": "TestMonitorsTable"}), \
             patch("app.boto3.resource") as mock_boto3_resource:
            mock_table = MagicMock()
            mock_boto3_resource.return_value.Table.return_value = mock_table

            body = json.dumps({
                "url": "https://example.com/api",
                "interval_seconds": 900,
            })
            response = app.lambda_handler({"body": body}, None)

            self.assertEqual(response.get("statusCode"), 201)
            mock_table.put_item.assert_called_once()

            inserted_item = mock_table.put_item.call_args.kwargs["Item"]
            self.assertEqual(inserted_item["url"], "https://example.com/api")
            self.assertEqual(inserted_item["interval_seconds"], 900)
            self.assertTrue(inserted_item["is_active"])
            self.assertIn("monitor_id", inserted_item)

if __name__ == '__main__':
    unittest.main()