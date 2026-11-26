"""Tests for what the post step does besides posting: reviewer suggestions and the bot login.

Like tests/test_github_action.py, these run review-pr.sh as a subprocess with a fake review.py
that records each call, so no network, container runtime or token is involved.
"""

import json
import os
import shutil
import sys
from pathlib import Path
from unittest.mock import patch

from tests.helpers import make_repo, write
from tests.test_github_action import (
    REVIEW_PY_REL,
    ScriptTestCase,
    evaluate_expression,
    step_env_value,
    write_executable,
)
from tests.test_github_review import review

TOKEN = "ghs_POSTEXTRASSECRET"

# Stands in for review.py: appends its argv, the environment it reads and its working directory
# to FAKE_LOG as one JSON line, then exits FAKE_POST_STATUS for post and FAKE_SUGGEST_STATUS for
# suggest-reviewers.
FAKE_REVIEW_PY = """
import json
import os
import sys

keys = ["UV_NO_CONFIG", "UV_PYTHON", "GIT_CONFIG_COUNT", "GIT_CONFIG_KEY_0",
        "GIT_CONFIG_VALUE_0", "GIT_CONFIG_KEY_1", "GIT_CONFIG_VALUE_1", "GITHUB_TOKEN",
        "AI_REVIEW_BOT_LOGIN"]
with open(os.environ["FAKE_LOG"], "a", encoding="utf-8") as f:
    f.write(json.dumps({
        "argv": sys.argv[1:],
        "env": {k: os.environ.get(k) for k in keys},
        "cwd": os.getcwd(),
    }) + "\\n")
name = "FAKE_SUGGEST_STATUS" if sys.argv[1:2] == ["suggest-reviewers"] else "FAKE_POST_STATUS"
sys.exit(int(os.environ.get(name, "0")))
"""

SUGGEST_ARGV = ["suggest-reviewers", "--platform", "github"]


class PostExtrasTestCase(ScriptTestCase):
    def setUp(self):
        super().setUp()
        repo, _base = make_repo(self.tmp, {"a.txt": "one\n"})
        self.repo = self.move_repo_into_place(repo)
        self.action_path = os.path.join(self.tmp, "action")
        self.log = os.path.join(self.tmp, "review-py.jsonl")
        write_executable(Path(self.action_path, REVIEW_PY_REL), FAKE_REVIEW_PY)
        write(self.out_dir, "ai-review-output.json", '{"summary": "ok"}')

    def post(self, **env: str):
        defaults = dict(GITHUB_ACTION_PATH=self.action_path, FAKE_LOG=self.log, GITHUB_TOKEN=TOKEN)
        defaults.update(env)
        return self.run_script("post", self.repo, **defaults)

    def calls(self) -> list[dict]:
        if not os.path.exists(self.log):
            return []
        lines = Path(self.log).read_text(encoding="utf-8").splitlines()
        return [json.loads(line) for line in lines]

    def commands(self) -> list[str]:
        return [call["argv"][0] for call in self.calls()]


