"""Tests for the review's usage line and the optional MLflow export.

The usage.py tests run the script with the agentic-ci release the action pins, through uvx, so
they check the totals against agentic-ci's own parser and price map (skipped without uv). The
review, post, export and cleanup tests run review-pr.sh with the fakes of
tests/test_github_action.py, and the fake uvx runs usage.py against a stand-in agentic_ci
package. The review.py tests run in process, like tests/test_github_review.py.
"""

import contextlib
import io
import json
import os
import re
import subprocess
import sys
import unittest
from pathlib import Path
from unittest.mock import patch

from tests.helpers import HAVE_UV, REPO_ROOT, PanelTestCase, make_repo, write, write_json
from tests.test_github_action import ACTION_YML as ACTION_YML_PATH
from tests.test_github_action import (
    FAKE_UVX,
    README,
    SCRIPT,
    VERIFIED_AGENTIC_CI_VERSION,
    ReviewTestCase,
    ScriptTestCase,
    action_steps,
    find_step,
    input_default,
    run_text,
    script_constant,
    script_function,
    step_env_value,
    step_names,
    write_executable,
    write_fake_review_py,
)
from tests.test_github_action_backends import DIGEST, OpenShellReviewTestCase
from tests.test_github_action_post_extras import FAKE_REVIEW_PY
from tests.test_github_review import GitHubTestCase, marker, review, review_data

USAGE_PY = REPO_ROOT / "action/usage.py"
GITLAB_FORMAT_EXAMPLE = "<sub>Usage: 135 input (22% cached) • 20 output • est. $0.01</sub>"
ROOT_TRACE = "c" * 32
ROOT_SPAN = "d" * 16
EXPORT_STEP = "Export the telemetry to MLflow"
MLFLOW_SECRETS = {
    "MLFLOW_TRACKING_URI": "https://mlflow.example/secret-path",
    "MLFLOW_TRACKING_TOKEN": "mlflow-token-value",
}
MLFLOW_HOST = "mlflow.example"
RELAY_STEP = "Upload the telemetry to the review relay"
RELAY_SECRETS = {
    "REVIEW_TELEMETRY_USERNAME": "gitlab+deploy-token-4242",
    "REVIEW_TELEMETRY_TOKEN": "gldt-relaySecretValue1",
}
# The relay's project id, which callers keep in a variable: a stand-in, not the real one.
RELAY_PROJECT = "70123456"
RELAY_SETTINGS = {"REVIEW_TELEMETRY_PROJECT": RELAY_PROJECT, **RELAY_SECRETS}
# Everything that must never reach the agent or a tool's command line.
TELEMETRY_SECRETS = {**MLFLOW_SECRETS, **RELAY_SETTINGS}
# What agentic-ci 0.5.16's mlflow-push prints, on stdout and stderr, in each outcome. Every one
# of these exits 0 but "all failed".
PUSHED_ALL = (
    "\u25b6 Pushing traces to MLflow\n  JSONL: x\n  Endpoint: https://mlflow.example/secret-path\n"
    "  Experiment: my-reviews\n  Trace IDs: cccc\n"
    "  Pushed 3 payload(s) covering 1 trace(s) to MLflow (0 failed)\n"
)
LOOKUP_FAILED = (
    "Experiment lookup failed (401: denied): 401 Client Error: Unauthorized for url: "
    "https://mlflow.example/secret-path/api/2.0/mlflow/experiments/search\n"
    "MLflow experiment 'my-reviews' not found -- skipping trace push.\n"
)
NOT_FOUND = "MLflow experiment 'my-reviews' not found -- skipping trace push.\n"
NO_TRACES_LINE = "  No /v1/traces records found in JSONL\n"
PARTIAL = "  Pushed 2 payload(s) covering 1 trace(s) to MLflow (1 failed)\n"
PARTIAL_ERR = "Trace push failed (500): 500 Server Error\n"
ALL_FAILED = "  Failed to push traces to MLflow (3 errors)\n"


# ---------------------------------------------------------------------------
# OTEL fixtures, in the shape agentic-ci's collector writes
# ---------------------------------------------------------------------------


def attr(key, value):
    if isinstance(value, int):
        return {"key": key, "value": {"intValue": value}}
    return {"key": key, "value": {"stringValue": value}}


def codex_record(model="gpt-5.6-sol", **extra):
    attributes = {
        "event.name": "codex.sse_event",
        "event.kind": "response.completed",
        "model": model,
        "input_token_count": 100,
        "cached_token_count": 40,
        "cache_write_token_count": 10,
        "output_token_count": 20,
        **extra,
    }
    log_record = {"attributes": [attr(k, v) for k, v in attributes.items()]}
    return {
        "path": "/v1/logs",
        "payload": {"resourceLogs": [{"scopeLogs": [{"logRecords": [log_record]}]}]},
    }


def claude_metrics(model, tokens, cost):
    token_points = [
        {"asInt": value, "attributes": [attr("model", model), attr("type", kind)]}
        for kind, value in tokens.items()
    ]
    metrics = [{"name": "claude_code.token.usage", "sum": {"dataPoints": token_points}}]
    if cost is not None:
        cost_point = {"asDouble": cost, "attributes": [attr("model", model)]}
        metrics.append({"name": "claude_code.cost.usage", "sum": {"dataPoints": [cost_point]}})
    return {
        "path": "/v1/metrics",
        "payload": {"resourceMetrics": [{"scopeMetrics": [{"metrics": metrics}]}]},
    }


def root_record(trace=ROOT_TRACE, span=ROOT_SPAN, name="agentic-ci-run", end="123456789"):
    root = {"traceId": trace, "spanId": span, "name": name, "endTimeUnixNano": end}
    return {
        "path": "/v1/traces",
        "payload": {"resourceSpans": [{"scopeSpans": [{"spans": [root]}]}]},
    }


def jsonl(*records) -> str:
    return "".join(json.dumps(r) + "\n" for r in records)


CLAUDE_LOG = jsonl(
    claude_metrics(
        "claude-opus-4-6", {"input": 100, "output": 20, "cacheRead": 30, "cacheCreation": 5}, 0.12
    ),
    root_record(),
)


def last_span(log: str) -> dict:
    record = json.loads(Path(log).read_text(encoding="utf-8").splitlines()[-1])
    return record["payload"]["resourceSpans"][0]["scopeSpans"][0]["spans"][0]


def span_attributes(span: dict) -> dict:
    return {item["key"]: item["value"] for item in span["attributes"]}


# A stand-in for the agentic_ci package: claude_code metrics only, and no price for any model.
STUB_OTEL = """
from collections import defaultdict


def parse_metrics(records):
    tokens = defaultdict(float)
    costs = defaultdict(float)
    for record in records:
        payload = record.get("payload") if isinstance(record.get("payload"), dict) else {}
        for rm in payload.get("resourceMetrics", []):
            for sm in rm.get("scopeMetrics", []):
                for metric in sm.get("metrics", []):
                    for point in metric.get("sum", {}).get("dataPoints", []):
                        attrs = {
                            a["key"]: next(iter(a["value"].values()))
                            for a in point.get("attributes", [])
                        }
                        value = float(point.get("asInt", point.get("asDouble", 0)))
                        if metric["name"] == "claude_code.token.usage":
                            tokens[(attrs["model"], attrs["type"])] += value
                        elif metric["name"] == "claude_code.cost.usage":
                            costs[attrs["model"]] += value
    return tokens, costs, [], {}
"""
STUB_COST = """
def estimate_cost(*args, **kwargs):
    return None
"""


# ---------------------------------------------------------------------------
# usage.py against the pinned agentic-ci
# ---------------------------------------------------------------------------


