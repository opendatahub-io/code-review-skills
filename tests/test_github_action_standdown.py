"""Tests for the action's stand-down step and the deep-review recommendation wiring.

Like tests/test_github_action.py, these run review-pr.sh as a subprocess with fake executables:
a `review.py` that records its argv and environment, and a `python3` that records the
environment the triage runs in. The triage itself is the real panel.py, so no network, container
runtime or token is involved.
"""

import json
import os
import shutil
import sys
import tempfile
from pathlib import Path
from unittest.mock import patch

from tests.helpers import REPO_ROOT, PanelTestCase, commit, git, make_repo, write
from tests.test_github_action import (
    REVIEW_PY_REL,
    CheckTestCase,
    PrepareTestCase,
    ScriptTestCase,
    script_constant,
    write_executable,
    write_fake_review_py,
)
from tests.test_github_review import review

TOKEN = "ghs_STANDDOWNSECRET"

# Stands in for review.py: appends its argv, the environment it reads and its working directory
# to FAKE_LOG as one JSON line, prints FAKE_PRINT, and exits FAKE_STATUS.
FAKE_REVIEW_PY = """
import json
import os
import sys

keys = ["UV_NO_CONFIG", "UV_PYTHON", "GIT_CONFIG_COUNT", "GIT_CONFIG_KEY_0",
        "GIT_CONFIG_VALUE_0", "GIT_CONFIG_KEY_1", "GIT_CONFIG_VALUE_1", "GITHUB_TOKEN"]
with open(os.environ["FAKE_LOG"], "a", encoding="utf-8") as f:
    f.write(json.dumps({
        "argv": sys.argv[1:],
        "env": {k: os.environ.get(k) for k in keys},
        "cwd": os.getcwd(),
    }) + "\\n")
if os.environ.get("FAKE_PRINT"):
    print(os.environ["FAKE_PRINT"])
sys.exit(int(os.environ.get("FAKE_STATUS", "0")))
"""

# Stands in for python3. The triage runs under env -i, so the log path and the behavior are part
# of the source, not the environment. Every call is logged with its argv and its whole
# environment. A triage call then fails, prints too much or prints something that is not a plan
# when MODE says so; every other call runs the real interpreter.
FAKE_PYTHON3 = """
import json
import os
import sys

LOG = {log!r}
MODE = {mode!r}
REAL = {real!r}
args = sys.argv[1:]
with open(LOG, "a", encoding="utf-8") as f:
    f.write(json.dumps({{"argv": args, "env": dict(os.environ)}}) + "\\n")
if "triage" in args:
    if MODE == "fail":
        sys.exit(2)
    if MODE == "big":
        sys.stdout.write("{{" + " " * (1024 * 1024) + "}}")
        sys.exit(0)
    if MODE == "garbage":
        print("not a plan")
        sys.exit(0)
    if MODE == "no-tier":
        print(json.dumps({{"tier": {{"name": "deep; rm -rf /"}}}}))
        sys.exit(0)
os.execv(REAL, [REAL] + args)
"""

# Stands in for timeout: records its arguments and reports that the limit was reached.
FAKE_TIMEOUT = """
import os
import sys

with open({log!r}, "w", encoding="utf-8") as f:
    f.write(" ".join(sys.argv[1:]))
sys.exit(124)
"""

TRIAGE_ENV_KEYS = {
    "PATH",
    "HOME",
    "XDG_CONFIG_HOME",
    "GIT_CONFIG_GLOBAL",
    "GIT_CONFIG_NOSYSTEM",
    "GIT_CONFIG_COUNT",
    "GIT_CONFIG_KEY_0",
    "GIT_CONFIG_VALUE_0",
    "GIT_CONFIG_KEY_1",
    "GIT_CONFIG_VALUE_1",
    "GIT_CONFIG_KEY_2",
    "GIT_CONFIG_VALUE_2",
    "PYTHONNOUSERSITE",
    "LANG",
    "TZ",
}


def read_log(path: str) -> list[dict]:
    if not os.path.exists(path):
        return []
    lines = Path(path).read_text(encoding="utf-8").splitlines()
    return [json.loads(line) for line in lines]


class FakeReviewPyMixin:
    """A stand-in action tree whose review.py is FAKE_REVIEW_PY."""

    def install_fake_review_py(self) -> None:
        self.action_path = os.path.join(self.tmp, "action")
        self.log = os.path.join(self.tmp, "review-py.jsonl")
        write_fake_review_py(self.action_path, FAKE_REVIEW_PY)

    def calls(self) -> list[dict]:
        return read_log(self.log)


