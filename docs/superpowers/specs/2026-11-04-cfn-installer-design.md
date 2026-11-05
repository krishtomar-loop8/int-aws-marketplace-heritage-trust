# Aphrodite CloudFormation Installer — Design

**Date:** 2026-11-04 · **Status:** Draft for review (rev 2) · **Owner:** Krish Tomar (Loop8)
**Relates to:** `MARKETPLACE_READINESS_PLAN.md`, Phase 4 (packaging & deployment mechanism)

**Rev 2 change:** the product uses **no Loop8 AWS resources**. Everything the product needs,
including the release files, lives in the buyer's AWS account.

## 1. Purpose

Give a buyer one CloudFormation stack that takes an empty AWS account to a working Aphrodite
deployment. The same stack upgrades Aphrodite in place and uninstalls it safely. This replaces
two things for buyers: the manual `terraform/bootstrap/bootstrap.yaml` step and the GitHub
Actions deploy pipeline.

### Success criteria

1. A single CloudFormation stack in an empty account ends with a working app URL in its
   Outputs. Getting there takes three steps:
   - **Create** the stack. This sets up the installer only.
   - **Upload** the release zip to the stack's artifacts bucket.
   - **Update** the stack with the version, which runs the deploy.
2. Updating `AphroditeVersion` upgrades in place against the same Terraform state, without
   touching customer data.
3. Every failure surfaces as a CloudFormation `FAILED` status with a readable reason within
   minutes. There are no silent half-deploys and no hour-long hangs, except the last-resort
   timeout backstop.
4. Uninstalling keeps records and audit data by default. Deleting data requires two
   deliberate steps.
5. No AWS resource outside the buyer's account is used at install time or at run time.

## 2. Decisions and context

| Decision | Source |
|---|---|
| **No Loop8 AWS resources.** The product runs, stores data, and keeps its release files entirely in the buyer's AWS account. | Krish |
| Release files are **uploaded into an artifacts bucket in the buyer's account**, created by the stack. The Loop8 engineer uploads them during assisted setup. | Krish |
| v1 launch is **Loop8-assisted**: a Loop8 engineer walks the buyer's admin through the launch in the buyer's account. | Krish |
| GitHub Actions keeps deploying Loop8's own dev/test account; it is not part of the product. The existing pilot deployment is left as-is; migrating it is a separate, later task. | Krish |
| Approach: **custom resource + CodeBuild + EventBridge completion callback**. Rejected: "fire-and-track" (stack status would not reflect deploy health) and a native-CloudFormation rewrite of the Terraform (~13 modules, two IaC codebases). | Krish (approved) |
| The installer deploys a **pre-built, versioned release bundle**. It never builds from source in the buyer's account. | Design |
| v1 serves the app on the **CloudFront default domain only**. Custom domain remains a documented manual follow-up, because the current flow needs a human DNS step between two applies. | Design |
| **Marketplace listing type is open again.** A SaaS listing requires a seller-managed control plane, which the no-Loop8-resources rule very likely excludes. AMI-based and container product types, or private offers, are the remaining options. This must be confirmed with AWS Marketplace Seller Operations. The installer design does not depend on the answer. | Research, 2026-11-04 |

### Non-goals (v1)

- Per-customer business configuration (readiness plan Phase 1).
- Connector and module toggles (Phase 2).
- Onboarding UI and OAuth callbacks (Phase 3).
- Billing and entitlement.
- Choosing the Marketplace listing type.
- Automated custom domain.
- Automated re-adoption of retained resources after an uninstall.
- Migrating the existing pilot.

## 3. Architecture overview

