"""Tests for the GitHub backend of review.py (`post --platform github`).

Everything runs in-process against a recording stand-in for the HTTP session, so no test
touches the network. Tests that need real commits build a throwaway repository.
"""

import contextlib
import importlib.util
import io
import json
import os
import sys
import unittest
from argparse import Namespace
from pathlib import Path
from types import ModuleType, SimpleNamespace
from unittest.mock import patch

from tests.helpers import PanelTestCase, commit, make_repo, write_json

REVIEW_PATH = Path(__file__).resolve().parents[1] / "skills/gitlab-code-review/scripts/review.py"
SPEC = importlib.util.spec_from_file_location("github_review", REVIEW_PATH)
review = importlib.util.module_from_spec(SPEC)
requests_stub = ModuleType("requests")
requests_stub.Session = type("Session", (), {})
_RequestException = type("RequestException", (Exception,), {})
requests_stub.exceptions = SimpleNamespace(
    RequestException=_RequestException,
    HTTPError=type("HTTPError", (_RequestException,), {}),
    ConnectionError=type("ConnectionError", (_RequestException,), {}),
)
with patch.dict(sys.modules, {"requests": requests_stub}):
    SPEC.loader.exec_module(review)

API = "https://api.github.com"
GRAPHQL = f"{API}/graphql"
REPO_URL = f"{API}/repos/o/r"
HEAD = "a" * 40
OLD = "b" * 40
NEWER = "c" * 40
ZWSP = "\u200b"
OURS_REST = {"login": "github-actions[bot]", "type": "Bot"}
OURS_GQL = {"__typename": "Bot", "login": "github-actions"}
HUMAN_GQL = {"__typename": "User", "login": "bob"}
OTHER_BOT_GQL = {"__typename": "Bot", "login": "renovate"}


def marker(sha: str = HEAD) -> str:
    return f"<!-- ai-review sha:{sha} -->"


class FakeResponse:
    def __init__(self, status=200, data=None, headers=None, text=""):
        self.status_code = status
        self._data = data
        self.headers = headers or {}
        self.text = text

    def json(self):
        return self._data

    def raise_for_status(self):
        if self.status_code >= 400:
            raise requests_stub.exceptions.HTTPError(f"HTTP {self.status_code}")


class FakeGitHub:
    """Recording stand-in for _SafeSession over a small in-memory pull request."""

    def __init__(self, head=HEAD, comment_pages=None, thread_pages=None, inline=None):
        self.head = head
        self.comment_pages = comment_pages if comment_pages is not None else [([], "")]
        self.thread_pages = thread_pages if thread_pages is not None else [([], False)]
        self.graphql_override = None
        self.inline = list(inline or [])  # status per inline POST, then 201
        self.headers = {}
        self.calls = []
        self._comment_gets = 0
        self._thread_posts = 0

    def get(self, url, params=None, **kwargs):
        self.calls.append(("GET", url, {"params": params, **kwargs}))
        if url == f"{REPO_URL}/pulls/7":
            return FakeResponse(200, {"head": {"sha": self.head}})
        data, link = self.comment_pages[min(self._comment_gets, len(self.comment_pages) - 1)]
        self._comment_gets += 1
        return FakeResponse(200, data, {"Link": link} if link else {})

    def post(self, url, json=None, **kwargs):
        self.calls.append(("POST", url, {"json": json, **kwargs}))
        if url == GRAPHQL:
            if self.graphql_override is not None:
                return self.graphql_override
            nodes, has_next = self.thread_pages[min(self._thread_posts, len(self.thread_pages) - 1)]
            self._thread_posts += 1
            connection = {
                "pageInfo": {"hasNextPage": has_next, "endCursor": "next"},
                "nodes": nodes,
            }
            return FakeResponse(
                200, {"data": {"repository": {"pullRequest": {"reviewThreads": connection}}}}
            )
        if url == f"{REPO_URL}/pulls/7/comments":
            return FakeResponse(self.inline.pop(0) if self.inline else 201, {})
        return FakeResponse(201, {})

    def patch(self, url, json=None, **kwargs):
        self.calls.append(("PATCH", url, {"json": json, **kwargs}))
        return FakeResponse(200, {})

    def delete(self, url, **kwargs):
        self.calls.append(("DELETE", url, kwargs))
        return FakeResponse(204)

    def of(self, method, suffix=""):
        return [c for c in self.calls if c[0] == method and c[1].endswith(suffix)]

    def inline_payloads(self):
        return [c[2]["json"] for c in self.of("POST", "/pulls/7/comments")]

    def summary_body(self):
        calls = self.of("PATCH", "/issues/comments/2") or self.of("POST", "/issues/7/comments")
        return calls[-1][2]["json"]["body"]


def comment(comment_id, body, user=OURS_REST):
    return {"id": comment_id, "body": body, "user": user}


def thread(
    root_id,
    *,
    resolved=False,
    outdated=False,
    path="x.py",
    line=10,
    start_line=None,
    replies=(),
    author=OURS_GQL,
    body=None,
    more=False,
):
    root = {
        "fullDatabaseId": str(root_id),
        "author": author,
        "body": body or f"Finding\n\n{marker(OLD)}",
    }
    return {
        "isResolved": resolved,
        "isOutdated": outdated,
        "path": path,
        "line": line,
        "startLine": start_line,
        "comments": {"pageInfo": {"hasNextPage": more}, "nodes": [root, *replies]},
    }


def reply(author, body="ok"):
    return {"fullDatabaseId": "900", "author": author, "body": body}


def finding(file="x.py", line=10, severity="major", text="Bug here", **extra):
    return {"file": file, "line": line, "severity": severity, "comment": text, **extra}


def review_data(findings=(), **extra):
    return {"summary": "Looks fine.", "inline_comments": list(findings), **extra}


class GitHubTestCase(PanelTestCase):
    def setUp(self):
        self.tmp = self.make_tmpdir()
        self.fake = FakeGitHub()

    def env(self, head=HEAD, **overrides):
        event = Path(self.tmp) / "event.json"
        event.write_text(json.dumps({"pull_request": {"number": 7, "head": {"sha": head}}}))
        env = {
            "PATH": os.environ["PATH"],
            "GIT_CONFIG_GLOBAL": "/dev/null",
            "GIT_CONFIG_NOSYSTEM": "1",
            "GITHUB_ACTIONS": "true",
            "GITHUB_TOKEN": "tok",
            "GITHUB_REPOSITORY": "o/r",
            "GITHUB_EVENT_PATH": str(event),
            "GITHUB_SERVER_URL": "https://github.com",
            "GITHUB_RUN_ID": "42",
            "AGENT_TOOL": "claude",
        }
        env.update(overrides)
        return {k: v for k, v in env.items() if v is not None}

    def args(self, data, platform="github"):
        path = write_json(os.path.join(self.tmp, "review.json"), data)
        return Namespace(
            review_file=path, chill=None, inline_fixes=None, verbose=False, platform=platform
        )

    def call(self, func, *args, env=None, fake=None):
        """Run func under the Actions env and the fake session; return (exit code, out, err)."""
        out, err = io.StringIO(), io.StringIO()
        code = 0
        with (
            patch.dict(os.environ, self.env() if env is None else env, clear=True),
            patch.object(review, "_SafeSession", return_value=fake or self.fake),
            contextlib.redirect_stdout(out),
            contextlib.redirect_stderr(err),
        ):
            try:
                func(*args)
            except SystemExit as e:
                code = e.code
        return code, out.getvalue(), err.getvalue()

    def post(self, data, env=None):
        return self.call(review._github_post, self.args(data), env=env)

    def context(self):
        with patch.dict(os.environ, self.env(), clear=True):
            return review._github_load_context()


