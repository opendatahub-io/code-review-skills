"""Tests for the GitHub stand-down check in review.py: the `stand-down` subcommand and the
re-checks `post --platform github --mode` makes before deleting old threads and before the
summary.

Everything runs in-process against the recording stand-in from test_github_review.py, which
this module extends with the Actions runs and jobs routes. The run and job objects follow
the shape of real API responses; their values are synthetic.
"""

import json
import sys
import unittest
from pathlib import Path
from unittest.mock import patch
from urllib.parse import parse_qs, urlparse

from tests.test_github_review import (
    API,
    GRAPHQL,
    HEAD,
    NEWER,
    OLD,
    REPO_URL,
    FakeGitHub,
    FakeResponse,
    GitHubTestCase,
    comment,
    finding,
    marker,
    requests_stub,
    review,
    review_data,
    thread,
)

TOKEN = "ghs_standdown_token_value"
OWN_RUN = 5001
OWN_RUNNER = "GitHub Actions 7"
# Next links of these lists name the repository by its numeric id.
REPOSITORY_ID_URL = f"{API}/repositories/9001"
RUNS_URL = f"{REPO_URL}/actions/runs"
BLOCKING = (("queued", None), ("in_progress", None), ("completed", "success"))


def pr_ref(number):
    side = {"ref": "feature", "sha": HEAD, "repo": {"id": 9001, "name": "r", "url": REPO_URL}}
    return {
        "id": number,
        "number": number,
        "url": f"{REPO_URL}/pulls/{number}",
        "head": side,
        "base": {**side, "ref": "main", "sha": OLD},
    }


def run(run_id, status, conclusion, *, head=HEAD, prs=(7,)):
    return {
        "id": run_id,
        "name": "AI review",
        "event": "pull_request",
        "status": status,
        "conclusion": conclusion,
        "head_sha": head,
        "head_branch": "feature",
        "run_attempt": 1,
        "path": ".github/workflows/ai-review.yml",
        "head_repository": {"full_name": "o/r"},
        "pull_requests": [pr_ref(n) for n in prs],
    }


def job(name, status, conclusion=None, *, run_id=OWN_RUN, runner=None, head=HEAD, job_id=90002):
    return {
        "id": job_id,
        "run_id": run_id,
        "run_attempt": 1,
        "workflow_name": "AI review",
        "head_branch": "feature",
        "head_sha": head,
        "name": name,
        "status": status,
        "conclusion": conclusion,
        "runner_name": runner,
        "labels": ["ubuntu-latest"],
    }


def own_job():
    """The standard review job running the check."""
    return job("review", "in_progress", run_id=OWN_RUN, runner=OWN_RUNNER, job_id=90001)


def deep(status="queued", conclusion=None, name="deep-review", **kwargs):
    return job(name, status, conclusion, **kwargs)


def runs_fixture():
    """Runs listed for the head, newest first.

    5003 had every job skipped, 5002 was cancelled before it started (no jobs, and fork-like
    empty pull_requests), 5001 is this synchronize run, 4999 belongs to PR 77 from a branch
    with the same head, and 4998 is another head the server should not have returned.
    """
    return [
        run(5003, "completed", "skipped"),
        run(5002, "completed", "cancelled", prs=()),
        run(5001, "in_progress", None),
        run(4999, "completed", "success", prs=(77,)),
        run(4998, "completed", "success", head=OLD, prs=()),
    ]


def jobs_fixture(second=None):
    """Jobs per run id, as pages. Runs 4999 and 4998 hold deep jobs that must never count."""
    jobs = [own_job()] + ([second] if second is not None else [])
    return {
        5003: [[deep("completed", "skipped")]],
        5002: [[]],
        5001: [jobs],
        4999: [[deep("completed", "success", run_id=4999)]],
        4998: [[deep("in_progress", run_id=4998, head=OLD)]],
    }


