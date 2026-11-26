"""Characterization tests for the GitLab suggest-reviewers command.

These pin today's output and requests, so later changes elsewhere in review.py
cannot alter what a GitLab merge request receives.
"""

import contextlib
import importlib.util
import io
import os
import re
import sys
import unittest
from argparse import Namespace
from pathlib import Path
from types import ModuleType, SimpleNamespace
from unittest.mock import patch

from tests.helpers import PanelTestCase, commit, make_repo

REVIEW_PATH = Path(__file__).resolve().parents[1] / "skills/gitlab-code-review/scripts/review.py"
SPEC = importlib.util.spec_from_file_location("gitlab_suggest_review", REVIEW_PATH)
review = importlib.util.module_from_spec(SPEC)
requests_stub = ModuleType("requests")
requests_stub.Session = type("Session", (), {})
_RequestException = type("RequestException", (Exception,), {})
requests_stub.exceptions = SimpleNamespace(
    RequestException=_RequestException,
    HTTPError=type("HTTPError", (_RequestException,), {}),
)
with patch.dict(sys.modules, {"requests": requests_stub}):
    SPEC.loader.exec_module(review)

GITLAB = "https://gitlab.example.com"
GRAPHQL = f"{GITLAB}/api/graphql"
API_BASE = f"{GITLAB}/api/v4/projects/42/merge_requests/3"
NOTES = f"{API_BASE}/notes"
PROJECT = "group/proj"
MARKER = "<!-- suggested-reviewers -->"
# One aliased commits field of the per-file history query; the path is a quoted literal.
ALIAS_RE = re.compile(r'file(\d+): commits\(ref: \$ref, path: "((?:[^"\\]|\\.)*)", first: 50\)')


def node(username, sha="0" * 40, state="active", name=None):
    return {"sha": sha, "author": {"username": username, "name": name or username, "state": state}}


class FakeResponse:
    def __init__(self, status=200, data=None, headers=None):
        self.status_code = status
        self._data = data
        self.headers = headers or {}
        self.text = ""

    def json(self):
        return self._data

    def raise_for_status(self):
        if self.status_code >= 400:
            raise requests_stub.exceptions.HTTPError(f"HTTP {self.status_code}")


class FakeGitLab:
    """Recording stand-in for _SafeSession: MR notes, per-file history and the MR author."""

    def __init__(self, notes=(), history=None, mr_author="author", post_status=201):
        self.notes = list(notes)
        self.history = history or {}
        self.mr_author = mr_author
        self.post_status = post_status
        self.headers = {}
        self.calls = []

    def get(self, url, params=None, **kwargs):
        self.calls.append(("GET", url, {"params": params, **kwargs}))
        if url == NOTES:
            return FakeResponse(200, self.notes)
        return FakeResponse(404, [])

    def post(self, url, json=None, **kwargs):
        self.calls.append(("POST", url, {"json": json, **kwargs}))
        if url == GRAPHQL:
            query = json["query"]
            if "mergeRequest" in query:
                author = {"username": self.mr_author} if self.mr_author else None
                return FakeResponse(
                    200, {"data": {"project": {"mergeRequest": {"author": author}}}}
                )
            repository = {}
            for m in ALIAS_RE.finditer(query):
                path = re.sub(r"\\(.)", r"\1", m.group(2))
                repository[f"file{m.group(1)}"] = {"nodes": self.history.get(path, [])}
            return FakeResponse(200, {"data": {"project": {"repository": repository}}})
        if url == NOTES:
            return FakeResponse(self.post_status, {})
        return FakeResponse(404, {})

    def graphql_calls(self):
        return [c[2]["json"] for c in self.calls if c[:2] == ("POST", GRAPHQL)]

    def history_calls(self):
        return [j for j in self.graphql_calls() if "mergeRequest" not in j["query"]]

    def note_posts(self):
        return [c[2]["json"] for c in self.calls if c[:2] == ("POST", NOTES)]


