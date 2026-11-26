#!/usr/bin/env bash
# Steps of the GitHub Action in ../action.yml, kept here so shellcheck covers them.
#
# Usage: review-pr.sh {check|prepare|review|post|cleanup}
#
# The agent step runs in a Podman container through agentic-ci and holds no write token.
# It reads an export of the PR checkout, without its .git, and writes one file. The post step
# holds the token, validates that one file and posts a copy of it through the same review.py
# GitLab uses.
#
# Environment (set by action.yml):
#   AI_REVIEW_EVENT               github.event_name (check)
#   AI_REVIEW_HEAD_REPO           pull_request.head.repo.full_name (check)
#   AI_REVIEW_ACTOR               github.actor (check)
#   AI_REVIEW_TRIGGERING_ACTOR    github.triggering_actor (check)
#   AI_REVIEW_HARNESS             harness input (check, review, post)
#   AI_REVIEW_BASE_SHA            pull_request.base.sha (prepare, review)
#   AI_REVIEW_HEAD_SHA            pull_request.head.sha (prepare, review)
#   AI_REVIEW_INSTRUCTIONS        review_instructions input (prepare)
#   AI_REVIEW_MODEL               model input (review)
#   AI_REVIEW_TIMEOUT_MINUTES     timeout_minutes input (review)
#   AI_REVIEW_AGENTIC_CI_VERSION  agentic_ci_version input (review)
#   AI_REVIEW_IMAGE               image input (review)
#   GITHUB_OUTPUT, GITHUB_REPOSITORY, GITHUB_WORKSPACE, GITHUB_ACTION_PATH:
#                                 set by the runner
#   RUNNER_TEMP                   set by the runner (review: parent of the agent's private TMPDIR,
#                                 post: parent of the copy of the output)
#
# The agent's credentials are not action inputs: the job environment carries the keys
# agentic-ci reads (ANTHROPIC_API_KEY or CLAUDE_CODE_OAUTH_TOKEN for claude-code,
# OPENAI_API_KEY for codex, or Vertex credentials), and they reach these steps unchanged.
#
# The PR head checkout is $GITHUB_WORKSPACE/.ai-review-head. The container never mounts it.
#
# Layout under $GITHUB_WORKSPACE/.ai-review, which the container mounts at /workspace:
#   repo/                         the checkout's working tree, exported without .git
#   pr.diff, instructions.md      inputs of the review
#   plugin/                       a copy of this action, loaded with --plugin-dir
#   out/                          the only place the review writes
#
# prepare and post run in .ai-review-head. review runs in .ai-review, not in a checkout, so
# uvx does not read a uv config that arrives with the PR. check and cleanup run anywhere.

set -euo pipefail

AI_DIR="${GITHUB_WORKSPACE:-}/.ai-review"
HEAD_DIR="${GITHUB_WORKSPACE:-}/.ai-review-head"
DIFF_FILE="$AI_DIR/pr.diff"
INSTRUCTIONS_FILE="$AI_DIR/instructions.md"
PLUGIN_DIR="$AI_DIR/plugin"
OUT_DIR="$AI_DIR/out"
OUT="$OUT_DIR/ai-review-output.json"
# The container's view of AI_DIR, which the prompt names.
CONTAINER_DIR=/workspace
# Reasoning effort of the agent, which the summary footer also reports.
AGENT_EFFORT=high
# The runner image of each harness, used when the image input is empty.
CLAUDE_RUNNER_IMAGE="quay.io/aipcc/agentic-ci/claude-runner:latest"
CODEX_RUNNER_IMAGE="quay.io/aipcc/agentic-ci/codex-runner:latest"
# The largest review output the post step accepts, in bytes.
MAX_OUTPUT_BYTES=1048576
REVIEW_PY_REL="skills/gitlab-code-review/scripts/review.py"
# The private TMPDIR of the review step, which the EXIT trap removes.
REVIEW_TMP=""

error() {
    echo "::error::$*"
    exit 1
}

notice() {
    echo "::notice::$*"
}

# A value like 1 or true; empty, 0 and false are off.
is_on() {
    case "${1:-}" in
        "" | 0 | false | False | FALSE) return 1 ;;
        *) return 0 ;;
    esac
}

