#!/usr/bin/env bash
# Steps of the GitHub Action in ../action.yml, kept here so shellcheck covers them.
#
# Usage: review-pr.sh {check|prepare|standdown|review|post|cleanup}
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
#
# Environment (set by action.yml):
#   AI_REVIEW_EVENT               github.event_name (check)
#   AI_REVIEW_HEAD_REPO           pull_request.head.repo.full_name (check)
#   AI_REVIEW_ACTOR               github.actor (check)
#   AI_REVIEW_TRIGGERING_ACTOR    github.triggering_actor (check)
#   AI_REVIEW_HARNESS             harness input (check, review, post)
#   AI_REVIEW_MODE                mode input: standard or deep (check, prepare, standdown,
#                                 review, post)
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
#   AI_REVIEW_MODEL               model input (review)
#   AI_REVIEW_TIMEOUT_MINUTES     timeout_minutes input (review)
#   AI_REVIEW_AGENTIC_CI_VERSION  agentic_ci_version input (review, cleanup): a PyPI version for
#                                 podman, an openshell image tag, optionally <tag>@sha256:<digest>,
#                                 for openshell
#   AI_REVIEW_IMAGE               image input (review)
#   GITHUB_OUTPUT, GITHUB_REPOSITORY, GITHUB_WORKSPACE, GITHUB_ACTION_PATH, GITHUB_RUN_ID,
#   GITHUB_RUN_ATTEMPT:           set by the runner
#   RUNNER_TEMP                   set by the runner (review: parent of the agent's private TMPDIR,
#                                 post: parent of the copy of the output and of the flag that the
#                                 summary was posted, prepare and post: parent of the triage plan,
#                                 standdown: its working directory)
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
#   plugin/                       a copy of this action, loaded with --plugin-dir
#   out/                          the only place the review writes
#
# The triage plan of the deep-review recommendation is $RUNNER_TEMP/ai-review-triage/plan.json,
# outside the mount, written by prepare before any agent runs and read only by post.
#
# prepare and post run in .ai-review-head. standdown runs in RUNNER_TEMP, before the checkout
# exists. review runs in .ai-review, not in a checkout, so uvx does not read a uv config that
# arrives with the PR. check and cleanup run anywhere.

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
PANEL_PY_REL="skills/code-review-panel/scripts/panel.py"
# The directory under RUNNER_TEMP that holds the triage plan, and the plan's largest size in bytes,
# which is also what review.py reads.
TRIAGE_DIR_NAME="ai-review-triage"
MAX_PLAN_BYTES=1048576
# The wall-clock limit of the triage, which reads only git metadata and the diff.
TRIAGE_TIMEOUT=60s
# The deep_review_job input's default, which review.py also uses.
DEFAULT_DEEP_JOB="deep-review"
# The deep_review_label input's default, which review.py also uses.
DEFAULT_DEEP_LABEL="deep-review"
# The exit status of review.py stand-down when a review that should win makes this one redundant.
STANDDOWN_EXIT=10
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
    rm -f -- "$DIFF_FILE" "$INSTRUCTIONS_FILE"
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

    local openshell_image suffix
    openshell_image="$(openshell_image_ref "$version")" \
        || error "No openshell image for agentic_ci_version '$version'"
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
    # cmd_review sets it for both backends.
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
    if [ "$backend" = "openshell" ]; then
        # The version is the openshell image's tag, which allows fewer characters than a
        # version, and can pin the image by digest.
        if [[ ! "${version%%@*}" =~ $version_re ]] || ! openshell_image_ref "$version" >/dev/null; then
            error "agentic_ci_version must be a tag of the openshell image with the openshell backend, such as 0.5.15, or a tag pinned to a digest, such as 0.5.15@sha256:<64 lowercase hex digits>, got: '$version'"
        fi
    else
        case "$version" in
            *@*) error "agentic_ci_version is the PyPI version of agentic-ci with backend: podman, so it cannot pin a digest: only the openshell image is pinned by digest, got: '$version'" ;;
        esac
        [[ "$version" =~ $version_re ]] || error "agentic_ci_version must be a package version such as 0.5.15, got: '$version'"
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

    # Narrows the image's skill catalog to this plugin, as the GitLab job does. agentic-ci reads
    # it from its environment: openshell passes it into the container, and with podman the uvx
    # run inherits it, like the credentials.
    export AGENT_ENABLED_PLUGINS="$ENABLED_PLUGINS"
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

    # A review step that was killed before its trap ran leaves its private TMPDIR behind, and the
    # post step leaves its copy of the review output.
    if [ -n "${RUNNER_TEMP:-}" ]; then
        for dir in "$RUNNER_TEMP"/ai-review-tmp.* "$RUNNER_TEMP"/ai-review-post.*; do
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
        cleanup) cmd_cleanup ;;
        *)
            echo "Usage: $0 {check|prepare|standdown|review|post|cleanup}" >&2
            exit 2
            ;;
    esac
}

main "$@"