class StanddownTests(FakeReviewPyMixin, ScriptTestCase):
    # The step runs before the checkout, in RUNNER_TEMP, so no checkout exists here.
    def setUp(self):
        super().setUp()
        os.makedirs(self.workspace)
        self.install_fake_review_py()
        self.github_env = os.path.join(self.tmp, "github_env")

    def standdown(self, **env: str):
        defaults = dict(
            GITHUB_ACTION_PATH=self.action_path,
            FAKE_LOG=self.log,
            GITHUB_TOKEN=TOKEN,
            GITHUB_ENV=self.github_env,
        )
        defaults.update(env)
        return self.run_script("standdown", self.runner_temp, **defaults)

    def test_exit_zero_proceeds(self):
        proc = self.standdown()
        self.assertEqual(proc.returncode, 0, proc.stdout + proc.stderr)
        self.assertEqual(self.github_output(), {"stand_down": "false"})
        self.assertNotIn("::notice::", proc.stdout)

    def test_exit_ten_stands_down_with_a_notice(self):
        proc = self.standdown(FAKE_STATUS="10")
        self.assertEqual(proc.returncode, 0, proc.stdout + proc.stderr)
        self.assertEqual(self.github_output(), {"stand_down": "true"})
        self.assertIn("::notice::", proc.stdout)

    def test_any_other_exit_is_a_bad_context_and_fails_the_step(self):
        for status in ("1", "2", "11", "127"):
            with self.subTest(status=status):
                if os.path.exists(self.output):
                    os.remove(self.output)
                proc = self.standdown(FAKE_STATUS=status)
                self.assertEqual(proc.returncode, 1, proc.stdout + proc.stderr)
                self.assertIn("::error::", proc.stdout)
                self.assertIn(f"exit {status}", proc.stdout)
                self.assertEqual(self.github_output(), {})

    def test_the_token_reaches_review_py_but_never_an_output_or_the_log(self):
        for status in ("0", "10", "1"):
            with self.subTest(status=status):
                proc = self.standdown(FAKE_STATUS=status)
                self.assertEqual(self.calls()[-1]["env"]["GITHUB_TOKEN"], TOKEN)
                self.assertNotIn(TOKEN, proc.stdout + proc.stderr)
                if os.path.exists(self.output):
                    self.assertNotIn(TOKEN, Path(self.output).read_text(encoding="utf-8"))
                self.assertFalse(os.path.exists(self.github_env))

    def test_standard_mode_names_the_deep_job(self):
        self.standdown(AI_REVIEW_MODE="standard", AI_REVIEW_DEEP_JOB="Deep review / panel")
        self.assertEqual(
            self.calls()[-1]["argv"],
            [
                "stand-down",
                "--platform",
                "github",
                "--mode",
                "standard",
                "--deep-job=Deep review / panel",
            ],
        )

    def test_an_empty_mode_and_deep_job_are_the_input_defaults(self):
        self.standdown(AI_REVIEW_MODE="", AI_REVIEW_DEEP_JOB="")
        self.assertEqual(
            self.calls()[-1]["argv"],
            [
                "stand-down",
                "--platform",
                "github",
                "--mode",
                "standard",
                "--deep-job=deep-review",
            ],
        )

    def test_deep_mode_checks_the_head_and_names_no_job(self):
        self.standdown(AI_REVIEW_MODE="deep", AI_REVIEW_DEEP_JOB="deep-review")
        self.assertEqual(
            self.calls()[-1]["argv"], ["stand-down", "--platform", "github", "--mode", "deep"]
        )

    def test_review_py_runs_in_runner_temp_with_no_uv_config_and_hardened_git(self):
        proc = self.standdown()
        self.assertEqual(proc.returncode, 0, proc.stdout + proc.stderr)
        self.assertFalse(os.path.exists(self.head_dir))
        call = self.calls()[-1]
        self.assertEqual(call["cwd"], self.runner_temp)
        env = call["env"]
        self.assertEqual(env["UV_NO_CONFIG"], "1")
        self.assertEqual(env["UV_PYTHON"], shutil.which("python3"))
        overrides = {
            env["GIT_CONFIG_KEY_0"]: env["GIT_CONFIG_VALUE_0"],
            env["GIT_CONFIG_KEY_1"]: env["GIT_CONFIG_VALUE_1"],
        }
        self.assertEqual(overrides, {"core.fsmonitor": "false", "core.hooksPath": "/dev/null"})

    def test_an_invalid_mode_fails_before_review_py(self):
        proc = self.standdown(AI_REVIEW_MODE="thorough")
        self.assertEqual(proc.returncode, 1, proc.stdout + proc.stderr)
        self.assertEqual(self.calls(), [])
        self.assertEqual(self.github_output(), {})

    def test_a_missing_or_non_executable_review_py_fails(self):
        Path(self.action_path, REVIEW_PY_REL).chmod(0o644)
        proc = self.standdown()
        self.assertEqual(proc.returncode, 1, proc.stdout + proc.stderr)
        self.assertIn("not executable", proc.stdout)
        proc = self.standdown(GITHUB_ACTION_PATH="")
        self.assertEqual(proc.returncode, 1, proc.stdout + proc.stderr)
        self.assertEqual(self.calls(), [])

    def test_a_missing_lock_fails_before_review_py(self):
        Path(self.action_path, REVIEW_PY_REL + ".lock").unlink()
        proc = self.standdown()
        self.assertEqual(proc.returncode, 1, proc.stdout + proc.stderr)
        self.assertIn(".lock is missing", proc.stdout)
        self.assertEqual(self.calls(), [])

    def test_a_symlinked_workdir_is_refused(self):
        target = tempfile.mkdtemp(dir=self.tmp)
        os.symlink(target, self.ai_dir)
        proc = self.standdown()
        self.assertEqual(proc.returncode, 1, proc.stdout + proc.stderr)
        self.assertEqual(self.calls(), [])


