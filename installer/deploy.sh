#!/usr/bin/env bash
# Aphrodite installer deploy script. CodeBuild runs it from the unzipped release bundle root
# (terraform/, frontend/dist/, bin/terraform, providers/, installer/).
#
# Env: ACTION (apply|destroy|unlock), VERSION, STATE_BUCKET, ARTIFACT_BUCKET, DELETE_DATA,
#      TF_VAR_*, CODEBUILD_BUILD_ID, AWS_REGION; LOCK_ID for ACTION=unlock.
# Exit 0 on success, non-zero on failure. On failure it first writes
# {"build_id","message"} to SSM /aphrodite/installer/last-error, which the installer Lambda
# turns into the CloudFormation failure reason.
#
# Never prints CFN_RESPONSE_URL (a pre-signed URL); the Lambda sends the reply, not this script.
# Never bypasses S3 Object Lock governance retention.
set -Eeuo pipefail
shopt -s inherit_errexit
unset CFN_RESPONSE_URL

SSM_PREFIX=/aphrodite/installer
OUTPUTS_PATH=$SSM_PREFIX/outputs
LAST_ERROR=$SSM_PREFIX/last-error
STATE_KEY=aphrodite/terraform.tfstate
RETAINED_KEY=retained-resources.json
OUTPUT_KEYS=(AppUrl DeployedVersion CognitoUserPoolId SamlSpEntityId SamlAcsUrl)
export AWS_PAGER="" TF_IN_AUTOMATION=1 TF_INPUT=0

STEP=start
ERR_MSG=""
WORK=$(mktemp -d)

# --- error handling --------------------------------------------------------------------------

