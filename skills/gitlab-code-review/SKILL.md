---
name: gitlab-code-review
description: >
  Perform AI code review on a GitLab merge request. Reviews all commits since
  the base branch, produces structured JSON feedback with inline comments, and
  posts results to the GitLab MR (in CI) or displays them locally for preview.
  Use when asked to review a GitLab merge request, do a GitLab code review, or
  run ai-review on a GitLab project.
allowed-tools: Bash Read Grep Glob
user-invocable: true
argument-hint: "[additional review instructions]"
compatibility: Requires python3, uv, and git. For CI posting requires GITLAB_API_TOKEN.
metadata:
  author: ODH
  version: "1.0"
  tags: code-review, gitlab, ci, merge-request
---

# GitLab Code Review

Perform a structured code review of the current branch's changes and post
results to a GitLab merge request (in CI) or display them locally for preview.

## Step 1: Review Code Changes

Review ALL commits in the branch since the base branch. Use git commands to
inspect the changes — do NOT access remote APIs (e.g., `glab` commands).

**IMPORTANT**: In CI, always use `$CI_MERGE_REQUEST_DIFF_BASE_SHA` and
`$CI_COMMIT_SHA` to define the diff range. Never use `origin/main..HEAD` in
CI because the checkout may leave HEAD detached at the target branch tip,
producing an empty diff. The CI variables point to the exact commits that
define the MR diff. Fall back to `origin/main..HEAD` only for local runs.

```bash
# Set the diff range — CI variables are authoritative when present
if [ -n "$CI_MERGE_REQUEST_DIFF_BASE_SHA" ] && [ -n "$CI_COMMIT_SHA" ]; then
  BASE="$CI_MERGE_REQUEST_DIFF_BASE_SHA"
  HEAD_REF="$CI_COMMIT_SHA"
else
  BASE="origin/main"  # adjust if default branch differs (e.g., master, develop)
  HEAD_REF="HEAD"
fi
```

Use `$BASE` and `$HEAD_REF` for all git commands:

```bash
git log --oneline ${BASE}..${HEAD_REF}
git diff ${BASE}..${HEAD_REF}
```

**Only review the committed diff between branches.** Do NOT run `git status`,
do NOT report on untracked files, and do NOT include uncommitted working-tree
changes in your review.

### Review Guidelines

$ARGUMENTS

If no additional instructions were provided above, follow these defaults:

Provide constructive feedback that helps maintain code quality and follows
project best practices. Be selective and focused: only comment on issues that
genuinely matter. Do not comment for the sake of commenting. If the code is
well-written and follows best practices, it is perfectly acceptable to return
zero inline comments. Prioritize critical and major issues over minor stylistic
preferences. Avoid repeating the same type of feedback across multiple
locations — one representative comment is sufficient.

When referencing coding standards, security norms, best practices, or
component-specific behavior, search for and cite the authoritative source
(official documentation, RFCs, upstream references) to back up the claim.

## Step 2: Produce Review JSON

Write your review output as a JSON file at `/tmp/ai-review-output.json`.

The JSON **must** be a valid object matching this schema exactly:

```json
{
  "summary": "Brief overall assessment of the changes (2-4 sentences, markdown allowed inside this string)",
  "positive_aspects": ["List of good practices and well-implemented features"],
  "inline_comments": [
    {
      "file": "path/to/file (relative to repo root)",
      "line": 42,
      "severity": "critical|major|minor|suggestion",
      "comment": "Description of the issue and suggested fix (markdown allowed inside this string)",
      "fix": {
        "start_line": 42,
        "end_line": 42,
        "code": "Optional: exact replacement for lines start_line..end_line (whole lines, new file numbering)"
      }
    }
  ],
  "fix_prompt": "Optional: a copy-paste prompt to fix all issues found. Omit this field if there are no actionable fixes."
}
```

### Inline comments rules

- ONLY comment on lines that appear in the diff (changed or added lines).
  Do NOT comment on unchanged lines.
- Use the line number as it appears in the NEW version of the file.
- `file` must be the path relative to the repository root
  (e.g., `src/main.py`, not `/workspace/src/main.py`).
- Severity levels:
  - **critical**: Security vulnerabilities, build-breaking changes
  - **major**: Significant logic errors, pattern violations
  - **minor**: Style issues, minor improvements
  - **suggestion**: Optional improvements for code quality
- Each comment should be self-contained and actionable.
- If there are no inline issues to report, use an empty array `[]`.

### Applicable fix rules (`fix`)

