"""Tests for the configurable GitHub posting account (AI_REVIEW_BOT_LOGIN) in review.py, and
for how the deep-review recommendation reads paths and plan files.

Everything runs in-process against the recording stand-in from test_github_review.py.
"""

import json
import unittest
from unittest.mock import patch

from tests.test_github_recommendation import LINE_PREFIX, RecommendationTestCase, plan
from tests.test_github_review import (
    HEAD,
    NEWER,
    OLD,
    OURS_GQL,
    OURS_REST,
    FakeGitHub,
    FakeResponse,
    GitHubTestCase,
    comment,
    finding,
    marker,
    review,
    review_data,
    thread,
)

APP_LOGIN = "review-app[bot]"
APP_REST = {"login": APP_LOGIN, "type": "Bot"}
APP_GQL = {"__typename": "Bot", "login": "review-app"}
USER_LOGIN = "Review-Owner"
USER_REST = {"login": "review-owner", "type": "User"}
USER_GQL = {"__typename": "User", "login": "review-owner"}
USER_LOOK_ALIKE_GQL = {"__typename": "User", "login": "review-owner-x"}


class StatusGitHub(FakeGitHub):
    """FakeGitHub whose PATCH and DELETE answer with a chosen status per comment id."""

    def __init__(self, *args, patch_status=None, delete_status=None, **kwargs):
        super().__init__(*args, **kwargs)
        self.patch_status = patch_status or {}
        self.delete_status = delete_status or {}

    def patch(self, url, json=None, **kwargs):
        super().patch(url, json=json, **kwargs)
        return FakeResponse(self.patch_status.get(url.rsplit("/", 1)[-1], 200), {})

    def delete(self, url, **kwargs):
        super().delete(url, **kwargs)
        return FakeResponse(self.delete_status.get(url.rsplit("/", 1)[-1], 204))


class IdentityTestCase(GitHubTestCase):
    def env_with(self, login):
        return self.env(AI_REVIEW_BOT_LOGIN=login)

    def post_as(self, login, data=None, fake=None):
        return self.call(
            review._github_post,
            self.args(data or review_data()),
            env=self.env_with(login),
            fake=fake,
        )

    def ours(self, login, rest=(), graphql=()):
        """Return which REST users and GraphQL authors count as ours under ``login``."""
        with patch.dict("os.environ", self.env_with(login), clear=True):
            return (
                [review._github_rest_is_ours(u) for u in rest],
                [review._github_comment_is_ours(u) for u in rest],
                [review._github_graphql_is_ours(a) for a in graphql],
            )

    def find(self, login, comments):
        fake = FakeGitHub(comment_pages=[(comments, "")])
        with patch.dict("os.environ", self.env_with(login), clear=True):
            return review._github_find_summary(fake, review._github_load_context())

    def summary_writes(self, fake):
        return [
            (c[0], c[1].rsplit("/", 2)[-2:])
            for c in fake.calls
            if "/issues/" in c[1] and c[0] != "GET"
        ]


class DefaultIdentityTests(IdentityTestCase):
    def scenario(self):
        return StatusGitHub(
            comment_pages=[
                (
                    [
                        comment(2, f"old\n\n{marker(OLD)}"),
                        comment(3, f"app\n\n{marker(OLD)}", APP_REST),
                    ],
                    "",
                )
            ],
            thread_pages=[([thread(101), thread(102, author=APP_GQL)], False)],
        )

    def test_unset_empty_and_the_workflow_bot_behave_the_same(self):
        baseline = self.scenario()
        code, _, _ = self.call(
            review._github_post, self.args(review_data([finding()])), fake=baseline
        )
        self.assertEqual(code, 0)
        for value in ("", "github-actions[bot]"):
            with self.subTest(value=value):
                fake = self.scenario()
                code, _, _ = self.post_as(value, review_data([finding()]), fake=fake)
                self.assertEqual(code, 0)
                self.assertEqual(fake.calls, baseline.calls)

    def test_unset_only_recognizes_the_workflow_bot(self):
        fake = self.scenario()
        code, _, _ = self.call(review._github_post, self.args(review_data()), fake=fake)
        self.assertEqual(code, 0)
        self.assertEqual(self.summary_writes(fake), [("PATCH", ["comments", "2"])])
        self.assertEqual([c[1].rsplit("/", 1)[-1] for c in fake.of("DELETE")], ["101"])

    def test_unset_rejects_app_and_user_forms(self):
        rest, comments, graphql = self.ours(
            None, [OURS_REST, APP_REST, USER_REST], [OURS_GQL, APP_GQL, USER_GQL]
        )
        self.assertEqual(rest, [True, False, False])
        self.assertEqual(comments, rest)
        self.assertEqual(graphql, [True, False, False])


