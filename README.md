# code-review-skills

Agent skills for AI code review of committed changes, packaged as a plugin for Claude Code and Codex. Each skill is a plain skill directory, so harnesses that load skill directories can run them too.

## Overview

| Skill | What it does | Use it when |
|-------|--------------|-------------|
| `gitlab-code-review` | One review pass over a GitLab MR. Produces structured JSON with inline comments, posts it to the MR in GitLab CI, or shows it in the terminal. Supports chill mode and deduplicates comments across review iterations. | You want a GitLab MR reviewed and the comments posted to it. |
| `github-code-review` | One review pass over a GitHub pull request diff that is already on disk. Writes the same structured JSON as `gitlab-code-review` and never posts. The [GitHub Action](#github-action) runs it and posts the result with a separate step. | You want a GitHub PR reviewed with inline comments, suggestions and a summary. |
| `code-review-panel` | A triage script sets the risk tier (quick, standard or deep) from what the change touches and picks the lenses. One reviewer subagent runs per lens, skeptic subagents verify each finding, and the ranked report blocks on security. It can also write `gitlab-code-review` JSON. | A change needs a verified, multi-lens review, or you want a risk tier and an approval hint for it. |
| `security-diff-review` | Security only. One finder subagent looks for exploitable vulnerabilities the change adds, one filter subagent per finding scores it, and only findings that score 8 or more out of 10 are kept. | You want a security pass on a branch, PR or MR. |

All four review committed changes only, not the working tree. The panel and the security review use subagents when the harness has them; without them, the report says the checks were not independent.

## Installation

### Claude Code

Load the plugin from a checkout for one session:

```bash
claude --plugin-dir /path/to/code-review-skills
```

Or install it from a marketplace that lists the plugin:

```bash
claude plugin install code-review-skills@<marketplace>
```

### Codex

Install it from a marketplace that lists the plugin:

```bash
codex plugin add code-review-skills@<marketplace>
```

Or copy all the skill directories into your Codex skills directory:

```bash
mkdir -p "${CODEX_HOME:-$HOME/.codex}/skills"
cp -r /path/to/code-review-skills/skills/* "${CODEX_HOME:-$HOME/.codex}/skills/"
```

Copy them all: the panel reads the security review's references and stops when they are missing.

## Usage

| Harness | Invoke a skill |
|---------|----------------|
| Claude Code | `/code-review-skills:<skill>` |
| Codex, plugin install | `$code-review-skills:<skill>` |
| Codex, copied skill directories | `$<skill>` |

You can also ask in plain words, such as "run the review panel on this branch" or "security review of this PR". Text after the skill name is passed to it:

```text
/code-review-skills:gitlab-code-review Focus on security and error handling
/code-review-skills:code-review-panel
/code-review-skills:security-diff-review against origin/main
```

The panel and the security review find the base on their own: GitLab CI merge request variables, then `GITHUB_BASE_REF` in GitHub Actions, then `origin/HEAD`. Name a base when none of these fits.

### Environment Variables (gitlab-code-review)

| Variable | Required for | Default | Description |
|----------|-------------|---------|-------------|
| `GITLAB_API_TOKEN` | GitLab CI | -- | GitLab Personal Access Token |
| `CI_SERVER_URL` | -- | `https://gitlab.com` | GitLab server URL |
| `CI_PROJECT_ID` | GitLab CI | -- | GitLab project ID |
| `CI_MERGE_REQUEST_IID` | GitLab CI | -- | Merge request IID |
| `CI_MERGE_REQUEST_DIFF_BASE_SHA` | GitLab CI | -- | Base SHA for diff positioning |
| `CI_COMMIT_SHA` | GitLab CI | -- | Head commit SHA |
| `CHILL_MODE` | -- | `true` | Filter out suggestion-level comments |
| `VERBOSE` | -- | `false` | Show detailed API error responses |

### REVIEW.md

Code owners tune the panel and the security review with a `REVIEW.md` file at the repo root, read from the base ref: sensitive paths, paths to skip, what failure means in the repo, a nit cap, and security rule overrides. See [skills/code-review-panel/references/review-md.md](skills/code-review-panel/references/review-md.md) for the format and a worked example.

## GitHub Action

The repo root is a composite action that reviews a pull request and posts the review: inline comments with applicable suggestions, one summary comment that is updated in place, cleanup of stale AI threads, and no repeats on code that did not change between pushes. The agent runs through [agentic-ci](https://github.com/opendatahub-io/agentic-ci) in an OpenShell sandbox, inside a privileged docker container on the runner (or in a Podman container with `backend: podman`, see [Backends](#backends)), reads code and writes one JSON file. A separate step holds the write token and posts that file through the same `review.py` GitLab uses.

```yaml
name: AI review
on:
  pull_request:
    types: [opened, synchronize, reopened, ready_for_review]

concurrency:
  group: ai-review-${{ github.event.pull_request.number }}
  cancel-in-progress: true

permissions:
  contents: read
  pull-requests: write

jobs:
  review:
    runs-on: ubuntu-latest
    # A composite action cannot set a timeout, so bound the whole job here. It must cover the
    # image pulls (up to 15 minutes each, about a minute in practice), timeout_minutes (20 by
    # default) and the checkout.
    timeout-minutes: 60
    env:
      # The credential is not an input: set the key agentic-ci reads in the job environment.
      OPENAI_API_KEY: ${{ secrets.OPENAI_API_KEY }}
    steps:
      - uses: opendatahub-io/code-review-skills@<full commit sha>
```

Pin a full commit SHA: there are no release tags yet. The action checks out the PR head itself, so the job needs no checkout step. Draft pull requests are skipped with a notice, and `ready_for_review` reviews them once they are marked ready.

Requirements: git 2.29 or later on the runner, what the backend needs (see [Backends](#backends)), and a credential for the harness in the job's `env:`. For `codex`, the default, that is `OPENAI_API_KEY`; for `claude-code` it is `ANTHROPIC_API_KEY` or `CLAUDE_CODE_OAUTH_TOKEN` (a token from `claude setup-token`). For Vertex, set `GCLOUD_CREDENTIALS` or `GCP_SERVICE_ACCOUNT_KEY` in the job's `env:`, or set `CLAUDE_CODE_USE_VERTEX=1` on its own, which needs application default credentials as a file (`GOOGLE_APPLICATION_CREDENTIALS`, or the well-known `~/.config/gcloud/application_default_credentials.json` that `gcloud auth application-default login` writes), plus `ANTHROPIC_VERTEX_PROJECT_ID` (or `GCP_PROJECT_ID`). With `backend: openshell` the review step reads that file on the runner (the well-known one under `CLOUDSDK_CONFIG` when that is set) and passes its content into the container, which accepts only a service account key or a `gcloud` user credential; with `CLAUDE_CODE_USE_VERTEX=1` and no such file, the step fails before pulling anything. With `backend: podman`, agentic-ci on the runner reads the file itself. The GCE metadata server is not supported, so `GCLOUD_CREDENTIALS` or `GCP_SERVICE_ACCOUNT_KEY` are the portable options. agentic-ci resolves the credentials. Bedrock is not supported.

Codex has no plugin flag, so it does not load the action's own copy of the skill: it loads the `github-code-review` skill installed in its image from the skills-registry. The image must be a build that already includes that skill: the first agentic-ci release after 0.5.15, or a `:latest` built after 2026-11-25, when code-review-skills commit `ac21ab6` added it. An older image cannot find the skill, and the run fails with "produced no output". The image version, not the action version, then decides which skill revision runs, so pin the image by digest.

To run Claude Code instead, set `harness: claude-code` and put its credential in the job's `env:`; the image then defaults to `quay.io/aipcc/agentic-ci/claude-sandbox:latest` (`claude-runner:latest` with `backend: podman`). Claude Code loads the skills from the action's own copy, so it does not depend on which skills the image ships.

| Input | Default | Description |
|-------|---------|-------------|
| `harness` | `codex` | Agent harness agentic-ci runs: `codex` or `claude-code`. |
| `model` | harness default | Model for the review, also shown in the summary footer. |
| `agentic_ci_version` | `0.5.15` | Version of `agentic-ci` that runs the agent. With `backend: openshell` it is the tag of `quay.io/aipcc/agentic-ci/openshell`, whose bundled agentic-ci runs; with `backend: podman` it is the package version `uvx` installs. Every flag the action passes was checked against the default. |
| `backend` | `openshell` | Where agentic-ci runs the agent: `openshell` or `podman` (see [Backends](#backends)). Any other value fails the review step. |
| `image` | image for the harness and backend | Container image the agent runs in. Empty picks the agentic-ci image for the harness: `quay.io/aipcc/agentic-ci/codex-sandbox:latest` or `.../claude-sandbox:latest` with `backend: openshell`, and `.../codex-runner:latest` or `.../claude-runner:latest` with `backend: podman`. The `*-sandbox` images do not work with Podman: they keep their config under a user that the Podman run does not start the agent as. The default tag floats until the action pins an agentic-ci release: pin a digest (`...sandbox@sha256:<digest>`) to freeze it. Codex needs an image that ships its skills (see above). |
| `mode` | `standard` | `standard` runs one review pass with `github-code-review`. `deep` runs `code-review-panel`, which ignores `review_instructions` (see [Deep review](#deep-review)). |
| `timeout_minutes` | `20` standard, `75` deep | Wall-clock limit for the agent step. The step fails when the review takes longer. Empty picks the default for the mode. |
| `review_instructions` | -- | Extra guidance such as focus areas or team standards, passed to the review as a file. Standard mode only. |
| `chill_mode` | `true` | Drop suggestion-level comments. |
| `inline_fixes` | `true` | Post `fix` objects as applicable suggestions. |

### Backends

`backend: openshell`, the default, runs the image `quay.io/aipcc/agentic-ci/openshell:<agentic_ci_version>` as a privileged docker container (`docker run --rm --privileged --init`). The agentic-ci bundled in that image runs `agentic-ci run --backend openshell`: it starts an OpenShell gateway with its own nested Podman, uploads `.ai-review` into the sandbox at `/sandbox/.ai-review`, runs the agent from the `*-sandbox` image and downloads the result back. This is the topology agentic-ci's own `E2E openshell-sandbox` job runs on `ubuntu-latest`: run [41913015162](https://github.com/opendatahub-io/agentic-ci/actions/runs/41913015162/job/126867367300) (job 126867367300) ran Codex and Claude Code through it with `openshell:0.5.15`, as did run 41777652433 on `main` at the same commit.

- Requirements: docker with privileged containers, as on GitHub-hosted Ubuntu runners. The image (about 300 MB) and the sandbox image (about 700 to 900 MB) are pulled before the agent starts, each under its own 15 minute limit, so neither counts against `timeout_minutes`.
- Only `.ai-review` is mounted, at its own path, with a docker volume of the run for the nested image storage: never `RUNNER_TEMP`, the checkout or the docker socket. The container gets the harness credentials as `-e NAME`, so no value is on a command line, and never `GITHUB_TOKEN`, `GH_TOKEN` or the OIDC variables.
- A Google credential file (`GCP_SERVICE_ACCOUNT_KEY` naming a file, `GOOGLE_APPLICATION_CREDENTIALS`, or the well-known `application_default_credentials.json` under `CLOUDSDK_CONFIG` or `~/.config/gcloud`, in that order) is not in the container, so the review step passes its content as `GCLOUD_CREDENTIALS`. Service account keys and `gcloud` user credentials work. Workload Identity Federation credentials (from `google-github-actions/auth`) do not work with either backend, and the step fails with an error that says so.
- agentic-ci's `--timeout` does not bound an OpenShell run, so `timeout_minutes` is enforced from outside: the agent gets SIGINT, then SIGKILL 60 seconds later.
- The download back into `.ai-review` leaves files owned by root, so a short container gives them back to the runner user after every run. The cleanup step removes the run's labelled containers and volume, also after a cancelled run.
- `--privileged` is root on the host. On a self-hosted runner, use it only where that is acceptable, or use Podman.

`backend: podman` is the fallback: agentic-ci runs on the runner through `uvx` and starts a Podman container from the `*-runner` image, with `.ai-review` mounted at `/workspace`. It needs Podman on the runner. The Podman on GitHub-hosted runners can need the crun workaround from agentic-ci's [setup-podman action](https://github.com/opendatahub-io/agentic-ci/blob/main/.github/actions/setup-podman/action.yml). The action does not run `sudo` for you, so add that step to the job when the container fails to start.

Limitations:

- Only the `pull_request` event is supported. Runs for pull requests from forks and runs triggered by Dependabot exit 0 with a notice, because they get no secrets and a read-only token. A run without a credential for the harness in the job environment, or for a draft pull request, does the same.
- The action posts as `github-actions[bot]` with the workflow token, and recognizes its own comments by that login and a marker. Run one copy of the action per pull request: two would fight over the same comments.
- The action fetches the full history of the PR head (`fetch-depth: 0`) on every run, to find the merge base reliably, and copies the history of the base and the head into the agent's repository. On a very large repo that costs time and disk on every push.
- A very large diff can exceed what the agent reads in one run. The review then says in its summary that it covered only part of the diff.
- The agent runs in a sandbox (OpenShell, or a Podman container with `backend: podman`) with every tool the harness provides enabled, so the sandbox is the boundary, not a tool allowlist. It reads an export of the PR's working tree in `.ai-review/repo` under the workspace, with a git repository of its own: HEAD detached at the PR head and the full history of the base and the head, fetched from the checkout's objects without its config, remotes, hooks or credentials. The checkout itself, with its `.git`, sits in `.ai-review-head` and is never mounted, so the container never sees it or the write token. The action never runs git in `.ai-review` once the agent starts. The post step reads one file from the mount, refuses a symlink or anything over 1 MiB, and posts a copy of it. Both directories are removed at the end.
- The agent has network access and the model credentials, and its tools include Bash, so a prompt-injected agent can use them.
- `backend: openshell` runs a `--privileged` docker container on the runner. On a self-hosted runner that is root on the host: use it only on runners dedicated to this job, or set `backend: podman`.
- The default `image` tag floats (it is rebuilt daily), so a new image can change the review between runs: pin a digest with the `image` input to freeze it.
- agentic-ci is pinned by version, not by hash, and its dependencies float.
- Do not use `pull_request_target` with a checkout of the PR head: that runs code from the PR with write access and secrets. The action refuses that event.

### Deep review

`mode: deep` runs the [code-review-panel](#overview) skill instead of the single pass: one reviewer per lens plus skeptics that verify each finding. It costs many times a standard review and takes much longer, so run it on demand, for example from a second workflow that a `deep-review` label starts:

```yaml
name: AI deep review
on:
  pull_request:
    types: [labeled, synchronize, reopened, ready_for_review]

permissions:
  contents: read
  pull-requests: write

jobs:
  deep-review:
    # When the label is added, and on later pushes while it is set. Other labels do not start it.
    if: >-
      github.event.action == 'labeled' && github.event.label.name == 'deep-review' ||
      github.event.action != 'labeled' && contains(github.event.pull_request.labels.*.name, 'deep-review')
    # Its own group, so a deep review and a standard review do not cancel each other.
    concurrency:
      group: ai-deep-review-${{ github.event.pull_request.number }}
      cancel-in-progress: true
    runs-on: ubuntu-latest
    # The image pulls (up to 15 minutes each), timeout_minutes (75 by default in deep mode) and the checkout.
    timeout-minutes: 120
    env:
      OPENAI_API_KEY: ${{ secrets.OPENAI_API_KEY }}
    steps:
      - uses: opendatahub-io/code-review-skills@<full commit sha>
        with:
          mode: deep
```

The panel writes the same review JSON, and the post step validates and posts it like a standard review, under the same summary comment and marker. A deep review and a standard review of the same pull request therefore replace each other's comments: the one that finishes last is what the pull request shows. Deep mode ignores `review_instructions` with a warning, because the panel reads its guidance from `REVIEW.md` at the base branch, so a pull request cannot loosen its own review. With `harness: codex`, the panel also comes from the image: `code-review-panel` predates `github-code-review`, so an image that ships the one ships both.

## Development

### Prerequisites

- Python 3.10+ with [ruff](https://docs.astral.sh/ruff/)
- [shellcheck](https://www.shellcheck.net/)
- [uv](https://docs.astral.sh/uv/) for running skillsaw

### Validate Changes

```bash
make lint
make test
```

See [CONTRIBUTING.md](CONTRIBUTING.md) for the full development workflow.

## Versioning

Use git tags (`v0.1.0`, `v0.2.0`, etc.) for releases. The `main` branch is the development head.

## License

[Apache License 2.0](LICENSE)

The finder and filter references in `skills/security-diff-review/` are adapted from [anthropics/claude-code-security-review](https://github.com/anthropics/claude-code-security-review), Copyright (c) 2025 Anthropic, under the MIT License: see [skills/security-diff-review/LICENSE-MIT](skills/security-diff-review/LICENSE-MIT).
