#!/usr/bin/env bash
set -euo pipefail

WORKSPACE="${HERMES_WORKSPACE:-.}"
PROMPT_FILE="${HERMES_PROMPT_FILE:-/tmp/hermes-prompt.txt}"
OUTPUT_FILE="${HERMES_OUTPUT_FILE:-hermes-response.md}"
HERMES_HOME_TMP="${HERMES_HOME_TMP:-/tmp/hermes-ci-${GITHUB_RUN_ID:-local}}"
TIMEOUT="${HERMES_TIMEOUT:-300}"
TOOLSETS="${HERMES_TOOLSETS:-safe,file}"
IMAGE="${HERMES_DOCKER_IMAGE:-nousresearch/hermes-agent:latest}"
MODEL="${HERMES_MODEL:-}"
PROVIDER="${HERMES_PROVIDER:-}"
ACCEPT_ALL="${HERMES_ACCEPT_ALL:-false}"

mkdir -p "$WORKSPACE" "$HERMES_HOME_TMP"
sudo chown -R 10000:10000 "$HERMES_HOME_TMP" 2>/dev/null || true

docker_args=(
  run --rm
  -v "$WORKSPACE:/workspace"
  -v "$HERMES_HOME_TMP:/opt/data"
  -v "$PROMPT_FILE:/tmp/prompt.txt:ro"
  -w /workspace
  -e HERMES_HOME=/opt/data
  -e HERMES_ACCEPT_HOOKS=1
  -e HERMES_SOURCE=github-action
)

for key in OPENROUTER_API_KEY OPENAI_API_KEY ANTHROPIC_API_KEY DEEPSEEK_API_KEY; do
  if [ -n "${!key:-}" ]; then
    docker_args+=( -e "$key" )
  fi
done

hermes_args=(
  -z "$(cat "$PROMPT_FILE")"
  --toolsets "$TOOLSETS"
  --accept-hooks
  --ignore-user-config
  --source github-action
)

if [ "$ACCEPT_ALL" = "true" ]; then
  hermes_args+=(--yolo)
fi
if [ -n "$MODEL" ]; then
  hermes_args+=(-m "$MODEL")
fi
if [ -n "$PROVIDER" ]; then
  hermes_args+=(--provider "$PROVIDER")
fi

set +e
timeout "$TIMEOUT" docker "${docker_args[@]}" "$IMAGE" "${hermes_args[@]}" > "$OUTPUT_FILE" 2>&1
rc=$?
set -e

printf 'HERMES_EXIT_CODE=%s\n' "$rc"
printf 'HERMES_OUTPUT_FILE=%s\n' "$OUTPUT_FILE"
exit "$rc"
