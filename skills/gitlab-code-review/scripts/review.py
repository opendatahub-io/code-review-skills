#!/usr/bin/env -S uv run --script
# /// script
# dependencies = [
#     "requests>=2.28.0",
# ]
# ///
"""
AI code review comment manager.

Post, update, and display AI-generated code review results on GitLab MRs and GitHub PRs.

Subcommands:
  post               Post review as inline comments + summary to a GitLab MR or GitHub PR
  display            Display review results in the terminal
  suggest-reviewers  Suggest reviewers based on git history of modified files
  stand-down         Exit 10 when another review should win over this one (GitHub only)

Environment variables (for 'post' with GitLab):
  GITLAB_API_TOKEN              GitLab Personal Access Token (required)
  CI_SERVER_URL                 GitLab server URL (default: https://gitlab.com)
  CI_PROJECT_ID                 GitLab project ID (required)
  CI_MERGE_REQUEST_IID          MR IID (required)
  CI_MERGE_REQUEST_DIFF_BASE_SHA  Base SHA for diff (required)
  CI_COMMIT_SHA                 Head SHA (required)
  CI_JOB_NAME                   Job name for footer (optional)
  CI_JOB_URL                    Job URL for footer (optional)
  AGENT_MODEL                   Model for footer, set by agentic-ci (optional)
  AGENT_REASONING_EFFORT        Reasoning effort for footer, set by agentic-ci (optional)
  CHILL_MODE                    Filter suggestion-level comments (default: true)
  INLINE_FIXES                  Post applicable suggestions from
                                inline_comments[].fix (default: true)
  VERBOSE                       Enable verbose output (default: false)

Environment variables (for 'post --platform github', which is never auto-detected):
  GITHUB_TOKEN                  Token that can write pull requests (required)
  GITHUB_API_URL                REST API URL, must be https (default: https://api.github.com)
  GITHUB_GRAPHQL_URL            GraphQL URL, same host as the API (default: <API URL>/graphql)
  GITHUB_REPOSITORY             owner/repo (required)
  GITHUB_EVENT_PATH             Path to the pull_request event payload (required)
  GITHUB_SERVER_URL             Server URL, for the run link in the footer (optional)
  GITHUB_RUN_ID                 Run ID, for the run link in the footer (optional)
  RUNNER_NAME                   Runner of this job; with GITHUB_RUN_ID, keeps the
                                stand-down check from matching its own job (optional)
  AI_REVIEW_BOT_LOGIN           Account GITHUB_TOKEN posts as, when it is not
                                github-actions[bot]: a GitHub App's <slug>[bot], or
                                the user login behind a personal access token
                                (optional). Comments of github-actions[bot] still
                                count as ours, but only the account's own summary
                                is updated in place.
  CHILL_MODE, INLINE_FIXES, VERBOSE, AGENT_MODEL, AGENT_REASONING_EFFORT apply as above

Usage:
  review.py post review.json [--chill|--no-chill] [--inline-fixes|--no-inline-fixes] [--verbose]
  review.py post review.json --platform github [--deep-recommendation-plan PATH]
                               [--mode standard|deep] [--deep-job NAME]
  review.py stand-down --platform github --mode standard|deep [--deep-job NAME]
  review.py display review.json [--chill|--no-chill] [--inline-fixes|--no-inline-fixes]
  review.py suggest-reviewers [--verbose]
"""

from __future__ import annotations

import argparse
import html
import json
import os
import re
import shutil
import stat
import subprocess
import sys
import unicodedata
from collections.abc import Iterator
from pathlib import Path
from typing import Any, NamedTuple
from urllib.parse import urlparse

import requests

# Exact marker appended to every AI review note ("<!-- ai-review -->" or
# "<!-- ai-review sha:<sha> -->"). Matching is exact so it never conflates a
# review note with other markers (e.g. SUGGESTED_REVIEWERS_MARKER).
_AI_REVIEW_MARKER_RE = re.compile(r"<!-- ai-review(?: sha:[0-9a-f]+)? -->")

SEVERITY_ICONS = {
    "critical": "\U0001f534",
    "major": "\U0001f7e0",
    "minor": "\U0001f7e1",
    "suggestion": "\U0001f4a1",
}

SEVERITY_ORDER = ["critical", "major", "minor", "suggestion"]
_VALID_SEVERITIES = frozenset(SEVERITY_ORDER)

# Upper bound on the number of lines a single applicable suggestion may replace.
# GitLab caps multi-line suggestions; anything this large is unlikely to be a
# safe one-click fix anyway.
_FIX_MAX_LINES = 50
_API_TIMEOUT = 30

_RED = "\033[0;31m"
_GREEN = "\033[0;32m"
_YELLOW = "\033[1;33m"
_BLUE = "\033[0;34m"
_CYAN = "\033[0;36m"
_NC = "\033[0m"


def _step(msg: str) -> None:
    print(f"{_BLUE}▶ {msg}{_NC}", file=sys.stderr)


def _success(msg: str) -> None:
    print(f"{_GREEN}✓ {msg}{_NC}", file=sys.stderr)


def _warning(msg: str) -> None:
    print(f"{_YELLOW}⚠ {msg}{_NC}", file=sys.stderr)


def _error(msg: str) -> None:
    print(f"{_RED}✗ {msg}{_NC}", file=sys.stderr)


def _header(msg: str) -> None:
    print(f"{_CYAN}{msg}{_NC}")


# ---------------------------------------------------------------------------
# JSON parsing (platform-independent)
# ---------------------------------------------------------------------------


def parse_review_json(
    filepath: str, chill_mode: bool = True, inline_fixes: bool = True
) -> dict[str, Any]:
    """Parse, validate, and optionally filter review JSON from a file.

    Structural errors in required fields raise ValueError so the agent can
    repair the JSON. A malformed optional ``fix`` object never fails the
    review: it is dropped with a warning and the comment posts as prose.
    """
    raw = Path(filepath).read_text(encoding="utf-8", errors="replace").strip()

    m = re.search(r"`{3,}(?:json)?\s*\n(.+?)\s*`{3,}", raw, re.DOTALL)
    if m:
        raw = m.group(1).strip()

    data = json.loads(raw)

    for field in ("summary", "inline_comments"):
        if field not in data:
            raise ValueError(f"Missing required field: {field}")

    if not isinstance(data["inline_comments"], list):
        raise ValueError("inline_comments must be an array")

    for i, c in enumerate(data["inline_comments"]):
        for req in ("file", "line", "severity", "comment"):
            if req not in c:
                raise ValueError(f"inline_comments[{i}] missing field: {req}")
        if not isinstance(c["file"], str):
            raise ValueError(
                f"inline_comments[{i}].file: expected str, got {type(c['file']).__name__}"
            )
        if not isinstance(c["line"], int) or isinstance(c["line"], bool):
            raise ValueError(f"inline_comments[{i}].line: expected positive int, got {c['line']!r}")
        if c["line"] <= 0:
            raise ValueError(f"inline_comments[{i}].line: expected positive int, got {c['line']}")
        if not isinstance(c["comment"], str):
            raise ValueError(
                f"inline_comments[{i}].comment: expected str, got {type(c['comment']).__name__}"
            )
        sev = c["severity"]
        if sev not in _VALID_SEVERITIES:
            raise ValueError(
                f"inline_comments[{i}].severity: expected one of "
                f"{sorted(_VALID_SEVERITIES)}, got {sev!r}"
            )
        if "fix" in c:
            problem = _validate_fix(c["fix"], c["line"])
            if problem or not inline_fixes:
                if problem:
                    _warning(f"inline_comments[{i}].fix dropped: {problem}")
                del c["fix"]

    if chill_mode:
        before = len(data["inline_comments"])
        data["inline_comments"] = [
            c for c in data["inline_comments"] if c.get("severity") != "suggestion"
        ]
        filtered = before - len(data["inline_comments"])
        if filtered > 0:
            _step(f"Chill mode: filtered out {filtered} suggestion(s)")

    return data


# ---------------------------------------------------------------------------
# Applicable fixes (GitLab suggestion blocks)
# ---------------------------------------------------------------------------


def _validate_fix(fix: Any, line: int) -> str | None:
    """Return a reason to drop ``fix``, or None when it is structurally sound.

    ``fix`` replaces whole lines ``start_line..end_line`` (inclusive, new file
    numbering) with ``code``. The anchored comment ``line`` must fall inside
    that range: GitLab expresses suggestions as offsets from the note, and the
    GitHub backend anchors the suggestion on the range itself.
    An empty ``code`` string deletes the range.
    """
    if not isinstance(fix, dict):
        return f"expected object, got {type(fix).__name__}"
    for key in ("start_line", "end_line"):
        val = fix.get(key)
        if not isinstance(val, int) or isinstance(val, bool) or val <= 0:
            return f"{key}: expected positive int, got {val!r}"
    code = fix.get("code")
    if not isinstance(code, str):
        return f"code: expected str, got {type(code).__name__}"
    start, end = fix["start_line"], fix["end_line"]
    if start > end:
        return f"start_line {start} is after end_line {end}"
    if not start <= line <= end:
        return f"comment line {line} is outside fix range {start}-{end}"
    if end - start + 1 > _FIX_MAX_LINES:
        return f"range spans {end - start + 1} lines, limit is {_FIX_MAX_LINES}"
    if "```" in code:
        return "code contains a ``` fence, which would break the suggestion block"
    return None


def _format_fix_block(fix: dict[str, Any], line: int) -> str:
    """Render ``fix`` as a GitLab ``suggestion`` fenced block anchored at ``line``."""
    above = line - fix["start_line"]
    below = fix["end_line"] - line
    header = "```suggestion" if above == 0 and below == 0 else f"```suggestion:-{above}+{below}"
    code = fix["code"].rstrip("\n")
    if code:
        return f"{header}\n{code}\n```"
    return f"{header}\n```"


def _verify_fix_against_head(
    comment: dict[str, Any], head_sha: str, *, verbose: bool = False
) -> None:
    """Drop ``comment['fix']`` when it does not fit the file at ``head_sha``.

    Keeps the fix when git is unavailable: the platform rejects a bad range
    itself and the comment still posts.
    """
    fix = comment.get("fix")
    if not fix:
        return
    file_path = comment["file"]
    try:
        content = subprocess.check_output(
            ["git", "show", f"{head_sha}:{file_path}"],
            text=True,
            stderr=subprocess.DEVNULL,
        )
    except (subprocess.CalledProcessError, FileNotFoundError):
        if verbose:
            _warning(f"Could not read {file_path} at {head_sha[:8]}; keeping fix unverified")
        return
    lines = content.split("\n")
    if content.endswith("\n"):
        lines.pop()
    if fix["end_line"] > len(lines):
        _warning(
            f"Dropping fix on {file_path}:{comment['line']}: "
            f"end_line {fix['end_line']} exceeds file length {len(lines)}"
        )
        del comment["fix"]
        return
    current = "\n".join(lines[fix["start_line"] - 1 : fix["end_line"]])
    if current == fix["code"].rstrip("\n"):
        _warning(f"Dropping fix on {file_path}:{comment['line']}: replacement equals current code")
        del comment["fix"]


# ---------------------------------------------------------------------------
# Dedup filtering (platform-independent)
# ---------------------------------------------------------------------------


def filter_already_reviewed(
    data: dict[str, Any], previous_sha: str, head_sha: str
) -> dict[str, Any]:
    """Remove comments on code unchanged since the previous review."""
    if previous_sha == head_sha:
        return data

    inline = data.get("inline_comments", [])
    if not inline:
        return data

    try:
        subprocess.check_output(
            ["git", "rev-parse", "--verify", previous_sha],
            stderr=subprocess.DEVNULL,
        )
    except (subprocess.CalledProcessError, FileNotFoundError):
        _warning("Dedup filter: previous SHA not reachable, keeping all comments")
        return data

    try:
        diff_output = subprocess.check_output(
            [
                "git",
                "diff",
                "--no-ext-diff",
                "--no-textconv",
                f"{previous_sha}..{head_sha}",
                "--unified=0",
            ],
            text=True,
            stderr=subprocess.DEVNULL,
        )
    except (subprocess.CalledProcessError, FileNotFoundError):
        _warning("Dedup filter: git diff failed, keeping all comments")
        return data

    changed_lines: dict[str, set[int]] = {}
    current_file: str | None = None
    for line in diff_output.split("\n"):
        file_match = re.match(r"^\+\+\+ b/(.+)$", line)
        if file_match:
            current_file = file_match.group(1)
            if current_file not in changed_lines:
                changed_lines[current_file] = set()
            continue
        hunk_match = re.match(r"^@@ -\d+(?:,\d+)? \+(\d+)(?:,(\d+))? @@", line)
        if hunk_match and current_file:
            start = int(hunk_match.group(1))
            count = int(hunk_match.group(2)) if hunk_match.group(2) else 1
            if count == 0:
                continue
            for i in range(start, start + count):
                changed_lines[current_file].add(i)

    filtered_comments = []
    skipped = 0
    for c in inline:
        fp = c.get("file", "")
        ln = c.get("line", 0)
        if fp in changed_lines and ln in changed_lines[fp]:
            filtered_comments.append(c)
        else:
            skipped += 1

    if skipped > 0:
        _step(f"Dedup filter: skipped {skipped} comment(s) on unchanged code")

    data["inline_comments"] = filtered_comments
    return data


