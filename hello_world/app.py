import json
import os
import time
from urllib.parse import urlsplit
import uuid
import boto3

def lambda_handler(event, context):
    """Sample pure Lambda function

    Parameters
    ----------
    event: dict, required
        API Gateway Lambda Proxy Input Format

        Event doc: https://docs.aws.amazon.com/apigateway/latest/developerguide/set-up-lambda-proxy-integrations.html#api-gateway-simple-proxy-for-lambda-input-format

    context: object, required
        Lambda Context runtime methods and attributes

        Context doc: https://docs.aws.amazon.com/lambda/latest/dg/python-context-object.html

    Returns
    ------
    API Gateway Lambda Proxy Output Format: dict

        Return doc: https://docs.aws.amazon.com/apigateway/latest/developerguide/set-up-lambda-proxy-integrations.html
    """
    body = event.get("body")

    if not body:
        return {
            "statusCode": 400,
            "body": json.dumps({
                "message": "Request body is empty",
            }),
        }
            
    try:
        parsed_body = json.loads(body)
    except json.JSONDecodeError:
        return {
            "statusCode": 400,
            "body": json.dumps({
                "message": "Invalid JSON format",
            }),
        }
    
    if not isinstance(parsed_body, dict):
            return {
                'statusCode': 400,
                'headers': {'Content-Type': 'application/json'},
                'body': json.dumps({'error': 'JSON payload must be an object'})
            }

    url = parsed_body.get("url")
    if not url or not isinstance(url, str) or not url.strip() or any(character.isspace() for character in url):
        return {
            "statusCode": 400,
            "body": json.dumps({
                "message": "URL is missing or invalid",
            }),
        }

    try:
        parsed_url = urlsplit(url)
    except ValueError:
        return {
            "statusCode": 400,
            "body": json.dumps({"message": "URL is malformed"}),
        }  

    if parsed_url.scheme not in ("http", "https"):
        return {
            "statusCode": 400,
            "body": json.dumps({"message": "URL must use http or https"}),
        }

    if not parsed_url.hostname:
        return {
            "statusCode": 400,
            "body": json.dumps({"message": "URL must include a hostname"}),
        }
    
    if parsed_url.username or parsed_url.password:
        return {
            "statusCode": 400,
            "body": json.dumps({"message": "URL must not contain credentials"}),
        }
    
    interval_seconds = parsed_body.get("interval_seconds")
    if isinstance(interval_seconds, bool) or not isinstance(interval_seconds, int):
        return {
            "statusCode": 400,
            "body": json.dumps({
                "message": "Interval seconds must be an integer",
            }),
        }
    
    if interval_seconds < 900:
        return {
            "statusCode": 400,
            "body": json.dumps({
                "message": "Interval seconds must be at least 900",
            }),
        }

    monitor_id = str(uuid.uuid4())
    current_time = int(time.time())
    next_check_at = current_time + interval_seconds

    record = {
        "monitor_id": monitor_id,
        "url": url,
        "interval_seconds": interval_seconds,
        "is_active": True,
        "next_check_at": next_check_at
    }

    table = boto3.resource("dynamodb").Table(os.environ["MONITORS_TABLE"])
    table.put_item(Item=record)
    

    return {
        "statusCode": 201,
        "body": json.dumps({
            "message": "URL and interval seconds received",
            "url": url,
            "interval_seconds": interval_seconds,
            "monitor_id": monitor_id
        }),
    }
