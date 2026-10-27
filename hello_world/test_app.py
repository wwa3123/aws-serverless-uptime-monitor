import json
import unittest

from app import lambda_handler 

class TestLambdaValidation(unittest.TestCase):
    
    def assert_status_code(self, event, expected_code):
        """Helper to invoke the handler and check the status code."""
        response = lambda_handler(event, None)
        self.assertEqual(response.get('statusCode'), expected_code)

    # --- 1. Happy Path ---
    def test_valid_payload_returns_200(self):
        body = json.dumps({"url": "https://example.com/api", "interval_seconds": 900})
        self.assert_status_code({"body": body}, 200)

    # --- 2. Structural JSON Checks ---
    def test_empty_body_returns_400(self):
        self.assert_status_code({}, 400)  # Missing 'body' key entirely
        self.assert_status_code({"body": None}, 400)

    def test_malformed_json_returns_400(self):
        self.assert_status_code({"body": '{"url": "https://example.com", "interval"}'}, 400)

    def test_json_array_returns_400(self):
        body = json.dumps([{"url": "https://example.com", "interval_seconds": 900}])
        self.assert_status_code({"body": body}, 400)

    # --- 3. URL Validation Checks ---
    def test_missing_url_returns_400(self):
        body = json.dumps({"interval_seconds": 900})
        self.assert_status_code({"body": body}, 400)

    def test_url_with_credentials_returns_400(self):
        body = json.dumps({"url": "https://user:pass@example.com", "interval_seconds": 900})
        self.assert_status_code({"body": body}, 400)

    def test_url_without_hostname_returns_400(self):
        body = json.dumps({"url": "https:///path/only", "interval_seconds": 900})
        self.assert_status_code({"body": body}, 400)

    # --- 4. Interval Validation Checks ---
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

if __name__ == '__main__':
    unittest.main()