```
Loop8 (GitHub only, no AWS)                Buyer account (stack region)
───────────────────────────                ─────────────────────────────
GitHub Actions release.yml (on tag vX.Y.Z)
  └─► GitHub Release assets:
        installer.yaml           ── admin launches ──► CloudFormation stack "aphrodite"
        aphrodite-<ver>.zip      ── engineer uploads ─► ArtifactBucket/releases/<ver>/
        aphrodite-<ver>.zip.sha256                       │
                                                         ├─ StateBucket (Retain)
                                                         ├─ ArtifactBucket (Retain)
                                                         ├─ DeployRole
                                                         ├─ DeployProject (CodeBuild)
                                                         ├─ InstallerFunction (Lambda, inline code)
                                                         ├─ BuildFinishedRule (EventBridge)
                                                         └─ Deployment (Custom::AphroditeDeployment)
                                                                │ start build
                                                                ▼
                                                         CodeBuild: verify bundle → preflight →
                                                         terraform init/plan/apply → publish SPA →
                                                         write outputs to SSM
                                                                │ build finished event
                                                                ▼
                                                         InstallerFunction → reply to CloudFormation
```

## 4. Release bundle

**Builder.** A new workflow, `.github/workflows/release.yml`, triggered by tags matching
`v*.*.*`.

- It runs entirely on GitHub, with no AWS credentials.
- It reuses the existing Lambda packaging step unchanged (wheels pinned to
  `manylinux2014_x86_64`, `cp313`) and runs `npm ci && npm run build` for the SPA.
- `config.json` is **not** baked in; it is written at deploy time from the buyer's Terraform
  outputs.
- It attaches three assets to the GitHub Release: `installer.yaml`, `aphrodite-<version>.zip`
  and `aphrodite-<version>.zip.sha256`.
- The existing `terraform.yml` workflow is unchanged.

**Contents of `aphrodite-<version>.zip`:**

| Path | Contents |
|---|---|
| `terraform/` | Root and modules, with `build/lambda/` pre-populated so the existing `archive_file` data sources work without `pip` in the buyer's account. |
| `frontend/dist/` | The built SPA, without `config.json`. |
| `installer/deploy.sh` | The deploy script CodeBuild runs (see §6). |
| `installer/retained-addresses.txt` | Terraform addresses kept on a data-keeping uninstall (see §7). |
| `bin/terraform` | A pinned Terraform binary (linux_amd64). Release builds stop using `latest`. |
| `providers/` | A filesystem mirror of the pinned `hashicorp/aws` provider, used through a CLI config file, so `terraform init` needs no internet access. |
| `manifest.json` | Version, git commit SHA, and a SHA-256 for every file. |

**Delivery into the buyer's account.** During assisted setup, the Loop8 engineer uploads
`aphrodite-<version>.zip` to `s3://<ArtifactBucket>/releases/<version>/aphrodite-<version>.zip`.
The engineer uses the buyer's console or CLI session. The upload is a documented runbook step.

**Integrity.** The admin passes the published SHA-256 as the `BundleSha256` stack parameter.
The CodeBuild run checks the zip against it, then checks each file against `manifest.json`,
before using anything.

**Template generation.** `release.yml` renders `installer.yaml` by inlining
`installer/function/handler.py` into the Lambda's `Code.ZipFile`, then runs `cfn-lint` on the
result.

## 5. Installer template (`installer.yaml`)

### Parameters

| Parameter | Type / default | Purpose |
|---|---|---|
| `AphroditeVersion` | String, default empty, pattern: empty or semver | Release to deploy. **Empty means install the installer only, deploy nothing.** Changing it later means an upgrade. |
| `BundleSha256` | String, default empty, pattern: empty or 64 hex chars | SHA-256 of the uploaded zip. Required whenever `AphroditeVersion` is set. |
| `Environment` | String, default `prod` | Passed to `TF_VAR_environment`. |
| `SamlMetadataUrl` | String, default empty | Passed to `TF_VAR_saml_metadata_url` (existing Entra SSO switch). |
| `DeleteData` | `true`/`false`, default `false` | Uninstall behaviour (see §7). |

The deployment region is the stack's region (`AWS::Region`), passed as `TF_VAR_aws_region`.

### Resources

