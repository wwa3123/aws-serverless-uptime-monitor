# AWS Serverless Uptime and Webhook Monitor

A Python/Boto3 project for creating endpoint monitors, scheduling checks, processing them asynchronously, keeping check history, and notifying subscribers when health changes.

## Architecture

- API Gateway `POST /monitors` uses AWS_IAM authorization. A DynamoDB transaction enforces the hard limit of 10 total monitor records.
- API Gateway `GET /status` is public and read-only. It returns only hostnames, health states, and last-check timestamps.
- A five-minute EventBridge schedule can dispatch due monitors to SQS. It is disabled by default.
- The SQS worker checks URLs, records results in DynamoDB with a 30-day TTL, and sends SNS alerts on health-state transitions. Failed SQS records retry and can reach the dead-letter queue.
- A private S3 bucket stores the dashboard page. CloudFront serves the page and proxies `/status` to API Gateway on the same origin.

The worker validates resolved IP addresses before connecting and does not follow redirects. This helps protect the checker from requests to private or otherwise unsafe network addresses.

## Cost and deployment safety

Do not treat AWS Free Tier as a spending cap. API Gateway, Lambda, DynamoDB, SQS, EventBridge, SNS, S3, CloudFront, data transfer, and CloudWatch Logs can incur charges. Public endpoints can also be accessed by anyone. The API throttle and 10-monitor limit reduce exposure but do not guarantee a maximum bill.

Develop and test with LocalStack first. The dispatcher schedule defaults to disabled, and the local SNS email placeholder does not deliver real email. Do not deploy to AWS until you have reviewed current prices, billing controls, public exposure, and teardown steps.

CloudFront support depends on the LocalStack license. A health response that lists CloudFront as available does not guarantee that the service is licensed. If the stack output is `https://unknown`, LocalStack did not create a usable distribution; the uploaded S3 object alone does not mean the dashboard is publicly reachable.

## Local development

Prerequisites: Python, AWS SAM CLI, Docker, and LocalStack with the S3 and CloudFront services available.

Run unit tests:

```powershell
python -m unittest discover -s uptime_monitor -p "test_*.py"
```

For a no-cost browser preview with clearly labeled synthetic status data:

```powershell
python .\site\preview_server.py
```

Open `http://127.0.0.1:8000`. The preview server binds only to the loopback interface and does not contact AWS or LocalStack.

Validate and build:

```powershell
sam validate --lint --region us-east-1 --template-file .\template.yaml
sam build --template-file .\template.yaml
```

Deploy to LocalStack only. Keep the endpoint wrapper in the command; a plain `sam deploy` targets AWS:

```powershell
lstk --endpoint-url http://localhost:4566 sam deploy `
  --template-file .aws-sam\build\template.yaml `
  --stack-name urlhealthcheck `
  --capabilities CAPABILITY_IAM `
  --resolve-s3 `
  --parameter-overrides AlertEmail=test@example.com EnableDispatchSchedule=false
```

Upload the static page to the private bucket created by the stack:

```powershell
$bucket = lstk --endpoint-url http://localhost:4566 aws cloudformation describe-stacks `
  --stack-name urlhealthcheck `
  --query "Stacks[0].Outputs[?OutputKey=='DashboardBucketName'].OutputValue | [0]" `
  --output text

lstk --endpoint-url http://localhost:4566 aws s3 cp .\site\index.html "s3://$bucket/index.html" `
  --content-type text/html `
  --cache-control "max-age=60"
```

Use the `DashboardUrl` stack output to open the dashboard. The page fetches `/status` through CloudFront; the distribution routes that path to the API Gateway `Prod` stage without requiring browser CORS configuration. The API's `POST /monitors` endpoint remains IAM-protected and is not exposed through the public dashboard.

To confirm the API response independently, invoke the local API Gateway stage at `/Prod/status`; LocalStack also exposes the REST API under its `restapis` URL format. The response should contain a top-level `monitors` array.

## Data and limits

- Minimum monitoring interval: 300 seconds.
- Maximum total monitor records: 10.
- Check history TTL: 30 days.
- Public dashboard fields: hostname, health state, and last checked time; full URL paths and query strings are not returned.
- Alert emails require a real SNS subscription confirmation on AWS. `test@example.com` is only a LocalStack placeholder.

## Cleanup

For LocalStack, empty the dashboard S3 bucket before removing the `urlhealthcheck` stack; CloudFormation cannot delete a non-empty bucket. Confirm you no longer need monitor data before deleting the stack. For AWS, review all resources and costs before deleting the stack and any retained resources.
