# Aphrodite installer runbook

For Loop8 engineers walking a buyer's admin through installing, upgrading and uninstalling
Aphrodite. Everything runs in the **buyer's own AWS account**. There are no Loop8 AWS
resources: the release files come from the GitHub Release and are uploaded by the admin into
a bucket the stack creates in their account.

**External dependencies at deploy time: none.** The release bundle carries Terraform, the
provider mirror, the Lambda code and the built SPA. Deployment makes no calls to Loop8 or to
any account other than the buyer's.

Release assets (GitHub Release for tag `vX.Y.Z`): `installer.yaml`, `aphrodite-X.Y.Z.zip`,
`aphrodite-X.Y.Z.zip.sha256`.

## Prerequisites

1. An AWS account with no existing Aphrodite resources, and an admin who can create
   CloudFormation stacks with IAM roles (the stack needs `CAPABILITY_IAM`).
2. **Bedrock model access** enabled in the stack's region for both models in
   `installer/preflight-models.txt`:
   - `amazon.titan-embed-text-v2:0`
   - `us.anthropic.claude-sonnet-4-6` (cross-region inference profile)

   Enable them in the Bedrock console under Model access. The deploy build checks both before
   running Terraform and fails fast if either is not accessible.
3. **Region.** The stack's region is the deployment region (all data at rest stays there). It
   must offer S3 Vectors and the Bedrock models above. The supported-regions list is pending
   and will be documented from the sandbox end-to-end runs; `ca-central-1` is the default
   target.
4. The three release assets downloaded from the GitHub Release.

## Install

Three steps. Replace `X.Y.Z`, `STACK` and the region.

1. **Create the stack with an empty version.** This creates only the buckets, build project
   and function; nothing is deployed.

   ```sh
   aws cloudformation create-stack --stack-name STACK --region ca-central-1 \
     --template-body file://installer.yaml --capabilities CAPABILITY_IAM \
     --parameters ParameterKey=AphroditeVersion,ParameterValue=""
   aws cloudformation wait stack-create-complete --stack-name STACK --region ca-central-1
   ```

2. **Upload the zip** to the `ArtifactBucketName` stack output, at exactly
   `releases/X.Y.Z/aphrodite-X.Y.Z.zip`:

   ```sh
   BUCKET=$(aws cloudformation describe-stacks --stack-name STACK --region ca-central-1 \
     --query 'Stacks[0].Outputs[?OutputKey==`ArtifactBucketName`].OutputValue' --output text)
   aws s3 cp aphrodite-X.Y.Z.zip "s3://$BUCKET/releases/X.Y.Z/aphrodite-X.Y.Z.zip"
   ```

3. **Update the stack** with `AphroditeVersion` and `BundleSha256` (the 64-hex value in
   `aphrodite-X.Y.Z.zip.sha256`):

   ```sh
   SHA=$(cut -d' ' -f1 aphrodite-X.Y.Z.zip.sha256)
   aws cloudformation update-stack --stack-name STACK --region ca-central-1 \
     --use-previous-template --capabilities CAPABILITY_IAM \
     --parameters ParameterKey=AphroditeVersion,ParameterValue=X.Y.Z \
                  ParameterKey=BundleSha256,ParameterValue="$SHA" \
                  ParameterKey=Environment,UsePreviousValue=true \
                  ParameterKey=SamlMetadataUrl,UsePreviousValue=true \
                  ParameterKey=DeleteData,UsePreviousValue=true
   aws cloudformation wait stack-update-complete --stack-name STACK --region ca-central-1
   ```

   The update starts a CodeBuild run (Bedrock pre-flight, `terraform apply`, SPA publish).
   Allow up to an hour; watch the build logs in the CodeBuild console (project
   `DeployProject`). On success the stack outputs carry `AppUrl`, `DeployedVersion`,
   `CognitoUserPoolId`, `SamlSpEntityId` and `SamlAcsUrl`. If `SamlMetadataUrl` is set, give
   the last two to the buyer's identity provider admin.

If the build fails, the update fails with a reason and a log link, and CloudFormation rolls
back. Fix the cause (see Messages below) and repeat step 3. Only one build runs at a time.

## Upgrade

1. Download the new release assets. Upload the new zip to
   `releases/NEW/aphrodite-NEW.zip` in the artifact bucket (step 2 above).
2. Update the stack with `AphroditeVersion=NEW` and the new `BundleSha256` (step 3).
3. **Keep the old zip in the bucket until the upgrade succeeds.** If the update fails,
   CloudFormation rolls back by re-applying the previous version, which reads the old zip.
   Remove old zips only after `DeployedVersion` shows the new version.