| Logical ID | Type | Notes |
|---|---|---|
| `StateBucket` | `AWS::S3::Bucket` | Terraform state with S3 native locking. Same hardening as the bootstrap bucket: versioned, SSE, TLS-only policy, public access blocked. `DeletionPolicy: Retain`. Also holds `retained-resources.json`. |
| `ArtifactBucket` | `AWS::S3::Bucket` | Release zips under `releases/<version>/`. Same hardening as `StateBucket`. `DeletionPolicy: Retain`; it holds code only, and is listed in the uninstall runbook for manual removal. |
| `DeployRole` | `AWS::IAM::Role` | Assumed by CodeBuild. Reuses the curated policy from `bootstrap.yaml`: the same service list, with IAM limited to `/aphrodite/` paths. Adds: `s3:GetObject` on `ArtifactBucket/releases/*`; `ssm:PutParameter` and `ssm:GetParameter*` on `/aphrodite/installer/*`; CloudWatch Logs for the build. |
| `DeployProject` | `AWS::CodeBuild::Project` | Standard Amazon Linux image, `NO_SOURCE`, 55-minute timeout. A small inline buildspec copies the zip from `ArtifactBucket`, verifies it against `BundleSha256` and `manifest.json`, then runs `installer/deploy.sh`. |
| `InstallerFunction` | `AWS::Lambda::Function` | Python 3.13, stdlib + boto3 only, code inlined. It handles both CloudFormation custom-resource events and CodeBuild state-change events. Role: `codebuild:StartBuild`, `codebuild:BatchGetBuilds` and `codebuild:ListBuildsForProject` on `DeployProject`; `s3:HeadObject` (via `s3:GetObject`) on `ArtifactBucket/releases/*`; `ssm:GetParameter*` on `/aphrodite/installer/*`; `cloudformation:DescribeStacks` on this stack; logs. |
| `BuildFinishedRule` | `AWS::Events::Rule` | Source `aws.codebuild`, detail-type `CodeBuild Build State Change`, `project-name` = `DeployProject`, any terminal status. Target: `InstallerFunction`, with an EventBridge retry policy. |
| `Deployment` | `Custom::AphroditeDeployment` | `ServiceToken` = `InstallerFunction`, `ServiceTimeout: 3600`. Properties: `Version`, `BundleSha256`, `Environment`, `SamlMetadataUrl`, `DeleteData`. |

### Outputs

`ArtifactBucketName`, `AppUrl`, `DeployedVersion`, `CognitoUserPoolId`, `SamlSpEntityId`,
`SamlAcsUrl`.

- The app values come from `Deployment`'s returned `Data`. The build writes them to SSM under
  `/aphrodite/installer/outputs/*`.
- `InstallerFunction` **always returns every key**. Before the first deploy, the values are
  `not-deployed`; `Fn::GetAtt` on a missing key would fail the stack.

### Why the Lambda code is inlined

The only bucket in the buyer's account is created by this same stack, so it is empty at
creation time. The function's code therefore cannot come from S3. Inlining also removes any
per-region code-bucket concern.

The constraint is that the function must stay small. The source lives in the repo as a
normal, unit-tested module (`installer/function/handler.py`), and a CI test enforces the
inline size limit.

## 6. Deploy loop

### Install sequence (Loop8-assisted)

1. The admin creates the stack from `installer.yaml` with `AphroditeVersion` empty.
   `Deployment` replies `SUCCESS` immediately, with no build and all outputs `not-deployed`.
2. The Loop8 engineer uploads `aphrodite-<version>.zip` to the `ArtifactBucketName` output
   location.
3. The admin updates the stack with `AphroditeVersion` and `BundleSha256`. This runs the
   apply flow below.

Upgrades repeat steps 2 and 3 with the new version.

### Create and Update (apply flow)

