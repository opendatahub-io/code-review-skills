# code-review-skills

Plugin of agent skills for AI code review: a GitLab MR reviewer, a GitHub PR reviewer with a composite GitHub Action, a risk-tiered review panel and a security diff review, for Claude Code, Codex and other harnesses.

## Repository Structure

```text
.claude-plugin/plugin.json   Plugin packaging metadata
action.yml                   Composite GitHub Action that reviews a pull request
action/review-pr.sh          The action's steps (check, prepare, review, post, cleanup)
skills/                      Skill directories (each contains SKILL.md, plus scripts/ or references/)
  gitlab-code-review/        GitLab code review skill
    SKILL.md                 Skill definition and review instructions
    scripts/review.py        Review posting and display script
  github-code-review/        GitHub PR review skill, run by the action
    SKILL.md                 Reads a precomputed diff, writes the review JSON, never posts
  code-review-panel/         Risk-tiered review panel
    SKILL.md                 Orchestration: triage, finders, skeptics, report
    references/              Finder brief, skeptic prompts, report formats, REVIEW.md guide
      lenses/                One persona per lens
    scripts/panel.py         Triage and report script
  security-diff-review/      Security-only diff review
    SKILL.md                 Finder, per-finding filters, report
    references/finder.md     Security finder persona
    references/filter.md     Exclusion rules (E1..), precedents (P1..), REVIEW.md grammar
    LICENSE-MIT              License of the adapted upstream text
tests/                       unittest suite for panel.py, review.py and the action (make test), run against throwaway git repos
```

## Architecture

The plugin provides four skills and one GitHub Action. Each SKILL.md orchestrates. Deterministic work lives in scripts, and prompts and formats live in `references/`.

**gitlab-code-review** performs a single-pass, structured AI code review of a GitLab merge request. Its SKILL.md instructs the agent to review the git diff, produce structured JSON output, and invoke the posting script. **review.py** handles all deterministic work: JSON parsing and validation, chill-mode filtering, platform detection (GitLab CI or local; GitHub PRs only with `--platform github`), posting inline comments and summaries to GitLab MRs and GitHub PRs, and terminal display for local use.