Clearing `AphroditeVersion` is not supported. Downgrades are an update to an older version
whose zip is still in the bucket.

## Uninstall

`DeleteData` defaults to `false`.

**Keep data (default):** `aws cloudformation delete-stack --stack-name STACK`. The build
removes compute and caches (Lambdas, API, schedules, CloudFront, web bucket, Cognito user
pool, connector secrets with their 7-day recovery window, and the other non-record
resources) and **keeps**:

- the outputs bucket (report drafts, AML exports, Certificates of Search)
- the CloudTrail audit bucket
- the DynamoDB `activity` and `jobs` tables
- the KMS key encrypting them

(Exact Terraform addresses: `installer/retained-addresses.txt`.) Their names and ARNs are
recorded in `s3://<StateBucket>/retained-resources.json`; the resources are removed from
Terraform state, not from AWS.

**Delete data (two steps).** CloudFormation deletes using the last saved parameters, so:

1. Update the stack with `DeleteData=true` (other parameters `UsePreviousValue=true`).
2. Delete the stack.

The build then empties and destroys everything it is allowed to. **S3 Object Lock is never
bypassed.** If locked versions remain in the outputs bucket, the build keeps that bucket and
the KMS key and lists them in `retained-resources.json`; they can be removed by the admin
only after the retention period ends.

**Always retained, in every uninstall:** `StateBucket` and `ArtifactBucket`
(`DeletionPolicy: Retain`). Remove them manually when no longer needed, after copying out
anything required (`retained-resources.json`, the zips):

```sh
aws s3 rb "s3://BUCKET" --force   # versioned: first delete all versions and delete markers
```

