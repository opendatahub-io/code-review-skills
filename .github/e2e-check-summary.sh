#!/usr/bin/env bash
# Fails unless github-actions[bot] posted or updated an AI review summary on the pull request
# since SINCE (UTC, ISO 8601), containing EXPECT when that is set. The summary carries the head
# SHA in its marker only when every inline comment landed, so the time is what proves this run
# posted it.
set -euo pipefail
: "${REPO:?}" "${PR:?}" "${SINCE:?}"
bodies="$(gh api --paginate "repos/$REPO/issues/$PR/comments" \
    -q '.[] | select(.user.login == "github-actions[bot]")
        | select(.updated_at >= env.SINCE)
        | select(.body | contains("<!-- ai-review")) | .body')"
if [ -z "$bodies" ]; then
    echo "::error::No AI review summary was posted or updated since $SINCE"
    exit 1
fi
if [ -n "${EXPECT:-}" ] && ! grep -qF -- "$EXPECT" <<<"$bodies"; then
    echo "::error::The AI review summary posted since $SINCE does not contain '$EXPECT'"
    exit 1
fi
echo "AI review summary found"
