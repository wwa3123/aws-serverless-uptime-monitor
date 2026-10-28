import json
import unittest
from boto3.dynamodb.conditions import Key
from uptime_monitor.worker import (
    build_check_result_item,
    check_monitor,
    get_http_status,
    get_health_status,
    get_latest_check_result,
    is_public_ip_address,
    lambda_handler,
    open_pinned_connection,
    parse_monitor_message,
    publish_alert,
    resolve_public_addresses,
    save_check_result,
)
from unittest.mock import MagicMock, patch
import socket

class TestWorker(unittest.TestCase):
    def make_record(self, **changes):
        message = {
            "monitor_id": "123",
            "url": "https://example.com",
            "interval_seconds": 900,
            "is_active": True,
            "next_check_at": 1234567890,
        }
        message.update(changes)
        return {"body": json.dumps(message)}

    def test_valid_record(self):
        result = parse_monitor_message(self.make_record())
        self.assertEqual(result["monitor_id"], "123")
        self.assertEqual(result["interval_seconds"], 900)

    def test_non_dictionary_record_raises_value_error(self):
        with self.assertRaises(ValueError):
            parse_monitor_message("not a dict")

    def test_empty_url_raises_value_error(self):
        with self.assertRaises(ValueError):
            parse_monitor_message(self.make_record(url=""))

    def test_url_with_whitespace_raises_value_error(self):
        with self.assertRaises(ValueError):
            parse_monitor_message(self.make_record(url="https://example.com/\tpath"))

    def test_parse_monitor_message_rejects_invalid_urls(self):
        base_payload = {
            "monitor_id": "123e4567-e89b-12d3-a456-426614174000",
            "interval_seconds": 900,
            "is_active": True,
            "next_check_at": 1700000900
        }
        
        invalid_urls = [
            "ftp://example.com",             
            "https:///path",                 
            "https://user:pass@example.com" 
        ]
        
        for url in invalid_urls:
            with self.subTest(url=url):
                invalid_payload = base_payload.copy()
                invalid_payload["url"] = url
                
                message_body = json.dumps(invalid_payload)

                with self.assertRaises(ValueError):
                    parse_monitor_message({"body": message_body})

    def test_malformed_json_raises_json_decode_error(self):
        with self.assertRaises(json.JSONDecodeError):
            parse_monitor_message({"body": "{invalid json"})

    def test_boolean_interval_raises_value_error(self):
        with self.assertRaises(ValueError):
            parse_monitor_message(self.make_record(interval_seconds=True))

    def test_interval_below_300_seconds_raises_value_error(self):
        with self.assertRaises(ValueError):
            parse_monitor_message(self.make_record(interval_seconds=299))

    def test_interval_of_300_seconds_is_valid(self):
        result = parse_monitor_message(self.make_record(interval_seconds=300))
        self.assertEqual(result["interval_seconds"], 300)

    def test_boolean_next_check_at_raises_value_error(self):
        with self.assertRaises(ValueError):
            parse_monitor_message(self.make_record(next_check_at=True))

    def test_parse_monitor_message_missing_fields_raises_error(self):
        valid_payload = {
            "monitor_id": "123e4567-e89b-12d3-a456-426614174000",
            "url": "https://example.com/api",
            "interval_seconds": 900,
            "is_active": True,
            "next_check_at": 1700000900
        }
        
        required_fields = [
            "monitor_id", 
            "url", 
            "interval_seconds", 
            "is_active", 
            "next_check_at"
        ]
        
        for field in required_fields:
            with self.subTest(field=field):
                invalid_payload = valid_payload.copy()
                del invalid_payload[field]

                message_body = json.dumps(invalid_payload)
                
                with self.assertRaises(ValueError):
                    parse_monitor_message({"body": message_body})

    def test_is_public_ip_address(self):
        test_cases = [
            ("8.8.8.8", True),
            ("93.184.216.34", True),
            ("2606:4700:4700::1111", True),
            
            ("127.0.0.1", False),
            ("127.123.0.1", False),
            ("::1", False),
            
            ("10.0.0.1", False),
            ("172.16.0.1", False),
            ("192.168.1.1", False),
            
            ("169.254.169.254", False), 
            ("fe80::1", False),
            
            ("localhost", False),
            ("not_an_ip", False),
            ("", False)
        ]
        
        for ip, expected in test_cases:
            with self.subTest(ip=ip):
                self.assertEqual(is_public_ip_address(ip), expected)

    @patch("uptime_monitor.worker.socket.getaddrinfo")
    def test_resolve_public_addresses_rejects_mixed_public_and_private(self, mock_getaddrinfo):
        mock_getaddrinfo.return_value = [
            (2, 1, 6, "", ("8.8.8.8", 443)),
            (2, 1, 6, "", ("10.0.0.1", 443)),
        ]

        with self.assertRaises(ValueError):
            resolve_public_addresses("example.com", 443)

        mock_getaddrinfo.assert_called_once()

    @patch('uptime_monitor.worker.socket.getaddrinfo')
    def test_resolve_public_addresses_returns_distinct_public_ips(self, mock_getaddrinfo):

        mock_getaddrinfo.return_value = [
            (socket.AF_INET, socket.SOCK_STREAM, 6, '', ('93.184.216.34', 443)),
            (socket.AF_INET, socket.SOCK_STREAM, 6, '', ('8.8.8.8', 443)),
            (socket.AF_INET, socket.SOCK_STREAM, 6, '', ('93.184.216.34', 443)) 
        ]
        
        hostname = "valid.example.com"
        
        result = resolve_public_addresses(hostname, 443)
        
        self.assertEqual(len(result), 2)
        
        self.assertCountEqual(result, ['93.184.216.34', '8.8.8.8'])

    @patch('uptime_monitor.worker.socket.getaddrinfo')
    def test_resolve_public_addresses_empty_result_raises_error(self, mock_getaddrinfo):
        mock_getaddrinfo.return_value = []
        
        hostname = "empty.example.com"
        
        with self.assertRaises(ValueError):
            resolve_public_addresses(hostname, 443)   

    def test_open_pinned_connection_rejects_non_public_ip(self):
        with self.assertRaises(ValueError):
            open_pinned_connection("example.com", 443, "10.0.0.1", use_tls=True)

    @patch("uptime_monitor.worker.socket.create_connection")
    def test_open_pinned_connection_uses_validated_ip(self, mock_create_connection):
        mock_connection = mock_create_connection.return_value

        result = open_pinned_connection(
            "example.com",
            443,
            "8.8.8.8",
            use_tls=False,
        )

        self.assertIs(result, mock_connection)
        mock_create_connection.assert_called_once_with(
            ("8.8.8.8", 443),
            timeout=5,
        )

    @patch("uptime_monitor.worker.ssl.create_default_context")
    @patch("uptime_monitor.worker.socket.create_connection")
    def test_open_pinned_connection_uses_tls_hostname(
        self,
        mock_create_connection,
        mock_create_default_context,
    ):
        raw_socket = MagicMock()
        wrapped_socket = MagicMock()
        ssl_context = mock_create_default_context.return_value
        ssl_context.wrap_socket.return_value = wrapped_socket
        mock_create_connection.return_value = raw_socket

        result = open_pinned_connection(
            "example.com",
            443,
            "8.8.8.8",
            use_tls=True,
        )

        self.assertIs(result, wrapped_socket)
        mock_create_connection.assert_called_once_with(
            ("8.8.8.8", 443),
            timeout=5,
        )
        ssl_context.wrap_socket.assert_called_once_with(
            raw_socket,
            server_hostname="example.com",
        )

    @patch("uptime_monitor.worker.http.client.HTTPResponse")
    @patch("uptime_monitor.worker.open_pinned_connection")
    def test_get_http_status_returns_status_and_closes_socket(
        self,
        mock_open_connection,
        mock_http_response,
    ):
        mock_socket = MagicMock()
        mock_open_connection.return_value = mock_socket
        mock_response = mock_http_response.return_value
        mock_response.status = 204

        status = get_http_status(
            "https://example.com/health?probe=1",
            "8.8.8.8",
        )

        self.assertEqual(status, 204)
        mock_open_connection.assert_called_once_with(
            "example.com",
            443,
            "8.8.8.8",
            True,
            timeout=5,
        )
        mock_socket.sendall.assert_called_once_with(
            b"GET /health?probe=1 HTTP/1.1\r\n"
            b"Host: example.com\r\n"
            b"Connection: close\r\n\r\n"
        )
        mock_http_response.assert_called_once_with(mock_socket)
        mock_response.begin.assert_called_once_with()
        mock_socket.close.assert_called_once_with()

    @patch("uptime_monitor.worker.get_http_status", return_value=204)
    @patch(
        "uptime_monitor.worker.resolve_public_addresses",
        return_value=["8.8.8.8", "1.1.1.1"],
    )
    def test_check_monitor_returns_healthy_result_for_2xx(
        self,
        mock_resolve_addresses,
        mock_get_http_status,
    ):
        monitor = {
            "monitor_id": "mon-1",
            "url": "https://example.com/health",
        }

        result = check_monitor(monitor)

        self.assertEqual(
            result,
            {
                "monitor_id": "mon-1",
                "status_code": 204,
                "is_healthy": True,
            },
        )
        mock_resolve_addresses.assert_called_once_with("example.com", 443)
        mock_get_http_status.assert_called_once_with(
            "https://example.com/health",
            "8.8.8.8",
        )

    @patch("uptime_monitor.worker.get_http_status", return_value=302)
    @patch(
        "uptime_monitor.worker.resolve_public_addresses",
        return_value=["8.8.8.8"],
    )
    def test_check_monitor_treats_redirect_as_unhealthy(
        self,
        mock_resolve_addresses,
        mock_get_http_status,
    ):
        monitor = {
            "monitor_id": "mon-1",
            "url": "https://example.com/health",
        }

        result = check_monitor(monitor)

        self.assertEqual(
            result,
            {
                "monitor_id": "mon-1",
                "status_code": 302,
                "is_healthy": False,
            },
        )
        mock_resolve_addresses.assert_called_once_with("example.com", 443)
        mock_get_http_status.assert_called_once_with(
            "https://example.com/health",
            "8.8.8.8",
        )

    @patch(
        "uptime_monitor.worker.get_http_status",
        side_effect=OSError("connection failed"),
    )
    @patch(
        "uptime_monitor.worker.resolve_public_addresses",
        return_value=["8.8.8.8"],
    )
    def test_check_monitor_records_network_error(
        self,
        mock_resolve_addresses,
        mock_get_http_status,
    ):
        monitor = {
            "monitor_id": "mon-1",
            "url": "https://example.com/health",
        }

        result = check_monitor(monitor)

        self.assertEqual(
            result,
            {
                "monitor_id": "mon-1",
                "status_code": None,
                "is_healthy": False,
                "error_category": "network_error",
            },
        )

        mock_resolve_addresses.assert_called_once_with("example.com", 443)
        mock_get_http_status.assert_called_once_with(
            "https://example.com/health",
            "8.8.8.8",
        )

    def test_build_check_result_item(self):
        checked_at = 1_700_000_900
        result = {
            "monitor_id": "mon-1",
            "status_code": 204,
            "is_healthy": True,
        }

        item = build_check_result_item(result, checked_at)

        self.assertEqual(item["monitor_id"], "mon-1")
        self.assertEqual(item["status_code"], 204)
        self.assertTrue(item["is_healthy"])
        self.assertRegex(item["checked_at"], rf"^{checked_at:010d}#[0-9a-f-]{{36}}$")
        self.assertEqual(item["expires_at"], checked_at + 30 * 24 * 60 * 60)
        self.assertIsInstance(item["expires_at"], int)

    def test_save_check_result_writes_and_returns_item(self):
        table = MagicMock()
        result = {
            "monitor_id": "mon-1",
            "status_code": 204,
            "is_healthy": True,
        }
        checked_at = 1_700_000_900

        item = save_check_result(table, result, checked_at)

        table.put_item.assert_called_once_with(Item=item)
        self.assertEqual(item["monitor_id"], result["monitor_id"])
        self.assertEqual(item["status_code"], result["status_code"])
        self.assertEqual(item["is_healthy"], result["is_healthy"])
        self.assertRegex(item["checked_at"], rf"^{checked_at:010d}#[0-9a-f-]{{36}}$")
        self.assertEqual(item["expires_at"], checked_at + 30 * 24 * 60 * 60)

    def test_build_check_result_item_includes_error_category_when_present(self):
        checked_at = 1_700_000_900
        result = {
            "monitor_id": "mon-1",
            "status_code": None,
            "is_healthy": False,
            "error_category": "network_error",
        }

        item = build_check_result_item(result, checked_at)

        self.assertEqual(item["monitor_id"], "mon-1")
        self.assertIsNone(item["status_code"])
        self.assertFalse(item["is_healthy"])
        self.assertEqual(item["error_category"], "network_error")
        self.assertRegex(item["checked_at"], rf"^{checked_at:010d}#[0-9a-f-]{{36}}$")
        self.assertEqual(item["expires_at"], checked_at + 30 * 24 * 60 * 60)

    def test_save_check_result_persists_error_category(self):
        table = MagicMock()
        result = {
            "monitor_id": "mon-1",
            "status_code": None,
            "is_healthy": False,
            "error_category": "network_error",
        }
        checked_at = 1_700_000_900

        item = save_check_result(table, result, checked_at)

        table.put_item.assert_called_once_with(Item=item)
        self.assertEqual(item["error_category"], "network_error")

    def test_get_latest_check_result_returns_newest_item(self):
        latest_item = {
            "monitor_id": "mon-1",
            "checked_at": "1700000900#latest",
            "is_healthy": True,
        }
        table = MagicMock()
        table.query.return_value = {"Items": [latest_item]}

        result = get_latest_check_result(table, "mon-1")

        self.assertEqual(result, latest_item)
        table.query.assert_called_once()
        query_args = table.query.call_args.kwargs
        self.assertEqual(
            query_args["KeyConditionExpression"].get_expression(),
            Key("monitor_id").eq("mon-1").get_expression(),
        )
        self.assertFalse(query_args["ScanIndexForward"])
        self.assertEqual(query_args["Limit"], 1)

    def test_get_latest_check_result_returns_none_when_no_items(self):
        table = MagicMock()
        table.query.return_value = {"Items": []}

        result = get_latest_check_result(table, "mon-1")

        self.assertIsNone(result)
        table.query.assert_called_once()
        query_args = table.query.call_args.kwargs
        self.assertFalse(query_args["ScanIndexForward"])
        self.assertEqual(query_args["Limit"], 1)

    def test_get_health_status_reports_outage_transition(self):
        previous_result = {"is_healthy": True}
        current_result = {"is_healthy": False}

        self.assertEqual(
            get_health_status(previous_result, current_result),
            "outage",
        )

    def test_get_health_status_reports_recovery_transition(self):
        previous_result = {"is_healthy": False}
        current_result = {"is_healthy": True}

        self.assertEqual(
            get_health_status(previous_result, current_result),
            "recovery",
        )

    def test_get_health_status_reports_first_observed_outage(self):
        current_result = {"is_healthy": False}

        self.assertEqual(get_health_status(None, current_result), "outage")

    def test_get_health_status_does_not_report_first_healthy_result(self):
        current_result = {"is_healthy": True}

        self.assertIsNone(get_health_status(None, current_result))

    def test_get_health_status_ignores_repeated_health_status(self):
        self.assertIsNone(
            get_health_status(
                {"is_healthy": True},
                {"is_healthy": True},
            )
        )
        self.assertIsNone(
            get_health_status(
                {"is_healthy": False},
                {"is_healthy": False},
            )
        )

    def test_publish_alert_sends_json_message_and_transition_subject(self):
        sns_client = MagicMock()
        result = {
            "status_code": None,
            "is_healthy": False,
        }

        publish_alert(
            sns_client,
            "arn:aws:sns:us-east-1:123456789012:alerts",
            "mon-1",
            "outage",
            result,
            1_700_000_900,
        )

        sns_client.publish.assert_called_once()
        publish_args = sns_client.publish.call_args.kwargs
        self.assertEqual(
            publish_args["TopicArn"],
            "arn:aws:sns:us-east-1:123456789012:alerts",
        )
        self.assertEqual(publish_args["Subject"], "Monitor Outage")
        self.assertEqual(
            json.loads(publish_args["Message"]),
            {
                "monitor_id": "mon-1",
                "transition": "outage",
                "status_code": None,
                "is_healthy": False,
                "checked_at": 1_700_000_900,
            },
        )

    @patch.dict(
        "os.environ",
        {
            "CHECK_RESULTS_TABLE": "TestCheckResults",
            "ALERT_TOPIC_ARN": "arn:aws:sns:us-east-1:123456789012:alerts",
        },
    )
    @patch("uptime_monitor.worker.save_check_result")
    @patch(
        "uptime_monitor.worker.check_monitor",
        return_value={
            "monitor_id": "mon-active",
            "status_code": 204,
            "is_healthy": True,
        },
    )
    @patch("uptime_monitor.worker.parse_monitor_message")
    @patch("uptime_monitor.worker.boto3.resource")
    @patch("uptime_monitor.worker.boto3.client")
    @patch("uptime_monitor.worker.get_latest_check_result", return_value=None)
    def test_lambda_handler_processes_active_skips_inactive_and_reports_failures(
        self,
        mock_get_latest,
        mock_client,
        mock_resource,
        mock_parse,
        mock_check_monitor,
        mock_save_result,
    ):
        active_record = {"messageId": "msg-active", "body": "active"}
        inactive_record = {"messageId": "msg-inactive", "body": "inactive"}
        invalid_record = {"messageId": "msg-invalid", "body": "invalid"}
        active_monitor = {
            "monitor_id": "mon-active",
            "url": "https://example.com",
            "interval_seconds": 900,
            "is_active": True,
            "next_check_at": 1234567890,
        }
        mock_parse.side_effect = [
            active_monitor,
            {
                "monitor_id": "mon-inactive",
                "url": "https://example.com",
                "interval_seconds": 900,
                "is_active": False,
                "next_check_at": 1234567890,
            },
            ValueError("invalid message"),
        ]
        table = mock_resource.return_value.Table.return_value
        records = [active_record, inactive_record, invalid_record]

        response = lambda_handler({"Records": records}, None)

        self.assertEqual(
            response,
            {"batchItemFailures": [{"itemIdentifier": "msg-invalid"}]},
        )
        mock_resource.assert_called_once_with("dynamodb")
        mock_resource.return_value.Table.assert_called_once_with("TestCheckResults")
        mock_check_monitor.assert_called_once_with(active_monitor)
        mock_save_result.assert_called_once()
        self.assertEqual(mock_save_result.call_args.args[0], table)
        self.assertEqual(mock_save_result.call_args.args[1], mock_check_monitor.return_value)
        self.assertIsInstance(mock_save_result.call_args.args[2], int)
        self.assertEqual(mock_parse.call_count, 3)

    @patch.dict(
        "os.environ",
        {
            "CHECK_RESULTS_TABLE": "TestCheckResults",
            "ALERT_TOPIC_ARN": "arn:aws:sns:us-east-1:123456789012:alerts",
        },
    )
    @patch(
        "uptime_monitor.worker.save_check_result",
        side_effect=RuntimeError("DynamoDB write failed"),
    )
    @patch(
        "uptime_monitor.worker.check_monitor",
        return_value={
            "monitor_id": "mon-write-failed",
            "status_code": 204,
            "is_healthy": True,
        },
    )
    @patch(
        "uptime_monitor.worker.parse_monitor_message",
        return_value={
            "monitor_id": "mon-write-failed",
            "url": "https://example.com",
            "interval_seconds": 900,
            "is_active": True,
            "next_check_at": 1234567890,
        },
    )
    @patch("uptime_monitor.worker.boto3.resource")
    @patch("uptime_monitor.worker.boto3.client")
    @patch("uptime_monitor.worker.get_latest_check_result", return_value=None)
    def test_lambda_handler_reports_result_write_failure(
        self,
        mock_get_latest,
        mock_client,
        mock_resource,
        mock_parse,
        mock_check_monitor,
        mock_save_result,
    ):
        record = {"messageId": "msg-write-failed", "body": "valid"}

        response = lambda_handler({"Records": [record]}, None)

        self.assertEqual(
            response,
            {"batchItemFailures": [{"itemIdentifier": "msg-write-failed"}]},
        )
        mock_resource.assert_called_once_with("dynamodb")
        mock_parse.assert_called_once_with(record)
        mock_check_monitor.assert_called_once()
        mock_save_result.assert_called_once()

    @patch.dict(
        "os.environ",
        {
            "CHECK_RESULTS_TABLE": "TestCheckResults",
            "ALERT_TOPIC_ARN": "arn:aws:sns:us-east-1:123456789012:alerts",
        },
    )
    @patch("uptime_monitor.worker.save_check_result")
    @patch(
        "uptime_monitor.worker.check_monitor",
        return_value={
            "monitor_id": "mon-network-failure",
            "status_code": None,
            "is_healthy": False,
            "error_category": "network_error",
        },
    )
    @patch(
        "uptime_monitor.worker.parse_monitor_message",
        return_value={
            "monitor_id": "mon-network-failure",
            "url": "https://example.com",
            "interval_seconds": 900,
            "is_active": True,
            "next_check_at": 1234567890,
        },
    )
    @patch("uptime_monitor.worker.boto3.resource")
    @patch("uptime_monitor.worker.boto3.client")
    @patch("uptime_monitor.worker.get_latest_check_result", return_value=None)
    def test_lambda_handler_saves_and_acknowledges_network_failure_result(
        self,
        mock_get_latest,
        mock_client,
        mock_resource,
        mock_parse,
        mock_check_monitor,
        mock_save_result,
    ):
        record = {"messageId": "msg-network-failure", "body": "valid"}

        response = lambda_handler({"Records": [record]}, None)

        self.assertEqual(response, {"batchItemFailures": []})
        mock_parse.assert_called_once_with(record)
        mock_check_monitor.assert_called_once_with(mock_parse.return_value)
        mock_save_result.assert_called_once()
        self.assertEqual(
            mock_save_result.call_args.args[1],
            mock_check_monitor.return_value,
        )
        self.assertEqual(
            mock_save_result.call_args.args[0],
            mock_resource.return_value.Table.return_value,
        )
        self.assertIsInstance(mock_save_result.call_args.args[2], int)

    def test_lambda_handler_publishes_alert_on_health_transition(self):
        monitor = {
            "monitor_id": "mon-transition",
            "url": "https://example.com",
            "interval_seconds": 900,
            "is_active": True,
            "next_check_at": 1234567890,
        }
        previous_result = {"is_healthy": True}
        current_result = {
            "monitor_id": "mon-transition",
            "status_code": None,
            "is_healthy": False,
        }
        record = {"messageId": "msg-transition", "body": "valid"}
        topic_arn = "arn:aws:sns:us-east-1:123456789012:alerts"

        with (
            patch.dict(
                "os.environ",
                {
                    "CHECK_RESULTS_TABLE": "TestCheckResults",
                    "ALERT_TOPIC_ARN": topic_arn,
                },
            ),
            patch("uptime_monitor.worker.boto3.resource") as mock_resource,
            patch("uptime_monitor.worker.boto3.client") as mock_client,
            patch(
                "uptime_monitor.worker.parse_monitor_message",
                return_value=monitor,
            ),
            patch(
                "uptime_monitor.worker.get_latest_check_result",
                return_value=previous_result,
            ),
            patch(
                "uptime_monitor.worker.check_monitor",
                return_value=current_result,
            ),
            patch("uptime_monitor.worker.save_check_result") as mock_save,
        ):
            response = lambda_handler({"Records": [record]}, None)

        self.assertEqual(response, {"batchItemFailures": []})
        mock_client.assert_called_once_with("sns")
        sns_client = mock_client.return_value
        sns_client.publish.assert_called_once()
        published = sns_client.publish.call_args.kwargs
        self.assertEqual(published["TopicArn"], topic_arn)
        self.assertEqual(published["Subject"], "Monitor Outage")
        self.assertEqual(
            json.loads(published["Message"]),
            {
                "monitor_id": "mon-transition",
                "transition": "outage",
                "status_code": None,
                "is_healthy": False,
                "checked_at": mock_save.call_args.args[2],
            },
        )
        self.assertEqual(
            mock_save.call_args.args[0],
            mock_resource.return_value.Table.return_value,
        )

    def test_lambda_handler_does_not_publish_when_health_is_unchanged(self):
        monitor = {
            "monitor_id": "mon-unchanged",
            "url": "https://example.com",
            "interval_seconds": 900,
            "is_active": True,
            "next_check_at": 1234567890,
        }
        healthy_result = {
            "monitor_id": "mon-unchanged",
            "status_code": 204,
            "is_healthy": True,
        }
        record = {"messageId": "msg-unchanged", "body": "valid"}

        with (
            patch.dict(
                "os.environ",
                {
                    "CHECK_RESULTS_TABLE": "TestCheckResults",
                    "ALERT_TOPIC_ARN": "arn:aws:sns:us-east-1:123456789012:alerts",
                },
            ),
            patch("uptime_monitor.worker.boto3.resource"),
            patch("uptime_monitor.worker.boto3.client") as mock_client,
            patch(
                "uptime_monitor.worker.parse_monitor_message",
                return_value=monitor,
            ),
            patch(
                "uptime_monitor.worker.get_latest_check_result",
                return_value={"is_healthy": True},
            ),
            patch(
                "uptime_monitor.worker.check_monitor",
                return_value=healthy_result,
            ),
            patch("uptime_monitor.worker.save_check_result") as mock_save,
        ):
            response = lambda_handler({"Records": [record]}, None)

        self.assertEqual(response, {"batchItemFailures": []})
        mock_client.assert_called_once_with("sns")
        mock_client.return_value.publish.assert_not_called()
        mock_save.assert_called_once()

            
if __name__ == '__main__':
    unittest.main()
