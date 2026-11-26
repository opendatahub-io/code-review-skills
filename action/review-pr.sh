#!/usr/bin/env bash
# Steps of the GitHub Action in ../action.yml, kept here so shellcheck covers them.
#
# Usage: review-pr.sh {check|prepare|review|post|cleanup}
#
# The agent step holds no write token. By default (backend openshell) the openshell image runs
# as a privileged docker container, and agentic-ci inside it runs the agent in an OpenShell
# sandbox, the way agentic-ci's own end-to-end job runs it on hosted runners. With backend podman
# agentic-ci runs on the runner and starts a Podman container.
# The agent reads an export of the PR checkout with a git repository of its own, built without
# the checkout's config, remotes or credentials, and writes one file. The post step holds the
# token, validates that one file and posts a copy of it through the same review.py GitLab uses.
#
# Environment (set by action.yml):
#   AI_REVIEW_EVENT               github.event_name (check)
#   AI_REVIEW_HEAD_REPO           pull_request.head.repo.full_name (check)
#   AI_REVIEW_ACTOR               github.actor (check)
#   AI_REVIEW_TRIGGERING_ACTOR    github.triggering_actor (check)
#   AI_REVIEW_HARNESS             harness input (check, review, post)
#   AI_REVIEW_MODE                mode input: standard or deep (check, prepare, review)
#   AI_REVIEW_DRAFT               pull_request.draft (check)
#   AI_REVIEW_BACKEND             backend input: openshell or podman (review, cleanup)
#   AI_REVIEW_BASE_SHA            pull_request.base.sha (prepare, review)
#   AI_REVIEW_HEAD_SHA            pull_request.head.sha (prepare, review)
#   AI_REVIEW_INSTRUCTIONS        review_instructions input (prepare, review)
#   AI_REVIEW_MODEL               model input (review)
#   AI_REVIEW_TIMEOUT_MINUTES     timeout_minutes input (review)
#   AI_REVIEW_AGENTIC_CI_VERSION  agentic_ci_version input (review, cleanup)
#   AI_REVIEW_IMAGE               image input (review)
#   GITHUB_OUTPUT, GITHUB_REPOSITORY, GITHUB_WORKSPACE, GITHUB_ACTION_PATH, GITHUB_RUN_ID,
#   GITHUB_RUN_ATTEMPT:           set by the runner
#   RUNNER_TEMP                   set by the runner (review: parent of the agent's private TMPDIR,
#                                 post: parent of the copy of the output)
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
# The agent's view of AI_DIR, which the prompt names, per backend. OpenShell uploads the workdir
# into the sandbox's home under its own name.
PODMAN_CONTAINER_DIR=/workspace
OPENSHELL_CONTAINER_DIR=/sandbox/.ai-review
# The image that runs agentic-ci and the OpenShell gateway for the openshell backend. Its tag is
# the agentic_ci_version input, so the bundled agentic-ci is the version the action asks for.
OPENSHELL_IMAGE_REPO="quay.io/aipcc/agentic-ci/openshell"
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
# run the agent as, so podman keeps the runner images.
CLAUDE_RUNNER_IMAGE="quay.io/aipcc/agentic-ci/claude-runner:latest"
CODEX_RUNNER_IMAGE="quay.io/aipcc/agentic-ci/codex-runner:latest"
CLAUDE_SANDBOX_IMAGE="quay.io/aipcc/agentic-ci/claude-sandbox:latest"
CODEX_SANDBOX_IMAGE="quay.io/aipcc/agentic-ci/codex-sandbox:latest"
# The oldest git whose fetch has --no-write-fetch-head and --no-auto-maintenance.
MIN_GIT_MAJOR=2
MIN_GIT_MINOR=29
# The largest review output the post step accepts, in bytes.
MAX_OUTPUT_BYTES=1048576
REVIEW_PY_REL="skills/gitlab-code-review/scripts/review.py"
# The private TMPDIR of the review step, which the EXIT trap removes.
REVIEW_TMP=""
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

    if [ "${AI_REVIEW_DRAFT:-}" = "true" ]; then
        skip_reason="the pull request is a draft. Marking it ready for review runs the review"
    elif [ -z "${AI_REVIEW_HEAD_REPO:-}" ] || [ "${AI_REVIEW_HEAD_REPO}" != "${GITHUB_REPOSITORY:-}" ]; then
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
    rm -f -- "$DIFF_FILE" "$INSTRUCTIONS_FILE"
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

    # Claude Code loads this copy, not the registry copy in the image, which lacks the skill.
    # Codex takes no plugin directory: it loads the skill installed in its runner image.
    # Only the plugin and the skills go in: with uses: ./ the action path is the workspace root,
    # where earlier steps can leave credentials, and the workdirs themselves.
    tar -C "$GITHUB_ACTION_PATH" -cf - .claude-plugin skills | tar -C "$PLUGIN_DIR" -xf -

    # Deep mode never passes the instructions, so they stay off the mount.
    if [ "$REVIEW_MODE" = "standard" ] && [ -n "${AI_REVIEW_INSTRUCTIONS:-}" ]; then
        printf '%s' "$AI_REVIEW_INSTRUCTIONS" >"$INSTRUCTIONS_FILE"
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

    local openshell_image="$OPENSHELL_IMAGE_REPO:$version" suffix
    suffix="$(docker_run_suffix)"
    local name="ai-review-$suffix" volume="ai-review-store-$suffix"
    local label="$WORKDIR_LABEL=$AI_DIR"

    # Nothing that can write to the repository or mint tokens reaches docker, not even empty.
    unset GITHUB_TOKEN GH_TOKEN
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
    # Narrows the image's skill catalog to this plugin, as the GitLab job does.
    export AGENT_ENABLED_PLUGINS=code-review-skills
    local env_args=(-e AGENT_ENABLED_PLUGINS)
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

    # agentic-ci is the direct child of the init process, which forwards the timeout's SIGINT to
    # it, so it deletes the sandbox and stops the gateway before it exits. No -i or -t: the
    # OpenShell CLI reads a non-terminal stdin to its end before each command it sends.
    AGENT_STATUS=0
    timeout -s INT --kill-after=60s "${minutes}m" \
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
    [[ "$version" =~ $version_re ]] || error "agentic_ci_version must be a package version such as 0.5.15, got: '$version'"
    # The version is the openshell image's tag, which allows fewer characters than a version.
    if [ "$backend" = "openshell" ] && [[ ! "$version" =~ ^[0-9][0-9A-Za-z._-]*$ ]]; then
        error "agentic_ci_version must be a tag of the openshell image with the openshell backend, got: '$version'"
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
        # a standard review. It takes the repo's guidance from REVIEW.md at the base ref, so a PR
        # cannot loosen its own review, and free-form instructions are not passed. One line: the
        # prompt is a single argument.
        # On a root runner the agent runs as another user than the owner of the mounted repo, so
        # git refuses it as dubious ownership. A global safe.directory reaches the triage's own
        # git calls and every subagent, where a -c on one command would not.
        local task="the committed changes in $cdir/repo from base $base to head $head. First run git config --global --add safe.directory $cdir/repo, because the repository can belong to another user. Run its triage from inside $cdir/repo with --base $base --head $head, pass --gitlab-json $cdir/out/ai-review-output.json to its final report command, and do not post anything."
        if [ "$harness" = "claude-code" ]; then
            prompt="/code-review-skills:code-review-panel Review $task"
        else
            prompt="Invoke the /code-review-skills:code-review-panel skill to review $task"
        fi
        if [ -n "${AI_REVIEW_INSTRUCTIONS:-}" ]; then
            warning "review_instructions is ignored in deep mode: the panel reads REVIEW.md from the base branch instead."
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
    fi

    # The container is the sandbox: agentic-ci runs the agent with every tool enabled, so a
    # tool allowlist would not hold. The container never mounts the checkout's .git, and the
    # post step reads one validated file from the mount.
    local args=(
        run
        --harness "$harness"
        --backend "$backend"
        --workdir "$AI_DIR"
        --image "$image"
        --no-otel
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

    if [ "$backend" = "openshell" ]; then
        run_openshell_agent "$harness" "$image" "$version" "$minutes" "${args[@]}"
    else
        run_podman_agent "$image" "$version" "$minutes" "${args[@]}"
    fi

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

    # The run can exit 0 when the agent wrote nothing.
    [ -s "$OUT" ] || error "The review produced no output at $OUT"
}