class TriageTestCase(PrepareTestCase):
    """Runs prepare with the recommendation on and the action's real panel.py."""

    def setUp(self):
        super().setUp()
        self.python_log = os.path.join(self.tmp, "python3.jsonl")
        self.triage_dir = os.path.join(self.runner_temp, "ai-review-triage")
        self.plan = os.path.join(self.triage_dir, "plan.json")

    def fake_python3(self, mode: str = "real") -> None:
        source = FAKE_PYTHON3.format(log=self.python_log, mode=mode, real=sys.executable)
        write_executable(Path(self.bin, "python3"), source)

    def make_pr_touching(self, path: str) -> tuple[str, str, str]:
        repo, _fork = make_repo(self.tmp, {"a.txt": "one\n"}, branch="feature")
        head = commit(repo, {path: "name: x\n"})
        git(repo, "checkout", "-q", "main")
        base = commit(repo, {"main_only.txt": "main\n"})
        git(repo, "checkout", "-q", "--detach", head)
        return self.move_repo_into_place(repo), base, head

    def prepare_with_triage(self, path: str = "action.yml", **env: str):
        repo, base, head = self.make_pr_touching(path)
        defaults = dict(
            GITHUB_ACTION_PATH=str(REPO_ROOT),
            AI_REVIEW_RECOMMEND_DEEP="true",
            LANG="C.UTF-8",
            TZ="UTC",
        )
        defaults.update(env)
        return self.prepare(repo, base, head, **defaults)

    def triage_calls(self) -> list[dict]:
        return [call for call in read_log(self.python_log) if "triage" in call["argv"]]

    def assert_no_plan(self, proc) -> None:
        self.assertEqual(proc.returncode, 0, proc.stdout + proc.stderr)
        self.assertFalse(os.path.exists(self.triage_dir), os.listdir(self.runner_temp))
        # prepare still finished.
        self.assertTrue(os.path.isdir(os.path.join(self.ai_dir, "plugin", "skills")))

    def assert_warned(self, proc) -> None:
        warnings = [line for line in proc.stdout.splitlines() if line.startswith("::warning::")]
        self.assertEqual(len(warnings), 1, proc.stdout)
        self.assertIn("will not recommend a deep review", warnings[0])
        self.assertNotIn("::error::", proc.stdout)


