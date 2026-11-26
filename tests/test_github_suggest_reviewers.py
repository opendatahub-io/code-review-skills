"""Tests for suggest-reviewers --platform github.

The pull request lives in a throwaway git repository; the GitHub API is a recording fake.
"""

import contextlib
import io
import json
import os
import re
import sys
import unittest
from argparse import Namespace
from pathlib import Path
from unittest.mock import patch

from tests.helpers import commit, git, make_repo
from tests.test_github_review import (
    GRAPHQL,
    LIVE_MENTION_RE,
    OURS_REST,
    REPO_URL,
    FakeGitHub,
    FakeResponse,
    GitHubTestCase,
    comment,
    requests_stub,
    review,
)

COMMENTS = f"{REPO_URL}/issues/7/comments"
MARKER = "<!-- suggested-reviewers -->"
TOKEN = "ghs_suggest_token_value"
USER_LOGIN_RE = re.compile(r"^[A-Za-z0-9](?:[A-Za-z0-9]|-(?=[A-Za-z0-9])){0,38}$")
HUMAN_REST = {"login": "carol", "type": "User"}
APP_REST = {"login": "review-app[bot]", "type": "Bot"}
IGNORED = "e" * 40


def hist(login, oid="0" * 40, typename="User"):
    """One history node; a None login is a commit email linked to no account."""
    user = None if login is None else {"__typename": typename, "login": login}
    return {"oid": oid, "author": {"user": user}}


def many(**counts):
    return [hist(login) for login, n in counts.items() for _ in range(n)]


class HistoryGitHub(FakeGitHub):
    """FakeGitHub that answers the file history query from a path -> nodes map."""

    def __init__(self, history=None, *, graphql=(), comment_status=(), post_status=201, **kwargs):
        super().__init__(**kwargs)
        self.history = history or {}
        self.graphql = list(graphql)  # canned responses, used before the history map
        self.comment_status = list(comment_status)  # status per comments GET, then 200
        self.post_status = post_status

    def get(self, url, params=None, **kwargs):
        if url.startswith(COMMENTS) and self.comment_status:
            status = self.comment_status.pop(0)
            if status != 200:
                self.calls.append(("GET", url, {"params": params, **kwargs}))
                return FakeResponse(status, {})
        return super().get(url, params, **kwargs)

    def post(self, url, json=None, **kwargs):
        if url == GRAPHQL:
            self.calls.append(("POST", url, {"json": json, **kwargs}))
            if self.graphql:
                response = self.graphql.pop(0)
                if isinstance(response, Exception):
                    raise response
                return response
            variables = json["variables"]
            commit_fields = {}
            i = 0
            while f"p{i}" in variables:
                commit_fields[f"f{i}"] = {"nodes": self.history.get(variables[f"p{i}"], [])}
                i += 1
            return FakeResponse(200, {"data": {"repository": {"object": commit_fields}}})
        if url == COMMENTS:
            self.calls.append(("POST", url, {"json": json, **kwargs}))
            return FakeResponse(self.post_status, {})
        return super().post(url, json=json, **kwargs)

    def history_calls(self):
        return [c[2]["json"] for c in self.calls if c[:2] == ("POST", GRAPHQL)]

    def comment_posts(self):
        return [c[2]["json"]["body"] for c in self.calls if c[:2] == ("POST", COMMENTS)]