(A versioned bucket needs every object version and delete marker removed first, for example
via the console's "Empty bucket".) Do not remove `StateBucket` while a stack still exists or
while you intend a reinstall that keeps data.

A failed first install is torn down fully on rollback (`DeleteData=true` semantics); no
customer data exists yet.

## Manual teardown

Message: *Terraform state exists but no version is set.* The stack is being deleted with an
empty `AphroditeVersion`, but `aphrodite/terraform.tfstate` exists in the state bucket (for
example a stack created against a state bucket kept from an earlier install, or a state left
by a build that was stopped partway). The installer will not guess which release to destroy
with, so the delete fails and nothing is removed.

A failed delete leaves the stack in `DELETE_FAILED`, where `update-stack` is not allowed. So
the fix depends on whether you have already tried to delete.

**(a) Before deleting** (you see an empty `AphroditeVersion` but resources exist): update the
stack to set `AphroditeVersion` and `BundleSha256` to the release last deployed (its zip must
still be in the artifact bucket), then delete the stack. Nothing else is needed.

**(b) The delete already failed** (stack is `DELETE_FAILED`):

1. Tear down manually. Unzip the deployed release locally and run `terraform init` against
   the state bucket (`-backend-config="bucket=<StateBucket>"
   -backend-config="key=aphrodite/terraform.tfstate" -backend-config="region=<region>"`),
   using the bundled `bin/terraform`, then `terraform destroy` with credentials for the
   buyer's account. Empty any non-Object-Lock S3 buckets first; they are not force-destroyed.
   Object-Lock-held versions are never bypassed.
2. Retry the delete skipping the custom resource:
   `aws cloudformation delete-stack --stack-name STACK --region REGION --retain-resources Deployment`.
   This is required, not optional: `terraform destroy` leaves an empty
   `aphrodite/terraform.tfstate` object in the state bucket, so the installer would still see
   "state exists" with an empty version and fail the same way. (Deleting that object would
   also work, but retaining `Deployment` avoids touching state.)

## Unlock a stale Terraform lock

If a build was killed mid-run, the next build can fail on a state lock. Take the lock ID from
the error in the build log, then start the build project with `ACTION=unlock`:

```sh
aws codebuild start-build --project-name PROJECT --region ca-central-1 \
  --environment-variables-override \
    name=ACTION,value=unlock,type=PLAINTEXT \
    name=LOCK_ID,value=LOCK-ID-FROM-LOG,type=PLAINTEXT \
    name=VERSION,value=X.Y.Z,type=PLAINTEXT \
    name=BUNDLE_SHA256,value=SHA,type=PLAINTEXT
```

Find `PROJECT` with
`aws cloudformation describe-stack-resource --stack-name STACK --region REGION --logical-resource-id DeployProject --query StackResourceDetail.PhysicalResourceId --output text`.
`VERSION` and
`BUNDLE_SHA256` must be those of an uploaded release, since the build downloads and verifies
the bundle first. Confirm no other build is running before unlocking.

## Reinstall after a data-keeping uninstall

Out of automation in v1. The kept resources clash by name with a fresh install, so they must
be imported into Terraform state before the first apply. Loop8-assisted:

1. Read `retained-resources.json` (address, id, arn, bucket, name) from the OLD, retained
   state bucket of the uninstalled stack: `s3://<OldStateBucket>/retained-resources.json`.
2. Create a new stack and run steps 1 and 2 of Install, but **do not** do step 3 yet.
3. Using the release bundle and a Terraform backend pointing at the NEW stack's
   `StateBucket` (init as in Manual teardown, with the new bucket name), run `terraform import <address> <id>` for each address in
   `installer/retained-addresses.txt` (use the recorded `id`, which is the bucket or table
   name, or the KMS key ID; aliases import by `alias/<name>`). Then `terraform plan` and
   confirm it proposes no replacement of those resources.
4. Run Install step 3.

## Custom domain

Not a stack parameter. The CloudFront URL in `AppUrl` always works. A custom domain is a
manual, Loop8-assisted follow-up using the Terraform variables `custom_domain` and
`enable_custom_domain`: apply once with the domain set to mint the ACM certificate (in
`us-east-1`) and read the DNS validation records from the `acm_validation_records` output;
the buyer adds the CNAME at their DNS provider; then apply again with
`enable_custom_domain=true`. Run these with credentials in the buyer's account against the
same state bucket as above, then re-publish the SPA config (the next release deploy does
this).

## Messages you may see

| Message | Cause | What to do |
|---|---|---|
| `Terraform state exists but no version is set — see installer/README.md 'Manual teardown'.` | Stack delete with empty `AphroditeVersion` while state exists. | See Manual teardown. |
| `Clearing AphroditeVersion is not supported; delete the stack to uninstall.` | Update set the version back to empty after a deploy. | Keep a version; to uninstall, delete the stack (see Uninstall). |
| `Release X has not been uploaded to <bucket> (expected key releases/X/aphrodite-X.zip)` | Step 3 before step 2, or wrong key or version. | Upload the zip to that exact key and retry the update. |
| `BundleSha256 is required` | Version set without a checksum. | Pass the value from the `.sha256` asset. |
| `a deployment is already in progress` | Another build is running. | Wait for it (CodeBuild console), then retry. |
| `Uploaded release does not match the expected checksum — re-upload the file from the GitHub Release.` | Zip corrupted, wrong file, or wrong `BundleSha256`. | Re-download, compare the sha256, re-upload, retry. |
| `Bedrock model <id> is not accessible in <region>. Enable it in the Bedrock console → Model access, then retry the stack update.` | Pre-flight failed. | Enable that model in the region (Prerequisites), retry the update. |
| `Deployment failed — logs: <link>` | Build failed without a specific message. | Open the CodeBuild log link; fix and retry. |
| `Deployment failed at step <step>` | `deploy.sh` failed in that step. | Same: read the log for that step. |
| `None of the resources to keep is in Terraform state and none was kept earlier; stopping before terraform destroy. Check installer/retained-addresses.txt against the Terraform modules.` | Retained-address list no longer matches the Terraform code. Nothing was destroyed. | Escalate to Loop8 engineering (bundle defect). |
| `Could not read s3://<StateBucket>/retained-resources.json` | Unexpected S3 error reading the record; the build refuses to overwrite it. | Check bucket permissions and retry. |
| `Installer bundle is missing <file>` / `has no entries` | Damaged bundle. | Re-download and re-upload the release. |
| `Installer misconfigured: <VAR> is not set` | Missing build environment variable, e.g. an unlock run without `LOCK_ID`. | Supply the variable (see Unlock). |
| `Unknown ACTION '<x>' (expected apply, destroy or unlock)` | Bad `ACTION` override. | Use `apply`, `destroy` or `unlock`. |
| `Object Lock still holds versions in s3://<bucket>; keeping it and its KMS key` (build log) | `DeleteData=true`, locked versions remain. | Expected. Never bypassed; see Uninstall. |
| `s3://<bucket> is still not empty after 5 passes` (build log) | Versions the build could not remove. | Retry the delete; if it persists, check for Object Lock or a writer. |
