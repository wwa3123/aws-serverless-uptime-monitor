import json
import unittest
from unittest.mock import MagicMock, patch
from botocore.exceptions import ClientError
from boto3.dynamodb.types import TypeDeserializer
import os
import app

class TestApp(unittest.TestCase):
    
    def assert_status_code(self, event, expected_code):
        response = app.lambda_handler(event, None)
        self.assertEqual(response.get('statusCode'), expected_code)

    def test_create_monitor_with_quota_transacts_quota_and_monitor(self):
        client = MagicMock()
        record = {
            "monitor_id": "monitor-1",
            "url": "https://example.com",
            "interval_seconds": 900,
            "is_active": True,
            "next_check_at": 1_700_000_000,
        }

        app.create_monitor_with_quota(
            client,
            "MonitorQuotaTable",
            "MonitorsTable",
            record,
        )

        client.transact_write_items.assert_called_once()
        transaction = client.transact_write_items.call_args.kwargs["TransactItems"]
        self.assertEqual(len(transaction), 2)

        quota_update = transaction[0]["Update"]
        self.assertEqual(quota_update["TableName"], "MonitorQuotaTable")
        self.assertEqual(quota_update["Key"], {"quota_id": {"S": "monitor_count"}})
        self.assertEqual(
            quota_update["UpdateExpression"],
            "SET #count = if_not_exists(#count, :zero) + :one",
        )
        self.assertEqual(
            quota_update["ConditionExpression"],
            "attribute_not_exists(#count) OR #count < :limit",
        )
        self.assertEqual(
            quota_update["ExpressionAttributeValues"][":limit"],
            {"N": "10"},
        )

        monitor_put = transaction[1]["Put"]
        self.assertEqual(monitor_put["TableName"], "MonitorsTable")
        deserializer = TypeDeserializer()
        deserialized_item = {
            key: deserializer.deserialize(value)
            for key, value in monitor_put["Item"].items()
        }
        self.assertEqual(deserialized_item, record)

    def test_create_monitor_with_quota_raises_when_limit_is_reached(self):
        client = MagicMock()
        client.transact_write_items.side_effect = ClientError(
            {
                "Error": {"Code": "TransactionCanceledException"},
                "CancellationReasons": [
                    {"Code": "ConditionalCheckFailed"},
                    {"Code": "None"},
                ],
            },
            "TransactWriteItems",
        )

        with self.assertRaises(app.MonitorLimitReached):
            app.create_monitor_with_quota(
                client,
                "MonitorQuotaTable",
                "MonitorsTable",
                {"monitor_id": "monitor-1"},
            )

    def test_create_monitor_with_quota_reraises_other_transaction_errors(self):
        client = MagicMock()
        transaction_error = ClientError(
            {
                "Error": {"Code": "ProvisionedThroughputExceededException"},
            },
            "TransactWriteItems",
        )
        client.transact_write_items.side_effect = transaction_error

        with self.assertRaises(ClientError) as raised:
            app.create_monitor_with_quota(
                client,
                "MonitorQuotaTable",
                "MonitorsTable",
                {"monitor_id": "monitor-1"},
            )

        self.assertIs(raised.exception, transaction_error)

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
        body = json.dumps({"url": "https://example.com", "interval_seconds": True})
        self.assert_status_code({"body": body}, 400)

    def test_interval_string_returns_400(self):
        body = json.dumps({"url": "https://example.com", "interval_seconds": "900"})
        self.assert_status_code({"body": body}, 400)

    def test_interval_below_minimum_returns_400(self):
        body = json.dumps({"url": "https://example.com", "interval_seconds": 299})
        response = app.lambda_handler({"body": body}, None)
        self.assertEqual(response["statusCode"], 400)
        self.assertEqual(
            json.loads(response["body"]),
            {"message": "Interval seconds must be at least 300"},
        )

    def test_interval_minimum_returns_201(self):
        with patch.dict(
            os.environ,
            {
                "MONITORS_TABLE": "TestMonitorsTable",
                "MONITOR_QUOTA_TABLE": "TestMonitorQuotaTable",
            },
        ), patch("app.boto3.client") as mock_boto3_client:
            mock_client = mock_boto3_client.return_value

            body = json.dumps({
                "url": "https://example.com/api",
                "interval_seconds": 300,
            })
            response = app.lambda_handler({"body": body}, None)

            self.assertEqual(response.get("statusCode"), 201)
            mock_boto3_client.assert_called_once_with("dynamodb")
            transaction = mock_client.transact_write_items.call_args.kwargs["TransactItems"]
            inserted_item = {
                key: TypeDeserializer().deserialize(value)
                for key, value in transaction[1]["Put"]["Item"].items()
            }
            self.assertEqual(inserted_item["url"], "https://example.com/api")
            self.assertEqual(inserted_item["interval_seconds"], 300)
            self.assertTrue(inserted_item["is_active"])
            self.assertIn("monitor_id", inserted_item)
            self.assertIn('next_check_at', inserted_item)
            self.assertEqual(inserted_item["next_check_at"] % 1, 0)

    def test_monitor_limit_returns_409(self):
        quota_error = ClientError(
            {
                "Error": {"Code": "TransactionCanceledException"},
                "CancellationReasons": [
                    {"Code": "ConditionalCheckFailed"},
                    {"Code": "None"},
                ],
            },
            "TransactWriteItems",
        )
        with patch.dict(
            os.environ,
            {
                "MONITORS_TABLE": "TestMonitorsTable",
                "MONITOR_QUOTA_TABLE": "TestMonitorQuotaTable",
            },
        ), patch("app.boto3.client") as mock_boto3_client:
            mock_boto3_client.return_value.transact_write_items.side_effect = quota_error
            body = json.dumps({
                "url": "https://example.com/api",
                "interval_seconds": 900,
            })

            response = app.lambda_handler({"body": body}, None)

        self.assertEqual(response["statusCode"], 409)
        self.assertEqual(
            json.loads(response["body"]),
            {"message": "Monitor limit of 10 reached"},
        )

if __name__ == '__main__':
    unittest.main()