class AppIdentityTests(IdentityTestCase):
    def test_both_forms_of_both_accounts_are_recognized(self):
        rest, comments, graphql = self.ours(APP_LOGIN, [OURS_REST, APP_REST], [OURS_GQL, APP_GQL])
        self.assertEqual(rest, [True, True])
        self.assertEqual(comments, [True, True])
        self.assertEqual(graphql, [True, True])

    def test_look_alikes_never_match(self):
        rest_look_alikes = [
            {"login": "review-app-x[bot]", "type": "Bot"},
            {"login": "review-app[bot]", "type": "User"},
            {"login": "review-app", "type": "Bot"},
            {"login": "Review-App[bot]", "type": "Bot"},
            {"login": "review-app[bot] ", "type": "Bot"},
            {"login": "github-actions[bot]", "type": "User"},
            {"login": APP_LOGIN},
            {"type": "Bot"},
            None,
            APP_LOGIN,
        ]
        graphql_look_alikes = [
            {"__typename": "User", "login": "review-app"},
            {"__typename": "Bot", "login": "review-app[bot]"},
            {"__typename": "Bot", "login": "review-app-x"},
            {"__typename": "Bot", "login": "Review-App"},
            {"__typename": "User", "login": "github-actions"},
            None,
        ]
        rest, comments, graphql = self.ours(APP_LOGIN, rest_look_alikes, graphql_look_alikes)
        self.assertEqual(rest, [False] * len(rest_look_alikes))
        self.assertEqual(comments, rest)
        self.assertEqual(graphql, [False] * len(graphql_look_alikes))

    def test_dedup_reads_the_summary_we_patch_else_the_newest_of_either_account(self):
        legacy = comment(1, f"legacy\n\n{marker(OLD)}")
        app = comment(2, f"app\n\n{marker(NEWER)}", APP_REST)
        newer_legacy = comment(3, f"legacy\n\n{marker(HEAD)}")
        older_legacy = comment(4, f"legacy\n\n{marker(NEWER)}")
        self.assertEqual(self.find(APP_LOGIN, [legacy]), (None, OLD))
        self.assertEqual(self.find(APP_LOGIN, [legacy, older_legacy]), (None, NEWER))
        self.assertEqual(self.find(APP_LOGIN, [legacy, app]), (2, NEWER))
        self.assertEqual(self.find(APP_LOGIN, [app, newer_legacy]), (2, NEWER))
        self.assertEqual(self.find(None, [legacy, app]), (1, OLD))

    def test_a_newer_legacy_summary_does_not_pin_dedup_to_its_sha(self):
        # Each run patches the app summary, so its SHA moves on while the legacy one stays.
        legacy = comment(6, f"legacy\n\n{marker(OLD)}")
        for app_sha in (NEWER, HEAD):
            with self.subTest(app_sha=app_sha):
                app = comment(5, f"app\n\n{marker(app_sha)}", APP_REST)
                self.assertEqual(self.find(APP_LOGIN, [app, legacy]), (5, app_sha))

    def test_post_dedups_against_the_summary_it_patches(self):
        comments = [
            comment(5, f"app\n\n{marker(NEWER)}", APP_REST),
            comment(6, f"legacy\n\n{marker(OLD)}"),
        ]
        fake = StatusGitHub(comment_pages=[(comments, "")])
        seen = []

        def dedup(data, prev, head):
            seen.append((prev, head))
            return data

        with patch.object(review, "filter_already_reviewed", side_effect=dedup):
            code, _, _ = self.post_as(APP_LOGIN, fake=fake)
        self.assertEqual(code, 0)
        self.assertEqual(seen, [(NEWER, HEAD)])
        self.assertEqual(self.summary_writes(fake), [("PATCH", ["comments", "5"])])

    def test_entries_that_are_not_objects_are_skipped(self):
        comments = ["text", None, 3, comment(2, f"app\n\n{marker(OLD)}", APP_REST)]
        self.assertEqual(self.find(APP_LOGIN, comments), (2, OLD))

    def test_legacy_summary_is_not_patched_and_still_drives_dedup(self):
        fake = StatusGitHub(
            comment_pages=[([comment(2, f"legacy\n\n{marker(OLD)}")], "")],
            patch_status={"2": 403},
        )
        seen = []

        def dedup(data, prev, head):
            seen.append((prev, head))
            return data

        with patch.object(review, "filter_already_reviewed", side_effect=dedup):
            code, _, _ = self.post_as(APP_LOGIN, fake=fake)
        self.assertEqual(code, 0)
        self.assertEqual(seen, [(OLD, HEAD)])
        self.assertEqual(fake.of("PATCH"), [])
        self.assertEqual(self.summary_writes(fake), [("POST", ["7", "comments"])])

    def test_app_summary_is_patched_even_below_a_newer_legacy_one(self):
        comments = [
            comment(5, f"app\n\n{marker(OLD)}", APP_REST),
            comment(6, f"legacy\n\n{marker(NEWER)}"),
        ]
        fake = StatusGitHub(comment_pages=[(comments, "")])
        code, _, _ = self.post_as(APP_LOGIN, fake=fake)
        self.assertEqual(code, 0)
        self.assertEqual(self.summary_writes(fake), [("PATCH", ["comments", "5"])])

    def test_a_failed_patch_of_our_own_summary_still_exits_1(self):
        fake = StatusGitHub(
            comment_pages=[([comment(5, f"app\n\n{marker(OLD)}", APP_REST)], "")],
            patch_status={"5": 500},
        )
        code, _, _ = self.post_as(APP_LOGIN, fake=fake)
        self.assertEqual(code, 1)

    def test_threads_of_both_accounts_are_cleaned_up(self):
        threads = [
            thread(101),
            thread(102, author=APP_GQL),
            thread(103, author={"__typename": "Bot", "login": "review-app-x"}),
            thread(104, author={"__typename": "User", "login": "review-app"}),
        ]
        fake = StatusGitHub(thread_pages=[(threads, False)])
        code, _, _ = self.post_as(APP_LOGIN, fake=fake)
        self.assertEqual(code, 0)
        self.assertEqual([c[1].rsplit("/", 1)[-1] for c in fake.of("DELETE")], ["101", "102"])

    def test_a_refused_legacy_delete_warns_and_the_post_succeeds(self):
        for status in (403, 404):
            with self.subTest(status=status):
                fake = StatusGitHub(
                    thread_pages=[([thread(101), thread(102, author=APP_GQL)], False)],
                    delete_status={"101": status},
                )
                code, _, err = self.post_as(APP_LOGIN, review_data([finding()]), fake=fake)
                self.assertEqual(code, 0)
                self.assertIn(f"Failed to delete review comment 101 (HTTP {status})", err)
                self.assertEqual(len(fake.of("DELETE")), 2)
                self.assertEqual(len(fake.inline_payloads()), 1)
                self.assertEqual(self.summary_writes(fake), [("POST", ["7", "comments"])])

    def test_threads_carried_on_a_dropped_finding_count_for_both_accounts(self):
        threads = [thread(101, line=10), thread(102, line=11, author=APP_GQL)]
        fake = StatusGitHub(
            comment_pages=[([comment(2, f"s\n\n{marker(OLD)}")], "")],
            thread_pages=[(threads, False)],
        )
        data = review_data([finding(line=10), finding(line=11)])
        with patch.object(
            review,
            "filter_already_reviewed",
            side_effect=lambda d, prev, head: {**d, "inline_comments": []},
        ):
            code, _, _ = self.post_as(APP_LOGIN, data, fake=fake)
        self.assertEqual(code, 0)
        self.assertEqual(fake.of("DELETE"), [])
        body = fake.of("POST", "/issues/7/comments")[-1][2]["json"]["body"]
        self.assertIn("- Still open from earlier runs, on code that did not change: 2", body)