# Run the agent with the podman backend: agentic-ci runs on the runner through uvx and starts the
# agent's Podman container itself. Sets AGENT_STATUS.
#
# Usage: run_podman_agent IMAGE VERSION MINUTES AGENTIC_CI_ARGS...
run_podman_agent() {
    local image="$1" version="$2" minutes="$3"
    shift 3

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

    # SIGINT makes agentic-ci stop its container before it exits.
    AGENT_STATUS=0
    TMPDIR="$REVIEW_TMP" timeout -s INT --kill-after=60s "${minutes}m" \
        uvx --from "agentic-ci==$version" agentic-ci "$@" || AGENT_STATUS=$?
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
    export AGENT_TOOL="${AI_REVIEW_HARNESS:-codex}" AGENT_REASONING_EFFORT="$AGENT_EFFORT"

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

# Whether this run used the openshell backend and docker is there to clean up after it.
uses_openshell_docker() {
    [ "${AI_REVIEW_BACKEND:-openshell}" != "podman" ] && command -v docker >/dev/null 2>&1
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
# cannot remove. The openshell image gives them back, when the run pulled it.
give_back_workdir() {
    uses_openshell_docker || return 1
    local version="${AI_REVIEW_AGENTIC_CI_VERSION:-}"
    [[ "$version" =~ ^[0-9][0-9A-Za-z._-]*$ ]] || return 1
    local openshell_image="$OPENSHELL_IMAGE_REPO:$version"
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