class SuggestTestCase(GitHubTestCase):
    def setUp(self):
        super().setUp()
        self.repo, fork_point = make_repo(
            self.tmp, {"keep.py": "a\n", "gone.py": "b\n", "docs/readme.md": "c\n"}, branch="pr"
        )
        self.head = commit(
            self.repo,
            {"keep.py": "a2\n", "gone.py": None, "new.py": "n\n", "docs/readme.md": "c2\n"},
        )
        # The base branch moves on after the fork; its own changes are not the pull request's.
        git(self.repo, "checkout", "-q", "main")
        self.base = commit(self.repo, {"base_only.py": "o\n", "keep.py": "a3\n"})
        git(self.repo, "checkout", "-q", "pr")
        self.assertNotEqual(self.base, fork_point)
        self.addCleanup(os.chdir, os.getcwd())
        os.chdir(self.repo)

    def env(self, head=None, base="", author="Author", **overrides):
        pull_request = {"number": 7, "head": {"sha": head or self.head}}
        if base is not None:
            pull_request["base"] = {"sha": base or self.base}
        if author is not None:
            pull_request["user"] = {"login": author, "type": "User"}
        # One file per call: a test may build several environments before using them.
        self.events = getattr(self, "events", 0) + 1
        event = Path(self.tmp) / f"suggest-event-{self.events}.json"
        event.write_text(json.dumps({"pull_request": pull_request}))
        settings = {"GITHUB_EVENT_PATH": str(event), "GITHUB_TOKEN": TOKEN}
        settings.update(overrides)
        return super().env(**settings)

    def suggest(self, fake, env=None):
        args = Namespace(platform="github", verbose=False)
        return self.call(review.cmd_suggest_reviewers, args, env=env, fake=fake)

    def assert_skipped_with_warning(self, code, out, err, fake, posts=0):
        self.assertEqual(code, 0)
        self.assertIn("::warning::Reviewer suggestion skipped", out)
        self.assertNotIn(TOKEN, out + err)
        self.assertEqual(len(fake.comment_posts()), posts)


class CommentTests(SuggestTestCase):
    def test_one_comment_with_at_most_five_valid_logins_highest_first(self):
        fake = HistoryGitHub(
            {"keep.py": many(u1=1, u2=7, u3=3, u4=6), "new.py": many(u5=2, u6=5, u7=4)}
        )
        code, out, err = self.suggest(fake)
        self.assertEqual(code, 0)
        self.assertNotIn(TOKEN, out + err)
        posts = fake.comment_posts()
        self.assertEqual(len(posts), 1)
        self.assertEqual(
            posts[0],
            "## Suggested Reviewers\n"
            "\n"
            "The following users have recently contributed to the files modified in this "
            "pull request and may be well-suited to review these changes:\n"
            "\n"
            "| Reviewer | Commits on modified files |\n"
            "|----------|--------------------------|\n"
            "| @u2 | 7 |\n"
            "| @u4 | 6 |\n"
            "| @u6 | 5 |\n"
            "| @u7 | 4 |\n"
            "| @u3 | 3 |\n"
            "\n"
            "@u2 @u4 @u6 @u7 @u3, please consider reviewing this pull request.\n"
            "\n"
            "<!-- suggested-reviewers -->",
        )

    def test_body_holds_live_mentions_of_valid_logins_and_ends_with_the_marker(self):
        fake = HistoryGitHub({"keep.py": many(**{"Dana-K": 2, "e1": 1})})
        self.suggest(fake)
        (body,) = fake.comment_posts()
        self.assertTrue(body.endswith(MARKER))
        self.assertNotIn("\u2014", body)
        self.assertNotIn("merge request", body)
        self.assertIn("pull request", body)
        mention_line = body.splitlines()[-3]
        logins = re.findall(r"@([^\s,]+)", mention_line)
        self.assertEqual(logins, ["Dana-K", "e1"])
        self.assertEqual(len(LIVE_MENTION_RE.findall(mention_line)), 2)
        for login in logins:
            self.assertRegex(login, USER_LOGIN_RE)

    def test_same_login_in_another_case_is_one_reviewer(self):
        fake = HistoryGitHub({"keep.py": many(Carol=2), "new.py": many(carol=1)})
        self.suggest(fake)
        (body,) = fake.comment_posts()
        self.assertIn("| @Carol | 3 |\n", body)
        self.assertNotIn("@carol", body)

    def test_posts_with_the_session_token_and_nothing_else(self):
        fake = HistoryGitHub({"keep.py": many(carol=1)})
        self.suggest(fake)
        self.assertEqual(fake.headers["Authorization"], f"Bearer {TOKEN}")
        self.assertEqual(
            [c[:2] for c in fake.calls],
            [("GET", COMMENTS), ("POST", GRAPHQL), ("POST", COMMENTS)],
        )


