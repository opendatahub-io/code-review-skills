"""Tests for the composite GitHub Action: action.yml, action/review-pr.sh and the skill's tools.

The static tests read action.yml as text (no PyYAML). The script tests run review-pr.sh as a
subprocess against throwaway git repos and fake `uvx`, `podman` and `review.py` executables, so
nothing here needs a network, a container runtime, a token or a real checkout of the plugin.
"""

import json
import os
import re
import shutil
import subprocess
import sys
import tempfile
from pathlib import Path

from tests.helpers import (
    REPO_ROOT,
    PanelTestCase,
    commit,
    git,
    hermetic_env,
    make_repo,
    run,
    write,
)

ACTION_YML = REPO_ROOT / "action.yml"
SCRIPT = REPO_ROOT / "action/review-pr.sh"
SKILL = REPO_ROOT / "skills/github-code-review/SKILL.md"
README = REPO_ROOT / "README.md"
REVIEW_PY_REL = "skills/gitlab-code-review/scripts/review.py"

# The agentic-ci version the flags in review-pr.sh were checked against with `agentic-ci run
# --help` and a smoke run. Bump it only after re-running that check, together with action.yml's
# default and the README row.
VERIFIED_AGENTIC_CI_VERSION = "0.5.15"

FORBIDDEN_TOOLS = re.compile(r"\b(Bash|WebFetch|WebSearch)\b")
# A bare `claude` word: not the `claude-code` tool name, `claude-runner` image or a comment.
CLAUDE_COMMAND = re.compile(r"(?<![\w/:.-])claude(?![\w-])")


def action_steps() -> list[str]:
    """The text of each step in action.yml, from its first key to the next step."""
    text = ACTION_YML.read_text(encoding="utf-8")
    steps = text.split("\n  steps:\n", 1)[1]
    return re.split(r"(?m)^    - (?=\S)", steps)[1:]


def find_step(name: str) -> str:
    for block in action_steps():
        if re.search(rf"(?m)^\s*name:\s*{re.escape(name)}\s*$", block):
            return block
    raise AssertionError(f"no step named {name!r} in action.yml")


def run_text(block: str) -> str | None:
    """The command of a step's run: key, whether inline or a block scalar."""
    lines = block.splitlines()
    for i, line in enumerate(lines):
        m = re.match(r"^(\s*)run:\s*(.*)$", line)
        if not m:
            continue
        if m.group(2) not in ("|", "|-", ">", ">-"):
            return m.group(2)
        indent = len(m.group(1))
        body = []
        for follow in lines[i + 1 :]:
            if follow.strip() and len(follow) - len(follow.lstrip()) <= indent:
                break
            body.append(follow)
        return "\n".join(body)
    return None


def working_directory(block: str) -> str:
    m = re.search(r"(?m)^\s*working-directory:\s*(\S+)\s*$", block)
    assert m, f"no working-directory in {block!r}"
    return m.group(1)


def script_function(name: str) -> str:
    text = SCRIPT.read_text(encoding="utf-8")
    m = re.search(rf"(?ms)^{name}\(\) \{{\n.*?^\}}$", text)
    assert m, f"function {name} not found in review-pr.sh"
    return m.group(0)


def skill_frontmatter_value(key: str) -> str:
    text = SKILL.read_text(encoding="utf-8")
    m = re.search(rf"(?m)^{re.escape(key)}:\s*(.+)$", text)
    assert m, f"{key} not found in the skill's frontmatter"
    return m.group(1).strip()


def script_constant(name: str) -> str:
    m = re.search(rf"(?m)^{name}=(\S+)$", SCRIPT.read_text(encoding="utf-8"))
    assert m, f"constant {name} not found in review-pr.sh"
    return m.group(1)


def run_script_function(name: str, *args: str) -> subprocess.CompletedProcess:
    """Run one function of review-pr.sh with the script's constants, but not its main."""
    constants = "\n".join(
        line
        for line in SCRIPT.read_text(encoding="utf-8").splitlines()
        if re.match(r"^[A-Z_]+=", line)
    )
    source = "\n".join([constants, script_function("error"), script_function(name)])
    return subprocess.run(
        ["bash", "-c", f'{source}\n{name} "$@"', "bash", *args],
        capture_output=True,
        text=True,
        check=False,
    )


def without_comments(text: str) -> str:
    return "\n".join(line for line in text.splitlines() if not line.lstrip().startswith("#"))


def input_default(name: str) -> str:
    text = ACTION_YML.read_text(encoding="utf-8")
    m = re.search(rf"(?ms)^  {name}:\n.*?^    default:\s*'([^']*)'", text)
    assert m, f"no default for input {name}"
    return m.group(1)