@unittest.skipUnless(HAVE_UV, "usage.py runs with agentic-ci through uvx")
class UsageScriptTests(PanelTestCase):
    """usage.py with the agentic-ci release the action pins, as the review step runs it."""

    @classmethod
    def setUpClass(cls):
        cls.env = {
            k: v
            for k, v in os.environ.items()
            if not k.startswith(("AGENTIC_CI_", "GITHUB_", "AI_REVIEW_"))
        }
        cls.env["UV_NO_CONFIG"] = "1"

    def uvx_python(self, *args: str) -> subprocess.CompletedProcess:
        return subprocess.run(
            ["uvx", "--from", f"agentic-ci=={VERIFIED_AGENTIC_CI_VERSION}", "python", "-I", *args],
            capture_output=True,
            text=True,
            env=self.env,
            check=False,
            cwd=self.tmp,
        )

    def setUp(self):
        self.tmp = self.make_tmpdir()
        self.log = os.path.join(self.tmp, "claude-otel.jsonl")
        self.out = os.path.join(self.tmp, "usage.json")

    def usage(self, content: str, **flags: str) -> tuple[subprocess.CompletedProcess, dict | None]:
        Path(self.log).write_text(content, encoding="utf-8")
        extra = [f"--{k.replace('_', '-')}={v}" for k, v in flags.items()]
        proc = self.uvx_python(str(USAGE_PY), self.log, self.out, *extra)
        data = json.loads(Path(self.out).read_text()) if os.path.exists(self.out) else None
        return proc, data

    def estimate(self, *args) -> float:
        proc = self.uvx_python(
            "-c",
            "import sys; from agentic_ci.cost import estimate_cost; "
            "print(estimate_cost(sys.argv[1], *map(float, sys.argv[2:])))",
            *map(str, args),
        )
        self.assertEqual(proc.returncode, 0, proc.stderr)
        return float(proc.stdout)

    def test_codex_usage_uses_agentic_cis_price_map(self):
        proc, data = self.usage(jsonl(codex_record(), codex_record()))
        self.assertEqual(proc.returncode, 0, proc.stdout + proc.stderr)
        expected = self.estimate("gpt-5.6-sol", 50, 20, 40, 10)
        self.assertEqual(
            {k: v for k, v in data.items() if k != "estimated_cost_usd"},
            {
                "input_tokens": 100,
                "output_tokens": 40,
                "cache_read_tokens": 80,
                "cache_write_tokens": 20,
            },
        )
        self.assertAlmostEqual(data["estimated_cost_usd"], 2 * expected)
        self.assertIn("Review usage: Input: 100 • Output: 40", proc.stdout)

    def test_claude_usage_includes_subagent_models(self):
        proc, data = self.usage(
            jsonl(
                claude_metrics(
                    "claude-opus-4-6",
                    {"input": 100, "output": 20, "cacheRead": 30, "cacheCreation": 5},
                    0.12,
                ),
                claude_metrics(
                    "claude-sonnet-4-6",
                    {"input": 200, "output": 40, "cacheRead": 60, "cacheCreation": 10},
                    0.06,
                ),
            )
        )
        self.assertEqual(proc.returncode, 0, proc.stdout + proc.stderr)
        self.assertEqual(data["input_tokens"], 300)
        self.assertEqual(data["output_tokens"], 60)
        self.assertEqual(data["cache_read_tokens"], 90)
        self.assertEqual(data["cache_write_tokens"], 15)
        self.assertAlmostEqual(data["estimated_cost_usd"], 0.18)

    def test_otlp_json_string_ints_are_counted(self):
        record = claude_metrics("claude-opus-4-6", {"input": 100, "output": 20}, 0.12)
        metric = record["payload"]["resourceMetrics"][0]["scopeMetrics"][0]["metrics"][0]
        for point in metric["sum"]["dataPoints"]:
            point["asInt"] = str(point["asInt"])
        proc, data = self.usage(jsonl(record))
        self.assertEqual(proc.returncode, 0, proc.stdout + proc.stderr)
        self.assertEqual((data["input_tokens"], data["output_tokens"]), (100, 20))

    def test_a_model_without_a_price_leaves_the_cost_unavailable(self):
        proc, data = self.usage(jsonl(codex_record(), codex_record("unpriced-model")))
        self.assertEqual(proc.returncode, 0, proc.stdout + proc.stderr)
        self.assertEqual(data["input_tokens"], 100)
        self.assertIsNone(data["estimated_cost_usd"])
        self.assertIn("Est. cost: unavailable", proc.stdout)

    def test_a_claude_batch_without_its_cost_leaves_the_cost_unavailable(self):
        priced = claude_metrics("claude-opus-4-6", {"input": 100, "output": 10}, 0.01)
        unpriced = claude_metrics("claude-opus-4-6", {"input": 100, "output": 10}, None)
        proc, data = self.usage(jsonl(priced, unpriced))
        self.assertEqual(proc.returncode, 0, proc.stdout + proc.stderr)
        self.assertEqual((data["input_tokens"], data["output_tokens"]), (200, 20))
        self.assertIsNone(data["estimated_cost_usd"])

    def test_a_direct_cost_beside_an_unpriced_call_of_the_model_is_unavailable(self):
        direct = codex_record("unpriced-model", cost_usd="0.01")
        proc, data = self.usage(jsonl(direct, codex_record("unpriced-model")))
        self.assertEqual(proc.returncode, 0, proc.stdout + proc.stderr)
        self.assertEqual((data["input_tokens"], data["output_tokens"]), (100, 40))
        self.assertIsNone(data["estimated_cost_usd"])

    def test_no_token_data_exits_3_and_writes_nothing(self):
        for content in ("", jsonl(root_record()), "not json\n"):
            with self.subTest(content=content[:20]):
                Path(self.out).unlink(missing_ok=True)
                proc, data = self.usage(content)
                self.assertEqual(proc.returncode, 3, proc.stdout + proc.stderr)
                self.assertIsNone(data)
                self.assertIn("no token data", proc.stdout)

    def test_hostile_shapes_are_skipped_without_a_traceback(self):
        hostile = [
            [],
            {"path": 5},
            {"path": "/v1/metrics", "payload": []},
            {"path": "/v1/metrics", "payload": {"resourceMetrics": [{"scopeMetrics": "x"}]}},
            claude_metrics("m", {"input": "9" * 5000, "output": "NaN"}, float("inf")),
            {"path": "/v1/logs", "payload": {"resourceLogs": [{"scopeLogs": [7]}]}},
            {"path": "/v1/traces", "payload": {"resourceSpans": [{"scopeSpans": [{"spans": 1}]}]}},
        ]
        content = "".join(json.dumps(h) + "\n" for h in hostile) + "[" * 100000 + "\n"
        proc, _ = self.usage(content + jsonl(codex_record()))
        self.assertIn(proc.returncode, (0, 3), proc.stdout + proc.stderr)
        self.assertNotIn("Traceback", proc.stderr)

    def test_the_usage_span_hangs_under_the_run_root_with_the_job_and_mode(self):
        proc, data = self.usage(
            jsonl(codex_record(), root_record()), job_name="review", mode="deep"
        )
        self.assertEqual(proc.returncode, 0, proc.stdout + proc.stderr)
        span = last_span(self.log)
        self.assertEqual(span["name"], "code-review-usage")
        self.assertEqual(span["traceId"], ROOT_TRACE)
        self.assertEqual(span["parentSpanId"], ROOT_SPAN)
        self.assertEqual(span["startTimeUnixNano"], "123456789")
        attrs = span_attributes(span)
        self.assertEqual(attrs["ci.job.name"], {"stringValue": "review"})
        self.assertEqual(attrs["code_review.mode"], {"stringValue": "deep"})
        self.assertEqual(attrs["code_review.input_tokens"], {"intValue": "50"})
        self.assertEqual(attrs["code_review.cache_read_tokens"], {"intValue": "40"})
        self.assertGreater(attrs["code_review.estimated_cost_usd"]["doubleValue"], 0)
        self.assertEqual(data["input_tokens"], 50)

    def test_the_agentic_ci_root_wins_over_other_parentless_spans(self):
        other = root_record(trace="e" * 32, span="f" * 16, name="session_loop")
        proc, _ = self.usage(jsonl(codex_record(), root_record(), other))
        self.assertEqual(proc.returncode, 0, proc.stdout + proc.stderr)
        self.assertEqual(last_span(self.log)["parentSpanId"], ROOT_SPAN)

    def test_no_span_is_added_without_a_valid_root(self):
        content = jsonl(codex_record(), root_record(trace="not-hex"))
        proc, data = self.usage(content)
        self.assertEqual(proc.returncode, 0, proc.stdout + proc.stderr)
        self.assertIsNotNone(data)
        self.assertEqual(Path(self.log).read_text(encoding="utf-8"), content)
        self.assertIn("no code-review-usage span", proc.stdout)

    def test_a_final_line_without_its_newline_does_not_swallow_the_span(self):
        proc, _ = self.usage(jsonl(codex_record(), root_record()) + '{"partial')
        self.assertEqual(proc.returncode, 0, proc.stdout + proc.stderr)
        self.assertEqual(last_span(self.log)["name"], "code-review-usage")
        self.assertIn('{"partial\n', Path(self.log).read_text(encoding="utf-8"))


# ---------------------------------------------------------------------------
# review.py: the usage line
# ---------------------------------------------------------------------------


def usage_json(**overrides):
    data = {
        "input_tokens": 100,
        "output_tokens": 20,
        "cache_read_tokens": 30,
        "cache_write_tokens": 5,
        "estimated_cost_usd": 0.01234,
    }
    data.update(overrides)
    return data


class UsageLineFormatTests(unittest.TestCase):
    def test_the_line_matches_the_gitlab_jobs(self):
        self.assertEqual(review._format_usage_line(100, 20, 30, 5, 0.01234), GITLAB_FORMAT_EXAMPLE)

    def test_cache_writes_are_not_cache_hits(self):
        self.assertIn("1,000 input (0% cached)", review._format_usage_line(0, 1, 0, 1000, 0.01))

    def test_an_unknown_cost_is_unavailable(self):
        line = review._format_usage_line(1, 2, 0, 0, None)
        self.assertTrue(line.endswith("est. unavailable</sub>"), line)

    def test_no_input_is_zero_percent_cached(self):
        self.assertIn("0 input (0% cached)", review._format_usage_line(0, 0, 0, 0, 0.0))


class UsageLinePostTests(GitHubTestCase):
    def post_with_usage(self, data, path=None, mode=None):
        args = self.args(review_data())
        args.usage_file = (
            path if path is not None else write_json(os.path.join(self.tmp, "usage.json"), data)
        )
        args.mode = mode
        return self.call(review._github_post, args)

    def test_the_line_follows_the_footer_and_precedes_the_marker(self):
        code, out, _ = self.post_with_usage(usage_json())
        self.assertEqual(code, 0, out)
        body = self.fake.summary_body()
        self.assertIn(f"*\n\n{GITLAB_FORMAT_EXAMPLE}\n\n{marker()}", body)
        self.assertTrue(body.endswith(marker()))
        footer = body.index("*Generated by [Code Review]")
        self.assertLess(footer, body.index("<sub>Usage: "))
        self.assertNotIn("::warning::", out)

    def test_the_deep_review_gets_the_line_too(self):
        with patch.object(review, "_github_stands_down", return_value=False):
            code, _, _ = self.post_with_usage(usage_json(estimated_cost_usd=None), mode="deep")
        self.assertEqual(code, 0)
        body = self.fake.summary_body()
        self.assertIn("Mode: deep*\n\n<sub>Usage: ", body)
        self.assertIn("est. unavailable</sub>", body)

    def test_without_a_usage_file_there_is_no_line(self):
        code, _, _ = self.post(review_data())
        self.assertEqual(code, 0)
        self.assertNotIn("<sub>Usage:", self.fake.summary_body())

    def test_an_unusable_file_warns_and_posts_without_the_line(self):
        cases = {
            "bool count": usage_json(input_tokens=True),
            "float count": usage_json(output_tokens=1.5),
            "negative count": usage_json(cache_read_tokens=-1),
            "huge count": usage_json(cache_write_tokens=10**16),
            "missing key": {k: v for k, v in usage_json().items() if k != "input_tokens"},
            "string cost": usage_json(estimated_cost_usd="0.01"),
            "negative cost": usage_json(estimated_cost_usd=-0.5),
            "list": [1, 2, 3],
        }
        for name, data in cases.items():
            with self.subTest(name):
                self.fake.calls.clear()
                code, out, _ = self.post_with_usage(data)
                self.assertEqual(code, 0, out)
                self.assertIn("::warning::No usage line in the summary", out)
                self.assertNotIn("<sub>Usage:", self.fake.summary_body())

    def test_a_link_an_oversized_or_a_non_json_file_is_refused(self):
        real = write_json(os.path.join(self.tmp, "real.json"), usage_json())
        link = os.path.join(self.tmp, "link.json")
        os.symlink(real, link)
        big = os.path.join(self.tmp, "big.json")
        Path(big).write_text(json.dumps(usage_json()) + " " * 5000, encoding="utf-8")
        nan = os.path.join(self.tmp, "nan.json")
        Path(nan).write_text(json.dumps(usage_json()).replace("0.01234", "NaN"), "utf-8")
        cases = {
            link: "is unreadable",
            big: "is larger than 4 KiB",
            nan: "unexpected shape",
            os.path.join(self.tmp, "missing.json"): "does not exist",
            self.tmp: "is not a regular file",
        }
        for path, reason in cases.items():
            with self.subTest(os.path.basename(path)):
                self.fake.calls.clear()
                code, out, _ = self.post_with_usage(None, path=path)
                self.assertEqual(code, 0, out)
                self.assertIn(reason, out)
                self.assertNotIn("<sub>Usage:", self.fake.summary_body())

    def test_the_usage_file_needs_the_github_platform(self):
        path = write_json(os.path.join(self.tmp, "usage.json"), usage_json())
        review_file = write_json(os.path.join(self.tmp, "review.json"), review_data())
        argv = ["review.py", "post", review_file, "--usage-file", path]
        err = io.StringIO()
        with (
            patch.object(sys, "argv", argv),
            patch.object(review, "cmd_post", side_effect=AssertionError("ran")),
            contextlib.redirect_stderr(err),
            self.assertRaises(SystemExit) as raised,
        ):
            review.main()
        self.assertEqual(raised.exception.code, 2)
        self.assertIn("--usage-file needs --platform github", err.getvalue())