class ActionsGitHub(FakeGitHub):
    """FakeGitHub that also serves the Actions runs and jobs lists.

    ``jobs`` maps a run id to its pages of jobs, and pages after the first are reached
    through ``/repositories/{id}/...`` next links, as on github.com. ``heads`` lists the
    head each pulls/7 lookup returns in turn, the last one repeating. ``on_write`` runs
    once, before the first DELETE, inline or summary write.
    """

    def __init__(self, *args, runs=None, jobs=None, runs_link="", **kwargs):
        super().__init__(*args, **kwargs)
        self.runs = runs_fixture() if runs is None else runs
        self.jobs = jobs_fixture() if jobs is None else jobs
        self.runs_link = runs_link
        self.actions_response = None  # answers every Actions GET when set
        self.next_host = REPOSITORY_ID_URL
        self.heads = []
        self.on_write = None

    def get(self, url, params=None, **kwargs):
        if "/actions/runs" in url:
            self.calls.append(("GET", url, {"params": params, **kwargs}))
            return self.actions_response or self.actions(url)
        if url == f"{REPO_URL}/pulls/7" and self.heads:
            self.head = self.heads.pop(0) if len(self.heads) > 1 else self.heads[0]
        return super().get(url, params, **kwargs)

    def actions(self, url):
        parsed = urlparse(url)
        path = f"{parsed.scheme}://{parsed.netloc}{parsed.path}"
        if path == RUNS_URL:
            data = {"total_count": len(self.runs), "workflow_runs": self.runs}
            return FakeResponse(200, data, {"Link": self.runs_link} if self.runs_link else {})
        run_id = int(path.rsplit("/", 2)[-2])
        pages = self.jobs.get(run_id, [[]])
        page = int(parse_qs(parsed.query).get("page", ["1"])[0])
        items = pages[page - 1]
        headers = {}
        if page < len(pages):
            next_url = (
                f"{self.next_host}/actions/runs/{run_id}/jobs"
                f"?filter=latest&per_page=100&page={page + 1}"
            )
            headers["Link"] = f'<{next_url}>; rel="next", <{next_url}>; rel="last"'
        return FakeResponse(200, {"total_count": len(items), "jobs": items}, headers)

    def _write(self):
        if self.on_write is not None:
            hook, self.on_write = self.on_write, None
            hook()

    def post(self, url, json=None, **kwargs):
        if url != GRAPHQL:
            self._write()
        return super().post(url, json=json, **kwargs)

    def patch(self, url, json=None, **kwargs):
        self._write()
        return super().patch(url, json=json, **kwargs)

    def delete(self, url, **kwargs):
        self._write()
        return super().delete(url, **kwargs)

    def actions_gets(self):
        return [c[1] for c in self.calls if "/actions/runs" in c[1]]

    def jobs_runs(self):
        """The run ids whose jobs were read, in order."""
        return [int(u.split("?")[0].rsplit("/", 2)[-2]) for u in self.actions_gets()[1:]]

    def writes(self):
        return [c[:2] for c in self.calls if c[0] != "GET" and c[1] != GRAPHQL]

    def summary_writes(self):
        return [w for w in self.writes() if "/issues/" in w[1]]


def warnings(out):
    return [line for line in out.splitlines() if line.startswith("::warning::")]


class StandDownTestCase(GitHubTestCase):
    def setUp(self):
        super().setUp()
        self.fake = ActionsGitHub()

    def env(self, head=HEAD, **overrides):
        values = {"GITHUB_RUN_ID": str(OWN_RUN), "RUNNER_NAME": OWN_RUNNER, "GITHUB_TOKEN": TOKEN}
        values.update(overrides)
        return super().env(head, **values)

    def stand_down(self, mode="standard", deep_job=None, fake=None, env=None, extra=()):
        """Run the stand-down subcommand; return (exit code, stdout, stderr)."""
        fake = fake or self.fake
        argv = ["review.py", "stand-down", "--platform", "github", "--mode", mode, *extra]
        if deep_job is not None:
            argv += ["--deep-job", deep_job]
        with patch.object(sys, "argv", argv):
            code, out, err = self.call(review.main, env=env, fake=fake)
        self.assertEqual(fake.writes(), [], "stand-down never writes")
        self.assertNotIn(TOKEN, out + err)
        return code, out, err

    def with_second(self, second):
        return ActionsGitHub(jobs=jobs_fixture(second))

    def post_mode(self, data=None, mode="standard", deep_job=None, fake=None, posted_flag=None):
        args = self.args(data or review_data([finding()]))
        args.mode = mode
        args.deep_job = deep_job
        args.posted_flag = posted_flag
        code, out, err = self.call(review._github_post, args, fake=fake)
        self.assertNotIn(TOKEN, out + err)
        return code, out, err

    def setup_pr(self, fake):
        """One summary of ours to update and one outdated thread to delete."""
        fake.comment_pages = [([comment(2, f"old\n\n{marker(OLD)}")], "")]
        fake.thread_pages = [([thread(101, path="gone.py", line=3, outdated=True)], False)]
        return fake