class OptInTests(GitHubTestCase):
    def test_auto_in_a_pull_request_env_displays_and_makes_no_request(self):
        args = self.args(review_data([finding()]), platform="auto")
        with patch.object(review, "_SafeSession", side_effect=AssertionError("network")):
            code, out, _ = self.call(review.cmd_post, args)
        self.assertEqual(code, 0)
        self.assertIn("AI CODE REVIEW", out)
        self.assertEqual(self.fake.calls, [])

    def test_github_platform_exits_1_on_unusable_env(self):
        push = Path(self.tmp) / "push.json"
        push.write_text(json.dumps({"ref": "refs/heads/main"}))
        cases = {
            "no token": self.env(GITHUB_TOKEN=""),
            "push payload": self.env(GITHUB_EVENT_PATH=str(push)),
            "no event path": self.env(GITHUB_EVENT_PATH=None),
            "http api url": self.env(GITHUB_API_URL="http://api.github.com"),
            "graphql on another host": self.env(GITHUB_GRAPHQL_URL="https://evil.example/graphql"),
            "http graphql url": self.env(GITHUB_GRAPHQL_URL="http://api.github.com/graphql"),
            "bad repository": self.env(GITHUB_REPOSITORY="not a repo"),
        }
        args = self.args(review_data())
        for name, env in cases.items():
            with self.subTest(name):
                fake = FakeGitHub()
                code, _, _ = self.call(review.cmd_post, args, env=env, fake=fake)
                self.assertEqual(code, 1)
                self.assertEqual(fake.calls, [])

    def test_gitlab_env_wins_under_auto(self):
        env = self.env(CI="true", CI_MERGE_REQUEST_IID="3")
        with (
            patch.object(review, "_gitlab_post") as gitlab,
            patch.object(review, "_github_post") as github,
        ):
            code, _, _ = self.call(review.cmd_post, self.args(review_data(), "auto"), env=env)
        self.assertEqual(code, 0)
        gitlab.assert_called_once()
        github.assert_not_called()

    def test_cli_accepts_platform_github_and_rejects_other_values(self):
        path = write_json(os.path.join(self.tmp, "review.json"), review_data())
        env = self.env(GITHUB_TOKEN="")
        for platform, expected in (("github", 1), ("gitlab", 2)):
            with self.subTest(platform):
                argv = ["review.py", "post", path, "--platform", platform]
                with patch.object(sys, "argv", argv):
                    code, _, _ = self.call(review.main, env=env)
                self.assertEqual(code, expected)


class EventPayloadTests(GitHubTestCase):
    def test_bad_pull_request_payload_exits_1(self):
        cases = {
            "not json": "{",
            "number zero": {"pull_request": {"number": 0, "head": {"sha": HEAD}}},
            "number bool": {"pull_request": {"number": True, "head": {"sha": HEAD}}},
            "short sha": {"pull_request": {"number": 7, "head": {"sha": "abc"}}},
            "uppercase sha": {"pull_request": {"number": 7, "head": {"sha": "A" * 40}}},
            "no head": {"pull_request": {"number": 7}},
        }
        args = self.args(review_data())
        for name, payload in cases.items():
            with self.subTest(name):
                event = Path(self.tmp) / "bad-event.json"
                event.write_text(payload if isinstance(payload, str) else json.dumps(payload))
                fake = FakeGitHub()
                env = self.env(GITHUB_EVENT_PATH=str(event))
                code, _, _ = self.call(review.cmd_post, args, env=env, fake=fake)
                self.assertEqual(code, 1)
                self.assertEqual(fake.calls, [])
        fake = FakeGitHub()
        env = self.env(GITHUB_EVENT_PATH=str(Path(self.tmp) / "missing.json"))
        code, _, _ = self.call(review.cmd_post, args, env=env, fake=fake)
        self.assertEqual(code, 1)
        self.assertEqual(fake.calls, [])


class StaleHeadTests(GitHubTestCase):
    def test_moved_head_exits_0_before_touching_anything(self):
        self.fake.head = NEWER
        code, out, _ = self.post(review_data([finding()]))
        self.assertEqual(code, 0)
        self.assertEqual([c[:2] for c in self.fake.calls], [("GET", f"{REPO_URL}/pulls/7")])
        self.assertIn("::notice::", out)


class PullRequestLookupTests(GitHubTestCase):
    def test_pr_lookup_failure_or_missing_head_exits_1_before_any_write(self):
        cases = (("404", FakeResponse(404)), ("no head", FakeResponse(200, {"head": {}})))
        for name, response in cases:
            with self.subTest(name):
                fake = FakeGitHub()
                original_get = fake.get

                def get(url, params=None, _orig=original_get, _resp=response, _fake=fake, **kwargs):
                    if url == f"{REPO_URL}/pulls/7":
                        _fake.calls.append(("GET", url, {}))
                        return _resp
                    return _orig(url, params, **kwargs)

                fake.get = get
                code, _, _ = self.call(
                    review._github_post, self.args(review_data([finding()])), fake=fake
                )
                self.assertEqual(code, 1)
                self.assertEqual([c[:2] for c in fake.calls], [("GET", f"{REPO_URL}/pulls/7")])


class PaginationTests(GitHubTestCase):
    def paginate(self, link):
        fake = FakeGitHub(comment_pages=[([comment(1, "a")], link), ([comment(2, "b")], "")])
        url = f"{REPO_URL}/issues/7/comments"
        with contextlib.redirect_stderr(io.StringIO()):
            return fake, review._github_paginated_get(fake, self.context(), url)

    def test_follows_a_same_host_https_next_link(self):
        next_url = f"{REPO_URL}/issues/7/comments?page=2"
        fake, results = self.paginate(f'<{next_url}>; rel="next"')
        self.assertEqual([c["id"] for c in results], [1, 2])
        self.assertEqual(fake.calls[0][2]["params"], {"per_page": 100})
        self.assertEqual(fake.calls[1][1], next_url)

    def test_stops_on_another_host_or_plain_http(self):
        for link in (
            '<https://evil.example/repos/o/r/issues/7/comments?page=2>; rel="next"',
            '<http://api.github.com/repos/o/r/issues/7/comments?page=2>; rel="next"',
        ):
            with self.subTest(link):
                fake, results = self.paginate(link)
                self.assertEqual([c["id"] for c in results], [1])
                self.assertEqual(len(fake.calls), 1)


class PaginationFailureTests(GitHubTestCase):
    def test_comment_list_failure_exits_1_without_writes(self):
        fake = FakeGitHub()
        original_get = fake.get

        def get(url, params=None, **kwargs):
            if url.endswith("/issues/7/comments"):
                fake.calls.append(("GET", url, {}))
                return FakeResponse(500)
            return original_get(url, params, **kwargs)

        fake.get = get
        code, _, _ = self.call(review._github_post, self.args(review_data([finding()])), fake=fake)
        self.assertEqual(code, 1)
        self.assertEqual(fake.of("POST") + fake.of("PATCH") + fake.of("DELETE"), [])

    def test_a_failing_later_page_keeps_earlier_results(self):
        next_url = f"{REPO_URL}/issues/7/comments?page=2"
        fake = FakeGitHub(comment_pages=[([comment(1, "a")], f'<{next_url}>; rel="next"')])
        original_get = fake.get

        def get(url, params=None, **kwargs):
            if url == next_url:
                return FakeResponse(502)
            return original_get(url, params, **kwargs)

        fake.get = get
        with contextlib.redirect_stderr(io.StringIO()):
            results = review._github_paginated_get(
                fake, self.context(), f"{REPO_URL}/issues/7/comments"
            )
        self.assertEqual([c["id"] for c in results], [1])