class ActionStaticTests(PanelTestCase):
    def test_composite_action(self):
        text = ACTION_YML.read_text(encoding="utf-8")
        self.assertRegex(text, r"(?m)^runs:\n  using: composite$")

    def test_every_run_step_names_a_shell(self):
        runs = 0
        for block in action_steps():
            if run_text(block) is not None:
                runs += 1
                self.assertRegex(block, r"(?m)^\s*shell:\s*\S+", block)
        self.assertGreater(runs, 0)

    def test_no_expression_inside_a_run_block(self):
        for block in action_steps():
            command = run_text(block)
            if command is not None:
                self.assertNotIn("${{", command, block)

    def test_every_uses_is_pinned_to_a_full_sha(self):
        uses = re.findall(r"(?m)^\s*uses:\s*(\S+)", ACTION_YML.read_text(encoding="utf-8"))
        self.assertGreaterEqual(len(uses), 2)
        for ref in uses:
            self.assertRegex(ref, r"@[0-9a-f]{40}$")

    def test_checkout_keeps_no_credentials_and_uses_its_own_directory(self):
        block = find_step("Check out the pull request head")
        self.assertRegex(block, r"(?m)^\s*persist-credentials:\s*false\s*$")
        self.assertRegex(block, r"(?m)^\s*fetch-depth:\s*0\s*$")
        self.assertRegex(block, r"(?m)^\s*path:\s*\.ai-review-head\s*$")
        self.assertRegex(
            block, r"(?m)^\s*ref:\s*\$\{\{ github\.event\.pull_request\.head\.sha \}\}"
        )

    def test_review_step_blanks_the_tokens(self):
        block = find_step("Review the pull request")
        for var in (
            "GITHUB_TOKEN",
            "GH_TOKEN",
            "ACTIONS_ID_TOKEN_REQUEST_TOKEN",
            "ACTIONS_ID_TOKEN_REQUEST_URL",
        ):
            self.assertRegex(block, rf"""(?m)^\s*{var}:\s*(''|"")\s*$""")
        self.assertNotIn("github.token", block)

    def test_only_the_post_step_holds_the_write_token(self):
        for block in action_steps():
            if "github.token" in block:
                self.assertIn('review-pr.sh" post', block)

    def test_post_step_pins_uv_and_the_platform(self):
        self.assertIn('review-pr.sh" post', run_text(find_step("Post the review")))
        post = script_function("cmd_post")
        self.assertIn("UV_NO_CONFIG=1", post)
        self.assertIn("--platform github", post)

    def test_cleanup_step_always_runs(self):
        self.assertRegex(find_step("Remove the checkout"), r"(?m)^\s*if:\s*always\(\)\s*$")

    def test_steps_after_check_honor_skip(self):
        for block in action_steps():
            if "id: check" in block or "name: Remove the checkout" in block:
                continue
            self.assertIn("steps.check.outputs.skip != 'true'", block)

    def test_every_input_is_documented_in_the_readme(self):
        text = ACTION_YML.read_text(encoding="utf-8")
        inputs_text = text.split("\ninputs:\n", 1)[1].split("\nruns:\n", 1)[0]
        names = re.findall(r"(?m)^  ([a-z_]+):\s*$", inputs_text)
        self.assertGreaterEqual(len(names), 6)
        readme = README.read_text(encoding="utf-8")
        for name in names:
            self.assertIn(f"`{name}`", readme)

    def test_claude_code_version_input_is_gone(self):
        text = ACTION_YML.read_text(encoding="utf-8")
        self.assertNotIn("claude_code_version", text)
        self.assertNotIn("claude_code_version", README.read_text(encoding="utf-8"))

    def test_the_api_key_is_not_an_input(self):
        # Credentials are the job environment's agentic-ci keys, not action inputs.
        for doc in (ACTION_YML, README):
            self.assertNotIn("anthropic_api_key", doc.read_text(encoding="utf-8"))
            self.assertNotIn("AI_REVIEW_API_KEY", doc.read_text(encoding="utf-8"))

    def test_agentic_ci_version_default_is_the_verified_one(self):
        self.assertEqual(input_default("agentic_ci_version"), VERIFIED_AGENTIC_CI_VERSION)

    def test_readme_advertises_the_verified_agentic_ci_version(self):
        row = next(
            line
            for line in README.read_text(encoding="utf-8").splitlines()
            if line.startswith("| `agentic_ci_version`")
        )
        self.assertIn(f"`{VERIFIED_AGENTIC_CI_VERSION}`", row)

    def test_codex_is_the_default_harness(self):
        self.assertEqual(input_default("harness"), "codex")
        script = SCRIPT.read_text(encoding="utf-8")
        self.assertEqual(len(re.findall(r"AI_REVIEW_HARNESS:-", script)), 3)
        self.assertEqual(len(re.findall(r"AI_REVIEW_HARNESS:-codex\}", script)), 3)

    def test_the_action_description_names_no_harness_vendor(self):
        text = ACTION_YML.read_text(encoding="utf-8")
        description = text.split("\ndescription:", 1)[1].split("\nauthor:", 1)[0]
        self.assertNotRegex(description, r"(?i)claude|anthropic")

    def test_image_constants_name_both_families(self):
        self.assertEqual(input_default("image"), "")
        for harness in ("claude", "codex"):
            for family in ("runner", "sandbox"):
                with self.subTest(harness=harness, family=family):
                    self.assertEqual(
                        script_constant(f"{harness.upper()}_{family.upper()}_IMAGE"),
                        f'"quay.io/aipcc/agentic-ci/{harness}-{family}:latest"',
                    )

    def test_default_image_picks_the_family_of_the_backend(self):
        cases = {
            ("codex", "podman"): "quay.io/aipcc/agentic-ci/codex-runner:latest",
            ("claude-code", "podman"): "quay.io/aipcc/agentic-ci/claude-runner:latest",
            ("codex", "openshell"): "quay.io/aipcc/agentic-ci/codex-sandbox:latest",
            ("claude-code", "openshell"): "quay.io/aipcc/agentic-ci/claude-sandbox:latest",
        }
        for (harness, backend), image in cases.items():
            with self.subTest(harness=harness, backend=backend):
                proc = run_script_function("default_image", harness, backend)
                self.assertEqual(proc.returncode, 0, proc.stdout + proc.stderr)
                self.assertEqual(proc.stdout, image + "\n")
        for args in (("opencode", "podman"), ("codex", "docker"), ("", "")):
            with self.subTest(args=args):
                proc = run_script_function("default_image", *args)
                self.assertNotEqual(proc.returncode, 0)
                self.assertEqual(proc.stdout, "")

    def test_the_review_step_uses_the_podman_family(self):
        self.assertIn('default_image "$harness" podman', script_function("cmd_review"))

    def test_no_node_or_claude_invocation(self):
        action = ACTION_YML.read_text(encoding="utf-8")
        for word in ("npm", "setup-node", "node-version"):
            self.assertNotIn(word, action)
        for name, text in (
            ("action.yml", without_comments(action)),
            ("review-pr.sh", without_comments(SCRIPT.read_text(encoding="utf-8"))),
        ):
            self.assertIsNone(CLAUDE_COMMAND.search(text), name)

    def test_setup_uv_does_not_cache_the_pull_requests_tree(self):
        block = find_step("Set up uv")
        self.assertRegex(block, r"(?m)^\s*enable-cache:\s*false\s*$")

    def test_setup_uv_reads_its_version_from_the_action_not_the_workspace(self):
        block = find_step("Set up uv")
        self.assertRegex(block, r"(?m)^\s*working-directory:\s*\$\{\{ github\.action_path \}\}\s*$")

    def test_readme_example_job_sets_a_timeout(self):
        self.assertRegex(README.read_text(encoding="utf-8"), r"(?m)^    timeout-minutes:\s*\d+\s*$")

    def test_skill_tools_cannot_run_commands_or_reach_the_web(self):
        self.assertIsNone(FORBIDDEN_TOOLS.search(skill_frontmatter_value("allowed-tools")))

    def test_skill_cannot_write(self):
        tools = skill_frontmatter_value("allowed-tools").split()
        self.assertEqual(tools, ["Read", "Grep", "Glob"])

    def test_poster_path_exists_and_is_executable(self):
        self.assertIn(REVIEW_PY_REL, SCRIPT.read_text(encoding="utf-8"))
        poster = REPO_ROOT / REVIEW_PY_REL
        self.assertTrue(poster.is_file())
        self.assertTrue(os.access(poster, os.X_OK))

    def test_script_is_executable(self):
        self.assertTrue(os.access(SCRIPT, os.X_OK))

    def test_checkout_path_matches_working_directories_and_cleanup(self):
        m = re.search(r"(?m)^\s*path:\s*(\S+)\s*$", find_step("Check out the pull request head"))
        path = m.group(1)
        for name in ("Prepare the diff", "Post the review"):
            self.assertEqual(working_directory(find_step(name)), path, name)
        text = SCRIPT.read_text(encoding="utf-8")
        self.assertIn(f'HEAD_DIR="${{GITHUB_WORKSPACE:-}}/{path}"', text)
        self.assertIn('AI_DIR="${GITHUB_WORKSPACE:-}/.ai-review"', text)
        cleanup = script_function("cmd_cleanup")
        self.assertIn('"$AI_DIR" "$HEAD_DIR"', cleanup)
        self.assertIn('rm -rf -- "$dir"', cleanup)

    def test_checkout_is_not_under_the_mounted_workdir(self):
        m = re.search(r"(?m)^\s*path:\s*(\S+)\s*$", find_step("Check out the pull request head"))
        checkout = m.group(1)
        workdir = ".ai-review"
        self.assertNotEqual(checkout, workdir)
        self.assertFalse(checkout.startswith(workdir + "/"), checkout)

    def test_review_step_runs_in_the_workdir_not_the_checkout(self):
        m = re.search(r"(?m)^\s*path:\s*(\S+)\s*$", find_step("Check out the pull request head"))
        review = working_directory(find_step("Review the pull request"))
        self.assertEqual(review, ".ai-review")
        self.assertNotEqual(review, m.group(1))

    def test_the_checkout_state_guards_are_gone(self):
        action = ACTION_YML.read_text(encoding="utf-8")
        script = SCRIPT.read_text(encoding="utf-8")
        for word in ("git_config_sha256", "git_status_sha256", "AI_REVIEW_GIT_"):
            self.assertNotIn(word, action)
            self.assertNotIn(word, script)
        self.assertNotIn("checkout_state_sha", script)
        for doc in ("README.md", "AGENTS.md"):
            self.assertNotIn("checkout changed", (REPO_ROOT / doc).read_text(encoding="utf-8"))

    def test_agent_runs_under_an_interrupting_timeout_with_a_margin(self):
        review = script_function("cmd_review")
        self.assertIn('timeout -s INT --kill-after=60s "${minutes}m"', review)
        self.assertIn('--timeout "$(( (minutes + 5) * 60 ))"', review)

    def test_image_is_pulled_in_its_own_bounded_step(self):
        review = script_function("cmd_review")
        pull = review.index('timeout 15m podman pull "$image"')
        run_agent = review.index("timeout -s INT")
        self.assertLess(pull, run_agent)

    def test_effort_comes_from_one_constant(self):
        script = SCRIPT.read_text(encoding="utf-8")
        self.assertEqual(len(re.findall(r"(?m)^AGENT_EFFORT=", script)), 1)
        self.assertIn('--effort "$AGENT_EFFORT"', script_function("cmd_review"))
        self.assertIn('AGENT_REASONING_EFFORT="$AGENT_EFFORT"', script_function("cmd_post"))

    def test_inputs_reach_the_steps_that_use_them(self):
        wiring = (
            ("Post the review", "CHILL_MODE", "inputs.chill_mode"),
            ("Post the review", "INLINE_FIXES", "inputs.inline_fixes"),
            ("Post the review", "AGENT_MODEL", "inputs.model"),
            ("Post the review", "AI_REVIEW_HARNESS", "inputs.harness"),
            ("Check the event and credentials", "AI_REVIEW_HARNESS", "inputs.harness"),
            ("Check the event and credentials", "AI_REVIEW_MODE", "inputs.mode"),
            (
                "Check the event and credentials",
                "AI_REVIEW_DRAFT",
                "github.event.pull_request.draft",
            ),
            ("Prepare the diff", "AI_REVIEW_MODE", "inputs.mode"),
            ("Review the pull request", "AI_REVIEW_MODE", "inputs.mode"),
            ("Review the pull request", "AI_REVIEW_MODEL", "inputs.model"),
            ("Review the pull request", "AI_REVIEW_TIMEOUT_MINUTES", "inputs.timeout_minutes"),
            ("Review the pull request", "AI_REVIEW_HARNESS", "inputs.harness"),
            (
                "Review the pull request",
                "AI_REVIEW_AGENTIC_CI_VERSION",
                "inputs.agentic_ci_version",
            ),
            ("Review the pull request", "AI_REVIEW_IMAGE", "inputs.image"),
            ("Review the pull request", "AI_REVIEW_INSTRUCTIONS", "inputs.review_instructions"),
            ("Prepare the diff", "AI_REVIEW_INSTRUCTIONS", "inputs.review_instructions"),
        )
        for step, var, expr in wiring:
            with self.subTest(var=var, step=step):
                self.assertRegex(
                    find_step(step), rf"(?m)^\s*{var}:\s*\$\{{\{{ {re.escape(expr)} \}}\}}\s*$"
                )


def script_env(**overrides: str) -> dict[str, str]:
    """hermetic_env without anything the script reads, plus overrides."""
    env = hermetic_env()
    for key in list(env):
        if key.startswith(
            ("ANTHROPIC_", "CLAUDE_CODE_", "AI_REVIEW_", "AGENT_", "OPENAI_", "GCLOUD_", "GCP_")
        ) or key in (
            "GH_TOKEN",
            "UV_NO_CONFIG",
            "UV_PYTHON",
        ):
            del env[key]
    env.update(overrides)
    return env


