# code-review-skills

Claude Code plugin providing AI code review for GitLab merge requests.

## Repository Structure

```text
.claude-plugin/plugin.json   Plugin packaging metadata
skills/                      Skill directories (each contains SKILL.md + scripts/)
  gitlab-code-review/        GitLab code review skill
    SKILL.md                 Skill definition and review instructions
    scripts/review.py        Review posting and display script
```

## Architecture

This plugin provides a single skill (`gitlab-code-review`) that performs structured AI code review of GitLab merge requests.

**SKILL.md** is the orchestrator: it instructs the agent to review the git diff, produce structured JSON output, and invoke the posting script.

**review.py** handles all deterministic work: JSON parsing and validation, chill-mode filtering, platform detection (GitLab CI, GitHub, local), posting inline comments and summaries to GitLab MRs, and terminal display for local use.

## Conventions

- The skill reviews committed diffs only (no uncommitted changes)
- Review output is written to `/tmp/ai-review-output.json` following a strict schema
- The posting script is invoked directly (not via `python`) to use the uv shebang
- `CHILL_MODE` (default: true) filters suggestion-level comments
- Previous AI review discussions are deleted before posting new ones (GitLab)
- Comments on unchanged code are deduplicated across review iterations
