# Aphrodite CloudFormation Installer Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** A single CloudFormation stack, run entirely in the buyer's AWS account, that installs,
upgrades and uninstalls Aphrodite from a release zip uploaded into that account.

**Architecture:** The template creates the following, all in the buyer's account:

- a state bucket and an artifacts bucket;
- a CodeBuild project;
- one inline Python Lambda;
- an EventBridge rule;
- a custom resource.

The Lambda starts a CodeBuild run of `installer/deploy.sh` from the uploaded bundle. When the
run finishes, the Lambda replies to CloudFormation. Loop8's only part is a GitHub Actions
workflow that builds the bundle and the template as GitHub Release assets.

**Tech Stack:** CloudFormation (YAML), Python 3.13 (stdlib + boto3 in the Lambda), Bash
(CodeBuild), Terraform ≥ 1.11 with `hashicorp/aws ~> 6.0`, GitHub Actions, pytest, ruff,
cfn-lint, shellcheck.

**Spec:** `docs/superpowers/specs/2026-11-04-cfn-installer-design.md` (rev 2). Read it
alongside this plan; section references (§) point there.

## Global Constraints

- No Loop8 AWS resources at install time or run time. Everything is in the buyer's account (§1, §2).
- Python: `target-version = "py313"`, ruff `line-length = 100`, lint rules `E,F,I,UP,B`. Tests use pytest with fakes injected via `monkeypatch` into a module-level `_DEPS`, matching the existing handler tests (e.g. `backend/lambdas/sanctions_refresh/tests/test_handler.py`).
- The Lambda source is inlined into the template (`Handler: index.handler`). It may import only the stdlib and boto3, never `services.*` or `backend.*`.
- Commit messages carry **no** Claude attribution or `Co-Authored-By` lines (CLAUDE.md). Work on branch `feat/cfn-installer`.
- No local deployment. Anything touching AWS runs through GitHub Actions against the sandbox account.
- Custom resource physical ID: `aphrodite-<account>-<region>`, never changing.
- SSM namespace: `/aphrodite/installer/`. Outputs live under `/aphrodite/installer/outputs/<Key>`, and the last readable error under `/aphrodite/installer/last-error`.
- Output keys: `AppUrl`, `DeployedVersion`, `CognitoUserPoolId`, `SamlSpEntityId`, `SamlAcsUrl`. The placeholder value before any deploy is `not-deployed`.
- Terraform state key: `aphrodite/terraform.tfstate`. Release zip key: `releases/<version>/aphrodite-<version>.zip`.
- `ServiceTimeout: 3600` on the custom resource. The CodeBuild timeout is 55 minutes.
- Exact user-facing messages (spec §6, §8):
  - `Release <version> has not been uploaded to <bucket>` (append the expected key).
  - `a deployment is already in progress`.
  - `Uploaded release does not match the expected checksum — re-upload the file from the GitHub Release.`
- The installer never uses `s3:BypassGovernanceRetention`.

## Resolved open questions (from spec §11)

- **Inline size.** CloudFormation's `ZipFile` limit is 4 MB zipped, so the handler is not the
  constraint. The real limit is the template itself: 51,200 bytes when passed as
  `--template-body`. Task 5 enforces a rendered size below 51,200 bytes.
- **Reply details on build finish.** AWS docs do not confirm that the CodeBuild state-change
  event carries environment-variable values. The Lambda therefore calls
  `codebuild:BatchGetBuilds` with the event's build ID and reads `environment.environmentVariables`
  and `logs.deepLink` from the result. No state table is needed. This replaces the spec's SSM
  fallback with something simpler.
- **Readable failure reasons.** CodeBuild phase messages read like "exit status 1". So
  `deploy.sh` writes its human-readable error to `/aphrodite/installer/last-error` as JSON:
  `{"build_id": "...", "message": "..."}`. The Lambda uses it when `build_id` matches.

## Review Focus

1. **Admin clears `AphroditeVersion` on a live stack.** Expect `FAILED` with "Clearing AphroditeVersion is not supported; delete the stack to uninstall." This must never trigger a teardown. A teardown happens only when the stack is in `UPDATE_ROLLBACK_IN_PROGRESS`. Test in Task 3.
2. **A manually started CodeBuild run (`ACTION=unlock`) finishes.** It has no `CFN_RESPONSE_URL`, so the Lambda logs and returns without replying and without raising. Test in Task 4.
3. **Delete when the version is empty but Terraform state exists** (an earlier rollback teardown failed). Expect `FAILED` pointing to the runbook "Manual teardown" section, not a silent `SUCCESS` that orphans resources. Test in Task 3.
4. **Upgrade where the old zip was deleted before the new deploy succeeded.** The rollback then fails with the "has not been uploaded" message for the *old* version, naming its key. Test in Task 3. The runbook warns against deleting old zips (Task 7).
5. **Stack launched in a region where `us.anthropic.claude-sonnet-4-6` or `amazon.titan-embed-text-v2:0` isn't available.** The pre-flight fails with a message naming the model and region. Covered by `deploy.sh` (Task 4) and e2e scenario 8 (Task 8).

