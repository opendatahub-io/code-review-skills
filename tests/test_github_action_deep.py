"""Tests for the action's deep mode, the draft skip and the git repository the container gets.

Like tests/test_github_action.py, these run review-pr.sh against throwaway git repos with fake
`uvx`, `podman` and `timeout` executables: no network, container runtime or token.
"""

import os
import re
import shutil
from pathlib import Path

from tests.helpers import REPO_ROOT, PanelTestCase, git, run, write
from tests.test_github_action import (
    README,
    CheckTestCase,
    PrepareTestCase,
    ReviewTestCase,
    find_step,
    input_default,
    script_constant,
    write_executable,
)

# The keys `git init` writes with an empty template. Anything else in the container repo's
# config came from somewhere it should not have.
CONFIG_ALLOW_LIST = {
    "core.repositoryformatversion",
    "core.filemode",
    "core.bare",
    "core.logallrefupdates",
    "core.symlinks",
    "core.ignorecase",
    "core.precomposeunicode",
}
FORBIDDEN_CONFIG_PREFIXES = (
    "remote.",
    "url.",
    "credential.",
    "include.",
    "includeif.",
    "http.",
    "filter.",
    "core.hookspath",
    "core.fsmonitor",
    "core.sshcommand",
    "core.worktree",
)
SECRET = "ghs_SECRETSECRETSECRET"


SAFE_DIRECTORY = (
    "First run git config --global --add safe.directory /workspace/repo, because the "
    "repository can belong to another user."
)


def deep_prompt(harness: str, base: str, head: str) -> str:
    task = (
        f"the committed changes in /workspace/repo from base {base} to head {head}. "
        f"{SAFE_DIRECTORY} Run its triage from inside /workspace/repo with --base {base} "
        f"--head {head}, pass --gitlab-json /workspace/out/ai-review-output.json to its final "
        "report command, and do not post anything."
    )
    if harness == "claude-code":
        return f"/code-review-skills:code-review-panel Review {task}"
    return f"Invoke the /code-review-skills:code-review-panel skill to review {task}"


# Stands in for git: reports FAKE_GIT_VERSION and hands every other call to the real git.
FAKE_GIT = """
import os
import sys

if sys.argv[1:] == ["--version"]:
    print(os.environ["FAKE_GIT_VERSION"])
    sys.exit(0)
os.execv(os.environ["REAL_GIT"], ["git"] + sys.argv[1:])
"""


class ModeStaticTests(PanelTestCase):
    def test_mode_defaults_to_standard(self):
        self.assertEqual(input_default("mode"), "standard")

    def test_timeout_input_is_empty_so_the_mode_picks_the_default(self):
        self.assertEqual(input_default("timeout_minutes"), "")
        self.assertEqual(script_constant("STANDARD_TIMEOUT_MINUTES"), "20")
        self.assertEqual(script_constant("DEEP_TIMEOUT_MINUTES"), "75")

    def test_the_new_env_holds_no_token(self):
        for step in ("Check the event and credentials", "Prepare the diff"):
            with self.subTest(step):
                self.assertNotIn("github.token", find_step(step))

    def test_readme_triggers_include_ready_for_review(self):
        self.assertIn(
            "types: [opened, synchronize, reopened, ready_for_review]",
            README.read_text(encoding="utf-8"),
        )

    def test_readme_deep_job_is_labeled_bounded_and_in_its_own_group(self):
        readme = README.read_text(encoding="utf-8")
        m = re.search(r"(?ms)^  deep-review:\n(.*?)^```", readme)
        self.assertTrue(m, "no deep-review job in the README")
        job = m.group(1)
        self.assertRegex(job, r"(?m)^    timeout-minutes: 120$")
        self.assertRegex(job, r"(?m)^          mode: deep$")
        self.assertIn("github.event.label.name == 'deep-review'", job)
        self.assertRegex(job, r"(?m)^      group: ai-deep-review-")
        self.assertIn("types: [labeled, synchronize, reopened, ready_for_review]", readme)