def write_executable(path: Path, body: str) -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(f"#!{sys.executable}\n{body}", encoding="utf-8")
    path.chmod(0o755)
    return path


# Stands in for podman, so no test touches a real container: records each call.
FAKE_PODMAN = """
import json
import os
import sys

with open(os.environ["FAKE_PODMAN_LOG"], "a", encoding="utf-8") as f:
    f.write(" ".join(sys.argv[1:]) + "\\n")
with open(os.environ["FAKE_PODMAN_LOG"] + ".tmpdir", "a", encoding="utf-8") as f:
    f.write(os.environ.get("TMPDIR", "") + "\\n")
# FAKE_PODMAN_CONTAINERS maps container IDs to their mount sources, for ps and inspect.
containers = json.loads(os.environ.get("FAKE_PODMAN_CONTAINERS", "{}"))
if sys.argv[1:2] == ["ps"]:
    print("\\n".join(containers))
elif sys.argv[1:2] == ["inspect"]:
    print("\\n".join(containers.get(sys.argv[-1], [])))
elif sys.argv[1:3] == ["rm", "-f"]:
    sys.exit(int(os.environ.get("FAKE_PODMAN_RM_STATUS", "0")))
sys.exit(int(os.environ.get("FAKE_PODMAN_STATUS", "0")))
"""


class ScriptTestCase(PanelTestCase):
    def setUp(self):
        self.tmp = self.make_tmpdir()
        self.output = os.path.join(self.tmp, "github_output")
        self.workspace = os.path.join(self.tmp, "workspace")
        self.ai_dir = os.path.join(self.workspace, ".ai-review")
        self.head_dir = os.path.join(self.workspace, ".ai-review-head")
        self.repo_dir = os.path.join(self.ai_dir, "repo")
        self.runner_temp = os.path.join(self.tmp, "runner_temp")
        os.makedirs(self.runner_temp)
        self.out_dir = os.path.join(self.ai_dir, "out")
        self.out = os.path.join(self.out_dir, "ai-review-output.json")
        self.bin = os.path.join(self.tmp, "bin")
        self.podman_log = os.path.join(self.tmp, "podman.log")
        # Always shadow podman: the script pulls the image, and a real pull would hit the network.
        write_executable(Path(self.bin, "podman"), FAKE_PODMAN)

    def env_path(self) -> str:
        return f"{self.bin}{os.pathsep}{os.environ['PATH']}"

    def run_script(self, command: str, cwd: str, **env: str):
        defaults = dict(
            GITHUB_OUTPUT=self.output,
            GITHUB_WORKSPACE=self.workspace,
            PATH=self.env_path(),
            FAKE_PODMAN_LOG=self.podman_log,
            RUNNER_TEMP=self.runner_temp,
        )
        full = script_env(**{**defaults, **env})
        return run(cwd, SCRIPT, command, env=full, check=False, text=True)

    def github_output(self) -> dict[str, str]:
        if not os.path.exists(self.output):
            return {}
        lines = Path(self.output).read_text(encoding="utf-8").splitlines()
        return dict(line.split("=", 1) for line in lines)

    def podman_calls(self) -> list[str]:
        if not os.path.exists(self.podman_log):
            return []
        return Path(self.podman_log).read_text(encoding="utf-8").splitlines()

    def move_repo_into_place(self, repo: str) -> str:
        """Move a throwaway repo to where the action's checkout step puts it."""
        os.makedirs(self.workspace, exist_ok=True)
        os.rename(repo, self.head_dir)
        return self.head_dir


class CheckTestCase(ScriptTestCase):
    """Runs the check step with a same-repo pull request event that has a key."""

    BASE = dict(
        AI_REVIEW_EVENT="pull_request",
        GITHUB_REPOSITORY="o/r",
        AI_REVIEW_HEAD_REPO="o/r",
        AI_REVIEW_ACTOR="alice",
        AI_REVIEW_TRIGGERING_ACTOR="alice",
        OPENAI_API_KEY="sk-job",
    )

    def check(self, **overrides: str):
        env = {**self.BASE, **overrides}
        return self.run_script("check", self.tmp, **env)


class CheckTests(CheckTestCase):
    def test_same_repo_run_with_a_key_proceeds(self):
        proc = self.check()
        self.assertEqual(proc.returncode, 0, proc.stdout + proc.stderr)
        self.assertEqual(self.github_output(), {"skip": "false"})

    def test_skips_exit_zero_and_say_why(self):
        cases = {
            "fork": dict(AI_REVIEW_HEAD_REPO="fork/r"),
            "deleted fork": dict(AI_REVIEW_HEAD_REPO=""),
            "dependabot actor": dict(AI_REVIEW_ACTOR="dependabot[bot]"),
            "dependabot triggering actor": dict(AI_REVIEW_TRIGGERING_ACTOR="dependabot[bot]"),
            "no key": dict(OPENAI_API_KEY=""),
            "codex without an openai key": dict(AI_REVIEW_HARNESS="codex", OPENAI_API_KEY=""),
            "claude-code without a key": dict(AI_REVIEW_HARNESS="claude-code"),
            "vertex off": dict(AI_REVIEW_HARNESS="claude-code", CLAUDE_CODE_USE_VERTEX="0"),
        }
        for name, overrides in cases.items():
            with self.subTest(name):
                Path(self.output).unlink(missing_ok=True)
                proc = self.check(**overrides)
                self.assertEqual(proc.returncode, 0, proc.stdout + proc.stderr)
                self.assertEqual(self.github_output(), {"skip": "true"})
                self.assertIn("::notice::", proc.stdout)

    def test_maintainer_on_a_dependabot_branch_still_gets_a_review(self):
        proc = self.check(AI_REVIEW_ACTOR="maintainer", AI_REVIEW_TRIGGERING_ACTOR="maintainer")
        self.assertEqual(proc.returncode, 0, proc.stdout + proc.stderr)
        self.assertEqual(self.github_output(), {"skip": "false"})

    def test_vertex_proceeds_without_a_key(self):
        # The flag covers application default credentials, the others are
        # the credential variables agentic-ci reads.
        for var in (
            "CLAUDE_CODE_USE_VERTEX",
            "CLAUDE_CODE_OAUTH_TOKEN",
            "GCLOUD_CREDENTIALS",
            "GCP_SERVICE_ACCOUNT_KEY",
        ):
            with self.subTest(var):
                Path(self.output).unlink(missing_ok=True)
                proc = self.check(AI_REVIEW_HARNESS="claude-code", OPENAI_API_KEY="", **{var: "1"})
                self.assertEqual(proc.returncode, 0, proc.stdout + proc.stderr)
                self.assertEqual(self.github_output(), {"skip": "false"})

    def test_bedrock_does_not_count_because_agentic_ci_has_no_bedrock_path(self):
        proc = self.check(AI_REVIEW_HARNESS="claude-code", CLAUDE_CODE_USE_BEDROCK="1")
        self.assertEqual(proc.returncode, 0, proc.stdout + proc.stderr)
        self.assertEqual(self.github_output(), {"skip": "true"})
        self.assertNotIn("BEDROCK", proc.stdout)

    def test_skip_message_names_the_credential_variables(self):
        proc = self.check(AI_REVIEW_HARNESS="claude-code")
        for name in (
            "ANTHROPIC_API_KEY",
            "CLAUDE_CODE_OAUTH_TOKEN",
            "CLAUDE_CODE_USE_VERTEX",
            "GCLOUD_CREDENTIALS",
            "GCP_SERVICE_ACCOUNT_KEY",
        ):
            self.assertIn(name, proc.stdout)
        proc = self.check(AI_REVIEW_HARNESS="codex", OPENAI_API_KEY="")
        self.assertIn("OPENAI_API_KEY", proc.stdout)

    def test_an_empty_harness_is_codex(self):
        for harness in ({}, {"AI_REVIEW_HARNESS": ""}):
            with self.subTest(harness=harness):
                Path(self.output).unlink(missing_ok=True)
                proc = self.check(OPENAI_API_KEY="", ANTHROPIC_API_KEY="sk-a", **harness)
                self.assertEqual(proc.returncode, 0, proc.stdout + proc.stderr)
                self.assertEqual(self.github_output(), {"skip": "true"})
                self.assertIn(
                    "OPENAI_API_KEY is not set, which the codex harness needs", proc.stdout
                )
                Path(self.output).unlink()
                proc = self.check(**harness)
                self.assertEqual(self.github_output(), {"skip": "false"})

    def test_codex_proceeds_with_an_openai_key(self):
        proc = self.check(AI_REVIEW_HARNESS="codex", OPENAI_API_KEY="sk-oai")
        self.assertEqual(proc.returncode, 0, proc.stdout + proc.stderr)
        self.assertEqual(self.github_output(), {"skip": "false"})

    def test_vertex_flag_set_to_false_does_not_count(self):
        for value in ("false", "False", "FALSE", ""):
            with self.subTest(value=value):
                Path(self.output).unlink(missing_ok=True)
                proc = self.check(AI_REVIEW_HARNESS="claude-code", CLAUDE_CODE_USE_VERTEX=value)
                self.assertEqual(proc.returncode, 0, proc.stdout + proc.stderr)
                self.assertEqual(self.github_output(), {"skip": "true"})

    def test_other_events_fail(self):
        for event in ("pull_request_target", "push", "workflow_dispatch", ""):
            with self.subTest(event):
                Path(self.output).unlink(missing_ok=True)
                proc = self.check(AI_REVIEW_EVENT=event)
                self.assertEqual(proc.returncode, 1, proc.stdout)
                self.assertIn("::error::", proc.stdout)
                self.assertEqual(self.github_output(), {})

    def test_an_unknown_harness_is_not_skipped_here(self):
        # The check step does not validate the harness: the review step rejects it loudly.
        proc = self.check(AI_REVIEW_HARNESS="bogus")
        self.assertEqual(proc.returncode, 0, proc.stdout + proc.stderr)
        self.assertEqual(self.github_output(), {"skip": "false"})