---

## File structure

| Path | Responsibility |
|---|---|
| `installer/__init__.py`, `installer/function/__init__.py` | Packages so pytest can import the handler. Both empty. |
| `installer/function/handler.py` | The inline Lambda: event routing, action decisions, CodeBuild start, CloudFormation replies. |
| `installer/function/tests/test_cfn_events.py` | Tests for custom-resource events. |
| `installer/function/tests/test_build_events.py` | Tests for build-finished events. |
| `installer/deploy.sh` | CodeBuild entry point: `ACTION=apply|destroy|unlock`. |
| `installer/preflight-models.txt` | Bedrock model IDs the pre-flight checks. |
| `installer/retained-addresses.txt` | Terraform addresses kept when `DeleteData=false`. |
| `installer/bundle_manifest.py` | Builds and verifies `manifest.json`. |
| `installer/tests/test_bundle_manifest.py` | Tests for the manifest module. |
| `installer/tests/test_bundle_files.py` | Checks that the two `.txt` files stay in sync with the Terraform and Python sources. |
| `installer/installer.yaml` | Template source, with the placeholder `#HANDLER_SOURCE#` in `Code.ZipFile`. |
| `installer/render.py` | Inlines `handler.py` into the template, producing `dist/installer.yaml`. |
| `installer/tests/test_render.py` | Tests for the render step. |
| `installer/README.md` | The Loop8 runbook. |
| `CHANGELOG.md` | Release notes. |
| `.github/workflows/release.yml` | Builds the bundle and template as GitHub Release assets. |
| `.github/workflows/installer-e2e.yml` | Sandbox end-to-end scenarios. |

**Modified:**

- `terraform/backend.tf`
- `.github/workflows/terraform.yml` (the `Init` step only)
- `.github/workflows/ci.yml` (adds installer checks)
- `terraform/modules/web/main.tf`
- `terraform/modules/search_api/main.tf`
- `pyproject.toml` (`testpaths`, dev deps)

---

### Task 1: Terraform preparation

**Files:**
- Modify: `terraform/backend.tf`. Delete the `region = "ca-central-1"` line and update the comment to say the region is supplied at init.
- Modify: `.github/workflows/terraform.yml`, `Init` step. Change it to `terraform init -input=false -backend-config="region=${{ vars.AWS_REGION }}"`.
- Modify: `terraform/modules/web/main.tf`. Add `wait_for_deployment = false` to `aws_cloudfront_distribution.web`, with a one-line comment citing the 1-hour installer budget.
- Modify: `terraform/modules/search_api/main.tf`. Add `point_in_time_recovery { enabled = true }` to `aws_dynamodb_table.jobs`, `.activity` and `.integration_health`.

**Interfaces:**
- Produces: the backend accepts `-backend-config=region=…`, which `deploy.sh` (Task 4) relies on.

- [ ] **Step 1:** Make the four edits above.
- [ ] **Step 2:** Run `cd terraform && terraform fmt -check -recursive && terraform init -backend=false -input=false && terraform validate`. Expected: no diff, then `Success! The configuration is valid.`
- [ ] **Step 3:** Run `tflint --init && tflint`. Expected: exit 0.
- [ ] **Step 4:** Open a PR. In the `terraform` workflow's plan output, expect exactly three DynamoDB tables changed (PITR on) and the CloudFront distribution unchanged. `wait_for_deployment` is not an API attribute, so it plans no change. Expect no resource replacements.
- [ ] **Step 5:** Commit.

```bash
git checkout -b feat/cfn-installer
git add terraform/backend.tf terraform/modules/web/main.tf terraform/modules/search_api/main.tf .github/workflows/terraform.yml
git commit -m "terraform: region-agnostic backend, no CloudFront wait, DynamoDB PITR"
```

---

### Task 2: Timing gate (sandbox)

This is a measurement, not code. It decides whether the one-custom-resource design (spec §6
"Time budget") holds.

**Prerequisite:** the sandbox AWS account from spec §11, bootstrapped with the existing
`terraform/bootstrap/bootstrap.yaml`, with the repo's `AWS_DEPLOY_ROLE_ARN` and `AWS_REGION`
variables pointed at it via a GitHub Environment named `sandbox`.

