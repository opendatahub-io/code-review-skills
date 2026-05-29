# code-review-skills

Claude Code plugin for AI-powered code review. Reviews GitLab MR diffs or local branch changes, produces structured feedback with inline comments, and posts results to GitLab or displays them in the terminal.

## Overview

This plugin provides a `code-review` skill that:

- Reviews all commits since the base branch using git diffs
- Produces structured JSON output with summary, positive aspects, and inline comments
- Posts inline comments and a summary note to GitLab MRs (in CI)
- Falls back to formatted terminal display when run locally
- Supports chill mode to filter suggestion-level comments
- Deduplicates comments across review iterations

## Installation

```bash
# Install as a Claude Code plugin
claude plugin install /path/to/code-review-skills
```

## Usage

Once installed, invoke the skill in Claude Code:

```
/code-review
```

Or with additional review instructions:

```
/code-review Focus on security and error handling
```

### Environment Variables

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
