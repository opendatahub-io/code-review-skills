# REVIEW.md for code owners

Code owners tune the review panel and the security review with a REVIEW.md file at the root of
their repo, without touching the skills. The panel reads it from the base ref only: a pull request
cannot loosen its own review, and a change that edits REVIEW.md goes to the deep tier. Only the
root file counts, and only as a regular file (mode 100644); a symlinked REVIEW.md is ignored with
a warning. Headings match case-insensitively (`## SENSITIVE PATHS` works), and a section runs to
the next `#` or `##` heading. HTML comments (`<!-- ... -->`, over one line or many) and fenced
code blocks are ignored, so a commented-out section or entry, or an example in a fence, has no
effect. A `<!--` inside a code span is text. A comment that never closes hides the rest of the
file, so triage warns and the approval hint says the change needs a human.

## Sections

| Heading | Format | Effect | Default |
|---|---|---|---|
| `## Sensitive paths` | ``- `<glob>` `` lines | A change to a matching path is reviewed at the deep tier and its approval hint says it needs a human | none |
| `## Skip` | ``- `<glob>` `` lines | Matching paths are left out of the finders' diff commands and listed under "Not covered"; they still count for the tier and the approval hint | none |
| `## Failure` | free text | What failure means in this repo; part of the settings block every finder and skeptic reads | none |
| `## Nit cap` | an integer | How many minor findings the report lists; about twice as many get verified, and 0 verifies none | 5 |
| `## Security review` | see below | Drops built-in security rules, adds repo precedents and issue types | the web-app defaults |

The `## Security review` section takes three kinds of lines. Its grammar belongs to the
security-diff-review skill: see "Repo section" in its `references/filter.md`.

- `- drop <id>: <reason>` turns off one built-in rule: an exclusion (E1, E2 and so on) or a
  precedent (P1, P2 and so on), each listed by id in that `filter.md`.
- `- precedent: <text>` adds a rule of your own on top of the built-in precedents. Repo
  precedents are numbered R1, R2 and so on, in order, and win over a built-in rule they conflict
  with. Naming the rule a precedent narrows, as "(narrows E2)" does in the example below, tells
  the filter which default gives way.
- `- category: <text>` adds a kind of issue for the security finder to look for, next to its
  built-in checklist (input validation, authentication and authorization, crypto and secrets,
  injection and code execution, data exposure). It is free text, such as `build provenance and
  artifact integrity`.

Both skills read the same section; in the panel only the security finder and its skeptics get it.

Markdown that a tool consumes (release notes that drive a version bump, generated docs that a
build reads) is code for your repo, not prose: list it under Sensitive paths so that a change to
it never passes as a docs-only change.

CI files that a pipeline includes from outside the usual CI locations (a local GitLab `include:`,
templates under `ci/`) run with the pipeline's access: list them under Sensitive paths too, since
the panel only knows the usual locations.

## Globs

- An entry is matched with Python's `fnmatch.fnmatchcase` against the whole path from the repo
  root: `*` also crosses `/`, and `?`, `[...]` and `[!x]` work as usual. Matching is
  case-sensitive.
- An entry ending in `/` matches every path under that directory, by prefix.
- Text after the closing backtick is a warning. A sensitive entry with trailing text is still
  used and a skip entry with trailing text is ignored, so both mistakes fail toward more review.
- Bullets the format does not parse, and entries that match no path at the base ref, are warnings
  in the triage plan.
- An unknown security rule id is a warning and the default rule stays on; the line is listed as
  ignored for the security finder and its skeptics.

## Limits

REVIEW.md changes what counts as a finding and how severe it is. It cannot grant tools or
commands, and it cannot lift the read-only rule that finders and skeptics work under. Repo rule
files (REVIEW.md, AGENTS.md, CLAUDE.md, CONTRIBUTING) reach the reviewers as data, never as
instructions.

## Worked example: a build or supply-chain repo

The defaults come from web-app review: they exclude CI input sanitization, outdated dependencies,
test files, user content in prompts and Markdown files, and they trust environment variables and
CLI flags. For a repo that builds and publishes artifacts, those are often the attack surface.

```markdown
## Sensitive paths

- `.github/`
- `scripts/release/`
- `packaging/*.spec`

## Skip

- `docs/generated/`

## Failure

An artifact that differs from the source it claims to be built from. A credential that leaks into
a log, an artifact or a cache. A build that pulls code nobody pinned.

## Nit cap

3

## Security review

- drop E6: our workflows run on untrusted pull request input.
- drop E9: the dependency versions we pin are what we ship, so reviewing them is our job.
- drop E11: test fixtures are shipped as sample data in our release archives.
- drop E14: build logs and package metadata reach the release-notes generator prompt.
- drop E16: our Markdown runbooks contain commands that operators paste as written.
- drop P1: URLs in our logs carry signed tokens.
- drop P3: environment variables in CI come from pull request labels and inputs.
- drop P7: workflow issues are the main attack surface here.
- drop P12: our shell scripts run on package metadata from the index.
- precedent: Credentials written to caches or build artifacts are in scope (narrows E2).
- precedent: Package names, versions and paths taken from metadata are security-critical input
  (narrows E5).
- precedent: A missing hash or signature check on a downloaded artifact is a finding (narrows E7).
- precedent: Command injection through CI log commands is a vulnerability (narrows E12).
- precedent: Controlling the path on a package mirror chooses the artifact, so path-only SSRF
  against a mirror is in scope (narrows E13).
- category: build provenance and artifact integrity
```