class RunSelectionTests(StandDownTestCase):
    def test_the_unfinished_run_is_read_first_and_its_queued_deep_job_blocks(self):
        fake = self.with_second(deep("queued"))
        code, out, _ = self.stand_down(fake=fake)
        self.assertEqual(code, 10)
        self.assertEqual(fake.jobs_runs(), [5001])
        runs_call, jobs_call = [c for c in fake.calls if c[0] == "GET"]
        self.assertEqual(runs_call[1], RUNS_URL)
        self.assertEqual(
            runs_call[2]["params"], {"head_sha": HEAD, "event": "pull_request", "per_page": 100}
        )
        self.assertEqual(jobs_call[1], f"{RUNS_URL}/5001/jobs")
        self.assertEqual(jobs_call[2]["params"], {"filter": "latest", "per_page": 100})
        self.assertIn("::notice::deep-review (queued) is running or has posted for aaaaaaaa", out)

    def test_skipped_runs_other_heads_and_other_pull_requests_are_never_read(self):
        code, out, _ = self.stand_down()
        self.assertEqual(code, 0)
        self.assertEqual(self.fake.jobs_runs(), [5001, 5002])
        self.assertEqual(warnings(out), [])

    def test_a_run_with_an_empty_pull_request_list_still_counts(self):
        fake = ActionsGitHub(jobs={**jobs_fixture(), 5002: [[deep("completed", "success")]]})
        code, _, _ = self.stand_down(fake=fake)
        self.assertEqual(code, 10)
        self.assertEqual(fake.jobs_runs(), [5001, 5002])

    def test_a_run_listing_this_pull_request_among_others_counts(self):
        runs = [run(5001, "in_progress", None, prs=(77, 7))]
        fake = ActionsGitHub(runs=runs, jobs={5001: [[deep("in_progress")]]})
        self.assertEqual(self.stand_down(fake=fake)[0], 10)

    def test_a_deep_job_of_another_head_is_ignored(self):
        fake = self.with_second(deep("in_progress", head=NEWER))
        self.assertEqual(self.stand_down(fake=fake)[0], 0)

    def test_malformed_runs_and_jobs_are_skipped(self):
        bad_runs = [
            "not a run",
            {**run(1, "in_progress", None), "id": True},
            {**run(2, "in_progress", None), "id": "3"},
            {**run(4, "in_progress", None), "event": "pull_request_target"},
        ]
        fake = ActionsGitHub(runs=bad_runs + [run(5001, "in_progress", None)])
        fake.jobs = {5001: [["not a job", {"name": None}, deep("in_progress")]]}
        self.assertEqual(self.stand_down(fake=fake)[0], 10)
        self.assertEqual(fake.jobs_runs(), [5001])

    def test_no_run_for_the_head_proceeds(self):
        fake = ActionsGitHub(runs=[], jobs={})
        code, out, _ = self.stand_down(fake=fake)
        self.assertEqual(code, 0)
        self.assertEqual(fake.actions_gets(), [RUNS_URL])
        self.assertEqual(warnings(out), [])


class StatusTests(StandDownTestCase):
    def test_a_started_or_successful_deep_job_blocks(self):
        for status, conclusion in (
            ("queued", None),
            ("waiting", None),
            ("pending", None),
            ("requested", None),
            ("in_progress", None),
            ("completed", "success"),
        ):
            with self.subTest(status=status):
                fake = self.with_second(deep(status, conclusion))
                code, out, _ = self.stand_down(fake=fake)
                self.assertEqual(code, 10)
                self.assertIn(f"deep-review ({status})", out)

    def test_a_deep_job_that_ended_without_success_does_not_block(self):
        for conclusion in (
            "failure",
            "cancelled",
            "skipped",
            "timed_out",
            "neutral",
            "action_required",
            None,
        ):
            with self.subTest(conclusion=conclusion):
                fake = self.with_second(deep("completed", conclusion))
                code, out, _ = self.stand_down(fake=fake)
                self.assertEqual(code, 0)
                self.assertEqual(warnings(out), [])

    def test_an_unknown_status_does_not_block(self):
        fake = self.with_second(deep("archived"))
        self.assertEqual(self.stand_down(fake=fake)[0], 0)

    def test_no_deep_job_does_not_block(self):
        self.assertEqual(self.stand_down()[0], 0)


class NameMatchTests(StandDownTestCase):
    def test_the_matching_rule(self):
        matches = review._github_deep_job_matches
        for name in (
            "deep-review",
            "deep-review / review",
            "deep-review (codex)",
            "deep-review (codex) / review",
            "deep-review / inner / review",
        ):
            with self.subTest(name=name):
                self.assertTrue(matches(name, "deep-review"))
        for name in (
            "review / review",
            "deep-review-extra",
            "ai / deep-review",
            "Deep review",
            "deep-review (codex",
            "deep-reviewer (codex)",
            "",
            None,
        ):
            with self.subTest(name=name):
                self.assertFalse(matches(name, "deep-review"))
        self.assertTrue(matches("ai / deep-review", "ai / deep-review"))
        self.assertFalse(matches("deep-review", ""))

    def test_reusable_workflow_and_matrix_jobs_block(self):
        for name, status in (
            ("deep-review / review", "in_progress"),
            ("deep-review (codex)", "pending"),
        ):
            with self.subTest(name=name):
                fake = self.with_second(deep(status, name=name))
                self.assertEqual(self.stand_down(fake=fake)[0], 10)

    def test_the_standard_job_of_a_shared_workflow_and_look_alikes_never_block(self):
        for name in ("review / review", "deep-review-extra", "ai / deep-review", "Deep review"):
            with self.subTest(name=name):
                fake = self.with_second(deep("in_progress", name=name))
                self.assertEqual(self.stand_down(fake=fake)[0], 0)

    def test_a_custom_display_name_matches_when_passed(self):
        fake = self.with_second(deep("in_progress", name="Deep review"))
        self.assertEqual(self.stand_down(deep_job="Deep review", fake=fake)[0], 10)
        fake = self.with_second(deep("in_progress", name="Deep review"))
        self.assertEqual(self.stand_down(deep_job="deep-review", fake=fake)[0], 0)