class DraftAndModeCheckTests(CheckTestCase):
    def test_a_draft_is_skipped_in_standard_mode(self):
        for mode in ("standard", ""):
            with self.subTest(mode=mode):
                Path(self.output).unlink(missing_ok=True)
                proc = self.check(AI_REVIEW_DRAFT="true", AI_REVIEW_MODE=mode)
                self.assertEqual(proc.returncode, 0, proc.stdout + proc.stderr)
                self.assertEqual(self.github_output(), {"skip": "true"})
                self.assertIn("::notice::", proc.stdout)
                self.assertIn("draft", proc.stdout)

    def test_a_draft_or_a_missing_credential_fails_the_deep_job(self):
        # A deep job that skips succeeds, and the standard review stands down for it.
        cases = {
            "draft": dict(AI_REVIEW_DRAFT="true"),
            "no codex key": dict(OPENAI_API_KEY=""),
            "no claude-code credential": dict(AI_REVIEW_HARNESS="claude-code"),
        }
        for name, overrides in cases.items():
            with self.subTest(name):
                Path(self.output).unlink(missing_ok=True)
                proc = self.check(AI_REVIEW_MODE="deep", **overrides)
                self.assertEqual(proc.returncode, 1, proc.stdout + proc.stderr)
                self.assertIn("::error::The deep review cannot run", proc.stdout)
                self.assertNotIn("::notice::", proc.stdout)
                self.assertEqual(self.github_output(), {})

    def test_forks_and_dependabot_still_skip_the_deep_job(self):
        # Their runs get no secrets, so a draft or a missing key there is expected.
        cases = {
            "fork": dict(AI_REVIEW_HEAD_REPO="fork/r", OPENAI_API_KEY=""),
            "draft fork": dict(AI_REVIEW_HEAD_REPO="fork/r", AI_REVIEW_DRAFT="true"),
            "dependabot": dict(AI_REVIEW_ACTOR="dependabot[bot]", OPENAI_API_KEY=""),
            "dependabot draft": dict(
                AI_REVIEW_TRIGGERING_ACTOR="dependabot[bot]", AI_REVIEW_DRAFT="true"
            ),
        }
        for name, overrides in cases.items():
            with self.subTest(name):
                Path(self.output).unlink(missing_ok=True)
                proc = self.check(AI_REVIEW_MODE="deep", **overrides)
                self.assertEqual(proc.returncode, 0, proc.stdout + proc.stderr)
                self.assertEqual(self.github_output(), {"skip": "true"})
                self.assertIn("::notice::", proc.stdout)
                self.assertNotIn("::error::", proc.stdout)

    def test_a_missing_credential_still_skips_the_standard_job(self):
        proc = self.check(AI_REVIEW_MODE="standard", OPENAI_API_KEY="")
        self.assertEqual(proc.returncode, 0, proc.stdout + proc.stderr)
        self.assertEqual(self.github_output(), {"skip": "true"})

    def test_a_ready_pull_request_proceeds_in_both_modes(self):
        for mode in ("standard", "deep", ""):
            for draft in ("false", ""):
                with self.subTest(mode=mode, draft=draft):
                    Path(self.output).unlink(missing_ok=True)
                    proc = self.check(AI_REVIEW_DRAFT=draft, AI_REVIEW_MODE=mode)
                    self.assertEqual(proc.returncode, 0, proc.stdout + proc.stderr)
                    self.assertEqual(self.github_output(), {"skip": "false"})

    def test_an_invalid_mode_fails_even_for_a_draft(self):
        for value in ("Deep", "panel", "deep ", "x;id"):
            for draft in ("true", "false"):
                with self.subTest(value=value, draft=draft):
                    Path(self.output).unlink(missing_ok=True)
                    proc = self.check(AI_REVIEW_MODE=value, AI_REVIEW_DRAFT=draft)
                    self.assertEqual(proc.returncode, 1, proc.stdout + proc.stderr)
                    self.assertIn("::error::mode must be standard or deep", proc.stdout)
                    self.assertEqual(self.github_output(), {})