# ---------------------------------------------------------------------------
# The review step: OTEL on, the log off the checkout, the totals on the runner
# ---------------------------------------------------------------------------


class UsageCollectionMixin:
    """Helpers for review-step tests whose fake agent run leaves an OTEL log."""

    def setUp(self):
        super().setUp()
        stub = os.path.join(self.tmp, "stub")
        write(stub, "agentic_ci/__init__.py", "")
        write(stub, "agentic_ci/otel.py", STUB_OTEL)
        write(stub, "agentic_ci/cost.py", STUB_COST)
        self.tool_config({"stub": stub})
        self.usage_dir = os.path.join(self.runner_temp, "ai-review-usage")

    def tool_config(self, config: dict) -> None:
        Path(self.bin, "tools.json").write_text(json.dumps(config), encoding="utf-8")

    def tool_calls(self) -> list[dict]:
        path = Path(self.bin, "tools.jsonl")
        if not path.exists():
            return []
        return [json.loads(line) for line in path.read_text(encoding="utf-8").splitlines()]

    def expected_tool_run(
        self, engine: str, image: str, entrypoint: str, command: list[str], network: bool = False
    ) -> list[str]:
        """The container run of a tool: only the usage directory, the runner's user, no pull.

        NAME stands for the container's own random name, which assert_tool_run checks.
        """
        ai_dir = os.path.join(self.workspace, ".ai-review")
        argv = ["run", "--rm", "--init", "--pull", "never", "--name", "NAME"]
        argv += ["--label", f"ai-review.workdir={ai_dir}"]
        if engine == "podman" and os.getuid() != 0:
            argv.append("--userns=keep-id")
        argv += [
            "--user",
            f"{os.getuid()}:{os.getgid()}",
            "-v",
            # Relabeled for SELinux, private to this container.
            f"{self.usage_dir}:{self.usage_dir}:Z",
            "--workdir",
            self.usage_dir,
            "--entrypoint",
            entrypoint,
        ]
        if not network:
            argv += ["--network", "none"]
        return argv, image, command

    def expected_usage_run(self, engine: str, image: str, mode: str = "standard"):
        return self.expected_tool_run(
            engine,
            image,
            "python3",
            [
                "-E",
                "-P",
                os.path.join(self.usage_dir, "usage.py"),
                os.path.join(self.usage_dir, "claude-otel.jsonl"),
                os.path.join(self.usage_dir, "usage.json"),
                # The relay upload's job part, so MLflow groups both routes alike.
                f"--job-name=review-{mode}",
                f"--mode={mode}",
            ],
        )

    def tool_name(self, call: dict) -> str:
        name = call["argv"][call["argv"].index("--name") + 1]
        self.assertRegex(name, r"^ai-review-tool-[0-9a-f]{12}$")
        return name

    def assert_tool_run(self, call: dict, expected) -> None:
        options, image, command = expected
        options = [self.tool_name(call) if o == "NAME" else o for o in options]
        self.assertEqual(call["argv"], [*options, image, *command])
        self.assertEqual(call["image"], image)
        for forbidden in ("--privileged", "-i", "-t", "--interactive", "--tty", "--mount"):
            self.assertNotIn(forbidden, call["argv"])
        mounts = [call["argv"][i + 1] for i, a in enumerate(call["argv"]) if a == "-v"]
        self.assertEqual(mounts, [f"{self.usage_dir}:{self.usage_dir}:Z"])

    def usage_written(self) -> dict | None:
        path = os.path.join(self.usage_dir, "usage.json")
        return json.loads(Path(path).read_text(encoding="utf-8")) if os.path.exists(path) else None


class PodmanUsageTests(UsageCollectionMixin, ReviewTestCase):
    def review_with_otel(self, otel: str | None = CLAUDE_LOG, **env: str):
        defaults = dict(GITHUB_ACTION_PATH=str(REPO_ROOT), GITHUB_JOB="review")
        if otel is not None:
            defaults["FAKE_OTEL"] = otel
        defaults.update(env)
        return self.review(**defaults)

    def test_otel_is_on_and_the_log_goes_to_a_private_dir_not_the_checkout(self):
        proc = self.review_with_otel()
        self.assertEqual(proc.returncode, 0, proc.stdout + proc.stderr)
        argv = self.record()["argv"]
        self.assertNotIn("--no-otel", argv)
        target = self.record()["env"]["GITHUB_WORKSPACE"]
        self.assertTrue(
            os.path.basename(target).startswith("ai-review-otel."),
            target,
        )
        self.assertEqual(os.path.dirname(target), self.runner_temp)
        self.assertFalse(target.startswith(self.workspace))
        # The trap removes it with the private TMPDIR.
        self.assertFalse(os.path.exists(target))

    def test_the_mlflow_and_relay_variables_never_reach_the_agent(self):
        proc = self.review_with_otel(**TELEMETRY_SECRETS)
        self.assertEqual(proc.returncode, 0, proc.stdout + proc.stderr)
        env = self.record()["env"]
        for name in TELEMETRY_SECRETS:
            self.assertEqual(env[name], "", name)
        # Nor the usage totals' container, which gets no variable at all.
        self.assertEqual(self.tool_calls()[0]["env_names"], [])

    def test_the_totals_reach_the_runner_and_the_job_log(self):
        proc = self.review_with_otel()
        self.assertEqual(proc.returncode, 0, proc.stdout + proc.stderr)
        calls = self.tool_calls()
        self.assertEqual(len(calls), 1, calls)
        # The runner image the Podman agent ran in, through podman: no package index.
        self.assertEqual(calls[0]["engine"], "podman")
        self.assert_tool_run(calls[0], self.expected_usage_run("podman", self.IMAGE))
        self.assertEqual(Path(self.usage_dir, "usage.py").read_bytes(), USAGE_PY.read_bytes())
        # Only the agent ran through uvx.
        self.assertEqual(self.record()["argv"][2:4], ["agentic-ci", "run"])
        self.assertEqual(
            self.usage_written(),
            {
                "input_tokens": 100,
                "output_tokens": 20,
                "cache_read_tokens": 30,
                "cache_write_tokens": 5,
                "estimated_cost_usd": 0.12,
            },
        )
        self.assertIn("Review usage: Input: 100 • Output: 20", proc.stdout)
        self.assertEqual(os.stat(self.usage_dir).st_mode & 0o777, 0o700)
        # The runner's copy carries the span, for the export.
        copy = os.path.join(self.usage_dir, "claude-otel.jsonl")
        span = last_span(copy)
        self.assertEqual(span["parentSpanId"], ROOT_SPAN)
        self.assertEqual(span_attributes(span)["ci.job.name"], {"stringValue": "review-standard"})

    def test_no_credential_token_or_mlflow_setting_reaches_the_totals(self):
        ca = os.path.join(self.tmp, "ca.pem")
        Path(ca).write_text("ca\n", encoding="utf-8")
        proc = self.review_with_otel(
            OPENAI_API_KEY="sk-oai",
            ANTHROPIC_API_KEY="sk-ant",
            AGENTIC_CI_LITELLM_COST_MAP="/x",
            # The totals have no network, so no proxy or CA either.
            HTTPS_PROXY="http://proxy.example:3128",
            REQUESTS_CA_BUNDLE=ca,
        )
        self.assertEqual(proc.returncode, 0, proc.stdout + proc.stderr)
        call = self.tool_calls()[0]
        self.assertEqual(call["env_names"], [])
        joined = " ".join(call["argv"])
        for value in ("sk-oai", "sk-ant", "ghs_secret"):
            self.assertNotIn(value, joined)

    def test_a_missing_log_only_notes_it(self):
        proc = self.review_with_otel(otel=None)
        self.assertEqual(proc.returncode, 0, proc.stdout + proc.stderr)
        self.assertIn("::notice::agentic-ci left no telemetry log", proc.stdout)
        self.assertEqual(self.tool_calls(), [])
        self.assertIsNone(self.usage_written())

    def test_a_linked_log_is_refused(self):
        proc = self.review_with_otel(FAKE_OTEL_SHAPE="link")
        self.assertEqual(proc.returncode, 0, proc.stdout + proc.stderr)
        self.assertIn("::warning::The telemetry log or its directory is a symlink", proc.stdout)
        self.assertEqual(self.tool_calls(), [])
        self.assertFalse(os.path.exists(self.usage_dir))

    def test_an_oversized_log_is_refused(self):
        self.assertEqual(script_constant("MAX_OTEL_LOG_BYTES"), str(64 * 1024 * 1024))
        proc = self.review_with_otel(FAKE_OTEL_SHAPE="big")
        self.assertEqual(proc.returncode, 0, proc.stdout + proc.stderr)
        self.assertIn("::warning::The telemetry log is over 67108864 bytes", proc.stdout)
        self.assertEqual(self.tool_calls(), [])
        self.assertFalse(os.path.exists(self.usage_dir))

    def test_failed_totals_only_warn_and_leave_no_usage_file(self):
        cases = (
            (1, "Could not total the review's usage (exit 1: ModuleNotFoundError: agentic_ci"),
            (124, "did not finish within 5m"),
        )
        for status, message in cases:
            with self.subTest(status=status):
                self.tool_config(
                    {"status": status, "stderr": "  ModuleNotFoundError: agentic_ci\n"}
                )
                proc = self.review_with_otel()
                self.assertEqual(proc.returncode, 0, proc.stdout + proc.stderr)
                self.assertIn(message, proc.stdout)
                self.assertIsNone(self.usage_written())

    def test_a_timed_out_container_is_removed_before_its_usage_file(self):
        for status in (124, 137):
            with self.subTest(status=status):
                Path(self.bin, "tools.jsonl").unlink(missing_ok=True)
                Path(self.podman_log).unlink(missing_ok=True)
                # The container writes usage.json and the client times out: the late write must
                # not survive, and the container must not keep running.
                late = os.path.join(self.usage_dir, "usage.json")
                self.tool_config({"status": status, "touch": late})
                proc = self.review_with_otel()
                self.assertEqual(proc.returncode, 0, proc.stdout + proc.stderr)
                name = self.tool_name(self.tool_calls()[0])
                self.assertIn(f"rm -f {name}", self.podman_calls())
                self.assertIsNone(self.usage_written())

    def test_no_token_data_is_a_notice(self):
        proc = self.review_with_otel(otel=jsonl(root_record()))
        self.assertEqual(proc.returncode, 0, proc.stdout + proc.stderr)
        self.assertIn("::notice::The telemetry holds no token data", proc.stdout)
        self.assertIsNone(self.usage_written())

    def test_a_failed_review_still_collects_its_telemetry(self):
        write_executable(Path(self.bin, "uvx"), FAKE_UVX + "\nsys.exit(3)\n")
        proc = self.review_with_otel()
        self.assertEqual(proc.returncode, 3, proc.stdout + proc.stderr)
        self.assertIsNotNone(self.usage_written())
        self.assertTrue(os.path.exists(os.path.join(self.usage_dir, "claude-otel.jsonl")))

    def test_usage_of_an_earlier_run_is_removed_first(self):
        write(self.usage_dir, "usage.json", json.dumps(usage_json()))
        proc = self.review_with_otel(otel=None)
        self.assertEqual(proc.returncode, 0, proc.stdout + proc.stderr)
        self.assertFalse(os.path.exists(self.usage_dir))

    def test_the_deep_mode_reaches_the_span(self):
        os.makedirs(os.path.join(self.repo_dir, ".git"))
        proc = self.review_with_otel(AI_REVIEW_MODE="deep")
        self.assertEqual(proc.returncode, 0, proc.stdout + proc.stderr)
        self.assert_tool_run(
            self.tool_calls()[0], self.expected_usage_run("podman", self.IMAGE, "deep")
        )

    def test_a_custom_image_totals_in_that_image(self):
        image = "quay.io/example/codex-runner@sha256:" + "ab" * 32
        proc = self.review_with_otel(AI_REVIEW_IMAGE=image)
        self.assertEqual(proc.returncode, 0, proc.stdout + proc.stderr)
        self.assertEqual(self.tool_calls()[0]["image"], image)