class SelfExclusionTests(StandDownTestCase):
    def test_the_job_running_the_check_is_skipped_even_when_its_name_matches(self):
        mine = deep("in_progress", runner=OWN_RUNNER)
        fake = ActionsGitHub(jobs={**jobs_fixture(), 5001: [[mine]]})
        self.assertEqual(self.stand_down(fake=fake)[0], 0)

    def test_only_the_same_run_and_the_same_runner_count_as_ourselves(self):
        cases = {
            "another runner": ({}, deep("in_progress", runner="GitHub Actions 8"), 5001),
            "another run": ({}, deep("in_progress", runner=OWN_RUNNER), 5002),
            "no RUNNER_NAME": ({"RUNNER_NAME": None}, deep("in_progress", runner=OWN_RUNNER), 5001),
            "no GITHUB_RUN_ID": (
                {"GITHUB_RUN_ID": None},
                deep("in_progress", runner=OWN_RUNNER),
                5001,
            ),
            "no runner on the job": ({"RUNNER_NAME": ""}, deep("queued"), 5001),
        }
        for name, (env, found, run_id) in cases.items():
            with self.subTest(name):
                fake = ActionsGitHub(jobs={**jobs_fixture(), run_id: [[found]]})
                code, _, _ = self.stand_down(fake=fake, env=self.env(**env))
                self.assertEqual(code, 10)


class FinishedJobOnTheSameRunnerTests(StandDownTestCase):
    """A self-hosted runner keeps its name, so it can run the deep job and then this one."""

    def same_runner(self, status="completed", conclusion="success"):
        return self.with_second(deep(status, conclusion, runner=OWN_RUNNER))

    def test_a_deep_job_that_finished_on_this_runner_in_this_run_blocks(self):
        code, out, _ = self.stand_down(fake=self.same_runner())
        self.assertEqual(code, 10)
        self.assertIn("deep-review (completed)", out)

    def test_only_an_in_progress_job_on_this_runner_counts_as_ourselves(self):
        for status, conclusion in BLOCKING:
            with self.subTest(status=status):
                code, _, _ = self.stand_down(fake=self.same_runner(status, conclusion))
                self.assertEqual(code, 0 if status == "in_progress" else 10)

    def test_it_stops_the_post_before_any_write(self):
        fake = self.setup_pr(self.same_runner())
        code, out, _ = self.post_mode(fake=fake)
        self.assertEqual(code, 0)
        self.assertEqual(fake.writes(), [])
        self.assertIn("the standard review stands down", out)

    def test_it_stops_the_summary_when_it_finishes_after_the_cleanup(self):
        fake = self.setup_pr(ActionsGitHub())
        fake.on_write = lambda: fake.jobs[5001][0].append(
            deep("completed", "success", runner=OWN_RUNNER)
        )
        code, out, _ = self.post_mode(fake=fake)
        self.assertEqual(code, 0)
        self.assertEqual(len(fake.of("DELETE")), 1)
        self.assertEqual(fake.summary_writes(), [])
        self.assertIn("the standard review stands down", out)


class PostedFlagTests(StandDownTestCase):
    """post --posted-flag creates the file only once the summary is up."""

    def setUp(self):
        super().setUp()
        self.flag = Path(self.tmp, "posted")

    def test_a_post_that_writes_its_summary_creates_the_flag(self):
        for mode in ("standard", "deep", None):
            with self.subTest(mode=mode):
                self.flag.unlink(missing_ok=True)
                fake = self.setup_pr(ActionsGitHub())
                code, _, _ = self.post_mode(mode=mode, fake=fake, posted_flag=str(self.flag))
                self.assertEqual(code, 0)
                self.assertEqual(len(fake.summary_writes()), 1)
                self.assertTrue(self.flag.is_file())

    def test_a_moved_head_leaves_no_flag(self):
        for mode in ("standard", None):
            with self.subTest(mode=mode):
                fake = self.setup_pr(ActionsGitHub())
                fake.heads = [NEWER]
                code, out, _ = self.post_mode(mode=mode, fake=fake, posted_flag=str(self.flag))
                self.assertEqual(code, 0)
                self.assertEqual(fake.writes(), [])
                self.assertIn("nothing was posted", out)
                self.assertFalse(self.flag.exists())

    def test_a_stand_down_before_the_cleanup_leaves_no_flag(self):
        cases = (
            ("standard", self.with_second(deep("in_progress")), []),
            ("deep", ActionsGitHub(), [HEAD, NEWER]),
        )
        for mode, fake, heads in cases:
            with self.subTest(mode=mode):
                fake = self.setup_pr(fake)
                fake.heads = heads
                code, _, _ = self.post_mode(mode=mode, fake=fake, posted_flag=str(self.flag))
                self.assertEqual(code, 0)
                self.assertEqual(fake.writes(), [])
                self.assertFalse(self.flag.exists())

    def test_a_stand_down_before_the_summary_leaves_no_flag(self):
        standard = self.setup_pr(ActionsGitHub())
        standard.on_write = lambda: standard.jobs[5001][0].append(deep("in_progress"))
        deep_mode = self.setup_pr(ActionsGitHub())
        deep_mode.heads = [HEAD, HEAD, NEWER]
        for mode, fake in (("standard", standard), ("deep", deep_mode)):
            with self.subTest(mode=mode):
                code, _, _ = self.post_mode(mode=mode, fake=fake, posted_flag=str(self.flag))
                self.assertEqual(code, 0)
                self.assertEqual(len(fake.of("DELETE")), 1)
                self.assertEqual(fake.summary_writes(), [])
                self.assertFalse(self.flag.exists())

    def test_a_failed_summary_leaves_no_flag(self):
        class SummaryFails(ActionsGitHub):
            def patch(self, url, json=None, **kwargs):
                super().patch(url, json=json, **kwargs)
                return FakeResponse(500, {})

        fake = self.setup_pr(SummaryFails())
        code, _, _ = self.post_mode(fake=fake, posted_flag=str(self.flag))
        self.assertEqual(code, 1)
        self.assertFalse(self.flag.exists())

    def test_a_flag_that_cannot_be_written_only_warns(self):
        missing = Path(self.tmp, "missing", "posted")
        fake = self.setup_pr(ActionsGitHub())
        code, out, err = self.post_mode(fake=fake, posted_flag=str(missing))
        self.assertEqual(code, 0)
        self.assertEqual(len(fake.summary_writes()), 1)
        self.assertIn("Could not write the posted flag", out + err)
        self.assertFalse(missing.exists())

    def test_the_parser_takes_the_flag_only_with_platform_github(self):
        path = self.args(review_data([finding()])).review_file
        fake = ActionsGitHub()
        with patch.object(sys, "argv", ["review.py", "post", path, "--posted-flag", "x"]):
            code, _, err = self.call(review.main, fake=fake)
        self.assertEqual(code, 2)
        self.assertIn("--posted-flag needs --platform github", err)
        self.assertEqual(fake.calls, [])

        fake = self.setup_pr(ActionsGitHub())
        argv = ["review.py", "post", path, "--platform", "github", "--mode", "standard"]
        argv += ["--posted-flag", str(self.flag)]
        with patch.object(sys, "argv", argv):
            code, _, _ = self.call(review.main, fake=fake)
        self.assertEqual(code, 0)
        self.assertTrue(self.flag.is_file())