class LaterPageFailureTests(GitHubTestCase):
    def test_a_failing_later_comment_page_exits_1_without_writes(self):
        next_url = f"{REPO_URL}/issues/7/comments?page=2"
        fake = FakeGitHub(comment_pages=[([comment(1, "a")], f'<{next_url}>; rel="next"')])
        original_get = fake.get

        def get(url, params=None, **kwargs):
            if url == next_url:
                fake.calls.append(("GET", url, {}))
                return FakeResponse(502)
            return original_get(url, params, **kwargs)

        fake.get = get
        code, _, _ = self.call(review._github_post, self.args(review_data([finding()])), fake=fake)
        self.assertEqual(code, 1)
        self.assertEqual(fake.of("POST") + fake.of("PATCH") + fake.of("DELETE"), [])


class OwnershipTests(GitHubTestCase):
    def find(self, comments):
        fake = FakeGitHub(comment_pages=[(comments, "")])
        return review._github_find_summary(fake, self.context())

    def test_newest_comment_of_ours_wins(self):
        comments = [comment(1, f"old\n\n{marker(OLD)}"), comment(2, f"new\n\n{marker(NEWER)}")]
        self.assertEqual(self.find(comments), (2, NEWER))

    def test_other_authors_and_buried_markers_are_ignored(self):
        comments = [
            comment(1, f"mine\n\n{marker(OLD)}"),
            comment(3, f"human\n\n{marker(HEAD)}", {"login": "bob", "type": "User"}),
            comment(4, f"bot\n\n{marker(HEAD)}", {"login": "renovate[bot]", "type": "Bot"}),
            comment(5, f"```\n{marker(HEAD)}\n```\nmore text"),
            comment(
                6, f"spoof\n\n{marker(HEAD)}", {"login": "github-actions[bot]", "type": "User"}
            ),
            comment(7, "no marker at all"),
        ]
        self.assertEqual(self.find(comments), (1, OLD))

    def test_no_comment_of_ours(self):
        self.assertEqual(
            self.find([comment(3, marker(), {"login": "bob", "type": "User"})]), (None, None)
        )


class SummaryTests(GitHubTestCase):
    def test_patches_our_newest_comment(self):
        self.fake.comment_pages = [([comment(2, f"old\n\n{marker(OLD)}")], "")]
        code, _, _ = self.post(review_data())
        self.assertEqual(code, 0)
        self.assertEqual(len(self.fake.of("PATCH", "/issues/comments/2")), 1)
        self.assertEqual(self.fake.of("POST", "/issues/7/comments"), [])

    def test_posts_when_only_someone_elses_marker_exists(self):
        human = {"login": "bob", "type": "User"}
        self.fake.comment_pages = [([comment(2, f"x\n\n{marker(OLD)}", human)], "")]
        code, _, _ = self.post(review_data())
        self.assertEqual(code, 0)
        self.assertEqual(self.fake.of("PATCH"), [])
        self.assertEqual(len(self.fake.of("POST", "/issues/7/comments")), 1)

    def test_body_is_capped_with_the_marker_intact(self):
        data = review_data()
        data["summary"] = "word " * 20000
        code, _, _ = self.post(data)
        body = self.fake.summary_body()
        self.assertEqual(code, 0)
        self.assertLessEqual(len(body), 65536)
        self.assertTrue(body.endswith(marker()))

    def test_footer_carries_the_run_url_and_effort(self):
        env = self.env(AGENT_REASONING_EFFORT="high", AGENT_MODEL="m1")
        self.post(review_data(), env=env)
        body = self.fake.summary_body()
        self.assertIn("https://github.com/o/r/actions/runs/42", body)
        self.assertIn("Effort: high", body)
        self.assertIn("Model: m1", body)
        self.assertIn("Harness: claude-code", body)

    def test_hostile_path_is_neutralized_inside_a_code_span(self):
        path = f"dir/@org/team/{'<!-- ai-review sha:abc -->'}.py"
        self.fake.inline = [422]
        self.post(review_data([finding(file=path)]))
        body = self.fake.summary_body()
        self.assertIn("Findings not posted inline", body)
        self.assertEqual(body.count("<!-- ai-review"), 1)
        self.assertIn(f"<{ZWSP}!-- ai-review sha:abc -->", body)
        self.assertTrue(body.endswith("<!-- ai-review -->"))

    def test_a_scoped_path_keeps_its_at_sign_inside_the_code_span(self):
        self.fake.inline = [422]
        self.post(review_data([finding(file="node_modules/@scope/pkg/index.js")]))
        body = self.fake.summary_body()
        self.assertIn("` node_modules/@scope/pkg/index.js `:10", body)
        self.assertNotIn(ZWSP, body)

    def test_counts_cover_posted_findings_only(self):
        fix = {"start_line": 5, "end_line": 5, "code": "y = 1"}
        findings = [finding(line=5, fix=fix), finding(line=6, severity="minor")]
        code, _, _ = self.post(review_data(findings))
        body = self.fake.summary_body()
        self.assertEqual(code, 0)
        self.assertIn("Issues Found (2 total)", body)
        self.assertIn("With applicable suggestion: 1", body)


class SummaryFailureTests(GitHubTestCase):
    def test_summary_failure_exits_1_after_the_inline_comments(self):
        self.fake.comment_pages = [([comment(2, f"old\n\n{marker(OLD)}")], "")]

        def failing_patch(url, json=None, **kwargs):
            self.fake.calls.append(("PATCH", url, {"json": json}))
            return FakeResponse(500, text="boom")

        self.fake.patch = failing_patch
        code, _, _ = self.post(review_data([finding()]))
        self.assertEqual(code, 1)
        self.assertEqual(len(self.fake.inline_payloads()), 1)
        self.assertEqual(len(self.fake.of("PATCH", "/issues/comments/2")), 1)


class SummaryTextTests(GitHubTestCase):
    def test_summary_and_positive_aspects_are_neutralized(self):
        data = review_data(positive_aspects=["Thanks @alice", "<!-- ai-review sha:abc -->"])
        data["summary"] = "cc @org/team"
        self.post(data)
        body = self.fake.summary_body()
        self.assertIn("### Positive Aspects", body)
        self.assertIn(f"- Thanks @{ZWSP}alice", body)
        self.assertIn(f"cc @{ZWSP}org/team", body)
        self.assertEqual(body.count("<!-- ai-review"), 1)
        self.assertTrue(body.endswith(marker()))

    def test_unanchored_multi_line_comment_stays_in_its_list_item(self):
        self.fake.inline = [422]
        self.post(review_data([finding(text="first\nsecond @bob")]))
        self.assertIn(f"first\n  second @{ZWSP}bob", self.fake.summary_body())


