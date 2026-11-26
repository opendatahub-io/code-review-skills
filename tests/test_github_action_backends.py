"""Tests for the GitHub Action's backends: openshell in a privileged docker container, and podman.

The review and cleanup steps run review-pr.sh as a subprocess with fake `docker`, `podman`,
`uvx` and `timeout` executables on PATH: no network, container runtime or token.
"""

import hashlib
import json
import os
import re
import shutil
from pathlib import Path
from unittest.mock import patch

from tests.helpers import PanelTestCase, write
from tests.test_github_action import (
    README,
    SCRIPT,
    VERIFIED_AGENTIC_CI_VERSION,
    ReviewTestCase,
    ScriptTestCase,
    find_step,
    input_default,
    script_constant,
    script_function,
    write_executable,
)
from tests.test_github_action_deep import deep_prompt

OPENSHELL_DIR = "/sandbox/.ai-review"
TOKEN_NAMES = ("GITHUB_TOKEN", "GH_TOKEN")
TOKEN_VALUES = ("ghs_secret", "gh_secret", "oidc-token", "https://oidc.example", "oidc-extra")
RUN_ID = "4242"
RUN_ATTEMPT = "3"
DIGEST = "sha256:" + "0123456789abcdef" * 4

# Stands in for timeout: appends its own options and the command to FAKE_TIMEOUT_LOG as one JSON
# line, then runs the command. With FAKE_TIMEOUT_STATUS set, the agent run (the call that starts
# agentic-ci) exits with that status instead of running.
FAKE_TIMEOUT = """
import json
import os
import sys

args = sys.argv[1:]
cut = min(args.index(tool) for tool in ("docker", "podman", "uvx") if tool in args)
with open(os.environ["FAKE_TIMEOUT_LOG"], "a", encoding="utf-8") as f:
    f.write(json.dumps({"limit": args[:cut], "command": args[cut:]}) + "\\n")
status = os.environ.get("FAKE_TIMEOUT_STATUS")
if status and "agentic-ci" in args:
    sys.exit(int(status))
os.execvp(args[cut], args[cut:])
"""


def is_token_name(name: str) -> bool:
    return name in TOKEN_NAMES or name.startswith("ACTIONS_ID_TOKEN_")


def quoted_constant(name: str) -> str:
    """A review-pr.sh constant whose value is quoted and may hold spaces."""
    m = re.search(rf"""(?m)^{name}=(["'])(.*)\1$""", SCRIPT.read_text(encoding="utf-8"))
    assert m, f"constant {name} not found in review-pr.sh"
    return m.group(2)


class OpenShellReviewTestCase(ReviewTestCase):
    """Runs the review step with the default backend, which is openshell."""

    def setUp(self):
        super().setUp()
        self.timeout_log = os.path.join(self.tmp, "timeout.jsonl")
        write_executable(Path(self.bin, "timeout"), FAKE_TIMEOUT)
        self.home = os.path.join(self.tmp, "home")
        os.makedirs(self.home)

    def openshell_review(self, **env: str):
        defaults = dict(
            AI_REVIEW_BACKEND="",
            AI_REVIEW_IMAGE="",
            GITHUB_RUN_ID=RUN_ID,
            GITHUB_RUN_ATTEMPT=RUN_ATTEMPT,
            FAKE_TIMEOUT_LOG=self.timeout_log,
            ACTIONS_ID_TOKEN_EXTRA="oidc-extra",
            # No gcloud credential file of the machine running the tests is read.
            HOME=self.home,
            CLOUDSDK_CONFIG="",
        )
        defaults.update(env)
        return self.review(**defaults)

    def suffix(self, mode: str = "standard") -> str:
        digest = hashlib.sha256(self.ai_dir.encode()).hexdigest()[:12]
        return f"{RUN_ID}-{RUN_ATTEMPT}-{mode}-{digest}"

    def openshell_image(self, version: str = VERIFIED_AGENTIC_CI_VERSION) -> str:
        return f"quay.io/aipcc/agentic-ci/openshell:{version}"

    def runs(self) -> list[list[str]]:
        return [argv for argv in self.docker_calls() if argv[:1] == ["run"]]

    def agent_run(self) -> list[str]:
        runs = [argv for argv in self.runs() if "agentic-ci" in argv]
        self.assertEqual(len(runs), 1, runs)
        return runs[0]

    def agent_record(self) -> dict:
        records = [r for r in self.docker_records() if "agentic-ci" in r["argv"]]
        self.assertEqual(len(records), 1)
        return records[0]

    def chown_run(self) -> list[str]:
        runs = [argv for argv in self.runs() if "--entrypoint" in argv]
        self.assertEqual(len(runs), 1, runs)
        return runs[0]

    def env_names(self, argv: list[str]) -> list[str]:
        return [argv[i + 1] for i, arg in enumerate(argv) if arg == "-e"]

    def timeouts(self) -> list[dict]:
        lines = Path(self.timeout_log).read_text(encoding="utf-8").splitlines()
        return [json.loads(line) for line in lines]

    def expected_prompt(
        self, harness: str, mode: str, instructions: bool = False, org_rules: bool = False
    ) -> str:
        if mode == "deep":
            return deep_prompt(harness, self.BASE, self.HEAD, org_rules=org_rules).replace(
                "/workspace", OPENSHELL_DIR
            )
        d = OPENSHELL_DIR
        paths = f"{d}/repo {d}/pr.diff {self.BASE} {self.HEAD} {d}/out/ai-review-output.json"
        if instructions:
            paths += f" {d}/instructions.md"
        if org_rules:
            paths += f" {d}/org-rules.yaml"
        if harness == "claude-code":
            return f"/code-review-skills:github-code-review {paths}"
        return (
            f"Invoke the /code-review-skills:github-code-review skill with the arguments: {paths}"
        )

    def expected_agent_run(
        self,
        harness: str = "codex",
        mode: str = "standard",
        env_names: tuple[str, ...] = ("OPENAI_API_KEY",),
        model: str | None = None,
    ) -> list[str]:
        suffix = self.suffix(mode)
        family = "claude" if harness == "claude-code" else "codex"
        argv = [
            "run",
            "--rm",
            "--privileged",
            "--init",
            "--pull",
            "never",
            "--name",
            f"ai-review-{suffix}",
            "--label",
            f"ai-review.workdir={self.ai_dir}",
            "-v",
            f"ai-review-store-{suffix}:/var/lib/containers",
            "-v",
            f"{self.ai_dir}:{self.ai_dir}",
            "--workdir",
            "/",
            "-e",
            "AGENT_ENABLED_PLUGINS",
        ]
        for name in env_names:
            argv += ["-e", name]
        argv += [
            self.openshell_image(),
            "agentic-ci",
            "run",
            "--harness",
            harness,
            "--backend",
            "openshell",
            "--workdir",
            self.ai_dir,
            "--image",
            f"quay.io/aipcc/agentic-ci/{family}-sandbox:{VERIFIED_AGENTIC_CI_VERSION}",
            "--no-otel",
            "--effort",
            script_constant("AGENT_EFFORT"),
        ]
        if model:
            argv += ["--model", model]
        argv.append(self.expected_prompt(harness, mode))
        if harness == "claude-code":
            argv += ["--", "--plugin-dir", f"{OPENSHELL_DIR}/plugin"]
        return argv