# ---------------------------------------------------------------------------
# Display (platform-independent)
# ---------------------------------------------------------------------------


def _display_review(data: dict[str, Any]) -> None:
    """Display review results in the terminal."""
    ruler = "━" * 90
    print()
    _header(ruler)
    _header("                                   AI CODE REVIEW                                    ")
    _header(ruler)
    print()

    print("## Summary")
    print(data.get("summary", "No summary provided."))
    print()

    positives = data.get("positive_aspects", [])
    if positives:
        print("## Positive Aspects")
        for p in positives:
            print(f"  + {p}")
        print()

    inline = data.get("inline_comments", [])
    if inline:
        print(f"## Issues Found ({len(inline)})")
        print()
        for c in inline:
            icon = SEVERITY_ICONS.get(c["severity"], "\U0001f4ac")
            print(f"  {icon} [{c['severity'].upper()}] {c['file']}:{c['line']}")
            for ln in c["comment"].split("\n"):
                print(f"     {ln}")
            fix = c.get("fix")
            if fix:
                span = f"{fix['start_line']}-{fix['end_line']}"
                print(f"     Suggested replacement for lines {span}:")
                for ln in fix["code"].rstrip("\n").split("\n"):
                    print(f"     + {ln}")
            print()

    fix_prompt = data.get("fix_prompt")
    if fix_prompt:
        print("## Fix Prompt")
        print(fix_prompt)
        print()

    _header(ruler)
    print()


# ---------------------------------------------------------------------------
# Platform detection
# ---------------------------------------------------------------------------


def _detect_platform() -> str:
    """Detect the CI platform from environment variables.

    Returns 'gitlab' or 'local'.
    """
    if os.environ.get("CI_MERGE_REQUEST_IID") and os.environ.get("CI"):
        return "gitlab"
    return "local"


# ===========================================================================
# GitLab backend
# ===========================================================================


class _SafeSession(requests.Session):
    """Session with default timeout and SSRF-hardening (no redirect following).

    Redirects are blocked so the PRIVATE-TOKEN header is never forwarded
    to an unexpected host.  All requests get a default timeout to prevent
    indefinite hangs in CI.
    """

    def request(self, *args: Any, **kwargs: Any) -> requests.Response:  # type: ignore[override]
        kwargs.setdefault("timeout", _API_TIMEOUT)
        kwargs.setdefault("allow_redirects", False)
        return super().request(*args, **kwargs)


def _gitlab_paginated_get(session: requests.Session, url: str) -> list[Any]:
    """Fetch all pages from a GitLab list endpoint.

    Raises requests.HTTPError on auth/server errors (401, 403, 5xx).
    Returns an empty list on 404.
    """
    results: list[Any] = []
    params: dict[str, int] = {"per_page": 100}
    first_page = True
    while True:
        resp = session.get(url, params=params)
        if resp.status_code == 404:
            return results
        if resp.status_code != 200:
            if first_page:
                resp.raise_for_status()
            _warning(f"GitLab API returned HTTP {resp.status_code} during pagination")
            break
        page_data = resp.json()
        if not page_data:
            break
        results.extend(page_data)
        first_page = False
        next_page = resp.headers.get("x-next-page")
        if not next_page:
            break
        params["page"] = int(next_page)
    return results


def _make_review_marker(sha: str | None = None) -> str:
    if sha:
        return f"<!-- ai-review sha:{sha} -->"
    return "<!-- ai-review -->"


_LEGACY_EMOJI_PATTERN = re.compile(
    r"^[\U0001f534\U0001f7e0\U0001f7e1\U0001f4a1\U0001f4ac]"
    r"\s+\*\*(?:Critical|Major|Minor|Suggestion)\*\*:"
)


def _is_ai_review_comment(body: str, *, allow_legacy_cleanup: bool = False) -> bool:
    """Check whether a note body is an AI review comment.

    Returns True only when the exact AI review marker is present, unless
    allow_legacy_cleanup is set, in which case the old emoji+severity
    pattern is also accepted (for cleaning up older reviews that
    predate the marker convention).
    """
    if _AI_REVIEW_MARKER_RE.search(body):
        return True
    if allow_legacy_cleanup and _LEGACY_EMOJI_PATTERN.match(body):
        return True
    return False


def _gitlab_get_previous_review_sha(session: requests.Session, api_base: str) -> str | None:
    """Extract commit SHA from the previous AI review summary note."""
    for note in _gitlab_paginated_get(session, f"{api_base}/notes"):
        if note.get("position") is not None:
            continue
        body = note.get("body", "")
        m = re.search(r"<!-- ai-review sha:([0-9a-f]+) -->", body)
        if m:
            return m.group(1)

    return None


def _gitlab_find_summary_note_ids(session: requests.Session, api_base: str) -> list[int]:
    """Find all AI review summary note IDs to remove before posting a new one.

    Matches broadly on purpose so a single sweep clears accumulated and legacy
    summaries: any "<!-- ai-review..." marker (which also covers the pre-rename
    "<!-- ai-review-reviewers -->" note — that gets reposted with the current
    SUGGESTED_REVIEWERS_MARKER), plus older marker-less summaries identified by
    their heading or footer. The current reviewer note uses
    SUGGESTED_REVIEWERS_MARKER, so it is left untouched.
    """
    ids: list[int] = []
    for note in _gitlab_paginated_get(session, f"{api_base}/notes"):
        if note.get("position") is not None:
            continue
        body = note.get("body", "")
        if (
            "<!-- ai-review" in body
            or "## AI Code Review Summary" in body
            or "*Generated by [Claude Code AI Review]" in body
        ):
            ids.append(note["id"])

    return ids


def _gitlab_delete_previous_discussions(
    session: requests.Session, api_base: str, *, verbose: bool = False
) -> None:
    """Delete unresolved AI review discussions without human responses."""
    _step("Checking for previous AI review discussions to delete...")

    discussions = _gitlab_paginated_get(session, f"{api_base}/discussions")
    if not discussions:
        _step("No previous AI review discussions found")
        return

    to_delete: list[tuple[str, int]] = []
    skipped = 0

    for d in discussions:
        notes = d.get("notes", [])
        if not notes:
            continue
        first_note = notes[0]
        if first_note.get("position") is None:
            continue
        body = first_note.get("body", "")
        if not _is_ai_review_comment(body):
            continue
        if first_note.get("resolved"):
            skipped += 1
            continue
        human_replies = [
            n
            for n in notes[1:]
            if not n.get("system", False) and not _AI_REVIEW_MARKER_RE.search(n.get("body", ""))
        ]
        if human_replies:
            skipped += 1
            continue
        to_delete.append((d["id"], first_note["id"]))

    if not to_delete:
        if skipped:
            _step(f"Keeping {skipped} discussion(s) with replies or resolutions")
        else:
            _step("No previous AI review discussions found")
        return

    _step(f"Deleting {len(to_delete)} previous AI review discussion(s)...")
    if skipped:
        _step(f"Keeping {skipped} discussion(s) with replies or resolutions")

    deleted = 0
    for disc_id, note_id in to_delete:
        resp = session.delete(f"{api_base}/discussions/{disc_id}/notes/{note_id}")
        if 200 <= resp.status_code < 300:
            deleted += 1
        else:
            _warning(f"Failed to delete discussion {disc_id} (HTTP {resp.status_code})")

    _success(f"Deleted {deleted}/{len(to_delete)} previous discussion(s)")


def _gitlab_resolve_old_path(base_sha: str, head_sha: str, new_path: str) -> str:
    """Find the pre-rename path of a file, if it was renamed between base and head."""
    try:
        output = subprocess.check_output(
            ["git", "diff", "--diff-filter=R", "--name-status", f"{base_sha}..{head_sha}"],
            text=True,
            stderr=subprocess.DEVNULL,
        )
    except (subprocess.CalledProcessError, FileNotFoundError):
        return new_path
    for diff_line in output.strip().split("\n"):
        parts = diff_line.split("\t")
        if len(parts) >= 3 and parts[2] == new_path:
            return parts[1]
    return new_path


def _gitlab_post_inline_comment(
    session: requests.Session,
    api_base: str,
    file_path: str,
    line: int,
    comment: str,
    base_sha: str,
    head_sha: str,
    start_sha: str,
    *,
    verbose: bool = False,
) -> bool:
    """Post an inline discussion on the MR diff."""
    old_path = _gitlab_resolve_old_path(base_sha, head_sha, file_path)
    payload = {
        "body": comment,
        "position": {
            "base_sha": base_sha,
            "head_sha": head_sha,
            "start_sha": start_sha,
            "position_type": "text",
            "old_path": old_path,
            "new_path": file_path,
            "new_line": line,
        },
    }

    resp = session.post(f"{api_base}/discussions", json=payload)
    if 200 <= resp.status_code < 300:
        return True

    _warning(f"Failed to post inline comment on {file_path}:{line} (HTTP {resp.status_code})")
    if verbose:
        print(resp.text, file=sys.stderr)
    return False


def _gitlab_upsert_summary_note(
    session: requests.Session,
    api_base: str,
    body: str,
    *,
    verbose: bool = False,
) -> bool:
    """Delete existing/legacy summary notes and post a new one."""
    existing_ids = _gitlab_find_summary_note_ids(session, api_base)
    for note_id in existing_ids:
        _step(f"Removing previous summary note (id: {note_id})...")
        del_resp = session.delete(f"{api_base}/notes/{note_id}")
        if not (200 <= del_resp.status_code < 300):
            _warning(f"Failed to delete previous summary note (HTTP {del_resp.status_code})")

    mr_iid = api_base.rsplit("/", 1)[-1]
    _step(f"Posting summary note to MR !{mr_iid}...")
    resp = session.post(f"{api_base}/notes", json={"body": body})
    if 200 <= resp.status_code < 300:
        return True

    _error(f"Failed to post summary note (HTTP {resp.status_code})")
    if verbose:
        print(resp.text, file=sys.stderr)
    return False


_AGENT_TOOL_HARNESS = {
    "claude": "claude-code",
    "claude-code": "claude-code",
    "opencode": "opencode",
    "codex": "codex",
}


def _detect_harness() -> str:
    # agentic-ci exports AGENT_TOOL in every sandbox; prefer it over binary
    # sniffing so images that bundle more than one CLI report the right one.
    agent_tool = os.environ.get("AGENT_TOOL", "").strip().lower()
    if agent_tool in _AGENT_TOOL_HARNESS:
        return _AGENT_TOOL_HARNESS[agent_tool]
    if shutil.which("claude"):
        return "claude-code"
    if shutil.which("opencode"):
        return "opencode"
    if shutil.which("codex"):
        return "codex"
    return "unknown"


