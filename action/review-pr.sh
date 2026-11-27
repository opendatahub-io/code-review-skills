#!/usr/bin/env bash
# Steps of the GitHub Action in ../action.yml, kept here so shellcheck covers them.
#
# Usage: review-pr.sh {check|prepare|standdown|review|post|export|relay|cleanup}
#
# The agent step holds no write token. By default (backend openshell) the openshell image runs
# as a privileged docker container, and agentic-ci inside it runs the agent in an OpenShell
# sandbox, the way agentic-ci's own end-to-end job runs it on hosted runners. With backend podman
# agentic-ci runs on the runner and starts a Podman container.
# The agent reads an export of the PR checkout with a git repository of its own, built without
# the checkout's config, remotes or credentials, and writes one file. The post step holds the
# token, validates that one file and posts a copy of it through the same review.py GitLab uses.
# The standdown step also holds the token, only to read the Actions API: it skips the review when
# a review that should win makes it redundant (a deep review of the same head for a standard
# review, a newer head for a deep review). The post step checks again before it writes.
# agentic-ci collects the agent's OpenTelemetry data. The review step copies the log to the runner
# and totals its tokens and cost with the same agentic-ci release, the post step adds that usage
# line to the summary, the export step pushes the log to the caller's MLflow when asked, and the
# relay step uploads it to the review-telemetry relay, which pushes it to the Red Hat MLflow.
#
# Environment (set by action.yml):
#   AI_REVIEW_EVENT               github.event_name (check)
#   AI_REVIEW_HEAD_REPO           pull_request.head.repo.full_name (check)
#   AI_REVIEW_ACTOR               github.actor (check)
#   AI_REVIEW_TRIGGERING_ACTOR    github.triggering_actor (check)
#   AI_REVIEW_HARNESS             harness input (check, review, post)
#   AI_REVIEW_MODE                mode input: standard or deep (check, prepare, standdown,
#                                 review, post, relay)
#   AI_REVIEW_DEEP_JOB            deep_review_job input (standdown, post)
#   AI_REVIEW_DEEP_LABEL          deep_review_label input (check, post)
#   AI_REVIEW_RECOMMEND_DEEP      recommend_deep_review input (check, prepare, post)
#   AI_REVIEW_SUGGEST_REVIEWERS   suggest_reviewers input (post)
#   AI_REVIEW_BOT_LOGIN           account the post token belongs to, empty for
#                                 github-actions[bot] (check validates the bot_login input,
#                                 post passes it to review.py); never the token
#   AI_REVIEW_CALLER_TOKEN        whether the github_token input is set, never the token (post)
#   AI_REVIEW_DRAFT               pull_request.draft (check)
#   AI_REVIEW_BACKEND             backend input: openshell or podman (review, cleanup)
#   AI_REVIEW_BASE_SHA            pull_request.base.sha (prepare, review)
#   AI_REVIEW_HEAD_SHA            pull_request.head.sha (prepare, review)
#   AI_REVIEW_INSTRUCTIONS        review_instructions input (prepare, review)
#   AI_REVIEW_ORG_RULES           org_review_rules input: a path to the caller-fetched rules
#                                 file (prepare)
#   AI_REVIEW_MODEL               model input (review)
#   AI_REVIEW_TIMEOUT_MINUTES     timeout_minutes input (review)
#   AI_REVIEW_AGENTIC_CI_VERSION  agentic_ci_version input (review, export, relay, cleanup): a PyPI
#                                 version for podman, an openshell image tag, optionally
#                                 <tag>@sha256:<digest>, for openshell
#   AI_REVIEW_IMAGE               image input (review)
#   AI_REVIEW_MLFLOW_EXPERIMENT   mlflow_experiment_name input (export, relay)
#   MLFLOW_TRACKING_URI, MLFLOW_TRACKING_TOKEN
#                                 the MLflow endpoint and its optional token, from the job
#                                 environment (export only: the review step blanks them)
#   REVIEW_TELEMETRY_PROJECT, REVIEW_TELEMETRY_USERNAME, REVIEW_TELEMETRY_TOKEN
#                                 the relay's GitLab project id and deploy token, from the job
#                                 environment (relay only; export reads only whether they are
#                                 set; the review step blanks them)
#   GITHUB_OUTPUT, GITHUB_REPOSITORY, GITHUB_WORKSPACE, GITHUB_ACTION_PATH, GITHUB_RUN_ID,
#   GITHUB_RUN_ATTEMPT, GITHUB_JOB: set by the runner (GITHUB_REPOSITORY, the run id and attempt
#                                 and the job also name the relay's package)
#   RUNNER_TEMP                   set by the runner (review: parent of the agent's private TMPDIR,
#                                 post: parent of the copy of the output and of the flag that the
#                                 summary was posted, prepare and post: parent of the triage plan,
#                                 review, post, export and relay: parent of the usage
#                                 directory, standdown, export and relay: their working directory)
#   GITHUB_TOKEN                  github.token (standdown); the github_token input, else a
#                                 minted App token, else github.token (post); no other step
#                                 gets one
#
# The agent's credentials are not action inputs: the job environment carries the keys
# agentic-ci reads (ANTHROPIC_API_KEY or CLAUDE_CODE_OAUTH_TOKEN for claude-code,
# OPENAI_API_KEY for codex, or Vertex credentials), and they reach these steps unchanged.
#
# The PR head checkout is $GITHUB_WORKSPACE/.ai-review-head. The container never mounts it.
#
# Layout under $GITHUB_WORKSPACE/.ai-review, which the agent sees at /sandbox/.ai-review
# (openshell) or /workspace (podman):
#   repo/                         the checkout's working tree, exported without its .git, plus
#                                 a fresh .git with the history of the base and the head
#   pr.diff, instructions.md      inputs of the review
#   org-rules.yaml                the org review rules, in both modes
#   plugin/                       a copy of this action, loaded with --plugin-dir
#   out/                          the only place the review writes
#   telemetry/                    where the openshell container's agentic-ci copies its OTEL log
#
# The triage plan of the deep-review recommendation is $RUNNER_TEMP/ai-review-triage/plan.json,
# outside the mount, written by prepare before any agent runs and read only by post.
#
# The usage directory is $RUNNER_TEMP/ai-review-usage (mode 700), outside the mount: the review
# step copies the OTEL log there once the agent has stopped and writes usage.json next to it, the
# post step passes usage.json to review.py, the export step pushes the log copy to MLflow, and the
# relay step writes meta.json there and uploads both.
# With podman, agentic-ci copies the log into a private $RUNNER_TEMP/ai-review-otel.* directory
# instead of the checkout, and the review step removes it.
#
# prepare and post run in .ai-review-head. standdown runs in RUNNER_TEMP, before the checkout
# exists, and export and relay run there too. review runs in .ai-review, not in a checkout, so
# uvx does not read a uv config that arrives with the PR. check and cleanup run anywhere.

set -euo pipefail

AI_DIR="${GITHUB_WORKSPACE:-}/.ai-review"
HEAD_DIR="${GITHUB_WORKSPACE:-}/.ai-review-head"
DIFF_FILE="$AI_DIR/pr.diff"
INSTRUCTIONS_FILE="$AI_DIR/instructions.md"
ORG_RULES_FILE="$AI_DIR/org-rules.yaml"
PLUGIN_DIR="$AI_DIR/plugin"
OUT_DIR="$AI_DIR/out"
OUT="$OUT_DIR/ai-review-output.json"
# The agent's view of AI_DIR, which the prompt names, per backend. OpenShell uploads the workdir
# into the sandbox's home under its own name.
PODMAN_CONTAINER_DIR=/workspace
OPENSHELL_CONTAINER_DIR=/sandbox/.ai-review
# The image that runs agentic-ci and the OpenShell gateway for the openshell backend. Its tag is
# the agentic_ci_version input, so the bundled agentic-ci is the version the action asks for.
# openshell_image_ref builds the reference.
OPENSHELL_IMAGE_REPO="quay.io/aipcc/agentic-ci/openshell"
# The plugin the agent's skill catalog is narrowed to, as the GitLab job does.
ENABLED_PLUGINS="code-review-skills"
# The label on every docker container and volume of a run, whose value is the absolute AI_DIR,
# so cleanup finds them after a SIGKILL.
WORKDIR_LABEL="ai-review.workdir"
# The variables the openshell container gets, by name only: docker copies each value from the
# review step's environment, so no value is on a command line. Model, effort and image come only
# from flags, and no GitHub token or OIDC variable is ever listed.
CODEX_CREDENTIAL_VARS="OPENAI_API_KEY"
CLAUDE_CREDENTIAL_VARS="ANTHROPIC_API_KEY CLAUDE_CODE_OAUTH_TOKEN GCLOUD_CREDENTIALS GCP_SERVICE_ACCOUNT_KEY ANTHROPIC_VERTEX_PROJECT_ID GCP_PROJECT_ID GOOGLE_CLOUD_PROJECT CLOUD_ML_REGION VERTEX_LOCATION CLAUDE_CODE_MAX_RETRIES"
# Pulls the sandbox image (when given) and the OpenShell supervisor and runtime images, which the
# openshell image names in its environment, into the podman storage the agent container reuses.
# shellcheck disable=SC2016 # expanded by bash in the container
PREPULL_SCRIPT='set -e; for i in "$@" "$OPENSHELL_SUPERVISOR_IMAGE" "$OPENSHELL_SANDBOX_RUNTIME_IMAGE"; do [ -z "$i" ] || podman pull --quiet "$i" >/dev/null; done'
# The agent's wall-clock limit in minutes when the timeout_minutes input is empty, per mode.
STANDARD_TIMEOUT_MINUTES=20
DEEP_TIMEOUT_MINUTES=75
# Reasoning effort of the agent, which the summary footer also reports.
AGENT_EFFORT=high
# The default image of each harness when the image input is empty. The sandbox images are for
# the openshell backend: they keep their config under a user that the podman backend does not
# run the agent as, so podman keeps the runner images. They are the agentic_ci_version release,
# and move with it.
# renovate: datasource=docker
CLAUDE_RUNNER_IMAGE="quay.io/aipcc/agentic-ci/claude-runner:0.5.16"
# renovate: datasource=docker
CODEX_RUNNER_IMAGE="quay.io/aipcc/agentic-ci/codex-runner:0.5.16"
# renovate: datasource=docker
CLAUDE_SANDBOX_IMAGE="quay.io/aipcc/agentic-ci/claude-sandbox:0.5.16"
# renovate: datasource=docker
CODEX_SANDBOX_IMAGE="quay.io/aipcc/agentic-ci/codex-sandbox:0.5.16"
# The oldest git whose fetch has --no-write-fetch-head and --no-auto-maintenance.
MIN_GIT_MAJOR=2
MIN_GIT_MINOR=29
# The largest review output the post step accepts, in bytes.
MAX_OUTPUT_BYTES=1048576
# The largest org review rules file prepare copies, in bytes. The real security-config file
# is about 63 KB; a bigger input means the caller pointed org_review_rules at the wrong file.
MAX_RULES_BYTES=102400
REVIEW_PY_REL="skills/gitlab-code-review/scripts/review.py"
USAGE_PY_REL="action/usage.py"
PANEL_PY_REL="skills/code-review-panel/scripts/panel.py"
# The directory under RUNNER_TEMP that holds the triage plan, and the plan's largest size in bytes,
# which is also what review.py reads.
TRIAGE_DIR_NAME="ai-review-triage"
MAX_PLAN_BYTES=1048576
# The wall-clock limit of the triage, which reads only git metadata and the diff.
TRIAGE_TIMEOUT=60s
# The OTEL log agentic-ci copies out of its run directory, under agentic-ci's own file name, and
# the directory of the mount that the openshell container's agentic-ci copies it to: not out/,
# whose one file the post step reads as the review.
OTEL_LOG_NAME="claude-otel.jsonl"
TELEMETRY_DIR="$AI_DIR/telemetry"
# The directory under RUNNER_TEMP that holds the runner's copy of the OTEL log and usage.json.
USAGE_DIR_NAME="ai-review-usage"
# The largest OTEL log the action reads, in bytes: 64 of the collector's largest exports (1 MiB
# each). That leaves room for a long deep review, while it bounds the copy and the memory the
# usage totals take on the runner, since the log comes from where the agent could write.
MAX_OTEL_LOG_BYTES=67108864
# The exit status of usage.py when the log holds no token data.
NO_USAGE_EXIT=3
# The wall-clock limits of the usage totals and of the MLflow export. Both can first fetch
# agentic-ci into uv's cache, and the export makes one request per trace payload.
USAGE_TIMEOUT=5m
MLFLOW_TIMEOUT=10m
# The largest CA bundle the export copies into its container, in bytes.
MAX_CA_BYTES=10485760
# The proxy variables the tool containers get: the runner's own for the export, empty otherwise.
TOOL_PROXY_VARS="HTTP_PROXY HTTPS_PROXY NO_PROXY FTP_PROXY ALL_PROXY http_proxy https_proxy no_proxy ftp_proxy all_proxy"
# The upload contract of the review-telemetry relay (its README's "Upload contract"), which pushes
# the uploads to the Red Hat MLflow. It is shared with that project: change both together. The
# host is part of it, so the deploy token only ever goes to gitlab.com; the project id comes from
# REVIEW_TELEMETRY_PROJECT, which the relay's maintainers give out with the token.
RELAY_API="https://gitlab.com/api/v4"
RELAY_PACKAGE="github-review-otel"
RELAY_META_NAME="meta.json"
# The relay's RELAY_MAX_BYTES default and its meta.json limit, in bytes.
RELAY_MAX_LOG_BYTES=67108864
RELAY_MAX_META_BYTES=65536
# The wall-clock limit of each upload, in seconds, and of connecting.
RELAY_MAX_TIME=300
RELAY_CONNECT_TIMEOUT=30
# The deep_review_job input's default, which review.py also uses.
DEFAULT_DEEP_JOB="deep-review"
# The deep_review_label input's default, which review.py also uses.
DEFAULT_DEEP_LABEL="deep-review"
# The exit status of review.py stand-down when a review that should win makes this one redundant.
STANDDOWN_EXIT=10
# The private TMPDIR of the review step, which the EXIT trap removes.
REVIEW_TMP=""
# The private directory agentic-ci copies its OTEL log to with podman, which the EXIT trap removes.
OTEL_TMP=""
# Where the agent run's OTEL log lands, set by the backend's run function.
AGENT_OTEL_LOG=""
# The scratch directory of the prepare step's git calls, which the EXIT trap removes.
PREPARE_TMP=""
# standard or deep, set by require_mode.
REVIEW_MODE=""
# The exit status of the agent run.
AGENT_STATUS=0