class PrepareTestCase(ScriptTestCase):
    """Runs the prepare step against a throwaway PR checkout and a stand-in action tree."""

    def setUp(self):
        super().setUp()
        # A stand-in for the action's own tree, which prepare copies into the workdir.
        self.action_path = os.path.join(self.tmp, "action")
        write(self.action_path, "skills/github-code-review/SKILL.md", "skill\n")
        write(self.action_path, ".claude-plugin/plugin.json", "{}\n")
        write(self.action_path, ".git/HEAD", "ref: refs/heads/main\n")
        write(self.action_path, ".ai-review/repo/inner.txt", "x\n")
        write(self.action_path, ".ai-review-head/inner.txt", "x\n")
        write(self.action_path, "gha-creds-x.json", "{}\n")
        write(self.action_path, "action/review-pr.sh", "#!/bin/sh\n")

    def make_pr(self) -> tuple[str, str, str]:
        """A checkout at the PR head, with the base branch moved on since the fork point."""
        repo, _fork = make_repo(self.tmp, {"a.txt": "one\n"}, branch="feature")
        head = commit(repo, {"feature.txt": "feature\n"})
        git(repo, "checkout", "-q", "main")
        base = commit(repo, {"main_only.txt": "main\n"})
        git(repo, "checkout", "-q", "--detach", head)
        return self.move_repo_into_place(repo), base, head

    def prepare(self, repo: str, base: str, head: str, **env: str):
        env.setdefault("GITHUB_ACTION_PATH", self.action_path)
        return self.run_script(
            "prepare", repo, AI_REVIEW_BASE_SHA=base, AI_REVIEW_HEAD_SHA=head, **env
        )


class PrepareTests(PrepareTestCase):
    def test_diff_starts_at_the_merge_base_not_the_base_tip(self):
        repo, base, head = self.make_pr()
        proc = self.prepare(repo, base, head)
        self.assertEqual(proc.returncode, 0, proc.stdout + proc.stderr)
        diff = Path(self.ai_dir, "pr.diff").read_text(encoding="utf-8")
        self.assertIn("+++ b/feature.txt", diff)
        self.assertIn("+feature", diff)
        self.assertNotIn("main_only.txt", diff)

    def test_workdir_layout(self):
        repo, base, head = self.make_pr()
        proc = self.prepare(repo, base, head)
        self.assertEqual(proc.returncode, 0, proc.stdout + proc.stderr)
        self.assertTrue(os.path.isdir(self.out_dir))
        self.assertEqual(os.listdir(self.out_dir), [])
        self.assertTrue(Path(self.ai_dir, "plugin/skills/github-code-review/SKILL.md").is_file())
        self.assertTrue(Path(self.ai_dir, "plugin/.claude-plugin/plugin.json").is_file())
        self.assertTrue(Path(self.repo_dir, "feature.txt").is_file())
        self.assertTrue(Path(self.repo_dir, "a.txt").is_file())

    def test_exported_repo_gets_a_fresh_git_at_the_head_and_keeps_the_files(self):
        # Not the checkout's .git: the container gets one built for it, at the PR head.
        repo, base, head = self.make_pr()
        proc = self.prepare(repo, base, head)
        self.assertEqual(proc.returncode, 0, proc.stdout + proc.stderr)
        self.assertTrue(Path(self.repo_dir, ".git").is_dir())
        self.assertFalse(Path(self.repo_dir, ".git").is_symlink())
        self.assertEqual(git(self.repo_dir, "rev-parse", "HEAD").stdout.decode().strip(), head)
        self.assertNotEqual(
            os.stat(Path(self.repo_dir, ".git")).st_ino, os.stat(Path(repo, ".git")).st_ino
        )
        self.assertEqual(Path(self.repo_dir, "a.txt").read_text(encoding="utf-8"), "one\n")

    def test_export_keeps_files_the_pr_marks_export_ignore(self):
        repo, base, head = self.make_pr()
        head = commit(repo, {".gitattributes": "secret.txt export-ignore\n", "secret.txt": "s\n"})
        proc = self.prepare(repo, base, head)
        self.assertEqual(proc.returncode, 0, proc.stdout + proc.stderr)
        self.assertEqual(Path(self.repo_dir, "secret.txt").read_text(encoding="utf-8"), "s\n")
        self.assertTrue(Path(self.repo_dir, ".gitattributes").is_file())

    def test_diff_shows_files_the_pr_marks_binary(self):
        repo, base, head = self.make_pr()
        head = commit(repo, {".gitattributes": "* -diff\n", "hidden.py": "evil()\n"})
        proc = self.prepare(repo, base, head)
        self.assertEqual(proc.returncode, 0, proc.stdout + proc.stderr)
        diff = Path(self.ai_dir, "pr.diff").read_text(encoding="utf-8")
        self.assertIn("+evil()", diff)
        self.assertNotIn("Binary files", diff)

    def test_diff_format_ignores_the_runner_git_config(self):
        repo, base, head = self.make_pr()
        git(repo, "config", "diff.noprefix", "true")
        git(repo, "config", "color.ui", "always")
        proc = self.prepare(repo, base, head)
        self.assertEqual(proc.returncode, 0, proc.stdout + proc.stderr)
        diff = Path(self.ai_dir, "pr.diff").read_text(encoding="utf-8")
        self.assertIn("+++ b/feature.txt", diff)
        self.assertNotIn("\x1b[", diff)

    def test_stale_export_is_replaced(self):
        repo, base, head = self.make_pr()
        write(self.repo_dir, "stale.txt", "old\n")
        proc = self.prepare(repo, base, head)
        self.assertEqual(proc.returncode, 0, proc.stdout + proc.stderr)
        self.assertFalse(Path(self.repo_dir, "stale.txt").exists())
        self.assertTrue(Path(self.repo_dir, "feature.txt").is_file())

    def test_plugin_copy_holds_only_the_plugin_and_the_skills(self):
        repo, base, head = self.make_pr()
        proc = self.prepare(repo, base, head)
        self.assertEqual(proc.returncode, 0, proc.stdout + proc.stderr)
        plugin = Path(self.ai_dir, "plugin")
        self.assertTrue((plugin / "skills/github-code-review/SKILL.md").is_file())
        self.assertTrue((plugin / ".claude-plugin/plugin.json").is_file())
        self.assertEqual(sorted(p.name for p in plugin.iterdir()), [".claude-plugin", "skills"])
        for stray in ("gha-creds-x.json", ".git", "action", ".ai-review", ".ai-review-head"):
            self.assertFalse((plugin / stray).exists(), stray)

    def test_a_tree_without_the_plugin_or_the_skills_fails_before_touching_the_workdir(self):
        for missing in (".claude-plugin", "skills"):
            with self.subTest(missing):
                repo, base, head = self.make_pr()
                shutil.rmtree(os.path.join(self.action_path, missing))
                proc = self.prepare(repo, base, head)
                self.assertEqual(proc.returncode, 1, proc.stdout + proc.stderr)
                self.assertIn(f"{missing} is missing", proc.stdout)
                self.assertFalse(Path(self.ai_dir, "pr.diff").exists())
                shutil.rmtree(self.workspace, ignore_errors=True)
                write(self.action_path, "skills/github-code-review/SKILL.md", "skill\n")
                write(self.action_path, ".claude-plugin/plugin.json", "{}\n")

    def test_prepare_leaves_the_checkout_clean(self):
        repo, base, head = self.make_pr()
        self.prepare(repo, base, head)
        status = run(repo, "git", "status", "--porcelain", "--ignored", text=True).stdout
        self.assertEqual(status, "")

    def test_head_mismatch_fails(self):
        repo, base, _head = self.make_pr()
        proc = self.prepare(repo, base, base)
        self.assertNotEqual(proc.returncode, 0)
        self.assertIn("::error::", proc.stdout)
        self.assertFalse(Path(self.ai_dir, "pr.diff").exists())

    def test_bad_sha_fails(self):
        repo, base, _head = self.make_pr()
        proc = self.prepare(repo, base, "HEAD")
        self.assertNotEqual(proc.returncode, 0)

    def test_stale_output_is_removed(self):
        repo, base, head = self.make_pr()
        write(self.out_dir, "ai-review-output.json", "{}")
        proc = self.prepare(repo, base, head)
        self.assertEqual(proc.returncode, 0, proc.stdout + proc.stderr)
        self.assertFalse(os.path.exists(self.out))
        self.assertEqual(os.listdir(self.out_dir), [])

    def test_instructions_land_verbatim(self):
        repo, base, head = self.make_pr()
        text = 'Focus on:\n\n- "quotes", $HOME and `ticks`\n- trailing newline kept\n'
        proc = self.prepare(repo, base, head, AI_REVIEW_INSTRUCTIONS=text)
        self.assertEqual(proc.returncode, 0, proc.stdout + proc.stderr)
        self.assertEqual(Path(self.ai_dir, "instructions.md").read_text(encoding="utf-8"), text)

    def test_no_instructions_means_no_file(self):
        repo, base, head = self.make_pr()
        write(self.ai_dir, "instructions.md", "stale")
        self.prepare(repo, base, head, AI_REVIEW_INSTRUCTIONS="")
        self.assertFalse(Path(self.ai_dir, "instructions.md").exists())

    def test_whitespace_in_the_workspace_path_fails(self):
        repo, base, head = self.make_pr()
        self.workspace = os.path.join(self.tmp, "work space")
        proc = self.prepare(repo, base, head)
        self.assertNotEqual(proc.returncode, 0)
        self.assertIn("whitespace", proc.stdout)

    def test_no_merge_base_fails_with_a_clear_error(self):
        repo, _base, head = self.make_pr()
        git(repo, "checkout", "-q", "--orphan", "unrelated")
        orphan = commit(repo, {"o.txt": "o\n"})
        git(repo, "checkout", "-q", "--detach", head)
        proc = self.prepare(repo, orphan, head)
        self.assertEqual(proc.returncode, 1, proc.stdout + proc.stderr)
        self.assertIn("No merge base", proc.stdout)
        self.assertFalse(Path(self.ai_dir, "pr.diff").exists())

    def test_missing_or_relative_workspace_fails(self):
        repo, base, head = self.make_pr()
        for value in ("", "relative/ws"):
            with self.subTest(value):
                self.workspace = value
                proc = self.prepare(repo, base, head)
                self.assertEqual(proc.returncode, 1, proc.stdout + proc.stderr)
                self.assertIn("::error::", proc.stdout)

    def test_missing_action_path_fails(self):
        repo, base, head = self.make_pr()
        proc = self.prepare(repo, base, head, GITHUB_ACTION_PATH="")
        self.assertEqual(proc.returncode, 1, proc.stdout + proc.stderr)
        self.assertIn("GITHUB_ACTION_PATH is not set", proc.stdout)