class FilteringTests(SuggestTestCase):
    def test_author_bots_unlinked_and_malformed_logins_are_left_out(self):
        fake = HistoryGitHub(
            {
                "keep.py": [
                    hist("author"),
                    hist("AUTHOR"),
                    hist("renovate", typename="Bot"),
                    hist("dependabot[bot]"),
                    hist("helper[bot]", typename="Bot"),
                    hist(None),
                    {"oid": "1" * 40, "author": None},
                    {"oid": "2" * 40},
                    "not a node",
                    hist("bad_login"),
                    hist("-lead"),
                    hist("trail-"),
                    hist("dou--ble"),
                    hist("x" * 40),
                    hist("evil\n::error::x"),
                    hist("@nested"),
                    hist(""),
                    hist(42),
                    hist("carol"),
                ],
            }
        )
        code, _, _ = self.suggest(fake)
        self.assertEqual(code, 0)
        (body,) = fake.comment_posts()
        self.assertEqual(re.findall(r"@([^\s|,]+)", body), ["carol", "carol"])

    def test_only_the_author_yields_no_comment(self):
        fake = HistoryGitHub({"keep.py": many(author=3)})
        code, _, err = self.suggest(fake)
        self.assertEqual(code, 0)
        self.assertIn("No suitable reviewers found", err)
        self.assertEqual(fake.comment_posts(), [])

    def test_ignore_revs_come_from_the_head_commit(self):
        ignore_file = f"# reformat\n{IGNORED.upper()}  # trailing note\nnot-a-sha\n"
        self.head = commit(self.repo, {".git-blame-ignore-revs": ignore_file})
        # The working tree copy is not what the head says; only the commit counts.
        Path(self.repo, ".git-blame-ignore-revs").unlink()
        fake = HistoryGitHub({"keep.py": [hist("carol", oid=IGNORED), hist("dave")]})
        code, _, err = self.suggest(fake)
        self.assertEqual(code, 0)
        self.assertIn("Ignoring 1 commit(s)", err)
        (body,) = fake.comment_posts()
        self.assertNotIn("@carol", body)
        self.assertIn("@dave", body)

    def test_an_uncommitted_ignore_file_is_not_read(self):
        Path(self.repo, ".git-blame-ignore-revs").write_text(f"{IGNORED}\n")
        fake = HistoryGitHub({"keep.py": [hist("carol", oid=IGNORED)]})
        self.suggest(fake)
        (body,) = fake.comment_posts()
        self.assertIn("@carol", body)