class OpenShellReviewTests(OpenShellReviewTestCase):
    def setUp(self):
        super().setUp()
        # Stands in for the .git prepare builds, which deep mode checks for.
        os.makedirs(os.path.join(self.repo_dir, ".git"))

    def test_default_argv_for_both_harnesses_and_both_modes(self):
        credentials = {
            "codex": ({"OPENAI_API_KEY": "sk-oai"}, ("OPENAI_API_KEY",)),
            "claude-code": ({"ANTHROPIC_API_KEY": "sk-ant"}, ("ANTHROPIC_API_KEY",)),
        }
        for harness, (env, names) in credentials.items():
            for mode in ("standard", "deep"):
                with self.subTest(harness=harness, mode=mode):
                    Path(self.docker_log).unlink(missing_ok=True)
                    proc = self.openshell_review(
                        AI_REVIEW_HARNESS=harness, AI_REVIEW_MODE=mode, **env
                    )
                    self.assertEqual(proc.returncode, 0, proc.stdout + proc.stderr)
                    self.assertEqual(
                        self.agent_run(),
                        self.expected_agent_run(harness=harness, mode=mode, env_names=names),
                    )
                    self.assertFalse(os.path.exists(self.log), "uvx ran with openshell")
                    self.assertEqual(self.podman_calls(), [])

    def test_credentials_of_the_machine_running_the_tests_do_not_reach_the_container(self):
        # A developer set up for Claude Code on Vertex exports these. The script must only see
        # what each test passes it.
        adc_dir = os.path.join(self.tmp, "gcloud")
        key = os.path.join(adc_dir, "application_default_credentials.json")
        write(adc_dir, "application_default_credentials.json", '{"type": "service_account"}')
        host = {
            "CLOUD_ML_REGION": "us-east5",
            "VERTEX_LOCATION": "us-east5",
            "GOOGLE_CLOUD_PROJECT": "host-project",
            "GOOGLE_APPLICATION_CREDENTIALS": key,
            "CLOUDSDK_CONFIG": adc_dir,
            "ANTHROPIC_VERTEX_PROJECT_ID": "host-project",
            "GCP_PROJECT_ID": "host-project",
            "CLAUDE_CODE_MAX_RETRIES": "9",
            "GCLOUD_CREDENTIALS": "host-credentials",
        }
        credentials = {
            "codex": ({"OPENAI_API_KEY": "sk-oai"}, ("OPENAI_API_KEY",)),
            "claude-code": ({"ANTHROPIC_API_KEY": "sk-ant"}, ("ANTHROPIC_API_KEY",)),
        }
        with patch.dict(os.environ, host):
            for harness, (env, names) in credentials.items():
                with self.subTest(harness=harness):
                    Path(self.docker_log).unlink(missing_ok=True)
                    proc = self.openshell_review(AI_REVIEW_HARNESS=harness, **env)
                    self.assertEqual(proc.returncode, 0, proc.stdout + proc.stderr)
                    self.assertEqual(
                        self.agent_run(),
                        self.expected_agent_run(harness=harness, env_names=names),
                    )
                    # Names only, so a failure never prints the environment.
                    agent_env = self.agent_record()["env"]
                    leaked = sorted(n for n, v in host.items() if agent_env.get(n) == v)
                    self.assertEqual(leaked, [])
                    self.assertNotIn("GCLOUD_CREDENTIALS", sorted(agent_env))
            # The host's key file is never read in place of a credential the test passes.
            with self.subTest("vertex without a credential"):
                Path(self.docker_log).unlink(missing_ok=True)
                proc = self.openshell_review(
                    AI_REVIEW_HARNESS="claude-code",
                    OPENAI_API_KEY="",
                    CLAUDE_CODE_USE_VERTEX="1",
                    ANTHROPIC_VERTEX_PROJECT_ID="proj",
                )
                self.assertEqual(proc.returncode, 1, proc.stdout + proc.stderr)
                self.assertIn("there is no Google credential", proc.stdout)
                self.assertEqual(self.docker_calls(), [])

    def test_explicit_openshell_matches_the_default(self):
        proc = self.openshell_review(AI_REVIEW_BACKEND="openshell")
        self.assertEqual(proc.returncode, 0, proc.stdout + proc.stderr)
        self.assertEqual(self.agent_run(), self.expected_agent_run())

    def test_the_agent_container_runs_privileged_with_an_init(self):
        self.openshell_review()
        argv = self.agent_run()
        image = argv.index(self.openshell_image())
        self.assertIn("--privileged", argv[:image])
        self.assertIn("--init", argv[:image])
        self.assertEqual(argv[image + 1 : image + 3], ["agentic-ci", "run"])

    def test_the_openshell_tag_is_the_agentic_ci_version(self):
        for version in (VERIFIED_AGENTIC_CI_VERSION, "0.5.17", "1.0.0rc1"):
            with self.subTest(version):
                Path(self.docker_log).unlink(missing_ok=True)
                self.VERSION = version
                proc = self.openshell_review()
                self.assertEqual(proc.returncode, 0, proc.stdout + proc.stderr)
                image = self.openshell_image(version)
                self.assertEqual(self.docker_calls()[0], ["pull", "--quiet", image])
                self.assertIn(image, self.agent_run())
                self.assertIn(image, self.chown_run())

    def test_a_version_that_is_not_an_image_tag_fails(self):
        for version in ("1.0+local.1", "2!1.0"):
            with self.subTest(version):
                self.VERSION = version
                proc = self.openshell_review()
                self.assertEqual(proc.returncode, 1, proc.stdout + proc.stderr)
                self.assertIn("tag of the openshell image", proc.stdout)
                self.assertEqual(self.docker_calls(), [])

    def test_a_digest_pins_the_openshell_image_everywhere_it_runs(self):
        self.VERSION = f"{VERIFIED_AGENTIC_CI_VERSION}@{DIGEST}"
        proc = self.openshell_review()
        self.assertEqual(proc.returncode, 0, proc.stdout + proc.stderr)
        image = f"quay.io/aipcc/agentic-ci/openshell:{VERIFIED_AGENTIC_CI_VERSION}@{DIGEST}"
        self.assertEqual(self.docker_calls()[0], ["pull", "--quiet", image])
        runs = self.runs()
        self.assertEqual(len(runs), 3, runs)
        for argv in runs:
            with self.subTest(argv=argv[:8]):
                self.assertIn(image, argv)

    def test_a_malformed_digest_fails_before_any_container(self):
        hex64 = "0123456789abcdef" * 4
        for version in (
            f"0.5.15@sha256:{hex64[:-1]}",
            f"0.5.15@sha256:{hex64}0",
            f"0.5.15@sha256:{hex64.upper()}",
            f"0.5.15@sha512:{hex64}",
            f"0.5.15@{hex64}",
            f"@sha256:{hex64}",
            f"0.5.15@sha256:{hex64}@sha256:{hex64}",
            f"0.5.15:latest@sha256:{hex64}",
            f"1.0+local@sha256:{hex64}",
            "0.5.15@",
        ):
            with self.subTest(version):
                self.VERSION = version
                proc = self.openshell_review()
                self.assertEqual(proc.returncode, 1, proc.stdout + proc.stderr)
                self.assertIn("tag of the openshell image", proc.stdout)
                self.assertIn("@sha256:", proc.stdout)
                self.assertEqual(self.docker_calls(), [])

    def test_prompt_paths_are_sandbox_paths(self):
        write(self.ai_dir, "instructions.md", "Focus on tests\n")
        for harness in ("codex", "claude-code"):
            with self.subTest(harness):
                Path(self.docker_log).unlink(missing_ok=True)
                self.openshell_review(AI_REVIEW_HARNESS=harness, ANTHROPIC_API_KEY="k")
                argv = self.agent_run()
                prompt = argv[argv.index("--effort") + 2]
                self.assertEqual(prompt, self.expected_prompt(harness, "standard", True))
                # The workdir flag is the host path; every path after it is the sandbox's.
                tail = argv[argv.index("--effort") + 2 :]
                self.assertNotIn("/workspace/", " ".join(tail))
                if harness == "claude-code":
                    self.assertEqual(tail[1:], ["--", "--plugin-dir", f"{OPENSHELL_DIR}/plugin"])

    def test_the_org_rules_path_is_a_sandbox_path(self):
        write(self.ai_dir, "org-rules.yaml", "tone_instructions: Be brief.\n")
        proc = self.openshell_review(AI_REVIEW_HARNESS="claude-code", ANTHROPIC_API_KEY="k")
        self.assertEqual(proc.returncode, 0, proc.stdout + proc.stderr)
        argv = self.agent_run()
        prompt = argv[argv.index("--effort") + 2]
        self.assertEqual(prompt, self.expected_prompt("claude-code", "standard", org_rules=True))
        self.assertNotIn("/workspace/", prompt)

    def test_the_deep_org_rules_path_is_a_sandbox_path(self):
        # The deep sentence is a different string from the standard prompt's path word, so it
        # must substitute the backend's container dir too.
        write(self.ai_dir, "org-rules.yaml", "tone_instructions: Be brief.\n")
        proc = self.openshell_review(
            AI_REVIEW_MODE="deep", AI_REVIEW_HARNESS="claude-code", ANTHROPIC_API_KEY="k"
        )
        self.assertEqual(proc.returncode, 0, proc.stdout + proc.stderr)
        argv = self.agent_run()
        prompt = argv[argv.index("--effort") + 2]
        self.assertEqual(prompt, self.expected_prompt("claude-code", "deep", org_rules=True))
        self.assertIn(f"Apply the org review rules at {OPENSHELL_DIR}/org-rules.yaml.", prompt)
        self.assertNotIn("/workspace/", prompt)

    def test_no_timeout_flag_reaches_agentic_ci(self):
        # OpenShell ignores it, so only the outer limit bounds the run.
        self.openshell_review()
        self.assertNotIn("--timeout", self.agent_run())

    def test_the_model_comes_from_its_flag_only(self):
        proc = self.openshell_review(AI_REVIEW_MODEL="some-model", CODEX_MODEL="other")
        self.assertEqual(proc.returncode, 0, proc.stdout + proc.stderr)
        self.assertEqual(self.agent_run(), self.expected_agent_run(model="some-model"))

    def test_credentials_cross_by_name_only(self):
        secrets = dict(
            ANTHROPIC_API_KEY="sk-ant-value",
            GCP_PROJECT_ID="proj-value",
            CLOUD_ML_REGION="region-value",
            CLAUDE_CODE_OAUTH_TOKEN="",
        )
        proc = self.openshell_review(AI_REVIEW_HARNESS="claude-code", **secrets)
        self.assertEqual(proc.returncode, 0, proc.stdout + proc.stderr)
        argv = self.agent_run()
        self.assertEqual(
            self.env_names(argv),
            ["AGENT_ENABLED_PLUGINS", "ANTHROPIC_API_KEY", "GCP_PROJECT_ID", "CLOUD_ML_REGION"],
        )
        for name in self.env_names(argv):
            self.assertNotIn("=", name)
        joined = " ".join(arg for argv in self.docker_calls() for arg in argv)
        for value in secrets.values():
            if value:
                self.assertNotIn(value, joined)
        env = self.agent_record()["env"]
        self.assertEqual(env["ANTHROPIC_API_KEY"], "sk-ant-value")
        self.assertEqual(env["AGENT_ENABLED_PLUGINS"], "code-review-skills")

    def test_only_the_harness_credentials_are_passed(self):
        proc = self.openshell_review(
            AI_REVIEW_HARNESS="codex", OPENAI_API_KEY="sk-oai", ANTHROPIC_API_KEY="sk-ant"
        )
        self.assertEqual(proc.returncode, 0, proc.stdout + proc.stderr)
        self.assertEqual(
            self.env_names(self.agent_run()), ["AGENT_ENABLED_PLUGINS", "OPENAI_API_KEY"]
        )

    def test_no_token_name_or_value_reaches_docker(self):
        proc = self.openshell_review(AI_REVIEW_MODE="deep")
        self.assertEqual(proc.returncode, 0, proc.stdout + proc.stderr)
        records = self.docker_records()
        self.assertGreaterEqual(len(records), 5)
        for record in records:
            joined = " ".join(record["argv"])
            for name in (*TOKEN_NAMES, "ACTIONS_ID_TOKEN_"):
                self.assertNotIn(name, joined)
            for value in TOKEN_VALUES:
                self.assertNotIn(value, joined)
            for name, value in record["env"].items():
                self.assertFalse(is_token_name(name), name)
                self.assertNotIn(value, TOKEN_VALUES, name)

    def test_only_the_workdir_and_the_run_volume_are_mounted(self):
        self.openshell_review()
        volume = f"ai-review-store-{self.suffix()}"
        allowed = {f"{volume}:/var/lib/containers", f"{self.ai_dir}:{self.ai_dir}"}
        for argv in self.runs():
            mounts = {argv[i + 1] for i, arg in enumerate(argv) if arg == "-v"}
            self.assertLessEqual(mounts, allowed, argv)
            joined = " ".join(argv)
            for path in (self.runner_temp, self.head_dir, "docker.sock"):
                self.assertNotIn(path, joined)
            self.assertNotIn("--mount", argv)
            self.assertNotIn("--volumes-from", argv)

    def test_no_container_gets_a_terminal_or_stdin(self):
        self.openshell_review()
        for argv in self.runs():
            for flag in ("-i", "-t", "-it", "--interactive", "--tty"):
                self.assertNotIn(flag, argv)

    def test_docker_calls_in_order(self):
        proc = self.openshell_review()
        self.assertEqual(proc.returncode, 0, proc.stdout + proc.stderr)
        suffix = self.suffix()
        calls = self.docker_calls()
        self.assertEqual(calls[0], ["pull", "--quiet", self.openshell_image()])
        self.assertEqual(
            calls[1],
            [
                "volume",
                "create",
                "--label",
                f"ai-review.workdir={self.ai_dir}",
                f"ai-review-store-{suffix}",
            ],
        )
        self.assertEqual(
            calls[2][:8],
            [
                "run",
                "--rm",
                "--privileged",
                "--init",
                "--pull",
                "never",
                "--name",
                f"ai-review-{suffix}-pull",
            ],
        )
        self.assertIn(f"ai-review-store-{suffix}:/var/lib/containers", calls[2])
        self.assertEqual(
            calls[2][-1], f"quay.io/aipcc/agentic-ci/codex-sandbox:{VERIFIED_AGENTIC_CI_VERSION}"
        )
        self.assertIn("agentic-ci", calls[3])
        self.assertIn("--entrypoint", calls[4])
        self.assertEqual(len(calls), 5)

    def test_the_pulls_are_bounded_and_run_before_the_agent_timeout(self):
        proc = self.openshell_review(AI_REVIEW_TIMEOUT_MINUTES="7")
        self.assertEqual(proc.returncode, 0, proc.stdout + proc.stderr)
        limits = [(t["limit"], t["command"][:2]) for t in self.timeouts()]
        self.assertEqual(
            limits,
            [
                (["15m"], ["docker", "pull"]),
                (["-s", "INT", "--kill-after=30s", "15m"], ["docker", "run"]),
                (["-s", "INT", "--kill-after=60s", "7m"], ["docker", "run"]),
                (["2m"], ["docker", "run"]),
            ],
        )
        self.assertNotIn("agentic-ci", self.timeouts()[1]["command"])
        self.assertIn("agentic-ci", self.timeouts()[2]["command"])

    def test_a_local_image_is_not_prepulled(self):
        proc = self.openshell_review(AI_REVIEW_IMAGE="localhost/codex-sandbox:dev")
        self.assertEqual(proc.returncode, 0, proc.stdout + proc.stderr)
        prepull = self.runs()[0]
        self.assertEqual(prepull[-3:-1], ["-c", quoted_constant("PREPULL_SCRIPT")])
        self.assertEqual(prepull[-1], "bash")
        argv = self.agent_run()
        self.assertEqual(
            argv[argv.index("--image", argv.index("agentic-ci")) + 1], "localhost/codex-sandbox:dev"
        )

    def test_a_failed_pull_fails_before_the_agent_runs(self):
        proc = self.openshell_review(FAKE_DOCKER_PULL_STATUS="1")
        self.assertEqual(proc.returncode, 1, proc.stdout + proc.stderr)
        self.assertIn(f"Could not pull the image {self.openshell_image()}", proc.stdout)
        self.assertEqual(self.runs(), [])

    def test_a_failed_prepull_fails_before_the_agent_runs(self):
        proc = self.openshell_review(FAKE_DOCKER_PREPULL_STATUS="1")
        self.assertEqual(proc.returncode, 1, proc.stdout + proc.stderr)
        self.assertIn("Could not pull the sandbox images", proc.stdout)
        self.assertEqual(len(self.runs()), 1)
        self.assertIn(["rm", "-f", f"ai-review-{self.suffix()}-pull"], self.docker_calls())

    def test_the_workdir_is_given_back_after_the_run(self):
        proc = self.openshell_review()
        self.assertEqual(proc.returncode, 0, proc.stdout + proc.stderr)
        self.assertEqual(
            self.chown_run(),
            [
                "run",
                "--rm",
                "--pull",
                "never",
                "--name",
                f"ai-review-{self.suffix()}-chown",
                "--label",
                f"ai-review.workdir={self.ai_dir}",
                "-v",
                f"{self.ai_dir}:{self.ai_dir}",
                "--entrypoint",
                "chown",
                self.openshell_image(),
                "-R",
                "-h",
                "-P",
                f"{os.getuid()}:{os.getgid()}",
                self.ai_dir,
            ],
        )
        self.assertNotIn("--privileged", self.chown_run())

    def test_a_failed_give_back_fails_a_successful_run(self):
        proc = self.openshell_review(FAKE_DOCKER_CHOWN_STATUS="1")
        self.assertEqual(proc.returncode, 1, proc.stdout + proc.stderr)
        self.assertIn("::error::Could not give", proc.stdout)

    def test_timeouts_and_kills_map_to_the_existing_errors(self):
        cases = {
            124: "did not finish within 7 minutes",
            137: "The review was killed (exit 137): it ran past the 7 minute limit",
            125: "Could not start the OpenShell container (exit 125)",
        }
        for status, message in cases.items():
            with self.subTest(status=status):
                Path(self.docker_log).unlink(missing_ok=True)
                proc = self.openshell_review(
                    FAKE_TIMEOUT_STATUS=str(status), AI_REVIEW_TIMEOUT_MINUTES="7"
                )
                self.assertEqual(proc.returncode, 1, proc.stdout + proc.stderr)
                self.assertIn(message, proc.stdout)
                calls = self.docker_calls()
                # The container is removed and the workdir given back even after a failure.
                self.assertIn(["rm", "-f", f"ai-review-{self.suffix()}"], calls)
                self.assertIn("--entrypoint", calls[-1])

    def test_an_agent_failure_status_passes_through(self):
        proc = self.openshell_review(FAKE_TIMEOUT_STATUS="3")
        self.assertEqual(proc.returncode, 3, proc.stdout + proc.stderr)
        self.assertNotIn("exit 125", proc.stdout)

    def test_an_empty_output_still_fails(self):
        proc = self.openshell_review(FAKE_WRITE="0")
        self.assertEqual(proc.returncode, 1, proc.stdout + proc.stderr)
        self.assertIn("produced no output", proc.stdout)

    def test_a_colon_in_the_workdir_fails(self):
        self.workspace = os.path.join(self.tmp, "work:space")
        proc = self.openshell_review()
        self.assertEqual(proc.returncode, 1, proc.stdout + proc.stderr)
        self.assertIn("cannot carry", proc.stdout)
        self.assertEqual(self.docker_calls(), [])

    def test_no_private_tmpdir_is_left_behind(self):
        self.openshell_review()
        leftovers = [n for n in os.listdir(self.runner_temp) if n.startswith("ai-review-tmp.")]
        self.assertEqual(leftovers, [])


