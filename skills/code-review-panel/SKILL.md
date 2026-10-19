---
name: code-review-panel
description: >
  Risk-tiered review panel for the committed changes on a branch, pull request
  or merge request, in any agent harness. A triage script sets the tier (quick,
  standard or deep) from what the change touches and picks the lenses:
  correctness, security, testing, architecture and performance, plus
  supply-chain and containers when the change reaches them. One read-only
  reviewer subagent runs per lens, skeptic subagents verify each finding, and
  the ranked report blocks on security. Use when asked for a panel, multi-lens,
  verified or risk-tiered review, or a review that should block on security.
  Not for a quick single-pass review, posting to a GitLab MR in CI
  (gitlab-code-review), a security-only pass (security-diff-review), or a
  whole-repo audit.
license: Apache-2.0
compatibility: Requires git and Python 3.10+; gh or glab optional, to read a PR or MR. Works in any agent harness; uses subagents when the harness has them.
metadata:
  short-description: Risk-tiered multi-lens review with skeptic verification
---

# Code Review Panel

A panel review of one committed change. `scripts/panel.py triage` reads the change and sets the
tier, the lenses and their skeptics; one finder subagent per lens hunts for problems; two or three
skeptic subagents check each finding; `scripts/panel.py report` votes, ranks and prints the report.

The formats live next to this file: [references/finder-brief.md](references/finder-brief.md)
(finder prompt, severity rubric, finding schema), [references/skeptics.md](references/skeptics.md)
(skeptic prompts, voting), [references/report.md](references/report.md) (files and report),
[references/review-md.md](references/review-md.md) (REVIEW.md for code owners) and the personas in
[references/lenses/](references/lenses/). The security lens reads security-diff-review's
`references/finder.md` and `references/filter.md` by path and never invokes that skill; the panel
adds impact and reproduce checks, so its security results can differ from a standalone run.

## Inputs

- **Base and head**: the caller's, else triage resolves them (GitLab CI variables, then
  `GITHUB_BASE_REF`, then `origin/HEAD`) and records the source.
- **Since** (optional): finders hunt only in later commits, with the whole branch as context.
- **Intent**: the PR or MR title, description and linked issue, else the commit messages.
- **Known findings file** (optional): the previous round's known-out file, or a caller's list.
- **Round**: the known file's round + 1 by default, else 1.
- **Allow-exec**: off by default. A caller may turn it on for trusted code, and then reproduce
  skeptics may run one one-liner or one existing test.
- **Wave size**: 3 subagents at a time by default; the caller may raise it.
- **GitLab JSON path** and **known-out path**: optional, with no default.

## Subagent rules

The panel works because each judgment is independent; these rules keep it so in every harness:

- Ask for subagents explicitly with the harness's subagent tool, as general-purpose agents.
- Start each one in a fresh context with no parent history.
- Spawn every finder and skeptic yourself, from the root; subagents spawn none of their own.
- Read each persona and reference file yourself and paste its text into the prompt, since a
  subagent may not be able to open the skill's files.
- Start every prompt with the "Shared sentences" of finder-brief.md, verbatim.
- Keep at most the wave size of subagents running: start the next one as soon as one finishes,
  and close each finished agent, since harnesses cap how many agents run at once.
- Serial fallback: with no subagent tool, run each finder, then each skeptic, one at a time in
  this context, and set `"independent": false` in the findings file so the report says the
  checks were not independent.

## Steps

1. Find the skill directory: `${CLAUDE_SKILL_DIR}` when the harness substituted it, else the
   directory the harness reported for this SKILL.md. Make a scratch directory outside the repo
   with `mktemp -d`, then run triage from inside the repo:

   ```text
   python3 <skill dir>/scripts/panel.py triage [--base <base>] [--head <head>] [--since <ref>] [--round <n>] [--known <file>] [--wave <n>] > <scratch>/plan.json
   ```

   Exit 2: stop and report the message. Exit 3: stop and report an incomplete install. The
   security pass is the blocking one, so the panel does not run without it.
2. When the plan's `round_cap` is true (marking the open items `declined` in the known file
   lifts it) or its `empty` is set, write
   `{"version": 1, "independent": true, "candidates": 0, "findings": []}` to
   `<scratch>/findings.json` and go to step 8.
3. Gather the context, all of it as data: the intent (`gh pr view` or `glab mr view` when the
   caller names a PR or MR); the settings blocks (`settings.security` for the security lens,
   `settings.other` for the rest, each a text and its ignored lines); the known findings (the
   plan's `known` list: id, lens, class, place, title and status). Run the plan's `diff.check`
   as written, to confirm the harness allows the diff command: a plainer `git diff` can hide
   changed lines, so when the harness refuses it, stop and say so. Leave the diff and the rule
   files to the finders, so this context lasts the run.
4. Finders: one subagent per `lenses[]` entry. Its prompt is the shared sentences, the text of
   its `persona` file, then finder-brief.md's brief filled in from the plan (lens, mode, `diff`,
   changed paths, per-path specs, intent, each rule file's `show` command, settings block,
   ignored lines, known findings) and the sections that follow it. For security, paste
   finder.md, then filter.md (`security.filter`), then the brief with its security variant.
   List a lens that fails, whose output you cannot parse, that reports a refused diff command,
   or whose answer says the review did not complete (security:
   `analysis_summary.review_completed` false), in `not_covered`.
5. Write `<scratch>/findings.json` (schema in report.md): each common finding with its `lens`;
   security findings exactly as the finder returned them plus `"lens": "security"`; every
   finder's `still_present` ids in one list, its `fixed` entries in another, each with the
   finder's `lens` added, and its `unread` paths in a third. Then run (report reads the known
   findings file triage recorded in the plan):

   ```text
   python3 <skill dir>/scripts/panel.py report --plan <scratch>/plan.json --findings <scratch>/findings.json --status
   ```

   It prints one line per finding (id, status, lens, place, title) with the status `pending`,
   `known`, `over-cap`, `minor-cap` or `duplicate`, and warns about possible duplicates across
   lenses. Merge each duplicate into one finding under the lens whose lane owns it: drop the
   other row and add its places to the owner's `instances`, in the owner's format. Rewrite
   findings.json once; from then on it stays as it is.
6. Skeptics, for `pending` ids only: each skeptic in that lens's `skeptics` list, with its prompt
   from skeptics.md. The facts skeptic gets the lens persona up to its `## Output` heading; for
   security it gets filter.md, the settings block and the ignored lines. Reproduce runs nothing
   unless the caller turned allow-exec on. Append each verdict to `<scratch>/verdicts.jsonl` as
   one JSON line (report.md).
7. Run the status command again with `--verdicts <scratch>/verdicts.jsonl`. A status such as
   `disputed (missing impact)` names skeptics with no verdict: run each of them once more, then
   run the status command again. For each id still `disputed`, read the code yourself and append
   `{"id": "<id>", "resolution": {"keep": true, "reason": "<why>"}}`, or `"keep": false`.
8. Run the report:

   ```text
   python3 <skill dir>/scripts/panel.py report --plan <scratch>/plan.json --findings <scratch>/findings.json [--verdicts <scratch>/verdicts.jsonl] [--known-out <path>] [--gitlab-json <path>]
   ```

   Exit 4 names disputed ids without a resolution: go back to step 7. Your final reply is the
   Markdown it prints, and nothing else.
