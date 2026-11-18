"""Placeholder Lambda handler.

Lets `terraform apply` create the loop8-landing-api function (api.tf) before
Task 3 (loop8-landing/backend/handler.py) existed. Task 3 has since landed;
this stub is now overwritten in place by loop8-landing-deploy.yml's "Build
Lambda package" CI step (`cp ../backend/handler.py lambda_placeholder/handler.py`)
before every `terraform apply`, so the deployed function always runs the real
handler, not this file.
"""


def handler(event, context):
    return {
        "statusCode": 501,
        "headers": {"Content-Type": "application/json"},
        "body": '{"error": "not implemented"}',
    }
