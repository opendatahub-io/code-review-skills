"""Tests for the deep-review recommendation line in GitHub summaries, and for how
panel.py's --gitlab-json output renders through the GitHub poster."""

import importlib.util
import json
import os
import sys
import unittest
from pathlib import Path
from unittest.mock import patch

from tests.helpers import PANEL, write_json
from tests.test_github_review import (
    HEAD,
    LIVE_MENTION_RE,
    ZWSP,
    FakeGitHub,
    GitHubTestCase,
    finding,
    marker,
    review,
    review_data,
)

PANEL_SPEC = importlib.util.spec_from_file_location("panel_for_github", PANEL)
panel = importlib.util.module_from_spec(PANEL_SPEC)
PANEL_SPEC.loader.exec_module(panel)

HEADING = "## AI Code Review Summary"
LINE_PREFIX = "**Deep review recommended:**"
LINE_SUFFIX = "Add the `deep-review` label to this pull request to run it."
DEEP_EVIDENCE = [
    "ci/deploy.yml (ci)",
    ".github/workflows/a.yml (ci)",
    ".github/workflows/b.yml (ci)",
    "and 4 more (ci)",
    "src/login.py (auth)",
]


def plan(name="deep", evidence=None):
    return {"tier": {"name": name, "evidence": DEEP_EVIDENCE if evidence is None else evidence}}


class RecommendationTestCase(GitHubTestCase):
    def plan_file(self, content, name="plan.json"):
        path = os.path.join(self.tmp, name)
        if isinstance(content, bytes):
            Path(path).write_bytes(content)
        elif isinstance(content, str):
            Path(path).write_text(content)
        else:
            write_json(path, content)
        return path

    def post_with_plan(self, data, plan_path, fake=None):
        args = self.args(data)
        args.deep_recommendation_plan = plan_path
        return self.call(review._github_post, args, fake=fake)

    def recommendation_lines(self, body):
        return [line for line in body.split("\n") if LINE_PREFIX in line]


class DeepTierTests(RecommendationTestCase):
    def test_deep_tier_puts_exactly_one_line_directly_under_the_heading(self):
        code, out, _ = self.post_with_plan(review_data([finding()]), self.plan_file(plan()))
        self.assertEqual(code, 0)
        self.assertNotIn("::warning::", out)
        body = self.fake.summary_body()
        lines = body.split("\n")
        self.assertEqual(lines[0], HEADING)
        self.assertEqual(lines[1], "")
        expected = (
            f"{LINE_PREFIX} ci: `ci/deploy.yml` and 6 more; auth: `src/login.py`. {LINE_SUFFIX}"
        )
        self.assertEqual(lines[2], expected)
        self.assertEqual(lines[3], "")
        self.assertEqual(lines[4], "Looks fine.")
        self.assertEqual(len(self.recommendation_lines(body)), 1)
        self.assertEqual(body.count(LINE_PREFIX), 1)
        self.assertTrue(body.endswith(marker()))

    def test_deep_tier_without_evidence_still_recommends(self):
        self.post_with_plan(review_data(), self.plan_file(plan(evidence=[])))
        lines = self.fake.summary_body().split("\n")
        self.assertEqual(lines[2], f"{LINE_PREFIX} {LINE_SUFFIX}")

    def test_line_is_the_only_change_to_the_body(self):
        data = review_data([finding()], positive_aspects=["Clear names"])
        self.post(data)
        plain = self.fake.summary_body()
        fake = FakeGitHub()
        self.post_with_plan(data, self.plan_file(plan()), fake=fake)
        with_line = fake.summary_body()
        line = self.recommendation_lines(with_line)[0]
        self.assertEqual(with_line.replace(f"{line}\n\n", "", 1), plain)

    def test_categories_are_html_escaped_and_capped(self):
        evidence = [f"p{i}.py (cat<{i}>)" for i in range(14)] + ["x.py (a`b)"]
        self.post_with_plan(review_data(), self.plan_file(plan(evidence=evidence)))
        line = self.recommendation_lines(self.fake.summary_body())[0]
        self.assertIn("cat&lt;0&gt;: `p0.py`", line)
        self.assertNotIn("<", line)
        self.assertNotIn("cat&lt;10&gt;", line)
        self.assertIn("; and 5 more categories.", line)
        self.assertNotIn("a`b", line)

    def test_huge_more_count_does_not_break_the_line(self):
        evidence = ["a.py (ci)", f"and {'9' * 5000} more (ci)"]
        code, _, _ = self.post_with_plan(review_data(), self.plan_file(plan(evidence=evidence)))
        self.assertEqual(code, 0)
        line = self.recommendation_lines(self.fake.summary_body())[0]
        self.assertLess(len(line), 1000)