- [ ] **Step 1:** Run the existing `terraform` workflow from the Task 1 branch against the empty sandbox (`workflow_dispatch`, environment `sandbox`). Record the durations of the `Apply` step and the `Build & deploy SPA` step from the run summary.
- [ ] **Step 2:** Decide:
  - **Total under 40 minutes:** continue with the plan unchanged.
  - **40 minutes or more:** stop. Report the numbers, and revise spec §6 to the two-custom-resource split (infrastructure apply, then SPA publish) before Task 3.
- [ ] **Step 3:** Record the measurement and the decision in `CHANGELOG.md` under `## Unreleased` → `### Notes`, then commit: `git commit -am "docs: record first-apply timing gate result"`.

---

### Task 3: Installer Lambda — custom-resource events

**Files:**
- Create: `installer/__init__.py`, `installer/function/__init__.py` (both empty)
- Create: `installer/function/handler.py`
- Test: `installer/function/tests/__init__.py`, `installer/function/tests/test_cfn_events.py`
- Modify: `pyproject.toml`. Set `testpaths = ["backend", "services", "installer"]`.

**Interfaces:**
- Produces:
  - `handler(event: dict, context) -> None`. This is the Lambda entry point. It routes on `"RequestType" in event` (custom resource) vs `event.get("source") == "aws.codebuild"`.
  - `@dataclass Deps`, with fields:
    - `codebuild`, `ssm`, `s3`, `cfn`: boto3 clients;
    - `http_put: Callable[[str, bytes], None]`;
    - `project_name: str`, `state_bucket: str`, `artifact_bucket: str`.

    The module-level `_DEPS: Deps | None` is built lazily from env vars `PROJECT_NAME`, `STATE_BUCKET` and `ARTIFACT_BUCKET`.
  - `decide(request_type: str, props: dict, old_props: dict, stack_status: str, state_exists: bool) -> Decision`, where `Decision` is a dataclass:
    - `kind`: `"noop" | "apply" | "destroy" | "fail"`;
    - `version: str`;
    - `delete_data: bool`;
    - `reason: str`.
  - `reply(event_like: dict, status: str, reason: str, data: dict) -> None`. It builds the CloudFormation response JSON and sends it with `_DEPS.http_put`. It never logs the URL. `event_like` carries the keys `ResponseURL`, `StackId`, `RequestId` and `LogicalResourceId`.
  - `physical_id(stack_id: str) -> str`. Returns `aphrodite-<account>-<region>`, parsed from the stack ARN.
  - `NOT_DEPLOYED: dict[str, str]`. Maps every output key to `"not-deployed"`.

**Decision table:** this is what `decide` must implement. It is copied from spec §6 and §7,
plus Review Focus 1 and 3.

| request_type | condition | Decision |
|---|---|---|
| Create | `Version` empty | noop |
| Create | `Version` set | apply(`Version`) |
| Update | new empty, old empty | noop |
| Update | new set | apply(new `Version`) |
| Update | new empty, old set, status `UPDATE_ROLLBACK_IN_PROGRESS` | destroy(old `Version`, delete_data=True) |
| Update | new empty, old set, any other status | fail: "Clearing AphroditeVersion is not supported; delete the stack to uninstall." |
| Delete | status `ROLLBACK_IN_PROGRESS` | destroy(`Version`, delete_data=True), or noop if `state_exists` is false |
| Delete | `state_exists` false | noop |
| Delete | `state_exists` true, `Version` empty | fail: "Terraform state exists but no version is set — see installer/README.md 'Manual teardown'." |
| Delete | otherwise | destroy(`Version`, delete_data = (`DeleteData` == "true")) |

**`handler` flow for custom-resource events:** wrap the whole flow in `try/except Exception`.
Any exception becomes `reply(FAILED, str(exc)[:500], NOT_DEPLOYED)`.

1. Get the stack status with `cfn.describe_stacks(StackName=StackId)`.
2. Work out `state_exists` with `s3.head_object` on `STATE_BUCKET` / `aphrodite/terraform.tfstate`. A 404 means false.
3. Call `decide`.
4. **noop:** reply `SUCCESS` with `NOT_DEPLOYED`. Then, if `/aphrodite/installer/outputs/*` holds values, reply with those instead.
5. **fail:** reply `FAILED` with the reason.
6. **apply or destroy:**
   1. `BundleSha256` must be set; otherwise reply `FAILED`.
   2. `s3.head_object` the release key; on 404, reply `FAILED` with "Release <version> has not been uploaded to <bucket> (expected key <key>)".
   3. Check for a running build: `codebuild.list_builds_for_project(projectName=…, sortOrder="DESCENDING")`, take the first 5 IDs, call `batch_get_builds`. If any has `buildStatus == "IN_PROGRESS"`, reply `FAILED` with "a deployment is already in progress".
   4. Call `codebuild.start_build` with these `environmentVariablesOverride`, all `PLAINTEXT`:
      - `ACTION`, `VERSION`, `BUNDLE_SHA256`, `DELETE_DATA`;
      - `TF_VAR_aws_region` (from the stack ARN), `TF_VAR_environment`, `TF_VAR_saml_metadata_url`;
      - `CFN_RESPONSE_URL`, `CFN_STACK_ID`, `CFN_REQUEST_ID`, `CFN_LOGICAL_ID`, `CFN_PHYSICAL_ID`.
   5. Clear `/aphrodite/installer/last-error`; ignore "parameter not found".
   6. Return without replying.

