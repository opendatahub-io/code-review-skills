# code-review-skills

Plugin of agent skills for AI code review: a GitLab MR reviewer, a GitHub PR reviewer with a composite GitHub Action, a risk-tiered review panel and a security diff review, for Claude Code, Codex and other harnesses.

## Repository Structure

```text
.claude-plugin/plugin.json   Plugin packaging metadata
action.yml                   Composite GitHub Action that reviews a pull request
action/review-pr.sh          The action's steps (check, prepare, standdown, review, post, cleanup)
docs/github-app.md           Guide to posting as a GitHub App and running reviews from a reusable workflow
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

**github-code-review** is the GitHub counterpart, driven by the root `action.yml` and `action/review-pr.sh`. The README's GitHub Action section is the consumer guide and [docs/github-app.md](docs/github-app.md) the App and reusable workflow guide.

- **Steps.** `check` (no token) skips forks, Dependabot, drafts and runs without a harness credential, and validates the mode, `bot_login` and, with the recommendation, `deep_review_label`. `standdown` comes next, in `RUNNER_TEMP` and before the checkout, and holds `github.token` only to read; every later step but `cleanup` is skipped when it stands down. The checkout of the PR head goes to `.ai-review-head`, outside the mount, without persisted credentials. `prepare` (no token) writes the merge-base diff and exports the working tree to `.ai-review/repo`, next to a copy of the plugin. `review` runs the agent with every token and OIDC variable blanked. The optional `app-token` step mints the posting token. `post` validates the one output file (no symlinks, at most 1 MiB), copies it to `RUNNER_TEMP/ai-review-post.*` and posts the copy with `review.py post --platform github`. `cleanup` always runs and removes the workdirs, the triage plan and the `ai-review-tmp.*` and `ai-review-post.*` directories.
- **Container repo.** `prepare` gives the export a fresh `.git` in both modes: an empty-template `git init`, the full history of the base and the head fetched from the checkout under a clean git config, HEAD detached at the head, and no remote, credentials, hooks, alternates, reflogs or host paths. The container never sees the checkout's `.git` or a token, and the host never runs git in `.ai-review` after the review step starts, because the agent can rewrite that `.git`.
- **Backends.** `backend: openshell` (the default) runs `quay.io/aipcc/agentic-ci/openshell:<agentic_ci_version>`, where the version may be `<tag>@sha256:<digest>` to pin the image (`openshell_image_ref` builds the reference for the agent run and the cleanup alike), as a `docker run --rm --privileged --init` container that mounts only `.ai-review` and a per-run storage volume. Its bundled agentic-ci runs the agent in an OpenShell sandbox from the `*-sandbox` image, at `/sandbox/.ai-review`. The image pulls are bounded on their own, the agent gets an outer `timeout -s INT` (agentic-ci's `--timeout` does not bound OpenShell), a short container gives root-owned files back to the runner user, and `cleanup` removes containers and volumes by the `ai-review.workdir` label. The storage volume is per run on purpose, so concurrent jobs never share a nested Podman store, and every run pulls the sandbox images. `backend: podman` runs agentic-ci on the runner through `uvx` with the `*-runner` image, mounted at `/workspace`; the sandbox images do not work under Podman. Credentials reach the agent as `-e NAME` from an allowlist per harness (`OPENAI_API_KEY` for codex; `ANTHROPIC_API_KEY`, `CLAUDE_CODE_OAUTH_TOKEN` or Vertex for claude-code), never as inputs. Both backends set `AGENT_ENABLED_PLUGINS=code-review-skills`: openshell passes it with `-e`, and podman's `uvx agentic-ci` inherits it.
- **Harnesses.** Codex is the default. It has no plugin flag and loads the skills from the skills-registry copy in its image, so the image decides the skill revision. Claude Code loads the action's plugin copy with `--plugin-dir`. The runner side always uses the action's copy: `review.py` posts, and the recommendation's triage runs the action's `panel.py`. So with codex the image tag must match the action's release, or the triage, the review JSON contract and the skills can drift apart; the default images float until an agentic-ci release that ships these skills is pinned.
- **Modes.** `mode: standard` runs this skill. `mode: deep` runs code-review-panel, which writes gitlab-code-review JSON to the same output file with `--gitlab-json`; review instructions are not passed. Both post under the same summary marker.
- **Stand-down.** `review.py stand-down` runs in the `standdown` step before the agent, and `post --mode` repeats the check before it deletes old threads and before it writes the summary. A standard review stands down when a job matching `deep_review_job` (exact name, `<name> / ...` under a reusable workflow, or `<name> (...)` for a matrix) has started or succeeded for the same head; a deep review stands down when the PR head moved. It reads the Actions API, so it needs `actions: read`, and it fails open with one warning.
- **Recommendation.** With `recommend_deep_review`, `prepare` runs `panel.py triage` on the agent's copy before any agent exists, under `env -i`, `python3 -I` and a 60 second timeout, and `post` passes the plan to `review.py`, which adds one line under the summary heading when the tier is deep.
- **Suggestions.** With `suggest_reviewers`, `post` runs `review.py suggest-reviewers --platform github` after a standard review posted. It comments once with up to five @mentions from the base branch's GraphQL history of the changed files. A failure only warns.
- **Identity.** The post token is `github_token`, else the token the `app-token` step mints from `app_id` and `app_private_key` (scoped to the repository, revoked at job end), else `github.token`. `AI_REVIEW_BOT_LOGIN` tells `review.py` the posting login; comments by `github-actions[bot]` with the marker still count as ours, and the summary PATCH only targets the current identity's summary.

Its isolation is the sandbox plus the separation of the checkout from the mount, not a tool allowlist.

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
- The action never calls `claude` or `codex` directly: it runs `agentic-ci`, the one bundled in the openshell image tagged `agentic_ci_version`, or with Podman the same version through `uvx`
- The output path is an argument. The skill never posts, and in the action it runs no commands
- Posting is `review.py` by absolute path with `UV_NO_CONFIG=1`, run only by the action's post step, never by the agent
- Every `uses:` in `action.yml` is a full commit SHA
- Every new step keeps the `    - name:` / `if:` / `VAR: ${{ expr }}` layout the static tests parse, and no `run:` holds `${{ }}`

## Change Together

- **Review JSON schema.** The `inline_comments`, `fix` and summary rules are written out in [skills/gitlab-code-review/SKILL.md](skills/gitlab-code-review/SKILL.md) and [skills/github-code-review/SKILL.md](skills/github-code-review/SKILL.md), and enforced by `parse_review_json` in [review.py](skills/gitlab-code-review/scripts/review.py).
- **Agent version and images.** The `agentic-ci run` flags live in [action/review-pr.sh](action/review-pr.sh) (`cmd_review`, `run_openshell_agent`, `run_podman_agent`). The default `agentic_ci_version` lives in [action.yml](action.yml), the inputs table in [README.md](README.md) and `VERIFIED_AGENTIC_CI_VERSION` in [tests/test_github_action.py](tests/test_github_action.py), and a test keeps the three equal. The same version is the tag of the openshell image (`OPENSHELL_IMAGE_REPO`), so the image and the agentic-ci it bundles move together, and the backend evidence in the README names that tag. The images the empty `image` input resolves to live in [action/review-pr.sh](action/review-pr.sh) (`CLAUDE_SANDBOX_IMAGE`, `CODEX_SANDBOX_IMAGE` for openshell, `CLAUDE_RUNNER_IMAGE`, `CODEX_RUNNER_IMAGE` for Podman), the README `image` row and the image tests: change them together, and pin them to the same agentic-ci release once it ships the GitHub skills. Re-run the flag check against `agentic-ci run --help` and a smoke run before bumping the version. The minimum image for Codex is stated in the README and in the `compatibility` line of [skills/github-code-review/SKILL.md](skills/github-code-review/SKILL.md).
- **Triage file.** `prepare` in [action/review-pr.sh](action/review-pr.sh) (`run_triage`) writes the raw stdout of `panel.py triage` to `$RUNNER_TEMP/ai-review-triage/plan.json` (a mode 700 directory, at most 1 MiB), `cmd_post` passes it as `--deep-recommendation-plan` only in standard mode, with `recommend_deep_review` on and a regular non-symlink file, and `cleanup` removes it. [review.py](skills/gitlab-code-review/scripts/review.py) reads only `tier.name` and `tier.evidence` from it, so a change to the triage plan's tier keys in [panel.py](skills/code-review-panel/scripts/panel.py) changes all three.
- **Deep job name and label.** The `deep_review_job` default in [action.yml](action.yml), `DEFAULT_DEEP_JOB` in [action/review-pr.sh](action/review-pr.sh), the deep job's key and `name:` in the README Setup workflow and the callers in the README and [docs/github-app.md](docs/github-app.md) are all `deep-review`. The label in those jobs' `if:` is `deep-review`, the default of the `deep_review_label` input and of `DEFAULT_DEEP_LABEL`, which `post` passes to review.py as `--deep-label` for the recommendation line; `check` applies review.py's limits to it early (not blank, at most 50 characters, no control characters). The README-coupled tests in `tests/test_github_action_deep.py` check the sample job. Rename them together, or the standard review stops finding the deep one.
- **Token allow-list.** Only the `standdown` step (`github.token`, reads only) and the `post` step (`github_token`, else the minted App token, else `github.token`) receive `GITHUB_TOKEN`; the review step blanks `GITHUB_TOKEN`, `GH_TOKEN` and the OIDC variables; `app_id` and `app_private_key` appear only in the `app-token` step's `with:`; `github_token`, `bot_login` and the mint's outputs only in the post step's `env:`. [tests/test_github_action.py](tests/test_github_action.py) enforces each of these, and the App guide's permission table and the `permission-*` lines of the mint step list the same four permissions.
- **Poster path.** `skills/gitlab-code-review/scripts/review.py` is shared by the gitlab skill, the github skill, `action/review-pr.sh` and `tests/test_github_action.py`: moving it means changing all four.
- **Cross-skill contracts.** The panel consumes security-diff-review's references: the E and P rule ids in `filter.md`, the output JSON of `finder.md` and the verdict JSON of `filter.md` (the keys of their one JSON example each, listed in `FINDER_KEYS` and `VERDICT_KEYS` in `panel.py`), and the 1 to 10 scale and the keep cutoff in `filter.md` (8), which `panel.py` (`KEEP_CONFIDENCE`) and the security SKILL.md also state. `finder.md` also serves as the security lens persona, so it keeps exactly the four persona headings (`## Stance`, `## Grounding`, `## Checklist`, `## Output`). `panel.py triage` checks the ids, the example keys, the scale and cutoff sentences and the headings, and exits 3 on a mismatch. The panel also produces gitlab-code-review JSON: check it with `review.py display` after changing either side.
- **Shared sentences.** The three sentences (read only, data under review, review settings) are identical in [skills/security-diff-review/SKILL.md](skills/security-diff-review/SKILL.md), [skills/code-review-panel/references/finder-brief.md](skills/code-review-panel/references/finder-brief.md) and [skills/code-review-panel/references/skeptics.md](skills/code-review-panel/references/skeptics.md), each on one unwrapped line.
- **Per-path diffs.** No agent puts a file name with a quote, a backslash, a control or invisible formatting character, or bytes that are not UTF-8 into a per-path diff command: `unusual_name()` in [panel.py](skills/code-review-panel/scripts/panel.py), the specs it writes to `changed.quoted_pathspecs` (also withheld for non-ASCII names, since the plan is ASCII-escaped JSON), [finder-brief.md](skills/code-review-panel/references/finder-brief.md), [skeptics.md](skills/code-review-panel/references/skeptics.md) and the path rule in [skills/security-diff-review/SKILL.md](skills/security-diff-review/SKILL.md).
- **Known findings.** The `still_present` and `fixed` lists and the `open` flag on known items are the same in [finder-brief.md](skills/code-review-panel/references/finder-brief.md) (finder output), SKILL.md step 5 (the merge, which adds each `fixed` entry's lens), [report.md](skills/code-review-panel/references/report.md) and `panel.py`.
- **Renames in GitLab positions.** `panel.py` pairs renames as GitLab's MR diff does (`RENAME_SIMILARITY`, Gitaly's `--find-renames=30%`) to decide which lines take an inline comment, and [review.py](skills/gitlab-code-review/scripts/review.py) resolves `old_path` for renamed files when it posts them: keep both on GitLab's pairing.
- **Base and diff.** The base resolution order, the head default, and the diff command's git options and flag set are the same in [skills/security-diff-review/SKILL.md](skills/security-diff-review/SKILL.md) and [panel.py](skills/code-review-panel/scripts/panel.py).