class TriageTests(TriageTestCase):
    def test_a_diff_that_touches_action_yml_gets_the_deep_tier(self):
        proc = self.prepare_with_triage("action.yml")
        self.assertEqual(proc.returncode, 0, proc.stdout + proc.stderr)
        plan = json.loads(Path(self.plan).read_text(encoding="utf-8"))
        self.assertEqual(plan["tier"]["name"], "deep")
        self.assertIn("action.yml (ci)", plan["tier"]["evidence"])
        self.assertIn("Triage tier for the deep-review recommendation: deep", proc.stdout)
        self.assertNotIn("::warning::", proc.stdout)

    def test_the_log_shows_only_the_tier(self):
        proc = self.prepare_with_triage("action.yml")
        self.assertNotIn("action.yml", proc.stdout + proc.stderr)

    def test_a_prose_change_keeps_its_plan_with_a_lower_tier(self):
        proc = self.prepare_with_triage("docs/notes.md")
        self.assertEqual(proc.returncode, 0, proc.stdout + proc.stderr)
        plan = json.loads(Path(self.plan).read_text(encoding="utf-8"))
        self.assertEqual(plan["tier"]["name"], "quick")

    def test_the_plan_is_outside_the_mount_and_private(self):
        self.prepare_with_triage()
        self.assertFalse(self.plan.startswith(self.ai_dir + os.sep))
        self.assertEqual(os.stat(self.triage_dir).st_mode & 0o777, 0o700)
        self.assertEqual(os.listdir(self.triage_dir), ["plan.json"])

    def test_the_triage_env_is_isolated_and_holds_no_token(self):
        self.fake_python3()
        proc = self.prepare_with_triage(
            GITHUB_TOKEN=TOKEN,
            GH_TOKEN=TOKEN,
            ACTIONS_ID_TOKEN_REQUEST_TOKEN=TOKEN,
            OPENAI_API_KEY="sk-secret",
            GIT_PAGER="/elsewhere/pager",
            PYTHONPATH="/elsewhere",
        )
        self.assertEqual(proc.returncode, 0, proc.stdout + proc.stderr)
        [call] = self.triage_calls()
        env = call["env"]
        self.assertEqual(set(env), TRIAGE_ENV_KEYS)
        self.assertNotIn("GITHUB_TOKEN", env)
        self.assertNotIn("GH_TOKEN", env)
        self.assertNotIn(TOKEN, json.dumps(env))
        self.assertEqual(env["LANG"], "C.UTF-8")
        self.assertEqual(env["TZ"], "UTC")
        self.assertEqual(env["GIT_CONFIG_GLOBAL"], "/dev/null")
        self.assertEqual(env["GIT_CONFIG_NOSYSTEM"], "1")
        self.assertEqual(env["PYTHONNOUSERSITE"], "1")
        overrides = {env[f"GIT_CONFIG_KEY_{i}"]: env[f"GIT_CONFIG_VALUE_{i}"] for i in range(3)}
        self.assertEqual(
            overrides,
            {
                "core.fsmonitor": "false",
                "core.hooksPath": "/dev/null",
                "safe.directory": self.repo_dir,
            },
        )
        self.assertEqual(env["GIT_CONFIG_COUNT"], "3")
        # A scratch HOME under RUNNER_TEMP that is gone after the step.
        self.assertTrue(env["HOME"].startswith(self.runner_temp + os.sep), env["HOME"])
        self.assertTrue(env["XDG_CONFIG_HOME"].startswith(env["HOME"] + os.sep))
        self.assertFalse(os.path.exists(env["HOME"]))
        # The real panel still ran.
        self.assertTrue(os.path.isfile(self.plan))

    def test_the_triage_runs_isolated_python_on_the_container_repo(self):
        self.fake_python3()
        repo, base, head = self.make_pr_touching("action.yml")
        self.prepare(
            repo, base, head, GITHUB_ACTION_PATH=str(REPO_ROOT), AI_REVIEW_RECOMMEND_DEEP="1"
        )
        [call] = self.triage_calls()
        self.assertEqual(
            call["argv"],
            [
                "-I",
                str(REPO_ROOT / "skills/code-review-panel/scripts/panel.py"),
                "triage",
                "--repo",
                self.repo_dir,
                "--base",
                base,
                "--head",
                head,
            ],
        )

    def test_unset_lang_and_tz_are_not_invented(self):
        self.fake_python3()
        proc = self.prepare_with_triage(LANG="", TZ="")
        self.assertEqual(proc.returncode, 0, proc.stdout + proc.stderr)
        [call] = self.triage_calls()
        self.assertNotIn("LANG", call["env"])
        self.assertNotIn("TZ", call["env"])

    def test_a_timeout_only_warns(self):
        timeout_log = os.path.join(self.tmp, "timeout.log")
        write_executable(Path(self.bin, "timeout"), FAKE_TIMEOUT.format(log=timeout_log))
        proc = self.prepare_with_triage()
        self.assert_no_plan(proc)
        self.assert_warned(proc)
        self.assertIn("did not finish within 60s", proc.stdout)
        self.assertTrue(
            Path(timeout_log).read_text(encoding="utf-8").startswith("60s env -i "),
        )
        self.assertEqual(script_constant("TRIAGE_TIMEOUT"), "60s")

    def test_a_failed_triage_only_warns(self):
        self.fake_python3("fail")
        proc = self.prepare_with_triage()
        self.assert_no_plan(proc)
        self.assert_warned(proc)
        self.assertIn("exit 2", proc.stdout)

    def test_an_oversized_plan_is_dropped(self):
        self.fake_python3("big")
        proc = self.prepare_with_triage()
        self.assert_no_plan(proc)
        self.assert_warned(proc)
        self.assertIn("1048576 bytes", proc.stdout)

    def test_output_that_is_not_a_plan_is_dropped(self):
        for mode in ("garbage", "no-tier"):
            with self.subTest(mode=mode):
                self.fake_python3(mode)
                proc = self.prepare_with_triage()
                self.assert_no_plan(proc)
                self.assert_warned(proc)
                self.assertNotIn("rm -rf", proc.stdout)
                shutil.rmtree(self.workspace)

    def test_a_missing_panel_only_warns(self):
        # The stand-in action tree has no panel.py.
        proc = self.prepare_with_triage(GITHUB_ACTION_PATH=self.action_path)
        self.assert_no_plan(proc)
        self.assert_warned(proc)

    def test_deep_mode_or_recommend_off_runs_no_triage(self):
        self.fake_python3()
        for env in (
            dict(AI_REVIEW_MODE="deep"),
            dict(AI_REVIEW_RECOMMEND_DEEP="false"),
            dict(AI_REVIEW_RECOMMEND_DEEP=""),
        ):
            with self.subTest(**env):
                proc = self.prepare_with_triage(**env)
                self.assertEqual(proc.returncode, 0, proc.stdout + proc.stderr)
                self.assertEqual(self.triage_calls(), [])
                self.assertFalse(os.path.exists(self.triage_dir))
                self.assertNotIn("Triage tier", proc.stdout)
                shutil.rmtree(self.workspace)

    def test_a_stale_plan_is_removed_even_when_no_triage_runs(self):
        for env in (dict(AI_REVIEW_MODE="deep"), dict(AI_REVIEW_RECOMMEND_DEEP="false")):
            with self.subTest(**env):
                write(self.triage_dir, "plan.json", '{"tier": {"name": "deep"}}')
                proc = self.prepare_with_triage(**env)
                self.assertEqual(proc.returncode, 0, proc.stdout + proc.stderr)
                self.assertFalse(os.path.exists(self.triage_dir))
                shutil.rmtree(self.workspace)

    def test_a_stale_plan_is_replaced_by_a_fresh_one(self):
        write(self.triage_dir, "plan.json", '{"tier": {"name": "stale"}}')
        write(self.triage_dir, "other.json", "{}")
        self.prepare_with_triage("action.yml")
        self.assertEqual(os.listdir(self.triage_dir), ["plan.json"])
        plan = json.loads(Path(self.plan).read_text(encoding="utf-8"))
        self.assertEqual(plan["tier"]["name"], "deep")

    def test_a_failed_triage_leaves_no_stale_plan(self):
        write(self.triage_dir, "plan.json", '{"tier": {"name": "deep"}}')
        self.fake_python3("fail")
        self.assert_no_plan(self.prepare_with_triage())