- [ ] **Step 1: Write failing tests** in `test_cfn_events.py`, using fakes for `codebuild`, `ssm`, `s3` and `cfn` plus a recording `http_put`, injected with `monkeypatch.setattr(h, "_DEPS", deps)`. Tests:
  - `test_decide_table`: parametrized over every row of the decision table above. Asserts `kind`, `version` and `delete_data`.
  - `test_create_empty_version_replies_success_without_build`: one PUT with `Status == "SUCCESS"`, `Data == NOT_DEPLOYED`, and `start_build` not called.
  - `test_apply_starts_build_and_does_not_reply`: `start_build` is called once with `ACTION=apply`, `VERSION=1.0.0` and `CFN_RESPONSE_URL` among the overrides, and no PUT is made.
  - `test_missing_bundle_sha_fails`: the PUT `Status == "FAILED"`.
  - `test_missing_release_zip_fails_with_key`: the reason contains `releases/1.0.0/aphrodite-1.0.0.zip` and the bucket name.
  - `test_rollback_names_old_version_key`: a rollback to v1 whose zip is missing; the reason names `releases/1.0.0/…` (Review Focus 4).
  - `test_build_in_progress_fails`: the reason equals `a deployment is already in progress`.
  - `test_exception_replies_failed`: `cfn.describe_stacks` raises; the PUT `Status == "FAILED"`.
  - `test_physical_id_stable_across_updates`: the PUT `PhysicalResourceId` is `aphrodite-123456789012-ca-central-1` for Create and for Update.
  - `test_response_url_never_logged`: use `caplog` at DEBUG across the apply, fail and noop paths; assert the URL string is in no record.
- [ ] **Step 2:** Run `pytest installer/function/tests/test_cfn_events.py -v`. Expected: FAIL (`ModuleNotFoundError: installer.function.handler`).
- [ ] **Step 3:** Implement `handler.py` with the interfaces above. `http_put` defaults to `urllib.request` PUT with `Content-Type: ""`, which CloudFormation's pre-signed URL requires. Use stdlib `logging`, not `services.logging`.
- [ ] **Step 4:** Run `pytest installer -v && ruff check installer`. Expected: PASS, no lint errors.
- [ ] **Step 5:** Commit: `git add installer pyproject.toml && git commit -m "installer: custom-resource event handling"`.

---

### Task 4: Installer Lambda build events, and `deploy.sh`

**Files:**
- Modify: `installer/function/handler.py`
- Test: `installer/function/tests/test_build_events.py`
- Create: `installer/deploy.sh` (executable), `installer/preflight-models.txt`, `installer/retained-addresses.txt`
- Test: `installer/tests/__init__.py`, `installer/tests/test_bundle_files.py`

**Interfaces:**
- Consumes: `reply`, `NOT_DEPLOYED` and `Deps` from Task 3.
- Produces:
  - `on_build_finished(event: dict) -> None`, called by `handler`.
  - The `deploy.sh` contract, which Task 5's buildspec and Task 8's e2e rely on:
    - **Working directory:** the unzipped bundle root.
    - **Required env:** `ACTION`, `VERSION`, `STATE_BUCKET`, `ARTIFACT_BUCKET`, `DELETE_DATA`, `TF_VAR_*`, `CODEBUILD_BUILD_ID`, `AWS_REGION`.
    - **Exit codes:** 0 on success, non-zero on failure. On failure it first writes `{"build_id","message"}` to `/aphrodite/installer/last-error`.

**`on_build_finished` behaviour:**

1. `build_id = event["detail"]["build-id"]`. Call `codebuild.batch_get_builds(ids=[build_id])` and turn `environment.environmentVariables` into a dict.
2. If `CFN_RESPONSE_URL` is absent, log "build <id> not started by CloudFormation; ignoring" and return (Review Focus 2).
3. Rebuild `event_like` from the `CFN_*` variables.
4. **`detail.build-status == "SUCCEEDED"`:**
   - For apply: read `/aphrodite/installer/outputs/<Key>` for each output key and reply `SUCCESS` with them.
   - For destroy: reply `SUCCESS` with `NOT_DEPLOYED`.