def _gitlab_build_summary_body(
    data: dict[str, Any],
    review_marker: str,
    job_name: str | None = None,
    job_url: str | None = None,
) -> str:
    """Build the markdown summary body for the MR note."""
    parts: list[str] = ["## AI Code Review Summary\n"]
    parts.append(data.get("summary", "No summary provided."))

    positives = data.get("positive_aspects", [])
    if positives:
        parts.append("\n### Positive Aspects")
        for p in positives:
            parts.append(f"- {p}")

    inline = data.get("inline_comments", [])
    if inline:
        severity_counts: dict[str, int] = {}
        for c in inline:
            s = c["severity"]
            severity_counts[s] = severity_counts.get(s, 0) + 1

        parts.append(f"\n### Issues Found ({len(inline)} total)")
        for sev in SEVERITY_ORDER:
            if sev in severity_counts:
                icon = SEVERITY_ICONS.get(sev, "")
                parts.append(f"- {icon} {sev.capitalize()}: {severity_counts[sev]}")
        fixes = sum(1 for c in inline if c.get("fix"))
        if fixes:
            parts.append(
                f"- \U0001f6e0\ufe0f With applicable suggestion: {fixes} "
                "(use **Apply suggestion** on the inline comment)"
            )
        parts.append("\n*See inline comments on the diff for details.*")

    fix_prompt = data.get("fix_prompt")
    if fix_prompt:
        parts.append("\n### \U0001f916 Fix all issues with AI agents")
        parts.append("\n**Copy-paste this into a prompt to automatically fix the issues found:**")
        parts.append(f"\n````\n{fix_prompt}\n````")

    summary = "\n".join(parts)

    jn = job_name or "ai-review"
    ju = job_url or "#"
    harness = _detect_harness()
    model = os.environ.get("AGENT_MODEL")
    # agentic-ci exports the effective effort (including its default) next to
    # AGENT_MODEL; it is unset when the effort is "none".
    effort = os.environ.get("AGENT_REASONING_EFFORT")
    extras = f" • Harness: {harness}"
    if model:
        extras += f" • Model: {model}"
    if effort:
        extras += f" • Effort: {effort}"
    footer = (
        "\n\n---\n"
        "*Generated by [Code Review]"
        "(https://gitlab.com/redhat/rhel-ai/agentic-ci/code-review)"
        f" • Job: [{jn}]({ju}){extras}*\n"
        f"{review_marker}"
    )

    return summary + footer


def _gitlab_post(args: argparse.Namespace) -> None:
    """Post review results to a GitLab MR."""
    token = os.environ.get("GITLAB_API_TOKEN")
    if not token:
        _error("GITLAB_API_TOKEN environment variable is required")
        sys.exit(1)

    project_id = os.environ.get("CI_PROJECT_ID")
    mr_iid = os.environ.get("CI_MERGE_REQUEST_IID")
    base_sha = os.environ.get("CI_MERGE_REQUEST_DIFF_BASE_SHA")
    head_sha = os.environ.get("CI_COMMIT_SHA")

    if not all([project_id, mr_iid, base_sha, head_sha]):
        _error(
            "Missing required CI variables: CI_PROJECT_ID, "
            "CI_MERGE_REQUEST_IID, CI_MERGE_REQUEST_DIFF_BASE_SHA, "
            "CI_COMMIT_SHA"
        )
        sys.exit(1)

    if not project_id.isdigit() or not mr_iid.isdigit():
        _error("CI_PROJECT_ID and CI_MERGE_REQUEST_IID must be numeric")
        sys.exit(1)

    gitlab_url = os.environ.get("CI_SERVER_URL", "https://gitlab.com").rstrip("/")
    start_sha = base_sha
    job_name = os.environ.get("CI_JOB_NAME")
    job_url = os.environ.get("CI_JOB_URL")
    verbose = _resolve_verbose(args)
    chill = _resolve_chill(args)
    inline_fixes = _resolve_inline_fixes(args)

    try:
        data = parse_review_json(args.review_file, chill_mode=chill, inline_fixes=inline_fixes)
    except (json.JSONDecodeError, ValueError) as e:
        _error(f"Failed to parse review JSON: {e}")
        sys.exit(1)

    parsed_url = urlparse(gitlab_url)
    if parsed_url.scheme != "https":
        _error(f"CI_SERVER_URL must use HTTPS, got scheme: {parsed_url.scheme}")
        sys.exit(1)

    expected_host = os.environ.get("CI_SERVER_HOST")
    if expected_host and parsed_url.hostname != expected_host:
        _error(
            f"CI_SERVER_URL hostname ({parsed_url.hostname}) "
            f"does not match CI_SERVER_HOST ({expected_host})"
        )
        sys.exit(1)

    api_base = f"{gitlab_url}/api/v4/projects/{project_id}/merge_requests/{mr_iid}"
    session = _SafeSession()
    session.headers.update({"PRIVATE-TOKEN": token})

    review_marker = _make_review_marker(head_sha)

    try:
        prev_sha = _gitlab_get_previous_review_sha(session, api_base)
        if prev_sha and prev_sha != head_sha:
            _step(f"Found previous review (sha: {prev_sha[:8]}...), filtering unchanged code...")
            data = filter_already_reviewed(data, prev_sha, head_sha)

        _gitlab_delete_previous_discussions(session, api_base, verbose=verbose)

        inline = data.get("inline_comments", [])
        posted_comments: list[dict[str, Any]] = []
        failed = 0

        if inline:
            _step(f"Posting {len(inline)} inline comment(s) to MR !{mr_iid}...")
            for c in inline:
                _verify_fix_against_head(c, head_sha, verbose=verbose)
                icon = SEVERITY_ICONS.get(c["severity"], "\U0001f4ac")
                comment_body = f"{icon} **{c['severity'].capitalize()}**: {c['comment']}"
                if c.get("fix"):
                    comment_body += f"\n\n{_format_fix_block(c['fix'], c['line'])}"
                comment_body += f"\n\n{review_marker}"
                if _gitlab_post_inline_comment(
                    session,
                    api_base,
                    c["file"],
                    c["line"],
                    comment_body,
                    base_sha,
                    head_sha,
                    start_sha,
                    verbose=verbose,
                ):
                    posted_comments.append(c)
                else:
                    failed += 1

            _success(
                f"Posted {len(posted_comments)}/{len(inline)} inline comments ({failed} failed)"
            )
        else:
            _step("No inline comments to post")

        # Rebuild inline_comments so the summary counts only what's visible on the MR.
        data["inline_comments"] = posted_comments
        # Drop fix_prompt when it would reference findings the reviewer can't see.
        if failed > 0:
            data.pop("fix_prompt", None)
        summary_body = _gitlab_build_summary_body(data, review_marker, job_name, job_url)
        if _gitlab_upsert_summary_note(session, api_base, summary_body, verbose=verbose):
            _success(f"Summary posted to merge request !{mr_iid}")
        else:
            _error("Failed to post summary note")
            sys.exit(1)

        if failed > 0:
            _error(f"{failed}/{len(posted_comments) + failed} inline comments failed to post")
            sys.exit(1)
    except requests.exceptions.HTTPError as e:
        _error(f"GitLab API error: {e}")
        sys.exit(1)


# ===========================================================================
# Summary helpers (platform-neutral)
# ===========================================================================


def _summary_positive_lines(positives: list[str]) -> list[str]:
    """Markdown lines for the "Positive Aspects" section, empty when there are none."""
    if not positives:
        return []
    return ["\n### Positive Aspects", *(f"- {p}" for p in positives)]


def _summary_count_lines(inline: list[dict[str, Any]], apply_hint: str) -> list[str]:
    """Markdown lines with severity counts and the applicable-suggestion count."""
    if not inline:
        return []
    severity_counts: dict[str, int] = {}
    for c in inline:
        severity_counts[c["severity"]] = severity_counts.get(c["severity"], 0) + 1

    lines = [f"\n### Issues Found ({len(inline)} total)"]
    for sev in SEVERITY_ORDER:
        if sev in severity_counts:
            icon = SEVERITY_ICONS.get(sev, "")
            lines.append(f"- {icon} {sev.capitalize()}: {severity_counts[sev]}")
    fixes = sum(1 for c in inline if c.get("fix"))
    if fixes:
        lines.append(f"- \U0001f6e0️ With applicable suggestion: {fixes} ({apply_hint})")
    lines.append("\n*See inline comments on the diff for details.*")
    return lines


def _keep_fix_prompt(failed: int, unanchored: list[dict[str, Any]]) -> bool:
    """Whether the fix prompt may stay: it must not cover findings nobody can see.

    Those are the failed posts and the findings left out of the capped list of unanchored ones.
    """
    return not failed and len(unanchored) <= _GITHUB_LISTED_FINDINGS


def _footer_facts() -> str:
    """Footer text naming the harness, model and effort of the run."""
    extras = f" • Harness: {_detect_harness()}"
    model = os.environ.get("AGENT_MODEL")
    if model:
        extras += f" • Model: {model}"
    # agentic-ci exports the effective effort (including its default) next to
    # AGENT_MODEL; it is unset when the effort is "none".
    effort = os.environ.get("AGENT_REASONING_EFFORT")
    if effort:
        extras += f" • Effort: {effort}"
    return extras


# ===========================================================================
# GitHub backend
# ===========================================================================

# GitHub rejects issue and review comment bodies above this many characters.
_GITHUB_BODY_LIMIT = 65536
_ZWSP = "\u200b"
# Past this many, the rest of the findings are listed in the summary instead.
MAX_INLINE_COMMENTS = 50
# The summary lists the findings that were not posted inline, capped at this many. When the body
# would not fit in full, each finding is cut to the character limit as well, so that the list
# leaves room in the body for the rest of the summary.
_GITHUB_LISTED_FINDING_CHARS = 500
_GITHUB_LISTED_FINDINGS = 100

# The workflow token posts as this bot. AI_REVIEW_BOT_LOGIN names another posting account
# (a GitHub App or a token owner); comments of this bot still count as ours after a switch.
_GITHUB_BOT_REST_LOGIN = "github-actions[bot]"
_GITHUB_BOT_GRAPHQL_LOGIN = "github-actions"
_GITHUB_BOT_LOGIN_ENV = "AI_REVIEW_BOT_LOGIN"
# The App slug charset is not documented; every observed slug is lowercase alphanumerics and
# hyphens. Logins are compared for equality, so a permissive pattern cannot cause a false match.
_GITHUB_APP_LOGIN_RE = re.compile(r"[a-z0-9][a-z0-9_-]{0,62}\[bot\]")
_GITHUB_USER_LOGIN_RE = re.compile(r"[A-Za-z0-9](?:[A-Za-z0-9]|-(?=[A-Za-z0-9])){0,38}")

# Our comments end with the marker; one earlier in the body (a fence, a path)
# never counts.
_GITHUB_TRAILING_MARKER_RE = re.compile(r"<!-- ai-review(?: sha:([0-9a-f]+))? -->\Z")

_CONTROL_CHARS_RE = re.compile(r"[\x00-\x1f\x7f]")
_GITHUB_REPO_RE = re.compile(r"^[A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+$")
_GITHUB_SHA_RE = re.compile(r"^[0-9a-f]{40,64}$")
_GITHUB_NEXT_LINK_RE = re.compile(r'<([^>]+)>\s*;\s*rel="next"')

# A backtick fence's info string may not contain a backtick.
_FENCE_OPEN_RE = re.compile(r"^( {0,3})(`{3,}(?=[^`]*$)|~{3,})")
_FENCE_CLOSE_RE = re.compile(r"^ {0,3}(`{3,}|~{3,})[ \t]*$")
_MENTION_RE = re.compile(r"(?<![A-Za-z0-9])(@|&#0*64;|&#[xX]0*40;|&commat;)(?=[A-Za-z0-9])")

# The deep-review recommendation reads panel.py triage output. Paths come from the
# pull request, so each is bounded and the number of categories shown is capped.
_PLAN_MAX_BYTES = 1024 * 1024
# DOTALL: a path may hold a newline, and its category still comes last.
_EVIDENCE_RE = re.compile(r"^(.*?) \(([^()]*)\)$", re.DOTALL)
_EVIDENCE_MORE_RE = re.compile(r"^and (\d{1,9}) more$")
_REASON_PATH_CHARS = 160
_REASON_CATEGORY_CHARS = 60
_REASON_CATEGORIES = 10

# Stand-down: a standard review gives way to a deep review of the same head, and a deep
# review gives way to a newer head. Each check makes at most _STANDDOWN_MAX_CALLS GETs,
# every page included: one page of runs (the newest 100 for the head) and at most two
# pages of jobs per run. Every lookup failure lets the review go ahead.
_STANDDOWN_EXIT = 10
_STANDDOWN_MAX_CALLS = 20
_STANDDOWN_RUN_PAGES = 1
_STANDDOWN_JOB_PAGES = 2
_STANDDOWN_PER_PAGE = 100
_GITHUB_DEEP_JOB_DEFAULT = "deep-review"
_GITHUB_DEEP_JOB_CHARS = 200
# A deep review job in one of these states has started or is about to. A completed one
# counts only when it succeeded: one that failed, was cancelled or skipped posted nothing.
_GITHUB_JOB_STARTED = frozenset({"queued", "in_progress", "waiting", "requested", "pending"})