class PostFlagTests(FakeReviewPyMixin, ScriptTestCase):
    def setUp(self):
        super().setUp()
        repo, _base = make_repo(self.tmp, {"a.txt": "one\n"})
        self.repo = self.move_repo_into_place(repo)
        self.install_fake_review_py()
        write(self.out_dir, "ai-review-output.json", '{"summary": "ok"}')
        self.triage_dir = os.path.join(self.runner_temp, "ai-review-triage")
        self.plan = os.path.join(self.triage_dir, "plan.json")

    def post(self, **env: str):
        defaults = dict(GITHUB_ACTION_PATH=self.action_path, FAKE_LOG=self.log, GITHUB_TOKEN=TOKEN)
        defaults.update(env)
        return self.run_script("post", self.repo, **defaults)

    def argv(self, **env: str) -> list[str]:
        proc = self.post(**env)
        self.assertEqual(proc.returncode, 0, proc.stdout + proc.stderr)
        argv = self.calls()[-1]["argv"]
        self.assertTrue(argv[-1].startswith(self.runner_temp + os.sep), argv)
        return argv[:-1]

    def write_plan(self) -> None:
        write(self.triage_dir, "plan.json", '{"tier": {"name": "deep", "evidence": []}}')

    def test_standard_mode_with_a_plan_passes_every_flag(self):
        self.write_plan()
        self.assertEqual(
            self.argv(AI_REVIEW_MODE="standard", AI_REVIEW_RECOMMEND_DEEP="true"),
            [
                "post",
                "--platform",
                "github",
                "--mode",
                "standard",
                "--deep-job=deep-review",
                "--deep-label=deep-review",
                "--deep-recommendation-plan",
                self.plan,
            ],
        )

    def test_the_deep_label_is_one_argument_whatever_it_looks_like(self):
        for label in ("needs-deep-review", "-deep", "--deep-review", "deep review (panel)"):
            with self.subTest(label=label):
                argv = self.argv(AI_REVIEW_RECOMMEND_DEEP="true", AI_REVIEW_DEEP_LABEL=label)
                self.assertIn(f"--deep-label={label}", argv)
                self.assertEqual(sum(a.startswith("--deep-label") for a in argv), 1)

    def test_an_empty_deep_label_is_the_input_default(self):
        argv = self.argv(AI_REVIEW_RECOMMEND_DEEP="true", AI_REVIEW_DEEP_LABEL="")
        self.assertIn("--deep-label=deep-review", argv)

    def test_the_deep_label_is_passed_without_a_plan(self):
        argv = self.argv(AI_REVIEW_RECOMMEND_DEEP="true", AI_REVIEW_DEEP_LABEL="x")
        self.assertIn("--deep-label=x", argv)
        self.assertNotIn("--deep-recommendation-plan", argv)

    def test_the_deep_label_needs_the_recommendation_and_standard_mode(self):
        for env in (
            dict(AI_REVIEW_RECOMMEND_DEEP="false"),
            dict(AI_REVIEW_RECOMMEND_DEEP=""),
            dict(AI_REVIEW_RECOMMEND_DEEP="true", AI_REVIEW_MODE="deep"),
        ):
            with self.subTest(env=env):
                argv = self.argv(AI_REVIEW_DEEP_LABEL="x", **env)
                self.assertFalse([a for a in argv if a.startswith("--deep-label")], argv)

    def test_standard_mode_passes_the_deep_job_name(self):
        argv = self.argv(AI_REVIEW_DEEP_JOB="Deep review (codex)")
        self.assertIn("--deep-job=Deep review (codex)", argv)

    def test_a_deep_job_name_that_starts_with_a_dash_stays_one_argument(self):
        argv = self.argv(AI_REVIEW_DEEP_JOB="-deep review")
        self.assertIn("--deep-job=-deep review", argv)

    def test_unset_inputs_mean_standard_mode_and_no_plan(self):
        self.write_plan()
        self.assertEqual(
            self.argv(),
            ["post", "--platform", "github", "--mode", "standard", "--deep-job=deep-review"],
        )

    def test_recommend_off_omits_the_plan(self):
        self.write_plan()
        for value in ("false", "0", ""):
            with self.subTest(value=value):
                argv = self.argv(AI_REVIEW_RECOMMEND_DEEP=value)
                self.assertNotIn("--deep-recommendation-plan", argv)

    def test_deep_mode_passes_only_the_mode(self):
        self.write_plan()
        self.assertEqual(
            self.argv(
                AI_REVIEW_MODE="deep", AI_REVIEW_RECOMMEND_DEEP="true", AI_REVIEW_DEEP_JOB="x"
            ),
            ["post", "--platform", "github", "--mode", "deep"],
        )

    def test_a_missing_plan_is_omitted(self):
        argv = self.argv(AI_REVIEW_RECOMMEND_DEEP="true")
        self.assertNotIn("--deep-recommendation-plan", argv)

    def test_a_plan_that_is_not_a_regular_file_is_omitted(self):
        target = os.path.join(self.tmp, "elsewhere.json")
        Path(target).write_text('{"tier": {"name": "deep"}}', encoding="utf-8")
        os.makedirs(self.triage_dir)
        cases = (
            ("symlink", lambda: os.symlink(target, self.plan)),
            ("directory", lambda: os.mkdir(self.plan)),
            ("fifo", lambda: os.mkfifo(self.plan)),
        )
        for name, make in cases:
            with self.subTest(name):
                make()
                argv = self.argv(AI_REVIEW_RECOMMEND_DEEP="true")
                self.assertNotIn("--deep-recommendation-plan", argv)
                if os.path.isdir(self.plan) and not os.path.islink(self.plan):
                    os.rmdir(self.plan)
                else:
                    os.remove(self.plan)

    def test_a_symlinked_triage_dir_is_not_followed(self):
        target = tempfile.mkdtemp(dir=self.tmp)
        write(target, "plan.json", '{"tier": {"name": "deep"}}')
        os.symlink(target, self.triage_dir)
        argv = self.argv(AI_REVIEW_RECOMMEND_DEEP="true")
        self.assertNotIn("--deep-recommendation-plan", argv)

    def test_an_invalid_mode_fails_before_review_py(self):
        proc = self.post(AI_REVIEW_MODE="thorough")
        self.assertEqual(proc.returncode, 1, proc.stdout + proc.stderr)
        self.assertEqual(self.calls(), [])