on_error() {
  local rc=$?
  # Subshells ($(...), pipeline stages) inherit the trap; let the parent report once.
  if ((BASH_SUBSHELL > 0)); then exit "$rc"; fi
  trap - ERR
  set +e
  local msg=${ERR_MSG:-"Deployment failed at step $STEP"} body
  echo "ERROR: $msg" >&2
  body=$(python3 -c 'import json, sys
print(json.dumps({"build_id": sys.argv[1], "message": sys.argv[2][:1000]}))' \
    "${CODEBUILD_BUILD_ID:-unknown}" "$msg")
  aws ssm put-parameter --name "$LAST_ERROR" --type String --overwrite --value "$body" \
    >/dev/null || echo "WARNING: could not write $LAST_ERROR" >&2
  exit "$rc"
}
trap on_error ERR

fail() {
  ERR_MSG=$1
  false
}

require() {
  local var
  for var in "$@"; do
    [[ -n ${!var:-} ]] || fail "Installer misconfigured: $var is not set"
  done
}

# shellcheck source=installer/classify_apply_failure.sh
source installer/classify_apply_failure.sh

# --- helpers ---------------------------------------------------------------------------------

tf() {
  bin/terraform -chdir=terraform "$@"
}

# Top-level identifiers (id, arn, bucket, name) of one resource, as a JSON object, read from
# `terraform state show` (nested blocks are skipped by tracking brace depth).
STATE_IDS_PY='
import json, sys
rec, depth = {"address": sys.argv[1]}, 0
for raw in sys.stdin:
    line = raw.strip()
    if line.startswith(("}", "]")):
        depth -= 1
    if depth == 1 and " = " in line:
        key, _, value = line.partition(" = ")
        key, value = key.strip(), value.strip()
        if key in ("id", "arn", "bucket", "name") and value.startswith("\""):
            rec[key] = value[1:-1]
    if line.endswith(("{", "[")):
        depth += 1
print(json.dumps(rec))
'

state_ids() {
  tf state show -no-color "$1" | python3 -c "$STATE_IDS_PY" "$1"
}

bucket_of() {
  state_ids "$1" | python3 -c 'import json, sys; print(json.load(sys.stdin)["bucket"])'
}

state_list() {
  tf state list
}

# read_lines VAR FILE : set array VAR to FILE's non-blank lines (whitespace stripped). Fails if
# FILE is missing, unreadable or has no entries, so a broken bundle never fails open.
read_lines() {
  local -n _lines=$1
  local file=$2 line
  [[ -r $file ]] || fail "Installer bundle is missing $file"
  _lines=()
  while IFS= read -r line || [[ -n $line ]]; do
    line=${line//[[:space:]]/}
    if [[ -n $line ]]; then _lines+=("$line"); fi
  done <"$file"
  ((${#_lines[@]} > 0)) || fail "Installer bundle file $file has no entries"
}

# load_retained FILE : download retained-resources.json to FILE, or start an empty record if it
# does not exist yet. Any other read error fails rather than risk overwriting earlier records.
load_retained() {
  local doc=$1 head_err=$WORK/head.err
  if aws s3api head-object --bucket "$STATE_BUCKET" --key "$RETAINED_KEY" >/dev/null 2>"$head_err"; then
    aws s3 cp --only-show-errors "s3://$STATE_BUCKET/$RETAINED_KEY" "$doc"
  elif grep -q '(404)' "$head_err"; then
    echo '{"resources": []}' >"$doc"
  else
    cat "$head_err" >&2
    fail "Could not read s3://$STATE_BUCKET/$RETAINED_KEY"
  fi
}

# retain REASON ADDRESS... : record each address's identifiers in
# s3://$STATE_BUCKET/retained-resources.json (merged with any earlier record), then remove it
# from Terraform state so `terraform destroy` leaves the resource in place. Addresses no longer
# in state (a retried destroy) are skipped. If none is in a non-empty state and nothing was
# retained earlier, the address list has drifted from the Terraform code: fail before destroy.
retain() {
  local reason=$1 addr doc=$WORK/retained.json new=$WORK/retained.jsonl earlier
  shift
  local present=()
  state_list >"$WORK/state.list"
  for addr in "$@"; do
    if grep -qxF "$addr" "$WORK/state.list"; then present+=("$addr"); fi
  done
  load_retained "$doc"
  if ((${#present[@]} == 0)); then
    earlier=$(python3 -c 'import json, sys
print(len(json.load(open(sys.argv[1])).get("resources") or []))' "$doc")
    if [[ -s $WORK/state.list ]] && ((earlier == 0)); then
      fail "None of the resources to keep is in Terraform state and none was kept earlier; stopping before terraform destroy. Check installer/retained-addresses.txt against the Terraform modules."
    fi
    return 0
  fi

  : >"$new"
  for addr in "${present[@]}"; do
    state_ids "$addr" >>"$new"
  done

  python3 - "$doc" "$new" "$reason" "${VERSION:-}" "$CODEBUILD_BUILD_ID" <<'PY'
import json, sys
doc_path, new_path, reason, version, build_id = sys.argv[1:]
with open(doc_path) as f:
    doc = json.load(f)
by_addr = {r["address"]: r for r in doc.get("resources", [])}
with open(new_path) as f:
    for line in f:
        rec = json.loads(line)
        rec.update(reason=reason, version=version, build_id=build_id)
        by_addr[rec["address"]] = rec
doc["resources"] = sorted(by_addr.values(), key=lambda r: r["address"])
with open(doc_path, "w") as f:
    json.dump(doc, f, indent=2)
PY
  aws s3 cp --only-show-errors "$doc" "s3://$STATE_BUCKET/$RETAINED_KEY"
  echo "Recorded ${#present[@]} retained resource(s) in s3://$STATE_BUCKET/$RETAINED_KEY"

  for addr in "${present[@]}"; do
    tf state rm "$addr"
  done
}

# empty_bucket BUCKET : delete every object version and delete marker S3 allows. Each pass lists
# the whole bucket first (CLI auto-pagination), then deletes in batches of up to 1000. Passes
# repeat until the bucket is empty or S3 refused every version still listed (held by Object
# Lock; governance retention is never bypassed). Capped at 5 passes; whatever is left then makes
# `terraform destroy` fail (and retry) or, for the outputs bucket, keeps it.
empty_bucket() {
  local bucket=$1 pass count refused n chunk listing=$WORK/versions.json
  echo "Emptying s3://$bucket (all versions)"
  for pass in 1 2 3 4 5; do
    aws s3api list-object-versions --bucket "$bucket" --output json >"$listing"
    rm -f "$WORK"/delete-*.json
    count=$(python3 - "$listing" "$WORK" <<'PY'
import json, sys
with open(sys.argv[1]) as f:
    text = f.read().strip()
listing = json.loads(text) if text else {}
items = [
    {"Key": o["Key"], "VersionId": o["VersionId"]}
    for o in (listing.get("Versions") or []) + (listing.get("DeleteMarkers") or [])
]
for i in range(0, len(items), 1000):
    with open(f"{sys.argv[2]}/delete-{i // 1000}.json", "w") as f:
        json.dump({"Objects": items[i : i + 1000], "Quiet": True}, f)
print(len(items))
PY
    )
    if ((count == 0)); then return 0; fi
    refused=0
    for chunk in "$WORK"/delete-*.json; do
      aws s3api delete-objects --bucket "$bucket" --delete "file://$chunk" --output json \
        >"$WORK/deleted.json"
      n=$(python3 -c 'import json, sys
text = open(sys.argv[1]).read().strip()
errors = (json.loads(text) if text else {}).get("Errors") or []
for msg in sorted({"%s: %s" % (e.get("Code"), e.get("Message")) for e in errors})[:3]:
    print("  refused: " + msg, file=sys.stderr)
print(len(errors))' "$WORK/deleted.json")
      refused=$((refused + n))
    done
    echo "  pass $pass: $count version(s) listed, $refused refused"
    if ((refused == count)); then return 0; fi
  done
  echo "s3://$bucket is still not empty after 5 passes"
}

remaining_versions() {
  aws s3api list-object-versions --bucket "$1" --max-items 1 --output json |
    python3 -c 'import json, sys
d = json.load(sys.stdin)
print(len(d.get("Versions") or []) + len(d.get("DeleteMarkers") or []))'
}

empty_remaining_buckets() {
  local addr bucket buckets
  state_list >"$WORK/state.list"
  mapfile -t buckets < <(grep -E '(^|\.)aws_s3_bucket\.[^.]+$' "$WORK/state.list" |
    grep -vE '(^|\.)data\.' || true)
  for addr in "${buckets[@]}"; do
    bucket=$(bucket_of "$addr")
    empty_bucket "$bucket"
  done
}

# --- steps -----------------------------------------------------------------------------------

preflight() {
  STEP="Bedrock pre-flight"
  local models model
  read_lines models installer/preflight-models.txt
  for model in "${models[@]}"; do
    echo "Pre-flight: checking Bedrock model $model"
    ERR_MSG="Bedrock model $model is not accessible in $AWS_REGION. Enable it in the Bedrock console → Model access, then retry the stack update."
    if [[ $model == *embed* ]]; then
      aws bedrock-runtime invoke-model --model-id "$model" --body '{"inputText":"ping"}' \
        --cli-binary-format raw-in-base64-out "$WORK/preflight.out" >/dev/null
    else
      aws bedrock-runtime converse --model-id "$model" \
        --messages '[{"role":"user","content":[{"text":"ping"}]}]' \
        --inference-config '{"maxTokens":1}' >/dev/null
    fi
    ERR_MSG=""
  done
}

tf_init() {
  STEP="terraform init"
  export TF_CLI_CONFIG_FILE=$WORK/terraform.rc
  cat >"$TF_CLI_CONFIG_FILE" <<EOF
provider_installation {
  filesystem_mirror {
    path = "$PWD/providers"
  }
}
EOF
  tf init -input=false \
    -backend-config="bucket=$STATE_BUCKET" \
    -backend-config="key=$STATE_KEY" \
    -backend-config="region=$AWS_REGION"
}

do_apply() {
  STEP="terraform plan"
  tf plan -input=false -out=tfplan
  STEP="terraform apply"
  # tf apply's own failure must not reach the ERR trap here: bash fires the ERR trap on a
  # failing simple command regardless of the current set -e/+e state, exempting only a command
  # on the left of && / || (among other contexts) -- `set +e; ...; set -e` around it does NOT
  # suppress the trap, so `|| rc=$?` is used instead to capture the exit code without tripping it.
  # Streamed through tee (rather than redirected and cat'd after) so the log is not lost if the
  # build times out mid-apply; pipefail (set at the top of this script) makes $? reflect tf
  # apply's own exit code rather than tee's, and the whole pipeline sits on the left of `||`
  # here, so the ERR trap is exempted the same way a single redirected command would be.
  local rc=0
  tf apply -input=false tfplan 2>&1 | tee "$WORK/apply.out" || rc=$?
  if ((rc != 0)); then
    local classified
    classified=$(classify_apply_failure "$WORK/apply.out")
    if [[ -n $classified ]]; then
      fail "$classified"
    fi
    return "$rc"
  fi
}

# Same as the "Build & deploy SPA" step in .github/workflows/terraform.yml, minus the npm
# build: the bundle already carries frontend/dist.
publish_spa() {
  STEP="publish SPA"
  local BUCKET DIST_ID API WEB_URL AUTH CLIENT
  BUCKET=$(tf output -raw web_bucket)
  DIST_ID=$(tf output -raw cloudfront_distribution_id)
  API=$(tf output -raw search_api_endpoint)
  WEB_URL=$(tf output -raw web_url)
  AUTH=$(tf output -raw cognito_authority)
  CLIENT=$(tf output -raw cognito_app_client_id)
  cat >frontend/dist/config.json <<JSON
{"apiEndpoint":"${API%/}","cognito":{"authority":"$AUTH","clientId":"$CLIENT","redirectUri":"$WEB_URL/","scope":"openid email profile"},"mock":false}
JSON
  # Cache policy: the hashed /assets/* bundles are content-addressed, so cache
  # them forever (immutable). index.html and the runtime config.json are NOT
  # hashed and gate which bundle/config loads, so they must always revalidate
  # (no-cache) — otherwise the browser keeps an old index.html and never picks
  # up a new deploy until a hard refresh (a CloudFront invalidation flushes the
  # edge but can't reach an already-cached browser copy).
  aws s3 sync frontend/dist/ "s3://$BUCKET/" --delete \
    --cache-control "public,max-age=31536000,immutable" \
    --exclude index.html --exclude config.json
  aws s3 cp frontend/dist/index.html "s3://$BUCKET/index.html" --cache-control "no-cache"
  aws s3 cp frontend/dist/config.json "s3://$BUCKET/config.json" --cache-control "no-cache"
  aws cloudfront create-invalidation --distribution-id "$DIST_ID" --paths "/index.html" "/config.json" "/"
}

put_output() {
  aws ssm put-parameter --name "$OUTPUTS_PATH/$1" --type String --overwrite --value "$2" >/dev/null
}

write_outputs() {
  STEP="write outputs"
  local app_url pool_id entity_id acs_url
  app_url=$(tf output -raw web_url)
  pool_id=$(tf output -raw cognito_user_pool_id)
  entity_id=$(tf output -raw saml_sp_entity_id)
  acs_url=$(tf output -raw saml_acs_url)
  put_output AppUrl "$app_url"
  put_output CognitoUserPoolId "$pool_id"
  put_output SamlSpEntityId "$entity_id"
  put_output SamlAcsUrl "$acs_url"
  put_output DeployedVersion "$VERSION"
}

do_destroy() {
  if [[ $DELETE_DATA != true ]]; then
    STEP="retain data resources"
    local addresses
    read_lines addresses installer/retained-addresses.txt
    retain "DeleteData=false" "${addresses[@]}"
  else
    STEP="delete outputs bucket contents"
    local outputs_addr=module.outputs_bucket.aws_s3_bucket.outputs outputs_bucket left
    state_list >"$WORK/state.list"
    if grep -qxF "$outputs_addr" "$WORK/state.list"; then
      outputs_bucket=$(bucket_of "$outputs_addr")
      empty_bucket "$outputs_bucket"
      left=$(remaining_versions "$outputs_bucket")
      if ((left > 0)); then
        echo "Object Lock still holds versions in s3://$outputs_bucket; keeping it and its KMS key"
        STEP="retain Object-Lock-held outputs bucket"
        local locked
        mapfile -t locked < <(grep -E '^module\.(outputs_bucket|kms)\.' "$WORK/state.list" |
          grep -v '\.data\.' || true)
        retain "object-lock" "${locked[@]}"
      fi
    fi
  fi

  STEP="empty buckets"
  empty_remaining_buckets
  STEP="terraform destroy"
  if ! tf destroy -input=false -auto-approve; then
    # CloudTrail can deliver a last log file after the trail bucket was emptied; retry once.
    echo "terraform destroy failed; emptying buckets again and retrying once"
    STEP="empty buckets (retry)"
    empty_remaining_buckets
    STEP="terraform destroy (retry)"
    tf destroy -input=false -auto-approve
  fi

  STEP="delete installer outputs"
  aws ssm delete-parameters --names "${OUTPUT_KEYS[@]/#/$OUTPUTS_PATH/}" >/dev/null
}

do_unlock() {
  STEP="terraform force-unlock"
  tf force-unlock -force "$LOCK_ID"
}

# --- main ------------------------------------------------------------------------------------

require ACTION STATE_BUCKET ARTIFACT_BUCKET AWS_REGION CODEBUILD_BUILD_ID
case "$ACTION" in
  apply)
    require VERSION
    preflight
    tf_init
    do_apply
    publish_spa
    write_outputs
    ;;
  destroy)
    require DELETE_DATA
    tf_init
    do_destroy
    ;;
  unlock)
    require LOCK_ID
    tf_init
    do_unlock
    ;;
  *)
    fail "Unknown ACTION '$ACTION' (expected apply, destroy or unlock)"
    ;;
esac
echo "ACTION=$ACTION completed"