class RunLinkTests(GitHubTestCase):
    def test_no_run_link_without_an_https_server_and_numeric_run_id(self):
        for overrides in (
            {"GITHUB_SERVER_URL": "http://github.com"},
            {"GITHUB_RUN_ID": "abc"},
            {"GITHUB_SERVER_URL": None},
        ):
            with self.subTest(overrides):
                self.fake = FakeGitHub()
                self.post(review_data(), env=self.env(**overrides))
                self.assertNotIn("Run:", self.fake.summary_body())


class CleanupTests(GitHubTestCase):
    def cleanup(self, threads, dropped=()):
        fake = FakeGitHub(thread_pages=[(threads, False)])
        out = io.StringIO()
        with contextlib.redirect_stdout(out), contextlib.redirect_stderr(io.StringIO()):
            review._github_delete_previous_threads(fake, self.context(), set(dropped))
        return [c[1].rsplit("/", 1)[-1] for c in fake.of("DELETE")]

    def test_deletes_only_our_unresolved_threads_without_replies(self):
        threads = [
            thread(101),
            thread(102, resolved=True),
            thread(103, replies=[reply(HUMAN_GQL)]),
            thread(104, replies=[reply(None)]),
            thread(105, author=OTHER_BOT_GQL),
            thread(106, author=HUMAN_GQL),
            thread(107, more=True),
            thread(108, body="no marker"),
            thread(109, replies=[reply(OURS_GQL, f"hi\n\n{marker(OLD)}")]),
        ]
        self.assertEqual(self.cleanup(threads), ["101", "109"])

    def test_follows_page_info(self):
        fake = FakeGitHub(thread_pages=[([thread(101)], True), ([thread(102)], False)])
        with contextlib.redirect_stdout(io.StringIO()), contextlib.redirect_stderr(io.StringIO()):
            review._github_delete_previous_threads(fake, self.context(), set())
        posts = fake.of("POST", "/graphql")
        self.assertEqual([p[2]["json"]["variables"]["after"] for p in posts], [None, "next"])
        self.assertEqual(len(fake.of("DELETE")), 2)

    def test_graphql_errors_warn_and_delete_nothing(self):
        for payload in ({"errors": [{"message": "boom"}], "data": None}, {"data": None}, {}):
            with self.subTest(payload=payload):
                fake = FakeGitHub()
                fake.graphql_override = FakeResponse(200, payload)
                out = io.StringIO()
                with contextlib.redirect_stdout(out), contextlib.redirect_stderr(io.StringIO()):
                    review._github_delete_previous_threads(fake, self.context(), set())
                self.assertEqual(fake.of("DELETE"), [])
                self.assertIn("::warning::", out.getvalue())

    def test_graphql_failure_does_not_fail_the_run(self):
        self.fake.graphql_override = FakeResponse(200, {"errors": [{"message": "x"}], "data": None})
        self.fake.thread_pages = [([thread(101)], False)]
        code, out, _ = self.post(review_data())
        self.assertEqual(code, 0)
        self.assertIn("::warning::", out)
        self.assertEqual(self.fake.of("DELETE"), [])

    def test_live_duplicate_of_a_dropped_finding_is_kept_unless_outdated(self):
        threads = [
            thread(101, path="x.py", line=10),
            thread(102, path="x.py", line=10, outdated=True),
            thread(103, path="x.py", line=11),
        ]
        self.assertEqual(self.cleanup(threads, dropped={("x.py", 10)}), ["102", "103"])

    def test_thread_spanning_a_dropped_line_is_kept_even_without_a_matching_fix(self):
        threads = [
            thread(101, path="x.py", line=11, start_line=9),
            thread(102, path="x.py", line=14, start_line=12),
            thread(103, path="x.py", line=9),
            thread(104, path="y.py", line=11, start_line=9),
            thread(105, path="x.py", line=11, start_line=9, outdated=True),
        ]
        self.assertEqual(
            self.cleanup(threads, dropped={("x.py", 10)}), ["102", "103", "104", "105"]
        )


class BadJson(FakeResponse):
    def json(self):
        raise ValueError("not json")


class ThreadIdTests(GitHubTestCase):
    def test_the_query_asks_for_the_start_line(self):
        self.assertIn("startLine", review._GITHUB_THREADS_QUERY)

    def test_the_query_asks_for_the_64_bit_id(self):
        self.assertIn("fullDatabaseId", review._GITHUB_THREADS_QUERY)
        self.assertNotIn(" databaseId", review._GITHUB_THREADS_QUERY)

    def test_an_id_past_32_bits_reaches_the_delete_url(self):
        big = 4_698_635_657
        self.fake.thread_pages = [([thread(big)], False)]
        code, _, _ = self.post(review_data())
        self.assertEqual(code, 0)
        self.assertEqual(
            [c[1] for c in self.fake.of("DELETE")], [f"{REPO_URL}/pulls/comments/{big}"]
        )

    def test_a_malformed_id_keeps_the_thread(self):
        for bad in (None, 12, "12x", "-3", "", "0", "9" * 4301, str(2**63)):
            with self.subTest(bad=bad):
                node = thread(101)
                node["comments"]["nodes"][0]["fullDatabaseId"] = bad
                self.assertIsNone(review._github_thread_root_to_delete(node, set()))


class CleanupSkipTests(GitHubTestCase):
    def test_http_error_bad_json_or_missing_pull_request_skip_cleanup(self):
        cases = {
            "http 502": FakeResponse(502),
            "not json": BadJson(200),
            "no pull request": FakeResponse(200, {"data": {"repository": {"pullRequest": None}}}),
        }
        for name, response in cases.items():
            with self.subTest(name):
                fake = FakeGitHub(thread_pages=[([thread(101)], False)])
                fake.graphql_override = response
                out = io.StringIO()
                with contextlib.redirect_stdout(out), contextlib.redirect_stderr(io.StringIO()):
                    review._github_delete_previous_threads(fake, self.context(), set())
                self.assertEqual(fake.of("DELETE"), [])
                self.assertIn("::warning::", out.getvalue())


class DeleteFailureTests(GitHubTestCase):
    def test_failed_delete_warns_and_the_run_still_succeeds(self):
        self.fake.thread_pages = [([thread(101), thread(102, line=11)], False)]
        statuses = [403, 204]

        def delete(url, **kwargs):
            self.fake.calls.append(("DELETE", url, kwargs))
            return FakeResponse(statuses.pop(0))

        self.fake.delete = delete
        code, _, _ = self.post(review_data([finding(line=12)]))
        self.assertEqual(code, 0)
        self.assertEqual(len(self.fake.of("DELETE")), 2)
        self.assertEqual([p["line"] for p in self.fake.inline_payloads()], [12])


class DedupAcrossRunsTests(GitHubTestCase):
    def test_unchanged_finding_keeps_its_thread_and_outdated_ones_go(self):
        repo, _ = make_repo(self.tmp, {"x.py": "".join(f"line{i}\n" for i in range(1, 31))})
        first = commit(repo, {"x.py": "".join(f"line{i}\n" for i in range(1, 31)) + "extra\n"})
        lines = [f"line{i}\n" for i in range(1, 31)] + ["extra\n"]
        lines[19] = "changed\n"
        second = commit(repo, {"x.py": "".join(lines)})
        self.fake.comment_pages = [([comment(2, f"s\n\n{marker(first)}")], "")]
        self.fake.thread_pages = [
            (
                [
                    thread(101, path="x.py", line=10),
                    thread(102, path="x.py", line=10, outdated=True),
                    thread(103, path="x.py", line=5),
                ],
                False,
            )
        ]
        data = review_data([finding(line=10), finding(line=20, text="New bug")])
        old_cwd = os.getcwd()
        self.addCleanup(os.chdir, old_cwd)
        os.chdir(repo)
        env = self.env(head=second)
        self.fake.head = second
        code, _, _ = self.post(data, env=env)
        self.assertEqual(code, 0)
        deleted = [c[1].rsplit("/", 1)[-1] for c in self.fake.of("DELETE")]
        self.assertEqual(deleted, ["102", "103"])
        self.assertEqual([p["line"] for p in self.fake.inline_payloads()], [20])