class DeepLabelCheckTests(CheckTestCase):
    """The check step fails on a label review.py would reject, before the agent runs."""

    def check_label(self, label: str, **env: str):
        Path(self.output).unlink(missing_ok=True)
        return self.check(AI_REVIEW_RECOMMEND_DEEP="true", AI_REVIEW_DEEP_LABEL=label, **env)

    def test_good_labels_pass(self):
        for label in ("", "deep-review", "needs: deep review", "-x", "é" * 50, "🔍 deep"):
            with self.subTest(label=label):
                proc = self.check_label(label)
                self.assertEqual(proc.returncode, 0, proc.stdout + proc.stderr)
                self.assertEqual(self.github_output(), {"skip": "false"})

    def test_bad_labels_fail_without_echoing_them(self):
        cases = {
            "blank": " \t ",
            "too long": "x" * 51,
            "newline": "deep\n::warning::injected",
            "carriage return": "deep\rreview",
            "escape": "deep\x1b[31m",
            "zero-width space": "deep\u200breview",
            "line separator": "deep\u2028review",
            "paragraph separator": "deep\u2029review",
        }
        for name, label in cases.items():
            with self.subTest(name):
                proc = self.check_label(label)
                self.assertEqual(proc.returncode, 1, proc.stdout + proc.stderr)
                self.assertIn("::error::deep_review_label ", proc.stdout)
                self.assertNotIn("injected", proc.stdout)
                self.assertEqual(self.github_output(), {})

    def test_a_label_post_does_not_pass_is_not_checked(self):
        for env in (
            dict(AI_REVIEW_RECOMMEND_DEEP="false"),
            dict(AI_REVIEW_RECOMMEND_DEEP="true", AI_REVIEW_MODE="deep"),
        ):
            with self.subTest(env=env):
                Path(self.output).unlink(missing_ok=True)
                proc = self.check(AI_REVIEW_DEEP_LABEL="x" * 51, **env)
                self.assertEqual(proc.returncode, 0, proc.stdout + proc.stderr)