_GITHUB_THREADS_QUERY = """
query($owner: String!, $name: String!, $number: Int!, $after: String) {
  repository(owner: $owner, name: $name) {
    pullRequest(number: $number) {
      reviewThreads(first: 100, after: $after) {
        pageInfo { hasNextPage endCursor }
        nodes {
          isResolved
          isOutdated
          path
          line
          startLine
          comments(first: 100) {
            pageInfo { hasNextPage }
            nodes { fullDatabaseId author { __typename login } body }
          }
        }
      }
    }
  }
}
"""


class _GitHubContext(NamedTuple):
    api_url: str
    graphql_url: str
    token: str
    owner: str
    repo: str
    number: int
    head_sha: str
    run_url: str | None
    # From the event payload, None when absent or malformed; nothing posts with them.
    base_sha: str | None = None
    author_login: str | None = None

    @property
    def repo_url(self) -> str:
        return f"{self.api_url}/repos/{self.owner}/{self.repo}"


class _GitHubIdentity(NamedTuple):
    """How one posting account appears as a REST ``user`` and as a GraphQL ``author``."""

    rest_login: str
    graphql_login: str
    kind: str  # REST ``type`` and GraphQL ``__typename``: "Bot" or "User"

    def _is_login(self, login: Any, expected: str) -> bool:
        if not isinstance(login, str):
            return False
        # GitHub user logins are case-insensitive and ASCII; bot logins are compared exactly.
        if self.kind == "User":
            return login.isascii() and login.lower() == expected.lower()
        return login == expected

    def wrote_rest(self, user: Any) -> bool:
        return (
            isinstance(user, dict)
            and user.get("type") == self.kind
            and self._is_login(user.get("login"), self.rest_login)
        )

    def wrote_graphql(self, author: Any) -> bool:
        # A deleted account comes back as a null author, which counts as human.
        return (
            isinstance(author, dict)
            and author.get("__typename") == self.kind
            and self._is_login(author.get("login"), self.graphql_login)
        )


_GITHUB_ACTIONS_IDENTITY = _GitHubIdentity(_GITHUB_BOT_REST_LOGIN, _GITHUB_BOT_GRAPHQL_LOGIN, "Bot")


def _github_configured_identity() -> _GitHubIdentity | None:
    """Return the account named by AI_REVIEW_BOT_LOGIN, or None for the workflow bot.

    ``<slug>[bot]`` is a GitHub App, which GraphQL names without the suffix. A
    plain login is the user behind a personal access token. Raises ValueError
    on anything else.
    """
    login = os.environ.get(_GITHUB_BOT_LOGIN_ENV, "")
    if not login or login == _GITHUB_BOT_REST_LOGIN:
        return None
    if _GITHUB_APP_LOGIN_RE.fullmatch(login):
        return _GitHubIdentity(login, login[: -len("[bot]")], "Bot")
    if _GITHUB_USER_LOGIN_RE.fullmatch(login):
        return _GitHubIdentity(login, login, "User")
    raise ValueError(
        f"{_GITHUB_BOT_LOGIN_ENV} must be a GitHub App bot login like my-app[bot] "
        f"or a GitHub user login, got: {login[:80]!r}"
    )


def _github_identities() -> tuple[_GitHubIdentity, tuple[_GitHubIdentity, ...]]:
    """Return the account that posts now, and every account whose comments are ours.

    Comments of the workflow bot stay ours after a switch to another account,
    so its summaries and threads from earlier runs are still recognized.
    """
    try:
        current = _github_configured_identity() or _GITHUB_ACTIONS_IDENTITY
    except ValueError:
        # _github_load_context exits on a bad value before any request; never widen on one.
        current = _GITHUB_ACTIONS_IDENTITY
    if current == _GITHUB_ACTIONS_IDENTITY:
        return current, (current,)
    return current, (current, _GITHUB_ACTIONS_IDENTITY)


def _github_annotation(level: str, msg: str) -> None:
    """Emit a GitHub Actions workflow command (notice, warning or error)."""
    data = msg.replace("%", "%25").replace("\r", "%0D").replace("\n", "%0A")
    print(f"::{level}::{data}")


def _github_load_context() -> _GitHubContext:
    """Read the token, API URLs and pull request from the Actions environment.

    Exits 1 on anything missing or unsafe, the way ``_gitlab_post`` does.
    """
    token = os.environ.get("GITHUB_TOKEN")
    if not token:
        _error("GITHUB_TOKEN environment variable is required")
        sys.exit(1)
    try:
        _github_configured_identity()
    except ValueError as e:
        _error(str(e))
        sys.exit(1)

    api_url = (os.environ.get("GITHUB_API_URL") or "https://api.github.com").rstrip("/")
    api = urlparse(api_url)
    if api.scheme != "https" or not api.hostname:
        _error(f"GITHUB_API_URL must be an https URL, got: {api_url}")
        sys.exit(1)
    graphql_url = os.environ.get("GITHUB_GRAPHQL_URL") or f"{api_url}/graphql"
    graphql = urlparse(graphql_url)
    if graphql.scheme != "https" or graphql.netloc != api.netloc:
        _error(f"GITHUB_GRAPHQL_URL must be an https URL on {api.netloc}, got: {graphql_url}")
        sys.exit(1)

    repository = os.environ.get("GITHUB_REPOSITORY", "")
    if not _GITHUB_REPO_RE.match(repository):
        _error(f"GITHUB_REPOSITORY must look like owner/repo, got: {repository!r}")
        sys.exit(1)
    owner, repo = repository.split("/")

    event_path = os.environ.get("GITHUB_EVENT_PATH")
    if not event_path:
        _error("GITHUB_EVENT_PATH environment variable is required")
        sys.exit(1)
    try:
        event = json.loads(Path(event_path).read_text(encoding="utf-8"))
    except (OSError, ValueError) as e:
        _error(f"Cannot read the event payload at GITHUB_EVENT_PATH: {e}")
        sys.exit(1)
    pull_request = event.get("pull_request") if isinstance(event, dict) else None
    if not isinstance(pull_request, dict):
        _error("GITHUB_EVENT_PATH does not hold a pull_request event payload")
        sys.exit(1)

    number = pull_request.get("number")
    if not isinstance(number, int) or isinstance(number, bool) or number <= 0:
        _error(f"pull_request.number: expected positive int, got {number!r}")
        sys.exit(1)
    head = pull_request.get("head")
    head_sha = head.get("sha") if isinstance(head, dict) else None
    if not isinstance(head_sha, str) or not _GITHUB_SHA_RE.match(head_sha):
        _error(f"pull_request.head.sha: expected a hex commit SHA, got {head_sha!r}")
        sys.exit(1)
    base = pull_request.get("base")
    base_sha = base.get("sha") if isinstance(base, dict) else None
    if not isinstance(base_sha, str) or not _GITHUB_SHA_RE.match(base_sha):
        base_sha = None
    # A pull request is opened by a user or by a GitHub App (<slug>[bot]).
    author = pull_request.get("user")
    author_login = author.get("login") if isinstance(author, dict) else None
    if not isinstance(author_login, str) or not any(
        pattern.fullmatch(author_login) for pattern in (_GITHUB_USER_LOGIN_RE, _GITHUB_APP_LOGIN_RE)
    ):
        author_login = None

    server_url = os.environ.get("GITHUB_SERVER_URL", "").rstrip("/")
    run_id = os.environ.get("GITHUB_RUN_ID", "")
    run_url = None
    if server_url.startswith("https://") and run_id.isdigit():
        run_url = f"{server_url}/{repository}/actions/runs/{run_id}"

    return _GitHubContext(
        api_url=api_url,
        graphql_url=graphql_url,
        token=token,
        owner=owner,
        repo=repo,
        number=number,
        head_sha=head_sha,
        run_url=run_url,
        base_sha=base_sha,
        author_login=author_login,
    )


def _github_paginated_get(
    session: requests.Session, ctx: _GitHubContext, url: str, *, strict: bool = False
) -> list[Any]:
    """Fetch all pages from a GitHub list endpoint.

    Follows ``Link: rel="next"`` only when it stays on the API host over
    https, so the token never goes anywhere else. Raises requests.HTTPError
    when the first page fails, or any page when ``strict`` is set: a caller
    that looks for the newest item cannot work from a truncated list.
    """
    api_netloc = urlparse(ctx.api_url).netloc
    results: list[Any] = []
    params: dict[str, int] | None = {"per_page": 100}
    first_page = True
    while True:
        resp = session.get(url, params=params)
        params = None
        if resp.status_code != 200:
            if first_page or strict:
                # raise_for_status() lets a redirect through, and redirects are never followed.
                raise requests.exceptions.HTTPError(f"GitHub API returned HTTP {resp.status_code}")
            _warning(f"GitHub API returned HTTP {resp.status_code} during pagination")
            break
        results.extend(resp.json())
        first_page = False
        m = _GITHUB_NEXT_LINK_RE.search(resp.headers.get("Link", ""))
        if not m:
            break
        next_url = urlparse(m.group(1))
        if next_url.scheme != "https" or next_url.netloc != api_netloc:
            _warning(f"Ignoring pagination link to {next_url.netloc or m.group(1)}")
            break
        url = m.group(1)
    return results


def _github_rest_is_ours(user: Any) -> bool:
    return any(identity.wrote_rest(user) for identity in _github_identities()[1])


def _github_graphql_is_ours(author: Any) -> bool:
    return any(identity.wrote_graphql(author) for identity in _github_identities()[1])


def _github_comment_is_ours(user: Any) -> bool:
    """Whether a REST issue or review comment author is one of our posting accounts.

    Callers still check that the body ends with their own marker.
    """
    return _github_rest_is_ours(user)


def _github_trailing_marker(body: Any) -> re.Match[str] | None:
    return _GITHUB_TRAILING_MARKER_RE.search(body.strip()) if isinstance(body, str) else None


def _github_find_summary(
    session: requests.Session, ctx: _GitHubContext
) -> tuple[int | None, str | None]:
    """Return the id of the summary to update and the review SHA to dedup against.

    Lists sort ascending, so the last match is the newest. A comment is ours
    only when one of our accounts wrote it and its body ends with the marker.
    The SHA comes from our newest summary, whichever account wrote it. The id
    is that of the newest summary the current account wrote: editing one of
    another account would keep that account as its author.
    """
    current, known = _github_identities()
    comment_id: int | None = None
    sha: str | None = None
    url = f"{ctx.repo_url}/issues/{ctx.number}/comments"
    for comment in _github_paginated_get(session, ctx, url, strict=True):
        marker = _github_trailing_marker(comment.get("body"))
        user = comment.get("user")
        if not marker or not any(identity.wrote_rest(user) for identity in known):
            continue
        sha = marker.group(1)
        if current.wrote_rest(user):
            comment_id = comment["id"]
    return comment_id, sha


def _github_thread_covers_dropped(thread: dict[str, Any], dropped: set[tuple[str, int]]) -> bool:
    """Whether a dropped finding sits on the thread's line or inside its multi-line range."""
    path, line = thread.get("path"), thread.get("line")
    if (path, line) in dropped:
        return True
    start = thread.get("startLine")
    if not isinstance(line, int) or not isinstance(start, int) or start > line:
        return False
    return any(p == path and start <= n <= line for p, n in dropped)


def _github_thread_ours_unanswered(thread: Any) -> list[Any] | None:
    """Return the comment nodes of an unresolved thread that only we wrote, else None."""
    if not isinstance(thread, dict) or thread.get("isResolved"):
        return None
    comments = thread.get("comments") or {}
    if (comments.get("pageInfo") or {}).get("hasNextPage"):
        return None
    nodes = comments.get("nodes") or []
    if not nodes:
        return None
    for node in nodes:
        if not isinstance(node, dict):
            return None
        if not (
            _github_graphql_is_ours(node.get("author"))
            and _github_trailing_marker(node.get("body"))
        ):
            return None
    return nodes


def _github_thread_is_protected(thread: Any, dropped: set[tuple[str, int]]) -> bool:
    """Whether cleanup keeps our thread only because a dropped finding still sits on it."""
    return (
        _github_thread_ours_unanswered(thread) is not None
        and not thread.get("isOutdated")
        and _github_thread_covers_dropped(thread, dropped)
    )


