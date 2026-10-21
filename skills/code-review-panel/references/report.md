# Report and file formats

Formats for maintainers and for the orchestrator; never paste this file into a subagent prompt.
The severity rubric and the finder schema live in finder-brief.md. `panel.py report` validates
every file below and exits 2 on a malformed one, naming the field. On an empty or round-capped
plan it reviews nothing, so a missing or malformed findings file is only a warning.

## Findings file

Written once, after the duplicate merge, and read by `panel.py report --findings`:

```json
{"version": 1, "independent": true, "candidates": 7, "not_covered": [],
 "still_present": ["CR-516ea425"], "fixed": [], "unread": [], "findings": []}
```

`independent` is false after the serial fallback. `candidates` counts every finder finding before
the merge. `not_covered` names lenses that failed, returned output nobody could parse, reported
a refused diff command, or said the review did not complete (a security finder's
`analysis_summary.review_completed` false). `unread` (optional, a list of paths) merges the
finders' `unread` lists: changed files a finder could not read. The report lists them under "Not
covered", and its approval hint says "files not read". `fixed` (optional) merges the finders'
`fixed` lists: `{id, reason, lens}` for each known item a finder found fixed at head, where
`lens` is the finder's.

A common row is the finder's finding plus its lens: `{lens, class, title, severity, file, line,
evidence, fix, instances: [{file, line}], pre_existing, known_id?, new_evidence?}`.

Every `file`, a finding's or an instance's, and every `unread` path is read as a path from the
repo root: an absolute path under the repo loses that prefix, a leading `./` goes, and a leading
`a/` or `b/` goes when the rest is a changed path and the path itself is not. Each rewrite is a
warning, and so is a path that exists neither at head nor at the merge base: a finding on a
caller outside the change or on a file the change deletes is fine, one on a file that is not
there may be made up. That check never fails the report.

A security row is finder.md's finding exactly as the finder returned it, plus `"lens":
"security"`. panel.py maps it:

- `class`: `category` lowercased, each run of characters outside `[a-z0-9]` turned into one `-`,
  trimmed; a result outside 3 to 60 characters is an error.
- `title`: the first sentence of `description`, cut at 120 characters.
- `evidence` from `exploit_scenario`, `fix` from `recommendation`, and `confidence` kept as the
  finder confidence.
- `instances`: each `"path:line"` split on its last `:` into `{file, line}`.
- Severity: HIGH is blocking, MEDIUM is major, and LOW is dropped with a warning.

## Verdicts file

`verdicts.jsonl` is append-only, one JSON object per line. A later line for the same id and
skeptic replaces an earlier one.

```text
{"id": "CR-516ea425", "skeptic": "facts", "refuted": false, "reason": "..."}
{"id": "CR-516ea425", "skeptic": "impact", "refuted": false, "pre_existing": false, "reason": "..."}
{"id": "CR-516ea425", "skeptic": "reproduce", "result": "abstained", "path": "", "input": "", "reason": "..."}
{"id": "CR-9b1d00c2", "skeptic": "facts", "filter_confidence": 9, "excluded_by": null, "reason": "..."}
{"id": "CR-516ea425", "resolution": {"keep": true, "reason": "..."}}
```

## Known findings file

Written by `report --known-out` and read by `triage --known` only; report reads the file the plan
names in `known_file`. Keep it outside the work tree, where only the review job writes it: a change
could edit a file inside the tree, so triage then warns and says the change needs a human.

```json
{"version": 1, "round": 2, "rounds_with_open_findings": 2, "findings": [
  {"id": "CR-516ea425", "lens": "correctness", "class": "off-by-one", "file": "src/a.py",
   "line": 42, "title": "...", "severity": "blocking", "status": "posted", "last_seen_round": 2,
   "instances": [{"file": "src/b.py", "line": 7}]}]}