class PaginationTests(StandDownTestCase):
    def test_a_second_jobs_page_on_the_repository_id_path_is_read(self):
        fake = ActionsGitHub(jobs={**jobs_fixture(), 5001: [[own_job()], [deep("in_progress")]]})
        code, _, _ = self.stand_down(fake=fake)
        self.assertEqual(code, 10)
        page_two = fake.calls[-1]
        self.assertEqual(
            page_two[1],
            f"{REPOSITORY_ID_URL}/actions/runs/5001/jobs?filter=latest&per_page=100&page=2",
        )
        self.assertIsNone(page_two[2]["params"])

    def test_a_third_jobs_page_is_never_read(self):
        pages = [[own_job()], [], [deep("in_progress")]]
        fake = ActionsGitHub(jobs={**jobs_fixture(), 5001: pages})
        code, out, _ = self.stand_down(fake=fake)
        self.assertEqual(code, 0)
        self.assertEqual(fake.jobs_runs(), [5001, 5001, 5002])
        self.assertEqual(len(warnings(out)), 1)
        self.assertIn("more jobs", out)

    def test_more_than_a_page_of_runs_warns_and_checks_the_newest(self):
        link = f'<{REPOSITORY_ID_URL}/actions/runs?head_sha={HEAD}&page=2>; rel="next"'
        fake = ActionsGitHub(jobs=jobs_fixture(deep("in_progress")), runs_link=link)
        code, out, _ = self.stand_down(fake=fake)
        self.assertEqual(code, 10)
        self.assertNotIn("page=2", " ".join(fake.actions_gets()))
        self.assertIn("checked the newest 5", out)

    def test_a_next_link_off_the_api_host_fails_open(self):
        for host in ("https://evil.example", "http://api.github.com"):
            with self.subTest(host=host):
                fake = ActionsGitHub(jobs={**jobs_fixture(), 5001: [[], [deep("in_progress")]]})
                fake.next_host = f"{host}/repositories/9001"
                code, out, _ = self.stand_down(fake=fake)
                self.assertEqual(code, 0)
                self.assertFalse(any(u.startswith(host) for u in fake.actions_gets()))
                self.assertEqual(len(warnings(out)), 1)

    def test_the_check_stops_after_twenty_calls(self):
        runs = [run(6000 + i, "in_progress", None) for i in range(30)]
        fake = ActionsGitHub(runs=runs, jobs={6025: [[deep("in_progress")]]})
        code, out, _ = self.stand_down(fake=fake)
        self.assertEqual(code, 0)
        self.assertEqual(len(fake.actions_gets()), 20)
        self.assertEqual(len(warnings(out)), 1)
        self.assertIn("stopped after 20 API calls", out)