class DedupMultiLineFixTests(GitHubTestCase):
    def test_dropped_multi_line_fix_keeps_its_thread_at_the_end_line(self):
        base = "".join(f"line{i}\n" for i in range(1, 31))
        repo, _ = make_repo(self.tmp, {"x.py": base})
        first = commit(repo, {"x.py": base + "extra\n"})
        lines = [f"line{i}\n" for i in range(1, 31)] + ["extra\n"]
        lines[19] = "changed\n"
        second = commit(repo, {"x.py": "".join(lines)})
        self.fake.comment_pages = [([comment(2, f"s\n\n{marker(first)}")], "")]
        self.fake.thread_pages = [
            ([thread(101, path="x.py", line=11), thread(102, path="x.py", line=5)], False)
        ]
        fix = {"start_line": 9, "end_line": 11, "code": "a = 1"}
        self.addCleanup(os.chdir, os.getcwd())
        os.chdir(repo)
        self.fake.head = second
        code, _, _ = self.post(review_data([finding(line=10, fix=fix)]), env=self.env(head=second))
        self.assertEqual(code, 0)
        self.assertEqual([c[1].rsplit("/", 1)[-1] for c in self.fake.of("DELETE")], ["102"])
        self.assertEqual(self.fake.inline_payloads(), [])


class SameHeadRerunTests(GitHubTestCase):
    def test_rerun_on_the_same_head_skips_dedup_and_replaces_old_threads(self):
        self.fake.comment_pages = [([comment(2, f"s\n\n{marker(HEAD)}")], "")]
        self.fake.thread_pages = [([thread(101, line=10)], False)]
        with patch.object(
            review, "filter_already_reviewed", side_effect=AssertionError("dedup ran")
        ):
            code, _, _ = self.post(review_data([finding(line=10)]))
        self.assertEqual(code, 0)
        self.assertEqual([c[1].rsplit("/", 1)[-1] for c in self.fake.of("DELETE")], ["101"])
        self.assertEqual([p["line"] for p in self.fake.inline_payloads()], [10])
        self.assertEqual(len(self.fake.of("PATCH", "/issues/comments/2")), 1)


class InlineTests(GitHubTestCase):
    def test_single_line_payload(self):
        code, _, _ = self.post(review_data([finding(line=12)]))
        (payload,) = self.fake.inline_payloads()
        self.assertEqual(code, 0)
        self.assertEqual(
            {k: payload[k] for k in ("commit_id", "path", "line", "side")},
            {"commit_id": HEAD, "path": "x.py", "line": 12, "side": "RIGHT"},
        )
        self.assertNotIn("start_line", payload)
        self.assertTrue(payload["body"].endswith(marker()))

    def test_multi_line_fix_anchors_on_its_range_with_a_plain_suggestion_fence(self):
        fix = {"start_line": 10, "end_line": 12, "code": "a = 1\nb = 2"}
        self.post(review_data([finding(line=11, fix=fix)]))
        (payload,) = self.fake.inline_payloads()
        self.assertEqual(
            {k: payload[k] for k in ("start_line", "line", "start_side")},
            {"start_line": 10, "line": 12, "start_side": "RIGHT"},
        )
        self.assertIn("```suggestion\na = 1\nb = 2\n```", payload["body"])

    def test_422_retries_without_the_fix_and_the_summary_count_follows(self):
        fix = {"start_line": 10, "end_line": 12, "code": "a = 1"}
        self.fake.inline = [422, 201]
        code, _, _ = self.post(review_data([finding(line=11, fix=fix)]))
        first, second = self.fake.inline_payloads()
        self.assertEqual(code, 0)
        self.assertIn("suggestion", first["body"])
        self.assertNotIn("suggestion", second["body"])
        self.assertNotIn("start_line", second)
        self.assertEqual(second["line"], 11)
        self.assertNotIn("With applicable suggestion", self.fake.summary_body())

    def test_422_twice_lists_the_finding_and_still_exits_0(self):
        fix = {"start_line": 11, "end_line": 11, "code": "a = 1"}
        self.fake.inline = [422, 422]
        data = review_data([finding(line=11, fix=fix)], fix_prompt="Fix everything")
        code, _, _ = self.post(data)
        body = self.fake.summary_body()
        self.assertEqual(code, 0)
        self.assertIn("Findings not posted inline", body)
        self.assertIn("x.py", body)
        self.assertIn("Fix all issues with AI agents", body)

    def test_server_error_exits_1_after_posting_the_summary(self):
        self.fake.inline = [500]
        code, _, _ = self.post(review_data([finding()], fix_prompt="Fix everything"))
        self.assertEqual(code, 1)
        self.assertEqual(len(self.fake.of("POST", "/issues/7/comments")), 1)
        self.assertNotIn("Fix all issues", self.fake.summary_body())

    def test_oversized_comment_is_capped_with_the_marker_kept(self):
        self.post(review_data([finding(text="word " * 20000)]))
        (payload,) = self.fake.inline_payloads()
        self.assertLessEqual(len(payload["body"]), 65536)
        self.assertTrue(payload["body"].endswith(marker()))

    def test_oversized_comment_drops_its_fix_instead_of_cutting_it(self):
        fix = {"start_line": 10, "end_line": 10, "code": "a = 1"}
        self.post(review_data([finding(text="word " * 20000, fix=fix)]))
        (payload,) = self.fake.inline_payloads()
        self.assertLessEqual(len(payload["body"]), 65536)
        self.assertNotIn("```suggestion", payload["body"])
        self.assertNotIn("With applicable suggestion", self.fake.summary_body())

    def test_fix_prompt_stays_when_everything_posted(self):
        self.post(review_data([finding()], fix_prompt="Fix everything"))
        self.assertIn("Fix all issues with AI agents", self.fake.summary_body())


class HostileModelTextTests(GitHubTestCase):
    def test_a_bare_fence_in_the_comment_cannot_hide_a_mention_behind_the_prefix(self):
        self.post(review_data([finding(text="```\n@victim please look")]))
        body = self.fake.inline_payloads()[0]["body"]
        self.assertIn(f"@{ZWSP}victim", body)

    def test_a_bare_fence_in_an_unanchored_comment_cannot_hide_a_mention(self):
        self.fake.inline = [422]
        self.post(review_data([finding(text="```\n@victim please look")]))
        self.assertIn(f"@{ZWSP}victim", self.fake.summary_body())

    def test_a_path_with_a_control_character_is_dropped_and_never_printed_raw(self):
        evil = "x.py\n::add-mask::secret"
        code, out, err = self.post(review_data([finding(file=evil), finding(line=12)]))
        self.assertEqual(code, 0)
        self.assertEqual([p["line"] for p in self.fake.inline_payloads()], [12])
        self.assertNotIn("::add-mask::", out + err)


