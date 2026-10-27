from boto3.dynamodb.conditions import Attr
import json
from botocore.exceptions import ClientError

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