class DeepReviewTests(ReviewTestCase):
    def setUp(self):
        super().setUp()
        # Stands in for the .git prepare builds. The review step only tests that it exists.
        os.makedirs(os.path.join(self.repo_dir, ".git"))

    def deep_argv(self, harness: str = "codex", minutes: int = 75) -> list[str]:
        prompt = deep_prompt(harness, self.BASE, self.HEAD)
        return self.expected_argv(harness=harness, minutes=minutes, prompt=prompt)

    def test_deep_argv_for_claude_code(self):
        proc = self.review(AI_REVIEW_MODE="deep", AI_REVIEW_HARNESS="claude-code")
        self.assertEqual(proc.returncode, 0, proc.stdout + proc.stderr)
        self.assertEqual(self.record()["argv"], self.deep_argv(harness="claude-code"))

    def test_deep_argv_for_codex(self):
        proc = self.review(AI_REVIEW_MODE="deep", AI_REVIEW_HARNESS="codex", OPENAI_API_KEY="k")
        self.assertEqual(proc.returncode, 0, proc.stdout + proc.stderr)
        self.assertEqual(self.record()["argv"], self.deep_argv(harness="codex"))

    def test_an_empty_harness_runs_the_codex_panel(self):
        for harness in ({}, {"AI_REVIEW_HARNESS": ""}):
            with self.subTest(harness=harness):
                proc = self.review(AI_REVIEW_MODE="deep", **harness)
                self.assertEqual(proc.returncode, 0, proc.stdout + proc.stderr)
                self.assertEqual(self.record()["argv"], self.deep_argv(harness="codex"))

    def test_the_deep_prompt_makes_git_accept_the_mounted_repo(self):
        # On a root runner the agent's user does not own the mount, and git refuses it.
        for harness in ("claude-code", "codex"):
            with self.subTest(harness):
                self.review(AI_REVIEW_MODE="deep", AI_REVIEW_HARNESS=harness)
                argv = self.record()["argv"]
                prompt = argv[argv.index("--effort") + 2]
                self.assertIn(SAFE_DIRECTORY, prompt)
                self.assertLess(prompt.index(SAFE_DIRECTORY), prompt.index("triage"))

    def test_the_standard_prompt_does_not_touch_git_config(self):
        for harness in ("claude-code", "codex"):
            with self.subTest(harness):
                self.review(AI_REVIEW_HARNESS=harness)
                self.assertNotIn("safe.directory", " ".join(self.record()["argv"]))

    def test_the_deep_prompt_is_one_line(self):
        for harness in ("claude-code", "codex"):
            with self.subTest(harness):
                self.review(AI_REVIEW_MODE="deep", AI_REVIEW_HARNESS=harness, OPENAI_API_KEY="k")
                argv = self.record()["argv"]
                prompt = argv[argv.index("--effort") + 2]
                self.assertNotIn("\n", prompt)
                self.assertIn("--gitlab-json /workspace/out/ai-review-output.json", prompt)

    def test_deep_mode_ignores_the_instructions_with_a_warning(self):
        proc = self.review(AI_REVIEW_MODE="deep", AI_REVIEW_INSTRUCTIONS="Focus on tests")
        self.assertEqual(proc.returncode, 0, proc.stdout + proc.stderr)
        self.assertEqual(self.record()["argv"], self.deep_argv())
        self.assertNotIn("instructions", " ".join(self.record()["argv"]))
        self.assertIn("::warning::review_instructions is ignored in deep mode", proc.stdout)

    def test_deep_mode_without_instructions_does_not_warn(self):
        proc = self.review(AI_REVIEW_MODE="deep", AI_REVIEW_INSTRUCTIONS="")
        self.assertNotIn("::warning::", proc.stdout)

    def test_deep_mode_ignores_a_stray_instructions_file(self):
        # The warning follows the input, and the file never reaches the deep prompt.
        write(self.ai_dir, "instructions.md", "Focus on tests\n")
        proc = self.review(AI_REVIEW_MODE="deep")
        self.assertEqual(proc.returncode, 0, proc.stdout + proc.stderr)
        self.assertEqual(self.record()["argv"], self.deep_argv())
        self.assertNotIn("::warning::", proc.stdout)

    def test_standard_mode_with_instructions_does_not_warn(self):
        write(self.ai_dir, "instructions.md", "Focus on tests\n")
        proc = self.review(AI_REVIEW_MODE="standard", AI_REVIEW_INSTRUCTIONS="Focus on tests\n")
        self.assertEqual(proc.returncode, 0, proc.stdout + proc.stderr)
        self.assertEqual(self.record()["argv"], self.expected_argv(instructions=True))
        self.assertNotIn("::warning::", proc.stdout)

    def test_explicit_standard_mode_keeps_the_standard_argv(self):
        for harness in ("claude-code", "codex"):
            with self.subTest(harness):
                proc = self.review(
                    AI_REVIEW_MODE="standard", AI_REVIEW_HARNESS=harness, OPENAI_API_KEY="k"
                )
                self.assertEqual(proc.returncode, 0, proc.stdout + proc.stderr)
                self.assertEqual(self.record()["argv"], self.expected_argv(harness=harness))

    def test_a_codex_deep_run_without_output_names_both_skills(self):
        proc = self.review(write_output=False, AI_REVIEW_MODE="deep")
        self.assertEqual(proc.returncode, 1, proc.stdout + proc.stderr)
        self.assertIn("produced no output", proc.stdout)
        self.assertIn("must ship github-code-review and code-review-panel", proc.stdout)
        self.assertIn("image input", proc.stdout)

    def test_timeout_defaults_follow_the_mode(self):
        log = os.path.join(self.tmp, "timeout.log")
        self.fake_timeout(124)
        for mode, minutes in (("standard", 20), ("deep", 75), ("", 20)):
            with self.subTest(mode=mode):
                proc = self.review(FAKE_TIMEOUT_LOG=log, AI_REVIEW_MODE=mode)
                self.assertEqual(proc.returncode, 1, proc.stdout + proc.stderr)
                self.assertEqual(
                    Path(log).read_text(encoding="utf-8"), f"-s INT --kill-after=60s {minutes}m"
                )
                self.assertIn(f"did not finish within {minutes} minutes", proc.stdout)

    def test_an_explicit_timeout_wins_in_deep_mode(self):
        proc = self.review(AI_REVIEW_MODE="deep", AI_REVIEW_TIMEOUT_MINUTES="30")
        self.assertEqual(proc.returncode, 0, proc.stdout + proc.stderr)
        self.assertEqual(self.record()["argv"], self.deep_argv(minutes=30))

    def test_an_explicit_timeout_is_still_validated_in_deep_mode(self):
        for value in ("0", "1.5", "soon"):
            with self.subTest(value):
                proc = self.review(AI_REVIEW_MODE="deep", AI_REVIEW_TIMEOUT_MINUTES=value)
                self.assertEqual(proc.returncode, 1, proc.stdout + proc.stderr)
                self.assertIn("timeout_minutes must be a positive integer", proc.stdout)
                self.assertFalse(os.path.exists(self.log))

    def test_an_invalid_mode_fails_before_the_pull_and_the_agent(self):
        for value in ("Deep", "panel", "x;id"):
            with self.subTest(value):
                proc = self.review(AI_REVIEW_MODE=value)
                self.assertEqual(proc.returncode, 1, proc.stdout + proc.stderr)
                self.assertIn("mode must be standard or deep", proc.stdout)
                self.assertFalse(os.path.exists(self.log))
                self.assertEqual(self.podman_calls(), [])

    def test_deep_mode_needs_the_container_repo(self):
        os.rmdir(os.path.join(self.repo_dir, ".git"))
        proc = self.review(AI_REVIEW_MODE="deep")
        self.assertEqual(proc.returncode, 1, proc.stdout + proc.stderr)
        self.assertIn(".git is missing", proc.stdout)
        self.assertFalse(os.path.exists(self.log))

    def test_the_agent_holds_no_write_token_in_deep_mode(self):
        self.review(AI_REVIEW_MODE="deep")
        env = self.record()["env"]
        self.assertEqual(env["OPENAI_API_KEY"], "sk-test")
        for var in (
            "GITHUB_TOKEN",
            "GH_TOKEN",
            "ACTIONS_ID_TOKEN_REQUEST_TOKEN",
            "ACTIONS_ID_TOKEN_REQUEST_URL",
        ):
            self.assertEqual(env[var], "", var)