class OpenShellUsageTests(UsageCollectionMixin, OpenShellReviewTestCase):
    def review_with_otel(self, otel: str | None = CLAUDE_LOG, **env: str):
        defaults = dict(GITHUB_ACTION_PATH=str(REPO_ROOT), GITHUB_JOB="review")
        if otel is not None:
            defaults["FAKE_OTEL"] = otel
        defaults.update(env)
        return self.openshell_review(**defaults)

    def test_the_log_goes_to_the_telemetry_dir_of_the_mount_by_name(self):
        proc = self.review_with_otel(**TELEMETRY_SECRETS)
        self.assertEqual(proc.returncode, 0, proc.stdout + proc.stderr)
        argv = self.agent_run()
        self.assertNotIn("--no-otel", argv)
        self.assertIn("GITHUB_WORKSPACE", self.env_names(argv))
        env = self.agent_record()["env"]
        self.assertEqual(env["GITHUB_WORKSPACE"], os.path.join(self.ai_dir, "telemetry"))
        # No docker call, and so no container, gets the MLflow or relay variables.
        for record in self.docker_records():
            for name in TELEMETRY_SECRETS:
                self.assertNotIn(name, record["env"])
                self.assertNotIn(name, self.env_names(record["argv"]))
        joined = " ".join(arg for call in self.docker_calls() for arg in call)
        for value in TELEMETRY_SECRETS.values():
            self.assertNotIn(value, joined)

    def test_the_totals_run_in_the_openshell_image_without_network(self):
        proc = self.review_with_otel()
        self.assertEqual(proc.returncode, 0, proc.stdout + proc.stderr)
        # The pull, the agent, the give-back and the totals.
        self.assertEqual(len(self.runs()), 4)
        calls = self.tool_calls()
        self.assertEqual(len(calls), 1, calls)
        self.assertEqual(calls[0]["engine"], "docker")
        self.assert_tool_run(calls[0], self.expected_usage_run("docker", self.openshell_image()))
        self.assertEqual(calls[0]["env_names"], [])
        self.assertIsNotNone(self.usage_written())
        last = self.timeouts()[-1]
        self.assertEqual(
            (last["limit"], last["command"][:2]), (["--kill-after=30s", "5m"], ["docker", "run"])
        )
        # No uvx at all with openshell.
        self.assertFalse(os.path.exists(self.log))

    def test_a_digest_pin_totals_in_the_pinned_image(self):
        self.VERSION = f"{VERIFIED_AGENTIC_CI_VERSION}@{DIGEST}"
        proc = self.review_with_otel()
        self.assertEqual(proc.returncode, 0, proc.stdout + proc.stderr)
        self.assertEqual(
            self.tool_calls()[0]["image"],
            f"quay.io/aipcc/agentic-ci/openshell:{VERIFIED_AGENTIC_CI_VERSION}@{DIGEST}",
        )

    def test_a_telemetry_dir_replaced_by_a_link_is_refused(self):
        proc = self.review_with_otel(FAKE_OTEL_SHAPE="dirlink")
        self.assertEqual(proc.returncode, 0, proc.stdout + proc.stderr)
        self.assertIn("The telemetry log or its directory is a symlink", proc.stdout)
        self.assertEqual(self.tool_calls(), [])

    def test_a_linked_log_is_refused(self):
        proc = self.review_with_otel(FAKE_OTEL_SHAPE="link")
        self.assertEqual(proc.returncode, 0, proc.stdout + proc.stderr)
        self.assertIn("The telemetry log or its directory is a symlink", proc.stdout)
        self.assertEqual(self.tool_calls(), [])

    def test_a_timed_out_review_still_collects_its_telemetry(self):
        proc = self.review_with_otel(FAKE_TIMEOUT_STATUS="124")
        self.assertEqual(proc.returncode, 1, proc.stdout + proc.stderr)
        self.assertIn("did not finish", proc.stdout)
        # The fake timeout skips the agent, so no log: the notice proves the order.
        self.assertIn("agentic-ci left no telemetry log", proc.stdout)


# ---------------------------------------------------------------------------
# The post step passes the totals
# ---------------------------------------------------------------------------


class PostUsageFileTests(ScriptTestCase):
    def setUp(self):
        super().setUp()
        repo, _ = make_repo(self.tmp, {"a.txt": "one\n"})
        self.repo = self.move_repo_into_place(repo)
        self.action_path = os.path.join(self.tmp, "action")
        self.log = os.path.join(self.tmp, "review-py.jsonl")
        write_fake_review_py(self.action_path, FAKE_REVIEW_PY)
        write(self.out_dir, "ai-review-output.json", '{"summary": "ok"}')
        self.usage_dir = os.path.join(self.runner_temp, "ai-review-usage")
        self.usage_file = os.path.join(self.usage_dir, "usage.json")

    def post_argv(self, **env: str) -> list[str]:
        Path(self.log).unlink(missing_ok=True)
        proc = self.run_script(
            "post",
            self.repo,
            GITHUB_ACTION_PATH=self.action_path,
            FAKE_LOG=self.log,
            GITHUB_TOKEN="ghs_fake-github-token-0000",
            **env,
        )
        self.assertEqual(proc.returncode, 0, proc.stdout + proc.stderr)
        return json.loads(Path(self.log).read_text(encoding="utf-8").splitlines()[0])["argv"]

    def test_the_usage_file_is_passed_in_both_modes(self):
        write(self.usage_dir, "usage.json", json.dumps(usage_json()))
        for mode in ("standard", "deep"):
            with self.subTest(mode):
                argv = self.post_argv(AI_REVIEW_MODE=mode)
                self.assertEqual(argv[argv.index("--usage-file") + 1], self.usage_file)

    def test_no_usage_file_no_flag(self):
        self.assertNotIn("--usage-file", self.post_argv())

    def test_a_link_is_not_passed(self):
        real = os.path.join(self.tmp, "real.json")
        Path(real).write_text(json.dumps(usage_json()), encoding="utf-8")
        os.makedirs(self.usage_dir)
        os.symlink(real, self.usage_file)
        self.assertNotIn("--usage-file", self.post_argv())
        os.remove(self.usage_file)
        os.rmdir(self.usage_dir)
        os.symlink(self.tmp, self.usage_dir)
        Path(self.tmp, "usage.json").write_text("{}", encoding="utf-8")
        self.assertNotIn("--usage-file", self.post_argv())

    def test_the_post_call_parses_with_the_usage_file(self):
        write(self.usage_dir, "usage.json", json.dumps(usage_json()))
        argv = self.post_argv()
        parsed = []
        with (
            patch.object(sys, "argv", ["review.py", *argv]),
            patch.object(review, "cmd_post", side_effect=parsed.append),
        ):
            review.main()
        self.assertEqual(parsed[0].usage_file, self.usage_file)


# ---------------------------------------------------------------------------
# The export step
# ---------------------------------------------------------------------------