# Stands in for uvx: records what the script passed and, when asked, writes the review output
# where agentic-ci would leave it, under the workdir's out/.
FAKE_UVX = """
import json
import os
import sys

env_keys = [
    "ANTHROPIC_API_KEY", "OPENAI_API_KEY", "GITHUB_TOKEN", "GH_TOKEN",
    "ACTIONS_ID_TOKEN_REQUEST_TOKEN", "ACTIONS_ID_TOKEN_REQUEST_URL", "UV_NO_CONFIG", "TMPDIR",
]
args = sys.argv[1:]
record = {
    "argv": args,
    "env": {k: os.environ.get(k) for k in env_keys},
    "cwd": os.getcwd(),
}
with open(os.environ["FAKE_LOG"], "w", encoding="utf-8") as f:
    json.dump(record, f)
# What agentic-ci leaves in its temp directory: a resolved credential file.
if os.environ.get("TMPDIR"):
    with open(os.path.join(os.environ["TMPDIR"], "credentials.json"), "w", encoding="utf-8") as f:
        f.write("{}")
if os.environ.get("FAKE_WRITE") == "1":
    workdir = args[args.index("--workdir") + 1]
    with open(os.path.join(workdir, "out", "ai-review-output.json"), "w", encoding="utf-8") as f:
        f.write("{}")
"""


class ReviewTestCase(ScriptTestCase):
    """Runs the review step against a prepared workdir with fake uvx and podman."""

    BASE = "a" * 40
    HEAD = "b" * 40
    IMAGE = "quay.io/aipcc/agentic-ci/codex-runner:latest"
    VERSION = VERIFIED_AGENTIC_CI_VERSION

    def setUp(self):
        super().setUp()
        self.log = os.path.join(self.tmp, "uvx.json")
        write(self.repo_dir, "src/a.py", "x = 1\n")
        write(self.ai_dir, "pr.diff", "diff\n")
        write(self.ai_dir, "plugin/skills/github-code-review/SKILL.md", "skill\n")
        os.makedirs(self.out_dir)
        write_executable(Path(self.bin, "uvx"), FAKE_UVX)

    def review(self, write_output: bool = True, **env: str):
        defaults = dict(
            FAKE_LOG=self.log,
            FAKE_WRITE="1" if write_output else "0",
            AI_REVIEW_BASE_SHA=self.BASE,
            AI_REVIEW_HEAD_SHA=self.HEAD,
            # The job environment carries the credential, as in a real run.
            OPENAI_API_KEY="sk-test",
            AI_REVIEW_IMAGE=self.IMAGE,
            AI_REVIEW_AGENTIC_CI_VERSION=self.VERSION,
            GITHUB_TOKEN="ghs_secret",
            GH_TOKEN="gh_secret",
            ACTIONS_ID_TOKEN_REQUEST_TOKEN="oidc-token",
            ACTIONS_ID_TOKEN_REQUEST_URL="https://oidc.example",
        )
        defaults.update(env)
        return self.run_script("review", self.ai_dir, **defaults)

    def record(self) -> dict:
        return json.loads(Path(self.log).read_text(encoding="utf-8"))

    def expected_argv(
        self,
        instructions: bool = False,
        model: str | None = None,
        minutes: int = 20,
        harness: str = "codex",
        prompt: str | None = None,
    ) -> list[str]:
        if prompt is not None:
            tail = ["--", "--plugin-dir", "/workspace/plugin"] if harness == "claude-code" else []
        elif harness == "claude-code":
            prompt = (
                "/code-review-skills:github-code-review /workspace/repo /workspace/pr.diff "
                f"{self.BASE} {self.HEAD} /workspace/out/ai-review-output.json"
            )
            tail = ["--", "--plugin-dir", "/workspace/plugin"]
        else:
            prompt = (
                "Invoke the /code-review-skills:github-code-review skill with the arguments: "
                f"/workspace/repo /workspace/pr.diff {self.BASE} {self.HEAD} "
                "/workspace/out/ai-review-output.json"
            )
            tail = []
        if instructions:
            prompt += " /workspace/instructions.md"
        argv = [
            "--from",
            f"agentic-ci=={self.VERSION}",
            "agentic-ci",
            "run",
            "--harness",
            harness,
            "--backend",
            "podman",
            "--workdir",
            self.ai_dir,
            "--image",
            self.IMAGE,
            "--no-otel",
            "--timeout",
            str((minutes + 5) * 60),
            "--effort",
            script_constant("AGENT_EFFORT"),
        ]
        if model:
            argv += ["--model", model]
        return argv + [prompt] + tail

    def fake_timeout(self, status: int) -> None:
        """Put a `timeout` on PATH that records the agent run's arguments and exits with `status`.

        A `timeout ... podman pull` call runs the fake podman instead.
        """
        body = (
            "import os, sys\n"
            "args = sys.argv[1:]\n"
            'if args[1] == "podman":\n'
            '    os.execvp("podman", args[1:])\n'
            'open(os.environ["FAKE_TIMEOUT_LOG"], "w").write(" ".join(args[:4]))\n'
            f"sys.exit({status})\n"
        )
        write_executable(Path(self.bin, "timeout"), body)