def _github_thread_is_carried(thread: Any, dropped: set[tuple[str, int]]) -> bool:
    """Whether our thread is still open on a dropped finding, whoever replied to it."""
    if not isinstance(thread, dict) or thread.get("isResolved") or thread.get("isOutdated"):
        return False
    nodes = (thread.get("comments") or {}).get("nodes") or []
    root = nodes[0] if nodes else None
    return (
        isinstance(root, dict)
        and _github_graphql_is_ours(root.get("author"))
        and bool(_github_trailing_marker(root.get("body")))
        and _github_thread_covers_dropped(thread, dropped)
    )


def _github_thread_root_to_delete(thread: Any, dropped: set[tuple[str, int]]) -> int | None:
    """Return the root comment id of a review thread that is safe to delete, else None.

    Safe means unresolved, written only by us (no human reply), and not a
    still-valid finding on unchanged code that dedup just dropped from this run.
    """
    nodes = _github_thread_ours_unanswered(thread)
    if nodes is None or _github_thread_is_protected(thread, dropped):
        return None
    # fullDatabaseId is a BigInt and arrives as a decimal string. databaseId is deprecated
    # because it cannot hold 64-bit ids.
    root_id = nodes[0].get("fullDatabaseId")
    if isinstance(root_id, str) and root_id.isascii() and root_id.isdigit() and len(root_id) <= 19:
        value = int(root_id)
        if 0 < value < 2**63:
            return value
    return None


def _github_fetch_review_threads(
    session: requests.Session, ctx: _GitHubContext
) -> list[Any] | None:
    """Return every review thread of the PR, or None when GraphQL did not answer cleanly."""
    threads: list[Any] = []
    after: str | None = None
    while True:
        resp = session.post(
            ctx.graphql_url,
            json={
                "query": _GITHUB_THREADS_QUERY,
                "variables": {
                    "owner": ctx.owner,
                    "name": ctx.repo,
                    "number": ctx.number,
                    "after": after,
                },
            },
        )
        if resp.status_code != 200:
            _github_annotation(
                "warning", f"Review thread query failed (HTTP {resp.status_code}); skipping cleanup"
            )
            return None
        try:
            payload = resp.json()
        except ValueError:
            payload = None
        data = payload.get("data") if isinstance(payload, dict) else None
        if not isinstance(payload, dict) or payload.get("errors") or not data:
            _github_annotation(
                "warning", "Review thread query returned errors or no data; skipping cleanup"
            )
            return None
        pull_request = (data.get("repository") or {}).get("pullRequest")
        connection = (pull_request or {}).get("reviewThreads")
        if not isinstance(connection, dict):
            _github_annotation(
                "warning", "Review thread query found no pull request; skipping cleanup"
            )
            return None
        threads.extend(connection.get("nodes") or [])
        page_info = connection.get("pageInfo") or {}
        if not page_info.get("hasNextPage"):
            return threads
        after = page_info.get("endCursor")


def _github_delete_previous_threads(
    session: requests.Session,
    ctx: _GitHubContext,
    dropped: set[tuple[str, int]],
    *,
    verbose: bool = False,
) -> int:
    """Delete unresolved AI review threads without human responses.

    Returns how many of our threads, replied to or not, are still open on a
    dropped finding, or 0 when the cleanup was skipped.
    """
    _step("Checking for previous AI review threads to delete...")
    threads = _github_fetch_review_threads(session, ctx)
    if threads is None:
        return 0

    protected = sum(1 for t in threads if _github_thread_is_carried(t, dropped))
    to_delete = [
        root_id
        for root_id in (_github_thread_root_to_delete(t, dropped) for t in threads)
        if root_id is not None
    ]
    kept = len(threads) - len(to_delete)
    if not to_delete:
        _step(f"No previous AI review threads to delete ({kept} thread(s) kept)")
        return protected

    _step(f"Deleting {len(to_delete)} previous AI review thread(s)...")
    deleted = 0
    for comment_id in to_delete:
        resp = session.delete(f"{ctx.repo_url}/pulls/comments/{comment_id}")
        if 200 <= resp.status_code < 300:
            deleted += 1
        else:
            _warning(f"Failed to delete review comment {comment_id} (HTTP {resp.status_code})")
            if verbose:
                print(resp.text, file=sys.stderr)
    _success(f"Deleted {deleted}/{len(to_delete)} previous thread(s)")
    return protected


# ---------------------------------------------------------------------------
# Stand-down
# ---------------------------------------------------------------------------


class _GitHubLookupError(Exception):
    """A stand-down lookup that did not answer cleanly. The message names the cause only."""


class _GitHubBudgetError(Exception):
    """A stand-down check used up its calls before it found an answer."""


class _GitHubBudget:
    def __init__(self, calls: int) -> None:
        self.remaining = calls

    def spend(self) -> None:
        if self.remaining <= 0:
            raise _GitHubBudgetError
        self.remaining -= 1


def _github_wrapped_pages(
    session: requests.Session,
    ctx: _GitHubContext,
    url: str,
    key: str,
    params: dict[str, Any],
    *,
    max_pages: int,
    budget: _GitHubBudget,
) -> Iterator[tuple[list[Any], bool]]:
    """Yield the list under ``key`` from each page of a wrapped list such as workflow runs.

    Follows ``Link: rel="next"`` only over https on the API host, as
    _github_paginated_get does. The path is not pinned: these next links use
    ``/repositories/{id}/...``. The flag is True when the page cap left a next
    page unread. Raises _GitHubLookupError on any failure, after yielding the
    pages read so far, and _GitHubBudgetError before a GET the budget cannot pay.
    """
    api_netloc = urlparse(ctx.api_url).netloc
    request_params: dict[str, Any] | None = params
    for page in range(1, max_pages + 1):
        budget.spend()
        try:
            resp = session.get(url, params=request_params)
        except requests.exceptions.RequestException as e:
            raise _GitHubLookupError(type(e).__name__) from None
        request_params = None
        if resp.status_code != 200:
            raise _GitHubLookupError(f"HTTP {resp.status_code}")
        try:
            payload = resp.json()
        except ValueError:
            raise _GitHubLookupError("invalid JSON") from None
        items = payload.get(key) if isinstance(payload, dict) else None
        if not isinstance(items, list):
            raise _GitHubLookupError(f"no {key} list")
        m = _GITHUB_NEXT_LINK_RE.search(resp.headers.get("Link", ""))
        if not m or page == max_pages:
            yield items, bool(m)
            return
        yield items, False
        next_url = urlparse(m.group(1))
        if next_url.scheme != "https" or next_url.netloc != api_netloc:
            raise _GitHubLookupError("pagination link off the API host")
        url = m.group(1)


def _github_deep_job_matches(name: Any, deep_job: str) -> bool:
    """Whether a job name from the jobs API is the deep review job ``deep_job``.

    ``deep_job`` is the job's display name: its ``name:``, else its key. A
    matrix job shows as ``J (value)``, and a job of a called reusable workflow
    as ``J / callee``. The comparison is exact: a suffix rule would let
    ``review`` match the standard job ``review / review``.
    """
    if not isinstance(name, str) or not deep_job:
        return False
    if name == deep_job or name.startswith(deep_job + " / "):
        return True
    first = name.split(" / ", 1)[0]
    return first.startswith(deep_job + " (") and first.endswith(")")


def _github_runs_to_check(runs: list[Any], ctx: _GitHubContext) -> list[dict[str, Any]]:
    """Return the runs of this PR head that may hold a deep review job, unfinished first."""
    candidates = []
    for run in runs:
        if not isinstance(run, dict):
            continue
        run_id = run.get("id")
        if not isinstance(run_id, int) or isinstance(run_id, bool) or run_id <= 0:
            continue
        if run.get("head_sha") != ctx.head_sha or run.get("event") != "pull_request":
            continue
        # Every job of such a run was skipped, so it has no deep review to find.
        if run.get("status") == "completed" and run.get("conclusion") == "skipped":
            continue
        # The list is empty for forks and closed PRs, so it can only rule a run out:
        # another open PR from a branch with the same head.
        prs = run.get("pull_requests")
        if (
            isinstance(prs, list)
            and prs
            and not any(isinstance(p, dict) and p.get("number") == ctx.number for p in prs)
        ):
            continue
        candidates.append(run)
    # sorted() is stable, so each group keeps the API's newest-first order.
    return sorted(candidates, key=lambda r: r.get("status") == "completed")


def _github_find_deep_review(
    session: requests.Session, ctx: _GitHubContext, deep_job: str
) -> dict[str, Any] | None:
    """Return a deep review job for this head that started or succeeded, else None.

    The current run counts, since a push can start both reviews in one run.
    Only the job running this check is skipped: same run and same runner, so a
    deep job name that also matches the standard job cannot block it forever.
    """
    budget = _GitHubBudget(_STANDDOWN_MAX_CALLS)
    own_run = os.environ.get("GITHUB_RUN_ID", "")
    own_runner = os.environ.get("RUNNER_NAME", "")
    runs: list[Any] = []
    params = {"head_sha": ctx.head_sha, "event": "pull_request", "per_page": _STANDDOWN_PER_PAGE}
    for items, more in _github_wrapped_pages(
        session,
        ctx,
        f"{ctx.repo_url}/actions/runs",
        "workflow_runs",
        params,
        max_pages=_STANDDOWN_RUN_PAGES,
        budget=budget,
    ):
        runs.extend(items)
        if more:
            _github_annotation(
                "warning",
                f"More than {len(runs)} workflow runs for {ctx.head_sha[:8]}; "
                f"checked the newest {len(runs)} for a deep review.",
            )

    for run in _github_runs_to_check(runs, ctx):
        own = bool(own_runner) and own_run.isdigit() and int(own_run) == run["id"]
        for items, more in _github_wrapped_pages(
            session,
            ctx,
            f"{ctx.repo_url}/actions/runs/{run['id']}/jobs",
            "jobs",
            {"filter": "latest", "per_page": _STANDDOWN_PER_PAGE},
            max_pages=_STANDDOWN_JOB_PAGES,
            budget=budget,
        ):
            for job in items:
                if not isinstance(job, dict):
                    continue
                if own and job.get("runner_name") == own_runner:
                    continue
                if not _github_deep_job_matches(job.get("name"), deep_job):
                    continue
                if job.get("head_sha") and job.get("head_sha") != ctx.head_sha:
                    continue
                status = job.get("status")
                if status in _GITHUB_JOB_STARTED or (
                    status == "completed" and job.get("conclusion") == "success"
                ):
                    return job
            if more:
                _github_annotation(
                    "warning",
                    f"Run {run['id']} has more jobs than the stand-down check reads; "
                    "checked the first pages only.",
                )
    return None


def _github_head_moved(session: requests.Session, ctx: _GitHubContext) -> bool:
    """Deep mode: whether the PR has a newer head than the one this run reviewed."""
    reason = None
    current = None
    try:
        resp = session.get(f"{ctx.repo_url}/pulls/{ctx.number}")
        if resp.status_code != 200:
            reason = f"HTTP {resp.status_code}"
        else:
            payload = resp.json()
            head = payload.get("head") if isinstance(payload, dict) else None
            current = head.get("sha") if isinstance(head, dict) else None
            if not isinstance(current, str) or not _GITHUB_SHA_RE.match(current):
                reason = "no head SHA"
    except requests.exceptions.RequestException as e:
        reason = type(e).__name__
    except ValueError:
        reason = "invalid JSON"
    if reason is not None or current is None:
        _github_annotation(
            "warning",
            f"Could not check whether PR #{ctx.number} has a newer head ({reason}); "
            "continuing without the check.",
        )
        return False
    if current == ctx.head_sha:
        return False
    _github_annotation(
        "notice",
        f"PR #{ctx.number} moved to {current[:8]} since this run started "
        f"({ctx.head_sha[:8]}); the deep review stands down.",
    )
    return True


def _github_stands_down(
    session: requests.Session, ctx: _GitHubContext, mode: str, deep_job: str
) -> bool:
    """Whether a review that should win makes this one redundant. Never writes.

    Standard mode gives way to a deep review job of the same head that started
    or succeeded. Deep mode gives way to a newer head. A lookup that fails
    warns once and lets the review go ahead.
    """
    if mode == "deep":
        return _github_head_moved(session, ctx)
    try:
        job = _github_find_deep_review(session, ctx, deep_job)
    except _GitHubLookupError as e:
        _github_annotation(
            "warning",
            f"Could not check for a deep review ({e}); grant the job 'actions: read' "
            "so the standard review can stand down for one. Continuing without the check.",
        )
        return False
    except _GitHubBudgetError:
        _github_annotation(
            "warning",
            f"The check for a deep review stopped after {_STANDDOWN_MAX_CALLS} API calls; "
            "continuing without it.",
        )
        return False
    if job is None:
        return False
    name = _CONTROL_CHARS_RE.sub("", job["name"])[:_GITHUB_DEEP_JOB_CHARS]
    _github_annotation(
        "notice",
        f"{name} ({job.get('status')}) is running or has posted for {ctx.head_sha[:8]}; "
        "the standard review stands down.",
    )
    return True