error() {
    echo "::error::$*"
    exit 1
}

notice() {
    echo "::notice::$*"
}

warning() {
    echo "::warning::$*"
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

# An empty mode is the input's default.
require_mode() {
    REVIEW_MODE="${AI_REVIEW_MODE:-standard}"
    case "$REVIEW_MODE" in
        standard | deep) ;;
        *) error "mode must be standard or deep, got: '$REVIEW_MODE'" ;;
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

# The image for a harness on a backend, when the image input is empty.
default_image() {
    case "$1:$2" in
        claude-code:podman) echo "$CLAUDE_RUNNER_IMAGE" ;;
        codex:podman) echo "$CODEX_RUNNER_IMAGE" ;;
        claude-code:openshell) echo "$CLAUDE_SANDBOX_IMAGE" ;;
        codex:openshell) echo "$CODEX_SANDBOX_IMAGE" ;;
        *) return 1 ;;
    esac
}

# The display name of the deep review job. An empty one is the input's default.
deep_job_name() {
    echo "${AI_REVIEW_DEEP_JOB:-$DEFAULT_DEEP_JOB}"
}

# The label that starts the deep review. An empty one is the input's default.
deep_label_name() {
    echo "${AI_REVIEW_DEEP_LABEL:-$DEFAULT_DEEP_LABEL}"
}

# The openshell image for an agentic_ci_version: a tag, or a tag pinned to a digest as
# <tag>@sha256:<64 lowercase hex>, which docker pulls and runs by the digest. Fails on anything
# else, so the agent run and the cleanup run only a reference this check accepted.
openshell_image_ref() {
    # Plain ASCII ranges, whatever the runner's locale.
    local LC_ALL=C
    [[ "${1:-}" =~ ^[0-9][0-9A-Za-z._-]*(@sha256:[0-9a-f]{64})?$ ]] || return 1
    echo "$OPENSHELL_IMAGE_REPO:$1"
}

# The directory of the triage plan. Fails when RUNNER_TEMP is not an absolute path, so no caller
# removes or writes a directory at the filesystem root.
triage_dir() {
    case "${RUNNER_TEMP:-}" in
        /?*) echo "$RUNNER_TEMP/$TRIAGE_DIR_NAME" ;;
        *) return 1 ;;
    esac
}

# The usage directory. Fails when RUNNER_TEMP is not an absolute path, like triage_dir, or holds
# a character the tool container's mount cannot carry.
usage_dir() {
    case "${RUNNER_TEMP:-}" in
        *[[:space:]:,]*) return 1 ;;
        /?*) echo "$RUNNER_TEMP/$USAGE_DIR_NAME" ;;
        *) return 1 ;;
    esac
}

# The agentic-ci package version for an agentic_ci_version: an openshell tag pinned to a digest
# is that tag's release. Fails on anything that is not a package version.
agentic_ci_package_version() {
    local LC_ALL=C version="${1%%@*}"
    [[ "$version" =~ ^[0-9][0-9A-Za-z.+!-]*$ ]] || return 1
    echo "$version"
}

# The pinned agentic-ci image that the review pulled, and the engine that runs it, for the usage
# totals and the MLflow export: the openshell image of agentic_ci_version (tag or digest) with
# docker, or the runner image the Podman agent ran in with podman. Both ship python3 and
# agentic-ci, so nothing is fetched from a package index at run time. Sets TOOL_ENGINE and
# TOOL_IMAGE; fails when there is no such image or engine.
#
# Usage: resolve_tool_image BACKEND VERSION AGENT_IMAGE
TOOL_ENGINE=""
TOOL_IMAGE=""
resolve_tool_image() {
    case "$1" in
        openshell)
            TOOL_ENGINE=docker
            TOOL_IMAGE="$(openshell_image_ref "$2")" || return 1
            ;;
        podman)
            TOOL_ENGINE=podman
            TOOL_IMAGE="$3"
            ;;
        *) return 1 ;;
    esac
    [ -n "$TOOL_IMAGE" ] && [[ "$TOOL_IMAGE" != *[[:space:]]* ]] \
        && command -v "$TOOL_ENGINE" >/dev/null 2>&1
}

# Copy the CA bundle that the variable NAME points at into DIR, which the tool container mounts,
# and print the copy's path. A copy, not a mount of its own: relabeling a host CA file for SELinux
# would change its label on the host. Fails without a regular readable file of at most 10 MiB.
#
# Usage: tool_ca_file DIR NAME
tool_ca_file() {
    local dir="$1" name="$2" file
    file="${!name:-}"
    [ -n "$file" ] && [ -f "$file" ] && [ -r "$file" ] || return 1
    [ "$(wc -c <"$file")" -le "$MAX_CA_BYTES" ] || return 1
    cp -- "$file" "$dir/ca-$name.pem" || return 1
    echo "$dir/ca-$name.pem"
}

# Run usage.py (ENTRYPOINT python3) or agentic-ci (ENTRYPOINT agentic-ci) in TOOL_IMAGE with
# TOOL_ENGINE, never pulling it. The container mounts only DIR, the usage directory, at its own
# path, relabeled for SELinux as private to it (:Z); it runs as the runner's user, so its files
# stay the runner's; it gets only the variables ENV_NAMES lists that are set, by name (-e NAME),
# so no secret is on a command line; it is never privileged; it has an init that forwards
# signals; and it carries the workdir label, so cleanup removes it after a kill. Without network
# unless NETWORK is "network", which also passes the proxy variables by name and the CA bundles
# REQUESTS_CA_BUNDLE and SSL_CERT_FILE name, as copies in DIR. A client that times out leaves
# its --rm container running, so the container, which has a name of its own, is removed then.
#
# Usage: run_agentic_ci_tool DIR "ENV_NAMES" NETWORK LIMIT ENTRYPOINT ARGS...
run_agentic_ci_tool() {
    local dir="$1" names="$2" network="$3" limit="$4" entrypoint="$5" name ca status=0
    shift 5
    local container
    container="ai-review-tool-$(od -An -N6 -tx1 /dev/urandom | tr -d ' \n')"
    local args=(run --rm --init --pull never --name "$container" --label "$WORKDIR_LABEL=$AI_DIR")
    # Rootless Podman maps the runner's user into the container only with keep-id.
    if [ "$TOOL_ENGINE" = podman ] && [ "$(id -u)" -ne 0 ]; then
        args+=(--userns=keep-id)
    fi
    args+=(--user "$(id -u):$(id -g)" -v "$dir:$dir:Z" --workdir "$dir")
    args+=(--entrypoint "$entrypoint")
    # Podman copies the host's proxy variables into every container unless told not to, and docker
    # can add the proxies of its client config: every proxy variable is set here instead, to the
    # runner's own value by name for the export, and to empty everywhere else.
    [ "$TOOL_ENGINE" != podman ] || args+=(--http-proxy=false)
    for name in $TOOL_PROXY_VARS; do
        if [ "$network" = network ] && [ -n "${!name:-}" ]; then
            args+=(-e "$name")
        else
            args+=(-e "$name=")
        fi
    done
    if [ "$network" = network ]; then
        # The host's network, so an MLflow server or proxy on the runner itself is reachable.
        args+=(--network host)
        for name in REQUESTS_CA_BUNDLE SSL_CERT_FILE; do
            if ca="$(tool_ca_file "$dir" "$name")"; then
                args+=(-e "$name=$ca")
            fi
        done
    else
        args+=(--network none)
    fi
    for name in $names; do
        [ -z "${!name:-}" ] || args+=(-e "$name")
    done
    timeout --kill-after=30s "$limit" "$TOOL_ENGINE" "${args[@]}" "$TOOL_IMAGE" "$@" </dev/null \
        || status=$?
    # 124 is the timeout, 137 its kill. Either leaves the container behind, still able to write
    # usage.json or push with the token.
    if [ "$status" -eq 124 ] || [ "$status" -eq 137 ]; then
        timeout 1m "$TOOL_ENGINE" rm -f "$container" >/dev/null 2>&1 || true
    fi
    return "$status"
}