require_workspace() {
    [ -n "${GITHUB_WORKSPACE:-}" ] || error "GITHUB_WORKSPACE is not set"
    case "$AI_DIR" in
        /*) ;;
        *) error "GITHUB_WORKSPACE must be an absolute path, got: $GITHUB_WORKSPACE" ;;
    esac
}

# A PR that was checked out at the workspace root before this action ran can commit a link
# named like a workdir. prepare would delete under its target and the container would mount it.
refuse_redirected_workdirs() {
    [ ! -L "$AI_DIR" ] || error "$AI_DIR is a symlink, so the action stopped before touching it"
    [ ! -e "$AI_DIR" ] || [ -d "$AI_DIR" ] \
        || error "$AI_DIR exists and is not a directory, so the action stopped before touching it"
    [ ! -L "$HEAD_DIR" ] || error "$HEAD_DIR is a symlink, so the action stopped before touching it"
}

# The workdir becomes a container mount, which cannot carry whitespace.
reject_whitespace() {
    case "$1" in
        *[[:space:]]*) error "Path contains whitespace, which the container mount cannot carry: $1" ;;
    esac
}

require_sha() {
    local name="$1" value="${2:-}"
    [[ "$value" =~ ^[0-9a-f]{40}$ ]] || error "$name must be a 40-character hex commit SHA, got: '$value'"
}

# Apply the git overrides that stop a hostile checkout config from running code.
# GIT_CONFIG_COUNT entries beat every config file, so they cover review.py's own git calls too.
harden_git() {
    export GIT_CONFIG_COUNT=2
    export GIT_CONFIG_KEY_0=core.fsmonitor GIT_CONFIG_VALUE_0=false
    export GIT_CONFIG_KEY_1=core.hooksPath GIT_CONFIG_VALUE_1=/dev/null
}

# Whether the job environment carries a credential agentic-ci can use for the harness.
# The keys are agentic-ci's, not the action's: it reads them from the environment.
has_harness_credentials() {
    case "$1" in
        codex)
            [ -n "${OPENAI_API_KEY:-}" ]
            ;;
        claude-code)
            [ -n "${ANTHROPIC_API_KEY:-}" ] || [ -n "${CLAUDE_CODE_OAUTH_TOKEN:-}" ] \
                || is_on "${CLAUDE_CODE_USE_VERTEX:-}" || [ -n "${GCLOUD_CREDENTIALS:-}" ] \
                || [ -n "${GCP_SERVICE_ACCOUNT_KEY:-}" ]
            ;;
        # An unknown harness is not skipped here: the review step rejects it loudly.
        *) return 0 ;;
    esac
}

cmd_check() {
    refuse_redirected_workdirs
    local skip_reason=""
    local actor="${AI_REVIEW_ACTOR:-}" triggering="${AI_REVIEW_TRIGGERING_ACTOR:-}"
    local harness="${AI_REVIEW_HARNESS:-claude-code}"

    if [ "${AI_REVIEW_EVENT:-}" != "pull_request" ]; then
        error "This action only supports the pull_request event, got '${AI_REVIEW_EVENT:-}'. Do not use pull_request_target: it runs with write access on code from forks."
    fi

    if [ -z "${AI_REVIEW_HEAD_REPO:-}" ] || [ "${AI_REVIEW_HEAD_REPO}" != "${GITHUB_REPOSITORY:-}" ]; then
        skip_reason="the pull request comes from a fork, where the workflow has no secrets and a read-only token"
    elif [ "$actor" = "dependabot[bot]" ] || [ "$triggering" = "dependabot[bot]" ]; then
        skip_reason="the run was triggered by Dependabot, which gets no secrets and a read-only token"
    elif ! has_harness_credentials "$harness"; then
        case "$harness" in
            codex)
                skip_reason="OPENAI_API_KEY is not set, which the codex harness needs"
                ;;
            *)
                skip_reason="none of ANTHROPIC_API_KEY, CLAUDE_CODE_OAUTH_TOKEN, CLAUDE_CODE_USE_VERTEX, GCLOUD_CREDENTIALS or GCP_SERVICE_ACCOUNT_KEY is set, which the claude-code harness needs"
                ;;
        esac
    fi

    if [ -n "$skip_reason" ]; then
        notice "Skipping the AI review: $skip_reason."
        echo "skip=true" >>"${GITHUB_OUTPUT:-/dev/null}"
    else
        echo "skip=false" >>"${GITHUB_OUTPUT:-/dev/null}"
    fi
}

cmd_prepare() {
    require_workspace
    refuse_redirected_workdirs
    [ -n "${GITHUB_ACTION_PATH:-}" ] || error "GITHUB_ACTION_PATH is not set"
    local plugin_part
    for plugin_part in .claude-plugin skills; do
        [ -d "$GITHUB_ACTION_PATH/$plugin_part" ] \
            || error "$GITHUB_ACTION_PATH/$plugin_part is missing: the action's own files are incomplete"
    done
    reject_whitespace "$AI_DIR"
    local base="${AI_REVIEW_BASE_SHA:-}" head="${AI_REVIEW_HEAD_SHA:-}"
    require_sha "pull_request.base.sha" "$base"
    require_sha "pull_request.head.sha" "$head"

    local checked_out
    checked_out="$(git rev-parse HEAD)"
    [ "$checked_out" = "$head" ] \
        || error "The checkout is at $checked_out, expected the PR head $head"

    local merge_base
    merge_base="$(git merge-base "$base" "$head")" \
        || error "No merge base between $base and $head: the checkout lacks history"

    mkdir -p -- "$AI_DIR"
    rm -rf -- "$OUT_DIR" "$PLUGIN_DIR" "$AI_DIR/repo"
    rm -f -- "$DIFF_FILE" "$INSTRUCTIONS_FILE"
    mkdir -p "$OUT_DIR" "$PLUGIN_DIR" "$AI_DIR/repo"
    # --text: the PR's .gitattributes must not mark files binary and hide their changes.
    # The prefixes and color are fixed so no runner-wide git config changes the format.
    git diff --no-ext-diff --no-textconv --text --no-color --src-prefix=a/ --dst-prefix=b/ \
        "$merge_base" "$head" >"$DIFF_FILE"

    # The agent gets the working tree without .git, so it cannot reach the config, the object
    # store or anything git would run later. Not git archive: it honors the PR's export-ignore.
    tar -C . --exclude=./.git -cf - . | tar -C "$AI_DIR/repo" -xf -

    # Claude Code loads this copy, not the registry copy in the image, which lacks the skill.
    # Codex takes no plugin directory: it loads the skill installed in its runner image.
    # Only the plugin and the skills go in: with uses: ./ the action path is the workspace root,
    # where earlier steps can leave credentials, and the workdirs themselves.
    tar -C "$GITHUB_ACTION_PATH" -cf - .claude-plugin skills | tar -C "$PLUGIN_DIR" -xf -

    if [ -n "${AI_REVIEW_INSTRUCTIONS:-}" ]; then
        printf '%s' "$AI_REVIEW_INSTRUCTIONS" >"$INSTRUCTIONS_FILE"
    fi
}

cmd_review() {
    require_workspace
    refuse_redirected_workdirs
    reject_whitespace "$AI_DIR"
    local harness="${AI_REVIEW_HARNESS:-claude-code}"
    case "$harness" in
        claude-code | codex) ;;
        *) error "harness must be claude-code or codex, got: '$harness'" ;;
    esac
    local base="${AI_REVIEW_BASE_SHA:-}" head="${AI_REVIEW_HEAD_SHA:-}"
    require_sha "pull_request.base.sha" "$base"
    require_sha "pull_request.head.sha" "$head"
    [ -f "$DIFF_FILE" ] || error "$DIFF_FILE is missing: the prepare step did not run"
    if [ "$harness" = "claude-code" ] && [ ! -d "$PLUGIN_DIR" ]; then
        error "$PLUGIN_DIR is missing: the prepare step did not run"
    fi

    local image="${AI_REVIEW_IMAGE:-}" version="${AI_REVIEW_AGENTIC_CI_VERSION:-}"
    if [ -z "$image" ]; then
        case "$harness" in
            claude-code) image="$CLAUDE_RUNNER_IMAGE" ;;
            codex) image="$CODEX_RUNNER_IMAGE" ;;
        esac
    fi
    [ -n "${RUNNER_TEMP:-}" ] || error "RUNNER_TEMP is not set"
    case "$image" in
        *[[:space:]]*) error "image must not contain whitespace, got: '$image'" ;;
    esac
    local version_re='^[0-9][0-9A-Za-z.+!-]*$'
    [[ "$version" =~ $version_re ]] || error "agentic_ci_version must be a package version such as 0.5.15, got: '$version'"
    local minutes="${AI_REVIEW_TIMEOUT_MINUTES:-20}"
    [[ "$minutes" =~ ^[1-9][0-9]*$ ]] || error "timeout_minutes must be a positive integer, got: '$minutes'"

    # The agent's credentials arrive from the job environment, the keys agentic-ci reads.
    # Nothing that can write to the repository or mint tokens reaches the agent.
    export GITHUB_TOKEN='' GH_TOKEN=''
    export ACTIONS_ID_TOKEN_REQUEST_TOKEN='' ACTIONS_ID_TOKEN_REQUEST_URL=''
    # uvx runs in the workdir, but no uv config should steer it.
    export UV_NO_CONFIG=1

    # agentic-ci writes the resolved cloud credentials into a temp directory and never deletes
    # it, so its run gets its own under RUNNER_TEMP and the trap removes it on every exit. Only
    # that run uses it: the image pull keeps the default, which may have more room.
    REVIEW_TMP="$(mktemp -d "$RUNNER_TEMP/ai-review-tmp.XXXXXX")"
    trap 'rm -rf -- "$REVIEW_TMP"' EXIT

    # The pull is about 1.3 GB, so it gets its own limit and stays out of the agent's minutes.
    # A local build (localhost/...) is not in a registry, and agentic-ci skips the pull for it too.
    case "$image" in
        localhost/*) ;;
        *) timeout 15m podman pull "$image" || error "Could not pull the image $image" ;;
    esac

    # Every path the prompt carries is a container path. Claude Code runs the skill from the
    # action's own plugin copy, so the prompt is its slash command. Codex has no plugin flag:
    # it loads the skill installed in its runner image, and the prompt names it the way
    # agentic-ci's skill runs do.
    local prompt
    if [ "$harness" = "claude-code" ]; then
        prompt="/code-review-skills:github-code-review $CONTAINER_DIR/repo $CONTAINER_DIR/pr.diff $base $head $CONTAINER_DIR/out/ai-review-output.json"
    else
        prompt="Use the /github-code-review skill with the arguments: $CONTAINER_DIR/repo $CONTAINER_DIR/pr.diff $base $head $CONTAINER_DIR/out/ai-review-output.json"
    fi
    if [ -f "$INSTRUCTIONS_FILE" ]; then
        prompt="$prompt $CONTAINER_DIR/instructions.md"
    fi

    # The container is the sandbox: agentic-ci runs the agent with every tool enabled, so a
    # tool allowlist would not hold. The container never mounts the checkout's .git, and the
    # post step reads one validated file from the mount.
    local args=(
        run
        --harness "$harness"
        --backend podman
        --workdir "$AI_DIR"
        --image "$image"
        --no-otel
        --timeout "$(( (minutes + 5) * 60 ))"
        --effort "$AGENT_EFFORT"
    )
    if [ -n "${AI_REVIEW_MODEL:-}" ]; then
        args+=(--model "$AI_REVIEW_MODEL")
    fi
    local agent_args=(uvx --from "agentic-ci==$version" agentic-ci "${args[@]}" "$prompt")
    if [ "$harness" = "claude-code" ]; then
        agent_args+=(-- --plugin-dir "$CONTAINER_DIR/plugin")
    fi

    # SIGINT makes agentic-ci stop its container before it exits. Its own timeout has a margin
    # over this limit, so this one fires first. Status 124 is a timeout; any other failure is
    # the agent's own.
    local status=0
    TMPDIR="$REVIEW_TMP" timeout -s INT --kill-after=60s "${minutes}m" \
        "${agent_args[@]}" || status=$?
    # 137 is the kill after the grace period, when the agent ignores the first signal. It is
    # also what an out-of-memory kill looks like.
    case "$status" in
        124) error "The review did not finish within $minutes minutes" ;;
        137) error "The review was killed (exit 137): it ran past the $minutes minute limit or the container ran out of memory" ;;
    esac
    [ "$status" -eq 0 ] || exit "$status"

    # The run can exit 0 when the agent wrote nothing.
    [ -s "$OUT" ] || error "The review produced no output at $OUT"
}

cmd_post() {
    require_workspace
    refuse_redirected_workdirs
    [ -n "${GITHUB_ACTION_PATH:-}" ] || error "GITHUB_ACTION_PATH is not set"
    [ -n "${RUNNER_TEMP:-}" ] || error "RUNNER_TEMP is not set"
    local review_py="$GITHUB_ACTION_PATH/$REVIEW_PY_REL"
    [ -x "$review_py" ] || error "$review_py is missing or not executable"

    # The mount is the one place the agent could write, so its output is untrusted: a
    # symlink there could point review.py at any file the runner can read.
    [ ! -L "$OUT_DIR" ] || error "$OUT_DIR is a symlink, so nothing was posted"
    [ ! -L "$OUT" ] || error "$OUT is a symlink, so nothing was posted"
    [ -f "$OUT" ] || error "No review output at $OUT"
    local size
    size="$(wc -c <"$OUT")"
    [ "$size" -gt 0 ] || error "The review output at $OUT is empty"
    [ "$size" -le "$MAX_OUTPUT_BYTES" ] \
        || error "The review output at $OUT is $size bytes, over the $MAX_OUTPUT_BYTES limit, so nothing was posted"

    # review.py reads a copy that the agent can no longer touch.
    local copy_dir
    copy_dir="$(mktemp -d "$RUNNER_TEMP/ai-review-post.XXXXXX")"
    cp -- "$OUT" "$copy_dir/ai-review-output.json"

    # The checkout is not in the mount, but its git config is still the PR's.
    harden_git

    # What agentic-ci exports inside its sandbox, for the footer.
    export AGENT_TOOL="${AI_REVIEW_HARNESS:-claude-code}" AGENT_REASONING_EFFORT="$AGENT_EFFORT"

    # uv must ignore config and .python-version files that arrive with the PR.
    UV_NO_CONFIG=1 UV_PYTHON="$(command -v python3)" \
        "$review_py" post --platform github "$copy_dir/ai-review-output.json"
}

# agentic-ci stops its container only when it exits normally. A SIGKILL (the timeout's kill
# after the grace period, or a cancelled run) leaves the container running with the model
# credentials, so remove any agentic-ci container that mounts this run's workdir.
remove_review_containers() {
    command -v podman >/dev/null 2>&1 || return 0
    local id source failed=0
    while IFS= read -r id; do
        [ -n "$id" ] || continue
        while IFS= read -r source; do
            if [ "$source" = "$AI_DIR" ]; then
                podman rm -f "$id" >/dev/null || failed=1
                break
            fi
        done < <(podman inspect --format '{{range .Mounts}}{{println .Source}}{{end}}' "$id" 2>/dev/null)
    done < <(podman ps -a --filter 'name=^agentic-ci-' --format '{{.ID}}' 2>/dev/null)
    return "$failed"
}

cmd_cleanup() {
    [ -n "${GITHUB_WORKSPACE:-}" ] || error "GITHUB_WORKSPACE is not set"

    # Attempt every removal: one that fails must not leave the credential directory behind.
    local dir failed=0
    remove_review_containers || failed=1
    for dir in "$AI_DIR" "$HEAD_DIR"; do
        if [ -d "$dir" ] && [ ! -L "$dir" ]; then
            rm -rf -- "$dir" || failed=1
        fi
    done

    # A review step that was killed before its trap ran leaves its private TMPDIR behind.
    if [ -n "${RUNNER_TEMP:-}" ]; then
        for dir in "$RUNNER_TEMP"/ai-review-tmp.*; do
            if [ -d "$dir" ] && [ ! -L "$dir" ]; then
                rm -rf -- "$dir" || failed=1
            fi
        done
    fi

    [ "$failed" -eq 0 ] || error "Could not remove everything the action left behind"
}

main() {
    case "${1:-}" in
        check) cmd_check ;;
        prepare) cmd_prepare ;;
        review) cmd_review ;;
        post) cmd_post ;;
        cleanup) cmd_cleanup ;;
        *)
            echo "Usage: $0 {check|prepare|review|post|cleanup}" >&2
            exit 2
            ;;
    esac
}

main "$@"