class UserIdentityTests(IdentityTestCase):
    def test_user_forms_match_case_insensitively(self):
        rest, comments, graphql = self.ours(
            USER_LOGIN,
            [USER_REST, {"login": "REVIEW-OWNER", "type": "User"}, OURS_REST],
            [USER_GQL, {"__typename": "User", "login": "Review-Owner"}, OURS_GQL],
        )
        self.assertEqual(rest, [True, True, True])
        self.assertEqual(comments, rest)
        self.assertEqual(graphql, [True, True, True])

    def test_user_look_alikes_never_match(self):
        rest, _, graphql = self.ours(
            "kate",
            [
                {"login": "kate", "type": "Bot"},
                {"login": "kate[bot]", "type": "Bot"},
                {"login": "kate-x", "type": "User"},
                {"login": "Kate", "type": "User"},
            ],
            [{"__typename": "Bot", "login": "kate"}, {"__typename": "User", "login": "Kate"}],
        )
        self.assertEqual(rest, [False] * 4)
        self.assertEqual(graphql, [False] * 2)

    def test_user_summary_is_patched_and_user_threads_are_deleted(self):
        comments = [
            comment(2, f"legacy\n\n{marker(OLD)}"),
            comment(3, f"mine\n\n{marker(OLD)}", USER_REST),
            comment(4, f"other\n\n{marker(OLD)}", {"login": "someone", "type": "User"}),
        ]
        threads = [thread(101, author=USER_GQL), thread(102, author=USER_LOOK_ALIKE_GQL)]
        fake = StatusGitHub(comment_pages=[(comments, "")], thread_pages=[(threads, False)])
        code, _, _ = self.post_as(USER_LOGIN, fake=fake)
        self.assertEqual(code, 0)
        self.assertEqual(self.summary_writes(fake), [("PATCH", ["comments", "3"])])
        self.assertEqual([c[1].rsplit("/", 1)[-1] for c in fake.of("DELETE")], ["101"])