1. CloudFormation invokes `InstallerFunction` with the custom-resource event.
2. `InstallerFunction`:
   1. If `Version` is empty, it replies `SUCCESS` with `not-deployed` outputs and stops.
   2. Validates the properties. `BundleSha256` must be present when `Version` is set.
   3. Confirms that `releases/<version>/aphrodite-<version>.zip` exists in `ArtifactBucket`.
      If not, it replies `FAILED`: "Release <version> has not been uploaded to <bucket>".
   4. Checks for an in-progress build. If one exists, it replies `FAILED`: "a deployment is
      already in progress".
   5. Starts a build with these environment overrides:
      - `ACTION=apply`
      - `VERSION`, `BUNDLE_SHA256`
      - `TF_VAR_aws_region`, `TF_VAR_environment`, `TF_VAR_saml_metadata_url`
      - `STATE_BUCKET`, `ARTIFACT_BUCKET`
      - The reply details: `CFN_RESPONSE_URL`, `CFN_STACK_ID`, `CFN_REQUEST_ID`,
        `CFN_LOGICAL_ID`, `CFN_PHYSICAL_ID`.
   6. Returns **without** replying.
3. `deploy.sh` with `ACTION=apply`:
   1. Bedrock pre-flight (see §8).
   2. `terraform init` with `-backend-config` for the bucket (`STATE_BUCKET`), the key
      (`aphrodite/terraform.tfstate`) and the region (stack region), using the bundled
      provider mirror.
   3. `terraform plan -out=tfplan`, then `terraform apply tfplan`.
   4. Publish the SPA. Write `config.json` from Terraform outputs, using the same shape as
      `terraform.yml` today. Run `aws s3 sync` with the existing cache-control rules: hashed
      assets immutable; `index.html` and `config.json` no-cache. Then create a CloudFront
      invalidation.
   5. Write the stack outputs and the deployed version to SSM `/aphrodite/installer/outputs/*`.
4. CodeBuild emits a state-change event. `BuildFinishedRule` invokes `InstallerFunction`,
   which reads the reply details from the event's environment variables:
   - `SUCCEEDED`: read the outputs from SSM and reply `SUCCESS` with `Data`.
   - Any other terminal status: reply `FAILED` with a short reason and the CodeBuild log
     deep link.

**Physical resource ID.** The ID is fixed for the life of the deployment:
`aphrodite-<account>-<region>`. If it ever changed, CloudFormation would treat the old
deployment as replaced and send it a Delete, which would destroy it. Unit tests enforce this.

**Upgrade rollback.** If an Update fails, CloudFormation sends another Update with the
previous properties. That re-applies the previous version, which is still in
`ArtifactBucket`, through the same loop. Rollback can take as long as the original deploy.
The runbook says not to delete old release zips until the upgrade succeeds.

### Time budget

- `ServiceTimeout: 3600` on the custom resource, the CloudFormation maximum.
- CodeBuild timeout of 55 minutes, so the build always finishes and reports before
  CloudFormation gives up.
- `wait_for_deployment = false` on `aws_cloudfront_distribution.web`, so Terraform does not
  block for 15–45 minutes on CloudFront propagation.
- **The first implementation task measures** a real first deploy against this budget. If it
  is too close to the limit, the fallback is to split `Deployment` into two custom resources
  (infrastructure apply, then SPA publish), each with its own hour.

## 7. Uninstall and data retention

A stack Delete invokes the same loop with `ACTION=destroy` and the current `DeleteData`
value. If nothing was ever deployed (no Terraform state), the Delete replies `SUCCESS`
immediately.

### `DeleteData=false` (default): keep records, remove compute and caches

| Kept | Terraform address(es), listed in `retained-addresses.txt` |
|---|---|
| Outputs bucket (report drafts, AML exports, Certificates of Search) | `module.outputs_bucket.*` |
| CloudTrail audit bucket | `module.logging.aws_s3_bucket.trail` and its configuration resources |
| DynamoDB `activity` and `jobs` tables | `module.search_api.aws_dynamodb_table.activity`, `...jobs` |
| KMS key encrypting the kept data | `module.kms.*` |

Removed: Lambdas, API, schedules, CloudFront, the web bucket, the Cognito user pool, connector
secrets (7-day recovery window), the SharePoint crawl bucket, the vector index, the Knowledge
Base, the reference bucket, and the `integration_health` table.

Mechanics in `deploy.sh`:

1. Record the kept resources' identifiers (names and ARNs) into
   `s3://<StateBucket>/retained-resources.json`.
2. `terraform state rm` each address in `retained-addresses.txt`.
3. Empty the remaining S3 buckets, including all object versions, because no bucket uses
   `force_destroy`.
4. `terraform destroy`.

`StateBucket` and `ArtifactBucket` are always retained by their `DeletionPolicy`.

### `DeleteData=true`: remove everything except Object-Lock-retained files

- Object Lock in governance mode on the outputs bucket is the compliance guarantee. The
  installer **never** uses `s3:BypassGovernanceRetention`.
- `deploy.sh` deletes every outputs-bucket object version it is allowed to delete.
- If locked versions remain, it keeps the outputs bucket and the KMS key (via `state rm`) and
  lists them in `retained-resources.json`.
- Everything else is emptied and destroyed.

**Two-step confirmation.** CloudFormation deletes using the stack's last saved properties. A
data-deleting uninstall therefore needs a stack Update to `DeleteData=true`, followed by a
Delete. A single accidental delete cannot remove data.

### Failed first deploy

- If the first deploy (the Update that sets a version) fails, CloudFormation rolls back to the
  previous properties, which have an empty version.
- `InstallerFunction` treats "Update from version X to empty" as **tear down whatever the
  failed deploy created, with full-teardown semantics**. No customer data exists yet.
- Otherwise, orphaned empty buckets with fixed names (e.g. `aphrodite-outputs-<account>`)
  would block the retry.
- The same rule applies to a Delete received while the stack is `ROLLBACK_IN_PROGRESS`.

### Reinstall after a data-keeping uninstall

Out of scope for automation in v1. The retained resources would clash by name with a fresh
install. A documented, Loop8-assisted runbook procedure re-adopts them into Terraform state
(import) before the apply.

### Related Terraform change

Enable point-in-time recovery on all three DynamoDB tables in `modules/search_api`. None have
it today.

## 8. Error handling

| Failure | Behaviour |
|---|---|
| Exception in `InstallerFunction` before a build starts | The top-level handler catches everything and replies `FAILED` with the reason. CloudFormation never waits out the hour. |
| Release zip not uploaded for the requested version | Immediate `FAILED`: "Release <version> has not been uploaded to <bucket>", with the exact S3 key expected. |
| `BundleSha256` missing or not matching the uploaded zip | Missing: immediate `FAILED`. Mismatch: the build fails in its first minute, before anything is deployed: "Uploaded release does not match the expected checksum — re-upload the file from the GitHub Release." |
| A build is already in progress, or `StartBuild` errors | Immediate `FAILED` with a specific message. |
| Bedrock model access not enabled | **Pre-flight** before Terraform. It checks that the Claude and embedding models used by the stack are accessible in the region, and fails naming the console step to enable them. |
| `terraform apply` fails partway | `FAILED` with a short reason and the log link; CloudFormation caps the reason length. Terraform state records partial progress. The CloudFormation rollback handles the rest (§6, §7). |
| Build timeout or manual stop | Any terminal status other than `SUCCEEDED` → `FAILED`. |
| Stale Terraform state lock after a killed build | `deploy.sh` supports `ACTION=unlock`, started by a Loop8 engineer from the CodeBuild console with an env override. Documented in the runbook. |
| Lost "build finished" event or a callback error | EventBridge retry policy plus Lambda async retries. Duplicate replies are harmless, because CloudFormation keeps only the first. The 1-hour `ServiceTimeout` is the last-resort backstop. |

**Logging hygiene.** `CFN_RESPONSE_URL` is a pre-signed URL. It is never logged by the Lambda
or by `deploy.sh`; there is a unit test for the Lambda. The build logs Terraform output but no
secret values. Connector secrets are empty containers at install time.

## 9. Testing