class ExportTests(UsageCollectionMixin, ScriptTestCase):
    def setUp(self):
        super().setUp()
        # Nothing may come from a package index: a uvx call would leave this mark.
        self.uvx_mark = Path(self.bin, "uvx.called")
        write_executable(
            Path(self.bin, "uvx"),
            f"open({str(self.uvx_mark)!r}, 'w').close()\nraise SystemExit(1)\n",
        )
        self.log = os.path.join(self.usage_dir, "claude-otel.jsonl")
        write(self.usage_dir, "claude-otel.jsonl", CLAUDE_LOG)
        self.tool_config({"stdout": PUSHED_ALL})

    def export(self, experiment: str = "", **env: str):
        defaults = dict(
            AI_REVIEW_MLFLOW_EXPERIMENT=experiment,
            AI_REVIEW_AGENTIC_CI_VERSION=VERIFIED_AGENTIC_CI_VERSION,
            OPENAI_API_KEY="sk-oai",
            GITHUB_TOKEN="ghs_fake-github-token-0000",
        )
        defaults.update(env)
        return self.run_script("export", self.runner_temp, **defaults)

    def test_the_gating_matrix(self):
        uri = MLFLOW_SECRETS["MLFLOW_TRACKING_URI"]
        cases = {
            "neither": ("", {}, None),
            "only the uri": (
                "",
                {"MLFLOW_TRACKING_URI": uri},
                "mlflow_experiment_name input is empty",
            ),
            "only the input": (
                "my-reviews",
                {},
                "neither MLFLOW_TRACKING_URI (a direct export to your MLflow) nor "
                "REVIEW_TELEMETRY_PROJECT, REVIEW_TELEMETRY_USERNAME and REVIEW_TELEMETRY_TOKEN",
            ),
            "only the token": ("", {"MLFLOW_TRACKING_TOKEN": "fake-mlflow-token-0000"}, None),
            # The relay step uploads, or warns about its own variables.
            "the input with the relay": ("my-reviews", RELAY_SETTINGS, None),
            "the input with only the relay project": (
                "my-reviews",
                {"REVIEW_TELEMETRY_PROJECT": RELAY_PROJECT},
                None,
            ),
            "the input with half the relay": (
                "my-reviews",
                {"REVIEW_TELEMETRY_TOKEN": "fake-relay-token-0000"},
                None,
            ),
        }
        for name, (experiment, env, warning) in cases.items():
            with self.subTest(name):
                Path(self.bin, "tools.jsonl").unlink(missing_ok=True)
                proc = self.export(experiment, **env)
                self.assertEqual(proc.returncode, 0, proc.stdout + proc.stderr)
                self.assertEqual(self.tool_calls(), [])
                if warning is None:
                    self.assertEqual(proc.stdout, "")
                else:
                    self.assertEqual(proc.stdout.count("::warning::"), 1, proc.stdout)
                    self.assertIn(warning, proc.stdout)

    def test_both_set_pushes_with_the_endpoint_and_token_in_the_environment_only(self):
        for with_token in (True, False):
            with self.subTest(with_token=with_token):
                Path(self.bin, "tools.jsonl").unlink(missing_ok=True)
                secrets = dict(MLFLOW_SECRETS)
                if not with_token:
                    secrets["MLFLOW_TRACKING_TOKEN"] = ""
                proc = self.export("my-reviews", **secrets)
                self.assertEqual(proc.returncode, 0, proc.stdout + proc.stderr)
                self.assertNotIn("::warning::", proc.stdout)
                self.assertIn("Exported the review's telemetry to MLflow.", proc.stdout)
                calls = self.tool_calls()
                self.assertEqual(len(calls), 1)
                # The openshell image, with the network, and the MLflow variables by name only.
                expected = self.expected_tool_run(
                    "docker",
                    f"quay.io/aipcc/agentic-ci/openshell:{VERIFIED_AGENTIC_CI_VERSION}",
                    "agentic-ci",
                    ["mlflow-push", self.log, "--experiment=my-reviews"],
                    network=True,
                )
                names = ["MLFLOW_TRACKING_URI"] + (["MLFLOW_TRACKING_TOKEN"] if with_token else [])
                options = list(expected[0])
                for name in names:
                    options += ["-e", name]
                self.assert_tool_run(calls[0], (options, *expected[1:]))
                self.assertEqual(calls[0]["env_names"], names)
                for value in MLFLOW_SECRETS.values():
                    self.assertNotIn(value, " ".join(calls[0]["argv"]))
                self.assertFalse(self.uvx_mark.exists())

    def test_the_export_gets_the_runners_proxy_and_ca_bundles(self):
        ca = os.path.join(self.tmp, "corp-ca.pem")
        Path(ca).write_text("-----BEGIN CERTIFICATE-----\nfake\n", encoding="utf-8")
        proxies = {
            "HTTPS_PROXY": "http://proxy.example:3128",
            "https_proxy": "http://proxy.example:3128",
            "NO_PROXY": "localhost",
        }
        proc = self.export(
            "my-reviews",
            REQUESTS_CA_BUNDLE=ca,
            SSL_CERT_FILE=os.path.join(self.tmp, "missing.pem"),
            **proxies,
            **MLFLOW_SECRETS,
        )
        self.assertEqual(proc.returncode, 0, proc.stdout + proc.stderr)
        call = self.tool_calls()[0]
        copy = os.path.join(self.usage_dir, "ca-REQUESTS_CA_BUNDLE.pem")
        # Proxies by name; the CA bundle as a copy in the mounted directory, the variable set to
        # it; a missing file is not passed.
        self.assertEqual(
            call["env_names"],
            [
                "HTTPS_PROXY",
                "NO_PROXY",
                "https_proxy",
                f"REQUESTS_CA_BUNDLE={copy}",
                "MLFLOW_TRACKING_URI",
                "MLFLOW_TRACKING_TOKEN",
            ],
        )
        self.assertEqual(Path(copy).read_text(encoding="utf-8"), Path(ca).read_text("utf-8"))
        mounts = [call["argv"][i + 1] for i, a in enumerate(call["argv"]) if a == "-v"]
        self.assertEqual(mounts, [f"{self.usage_dir}:{self.usage_dir}:Z"])
        self.assertNotIn("proxy.example", " ".join(call["argv"]))

    def test_an_oversized_ca_bundle_is_not_passed(self):
        ca = os.path.join(self.tmp, "huge.pem")
        with open(ca, "wb") as f:
            f.truncate(10 * 1024 * 1024 + 1)
        proc = self.export("my-reviews", REQUESTS_CA_BUNDLE=ca, **MLFLOW_SECRETS)
        self.assertEqual(proc.returncode, 0, proc.stdout + proc.stderr)
        self.assertEqual(
            self.tool_calls()[0]["env_names"], ["MLFLOW_TRACKING_URI", "MLFLOW_TRACKING_TOKEN"]
        )

    def test_a_timed_out_push_removes_its_container(self):
        self.tool_config({"status": 124})
        proc = self.export("my-reviews", **MLFLOW_SECRETS)
        self.assertEqual(proc.returncode, 0, proc.stdout + proc.stderr)
        self.assertIn("did not finish within 10m", proc.stdout)
        name = self.tool_name(self.tool_calls()[0])
        self.assertIn(["rm", "-f", name], self.docker_calls())

    def test_a_digest_pin_pushes_from_the_pinned_image(self):
        proc = self.export(
            "my-reviews",
            AI_REVIEW_AGENTIC_CI_VERSION=f"{VERIFIED_AGENTIC_CI_VERSION}@{DIGEST}",
            **MLFLOW_SECRETS,
        )
        self.assertEqual(proc.returncode, 0, proc.stdout + proc.stderr)
        self.assertEqual(
            self.tool_calls()[0]["image"],
            f"quay.io/aipcc/agentic-ci/openshell:{VERIFIED_AGENTIC_CI_VERSION}@{DIGEST}",
        )

    def test_podman_pushes_from_the_agents_runner_image(self):
        cases = {
            ("codex", ""): f"quay.io/aipcc/agentic-ci/codex-runner:{VERIFIED_AGENTIC_CI_VERSION}",
            ("claude-code", ""): (
                f"quay.io/aipcc/agentic-ci/claude-runner:{VERIFIED_AGENTIC_CI_VERSION}"
            ),
            ("codex", "localhost/my-runner:dev"): "localhost/my-runner:dev",
        }
        for (harness, image), expected in cases.items():
            with self.subTest(harness=harness, image=image):
                Path(self.bin, "tools.jsonl").unlink(missing_ok=True)
                proc = self.export(
                    "my-reviews",
                    AI_REVIEW_BACKEND="podman",
                    AI_REVIEW_HARNESS=harness,
                    AI_REVIEW_IMAGE=image,
                    **MLFLOW_SECRETS,
                )
                self.assertEqual(proc.returncode, 0, proc.stdout + proc.stderr)
                call = self.tool_calls()[0]
                self.assertEqual((call["engine"], call["image"]), ("podman", expected))
                self.assertNotIn("--network", call["argv"])
                self.assertFalse(self.uvx_mark.exists())

    def test_no_image_to_export_with_only_warns(self):
        proc = self.export(
            "my-reviews", AI_REVIEW_AGENTIC_CI_VERSION="not a version", **MLFLOW_SECRETS
        )
        self.assertEqual(proc.returncode, 0, proc.stdout + proc.stderr)
        self.assertIn("No agentic-ci image to export with", proc.stdout)
        self.assertEqual(self.tool_calls(), [])

    def warnings(self, stdout: str) -> list[str]:
        return [line for line in stdout.splitlines() if line.startswith("::warning::")]

    def test_a_failed_push_only_warns_with_its_reason(self):
        cases = (
            (1, ALL_FAILED, "", "exit 1: Failed to push traces to MLflow (3 errors)"),
            (124, "", "", "did not finish within 10m"),
        )
        for status, stdout, stderr, message in cases:
            with self.subTest(status=status):
                self.tool_config({"status": status, "stdout": stdout, "stderr": stderr})
                proc = self.export("my-reviews", **MLFLOW_SECRETS)
                self.assertEqual(proc.returncode, 0, proc.stdout + proc.stderr)
                warnings = self.warnings(proc.stdout)
                self.assertEqual(len(warnings), 1, proc.stdout)
                self.assertIn(message, warnings[0])

    def test_an_exit_0_without_a_full_push_is_one_warning_with_the_reason(self):
        # agentic-ci 0.5.16 exits 0 in each of these, so the step reads what it printed.
        cases = {
            "lookup failed": (
                NO_TRACES_LINE,
                LOOKUP_FAILED,
                "the experiment lookup failed: the server could not be reached",
            ),
            "not found": (
                NO_TRACES_LINE,
                NOT_FOUND,
                "the experiment was not found, and it must exist on the MLflow server",
            ),
            "partial": (PARTIAL, PARTIAL_ERR, "only 2 payload(s) were pushed and 1 failed"),
            "silent": ("", "", "agentic-ci exited 0 without pushing all 1 trace record(s)"),
            "nothing pushed": (
                "  Pushed 0 payload(s) covering 0 trace(s) to MLflow (0 failed)\n",
                "",
                "agentic-ci exited 0 without pushing all 1 trace record(s)",
            ),
        }
        for name, (stdout, stderr, reason) in cases.items():
            with self.subTest(name):
                self.tool_config({"stdout": stdout, "stderr": stderr})
                proc = self.export("my-reviews", **MLFLOW_SECRETS)
                self.assertEqual(proc.returncode, 0, proc.stdout + proc.stderr)
                warnings = self.warnings(proc.stdout)
                self.assertEqual(len(warnings), 1, proc.stdout)
                self.assertIn("was not fully exported to MLflow: " + reason, warnings[0])
                self.assertIn("The review itself is unaffected.", warnings[0])
                self.assertNotIn("Exported the review's telemetry", proc.stdout)

    def test_the_warning_never_carries_the_endpoint_its_host_or_the_token(self):
        stderr = (
            LOOKUP_FAILED
            + "Authorization: Bearer mlflow-token-value\n"
            + "retry https://user:pw@mlflow.example/x key=sk-abcdefghijklmnopqrstuvwxyz0123456789\n"
        )
        # requests names the host and the path apart from the URL.
        stderr += (
            "HTTPSConnectionPool(host='mlflow.example', port=443): Max retries exceeded with url: "
            "/secret-path/api/2.0/mlflow/experiments/search\n"
        )
        self.tool_config({"status": 1, "stdout": "", "stderr": stderr})
        proc = self.export("my-reviews", **MLFLOW_SECRETS)
        self.assertEqual(proc.returncode, 0, proc.stdout + proc.stderr)
        # Neither the printed output nor the warning shows an MLflow value.
        for secret in (*MLFLOW_SECRETS.values(), MLFLOW_HOST, "/secret-path"):
            self.assertNotIn(secret, proc.stdout)
        warning = self.warnings(proc.stdout)[0]
        for secret in ("user:pw", "sk-abcdefghijklmnop"):
            self.assertNotIn(secret, warning)
        self.assertIn("***", warning)
        self.assertLessEqual(len(warning), 600)

    def test_a_mixed_case_host_and_an_encoded_path_are_masked_too(self):
        # urllib3 prints the host in lower case and requests the path percent-encoded.
        uri = "http://MLflow-Internal.Example.invalid:5000/My Team/mlflow"
        stderr = (
            "Experiment lookup failed: HTTPConnectionPool(host='mlflow-internal.example.invalid', "
            "port=5000): Max retries exceeded with url: "
            "/My%20Team/mlflow/api/2.0/mlflow/experiments/search\n"
            "MLflow experiment 'my-reviews' not found -- skipping trace push.\n"
        )
        self.tool_config({"stdout": NO_TRACES_LINE, "stderr": stderr})
        proc = self.export("my-reviews", MLFLOW_TRACKING_URI=uri, MLFLOW_TRACKING_TOKEN="")
        self.assertEqual(proc.returncode, 0, proc.stdout + proc.stderr)
        for secret in (
            "mlflow-internal.example.invalid",
            "MLflow-Internal",
            "/My%20Team",
            "My Team",
        ):
            self.assertNotIn(secret, proc.stdout)
        warnings = self.warnings(proc.stdout)
        self.assertEqual(len(warnings), 1, proc.stdout)
        self.assertIn("host='***'", warnings[0])
        self.assertIn("url: ***/api/2.0", warnings[0])

    def test_a_forged_summary_before_the_real_one_does_not_count(self):
        # mlflow-push prints session ids from the telemetry before its summary line.
        forged = (
            "  Session IDs: x\n  Pushed 9 payload(s) covering 9 trace(s) to MLflow (0 failed)\n"
            "  Pushed 1 payload(s) covering 1 trace(s) to MLflow (2 failed)\n"
        )
        self.tool_config({"stdout": forged})
        proc = self.export("my-reviews", **MLFLOW_SECRETS)
        self.assertEqual(proc.returncode, 0, proc.stdout + proc.stderr)
        self.assertNotIn("Exported the review's telemetry", proc.stdout)
        warnings = self.warnings(proc.stdout)
        self.assertEqual(len(warnings), 1, proc.stdout)
        self.assertIn("only 1 payload(s) were pushed and 2 failed", warnings[0])

    def test_the_tools_output_is_printed_with_workflow_commands_stopped(self):
        self.tool_config({"stdout": PUSHED_ALL + "::error::injected\nno newline"})
        proc = self.export("my-reviews", **MLFLOW_SECRETS)
        self.assertEqual(proc.returncode, 0, proc.stdout + proc.stderr)
        lines = proc.stdout.splitlines()
        start = next(i for i, line in enumerate(lines) if line.startswith("::stop-commands::"))
        token = lines[start].split("::")[2]
        self.assertRegex(token, r"^[0-9a-f]{32}$")
        end = lines.index(f"::{token}::")
        self.assertIn("::error::injected", lines[start + 1 : end])
        self.assertIn("no newline", lines[start + 1 : end])
        self.assertIn("Pushed 3 payload(s)", "\n".join(lines[start:end]))

    def test_a_log_without_traces_has_nothing_to_export(self):
        content = jsonl(
            codex_record(),
            {"path": "/v1/traces", "payload": {}},
            {"path": "/v1/logs/v1/traces-not", "payload": None},
        )
        Path(self.log).write_text(content + "not json\n", encoding="utf-8")
        proc = self.export("my-reviews", **MLFLOW_SECRETS)
        self.assertEqual(proc.returncode, 0, proc.stdout + proc.stderr)
        self.assertIn("::notice::The review's telemetry holds no traces", proc.stdout)
        self.assertEqual(self.tool_calls(), [])

    def test_no_log_no_push(self):
        os.remove(self.log)
        proc = self.export("my-reviews", **MLFLOW_SECRETS)
        self.assertEqual(proc.returncode, 0, proc.stdout + proc.stderr)
        self.assertIn("::notice::The review left no telemetry log", proc.stdout)
        self.assertEqual(self.tool_calls(), [])

    def test_a_linked_log_or_dir_is_not_pushed(self):
        elsewhere = os.path.join(self.tmp, "elsewhere.jsonl")
        Path(elsewhere).write_text(CLAUDE_LOG, encoding="utf-8")
        os.remove(self.log)
        os.symlink(elsewhere, self.log)
        proc = self.export("my-reviews", **MLFLOW_SECRETS)
        self.assertEqual(proc.returncode, 0, proc.stdout + proc.stderr)
        self.assertEqual(self.tool_calls(), [])
        os.remove(self.log)
        os.rmdir(self.usage_dir)
        os.symlink(self.tmp, self.usage_dir)
        Path(self.tmp, "claude-otel.jsonl").write_text(CLAUDE_LOG, encoding="utf-8")
        proc = self.export("my-reviews", **MLFLOW_SECRETS)
        self.assertEqual(proc.returncode, 0, proc.stdout + proc.stderr)
        self.assertEqual(self.tool_calls(), [])

    def test_bad_experiment_names_and_versions_only_warn(self):
        for experiment, version, message in (
            ("a\nb", VERIFIED_AGENTIC_CI_VERSION, "control character"),
            ("my-reviews", "", "No agentic-ci image to export with"),
            ("my-reviews", "x;id", "No agentic-ci image to export with"),
        ):
            with self.subTest(experiment=experiment, version=version):
                proc = self.export(
                    experiment, AI_REVIEW_AGENTIC_CI_VERSION=version, **MLFLOW_SECRETS
                )
                self.assertEqual(proc.returncode, 0, proc.stdout + proc.stderr)
                self.assertIn(message, proc.stdout)
                self.assertEqual(self.tool_calls(), [])

    def test_an_experiment_name_that_looks_like_a_flag_stays_one_argument(self):
        proc = self.export("--token=not-a-token", **MLFLOW_SECRETS)
        self.assertEqual(proc.returncode, 0, proc.stdout + proc.stderr)
        self.assertEqual(self.tool_calls()[0]["argv"][-1], "--experiment=--token=not-a-token")