5. **Any other status:**
   - The reason is the `last-error` message if its `build_id` matches, else "Deployment failed".
   - Append ` — logs: <logs.deepLink>` and truncate to 900 characters.
   - Reply `FAILED`.

**`deploy.sh` behaviour (`set -euo pipefail`):**

1. **Error trap:** an `ERR` trap writes the current `$ERR_MSG` (default "Deployment failed at step <step>") to `last-error`.
2. **Pre-flight** (apply only): for each model in `preflight-models.txt`, make a minimal call.
   - Embedding: `aws bedrock-runtime invoke-model` with `{"inputText":"ping"}`.
   - Claude: `aws bedrock-runtime converse` with one user message "ping" and `maxTokens=1`.
   - On failure: `ERR_MSG="Bedrock model <id> is not accessible in <region>. Enable it in the Bedrock console → Model access, then retry the stack update."`
3. **Terraform setup:** `export TF_CLI_CONFIG_FILE` pointing to a generated file that sets `provider_installation { filesystem_mirror { path = "$PWD/providers" } }`. Then `bin/terraform -chdir=terraform init -input=false -backend-config=bucket=$STATE_BUCKET -backend-config=key=aphrodite/terraform.tfstate -backend-config=region=$AWS_REGION`.
4. **`apply`:**
   1. `plan -out=tfplan`, then `apply tfplan`.
   2. Publish the SPA. Copy the `terraform.yml` "Build & deploy SPA" logic verbatim, but write `config.json` into `frontend/dist` and skip `npm`.
   3. Write SSM outputs: `AppUrl` = `web_url`, `CognitoUserPoolId` = `cognito_user_pool_id`, `SamlSpEntityId` = `saml_sp_entity_id`, `SamlAcsUrl` = `saml_acs_url`, `DeployedVersion` = `$VERSION`.
5. **`destroy`:**
   1. If `DELETE_DATA != true`: record the identifiers of each address in `retained-addresses.txt` (`terraform state show -no-color`) into `s3://$STATE_BUCKET/retained-resources.json`, then `terraform state rm` each address.
   2. If `DELETE_DATA == true`: delete every outputs-bucket object version, without the bypass. If any versions remain, `state rm` `module.outputs_bucket.*` and `module.kms.*`, and record them.
   3. Empty every remaining Aphrodite bucket still in state, all versions.
   4. `terraform destroy -auto-approve`.
   5. Delete `/aphrodite/installer/outputs/*`.
6. **`unlock`:** `terraform force-unlock -force "$LOCK_ID"`. `LOCK_ID` is required.

**File contents:**

`preflight-models.txt`, one ID per line:
- `amazon.titan-embed-text-v2:0`
- `us.anthropic.claude-sonnet-4-6`

`retained-addresses.txt`, one address per line:
- `module.outputs_bucket.aws_s3_bucket.outputs`
- `module.outputs_bucket.aws_s3_bucket_versioning.outputs`
- `module.outputs_bucket.aws_s3_bucket_object_lock_configuration.outputs`
- `module.outputs_bucket.aws_s3_bucket_server_side_encryption_configuration.outputs`
- `module.outputs_bucket.aws_s3_bucket_public_access_block.outputs`
- `module.logging.aws_s3_bucket.trail`
- `module.logging.aws_s3_bucket_versioning.trail`
- `module.logging.aws_s3_bucket_public_access_block.trail`
- `module.logging.aws_s3_bucket_server_side_encryption_configuration.trail`
- `module.logging.aws_s3_bucket_policy.trail`
- `module.search_api.aws_dynamodb_table.jobs`
- `module.search_api.aws_dynamodb_table.activity`
- `module.kms.aws_kms_key.secrets`
- `module.kms.aws_kms_alias.secrets`

- [ ] **Step 1: Write failing tests.**
  - `test_build_events.py`:
    - `test_success_replies_with_ssm_outputs`: the PUT `Data` has all five keys with the SSM values.
    - `test_destroy_success_replies_not_deployed`.
    - `test_failure_uses_matching_last_error`: the reason starts with the `last-error` message and contains the `deepLink`.
    - `test_failure_ignores_stale_last_error`: a different `build_id` → the reason starts with `Deployment failed`.
    - `test_stopped_build_fails`: `build-status` `STOPPED` → `FAILED`.
    - `test_manual_build_without_response_url_is_ignored`: no PUT, no exception.
    - `test_reason_truncated`: `len(reason) <= 900`.
  - `test_bundle_files.py`:
    - `test_preflight_models_match_sources`: the file's IDs equal the `embedding_model_id` default parsed from `terraform/modules/sharepoint_kb/variables.tf`, plus `DEFAULT_MODEL_ID` from `services/llm.py`.
    - `test_retained_addresses_exist_in_terraform`: each `module.<m>.<type>.<name>` has a matching `resource "<type>" "<name>"` in `terraform/modules/<m>/main.tf`.