When a finding has a small, unambiguous code fix, attach a `fix` object. The
posting script turns it into a GitLab
[suggestion](https://docs.gitlab.com/user/project/merge_requests/reviews/suggestions/)
that the MR author applies with one click, so the replacement must be exact.

- `fix` is optional. Omit it for design-level findings, findings that need
  changes in several places, or whenever you are not certain the replacement
  is complete and correct in context.
- `start_line` and `end_line` are inclusive line numbers in the NEW version of
  the file, and `line` must fall inside that range. Use `start_line ==
  end_line == line` for a one-line replacement.
- `code` replaces the whole lines `start_line..end_line`. Copy the exact
  indentation (tabs vs spaces) from the file. Do not include diff markers
  (`+`/`-`), line numbers, or a code fence. Do not include a trailing newline.
- An empty `code` string deletes the range.
- Keep it small: a few lines, never more than about 20. The script drops
  ranges above 50 lines, ranges outside the file, and replacements identical
  to the current code, and posts the comment as prose instead.
- The `comment` text must still explain the change; the suggestion block is
  appended after it, not instead of it.

Example: the diff adds an `else:` at line 74 that should only run when the
wheel is absent from the target set.

```json
{
  "file": "src/pulp_qualify_wheels.py",
  "line": 74,
  "severity": "major",
  "comment": "Only record a QA failure when the wheel is also absent from production; otherwise an already-published dependency blocks its promotion unit.",
  "fix": {
    "start_line": 74,
    "end_line": 74,
    "code": "            elif wheel not in target_wheel_set:"
  }
}
```

### Summary rules

- Keep it short (2-4 sentences). The inline comments carry the detail.
- Mention the overall quality and any critical concerns.

### Positive aspects rules

- List 1-3 things done well. If nothing stands out, use an empty array `[]`.

### Fix prompt rules

- Omit this field entirely if there are no actionable fixes.

## Step 3: Post Results

Run the `review.py` script from this skill's `scripts/` directory.
Execute it directly (not via `python`) to invoke uv via the shebang:

```bash
./scripts/review.py post /tmp/ai-review-output.json
```

The script auto-detects the platform (GitLab CI, GitHub, or local) and handles:

- JSON validation and chill-mode filtering (controlled by `$CHILL_MODE` env var)
- Rendering `fix` objects as applicable GitLab suggestions (disabled when
  `$INLINE_FIXES` is `false`)
- Deduplication against previous reviews (skips comments on unchanged code)
- Deleting previous AI review discussions on the MR (GitLab)
- Posting inline comments and a summary note to the MR (GitLab)
- Falling back to formatted terminal display when no CI platform is detected

If the script reports a JSON parse error, fix the JSON in
`/tmp/ai-review-output.json` and re-run the command.

## Step 3.5: Suggest Reviewers (Optional)

If the `$SUGGEST_REVIEWERS` environment variable is set to `"true"`, run:

```bash
./scripts/review.py suggest-reviewers
```

This identifies potential reviewers based on git history of the modified files
and posts a separate comment on the MR tagging them. It uses the GitLab GraphQL
API to resolve git commit authors to GitLab usernames.

If a `.git-blame-ignore-revs` file exists at the repo root, commits listed in
it are excluded from authorship counts, the same way `git blame
--ignore-revs-file` would treat them.

If the script fails, report the error but continue to Step 4.

## Step 4: Report Results

After the script completes successfully:

- **CI**: Confirm the review was posted to the merge request
- **Local**: The script displays results directly in the terminal
- **Errors**: Report any failures from the script output

## Gotchas

- Line numbers in `inline_comments` must reference the NEW file version, not the old one; using old-side line numbers causes comments to land on the wrong line in GitLab.
- A `fix` replaces whole lines. Partial-line edits, wrong indentation, or a range that does not contain `line` produce a suggestion that breaks the file when applied, or get dropped by the script.
- The JSON output must be strict JSON (no trailing commas, no comments). Invalid JSON will cause the posting script to fail.
- Running `git status` or reviewing uncommitted changes will produce false findings that are not part of the MR diff.

## Environment Variables

The Python script reads these from the environment. In GitLab CI, most are
set automatically — no manual configuration needed.

### GitLab

| Variable | Required for | Default | Description |
|----------|-------------|---------|-------------|
| `GITLAB_API_TOKEN` | CI/MR | — | GitLab Personal Access Token |
| `CI_SERVER_URL` | — | `https://gitlab.com` | GitLab server URL |
| `CI_PROJECT_ID` | CI/MR | — | GitLab project ID |
| `CI_MERGE_REQUEST_IID` | CI/MR | — | Merge request IID |
| `CI_MERGE_REQUEST_DIFF_BASE_SHA` | CI/MR | — | Base SHA for diff positioning |
| `CI_COMMIT_SHA` | CI/MR | — | Head commit SHA |
| `CI_PROJECT_PATH` | suggest-reviewers | — | Full project path for GraphQL queries |
| `CI_JOB_NAME` | — | `ai-review` | Job name for summary footer |
| `CI_JOB_URL` | — | `#` | Job URL for summary footer |
| `AGENT_MODEL` | — | — | Model shown in the summary footer (set by agentic-ci) |
| `AGENT_REASONING_EFFORT` | — | — | Reasoning effort shown in the summary footer (set by agentic-ci) |

### Common

| Variable | Required for | Default | Description |
|----------|-------------|---------|-------------|
| `CHILL_MODE` | — | `true` | Filter out suggestion-level comments |
| `INLINE_FIXES` | — | `true` | Render `fix` objects as applicable GitLab suggestions |
| `VERBOSE` | — | `false` | Show detailed API error responses |
| `SUGGEST_REVIEWERS` | — | `false` | Suggest reviewers based on git history |