**github-code-review** is the GitHub counterpart, driven by the root `action.yml` and `action/review-pr.sh`. The action checks out the PR head in `.ai-review-head`, outside the mount, and exports its working tree without `.git` to `.ai-review/repo`, next to the merge-base diff and a copy of the plugin. It runs the skill through [agentic-ci](https://github.com/opendatahub-io/agentic-ci) in a Podman container that mounts `.ai-review` at `/workspace`, with the `claude-code` or `codex` harness. The credentials are not action inputs: the job environment carries the keys agentic-ci reads (`ANTHROPIC_API_KEY` or `CLAUDE_CODE_OAUTH_TOKEN` for claude-code, `OPENAI_API_KEY` for codex, or Vertex credentials). Claude Code loads the action's plugin copy; Codex has no plugin flag and loads the skill installed in its runner image. The skill reads the diff and the files and writes the same review JSON to `/workspace/out`. A separate trusted step then validates that one file (no symlinks, at most 1 MiB), copies it to `RUNNER_TEMP` and posts the copy with `review.py post --platform github` and the workflow token. The container never sees the checkout's `.git` or the token. Its isolation is the container plus the separation of the checkout from the mount, not a tool allowlist.

**code-review-panel** runs a risk-tiered panel. `panel.py triage` resolves the diff range, classifies every changed path, sets the tier (quick, standard, deep) and the approval hint, picks the lenses (five core plus detected domains) and their skeptics, and reads REVIEW.md and the repo rules from the base ref. The SKILL.md spawns one finder subagent per lens with that lens's persona, then skeptic subagents for each finding. `panel.py report` maps security rows, assigns stable ids, votes, ranks, and prints the Markdown report; it can also write gitlab-code-review JSON and the known findings file for the next round.

**security-diff-review** has no script. One finder subagent reads `references/finder.md` and `references/filter.md`, then one filter subagent per finding scores it against the exclusion rules and precedents, and the report keeps findings scoring 8 or more. The panel's security lens reads these two references by path and adds its own impact and reproduce checks.

## Conventions

- The skills review committed diffs only (no uncommitted changes)
- `panel.py` uses the Python standard library only, runs with `python3`, and finds its files relative to its own path
- Skill directories hold regular files only: no symlinks, and no SKILL.md below a skill root
- Personas and prompt text live as Markdown under `references/`
- A skill reads a sibling skill's files only when both ship in this plugin, and fails closed when they are missing
- Bump the version in [.claude-plugin/plugin.json](.claude-plugin/plugin.json) on every skill change
- Markdown code blocks for Python snippets and prompt text use `text` fences, not `python`, because ruff formats `python` blocks; shell commands may use `bash`
- Each SKILL.md stays within 12,000 characters and each description within 800
- A change to `panel.py` behavior comes with a test in `tests/`, which checks report.md's and skeptics.md's rules against the script; `make test` runs it on Python 3.10 and later

### gitlab-code-review

- Review output is written to `/tmp/ai-review-output.json` following a strict schema
- The posting script is invoked directly (not via `python`) to use the uv shebang
- `CHILL_MODE` (default: true) filters suggestion-level comments
- `INLINE_FIXES` (default: true) renders optional `fix` objects as applicable GitLab suggestion blocks
- Unresolved AI review discussions without human responses are deleted before posting new ones (GitLab)
- Comments on unchanged code are deduplicated across review iterations

### github-code-review

- `allowed-tools` has no Bash and no Write or Edit, so a local user gets a normal permission prompt. In the action, agentic-ci runs the agent with every tool enabled inside the container, so the container, the checkout kept out of the mount and the post step's validation of the one output file are what hold
- The action never calls `claude` directly: it runs `agentic-ci` through `uvx`, pinned by `agentic_ci_version`
- The output path is an argument. The skill never posts and never runs commands
- Posting is `review.py` by absolute path with `UV_NO_CONFIG=1`, run only by the action's post step, never by the agent
- Every `uses:` in `action.yml` is a full commit SHA

## Change Together

- **Review JSON schema.** The `inline_comments`, `fix` and summary rules are written out in [skills/gitlab-code-review/SKILL.md](skills/gitlab-code-review/SKILL.md) and [skills/github-code-review/SKILL.md](skills/github-code-review/SKILL.md), and enforced by `parse_review_json` in [review.py](skills/gitlab-code-review/scripts/review.py).
- **Agent hardening.** The `agentic-ci run` flags live in [action/review-pr.sh](action/review-pr.sh) (`cmd_review`). The default `agentic_ci_version` lives in [action.yml](action.yml), the inputs table in [README.md](README.md) and `VERIFIED_AGENTIC_CI_VERSION` in [tests/test_github_action.py](tests/test_github_action.py), and a test keeps the three equal. Re-run the flag check against `agentic-ci run --help` and a smoke run before bumping the version. The runner images the empty `image` input resolves to live in [action/review-pr.sh](action/review-pr.sh) (`CLAUDE_RUNNER_IMAGE`, `CODEX_RUNNER_IMAGE`) and the README row: change them together.
- **Poster path.** `skills/gitlab-code-review/scripts/review.py` is shared by the gitlab skill, the github skill, `action/review-pr.sh` and `tests/test_github_action.py`: moving it means changing all four.
- **Cross-skill contracts.** The panel consumes security-diff-review's references: the E and P rule ids in `filter.md`, the output JSON of `finder.md` and the verdict JSON of `filter.md` (the keys of their one JSON example each, listed in `FINDER_KEYS` and `VERDICT_KEYS` in `panel.py`), and the 1 to 10 scale and the keep cutoff in `filter.md` (8), which `panel.py` (`KEEP_CONFIDENCE`) and the security SKILL.md also state. `finder.md` also serves as the security lens persona, so it keeps exactly the four persona headings (`## Stance`, `## Grounding`, `## Checklist`, `## Output`). `panel.py triage` checks the ids, the example keys, the scale and cutoff sentences and the headings, and exits 3 on a mismatch. The panel also produces gitlab-code-review JSON: check it with `review.py display` after changing either side.
- **Shared sentences.** The three sentences (read only, data under review, review settings) are identical in [skills/security-diff-review/SKILL.md](skills/security-diff-review/SKILL.md), [skills/code-review-panel/references/finder-brief.md](skills/code-review-panel/references/finder-brief.md) and [skills/code-review-panel/references/skeptics.md](skills/code-review-panel/references/skeptics.md), each on one unwrapped line.
- **Per-path diffs.** No agent puts a file name with a quote, a backslash, a control or invisible formatting character, or bytes that are not UTF-8 into a per-path diff command: `unusual_name()` in [panel.py](skills/code-review-panel/scripts/panel.py), the specs it writes to `changed.quoted_pathspecs` (also withheld for non-ASCII names, since the plan is ASCII-escaped JSON), [finder-brief.md](skills/code-review-panel/references/finder-brief.md), [skeptics.md](skills/code-review-panel/references/skeptics.md) and the path rule in [skills/security-diff-review/SKILL.md](skills/security-diff-review/SKILL.md).
- **Known findings.** The `still_present` and `fixed` lists and the `open` flag on known items are the same in [finder-brief.md](skills/code-review-panel/references/finder-brief.md) (finder output), SKILL.md step 5 (the merge, which adds each `fixed` entry's lens), [report.md](skills/code-review-panel/references/report.md) and `panel.py`.
- **Renames in GitLab positions.** `panel.py` pairs renames as GitLab's MR diff does (`RENAME_SIMILARITY`, Gitaly's `--find-renames=30%`) to decide which lines take an inline comment, and [review.py](skills/gitlab-code-review/scripts/review.py) resolves `old_path` for renamed files when it posts them: keep both on GitLab's pairing.
- **Base and diff.** The base resolution order, the head default, and the diff command's git options and flag set are the same in [skills/security-diff-review/SKILL.md](skills/security-diff-review/SKILL.md) and [panel.py](skills/code-review-panel/scripts/panel.py).