def _github_deep_job(value: str | None) -> str:
    """Return the deep review job name to match, exiting 1 on an unusable one."""
    if value is None:
        return _GITHUB_DEEP_JOB_DEFAULT
    if not value or len(value) > _GITHUB_DEEP_JOB_CHARS or _CONTROL_CHARS_RE.search(value):
        _error(
            f"--deep-job must be a job name of 1 to {_GITHUB_DEEP_JOB_CHARS} characters "
            "without control characters"
        )
        sys.exit(1)
    return value


def _github_session(ctx: _GitHubContext) -> requests.Session:
    session = _SafeSession()
    session.headers.update(
        {
            "Authorization": f"Bearer {ctx.token}",
            "Accept": "application/vnd.github+json",
            "X-GitHub-Api-Version": "2022-11-28",
        }
    )
    return session


def cmd_stand_down(args: argparse.Namespace) -> None:
    """Exit 10 when the review should stand down, 0 when it should go ahead."""
    ctx = _github_load_context()
    deep_job = _github_deep_job(args.deep_job)
    if _github_stands_down(_github_session(ctx), ctx, args.mode, deep_job):
        sys.exit(_STANDDOWN_EXIT)


def _scan_fences(text: str) -> tuple[list[tuple[str, bool]], str | None]:
    """Split ``text`` into lines flagged as inside a fenced block (fences included).

    Also returns a closer for the fence still open at the end, if any. The
    closer keeps the opener's indentation, so a fence opened inside a list
    item closes there instead of opening a new top-level fence.
    """
    lines: list[tuple[str, bool]] = []
    fence: str | None = None
    indent = ""
    for line in text.split("\n"):
        if fence is None:
            opened = _FENCE_OPEN_RE.match(line)
            if opened:
                indent, fence = opened.group(1), opened.group(2)
            lines.append((line, bool(opened)))
            continue
        lines.append((line, True))
        closed = _FENCE_CLOSE_RE.match(line)
        if closed and closed.group(1)[0] == fence[0] and len(closed.group(1)) >= len(fence):
            fence = None
    return lines, (f"{indent}{fence}" if fence else None)


def _github_defuse(text: str) -> str:
    """Put a zero-width space after each ``<!--`` and each @ (or its HTML entity).

    Applies everywhere in model text, code spans and fences included. Finding
    code by regex is not reliable (HTML blocks, spans across lines, list
    indentation), and a missed span leaves a live mention. A zero-width space
    in a code sample costs only copy-paste fidelity.
    """
    text = text.replace("<!--", f"<{_ZWSP}!--")
    return _MENTION_RE.sub(lambda m: m.group(1) + _ZWSP, text)


def _github_text(text: str) -> str:
    """Make model-written text safe to post as one fragment of a GitHub comment.

    ``<!--`` is defused because the ownership marker is matched on the raw
    text, and mentions (``@user``, ``@org/team``) so model text cannot ping
    anyone. A fence left open is closed, so it cannot swallow the next fragment.
    """
    return _github_close_fence(_github_defuse(text))


def _github_close_fence(text: str) -> str:
    """Close a fence left open at the end of ``text``."""
    _, open_fence = _scan_fences(text)
    return f"{text}\n{open_fence}" if open_fence else text


def _longest_backtick_run(text: str) -> int:
    return max((len(run) for run in re.findall(r"`+", text)), default=0)


def _github_code_span(text: str) -> str:
    """Render ``text`` as an inline code span whose delimiter outlasts any backtick run in it."""
    text = " ".join(text.split())
    delimiter = "`" * (_longest_backtick_run(text) + 1)
    return f"{delimiter} {text} {delimiter}"


def _github_reason_text(value: str) -> str:
    """Flatten ``value`` to one line without backticks, control or format characters.

    Format characters (category Cf, such as bidi overrides and zero-width
    marks) are invisible, so they could make a path read differently than it is.
    """
    value = "".join(ch for ch in value if unicodedata.category(ch) != "Cf")
    return " ".join(_CONTROL_CHARS_RE.sub(" ", value.replace("`", "")).split())


def _github_reason_path(value: str) -> str:
    """Render a path from the pull request as an inert, bounded code span."""
    cleaned = _github_reason_text(value)
    cleaned = cleaned[:_REASON_PATH_CHARS]
    return f"`{cleaned}`" if cleaned else "(unnamed path)"


def _github_reason_summary(evidence: list[str]) -> str:
    """Show one path per evidence category and count the remaining paths.

    Evidence entries look like ``path (category)`` or ``and N more (category)``,
    as panel.py triage writes them.
    """
    grouped: dict[str, tuple[str | None, int]] = {}
    for reason in evidence:
        match = _EVIDENCE_RE.fullmatch(reason)
        label, category = match.groups() if match else (reason, "changes")
        example, count = grouped.get(category, (None, 0))
        more = _EVIDENCE_MORE_RE.fullmatch(label)
        if more:
            count += int(more.group(1))
        else:
            example = example or label
            count += 1
        grouped[category] = (example, count)
    parts = []
    for category, (example, count) in list(grouped.items())[:_REASON_CATEGORIES]:
        name = _github_reason_text(category)
        name = html.escape(name[:_REASON_CATEGORY_CHARS], quote=False) or "changes"
        path = f": {_github_reason_path(example)}" if example is not None else ""
        extra = f" and {count - 1} more" if example is not None and count > 1 else ""
        parts.append(f"{name}{path}{extra}")
    if len(grouped) > _REASON_CATEGORIES:
        parts.append(f"and {len(grouped) - _REASON_CATEGORIES} more categories")
    return "; ".join(parts)


def _github_read_plan(path: str) -> dict[str, Any] | None:
    """Read a triage plan file, or warn and return None when it is unusable."""
    flags = os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0) | getattr(os, "O_NONBLOCK", 0)
    try:
        fd = os.open(path, flags)
    except OSError as e:
        reason = "does not exist" if isinstance(e, FileNotFoundError) else "is unreadable"
        _github_annotation("warning", f"No deep-review recommendation: the triage plan {reason}")
        return None
    try:
        info = os.fstat(fd)
        if not stat.S_ISREG(info.st_mode):
            reason = "is not a regular file"
        elif info.st_size > _PLAN_MAX_BYTES:
            reason = "is larger than 1 MiB"
        else:
            with os.fdopen(fd, "rb") as f:
                fd = -1
                raw = f.read(_PLAN_MAX_BYTES + 1)
            if len(raw) > _PLAN_MAX_BYTES:
                reason = "is larger than 1 MiB"
            else:
                try:
                    # json.loads would also take UTF-16 and UTF-32 bytes; panel.py writes UTF-8.
                    plan = json.loads(raw.decode("utf-8"))
                except (ValueError, RecursionError):
                    reason = "is not valid JSON"
                else:
                    if isinstance(plan, dict):
                        return plan
                    reason = "is not a JSON object"
    except OSError:
        reason = "is unreadable"
    finally:
        if fd >= 0:
            os.close(fd)
    _github_annotation("warning", f"No deep-review recommendation: the triage plan {reason}")
    return None


def _github_recommendation(path: str) -> str | None:
    """Build the deep-review recommendation line from a triage plan, or None.

    Only a plan whose tier is ``deep`` yields a line. Any other tier is silent;
    a file that cannot be used, or has the wrong shape, gets one warning.
    """
    plan = _github_read_plan(path)
    if plan is None:
        return None
    tier = plan.get("tier")
    name = tier.get("name") if isinstance(tier, dict) else None
    evidence = tier.get("evidence") if isinstance(tier, dict) else None
    if isinstance(name, str) and name != "deep":
        return None
    if (
        name != "deep"
        or not isinstance(evidence, list)
        or not all(isinstance(e, str) for e in evidence)
    ):
        _github_annotation(
            "warning", "No deep-review recommendation: the triage plan has an unexpected shape"
        )
        return None
    reasons = _github_reason_summary(evidence)
    reason_text = f"{reasons}. " if reasons else ""
    return (
        f"**Deep review recommended:** {reason_text}"
        "Add the `deep-review` label to this pull request to run it."
    )


def _github_cap(text: str, marker: str, limit: int = _GITHUB_BODY_LIMIT) -> str:
    """Return ``text`` plus the marker, truncated so the result fits in ``limit`` characters.

    The marker always survives, and a fenced block cut in half is closed again
    before the marker.
    """
    body = f"{text}\n\n{marker}"
    if len(body) <= limit:
        return body

    note = "\n\n*(truncated)*"
    keep = limit - len(marker) - len(note) - 2
    for _ in range(2):
        cut = text[:keep]
        _, open_fence = _scan_fences(cut)
        if open_fence is None:
            break
        keep -= len(open_fence) + 1
    cut = text[:keep]
    _, open_fence = _scan_fences(cut)
    if open_fence:
        cut += f"\n{open_fence}"
    return f"{cut}{note}\n\n{marker}"


def _github_format_fix_block(fix: dict[str, Any]) -> str:
    """Render ``fix`` as a GitHub ``suggestion`` fenced block.

    GitHub takes the replaced range from the comment's own ``start_line`` and
    ``line``, so the fence needs no offsets. ``_validate_fix`` already rejects
    code that contains a ``` fence.
    """
    code = fix["code"].rstrip("\n")
    if code:
        return f"```suggestion\n{code}\n```"
    return "```suggestion\n```"


def _github_comment_body(c: dict[str, Any], marker: str) -> str:
    """Build the inline comment body, dropping a fix that would not fit rather than cutting it."""
    icon = SEVERITY_ICONS.get(c["severity"], "\U0001f4ac")
    # Defuse the whole line: the severity prefix can turn a fence opener in the comment into prose.
    text = _github_text(f"{icon} **{c['severity'].capitalize()}**: {c['comment']}")
    fix = c.get("fix")
    if fix:
        with_fix = f"{text}\n\n{_github_format_fix_block(fix)}"
        if len(with_fix) + len(marker) + 2 <= _GITHUB_BODY_LIMIT:
            return f"{with_fix}\n\n{marker}"
        _warning(f"Dropping fix on {c['file']}:{c['line']}: comment is too large for GitHub")
        del c["fix"]
    return _github_cap(text, marker)


def _github_post_inline_comment(
    session: requests.Session,
    ctx: _GitHubContext,
    c: dict[str, Any],
    marker: str,
    *,
    verbose: bool = False,
) -> str:
    """Post one inline review comment.

    Returns "posted", "unanchored" (GitHub answered 422: the line is not in
    the diff) or "failed". A 422 on a comment with a fix retries once without
    the fix, anchored at the comment's own line.
    """
    url = f"{ctx.repo_url}/pulls/{ctx.number}/comments"
    while True:
        body = _github_comment_body(c, marker)
        fix = c.get("fix")
        payload: dict[str, Any] = {
            "body": body,
            "commit_id": ctx.head_sha,
            "path": c["file"],
            "side": "RIGHT",
            "line": c["line"],
        }
        if fix and fix["start_line"] != fix["end_line"]:
            payload["start_line"] = fix["start_line"]
            payload["start_side"] = "RIGHT"
            payload["line"] = fix["end_line"]

        resp = session.post(url, json=payload)
        if 200 <= resp.status_code < 300:
            return "posted"
        if resp.status_code == 422 and fix:
            _warning(
                f"GitHub rejected the suggestion on {c['file']}:{c['line']}; retrying as prose"
            )
            del c["fix"]
            continue
        if resp.status_code == 422:
            _warning(f"Cannot anchor comment on {c['file']}:{c['line']} (HTTP 422)")
            if verbose:
                print(resp.text, file=sys.stderr)
            return "unanchored"
        _warning(
            f"Failed to post inline comment on {c['file']}:{c['line']} (HTTP {resp.status_code})"
        )
        if verbose:
            print(resp.text, file=sys.stderr)
        return "failed"