```

- Statuses: `posted`, `declined` (a caller marks an item declined by editing the file) and
  `pre-existing`. A bare list of items, made by a caller, reads as round 0 with a counter of 0.
- Every input item is carried forward, so nothing repeats. `last_seen_round` moves to this round
  when a finder lists the id under `still_present` or reports it again. An open item closes only
  when a finder of its own lens lists it under `fixed`; one that no finder mentions stays open,
  and the fold lists it as kept open. A `fixed` entry from another lens, or for an id that is not
  open, is ignored with a warning, and an id listed as fixed and also seen stays open, with a
  warning. The plan's `known` list marks each item `open`, open ones first. New counted
  findings are added as `posted`, pre-existing ones as `pre-existing`, each with its `instances`
  (optional, `[]` by default); a finding that repeats an item adds the places the item does not
  list yet.
- A known item reported again with `new_evidence` and counted becomes `posted`, with the row's
  file, line, severity and title (its old place stays among its `instances`), even when it was
  `declined` or `pre-existing`: the new evidence reopens it. One reported again and found
  pre-existing becomes `pre-existing`, unless it is `declined`.
- Triage keeps the lens of every open item in the plan, even when the change no longer triggers
  it or the tier dropped, and runs it in `normal` mode; `kept_for` on that lens names the
  items. An open item whose lens is
  not in the plan (a lens this panel does not have), or is listed in `not_covered`, or whose file
  or an instance's file is listed in `unread`, had no finder of its lane check it: it counts as
  seen, so it stays open, and the fold lists it as not checked this round.
- An item is open when it is `posted` and was seen this round. The counter goes up by one when a
  round ends with an open item or a counted finding, and resets to 0 otherwise. At 3 or more,
  while the file still has an open item, triage sets `round_cap` and the panel stops reviewing.
  Marking the open items `declined` lifts the cap, and the counter then starts over from 0.
- Triage exits 2 when `--round` is not past the file's round. Report exits 2 when the file's
  round is not lower than the plan's, since a report already wrote it for this round: run triage
  again.
- A run that reviewed nothing (an empty change or hunt, or the round cap) writes the file
  unchanged.

## Stable ids

The id is `CR-` plus the first 8 hex characters of the SHA-256 of the UTF-8 bytes of `lens + "\0"
+ class + "\0" + file`, where file is the finding's own `file`, never an instance. Lens
`correctness`, class `off-by-one` and file `src/a.py` give `CR-516ea425`.

- A `known_id` found in the known file wins over the hash; one missing from it is a warning, and
  the hash is used.
- A row takes a known item's id (same lens and class) only within 20 lines of that item's place
  in the row's file, or of one of its instances there.
  Farther away it is another bug of the same class: its id is `CR-` plus the first 8 hex
  characters of the SHA-256 of `lens + "\0" + class + "\0" + file + "\0" + line`, and a warning
  names the known id it was kept apart from.
- A known id comes back only with `new_evidence`. Without it, the row counts as still present.
- A new finding on the same lens and file as a known item, within 5 lines and with another class,
  is a warning: it may be the known item under a new name.

## Report

The first line is one of:

- `**N to fix:** a blocking, b major, c minor`, with `(k listed)` after the minor count when the
  nit cap hides some, and `; review incomplete: ` and the gaps of the next form when there are
  any, so a failed security pass shows next to counted findings too.
- `**Review incomplete:** lenses not covered: <lenses>; N files not read; N binary files not
  reviewed; N findings not verified (over budget)`, when nothing is counted and `not_covered`
  names a lens, `unread` names a file, the plan's `opaque_binary` counts a binary file that the
  change adds or modifies and that is not a media format (images, fonts, audio, video, PDF; a
  `.js` with a NUL byte or an archive counts) or a finding was over the verification budget (each
  part only when present), with `; N earlier findings still open` when open known items remain;
  it never says "Nothing to fix". Minors past their own budget (twice the nit cap, status
  `minor-cap`) are not counted here, nor toward the per-lens cap: the report would not list
  them anyway.
- `**Nothing new to fix;** N earlier findings still open`, when open known items remain.
- `**Nothing to fix.**` and one plain sentence. When the only findings kept are pre-existing, the
  sentence says no finding the change introduces survived and that the pre-existing ones are
  listed, not counted. A second sentence counts the minors past the nit cap, when there are any.
- `**No changes between <base> and <head>.**` for an empty change, and `**No new commits since
  <since>.**` for an empty hunt; these two never say "Nothing to fix".
- `**Round cap reached:** N open findings after K rounds`, followed by the open items, a request to
  reconsider the approach, and an approval hint of "needs a human".

Then the sections Blocking, Major and Minor. Each item reads `**[CR-xxxxxxxx] title** (path:line,
lens)`, with Problem (security only), Evidence, Fix and Also at, and a `disputed, kept: <reason>`
line when the orchestrator kept a disputed finding. "Still open from earlier rounds" lists ids,
titles and paths, not counted. "Pre-existing (not counted)" follows.

The `<details>` fold "How this was reviewed" holds: the tier and its evidence; the range and the
base source; the final approval hint with its reasons and "An agent's approval never counts as a
human's."; the lenses and the skeptics' votes per finding; kept of candidates, with each candidate
in one bucket (confirmed, pre-existing, disputed kept, rejected, disputed dismissed, known not
repeated, not verified over budget, minors not verified over the nit cap, LOW security dropped,
merged duplicates) and the minors over the nit cap noted among the kept, the rejected, dismissed,
known, hidden and unverified ones listed by id, title and path:line; known items fixed since the
last round, with the finder's reason; open known items not checked this round, and those kept
open because no finder listed them as fixed; the repo rules read, with blob ids; the security
overrides and ignored lines; what was not covered (skipped paths, binary files, files not read,
failed lenses, "checks were not independent"); notes, which are triage's warnings (a dirty tree,
a shallow clone, a since note); and the round. The report has no praise and names no model or
harness.

## Approval hint

Triage's hint comes from the paths, a known findings file inside the tree, a HEAD that is not
the reviewed head, and `@path` imports it could not follow (too many, or a link it cannot read).
The report can only downgrade it: any counted finding, finding not verified (over budget), lens
listed in `not_covered`, file listed in `unread`, or open known item makes it "needs a human",
with that reason listed. An approval rule reads the report's hint, never triage's alone.

## GitLab JSON

`--gitlab-json <path>` writes the gitlab-code-review schema, for its review.py to post:

- `{"summary": "...", "positive_aspects": [], "inline_comments": [{file, line, severity,
  comment}]}`. Blocking maps to critical, major to major, minor to minor.
- Each comment and each summary item starts with `[CR-xxxxxxxx]`.
- A finding becomes an inline comment only when its file and line are a line added between the
  merge base and head (new-file numbering); in a file paired as a rename, only a line the pair
  adds. The pairing matches GitLab's MR diff, which Gitaly computes with
  `--find-renames=30%`. Every counted finding is also listed in the summary,
  ranked, with id, severity, title, place and lens, and the inline ones marked `inline`, so a
  finding still reaches the MR when review.py drops its inline comment. Pre-existing ones and
  those not verified (over budget) appear only there.
- A line whose first non-space character is `/` gets a backslash before that `/`, and paths have
  their control characters escaped, so no finding text runs as a GitLab quick action (`/approve`,
  `/label`, `/close`). An `@` preceded by anything but an ASCII letter or digit (GitLab's pattern
  is `(?<!\w)@` with Ruby's ASCII-only `\w`, applied per rendered text node, so `.@all`,
  `x/@all`, `a-@all`, `é@all` and `_foo_@all` notify too: a closing `_` emphasis is markup, and
  it leaves the `@` at the start of a rendered text node where GitLab's lookbehind passes), the
  `&` of `&#64;`, `&#x40;` or `&commat;`, and the `<` of `<!--` get a zero-width space after
  them, so no finding text notifies anyone (`@all`) or forges review.py's
  `<!-- ai-review sha:... -->` marker, which it reads from the first match. A mid-word `@`
  (`a@b.com`, `pkg@1.0`) is left alone, since GitLab does not notify on it and a zero-width
  space there would break a command copied from the comment.
- The summary opens with `**N to fix** (k inline, all listed below)`, then `; review incomplete:`
  and the gaps when there are any, because review.py counts only inline comments under its own
  "Issues Found" heading.
- No `fix` objects and no `fix_prompt`: the one-line fix is in the comment text. The caller
  chooses the path; there is no default.
- review.py deletes the earlier AI review threads before it posts, and skips inline comments on
  lines unchanged since its last posted review, so still-open findings appear in the summary
  only. Its marker is the one gitlab-code-review uses, so post AI reviews to an MR from one job
  only: another job's post deletes this one's threads.