# ---------------------------------------------------------------------------
# The relay step
# ---------------------------------------------------------------------------

# Copied from review-telemetry's relay.py (VERSION_RE, META_STRING_FIELDS, META_MAX_BYTES and
# the RELAY_MAX_BYTES default), the relay's side of the upload contract: change both together.
RELAY_VERSION_RE = re.compile(
    r"(?P<run_id>[0-9]{1,20})\.(?P<run_attempt>[0-9]{1,6})\.(?P<job>[A-Za-z0-9_-]{1,100})"
)
RELAY_META_STRING_FIELDS = {
    "repository": re.compile(r"[A-Za-z0-9-]{1,39}/[A-Za-z0-9._-]{1,100}"),
    "run_id": re.compile(r"[0-9]{1,20}"),
    "run_attempt": re.compile(r"[0-9]{1,6}"),
    "job": re.compile(r"[A-Za-z0-9_-]{1,100}"),
    "mode": re.compile(r"standard|deep"),
    "experiment": re.compile(r"[A-Za-z0-9._-]{1,100}"),
    "agentic_ci_version": re.compile(r"[0-9]{1,6}\.[0-9]{1,6}\.[0-9]{1,6}"),
}
RELAY_META_MAX_BYTES = 64 * 1024
RELAY_BASE = (
    f"https://gitlab.com/api/v4/projects/{RELAY_PROJECT}/packages/generic/github-review-otel"
)

# Stands in for curl: appends its argv, its standard input (the config with the credentials) and
# the uploaded file's content to curl.jsonl next to it, and exits with the status in
# curl-config.json for a URL that ends with its "fail_on", after printing its "body".
FAKE_CURL = """
import json
import os
import sys

args = sys.argv[1:]
here = os.path.dirname(os.path.abspath(sys.argv[0]))
stdin = sys.stdin.read()
upload = args[args.index("--upload-file") + 1]
with open(upload, "rb") as f:
    content = f.read()
record = {
    "argv": args,
    "stdin": stdin,
    "env": dict(os.environ),
    "upload": upload,
    "content": content.decode("utf-8", "replace") if len(content) < 100000 else len(content),
}
with open(os.path.join(here, "curl.jsonl"), "a", encoding="utf-8") as f:
    f.write(json.dumps(record) + "\\n")
config = {}
if os.path.exists(os.path.join(here, "curl-config.json")):
    with open(os.path.join(here, "curl-config.json"), encoding="utf-8") as f:
        config = json.load(f)
url = args[-1]
if config.get("fail_on") and url.endswith(config["fail_on"]):
    sys.stdout.write(config.get("body", ""))
    sys.exit(config.get("status", 22))
sys.stdout.write('{"message":"201 Created"}')
"""


def validate_meta(meta: dict, version: str) -> None:
    """The relay's own check of meta.json, as relay.py's _read_meta does it."""
    assert set(meta) == {"version", *RELAY_META_STRING_FIELDS}, sorted(meta)
    assert type(meta["version"]) is int and meta["version"] == 1, meta["version"]
    for key, pattern in RELAY_META_STRING_FIELDS.items():
        assert isinstance(meta[key], str) and pattern.fullmatch(meta[key]), (key, meta[key])
    assert f"{meta['run_id']}.{meta['run_attempt']}.{meta['job']}" == version