def _github_build_summary_body(
    data: dict[str, Any],
    review_marker: str,
    run_url: str | None = None,
    unanchored: list[dict[str, Any]] | None = None,
    carried: int = 0,
    recommendation: str | None = None,
) -> str:
    """Build the markdown summary body for the PR comment.

    ``data["inline_comments"]`` holds the findings that were posted inline;
    ``unanchored`` holds the ones that were not posted inline; ``carried`` is
    the number of earlier threads still open on code that did not change.
    ``recommendation`` is a line placed directly under the heading.
    """
    parts: list[str] = ["## AI Code Review Summary\n"]
    if recommendation:
        parts.append(_github_defuse(recommendation) + "\n")
    parts.append(_github_text(str(data.get("summary") or "No summary provided.")))
    raw_positives = data.get("positive_aspects")
    if isinstance(raw_positives, str):
        raw_positives = [raw_positives]
    elif not isinstance(raw_positives, list):
        raw_positives = []
    positives = [str(p) for p in raw_positives]
    # Defuse each line after its list prefix is added, as GitHub renders it.
    parts.extend(_github_text(line) for line in _summary_positive_lines(positives))
    count_lines = _summary_count_lines(
        data.get("inline_comments", []),
        "use **Commit suggestion** on the inline comment",
    )
    if carried > 0:
        carried_line = f"- Still open from earlier runs, on code that did not change: {carried}"
        if count_lines:
            count_lines.insert(len(count_lines) - 1, carried_line)
        else:
            count_lines = ["\n### Issues Found", carried_line]
    parts.extend(count_lines)

    fix_prompt = data.get("fix_prompt")
    run = f" • Run: [{run_url}]({run_url})" if run_url else ""
    footer = (
        "\n\n---\n"
        "*Generated by [Code Review](https://github.com/opendatahub-io/code-review-skills)"
        f"{run}{_footer_facts()}*"
    )
    # Most severe first, so the cap leaves out the least severe findings.
    ranked = sorted(
        unanchored or [],
        key=lambda c: (SEVERITY_ORDER.index(c["severity"]), c["file"], c["line"]),
    )

    def assemble(max_chars: int | None) -> str:
        lines = list(parts)
        if ranked:
            lines.append("\n### Findings not posted inline")
            lines.append(
                "*These were not posted inline: GitHub could not anchor them to the diff, "
                f"or the run reached its limit of {MAX_INLINE_COMMENTS} inline comments.*\n"
            )
            for c in ranked[:_GITHUB_LISTED_FINDINGS]:
                path = c["file"].replace("<!--", f"<{_ZWSP}!--")
                where = f"{_github_code_span(path)}:{c['line']}"
                comment = c["comment"]
                if max_chars is not None and len(comment) > max_chars:
                    comment = comment[: max_chars - 1] + "\u2026"
                # The path sits in a code span built here, so only the comment is defused.
                # The fence check runs on the whole line: the prefix can make an opener prose.
                text = _github_close_fence(
                    f"- {where} ({c['severity']}): {_github_defuse(comment)}"
                )
                lines.append(text.replace("\n", "\n  "))
            if len(ranked) > _GITHUB_LISTED_FINDINGS:
                lines.append(f"- ... and {len(ranked) - _GITHUB_LISTED_FINDINGS} more not listed")

        if isinstance(fix_prompt, str) and fix_prompt:
            fence = "`" * max(4, _longest_backtick_run(fix_prompt) + 1)
            lines.append("\n### \U0001f916 Fix all issues with AI agents")
            lines.append(
                "\n**Copy-paste this into a prompt to automatically fix the issues found:**"
            )
            lines.append(f"\n{fence}\n{_github_defuse(fix_prompt)}\n{fence}")
        return "\n".join(lines) + footer

    text = assemble(None)
    if len(f"{text}\n\n{review_marker}") > _GITHUB_BODY_LIMIT:
        text = assemble(_GITHUB_LISTED_FINDING_CHARS)
    if len(f"{text}\n\n{review_marker}") > _GITHUB_BODY_LIMIT:
        _github_annotation(
            "warning", "The summary comment is over GitHub's size limit, so its end was cut"
        )
    return _github_cap(text, review_marker)


def _github_upsert_summary(
    session: requests.Session,
    ctx: _GitHubContext,
    body: str,
    existing_id: int | None,
    *,
    verbose: bool = False,
) -> bool:
    """Update our newest summary comment in place, or post the first one."""
    if existing_id is not None:
        _step(f"Updating summary comment (id: {existing_id})...")
        resp = session.patch(f"{ctx.repo_url}/issues/comments/{existing_id}", json={"body": body})
    else:
        _step(f"Posting summary comment to PR #{ctx.number}...")
        resp = session.post(f"{ctx.repo_url}/issues/{ctx.number}/comments", json={"body": body})
    if 200 <= resp.status_code < 300:
        return True

    _error(f"Failed to post summary comment (HTTP {resp.status_code})")
    if verbose:
        print(resp.text, file=sys.stderr)
    return False


def _github_post(args: argparse.Namespace) -> None:
    """Post review results to the GitHub pull request named by the Actions event."""
    ctx = _github_load_context()
    # Without --mode nothing checks for a review that should win, as before.
    mode = getattr(args, "mode", None)
    deep_job = _github_deep_job(getattr(args, "deep_job", None))
    verbose = _resolve_verbose(args)
    chill = _resolve_chill(args)
    inline_fixes = _resolve_inline_fixes(args)

    try:
        data = parse_review_json(args.review_file, chill_mode=chill, inline_fixes=inline_fixes)
    except (json.JSONDecodeError, ValueError) as e:
        _error(f"Failed to parse review JSON: {e}")
        sys.exit(1)

    plan_path = getattr(args, "deep_recommendation_plan", None)
    recommendation = _github_recommendation(plan_path) if plan_path is not None else None

    # A path with a control character is never in a diff, and printing it
    # would let the model start a workflow command in the log.
    clean = [c for c in data["inline_comments"] if not _CONTROL_CHARS_RE.search(c["file"])]
    if len(clean) != len(data["inline_comments"]):
        _warning(f"Dropped {len(data['inline_comments']) - len(clean)} comment(s): bad file path")
        data["inline_comments"] = clean

    session = _github_session(ctx)
    head_sha = ctx.head_sha
    review_marker = _make_review_marker(head_sha)

    try:
        pr_resp = session.get(f"{ctx.repo_url}/pulls/{ctx.number}")
        pr_resp.raise_for_status()
        current_head = (pr_resp.json().get("head") or {}).get("sha")
        if not current_head:
            _error("GitHub returned no head SHA for the pull request")
            sys.exit(1)
        if current_head != head_sha:
            _github_annotation(
                "notice",
                f"PR #{ctx.number} moved to {current_head[:8]} since this run started "
                f"({head_sha[:8]}); a newer run owns the review, so nothing was posted.",
            )
            return

        summary_id, prev_sha = _github_find_summary(session, ctx)
        original = list(data["inline_comments"])
        if prev_sha and prev_sha != head_sha:
            _step(f"Found previous review (sha: {prev_sha[:8]}...), filtering unchanged code...")
            data = filter_already_reviewed(data, prev_sha, head_sha)

        # Findings dedup dropped are still valid on unchanged code. Their old
        # threads must survive cleanup, or they would vanish from the PR. A
        # multi-line suggestion anchors at its end line, so match that too.
        kept_ids = {id(c) for c in data["inline_comments"]}
        dropped: set[tuple[str, int]] = set()
        for c in original:
            if id(c) not in kept_ids:
                dropped.add((c["file"], c["line"]))
                if c.get("fix"):
                    dropped.add((c["file"], c["fix"]["end_line"]))

        if mode and _github_stands_down(session, ctx, mode, deep_job):
            return
        carried = _github_delete_previous_threads(session, ctx, dropped, verbose=verbose)

        inline = data.get("inline_comments", [])
        posted_comments: list[dict[str, Any]] = []
        unanchored: list[dict[str, Any]] = []
        failed = 0

        if len(inline) > MAX_INLINE_COMMENTS:
            ranked = sorted(inline, key=lambda c: SEVERITY_ORDER.index(c["severity"]))
            _warning(
                f"{len(inline)} findings; posting {MAX_INLINE_COMMENTS} inline, "
                "listing the rest in the summary"
            )
            inline = ranked[:MAX_INLINE_COMMENTS]
            unanchored.extend(ranked[MAX_INLINE_COMMENTS:])

        if inline:
            _step(f"Posting {len(inline)} inline comment(s) to PR #{ctx.number}...")
            for c in inline:
                _verify_fix_against_head(c, head_sha, verbose=verbose)
                outcome = _github_post_inline_comment(
                    session, ctx, c, review_marker, verbose=verbose
                )
                if outcome == "posted":
                    posted_comments.append(c)
                elif outcome == "unanchored":
                    unanchored.append(c)
                else:
                    failed += 1

            _success(
                f"Posted {len(posted_comments)}/{len(inline)} inline comments "
                f"({len(unanchored)} not posted inline, {failed} failed)"
            )
        else:
            _step("No inline comments to post")

        # Count only what is visible on the PR; unanchored findings get their own list.
        data["inline_comments"] = posted_comments
        if not _keep_fix_prompt(failed, unanchored):
            data.pop("fix_prompt", None)
        # Without the SHA the next run skips dedup, so findings missing from the diff
        # come back instead of staying dropped as "unchanged code".
        summary_marker = review_marker if not (unanchored or failed) else _make_review_marker()
        summary_body = _github_build_summary_body(
            data, summary_marker, ctx.run_url, unanchored, carried, recommendation
        )
        # The inline comments are already up; a review that should win replaces them.
        if mode and _github_stands_down(session, ctx, mode, deep_job):
            return
        if _github_upsert_summary(session, ctx, summary_body, summary_id, verbose=verbose):
            _success(f"Summary posted to pull request #{ctx.number}")
        else:
            _error("Failed to post summary comment")
            sys.exit(1)

        if failed > 0:
            _error(f"{failed}/{len(inline)} inline comments failed to post")
            sys.exit(1)
    except (requests.exceptions.RequestException, ValueError) as e:
        _error(f"GitHub API error ({type(e).__name__}): {e}")
        sys.exit(1)


# ===========================================================================
# Suggest reviewers
# ===========================================================================

SUGGESTED_REVIEWERS_MARKER = "<!-- suggested-reviewers -->"

_FILES_PER_GRAPHQL_BATCH = 20
_MAX_SUGGESTED_REVIEWERS = 5
_BOT_USERNAME_SUFFIXES = ("_bot", "-bot", "[bot]")


def graphql_query(
    session: requests.Session, gitlab_url: str, query: str, variables: dict[str, Any]
) -> dict[str, Any]:
    """Execute a GitLab GraphQL query."""
    resp = session.post(
        f"{gitlab_url}/api/graphql",
        json={"query": query, "variables": variables},
    )
    resp.raise_for_status()
    data = resp.json()
    if "errors" in data:
        error_msgs = "; ".join(e.get("message", "") for e in data["errors"])
        _warning(f"GraphQL errors: {error_msgs}")
    return data


def get_modified_files(base_sha: str, head_sha: str) -> list[str]:
    """Get files modified in the MR, excluding deleted files."""
    try:
        output = subprocess.check_output(
            ["git", "diff", "--name-only", "--diff-filter=d", f"{base_sha}..{head_sha}"],
            text=True,
            stderr=subprocess.DEVNULL,
        )
    except (subprocess.CalledProcessError, FileNotFoundError):
        _error("Failed to get modified files from git diff")
        return []
    return [f for f in output.strip().split("\n") if f]


def get_ignored_shas() -> set[str]:
    """Read commit SHAs to ignore from .git-blame-ignore-revs at the repo root."""
    try:
        toplevel = subprocess.check_output(
            ["git", "rev-parse", "--show-toplevel"],
            text=True,
            stderr=subprocess.DEVNULL,
        ).strip()
    except (subprocess.CalledProcessError, FileNotFoundError):
        return set()

    ignore_file = Path(toplevel) / ".git-blame-ignore-revs"
    if not ignore_file.is_file():
        return set()

    shas = set()
    for line in ignore_file.read_text(encoding="utf-8", errors="replace").splitlines():
        line = line.strip()
        if not line or line.startswith("#"):
            continue
        shas.add(line.lower())
    return shas