# A URL path as requests sends and prints it: every byte outside the characters it leaves alone
# as %XX, so a space shows as %20.
percent_encode_path() {
    local LC_ALL=C text="$1" out="" c i
    local safe="ABCDEFGHIJKLMNOPQRSTUVWXYZabcdefghijklmnopqrstuvwxyz0123456789-._~/!\$&'()*+,;=:@%"
    for ((i = 0; i < ${#text}; i++)); do
        c="${text:i:1}"
        if [[ "$safe" == *"$c"* ]]; then
            out+="$c"
        else
            out+="$(printf '%%%02X' "'$c")"
        fi
    done
    printf '%s\n' "$out"
}

# Every byte of TEXT outside the unreserved characters as %XX, as a URL's userinfo is sent.
percent_encode_strict() {
    local LC_ALL=C text="$1" out="" c i
    for ((i = 0; i < ${#text}; i++)); do
        c="${text:i:1}"
        case "$c" in
            [A-Za-z0-9._~-]) out+="$c" ;;
            *) out+="$(printf '%%%02X' "'$c")" ;;
        esac
    done
    printf '%s\n' "$out"
}

# Add a piece of a secret to MLFLOW_SECRETS with its percent-decoded and percent-encoded forms,
# when it is at least 4 characters: a shorter piece would mask common text all over the output,
# and the whole value it came from is masked anyway.
add_secret_piece() {
    local piece="$1" decoded
    [ "${#piece}" -ge 4 ] || return 0
    MLFLOW_SECRETS+=("$piece")
    decoded="$(printf '%b' "${piece//%/\\x}" 2>/dev/null)" || decoded=""
    [ -z "$decoded" ] || [ "$decoded" = "$piece" ] || MLFLOW_SECRETS+=("$decoded")
    local encoded
    encoded="$(percent_encode_strict "${decoded:-$piece}")"
    [ "$encoded" = "$piece" ] || MLFLOW_SECRETS+=("$encoded")
}

# The values a tool's output must not show, longest first: the MLflow endpoint, its token, the
# relay's deploy token and its user name, every proxy value the export forwards with the
# credentials of a proxy URL (user:password and the password alone), and the endpoint's host and
# path, which HTTP errors print apart from the URL. urllib3 prints the host in lower case and
# requests the path percent-encoded, so those forms are masked too. Sets MLFLOW_SECRETS.
MLFLOW_SECRETS=()
collect_mlflow_secrets() {
    local uri="${MLFLOW_TRACKING_URI:-}" rest host path encoded
    MLFLOW_SECRETS=()
    [ -z "$uri" ] || MLFLOW_SECRETS+=("$uri")
    [ -z "${MLFLOW_TRACKING_TOKEN:-}" ] || MLFLOW_SECRETS+=("$MLFLOW_TRACKING_TOKEN")
    [ -z "${REVIEW_TELEMETRY_TOKEN:-}" ] || MLFLOW_SECRETS+=("$REVIEW_TELEMETRY_TOKEN")
    [ -z "${REVIEW_TELEMETRY_USERNAME:-}" ] || MLFLOW_SECRETS+=("$REVIEW_TELEMETRY_USERNAME")
    # A proxy URL can carry credentials, and a tool that cannot parse it can print it, or a part.
    local name value authority userinfo
    for name in $TOOL_PROXY_VARS; do
        value="${!name:-}"
        [ -n "$value" ] || continue
        MLFLOW_SECRETS+=("$value")
        authority="${value#*://}"
        authority="${authority%%[/?#]*}"
        case "$authority" in
            *@*)
                userinfo="${authority%@*}"
                add_secret_piece "$userinfo"
                case "$userinfo" in
                    *:*) add_secret_piece "${userinfo#*:}" ;;
                esac
                ;;
        esac
    done
    [ -n "$uri" ] || return 0
    rest="${uri#*://}"
    host="${rest%%[/?#]*}"
    host="${host#*@}"
    host="${host%%:*}"
    if [ -n "$host" ]; then
        MLFLOW_SECRETS+=("$host")
        [ "${host,,}" = "$host" ] || MLFLOW_SECRETS+=("${host,,}")
    fi
    case "$rest" in
        */?*)
            path="/${rest#*/}"
            path="${path%%[?#]*}"
            path="${path%/}"
            if [ -n "${path#/}" ]; then
                MLFLOW_SECRETS+=("$path")
                encoded="$(percent_encode_path "$path")"
                [ "$encoded" = "$path" ] || MLFLOW_SECRETS+=("$encoded")
            fi
            ;;
    esac
}

# A line with every MLflow value collect_mlflow_secrets found replaced by ***.
mask_mlflow() {
    local line="$1" secret
    for secret in ${MLFLOW_SECRETS[@]+"${MLFLOW_SECRETS[@]}"}; do
        line="${line//"$secret"/***}"
    done
    printf '%s\n' "$line"
}

# Print a tool's captured output to the job log with the MLflow values masked and workflow
# commands off, since it can quote what a server or the telemetry said, then turn them back on.
print_tool_output() {
    [ -s "$1" ] || return 0
    local token line
    token="$(od -An -N16 -tx1 /dev/urandom | tr -d ' \n')"
    collect_mlflow_secrets
    echo "::stop-commands::$token"
    # The last line can lack its newline; read still returns it.
    while IFS= read -r line || [ -n "$line" ]; do
        mask_mlflow "$line"
    done <"$1"
    echo "::$token::"
}

# A short reason from a tool's captured output for a warning: its last non-empty lines on one
# line, without control characters and with anything secret-looking masked. The MLflow endpoint,
# its host and path and the token never appear, whether or not GitHub masks them.
tool_reason() {
    local line reason=""
    collect_mlflow_secrets
    while IFS= read -r line; do
        line="${line//[[:cntrl:]]/ }"
        line="${line#"${line%%[![:space:]]*}"}"
        line="${line%"${line##*[![:space:]]}"}"
        reason+="${reason:+ | }$(mask_mlflow "$line")"
    done < <(grep -v '^[[:space:]]*$' -- "$1" 2>/dev/null | tail -n 3)
    # Credentials in a header or a URL, and long opaque strings that look like keys.
    reason="$(sed -E 's#(Bearer|Basic|[Tt]oken|[Kk]ey|[Pp]assword)([=: ]+)[^ |]+#\1\2***#g; s#://[^/@ ]*@#://***@#g; s#[A-Za-z0-9_+=-]{32,}#***#g' <<<"$reason")"
    # % would start an escape in the workflow command.
    reason="${reason//%/%25}"
    echo "${reason:0:400}"
}

# The number of records agentic-ci mlflow-push sends from a log, selected as it selects them:
# JSON lines whose path contains /v1/traces and whose payload is not empty. Fails when the log
# cannot be read.
count_trace_records() {
    env -i "PATH=$PATH" python3 -I -c 'import json, sys
count = 0
with open(sys.argv[1], encoding="utf-8", errors="replace") as lines:
    for line in lines:
        line = line.strip()
        if not line:
            continue
        try:
            record = json.loads(line)
        except (ValueError, RecursionError):
            continue
        if not isinstance(record, dict):
            continue
        path = record.get("path", "")
        if isinstance(path, str) and "/v1/traces" in path and record.get("payload"):
            count += 1
print(count)' "$1"
}

# Warn once when the image's agentic-ci, which usage.py reports, is not the agentic_ci_version
# release: with Podman the agent runs that release from PyPI, but the totals and the export use
# the runner image, whose release is its own. The usage line is still shown, priced by the
# image's release.
#
# Usage: check_tool_version VERSION OUTPUT
check_tool_version() {
    local wanted found
    wanted="$(agentic_ci_package_version "$1")" || return 0
    found="$(sed -n 's/^agentic-ci version: \([0-9][0-9A-Za-z.+!-]*\)$/\1/p' -- "$2" | tail -n 1)"
    [ -n "$found" ] && [ "$found" != "$wanted" ] || return 0
    warning "The agentic-ci image $TOOL_IMAGE has agentic-ci $found, while agentic_ci_version is $wanted: the usage line was priced, and the MLflow export runs, with agentic-ci $found. Set the image input to the runner image of $wanted to match."
}

# Copy the agent run's OTEL log to the usage directory and total its tokens and cost into
# usage.json there, for the summary and the job log. The log comes from where the agent could
# write, so a link, anything but a regular file or a log over MAX_OTEL_LOG_BYTES is refused, and
# usage.py reads only numbers from it, in the pinned agentic-ci image without network. Usage only
# informs, so this never fails the step: a missing or refused log, or totals that cannot be made,
# mean no usage line.
#
# Usage: collect_usage BACKEND VERSION AGENT_IMAGE LOG
collect_usage() {
    local backend="$1" version="$2" agent_image="$3" log="$4" dir parent size status=0
    local no_line="so the summary will not show the review's token usage."
    if ! dir="$(usage_dir)"; then
        warning "RUNNER_TEMP is not set or holds a character a container mount cannot carry, $no_line"
        return 0
    fi
    if [ -z "$log" ]; then
        notice "agentic-ci left no telemetry log, $no_line"
        return 0
    fi
    parent="${log%/*}"
    if [ -L "$parent" ] || [ -L "$log" ]; then
        warning "The telemetry log or its directory is a symlink, $no_line"
        return 0
    fi
    if [ ! -e "$log" ]; then
        notice "agentic-ci left no telemetry log, $no_line"
        return 0
    fi
    if [ ! -f "$log" ]; then
        warning "The telemetry log is not a regular file, $no_line"
        return 0
    fi
    size="$(wc -c <"$log")" || size=-1
    if [ "$size" -lt 0 ] || [ "$size" -gt "$MAX_OTEL_LOG_BYTES" ]; then
        warning "The telemetry log is over $MAX_OTEL_LOG_BYTES bytes or unreadable, $no_line"
        return 0
    fi
    local copy="${dir:?}/$OTEL_LOG_NAME" totals="${dir:?}/usage.json"
    # head keeps one byte past the limit, so a log that grew since the check shows as too big.
    if ! { mkdir -m 700 -- "$dir" && head -c "$((MAX_OTEL_LOG_BYTES + 1))" <"$log" >"$copy"; }; then
        warning "Could not copy the telemetry log to $dir, $no_line"
        return 0
    fi
    if [ "$(wc -c <"$copy")" -gt "$MAX_OTEL_LOG_BYTES" ]; then
        rm -f -- "${copy:?}"
        warning "The telemetry log is over $MAX_OTEL_LOG_BYTES bytes, $no_line"
        return 0
    fi
    if ! resolve_tool_image "$backend" "$version" "$agent_image"; then
        warning "No agentic-ci image to total the usage in, $no_line"
        return 0
    fi
    # The container mounts only the usage directory, so the script goes there. -E -P, not -I: the
    # images' RHEL Python finds agentic-ci under /usr/local only outside isolated mode, and -E -P
    # still ignore PYTHON* variables and keep the script's directory off the import path.
    local script="${GITHUB_ACTION_PATH:-}/$USAGE_PY_REL"
    if [ ! -f "$script" ] || ! cp -- "$script" "$dir/usage.py"; then
        warning "$script is missing, $no_line"
        return 0
    fi
    local output="${dir:?}/usage-output.log"
    run_agentic_ci_tool "$dir" "" none "$USAGE_TIMEOUT" \
        python3 -E -P "$dir/usage.py" "$copy" "$totals" \
        "--job-name=${GITHUB_JOB:+$GITHUB_JOB-$REVIEW_MODE}" "--mode=$REVIEW_MODE" \
        >"$output" 2>&1 || status=$?
    print_tool_output "$output"
    case "$status" in
        0) ;;
        "$NO_USAGE_EXIT") notice "The telemetry holds no token data, $no_line" ;;
        124) warning "The usage totals did not finish within $USAGE_TIMEOUT, $no_line" ;;
        *) warning "Could not total the review's usage (exit $status: $(tool_reason "$output")), $no_line" ;;
    esac
    check_tool_version "$version" "$output"
    # A partial file from a failed run is not the run's usage. After the timeout, the container is
    # gone by now, so it cannot write the file again.
    [ "$status" -eq 0 ] || rm -f -- "${totals:?}"
}

