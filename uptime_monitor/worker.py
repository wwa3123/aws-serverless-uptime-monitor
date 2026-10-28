import http.client
import json
import os
import socket
import ssl
import sys
import time
from urllib.parse import urlparse
import ipaddress
import uuid
import traceback
import boto3
from boto3.dynamodb.conditions import Key

MIN_INTERVAL_SECONDS = 300


def parse_monitor_message(record):
    if not isinstance(record, dict):
        raise ValueError("Record must be a dictionary")

    message_body = record.get("body")
    if not isinstance(message_body, str) or not message_body:
        raise ValueError("Invalid message body")
    
    body = json.loads(message_body)

    if not isinstance(body, dict):
        raise ValueError("Invalid message body format")

    monitor_id = body.get("monitor_id")
    url = body.get("url")
    interval_seconds = body.get("interval_seconds")
    is_active = body.get("is_active")
    next_check_at = body.get("next_check_at")

    if not isinstance(monitor_id, str) or not monitor_id.strip():
        raise ValueError("Invalid monitor_id")

    if not isinstance(url, str) or not url.strip():
        raise ValueError("Invalid URL")

    if (
        isinstance(interval_seconds, bool)
        or not isinstance(interval_seconds, int)
        or interval_seconds < MIN_INTERVAL_SECONDS
    ):
        raise ValueError("Invalid interval_seconds")

    if not isinstance(is_active, bool):
        raise ValueError("Invalid is_active")

    if isinstance(next_check_at, bool) or not isinstance(next_check_at, int):
        raise ValueError("Invalid next_check_at")

    validate_url_syntax(url)
    
    return body

def validate_url_syntax(url):  
    if not url or not isinstance(url, str) or not url.strip() or any(character.isspace() for character in url):
        raise ValueError("Invalid URL")

    parsed_url = urlparse(url)
    if parsed_url.scheme not in ("http", "https"):
        raise ValueError("Invalid URL scheme")

    if not parsed_url.hostname:
        raise ValueError("Invalid URL hostname")

    if parsed_url.username or parsed_url.password:
        raise ValueError("URL must not contain username or password")

    if parsed_url.port is not None and (parsed_url.port <= 0 or parsed_url.port > 65535):
        raise ValueError("Invalid URL port")

def is_public_ip_address(address):
    try:
        return ipaddress.ip_address(address).is_global
    except ValueError:
        return False

def resolve_public_addresses(hostname, port):
    addr_info = socket.getaddrinfo(
        hostname,
        port,
        type=socket.SOCK_STREAM,
    )

    addresses = list(dict.fromkeys(entry[4][0] for entry in addr_info))
    if not addresses:
        raise ValueError("Hostname resolved to no addresses")

    if any(not is_public_ip_address(address) for address in addresses):
        raise ValueError("Hostname resolved to a non-public address")

    return addresses

def open_pinned_connection(hostname, port, address, use_tls, timeout=5):
    if not is_public_ip_address(address):
        raise ValueError("Address is not a public IP")

    sock = socket.create_connection((address, port), timeout=timeout)

    if use_tls:
        try:
            context = ssl.create_default_context()
            sock = context.wrap_socket(sock, server_hostname=hostname)
        except Exception:
            sock.close()
            raise

    return sock

def get_http_status(url, address, timeout=5):
    parsed_url = urlparse(url)
    scheme = parsed_url.scheme
    port = parsed_url.port
    if port is None:
        if scheme == "http":
            port = 80
        elif scheme == "https":
            port = 443
        else:
            raise ValueError("Unsupported URL scheme")
    use_tls = scheme == "https"

    sock = open_pinned_connection(parsed_url.hostname, port, address, use_tls, timeout=timeout)
    try:
        path = parsed_url.path or "/"
        if parsed_url.query:
            path += "?" + parsed_url.query
        request = f"GET {path} HTTP/1.1\r\nHost: {parsed_url.hostname}\r\nConnection: close\r\n\r\n"
        sock.sendall(request.encode("utf-8"))

        response = http.client.HTTPResponse(sock)
        response.begin()
        return response.status
    finally:
        sock.close()

def check_monitor(monitor):
    url = monitor.get("url")
    parsed_url = urlparse(url)
    port = parsed_url.port
    if port is None:
        if parsed_url.scheme == "http":
            port = 80
        elif parsed_url.scheme == "https":
            port = 443

    try:
        addresses = resolve_public_addresses(parsed_url.hostname, port)
        address = addresses[0]
        status_code = get_http_status(url, address)
    except (OSError, http.client.HTTPException):
        return {
            "monitor_id": monitor["monitor_id"],
            "status_code": None,
            "is_healthy": False,
            "error_category": "network_error",
        }

    return {
        "monitor_id": monitor["monitor_id"],
        "status_code": status_code,
        "is_healthy": 200 <= status_code < 300,
    }

def build_check_result_item(result, checked_at):
    item = {
        "monitor_id": result["monitor_id"],
        "status_code": result["status_code"],
        "is_healthy": result["is_healthy"],
        "checked_at": f"{checked_at:010d}#{uuid.uuid4()}",
        "expires_at": checked_at + 30 * 24 * 60 * 60,
    }
    if "error_category" in result:
        item["error_category"] = result["error_category"]
    
    return item

def save_check_result(table, result, checked_at):
    item = build_check_result_item(result, checked_at)
    table.put_item(Item=item)
    
    return item

def get_latest_check_result(table, monitor_id):
    response = table.query(
        KeyConditionExpression=Key("monitor_id").eq(monitor_id),
        ScanIndexForward=False,
        Limit=1,
    )
    items = response.get("Items", [])
    return items[0] if items else None  

def get_health_status(previous_result, current_result):
    current_is_healthy = current_result["is_healthy"]

    if previous_result is None:
        return None if current_is_healthy else "outage"

    previous_is_healthy = previous_result["is_healthy"]
    if previous_is_healthy and not current_is_healthy:
        return "outage"
    if not previous_is_healthy and current_is_healthy:
        return "recovery"

    return None

def publish_alert(sns_client, topic_arn, monitor_id, transition, result, checked_at):
    message = {
        "monitor_id": monitor_id,
        "transition": transition,
        "status_code": result["status_code"],
        "is_healthy": result["is_healthy"],
        "checked_at": checked_at,
    }
    subject = f"Monitor {transition.title()}"
    sns_client.publish(
        TopicArn=topic_arn,
        Subject=subject,
        Message=json.dumps(message),
    )

def process_monitor(monitor, table, sns_client, topic_arn):
    previous_result = get_latest_check_result(table, monitor["monitor_id"])
    result = check_monitor(monitor)
    checked_at = int(time.time())

    transition = get_health_status(previous_result, result)
    if transition:
        publish_alert(
            sns_client,
            topic_arn,
            monitor["monitor_id"],
            transition,
            result,
            checked_at,
        )

    save_check_result(table, result, checked_at)

def lambda_handler(event, context):
    table = boto3.resource("dynamodb").Table(os.environ["CHECK_RESULTS_TABLE"])
    batch_item_failures = []
    sns_client = boto3.client("sns")
    topic_arn = os.environ["ALERT_TOPIC_ARN"]

    for record in event["Records"]:
        message_id = record["messageId"]

        try:
            monitor = parse_monitor_message(record)

            if not monitor["is_active"]:
                continue

            process_monitor(monitor, table, sns_client, topic_arn)

        except Exception:
            traceback.print_exc(file=sys.stderr)    
            print(f"Error processing message {message_id}", file=sys.stderr)
            batch_item_failures.append({"itemIdentifier": message_id})

    return {"batchItemFailures": batch_item_failures}