class InvalidLoginTests(IdentityTestCase):
    def test_invalid_login_exits_1_before_any_request(self):
        invalid = [
            "Review-App[bot]",
            "review app[bot]",
            "review-app[bot]\n",
            "review-app[bot][bot]",
            "[bot]",
            "-review-app[bot]",
            "a" * 64 + "[bot]",
            "-owner",
            "owner-",
            "own--er",
            "own_er",
            "a" * 40,
            " owner",
            "owner\n::error::forged",
            "İnan",
            "github-actions[bot] ",
        ]
        for value in invalid:
            with self.subTest(value=value):
                fake = FakeGitHub()
                code, out, err = self.post_as(value, fake=fake)
                self.assertEqual(code, 1)
                self.assertEqual(fake.calls, [])
                self.assertIn("AI_REVIEW_BOT_LOGIN must be", err)
                self.assertNotIn("\n::error::forged", out + err)

    def test_boundary_logins_are_accepted(self):
        for value in ("a" * 63 + "[bot]", "a_b-c[bot]", "0[bot]", "a" * 39, "a-b-c", "A1"):
            with self.subTest(value=value):
                code, _, _ = self.post_as(value)
                self.assertEqual(code, 0)


class ReasonFormatCharacterTests(RecommendationTestCase):
    FORMAT_CHARS = "‪‫‬‭‮⁦⁧⁨⁩​‎﻿­"

    def test_bidi_and_format_characters_are_stripped_from_paths_and_categories(self):
        hostile = "a" + self.FORMAT_CHARS + "b.py"
        self.assertEqual(review._github_reason_path(hostile), "`ab.py`")
        self.assertEqual(review._github_reason_path(self.FORMAT_CHARS), "(unnamed path)")
        summary = review._github_reason_summary([f"{hostile} (c‮i)"])
        self.assertEqual(summary, "ci: `ab.py`")

    def test_stripped_characters_cannot_hide_a_mention(self):
        evidence = ["@⁦alice.py (ci)"]
        code, _, _ = self.post_with_plan(review_data(), self.plan_file(plan(evidence=evidence)))
        self.assertEqual(code, 0)
        line = self.recommendation_lines(self.fake.summary_body())[0]
        self.assertIn("`@​alice.py`", line)
        self.assertNotIn("⁦", line)


class PlanEncodingTests(RecommendationTestCase):
    def test_utf16_and_utf32_plans_warn_and_add_no_line(self):
        text = json.dumps(plan())
        for encoding in ("utf-16", "utf-16-le", "utf-32", "utf-32-be"):
            with self.subTest(encoding=encoding):
                path = self.plan_file(text.encode(encoding), f"plan-{encoding}.json")
                code, out, _ = self.post_with_plan(review_data(), path, fake=FakeGitHub())
                self.assertEqual(code, 0)
                self.assertEqual(out.count("::warning::"), 1, out)
                self.assertIn("the triage plan is not valid JSON", out)

    def test_utf8_plan_with_a_bom_yields_the_line(self):
        path = self.plan_file(json.dumps(plan()).encode("utf-8-sig"))
        code, out, _ = self.post_with_plan(review_data(), path)
        self.assertEqual(code, 0)
        self.assertNotIn("::warning::", out)
        self.assertEqual(len(self.recommendation_lines(self.fake.summary_body())), 1)

    def test_utf8_plan_still_yields_the_line(self):
        path = self.plan_file(json.dumps(plan()).encode("utf-8"))
        code, out, _ = self.post_with_plan(review_data(), path)
        self.assertEqual(code, 0)
        self.assertNotIn("::warning::", out)
        self.assertEqual(len(self.recommendation_lines(self.fake.summary_body())), 1)
        self.assertTrue(self.fake.summary_body().split("\n")[2].startswith(LINE_PREFIX))


if __name__ == "__main__":
    unittest.main()