class RenderedBodyTests(GitHubTestCase):
    def test_a_positive_aspect_that_opens_a_fence_cannot_hide_a_mention(self):
        self.post(review_data(positive_aspects=["```\n@victim"]))
        self.assertIn(f"@{ZWSP}victim", self.fake.summary_body())

    def test_a_fence_left_open_in_the_summary_cannot_expose_the_fix_prompt(self):
        data = review_data([finding()], fix_prompt="please @org/team fix")
        data["summary"] = "ok\n```"
        self.post(data)
        body = self.fake.summary_body()
        before_prompt = body.split("Copy-paste this")[0]
        self.assertEqual(before_prompt.count("```") % 2, 0)

    def test_null_summary_fields_do_not_crash_the_poster(self):
        data = review_data([finding()], positive_aspects=None, fix_prompt=["not", "a string"])
        data["summary"] = None
        code, _, _ = self.post(data)
        self.assertEqual(code, 0)
        self.assertIn("No summary provided.", self.fake.summary_body())
        self.assertNotIn("Fix all issues", self.fake.summary_body())

    def test_a_redirect_while_listing_comments_exits_1_without_writes(self):
        fake = FakeGitHub()
        original_get = fake.get

        def get(url, params=None, **kwargs):
            if url.endswith("/issues/7/comments"):
                fake.calls.append(("GET", url, {}))
                return FakeResponse(301)
            return original_get(url, params, **kwargs)

        fake.get = get
        code, _, _ = self.call(review._github_post, self.args(review_data([finding()])), fake=fake)
        self.assertEqual(code, 1)
        self.assertEqual(fake.of("POST") + fake.of("PATCH") + fake.of("DELETE"), [])


class EnvSwitchTests(GitHubTestCase):
    def test_inline_fixes_off_posts_prose_only(self):
        fix = {"start_line": 10, "end_line": 12, "code": "a = 1"}
        self.post(review_data([finding(line=11, fix=fix)]), env=self.env(INLINE_FIXES="false"))
        (payload,) = self.fake.inline_payloads()
        self.assertNotIn("```suggestion", payload["body"])
        self.assertNotIn("start_line", payload)
        self.assertEqual(payload["line"], 11)
        self.assertNotIn("With applicable suggestion", self.fake.summary_body())

    def test_chill_mode_controls_suggestion_level_findings(self):
        for chill, expected in (("true", 0), ("false", 1)):
            with self.subTest(chill):
                self.fake = FakeGitHub()
                self.post(
                    review_data([finding(severity="suggestion")]), env=self.env(CHILL_MODE=chill)
                )
                self.assertEqual(len(self.fake.inline_payloads()), expected)


class AnnotationTests(unittest.TestCase):
    def test_annotation_escapes_percent_and_newlines(self):
        out = io.StringIO()
        with contextlib.redirect_stdout(out):
            review._github_annotation("warning", "50% done\r\n::error::x")
        self.assertEqual(out.getvalue(), "::warning::50%25 done%0D%0A::error::x\n")


class TextSafetyTests(unittest.TestCase):
    def test_mentions_and_comment_openers_get_a_zero_width_space(self):
        text = review._github_text("hi @user, @org/team, &#64;user, &#x40;user and &commat;user")
        for expected in (
            f"@{ZWSP}user,",
            f"@{ZWSP}org/team",
            f"&#64;{ZWSP}user",
            f"&#x40;{ZWSP}user",
            f"&commat;{ZWSP}user",
        ):
            self.assertIn(expected, text)

    def test_comment_opener_is_defused_everywhere(self):
        text = review._github_text("a <!-- b\n```\n<!-- ai-review -->\n```\n`<!-- x -->`")
        self.assertNotIn("<!--", text)

    def test_code_stays_verbatim(self):
        for text in (
            "`@property` is fine",
            "```python\n@pytest.fixture\n```",
            "~~~\n@scope/pkg\n~~~",
            "mail a@b.com",
            "install pkg@1.0",
            "`@types/node` and `` `@x` ``",
        ):
            with self.subTest(text):
                self.assertEqual(review._github_text(text), text)

    def test_mention_after_a_fence_is_defused(self):
        text = review._github_text("```\n@a\n```\n@b")
        self.assertEqual(text, f"```\n@a\n```\n@{ZWSP}b")

    def test_escaped_backtick_does_not_open_a_code_span(self):
        for text, expected in (
            ("see \\`@org/team` now", f"see \\`@{ZWSP}org/team` now"),
            ("\\\\`@org/team` now", "\\\\`@org/team` now"),
        ):
            with self.subTest(text):
                self.assertEqual(review._github_text(text), expected)

    def test_backtick_fence_with_a_backtick_in_its_info_string_is_not_a_fence(self):
        text = review._github_text("```x`\n@org/team\n```")
        # The last line opens a fence nothing closes, so it is closed for the next fragment.
        self.assertEqual(text, f"```x`\n@{ZWSP}org/team\n```\n```")

    def test_fix_prompt_stays_inside_a_longer_fence(self):
        body = review._github_build_summary_body(
            review_data(fix_prompt="x ```` y\n@user"), marker()
        )
        self.assertIn("`````\nx ```` y\n@user\n`````", body)

    def test_suggestion_code_is_left_verbatim(self):
        fix = {"code": "x = '@user'  # <!-- c -->\n"}
        self.assertEqual(
            review._github_format_fix_block(fix), "```suggestion\nx = '@user'  # <!-- c -->\n```"
        )
        self.assertEqual(review._github_format_fix_block({"code": ""}), "```suggestion\n```")

    def test_code_span_outlasts_backticks_in_the_path(self):
        self.assertEqual(review._github_code_span("a`b``c"), "``` a`b``c ```")

    def test_cap_closes_a_fence_it_cut_open(self):
        text = "intro\n```\n" + "code\n" * 100
        capped = review._github_cap(text, marker(), limit=200)
        self.assertLessEqual(len(capped), 200)
        self.assertTrue(capped.endswith(marker()))
        self.assertIn("\n```\n\n*(truncated)*", capped)


class EndToEndTests(GitHubTestCase):
    def kinds(self):
        names = {
            ("GET", f"{REPO_URL}/pulls/7"): "pr",
            ("GET", f"{REPO_URL}/issues/7/comments"): "list-comments",
            ("POST", GRAPHQL): "threads",
            ("POST", f"{REPO_URL}/pulls/7/comments"): "inline",
            ("PATCH", f"{REPO_URL}/issues/comments/2"): "summary-patch",
            ("DELETE", f"{REPO_URL}/pulls/comments/101"): "delete",
        }
        return [names[c[:2]] for c in self.fake.calls]

    def setup_pr(self):
        self.fake.comment_pages = [([comment(2, f"old\n\n{marker(OLD)}")], "")]
        self.fake.thread_pages = [([thread(101, path="gone.py", line=3, outdated=True)], False)]

    def test_call_order_and_counts_when_everything_posts(self):
        self.setup_pr()
        fix = {"start_line": 5, "end_line": 5, "code": "y = 1"}
        data = review_data([finding(line=5, fix=fix), finding(line=6, severity="minor")])
        code, _, _ = self.post(data)
        self.assertEqual(code, 0)
        self.assertEqual(
            self.kinds(),
            ["pr", "list-comments", "threads", "delete", "inline", "inline", "summary-patch"],
        )
        body = self.fake.summary_body()
        self.assertIn("Issues Found (2 total)", body)
        self.assertIn("With applicable suggestion: 1", body)

    def test_unanchored_finding_exits_0(self):
        self.setup_pr()
        self.fake.inline = [422]
        code, _, _ = self.post(review_data([finding()]))
        self.assertEqual(code, 0)
        self.assertIn("Findings not posted inline", self.fake.summary_body())

    def test_one_server_error_exits_1(self):
        self.setup_pr()
        self.fake.inline = [201, 500]
        code, _, _ = self.post(review_data([finding(line=5), finding(line=6)]))
        self.assertEqual(code, 1)
        self.assertIn("Issues Found (1 total)", self.fake.summary_body())