class ReviewTests(ReviewTestCase):
    def test_agent_runs_under_the_timeout(self):
        log = os.path.join(self.tmp, "timeout.log")
        self.fake_timeout(124)
        proc = self.review(FAKE_TIMEOUT_LOG=log, AI_REVIEW_TIMEOUT_MINUTES="7")
        self.assertEqual(Path(log).read_text(encoding="utf-8"), "-s INT --kill-after=60s 7m")
        self.assertEqual(proc.returncode, 1, proc.stdout + proc.stderr)
        self.assertIn("did not finish within 7 minutes", proc.stdout)

    def test_a_killed_agent_is_not_reported_as_a_plain_timeout(self):
        self.fake_timeout(137)
        proc = self.review(
            FAKE_TIMEOUT_LOG=os.path.join(self.tmp, "timeout.log"), AI_REVIEW_TIMEOUT_MINUTES="7"
        )
        self.assertEqual(proc.returncode, 1, proc.stdout + proc.stderr)
        self.assertIn("The review was killed (exit 137)", proc.stdout)
        self.assertIn("7 minute limit or the container ran out of memory", proc.stdout)
        self.assertNotIn("did not finish", proc.stdout)

    def test_agent_failure_status_passes_through_the_timeout(self):
        self.fake_timeout(3)
        proc = self.review(
            FAKE_TIMEOUT_LOG=os.path.join(self.tmp, "timeout.log"), AI_REVIEW_TIMEOUT_MINUTES="7"
        )
        self.assertEqual(proc.returncode, 3, proc.stdout + proc.stderr)

    def test_timeout_must_be_a_positive_integer(self):
        for value in ("0", "-1", "1.5", "soon", "5m"):
            with self.subTest(value):
                proc = self.review(AI_REVIEW_TIMEOUT_MINUTES=value)
                self.assertEqual(proc.returncode, 1, proc.stdout + proc.stderr)
                self.assertIn("timeout_minutes must be a positive integer", proc.stdout)
                self.assertFalse(os.path.exists(self.log))

    def test_container_timeout_follows_the_minutes(self):
        proc = self.review(AI_REVIEW_TIMEOUT_MINUTES="7")
        self.assertEqual(proc.returncode, 0, proc.stdout + proc.stderr)
        self.assertEqual(self.record()["argv"], self.expected_argv(minutes=7))

    def test_exact_argv(self):
        proc = self.review()
        self.assertEqual(proc.returncode, 0, proc.stdout + proc.stderr)
        self.assertEqual(self.record()["argv"], self.expected_argv())

    def test_instructions_file_is_the_last_prompt_word(self):
        write(self.ai_dir, "instructions.md", "Focus on tests\n")
        proc = self.review()
        self.assertEqual(proc.returncode, 0, proc.stdout + proc.stderr)
        self.assertEqual(self.record()["argv"], self.expected_argv(instructions=True))

    def test_model_is_passed_when_set(self):
        self.review(AI_REVIEW_MODEL="some-model")
        self.assertEqual(self.record()["argv"], self.expected_argv(model="some-model"))

    def test_no_model_flag_when_unset(self):
        self.review(AI_REVIEW_MODEL="")
        self.assertNotIn("--model", self.record()["argv"])

    def test_agent_sees_the_key_but_no_write_token(self):
        self.review()
        env = self.record()["env"]
        self.assertEqual(env["OPENAI_API_KEY"], "sk-test")
        for var in (
            "GITHUB_TOKEN",
            "GH_TOKEN",
            "ACTIONS_ID_TOKEN_REQUEST_TOKEN",
            "ACTIONS_ID_TOKEN_REQUEST_URL",
        ):
            self.assertEqual(env[var], "", var)

    def test_the_key_is_not_on_the_command_line(self):
        self.review()
        self.assertNotIn("sk-test", " ".join(self.record()["argv"]))

    def test_uvx_runs_in_the_workdir_with_no_uv_config(self):
        self.review()
        record = self.record()
        self.assertEqual(record["cwd"], self.ai_dir)
        self.assertEqual(record["env"]["UV_NO_CONFIG"], "1")

    def test_fails_when_the_agent_writes_nothing(self):
        proc = self.review(write_output=False)
        self.assertEqual(proc.returncode, 1, proc.stdout + proc.stderr)
        self.assertIn("::error::", proc.stdout)
        self.assertIn("produced no output", proc.stdout)

    def test_an_empty_output_file_fails(self):
        write(self.out_dir, "ai-review-output.json", "")
        proc = self.review(write_output=False)
        self.assertEqual(proc.returncode, 1, proc.stdout + proc.stderr)

    def test_agent_failure_fails_the_step_even_with_output(self):
        write_executable(Path(self.bin, "uvx"), FAKE_UVX + "\nsys.exit(3)\n")
        proc = self.review()
        self.assertEqual(proc.returncode, 3, proc.stdout + proc.stderr)
        self.assertTrue(os.path.exists(self.out))

    def test_fails_without_a_diff(self):
        os.remove(os.path.join(self.ai_dir, "pr.diff"))
        proc = self.review()
        self.assertEqual(proc.returncode, 1)
        self.assertFalse(os.path.exists(self.log))

    def test_fails_without_the_plugin_copy(self):
        shutil.rmtree(os.path.join(self.ai_dir, "plugin"))
        proc = self.review(AI_REVIEW_HARNESS="claude-code", ANTHROPIC_API_KEY="sk-a")
        self.assertEqual(proc.returncode, 1)
        self.assertFalse(os.path.exists(self.log))

    def test_codex_does_not_need_the_plugin_copy(self):
        # Codex loads the skill from its runner image, not from the action's copy.
        shutil.rmtree(os.path.join(self.ai_dir, "plugin"))
        proc = self.review(AI_REVIEW_HARNESS="codex", OPENAI_API_KEY="sk-oai")
        self.assertEqual(proc.returncode, 0, proc.stdout + proc.stderr)
        self.assertTrue(os.path.exists(self.log))

    def test_invalid_harness_is_rejected(self):
        for value in ("claude", "Claude-Code", "codex ", "opencode", "x;id"):
            with self.subTest(value):
                proc = self.review(AI_REVIEW_HARNESS=value)
                self.assertEqual(proc.returncode, 1, proc.stdout + proc.stderr)
                self.assertIn("harness must be claude-code or codex", proc.stdout)
                self.assertFalse(os.path.exists(self.log))

    def test_codex_harness_argv_and_credentials(self):
        proc = self.review(AI_REVIEW_HARNESS="codex", OPENAI_API_KEY="sk-oai")
        self.assertEqual(proc.returncode, 0, proc.stdout + proc.stderr)
        self.assertEqual(self.record()["argv"], self.expected_argv(harness="codex"))
        self.assertEqual(self.record()["env"]["OPENAI_API_KEY"], "sk-oai")
        self.assertNotIn("sk-oai", " ".join(self.record()["argv"]))

    def test_claude_code_harness_argv_and_credentials(self):
        proc = self.review(AI_REVIEW_HARNESS="claude-code", ANTHROPIC_API_KEY="sk-ant")
        self.assertEqual(proc.returncode, 0, proc.stdout + proc.stderr)
        self.assertEqual(self.record()["argv"], self.expected_argv(harness="claude-code"))
        self.assertEqual(self.record()["env"]["ANTHROPIC_API_KEY"], "sk-ant")
        self.assertNotIn("sk-ant", " ".join(self.record()["argv"]))

    def test_an_empty_harness_is_codex(self):
        for harness in ({}, {"AI_REVIEW_HARNESS": ""}):
            with self.subTest(harness=harness):
                proc = self.review(**harness)
                self.assertEqual(proc.returncode, 0, proc.stdout + proc.stderr)
                self.assertEqual(self.record()["argv"], self.expected_argv(harness="codex"))

    def test_an_empty_image_picks_the_runner_image_of_the_harness(self):
        # The podman backend cannot run the sandbox images, so it keeps the runner family.
        for harness, image in (
            ("", "quay.io/aipcc/agentic-ci/codex-runner:latest"),
            ("codex", "quay.io/aipcc/agentic-ci/codex-runner:latest"),
            ("claude-code", "quay.io/aipcc/agentic-ci/claude-runner:latest"),
        ):
            with self.subTest(harness=harness):
                Path(self.podman_log).unlink(missing_ok=True)
                proc = self.review(AI_REVIEW_IMAGE="", AI_REVIEW_HARNESS=harness)
                self.assertEqual(proc.returncode, 0, proc.stdout + proc.stderr)
                self.assertEqual(self.podman_calls(), [f"pull {image}"])
                argv = self.record()["argv"]
                self.assertEqual(argv[argv.index("--image") + 1], image)
                self.assertNotIn("sandbox", " ".join(argv))

    def test_invalid_image_is_rejected(self):
        for value in ("quay.io/x/y z:latest", "img\tname"):
            with self.subTest(value):
                proc = self.review(AI_REVIEW_IMAGE=value)
                self.assertEqual(proc.returncode, 1, proc.stdout + proc.stderr)
                self.assertIn("image must", proc.stdout)
                self.assertFalse(os.path.exists(self.log))

    def test_invalid_agentic_ci_version_is_rejected(self):
        for value in ("", "latest", "--index-url", "0.5.15 --x", "0.5.15;id", "v0.5.15", "1.0 "):
            with self.subTest(value):
                proc = self.review(AI_REVIEW_AGENTIC_CI_VERSION=value)
                self.assertEqual(proc.returncode, 1, proc.stdout + proc.stderr)
                self.assertIn("agentic_ci_version must be", proc.stdout)
                self.assertFalse(os.path.exists(self.log))

    def test_pep440_style_versions_are_accepted(self):
        for value in ("0.5.15", "1.0.0rc1", "2!1.0.post1", "1.0+local.1"):
            with self.subTest(value):
                self.VERSION = value
                proc = self.review()
                self.assertEqual(proc.returncode, 0, proc.stdout + proc.stderr)
                self.assertEqual(self.record()["argv"][:2], ["--from", f"agentic-ci=={value}"])

    def test_image_is_pulled_before_the_run_and_nothing_else_calls_podman(self):
        proc = self.review()
        self.assertEqual(proc.returncode, 0, proc.stdout + proc.stderr)
        self.assertEqual(self.podman_calls(), [f"pull {self.IMAGE}"])

    def test_the_pull_does_not_count_against_the_agent_timeout(self):
        log = os.path.join(self.tmp, "timeout.log")
        self.fake_timeout(124)
        self.review(FAKE_TIMEOUT_LOG=log)
        self.assertEqual(self.podman_calls(), [f"pull {self.IMAGE}"])
        self.assertNotIn("podman", Path(log).read_text(encoding="utf-8"))

    def test_a_failed_pull_fails_before_the_agent_runs(self):
        proc = self.review(FAKE_PODMAN_STATUS="1")
        self.assertEqual(proc.returncode, 1, proc.stdout + proc.stderr)
        self.assertIn(f"Could not pull the image {self.IMAGE}", proc.stdout)
        self.assertFalse(os.path.exists(self.log))

    def leftover_tmp_dirs(self) -> list[str]:
        return [n for n in os.listdir(self.runner_temp) if n.startswith("ai-review-tmp.")]

    def test_the_agent_runs_with_a_private_tmpdir_under_runner_temp(self):
        proc = self.review()
        self.assertEqual(proc.returncode, 0, proc.stdout + proc.stderr)
        tmpdir = self.record()["env"]["TMPDIR"]
        self.assertEqual(os.path.dirname(tmpdir), self.runner_temp)
        self.assertTrue(os.path.basename(tmpdir).startswith("ai-review-tmp."), tmpdir)

    def test_the_pull_does_not_use_the_private_tmpdir(self):
        self.review()
        pulled = Path(self.podman_log + ".tmpdir").read_text(encoding="utf-8").splitlines()
        self.assertEqual(pulled, [os.environ.get("TMPDIR", "")])
        self.assertNotEqual(pulled, [self.record()["env"]["TMPDIR"]])

    def test_the_tmpdir_is_gone_after_the_step(self):
        self.review()
        self.assertFalse(os.path.exists(self.record()["env"]["TMPDIR"]))
        self.assertEqual(self.leftover_tmp_dirs(), [])

    def test_the_tmpdir_is_gone_when_the_agent_fails(self):
        write_executable(Path(self.bin, "uvx"), FAKE_UVX + "\nsys.exit(3)\n")
        proc = self.review()
        self.assertEqual(proc.returncode, 3, proc.stdout + proc.stderr)
        self.assertEqual(self.leftover_tmp_dirs(), [])

    def test_the_tmpdir_is_gone_when_the_agent_times_out(self):
        self.fake_timeout(124)
        proc = self.review(FAKE_TIMEOUT_LOG=os.path.join(self.tmp, "timeout.log"))
        self.assertEqual(proc.returncode, 1, proc.stdout + proc.stderr)
        self.assertEqual(self.leftover_tmp_dirs(), [])

    def test_the_tmpdir_is_gone_when_the_pull_fails(self):
        proc = self.review(FAKE_PODMAN_STATUS="1")
        self.assertEqual(proc.returncode, 1, proc.stdout + proc.stderr)
        self.assertEqual(self.leftover_tmp_dirs(), [])

    def test_missing_runner_temp_is_an_error(self):
        proc = self.review(RUNNER_TEMP="")
        self.assertEqual(proc.returncode, 1, proc.stdout + proc.stderr)
        self.assertIn("RUNNER_TEMP is not set", proc.stdout)
        self.assertFalse(os.path.exists(self.log))
        self.assertEqual(self.podman_calls(), [])

    def test_a_local_image_is_not_pulled(self):
        proc = self.review(AI_REVIEW_IMAGE="localhost/codex-runner:dev")
        self.assertEqual(proc.returncode, 0, proc.stdout + proc.stderr)
        self.assertEqual(self.podman_calls(), [])
        self.assertIn("localhost/codex-runner:dev", self.record()["argv"])

    def test_a_registry_image_is_pulled(self):
        self.review(AI_REVIEW_IMAGE="quay.io/localhost/x:1")
        self.assertEqual(self.podman_calls(), ["pull quay.io/localhost/x:1"])

    def test_effort_is_passed_to_agentic_ci(self):
        self.review()
        argv = self.record()["argv"]
        self.assertEqual(argv[argv.index("--effort") + 1], script_constant("AGENT_EFFORT"))

    def test_whitespace_in_the_workspace_path_fails(self):
        self.workspace = os.path.join(self.tmp, "work space")
        proc = self.review()
        self.assertEqual(proc.returncode, 1, proc.stdout + proc.stderr)
        self.assertIn("whitespace", proc.stdout)