class CleanupTriageTests(ScriptTestCase):
    def cleanup(self, **env: str):
        return self.run_script("cleanup", self.tmp, **env)

    def test_cleanup_removes_the_triage_dir(self):
        write(self.runner_temp, "ai-review-triage/plan.json", "{}")
        write(self.runner_temp, "other/file.txt", "x\n")
        proc = self.cleanup()
        self.assertEqual(proc.returncode, 0, proc.stdout + proc.stderr)
        self.assertEqual(os.listdir(self.runner_temp), ["other"])

    def test_cleanup_removes_a_symlinked_triage_dir_but_not_its_target(self):
        target = tempfile.mkdtemp(dir=self.tmp)
        write(target, "precious.txt", "x\n")
        os.symlink(target, os.path.join(self.runner_temp, "ai-review-triage"))
        proc = self.cleanup()
        self.assertEqual(proc.returncode, 0, proc.stdout + proc.stderr)
        self.assertFalse(os.path.lexists(os.path.join(self.runner_temp, "ai-review-triage")))
        self.assertTrue(os.path.exists(os.path.join(target, "precious.txt")))

    def test_cleanup_without_runner_temp_touches_no_root_path(self):
        proc = self.cleanup(RUNNER_TEMP="")
        self.assertEqual(proc.returncode, 0, proc.stdout + proc.stderr)
        self.assertEqual(self.github_output(), {})