class MarkerCheckTests(SuggestTestCase):
    def test_our_marker_comment_suppresses_before_any_query(self):
        for name, user, env in (
            ("workflow bot", OURS_REST, {}),
            ("configured app", APP_REST, {"AI_REVIEW_BOT_LOGIN": "review-app[bot]"}),
            ("workflow bot after a switch", OURS_REST, {"AI_REVIEW_BOT_LOGIN": "review-app[bot]"}),
        ):
            with self.subTest(name):
                fake = HistoryGitHub(
                    {"keep.py": many(carol=1)},
                    comment_pages=[([comment(1, f"Earlier\n\n{MARKER}\n", user)], "")],
                )
                code, _, err = self.suggest(fake, env=self.env(**env))
                self.assertEqual(code, 0)
                self.assertIn("Reviewers already suggested, skipping", err)
                self.assertEqual([c[:2] for c in fake.calls], [("GET", COMMENTS)])

    def test_marker_on_a_later_page_suppresses(self):
        fake = HistoryGitHub(
            {"keep.py": many(carol=1)},
            comment_pages=[
                ([comment(1, "hello", HUMAN_REST)], f'<{COMMENTS}?page=2>; rel="next"'),
                ([comment(2, MARKER, OURS_REST)], ""),
            ],
        )
        self.suggest(fake)
        self.assertEqual(fake.comment_posts(), [])
        self.assertEqual(fake.history_calls(), [])

    def test_marker_from_someone_else_or_not_at_the_end_does_not_suppress(self):
        for name, comments in (
            ("human", [comment(1, MARKER, HUMAN_REST)]),
            ("other bot", [comment(1, MARKER, {"login": "renovate[bot]", "type": "Bot"})]),
            ("app not configured", [comment(1, MARKER, APP_REST)]),
            ("marker mid-body", [comment(1, f"{MARKER}\nmore", OURS_REST)]),
            ("review summary", [comment(1, "Summary\n<!-- ai-review -->", OURS_REST)]),
            ("no user", [{"id": 1, "body": MARKER, "user": None}]),
        ):
            with self.subTest(name):
                fake = HistoryGitHub({"keep.py": many(carol=1)}, comment_pages=[(comments, "")])
                code, _, _ = self.suggest(fake)
                self.assertEqual(code, 0)
                self.assertEqual(len(fake.comment_posts()), 1)


class HistoryQueryTests(SuggestTestCase):
    def test_query_shape_is_pinned(self):
        self.assertEqual(
            review._github_history_query(2),
            "query($owner: String!, $name: String!, $oid: GitObjectID!, $p0: String!, "
            "$p1: String!) {\n"
            "  repository(owner: $owner, name: $name) {\n"
            "    object(oid: $oid) {\n"
            "      ... on Commit {\n"
            "        f0: history(first: 50, path: $p0) "
            "{ nodes { oid author { user { __typename login } } } }\n"
            "        f1: history(first: 50, path: $p1) "
            "{ nodes { oid author { user { __typename login } } } }\n"
            "      }\n"
            "    }\n"
            "  }\n"
            "}",
        )

    def test_paths_and_refs_are_variables_only_in_batches_of_20(self):
        odd = ['q"uote.py', "dollar$p0.py", "brace}{.py", "back\\slash.py", "sp ace.py"]
        paths = odd + [f"pkg/f{i:02}.py" for i in range(40)]
        self.head = commit(self.repo, {p: "z\n" for p in paths})
        fake = HistoryGitHub({p: [hist("carol")] for p in paths})
        code, _, _ = self.suggest(fake)
        self.assertEqual(code, 0)
        calls = fake.history_calls()
        self.assertEqual([len(c["variables"]) - 3 for c in calls], [20, 20, 8])
        queried = []
        for call in calls:
            n = len(call["variables"]) - 3
            self.assertEqual(call["query"], review._github_history_query(n))
            self.assertEqual(
                {k: call["variables"][k] for k in ("owner", "name", "oid")},
                {"owner": "o", "name": "r", "oid": self.base},
            )
            batch = [call["variables"][f"p{i}"] for i in range(n)]
            for value in [self.base, self.head, *batch]:
                self.assertNotIn(value, call["query"])
            queried.extend(batch)
        self.assertEqual(sorted(queried), sorted(paths + ["docs/readme.md", "keep.py", "new.py"]))
        (body,) = fake.comment_posts()
        self.assertIn(f"| @carol | {len(paths)} |", body)

    def test_deleted_and_base_only_files_are_not_queried(self):
        fake = HistoryGitHub({"keep.py": many(carol=1)})
        self.suggest(fake)
        (call,) = fake.history_calls()
        self.assertEqual(
            sorted(v for k, v in call["variables"].items() if k.startswith("p")),
            ["docs/readme.md", "keep.py", "new.py"],
        )

    def test_history_is_read_for_at_most_400_files(self):
        self.head = commit(self.repo, {f"bulk/f{i:03}.py": "z\n" for i in range(410)})
        fake = HistoryGitHub({"keep.py": many(carol=1)})
        code, _, err = self.suggest(fake)
        self.assertEqual(code, 0)
        self.assertEqual(len(fake.history_calls()), 20)
        self.assertIn("Found 413 modified files; reading the history of the first 400", err)

    def test_no_changed_files_makes_no_query(self):
        code, _, err = self.suggest(HistoryGitHub(), env=self.env(head=self.base, base=self.base))
        self.assertEqual(code, 0)
        self.assertIn("No modified files found", err)


