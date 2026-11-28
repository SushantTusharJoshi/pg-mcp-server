#!/usr/bin/env bash
# Turn the per-image results of security-drift.yml into one GitHub issue.
#
#   scripts/drift_report.sh RESULTS_DIR "postgres:16 postgres:latest"
#
# RESULTS_DIR holds one folder per image (the downloaded artifacts), each
# with a result.env written by the matrix job. An image with no result file
# (container failed to start, job cancelled) counts as a failure.
#
# Any failure: open, or update, the single open issue titled
# "Security drift detected" with label `drift` (label created if missing).
# Clean run with that issue open: comment that the latest run is clean.
# Needs GH_TOKEN and GH_REPO. DRY_RUN=1 prints the writes instead.
set -euo pipefail

results_dir="$1"
images="$2"
run_url="${RUN_URL:-}"
title="Security drift detected"
label="drift"
latest="postgres:latest"

if [[ -z "${images// /}" ]]; then
  echo "No image list given; refusing to report a clean run." >&2
  exit 1
fi

failed=()
table=$'| Image | Server version | Digest | Result |\n|---|---|---|---|\n'
for image in $images; do
  slug="${image//[:\/]/-}"
  file="$results_dir/drift-$slug/result.env"
  outcome="missing (job did not report)"
  version="unknown"
  digest="unknown"
  if [[ -f "$file" ]]; then
    outcome="$(sed -n 's/^outcome=//p' "$file")"
    version="$(sed -n 's/^version=//p' "$file")"
    digest="$(sed -n 's/^digest=//p' "$file")"
  fi
  [[ "$outcome" == "success" ]] || failed+=("$image")
  table+="| \`$image\` | ${version:-unknown} | \`${digest:-unknown}\` | $outcome |"$'\n'
done

summary="${GITHUB_STEP_SUMMARY:-/dev/stdout}"
{
  echo "## Security drift report"
  echo
  echo "$table"
} >>"$summary"

gh_write() {
  if [[ "${DRY_RUN:-0}" == "1" ]]; then
    echo "DRY_RUN: gh $*"
  else
    gh "$@"
  fi
}

label_exists="$(gh label list --limit 200 --json name \
  --jq "map(select(.name == \"$label\")) | length")"

existing=""
if [[ "$label_exists" != "0" ]]; then
  existing="$(gh issue list --label "$label" --state open --limit 100 --json number,title \
    --jq "map(select(.title == \"$title\")) | .[0].number // empty")"
fi

if [[ ${#failed[@]} -eq 0 ]]; then
  echo "All images passed."
  if [[ -n "$existing" ]]; then
    gh_write issue comment "$existing" --body "The latest drift run is clean: every image passed the integration suites. ${run_url}"
  fi
  exit 0
fi

# shellcheck disable=SC2016 # literal backticks for Markdown
failed_list="$(printf '`%s`, ' "${failed[@]}")"
failed_list="${failed_list%, }"
kind="regression on a supported image"
if [[ ${#failed[@]} -eq 1 && "${failed[0]}" == "$latest" ]]; then
  kind="new major version (only \`$latest\` failed; the supported images still pass)"
fi

body_file="$(mktemp)"
cat >"$body_file" <<EOF
The nightly security drift check failed on: ${failed_list}

Kind: ${kind}

The same integration suites as CI (SQL attack corpus, read-only transaction, role grants and revokes, generic mode on the clinic schema) ran against each image. If main has not changed since the last clean run, the cause is outside the repository: a new Postgres image or a changed default privilege.

${table}
Run: ${run_url}

Reproduce locally: set the image in docker-compose.yml to the failing one, then run \`scripts/run_integration.sh\` with the integration env from ci.yml. This issue is updated by each failing run and gets a comment when a run is clean.
EOF

cat "$body_file"

if [[ "$label_exists" == "0" ]]; then
  gh_write label create "$label" --color B60205 --description "Nightly security drift check failed"
fi

if [[ -n "$existing" ]]; then
  gh_write issue edit "$existing" --body-file "$body_file"
  gh_write issue comment "$existing" --body "Still failing on ${failed_list}. Kind: ${kind}. ${run_url}"
else
  gh_write issue create --title "$title" --label "$label" --body-file "$body_file"
fi