# Older runners can ship a git that lacks the fetch options build_container_repo needs.
require_git_version() {
    local version
    version="$(git --version 2>/dev/null)" || error "git is not installed on the runner"
    [[ "$version" =~ ^git\ version\ ([0-9]+)\.([0-9]+) ]] \
        || error "Could not read the git version from: '$version'"
    local major=$((10#${BASH_REMATCH[1]})) minor=$((10#${BASH_REMATCH[2]}))
    if [ "$major" -lt "$MIN_GIT_MAJOR" ] \
        || { [ "$major" -eq "$MIN_GIT_MAJOR" ] && [ "$minor" -lt "$MIN_GIT_MINOR" ]; }; then
        error "git $MIN_GIT_MAJOR.$MIN_GIT_MINOR or later is required, the runner has: $version"
    fi
}

# Fail on a bot_login that review.py would reject in the post step, before any agent spends on
# the review. The patterns are review.py's _GITHUB_APP_LOGIN_RE and _GITHUB_USER_LOGIN_RE: an
# App's <slug>[bot], or a user login of 1 to 39 alphanumerics and single inner hyphens.
require_bot_login() {
    # Plain ASCII ranges, whatever the runner's locale.
    local LC_ALL=C
    local login="${AI_REVIEW_BOT_LOGIN:-}"
    [ -n "$login" ] || return 0
    if [[ "$login" =~ ^[a-z0-9][a-z0-9_-]{0,62}\[bot\]$ ]]; then
        return 0
    fi
    if [ "${#login}" -le 39 ] && [[ "$login" =~ ^[A-Za-z0-9](-?[A-Za-z0-9])*$ ]]; then
        return 0
    fi
    # Only printable characters are shown, so the value cannot start a workflow command.
    local shown="${login:0:80}"
    shown="${shown//[^[:alnum:][:punct:] ]/?}"
    error "bot_login must be a GitHub App bot login like my-app[bot] or a GitHub user login, got: '$shown'"
}

# Fail on a deep_review_label that review.py would reject in the post step, before any agent
# spends on the review: blank, over 50 characters, or with a control, format, surrogate or line
# or paragraph separator character. Only checked where post passes it: standard mode with the
# recommendation on. review.py stays the full check.
require_deep_label() {
    [ "$REVIEW_MODE" = "standard" ] && is_on "${AI_REVIEW_RECOMMEND_DEEP:-}" || return 0
    local label="${AI_REVIEW_DEEP_LABEL:-}" problem
    # Empty is the input's default.
    [ -n "$label" ] || return 0
    problem="$(python3 -I -c 'import sys, unicodedata
label = sys.argv[1]
if not label.strip():
    print("is blank")
elif len(label) > 50:
    print("is longer than 50 characters")
elif any(unicodedata.category(c) in ("Cc", "Cf", "Cs", "Zl", "Zp") for c in label):
    print("contains a control, format or line separator character")' "$label")" \
        || error "Could not check the deep_review_label input"
    # The value is not shown: it can hold a newline that would start a workflow command.
    [ -z "$problem" ] || error "deep_review_label $problem, so the summary could not name it"
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
    local harness="${AI_REVIEW_HARNESS:-codex}"

    if [ "${AI_REVIEW_EVENT:-}" != "pull_request" ]; then
        error "This action only supports the pull_request event, got '${AI_REVIEW_EVENT:-}'. Do not use pull_request_target: it runs with write access on code from forks."
    fi
    require_mode
    require_bot_login
    require_deep_label

    # Forks and Dependabot come first: their runs get no secrets, so they skip in every mode.
    local fail_in_deep=""
    if [ -z "${AI_REVIEW_HEAD_REPO:-}" ] || [ "${AI_REVIEW_HEAD_REPO}" != "${GITHUB_REPOSITORY:-}" ]; then
        skip_reason="the pull request comes from a fork, where the workflow has no secrets and a read-only token"
    elif [ "$actor" = "dependabot[bot]" ] || [ "$triggering" = "dependabot[bot]" ]; then
        skip_reason="the run was triggered by Dependabot, which gets no secrets and a read-only token"
    elif [ "${AI_REVIEW_DRAFT:-}" = "true" ]; then
        skip_reason="the pull request is a draft. Marking it ready for review runs the review"
        fail_in_deep=1
    elif ! has_harness_credentials "$harness"; then
        fail_in_deep=1
        case "$harness" in
            codex)
                skip_reason="OPENAI_API_KEY is not set, which the codex harness needs"
                ;;
            *)
                skip_reason="none of ANTHROPIC_API_KEY, CLAUDE_CODE_OAUTH_TOKEN, CLAUDE_CODE_USE_VERTEX, GCLOUD_CREDENTIALS or GCP_SERVICE_ACCOUNT_KEY is set, which the claude-code harness needs"
                ;;
        esac
    fi

    # A deep job that skips still succeeds, and a standard review of the same head takes a
    # successful deep job for a posted deep review and stands down: nothing would be posted.
    # A failed deep job does not count, so these fail instead.
    if [ -n "$fail_in_deep" ] && [ "$REVIEW_MODE" = "deep" ]; then
        error "The deep review cannot run: $skip_reason. Failing rather than skipping, so the standard review of this commit does not stand down for a deep review that posted nothing."
    fi

    if [ -n "$skip_reason" ]; then
        notice "Skipping the AI review: $skip_reason."
        echo "skip=true" >>"${GITHUB_OUTPUT:-/dev/null}"
    else
        echo "skip=false" >>"${GITHUB_OUTPUT:-/dev/null}"
    fi
}

# Give the export a .git of its own: the history of the base and the head, fetched from the
# checkout's objects, and nothing else of the checkout. No remote, credentials, hooks,
# alternates, hardlinks or config come over, and no host path is left in it, so the agent can
# run git log and merge-base without reaching the checkout or the token.
# The host never runs git in AI_DIR once the review step starts: the agent can rewrite this
# .git, and the post step reads back only the one validated output file.
build_container_repo() {
    local base="$1" head="$2" repo="$AI_DIR/repo"
    local src="$PWD"
    [ ! -e "$repo/.git" ] || error "$repo/.git exists after the export, so the action stopped"
    PREPARE_TMP="$(mktemp -d "${RUNNER_TEMP:-${TMPDIR:-/tmp}}/ai-review-tmp.XXXXXX")"
    trap 'rm -rf -- "$PREPARE_TMP"' EXIT
    mkdir "$PREPARE_TMP/template" "$PREPARE_TMP/home"
    # Each git call exits on failure itself: errexit does not apply in a subshell tested by ||.
    (
        # Only the command line and the overrides below configure git: no global or system
        # config, no user config under HOME, and no repo or work tree from the environment.
        unset GIT_DIR GIT_WORK_TREE GIT_INDEX_FILE GIT_OBJECT_DIRECTORY \
            GIT_ALTERNATE_OBJECT_DIRECTORIES GIT_COMMON_DIR GIT_NAMESPACE
        export GIT_CONFIG_GLOBAL=/dev/null GIT_CONFIG_NOSYSTEM=1
        # Local to this subshell on purpose.
        # shellcheck disable=SC2030
        export HOME="$PREPARE_TMP/home" XDG_CONFIG_HOME="$PREPARE_TMP/home/.config"
        harden_git
        # An empty template: no hooks, no info/exclude, no description.
        git -c init.defaultBranch=main init -q --template="$PREPARE_TMP/template" "$repo" || exit 1
        # A fetch, not clone --local: no hardlinks into the checkout and no alternates file.
        # Named refs keep both commits reachable. No FETCH_HEAD, which would name this path.
        git -C "$repo" fetch -q --no-tags --no-write-fetch-head --no-recurse-submodules \
            --no-auto-maintenance "$src" "$base:refs/ai-review/base" "$head:refs/ai-review/head" \
            || exit 1
        # Detach HEAD at the head and load its tree into the index. The export is the working
        # tree already, so nothing is checked out over it.
        git -C "$repo" update-ref --no-deref HEAD "$head" || exit 1
        git -C "$repo" read-tree "$head" || exit 1
        # Record the exported files' stat data, so git status in the container reads clean.
        git -C "$repo" update-index -q --refresh >/dev/null || true
    ) || error "Could not build the git repository for the review in $repo"
    rm -rf -- "$repo/.git/FETCH_HEAD" "$repo/.git/logs"
}