class GoogleCredentialFileTests(OpenShellReviewTestCase):
    """A credential file on the runner is not in the container, so its content crosses."""

    def credential_file(self, kind: str) -> str:
        path = os.path.join(self.tmp, "adc.json")
        Path(path).write_text(json.dumps({"type": kind, "private_key": "pk-value"}), "utf-8")
        return path

    def vertex_review(self, **env: str):
        return self.openshell_review(
            AI_REVIEW_HARNESS="claude-code",
            OPENAI_API_KEY="",
            CLAUDE_CODE_USE_VERTEX="1",
            ANTHROPIC_VERTEX_PROJECT_ID="proj",
            **env,
        )

    def test_a_key_file_crosses_as_gcloud_credentials(self):
        for kind in ("service_account", "authorized_user"):
            for var in ("GOOGLE_APPLICATION_CREDENTIALS", "GCP_SERVICE_ACCOUNT_KEY"):
                with self.subTest(kind=kind, var=var):
                    Path(self.docker_log).unlink(missing_ok=True)
                    path = self.credential_file(kind)
                    proc = self.vertex_review(**{var: path})
                    self.assertEqual(proc.returncode, 0, proc.stdout + proc.stderr)
                    argv = self.agent_run()
                    self.assertEqual(
                        self.env_names(argv),
                        [
                            "AGENT_ENABLED_PLUGINS",
                            "GCLOUD_CREDENTIALS",
                            "ANTHROPIC_VERTEX_PROJECT_ID",
                        ],
                    )
                    env = self.agent_record()["env"]
                    self.assertEqual(env["GCLOUD_CREDENTIALS"], Path(path).read_text("utf-8"))
                    self.assertNotIn(path, " ".join(argv))
                    self.assertNotIn("pk-value", " ".join(argv) + proc.stdout + proc.stderr)

    def test_inline_key_content_crosses_as_it_is(self):
        proc = self.vertex_review(GCP_SERVICE_ACCOUNT_KEY='{"type": "service_account"}')
        self.assertEqual(proc.returncode, 0, proc.stdout + proc.stderr)
        self.assertEqual(
            self.env_names(self.agent_run()),
            ["AGENT_ENABLED_PLUGINS", "GCP_SERVICE_ACCOUNT_KEY", "ANTHROPIC_VERTEX_PROJECT_ID"],
        )

    def test_workload_identity_federation_fails_clearly(self):
        proc = self.vertex_review(
            GOOGLE_APPLICATION_CREDENTIALS=self.credential_file("external_account")
        )
        self.assertEqual(proc.returncode, 1, proc.stdout + proc.stderr)
        self.assertIn(
            "::error::Workload Identity Federation credentials cannot be used", proc.stdout
        )
        self.assertIn("GCP_SERVICE_ACCOUNT_KEY", proc.stdout)
        self.assertEqual(self.docker_calls(), [])

    def test_an_unreadable_or_unknown_file_points_to_podman(self):
        bad = os.path.join(self.tmp, "bad.json")
        Path(bad).write_text("not json", "utf-8")
        for path in (bad, os.path.join(self.tmp, "missing.json"), self.credential_file("other")):
            with self.subTest(path=os.path.basename(path)):
                proc = self.vertex_review(GOOGLE_APPLICATION_CREDENTIALS=path)
                self.assertEqual(proc.returncode, 1, proc.stdout + proc.stderr)
                self.assertIn("backend: podman", proc.stdout)
                self.assertEqual(self.docker_calls(), [])

    def adc_file(self, config_dir: str, kind: str = "authorized_user") -> str:
        path = os.path.join(config_dir, "application_default_credentials.json")
        os.makedirs(config_dir, exist_ok=True)
        Path(path).write_text(json.dumps({"type": kind, "refresh_token": "rt-value"}), "utf-8")
        return path

    def test_the_gcloud_adc_file_crosses_as_gcloud_credentials(self):
        cases = {
            "home": (os.path.join(self.home, ".config", "gcloud"), ""),
            "cloudsdk config": (os.path.join(self.tmp, "gcloud"), os.path.join(self.tmp, "gcloud")),
        }
        for name, (config_dir, cloudsdk_config) in cases.items():
            with self.subTest(name):
                Path(self.docker_log).unlink(missing_ok=True)
                path = self.adc_file(config_dir)
                proc = self.vertex_review(CLOUDSDK_CONFIG=cloudsdk_config)
                self.assertEqual(proc.returncode, 0, proc.stdout + proc.stderr)
                argv = self.agent_run()
                self.assertEqual(
                    self.env_names(argv),
                    ["AGENT_ENABLED_PLUGINS", "GCLOUD_CREDENTIALS", "ANTHROPIC_VERTEX_PROJECT_ID"],
                )
                env = self.agent_record()["env"]
                self.assertEqual(env["GCLOUD_CREDENTIALS"], Path(path).read_text("utf-8"))
                self.assertNotIn("rt-value", " ".join(argv) + proc.stdout + proc.stderr)
                os.remove(path)

    def test_an_explicit_credential_file_wins_over_the_gcloud_adc_file(self):
        self.adc_file(os.path.join(self.home, ".config", "gcloud"), "external_account")
        path = self.credential_file("service_account")
        proc = self.vertex_review(GOOGLE_APPLICATION_CREDENTIALS=path)
        self.assertEqual(proc.returncode, 0, proc.stdout + proc.stderr)
        env = self.agent_record()["env"]
        self.assertEqual(env["GCLOUD_CREDENTIALS"], Path(path).read_text("utf-8"))

    def test_a_workload_identity_adc_file_fails_clearly(self):
        self.adc_file(os.path.join(self.home, ".config", "gcloud"), "external_account")
        proc = self.vertex_review()
        self.assertEqual(proc.returncode, 1, proc.stdout + proc.stderr)
        self.assertIn(
            "::error::Workload Identity Federation credentials cannot be used", proc.stdout
        )
        self.assertEqual(self.docker_calls(), [])

    def test_vertex_without_any_google_credential_points_to_podman(self):
        proc = self.vertex_review()
        self.assertEqual(proc.returncode, 1, proc.stdout + proc.stderr)
        self.assertIn("::error::CLAUDE_CODE_USE_VERTEX is set, but there is no Google", proc.stdout)
        self.assertIn(
            os.path.join(self.home, ".config", "gcloud", "application_default_credentials.json"),
            proc.stdout,
        )
        self.assertIn("backend: podman", proc.stdout)
        # It fails before any pull or container.
        self.assertEqual(self.docker_calls(), [])

    def test_an_api_key_wins_over_a_credential_file(self):
        proc = self.vertex_review(
            ANTHROPIC_API_KEY="sk-ant",
            GOOGLE_APPLICATION_CREDENTIALS=self.credential_file("external_account"),
        )
        self.assertEqual(proc.returncode, 0, proc.stdout + proc.stderr)
        self.assertNotIn("GCLOUD_CREDENTIALS", self.env_names(self.agent_run()))