def get_file_authors(
    session: requests.Session,
    gitlab_url: str,
    project_path: str,
    files: list[str],
    ref: str,
    ignored_shas: set[str] | None = None,
) -> dict[str, dict[str, Any]]:
    """Query GitLab GraphQL for commit authors on each file.

    Commits whose SHA matches an entry in ignored_shas (as read from
    .git-blame-ignore-revs) are excluded from authorship counts.

    Returns {username: {"name": str, "commit_count": int, "state": str}}.
    """
    ignored_shas = ignored_shas or set()
    authors: dict[str, dict[str, Any]] = {}

    for batch_start in range(0, len(files), _FILES_PER_GRAPHQL_BATCH):
        batch = files[batch_start : batch_start + _FILES_PER_GRAPHQL_BATCH]

        alias_fragments = []
        for i, filepath in enumerate(batch):
            escaped = filepath.replace("\\", "\\\\").replace('"', '\\"')
            alias_fragments.append(
                f'file{i}: commits(ref: $ref, path: "{escaped}", first: 50) {{\n'
                f"  nodes {{ sha author {{ username name state }} }}\n"
                f"}}"
            )

        query = (
            "query($projectPath: ID!, $ref: String!) {\n"
            "  project(fullPath: $projectPath) {\n"
            "    repository {\n"
            "      " + "\n      ".join(alias_fragments) + "\n"
            "    }\n"
            "  }\n"
            "}"
        )

        result = graphql_query(
            session, gitlab_url, query, {"projectPath": project_path, "ref": ref}
        )
        repo_data = result.get("data", {}).get("project", {}).get("repository", {})

        for i in range(len(batch)):
            commits = repo_data.get(f"file{i}", {}).get("nodes", [])
            for commit in commits:
                sha = commit.get("sha", "")
                if sha and sha.lower() in ignored_shas:
                    continue
                author = commit.get("author")
                if not author or not author.get("username"):
                    continue
                username = author["username"]
                if username not in authors:
                    authors[username] = {
                        "name": author.get("name", username),
                        "commit_count": 0,
                        "state": author.get("state", "unknown"),
                    }
                authors[username]["commit_count"] += 1

    return authors


def get_mr_author(
    session: requests.Session, gitlab_url: str, project_path: str, mr_iid: str
) -> str | None:
    """Get the MR author's username via GraphQL."""
    query = """
    query($projectPath: ID!, $mrIid: String!) {
      project(fullPath: $projectPath) {
        mergeRequest(iid: $mrIid) {
          author { username }
        }
      }
    }
    """
    result = graphql_query(
        session, gitlab_url, query, {"projectPath": project_path, "mrIid": mr_iid}
    )
    mr = result.get("data", {}).get("project", {}).get("mergeRequest")
    if mr and mr.get("author"):
        return mr["author"]["username"]
    return None


def build_reviewer_comment(reviewers: list[dict[str, Any]]) -> str:
    """Format the reviewer suggestion MR note body."""
    lines = ["## Suggested Reviewers", ""]
    lines.append(
        "The following users have recently contributed to the files modified "
        "in this MR and may be well-suited to review these changes:"
    )
    lines.append("")
    lines.append("| Reviewer | Commits on modified files |")
    lines.append("|----------|--------------------------|")
    for r in reviewers:
        lines.append(f"| @{r['username']} | {r['commit_count']} |")
    lines.append("")

    mentions = " ".join(f"@{r['username']}" for r in reviewers)
    lines.append(f"{mentions} — please consider reviewing this merge request.")
    lines.append("")
    lines.append(SUGGESTED_REVIEWERS_MARKER)
    return "\n".join(lines)


def gitlab_has_reviewer_suggestion(session: requests.Session, api_base: str) -> bool:
    """Check whether a reviewer suggestion note already exists on the MR."""
    for note in _gitlab_paginated_get(session, f"{api_base}/notes"):
        if note.get("position") is not None:
            continue
        if SUGGESTED_REVIEWERS_MARKER in note.get("body", ""):
            return True
    return False


def cmd_suggest_reviewers(args: argparse.Namespace) -> None:
    """Identify and suggest reviewers for the MR based on git history."""
    token = os.environ.get("GITLAB_API_TOKEN")
    if not token:
        _error("GITLAB_API_TOKEN environment variable is required")
        sys.exit(1)

    project_path = os.environ.get("CI_PROJECT_PATH")
    project_id = os.environ.get("CI_PROJECT_ID")
    mr_iid = os.environ.get("CI_MERGE_REQUEST_IID")
    base_sha = os.environ.get("CI_MERGE_REQUEST_DIFF_BASE_SHA")
    head_sha = os.environ.get("CI_COMMIT_SHA")

    if not all([project_path, project_id, mr_iid, base_sha, head_sha]):
        _error(
            "Missing required CI variables: CI_PROJECT_PATH, CI_PROJECT_ID, "
            "CI_MERGE_REQUEST_IID, CI_MERGE_REQUEST_DIFF_BASE_SHA, CI_COMMIT_SHA"
        )
        sys.exit(1)

    gitlab_url = os.environ.get("CI_SERVER_URL", "https://gitlab.com").rstrip("/")
    verbose = _resolve_verbose(args)

    session = _SafeSession()
    session.headers.update({"PRIVATE-TOKEN": token})

    api_base = f"{gitlab_url}/api/v4/projects/{project_id}/merge_requests/{mr_iid}"

    if gitlab_has_reviewer_suggestion(session, api_base):
        _success("Reviewers already requested, skipping")
        return

    _step("Getting modified files...")
    files = get_modified_files(base_sha, head_sha)
    if not files:
        _step("No modified files found, skipping reviewer suggestion")
        return
    _step(f"Found {len(files)} modified file(s)")

    ignored_shas = get_ignored_shas()
    if ignored_shas:
        _step(f"Ignoring {len(ignored_shas)} commit(s) listed in .git-blame-ignore-revs")

    _step("Querying git history for file authors...")
    authors = get_file_authors(session, gitlab_url, project_path, files, "HEAD", ignored_shas)

    if not authors:
        _step("No file authors found, skipping reviewer suggestion")
        return

    mr_author = get_mr_author(session, gitlab_url, project_path, mr_iid)
    _step(f"MR author: @{mr_author}" if mr_author else "Could not determine MR author")

    reviewers = []
    for username, info in authors.items():
        if mr_author and username == mr_author:
            continue
        if info["state"] != "active":
            continue
        if any(username.endswith(suffix) for suffix in _BOT_USERNAME_SUFFIXES):
            continue
        reviewers.append({"username": username, **info})

    reviewers.sort(key=lambda r: r["commit_count"], reverse=True)
    reviewers = reviewers[:_MAX_SUGGESTED_REVIEWERS]

    if not reviewers:
        _step("No suitable reviewers found after filtering")
        return

    _step(f"Suggesting {len(reviewers)} reviewer(s)")
    comment_body = build_reviewer_comment(reviewers)

    _step(f"Posting reviewer suggestion to MR !{mr_iid}...")
    resp = session.post(f"{api_base}/notes", json={"body": comment_body})
    if 200 <= resp.status_code < 300:
        _success(f"Reviewer suggestion posted to MR !{mr_iid}")
    else:
        _error(f"Failed to post reviewer suggestion (HTTP {resp.status_code})")
        if verbose:
            print(resp.text, file=sys.stderr)
        sys.exit(1)


# ---------------------------------------------------------------------------
# Subcommands
# ---------------------------------------------------------------------------


def _resolve_chill(args: argparse.Namespace) -> bool:
    """Resolve chill mode: CLI flag > env var > default True."""
    if args.chill is not None:
        return args.chill
    return os.environ.get("CHILL_MODE", "true").lower() == "true"


def _resolve_inline_fixes(args: argparse.Namespace) -> bool:
    """Resolve inline fixes: CLI flag > env var > default True."""
    if getattr(args, "inline_fixes", None) is not None:
        return args.inline_fixes
    return os.environ.get("INLINE_FIXES", "true").lower() == "true"


def _resolve_verbose(args: argparse.Namespace) -> bool:
    """Resolve verbose: CLI flag > env var > default False."""
    if getattr(args, "verbose", False):
        return True
    return os.environ.get("VERBOSE", "false").lower() == "true"


def cmd_post(args: argparse.Namespace) -> None:
    """Post review results to the detected platform.

    Falls back to terminal display when no CI platform is detected. GitHub is
    never detected: it needs ``--platform github``.
    """
    if getattr(args, "platform", "auto") == "github":
        _github_post(args)
        return

    platform = _detect_platform()

    if platform == "gitlab":
        _gitlab_post(args)
    else:
        _step("No CI platform detected — displaying review locally.")
        cmd_display(args)


def cmd_display(args: argparse.Namespace) -> None:
    """Display review results in terminal."""
    chill = _resolve_chill(args)
    inline_fixes = _resolve_inline_fixes(args)

    try:
        data = parse_review_json(args.review_file, chill_mode=chill, inline_fixes=inline_fixes)
    except (json.JSONDecodeError, ValueError) as e:
        _error(f"Failed to parse review JSON: {e}")
        sys.exit(1)

    _display_review(data)


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------


def _add_common_args(parser: argparse.ArgumentParser) -> None:
    parser.add_argument("review_file", help="Path to the review JSON file")
    chill_group = parser.add_mutually_exclusive_group()
    chill_group.add_argument(
        "--chill",
        action="store_true",
        default=None,
        dest="chill",
        help="Filter out suggestion-level comments (default)",
    )
    chill_group.add_argument(
        "--no-chill",
        action="store_false",
        dest="chill",
        help="Include suggestion-level comments",
    )
    fix_group = parser.add_mutually_exclusive_group()
    fix_group.add_argument(
        "--inline-fixes",
        action="store_true",
        default=None,
        dest="inline_fixes",
        help="Post applicable suggestions from inline_comments[].fix (default)",
    )
    fix_group.add_argument(
        "--no-inline-fixes",
        action="store_false",
        dest="inline_fixes",
        help="Drop inline_comments[].fix and post comments as prose only",
    )


def main() -> None:
    parser = argparse.ArgumentParser(
        description="AI code review comment manager.",
    )
    subparsers = parser.add_subparsers(dest="command", required=True)

    post_parser = subparsers.add_parser("post", help="Post review to a GitLab MR or GitHub PR")
    _add_common_args(post_parser)
    post_parser.add_argument(
        "--platform",
        choices=["auto", "github"],
        default="auto",
        help=(
            "auto detects GitLab CI and otherwise displays the review; "
            "github posts to the pull request named by the Actions event (never auto-detected)"
        ),
    )
    post_parser.add_argument(
        "--deep-recommendation-plan",
        metavar="PATH",
        help=(
            "panel.py triage output; when its tier is deep, the summary recommends "
            "a deep review (needs --platform github)"
        ),
    )
    post_parser.add_argument(
        "--mode",
        choices=["standard", "deep"],
        help=(
            "check for a review that should win before deleting old threads and before "
            "the summary, as stand-down does (needs --platform github)"
        ),
    )
    post_parser.add_argument(
        "--deep-job",
        metavar="NAME",
        help=(
            f"display name of the deep review job (default: {_GITHUB_DEEP_JOB_DEFAULT}; "
            "needs --platform github)"
        ),
    )
    post_parser.add_argument("--verbose", action="store_true", help="Verbose output")
    post_parser.set_defaults(func=cmd_post)

    display_parser = subparsers.add_parser("display", help="Display review in terminal")
    _add_common_args(display_parser)
    display_parser.set_defaults(func=cmd_display)

    suggest_parser = subparsers.add_parser(
        "suggest-reviewers", help="Suggest reviewers based on git history"
    )
    suggest_parser.add_argument("--verbose", action="store_true", help="Verbose output")
    suggest_parser.set_defaults(func=cmd_suggest_reviewers)

    standdown_parser = subparsers.add_parser(
        "stand-down",
        help=(
            "Exit 10 when a review that should win makes this one redundant, 0 otherwise: "
            "a deep review job of the same head for standard, a newer head for deep"
        ),
    )
    standdown_parser.add_argument("--platform", choices=["github"], required=True)
    standdown_parser.add_argument("--mode", choices=["standard", "deep"], required=True)
    standdown_parser.add_argument(
        "--deep-job",
        metavar="NAME",
        help=f"display name of the deep review job (default: {_GITHUB_DEEP_JOB_DEFAULT})",
    )
    standdown_parser.set_defaults(func=cmd_stand_down)

    args = parser.parse_args()
    if args.command == "post" and args.platform != "github":
        for flag, value in (
            ("--deep-recommendation-plan", args.deep_recommendation_plan),
            ("--mode", args.mode),
            ("--deep-job", args.deep_job),
        ):
            if value is not None:
                post_parser.error(f"{flag} needs --platform github")
    args.func(args)


if __name__ == "__main__":
    main()
