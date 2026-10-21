# Contributing to code-review-skills

Thank you for your interest in contributing to code-review-skills! This plugin provides agent skills for AI code review: a GitLab MR reviewer, a risk-tiered review panel and a security diff review.

## Getting Started

1. **Fork the repository** on GitHub.
2. **Clone your fork** locally:
   ```bash
   git clone https://github.com/<your-username>/code-review-skills.git
   cd code-review-skills
   ```
3. **Create a branch** for your work:
   ```bash
   git checkout -b my-change
   ```

## Repository Layout

```text
.claude-plugin/plugin.json   Plugin packaging metadata
skills/                      Skill directories (SKILL.md, plus scripts/ or references/)
  gitlab-code-review/        Single-pass GitLab MR review (scripts/review.py)
  code-review-panel/         Risk-tiered review panel (references/lenses/, scripts/panel.py)
  security-diff-review/      Security-only diff review (references/finder.md, references/filter.md)
```

Read [CLAUDE.md](CLAUDE.md) for architecture details and conventions.

## Ways to Contribute

- **Improve a skill** in `skills/<name>/`
- **Add a lens** to the review panel: write one persona in `skills/code-review-panel/references/lenses/<lens>.md` with the four headings `## Stance`, `## Grounding`, `## Checklist` and `## Output`, and add one `LENS_RULES` entry to `skills/code-review-panel/scripts/panel.py` with the lens's own path and content regexes. Add a path-category row to `panel.py` only when those paths should also change the tier, the approval hint, or whether a change counts as docs or tests only. `panel.py triage` exits 3 when the persona files and `LENS_RULES` disagree.
- **Add a security rule** to `skills/security-diff-review/references/filter.md` under the next free id (E18 and up for exclusions, P14 and up for precedents). Keep existing ids as they are, and list a retired rule as retired: repos drop rules by id in their REVIEW.md. The ids differ from upstream's numbering: upstream printed 16 twice, so E15 covers regex injection and regex DoS (upstream 15 and the first 16), E16 is insecure documentation and E17 is audit logs. E10 is narrowed to safe code of a memory-safe language, and P13 is ours.
- **Add platform support** (e.g. GitHub PR posting)
- **Fix a bug** or improve existing functionality
- **Improve documentation**

## Development Setup

### Prerequisites

- **Python 3.10+** with [ruff](https://docs.astral.sh/ruff/) (`pip install ruff`)
- **shellcheck** (`dnf install ShellCheck` on Fedora)
- **[uv](https://docs.astral.sh/uv/)** for running skillsaw
- **Git**

### Validate Your Changes

Before submitting, always run:

```bash
make lint
```

The `lint` target runs:
- **skillsaw** validates skill frontmatter and plugin structure
- **ruff** checks and formats Python code
- **shellcheck** lints shell scripts

Then run the tests, which use the standard library only and build throwaway git repos (the
GitLab JSON checks also need `uv`):

```bash
make test
```

### Test Locally

Check what the review panel would run on your branch, without starting any subagent:

```bash
python3 skills/code-review-panel/scripts/panel.py triage --base origin/main --format text
```

To test the skills with Claude Code, load the plugin from your checkout for one session, then invoke a skill such as `/code-review-skills:code-review-panel`:

```bash
claude --plugin-dir .
```

To test them with Codex, copy the skill directories into your Codex skills directory, then invoke a skill such as `$code-review-panel`. Remove the copies when done testing:

```bash
mkdir -p "${CODEX_HOME:-$HOME/.codex}/skills"
cp -r skills/* "${CODEX_HOME:-$HOME/.codex}/skills/"
codex
```

## Submitting Your Contribution

1. **Run validation** before committing:
   ```bash
   make lint
   make test
   ```
2. **Commit your changes** with a clear, descriptive commit message.
3. **Push to your fork** and open a Pull Request against `main`.
4. **Fill out the PR template**.

### Commit Messages

Write concise commit messages that explain *why* the change was made:

- `feat:` for new features or improvements
- `fix:` for bug fixes
- `docs:` for documentation changes
- `chore:` for maintenance tasks

Example: `feat: add GitHub PR posting support`

## Pull Request Process

1. **All PRs must pass CI checks**, including `make lint` and `make test`.
2. **Fill out the PR template completely**.
3. **At least one maintainer review** is required before merging.
4. **Keep PRs focused**: one logical change per PR.

## Style and Conventions

### Python

- Format with `ruff format`
- Pass `ruff check` with no errors
- Use Python 3.10+ type annotations

### Shell Scripts

- Always use `set -euo pipefail`
- Pass `shellcheck` with no warnings

### Skills

- Each skill lives in `skills/<name>/` with a `SKILL.md` and optional `scripts/` and `references/` directories
- `SKILL.md` must include YAML frontmatter with at least `name` and `description`
- Keep each `SKILL.md` within 12,000 characters and each description within 800; put tables, prompts and formats in `references/`
- Put personas and prompt text in Markdown under `references/`, since Codex ignores `agents/`
- Use regular files only: flat installs skip symlinks, and they register every `SKILL.md` they find, so keep one per skill root
- Use `text` fences, not `python`, for Python snippets and prompt text in Markdown, because ruff formats `python` blocks; shell commands may use `bash`
- Bump the version in `.claude-plugin/plugin.json` on every skill change, so Claude Code and Codex refresh their cached copy
- [CLAUDE.md](CLAUDE.md) lists the files that change together across skills

## Code of Conduct

We are committed to providing a welcoming and respectful environment for everyone. Be kind, constructive, and professional in all interactions.

## Getting Help

- **Questions?** Open a [blank issue](https://github.com/opendatahub-io/code-review-skills/issues/new) or reach out to the maintainers.

## License

By contributing, you agree that your contributions will be licensed under the [Apache License 2.0](LICENSE).

The exception is the text in `skills/security-diff-review/references/` adapted from [anthropics/claude-code-security-review](https://github.com/anthropics/claude-code-security-review), which stays under its MIT License: see [LICENSE-MIT](skills/security-diff-review/LICENSE-MIT). Keep the attribution line at the top of `finder.md` and `filter.md` when you edit them.