class FailureTests(SuggestTestCase):
    def test_api_failures_warn_and_exit_0(self):
        errors = {"errors": [{"message": "Something went wrong"}], "data": None}
        partial = {"errors": [{"message": "bad path"}], "data": {"repository": {"object": {}}}}
        no_commit = {"data": {"repository": {"object": None}}}
        no_repository = {"data": {"repository": None}}
        boom = requests_stub.exceptions.ConnectionError("connection refused")
        cases = {
            "comments 500": dict(comment_status=[500]),
            "comments 403 on page 2": dict(
                comment_status=[200, 403],
                comment_pages=[([], f'<{COMMENTS}?page=2>; rel="next"'), ([], "")],
            ),
            "graphql 502": dict(graphql=[FakeResponse(502, {})]),
            "graphql errors": dict(graphql=[FakeResponse(200, errors)]),
            "graphql partial errors": dict(graphql=[FakeResponse(200, partial)]),
            "graphql unknown commit": dict(graphql=[FakeResponse(200, no_commit)]),
            "graphql no repository": dict(graphql=[FakeResponse(200, no_repository)]),
            "graphql not an object": dict(graphql=[FakeResponse(200, ["x"])]),
            "graphql connection error": dict(graphql=[boom]),
        }
        for name, kwargs in cases.items():
            with self.subTest(name):
                fake = HistoryGitHub({"keep.py": many(carol=1)}, **kwargs)
                code, out, err = self.suggest(fake)
                self.assert_skipped_with_warning(code, out, err, fake)

    def test_a_comments_page_that_is_not_a_list_warns_and_exits_0(self):
        # An object page must not count as a list of its keys, or a second comment could post.
        link = f'<{COMMENTS}?page=2>; rel="next"'
        cases = {
            "null": [(None, "")],
            "number": [(3, "")],
            "object": [({"body": MARKER, "user": OURS_REST}, "")],
            "object on page 2": [([], link), ({"id": 1}, "")],
        }
        for name, pages in cases.items():
            with self.subTest(name):
                fake = HistoryGitHub({"keep.py": many(carol=1)}, comment_pages=pages)
                code, out, err = self.suggest(fake)
                self.assert_skipped_with_warning(code, out, err, fake)
                self.assertIn("not a list", out)
                self.assertEqual(fake.history_calls(), [])

    def test_failed_comment_post_warns_and_exits_0(self):
        for status in (403, 422, 500, 302):
            with self.subTest(status):
                fake = HistoryGitHub({"keep.py": many(carol=1)}, post_status=status)
                code, out, err = self.suggest(fake)
                self.assert_skipped_with_warning(code, out, err, fake, posts=1)
                self.assertIn(f"HTTP {status}", out)

    def test_git_failures_warn_and_exit_0(self):
        unknown = "d" * 40
        not_a_repo = self.make_tmpdir()
        cases = {
            "base not in the checkout": (self.repo, dict(base=unknown)),
            "head not in the checkout": (self.repo, dict(head=unknown)),
            "not a git checkout": (not_a_repo, {}),
        }
        for name, (cwd, env) in cases.items():
            with self.subTest(name):
                os.chdir(cwd)
                fake = HistoryGitHub({"keep.py": many(carol=1)})
                code, out, err = self.suggest(fake, env=self.env(**env))
                self.assert_skipped_with_warning(code, out, err, fake)
                self.assertEqual(fake.history_calls(), [])
                self.assertIn("git merge-base failed", out)

    def test_missing_git_warns_and_exits_0(self):
        fake = HistoryGitHub({"keep.py": many(carol=1)})
        with patch.object(review.subprocess, "check_output", side_effect=FileNotFoundError("git")):
            code, out, err = self.suggest(fake)
        self.assert_skipped_with_warning(code, out, err, fake)

    def test_bad_environment_exits_1_before_any_request(self):
        cases = {
            "no token": self.env(GITHUB_TOKEN=""),
            "no base sha": self.env(base=None),
            "malformed base sha": self.env(base="main"),
            "no author": self.env(author=None),
            "malformed author": self.env(author="bad login"),
            "bad bot login": self.env(AI_REVIEW_BOT_LOGIN="not a login"),
            "http api url": self.env(GITHUB_API_URL="http://api.github.com"),
            "bad repository": self.env(GITHUB_REPOSITORY="o/r/x"),
        }
        for name, env in cases.items():
            with self.subTest(name):
                fake = HistoryGitHub({"keep.py": many(carol=1)})
                code, out, err = self.suggest(fake, env=env)
                self.assertEqual(code, 1)
                self.assertEqual(fake.calls, [])
                self.assertNotIn(TOKEN, out + err)


