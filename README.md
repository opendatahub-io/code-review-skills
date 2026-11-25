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

The repo root is a composite action that reviews a pull request and posts the review: inline comments with applicable suggestions, one summary comment that is updated in place, cleanup of stale AI threads, and no repeats on code that did not change between pushes. The agent runs in a Podman container through [agentic-ci](https://github.com/opendatahub-io/agentic-ci), reads code and writes one JSON file. A separate step holds the write token and posts that file through the same `review.py` GitLab uses.

```yaml
name: AI review
on: pull_request

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
    # image pull (up to 15 minutes), timeout_minutes (20 by default) and the checkout.
    timeout-minutes: 45
    env:
      # The credential is not an input: set the key agentic-ci reads in the job environment.
      ANTHROPIC_API_KEY: ${{ secrets.ANTHROPIC_API_KEY }}
    steps:
      - uses: opendatahub-io/code-review-skills@<full commit sha>
```

Pin a full commit SHA: there are no release tags yet. The action checks out the PR head itself, so the job needs no checkout step.

Requirements: Podman on the runner, and a credential for the harness in the job's `env:`. For `claude-code` that is `ANTHROPIC_API_KEY` or `CLAUDE_CODE_OAUTH_TOKEN` (a token from `claude setup-token`); for `codex` it is `OPENAI_API_KEY`. For Vertex, set `GCLOUD_CREDENTIALS` or `GCP_SERVICE_ACCOUNT_KEY` in the job's `env:`, or set `CLAUDE_CODE_USE_VERTEX=1` on its own, which needs application default credentials as a file (`~/.config/gcloud/application_default_credentials.json` or `GOOGLE_APPLICATION_CREDENTIALS`), plus `ANTHROPIC_VERTEX_PROJECT_ID` (or `GCP_PROJECT_ID`). The GCE metadata server is not supported, so `GCLOUD_CREDENTIALS` or `GCP_SERVICE_ACCOUNT_KEY` are the portable options. agentic-ci resolves the credentials. Bedrock is not supported. The Podman on GitHub-hosted runners can need the crun workaround from agentic-ci's [setup-podman action](https://github.com/opendatahub-io/agentic-ci/blob/main/.github/actions/setup-podman/action.yml). The action does not run `sudo` for you, so add that step to the job when the container fails to start.

To run Codex instead, set `harness: codex` and put `OPENAI_API_KEY` in the job's `env:`; the image then defaults to `quay.io/aipcc/agentic-ci/codex-runner:latest`. Codex has no plugin flag, so it does not load the action's own copy of the skill: it loads the `github-code-review` skill installed in its runner image from the skills-registry. The image version, not the action version, then decides which skill revision runs, so pin the image by digest.

| Input | Default | Description |
|-------|---------|-------------|
| `harness` | `claude-code` | Agent harness agentic-ci runs: `claude-code` or `codex`. |
| `model` | harness default | Model for the review, also shown in the summary footer. |
| `agentic_ci_version` | `0.5.15` | Version of the `agentic-ci` package that runs the agent. Every flag the action passes was checked against the default. |
| `image` | runner image for the harness | Container image the agent runs in. Empty picks `quay.io/aipcc/agentic-ci/claude-runner:latest` or `.../codex-runner:latest`. The default tag floats: pin a digest (`...runner@sha256:<digest>`) to freeze it. |
| `timeout_minutes` | `20` | Wall-clock limit for the agent step. The step fails when the review takes longer. |
| `review_instructions` | -- | Extra guidance such as focus areas or team standards, passed to the review as a file. |
| `chill_mode` | `true` | Drop suggestion-level comments. |
| `inline_fixes` | `true` | Post `fix` objects as applicable suggestions. |

Limitations:

- Only the `pull_request` event is supported. Runs for pull requests from forks and runs triggered by Dependabot exit 0 with a notice, because they get no secrets and a read-only token. A run without a credential for the harness in the job environment does the same.
- The action posts as `github-actions[bot]` with the workflow token, and recognizes its own comments by that login and a marker. Run one copy of the action per pull request: two would fight over the same comments.
- The action fetches the full history of the PR head (`fetch-depth: 0`) on every run, to find the merge base reliably. On a very large repo that costs time on every push.
- A very large diff can exceed what the agent reads in one run. The review then says in its summary that it covered only part of the diff.
- The agent runs in a Podman container with every tool the harness provides enabled, so the container is the boundary, not a tool allowlist. It reads an export of the PR's working tree in `.ai-review/repo` under the workspace. The checkout itself, with its `.git`, sits in `.ai-review-head` and is never mounted, so the container never sees it or the write token. The post step reads one file from the mount, refuses a symlink or anything over 1 MiB, and posts a copy of it. Both directories are removed at the end.
- The agent has network access and the model credentials, and its tools include Bash, so a prompt-injected agent can use them.
- The default `image` tag floats (it is rebuilt daily), so a new image can change the review between runs: pin a digest with the `image` input to freeze it.
- agentic-ci is pinned by version, not by hash, and its dependencies float.
- Do not use `pull_request_target` with a checkout of the PR head: that runs code from the PR with write access and secrets. The action refuses that event.

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
