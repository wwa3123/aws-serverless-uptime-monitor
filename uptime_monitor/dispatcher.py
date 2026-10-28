from boto3.dynamodb.conditions import Attr
import json
import os
from botocore.exceptions import ClientError
import boto3
import time

def find_due_monitors(table, now):
    due_monitors = []
    scan_kwargs = {
        "FilterExpression": (
            Attr("is_active").eq(True)
            & Attr("next_check_at").lte(now)
        )
    }

    while True:
        response = table.scan(**scan_kwargs)
        due_monitors.extend(response["Items"])
        if "LastEvaluatedKey" not in response:
            break
        scan_kwargs["ExclusiveStartKey"] = response["LastEvaluatedKey"]

    return due_monitors

def serialize_monitor(monitor):
    monitor_json = json.dumps({
        "monitor_id": monitor.get("monitor_id"),
        "url": monitor.get("url"),
        "interval_seconds": int(monitor.get("interval_seconds")),
        "is_active": monitor.get("is_active"),
        "next_check_at": int(monitor.get("next_check_at"))
    })
    return monitor_json

def claim_monitor_for_dispatch(table, monitor, now):
    try:
        table.update_item(
            Key={"monitor_id": monitor["monitor_id"]},
            UpdateExpression="SET next_check_at = :new_next_check_at",
            ConditionExpression=Attr("is_active").eq(True) & Attr("next_check_at").lte(now),
            ExpressionAttributeValues={
                ":new_next_check_at": now + int(monitor["interval_seconds"])
            },
        )
    except ClientError as e:
        if e.response['Error']['Code'] == 'ConditionalCheckFailedException':
            return False
        raise
    return True

def send_monitor_to_queue(sqs_client, queue_url, monitor):
    message_body = serialize_monitor(monitor)
    return sqs_client.send_message(
        QueueUrl=queue_url,
        MessageBody=message_body
    )

def dispatch_monitor(table, sqs_client, queue_url, monitor, now):
    if claim_monitor_for_dispatch(table, monitor, now):
        send_monitor_to_queue(sqs_client, queue_url, monitor)
        return True
    return False

def lambda_handler(event, context):
    dynamodb = boto3.resource("dynamodb")
    sqs_client = boto3.client("sqs")

    table = dynamodb.Table(os.environ["MONITORS_TABLE"])
    queue_url = os.environ["CHECK_QUEUE_URL"]
    now = int(time.time())

    due_monitors = find_due_monitors(table, now)
    queued_count = 0

    for monitor in due_monitors:
        if dispatch_monitor(table, sqs_client, queue_url, monitor, now):
            queued_count += 1

    return {
        "found": len(due_monitors),
        "queued": queued_count,
    }