FAKE_REVIEW_PY = """
import json
import os
import sys

keys = ["UV_NO_CONFIG", "UV_PYTHON", "GIT_CONFIG_COUNT", "GIT_CONFIG_KEY_0",
        "GIT_CONFIG_VALUE_0", "GIT_CONFIG_KEY_1", "GIT_CONFIG_VALUE_1", "GITHUB_TOKEN",
        "AGENT_TOOL", "AGENT_MODEL", "AGENT_REASONING_EFFORT"]
with open(os.environ["FAKE_LOG"], "w", encoding="utf-8") as f:
    json.dump(
        {
            "argv": sys.argv[1:],
            "env": {k: os.environ.get(k) for k in keys},
            "output": open(sys.argv[-1], encoding="utf-8").read(),
        },
        f,
    )
"""


class PostTests(ScriptTestCase):
    def setUp(self):
        super().setUp()
        repo, _base = make_repo(self.tmp, {"a.txt": "one\n", ".gitignore": "ignored.txt\n"})
        self.repo = self.move_repo_into_place(repo)
        self.action_path = os.path.join(self.tmp, "action")
        self.log = os.path.join(self.tmp, "review-py.json")
        write_executable(Path(self.action_path, REVIEW_PY_REL), FAKE_REVIEW_PY)
        write(self.out_dir, "ai-review-output.json", '{"summary": "ok"}')

    def post(self, **env: str):
        return self.run_script(
            "post",
            self.repo,
            GITHUB_ACTION_PATH=self.action_path,
            FAKE_LOG=self.log,
            GITHUB_TOKEN="ghs_token",
            **env,
        )

    def assert_refused(self, proc) -> None:
        self.assertEqual(proc.returncode, 1, proc.stdout + proc.stderr)
        self.assertIn("::error::", proc.stdout)
        self.assertFalse(os.path.exists(self.log), "review.py ran despite the guard")

    def recorded(self) -> dict:
        return json.loads(Path(self.log).read_text(encoding="utf-8"))

    def test_output_is_posted_through_review_py_from_a_copy(self):
        proc = self.post()
        self.assertEqual(proc.returncode, 0, proc.stdout + proc.stderr)
        record = self.recorded()
        self.assertEqual(record["argv"][:3], ["post", "--platform", "github"])
        copy = record["argv"][3]
        self.assertNotEqual(copy, self.out)
        self.assertTrue(copy.startswith(self.runner_temp + os.sep), copy)
        self.assertEqual(record["output"], '{"summary": "ok"}')
        env = record["env"]
        self.assertEqual(env["UV_NO_CONFIG"], "1")
        self.assertEqual(env["UV_PYTHON"], shutil.which("python3"))
        self.assertEqual(env["GITHUB_TOKEN"], "ghs_token")
        self.assertEqual(env["GIT_CONFIG_COUNT"], "2")
        overrides = {
            env["GIT_CONFIG_KEY_0"]: env["GIT_CONFIG_VALUE_0"],
            env["GIT_CONFIG_KEY_1"]: env["GIT_CONFIG_VALUE_1"],
        }
        self.assertEqual(overrides, {"core.fsmonitor": "false", "core.hooksPath": "/dev/null"})

    def test_footer_env_reaches_review_py(self):
        proc = self.post(AGENT_MODEL="some-model")
        self.assertEqual(proc.returncode, 0, proc.stdout + proc.stderr)
        env = self.recorded()["env"]
        self.assertEqual(env["AGENT_TOOL"], "codex")
        self.assertEqual(env["AGENT_MODEL"], "some-model")
        self.assertEqual(env["AGENT_REASONING_EFFORT"], script_constant("AGENT_EFFORT"))

    def test_the_footer_names_the_harness(self):
        for harness, tool in (("codex", "codex"), ("claude-code", "claude-code"), ("", "codex")):
            with self.subTest(harness=harness):
                proc = self.post(AI_REVIEW_HARNESS=harness)
                self.assertEqual(proc.returncode, 0, proc.stdout + proc.stderr)
                self.assertEqual(self.recorded()["env"]["AGENT_TOOL"], tool)

    def test_an_already_dirty_checkout_still_posts(self):
        write(self.repo, "a.txt", "normalized differently\n")
        write(self.repo, "dropped.txt", "x\n")
        write(self.repo, "ignored.txt", "x\n")
        proc = self.post()
        self.assertEqual(proc.returncode, 0, proc.stdout + proc.stderr)

    def test_symlinked_output_is_refused(self):
        target = os.path.join(self.tmp, "elsewhere.json")
        Path(target).write_text('{"summary": "other"}', encoding="utf-8")
        os.remove(self.out)
        os.symlink(target, self.out)
        self.assert_refused(self.post())

    def test_symlinked_out_dir_is_refused(self):
        moved = os.path.join(self.tmp, "moved_out")
        os.rename(self.out_dir, moved)
        os.symlink(moved, self.out_dir)
        self.assert_refused(self.post())

    def test_output_that_is_not_a_regular_file_is_refused(self):
        os.remove(self.out)
        os.mkdir(self.out)
        self.assert_refused(self.post())

    def test_oversized_output_is_refused(self):
        Path(self.out).write_bytes(b"x" * (1024 * 1024 + 1))
        self.assert_refused(self.post())

    def test_output_of_exactly_one_mib_is_posted(self):
        Path(self.out).write_bytes(b"x" * (1024 * 1024))
        proc = self.post()
        self.assertEqual(proc.returncode, 0, proc.stdout + proc.stderr)
        self.assertEqual(len(self.recorded()["output"]), 1024 * 1024)

    def test_missing_output_is_refused(self):
        os.remove(self.out)
        self.assert_refused(self.post())

    def test_empty_output_is_refused(self):
        Path(self.out).write_text("", encoding="utf-8")
        self.assert_refused(self.post())

    def test_missing_runner_temp_is_refused(self):
        self.assert_refused(self.post(RUNNER_TEMP=""))

    def test_review_py_failure_fails_the_step(self):
        write_executable(Path(self.action_path, REVIEW_PY_REL), FAKE_REVIEW_PY + "\nsys.exit(1)\n")
        proc = self.post()
        self.assertEqual(proc.returncode, 1, proc.stdout + proc.stderr)
        self.assertTrue(os.path.exists(self.log))

    def test_non_executable_poster_is_refused(self):
        Path(self.action_path, REVIEW_PY_REL).chmod(0o644)
        self.assert_refused(self.post())


