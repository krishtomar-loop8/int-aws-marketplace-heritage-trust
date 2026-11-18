"""Placeholder Lambda handler.

Lets `terraform apply` create the loop8-landing-api function (api.tf) before
Task 3 (loop8-landing/backend/handler.py) exists. Once Task 3 lands, swap
api.tf's `data "archive_file" "lambda_placeholder"` `source_dir` to point at
the real backend directory (or have CI build/upload the real package) -
don't leave the stack pointed at this stub.
"""


def handler(event, context):
    return {
        "statusCode": 501,
        "headers": {"Content-Type": "application/json"},
        "body": '{"error": "not implemented"}',
    }