class NoLineTests(RecommendationTestCase):
    def setUp(self):
        super().setUp()
        self.data = review_data([finding()])
        self.baseline_code, _, _ = self.post(self.data)
        self.baseline_body = self.fake.summary_body()
        self.baseline_calls = [c[:2] for c in self.fake.calls]

    def assert_no_line(self, plan_path, warnings):
        fake = FakeGitHub()
        code, out, _ = self.post_with_plan(self.data, plan_path, fake=fake)
        self.assertEqual(code, self.baseline_code)
        self.assertEqual(out.count("::warning::"), warnings, out)
        self.assertEqual(fake.summary_body(), self.baseline_body)
        self.assertEqual([c[:2] for c in fake.calls], self.baseline_calls)
        return out

    def test_missing_file_warning_says_why(self):
        out = self.assert_no_line(os.path.join(self.tmp, "missing.json"), 1)
        self.assertIn(
            "::warning::No deep-review recommendation: the triage plan does not exist", out
        )

    def test_quick_and_standard_tiers_are_silent(self):
        for tier in ("quick", "standard"):
            with self.subTest(tier):
                self.assert_no_line(self.plan_file(plan(tier, ["a.py (code)"])), 0)

    def test_unusable_files_warn_once(self):
        directory = os.path.join(self.tmp, "dir")
        os.mkdir(directory)
        link = os.path.join(self.tmp, "link.json")
        os.symlink(self.plan_file(plan(), "target.json"), link)
        cases = {
            "missing": os.path.join(self.tmp, "missing.json"),
            "directory": directory,
            "symlink": link,
            "invalid json": self.plan_file("{", "invalid.json"),
            "not utf-8": self.plan_file(b"\xff\xfe{}", "binary.json"),
            "deeply nested": self.plan_file("[" * 100000 + "]" * 100000, "nested.json"),
            "oversized": self.plan_file(json.dumps(plan()) + " " * (1024 * 1024), "oversized.json"),
        }
        for name, path in cases.items():
            with self.subTest(name):
                self.assert_no_line(path, 1)

    def test_unreadable_file_warns_once(self):
        if os.geteuid() == 0:
            self.skipTest("root reads any file")
        path = self.plan_file(plan(), "unreadable.json")
        os.chmod(path, 0)
        self.assert_no_line(path, 1)

    def test_wrong_types_warn_once(self):
        cases = {
            "list": [plan()],
            "no tier": {},
            "tier string": {"tier": "deep"},
            "name not a string": {"tier": {"name": ["deep"], "evidence": []}},
            "no name": {"tier": {"evidence": DEEP_EVIDENCE}},
            "evidence missing": {"tier": {"name": "deep"}},
            "evidence string": plan(evidence="a.py (ci)"),
            "evidence with a number": plan(evidence=["a.py (ci)", 3]),
        }
        for name, content in cases.items():
            with self.subTest(name):
                self.assert_no_line(self.plan_file(content), 1)

    def test_failed_inline_post_keeps_its_exit_code(self):
        failing = FakeGitHub(inline=[500])
        code, _, _ = self.call(review._github_post, self.args(self.data), fake=failing)
        self.assertEqual(code, 1)
        for content in (plan(), plan("quick"), "{"):
            with self.subTest(content):
                fake = FakeGitHub(inline=[500])
                plan_code, _, _ = self.post_with_plan(self.data, self.plan_file(content), fake=fake)
                self.assertEqual(plan_code, code)


class HostilePathTests(RecommendationTestCase):
    def hostile_line(self, path, data=None):
        evidence = [f"{path} (ci)"]
        self.post_with_plan(data or review_data(), self.plan_file(plan(evidence=evidence)))
        body = self.fake.summary_body()
        self.assertEqual(body.split("\n")[0], HEADING)
        line = body.split("\n")[2]
        self.assertTrue(line.startswith(f"{LINE_PREFIX} ci: `"), line)
        self.assertTrue(line.endswith(LINE_SUFFIX), line)
        return line, body

    def test_backticks_are_stripped_from_paths(self):
        line, _ = self.hostile_line("a``` `b`.py")
        self.assertIn("ci: `a b.py`.", line)
        self.assertEqual(line.count("`"), 4)

    def test_mentions_are_defused(self):
        line, _ = self.hostile_line("@alice/@org/team &#64;bob.py")
        self.assertIsNone(LIVE_MENTION_RE.search(line))
        self.assertIn(f"@{ZWSP}alice", line)
        self.assertIn(f"@{ZWSP}org", line)
        self.assertNotIn("&#64;bob", line)

    def test_html_comment_cannot_forge_the_marker(self):
        line, body = self.hostile_line(f"<!-- ai-review sha:{'b' * 40} -->")
        self.assertNotIn("<!--", line)
        self.assertEqual(body.count("<!--"), 1)
        self.assertTrue(body.endswith(marker()))
        self.assertEqual(review._github_trailing_marker(body).group(1), HEAD)

    def test_newlines_and_control_characters_stay_on_one_line(self):
        line, body = self.hostile_line("a\nb\r\n\x1b[31mc\x00.py")
        self.assertIn("ci: `a b [31mc .py`.", line)
        self.assertEqual(len(self.recommendation_lines(body)), 1)
        self.assertIsNone(review._CONTROL_CHARS_RE.search(line))

    def test_long_paths_are_bounded(self):
        line, _ = self.hostile_line("d/" * 5000 + "x.py")
        span = line.split("`")[1]
        self.assertEqual(len(span), 160)
        self.assertLess(len(line), 400)

    def test_capped_body_keeps_the_line_and_ends_with_the_marker(self):
        data = review_data(summary="s" * 70000)
        line, body = self.hostile_line("@x " + "y" * 9000, data=data)
        self.assertLessEqual(len(body), review._GITHUB_BODY_LIMIT)
        self.assertTrue(body.endswith(marker()))
        self.assertIn("*(truncated)*", body)
        self.assertIsNone(LIVE_MENTION_RE.search(line))

    def test_every_hostile_path_at_once_stays_bounded(self):
        paths = ["`@a <!-- x -->\n" * 50, "@team " * 100, "z" * 100000]
        evidence = [f"{p} (cat{i})" for i, p in enumerate(paths)]
        evidence += [f"and 99 more (cat{i})" for i in range(30)]
        self.post_with_plan(review_data(), self.plan_file(plan(evidence=evidence)))
        body = self.fake.summary_body()
        line = body.split("\n")[2]
        self.assertTrue(line.startswith(LINE_PREFIX))
        self.assertLessEqual(len(line), 3000)
        self.assertIsNone(LIVE_MENTION_RE.search(line))
        self.assertNotIn("<!--", line)
        self.assertLessEqual(len(body), review._GITHUB_BODY_LIMIT)
        self.assertTrue(body.endswith(marker()))