class BackendChoiceTests(OpenShellReviewTestCase):
    def test_an_invalid_backend_fails_before_any_container(self):
        for value in ("docker", "OpenShell", "podman ", "x;id"):
            with self.subTest(value):
                proc = self.openshell_review(AI_REVIEW_BACKEND=value)
                self.assertEqual(proc.returncode, 1, proc.stdout + proc.stderr)
                self.assertIn("backend must be openshell or podman", proc.stdout)
                self.assertEqual(self.docker_calls(), [])
                self.assertEqual(self.podman_calls(), [])
                self.assertFalse(os.path.exists(self.log))

    def test_podman_argv_equals_the_shape_before_the_backend_input(self):
        proc = self.openshell_review(AI_REVIEW_BACKEND="podman")
        self.assertEqual(proc.returncode, 0, proc.stdout + proc.stderr)
        image = f"quay.io/aipcc/agentic-ci/codex-runner:{VERIFIED_AGENTIC_CI_VERSION}"
        prompt = (
            "Invoke the /code-review-skills:github-code-review skill with the arguments: "
            f"/workspace/repo /workspace/pr.diff {self.BASE} {self.HEAD} "
            "/workspace/out/ai-review-output.json"
        )
        self.assertEqual(
            self.record()["argv"],
            [
                "--from",
                f"agentic-ci=={VERIFIED_AGENTIC_CI_VERSION}",
                "agentic-ci",
                "run",
                "--harness",
                "codex",
                "--backend",
                "podman",
                "--workdir",
                self.ai_dir,
                "--image",
                image,
                "--no-otel",
                "--timeout",
                str(25 * 60),
                "--effort",
                "high",
                prompt,
            ],
        )
        self.assertEqual(self.podman_calls(), [f"pull {image}"])
        self.assertEqual(self.docker_calls(), [])
        limits = [t["limit"] for t in self.timeouts()]
        self.assertEqual(limits, [["15m"], ["-s", "INT", "--kill-after=60s", "20m"]])

    def test_podman_rejects_a_digest_with_a_clear_error(self):
        self.VERSION = f"{VERIFIED_AGENTIC_CI_VERSION}@{DIGEST}"
        proc = self.openshell_review(AI_REVIEW_BACKEND="podman")
        self.assertEqual(proc.returncode, 1, proc.stdout + proc.stderr)
        self.assertIn("PyPI version", proc.stdout)
        self.assertIn("cannot pin a digest", proc.stdout)
        self.assertFalse(os.path.exists(self.log))
        self.assertEqual(self.podman_calls(), [])
        self.assertEqual(self.docker_calls(), [])

    def test_podman_narrows_the_skill_catalog_to_this_plugin(self):
        # A value from the job environment does not widen it.
        for job_env in ({}, {"AGENT_ENABLED_PLUGINS": "other-plugin,code-review-skills"}):
            with self.subTest(job_env=job_env):
                proc = self.openshell_review(AI_REVIEW_BACKEND="podman", **job_env)
                self.assertEqual(proc.returncode, 0, proc.stdout + proc.stderr)
                self.assertEqual(
                    self.record()["env"]["AGENT_ENABLED_PLUGINS"], "code-review-skills"
                )

    def test_both_backends_narrow_the_catalog_from_one_constant(self):
        self.assertEqual(script_constant("ENABLED_PLUGINS"), '"code-review-skills"')
        self.assertIn(
            'export AGENT_ENABLED_PLUGINS="$ENABLED_PLUGINS"', script_function("cmd_review")
        )
        self.assertEqual(SCRIPT.read_text(encoding="utf-8").count("AGENT_ENABLED_PLUGINS="), 1)

    def test_podman_keeps_the_blanked_tokens(self):
        self.openshell_review(AI_REVIEW_BACKEND="podman")
        env = self.record()["env"]
        for var in TOKEN_NAMES + ("ACTIONS_ID_TOKEN_REQUEST_TOKEN",):
            self.assertEqual(env[var], "", var)