class SummaryMarkerTests(GitHubTestCase):
    """A summary that hides findings must not carry the SHA, or the next run dedups them away."""

    NO_SHA = "<!-- ai-review -->"

    def test_a_422_leaves_a_sha_less_marker_on_the_summary_only(self):
        self.fake.inline = [201, 422]
        code, _, _ = self.post(review_data([finding(line=5), finding(line=6)]))
        self.assertEqual(code, 0)
        posted, _ = self.fake.inline_payloads()
        self.assertTrue(posted["body"].endswith(marker()))
        self.assertTrue(self.fake.summary_body().endswith(self.NO_SHA))

    def test_a_failed_comment_leaves_a_sha_less_marker_too(self):
        self.fake.inline = [500]
        code, _, _ = self.post(review_data([finding()]))
        self.assertEqual(code, 1)
        self.assertTrue(self.fake.summary_body().endswith(self.NO_SHA))

    def test_the_next_run_posts_the_finding_again_instead_of_dropping_it(self):
        self.fake.comment_pages = [([comment(2, f"s\n\n{self.NO_SHA}")], "")]
        with patch.object(
            review, "filter_already_reviewed", side_effect=AssertionError("dedup ran")
        ):
            code, _, _ = self.post(review_data([finding(line=10)]))
        self.assertEqual(code, 0)
        self.assertEqual([p["line"] for p in self.fake.inline_payloads()], [10])
        self.assertTrue(self.fake.summary_body().endswith(marker()))

    def test_the_summary_keeps_the_sha_when_everything_posted(self):
        self.post(review_data([finding()]))
        self.assertTrue(self.fake.summary_body().endswith(marker()))


class InlineCapTests(GitHubTestCase):
    def test_findings_past_the_cap_are_listed_in_severity_order(self):
        minors = [finding(line=n, severity="minor") for n in range(1, 56)]
        criticals = [finding(line=n, severity="critical") for n in range(100, 105)]
        code, _, _ = self.post(review_data(minors + criticals))
        posted = [p["line"] for p in self.fake.inline_payloads()]
        body = self.fake.summary_body()
        self.assertEqual(code, 0)
        self.assertEqual(len(posted), review.MAX_INLINE_COMMENTS)
        self.assertEqual(review.MAX_INLINE_COMMENTS, 50)
        self.assertTrue(set(range(100, 105)) <= set(posted))
        self.assertEqual(posted[:5], list(range(100, 105)))
        self.assertIn("Findings not posted inline", body)
        self.assertIn("reached its limit of 50 inline comments", body)
        for n in range(46, 56):
            self.assertIn(f"` x.py `:{n} (minor)", body)
        self.assertNotIn("` x.py `:45 (minor)", body)
        self.assertIn("Issues Found (50 total)", body)
        self.assertTrue(body.endswith("<!-- ai-review -->"))

    def test_a_critical_422_is_listed_first_among_the_overflow_findings(self):
        minors = [finding(line=n, severity="minor") for n in range(1, 151)]
        critical = finding(line=500, severity="critical", text="Crash here")
        self.fake.inline = [422]
        code, _, _ = self.post(review_data(minors + [critical], fix_prompt="Fix everything"))
        body = self.fake.summary_body()
        self.assertEqual(code, 0)
        self.assertIn("` x.py `:500 (critical): Crash here", body)
        self.assertLess(body.index("(critical)"), body.index("(minor)"))
        self.assertIn("- ... and 2 more not listed", body)
        self.assertNotIn("Fix all issues with AI agents", body)

    def test_sixty_findings_post_fifty_and_list_ten(self):
        code, _, _ = self.post(review_data([finding(line=n) for n in range(1, 61)]))
        self.assertEqual(code, 0)
        self.assertEqual(len(self.fake.inline_payloads()), 50)
        self.assertEqual(self.fake.summary_body().count(" (major): Bug here"), 10)


class CarriedFindingsTests(GitHubTestCase):
    LINE = "- Still open from earlier runs, on code that did not change: 3"

    def post_with_everything_dropped(self, data, threads):
        self.fake.comment_pages = [([comment(2, f"s\n\n{marker(OLD)}")], "")]
        self.fake.thread_pages = [(threads, False)]
        with patch.object(
            review,
            "filter_already_reviewed",
            side_effect=lambda d, prev, head: {**d, "inline_comments": []},
        ):
            return self.post(data)

    def test_the_summary_counts_threads_a_dropped_finding_protects(self):
        data = review_data([finding(line=n) for n in (10, 11, 12)])
        threads = [thread(100 + n, line=n) for n in (10, 11, 12)]
        code, _, _ = self.post_with_everything_dropped(data, threads)
        body = self.fake.summary_body()
        self.assertEqual(code, 0)
        self.assertEqual(self.fake.inline_payloads(), [])
        self.assertEqual(self.fake.of("DELETE"), [])
        self.assertIn("### Issues Found", body)
        self.assertIn(self.LINE, body)

    def test_no_line_when_dedup_dropped_findings_that_have_no_thread(self):
        data = review_data([finding(line=10)])
        code, _, _ = self.post_with_everything_dropped(data, [])
        self.assertEqual(code, 0)
        self.assertNotIn("Still open", self.fake.summary_body())

    def test_two_dropped_findings_on_one_live_thread_count_once(self):
        data = review_data([finding(line=10), finding(line=11)])
        threads = [thread(101, line=11, start_line=10)]
        code, _, _ = self.post_with_everything_dropped(data, threads)
        self.assertEqual(code, 0)
        self.assertIn(
            "- Still open from earlier runs, on code that did not change: 1",
            self.fake.summary_body(),
        )

    def test_the_cleanup_returns_the_number_of_protected_threads(self):
        threads = [
            thread(101, line=10),
            thread(102, line=10, outdated=True),
            thread(103, line=10, resolved=True),
            thread(104, line=10, author=HUMAN_GQL),
            thread(105, line=20),
        ]
        fake = FakeGitHub(thread_pages=[(threads, False)])
        with contextlib.redirect_stdout(io.StringIO()), contextlib.redirect_stderr(io.StringIO()):
            protected = review._github_delete_previous_threads(fake, self.context(), {("x.py", 10)})
        self.assertEqual(protected, 1)

    def test_a_thread_a_human_replied_to_is_kept_and_counted(self):
        threads = [
            thread(101, line=10, replies=[reply(HUMAN_GQL)]),
            thread(102, line=10, author=HUMAN_GQL),
            thread(103, line=10, resolved=True, replies=[reply(HUMAN_GQL)]),
            thread(104, line=10, outdated=True, replies=[reply(HUMAN_GQL)]),
            thread(105, line=20, replies=[reply(HUMAN_GQL)]),
            thread(106, line=10, body="no marker", replies=[reply(HUMAN_GQL)]),
        ]
        fake = FakeGitHub(thread_pages=[(threads, False)])
        with contextlib.redirect_stdout(io.StringIO()), contextlib.redirect_stderr(io.StringIO()):
            carried = review._github_delete_previous_threads(fake, self.context(), {("x.py", 10)})
        self.assertEqual(carried, 1)
        self.assertEqual(fake.of("DELETE"), [])

    def test_the_cleanup_returns_zero_when_it_is_skipped(self):
        fake = FakeGitHub(thread_pages=[([thread(101)], False)])
        fake.graphql_override = FakeResponse(502)
        with contextlib.redirect_stdout(io.StringIO()), contextlib.redirect_stderr(io.StringIO()):
            protected = review._github_delete_previous_threads(fake, self.context(), {("x.py", 10)})
        self.assertEqual(protected, 0)

    def test_the_line_sits_under_the_counts_when_findings_posted(self):
        data = review_data([finding(line=5)])
        body = review._github_build_summary_body(data, marker(), None, None, carried=3)
        self.assertLess(body.index("Major: 1"), body.index(self.LINE))
        self.assertLess(body.index(self.LINE), body.index("See inline comments"))

    def test_no_line_without_carried_findings(self):
        body = review._github_build_summary_body(review_data(), marker())
        self.assertNotIn("Still open", body)


