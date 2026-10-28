import json
import os
from urllib.parse import urlsplit

import boto3

if __package__:
    from .worker import get_latest_check_result
else:
    from worker import get_latest_check_result


def build_public_monitor_summary(monitor, latest_result):
    hostname = urlsplit(monitor["url"]).hostname
    if hostname is None:
        raise ValueError("Monitor URL must include a hostname")

    if latest_result is None:
        health_state = "unknown"
        last_checked = None
    else:
        health_state = (
            "healthy" if latest_result["is_healthy"] else "unhealthy"
        )
        checked_at = latest_result["checked_at"].split("#", 1)[0]
        last_checked = int(checked_at)

    return {
        "hostname": hostname,
        "health_state": health_state,
        "last_checked": last_checked,
    }


def lambda_handler(event, context):
    dynamodb = boto3.resource("dynamodb")
    monitors_table = dynamodb.Table(os.environ["MONITORS_TABLE"])
    results_table = dynamodb.Table(os.environ["CHECK_RESULTS_TABLE"])

    monitors = []
    scan_kwargs = {}
    while True:
        response = monitors_table.scan(**scan_kwargs)
        monitors.extend(response.get("Items", []))
        if "LastEvaluatedKey" not in response:
            break
        scan_kwargs["ExclusiveStartKey"] = response["LastEvaluatedKey"]

    summaries = []
    for monitor in monitors:
        latest_result = get_latest_check_result(
            results_table,
            monitor["monitor_id"],
        )
        summaries.append(
            build_public_monitor_summary(monitor, latest_result)
        )

    return {
        "statusCode": 200,
        "headers": {"Content-Type": "application/json"},
        "body": json.dumps({"monitors": summaries}),
    }
