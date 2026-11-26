# Running reviews centrally with a GitHub App

This guide is for an organization that wants one place to maintain the AI review setup for many repositories, and wants the reviews to post under its own GitHub App instead of `github-actions[bot]`. It builds on the [GitHub Action](../README.md#github-action) section of the README, which covers the action itself, its [inputs](../README.md#inputs) and its limitations, and starts from the single-repository [setup](../README.md#setup).

The recommended setup is an organization reusable workflow with a thin caller in each repository:

- A central repository, for example `my-org/ai-review-workflows`, holds one reusable workflow that runs the action.
- Each repository that wants reviews adds a short workflow that calls it, once for the standard review and once for the deep review.
- Each calling repository provides its own credentials: the model key (`OPENAI_API_KEY` and so on) and, optionally, the App credentials. The central repository holds no secrets.
- The runs belong to the calling repository. They show in its Actions tab, use its runner minutes, and spend on its model key.

The App needs no webhook in this setup. It is only an identity that the action mints a short-lived token for, right before it posts.

## Choosing the posting token

The action posts with one of three tokens. The first one that is set wins:

| Option | Inputs | Comments appear as | Use it when |
|--------|--------|--------------------|-------------|
| A caller-provided token | `github_token`, plus `bot_login` | The user or App the token belongs to | You already mint a token yourself, or you accept a fine-grained personal access token (see the caveats below). |
| App credentials | `app_id`, `app_private_key` | `<app-slug>[bot]` | You want a dedicated review identity. This guide is about this option. |
| The workflow token | none | `github-actions[bot]` | The default. Nothing to set up. |

Set only one of `github_token` and the App inputs. With neither set, the action uses the job's `github.token`, as it always has.

Caveats for a personal access token in `github_token`:

- It is long-lived. A leaked token works until someone revokes it, while an App token expires within an hour and the action revokes it at the end of the job.
- It acts as a person. Review comments, notifications and audit log entries show that user, and the review stops working when the user leaves.
- Set `bot_login` to that user's login. The action then treats any comment by that user that ends with the review marker as its own, so it can edit or delete it. Use a dedicated machine account, not someone's personal account.
- Give a fine-grained token access to the reviewed repositories only, with Pull requests read and write, Issues read and write, Contents read and Actions read.

In the reusable workflow setup, a token minted by an earlier job in the caller cannot be handed to the central workflow (a job output that holds a secret is dropped), so prefer the App inputs there and let the action mint the token.

## Creating the App

1. In the organization settings, open **Developer settings**, then **GitHub Apps**, then **New GitHub App**.
2. Pick the name with care. Its slug (lowercase, spaces turned into hyphens) becomes the comment author, `<app-slug>[bot]`, on every review.
3. Set any homepage URL, for example the central repository.
4. Under **Webhook**, clear **Active**. The App only authenticates, so it receives no events.
5. Set the repository permissions below, and leave every organization and account permission at **No access**.
6. Under **Where can this GitHub App be installed?**, choose **Only on this account**.
7. Create the App and note its **App ID** (the **Client ID** works too).
8. Install it on the organization with **Only select repositories**, and add each repository that should be reviewed under the App's name.

| Repository permission | Access | Why |
|-----------------------|--------|-----|
| Pull requests | Read and write | Inline review comments with suggestions, and cleanup of stale review threads. |
| Issues | Read and write | The summary comment and the suggested-reviewers comment are issue comments on the pull request. |
| Contents | Read | The file history that the reviewer suggestions read. The checkout of the pull request itself uses the workflow token. |
| Actions | Read | The check, right before posting, for a deep review that should win over this one. |
| Metadata | Read | Required by GitHub for every App. |

The installation decides which repositories the App's key can ever reach. Keep it to the repositories that run reviews.

### Storing the private key

On the App's settings page, under **Private keys**, select **Generate a private key**. GitHub downloads a `.pem` file. Store its whole contents, header and footer lines included, as an Actions secret, then delete the downloaded file. Never put it in a repository or in a configuration variable.

There are two places to keep it:

- **A repository secret in each repository.** Each repository owner adds `AI_REVIEW_APP_PRIVATE_KEY` and `AI_REVIEW_APP_ID` to their own repository. This leaves the choice with each team, at the cost of copying the key around.
- **An organization secret with selected repository access.** An organization owner adds both secrets once, under **Repository access: Selected repositories**, and adds each repository as it adopts the review.

Either way, treat the key as the App itself. Anyone who can run a workflow in a repository that can read the secret can mint a token for any repository where the App is installed, with every permission the App has. The action narrows its own token to one repository, but a hand-written workflow does not have to. If the installation spans repositories with different owners or trust levels, create one App per group instead of sharing one key.

An App can hold more than one private key at a time. To rotate, generate a new key, update the secret, confirm a review posts, then delete the old key.

### What the action does with it

When `app_id` is set, the action mints the token in its `app-token` step, which runs after the review has finished and directly before the post step:

- It uses `actions/create-github-app-token@f418cff399f7aef4c2f0ad47e25ca7a6abbdd6fc` (v3.4.0), with `app_id` passed as its `client-id` and `app_private_key` as its `private-key`. The private key is referenced in that step's `with:` and nowhere else.
- The token is scoped to the reviewed repository only, with Pull requests write, Issues write, Contents read and Actions read.
- The token is masked in the log and is not exposed as an output of the action. It is revoked when the job ends, and it would expire within an hour anyway.
- The step that runs the review never sees the App key or the token. Neither do the earlier steps: the check for a deep review before the review starts uses the workflow token, which is why the job still needs `actions: read`.
- The mint is not allowed to fail quietly. If the App is not installed on the repository, or the key is wrong, the job fails instead of posting as `github-actions[bot]`.
- The bot login is taken from the mint's `app-slug` output on every run, so there is nothing to configure when the App is renamed.

Set both `app_id` and `app_private_key`, or neither: a set `app_id` without a key fails the mint. When `app_id` is empty, the mint step does not run and the post step falls back to `github.token`. A repository without the App secrets therefore still gets reviews, as `github-actions[bot]`.

## The central reusable workflow

Put this in the central repository, for example as `.github/workflows/ai-review.yml`. If that repository is private or internal, allow other repositories to use it under **Settings**, **Actions**, **General**, **Access**.

```yaml
name: AI review
on:
  workflow_call:
    inputs:
      mode:
        type: string
        default: standard
      # The display name of the caller job that runs the deep review. The standard review
      # looks for that job before it starts and again before it posts.
      deep_review_job:
        type: string
        default: deep-review
      harness:
        type: string
        default: codex
      model:
        type: string
        default: ''
      review_instructions:
        type: string
        default: ''
      recommend_deep_review:
        type: boolean
        default: false
      suggest_reviewers:
        type: boolean
        default: false
    # Every secret is optional and comes from the calling repository.
    secrets:
      OPENAI_API_KEY:
        required: false
      ANTHROPIC_API_KEY:
        required: false
      GCP_SERVICE_ACCOUNT_KEY:
        required: false
      AI_REVIEW_APP_ID:
        required: false
      AI_REVIEW_APP_PRIVATE_KEY:
        required: false

jobs:
  review:
    runs-on: ubuntu-latest
    # A composite action cannot set a timeout. The deep review allows 75 agent minutes,
    # the standard one 20, and both need time for the image pull and the checkout.
    timeout-minutes: ${{ inputs.mode == 'deep' && 120 || 45 }}
    # The caller must grant at least these, or the run does not start.
    permissions:
      contents: read
      pull-requests: write
      issues: write
      actions: read
    # The model credentials are read from the job environment, not from inputs. A secret the
    # caller did not pass is an empty variable, which the action treats as not set.
    env:
      OPENAI_API_KEY: ${{ secrets.OPENAI_API_KEY }}
      ANTHROPIC_API_KEY: ${{ secrets.ANTHROPIC_API_KEY }}
      GCP_SERVICE_ACCOUNT_KEY: ${{ secrets.GCP_SERVICE_ACCOUNT_KEY }}
    steps:
      # Replace with the full commit SHA of the code-review-skills version you reviewed.
      - uses: opendatahub-io/code-review-skills@0000000000000000000000000000000000000000
        with:
          mode: ${{ inputs.mode }}
          deep_review_job: ${{ inputs.deep_review_job }}
          harness: ${{ inputs.harness }}
          model: ${{ inputs.model }}
          review_instructions: ${{ inputs.review_instructions }}
          recommend_deep_review: ${{ inputs.recommend_deep_review }}
          suggest_reviewers: ${{ inputs.suggest_reviewers }}
          app_id: ${{ secrets.AI_REVIEW_APP_ID }}
          app_private_key: ${{ secrets.AI_REVIEW_APP_PRIVATE_KEY }}
```

Add any other credential the harness needs (for example `CLAUDE_CODE_OAUTH_TOKEN`, or `ANTHROPIC_VERTEX_PROJECT_ID` for Vertex) the same way: an optional secret, mapped into `env:`.

Notes:

- A called workflow sees the caller's event, so `github.event_name` is still `pull_request` and the action works unchanged.
- `runs-on: ubuntu-latest` suits the default `openshell` backend, which starts a privileged Docker container. On a self-hosted runner that container is root on the host. See [Backends](../README.md#backends) in the README for the backend choice and the `podman` fallback.
- Pin the action by full commit SHA here, and have callers pin this workflow by full commit SHA too. A branch reference would let anyone who can push to the central repository run new code with every caller's secrets.

## The caller in each repository

```yaml
name: AI review
on:
  pull_request:
    # labeled starts the deep review; ready_for_review reviews a draft once it is marked ready.
    types: [opened, synchronize, reopened, ready_for_review, labeled]

# Reusable workflows can only narrow these, so grant everything the central job asks for.
permissions:
  contents: read
  pull-requests: write
  issues: write
  actions: read

jobs:
  review:
    # Every label change starts a run; the standard review ignores them.
    if: github.event.action != 'labeled'
    # Job-level groups, so the standard and the deep review never cancel each other. Label
    # events get a group of their own, so they can never cancel a standard review in flight.
    concurrency:
      group: >-
        ${{ github.event.action != 'labeled'
            && format('ai-review-{0}', github.event.pull_request.number)
            || format('ai-review-noop-{0}', github.run_id) }}
      cancel-in-progress: true
    uses: my-org/ai-review-workflows/.github/workflows/ai-review.yml@1111111111111111111111111111111111111111
    with:
      recommend_deep_review: true
      # Must name the deep job below. It matters to this job: it is how the standard review
      # finds the deep review and stands down.
      deep_review_job: deep-review
    secrets: inherit

  deep-review:
    # Only for the deep-review label, never for a draft, and on later pushes while the label is set.
    if: >-
      contains(github.event.pull_request.labels.*.name, 'deep-review') &&
      !github.event.pull_request.draft &&
      (github.event.action != 'labeled' || github.event.label.name == 'deep-review')
    # Other labels get a group of their own, so they can never cancel a deep review in flight.
    concurrency:
      group: >-
        ${{ (github.event.action != 'labeled' || github.event.label.name == 'deep-review')
            && format('ai-deep-review-{0}', github.event.pull_request.number)
            || format('ai-deep-review-noop-{0}', github.run_id) }}
      cancel-in-progress: true
    uses: my-org/ai-review-workflows/.github/workflows/ai-review.yml@1111111111111111111111111111111111111111
    with:
      mode: deep
      deep_review_job: deep-review
    secrets: inherit
```

Replace `1111111111111111111111111111111111111111` with the full commit SHA of the central workflow. Dependabot or Renovate can keep that pin current.

Keep the label named `deep-review`: the deep review recommendation in the standard summary tells people to add exactly that label.

Remove any older per-repository review workflow when you add the caller. Two copies of the action on one pull request fight over the same comments.

### Passing the secrets

The caller passes its own secrets in one of two ways:

- `secrets: inherit` passes every secret the calling repository can read (repository and organization secrets) to the central workflow. It works only when the caller and the central repository are in the same organization or enterprise. It is the least work, but the central workflow could then read any of those secrets, which is one more reason to pin it by SHA.
- Explicit secrets name each one, and work across organizations:

  ```yaml
      secrets:
        OPENAI_API_KEY: ${{ secrets.OPENAI_API_KEY }}
        AI_REVIEW_APP_ID: ${{ secrets.AI_REVIEW_APP_ID }}
        AI_REVIEW_APP_PRIVATE_KEY: ${{ secrets.AI_REVIEW_APP_PRIVATE_KEY }}
  ```

Model keys follow the same choice as the App key:

- **A key per repository**, as a repository secret. Each repository's model spend shows on its own key, so cost tracking and budget limits are per repository. Revoking one key affects one repository.
- **One organization secret with selected repository access.** Easier to roll out, but every selected repository spends from one key and one bill. To still see the cost per repository or team, use separate keys (or separate projects at the model provider) for each group of repositories.

Runner minutes are always billed to the calling repository's owner, whichever option you pick.

### Naming the deep review job

The standard review stands down when it finds a deep review of the same pull request head that is queued, running, or has succeeded. It finds that job by its name, which the `deep_review_job` input gives (default `deep-review`).

Under a reusable workflow, GitHub names each job `<caller job> / <central job>`. In the caller above, the deep review shows as `deep-review / review` and the standard one as `review / review`. Set `deep_review_job` to the caller job's display name, the part before ` / `: its `name:` if it has one, else its job key. The central workflow cannot learn that name by itself, which is why it takes it as an input and why the caller passes it.

The match is exact and case-sensitive on that first part:

- `deep-review` matches `deep-review`, `deep-review / review` and the matrix forms `deep-review (codex)` and `deep-review (codex) / review`.
- It does not match `review / review`, `deep-review-extra` or `Deep review`.
- If the caller job has `name: Deep review`, pass `deep_review_job: Deep review`.

Get this wrong and the standard review never finds the deep one: both post, and whichever finishes last replaces the other.

## Identity and the review marker

With the App, review comments are authored by `<app-slug>[bot]`. The hidden marker at the end of the summary is the same as before, so a repository can move from `github-actions[bot]` to the App without losing track of earlier reviews:

- Comments by `github-actions[bot]` that carry the marker are still recognized as the review's own, alongside the App's.
- Code that did not change since the last review is not reviewed again, whichever identity posted that review.
- The first App run leaves the old `github-actions[bot]` summary in place and posts a new summary as the App. From then on the App updates its own summary. It never edits the old one, because the edit would still show `github-actions[bot]` as the author.
- Old unanswered review threads are deleted as before when the App's token is allowed to. If GitHub refuses, they stay on the pull request, the run logs a warning, and they are counted in the summary as carried over.
- If the App is removed later, the action goes back to `github-actions[bot]` and updates the newest summary that identity wrote, which may sit above the App's newer one.

The suggested-reviewers comment follows the same rules: it is posted once, and a comment by either identity counts.

## Keeping the cost down

A deep review costs many times a standard one. The setup above keeps it opt-in:

- **Label only.** The deep job runs only on pull requests carrying the `deep-review` label: when the label is added, and on later pushes while it stays. A label added by a workflow with `GITHUB_TOKEN` starts no run at all, so a person or an App token has to add it.
- **Drafts.** The standard review skips draft pull requests with a notice and runs on `ready_for_review`. The deep job has the draft condition in its `if:` on purpose: a deep review that ran and skipped a draft would count as a successful deep review, and a later standard review of the same head would stand down with nothing posted. Skipped by `if:`, it never counts.
- **Stand-down.** When a push runs both jobs, the standard review sees the deep one and stops before the agent starts. It checks again right before posting, so a deep review that starts mid-review still wins. A deep review whose pull request gets a new commit stops without posting. These checks fail open: if the Actions API cannot be read (for example without Actions read), the run warns and goes on. Spend already made by a review that then stands down is not recovered.
- **Concurrency.** Each job has its own job-level group with `cancel-in-progress`, so a new push cancels the outdated review of the same kind, and an unrelated label never cancels anything. Do not use a workflow-level group: every label change starts a run, and it would cancel a review in flight.
- **Timeouts.** The central job bounds the whole run, 45 minutes for a standard review and 120 for a deep one.
- **Recommendation.** With `recommend_deep_review` on, the standard summary says when the change looks risky enough for a deep review, so the label goes where it is worth it.

## Fork pull requests

Pull requests from forks are not supported, with or without the App. A `pull_request` run for a fork gets no secrets and a read-only token, so the action exits 0 with a notice. Do not switch to `pull_request_target` to get around this: that runs code from the fork with write access and secrets, and the action refuses that event.

## Alternatives

**An App webhook relay to a central repository.** The App's webhook delivers pull request events to a small service, which starts a run in a central repository for each one. This avoids a caller file in every repository, but it moves every secret to the center: the model keys and the App key live in one place, the spend lands on one bill, and that repository becomes a target worth attacking. If you go this way:

- Verify the webhook signature, and pass on nothing but the repository and the pull request number.
- Have the central run fetch the pull request again from the API (head and base SHAs, draft state, labels, whether it comes from a fork) and never trust the payload.
- Mint each token for the one repository under review, with the permissions in the table above.
- The action itself only supports the `pull_request` event, so this setup needs its own wrapper around `review.py`.

**Organization required workflows or rulesets.** Rulesets can require a workflow from a central repository to run on every pull request in selected repositories, which removes the caller file. Availability depends on your GitHub plan. A required workflow also becomes a merge requirement, so a failed or timed-out review would block merging, and the deep review needs a rule or a caller of its own. Check what your plan supports before choosing it over the reusable workflow.