class OpenShellCleanupTests(ScriptTestCase):
    def setUp(self):
        super().setUp()
        write(self.ai_dir, "out/ai-review-output.json", "{}")
        write(self.head_dir, ".git/HEAD", "x\n")

    def cleanup(self, **env: str):
        defaults = dict(AI_REVIEW_AGENTIC_CI_VERSION=VERIFIED_AGENTIC_CI_VERSION)
        defaults.update(env)
        return self.run_script("cleanup", self.tmp, **defaults)

    def removals(self) -> list[list[str]]:
        return [
            argv
            for argv in self.docker_calls()
            if argv[:1] == ["rm"] or argv[:2] == ["volume", "rm"]
        ]

    def test_removes_only_the_labelled_containers_and_volumes_of_this_workdir(self):
        other = os.path.join(self.tmp, "other", ".ai-review")
        containers = {"c1": self.ai_dir, "c2": other, "c3": self.ai_dir, "c4": self.ai_dir + "x"}
        volumes = {"v1": self.ai_dir, "v2": other}
        proc = self.cleanup(
            FAKE_DOCKER_CONTAINERS=json.dumps(containers), FAKE_DOCKER_VOLUMES=json.dumps(volumes)
        )
        self.assertEqual(proc.returncode, 0, proc.stdout + proc.stderr)
        self.assertEqual(
            self.removals(), [["rm", "-f", "c1"], ["rm", "-f", "c3"], ["volume", "rm", "-f", "v1"]]
        )
        label = f"label=ai-review.workdir={self.ai_dir}"
        self.assertIn(["ps", "-aq", "--filter", label], self.docker_calls())
        self.assertIn(["volume", "ls", "-q", "--filter", label], self.docker_calls())
        self.assertFalse(os.path.exists(self.ai_dir))

    def test_the_podman_backend_does_not_call_docker(self):
        proc = self.cleanup(
            AI_REVIEW_BACKEND="podman", FAKE_DOCKER_CONTAINERS=json.dumps({"c1": self.ai_dir})
        )
        self.assertEqual(proc.returncode, 0, proc.stdout + proc.stderr)
        self.assertEqual(self.docker_calls(), [])

    def test_an_unreachable_docker_daemon_is_nothing_to_clean(self):
        # A docker CLI without daemon access, as on a self-hosted runner outside the docker
        # group, after a run that skipped itself before any container.
        proc = self.cleanup(
            FAKE_DOCKER_INFO_STATUS="1",
            FAKE_DOCKER_CONTAINERS=json.dumps({"c1": self.ai_dir}),
            FAKE_DOCKER_VOLUMES=json.dumps({"v1": self.ai_dir}),
            FAKE_DOCKER_RM_STATUS="1",
        )
        self.assertEqual(proc.returncode, 0, proc.stdout + proc.stderr)
        self.assertNotIn("::error::", proc.stdout)
        self.assertEqual(self.docker_calls(), [["info"]])
        self.assertFalse(os.path.exists(self.ai_dir))
        self.assertFalse(os.path.exists(self.head_dir))

    def test_a_skipped_run_with_an_unreachable_daemon_cleans_up_quietly(self):
        shutil.rmtree(self.ai_dir)
        shutil.rmtree(self.head_dir)
        proc = self.cleanup(FAKE_DOCKER_INFO_STATUS="1")
        self.assertEqual(proc.returncode, 0, proc.stdout + proc.stderr)
        self.assertEqual(proc.stdout, "")
        self.assertEqual(self.docker_calls(), [["info"]])

    def test_leftovers_with_an_unreachable_daemon_fail_at_the_end(self):
        self.lock_a_directory()
        proc = self.cleanup(FAKE_DOCKER_INFO_STATUS="1", FAKE_DOCKER_IMAGE_PRESENT="1")
        self.assertEqual(proc.returncode, 1, proc.stdout + proc.stderr)
        self.assertIn("::error::", proc.stdout)
        self.assertEqual([a for a in self.docker_calls() if a != ["info"]], [])
        self.assertFalse(os.path.exists(self.head_dir))

    def test_the_daemon_check_is_bounded(self):
        self.assertIn("timeout 1m docker info", script_function("uses_openshell_docker"))

    def test_a_hung_docker_daemon_is_reported(self):
        proc = self.cleanup(FAKE_DOCKER_INFO_STATUS="124")
        self.assertEqual(proc.returncode, 0, proc.stdout + proc.stderr)
        self.assertIn("::warning::The docker daemon did not answer", proc.stdout)
        self.assertEqual(self.docker_calls(), [["info"]])

    def test_the_daemon_is_asked_once_per_cleanup(self):
        self.lock_a_directory()
        self.cleanup(FAKE_DOCKER_INFO_STATUS="1", FAKE_DOCKER_IMAGE_PRESENT="1")
        self.assertEqual(self.docker_calls().count(["info"]), 1)

    def test_a_failed_removal_still_removes_the_workdirs_and_fails_at_the_end(self):
        proc = self.cleanup(
            FAKE_DOCKER_CONTAINERS=json.dumps({"c1": self.ai_dir}),
            FAKE_DOCKER_VOLUMES=json.dumps({"v1": self.ai_dir}),
            FAKE_DOCKER_RM_STATUS="1",
        )
        self.assertEqual(proc.returncode, 1, proc.stdout + proc.stderr)
        self.assertIn("::error::", proc.stdout)
        self.assertEqual(len(self.removals()), 2)
        self.assertFalse(os.path.exists(self.ai_dir))
        self.assertFalse(os.path.exists(self.head_dir))

    def lock_a_directory(self) -> None:
        locked = os.path.join(self.ai_dir, "out", "locked")
        write(locked, "file.txt", "x\n")
        os.chmod(locked, 0)
        self.addCleanup(lambda: os.path.isdir(locked) and os.chmod(locked, 0o755))
        if os.access(locked, os.R_OK):
            self.skipTest("running as root: a mode-000 directory does not block the removal")

    def test_root_owned_leftovers_are_given_back_and_removed(self):
        self.lock_a_directory()
        proc = self.cleanup(FAKE_DOCKER_IMAGE_PRESENT="1")
        self.assertEqual(proc.returncode, 0, proc.stdout + proc.stderr)
        self.assertFalse(os.path.exists(self.ai_dir))
        chown = [argv for argv in self.docker_calls() if "--entrypoint" in argv]
        self.assertEqual(len(chown), 1)
        image = f"quay.io/aipcc/agentic-ci/openshell:{VERIFIED_AGENTIC_CI_VERSION}"
        self.assertEqual(
            chown[0][-6:], [image, "-R", "-h", "-P", f"{os.getuid()}:{os.getgid()}", self.ai_dir]
        )
        self.assertNotIn("--privileged", chown[0])

    def test_root_owned_leftovers_are_given_back_by_the_pinned_image(self):
        self.lock_a_directory()
        version = f"{VERIFIED_AGENTIC_CI_VERSION}@{DIGEST}"
        proc = self.cleanup(FAKE_DOCKER_IMAGE_PRESENT="1", AI_REVIEW_AGENTIC_CI_VERSION=version)
        self.assertEqual(proc.returncode, 0, proc.stdout + proc.stderr)
        image = f"quay.io/aipcc/agentic-ci/openshell:{version}"
        self.assertIn(["image", "inspect", image], self.docker_calls())
        chown = [argv for argv in self.docker_calls() if "--entrypoint" in argv]
        self.assertEqual(len(chown), 1)
        self.assertEqual(chown[0][-6], image)

    def test_a_malformed_version_runs_no_cleanup_image(self):
        self.lock_a_directory()
        for version in (f"0.5.15@sha256:{'0' * 63}", "0.5.15 ; id", ""):
            with self.subTest(version):
                Path(self.docker_log).unlink(missing_ok=True)
                proc = self.cleanup(
                    FAKE_DOCKER_IMAGE_PRESENT="1", AI_REVIEW_AGENTIC_CI_VERSION=version
                )
                self.assertEqual(proc.returncode, 1, proc.stdout + proc.stderr)
                calls = self.docker_calls()
                self.assertEqual([a for a in calls if a[:2] == ["image", "inspect"]], [])
                self.assertEqual([a for a in calls if "--entrypoint" in a], [])

    def test_the_agent_and_the_cleanup_build_the_image_ref_one_way(self):
        for function in ("run_openshell_agent", "give_back_workdir"):
            with self.subTest(function):
                self.assertIn("openshell_image_ref", script_function(function))
                self.assertNotIn("OPENSHELL_IMAGE_REPO", script_function(function))
        script = SCRIPT.read_text(encoding="utf-8")
        self.assertEqual(script.count("$OPENSHELL_IMAGE_REPO"), 1)
        self.assertIn("$OPENSHELL_IMAGE_REPO", script_function("openshell_image_ref"))

    def test_leftovers_without_the_image_fail_at_the_end(self):
        self.lock_a_directory()
        proc = self.cleanup()
        self.assertEqual(proc.returncode, 1, proc.stdout + proc.stderr)
        self.assertIn("::error::", proc.stdout)
        self.assertEqual([a for a in self.docker_calls() if "--entrypoint" in a], [])
        self.assertFalse(os.path.exists(self.head_dir))