- [ ] **Step 2:** Run `pytest installer -v`. Expected: the new tests FAIL.
- [ ] **Step 3:** Implement `on_build_finished`. Write `deploy.sh`, `preflight-models.txt` and `retained-addresses.txt` as specified.
- [ ] **Step 4:** Run `pytest installer -v && ruff check installer && shellcheck installer/deploy.sh`. Expected: all pass, and shellcheck exits 0.
- [ ] **Step 5:** Commit: `git commit -am "installer: build-finished handling and deploy script"`, after `git add` of the new files.

---

### Task 5: Template, render step and CI checks

**Files:**
- Create: `installer/installer.yaml`, `installer/render.py`
- Test: `installer/tests/test_render.py`
- Modify: `.github/workflows/ci.yml` (adds `installer` checks)
- Modify: `pyproject.toml` (dev deps: `cfn-lint`, `pyyaml`)

**Interfaces:**
- Consumes: `handler.py` (Task 3–4), and the `deploy.sh` contract (Task 4).
- Produces: `render(template_path: Path, handler_path: Path) -> str`, plus a CLI `python -m installer.render <out_path>` that writes `dist/installer.yaml`.

**Template contents:** the resources, parameters and outputs exactly as spec §5. Points that
pin values:

- **Parameters:**
  - `AphroditeVersion`: `AllowedPattern: '^$|^\d+\.\d+\.\d+$'`.
  - `BundleSha256`: `AllowedPattern: '^$|^[a-f0-9]{64}$'`.
  - `DeleteData`: `AllowedValues: ["true","false"]`, `Default: "false"`.
- **Buckets:** `StateBucket` and `ArtifactBucket` both get `DeletionPolicy: Retain`, `UpdateReplacePolicy: Retain`, and the bootstrap bucket's hardening (copy the properties from `terraform/bootstrap/bootstrap.yaml`). Names come from the stack, not from hardcoded values.
- **`DeployRole`:** copy both inline policies from `bootstrap.yaml` verbatim, then add a third with:
  - `s3:GetObject` on `${ArtifactBucket.Arn}/releases/*`;
  - `s3:*` on `StateBucket` and its objects;
  - `ssm:PutParameter`, `ssm:GetParameter*` and `ssm:DeleteParameter*` on `arn:aws:ssm:${AWS::Region}:${AWS::AccountId}:parameter/aphrodite/installer/*`;
  - `bedrock:InvokeModel` on `*`.
- **`DeployProject`:**
  - `Source: {Type: NO_SOURCE, BuildSpec: <inline>}`, `TimeoutInMinutes: 55`.
  - Image `aws/codebuild/amazonlinux-x86_64-standard:5.0`.
  - Environment variables `STATE_BUCKET` and `ARTIFACT_BUCKET`.
  - The inline buildspec:
    1. `aws s3 cp s3://$ARTIFACT_BUCKET/releases/$VERSION/aphrodite-$VERSION.zip .`
    2. `echo "$BUNDLE_SHA256  aphrodite-$VERSION.zip" | sha256sum -c -`. On mismatch, write the exact checksum message to `last-error` and exit 1.
    3. `unzip -q`, then `python3 -m installer.bundle_manifest verify .` (Task 6).
    4. `bash installer/deploy.sh`.
- **`InstallerFunction`:**
  - `Runtime: python3.13`, `Handler: index.handler`, `Timeout: 60`.
  - `Code.ZipFile` holds `#HANDLER_SOURCE#`.
  - Environment variables `PROJECT_NAME`, `STATE_BUCKET` and `ARTIFACT_BUCKET`.
  - Role permissions as spec §5, plus `s3:GetObject` (head) on `StateBucket` / `aphrodite/terraform.tfstate`, and `ssm:DeleteParameter` on `last-error`.
- **`BuildFinishedRule`:** the pattern matches `detail.project-name: [!Ref DeployProject]` and `detail.build-status: ["SUCCEEDED","FAILED","STOPPED"]`. Use `RetryPolicy: {MaximumRetryAttempts: 5}` and an `AWS::Lambda::Permission` for `events.amazonaws.com`.
- **`Deployment`:** `ServiceTimeout: 3600`. Its properties come from the parameters.
- **Outputs:** `!GetAtt Deployment.<Key>` for each of the five keys, plus `ArtifactBucketName`.