class SuggestReviewersTests(PostExtrasTestCase):
    def test_suggestions_are_off_by_default(self):
        proc = self.post()
        self.assertEqual(proc.returncode, 0, proc.stdout + proc.stderr)
        self.assertEqual(self.commands(), ["post"])

    def test_off_values_make_no_call(self):
        for value in ("", "false", "0", "False"):
            with self.subTest(value=value):
                Path(self.log).unlink(missing_ok=True)
                proc = self.post(AI_REVIEW_SUGGEST_REVIEWERS=value)
                self.assertEqual(proc.returncode, 0, proc.stdout + proc.stderr)
                self.assertEqual(self.commands(), ["post"])

    def test_suggestions_run_after_a_successful_standard_post(self):
        proc = self.post(AI_REVIEW_SUGGEST_REVIEWERS="true", AI_REVIEW_MODE="standard")
        self.assertEqual(proc.returncode, 0, proc.stdout + proc.stderr)
        calls = self.calls()
        self.assertEqual([call["argv"][0] for call in calls], ["post", "suggest-reviewers"])
        self.assertEqual(calls[1]["argv"], SUGGEST_ARGV)

    def test_an_empty_mode_is_standard_and_suggests(self):
        proc = self.post(AI_REVIEW_SUGGEST_REVIEWERS="true", AI_REVIEW_MODE="")
        self.assertEqual(proc.returncode, 0, proc.stdout + proc.stderr)
        self.assertEqual(self.commands(), ["post", "suggest-reviewers"])

    def test_suggestions_run_in_the_checkout_with_the_post_steps_hardened_env(self):
        proc = self.post(AI_REVIEW_SUGGEST_REVIEWERS="true")
        self.assertEqual(proc.returncode, 0, proc.stdout + proc.stderr)
        post, suggest = self.calls()
        self.assertEqual(suggest["env"], post["env"])
        self.assertEqual(suggest["cwd"], post["cwd"])
        self.assertEqual(os.path.realpath(suggest["cwd"]), os.path.realpath(self.repo))
        env = suggest["env"]
        self.assertEqual(env["UV_NO_CONFIG"], "1")
        self.assertEqual(env["UV_PYTHON"], shutil.which("python3"))
        self.assertEqual(env["GITHUB_TOKEN"], TOKEN)
        overrides = {
            env["GIT_CONFIG_KEY_0"]: env["GIT_CONFIG_VALUE_0"],
            env["GIT_CONFIG_KEY_1"]: env["GIT_CONFIG_VALUE_1"],
        }
        self.assertEqual(overrides, {"core.fsmonitor": "false", "core.hooksPath": "/dev/null"})

    def test_deep_mode_never_suggests(self):
        proc = self.post(AI_REVIEW_SUGGEST_REVIEWERS="true", AI_REVIEW_MODE="deep")
        self.assertEqual(proc.returncode, 0, proc.stdout + proc.stderr)
        self.assertEqual(self.commands(), ["post"])

    def test_a_failed_post_fails_the_step_with_its_status_and_suggests_nothing(self):
        proc = self.post(AI_REVIEW_SUGGEST_REVIEWERS="true", FAKE_POST_STATUS="3")
        self.assertEqual(proc.returncode, 3, proc.stdout + proc.stderr)
        self.assertEqual(self.commands(), ["post"])

    def test_a_failed_suggestion_only_warns(self):
        proc = self.post(AI_REVIEW_SUGGEST_REVIEWERS="true", FAKE_SUGGEST_STATUS="1")
        self.assertEqual(proc.returncode, 0, proc.stdout + proc.stderr)
        self.assertEqual(self.commands(), ["post", "suggest-reviewers"])
        self.assertIn("::warning::Could not suggest reviewers (review.py exit 1)", proc.stdout)
        self.assertNotIn("::error::", proc.stdout)

    def test_a_successful_suggestion_does_not_warn(self):
        proc = self.post(AI_REVIEW_SUGGEST_REVIEWERS="true")
        self.assertEqual(proc.returncode, 0, proc.stdout + proc.stderr)
        self.assertNotIn("::warning::", proc.stdout)

    def test_the_token_is_never_written_to_the_step_outputs(self):
        self.post(AI_REVIEW_SUGGEST_REVIEWERS="true", FAKE_SUGGEST_STATUS="1")
        if os.path.exists(self.output):
            self.assertNotIn(TOKEN, Path(self.output).read_text(encoding="utf-8"))


class BotLoginTests(PostExtrasTestCase):
    def bot_login(self, inputs: dict[str, str], app_token: dict[str, str]) -> str:
        """What action.yml passes the post step as AI_REVIEW_BOT_LOGIN."""
        expression = step_env_value("Post the review", "AI_REVIEW_BOT_LOGIN")
        return evaluate_expression(expression, inputs, app_token)

    def test_the_app_login_reaches_both_review_py_calls(self):
        login = self.bot_login({"app_id": "123"}, {"token": "ghs_app", "app-slug": "my-app"})
        self.assertEqual(login, "my-app[bot]")
        proc = self.post(AI_REVIEW_SUGGEST_REVIEWERS="true", AI_REVIEW_BOT_LOGIN=login)
        self.assertEqual(proc.returncode, 0, proc.stdout + proc.stderr)
        calls = self.calls()
        self.assertEqual(len(calls), 2)
        for call in calls:
            with self.subTest(call["argv"][0]):
                self.assertEqual(call["env"]["AI_REVIEW_BOT_LOGIN"], "my-app[bot]")

    def test_the_login_is_empty_without_an_app(self):
        login = self.bot_login({}, {})
        self.assertEqual(login, "")
        proc = self.post(AI_REVIEW_BOT_LOGIN=login)
        self.assertEqual(proc.returncode, 0, proc.stdout + proc.stderr)
        self.assertEqual(self.calls()[0]["env"]["AI_REVIEW_BOT_LOGIN"], "")

    def test_a_caller_token_posts_as_the_bot_login_input(self):
        login = self.bot_login({"github_token": "github_pat_x", "bot_login": "review-bot"}, {})
        self.assertEqual(login, "review-bot")
        proc = self.post(AI_REVIEW_BOT_LOGIN=login)
        self.assertEqual(proc.returncode, 0, proc.stdout + proc.stderr)
        self.assertEqual(self.calls()[0]["env"]["AI_REVIEW_BOT_LOGIN"], "review-bot")


class SuggestParserTests(PostExtrasTestCase):
    """The real review.py parser accepts the suggest-reviewers call the script makes."""

    def test_the_suggest_call_parses_as_the_github_command(self):
        proc = self.post(AI_REVIEW_SUGGEST_REVIEWERS="true")
        self.assertEqual(proc.returncode, 0, proc.stdout + proc.stderr)
        argv = self.calls()[-1]["argv"]
        parsed = []
        with (
            patch.object(sys, "argv", ["review.py", *argv]),
            patch.object(review, "cmd_suggest_reviewers", side_effect=parsed.append),
        ):
            review.main()
        self.assertEqual(len(parsed), 1)
        self.assertEqual(parsed[0].command, "suggest-reviewers")
        self.assertEqual(parsed[0].platform, "github")