class UnanchoredListTests(GitHubTestCase):
    def build(self, count, text_len):
        unanchored = [finding(line=n, text="x" * text_len) for n in range(1, count + 1)]
        out = io.StringIO()
        with contextlib.redirect_stdout(out):
            body = review._github_build_summary_body(review_data(), marker(), None, unanchored)
        return body, out.getvalue()

    def test_the_list_is_sorted_by_severity_before_the_cap(self):
        unanchored = [finding(line=n, severity="minor") for n in range(1, 151)]
        unanchored.append(finding(line=500, severity="critical", text="Crash here"))
        unanchored.append(finding(line=400, severity="major", text="Wrong here"))
        body = review._github_build_summary_body(review_data(), marker(), None, unanchored)
        self.assertIn("` x.py `:500 (critical): Crash here", body)
        self.assertLess(body.index("(critical)"), body.index("(major)"))
        self.assertLess(body.index("(major)"), body.index("(minor)"))
        self.assertIn("` x.py `:1 (minor)", body)
        self.assertNotIn("` x.py `:99 (minor)", body)
        self.assertIn("- ... and 52 more not listed", body)

    def test_the_sort_is_stable_within_a_severity_then_by_file_and_line(self):
        unanchored = [
            finding(file="b.py", line=1),
            finding(file="a.py", line=9),
            finding(file="a.py", line=2),
        ]
        body = review._github_build_summary_body(review_data(), marker(), None, unanchored)
        order = [body.index(f"` {f} `:{n}") for f, n in (("a.py", 2), ("a.py", 9), ("b.py", 1))]
        self.assertEqual(order, sorted(order))

    def test_the_fix_prompt_goes_when_the_list_leaves_findings_out(self):
        some = [finding(line=n) for n in range(1, 101)]
        self.assertTrue(review._keep_fix_prompt(0, some))
        self.assertFalse(review._keep_fix_prompt(0, some + [finding(line=101)]))
        self.assertFalse(review._keep_fix_prompt(1, some))

    def test_a_long_finding_is_listed_in_full_when_the_body_has_room(self):
        body, out = self.build(1, 530)
        self.assertIn("x" * 530, body)
        self.assertNotIn("\u2026", body)
        self.assertEqual(out, "")

    def test_long_findings_are_cut_so_that_all_of_them_fit(self):
        body, out = self.build(70, 960)
        self.assertLessEqual(len(body), review._GITHUB_BODY_LIMIT)
        self.assertEqual(body.count(" (major): "), 70)
        self.assertNotIn("(truncated)", body)
        self.assertNotIn("more not listed", body)
        self.assertEqual(body.count("x" * 499 + "\u2026"), 70)
        self.assertNotIn("x" * 500, body)
        self.assertEqual(out, "")

    def test_a_short_finding_is_not_cut(self):
        body, _ = self.build(1, 500)
        self.assertIn("x" * 500, body)
        self.assertNotIn("\u2026", body)

    def test_at_most_a_hundred_findings_are_listed(self):
        body, out = self.build(300, 20)
        self.assertEqual(body.count(" (major): "), 100)
        self.assertIn("- ... and 200 more not listed", body)
        self.assertNotIn("(truncated)", body)
        self.assertEqual(out, "")

    def test_exactly_a_hundred_findings_need_no_more_line(self):
        body, _ = self.build(100, 20)
        self.assertEqual(body.count(" (major): "), 100)
        self.assertNotIn("more not listed", body)

    def test_a_body_that_still_overflows_warns(self):
        out = io.StringIO()
        data = review_data(summary="y" * (review._GITHUB_BODY_LIMIT + 10))
        with contextlib.redirect_stdout(out):
            body = review._github_build_summary_body(data, marker())
        self.assertIn("(truncated)", body)
        self.assertIn("::warning::", out.getvalue())

    def test_a_body_that_fits_does_not_warn(self):
        out = io.StringIO()
        with contextlib.redirect_stdout(out):
            review._github_build_summary_body(review_data(), marker())
        self.assertEqual(out.getvalue(), "")


class PositiveAspectsTypeTests(GitHubTestCase):
    def test_a_string_is_one_bullet_not_one_per_character(self):
        self.post(review_data(positive_aspects="Clean tests"))
        body = self.fake.summary_body()
        self.assertIn("- Clean tests\n", body)
        self.assertEqual(body.count("\n- "), 1)

    def test_other_types_are_tolerated(self):
        body = review._github_build_summary_body(review_data(positive_aspects={"a": 1}), marker())
        self.assertNotIn("Positive Aspects", body)
        body = review._github_build_summary_body(
            review_data(positive_aspects=["ok", 3, None]), marker()
        )
        self.assertIn("- ok\n- 3\n- None", body)


class DedupDiffFlagsTests(unittest.TestCase):
    def test_git_diff_runs_without_external_diff_or_textconv(self):
        calls = []

        def fake_check_output(argv, **kwargs):
            calls.append(argv)
            return ""

        data = review_data([finding()])
        with (
            patch.object(review.subprocess, "check_output", side_effect=fake_check_output),
            contextlib.redirect_stdout(io.StringIO()),
        ):
            review.filter_already_reviewed(data, OLD, HEAD)
        (diff,) = [argv for argv in calls if argv[:2] == ["git", "diff"]]
        self.assertIn("--no-ext-diff", diff)
        self.assertIn("--no-textconv", diff)


class RequestFailureTests(GitHubTestCase):
    def test_a_connection_error_exits_1_and_names_the_class(self):
        def get(url, params=None, **kwargs):
            raise requests_stub.exceptions.ConnectionError("no route")

        self.fake.get = get
        code, _, err = self.post(review_data([finding()]))
        self.assertEqual(code, 1)
        self.assertIn("ConnectionError", err)
        self.assertEqual(self.fake.of("POST"), [])

    def test_a_non_json_pull_request_response_exits_1(self):
        original_get = self.fake.get

        def get(url, params=None, **kwargs):
            if url == f"{REPO_URL}/pulls/7":
                return BadJson(200)
            return original_get(url, params, **kwargs)

        self.fake.get = get
        code, _, err = self.post(review_data([finding()]))
        self.assertEqual(code, 1)
        self.assertIn("ValueError", err)
        self.assertEqual(self.fake.of("POST"), [])


if __name__ == "__main__":
    unittest.main()