class ReviewPyParserTests(FakeReviewPyMixin, ScriptTestCase):
    """The real review.py parser accepts every subcommand and flag the script passes."""

    def setUp(self):
        super().setUp()
        repo, _base = make_repo(self.tmp, {"a.txt": "one\n"})
        self.repo = self.move_repo_into_place(repo)
        self.install_fake_review_py()
        write(self.out_dir, "ai-review-output.json", '{"summary": "ok"}')
        write(self.runner_temp, "ai-review-triage/plan.json", "{}")

    def recorded_argvs(self, deep_job: str = "Deep review / panel") -> list[list[str]]:
        common = dict(GITHUB_ACTION_PATH=self.action_path, FAKE_LOG=self.log, GITHUB_TOKEN=TOKEN)
        start = len(self.calls())
        for mode in ("standard", "deep"):
            for command in ("standdown", "post"):
                proc = self.run_script(
                    command,
                    self.repo,
                    AI_REVIEW_MODE=mode,
                    AI_REVIEW_RECOMMEND_DEEP="true",
                    AI_REVIEW_DEEP_JOB=deep_job,
                    **common,
                )
                self.assertEqual(proc.returncode, 0, proc.stdout + proc.stderr)
        return [call["argv"] for call in self.calls()[start:]]

    def parse(self, argv: list[str]):
        parsed = []
        with (
            patch.object(sys, "argv", ["review.py", *argv]),
            patch.object(review, "cmd_post", side_effect=parsed.append),
            patch.object(review, "cmd_stand_down", side_effect=parsed.append),
        ):
            review.main()
        self.assertEqual(len(parsed), 1, argv)
        return parsed[0]

    def test_every_recorded_argv_parses(self):
        argvs = self.recorded_argvs()
        self.assertEqual(len(argvs), 4)
        flags = set()
        for argv in argvs:
            with self.subTest(argv=argv):
                args = self.parse(argv)
                self.assertEqual(args.platform, "github")
                self.assertIn(args.mode, ("standard", "deep"))
                flags.update(a.split("=", 1)[0] for a in argv if a.startswith("--"))
        self.assertEqual(
            flags,
            {"--platform", "--mode", "--deep-job", "--deep-label", "--deep-recommendation-plan"},
        )
        post = self.parse(argvs[1])
        self.assertEqual(post.deep_job, "Deep review / panel")
        self.assertTrue(post.deep_recommendation_plan.endswith("ai-review-triage/plan.json"))
        self.assertEqual(post.review_file, argvs[1][-1])

    def test_the_deep_job_name_round_trips_whatever_it_looks_like(self):
        names = (
            "deep-review",
            "Deep review / panel",
            "-deep",
            "--deep-review",
            "-",
            "deep=review",
            "Deep review (codex, -x)",
        )
        for name in names:
            with self.subTest(name=name):
                standdown, post = self.recorded_argvs(name)[:2]
                self.assertEqual(self.parse(standdown).deep_job, name)
                parsed = self.parse(post)
                self.assertEqual(parsed.deep_job, name)
                self.assertEqual(parsed.review_file, post[-1])

    def test_the_deep_label_round_trips_whatever_it_looks_like(self):
        common = dict(GITHUB_ACTION_PATH=self.action_path, FAKE_LOG=self.log, GITHUB_TOKEN=TOKEN)
        for label in ("deep-review", "-deep", "--deep-review", "deep=review", "deep review (x)"):
            with self.subTest(label=label):
                proc = self.run_script(
                    "post",
                    self.repo,
                    AI_REVIEW_RECOMMEND_DEEP="true",
                    AI_REVIEW_DEEP_LABEL=label,
                    **common,
                )
                self.assertEqual(proc.returncode, 0, proc.stdout + proc.stderr)
                post = self.calls()[-1]["argv"]
                parsed = self.parse(post)
                self.assertEqual(parsed.deep_label, label)
                self.assertEqual(parsed.review_file, post[-1])


class StanddownConstantTests(PanelTestCase):
    def test_the_stand_down_exit_matches_review_py(self):
        self.assertEqual(script_constant("STANDDOWN_EXIT"), str(review._STANDDOWN_EXIT))

    def test_the_default_deep_job_matches_review_py(self):
        self.assertEqual(
            script_constant("DEFAULT_DEEP_JOB"), f'"{review._GITHUB_DEEP_JOB_DEFAULT}"'
        )

    def test_the_default_deep_label_matches_review_py(self):
        self.assertEqual(
            script_constant("DEFAULT_DEEP_LABEL"), f'"{review._GITHUB_DEEP_LABEL_DEFAULT}"'
        )

    def test_the_plan_limit_matches_review_py(self):
        self.assertEqual(script_constant("MAX_PLAN_BYTES"), str(review._PLAN_MAX_BYTES))