class PageShapeTests(GitHubTestCase):
    def test_a_later_page_that_is_not_a_list_ends_a_lenient_read(self):
        url = f"{REPO_URL}/issues/7/comments"
        link = f'<{url}?page=2>; rel="next"'
        fake = FakeGitHub(comment_pages=[([comment(1, "a")], link), ({"id": 2}, "")])
        with patch.dict(os.environ, self.env(), clear=True):
            ctx = review._github_load_context()
        with contextlib.redirect_stderr(io.StringIO()) as err:
            results = review._github_paginated_get(fake, ctx, url)
        self.assertEqual([c["id"] for c in results], [1])
        self.assertIn("not a list", err.getvalue())

    def test_a_first_page_that_is_not_a_list_raises(self):
        url = f"{REPO_URL}/issues/7/comments"
        with patch.dict(os.environ, self.env(), clear=True):
            ctx = review._github_load_context()
        for data in (None, 3, {"id": 1}, "text"):
            with self.subTest(data=data):
                fake = FakeGitHub(comment_pages=[(data, "")])
                with self.assertRaises(requests_stub.exceptions.HTTPError):
                    review._github_paginated_get(fake, ctx, url)


class CliTests(SuggestTestCase):
    def test_platform_github_runs_the_github_path(self):
        fake = HistoryGitHub({"keep.py": many(carol=1)})
        argv = ["review.py", "suggest-reviewers", "--platform", "github"]
        with patch.object(sys, "argv", argv):
            code, _, _ = self.call(review.main, fake=fake)
        self.assertEqual(code, 0)
        self.assertEqual(len(fake.comment_posts()), 1)

    def test_default_platform_keeps_the_gitlab_path(self):
        for argv in (
            ["review.py", "suggest-reviewers"],
            ["review.py", "suggest-reviewers", "--platform", "auto"],
        ):
            with self.subTest(argv=argv):
                fake = HistoryGitHub({"keep.py": many(carol=1)})
                with (
                    patch.object(sys, "argv", argv),
                    patch.object(review, "_github_suggest_reviewers") as github,
                ):
                    code, _, err = self.call(review.main, fake=fake)
                # The GitLab command wants its own token and makes no GitHub call.
                self.assertEqual(code, 1)
                self.assertIn("GITLAB_API_TOKEN", err)
                github.assert_not_called()
                self.assertEqual(fake.calls, [])

    def test_unknown_platform_is_a_usage_error(self):
        argv = ["review.py", "suggest-reviewers", "--platform", "gitlab"]
        with patch.object(sys, "argv", argv):
            code, _, _ = self.call(review.main, fake=HistoryGitHub())
        self.assertEqual(code, 2)


if __name__ == "__main__":
    unittest.main()