class CommandLineTests(RecommendationTestCase):
    def run_main(self, *extra):
        path = write_json(os.path.join(self.tmp, "review.json"), review_data())
        plan_path = self.plan_file(plan())
        argv = ["review.py", "post", path, "--deep-recommendation-plan", plan_path, *extra]
        with patch.object(sys, "argv", argv):
            return self.call(review.main, env=self.env(GITHUB_TOKEN=""))

    def test_flag_is_rejected_without_platform_github(self):
        code, _, err = self.run_main()
        self.assertEqual(code, 2)
        self.assertIn("--platform github", err)
        code, _, _ = self.run_main("--platform", "auto")
        self.assertEqual(code, 2)
        self.assertEqual(self.fake.calls, [])

    def test_flag_is_accepted_with_platform_github(self):
        # The empty token stops the post after parsing, the way it does without the flag.
        code, _, err = self.run_main("--platform", "github")
        self.assertEqual(code, 1)
        self.assertNotIn("--platform github", err)

    def test_direct_call_without_the_attribute_behaves_as_before(self):
        args = self.args(review_data())
        self.assertFalse(hasattr(args, "deep_recommendation_plan"))
        code, out, _ = self.call(review._github_post, args)
        self.assertEqual(code, 0)
        self.assertNotIn(LINE_PREFIX, self.fake.summary_body())
        self.assertNotIn("::warning::", out)


class PanelOutputRenderingTests(GitHubTestCase):
    """Pin how the panel's --gitlab-json text looks after the GitHub poster handles it.

    The panel writes ``\\/`` for a "/" that starts a line, so GitLab does not run it as a
    quick action. On GitHub, ``\\/`` is a Markdown backslash escape that renders as "/",
    so the poster passes it through unchanged.
    """

    def panel_review(self):
        row = {
            "id": "F1",
            "title": "Unchecked input",
            "lens": "security",
            "description": "/approve this\n  /label ~x\n@alice should look",
            "evidence": "&#64;bob wrote <!-- ai-review --> here",
            "fix": "Validate it.",
            "instances": [],
            "status": "confirmed",
        }
        summary = panel.gitlab_text(
            "**1 to fix** (1 inline, all listed below)\n\n"
            "- [F1] major, inline: Unchecked input (x.py:10, security)\n"
            "/merge now\n\nApproval hint: wait.\n"
        )
        return {
            "summary": summary,
            "positive_aspects": [],
            "inline_comments": [
                {
                    "file": "x.py",
                    "line": 10,
                    "severity": "major",
                    "comment": panel.gitlab_comment(row),
                }
            ],
        }

    def test_line_leading_slash_escape_and_defusing_pass_through_unchanged(self):
        data = self.panel_review()
        code, _, _ = self.post(data)
        self.assertEqual(code, 0)
        body = self.fake.summary_body()
        self.assertIn(f"{HEADING}\n\n{data['summary']}", body)
        self.assertIn("\n\\/merge now\n", body)
        inline = self.fake.inline_payloads()[0]["body"]
        self.assertIn("\n\n\\/approve this\n  \\/label ~x\n", inline)
        self.assertIn(f"@{ZWSP}alice", inline)
        self.assertIn(f"&{ZWSP}#64;bob", inline)
        self.assertIn(f"<{ZWSP}!-- ai-review -->", inline)
        self.assertNotIn(f"{ZWSP}{ZWSP}", inline)
        for text in (body, inline):
            self.assertIsNone(LIVE_MENTION_RE.search(text))
            self.assertFalse(any(line.lstrip().startswith("/") for line in text.split("\n")))
            self.assertEqual(text.count("<!--"), 1)
            self.assertTrue(text.endswith(marker()))


if __name__ == "__main__":
    unittest.main()