# Run panel.py triage on the container repo for the deep-review recommendation, before any agent
# has touched that repo, and keep its plan outside the mount for the post step. The triage reads
# the PR's content, so it gets no token and none of the runner's environment: env -i, an empty
# HOME, no git config but the overrides, and an isolated Python. It only advises, so a failure
# warns, leaves no plan and lets prepare go on.
run_triage() {
    local base="$1" head="$2" repo="$AI_DIR/repo" dir
    local no_line="so the summary will not recommend a deep review."
    if ! dir="$(triage_dir)"; then
        warning "RUNNER_TEMP is not set, $no_line"
        return 0
    fi
    local panel="$GITHUB_ACTION_PATH/$PANEL_PY_REL" work="$PREPARE_TMP/triage"
    if [ ! -f "$panel" ]; then
        warning "$panel is missing, $no_line"
        return 0
    fi
    mkdir "$work" "$work/home"
    local triage_env=(
        "PATH=$PATH"
        "HOME=$work/home"
        "XDG_CONFIG_HOME=$work/home/.config"
        GIT_CONFIG_GLOBAL=/dev/null
        GIT_CONFIG_NOSYSTEM=1
        # The keys harden_git sets, and the repo as safe: on a root runner it can belong to
        # another user.
        GIT_CONFIG_COUNT=3
        GIT_CONFIG_KEY_0=core.fsmonitor GIT_CONFIG_VALUE_0=false
        GIT_CONFIG_KEY_1=core.hooksPath GIT_CONFIG_VALUE_1=/dev/null
        GIT_CONFIG_KEY_2=safe.directory "GIT_CONFIG_VALUE_2=$repo"
        PYTHONNOUSERSITE=1
    )
    [ -z "${LANG:-}" ] || triage_env+=("LANG=$LANG")
    [ -z "${TZ:-}" ] || triage_env+=("TZ=$TZ")

    # head keeps one byte past the limit, so an oversized plan shows as one and is dropped.
    local status=0 size tier
    timeout "$TRIAGE_TIMEOUT" env -i "${triage_env[@]}" \
        python3 -I "$panel" triage --repo "$repo" --base "$base" --head "$head" \
        </dev/null 2>/dev/null | head -c "$((MAX_PLAN_BYTES + 1))" >"$work/plan.json" \
        || status=$?
    size="$(wc -c <"$work/plan.json")"
    if [ "$size" -gt "$MAX_PLAN_BYTES" ]; then
        warning "The triage plan is over $MAX_PLAN_BYTES bytes, $no_line"
        return 0
    elif [ "$status" -eq 124 ]; then
        warning "The triage did not finish within $TRIAGE_TIMEOUT, $no_line"
        return 0
    elif [ "$status" -ne 0 ]; then
        warning "The triage failed (exit $status), $no_line"
        return 0
    fi
    # Only the tier name is logged: the rest of the plan names the PR's files.
    tier="$(env -i "PATH=$PATH" python3 -I -c 'import json, sys
plan = json.load(open(sys.argv[1], encoding="utf-8"))
tier = plan.get("tier") if isinstance(plan, dict) else None
name = tier.get("name") if isinstance(tier, dict) else None
print(name if isinstance(name, str) else "")' "$work/plan.json" 2>/dev/null)" || tier=""
    if [[ ! "$tier" =~ ^[a-z]{1,20}$ ]]; then
        warning "The triage printed no plan with a tier, $no_line"
        return 0
    fi
    if ! { mkdir -m 700 -- "$dir" && mv -- "$work/plan.json" "$dir/plan.json"; }; then
        warning "Could not keep the triage plan in $dir, $no_line"
        return 0
    fi
    echo "Triage tier for the deep-review recommendation: $tier"
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
    require_mode
    require_git_version
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
    rm -f -- "$DIFF_FILE" "$INSTRUCTIONS_FILE" "$ORG_RULES_FILE"
    # A plan from an earlier run must not reach this run's summary.
    local dir
    if dir="$(triage_dir)"; then
        rm -rf -- "$dir"
    fi
    mkdir -p "$OUT_DIR" "$PLUGIN_DIR" "$AI_DIR/repo"
    # --text: the PR's .gitattributes must not mark files binary and hide their changes.
    # The prefixes and color are fixed so no runner-wide git config changes the format.
    git diff --no-ext-diff --no-textconv --text --no-color --src-prefix=a/ --dst-prefix=b/ \
        "$merge_base" "$head" >"$DIFF_FILE"

    # The agent gets the working tree without the checkout's .git, so it cannot reach that
    # config, its object store or anything git would run later there. Not git archive: it honors
    # the PR's export-ignore.
    tar -C . --exclude=./.git -cf - . | tar -C "$AI_DIR/repo" -xf -
    build_container_repo "$base" "$head"
    # Deep mode is the deep review, so it recommends nothing.
    if [ "$REVIEW_MODE" = "standard" ] && is_on "${AI_REVIEW_RECOMMEND_DEEP:-}"; then
        run_triage "$base" "$head"
    fi

    # Claude Code loads this copy, not the registry copy in the image, which lacks the skill.
    # Codex takes no plugin directory: it loads the skill installed in its runner image.
    # Only the plugin and the skills go in: with uses: ./ the action path is the workspace root,
    # where earlier steps can leave credentials, and the workdirs themselves.
    tar -C "$GITHUB_ACTION_PATH" -cf - .claude-plugin skills | tar -C "$PLUGIN_DIR" -xf -

    # Per-repo instructions stay off the mount in deep mode, where REVIEW.md supersedes them.
    if [ "$REVIEW_MODE" = "standard" ] && [ -n "${AI_REVIEW_INSTRUCTIONS:-}" ]; then
        printf '%s' "$AI_REVIEW_INSTRUCTIONS" >"$INSTRUCTIONS_FILE"
    fi
    # Org-wide policy rules mount in both modes, unlike the per-repo instructions. The input
    # is a path to a file the caller fetched, never the content: the runner prints each step
    # env value into the log, which would publish the internal rules in viewable logs.
    if [ -n "${AI_REVIEW_ORG_RULES:-}" ]; then
        local rules_src="${AI_REVIEW_ORG_RULES}"
        case "$rules_src" in
            /*) ;;
            *) error "org_review_rules must be an absolute path outside the workspace: $AI_REVIEW_ORG_RULES" ;;
        esac
        [ -f "$rules_src" ] && [ ! -L "$rules_src" ] \
            || error "org_review_rules is not a regular file: $AI_REVIEW_ORG_RULES"
        # Resolved on both sides, so a symlinked workspace or a symlinked path component
        # cannot point into the workspace: the PR checkout lives there, and a PR must not
        # supply its own org rules through the caller's input.
        local rules_real workspace_real
        rules_real="$(realpath -- "$rules_src")"
        workspace_real="$(realpath -- "$GITHUB_WORKSPACE")"
        case "$rules_real" in
            "$GITHUB_WORKSPACE"|"$GITHUB_WORKSPACE"/*|"$workspace_real"|"$workspace_real"/*)
                error "org_review_rules must point outside the workspace: $AI_REVIEW_ORG_RULES"
                ;;
        esac
        local rules_bytes
        rules_bytes="$(wc -c <"$rules_src")"
        [ "$rules_bytes" -le "$MAX_RULES_BYTES" ] \
            || error "org_review_rules is $rules_bytes bytes, over the $MAX_RULES_BYTES limit: the input must name the org rules file, not some other content"
        cp -- "$rules_src" "$ORG_RULES_FILE"
    fi
}

# A part of a container name that is the same for every container and volume of one review
# run, and differs between the jobs of a workflow run and between workdirs.
docker_run_suffix() {
    local run_id="${GITHUB_RUN_ID:-}" attempt="${GITHUB_RUN_ATTEMPT:-}" digest
    [[ "$run_id" =~ ^[0-9]+$ ]] || run_id=0
    [[ "$attempt" =~ ^[0-9]+$ ]] || attempt=0
    digest="$(printf '%s' "$AI_DIR" | sha256sum | cut -c1-12)"
    echo "$run_id-$attempt-$REVIEW_MODE-$digest"
}

# A Google credential file on the runner is not in the container, so its content crosses as
# GCLOUD_CREDENTIALS, the source agentic-ci reads first. Only the Vertex path reads it: an API key
# or an OAuth token wins over every Google credential. The gcloud application default credentials
# file is the last source, as for Google's client libraries: the container has its own HOME. gcloud
# writes it under CLOUDSDK_CONFIG when that is set, and agentic-ci on the runner reads it under HOME.
load_google_credential_file() {
    [ -z "${ANTHROPIC_API_KEY:-}" ] && [ -z "${CLAUDE_CODE_OAUTH_TOKEN:-}" ] || return 0
    [ -z "${GCLOUD_CREDENTIALS:-}" ] || return 0
    local file="" adc adcs=()
    [ -z "${CLOUDSDK_CONFIG:-}" ] \
        || adcs+=("$CLOUDSDK_CONFIG/application_default_credentials.json")
    # The review step's HOME: cmd_prepare changes it only in a subshell.
    # shellcheck disable=SC2031
    adcs+=("${HOME:-}/.config/gcloud/application_default_credentials.json")
    if [ -n "${GCP_SERVICE_ACCOUNT_KEY:-}" ]; then
        # Inline JSON or base64 crosses as it is.
        [ -f "$GCP_SERVICE_ACCOUNT_KEY" ] || return 0
        file="$GCP_SERVICE_ACCOUNT_KEY"
    elif [ -n "${GOOGLE_APPLICATION_CREDENTIALS:-}" ]; then
        file="$GOOGLE_APPLICATION_CREDENTIALS"
    else
        for adc in "${adcs[@]}"; do
            if [ -e "$adc" ]; then
                file="$adc"
                break
            fi
        done
        if [ -z "$file" ]; then
            # Without this, the container starts and agentic-ci fails inside it with no hint.
            is_on "${CLAUDE_CODE_USE_VERTEX:-}" || return 0
            error "CLAUDE_CODE_USE_VERTEX is set, but there is no Google credential for the review container: none of GCLOUD_CREDENTIALS, GCP_SERVICE_ACCOUNT_KEY or GOOGLE_APPLICATION_CREDENTIALS is set, and there is no application default credentials file (${adcs[*]}). Put a service account key in GCP_SERVICE_ACCOUNT_KEY, or set backend: podman"
        fi
    fi
    [ -f "$file" ] && [ -r "$file" ] \
        || error "The Google credential file $file cannot be read, so the review container cannot use it. Put its content in GCLOUD_CREDENTIALS, or set backend: podman"
    # Only the type is printed, never the key.
    local type
    type="$(python3 -I -c 'import json, sys
data = json.load(open(sys.argv[1], encoding="utf-8"))
print(data.get("type", "") if isinstance(data, dict) else "")' "$file" 2>/dev/null)" \
        || error "The Google credential file $file is not JSON, so the review container cannot use it. Put its content in GCLOUD_CREDENTIALS, or set backend: podman"
    case "$type" in
        service_account | authorized_user) ;;
        external_account)
            error "Workload Identity Federation credentials cannot be used by the review container; provide a service account key secret (GCP_SERVICE_ACCOUNT_KEY) or another credential"
            ;;
        *)
            error "The Google credential file $file is not a service account key or a user credential, so the review container cannot use it. Put a service account key in GCLOUD_CREDENTIALS, or set backend: podman"
            ;;
    esac
    GCLOUD_CREDENTIALS="$(cat -- "$file")"
    export GCLOUD_CREDENTIALS
    # The path means nothing in the container.
    unset GCP_SERVICE_ACCOUNT_KEY
}

# Run the agent with the openshell backend: a privileged container of the openshell image runs the
# bundled agentic-ci, which starts the OpenShell gateway and a nested podman, uploads AI_DIR into
# the sandbox and downloads it back. Only AI_DIR and a per-run storage volume are mounted: never
# RUNNER_TEMP, the checkout or the docker socket. Sets AGENT_STATUS.
#
# Usage: run_openshell_agent HARNESS IMAGE VERSION MINUTES AGENTIC_CI_ARGS...
run_openshell_agent() {
    local harness="$1" image="$2" version="$3" minutes="$4"
    shift 4
    command -v docker >/dev/null 2>&1 \
        || error "The openshell backend needs docker on the runner. Set backend: podman to run the agent with Podman instead"

    local openshell_image suffix
    openshell_image="$(openshell_image_ref "$version")" \
        || error "No openshell image for agentic_ci_version '$version'"
    suffix="$(docker_run_suffix)"
    local name="ai-review-$suffix" volume="ai-review-store-$suffix"
    local label="$WORKDIR_LABEL=$AI_DIR"

    # Nothing that can write to the repository or mint tokens reaches docker, not even empty,
    # and neither does the MLflow endpoint, its token or anything of the relay.
    unset GITHUB_TOKEN GH_TOKEN MLFLOW_TRACKING_URI MLFLOW_TRACKING_TOKEN \
        REVIEW_TELEMETRY_PROJECT REVIEW_TELEMETRY_USERNAME REVIEW_TELEMETRY_TOKEN
    local var
    for var in $(compgen -e); do
        case "$var" in
            ACTIONS_ID_TOKEN_*) unset "$var" ;;
        esac
    done

    local names
    if [ "$harness" = "claude-code" ]; then
        load_google_credential_file
        names="$CLAUDE_CREDENTIAL_VARS"
    else
        names="$CODEX_CREDENTIAL_VARS"
    fi
    # cmd_review sets AGENT_ENABLED_PLUGINS for both backends. agentic-ci copies its OTEL log to
    # GITHUB_WORKSPACE, which the agent run below sets to the telemetry directory of the mount.
    local env_args=(-e AGENT_ENABLED_PLUGINS -e GITHUB_WORKSPACE)
    for var in $names; do
        if [ -n "${!var:-}" ]; then
            env_args+=(-e "$var")
        fi
    done

    # The pulls have their own limits and stay out of the agent's minutes. The sandbox images go
    # into a volume that the agent container mounts as its podman storage, so OpenShell finds them
    # there instead of pulling them on the agent's clock. A local build (localhost/...) is not in
    # a registry, so only the OpenShell images are pulled for it.
    timeout 15m docker pull --quiet "$openshell_image" \
        || error "Could not pull the image $openshell_image"
    docker volume create --label "$label" "$volume" >/dev/null \
        || error "Could not create the docker volume $volume"
    local pull_args=()
    case "$image" in
        localhost/*) ;;
        *) pull_args=("$image") ;;
    esac
    if ! timeout -s INT --kill-after=30s 15m \
        docker run --rm --privileged --init --pull never \
        --name "$name-pull" --label "$label" \
        -v "$volume:/var/lib/containers" \
        "$openshell_image" \
        bash -c "$PREPULL_SCRIPT" bash ${pull_args[@]+"${pull_args[@]}"}; then
        docker rm -f "$name-pull" >/dev/null 2>&1 || true
        error "Could not pull the sandbox images into the OpenShell container's storage"
    fi

    # The telemetry directory exists before the upload, so the copy lands in it rather than in a
    # file of its name. The agent can replace it in the sandbox, so collect_usage checks it again.
    rm -rf -- "${TELEMETRY_DIR:?}"
    mkdir -- "$TELEMETRY_DIR" || error "Could not create $TELEMETRY_DIR"
    AGENT_OTEL_LOG="$TELEMETRY_DIR/$OTEL_LOG_NAME"

    # agentic-ci is the direct child of the init process, which forwards the timeout's SIGINT to
    # it, so it deletes the sandbox and stops the gateway before it exits. No -i or -t: the
    # OpenShell CLI reads a non-terminal stdin to its end before each command it sends.
    AGENT_STATUS=0
    GITHUB_WORKSPACE="$TELEMETRY_DIR" timeout -s INT --kill-after=60s "${minutes}m" \
        docker run --rm --privileged --init --pull never \
        --name "$name" --label "$label" \
        -v "$volume:/var/lib/containers" \
        -v "$AI_DIR:$AI_DIR" \
        --workdir / \
        "${env_args[@]}" \
        "$openshell_image" \
        agentic-ci "$@" || AGENT_STATUS=$?
    # A killed docker client leaves the container running with the model credentials.
    if [ "$AGENT_STATUS" -ne 0 ]; then
        docker rm -f "$name" >/dev/null 2>&1 || true
    fi

    # The download back into AI_DIR leaves root's files, which the runner user cannot read or
    # remove. A separate container gives them back, whatever the agent's status: a wrapper in the
    # agent container would take the signal that agentic-ci needs.
    if ! timeout 2m docker run --rm --pull never \
        --name "$name-chown" --label "$label" \
        -v "$AI_DIR:$AI_DIR" \
        --entrypoint chown \
        "$openshell_image" \
        -R -h -P "$(id -u):$(id -g)" "$AI_DIR"; then
        # The agent's own failure says more, so it stays the step's error.
        [ "$AGENT_STATUS" -ne 0 ] || error "Could not give $AI_DIR back to the runner user"
        warning "Could not give $AI_DIR back to the runner user"
    fi
}

cmd_review() {
    require_workspace
    refuse_redirected_workdirs
    reject_whitespace "$AI_DIR"
    require_mode
    local harness="${AI_REVIEW_HARNESS:-codex}"
    case "$harness" in
        claude-code | codex) ;;
        *) error "harness must be claude-code or codex, got: '$harness'" ;;
    esac
    local backend="${AI_REVIEW_BACKEND:-openshell}" cdir
    case "$backend" in
        openshell)
            cdir="$OPENSHELL_CONTAINER_DIR"
            # docker -v splits its argument on colons.
            case "$AI_DIR" in
                *[:,]*) error "Path contains ':' or ',', which the docker mount cannot carry: $AI_DIR" ;;
            esac
            ;;
        podman) cdir="$PODMAN_CONTAINER_DIR" ;;
        *) error "backend must be openshell or podman, got: '$backend'" ;;
    esac
    local base="${AI_REVIEW_BASE_SHA:-}" head="${AI_REVIEW_HEAD_SHA:-}"
    require_sha "pull_request.base.sha" "$base"
    require_sha "pull_request.head.sha" "$head"
    [ -f "$DIFF_FILE" ] || error "$DIFF_FILE is missing: the prepare step did not run"
    if [ "$harness" = "claude-code" ] && [ ! -d "$PLUGIN_DIR" ]; then
        error "$PLUGIN_DIR is missing: the prepare step did not run"
    fi
    # The panel reads the history. A test, not a git call: the host never runs git in AI_DIR.
    if [ "$REVIEW_MODE" = "deep" ] && [ ! -d "$AI_DIR/repo/.git" ]; then
        error "$AI_DIR/repo/.git is missing: the prepare step did not run"
    fi

    local image="${AI_REVIEW_IMAGE:-}" version="${AI_REVIEW_AGENTIC_CI_VERSION:-}"
    if [ -z "$image" ]; then
        image="$(default_image "$harness" "$backend")" || error "No default image for $harness"
    fi
    [ -n "${RUNNER_TEMP:-}" ] || error "RUNNER_TEMP is not set"
    case "$image" in
        *[[:space:]]*) error "image must not contain whitespace, got: '$image'" ;;
    esac
    local version_re='^[0-9][0-9A-Za-z.+!-]*$'
    if [ "$backend" = "openshell" ]; then
        # The version is the openshell image's tag, which allows fewer characters than a
        # version, and can pin the image by digest.
        if [[ ! "${version%%@*}" =~ $version_re ]] || ! openshell_image_ref "$version" >/dev/null; then
            error "agentic_ci_version must be a tag of the openshell image with the openshell backend, such as 0.5.16, or a tag pinned to a digest, such as 0.5.16@sha256:<64 lowercase hex digits>, got: '$version'"
        fi
    else
        case "$version" in
            *@*) error "agentic_ci_version is the PyPI version of agentic-ci with backend: podman, so it cannot pin a digest: only the openshell image is pinned by digest, got: '$version'" ;;
        esac
        [[ "$version" =~ $version_re ]] || error "agentic_ci_version must be a package version such as 0.5.16, got: '$version'"
    fi
    local minutes="${AI_REVIEW_TIMEOUT_MINUTES:-}"
    if [ -z "$minutes" ]; then
        if [ "$REVIEW_MODE" = "deep" ]; then
            minutes="$DEEP_TIMEOUT_MINUTES"
        else
            minutes="$STANDARD_TIMEOUT_MINUTES"
        fi
    fi
    [[ "$minutes" =~ ^[1-9][0-9]*$ ]] || error "timeout_minutes must be a positive integer, got: '$minutes'"

    # Every path the prompt carries is the agent's path. Claude Code runs the skill from the
    # action's own plugin copy, so the prompt is its slash command. Codex has no plugin flag:
    # it loads the skill installed in its image, and the prompt names it the way the GitLab
    # job does.
    local prompt
    if [ "$REVIEW_MODE" = "deep" ]; then
        # The panel writes gitlab-code-review's JSON, which the post step validates and posts like
        # a standard review. Its guidance comes from REVIEW.md at the base ref, so a PR cannot
        # loosen its own review, and the per-repo instructions are not passed. The caller-supplied
        # org policy rules are passed in both modes: they share the caller's workflow file's trust
        # level, and the cannot-loosen guarantee covers only the base-ref guidance. One line: the
        # prompt is a single argument.
        # On a root runner the agent runs as another user than the owner of the mounted repo, so
        # git refuses it as dubious ownership. A global safe.directory reaches the triage's own
        # git calls and every subagent, where a -c on one command would not.
        local task="the committed changes in $cdir/repo from base $base to head $head. First run git config --global --add safe.directory $cdir/repo, because the repository can belong to another user. Run its triage from inside $cdir/repo with --base $base --head $head, pass --gitlab-json $cdir/out/ai-review-output.json to its final report command, and do not post anything."
        if [ -f "$ORG_RULES_FILE" ]; then
            if [ "$harness" = "claude-code" ]; then
                task="$task Apply the org review rules at $cdir/org-rules.yaml."
            else
                warning "org_review_rules is not passed to the codex harness in deep mode yet: its image ships a code-review-panel skill without the org-rules section, so the rules are dropped until that gate is removed."
            fi
        fi
        if [ "$harness" = "claude-code" ]; then
            prompt="/code-review-skills:code-review-panel Review $task"
        else
            prompt="Invoke the /code-review-skills:code-review-panel skill to review $task"
        fi
        if [ -n "${AI_REVIEW_INSTRUCTIONS:-}" ]; then
            warning "review_instructions is ignored in deep mode: the panel reads REVIEW.md from the base branch instead. Org-wide policy is the separate org_review_rules input, passed in both modes."
        fi
    else
        if [ "$harness" = "claude-code" ]; then
            prompt="/code-review-skills:github-code-review $cdir/repo $cdir/pr.diff $base $head $cdir/out/ai-review-output.json"
        else
            prompt="Invoke the /code-review-skills:github-code-review skill with the arguments: $cdir/repo $cdir/pr.diff $base $head $cdir/out/ai-review-output.json"
        fi
        if [ -f "$INSTRUCTIONS_FILE" ]; then
            prompt="$prompt $cdir/instructions.md"
        fi
        if [ -f "$ORG_RULES_FILE" ]; then
            if [ "$harness" = "claude-code" ]; then
                prompt="$prompt $cdir/org-rules.yaml"
            else
                warning "org_review_rules is not passed to the codex harness yet: its image ships a github-code-review skill that takes no org-rules argument, so the rules are dropped until that gate is removed."
            fi
        fi
    fi

    # The container is the sandbox: agentic-ci runs the agent with every tool enabled, so a
    # tool allowlist would not hold. The container never mounts the checkout's .git, and the
    # post step reads one validated file from the mount. OTEL stays on: agentic-ci collects the
    # agent's telemetry for the usage line and the MLflow export. Its collector starts before the
    # agent, and agentic-ci stops the run when it cannot start, as in the GitLab job.
    local args=(
        run
        --harness "$harness"
        --backend "$backend"
        --workdir "$AI_DIR"
        --image "$image"
    )
    # OpenShell ignores --timeout, so the outer limit is its only one. Podman's has a margin over
    # it, so the outer one fires first.
    if [ "$backend" = "podman" ]; then
        args+=(--timeout "$(( (minutes + 5) * 60 ))")
    fi
    args+=(--effort "$AGENT_EFFORT")
    if [ -n "${AI_REVIEW_MODEL:-}" ]; then
        args+=(--model "$AI_REVIEW_MODEL")
    fi
    args+=("$prompt")
    if [ "$harness" = "claude-code" ]; then
        args+=(-- --plugin-dir "$cdir/plugin")
    fi

    # Narrows the image's skill catalog to this plugin, as the GitLab job does. agentic-ci reads
    # it from its environment: openshell passes it into the container, and with podman the uvx
    # run inherits it, like the credentials.
    export AGENT_ENABLED_PLUGINS="$ENABLED_PLUGINS"
    # Usage from an earlier run must not reach this run's summary or MLflow.
    local usage
    if usage="$(usage_dir)"; then
        rm -rf -- "${usage:?}"
    fi
    if [ "$backend" = "openshell" ]; then
        run_openshell_agent "$harness" "$image" "$version" "$minutes" "${args[@]}"
    else
        run_podman_agent "$image" "$version" "$minutes" "${args[@]}"
    fi
    # Before the status checks, so a failed or timed-out review still leaves its telemetry for
    # the MLflow export.
    collect_usage "$backend" "$version" "$image" "$AGENT_OTEL_LOG"

    # 124 is the timeout. 137 is the kill after the grace period, when the agent ignores the first
    # signal; it is also what an out-of-memory kill looks like. Any other failure is the agent's.
    case "$AGENT_STATUS" in
        124) error "The review did not finish within $minutes minutes" ;;
        137) error "The review was killed (exit 137): it ran past the $minutes minute limit or the container ran out of memory" ;;
    esac
    if [ "$backend" = "openshell" ] && [ "$AGENT_STATUS" -eq 125 ]; then
        error "Could not start the OpenShell container (exit 125)"
    fi
    [ "$AGENT_STATUS" -eq 0 ] || exit "$AGENT_STATUS"

    # The run can exit 0 when the agent wrote nothing. Codex loads its skills from the image,
    # so an image without them is the usual cause.
    if [ ! -s "$OUT" ]; then
        local hint=""
        if [ "$harness" = "codex" ]; then
            local skills="github-code-review"
            [ "$REVIEW_MODE" != "deep" ] || skills="github-code-review and code-review-panel"
            hint=". The codex harness loads its skills from the image, which must ship $skills: set the image input to a build that does (see Credentials in the README)"
        fi
        error "The review produced no output at $OUT$hint"
    fi
}

# Run the agent with the podman backend: agentic-ci runs on the runner through uvx and starts the
# agent's Podman container itself. Sets AGENT_STATUS.
#
# Usage: run_podman_agent IMAGE VERSION MINUTES AGENTIC_CI_ARGS...
run_podman_agent() {
    local image="$1" version="$2" minutes="$3"
    shift 3

    # The agent's credentials arrive from the job environment, the keys agentic-ci reads.
    # Nothing that can write to the repository or mint tokens reaches the agent, and neither does
    # the MLflow endpoint, its token or anything of the relay.
    export GITHUB_TOKEN='' GH_TOKEN=''
    export ACTIONS_ID_TOKEN_REQUEST_TOKEN='' ACTIONS_ID_TOKEN_REQUEST_URL=''
    export MLFLOW_TRACKING_URI='' MLFLOW_TRACKING_TOKEN=''
    export REVIEW_TELEMETRY_PROJECT='' REVIEW_TELEMETRY_USERNAME='' REVIEW_TELEMETRY_TOKEN=''
    # uvx runs in the workdir, but no uv config should steer it.
    export UV_NO_CONFIG=1

    # agentic-ci writes the resolved cloud credentials into a temp directory and never deletes
    # it, so its run gets its own under RUNNER_TEMP and the trap removes it on every exit. Only
    # that run uses it: the image pull keeps the default, which may have more room.
    REVIEW_TMP="$(mktemp -d "$RUNNER_TEMP/ai-review-tmp.XXXXXX")"
    # agentic-ci copies its OTEL log to GITHUB_WORKSPACE, the checkout's parent on the runner, so
    # its run gets a private directory instead, which the agent's container does not mount.
    OTEL_TMP="$(mktemp -d "$RUNNER_TEMP/ai-review-otel.XXXXXX")"
    trap 'rm -rf -- "${REVIEW_TMP:?}" "${OTEL_TMP:?}"' EXIT
    AGENT_OTEL_LOG="$OTEL_TMP/$OTEL_LOG_NAME"

    # The pull is about 1.3 GB, so it gets its own limit and stays out of the agent's minutes.
    # A local build (localhost/...) is not in a registry, and agentic-ci skips the pull for it too.
    case "$image" in
        localhost/*) ;;
        *) timeout 15m podman pull "$image" || error "Could not pull the image $image" ;;
    esac

    # SIGINT makes agentic-ci stop its container before it exits.
    AGENT_STATUS=0
    TMPDIR="$REVIEW_TMP" GITHUB_WORKSPACE="$OTEL_TMP" \
        timeout -s INT --kill-after=60s "${minutes}m" \
        uvx --from "agentic-ci==$version" agentic-ci "$@" || AGENT_STATUS=$?
}

# review.py runs in steps that hold a token, so its dependencies come from the lock next to it,
# never from a fresh resolve: uv fails on a stale lock with UV_LOCKED=1, but ignores a missing one.
require_review_lock() {
    [ ! -L "$1.lock" ] && [ -f "$1.lock" ] || error "$1.lock is missing or not a regular file"
}

cmd_post() {
    require_workspace
    refuse_redirected_workdirs
    [ -n "${GITHUB_ACTION_PATH:-}" ] || error "GITHUB_ACTION_PATH is not set"
    [ -n "${RUNNER_TEMP:-}" ] || error "RUNNER_TEMP is not set"
    local review_py="$GITHUB_ACTION_PATH/$REVIEW_PY_REL"
    [ -x "$review_py" ] || error "$review_py is missing or not executable"
    require_review_lock "$review_py"
    require_mode

    # review.py would take github-actions[bot] for its own account, while the token posts as
    # another one: the summary is not found to update in place and old threads are not its own.
    if is_on "${AI_REVIEW_CALLER_TOKEN:-}" && [ -z "${AI_REVIEW_BOT_LOGIN:-}" ]; then
        warning "github_token is set but bot_login is empty, so the review cannot recognize its own earlier comments. Set bot_login to the login the token posts as."
    fi

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
    export AGENT_TOOL="${AI_REVIEW_HARNESS:-codex}" AGENT_REASONING_EFFORT="$AGENT_EFFORT"

    # With --mode, review.py checks again for a review that should win before it deletes old
    # threads and before the summary, since one can start while this review runs.
    local args=(post --platform github --mode "$REVIEW_MODE")
    # review.py exits 0 without posting when the head moved or the review stood down. It creates
    # this file only once the summary is up, outside the mount, and suggestions follow only then.
    local suggest="" posted_flag="$copy_dir/posted"
    if [ "$REVIEW_MODE" = "standard" ] && is_on "${AI_REVIEW_SUGGEST_REVIEWERS:-}"; then
        suggest=1
        rm -f -- "$posted_flag"
        args+=(--posted-flag "$posted_flag")
    fi
    if [ "$REVIEW_MODE" = "standard" ]; then
        # One argument, so argparse never takes a job name that starts with '-' for a flag.
        args+=("--deep-job=$(deep_job_name)")
        local dir plan
        # Only prepare writes the plan, outside the mount. A link or anything but a file there
        # was not written by it.
        if is_on "${AI_REVIEW_RECOMMEND_DEEP:-}"; then
            # The label the recommendation tells people to add. One argument, like the job name.
            args+=("--deep-label=$(deep_label_name)")
            if dir="$(triage_dir)"; then
                plan="$dir/plan.json"
                if [ ! -L "$dir" ] && [ -d "$dir" ] && [ ! -L "$plan" ] && [ -f "$plan" ]; then
                    args+=(--deep-recommendation-plan "$plan")
                fi
            fi
        fi
    fi

    # The usage totals the review step wrote outside the mount, in both modes. A link or anything
    # but a file there was not written by it.
    local usage
    if usage="$(usage_dir)" && [ ! -L "$usage" ] && [ -d "$usage" ] \
        && [ ! -L "$usage/usage.json" ] && [ -f "$usage/usage.json" ]; then
        args+=(--usage-file "$usage/usage.json")
    fi

    # uv must ignore config and .python-version files that arrive with the PR.
    local status=0
    UV_NO_CONFIG=1 UV_LOCKED=1 UV_PYTHON="$(command -v python3)" \
        "$review_py" "${args[@]}" "$copy_dir/ai-review-output.json" || status=$?
    [ "$status" -eq 0 ] || exit "$status"

    # Suggestions only follow a review that posted, and never decide the step's status.
    if [ -n "$suggest" ] && [ ! -L "$posted_flag" ] && [ -f "$posted_flag" ]; then
        UV_NO_CONFIG=1 UV_LOCKED=1 UV_PYTHON="$(command -v python3)" \
            "$review_py" suggest-reviewers --platform github || status=$?
        [ "$status" -eq 0 ] || warning "Could not suggest reviewers (review.py exit $status)"
    fi
}

# Ask review.py whether a review that should win makes this one redundant, before any agent
# starts: a deep review job of the same head for a standard review, or a newer head for a deep
# review. review.py only reads, and a lookup it cannot make lets the review go ahead with a
# warning. Any exit but 0 and 10 means a bad context, which fails the step.
cmd_standdown() {
    require_workspace
    refuse_redirected_workdirs
    [ -n "${GITHUB_ACTION_PATH:-}" ] || error "GITHUB_ACTION_PATH is not set"
    local review_py="$GITHUB_ACTION_PATH/$REVIEW_PY_REL"
    [ -x "$review_py" ] || error "$review_py is missing or not executable"
    require_review_lock "$review_py"
    require_mode

    local args=(stand-down --platform github --mode "$REVIEW_MODE")
    if [ "$REVIEW_MODE" = "standard" ]; then
        # One argument, so argparse never takes a job name that starts with '-' for a flag.
        args+=("--deep-job=$(deep_job_name)")
    fi

    # The step runs in RUNNER_TEMP, before the checkout, and review.py runs no git here. The
    # overrides still stop any git config above this directory from running code.
    harden_git
    local status=0
    # uv must ignore config and .python-version files, here or above this directory.
    UV_NO_CONFIG=1 UV_LOCKED=1 UV_PYTHON="$(command -v python3)" "$review_py" "${args[@]}" || status=$?
    case "$status" in
        0)
            echo "stand_down=false" >>"${GITHUB_OUTPUT:-/dev/null}"
            ;;
        "$STANDDOWN_EXIT")
            notice "Skipping the AI review: a review that should win makes this one redundant."
            echo "stand_down=true" >>"${GITHUB_OUTPUT:-/dev/null}"
            ;;
        *)
            error "Could not check for a review that should win (review.py exit $status)"
            ;;
    esac
}

# Push the review's OTEL log to MLflow with agentic-ci mlflow-push, when the caller set both the
# mlflow_experiment_name input and MLFLOW_TRACKING_URI. Runs after a failed review too, whenever
# the review step left a log. The endpoint and the token reach agentic-ci only through its
# environment, never a command line, and it never prints the token. Telemetry is optional, so
# every problem only warns and the step always succeeds.
cmd_export() {
    local experiment="${AI_REVIEW_MLFLOW_EXPERIMENT:-}" dir log status=0
    if [ -z "$experiment" ] && [ -z "${MLFLOW_TRACKING_URI:-}" ]; then
        return 0
    fi
    if [ -z "$experiment" ]; then
        warning "MLFLOW_TRACKING_URI is set but the mlflow_experiment_name input is empty, so the review's telemetry was not exported to MLflow. Set mlflow_experiment_name to export it."
        return 0
    fi
    if [ -z "${MLFLOW_TRACKING_URI:-}" ]; then
        # The relay step uploads for an experiment with relay settings, and warns about them.
        if [ -n "${REVIEW_TELEMETRY_PROJECT:-}" ] || [ -n "${REVIEW_TELEMETRY_USERNAME:-}" ] \
            || [ -n "${REVIEW_TELEMETRY_TOKEN:-}" ]; then
            return 0
        fi
        warning "mlflow_experiment_name is set but neither MLFLOW_TRACKING_URI (a direct export to your MLflow) nor REVIEW_TELEMETRY_PROJECT, REVIEW_TELEMETRY_USERNAME and REVIEW_TELEMETRY_TOKEN (the review relay) are set in the job environment, so the review's telemetry went nowhere. Pass them in the job's env:."
        return 0
    fi
    # agentic-ci prints the name, so a control character could start a workflow command.
    if [[ "$experiment" == *[[:cntrl:]]* ]]; then
        warning "mlflow_experiment_name contains a control character, so the review's telemetry was not exported to MLflow."
        return 0
    fi
    if ! dir="$(usage_dir)"; then
        warning "RUNNER_TEMP is not set, so the review's telemetry was not exported to MLflow."
        return 0
    fi
    log="$dir/$OTEL_LOG_NAME"
    # Only the review step writes here, outside the mount: a link was not written by it.
    if [ -L "$dir" ] || [ ! -d "$dir" ] || [ -L "$log" ] || [ ! -f "$log" ]; then
        notice "The review left no telemetry log, so nothing was exported to MLflow."
        return 0
    fi
    # The image the review step pulled: the openshell image, or the Podman agent's image.
    local backend="${AI_REVIEW_BACKEND:-openshell}" agent_image="${AI_REVIEW_IMAGE:-}"
    if [ "$backend" = podman ] && [ -z "$agent_image" ]; then
        agent_image="$(default_image "${AI_REVIEW_HARNESS:-codex}" podman)" || agent_image=""
    fi
    if ! resolve_tool_image "$backend" "${AI_REVIEW_AGENTIC_CI_VERSION:-}" "$agent_image"; then
        warning "No agentic-ci image to export with, so the review's telemetry was not exported to MLflow."
        return 0
    fi
    local traces
    if ! traces="$(count_trace_records "$log")" || [[ ! "$traces" =~ ^[0-9]+$ ]]; then
        warning "Could not read the review's telemetry log, so nothing was exported to MLflow."
        return 0
    fi
    if [ "$traces" -eq 0 ]; then
        notice "The review's telemetry holds no traces, so there was nothing to export to MLflow."
        return 0
    fi

    # One argument, so argparse never takes a name that starts with '-' for a flag.
    local output="${dir:?}/mlflow-push.log" unaffected="The review itself is unaffected."
    run_agentic_ci_tool "$dir" "MLFLOW_TRACKING_URI MLFLOW_TRACKING_TOKEN" network \
        "$MLFLOW_TIMEOUT" agentic-ci mlflow-push "$log" "--experiment=$experiment" \
        >"$output" 2>&1 || status=$?
    print_tool_output "$output"
    case "$status" in
        0) ;;
        124)
            warning "The MLflow export did not finish within $MLFLOW_TIMEOUT. $unaffected"
            return 0
            ;;
        *)
            warning "Could not export the review's telemetry to MLflow (exit $status: $(tool_reason "$output")). $unaffected"
            return 0
            ;;
    esac

    # agentic-ci 0.5.16 exits 0 when the experiment lookup fails, when the experiment does not
    # exist and when only some payloads were pushed, so only its summary line with no failure
    # proves the export. The rule is the review-telemetry relay's.
    local pushed_re='^[[:space:]]*Pushed ([0-9]+) payload\(s\) covering .* \(0 failed\)[[:space:]]*$'
    local partial_re='Pushed ([0-9]+) payload\(s\) covering .* \(([0-9]+) failed\)'
    # Only the last line counts: agentic-ci prints its summary there, and earlier lines (the trace
    # and session ids) are strings from the telemetry, which could imitate it.
    local last reason=""
    last="$(grep -v '^[[:space:]]*$' -- "$output" | tail -n 1)" || last=""
    if [[ "$last" =~ $pushed_re ]] && [ "${BASH_REMATCH[1]}" -gt 0 ]; then
        echo "Exported the review's telemetry to MLflow."
        return 0
    fi
    if [[ "$last" =~ $partial_re ]] && [ "${BASH_REMATCH[2]}" -gt 0 ]; then
        reason="only ${BASH_REMATCH[1]} payload(s) were pushed and ${BASH_REMATCH[2]} failed"
    elif grep -q "Experiment lookup failed" -- "$output"; then
        reason="the experiment lookup failed: the server could not be reached, refused the token or failed TLS"
    elif grep -q "not found -- skipping trace push" -- "$output"; then
        reason="the experiment was not found, and it must exist on the MLflow server before the export"
    else
        reason="agentic-ci exited 0 without pushing all $traces trace record(s)"
    fi
    warning "The review's telemetry was not fully exported to MLflow: $reason ($(tool_reason "$output")). $unaffected"
}

# PUT one file of the relay package with the deploy token, which reaches curl on its standard
# input as config, never on its command line. Only HTTPS, no redirect, a bounded time. -q comes
# first, so no .curlrc on the runner can loosen any of that. Warns and fails on any error.
#
# Usage: relay_put FILE URL OUTPUT
relay_put() {
    local file="$1" url="$2" output="$3" status=0
    curl -q --config - --silent --show-error --fail-with-body --proto =https \
        --connect-timeout "$RELAY_CONNECT_TIMEOUT" --max-time "$RELAY_MAX_TIME" \
        --upload-file "$file" "$url" >"$output" 2>&1 \
        <<<"user = \"$REVIEW_TELEMETRY_USERNAME:$REVIEW_TELEMETRY_TOKEN\"" || status=$?
    [ "$status" -ne 0 ] || return 0
    warning "Could not upload ${file##*/} to the review relay (curl exit $status: $(tool_reason "$output")). The review itself is unaffected."
    return 1
}