**Render behaviour:** `render` replaces `#HANDLER_SOURCE#` with the handler source, indented
to match the `ZipFile: |` block. It raises `ValueError` if the placeholder is not found
exactly once.

- [ ] **Step 1: Write failing tests** in `test_render.py`:
  - `test_render_inlines_handler_verbatim`: parse the rendered YAML with a loader that tolerates CloudFormation tags; `Resources.InstallerFunction.Properties.Code.ZipFile` equals the `handler.py` text.
  - `test_render_under_template_body_limit`: `len(rendered.encode()) < 51200`.
  - `test_render_requires_single_placeholder`: raises `ValueError` for 0 or 2 placeholders.
  - `test_handler_is_index_handler`.
  - `test_deployment_service_timeout_is_3600`.
  - `test_buckets_retained`: both buckets have `DeletionPolicy == "Retain"`.
  - `test_build_timeout_is_55`.
- [ ] **Step 2:** Run `pytest installer/tests/test_render.py -v`. Expected: FAIL.
- [ ] **Step 3:** Write `installer.yaml` and `render.py`. In `ci.yml`, add these steps after `Test (pytest)`:
  - `python -m installer.render dist/installer.yaml && cfn-lint dist/installer.yaml`;
  - `shellcheck installer/deploy.sh`.
- [ ] **Step 4:** Run `pytest installer -v && python -m installer.render /tmp/i.yaml && cfn-lint /tmp/i.yaml`. Expected: PASS, and cfn-lint exits 0.
- [ ] **Step 5:** Commit: `git add installer .github/workflows/ci.yml pyproject.toml && git commit -m "installer: CloudFormation template, render step, CI lint"`.

---

### Task 6: Release bundle and release workflow

**Files:**
- Create: `installer/bundle_manifest.py`
- Test: `installer/tests/test_bundle_manifest.py`
- Create: `.github/workflows/release.yml`

**Interfaces:**
- Produces:
  - `build_manifest(root: Path, version: str, commit: str) -> dict`. Returns `{"version", "commit", "files": {relpath: sha256}}`, excluding `manifest.json` itself.
  - `verify_manifest(root: Path) -> list[str]`. Returns the relative paths that are missing, changed or extra; an empty list means OK.
  - CLI: `python -m installer.bundle_manifest build <root> <version> <commit>` writes `manifest.json`. `verify <root>` exits 1 and prints the paths if the list is non-empty.
  - Release assets: `installer.yaml`, `aphrodite-<version>.zip`, `aphrodite-<version>.zip.sha256`.

**`release.yml`:**

- Trigger: `push: tags: ['v*.*.*']`, with `permissions: contents: write` and no AWS credentials.
- Steps:
  1. Lambda build: copy the existing "Build Lambda package" step verbatim into `terraform/build/lambda`.
  2. `npm ci && npm run build` in `frontend/`.
  3. Download the pinned Terraform. Set `TERRAFORM_VERSION` at the top of the workflow to the newest 1.x release on the day this task is implemented, as an exact version. Put the binary in `bundle/bin/`.
  4. `terraform -chdir=terraform providers mirror -platform=linux_amd64 ../bundle/providers`.
  5. Assemble `bundle/`: `terraform/` (with `build/lambda`, without `.terraform`), `frontend/dist/`, `installer/` (`deploy.sh`, both `.txt` files, `bundle_manifest.py`, `__init__.py`).
  6. Write the manifest.
  7. Zip as `aphrodite-${VERSION}.zip` and write `sha256sum` to the `.sha256` file.
  8. `python -m installer.render installer.yaml`.
  9. `gh release create "$GITHUB_REF_NAME" <three assets> --notes-file <CHANGELOG section>`.

- [ ] **Step 1: Write failing tests** in `test_bundle_manifest.py`:
  - `test_roundtrip_ok`: build, then verify → `[]`.
  - `test_detects_changed_file`.
  - `test_detects_missing_file`.
  - `test_detects_extra_file`.
  - `test_manifest_excludes_itself`.
- [ ] **Step 2:** Run `pytest installer/tests/test_bundle_manifest.py -v`. Expected: FAIL.
- [ ] **Step 3:** Implement `bundle_manifest.py` (`hashlib.sha256`, files sorted by relative path) and write `release.yml`.
- [ ] **Step 4:** Run `pytest installer -v && ruff check installer`. Expected: PASS. Then push a throwaway tag `v0.0.1-rc1` to the branch's fork or run it with `workflow_dispatch` in dry-run mode, and confirm the three assets are produced. For the dry run, add a `dry_run` input that uploads them as workflow artifacts instead of creating a release.
- [ ] **Step 5:** Commit: `git add installer/bundle_manifest.py installer/tests .github/workflows/release.yml && git commit -m "release: build installer bundle and template as GitHub Release assets"`.

