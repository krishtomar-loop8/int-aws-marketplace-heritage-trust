#!/usr/bin/env bash
# Classifies a failed `terraform apply`'s captured output against a small set
# of recognized AWS deploy-time failure signatures, so the CloudFormation
# failure reason names the real cause instead of a generic message (same
# idea as deploy.sh's Bedrock pre-flight check, generalized to terraform
# apply). Sourced by deploy.sh; never executed standalone in production.

classify_apply_failure() {
  local output_file=$1 text
  text=$(cat "$output_file")

  if [[ $text == *LimitExceededException* || $text == *ServiceQuotaExceededException* ]]; then
    echo "This AWS account has hit a service quota/limit while deploying (LimitExceededException/ServiceQuotaExceededException). Request a quota increase for the affected service in the Service Quotas console, then retry the stack update."
    return
  fi
  if [[ $text == *"account is currently blocked"* ]]; then
    echo "AWS has placed a billing/verification hold on this account (\"account is currently blocked\"). Resolve this with AWS Billing support, then retry the stack update."
    return
  fi
  if [[ $text == *AccessDenied* || $text == *UnauthorizedOperation* || $text == *"is not authorized to perform"* ]]; then
    echo "The deploying IAM role/user lacks a permission terraform needs (AccessDenied/UnauthorizedOperation). Check the IAM policy attached to the role running this deployment, then retry the stack update."
    return
  fi
}