class FailOpenTests(StandDownTestCase):
    def forbidden(self):
        body = {
            "message": "Resource not accessible by integration",
            "documentation_url": "https://docs.github.com/rest/actions/workflow-runs",
            "status": "403",
        }
        return FakeResponse(403, body, {"X-Accepted-GitHub-Permissions": "actions=read"})

    def test_a_403_warns_once_naming_actions_read_and_proceeds(self):
        fake = self.with_second(deep("in_progress"))
        fake.actions_response = self.forbidden()
        code, out, err = self.stand_down(fake=fake)
        self.assertEqual(code, 0)
        self.assertEqual(len(warnings(out)), 1)
        self.assertIn("HTTP 403", out)
        self.assertIn("actions: read", out)
        self.assertNotIn("Resource not accessible", out + err)

    def test_every_lookup_failure_proceeds_with_one_warning(self):
        def raises(*args, **kwargs):
            raise requests_stub.exceptions.ConnectionError("detail of the failure")

        cases = {
            "server error": FakeResponse(500, {}),
            "redirect": FakeResponse(302, {}, {"Location": "https://evil.example/"}),
            "not json": FakeResponse(200, None),
            "no list": FakeResponse(200, {"total_count": 0}),
            "not a list": FakeResponse(200, {"workflow_runs": {}, "jobs": {}}),
        }
        for name, response in cases.items():
            with self.subTest(name):
                fake = self.with_second(deep("in_progress"))
                fake.actions_response = response
                code, out, _ = self.stand_down(fake=fake)
                self.assertEqual(code, 0)
                self.assertEqual(len(warnings(out)), 1)
                # The permission hint is only for a refused lookup.
                self.assertNotIn("actions: read", out)
                self.assertIn("the lookup failed", out)
        fake = self.with_second(deep("in_progress"))
        fake.actions_response = FakeResponse(404, {})
        code, out, _ = self.stand_down(fake=fake)
        self.assertEqual(code, 0)
        self.assertIn("actions: read", out)
        fake = self.with_second(deep("in_progress"))
        fake.actions = raises
        code, out, _ = self.stand_down(fake=fake)
        self.assertEqual(code, 0)
        self.assertIn("ConnectionError", out)
        self.assertNotIn("detail of the failure", out)

    def test_a_job_status_that_is_not_a_string_warns_once_and_proceeds(self):
        for status in (["in_progress"], {"name": "queued"}, 3):
            with self.subTest(status=status):
                fake = self.with_second(deep(status))
                code, out, err = self.stand_down(fake=fake)
                self.assertEqual(code, 0)
                self.assertEqual(len(warnings(out)), 1)
                self.assertIn("unexpected job status", out)
                self.assertNotIn("Traceback", err)

    def test_a_job_status_that_is_not_a_string_still_lets_the_post_go_ahead(self):
        fake = self.setup_pr(self.with_second(deep(["in_progress"])))
        code, out, _ = self.post_mode(fake=fake)
        self.assertEqual(code, 0)
        self.assertEqual(len(fake.summary_writes()), 1)
        self.assertEqual(len(warnings(out)), 2)

    def test_a_failing_jobs_list_after_the_runs_list_still_proceeds(self):
        fake = self.with_second(deep("in_progress"))
        original = fake.actions
        fake.actions = lambda url: (
            original(url) if url.startswith(f"{RUNS_URL}?") or url == RUNS_URL else self.forbidden()
        )
        code, out, _ = self.stand_down(fake=fake)
        self.assertEqual(code, 0)
        self.assertEqual(len(warnings(out)), 1)


class BadJson(FakeResponse):
    def json(self):
        raise ValueError("bad json")


class DeepModeTests(StandDownTestCase):
    def test_a_moved_head_stands_down_reading_only_the_pull_request(self):
        fake = self.with_second(deep("in_progress"))
        fake.head = NEWER
        code, out, _ = self.stand_down("deep", fake=fake)
        self.assertEqual(code, 10)
        self.assertEqual([c[:2] for c in fake.calls], [("GET", f"{REPO_URL}/pulls/7")])
        self.assertIn("::notice::PR #7 moved to cccccccc", out)

    def test_the_same_head_proceeds_and_ignores_deep_jobs(self):
        fake = self.with_second(deep("in_progress"))
        code, out, _ = self.stand_down("deep", fake=fake)
        self.assertEqual(code, 0)
        self.assertEqual(fake.actions_gets(), [])
        self.assertEqual(warnings(out), [])

    def test_a_failed_pull_request_lookup_proceeds_with_one_warning(self):
        cases = {
            "403": FakeResponse(403, {}),
            "no head": FakeResponse(200, {"head": {}}),
            "short sha": FakeResponse(200, {"head": {"sha": "abc"}}),
            "head not an object": FakeResponse(200, {"head": "x"}),
            "not json": BadJson(200),
        }
        for name, response in cases.items():
            with self.subTest(name):
                fake = ActionsGitHub()
                fake.get = lambda url, params=None, _r=response, **kw: _r
                code, out, _ = self.stand_down("deep", fake=fake)
                self.assertEqual(code, 0)
                self.assertEqual(len(warnings(out)), 1)

    def test_post_writes_nothing_when_the_head_moves_before_the_cleanup(self):
        fake = self.setup_pr(ActionsGitHub())
        fake.heads = [HEAD, NEWER]
        code, out, _ = self.post_mode(mode="deep", fake=fake)
        self.assertEqual(code, 0)
        self.assertEqual(fake.writes(), [])
        self.assertEqual(fake.of("POST", "/graphql"), [])
        self.assertEqual(fake.actions_gets(), [])
        self.assertIn("the deep review stands down", out)

    def test_post_skips_the_summary_when_the_head_moves_after_the_cleanup(self):
        fake = self.setup_pr(ActionsGitHub())
        fake.heads = [HEAD, HEAD, NEWER]
        code, _, _ = self.post_mode(mode="deep", fake=fake)
        self.assertEqual(code, 0)
        self.assertEqual(len(fake.of("DELETE")), 1)
        self.assertEqual(len(fake.inline_payloads()), 1)
        self.assertEqual(fake.summary_writes(), [])

    def test_post_with_an_unmoved_head_posts_everything(self):
        fake = self.setup_pr(ActionsGitHub())
        code, _, _ = self.post_mode(mode="deep", fake=fake)
        self.assertEqual(code, 0)
        self.assertEqual(len(fake.of("GET", "/pulls/7")), 3)
        self.assertEqual(len(fake.summary_writes()), 1)