---

### Task 7: Runbook and changelog

**Files:**
- Create: `installer/README.md`, `CHANGELOG.md` (or extend the Task 2 file)
- Modify: `README.md`. Add one row to the layout table: `installer/` → "CloudFormation installer for buyer accounts".

**Required runbook sections:** the e2e workflow (Task 8) and support staff follow these.

- **Prerequisites:** Bedrock model access for both models in `preflight-models.txt`, and the region list.
- **Install:** create the stack with an empty version, upload the zip to `releases/<v>/` in the `ArtifactBucketName` output, then update the stack with `AphroditeVersion` and `BundleSha256` taken from the `.sha256` asset.
- **Upgrade:** keep old zips until the upgrade succeeds (Review Focus 4).
- **Uninstall:** the two-step `DeleteData=true` procedure, what `retained-resources.json` lists, and the manual removal of the two retained buckets.
- **Manual teardown:** what to do when state exists and the version is empty (Review Focus 3).
- **Unlock:** start the CodeBuild project with `ACTION=unlock` and `LOCK_ID`.
- **Reinstall after a data-keeping uninstall:** `terraform import` of each retained address.
- **Custom domain:** the manual follow-up.
- **External dependencies:** none at deploy time; state this explicitly.

- [ ] **Step 1:** Write both files.
- [ ] **Step 2:** Check that every message string in the Global Constraints, and every runbook section referenced by a handler message, appears in `installer/README.md`: `grep -c "Manual teardown" installer/README.md` ≥ 1.
- [ ] **Step 3:** Commit: `git add installer/README.md CHANGELOG.md README.md && git commit -m "docs: installer runbook and changelog"`.

---

### Task 8: Sandbox end-to-end workflow

**Files:**
- Create: `.github/workflows/installer-e2e.yml`

**Interfaces:**
- Consumes: release assets (Task 6), template outputs (Task 5), and the runbook steps (Task 7).

**Workflow:**

- **Trigger:** `workflow_dispatch` with inputs:
  - `scenario` (choice): `install`, `upgrade`, `broken-upgrade`, `failed-first-deploy`, `uninstall-keep`, `uninstall-delete`, `bad-inputs`, `bedrock-off`;
  - `version_a`, `version_b`.
- **Environment:** `sandbox`, with OIDC to the sandbox role.
- **Shared steps:**
  1. Download the release assets with `gh release download`.
  2. `aws cloudformation deploy` with an empty version.
  3. Upload the zip.
  4. `aws cloudformation deploy` with the version and checksum.
  5. `aws cloudformation wait`.
- **Assertions per scenario:**
  - **install:** the stack is `UPDATE_COMPLETE`, and Playwright (`npx playwright` with a 20-line script in the workflow) loads `AppUrl` and finds the Cognito sign-in redirect. `curl $AppUrl/config.json` contains the `apiEndpoint` matching `search_api_endpoint`.
  - **upgrade:** `DeployedVersion == version_b`.
  - **broken-upgrade:** use a `BundleSha256` of 64 zeros for B. The stack is `UPDATE_ROLLBACK_COMPLETE`, the reason contains "does not match the expected checksum", and `DeployedVersion == version_a`.
  - **failed-first-deploy:** a wrong checksum on the first deploy. After rollback, no `aphrodite-outputs-*` bucket exists, and a retry with the right checksum succeeds.
  - **uninstall-keep:** after Delete, `retained-resources.json` lists the outputs bucket, trail bucket, both tables and the KMS key, and each exists.
  - **uninstall-delete:** update to `DeleteData=true`, then Delete. Only the two installer buckets remain, plus any Object-Lock-held outputs bucket listed in `retained-resources.json`.
  - **bad-inputs:** version `9.9.9` not uploaded gives "has not been uploaded"; the wrong checksum gives the checksum message.
  - **bedrock-off:** run in a sandbox region without model access configured. The reason contains "is not accessible in".
- **Every scenario** writes the stack events and timings to the job summary.

- [ ] **Step 1:** Write the workflow.
- [ ] **Step 2:** Run `actionlint .github/workflows/installer-e2e.yml`. Expected: exit 0.
- [ ] **Step 3:** Run every scenario against the sandbox. Expected: all assertions pass. Record each scenario's duration in `CHANGELOG.md` notes.
- [ ] **Step 4:** Commit: `git add .github/workflows/installer-e2e.yml CHANGELOG.md && git commit -m "ci: installer end-to-end scenarios"`.
- [ ] **Step 5:** Open the PR for `feat/cfn-installer`. The PR description has no Claude attribution, per CLAUDE.md.
