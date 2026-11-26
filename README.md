# code-review-skills

Agent skills for AI code review of committed changes, packaged as a plugin for Claude Code and Codex. Each skill is a plain skill directory, so harnesses that load skill directories can run them too.

## Overview

| Skill | What it does | Use it when |
|-------|--------------|-------------|
| `gitlab-code-review` | One review pass over a GitLab MR. Produces structured JSON with inline comments, posts it to the MR in GitLab CI, or shows it in the terminal. Supports chill mode and deduplicates comments across review iterations. | You want a GitLab MR reviewed and the comments posted to it. |
| `github-code-review` | One review pass over a GitHub pull request diff that is already on disk. Writes the same structured JSON as `gitlab-code-review` and never posts. The [GitHub Action](#github-action) runs it and posts the result with a separate step. | You want a GitHub PR reviewed with inline comments, suggestions and a summary. |
| `code-review-panel` | A triage script sets the risk tier (quick, standard or deep) from what the change touches and picks the lenses. One reviewer subagent runs per lens, skeptic subagents verify each finding, and the ranked report blocks on security. It can also write `gitlab-code-review` JSON. | A change needs a verified, multi-lens review, or you want a risk tier and an approval hint for it. |
| `security-diff-review` | Security only. One finder subagent looks for exploitable vulnerabilities the change adds, one filter subagent per finding scores it, and only findings that score 8 or more out of 10 are kept. | You want a security pass on a branch, PR or MR. |

All four review committed changes only, not the working tree. The panel and the security review use subagents when the harness has them; without them, the report says the checks were not independent.

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

Or copy all the skill directories into your Codex skills directory:

```bash
mkdir -p "${CODEX_HOME:-$HOME/.codex}/skills"
cp -r /path/to/code-review-skills/skills/* "${CODEX_HOME:-$HOME/.codex}/skills/"
```

Copy them all: the panel reads the security review's references and the GitLab review's org-rules reference, and stops when they are missing.

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
| `ORG_REVIEW_RULES` | Org rules | `false` | Opt in to the org-wide PR review rules: `true` fetches and applies them |
| `GH_TOKEN` | Org rules | -- | Fine-grained read-only token scoped to security-config; local runs use your authenticated `gh` |

The GitLab review can apply the org-wide PR review rules from `opendatahub-io/security-config` (`configs/.coderabbit.yaml`, at `main`). It is opt-in: set `ORG_REVIEW_RULES` to `true` and the skill fetches the file itself with `gh api`, bounded by `timeout 30`, before reviewing, so the source (repo, path and ref) stays fixed in the skill instead of in per-repo CI text. The runner needs `gh` in its image, a `GH_TOKEN` that can read the internal security-config repo, and egress to github.com. Give the job a fine-grained, read-only token scoped to security-config alone: the review agent reads MR text, so the fetch token should hold nothing else. Until agentic-ci forwards `ORG_REVIEW_RULES` and `GH_TOKEN` into the review sandbox, the opt-in works in local runs and in CI jobs whose image passes both through. With the flag on and the fetch unavailable, or when the blob exceeds the 1 MiB guard, the review continues without the org rules and says so in its summary; with the flag off, the default, nothing is fetched and no note is added.

### REVIEW.md

Code owners tune the panel and the security review with a `REVIEW.md` file at the repo root, read from the base ref: sensitive paths, paths to skip, what failure means in the repo, a nit cap, and security rule overrides. See [skills/code-review-panel/references/review-md.md](skills/code-review-panel/references/review-md.md) for the format and a worked example.

## GitHub Action

The repo root is a composite action that reviews a pull request and posts the review: inline comments with applicable suggestions, one summary comment that is updated in place, cleanup of stale AI threads, and no repeats on code that did not change between pushes. The agent runs through [agentic-ci](https://github.com/opendatahub-io/agentic-ci) in an OpenShell sandbox, inside a privileged docker container on the runner (or in a Podman container with `backend: podman`, see [Backends](#backends)), reads code and writes one JSON file. A separate step holds the write token and posts that file through the same `review.py` GitLab uses.

### Setup

Add this workflow to the repository, for example as `.github/workflows/ai-review.yml`. It runs a standard review on every push to a pull request, and a [deep review](#deep-review) while the pull request carries the `deep-review` label:

```yaml
name: AI review
on:
  pull_request:
    # labeled starts the deep review. ready_for_review reviews a draft once it is marked ready.
    types: [opened, synchronize, reopened, ready_for_review, labeled]

permissions:
  contents: read
  pull-requests: write
  # The summary and the reviewer suggestions are issue comments.
  issues: write
  # Lets the standard review find a deep review of the same commit and stand down for it.
  actions: read

jobs:
  review:
    # Every label change starts a run. The standard review ignores them.
    if: github.event.action != 'labeled'
    # A group per job, so the standard and the deep review never cancel each other. A run this
    # job skips gets a group of its own, so it cannot cancel a review in flight either.
    concurrency:
      group: ai-review-${{ github.event.action != 'labeled' && github.event.pull_request.number || format('noop-{0}', github.run_id) }}
      cancel-in-progress: true
    runs-on: ubuntu-latest
    # A composite action cannot set a timeout, so bound the job: 20 agent minutes by default,
    # plus the image pulls and the checkout.
    timeout-minutes: 45
    env:
      # The credentials are not inputs: the action reads them from the job environment.
      OPENAI_API_KEY: ${{ secrets.OPENAI_API_KEY }}
    steps:
      - uses: opendatahub-io/code-review-skills@<full commit sha>
        with:
          recommend_deep_review: 'true'
          # The label in the deep job's if: below, which the recommendation tells people to add.
          deep_review_label: deep-review

  deep-review:
    # The standard review finds this job by its name: keep it equal to deep_review_job.
    name: deep-review
    # When the deep-review label is added, and on later pushes while it is set. Never for a draft:
    # the action fails a deep review of a draft, which would show as a failed job. The label is
    # the standard job's deep_review_label input: keep the two equal.
    if: >-
      contains(github.event.pull_request.labels.*.name, 'deep-review') &&
      !github.event.pull_request.draft &&
      (github.event.action != 'labeled' || github.event.label.name == 'deep-review')
    concurrency:
      group: ai-deep-review-${{ (github.event.action != 'labeled' || github.event.label.name == 'deep-review') && github.event.pull_request.number || format('noop-{0}', github.run_id) }}
      cancel-in-progress: true
    runs-on: ubuntu-latest
    # 75 agent minutes by default in deep mode, plus the image pulls and the checkout.
    timeout-minutes: 120
    env:
      OPENAI_API_KEY: ${{ secrets.OPENAI_API_KEY }}
    steps:
      - uses: opendatahub-io/code-review-skills@<full commit sha>
        with:
          mode: deep
```

Then:

1. Replace `<full commit sha>` with the commit of this repository you want to run. There are no release tags yet, so pin a full commit SHA.
2. Add the model key as a secret of the repository, or as an organization secret that the repository can read: `OPENAI_API_KEY` for the default `codex` harness. See [Credentials](#credentials) for Claude Code and Vertex.
3. Create a `deep-review` label in the repository. Adding it to a pull request starts a deep review, and later pushes run the deep review again while the label is set. Removing it goes back to standard reviews. To use another label, put its name in the deep job's `if:` and `concurrency:` and in the standard job's `deep_review_label` input.

What each part is for:

- **Triggers.** Without `types:`, GitHub runs a `pull_request` workflow only for `opened`, `synchronize` and `reopened`. `ready_for_review` is needed because draft pull requests are skipped with a notice: it reviews the pull request once it is marked ready. `labeled` starts the deep review. Every label change starts a run, which the standard job skips. A label added by a workflow with the `GITHUB_TOKEN` starts no run at all, so a person or a GitHub App token must add `deep-review`. GitHub also starts no `pull_request` run while the pull request has a merge conflict.
- **Permissions.** `pull-requests: write` and `issues: write` post the inline comments, the summary and the reviewer suggestions. `contents: read` covers the checkout. `actions: read` lets the standard review find the deep job of the same commit and stand down for it. Without it, on a private repository, the standard review warns and posts anyway.
- **Concurrency.** The groups are set on the jobs, not the workflow. A new push cancels the outdated review of the same kind, a standard and a deep review never cancel each other, and a run that a job skips gets a `noop` group of its own, so an unrelated label cannot cancel a review in flight. A workflow-level group would: every label change starts a run.
- **The deep job's `if:`.** It skips drafts itself rather than leaving that to the action. In deep mode the action fails on a draft instead of skipping it: a skipped run would end in success, and a later standard review of the same commit would take it for a finished deep review and stand down with nothing posted. A failed deep job, or one skipped by `if:`, never counts, but the `if:` keeps a failed job off the pull request.
- **The deep job's credentials.** Give the deep job the same model credentials as the standard job. A deep job without one fails with an error rather than skipping with a notice, for the same reason as a draft, so a missing secret shows as a failed job instead of passing silently. The standard review of that push can still stand down while the deep job is starting, so fix the secret and push again.
- **The deep job's name and label.** The standard review finds the deep job by its display name, which the `deep_review_job` input gives (`deep-review` by default). Keep the job's `name:` and that input equal. The deep review recommendation in the summary tells people to add the label that the `deep_review_label` input names (`deep-review` by default), so keep it equal to the label in the deep job's `if:`.
- **Timeouts.** A composite action cannot bound itself, so the jobs do: 45 minutes for the standard review (20 agent minutes by default) and 120 for the deep one (75 agent minutes). The rest covers the image pulls, about a minute in practice and at most 15 minutes each, and the checkout. When you raise `timeout_minutes`, raise the job's `timeout-minutes` with it.
- **No checkout step.** The action checks out the pull request head itself.

To run only the standard review, drop the `deep-review` job, `labeled` and `recommend_deep_review`, which leaves `types: [opened, synchronize, reopened, ready_for_review]`. To keep the deep review in a workflow file of its own, give that file the deep job and `types: [labeled, synchronize, reopened, ready_for_review]`, and drop `labeled` and the `if:` from the standard one. Run one standard and at most one deep copy of the action per pull request: two of the same kind fight over the same comments.

### Credentials

The model credentials are not action inputs. The action reads them from the job's `env:`, and the repository that runs the workflow provides them from its own secrets (repository secrets, or organization secrets that the repository can read). Nothing else holds them.

| Harness | Credential in the job's `env:` |
|---------|--------------------------------|
| `codex` (default) | `OPENAI_API_KEY` |
| `claude-code` | `ANTHROPIC_API_KEY`, or `CLAUDE_CODE_OAUTH_TOKEN` (a token from `claude setup-token`), or Vertex credentials (below) |

To run Claude Code instead of Codex, set the harness and its credential in both jobs:

```yaml
    env:
      ANTHROPIC_API_KEY: ${{ secrets.ANTHROPIC_API_KEY }}
    steps:
      - uses: opendatahub-io/code-review-skills@<full commit sha>
        with:
          harness: claude-code
```

For Vertex, set `GCP_SERVICE_ACCOUNT_KEY` (a service account key, inline JSON or base64) or `GCLOUD_CREDENTIALS`, plus `ANTHROPIC_VERTEX_PROJECT_ID` (or `GCP_PROJECT_ID`) and, when it is not `global`, `CLOUD_ML_REGION`. `CLAUDE_CODE_USE_VERTEX=1` on its own also works when application default credentials exist as a file: `GOOGLE_APPLICATION_CREDENTIALS`, or the well-known `application_default_credentials.json` that `gcloud auth application-default login` writes. How each backend gets the credentials:

- `backend: openshell` passes each credential into the container as `-e NAME`, so no value is on a command line. A credential file is not in the container, so the review step reads it on the runner (`GCP_SERVICE_ACCOUNT_KEY` naming a file, `GOOGLE_APPLICATION_CREDENTIALS`, or the well-known file under `CLOUDSDK_CONFIG` or `~/.config/gcloud`, in that order) and passes its content as `GCLOUD_CREDENTIALS`. Service account keys and `gcloud` user credentials work. With `CLAUDE_CODE_USE_VERTEX=1` and no such file, the step fails before pulling anything.
- `backend: podman` runs agentic-ci on the runner, which reads the variables and files itself. Its order differs for application default credentials: openshell gives `GOOGLE_APPLICATION_CREDENTIALS` precedence over the well-known file, while podman reads the well-known file under `~/.config/gcloud` first (it ignores `CLOUDSDK_CONFIG`) and falls back to `GOOGLE_APPLICATION_CREDENTIALS` when that file is missing. On a runner with both, the two backends can pick different credentials.
- Neither backend supports Workload Identity Federation credentials (from `google-github-actions/auth`) or the GCE metadata server, so `GCP_SERVICE_ACCOUNT_KEY` and `GCLOUD_CREDENTIALS` are the portable options. Bedrock is not supported.

A standard review without a credential for its harness exits 0 with a notice and posts nothing. A deep review without one fails, so that the standard review of the same commit does not stand down for it.

Codex has no plugin flag, so it does not load the action's own copy of the skills: it loads `github-code-review` (and `code-review-panel` in deep mode) from the skills-registry copy installed in its image. The image must be a build that already includes `github-code-review`: agentic-ci 0.5.16 or later, which the default images are. An older image cannot find the skill, and the run fails with "produced no output" and a hint that names the `image` input. The image, not the action, decides which skill revision runs. Claude Code loads the skills from the action's own copy, so it does not depend on which skills its image ships.

The steps that run on the runner always use the action's own copy: `review.py` posts, and with `recommend_deep_review` the triage runs the action's `panel.py`. With `harness: codex` the agent's skills (and, in deep mode, the panel's own triage) come from the image instead, so the image and the action's commit should come from the same release of this repository, or the recommendation and the review JSON can come from different skill revisions. The default images are pinned to the agentic-ci release in `agentic_ci_version`, whose images ship the code-review-skills release that this action's commit belongs to; a custom `image` should keep that pairing.

### Inputs

| Input | Default | Description |
|-------|---------|-------------|
| `harness` | `codex` | Agent harness agentic-ci runs: `codex` or `claude-code`. Its credential goes in the job's `env:` (see [Credentials](#credentials)). |
| `model` | harness default | Model for the review, also shown in the summary footer. |
| `agentic_ci_version` | `0.5.16` | Version of `agentic-ci` that runs the agent. With `backend: openshell` it is the tag of `quay.io/aipcc/agentic-ci/openshell`, whose bundled agentic-ci runs, and `<tag>@sha256:<digest>` (64 lowercase hex digits) pins that image by digest; with `backend: podman` it is the package version `uvx` installs, and a digest fails the review step. Every flag the action passes was checked against the default. |
| `backend` | `openshell` | Where agentic-ci runs the agent: `openshell` or `podman` (see [Backends](#backends)). Any other value fails the review step. |
| `image` | image for the harness and backend | Container image the agent runs in. Empty picks the agentic-ci image for the harness: `quay.io/aipcc/agentic-ci/codex-sandbox` or `.../claude-sandbox` with `backend: openshell`, and `.../codex-runner` or `.../claude-runner` with `backend: podman`, each tagged with the `agentic_ci_version` default release. The `*-sandbox` images do not work with Podman: they keep their config under a user that the Podman run does not start the agent as. A tag can be moved, so pin a digest (`...sandbox@sha256:<digest>`) to freeze it. Codex needs an image that ships its skills (see [Credentials](#credentials)). With either backend the action narrows the image's plugin catalog to `code-review-skills`, so a custom image must include that plugin. |
| `mode` | `standard` | `standard` runs one review pass with `github-code-review`. `deep` runs `code-review-panel`, which ignores `review_instructions` (see [Deep review](#deep-review)). |
| `timeout_minutes` | `20` standard, `75` deep | Wall-clock limit for the agent step. The step fails when the review takes longer. Empty picks the default for the mode. Keep the job's `timeout-minutes` above it. |
| `review_instructions` | -- | Extra guidance such as focus areas or team standards, passed to the review as a file. Standard mode only. |
| `org_review_rules` | -- | Path to the org-wide PR review rules file the caller's workflow fetched, such as `opendatahub-io/security-config`'s `configs/.coderabbit.yaml`; absolute, outside the workspace, a regular file of at most 100 KiB. Passed to the review as a file in both modes (applied by the claude-code harness; codex drops them with a warning until its images ship the updated skill). |
| `chill_mode` | `true` | Drop suggestion-level comments. |
| `inline_fixes` | `true` | Post `fix` objects as applicable suggestions. |
| `recommend_deep_review` | `false` | Standard mode only. Before the agent starts, run the panel's triage on the runner and, when it picks the deep tier, add a line to the summary that recommends adding the `deep_review_label` label and names what triggered it (see [The deep review recommendation](#the-deep-review-recommendation)). |
| `deep_review_job` | `deep-review` | Display name of the job that runs the action with `mode: deep`, which a standard review stands down for (see [Deep review](#deep-review)). |
| `deep_review_label` | `deep-review` | The label that starts the deep job, which the recommendation tells people to add. Keep it equal to the label in the deep job's `if:`. Read only with `recommend_deep_review` in standard mode, where a blank value, one over 50 characters or one with a control character fails the job before the review runs. |
| `suggest_reviewers` | `false` | Standard mode only. After the review posts, comment once on the pull request with up to five reviewers (see [Reviewer suggestions](#reviewer-suggestions)). |
| `app_id` | -- | App ID or Client ID of a GitHub App to post as (see [Posting identity](#posting-identity)). Empty posts with the workflow token. |
| `app_private_key` | -- | Private key of that App. Pass it from a secret. |
| `github_token` | -- | A token to post with instead, such as one you minted or a fine-grained personal access token (see [Posting identity](#posting-identity)). |
| `bot_login` | -- | The login `github_token` posts as: `<app-slug>[bot]` for an App token, or the user's login for a personal access token. Read only with `github_token`. A value that is neither fails the job before the review runs. |

`org_review_rules` carries org-wide policy, so the caller's workflow does the authenticated fetch and passes the path of the fetched file: the action needs no token or App installation on the rules repository, and the content never travels through a step environment, which the runner prints into viewable logs. A step can fetch it with `timeout 30 gh api --hostname github.com -H "Accept: application/vnd.github.raw" "repos/opendatahub-io/security-config/contents/configs/.coderabbit.yaml?ref=main" > "$RUNNER_TEMP/org-rules.yaml"`, publish the path as a step output only after a successful fetch, and pass `steps.<id>.outputs.path` to the input, which fixes the rules revision in the caller (the central workflow example in [docs/github-app.md](docs/github-app.md) does exactly that, and warns instead of publishing an over-cap path). A fetch step that warns and continues must leave the input empty: the path is absolute and resolves outside the workspace (the PR checkout lives there), the file must be a regular file of at most 100 KiB, and anything else fails the prepare step with a clear error. Fork pull requests are skipped before any review runs, so they post nothing at all; a same-repository review whose fetch warned, or whose harness does not apply the rules, still posts without them.

### Backends

`backend: openshell`, the default, runs the image `quay.io/aipcc/agentic-ci/openshell:<agentic_ci_version>` as a privileged docker container (`docker run --rm --privileged --init`). The agentic-ci bundled in that image runs `agentic-ci run --backend openshell`: it starts an OpenShell gateway with its own nested Podman, uploads `.ai-review` into the sandbox at `/sandbox/.ai-review`, runs the agent from the `*-sandbox` image and downloads the result back. This is the topology agentic-ci's own `E2E openshell-sandbox` job runs on `ubuntu-latest`: run [41913015162](https://github.com/opendatahub-io/agentic-ci/actions/runs/41913015162/job/126867367300) (job 126867367300) ran Codex and Claude Code through it, as did run 41777652433 on `main` at the same commit. This repository's [E2E workflow](.github/workflows/e2e.yml) runs the action with it and the default images: a standard review on every change to the action or the skills, and a standard and a deep review on every agentic-ci image bump.

- Requirements: docker with privileged containers, as on GitHub-hosted Ubuntu runners. The image (about 300 MB) and the sandbox image (about 700 to 900 MB) are pulled before the agent starts, each under its own 15 minute limit, so neither counts against `timeout_minutes`.
- Pinning: the image runs as root with the model credentials, and a tag can be moved to another image. To freeze it, set `agentic_ci_version` to the tag and its digest, `<tag>@sha256:<digest>`: the action then pulls and runs `quay.io/aipcc/agentic-ci/openshell:<tag>@sha256:<digest>`, for the agent and for the cleanup alike. Pin the sandbox image with the `image` input too.
- Only `.ai-review` is mounted, at its own path, with a docker volume of the run for the nested image storage: never `RUNNER_TEMP`, the checkout or the docker socket. The container gets the harness credentials as `-e NAME`, and never `GITHUB_TOKEN`, `GH_TOKEN` or the OIDC variables.
- agentic-ci's `--timeout` does not bound an OpenShell run, so `timeout_minutes` is enforced from outside: the agent gets SIGINT, then SIGKILL 60 seconds later.
- The download back into `.ai-review` leaves files owned by root, so a short container gives them back to the runner user after every run. The cleanup step removes the run's labelled containers and volume, also after a cancelled run.
- Each run gets a fresh docker volume for the nested image storage, on purpose: concurrent jobs on one runner never share a nested Podman store. So every run pulls the sandbox, supervisor and runtime images again, and the cleanup step deletes them.
- `--privileged` is root on the host. On a self-hosted runner, use it only on runners dedicated to this job, or use Podman, which reuses the runner's image store instead of pulling on every run.

`backend: podman` is the fallback, for runners without privileged docker: agentic-ci runs on the runner through `uvx` and starts a Podman container from the `*-runner` image, with `.ai-review` mounted at `/workspace`. It needs Podman on the runner. The Podman on GitHub-hosted runners can need the crun workaround from agentic-ci's [setup-podman action](https://github.com/opendatahub-io/agentic-ci/blob/main/.github/actions/setup-podman/action.yml). The action does not run `sudo` for you, so add that step to the job when the container fails to start.

### Posting identity

The repository that runs the workflow always provides the token the review posts with. The post step uses the first of these that is set:

1. `github_token`: a token you minted yourself, or a fine-grained personal access token with pull requests and issues write, contents read and actions read. Set `bot_login` to the account it posts as. A personal access token is long-lived and acts as a person, and the action treats any comment by that user that ends with the review marker as its own, so use a dedicated machine account. Only the post step's environment holds the token. Without `bot_login` the post step warns: the review would take `github-actions[bot]` for its own account, so it could not find its summary to update or its old threads to clean up.
2. `app_id` and `app_private_key`, from the repository's (or the organization's) secrets: after the review finishes, the action mints a token for this repository only, with pull requests and issues write, contents read and actions read, and the review appears as `<app-slug>[bot]`. Only the minting step reads the key, and the review step never sees the key or the token. A mint that fails (the App is not installed on the repository, or the key is wrong) fails the job instead of posting as `github-actions[bot]`.
3. The job's `github.token`, the default: reviews appear as `github-actions[bot]`. Nothing to set up.

Set either `github_token` or the App inputs, not both. With both, `github_token` wins and no App token is minted. Comments that `github-actions[bot]` posted before a switch still count as the review's own. The check for a deep review before the agent starts always uses `github.token`, so the job still needs `actions: read`. See [docs/github-app.md](docs/github-app.md) for creating the App, storing its key, and the identity change.

### Many repositories

To maintain the setup in one place, put a reusable workflow in a central repository and give each repository a short caller. Each calling repository still provides its own model key and, optionally, its own App credentials: the central workflow declares them as optional secrets and maps them to the job's environment, and holds none itself. The runs belong to the caller: they show in its Actions tab, use its runner minutes and spend on its key.

The central workflow, for example `my-org/ai-review-workflows/.github/workflows/ai-review.yml`:

```yaml
on:
  workflow_call:
    inputs:
      mode:
        type: string
        default: standard
      # The caller's deep job name: the central job cannot learn it by itself.
      deep_review_job:
        type: string
        default: deep-review
      # The label in the caller's deep job, which the recommendation names.
      deep_review_label:
        type: string
        default: deep-review
    secrets:
      OPENAI_API_KEY:
        required: false
      ANTHROPIC_API_KEY:
        required: false
      AI_REVIEW_APP_ID:
        required: false
      AI_REVIEW_APP_PRIVATE_KEY:
        required: false

jobs:
  review:
    runs-on: ubuntu-latest
    timeout-minutes: ${{ inputs.mode == 'deep' && 120 || 45 }}
    permissions:
      contents: read
      pull-requests: write
      issues: write
      actions: read
    env:
      OPENAI_API_KEY: ${{ secrets.OPENAI_API_KEY }}
      ANTHROPIC_API_KEY: ${{ secrets.ANTHROPIC_API_KEY }}
    steps:
      - uses: opendatahub-io/code-review-skills@<full commit sha>
        with:
          mode: ${{ inputs.mode }}
          deep_review_job: ${{ inputs.deep_review_job }}
          deep_review_label: ${{ inputs.deep_review_label }}
          recommend_deep_review: ${{ inputs.mode == 'standard' }}
          app_id: ${{ secrets.AI_REVIEW_APP_ID }}
          app_private_key: ${{ secrets.AI_REVIEW_APP_PRIVATE_KEY }}
```

The caller in each repository keeps the triggers, permissions, `if:` conditions and concurrency groups of the [Setup](#setup) workflow, and calls the central workflow from both jobs:

```yaml
jobs:
  review:
    if: github.event.action != 'labeled'
    concurrency:
      group: ai-review-${{ github.event.action != 'labeled' && github.event.pull_request.number || format('noop-{0}', github.run_id) }}
      cancel-in-progress: true
    uses: my-org/ai-review-workflows/.github/workflows/ai-review.yml@<full commit sha>
    with:
      deep_review_job: deep-review
      # The label in the deep job's if: below.
      deep_review_label: deep-review
    secrets: inherit

  deep-review:
    if: >-
      contains(github.event.pull_request.labels.*.name, 'deep-review') &&
      !github.event.pull_request.draft &&
      (github.event.action != 'labeled' || github.event.label.name == 'deep-review')
    concurrency:
      group: ai-deep-review-${{ (github.event.action != 'labeled' || github.event.label.name == 'deep-review') && github.event.pull_request.number || format('noop-{0}', github.run_id) }}
      cancel-in-progress: true
    uses: my-org/ai-review-workflows/.github/workflows/ai-review.yml@<full commit sha>
    with:
      mode: deep
      deep_review_job: deep-review
    secrets: inherit
```

- **Passing the secrets.** `secrets: inherit` passes every secret the caller can read, and works only when the caller and the central repository are in the same organization or enterprise. Across organizations, or to pass less, name them: `secrets: { OPENAI_API_KEY: "${{ secrets.OPENAI_API_KEY }}" }`. A secret the caller does not pass is empty, which the action treats as not set.
- **Where the keys live.** A key per repository, as a repository secret, keeps the model spend and its budget per repository, and revoking it affects one repository. One organization secret with access for selected repositories is easier to roll out, but every selected repository then spends from one key and one bill: use a key per team or group of repositories to keep the cost apart.
- **The deep job's name.** GitHub names a called job `<caller job> / <central job>`, so the deep review shows as `deep-review / review`. `deep_review_job` is the caller job's display name, the part before ` / `, and the caller must pass it to the central workflow whenever it differs from the default.
- **Pinning.** Pin the action in the central workflow and the central workflow in each caller by full commit SHA. A branch would let anyone who can push to the central repository run new code with every caller's secrets.

An App webhook relay, where the App's webhook starts runs in a central repository, avoids the caller file but moves every model key and the App key to one place and one bill. [docs/github-app.md](docs/github-app.md) covers both setups in full, with the App's permissions and the identity change.

### Deep review

`mode: deep` runs the [code-review-panel](#overview) skill instead of the single pass: one reviewer per lens plus skeptics that verify each finding. It costs many times a standard review and takes much longer, so the [Setup](#setup) workflow runs it only while the pull request carries the `deep-review` label.

The panel writes the same review JSON, and the post step validates and posts it like a standard review, under the same summary comment and marker, so a deep review and a standard review of the same pull request would replace each other's comments. Instead, the review that should win is the one the pull request shows:

- A standard review stands down when a job named by `deep_review_job` has started for the same commit (queued, waiting or in progress) or has succeeded for it. A deep job that failed, was cancelled or was skipped does not count.
- A deep review stands down when the pull request has a newer commit than the one it reviewed.
- Each review checks before the checkout and the agent, then again before it deletes old threads and before it writes the summary, so one that starts while the other runs still wins. A review that stands down exits 0 with a notice and posts nothing. The spend of a review that stands down after its agent ran is not recovered.

On a push while the label is set, both jobs run in the same workflow run: the standard review sees the deep job queued and stands down before it checks out the pull request, so it spends neither the checkout nor the agent.

The checks fail open. The standard review finds the deep job through the Actions API, so its job needs `actions: read`. When the API refuses the call (for example on a private repository without `actions: read`) or cannot answer, the review warns, names the permission, and goes ahead as if there were no deep review, so it may then replace a deep review's comments. The warning repeats each time the check runs, so a standard review can log it up to three times: before the agent starts, before it deletes old threads and before it writes the summary. The deep review's own check reads only the pull request.

`deep_review_job` is the deep job's display name as GitHub shows it: its `name:` when it sets one, otherwise its job key (`deep-review` in the [Setup](#setup) workflow). With `name: Deep review`, set `deep_review_job: Deep review` in the standard job's `with:`. When the deep job calls a reusable workflow, use the calling job's name: GitHub lists the called jobs as `<caller> / <job>`. A matrix job matches as `<name> (<values>)`. The match is exact and case sensitive, so a name that is only a prefix or a suffix of another job's name does not match it. A wrong name means the standard review never finds the deep one: both post, and whichever finishes last replaces the other.

Deep mode ignores `review_instructions` with a warning, because the panel reads its guidance from `REVIEW.md` at the base branch, so a pull request cannot loosen that base-ref guidance. `org_review_rules` is passed in both modes, standard and deep (applied by the claude-code harness; codex drops them with a warning until its images ship the updated skill): it is caller-supplied like `review_instructions`, so it is as trustworthy as the caller's workflow file, and a same-repo pull request that edits that workflow can change its own input. With `harness: codex`, the panel also comes from the image: `code-review-panel` predates `github-code-review`, so an image that ships the one ships both.

#### The deep review recommendation

With `recommend_deep_review: 'true'`, a standard review runs the panel's triage on the runner before the agent starts, on the agent's copy of the repository, with no token and an empty environment, under a 60 second limit. When the triage picks the deep tier (for changes to CI, agent instructions, installers, hooks, authentication, release or repository control files, or to paths `REVIEW.md` marks sensitive), the summary gets one line under its heading:

```text
**Deep review recommended:** <category>: `<path>` and N more; ... Add the `deep-review` label to this pull request to run it.
```

The line names the label that `deep_review_label` gives, `deep-review` by default. To act on it, add that label. The deep job starts from that `labeled` event, and its review replaces the standard one under the same summary comment. Leave the label off to keep the standard review. The log shows only the tier. A triage that fails or times out only warns, and the summary then has no recommendation. A deep review never recommends one. Keep `deep_review_label` equal to the label in the deep job's `if:`, and leave `recommend_deep_review` off in a repository without a deep job.

### Reviewer suggestions

With `suggest_reviewers: 'true'`, a standard review that posted then comments once on the pull request with up to five reviewers, @mentioned so they are notified. They are the most frequent authors in the base branch's history of the files the pull request changes, leaving out the pull request's author, bots and accounts not linked to a GitHub user. A review that stands down or finds that the pull request moved to a newer commit posts nothing, so it suggests nobody either. A run that finds that comment already posted adds none, so each pull request gets one. A suggestion that fails only warns and never fails the step. It reads the history through the GraphQL API, which needs `contents: read`.

### What the agent sees

The agent runs in a sandbox (OpenShell, or a Podman container with `backend: podman`) with every tool the harness provides enabled, so the sandbox is the boundary, not a tool allowlist.

- It reads an export of the pull request's working tree in `.ai-review/repo`, next to the merge-base diff and a copy of the plugin.
- That export has a git repository of its own, so the agent can run `git log`, `git blame` and `git merge-base`: HEAD detached at the pull request head and the full history of the base and the head, fetched from the checkout's objects without its config, remotes, hooks, alternates or credentials.
- The checkout itself, with its `.git`, sits in `.ai-review-head` and is never mounted, so the container never sees it or the write token. The action never runs git in `.ai-review` once the agent starts.
- The post step reads one file from the mount, refuses a symlink or anything over 1 MiB, and posts a copy of it. Both directories are removed at the end.

### Compatibility notes

Changes that existing workflows notice:

- **Codex is the default harness.** A workflow that sets only `ANTHROPIC_API_KEY` (or another Claude Code credential) now gets a notice that `OPENAI_API_KEY` is not set and posts nothing, until it sets `harness: claude-code`.
- **OpenShell is the default backend.** It needs docker with privileged containers. Runners without it set `backend: podman`.
- **Codex needs agentic-ci 0.5.16 or later.** Older codex images do not ship `github-code-review` (see [Credentials](#credentials)).
- **Draft pull requests are skipped.** Add `ready_for_review` to `types:`, or a draft is never reviewed once it is marked ready. In deep mode a draft, or a missing model credential, fails the job instead: keep drafts out with the deep job's `if:`.
- **Stand-down needs `actions: read`.** Without it, on a private repository, a standard review warns and posts even while a deep review runs.
- **The agent sees git history.** Its copy of the repository now has a credential-free `.git` with the history of the base and the head. The skill contract and the review JSON are unchanged.
- **Default images** are the `*-sandbox` images with `backend: openshell` and the `*-runner` images with `backend: podman`.

### Limitations

- Only the `pull_request` event is supported. Runs for pull requests from forks and runs triggered by Dependabot exit 0 with a notice, because they get no secrets and a read-only token. Do not use `pull_request_target` with a checkout of the PR head: that runs code from the PR with write access and secrets. The action refuses that event.
- The action recognizes its own comments by the login it posts as (see [Posting identity](#posting-identity)) and a marker. Run one standard and at most one deep copy of the action per pull request: two of the same mode would fight over the same comments.
- The action fetches the full history of the PR head (`fetch-depth: 0`) on every run that does not stand down, to find the merge base reliably, and copies the history of the base and the head into the agent's repository. On a very large repo that costs time and disk on every push.
- A very large diff can exceed what the agent reads in one run. The review then says in its summary that it covered only part of the diff.
- The agent has network access and the model credentials, and its tools include Bash, so a prompt-injected agent can use them.
- `backend: openshell` runs a `--privileged` docker container on the runner. On a self-hosted runner that is root on the host: use it only on runners dedicated to this job, or set `backend: podman`.
- The default `image` tag floats (it is rebuilt daily), so a new image can change the review between runs: pin a digest with the `image` input to freeze it.
- With `backend: openshell`, `agentic_ci_version` pins the openshell image by tag unless it carries a digest (see [Backends](#backends)). With `backend: podman`, agentic-ci is pinned by version, not by hash, and its dependencies float.

## Development

### Prerequisites

- Python 3.10+ with [ruff](https://docs.astral.sh/ruff/)
- [shellcheck](https://www.shellcheck.net/)
- [uv](https://docs.astral.sh/uv/) for running skillsaw

### Validate Changes

```bash
make lint
make test
```

See [CONTRIBUTING.md](CONTRIBUTING.md) for the full development workflow.

## Versioning

Use git tags (`v0.1.0`, `v0.2.0`, etc.) for releases. The `main` branch is the development head.

## License

[Apache License 2.0](LICENSE)

The finder and filter references in `skills/security-diff-review/` are adapted from [anthropics/claude-code-security-review](https://github.com/anthropics/claude-code-security-review), Copyright (c) 2025 Anthropic, under the MIT License: see [skills/security-diff-review/LICENSE-MIT](skills/security-diff-review/LICENSE-MIT).