# Upload the review's OTEL log and its meta.json to the review-telemetry relay, which pushes them
# to the Red Hat MLflow, when the caller set mlflow_experiment_name, the relay's project and both
# deploy token variables. The package and meta.json follow the relay's upload contract exactly, and a value
# that would not match it skips the upload with a warning. Runs after a failed review too.
# Telemetry is optional, so every problem only warns and the step always succeeds.
cmd_relay() {
    local experiment="${AI_REVIEW_MLFLOW_EXPERIMENT:-}" project="${REVIEW_TELEMETRY_PROJECT:-}"
    local user="${REVIEW_TELEMETRY_USERNAME:-}" token="${REVIEW_TELEMETRY_TOKEN:-}"
    local skip="so the review's telemetry was not uploaded to the review relay."
    # Without any relay setting the caller does not use the relay; the export step warns about an
    # experiment that goes nowhere.
    if [ -z "$project" ] && [ -z "$user" ] && [ -z "$token" ]; then
        return 0
    fi
    local missing=() var
    for var in REVIEW_TELEMETRY_PROJECT REVIEW_TELEMETRY_USERNAME REVIEW_TELEMETRY_TOKEN; do
        [ -n "${!var:-}" ] || missing+=("$var")
    done
    if [ -z "$experiment" ]; then
        missing+=("the mlflow_experiment_name input")
    fi
    if [ "${#missing[@]}" -gt 0 ]; then
        local list
        list="$(IFS=,; echo "${missing[*]}")"
        warning "The review relay is partly set up, $skip Missing: ${list//,/, }."
        return 0
    fi
    # A project id only: it goes into the upload URL.
    if [[ ! "$project" =~ ^[0-9]{1,12}$ ]]; then
        warning "REVIEW_TELEMETRY_PROJECT must be a numeric GitLab project id, $skip"
        return 0
    fi
    # The credentials go into curl's config, where a quote, a backslash or a control character
    # would change it.
    if [[ "$user$token" == *[[:cntrl:]\"\\]* ]]; then
        warning "REVIEW_TELEMETRY_USERNAME or REVIEW_TELEMETRY_TOKEN contains a quote, a backslash or a control character, $skip"
        return 0
    fi

    # The relay's patterns (relay.py VERSION_RE and META_STRING_FIELDS), on plain ASCII ranges.
    local LC_ALL=C mode="${AI_REVIEW_MODE:-standard}" package
    package="$(agentic_ci_package_version "${AI_REVIEW_AGENTIC_CI_VERSION:-}")" || package=""
    # The job part names the mode too: under one reusable workflow the standard and the deep
    # caller run the same job key in the same run, and the relay keeps only the newest file of a
    # name, so one version per job and mode keeps both reviews. Checked after the mode.
    local job="${GITHUB_JOB:+$GITHUB_JOB-$mode}"
    local name value pattern
    for name in repository run_id run_attempt mode job experiment agentic_ci_version; do
        case "$name" in
            repository) value="${GITHUB_REPOSITORY:-}" pattern='^[A-Za-z0-9-]{1,39}/[A-Za-z0-9._-]{1,100}$' ;;
            run_id) value="${GITHUB_RUN_ID:-}" pattern='^[0-9]{1,20}$' ;;
            run_attempt) value="${GITHUB_RUN_ATTEMPT:-}" pattern='^[0-9]{1,6}$' ;;
            job) value="$job" pattern='^[A-Za-z0-9_-]{1,100}$' ;;
            mode) value="$mode" pattern='^(standard|deep)$' ;;
            experiment) value="$experiment" pattern='^[A-Za-z0-9._-]{1,100}$' ;;
            agentic_ci_version) value="$package" pattern='^[0-9]{1,6}\.[0-9]{1,6}\.[0-9]{1,6}$' ;;
        esac
        if [[ ! "$value" =~ $pattern ]]; then
            warning "The relay's $name must match $pattern, $skip"
            return 0
        fi
    done

    local dir log meta
    if ! dir="$(usage_dir)"; then
        warning "RUNNER_TEMP is not set, $skip"
        return 0
    fi
    log="${dir:?}/$OTEL_LOG_NAME"
    meta="${dir:?}/$RELAY_META_NAME"
    # Only the review step writes here, outside the mount: a link was not written by it.
    if [ -L "$dir" ] || [ ! -d "$dir" ] || [ -L "$log" ] || [ ! -f "$log" ]; then
        notice "The review left no telemetry log, so nothing was uploaded to the review relay."
        return 0
    fi
    local size
    size="$(wc -c <"$log")" || size=-1
    if [ "$size" -lt 0 ] || [ "$size" -gt "$RELAY_MAX_LOG_BYTES" ]; then
        warning "The telemetry log is over the relay's $RELAY_MAX_LOG_BYTES bytes or unreadable, $skip"
        return 0
    fi
    # Every value matched a pattern without quotes or backslashes, so it needs no JSON escaping.
    rm -f -- "$meta"
    if ! printf '{"version": 1, "repository": "%s", "run_id": "%s", "run_attempt": "%s", "job": "%s", "mode": "%s", "experiment": "%s", "agentic_ci_version": "%s"}\n' \
        "$GITHUB_REPOSITORY" "$GITHUB_RUN_ID" "$GITHUB_RUN_ATTEMPT" "$job" "$mode" \
        "$experiment" "$package" >"$meta"; then
        warning "Could not write $meta, $skip"
        return 0
    fi
    if [ "$(wc -c <"$meta")" -gt "$RELAY_MAX_META_BYTES" ]; then
        warning "meta.json is over the relay's $RELAY_MAX_META_BYTES bytes, $skip"
        return 0
    fi

    # The log first and meta.json last: meta.json marks the upload complete.
    local version="$GITHUB_RUN_ID.$GITHUB_RUN_ATTEMPT.$job"
    local base="$RELAY_API/projects/$project/packages/generic/$RELAY_PACKAGE/$version"
    local output="${dir:?}/relay-put.log"
    relay_put "$log" "$base/$OTEL_LOG_NAME" "$output" || return 0
    relay_put "$meta" "$base/$RELAY_META_NAME" "$output" || return 0
    notice "Uploaded the review's telemetry to the review relay as $RELAY_PACKAGE $version."
}