class GitLabSuggestTestCase(PanelTestCase):
    def setUp(self):
        self.tmp = self.make_tmpdir()
        self.repo, self.base = make_repo(
            self.tmp, {"keep.py": "a\n", "gone.py": "b\n", "docs/readme.md": "c\n"}, branch="mr"
        )
        self.head = commit(
            self.repo,
            {"keep.py": "a2\n", "gone.py": None, "new.py": "n\n", "docs/readme.md": "c2\n"},
        )
        self.addCleanup(os.chdir, os.getcwd())
        os.chdir(self.repo)

    def env(self, **overrides):
        env = {
            "PATH": os.environ["PATH"],
            "GIT_CONFIG_GLOBAL": "/dev/null",
            "GIT_CONFIG_NOSYSTEM": "1",
            "GITLAB_API_TOKEN": "glpat-token",
            "CI_SERVER_URL": GITLAB,
            "CI_PROJECT_PATH": PROJECT,
            "CI_PROJECT_ID": "42",
            "CI_MERGE_REQUEST_IID": "3",
            "CI_MERGE_REQUEST_DIFF_BASE_SHA": self.base,
            "CI_COMMIT_SHA": self.head,
        }
        env.update(overrides)
        return {k: v for k, v in env.items() if v is not None}

    def suggest(self, fake, env=None):
        """Run cmd_suggest_reviewers with the fake session; return (exit code, out, err)."""
        out, err = io.StringIO(), io.StringIO()
        code = 0
        with (
            patch.dict(os.environ, self.env() if env is None else env, clear=True),
            patch.object(review, "_SafeSession", return_value=fake),
            contextlib.redirect_stdout(out),
            contextlib.redirect_stderr(err),
        ):
            try:
                review.cmd_suggest_reviewers(Namespace(verbose=False))
            except SystemExit as e:
                code = e.code
        return code, out.getvalue(), err.getvalue()


class ReviewerCommentTextTests(unittest.TestCase):
    def test_comment_text_is_pinned(self):
        body = review.build_reviewer_comment(
            [
                {"username": "alice", "commit_count": 3, "name": "Alice", "state": "active"},
                {"username": "bob", "commit_count": 1, "name": "Bob", "state": "active"},
            ]
        )
        self.assertEqual(
            body,
            "## Suggested Reviewers\n"
            "\n"
            "The following users have recently contributed to the files modified in this MR "
            "and may be well-suited to review these changes:\n"
            "\n"
            "| Reviewer | Commits on modified files |\n"
            "|----------|--------------------------|\n"
            "| @alice | 3 |\n"
            "| @bob | 1 |\n"
            "\n"
            "@alice @bob \u2014 please consider reviewing this merge request.\n"
            "\n"
            "<!-- suggested-reviewers -->",
        )

    def test_marker_constant_is_pinned(self):
        self.assertEqual(review.SUGGESTED_REVIEWERS_MARKER, MARKER)


class MarkerCheckTests(GitLabSuggestTestCase):
    def test_general_note_holding_the_marker_anywhere_skips_before_any_query(self):
        for body in (f"intro\n{MARKER}", f"{MARKER}\nmore text after", f"x {MARKER} y"):
            with self.subTest(body=body):
                fake = FakeGitLab(notes=[{"body": body, "position": None}])
                code, _, err = self.suggest(fake)
                self.assertEqual(code, 0)
                self.assertIn("Reviewers already requested, skipping", err)
                self.assertEqual([c[:2] for c in fake.calls], [("GET", NOTES)])
                self.assertEqual(fake.calls[0][2]["params"], {"per_page": 100})

    def test_marker_in_a_diff_note_does_not_count(self):
        fake = FakeGitLab(
            notes=[{"body": MARKER, "position": {"new_path": "keep.py"}}],
            history={"keep.py": [node("carol")]},
        )
        code, _, _ = self.suggest(fake)
        self.assertEqual(code, 0)
        self.assertEqual(len(fake.note_posts()), 1)

    def test_token_goes_in_the_private_token_header(self):
        fake = FakeGitLab(notes=[{"body": MARKER, "position": None}])
        self.suggest(fake)
        self.assertEqual(fake.headers, {"PRIVATE-TOKEN": "glpat-token"})


class HistoryQueryTests(GitLabSuggestTestCase):
    def test_query_text_and_variables_are_pinned(self):
        fake = FakeGitLab()
        with patch.dict(os.environ, self.env(), clear=True):
            review.get_file_authors(fake, GITLAB, PROJECT, ["a.py", 'q"uo\\te.py'], "HEAD")
        self.assertEqual(
            fake.history_calls(),
            [
                {
                    "query": "query($projectPath: ID!, $ref: String!) {\n"
                    "  project(fullPath: $projectPath) {\n"
                    "    repository {\n"
                    '      file0: commits(ref: $ref, path: "a.py", first: 50) {\n'
                    "  nodes { sha author { username name state } }\n"
                    "}\n"
                    '      file1: commits(ref: $ref, path: "q\\"uo\\\\te.py", first: 50) {\n'
                    "  nodes { sha author { username name state } }\n"
                    "}\n"
                    "    }\n"
                    "  }\n"
                    "}",
                    "variables": {"projectPath": PROJECT, "ref": "HEAD"},
                }
            ],
        )

    def test_files_go_in_batches_of_20(self):
        files = [f"f{i:02}.py" for i in range(41)]
        fake = FakeGitLab()
        with patch.dict(os.environ, self.env(), clear=True):
            review.get_file_authors(fake, GITLAB, PROJECT, files, "HEAD")
        batches = [ALIAS_RE.findall(j["query"]) for j in fake.history_calls()]
        self.assertEqual([len(b) for b in batches], [20, 20, 1])
        self.assertEqual([p for b in batches for _, p in b], files)

    def test_command_queries_changed_files_at_head_ref_and_the_mr_author(self):
        fake = FakeGitLab(history={"keep.py": [node("carol")]})
        code, _, _ = self.suggest(fake)
        self.assertEqual(code, 0)
        history = fake.history_calls()
        self.assertEqual(len(history), 1)
        # Deleted files are left out; added and modified ones are queried.
        self.assertEqual(
            sorted(p for _, p in ALIAS_RE.findall(history[0]["query"])),
            ["docs/readme.md", "keep.py", "new.py"],
        )
        self.assertEqual(history[0]["variables"], {"projectPath": PROJECT, "ref": "HEAD"})
        author_query = [j for j in fake.graphql_calls() if "mergeRequest" in j["query"]]
        self.assertEqual(
            [j["variables"] for j in author_query], [{"projectPath": PROJECT, "mrIid": "3"}]
        )
        self.assertEqual(
            [c[:2] for c in fake.calls],
            [("GET", NOTES), ("POST", GRAPHQL), ("POST", GRAPHQL), ("POST", NOTES)],
        )