# Stands in for a command a hostile checkout config would run: leaves a marker behind.
MARKER_SCRIPT = """
import os
import sys

open(os.path.join(os.environ.get("MARKER_DIR", os.path.dirname(sys.argv[0])), "RAN"), "w").close()
"""


class ContainerRepoTests(PrepareTestCase):
    def prepared(self, **env: str) -> tuple[str, str, str]:
        repo, base, head = self.make_pr()
        proc = self.prepare(repo, base, head, **env)
        self.assertEqual(proc.returncode, 0, proc.stdout + proc.stderr)
        return repo, base, head

    def container_git(self, *args: str) -> str:
        return git(self.repo_dir, *args).stdout.decode().strip()

    def config_keys(self) -> set[str]:
        out = run(
            self.repo_dir,
            "git",
            "config",
            "--file",
            os.path.join(self.repo_dir, ".git/config"),
            "--list",
            "--name-only",
        ).stdout.decode()
        return {line.lower() for line in out.splitlines() if line}

    def git_files(self) -> list[Path]:
        return [p for p in Path(self.repo_dir, ".git").rglob("*") if p.is_file()]

    def test_head_is_detached_at_the_pr_head(self):
        _repo, _base, head = self.prepared()
        self.assertEqual(self.container_git("rev-parse", "HEAD"), head)
        detached = git(self.repo_dir, "symbolic-ref", "-q", "HEAD", check=False)
        self.assertNotEqual(detached.returncode, 0)

    def test_merge_base_works_and_matches_the_checkout(self):
        repo, base, head = self.prepared()
        expected = git(repo, "merge-base", base, head).stdout.decode().strip()
        self.assertEqual(self.container_git("merge-base", base, head), expected)

    def test_full_history_of_the_base_and_the_head(self):
        repo, base, head = self.prepared()
        for sha in (base, head):
            with self.subTest(sha=sha):
                expected = git(repo, "rev-list", "--count", sha).stdout.decode().strip()
                self.assertEqual(self.container_git("rev-list", "--count", sha), expected)
        self.assertFalse(Path(self.repo_dir, ".git/shallow").exists())

    def test_status_reads_clean_and_the_index_is_the_head_tree(self):
        _repo, _base, head = self.prepared()
        self.assertEqual(self.container_git("status", "--porcelain"), "")
        self.assertEqual(
            self.container_git("write-tree"), self.container_git("rev-parse", f"{head}^{{tree}}")
        )

    def test_the_export_is_not_rewritten_from_the_head(self):
        repo, base, head = self.make_pr()
        write(repo, "a.txt", "changed in the checkout\n")
        proc = self.prepare(repo, base, head)
        self.assertEqual(proc.returncode, 0, proc.stdout + proc.stderr)
        self.assertEqual(
            Path(self.repo_dir, "a.txt").read_text(encoding="utf-8"), "changed in the checkout\n"
        )

    def test_config_holds_only_repository_format_keys(self):
        self.prepared()
        keys = self.config_keys()
        self.assertTrue(keys, "the container repo has no config")
        self.assertLessEqual(keys, CONFIG_ALLOW_LIST)

    def test_no_hooks_alternates_fetch_head_or_reflogs(self):
        self.prepared()
        dot_git = Path(self.repo_dir, ".git")
        hooks = dot_git / "hooks"
        self.assertTrue(not hooks.exists() or not any(hooks.iterdir()))
        self.assertFalse((dot_git / "objects/info/alternates").exists())
        self.assertFalse((dot_git / "FETCH_HEAD").exists())
        self.assertFalse((dot_git / "logs").exists())
        self.assertEqual(self.container_git("remote"), "")

    def test_objects_are_copies_not_hardlinks(self):
        self.prepared()
        objects = [p for p in Path(self.repo_dir, ".git/objects").rglob("*") if p.is_file()]
        self.assertTrue(objects)
        for path in objects:
            self.assertEqual(os.stat(path).st_nlink, 1, path)

    def test_no_host_path_bytes_under_git(self):
        self.prepared()
        for host_path in (self.head_dir, self.workspace, self.tmp):
            needle = os.fsencode(host_path)
            for path in self.git_files():
                with self.subTest(path=str(path), host_path=host_path):
                    self.assertNotIn(needle, path.read_bytes())

    def test_hostile_checkout_config_does_not_carry_over(self):
        repo, base, head = self.make_pr()
        marker_dir = os.path.join(self.tmp, "marker")
        os.makedirs(marker_dir)
        script = write_executable(Path(self.tmp, "hostile", "run"), MARKER_SCRIPT)
        hooks = os.path.join(self.tmp, "hostile", "hooks")
        for hook in ("reference-transaction", "post-checkout", "post-index-change"):
            write_executable(Path(hooks, hook), MARKER_SCRIPT)
        for key, value in (
            ("remote.origin.url", f"https://x-access-token:{SECRET}@github.com/o/r"),
            ("remote.origin.fetch", "+refs/heads/*:refs/remotes/origin/*"),
            ("url.https://evil.example/.insteadOf", "https://github.com/"),
            ("http.https://github.com/.extraheader", f"AUTHORIZATION: basic {SECRET}"),
            ("credential.helper", f"!{script}"),
            ("core.fsmonitor", str(script)),
            ("core.hooksPath", hooks),
            ("include.path", os.path.join(self.tmp, "hostile", "included")),
        ):
            git(repo, "config", key, value)
        write(self.tmp, "hostile/included", "[core]\n\tsshCommand = evil\n")
        proc = self.prepare(repo, base, head, MARKER_DIR=marker_dir)
        self.assertEqual(proc.returncode, 0, proc.stdout + proc.stderr)
        self.assertEqual(os.listdir(marker_dir), [])
        keys = self.config_keys()
        self.assertLessEqual(keys, CONFIG_ALLOW_LIST)
        for key in keys:
            self.assertFalse(key.startswith(FORBIDDEN_CONFIG_PREFIXES), key)
        for path in self.git_files():
            with self.subTest(path=str(path)):
                data = path.read_bytes()
                self.assertNotIn(SECRET.encode(), data)
                self.assertNotIn(b"evil", data)
        self.assertEqual(self.container_git("rev-parse", "HEAD"), head)

    def test_a_stale_repo_is_replaced(self):
        repo, base, head = self.make_pr()
        write(self.repo_dir, ".git/config", f'[remote "origin"]\n\turl = https://{SECRET}@x\n')
        write(self.repo_dir, ".git/objects/info/alternates", "/elsewhere\n")
        write(self.repo_dir, ".git/hooks/post-checkout", "#!/bin/sh\n")
        proc = self.prepare(repo, base, head)
        self.assertEqual(proc.returncode, 0, proc.stdout + proc.stderr)
        self.assertLessEqual(self.config_keys(), CONFIG_ALLOW_LIST)
        self.assertFalse(Path(self.repo_dir, ".git/objects/info/alternates").exists())
        self.assertFalse(Path(self.repo_dir, ".git/hooks").exists())
        self.assertEqual(self.container_git("rev-parse", "HEAD"), head)

    def test_both_modes_build_the_same_repo(self):
        for mode in ("standard", "deep"):
            with self.subTest(mode):
                shutil.rmtree(self.workspace, ignore_errors=True)
                _repo, _base, head = self.prepared(AI_REVIEW_MODE=mode)
                self.assertEqual(self.container_git("rev-parse", "HEAD"), head)
                self.assertLessEqual(self.config_keys(), CONFIG_ALLOW_LIST)

    def test_deep_mode_writes_no_instructions_into_the_mount(self):
        repo, base, head = self.make_pr()
        write(self.ai_dir, "instructions.md", "stale")
        proc = self.prepare(repo, base, head, AI_REVIEW_MODE="deep", AI_REVIEW_INSTRUCTIONS="x")
        self.assertEqual(proc.returncode, 0, proc.stdout + proc.stderr)
        self.assertFalse(Path(self.ai_dir, "instructions.md").exists())
        self.assertNotIn("::warning::", proc.stdout)

    def test_standard_mode_still_writes_the_instructions(self):
        repo, base, head = self.make_pr()
        for mode in ("standard", ""):
            with self.subTest(mode=mode):
                proc = self.prepare(
                    repo, base, head, AI_REVIEW_MODE=mode, AI_REVIEW_INSTRUCTIONS="x"
                )
                self.assertEqual(proc.returncode, 0, proc.stdout + proc.stderr)
                self.assertEqual(
                    Path(self.ai_dir, "instructions.md").read_text(encoding="utf-8"), "x"
                )

    def fake_git(self, version: str) -> dict[str, str]:
        write_executable(Path(self.bin, "git"), FAKE_GIT)
        return dict(FAKE_GIT_VERSION=version, REAL_GIT=shutil.which("git"))

    def test_an_old_git_fails_before_touching_the_workdir(self):
        repo, base, head = self.make_pr()
        for version in ("git version 2.28.1", "git version 1.99.0", "git version 2.9.5"):
            with self.subTest(version):
                proc = self.prepare(repo, base, head, **self.fake_git(version))
                self.assertEqual(proc.returncode, 1, proc.stdout + proc.stderr)
                self.assertIn("::error::git 2.29 or later is required", proc.stdout)
                self.assertIn(version, proc.stdout)
                self.assertFalse(Path(self.ai_dir, "pr.diff").exists())

    def test_a_git_that_is_new_enough_proceeds(self):
        for version in (
            "git version 2.29.0",
            "git version 2.39.3 (Apple Git-146)",
            "git version 2.043.0",
            "git version 3.0.0",
        ):
            with self.subTest(version):
                shutil.rmtree(self.workspace, ignore_errors=True)
                repo, base, head = self.make_pr()
                proc = self.prepare(repo, base, head, **self.fake_git(version))
                self.assertEqual(proc.returncode, 0, proc.stdout + proc.stderr)
                self.assertTrue(Path(self.ai_dir, "pr.diff").exists())

    def test_an_unreadable_git_version_fails(self):
        repo, base, head = self.make_pr()
        proc = self.prepare(repo, base, head, **self.fake_git("something else"))
        self.assertEqual(proc.returncode, 1, proc.stdout + proc.stderr)
        self.assertIn("::error::Could not read the git version", proc.stdout)

    def test_an_invalid_mode_fails_before_touching_the_workdir(self):
        repo, base, head = self.make_pr()
        proc = self.prepare(repo, base, head, AI_REVIEW_MODE="panel")
        self.assertEqual(proc.returncode, 1, proc.stdout + proc.stderr)
        self.assertIn("mode must be standard or deep", proc.stdout)
        self.assertFalse(Path(self.ai_dir, "pr.diff").exists())

    def test_the_scratch_directory_is_removed(self):
        self.prepared()
        leftovers = [n for n in os.listdir(self.runner_temp) if n.startswith("ai-review-tmp.")]
        self.assertEqual(leftovers, [])

    def test_the_plugin_copy_ships_the_panel_and_the_security_review(self):
        _repo, _base, _head = self.prepared(GITHUB_ACTION_PATH=str(REPO_ROOT))
        plugin = Path(self.ai_dir, "plugin/skills")
        for rel in (
            "github-code-review/SKILL.md",
            "code-review-panel/SKILL.md",
            "code-review-panel/scripts/panel.py",
            "security-diff-review/SKILL.md",
            "security-diff-review/references/finder.md",
        ):
            self.assertTrue((plugin / rel).is_file(), rel)