# agentic-ci stops its container only when it exits normally. A SIGKILL (the timeout's kill
# after the grace period, or a cancelled run) leaves the container running with the model
# credentials, so remove any agentic-ci container that mounts this run's workdir.
remove_review_containers() {
    command -v podman >/dev/null 2>&1 || return 0
    local id source failed=0 ids
    # The usage and export tool containers carry the workdir label instead.
    ids="$(podman ps -aq --filter "label=$WORKDIR_LABEL=$AI_DIR" 2>/dev/null)" || ids=""
    while IFS= read -r id; do
        [ -n "$id" ] || continue
        podman rm -f "$id" >/dev/null || failed=1
    done <<<"$ids"
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

# Whether this run used the openshell backend and docker is there to clean up after it. A docker
# CLI whose daemon cannot be reached (none running, or a user outside the docker group) ran no
# container of this run, so there is nothing to clean: skipped runs must not fail on it.
# Asked once per cleanup and cached: a hung daemon would otherwise cost the bound twice.
OPENSHELL_DOCKER=""
uses_openshell_docker() {
    if [ -z "$OPENSHELL_DOCKER" ]; then
        OPENSHELL_DOCKER=no
        if [ "${AI_REVIEW_BACKEND:-openshell}" != "podman" ] && command -v docker >/dev/null 2>&1; then
            local status=0
            timeout 1m docker info >/dev/null 2>&1 || status=$?
            if [ "$status" -eq 0 ]; then
                OPENSHELL_DOCKER=yes
            elif [ "$status" -eq 124 ]; then
                # A daemon that refuses the connection ran nothing, but one that hangs may still run
                # a container from a killed review, with the model credentials in its config.
                warning "The docker daemon did not answer within a minute, so a review container left by a killed run may still be running. Check the runner."
            fi
        fi
    fi
    [ "$OPENSHELL_DOCKER" = yes ]
}

# The openshell backend's containers and its storage volume carry this workdir's label. After a
# SIGKILL the agent container keeps running, with the model credentials in its config, and the
# volume holds the sandbox images, so remove both. Containers first: a volume in use stays.
remove_docker_leftovers() {
    uses_openshell_docker || return 0
    local filter="label=$WORKDIR_LABEL=$AI_DIR" ids id volumes volume failed=0
    ids="$(docker ps -aq --filter "$filter")" || failed=1
    while IFS= read -r id; do
        [ -n "$id" ] || continue
        docker rm -f "$id" >/dev/null || failed=1
    done <<<"$ids"
    volumes="$(docker volume ls -q --filter "$filter")" || failed=1
    while IFS= read -r volume; do
        [ -n "$volume" ] || continue
        docker volume rm -f "$volume" >/dev/null || failed=1
    done <<<"$volumes"
    return "$failed"
}

# A review step killed before its own chown leaves root's files in AI_DIR, which the runner user
# cannot remove. The openshell image gives them back, when the run pulled it. There is no sudo
# fallback for a missing image: only containers of that image write root's files here, so a run
# that left them has it, and a root chown over a tree the agent wrote is not worth the risk.
give_back_workdir() {
    uses_openshell_docker || return 1
    local openshell_image
    openshell_image="$(openshell_image_ref "${AI_REVIEW_AGENTIC_CI_VERSION:-}")" || return 1
    docker image inspect "$openshell_image" >/dev/null 2>&1 || return 1
    timeout 2m docker run --rm --pull never \
        --label "$WORKDIR_LABEL=$AI_DIR" \
        -v "$AI_DIR:$AI_DIR" \
        --entrypoint chown \
        "$openshell_image" \
        -R -h -P "$(id -u):$(id -g)" "$AI_DIR"
}

cmd_cleanup() {
    [ -n "${GITHUB_WORKSPACE:-}" ] || error "GITHUB_WORKSPACE is not set"

    # Attempt every removal: one that fails must not leave the credential directory behind.
    local dir failed=0
    remove_review_containers || failed=1
    remove_docker_leftovers || failed=1
    if [ -d "$AI_DIR" ] && [ ! -L "$AI_DIR" ]; then
        rm -rf -- "$AI_DIR" 2>/dev/null || { give_back_workdir; rm -rf -- "$AI_DIR"; } || failed=1
    fi
    if [ -d "$HEAD_DIR" ] && [ ! -L "$HEAD_DIR" ]; then
        rm -rf -- "$HEAD_DIR" || failed=1
    fi

    # rm does not follow a link given as the path itself.
    if dir="$(triage_dir)"; then
        rm -rf -- "$dir" || failed=1
    fi
    if dir="$(usage_dir)"; then
        rm -rf -- "${dir:?}" || failed=1
    fi

    # A review step that was killed before its trap ran leaves its private TMPDIR and OTEL
    # directory behind, and the post step leaves its copy of the review output.
    if [ -n "${RUNNER_TEMP:-}" ]; then
        for dir in "$RUNNER_TEMP"/ai-review-tmp.* "$RUNNER_TEMP"/ai-review-otel.* \
            "$RUNNER_TEMP"/ai-review-post.*; do
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
        standdown) cmd_standdown ;;
        review) cmd_review ;;
        post) cmd_post ;;
        export) cmd_export ;;
        relay) cmd_relay ;;
        cleanup) cmd_cleanup ;;
        *)
            echo "Usage: $0 {check|prepare|standdown|review|post|export|relay|cleanup}" >&2
            exit 2
            ;;
    esac
}

main "$@"