1. **Unit tests (pytest, test-first)** for `installer/function/handler.py`, following the
   mocking style of the existing connector tests. They cover:
   - An empty version replies `SUCCESS` with `not-deployed` outputs and starts no build.
   - A missing release zip or a missing `BundleSha256` replies `FAILED`.
   - Create and Update start a build with the correct overrides and do not reply.
   - Any pre-build error replies `FAILED`.
   - `SUCCEEDED` replies `SUCCESS` with every output key.
   - Failed or stopped builds reply `FAILED` with the reason and the log link.
   - Version → empty rollback and Delete-during-rollback force a full teardown.
   - A normal Delete honours `DeleteData`.
   - The physical ID is stable across Updates.
   - The response URL never appears in logs.
   - Inline size limit enforced.
2. **Static checks in CI:**
   - `cfn-lint` on the rendered `installer.yaml`.
   - `shellcheck` on `installer/deploy.sh`.
   - The existing `terraform fmt`, `validate` and `tflint` on the changed Terraform.
3. **Sandbox end-to-end**, run by a new manually dispatched workflow,
   `.github/workflows/installer-e2e.yml`. It runs against a sandbox account over OIDC,
   consistent with "no local deployment". The workflow follows the real three-step sequence:
   create the stack, upload the zip, update with the version. Scenarios:
   1. First install into an empty account. Headless Playwright confirms the login page loads
      and `config.json` points at the deployed API.
   2. Upgrade v1 → v2.
   3. A deliberately broken upgrade rolls back to the previous version.
   4. A deliberately failed first deploy cleans up, and a retry succeeds.
   5. Uninstall with `DeleteData=false`: the retained resources match
      `retained-resources.json`.
   6. Two-step `DeleteData=true` uninstall.
   7. Version not uploaded, and wrong checksum: each produces its readable failure.
   8. Bedrock access disabled: the pre-flight produces the readable failure.
   9. **Timing:** first-deploy duration recorded against the 1-hour budget. This is the first
      task in the implementation plan.

## 10. Changes to existing files

| File | Change |
|---|---|
| `terraform/backend.tf` | Remove the hardcoded `region`; it is supplied at `terraform init`. |
| `.github/workflows/terraform.yml` | Pass `-backend-config="region=ca-central-1"` (or `${{ vars.AWS_REGION }}`) to `terraform init` so current behaviour is unchanged. No other edits. |
| `terraform/modules/web/main.tf` | `wait_for_deployment = false` on the CloudFront distribution. |
| `terraform/modules/search_api/main.tf` | Point-in-time recovery on the three DynamoDB tables. |

New files: `installer/installer.yaml` (template source), `installer/function/handler.py` and
its tests, `installer/deploy.sh`, `installer/retained-addresses.txt`, `installer/README.md`
(Loop8 install, upgrade and uninstall runbook), `.github/workflows/release.yml`,
`.github/workflows/installer-e2e.yml`, `CHANGELOG.md`.

**Not touched:** backend application code, the frontend, and other Terraform modules.

## 11. Prerequisites and open questions

**Needed before the sandbox end-to-end runs (not before implementation starts):** a sandbox
AWS account with:

- no existing Aphrodite resources;
- Bedrock model access enabled;
- an OIDC role allowing the e2e workflow to create, update and delete CloudFormation stacks
  and to upload to the artifacts bucket.

**Open questions to resolve during implementation:**

1. **Inline code size limit** for `AWS::Lambda::Function` `Code.ZipFile`. Confirm the current
   limit and keep the handler within it. Verified in task 1.
2. **CodeBuild state-change events carry the build's environment variables.** The design
   relies on this to avoid a state table. Verified in task 1. If it is not true, fall back to
   storing reply details in SSM, keyed by build ID.
3. **AWS Marketplace listing type with no seller-hosted infrastructure.** Ask Seller
   Operations which product type fits. The candidates are AMI-based, container, or private
   offers outside a public listing. The answer affects how the listing distributes
   `installer.yaml` and the release zip, and how billing works. It does not affect the
   installer's behaviour inside the buyer's account.
4. **Regional availability:** the stack depends on S3 Vectors and the Bedrock models it uses.
   The pre-flight covers Bedrock. The supported-regions list is documented from the e2e runs.