class BackendStaticTests(PanelTestCase):
    def test_the_backend_input_defaults_to_openshell(self):
        self.assertEqual(input_default("backend"), "openshell")

    def test_the_backend_reaches_review_and_cleanup_only(self):
        for step in ("Review the pull request", "Remove the checkout"):
            with self.subTest(step):
                self.assertRegex(
                    find_step(step), r"(?m)^\s*AI_REVIEW_BACKEND:\s*\$\{\{ inputs\.backend \}\}\s*$"
                )
        for step in ("Check the event and credentials", "Prepare the diff", "Post the review"):
            with self.subTest(step):
                self.assertNotIn("AI_REVIEW_BACKEND", find_step(step))

    def test_the_cleanup_step_holds_no_token(self):
        self.assertNotIn("github.token", find_step("Remove the checkout"))

    def test_the_credential_lists_name_no_token(self):
        for name in ("CODEX_CREDENTIAL_VARS", "CLAUDE_CREDENTIAL_VARS"):
            with self.subTest(name):
                for var in quoted_constant(name).split():
                    self.assertFalse(is_token_name(var), var)
                    self.assertFalse(var.startswith("ACTIONS_"), var)
                    self.assertNotRegex(var, r"MODEL|EFFORT|IMAGE")

    def test_the_readme_documents_the_backends(self):
        readme = README.read_text(encoding="utf-8")
        row = next(line for line in readme.splitlines() if line.startswith("| `backend`"))
        self.assertIn("`openshell`", row)
        self.assertIn("41913015162", readme)
        self.assertIn("126867367300", readme)
        self.assertIn("41777652433", readme)
        self.assertRegex(readme, r"`--privileged`[^\n]*root on the host")
        self.assertIn("`backend: podman` is the fallback", readme)

    def test_no_bash_wrapper_around_agentic_ci(self):
        # The init process signals its direct child only, so agentic-ci must be that child.
        function = script_function("run_openshell_agent")
        agent = function[function.index('timeout -s INT --kill-after=60s "${minutes}m"') :]
        agent = agent[: agent.index("|| AGENT_STATUS")]
        self.assertIn('agentic-ci "$@"', agent)
        self.assertNotIn("bash", agent)