class FilteringTests(GitLabSuggestTestCase):
    def test_author_inactive_users_bots_and_ignored_commits_are_left_out(self):
        ignored = "f" * 40
        Path(self.repo, ".git-blame-ignore-revs").write_text(f"# formatting\n\n{ignored.upper()}\n")
        fake = FakeGitLab(
            mr_author="author",
            history={
                "keep.py": [
                    node("author"),
                    node("author"),
                    node("blocked-user", state="blocked"),
                    node("ci_bot"),
                    node("deploy-bot"),
                    node("app[bot]"),
                    node("carol"),
                    node("dave", sha=ignored),
                    {"sha": "1" * 40, "author": None},
                    {"sha": "2" * 40, "author": {"username": ""}},
                ],
                "new.py": [node("carol"), node("erin")],
            },
        )
        code, _, err = self.suggest(fake)
        self.assertEqual(code, 0)
        self.assertIn("Ignoring 1 commit(s) listed in .git-blame-ignore-revs", err)
        self.assertIn("MR author: @author", err)
        body = fake.note_posts()[0]["body"]
        self.assertIn("| @carol | 2 |\n| @erin | 1 |\n", body)
        for login in ("author", "blocked-user", "ci_bot", "deploy-bot", "app[bot]", "dave"):
            self.assertNotIn(f"@{login} ", body)

    def test_top_five_by_commit_count_are_kept(self):
        counts = {"u1": 1, "u2": 7, "u3": 3, "u4": 6, "u5": 2, "u6": 5, "u7": 4}
        nodes = [node(login) for login, n in counts.items() for _ in range(n)]
        fake = FakeGitLab(history={"keep.py": nodes})
        code, _, _ = self.suggest(fake)
        self.assertEqual(code, 0)
        posts = fake.note_posts()
        self.assertEqual(len(posts), 1)
        self.assertEqual(
            posts[0]["body"],
            review.build_reviewer_comment(
                [{"username": u, "commit_count": counts[u]} for u in ("u2", "u4", "u6", "u7", "u3")]
            ),
        )

    def test_nobody_left_after_filtering_posts_nothing(self):
        fake = FakeGitLab(mr_author="author", history={"keep.py": [node("author")]})
        code, _, err = self.suggest(fake)
        self.assertEqual(code, 0)
        self.assertIn("No suitable reviewers found after filtering", err)
        self.assertEqual(fake.note_posts(), [])

    def test_no_history_posts_nothing_and_skips_the_author_query(self):
        fake = FakeGitLab()
        code, _, err = self.suggest(fake)
        self.assertEqual(code, 0)
        self.assertIn("No file authors found, skipping reviewer suggestion", err)
        self.assertEqual(fake.note_posts(), [])
        self.assertEqual(len(fake.graphql_calls()), 1)


class ExitCodeTests(GitLabSuggestTestCase):
    def test_missing_ci_variables_exit_1_before_any_request(self):
        for name in ("GITLAB_API_TOKEN", "CI_PROJECT_PATH", "CI_COMMIT_SHA"):
            with self.subTest(name):
                fake = FakeGitLab()
                code, _, _ = self.suggest(fake, env=self.env(**{name: None}))
                self.assertEqual(code, 1)
                self.assertEqual(fake.calls, [])

    def test_failed_note_post_exits_1(self):
        fake = FakeGitLab(history={"keep.py": [node("carol")]}, post_status=403)
        code, _, err = self.suggest(fake)
        self.assertEqual(code, 1)
        self.assertIn("Failed to post reviewer suggestion (HTTP 403)", err)


if __name__ == "__main__":
    unittest.main()