class PostCheckPointTests(StandDownTestCase):
    def kinds(self, fake):
        names = {
            ("GET", f"{REPO_URL}/pulls/7"): "pr",
            ("GET", f"{REPO_URL}/issues/7/comments"): "list-comments",
            ("GET", RUNS_URL): "runs",
            ("GET", f"{RUNS_URL}/5001/jobs"): "jobs",
            ("GET", f"{RUNS_URL}/5002/jobs"): "jobs",
            ("POST", GRAPHQL): "threads",
            ("POST", f"{REPO_URL}/pulls/7/comments"): "inline",
            ("PATCH", f"{REPO_URL}/issues/comments/2"): "summary-patch",
            ("POST", f"{REPO_URL}/issues/7/comments"): "summary-post",
            ("DELETE", f"{REPO_URL}/pulls/comments/101"): "delete",
        }
        return [names[c[:2]] for c in fake.calls]

    def test_without_mode_the_calls_match_a_plain_post(self):
        baseline = self.setup_pr(FakeGitHub())
        self.assertEqual(
            self.call(review._github_post, self.args(review_data([finding()])), fake=baseline)[0], 0
        )
        fake = self.setup_pr(self.with_second(deep("in_progress")))
        code, _, _ = self.call(review._github_post, self.args(review_data([finding()])), fake=fake)
        self.assertEqual(code, 0)
        self.assertEqual(fake.calls, baseline.calls)
        self.assertEqual(fake.actions_gets(), [])

    def test_the_lookups_run_before_the_first_delete_and_again_before_the_summary(self):
        fake = self.setup_pr(ActionsGitHub())
        code, _, _ = self.post_mode(fake=fake)
        self.assertEqual(code, 0)
        lookup = ["runs", "jobs", "jobs"]
        self.assertEqual(
            self.kinds(fake),
            [
                "pr",
                "list-comments",
                *lookup,
                "threads",
                "delete",
                "inline",
                *lookup,
                "summary-patch",
            ],
        )

    def test_the_lookups_also_precede_a_first_summary_post(self):
        fake = ActionsGitHub()
        code, _, _ = self.post_mode(fake=fake)
        self.assertEqual(code, 0)
        kinds = self.kinds(fake)
        self.assertEqual(kinds[-4:], ["runs", "jobs", "jobs", "summary-post"])

    def test_a_blocking_deep_job_stops_the_post_before_any_write(self):
        for status, conclusion in BLOCKING:
            with self.subTest(status=status):
                fake = self.setup_pr(self.with_second(deep(status, conclusion)))
                code, out, _ = self.post_mode(fake=fake)
                self.assertEqual(code, 0)
                self.assertEqual(fake.writes(), [])
                self.assertEqual(self.kinds(fake), ["pr", "list-comments", "runs", "jobs"])
                self.assertIn("the standard review stands down", out)

    def test_a_deep_job_that_appears_after_the_cleanup_stops_the_summary(self):
        for status, conclusion in BLOCKING:
            with self.subTest(status=status):
                fake = self.setup_pr(ActionsGitHub())
                fake.on_write = lambda f=fake, s=status, c=conclusion: f.jobs[5001][0].append(
                    deep(s, c)
                )
                code, out, _ = self.post_mode(fake=fake)
                self.assertEqual(code, 0)
                self.assertEqual(
                    self.kinds(fake),
                    [
                        "pr",
                        "list-comments",
                        "runs",
                        "jobs",
                        "jobs",
                        "threads",
                        "delete",
                        "inline",
                        "runs",
                        "jobs",
                    ],
                )
                self.assertEqual(fake.summary_writes(), [])
                self.assertIn(f"deep-review ({status})", out)

    def test_a_deep_job_that_did_not_succeed_lets_the_review_post(self):
        for second in (deep("completed", "failure"), deep("completed", "cancelled"), None):
            with self.subTest(second=second and second["conclusion"]):
                fake = self.setup_pr(self.with_second(second))
                code, _, _ = self.post_mode(fake=fake)
                self.assertEqual(code, 0)
                self.assertEqual(len(fake.of("DELETE")), 1)
                self.assertEqual(len(fake.inline_payloads()), 1)
                self.assertEqual(len(fake.summary_writes()), 1)

    def test_a_403_at_both_check_points_still_posts(self):
        fake = self.setup_pr(self.with_second(deep("in_progress")))
        fake.actions_response = FakeResponse(403, {})
        code, out, _ = self.post_mode(fake=fake)
        self.assertEqual(code, 0)
        self.assertEqual(len(fake.summary_writes()), 1)
        self.assertEqual(len(warnings(out)), 2)
        self.assertTrue(all("actions: read" in w for w in warnings(out)))

    def test_the_deep_job_name_reaches_the_post_time_checks(self):
        fake = self.setup_pr(self.with_second(deep("in_progress", name="Deep review")))
        code, _, _ = self.post_mode(deep_job="Deep review", fake=fake)
        self.assertEqual(code, 0)
        self.assertEqual(fake.writes(), [])

    def test_a_bad_deep_job_name_exits_1_before_any_request(self):
        fake = ActionsGitHub()
        code, _, _ = self.post_mode(deep_job="", fake=fake)
        self.assertEqual(code, 1)
        self.assertEqual(fake.calls, [])


