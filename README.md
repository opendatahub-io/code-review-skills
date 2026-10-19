# code-review-skills

Agent skills for AI code review of committed changes, packaged as a plugin for Claude Code and Codex. Each skill is a plain skill directory, so harnesses that load skill directories can run them too.

## Overview

| Skill | What it does | Use it when |
|-------|--------------|-------------|
| `gitlab-code-review` | One review pass over a GitLab MR. Produces structured JSON with inline comments, posts it to the MR in GitLab CI, or shows it in the terminal. Supports chill mode and deduplicates comments across review iterations. | You want a GitLab MR reviewed and the comments posted to it. |
| `code-review-panel` | A triage script sets the risk tier (quick, standard or deep) from what the change touches and picks the lenses. One reviewer subagent runs per lens, skeptic subagents verify each finding, and the ranked report blocks on security. It can also write `gitlab-code-review` JSON. | A change needs a verified, multi-lens review, or you want a risk tier and an approval hint for it. |
| `security-diff-review` | Security only. One finder subagent looks for exploitable vulnerabilities the change adds, one filter subagent per finding scores it, and only findings that score 8 or more out of 10 are kept. | You want a security pass on a branch, PR or MR. |

All three review committed changes only, not the working tree. The panel and the security review use subagents when the harness has them; without them, the report says the checks were not independent.

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

Or copy all three skill directories into your Codex skills directory:

```bash
mkdir -p "${CODEX_HOME:-$HOME/.codex}/skills"
cp -r /path/to/code-review-skills/skills/* "${CODEX_HOME:-$HOME/.codex}/skills/"
```

Copy all three: the panel reads the security review's references and stops when they are missing.

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

## Development

### Prerequisites

- Python 3.10+ with [ruff](https://docs.astral.sh/ruff/)
- [shellcheck](https://www.shellcheck.net/)
- [uv](https://docs.astral.sh/uv/) for running skillsaw

### Validate Changes

```bash
make lint
```

See [CONTRIBUTING.md](CONTRIBUTING.md) for the full development workflow.

## Versioning

Use git tags (`v0.1.0`, `v0.2.0`, etc.) for releases. The `main` branch is the development head.

## License

[Apache License 2.0](LICENSE)

The finder and filter references in `skills/security-diff-review/` are adapted from [anthropics/claude-code-security-review](https://github.com/anthropics/claude-code-security-review), Copyright (c) 2025 Anthropic, under the MIT License: see [skills/security-diff-review/LICENSE-MIT](skills/security-diff-review/LICENSE-MIT).