class WorkdirGuardTests(ScriptTestCase):
    """A committed .ai-review or .ai-review-head link must not redirect a delete or a mount."""

    ENV = dict(
        AI_REVIEW_EVENT="pull_request",
        GITHUB_REPOSITORY="o/r",
        AI_REVIEW_HEAD_REPO="o/r",
        ANTHROPIC_API_KEY="sk-test",
        AI_REVIEW_BASE_SHA="a" * 40,
        AI_REVIEW_HEAD_SHA="b" * 40,
        AI_REVIEW_IMAGE="quay.io/x/y:latest",
        AI_REVIEW_AGENTIC_CI_VERSION=VERIFIED_AGENTIC_CI_VERSION,
        GITHUB_ACTION_PATH="/nonexistent",
    )

    def setUp(self):
        super().setUp()
        os.makedirs(self.workspace)
        self.target = tempfile.mkdtemp(dir=self.tmp)
        for rel in ("out/x", "plugin/x", "repo/x", "pr.diff", "precious.txt"):
            write(self.target, rel, "x\n")

    def assert_target_untouched(self) -> None:
        for rel in ("out/x", "plugin/x", "repo/x", "pr.diff", "precious.txt"):
            self.assertTrue(os.path.exists(os.path.join(self.target, rel)), rel)

    def test_a_symlink_at_either_path_is_refused_by_every_step(self):
        for link in (self.ai_dir, self.head_dir):
            for command in ("check", "prepare", "review", "post"):
                with self.subTest(link=os.path.basename(link), command=command):
                    os.symlink(self.target, link)
                    self.addCleanup(lambda p=link: os.path.islink(p) and os.remove(p))
                    proc = self.run_script(command, self.tmp, **self.ENV)
                    self.assertEqual(proc.returncode, 1, proc.stdout + proc.stderr)
                    self.assertIn("::error::", proc.stdout)
                    self.assertIn("symlink", proc.stdout)
                    self.assert_target_untouched()
                    os.remove(link)

    def test_a_file_at_the_workdir_path_is_refused_by_every_step(self):
        Path(self.ai_dir).write_text("x\n", encoding="utf-8")
        for command in ("check", "prepare", "review", "post"):
            with self.subTest(command=command):
                proc = self.run_script(command, self.tmp, **self.ENV)
                self.assertEqual(proc.returncode, 1, proc.stdout + proc.stderr)
                self.assertIn("::error::", proc.stdout)
                self.assertIn("not a directory", proc.stdout)


class CleanupTests(ScriptTestCase):
    def setUp(self):
        super().setUp()
        self.action_path = os.path.join(self.tmp, "action")
        write(self.ai_dir, "repo/sub/file.txt", "x\n")
        write(self.ai_dir, "out/ai-review-output.json", "{}")
        write(self.head_dir, ".git/HEAD", "x\n")
        write(self.workspace, "keep.txt", "x\n")
        write(self.workspace, ".ai-review-other/file.txt", "x\n")
        write(self.action_path, "action/review-pr.sh", "x\n")

    def cleanup(self, **env: str):
        return self.run_script("cleanup", self.tmp, GITHUB_ACTION_PATH=self.action_path, **env)

    def test_removes_only_the_workdir_and_the_checkout(self):
        proc = self.cleanup()
        self.assertEqual(proc.returncode, 0, proc.stdout + proc.stderr)
        self.assertFalse(os.path.exists(self.ai_dir))
        self.assertFalse(os.path.exists(self.head_dir))
        self.assertTrue(os.path.exists(os.path.join(self.workspace, "keep.txt")))
        self.assertTrue(os.path.exists(os.path.join(self.workspace, ".ai-review-other")))
        self.assertTrue(os.path.exists(os.path.join(self.action_path, "action/review-pr.sh")))

    def test_removes_leftover_private_tmpdirs_under_runner_temp_and_nothing_else(self):
        write(self.runner_temp, "ai-review-tmp.AbC123/credentials.json", "{}")
        write(self.runner_temp, "ai-review-tmp.XyZ789/credentials.json", "{}")
        write(self.runner_temp, "ai-review-post.Q1/ai-review-output.json", "{}")
        write(self.runner_temp, "other/file.txt", "x\n")
        proc = self.cleanup()
        self.assertEqual(proc.returncode, 0, proc.stdout + proc.stderr)
        self.assertEqual(sorted(os.listdir(self.runner_temp)), ["ai-review-post.Q1", "other"])

    def test_a_failed_removal_still_removes_the_private_tmpdirs_and_fails_at_the_end(self):
        write(self.ai_dir, "out/locked/file.txt", "x\n")
        write(self.runner_temp, "ai-review-tmp.AbC123/credentials.json", "{}")
        locked = os.path.join(self.ai_dir, "out/locked")
        os.chmod(locked, 0)
        self.addCleanup(os.chmod, locked, 0o755)
        if os.access(locked, os.R_OK):
            self.skipTest("running as root: a mode-000 directory does not block the removal")
        proc = self.cleanup()
        self.assertEqual(proc.returncode, 1, proc.stdout + proc.stderr)
        self.assertIn("::error::", proc.stdout)
        self.assertFalse(os.path.exists(os.path.join(self.runner_temp, "ai-review-tmp.AbC123")))

    def test_a_symlinked_private_tmpdir_is_not_followed(self):
        target = tempfile.mkdtemp(dir=self.tmp)
        write(target, "precious.txt", "x\n")
        link = os.path.join(self.runner_temp, "ai-review-tmp.Link01")
        os.symlink(target, link)
        self.assertEqual(self.cleanup().returncode, 0)
        self.assertTrue(os.path.exists(os.path.join(target, "precious.txt")))

    def test_cleanup_without_runner_temp_still_removes_the_workdirs(self):
        proc = self.cleanup(RUNNER_TEMP="")
        self.assertEqual(proc.returncode, 0, proc.stdout + proc.stderr)
        self.assertFalse(os.path.exists(self.ai_dir))

    def test_missing_workdir_is_fine(self):
        shutil.rmtree(self.ai_dir)
        self.assertEqual(self.cleanup().returncode, 0)

    def test_cleanup_removes_only_containers_that_mount_the_workdir(self):
        containers = {"c1": [self.ai_dir], "c2": ["/elsewhere"], "c3": ["/x", self.ai_dir]}
        proc = self.cleanup(FAKE_PODMAN_CONTAINERS=json.dumps(containers))
        self.assertEqual(proc.returncode, 0, proc.stdout + proc.stderr)
        removed = [c for c in self.podman_calls() if c.startswith("rm ")]
        self.assertEqual(removed, ["rm -f c1", "rm -f c3"])

    def test_cleanup_without_containers_only_lists_them(self):
        proc = self.cleanup()
        self.assertEqual(proc.returncode, 0, proc.stdout + proc.stderr)
        self.assertEqual([c.split()[0] for c in self.podman_calls()], ["ps"])

    def test_a_failed_container_removal_still_removes_the_workdirs_and_fails(self):
        proc = self.cleanup(
            FAKE_PODMAN_CONTAINERS=json.dumps({"c1": [self.ai_dir]}), FAKE_PODMAN_RM_STATUS="1"
        )
        self.assertEqual(proc.returncode, 1, proc.stdout + proc.stderr)
        self.assertFalse(os.path.exists(self.ai_dir))

    def test_cleanup_without_podman_still_removes_the_workdirs(self):
        # Only the tools cleanup needs, so a podman installed on the host stays out of reach.
        tools = os.path.join(self.tmp, "tools")
        os.makedirs(tools)
        for tool in ("bash", "rm"):
            os.symlink(shutil.which(tool), os.path.join(tools, tool))
        proc = self.cleanup(PATH=tools)
        self.assertEqual(proc.returncode, 0, proc.stdout + proc.stderr)
        self.assertFalse(os.path.exists(self.ai_dir))

    def test_a_symlink_is_not_followed(self):
        for path in (self.ai_dir, self.head_dir):
            with self.subTest(path=path):
                target = tempfile.mkdtemp(dir=self.tmp)
                write(target, "precious.txt", "x\n")
                shutil.rmtree(path, ignore_errors=True)
                os.symlink(target, path)
                self.assertEqual(self.cleanup().returncode, 0)
                self.assertTrue(os.path.exists(os.path.join(target, "precious.txt")))
                self.assertTrue(os.path.islink(path))
                os.remove(path)

    def test_missing_workspace_fails(self):
        proc = self.cleanup(GITHUB_WORKSPACE="")
        self.assertEqual(proc.returncode, 1, proc.stdout + proc.stderr)