class CliTests(StandDownTestCase):
    def test_exit_codes(self):
        self.assertEqual(self.stand_down(fake=self.with_second(deep("queued")))[0], 10)
        self.assertEqual(self.stand_down(fake=ActionsGitHub())[0], 0)
        fake = ActionsGitHub()
        self.assertEqual(self.stand_down(fake=fake, env=self.env(GITHUB_TOKEN=""))[0], 1)
        self.assertEqual(fake.calls, [])

    def test_a_bad_deep_job_name_exits_1_before_any_request(self):
        for value in ("", "x" * 201, "deep\nreview", "deep\x1breview"):
            with self.subTest(value=value):
                fake = ActionsGitHub()
                code, _, _ = self.stand_down(deep_job=value, fake=fake)
                self.assertEqual(code, 1)
                self.assertEqual(fake.calls, [])
        self.assertEqual(self.stand_down(deep_job="x" * 200, fake=ActionsGitHub())[0], 0)

    def test_the_parser_needs_github_and_a_known_mode(self):
        for argv in (
            ["review.py", "stand-down", "--mode", "standard"],
            ["review.py", "stand-down", "--platform", "github"],
            ["review.py", "stand-down", "--platform", "gitlab", "--mode", "standard"],
            ["review.py", "stand-down", "--platform", "github", "--mode", "quick"],
        ):
            with self.subTest(argv=argv[2:]):
                fake = ActionsGitHub()
                with patch.object(sys, "argv", argv):
                    code, _, _ = self.call(review.main, fake=fake)
                self.assertEqual(code, 2)
                self.assertEqual(fake.calls, [])

    def test_post_rejects_the_new_flags_without_platform_github(self):
        path = self.args(review_data()).review_file
        for flags in (["--mode", "standard"], ["--mode", "deep"], ["--deep-job", "deep-review"]):
            with self.subTest(flags=flags):
                fake = ActionsGitHub()
                with patch.object(sys, "argv", ["review.py", "post", path, *flags]):
                    code, _, err = self.call(review.main, fake=fake)
                self.assertEqual(code, 2)
                self.assertIn("needs --platform github", err)
                self.assertEqual(fake.calls, [])

    def test_post_passes_the_new_flags_through(self):
        path = self.args(review_data([finding()])).review_file
        fake = self.setup_pr(self.with_second(deep("in_progress", name="Deep review")))
        argv = ["review.py", "post", path, "--platform", "github"]
        argv += ["--mode", "standard", "--deep-job", "Deep review"]
        with patch.object(sys, "argv", argv):
            code, out, _ = self.call(review.main, fake=fake)
        self.assertEqual(code, 0)
        self.assertEqual(fake.writes(), [])
        self.assertIn("the standard review stands down", out)

    def test_the_deep_job_defaults_to_deep_review(self):
        self.assertEqual(self.stand_down(fake=self.with_second(deep("queued")))[0], 10)


class ContextTests(StandDownTestCase):
    def load(self, pull_request):
        event = Path(self.tmp) / "context-event.json"
        event.write_text(json.dumps({"pull_request": pull_request}))
        with patch.dict("os.environ", self.env(GITHUB_EVENT_PATH=str(event)), clear=True):
            return review._github_load_context()

    def test_base_sha_and_author_come_from_the_event(self):
        base = {"number": 7, "head": {"sha": HEAD}}
        for user, expected in (
            ({"login": "alice", "type": "User"}, "alice"),
            ({"login": "review-app[bot]", "type": "Bot"}, "review-app[bot]"),
        ):
            with self.subTest(login=expected):
                ctx = self.load({**base, "base": {"sha": OLD}, "user": user})
                self.assertEqual(ctx.base_sha, OLD)
                self.assertEqual(ctx.author_login, expected)

    def test_missing_or_malformed_base_and_author_are_none(self):
        head = {"number": 7, "head": {"sha": HEAD}}
        for name, extra in {
            "missing": {},
            "not objects": {"base": "main", "user": "alice"},
            "short sha": {"base": {"sha": "abc"}, "user": {"login": "-alice"}},
            "uppercase sha": {"base": {"sha": OLD.upper()}, "user": {"login": "a\nb"}},
            "wrong types": {"base": {"sha": 1}, "user": {"login": ["alice"]}},
            "workflow command": {"base": {}, "user": {"login": "::error::x"}},
        }.items():
            with self.subTest(name):
                ctx = self.load({**head, **extra})
                self.assertIsNone(ctx.base_sha)
                self.assertIsNone(ctx.author_login)


if __name__ == "__main__":
    unittest.main()