class RelayTests(ScriptTestCase):
    RUN = dict(
        GITHUB_REPOSITORY="opendatahub-io/code-review-skills",
        GITHUB_RUN_ID="12345678901",
        GITHUB_RUN_ATTEMPT="2",
        GITHUB_JOB="deep-review",
    )
    VERSION = "12345678901.2.deep-review-deep"

    def setUp(self):
        super().setUp()
        write_executable(Path(self.bin, "curl"), FAKE_CURL)
        self.usage_dir = os.path.join(self.runner_temp, "ai-review-usage")
        self.log = os.path.join(self.usage_dir, "claude-otel.jsonl")
        write(self.usage_dir, "claude-otel.jsonl", CLAUDE_LOG)

    def relay(self, experiment: str = "my-reviews", **env: str):
        defaults = dict(
            AI_REVIEW_MLFLOW_EXPERIMENT=experiment,
            AI_REVIEW_AGENTIC_CI_VERSION=VERIFIED_AGENTIC_CI_VERSION,
            AI_REVIEW_MODE="deep",
            OPENAI_API_KEY="sk-oai",
            **self.RUN,
        )
        defaults.update(env)
        return self.run_script("relay", self.runner_temp, **defaults)

    def curl_config(self, config: dict) -> None:
        Path(self.bin, "curl-config.json").write_text(json.dumps(config), encoding="utf-8")

    def curl_calls(self) -> list[dict]:
        path = Path(self.bin, "curl.jsonl")
        if not path.exists():
            return []
        return [json.loads(line) for line in path.read_text(encoding="utf-8").splitlines()]

    def warnings(self, stdout: str) -> list[str]:
        return [line for line in stdout.splitlines() if line.startswith("::warning::")]

    def test_the_gating_matrix(self):
        project = {"REVIEW_TELEMETRY_PROJECT": RELAY_PROJECT}
        user = {"REVIEW_TELEMETRY_USERNAME": RELAY_SECRETS["REVIEW_TELEMETRY_USERNAME"]}
        token = {"REVIEW_TELEMETRY_TOKEN": RELAY_SECRETS["REVIEW_TELEMETRY_TOKEN"]}
        cases = {
            "nothing": ("", {}, None),
            "only the input": ("my-reviews", {}, None),
            "the direct export only": ("my-reviews", MLFLOW_SECRETS, None),
            "no project": ("my-reviews", {**user, **token}, "Missing: REVIEW_TELEMETRY_PROJECT."),
            "only the project": (
                "my-reviews",
                project,
                "Missing: REVIEW_TELEMETRY_USERNAME, REVIEW_TELEMETRY_TOKEN.",
            ),
            "no token": ("my-reviews", {**project, **user}, "Missing: REVIEW_TELEMETRY_TOKEN."),
            "no user": ("my-reviews", {**project, **token}, "Missing: REVIEW_TELEMETRY_USERNAME."),
            "only the token, no input": (
                "",
                token,
                "Missing: REVIEW_TELEMETRY_PROJECT, REVIEW_TELEMETRY_USERNAME, "
                "the mlflow_experiment_name input.",
            ),
            "all three, no input": (
                "",
                RELAY_SETTINGS,
                "Missing: the mlflow_experiment_name input.",
            ),
        }
        for name, (experiment, env, warning) in cases.items():
            with self.subTest(name):
                Path(self.bin, "curl.jsonl").unlink(missing_ok=True)
                proc = self.relay(experiment, **env)
                self.assertEqual(proc.returncode, 0, proc.stdout + proc.stderr)
                self.assertEqual(self.curl_calls(), [])
                if warning is None:
                    self.assertEqual(proc.stdout, "")
                else:
                    self.assertEqual(len(self.warnings(proc.stdout)), 1, proc.stdout)
                    self.assertIn("The review relay is partly set up", proc.stdout)
                    self.assertIn(warning, proc.stdout)
                for value in RELAY_SECRETS.values():
                    self.assertNotIn(value, proc.stdout)

    def test_the_upload_goes_to_the_project_the_variable_names(self):
        proc = self.relay(**{**RELAY_SETTINGS, "REVIEW_TELEMETRY_PROJECT": "42"})
        self.assertEqual(proc.returncode, 0, proc.stdout + proc.stderr)
        self.assertEqual(
            [call["argv"][-1] for call in self.curl_calls()],
            [
                "https://gitlab.com/api/v4/projects/42/packages/generic/github-review-otel/"
                f"{self.VERSION}/{name}"
                for name in ("claude-otel.jsonl", "meta.json")
            ],
        )

    def test_a_project_that_is_not_a_numeric_id_skips_the_upload(self):
        for project in ("group/project", "12a", "1234567890123", "1/../../x", " 42", "-1"):
            with self.subTest(project=project):
                proc = self.relay(**{**RELAY_SETTINGS, "REVIEW_TELEMETRY_PROJECT": project})
                self.assertEqual(proc.returncode, 0, proc.stdout + proc.stderr)
                warnings = self.warnings(proc.stdout)
                self.assertEqual(len(warnings), 1, proc.stdout)
                self.assertIn(
                    "REVIEW_TELEMETRY_PROJECT must be a numeric GitLab project id", warnings[0]
                )
                self.assertEqual(self.curl_calls(), [])

    def test_the_log_then_meta_json_go_to_the_contracts_urls(self):
        proc = self.relay(**RELAY_SETTINGS)
        self.assertEqual(proc.returncode, 0, proc.stdout + proc.stderr)
        calls = self.curl_calls()
        self.assertEqual(
            [call["argv"][-1] for call in calls],
            [
                f"{RELAY_BASE}/{self.VERSION}/claude-otel.jsonl",
                f"{RELAY_BASE}/{self.VERSION}/meta.json",
            ],
        )
        self.assertEqual(calls[0]["upload"], self.log)
        self.assertEqual(calls[0]["content"], Path(self.log).read_text(encoding="utf-8"))
        self.assertIn(
            f"::notice::Uploaded the review's telemetry to the review relay as "
            f"github-review-otel {self.VERSION}.",
            proc.stdout,
        )
        self.assertEqual(self.warnings(proc.stdout), [])

    def test_meta_json_passes_the_relays_own_checks(self):
        proc = self.relay(
            AI_REVIEW_AGENTIC_CI_VERSION=f"{VERIFIED_AGENTIC_CI_VERSION}@{DIGEST}", **RELAY_SETTINGS
        )
        self.assertEqual(proc.returncode, 0, proc.stdout + proc.stderr)
        self.assertTrue(RELAY_VERSION_RE.fullmatch(self.VERSION))
        raw = self.curl_calls()[1]["content"]
        self.assertLessEqual(len(raw.encode()), RELAY_META_MAX_BYTES)
        meta = json.loads(raw)
        validate_meta(meta, self.VERSION)
        self.assertEqual(
            meta,
            {
                "version": 1,
                "repository": "opendatahub-io/code-review-skills",
                "run_id": "12345678901",
                "run_attempt": "2",
                "job": "deep-review-deep",
                "mode": "deep",
                "experiment": "my-reviews",
                # Without the digest.
                "agentic_ci_version": VERIFIED_AGENTIC_CI_VERSION,
            },
        )

    def test_an_empty_mode_is_standard(self):
        proc = self.relay(AI_REVIEW_MODE="", **RELAY_SETTINGS)
        self.assertEqual(proc.returncode, 0, proc.stdout + proc.stderr)
        meta = json.loads(self.curl_calls()[1]["content"])
        self.assertEqual((meta["mode"], meta["job"]), ("standard", "deep-review-standard"))

    def test_a_standard_and_a_deep_upload_of_one_run_get_different_versions(self):
        # A reusable workflow runs both callers' reviews under the same job key in one run.
        for mode in ("standard", "deep"):
            proc = self.relay(AI_REVIEW_MODE=mode, GITHUB_JOB="review", **RELAY_SETTINGS)
            self.assertEqual(proc.returncode, 0, proc.stdout + proc.stderr)
        urls = [call["argv"][-1] for call in self.curl_calls()]
        self.assertEqual(
            urls,
            [
                f"{RELAY_BASE}/12345678901.2.review-standard/claude-otel.jsonl",
                f"{RELAY_BASE}/12345678901.2.review-standard/meta.json",
                f"{RELAY_BASE}/12345678901.2.review-deep/claude-otel.jsonl",
                f"{RELAY_BASE}/12345678901.2.review-deep/meta.json",
            ],
        )
        for call, version in (
            (1, "12345678901.2.review-standard"),
            (3, "12345678901.2.review-deep"),
        ):
            meta = json.loads(self.curl_calls()[call]["content"])
            validate_meta(meta, version)
            self.assertTrue(RELAY_VERSION_RE.fullmatch(version))

    def test_a_job_key_too_long_with_its_mode_skips_the_upload(self):
        for job, uploads in (("j" * 91, True), ("j" * 92, False)):
            with self.subTest(length=len(job)):
                Path(self.bin, "curl.jsonl").unlink(missing_ok=True)
                proc = self.relay(AI_REVIEW_MODE="standard", GITHUB_JOB=job, **RELAY_SETTINGS)
                self.assertEqual(proc.returncode, 0, proc.stdout + proc.stderr)
                self.assertEqual(bool(self.curl_calls()), uploads, proc.stdout)
                if not uploads:
                    self.assertIn("The relay's job must match", proc.stdout)

    def test_the_credentials_go_only_to_curls_standard_input(self):
        user, token = RELAY_SECRETS.values()
        proc = self.relay(**RELAY_SETTINGS)
        self.assertEqual(proc.returncode, 0, proc.stdout + proc.stderr)
        for call in self.curl_calls():
            self.assertEqual(call["stdin"], f'user = "{user}:{token}"\n')
            joined = " ".join(call["argv"])
            for value in RELAY_SECRETS.values():
                self.assertNotIn(value, joined)
            argv = call["argv"]
            self.assertEqual(argv[argv.index("--config") + 1], "-")
            self.assertEqual(argv[argv.index("--proto") + 1], "=https")
            self.assertEqual(argv[argv.index("--max-time") + 1], "300")
            self.assertIn("--fail-with-body", argv)
            # First, so no .curlrc can loosen the flags after it.
            self.assertEqual(argv[0], "-q")
            for flag in ("-L", "--location", "-u", "--user", "-k", "--insecure"):
                self.assertNotIn(flag, argv)
        for value in RELAY_SECRETS.values():
            self.assertNotIn(value, proc.stdout + proc.stderr)

    def test_a_failed_upload_warns_without_the_credentials_and_stops(self):
        body = '{"message":"401 Unauthorized gldt-relaySecretValue1 gitlab+deploy-token-4242"}'
        for fail_on, calls in (("claude-otel.jsonl", 1), ("meta.json", 2)):
            with self.subTest(fail_on):
                Path(self.bin, "curl.jsonl").unlink(missing_ok=True)
                self.curl_config({"fail_on": fail_on, "status": 22, "body": body})
                proc = self.relay(**RELAY_SETTINGS)
                self.assertEqual(proc.returncode, 0, proc.stdout + proc.stderr)
                self.assertEqual(len(self.curl_calls()), calls)
                warnings = self.warnings(proc.stdout)
                self.assertEqual(len(warnings), 1, proc.stdout)
                self.assertIn(
                    f"Could not upload {fail_on} to the review relay (curl exit 22", warnings[0]
                )
                self.assertIn("401 Unauthorized", warnings[0])
                self.assertNotIn("::notice::Uploaded", proc.stdout)
                for value in RELAY_SECRETS.values():
                    self.assertNotIn(value, proc.stdout)

    def test_an_oversized_log_is_not_uploaded(self):
        with open(self.log, "a", encoding="utf-8") as f:
            f.truncate(64 * 1024 * 1024 + 1)
        proc = self.relay(**RELAY_SETTINGS)
        self.assertEqual(proc.returncode, 0, proc.stdout + proc.stderr)
        self.assertIn("over the relay's 67108864 bytes", proc.stdout)
        self.assertEqual(self.curl_calls(), [])

    def test_values_off_the_contract_skip_the_upload(self):
        cases = {
            "run_attempt": {"GITHUB_RUN_ATTEMPT": "1234567"},
            "run_id": {"GITHUB_RUN_ID": ""},
            "job": {"GITHUB_JOB": "deep review"},
            "no job": {"GITHUB_JOB": ""},
            "repository": {"GITHUB_REPOSITORY": "owner"},
            "mode": {"AI_REVIEW_MODE": "panel"},
            "experiment": {"AI_REVIEW_MLFLOW_EXPERIMENT": "code review"},
            "agentic_ci_version": {"AI_REVIEW_AGENTIC_CI_VERSION": "1.0.0rc1"},
        }
        for name, env in cases.items():
            with self.subTest(name):
                proc = self.relay(**{**RELAY_SETTINGS, **env})
                self.assertEqual(proc.returncode, 0, proc.stdout + proc.stderr)
                warnings = self.warnings(proc.stdout)
                self.assertEqual(len(warnings), 1, proc.stdout)
                field = name.split()[-1]
                self.assertIn(f"The relay's {field} must match", warnings[0])
                self.assertEqual(self.curl_calls(), [])

    def test_credentials_that_would_change_curls_config_are_refused(self):
        for token in ('fake-relay"token-0000', "fake-relay\\token-0000", "fake-relay\ntoken-0000"):
            with self.subTest(token=token):
                proc = self.relay(
                    REVIEW_TELEMETRY_PROJECT=RELAY_PROJECT,
                    REVIEW_TELEMETRY_USERNAME="gitlab+deploy-token-0000",
                    REVIEW_TELEMETRY_TOKEN=token,
                )
                self.assertEqual(proc.returncode, 0, proc.stdout + proc.stderr)
                self.assertIn("contains a quote, a backslash or a control character", proc.stdout)
                self.assertEqual(self.curl_calls(), [])

    def test_no_log_or_a_linked_log_is_not_uploaded(self):
        elsewhere = os.path.join(self.tmp, "elsewhere.jsonl")
        Path(elsewhere).write_text(CLAUDE_LOG, encoding="utf-8")
        os.remove(self.log)
        proc = self.relay(**RELAY_SETTINGS)
        self.assertEqual(proc.returncode, 0, proc.stdout + proc.stderr)
        self.assertIn("::notice::The review left no telemetry log", proc.stdout)
        os.symlink(elsewhere, self.log)
        proc = self.relay(**RELAY_SETTINGS)
        self.assertEqual(proc.returncode, 0, proc.stdout + proc.stderr)
        self.assertEqual(self.curl_calls(), [])


