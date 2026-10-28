import os
import unittest
from unittest.mock import MagicMock, patch
from dispatcher import dispatch_monitor, find_due_monitors, lambda_handler, send_monitor_to_queue, serialize_monitor, claim_monitor_for_dispatch
from decimal import Decimal
import json
from botocore.exceptions import ClientError

class TestDispatcher(unittest.TestCase):

    def test_find_due_monitors_pagination(self):
        mock_table = MagicMock()
        mock_table.scan.side_effect = [
            {
                "Items": [{"monitor_id": "mon-1", "url": "https://example.com/1"}],
                "LastEvaluatedKey": {"monitor_id": "mon-1"}
            },
            {
                "Items": [{"monitor_id": "mon-2", "url": "https://example.com/2"}]
            }
        ]
        
        current_time = 1700000000
        
        monitors = find_due_monitors(mock_table, current_time)
        
        self.assertEqual(len(monitors), 2)
        self.assertEqual(monitors[0]["monitor_id"], "mon-1")
        self.assertEqual(monitors[1]["monitor_id"], "mon-2")
        
        self.assertEqual(mock_table.scan.call_count, 2)

        first_call_kwargs = mock_table.scan.call_args_list[0].kwargs
        self.assertIn("FilterExpression", first_call_kwargs)

        second_call_kwargs = mock_table.scan.call_args_list[1].kwargs
        self.assertEqual(
            second_call_kwargs.get("ExclusiveStartKey"), 
            {"monitor_id": "mon-1"}
        )

    def test_serialize_monitor_handles_decimals(self):
        sample_record = {
            "monitor_id": "123e4567-e89b-12d3-a456-426614174000",
            "url": "https://example.com/api",
            "interval_seconds": Decimal("900"),
            "is_active": True,
            "next_check_at": Decimal("1700000900")
        }
        
        json_result = serialize_monitor(sample_record)
        
        parsed_data = json.loads(json_result)
        
        self.assertEqual(parsed_data["interval_seconds"], 900)
        self.assertIsInstance(parsed_data["interval_seconds"], int)
        
        self.assertEqual(parsed_data["next_check_at"], 1700000900)
        self.assertIsInstance(parsed_data["next_check_at"], int)
        
        self.assertEqual(parsed_data["monitor_id"], sample_record["monitor_id"])
        self.assertEqual(parsed_data["url"], "https://example.com/api")
        self.assertTrue(parsed_data["is_active"])

    def test_claim_monitor_for_dispatch_success(self):
        mock_table = MagicMock()
        monitor = {
            "monitor_id": "mon-1",
            "interval_seconds": 900
        }
        now = 1700000000

        result = claim_monitor_for_dispatch(mock_table, monitor, now)

        self.assertTrue(result)
        call_kwargs = mock_table.update_item.call_args.kwargs
        self.assertEqual(call_kwargs["Key"], {"monitor_id": "mon-1"})
        self.assertEqual(
            call_kwargs["ExpressionAttributeValues"][":new_next_check_at"],
            now + 900,
        )
        mock_table.update_item.assert_called_once()

    def test_claim_monitor_for_dispatch_conditional_check_failed(self):
        mock_table = MagicMock()
        mock_table.update_item.side_effect = ClientError(
            {"Error": {"Code": "ConditionalCheckFailedException"}}, "UpdateItem"
        )
        monitor = {
            "monitor_id": "mon-1",
            "interval_seconds": 900
        }
        now = 1700000000

        result = claim_monitor_for_dispatch(mock_table, monitor, now)

        self.assertFalse(result)

    def test_send_monitor_to_queue(self):
        mock_sqs_client = MagicMock()
        queue_url = "https://sqs.us-east-1.amazonaws.com/123456789012/CheckQueue"
        monitor = {
            "monitor_id": "mon-1",
            "url": "https://example.com/api",
            "interval_seconds": 900,
            "is_active": True,
            "next_check_at": 1700000900
        }

        expected_response = {"MessageId": "msg-123"}
        mock_sqs_client.send_message.return_value = expected_response

        result = send_monitor_to_queue(mock_sqs_client, queue_url, monitor)
        self.assertEqual(result, expected_response)


        mock_sqs_client.send_message.assert_called_once()
        call_kwargs = mock_sqs_client.send_message.call_args.kwargs
        self.assertEqual(call_kwargs["QueueUrl"], queue_url)
        self.assertEqual(
            json.loads(call_kwargs["MessageBody"]),
            {
                "monitor_id": "mon-1",
                "url": "https://example.com/api",
                "interval_seconds": 900,
                "is_active": True,
                "next_check_at": 1700000900
            }
        )

    def test_dispatch_monitor_success(self):
        mock_table = MagicMock()
        mock_sqs_client = MagicMock()
        queue_url = "https://sqs.us-east-1.amazonaws.com/123456789012/CheckQueue"
        monitor = {
            "monitor_id": "mon-1",
            "url": "https://example.com/api",
            "interval_seconds": 900,
            "is_active": True,
            "next_check_at": 1700000900
        }
        now = 1700000000

        result = dispatch_monitor(mock_table, mock_sqs_client, queue_url, monitor, now)

        self.assertTrue(result)
        mock_table.update_item.assert_called_once()
        mock_sqs_client.send_message.assert_called_once()

    def test_dispatch_monitor_failure(self):
        mock_table = MagicMock()
        mock_sqs_client = MagicMock()
        queue_url = "https://sqs.us-east-1.amazonaws.com/123456789012/CheckQueue"
        monitor = {
            "monitor_id": "mon-1",
            "url": "https://example.com/api",
            "interval_seconds": 900,
            "is_active": True,
            "next_check_at": 1700000900
        }
        now = 1700000000

        mock_table.update_item.side_effect = ClientError(
            {"Error": {"Code": "ConditionalCheckFailedException"}}, "UpdateItem"
        )

        result = dispatch_monitor(mock_table, mock_sqs_client, queue_url, monitor, now)

        self.assertFalse(result)
        mock_table.update_item.assert_called_once()
        mock_sqs_client.send_message.assert_not_called()

    @patch.dict(os.environ, {
        "MONITORS_TABLE": "TestMonitorsTable",
        "CHECK_QUEUE_URL": "https://sqs.example.test/CheckQueue",
    })
    @patch("dispatcher.dispatch_monitor", side_effect=[True, False])
    @patch("dispatcher.find_due_monitors")
    @patch("dispatcher.boto3.client")
    @patch("dispatcher.boto3.resource")
    def test_lambda_handler(
        self,
        mock_resource,
        mock_client,
        mock_find_due,
        mock_dispatch,
    ):
        mock_find_due.return_value = [
            {"monitor_id": "mon-1"},
            {"monitor_id": "mon-2"},
        ]

        result = lambda_handler({}, None)

        self.assertEqual(result, {"found": 2, "queued": 1})
        mock_resource.return_value.Table.assert_called_once_with("TestMonitorsTable")
        mock_client.assert_called_once_with("sqs")
        self.assertEqual(mock_dispatch.call_count, 2)
        
if __name__ == '__main__':
    unittest.main()