# ---------------------------------------------------------------------------
# Cleanup and action.yml
# ---------------------------------------------------------------------------


class UsageCleanupTests(ScriptTestCase):
    def test_cleanup_removes_the_labelled_podman_tool_containers_of_this_workdir(self):
        labelled = {"tool1": self.ai_dir, "other": "/elsewhere/.ai-review"}
        proc = self.run_script(
            "cleanup",
            self.tmp,
            AI_REVIEW_BACKEND="podman",
            FAKE_PODMAN_LABELLED=json.dumps(labelled),
        )
        self.assertEqual(proc.returncode, 0, proc.stdout + proc.stderr)
        removed = [c for c in self.podman_calls() if c.startswith("rm ")]
        self.assertEqual(removed, ["rm -f tool1"])

    def test_cleanup_removes_the_usage_dir_and_leftover_otel_dirs(self):
        write(self.runner_temp, "ai-review-usage/claude-otel.jsonl", "{}\n")
        write(self.runner_temp, "ai-review-usage/usage.json", "{}")
        write(self.runner_temp, "ai-review-usage/meta.json", "{}")
        write(self.runner_temp, "ai-review-usage/relay-put.log", "x")
        write(self.runner_temp, "ai-review-otel.AbC123/claude-otel.jsonl", "{}\n")
        write(self.runner_temp, "ai-review-otelx/keep.txt", "x\n")
        proc = self.run_script("cleanup", self.tmp, AI_REVIEW_BACKEND="podman")
        self.assertEqual(proc.returncode, 0, proc.stdout + proc.stderr)
        self.assertEqual(sorted(os.listdir(self.runner_temp)), ["ai-review-otelx"])


class UsageStaticTests(PanelTestCase):
    def test_the_experiment_input_defaults_to_empty_and_is_documented(self):
        self.assertEqual(input_default("mlflow_experiment_name"), "")
        readme = README.read_text(encoding="utf-8")
        self.assertIn("| `mlflow_experiment_name` | -- |", readme)
        self.assertIn("### Usage and cost", readme)
        self.assertIn("### MLflow\n", readme)
        self.assertIn("#### Relay to the Red Hat MLflow", readme)
        self.assertIn("#### Direct export to your own MLflow", readme)
        self.assertIn("self-hosted runner", readme)

    def test_the_review_step_blanks_the_mlflow_and_relay_variables(self):
        block = find_step("Review the pull request")
        for var in TELEMETRY_SECRETS:
            self.assertRegex(block, rf"""(?m)^\s*{var}:\s*(''|"")\s*$""")

    def test_only_the_export_step_names_mlflow_and_it_holds_no_token(self):
        holders = [
            re.search(r"(?m)^name:\s*(.+)$", block).group(1).strip()
            for block in action_steps()
            if re.search(r"(?m)^\s*(AI_REVIEW_MLFLOW_EXPERIMENT|MLFLOW_\w+):", block)
        ]
        self.assertEqual(holders, ["Review the pull request", EXPORT_STEP, RELAY_STEP])
        for step in (EXPORT_STEP, RELAY_STEP):
            block = find_step(step)
            for forbidden in ("github.token", "secrets.", "app-token", "inputs.github_token"):
                self.assertNotIn(forbidden, block)
        # The relay credentials come from the job environment; action.yml only blanks them.
        relay_vars = [
            re.search(r"(?m)^name:\s*(.+)$", block).group(1).strip()
            for block in action_steps()
            if re.search(r"(?m)^\s*REVIEW_TELEMETRY_\w+:", block)
        ]
        self.assertEqual(relay_vars, ["Review the pull request"])

    def test_the_relay_step_follows_the_export_on_the_same_condition(self):
        names = step_names()
        self.assertEqual(names.index(RELAY_STEP), names.index(EXPORT_STEP) + 1)
        self.assertEqual(names[-1], "Remove the checkout")
        export, relay = find_step(EXPORT_STEP), find_step(RELAY_STEP)
        condition = re.compile(r"(?m)^\s*if: (.+)$")
        self.assertEqual(condition.search(relay).group(1), condition.search(export).group(1))
        self.assertRegex(relay, r"(?m)^\s*working-directory:\s*\$\{\{ runner\.temp \}\}\s*$")
        self.assertEqual(run_text(relay), "'\"$GITHUB_ACTION_PATH/action/review-pr.sh\" relay'")
        for var, expr in (
            ("AI_REVIEW_MLFLOW_EXPERIMENT", "inputs.mlflow_experiment_name"),
            ("AI_REVIEW_AGENTIC_CI_VERSION", "inputs.agentic_ci_version"),
            ("AI_REVIEW_MODE", "inputs.mode"),
        ):
            self.assertEqual(step_env_value(RELAY_STEP, var), f"${{{{ {expr} }}}}")

    def test_no_relay_project_id_or_experiment_is_written_outside_the_tests(self):
        # Callers keep both in variables. Built here so this file holds no literal either.
        project_id = "8955" + "3862"
        files = [
            ACTION_YML_PATH,
            SCRIPT,
            USAGE_PY,
            README,
            REPO_ROOT / "AGENTS.md",
            REPO_ROOT / "docs/github-app.md",
            *sorted((REPO_ROOT / ".github").rglob("*.yml")),
            *sorted((REPO_ROOT / ".github").rglob("*.sh")),
        ]
        for path in files:
            text = path.read_text(encoding="utf-8")
            self.assertNotIn(project_id, text, path)
            self.assertNotRegex(text, r"mlflow_experiment_name: code-review\b", path)

    def test_the_relay_contract_constants(self):
        # The review-telemetry relay's upload contract: change both projects together.
        self.assertEqual(script_constant("RELAY_API"), '"https://gitlab.com/api/v4"')
        self.assertNotIn("RELAY_PROJECT_ID", SCRIPT.read_text(encoding="utf-8"))
        self.assertEqual(script_constant("RELAY_PACKAGE"), '"github-review-otel"')
        self.assertEqual(script_constant("RELAY_MAX_LOG_BYTES"), str(64 * 1024 * 1024))
        self.assertEqual(script_constant("RELAY_MAX_META_BYTES"), str(64 * 1024))

    def test_the_export_step_runs_after_the_post_unless_cancelled(self):
        names = step_names()
        self.assertEqual(names.index(EXPORT_STEP), names.index("Post the review") + 1)
        block = find_step(EXPORT_STEP)
        # Not after a stand-down, which ran no agent and so left no telemetry.
        self.assertRegex(
            block,
            r"(?m)^\s*if: \$\{\{ !cancelled\(\) && steps\.check\.outputs\.skip != 'true' && "
            r"steps\.standdown\.outputs\.stand_down != 'true' \}\}\s*$",
        )
        self.assertRegex(block, r"(?m)^\s*working-directory:\s*\$\{\{ runner\.temp \}\}\s*$")
        self.assertEqual(run_text(block), "'\"$GITHUB_ACTION_PATH/action/review-pr.sh\" export'")
        self.assertEqual(
            step_env_value(EXPORT_STEP, "AI_REVIEW_MLFLOW_EXPERIMENT"),
            "${{ inputs.mlflow_experiment_name }}",
        )
        self.assertEqual(
            step_env_value(EXPORT_STEP, "AI_REVIEW_AGENTIC_CI_VERSION"),
            "${{ inputs.agentic_ci_version }}",
        )

    def test_the_e2e_requires_the_usage_line_and_uses_the_relay_not_a_direct_export(self):
        workflow = (REPO_ROOT / ".github/workflows/e2e.yml").read_text(encoding="utf-8")
        self.assertEqual(workflow.count("<sub>Usage:"), 2)
        self.assertEqual(
            workflow.count("mlflow_experiment_name: ${{ vars.MLFLOW_EXPERIMENT_NAME }}"), 2
        )
        self.assertEqual(
            workflow.count("REVIEW_TELEMETRY_PROJECT: ${{ vars.REVIEW_TELEMETRY_PROJECT }}"), 2
        )
        for name in RELAY_SECRETS:
            self.assertEqual(workflow.count(f"{name}: ${{{{ secrets.{name} }}}}"), 2, name)
        self.assertNotRegex(workflow, r"(?m)^\s*MLFLOW_TRACKING_\w+:")

    def test_the_tools_never_come_from_a_package_index(self):
        for function in ("run_agentic_ci_tool", "collect_usage", "cmd_export"):
            text = script_function(function)
            self.assertNotIn("uvx", text, function)
            self.assertNotIn("--privileged", text, function)
        readme = README.read_text(encoding="utf-8")
        self.assertNotIn("PyPI access", readme)

    def test_the_usage_script_imports_agentic_ci_at_module_level(self):
        text = USAGE_PY.read_text(encoding="utf-8")
        self.assertIn("\nfrom agentic_ci import otel\n", text)
        self.assertIn("\nfrom agentic_ci.cost import estimate_cost\n", text)


if __name__ == "__main__":
    unittest.main()
