"""End-to-end tests for `panel.py report`, its status lines, GitLab JSON and known findings file."""

import hashlib
import json
import os
import re
import shutil
import subprocess
import tempfile
import unittest
from dataclasses import dataclass

from tests.helpers import (
    HAVE_UV,
    REPO_ROOT,
    PanelTestCase,
    commit,
    git,
    hermetic_env,
    load_json,
    make_repo,
    new_tmpdir,
    rev_parse,
    review_py_display,
    run_panel,
    triage_json,
    write_json,
)

BASE_A = "".join(f"line{i}\n" for i in range(1, 30))
A = "CR-516ea425"
SECURITY_ROW = {
    "lens": "security",
    "file": "src/db.py",
    "line": 42,
    "severity": "HIGH",
    "category": "sql_injection",
    "description": "The term is formatted into SQL. More text.",
    "exploit_scenario": "q=' OR 1=1",
    "recommendation": "Bind it.",
    "confidence": 0.95,
    "instances": ["src/admin.py:17"],
    "pre_existing": False,
}
KNOWN_ITEM = {
    "id": A,
    "lens": "correctness",
    "class": "off-by-one",
    "file": "src/a.py",
    "line": 3,
    "title": "Off by one",
    "severity": "blocking",
    "status": "posted",
    "last_seen_round": 1,
}
KNOWN = {"version": 1, "round": 1, "rounds_with_open_findings": 1, "findings": [KNOWN_ITEM]}


def finding_id(lens: str, cls: str, file: str, line: int | None = None) -> str:
    """The stable id from references/report.md, with the line when kept apart from a known id."""
    key = f"{lens}\0{cls}\0{file}" + ("" if line is None else f"\0{line}")
    return "CR-" + hashlib.sha256(key.encode()).hexdigest()[:8]


S = finding_id("security", "sql-injection", "src/db.py")
FINDER_MD = REPO_ROOT / "skills/security-diff-review/references/finder.md"


def finder_example() -> dict:
    """The finding in security-diff-review's finder.md, the shape the security finder returns."""
    text = FINDER_MD.read_text(encoding="utf-8")
    block = re.search(r"^```json\n(.*?)^```", text, re.M | re.S)
    return json.loads(block.group(1))["findings"][0]


G1 = finding_id("correctness", "added-bug", "src/a.py")
G2 = finding_id("correctness", "old-line-bug", "src/a.py")
NEW_BUG = finding_id("correctness", "new-bug", "src/a.py")


def row(lens="correctness", cls="off-by-one", sev="blocking", file="src/a.py", line=3, **kw):
    """A common finding row, as a finder returns it plus its lens."""
    finding = {
        "lens": lens,
        "class": cls,
        "title": f"{cls} in {file}",
        "severity": sev,
        "file": file,
        "line": line,
        "evidence": "evidence",
        "fix": "fix it",
        "instances": [],
        "pre_existing": False,
    }
    finding.update(kw)
    return finding


def findings_doc(rows: list, **kw) -> dict:
    doc = {"version": 1, "independent": True, "candidates": len(rows), "findings": rows}
    doc.update(kw)
    return doc


def verdict(fid: str, skeptic: str, **kw) -> dict:
    return dict({"id": fid, "skeptic": skeptic, "reason": "r"}, **kw)


def upheld(fid: str) -> list[dict]:
    return [verdict(fid, "facts", refuted=False), verdict(fid, "impact", refuted=False)]


def parse_status(stdout: str) -> dict[str, str]:
    lines = [ln.split("\t") for ln in stdout.splitlines() if ln.startswith("CR-")]
    return {ln[0]: ln[1] for ln in lines}


def fold_buckets(text: str) -> tuple[str, int, dict[str, int]]:
    """The fold's "- Kept k of n: ..." line, n, and its buckets by label."""
    line = next((ln for ln in text.splitlines() if ln.startswith("- Kept ")), "")
    head, _, tail = line.partition(": ")
    n = int(head.split(" of ")[1].split()[0]) if " of " in head else -1
    buckets = {}
    for part in tail.split(", "):
        num, _, label = part.partition(" ")
        if num.isdigit():
            buckets[label] = int(num)
    return line, n, buckets


@dataclass(frozen=True)
class Plan:
    repo: str
    base: str
    path: str
    data: dict


def report_repo(parent: str, files: dict, base_files: dict | None = None) -> tuple[str, str]:
    """base_files (default: a 29-line src/a.py) committed on main, then files in a second commit."""
    repo, base = make_repo(parent, base_files or {"src/a.py": BASE_A})
    commit(repo, files)
    return repo, base


def make_plan(workdir: str, repo: str, base: str, *args: str) -> Plan:
    data = triage_json(repo, "--base", base, *args)
    fd, path = tempfile.mkstemp(suffix=".json", prefix="plan-", dir=workdir)
    with os.fdopen(fd, "w", encoding="utf-8") as f:
        json.dump(data, f)
    return Plan(repo, base, path, data)


def write_jsonl(path: str, rows: list[dict]) -> str:
    with open(path, "w", encoding="utf-8") as f:
        f.write("".join(json.dumps(r) + "\n" for r in rows))
    return path


def report_files(
    plan: Plan, findings: str, verdicts: str | None = None, *extra: str, env=None
) -> subprocess.CompletedProcess:
    """Run report on files already written, without checking the exit code."""
    args = ["report", "--plan", plan.path, "--findings", findings]
    if verdicts is not None:
        args += ["--verdicts", verdicts]
    return run_panel(plan.repo, *args, *extra, env=env, text=True)


def expect_report(workdir: str, plan: Plan, findings, verdicts=None, *extra: str, code: int = 0):
    """Write the findings and verdicts to new files, run report, and check its exit code."""
    fd, fpath = tempfile.mkstemp(suffix=".json", prefix="findings-", dir=workdir)
    os.close(fd)
    write_json(fpath, findings)
    vpath = None
    if verdicts is not None:
        fd, vpath = tempfile.mkstemp(suffix=".jsonl", prefix="verdicts-", dir=workdir)
        os.close(fd)
        write_jsonl(vpath, verdicts)
    proc = report_files(plan, fpath, vpath, *extra)
    if proc.returncode != code:
        raise AssertionError(
            f"report exited {proc.returncode}, expected {code}: {proc.stderr}{proc.stdout[:400]}"
        )
    return proc


class Fixtures:
    """Repositories and plans shared by this module, built once in setUpModule."""

    deep: Plan
    standard: Plan
    known_file: str
    known: Plan


FX = Fixtures()


def setUpModule() -> None:
    root = new_tmpdir()
    unittest.addModuleCleanup(shutil.rmtree, root)
    # A workflow change makes the plan deep, so correctness gets a reproduce skeptic.
    repo, base = report_repo(
        root, {".github/workflows/ci.yml": "on: push\n", "src/a.py": BASE_A + "new1\nnew2\n"}
    )
    FX.deep = make_plan(root, repo, base)
    # Standard plan: no reproduce skeptic, and src/a.py:30 is the one added line.
    repo, base = report_repo(root, {"src/a.py": BASE_A + "new1\n", "src/b.py": "b\n"})
    FX.standard = make_plan(root, repo, base)
    FX.known_file = write_json(os.path.join(root, "known.json"), KNOWN)
    FX.known = make_plan(root, repo, base, "--known", FX.known_file)


class ReportTestCase(PanelTestCase):
    def setUp(self) -> None:
        self.work = self.make_tmpdir()

    def path(self, name: str) -> str:
        return os.path.join(self.work, name)

    def json_file(self, name: str, data) -> str:
        return write_json(self.path(name), data)

    def new_repo(self, files: dict, base_files: dict | None = None) -> tuple[str, str]:
        return report_repo(self.work, files, base_files)

    def triage(self, repo: str, base: str, *args: str) -> Plan:
        return make_plan(self.work, repo, base, *args)

    def retriage(self, plan: Plan, *args: str) -> Plan:
        return make_plan(self.work, plan.repo, plan.base, *args)

    def report(self, plan: Plan, findings, verdicts=None, *extra: str, code: int = 0):
        return expect_report(self.work, plan, findings, verdicts, *extra, code=code)

    def status(self, plan: Plan, findings, verdicts=None) -> tuple[dict[str, str], str]:
        proc = self.report(plan, findings, verdicts, "--status")
        return parse_status(proc.stdout), proc.stdout


class StableIdTests(ReportTestCase):
    def test_documented_example_id(self) -> None:
        self.assertEqual(finding_id("correctness", "off-by-one", "src/a.py"), A)

    def test_row_within_20_lines_of_a_known_item_takes_its_id(self) -> None:
        st, out = self.status(FX.known, findings_doc([row(line=23)]))
        self.assertEqual(st.get(A), "known", out)

    def test_same_class_past_the_window_gets_a_line_id(self) -> None:
        far = finding_id("correctness", "off-by-one", "src/a.py", 24)
        st, out = self.status(FX.known, findings_doc([row(line=24)]))
        self.assertEqual(st.get(far), "pending", out)
        self.assertNotIn(A, st)
        self.assertIn(f"kept apart from known {A}", out)

    def test_known_id_names_the_item_at_any_distance(self) -> None:
        st, out = self.status(FX.known, findings_doc([row(line=100, known_id=A)]))
        self.assertEqual(st.get(A), "known", out)

    def test_near_and_far_rows_split(self) -> None:
        st, out = self.status(FX.known, findings_doc([row(line=3), row(line=60)]))
        self.assertEqual(st.get(A), "known", out)
        far = finding_id("correctness", "off-by-one", "src/a.py", 60)
        self.assertEqual(st.get(far), "pending", out)

    def two_known_items_plan(self) -> Plan:
        line_id = finding_id("correctness", "off-by-one", "src/a.py", 100)
        second = dict(KNOWN_ITEM, id=line_id, line=100, title="Second off by one")
        known = self.json_file("known2.json", dict(KNOWN, findings=[KNOWN_ITEM, second]))
        return self.retriage(FX.standard, "--known", known)

    def test_known_line_id_matches_within_its_window(self) -> None:
        line_id = finding_id("correctness", "off-by-one", "src/a.py", 100)
        st, out = self.status(self.two_known_items_plan(), findings_doc([row(line=105)]))
        self.assertEqual(st.get(line_id), "known", out)

    def test_row_between_two_known_items_gets_a_new_id(self) -> None:
        st, out = self.status(self.two_known_items_plan(), findings_doc([row(line=50)]))
        new = finding_id("correctness", "off-by-one", "src/a.py", 50)
        self.assertEqual(st.get(new), "pending", out)


class VotingTests(ReportTestCase):
    UPHELD = [
        verdict(A, "facts", refuted=False),
        verdict(A, "impact", refuted=False, pre_existing=False),
    ]

    def test_deep_plan_gives_correctness_a_reproduce_skeptic(self) -> None:
        lenses = FX.deep.data["lenses"]
        skeptics = [s for le in lenses if le["name"] == "correctness" for s in le["skeptics"]]
        self.assertIn("reproduce", skeptics, lenses)

    def test_all_skeptics_upholding_confirms(self) -> None:
        verdicts = self.UPHELD + [verdict(A, "reproduce", result="upheld")]
        proc = self.report(FX.deep, findings_doc([row()]), verdicts)
        self.assert_starts_with(proc.stdout, "**1 to fix:** 1 blocking")

    def test_all_skeptics_refuting_rejects(self) -> None:
        verdicts = [
            verdict(A, "facts", refuted=True),
            verdict(A, "impact", refuted=True),
            verdict(A, "reproduce", result="refuted"),
        ]
        proc = self.report(FX.deep, findings_doc([row()]), verdicts)
        self.assert_starts_with(proc.stdout, "**Nothing to fix.**")
        self.assertIn("1 rejected", proc.stdout)

    def test_reproduce_abstaining_leaves_two_upholds_confirmed(self) -> None:
        verdicts = self.UPHELD + [verdict(A, "reproduce", result="abstained")]
        proc = self.report(FX.deep, findings_doc([row()]), verdicts)
        self.assert_starts_with(proc.stdout, "**1 to fix:**")

    def test_status_names_the_missing_skeptic(self) -> None:
        cases = [
            (
                "deep plan",
                FX.deep,
                [verdict(A, "facts", refuted=False), verdict(A, "reproduce", result="upheld")],
            ),
            ("standard plan", FX.standard, [verdict(A, "facts", refuted=False)]),
        ]
        for label, plan, verdicts in cases:
            with self.subTest(label):
                st, out = self.status(plan, findings_doc([row()]), verdicts)
                self.assertEqual(st.get(A), "disputed (missing impact)", out)

    def test_missing_skeptic_without_a_resolution_exits_4(self) -> None:
        verdicts = [verdict(A, "facts", refuted=False), verdict(A, "reproduce", result="upheld")]
        proc = self.report(FX.deep, findings_doc([row()]), verdicts, code=4)
        self.assertIn(A, proc.stderr)

    def test_resolution_keeps_a_disputed_finding(self) -> None:
        verdicts = [
            verdict(A, "facts", refuted=False),
            verdict(A, "reproduce", result="upheld"),
            {"id": A, "resolution": {"keep": True, "reason": "read it"}},
        ]
        proc = self.report(FX.deep, findings_doc([row()]), verdicts)
        self.assertIn("disputed, kept: read it", proc.stdout)
        self.assert_starts_with(proc.stdout, "**1 to fix:**")

    def test_split_vote_is_disputed(self) -> None:
        verdicts = [
            verdict(A, "facts", refuted=False),
            verdict(A, "impact", refuted=True),
            verdict(A, "reproduce", result="upheld"),
        ]
        st, out = self.status(FX.deep, findings_doc([row()]), verdicts)
        self.assertEqual(st.get(A), "disputed", out)

    def test_pre_existing_vote_lists_the_finding_outside_the_count(self) -> None:
        verdicts = [
            verdict(A, "facts", refuted=False),
            verdict(A, "impact", refuted=False, pre_existing=True),
            verdict(A, "reproduce", result="abstained"),
        ]
        proc = self.report(FX.deep, findings_doc([row()]), verdicts)
        self.assertIn("### Pre-existing (not counted)", proc.stdout)
        self.assert_starts_with(proc.stdout, "**Nothing to fix.**")

    def test_later_verdict_line_replaces_an_earlier_one(self) -> None:
        verdicts = [
            verdict(A, "facts", refuted=True),
            verdict(A, "facts", refuted=False),
            verdict(A, "impact", refuted=False),
            verdict(A, "reproduce", result="upheld"),
        ]
        proc = self.report(FX.deep, findings_doc([row()]), verdicts)
        self.assert_starts_with(proc.stdout, "**1 to fix:**")


class SecurityMappingTests(ReportTestCase):
    GOOD = [
        verdict(S, "facts", filter_confidence=9, excluded_by=None),
        verdict(S, "impact", refuted=False),
        verdict(S, "reproduce", result="upheld"),
    ]

    def test_finder_md_example_maps_every_field(self) -> None:
        """finder.md's own example, not a copy: a key renamed there and in the install check
        but not in the mapping fails here."""
        example = dict(finder_example(), lens="security")
        cls = re.sub(r"[^a-z0-9]+", "-", example["category"].lower()).strip("-")
        fid = finding_id("security", cls, example["file"])
        title = example["description"].split(". ")[0].rstrip(".") + "."
        head = f"**[{fid}] {title}** ({example['file']}:{example['line']}, security)"
        facts = verdict(fid, "facts", filter_confidence=9, excluded_by=None)
        out = self.report(
            FX.standard, findings_doc([example]), [facts, verdict(fid, "impact", refuted=False)]
        ).stdout
        self.assertIn("### Blocking\n\n" + head, out)
        self.assertIn(f"- Evidence: {example['exploit_scenario']}", out)
        self.assertIn(f"- Fix: {example['recommendation']}", out)
        self.assertIn("- Also at: " + ", ".join(example["instances"]), out)
        # Without an impact verdict, the finder's own pre_existing decides.
        keep = {"id": fid, "resolution": {"keep": True, "reason": "r"}}
        doc = findings_doc([dict(example, pre_existing=True)])
        out = self.report(FX.standard, doc, [facts, keep]).stdout
        self.assertIn("### Pre-existing (not counted)\n\n" + head, out)

    def test_security_id_comes_from_the_mapped_class(self) -> None:
        for instances in (["src/admin.py:17"], ["src/db.py:42"]):
            with self.subTest(instances=instances):
                doc = findings_doc([dict(SECURITY_ROW, instances=instances)])
                st, out = self.status(FX.deep, doc)
                self.assertEqual(st.get(S), "pending", out)

    def test_high_is_blocking_with_problem_and_mapped_instances(self) -> None:
        proc = self.report(FX.deep, findings_doc([SECURITY_ROW]), self.GOOD)
        self.assert_starts_with(proc.stdout, "**1 to fix:** 1 blocking")
        self.assertIn("Also at: src/admin.py:17", proc.stdout)
        self.assertIn("The term is formatted into SQL.", proc.stdout)
        self.assertIn("- Problem:", proc.stdout)

    def test_instance_at_the_finding_place_is_mapped(self) -> None:
        doc = findings_doc([dict(SECURITY_ROW, instances=["src/db.py:42"])])
        proc = self.report(FX.deep, doc, self.GOOD)
        self.assert_starts_with(proc.stdout, "**1 to fix:** 1 blocking")
        self.assertIn("Also at: src/db.py:42", proc.stdout)
        self.assertIn(f"[{S}]", proc.stdout)

    def test_medium_is_major(self) -> None:
        doc = findings_doc([dict(SECURITY_ROW, severity="MEDIUM")])
        proc = self.report(FX.deep, doc, self.GOOD)
        self.assertIn("0 blocking, 1 major", proc.stdout)

    def test_low_is_dropped_with_a_warning(self) -> None:
        doc = findings_doc([dict(SECURITY_ROW, severity="LOW")])
        proc = self.report(FX.deep, doc, [])
        self.assert_starts_with(proc.stdout, "**Nothing to fix.**")
        self.assertIn("LOW security finding dropped", proc.stdout)

    def test_category_outside_3_to_60_characters_exits_2(self) -> None:
        doc = findings_doc([dict(SECURITY_ROW, category="x")])
        proc = self.report(FX.deep, doc, code=2)
        self.assertIn("3-60", proc.stderr)

    def test_superscript_digit_is_not_a_line_number(self) -> None:
        doc = findings_doc([dict(SECURITY_ROW, instances=["src/admin.py:²"])])
        st, out = self.status(FX.standard, doc)
        self.assertEqual(st.get(S), "pending", out)


class SecurityVetoTests(ReportTestCase):
    def test_filter_exclusion_rejects(self) -> None:
        verdicts = [
            verdict(S, "facts", filter_confidence=9, excluded_by="E6"),
            verdict(S, "impact", refuted=False),
            verdict(S, "reproduce", result="upheld"),
        ]
        proc = self.report(FX.deep, findings_doc([SECURITY_ROW]), verdicts)
        self.assertIn("1 rejected", proc.stdout)
        self.assert_starts_with(proc.stdout, "**Nothing to fix.**")

    def test_filter_confidence_below_the_cutoff_rejects(self) -> None:
        verdicts = [
            verdict(S, "facts", filter_confidence=7, excluded_by=None),
            verdict(S, "impact", refuted=False),
        ]
        proc = self.report(FX.deep, findings_doc([SECURITY_ROW]), verdicts)
        self.assertIn("1 rejected", proc.stdout)

    def test_security_facts_verdict_with_refuted_exits_4(self) -> None:
        verdicts = [verdict(S, "facts", filter_confidence=9, excluded_by=None, refuted=False)]
        proc = self.report(FX.deep, findings_doc([SECURITY_ROW]), verdicts, code=4)
        self.assertIn(S, proc.stderr)

    def test_security_facts_verdict_with_refuted_is_disputed_with_a_warning(self) -> None:
        """The security facts skeptic answers with filter fields; `refuted` makes it unparsable."""
        verdicts = [
            verdict(S, "facts", filter_confidence=9, excluded_by=None, refuted=False),
            verdict(S, "impact", refuted=False),
            verdict(S, "reproduce", result="upheld"),
            verdict(A, "facts", refuted=False),
            verdict(A, "impact", refuted=False, pre_existing=False),
            verdict(A, "reproduce", result="upheld"),
        ]
        st, out = self.status(FX.standard, findings_doc([SECURITY_ROW, row()]), verdicts)
        self.assertEqual(st.get(S), "disputed", out)
        self.assertEqual(st.get(A), "confirmed", out)
        self.assertIn("refuted", out)

    def test_missing_security_facts_verdict_exits_4(self) -> None:
        verdicts = [verdict(S, "impact", refuted=False)]
        self.report(FX.deep, findings_doc([SECURITY_ROW]), verdicts, code=4)

    def test_veto_by_a_value_that_is_not_a_rule_is_disputed(self) -> None:
        for bad in ("E99", "", "R1", "e6", 6):
            with self.subTest(excluded_by=bad):
                verdicts = [
                    verdict(S, "facts", filter_confidence=9, excluded_by=bad),
                    verdict(S, "impact", refuted=False),
                    verdict(S, "reproduce", result="upheld"),
                ]
                st, out = self.status(FX.deep, findings_doc([SECURITY_ROW]), verdicts)
                self.assertEqual(st.get(S), "disputed", out)
                self.assertIn("warning:", out)
                proc = self.report(FX.deep, findings_doc([SECURITY_ROW]), verdicts, code=4)
                self.assertIn(S, proc.stderr)

    def test_veto_follows_review_md_drops_and_precedents(self) -> None:
        review = (
            "## Security review\n\n- drop E6: workflows take untrusted input\n"
            "- precedent: Caches are in scope.\n"
        )
        repo, base = self.new_repo(
            {"src/db.py": "".join(f"q{i}\n" for i in range(1, 50))},
            base_files={"REVIEW.md": review, "src/db.py": "q\n"},
        )
        plan = self.triage(repo, base)
        cases = [
            ("E6", "disputed"),
            ("R1", "rejected"),
            ("R2", "disputed"),
            ("P7", "rejected"),
            ("E7", "rejected"),
        ]
        for rule, expected in cases:
            with self.subTest(excluded_by=rule):
                verdicts = [
                    verdict(S, "facts", filter_confidence=9, excluded_by=rule),
                    verdict(S, "impact", refuted=False),
                ]
                st, out = self.status(plan, findings_doc([SECURITY_ROW]), verdicts)
                self.assertEqual(st.get(S), expected, out)

    def test_confidence_outside_1_to_10_is_disputed_not_vetoed(self) -> None:
        """A finder's 0.95 copied by mistake must not veto every security finding."""
        for bad in (0.95, 0, 11, 8.5, "9", True):
            with self.subTest(filter_confidence=bad):
                verdicts = [
                    verdict(S, "facts", filter_confidence=bad, excluded_by=None),
                    verdict(S, "impact", refuted=False),
                    verdict(S, "reproduce", result="upheld"),
                ]
                st, out = self.status(FX.standard, findings_doc([SECURITY_ROW]), verdicts)
                self.assertEqual(st.get(S), "disputed", out)
                self.assertIn("not an integer from 1 to 10", out)

    def test_active_exclusion_vetoes_and_confidence_is_a_fallback_key(self) -> None:
        cases = [
            ("null confidence", {"filter_confidence": None, "excluded_by": "E7"}, "rejected"),
            ("no confidence", {"excluded_by": "E7"}, "rejected"),
            ("confidence key veto", {"confidence": 9, "excluded_by": "E7"}, "rejected"),
            ("confidence key low", {"confidence": 5, "excluded_by": None}, "rejected"),
            ("confidence key kept", {"confidence": 9, "excluded_by": None}, "confirmed"),
        ]
        for label, facts, expected in cases:
            with self.subTest(label):
                verdicts = [
                    verdict(S, "facts", **facts),
                    verdict(S, "impact", refuted=False),
                    verdict(S, "reproduce", result="upheld"),
                ]
                st, out = self.status(FX.standard, findings_doc([SECURITY_ROW]), verdicts)
                self.assertEqual(st.get(S), expected, out)


class KnownFindingsTests(ReportTestCase):
    def test_plan_records_the_known_file(self) -> None:
        self.assertEqual(FX.known.data["known_file"], os.path.realpath(FX.known_file))

    def test_repeated_finding_is_known(self) -> None:
        st, out = self.status(FX.known, findings_doc([row()]))
        self.assertEqual(st.get(A), "known", out)

    def test_known_id_with_another_class_is_known(self) -> None:
        st, out = self.status(FX.known, findings_doc([row(cls="wrong-bound", known_id=A)]))
        self.assertEqual(st.get(A), "known", out)

    def test_known_id_with_new_evidence_is_pending(self) -> None:
        doc = findings_doc([row(cls="wrong-bound", known_id=A, new_evidence="new caller")])
        st, out = self.status(FX.known, doc)
        self.assertEqual(st.get(A), "pending", out)

    def test_new_evidence_is_tagged_in_the_report(self) -> None:
        doc = findings_doc([row(cls="wrong-bound", known_id=A, new_evidence="new caller")])
        proc = self.report(FX.known, doc, upheld(A))
        self.assertIn("new evidence: new caller", proc.stdout)
        self.assert_starts_with(proc.stdout, "**1 to fix:**")

    def test_absent_known_id_warns_and_falls_back_to_the_hash(self) -> None:
        st, out = self.status(FX.known, findings_doc([row(known_id="CR-00000000")]))
        self.assertIn("CR-00000000 is not in the known", out)
        self.assertEqual(st.get(A), "known", out)

    def test_repeat_without_new_evidence_counts_as_still_present(self) -> None:
        proc = self.report(FX.known, findings_doc([row()]), [])
        self.assert_starts_with(
            proc.stdout, "**Nothing new to fix;** 1 earlier findings still open"
        )
        self.assertIn(f"- [{A}] Off by one (src/a.py:3)", proc.stdout)
        self.assertIn("Known, not repeated: [" + A, proc.stdout)
        self.assertIn("1 known not repeated", proc.stdout)

    def test_new_class_near_a_known_item_warns_of_a_possible_rename(self) -> None:
        st, out = self.status(FX.known, findings_doc([row(cls="null-deref", line=6)]))
        self.assertIn(f"may duplicate known {A}", out)

    def test_still_present_ids_keep_items_open(self) -> None:
        proc = self.report(FX.known, findings_doc([], still_present=[A]), [])
        self.assert_starts_with(
            proc.stdout, "**Nothing new to fix;** 1 earlier findings still open"
        )
        self.assertIn("### Still open from earlier rounds", proc.stdout)
        self.assertIn("needs a human", proc.stdout)
        self.assertIn("1 earlier findings still open", proc.stdout)

    def test_open_item_of_a_lens_this_panel_lacks_stays_open_unchecked(self) -> None:
        item = dict(KNOWN_ITEM, id="CR-5c000001", lens="retired-lens")
        known = self.json_file("k5i.json", dict(KNOWN, findings=[item]))
        plan = self.retriage(FX.standard, "--known", known)
        out = self.path("k5o.json")
        proc = self.report(plan, findings_doc([]), [], "--known-out", out)
        doc = load_json(out)
        self.assertNotIn("retired-lens", {le["name"] for le in plan.data["lenses"]})
        self.assertTrue(any("retired-lens" in w for w in plan.data["warnings"]), plan.data)
        self.assert_starts_with(
            proc.stdout, "**Nothing new to fix;** 1 earlier findings still open"
        )
        self.assertIn("Not checked this round", proc.stdout)
        self.assertEqual(doc["findings"][0]["last_seen_round"], 2, doc)
        self.assertEqual(doc["rounds_with_open_findings"], 2, doc)

    def test_open_item_of_a_lens_not_covered_stays_open_unchecked(self) -> None:
        out = self.path("k5o.json")
        doc = findings_doc([], not_covered=["correctness"])
        proc = self.report(FX.known, doc, [], "--known-out", out)
        self.assertIn("1 earlier findings still open", proc.stdout.split("\n")[0])
        self.assertIn("Not checked this round", proc.stdout)
        self.assertEqual(load_json(out)["findings"][0]["last_seen_round"], 2)

    def test_open_item_in_an_unread_file_stays_open_unchecked(self) -> None:
        out = self.path("k31.json")
        proc = self.report(FX.known, findings_doc([], unread=["src/a.py"]), [], "--known-out", out)
        doc = load_json(out)
        self.assert_starts_with(
            proc.stdout, "**Review incomplete:** 1 files not read; 1 earlier findings still open"
        )
        self.assertIn("Not checked this round", proc.stdout)
        self.assertEqual(doc["findings"][0]["last_seen_round"], 2, doc)
        self.assertEqual(doc["rounds_with_open_findings"], 2, doc)

    def test_open_item_with_an_instance_in_an_unread_file_stays_open(self) -> None:
        item = dict(KNOWN_ITEM, file="src/b.py", instances=[{"file": "src/a.py", "line": 9}])
        known = self.json_file("k31i.json", dict(KNOWN, findings=[item]))
        plan = self.retriage(FX.standard, "--known", known)
        out = self.path("k31o.json")
        proc = self.report(plan, findings_doc([], unread=["src/a.py"]), [], "--known-out", out)
        doc = load_json(out)
        self.assertEqual(len(plan.data["open_known"]), 1)
        self.assertIn("Not checked this round", proc.stdout)
        self.assertEqual(doc["findings"][0]["last_seen_round"], 2, doc)
        self.assertEqual(doc["rounds_with_open_findings"], 2, doc)

    def test_unread_file_elsewhere_still_checks_the_known_item(self) -> None:
        """The item's own file was read, so it is checked, and an unmentioned item stays open."""
        out = self.path("k31n.json")
        proc = self.report(FX.known, findings_doc([], unread=["src/c.py"]), [], "--known-out", out)
        doc = load_json(out)
        self.assert_starts_with(
            proc.stdout, "**Review incomplete:** 1 files not read; 1 earlier findings still open"
        )
        self.assertNotIn("Not checked this round", proc.stdout)
        self.assertIn(f"Kept open, no finder listed them as fixed: [{A}]", proc.stdout)
        self.assertEqual(doc["findings"][0]["last_seen_round"], 2, doc)

    def test_fixed_on_an_item_its_lens_did_not_check_stays_open(self) -> None:
        fixed = [{"id": A, "reason": "r", "lens": "correctness"}]
        doc = findings_doc([], not_covered=["correctness"], fixed=fixed)
        proc = self.report(FX.known, doc, [])
        self.assertIn("Not checked this round", proc.stdout)
        self.assertIn(
            f"{A} is listed as fixed but also reported, still present or not", proc.stdout
        )
        self.assertIn("1 earlier findings still open", proc.stdout.split("\n")[0])

    def test_unread_file_elsewhere_and_fixed_closes_the_known_item(self) -> None:
        out = self.path("k31f.json")
        fixed = [{"id": A, "reason": "src/a.py:3 now stops at the end.", "lens": "correctness"}]
        doc = findings_doc([], unread=["src/c.py"], fixed=fixed)
        proc = self.report(FX.known, doc, [], "--known-out", out)
        known = load_json(out)
        self.assert_starts_with(proc.stdout, "**Review incomplete:** 1 files not read\n")
        self.assertEqual(known["findings"][0]["last_seen_round"], 1, known)
        self.assertEqual(known["rounds_with_open_findings"], 0, known)

    def report_again(self, status: str, pre_existing: bool) -> tuple[str, dict]:
        """Report the known item again with new evidence; return the known-out file and item."""
        known = self.json_file("k32.json", dict(KNOWN, findings=[dict(KNOWN_ITEM, status=status)]))
        plan = self.retriage(FX.standard, "--known", known)
        out = self.path("k32-out.json")
        again = row(
            cls="wrong-bound", known_id=A, new_evidence="introduced here", line=5, sev="major"
        )
        verdicts = [
            verdict(A, "facts", refuted=False),
            verdict(A, "impact", refuted=False, pre_existing=pre_existing),
        ]
        self.report(plan, findings_doc([again]), verdicts, "--known-out", out)
        return out, load_json(out)["findings"][0]

    def test_new_evidence_reopens_a_pre_existing_item(self) -> None:
        out, item = self.report_again("pre-existing", False)
        next_plan = self.retriage(FX.standard, "--known", out)
        self.assertEqual(item["status"], "posted", item)
        self.assertEqual(item["line"], 5)
        self.assertEqual(item["severity"], "major")
        self.assertEqual(item["title"], "wrong-bound in src/a.py")
        self.assertEqual(item["last_seen_round"], 2)
        self.assertEqual([i["id"] for i in next_plan.data["open_known"]], [A])

    def test_new_evidence_reopens_a_declined_item(self) -> None:
        _, item = self.report_again("declined", False)
        self.assertEqual(item["status"], "posted", item)
        self.assertEqual(item["line"], 5)

    def test_posted_item_found_pre_existing_becomes_pre_existing(self) -> None:
        _, item = self.report_again("posted", True)
        self.assertEqual(item["status"], "pre-existing", item)
        self.assertEqual(item["last_seen_round"], 2)

    def test_declined_item_found_pre_existing_stays_declined(self) -> None:
        _, item = self.report_again("declined", True)
        self.assertEqual(item["status"], "declined", item)
        self.assertEqual(item["last_seen_round"], 2)


class KnownInstancesTests(ReportTestCase):
    LEAK = finding_id("correctness", "leak", "src/a.py")

    @classmethod
    def setUpClass(cls) -> None:
        super().setUpClass()
        work = cls.make_class_tmpdir()
        known = os.path.join(work, "k7.json")
        leak = row(cls="leak", instances=[{"file": "src/b.py", "line": 1}])
        expect_report(
            work, FX.standard, findings_doc([leak]), upheld(cls.LEAK), "--known-out", known
        )
        cls.known = load_json(known)
        cls.plan = make_plan(work, FX.standard.repo, FX.standard.base, "--known", known)

    def test_known_item_stores_its_instances(self) -> None:
        self.assertEqual(
            self.known["findings"][0].get("instances"), [{"file": "src/b.py", "line": 1}]
        )

    def test_row_at_a_known_instance_is_the_known_item(self) -> None:
        st, out = self.status(self.plan, findings_doc([row(cls="leak", file="src/b.py", line=4)]))
        self.assertEqual(st.get(self.LEAK), "known", out)
        self.assertEqual(len(st), 1, out)

    def test_new_instances_are_merged_into_the_known_item(self) -> None:
        out = self.path("k7b.json")
        leak = row(cls="leak", instances=[{"file": "src/c.py", "line": 7}])
        self.report(self.plan, findings_doc([leak]), [], "--known-out", out)
        self.assertEqual(
            load_json(out)["findings"][0].get("instances"),
            [{"file": "src/b.py", "line": 1}, {"file": "src/c.py", "line": 7}],
        )


class RoundTripTests(ReportTestCase):
    """A finding posted in round 1, carried to round 2 through the known findings file."""

    NULL_DEREF = finding_id("correctness", "null-deref", "src/b.py")
    ROWS = [row(cls="null-deref", file="src/b.py", line=1)]
    VERDICTS = upheld(NULL_DEREF)

    @classmethod
    def setUpClass(cls) -> None:
        super().setUpClass()
        work = cls.make_class_tmpdir()
        cls.k1 = os.path.join(work, "k1.json")
        expect_report(
            work, FX.standard, findings_doc(cls.ROWS), cls.VERDICTS, "--known-out", cls.k1
        )
        cls.plan2 = make_plan(work, FX.standard.repo, FX.standard.base, "--known", cls.k1)
        cls.k2 = os.path.join(work, "k2.json")
        cls.round2 = expect_report(
            work, cls.plan2, findings_doc(cls.ROWS), cls.VERDICTS, "--known-out", cls.k2
        )

    def test_first_round_writes_a_posted_item(self) -> None:
        doc = load_json(self.k1)
        self.assertEqual(doc["round"], 1, doc)
        self.assertEqual(doc["rounds_with_open_findings"], 1, doc)
        self.assertEqual(doc["findings"][0]["status"], "posted", doc)

    def test_triage_takes_the_next_round_from_the_known_file(self) -> None:
        self.assertEqual(self.plan2.data["round"], 2)
        self.assertEqual(len(self.plan2.data["open_known"]), 1, self.plan2.data["open_known"])

    def test_second_round_reads_the_plan_known_file_and_repeats_nothing(self) -> None:
        """Report has no --known flag; it reads the known file the plan names."""
        doc = load_json(self.k2)
        self.assert_starts_with(self.round2.stdout, "**Nothing new to fix;** 1 earlier")
        self.assertEqual(doc["round"], 2, doc)
        self.assertEqual(doc["rounds_with_open_findings"], 2, doc)
        self.assertEqual(len(doc["findings"]), 1, doc)
        self.assertEqual(doc["findings"][0]["last_seen_round"], 2, doc)

    def test_round_with_the_item_fixed_resets_the_counter(self) -> None:
        plan3 = self.retriage(FX.standard, "--known", self.k2)
        out = self.path("k3.json")
        fixed = [
            {"id": self.NULL_DEREF, "reason": "src/b.py:1 checks for None.", "lens": "correctness"}
        ]
        proc = self.report(plan3, findings_doc([], fixed=fixed), [], "--known-out", out)
        doc = load_json(out)
        self.assertEqual(doc["rounds_with_open_findings"], 0, doc)
        self.assertEqual(doc["findings"][0]["last_seen_round"], 2, doc)
        self.assert_starts_with(proc.stdout, "**Nothing to fix.**")
        self.assertIn(
            f"- Fixed since the last round: [{self.NULL_DEREF}] null-deref in src/b.py "
            "(src/b.py:1): src/b.py:1 checks for None.",
            proc.stdout,
        )

    def test_item_no_finder_mentions_stays_open(self) -> None:
        """Only a finder's fixed list closes an item; leaving it out does not."""
        plan3 = self.retriage(FX.standard, "--known", self.k2)
        out = self.path("k3s.json")
        proc = self.report(plan3, findings_doc([]), [], "--known-out", out)
        doc = load_json(out)
        self.assert_starts_with(
            proc.stdout, "**Nothing new to fix;** 1 earlier findings still open"
        )
        self.assertIn(
            f"Kept open, no finder listed them as fixed: [{self.NULL_DEREF}]", proc.stdout
        )
        self.assertEqual(doc["rounds_with_open_findings"], 3, doc)
        self.assertEqual(doc["findings"][0]["last_seen_round"], 3, doc)

    def test_item_listed_fixed_and_still_present_stays_open(self) -> None:
        plan3 = self.retriage(FX.standard, "--known", self.k2)
        fixed = [{"id": self.NULL_DEREF, "reason": "r", "lens": "correctness"}]
        doc = findings_doc([], fixed=fixed, still_present=[self.NULL_DEREF])
        proc = self.report(plan3, doc, [])
        self.assert_starts_with(
            proc.stdout, "**Nothing new to fix;** 1 earlier findings still open"
        )
        self.assertIn(
            "listed as fixed but also reported, still present or not checked", proc.stdout
        )

    def test_fixed_from_another_lens_is_ignored(self) -> None:
        plan3 = self.retriage(FX.standard, "--known", self.k2)
        fixed = [{"id": self.NULL_DEREF, "reason": "looks fine", "lens": "security"}]
        proc = self.report(plan3, findings_doc([], fixed=fixed), [])
        self.assert_starts_with(
            proc.stdout, "**Nothing new to fix;** 1 earlier findings still open"
        )
        self.assertIn(
            f"fixed {self.NULL_DEREF} comes from lens security, not its lens correctness; ignored",
            proc.stdout,
        )

    def test_plan_marks_which_known_items_are_open(self) -> None:
        plan3 = self.retriage(FX.standard, "--known", self.k2)
        self.assertEqual(
            [(k["id"], k["open"]) for k in plan3.data["known"]], [(self.NULL_DEREF, True)]
        )
        out = self.path("k3o.json")
        fixed = [{"id": self.NULL_DEREF, "reason": "r", "lens": "correctness"}]
        self.report(plan3, findings_doc([], fixed=fixed), [], "--known-out", out)
        plan4 = self.retriage(FX.standard, "--known", out)
        self.assertEqual(
            [(k["id"], k["open"]) for k in plan4.data["known"]], [(self.NULL_DEREF, False)]
        )
        self.assertEqual(plan4.data["open_known"], [])

    def test_fixed_id_that_is_not_open_is_ignored_with_a_warning(self) -> None:
        plan3 = self.retriage(FX.standard, "--known", self.k2)
        fixed = [{"id": "CR-00000000", "reason": "r", "lens": "correctness"}]
        proc = self.report(
            plan3, findings_doc([], fixed=fixed, still_present=[self.NULL_DEREF]), []
        )
        self.assertIn("fixed CR-00000000 is not an open known item; ignored", proc.stdout)

    def test_fixed_must_be_a_list_of_id_and_reason(self) -> None:
        for bad in (
            ["CR-00000000"],
            [{"id": "CR-00000000"}],
            [{"id": "CR-00000000", "reason": "r"}],
            {"id": "CR-00000000", "reason": "r", "lens": "correctness"},
        ):
            with self.subTest(fixed=bad):
                proc = self.report(self.plan2, findings_doc([], fixed=bad), [], code=2)
                self.assertIn("fixed must be a list", proc.stderr)

    def test_empty_hunt_keeps_the_known_file(self) -> None:
        head = rev_parse(FX.standard.repo)
        plan4 = self.retriage(FX.standard, "--known", self.k2, "--since", head)
        out = self.path("k4.json")
        proc = self.report(plan4, findings_doc([]), None, "--known-out", out)
        self.assertEqual(load_json(out), load_json(self.k2))
        self.assert_starts_with(proc.stdout, f"**No new commits since {head}.**")

    def test_report_rejects_a_known_flag(self) -> None:
        doc = findings_doc(self.ROWS)
        proc = self.report(self.plan2, doc, self.VERDICTS, "--known", self.k1, code=2)
        self.assertIn("--known", proc.stderr)

    def test_unreadable_known_file_named_by_the_plan_exits_2(self) -> None:
        gone = self.path("gone.json")
        shutil.copy(self.k1, gone)
        plan = self.retriage(FX.standard, "--known", gone)
        os.remove(gone)
        proc = self.report(plan, findings_doc([]), [], code=2)
        self.assertIn("cannot read known findings file", proc.stderr)

    def test_report_rerun_on_its_own_known_out_exits_2(self) -> None:
        known = self.path("k6.json")
        shutil.copy(self.k1, known)
        plan = self.retriage(FX.standard, "--known", known)
        bug = finding_id("correctness", "bug-00", "src/a.py")
        doc = findings_doc([row(cls="bug-00", sev="major", line=1)])
        self.report(plan, doc, upheld(bug), "--known-out", known)
        proc = self.report(plan, doc, upheld(bug), "--known-out", known, code=2)
        self.assertIn("triage again", proc.stderr)


class RoundCapTests(ReportTestCase):
    def declined_plan(self) -> Plan:
        item = dict(KNOWN_ITEM, last_seen_round=3, status="declined")
        doc = {"version": 1, "round": 3, "rounds_with_open_findings": 3, "findings": [item]}
        return self.retriage(FX.standard, "--known", self.json_file("declined.json", doc))

    def test_round_cap_report(self) -> None:
        item = dict(KNOWN_ITEM, last_seen_round=3)
        doc = {"version": 1, "round": 3, "rounds_with_open_findings": 3, "findings": [item]}
        plan = self.retriage(FX.standard, "--known", self.json_file("cap.json", doc))
        out = self.path("cap-out.json")
        proc = self.report(plan, findings_doc([row()]), None, "--known-out", out)
        self.assertTrue(plan.data["round_cap"])
        self.assert_starts_with(
            proc.stdout, "**Round cap reached:** 1 open findings after 3 rounds"
        )
        self.assertIn("reconsider the approach", proc.stdout)
        self.assertIn("needs a human", proc.stdout)
        self.assertEqual(load_json(out)["rounds_with_open_findings"], 3)

    def test_declined_items_lift_the_round_cap(self) -> None:
        plan = self.declined_plan()
        proc = self.report(plan, findings_doc([row(cls="new-bug", line=9)]), upheld(NEW_BUG))
        self.assertFalse(plan.data["round_cap"])
        self.assert_starts_with(proc.stdout, "**1 to fix:**")
        self.assertNotIn("Round cap", proc.stdout)

    def test_counter_restarts_after_the_cap_is_lifted(self) -> None:
        out = self.path("decl-out.json")
        doc = findings_doc([row(cls="new-bug", line=9)])
        self.report(self.declined_plan(), doc, upheld(NEW_BUG), "--known-out", out)
        self.assertEqual(load_json(out)["rounds_with_open_findings"], 1)


def thirty_majors() -> list[dict]:
    lenses = ["correctness", "testing", "architecture", "performance"]
    return [row(lens=lenses[i % 4], cls=f"bug-{i:02d}", sev="major", line=i + 1) for i in range(30)]


def twelve_minors() -> list[dict]:
    lenses = ["correctness", "testing"]
    return [
        row(cls=f"minor-{i:02d}", sev="minor", line=i + 1, lens=lenses[i % 2]) for i in range(12)
    ]


def twelve_majors() -> list[dict]:
    return [row(cls=f"bug-{i:02d}", sev="major", line=i + 1) for i in range(12)]


class BudgetAndCapTests(ReportTestCase):
    def test_verification_budget_is_20_findings(self) -> None:
        st, out = self.status(FX.standard, findings_doc(thirty_majors()))
        values = list(st.values())
        self.assertEqual(values.count("pending"), 20, out)
        self.assertEqual(values.count("over-cap"), 10, out)

    def test_findings_over_budget_are_listed_as_not_verified(self) -> None:
        rows = thirty_majors()
        st, _ = self.status(FX.standard, findings_doc(rows))
        pending = [k for k, s in st.items() if s == "pending"]
        over = [k for k, s in st.items() if s == "over-cap"]
        verdicts = [verdict(k, s, refuted=False) for k in pending for s in ("facts", "impact")]
        out = self.report(FX.standard, findings_doc(rows), verdicts).stdout
        self.assert_starts_with(out, "**20 to fix:** 0 blocking, 20 major")
        self.assertIn("10 not verified over budget", out)
        self.assertIn("Not verified (over budget): ", out)
        listed = out.split("Not verified (over budget): ", 1)[1]
        for fid in over:
            self.assertIn(fid, listed)
        self.assertIn("10 findings not verified", out)

    def test_minor_budget_is_10(self) -> None:
        st, out = self.status(FX.standard, findings_doc(twelve_minors()))
        self.assertEqual(list(st.values()).count("pending"), 10, out)

    def nit_cap_report(self) -> str:
        """12 minors: 10 verified, of which 7 are kept; the default nit cap lists 5."""
        rows = twelve_minors()
        st, _ = self.status(FX.standard, findings_doc(rows))
        pending = [k for k, s in st.items() if s == "pending"]
        verdicts = []
        for i, fid in enumerate(pending):
            verdicts += [
                verdict(fid, "facts", refuted=i >= 7),
                verdict(fid, "impact", refuted=i >= 7),
            ]
        return self.report(FX.standard, findings_doc(rows), verdicts).stdout

    def test_nit_cap_lists_5_of_7_minors(self) -> None:
        out = self.nit_cap_report()
        self.assert_starts_with(out, "**7 to fix:** 0 blocking, 0 major, 7 minor (5 listed)\n")
        self.assertIn("2 minors not verified over the nit cap", out)
        self.assertNotIn("findings not verified", out)

    def test_minors_past_their_budget_have_their_own_status(self) -> None:
        st, out = self.status(FX.standard, findings_doc(twelve_minors()))
        self.assertEqual(list(st.values()).count("minor-cap"), 2, out)
        self.assertNotIn("over-cap", st.values(), out)

    def test_nit_cap_0_verifies_no_minor_and_stays_complete(self) -> None:
        repo, base = self.new_repo(
            {"src/a.py": BASE_A + "new1\n"},
            base_files={"src/a.py": BASE_A, "REVIEW.md": "## Nit cap\n\n0\n"},
        )
        plan = self.triage(repo, base)
        rows = twelve_minors()[:3]
        st, out = self.status(plan, findings_doc(rows))
        self.assertEqual(list(st.values()), ["minor-cap"] * 3, out)
        major = finding_id("correctness", "bug-00", "src/a.py")
        text = self.report(plan, findings_doc(rows), []).stdout
        self.assert_starts_with(
            text,
            "**Nothing to fix.** None of the 3 candidate findings was kept; "
            "3 minor findings past the nit cap were not verified.\n",
        )
        self.assertNotIn("not verified (over budget)", text)
        self.assertNotIn("findings not verified", text)
        doc = findings_doc(rows + [row(cls="bug-00", sev="major", line=30)])
        text = self.report(plan, doc, upheld(major)).stdout
        self.assert_starts_with(text, "**1 to fix:** 0 blocking, 1 major, 0 minor\n")

    def test_fold_notes_minors_over_the_nit_cap_among_the_kept(self) -> None:
        line, n, buckets = fold_buckets(self.nit_cap_report())
        self.assertEqual(n, 12, line)
        self.assertEqual(sum(buckets.values()), 12, line)
        self.assertIn("(2 of them minors over the nit cap, not listed)", line)

    def test_minor_budget_comes_before_the_per_lens_cap(self) -> None:
        repo, base = self.new_repo(
            {"src/a.py": BASE_A + "new1\n"},
            base_files={"src/a.py": BASE_A, "REVIEW.md": "## Nit cap\n\n0\n"},
        )
        plan = self.triage(repo, base)
        rows = [row(cls=f"minor-{i:02d}", sev="minor", line=i + 1) for i in range(11)]
        st, out = self.status(plan, findings_doc(rows))
        self.assertEqual(list(st.values()), ["minor-cap"] * 11, out)
        self.assertNotIn("past 10", out)

    def test_one_lens_is_capped_at_10(self) -> None:
        st, out = self.status(FX.standard, findings_doc(twelve_majors()))
        values = list(st.values())
        self.assertEqual(values.count("pending"), 10, out)
        self.assertEqual(values.count("over-cap"), 2, out)
        self.assertIn("past 10", out)

    def over_budget_all_refuted(self) -> tuple[str, list[str], str]:
        rows = twelve_majors()
        st, _ = self.status(FX.standard, findings_doc(rows))
        pending = [k for k, s in st.items() if s == "pending"]
        over = [k for k, s in st.items() if s == "over-cap"]
        verdicts = [verdict(k, s, refuted=True) for k in pending for s in ("facts", "impact")]
        gitlab = self.path("glo.json")
        proc = self.report(FX.standard, findings_doc(rows), verdicts, "--gitlab-json", gitlab)
        return proc.stdout, over, load_json(gitlab)["summary"]

    def test_over_budget_with_nothing_counted_is_incomplete(self) -> None:
        out, _, _ = self.over_budget_all_refuted()
        self.assert_starts_with(out, "**Review incomplete:** 2 findings not verified (over budget)")
        self.assertNotIn("Nothing to fix", out.split("\n")[0])

    def test_gitlab_summary_lists_the_over_budget_ids(self) -> None:
        _, over, summary = self.over_budget_all_refuted()
        for fid in over:
            self.assertIn(f"[{fid}] not verified (over budget)", summary)


class DuplicateTests(ReportTestCase):
    def test_same_id_twice_is_marked_duplicate(self) -> None:
        _, out = self.status(FX.standard, findings_doc([row(line=3), row(line=9)]))
        self.assertIn("duplicate", out)

    def test_duplicate_ids_exit_2(self) -> None:
        proc = self.report(FX.standard, findings_doc([row(line=3), row(line=9)]), [], code=2)
        self.assertIn("duplicate ids", proc.stderr)

    def test_cross_lens_duplicate_warns(self) -> None:
        doc = findings_doc([row(line=3), row(lens="testing", cls="untested", line=5)])
        _, out = self.status(FX.standard, doc)
        self.assertIn("possible cross-lens duplicate", out)


class HeadlineTests(ReportTestCase):
    def test_no_findings_says_nothing_to_fix(self) -> None:
        proc = self.report(FX.standard, findings_doc([]), [])
        self.assert_starts_with(proc.stdout, "**Nothing to fix.** No lens reported a finding.")

    def test_lens_not_covered_makes_the_review_incomplete(self) -> None:
        out = self.report(FX.standard, findings_doc([], not_covered=["performance"]), []).stdout
        self.assert_starts_with(out, "**Review incomplete:** lenses not covered: performance")
        self.assertNotIn("Nothing to fix", out)
        self.assertIn("needs a human", out)
        self.assertIn("lenses not covered: performance", out.split("\n", 1)[1])

    def test_serial_fallback_is_noted(self) -> None:
        proc = self.report(FX.standard, findings_doc([], independent=False), [])
        self.assertIn("checks were not independent", proc.stdout)

    def test_unread_files_make_the_review_incomplete(self) -> None:
        doc = findings_doc([], unread=["src/big.py", "src/b.py"])
        out = self.report(FX.standard, doc, []).stdout
        self.assert_starts_with(out, "**Review incomplete:** 2 files not read")
        self.assertNotIn("Nothing to fix", out)
        self.assertIn("files not read: src/big.py, src/b.py", out)
        self.assertIn("Approval hint: needs a human (code changes; files not read)", out)

    def test_unread_files_and_a_lens_not_covered_are_both_named(self) -> None:
        doc = findings_doc([], unread=["src/big.py"], not_covered=["performance"])
        proc = self.report(FX.standard, doc, [])
        self.assert_starts_with(
            proc.stdout, "**Review incomplete:** lenses not covered: performance; 1 files not read"
        )

    def test_unread_files_with_a_counted_finding_still_count_it(self) -> None:
        counted = finding_id("correctness", "counted-bug", "src/a.py")
        doc = findings_doc([row(cls="counted-bug")], unread=["src/big.py"])
        proc = self.report(FX.standard, doc, upheld(counted))
        self.assert_starts_with(proc.stdout, "**1 to fix:**")
        self.assertIn("files not read", proc.stdout)

    def test_counted_finding_with_security_not_covered_says_incomplete(self) -> None:
        counted = finding_id("correctness", "counted-bug", "src/a.py")
        doc = findings_doc([row(cls="counted-bug", sev="minor")], not_covered=["security"])
        gitlab = self.path("gl-gap.json")
        proc = self.report(FX.standard, doc, upheld(counted), "--gitlab-json", gitlab)
        self.assert_starts_with(
            proc.stdout,
            "**1 to fix:** 0 blocking, 0 major, 1 minor; review incomplete: "
            "lenses not covered: security\n",
        )
        self.assert_starts_with(
            load_json(gitlab)["summary"],
            "**1 to fix** (0 inline, all listed below); review incomplete: "
            "lenses not covered: security\n",
        )

    def test_triage_warnings_are_notes_not_gaps(self) -> None:
        repo, base = self.new_repo({"src/a.py": BASE_A + "new1\n"})
        with open(os.path.join(repo, "src/a.py"), "a", encoding="utf-8") as f:
            f.write("uncommitted\n")
        plan = self.triage(repo, base)
        out = self.report(plan, findings_doc([]), []).stdout
        self.assertIn("- Notes: dirty tree: uncommitted changes are not reviewed", out)
        self.assertNotIn("- Not covered:", out)

    def test_finding_on_a_path_missing_at_head_warns(self) -> None:
        instances = [{"file": "src/b.py", "line": 1}, {"file": "src/gone.py", "line": 2}]
        doc = findings_doc([row(cls="ghost", file="src/ghost.py", line=1, instances=instances)])
        _, out = self.status(FX.standard, doc)
        head = rev_parse(FX.standard.repo)[:12]
        self.assertIn(f"warning: src/ghost.py does not exist at head {head} or the merge base", out)
        self.assertIn("warning: src/gone.py does not exist at head", out)
        self.assertNotIn("src/b.py does not exist", out)
        self.assertNotIn("src/a.py does not exist", out)

    def test_finding_on_a_file_the_change_deletes_does_not_warn(self) -> None:
        repo, base = self.new_repo({"src/old.py": None}, base_files={"src/old.py": "x\n"})
        plan = self.triage(repo, base)
        _, out = self.status(plan, findings_doc([row(cls="callers-break", file="src/old.py")]))
        self.assertNotIn("does not exist", out)

    def test_unread_must_be_a_list_of_strings(self) -> None:
        proc = self.report(FX.standard, findings_doc([], unread="src/big.py"), [], code=2)
        self.assertIn("unread", proc.stderr)

    def test_pre_existing_only_headline_says_so(self) -> None:
        verdicts = [
            verdict(A, "facts", refuted=False),
            verdict(A, "impact", refuted=False, pre_existing=True),
        ]
        first = self.report(FX.standard, findings_doc([row()]), verdicts).stdout.split("\n")[0]
        self.assert_starts_with(first, "**Nothing to fix.**")
        self.assertNotIn("None of the", first)
        self.assertIn("pre-existing", first)

    def test_finding_downgrades_a_low_risk_approval_hint(self) -> None:
        repo, base = self.new_repo(
            {"docs/guide.md": "hello\nworld\n"},
            base_files={
                "docs/guide.md": "hello\n",
                "src/c.py": "".join(f"c{i}\n" for i in range(1, 20)),
            },
        )
        plan = self.triage(repo, base)
        self.assertEqual(plan.data["approval"]["hint"], "low risk", plan.data["approval"])
        doc_bug = finding_id("correctness", "wrong-doc", "docs/guide.md")
        doc = findings_doc([row(cls="wrong-doc", file="docs/guide.md", line=2)])
        proc = self.report(plan, doc, upheld(doc_bug))
        self.assertIn("Approval hint: needs a human (1 findings to fix)", proc.stdout)

    def test_fold_buckets_sum_to_the_candidates(self) -> None:
        pre = finding_id("correctness", "pre-bug", "src/a.py")
        refuted = finding_id("correctness", "refuted-bug", "src/a.py")
        rows = [
            row(cls="added-bug", line=30),
            row(cls="pre-bug", line=4),
            row(cls="refuted-bug", line=12),
            dict(SECURITY_ROW, severity="LOW"),
        ]
        verdicts = (
            upheld(G1)
            + [
                verdict(pre, "facts", refuted=False),
                verdict(pre, "impact", refuted=False, pre_existing=True),
            ]
            + [verdict(refuted, s, refuted=True) for s in ("facts", "impact")]
            + [
                verdict(G1, "reproduce", result="upheld"),
                verdict(pre, "reproduce", result="upheld"),
                verdict(refuted, "reproduce", result="refuted"),
            ]
        )
        out = self.report(FX.standard, findings_doc(rows, candidates=5), verdicts).stdout
        line, n, buckets = fold_buckets(out)
        self.assertEqual(n, 5, line)
        self.assertEqual(sum(buckets.values()), 5, line)
        expected = {
            "confirmed": 1,
            "pre-existing": 1,
            "rejected": 1,
            "LOW security dropped": 1,
            "merged duplicates": 1,
        }
        for label, count in expected.items():
            self.assertEqual(buckets.get(label), count, line)
        self.assertTrue(line.startswith("- Kept 2 of 5"), line)

    def test_warnings_keep_their_order_across_hash_seeds(self) -> None:
        findings = self.json_file("findings.json", findings_doc([row()]))
        unknown = [verdict(f"CR-0000000{i}", "facts", refuted=False) for i in range(1, 10)]
        verdicts = write_jsonl(self.path("v-order.jsonl"), unknown)
        outputs = set()
        for seed in ("1", "2", "3"):
            env = hermetic_env(PYTHONHASHSEED=seed)
            proc = report_files(FX.standard, findings, verdicts, "--status", env=env)
            outputs.add(proc.stdout)
        self.assertEqual(len(outputs), 1, outputs)


class EmptyPlanTests(ReportTestCase):
    """An empty change reviews nothing: the known file is carried and findings are optional."""

    @classmethod
    def setUpClass(cls) -> None:
        super().setUpClass()
        work = cls.make_class_tmpdir()
        repo, cls.base = report_repo(work, {})
        known = write_json(os.path.join(work, "ek.json"), KNOWN)
        cls.plan = make_plan(work, repo, cls.base, "--known", known)
        cls.known_out = os.path.join(work, "ek-out.json")
        cls.proc = expect_report(
            work, cls.plan, findings_doc([]), None, "--known-out", cls.known_out
        )

    def test_empty_change_headline(self) -> None:
        self.assertEqual(self.plan.data["empty"], "change")
        self.assert_starts_with(self.proc.stdout, f"**No changes between {self.base} and HEAD.**")
        self.assertNotIn("Nothing to fix", self.proc.stdout)

    def test_empty_change_keeps_the_known_file(self) -> None:
        expected = dict(KNOWN, findings=[dict(i, instances=[]) for i in KNOWN["findings"]])
        self.assertEqual(load_json(self.known_out), expected)

    def test_missing_findings_file_is_only_a_warning(self) -> None:
        proc = report_files(self.plan, self.path("missing.json"))
        self.assertEqual(proc.returncode, 0, proc.stderr[-300:])
        self.assertIn("findings file", proc.stdout)

    def test_malformed_findings_file_is_only_a_warning(self) -> None:
        bad = self.path("bad.json")
        with open(bad, "w", encoding="utf-8") as f:
            f.write('{"version": 2}')
        proc = report_files(self.plan, bad)
        self.assertEqual(proc.returncode, 0, proc.stderr[-300:])
        self.assertIn("findings file", proc.stdout)


class BinaryReportTests(ReportTestCase):
    @classmethod
    def setUpClass(cls) -> None:
        super().setUpClass()
        work = cls.make_class_tmpdir()

        # A binary file, and a known findings file inside the work tree.
        repo, base = report_repo(work, {"assets/x.bin": "a\0b\n", "src/a.py": "changed\n"})
        inside = write_json(os.path.join(repo, "known.json"), KNOWN)
        plan = make_plan(work, repo, base, "--known", inside)
        cls.incomplete_json = os.path.join(work, "bgl.json")
        doc = findings_doc([], unread=["src/a.py"])
        cls.incomplete = expect_report(work, plan, doc, [], "--gitlab-json", cls.incomplete_json)

        # A NUL byte makes a code file binary, and it is not a media format.
        repo, base = report_repo(
            work, {"app.js": 'require("child_process").execSync(process.env.Q) // \0\n'}
        )
        cls.nul_plan = make_plan(work, repo, base)
        cls.nul_report = expect_report(work, cls.nul_plan, findings_doc([]), [])
        cls.nul_json = os.path.join(work, "jgl.json")
        expect_report(work, cls.nul_plan, findings_doc([]), [], "--gitlab-json", cls.nul_json)

        repo, base = report_repo(work, {"img/logo.png": "\x89PNG\0\0", "src/a.py": "changed\n"})
        cls.png_plan = make_plan(work, repo, base)
        cls.png_report = expect_report(work, cls.png_plan, findings_doc([]), [])

        repo, base = report_repo(
            work,
            {"src/a.py": "changed\n"},
            base_files={"src/a.py": "x\n", "vendor/old.whl": "PK\0\0"},
        )
        os.remove(os.path.join(repo, "vendor/old.whl"))
        git(repo, "add", "-A")
        git(repo, "commit", "-q", "-m", "drop wheel")
        cls.deleted_plan = make_plan(work, repo, base)
        cls.deleted_report = expect_report(work, cls.deleted_plan, findings_doc([]), [])

    def test_binary_files_are_listed_as_not_reviewed(self) -> None:
        self.assertIn("binary files not reviewed: assets/x.bin", self.incomplete.stdout)
        self.assertIn("binary files not reviewed;", self.incomplete.stdout)

    def test_known_file_inside_the_tree_is_named_in_the_report(self) -> None:
        self.assertIn("known findings file inside the reviewed tree", self.incomplete.stdout)

    def test_incomplete_review_gitlab_summary_says_so(self) -> None:
        self.assertIn("Review incomplete", load_json(self.incomplete_json)["summary"])

    @unittest.skipUnless(HAVE_UV, "review.py runs through uv")
    def test_review_py_display_accepts_the_incomplete_report(self) -> None:
        proc = review_py_display(self.incomplete_json)
        self.assertEqual(proc.returncode, 0, proc.stderr[-400:])

    def test_nul_byte_in_a_code_file_is_binary_not_media(self) -> None:
        binary = self.nul_plan.data["changed"]["binary"]
        self.assertEqual([(b["path"], b["media"]) for b in binary], [("app.js", False)])

    def test_nul_byte_in_a_code_file_makes_the_review_incomplete(self) -> None:
        first = self.nul_report.stdout.split("\n")[0]
        self.assertEqual(first, "**Review incomplete:** 1 binary files not reviewed")

    def test_nul_byte_in_a_code_file_never_says_nothing_to_fix(self) -> None:
        self.assertNotIn("Nothing to fix", self.nul_report.stdout)

    def test_nul_byte_in_a_code_file_makes_the_gitlab_summary_incomplete(self) -> None:
        summary = load_json(self.nul_json)["summary"]
        self.assert_starts_with(summary, "**Review incomplete:**")

    def test_png_is_media(self) -> None:
        binary = self.png_plan.data["changed"]["binary"]
        self.assertEqual([(b["path"], b["media"]) for b in binary], [("img/logo.png", True)])

    def test_media_only_binary_says_nothing_to_fix(self) -> None:
        self.assert_starts_with(
            self.png_report.stdout, "**Nothing to fix.** No lens reported a finding."
        )

    def test_media_binary_is_still_listed_and_needs_a_human(self) -> None:
        self.assertIn("binary files not reviewed: img/logo.png", self.png_report.stdout)
        self.assertIn("needs a human", self.png_report.stdout)

    def test_plan_without_a_media_flag_counts_binaries_as_opaque(self) -> None:
        legacy = dict(self.png_plan.data)
        legacy["changed"] = {
            k: v for k, v in self.png_plan.data["changed"].items() if k != "opaque_binary"
        }
        legacy["changed"]["binary"] = [
            {k: v for k, v in b.items() if k not in ("media", "deleted")}
            for b in self.png_plan.data["changed"]["binary"]
        ]
        path = self.json_file("legacy-plan.json", legacy)
        plan = Plan(self.png_plan.repo, self.png_plan.base, path, legacy)
        proc = self.report(plan, findings_doc([]), [])
        self.assert_starts_with(proc.stdout, "**Review incomplete:** 1 binary files not reviewed")

    def test_deleted_archive_is_listed_but_not_counted(self) -> None:
        changed = self.deleted_plan.data["changed"]
        self.assertEqual([b["path"] for b in changed["binary"]], ["vendor/old.whl"])
        self.assertEqual(changed["opaque_binary"], 0)

    def test_deleted_archive_says_nothing_to_fix(self) -> None:
        self.assert_starts_with(self.deleted_report.stdout, "**Nothing to fix.**")

    def test_binary_count_is_not_cut_at_the_list_cap(self) -> None:
        files = {f"blobs/b{i:03}.dat": "\0" for i in range(230)}
        files["src/a.py"] = "changed\n"
        repo, base = self.new_repo(files)
        plan = self.triage(repo, base)
        proc = self.report(plan, findings_doc([]), [])
        self.assertEqual(len(plan.data["changed"]["binary"]), 200)
        self.assert_starts_with(proc.stdout, "**Review incomplete:** 230 binary files not reviewed")


class GitLabJsonTests(ReportTestCase):
    """--gitlab-json output on the standard plan, where only src/a.py:30 is an added line."""

    @classmethod
    def setUpClass(cls) -> None:
        super().setUpClass()
        work = cls.make_class_tmpdir()

        pre = finding_id("testing", "pre-bug", "src/a.py")
        cls.mapping_path = os.path.join(work, "gl.json")
        rows = [
            row(cls="added-bug", line=30),
            row(cls="old-line-bug", line=2, sev="major"),
            row(lens="testing", cls="pre-bug", line=4, sev="minor"),
        ]
        verdicts = (
            upheld(G1)
            + upheld(G2)
            + [
                verdict(pre, "facts", refuted=False),
                verdict(pre, "impact", refuted=False, pre_existing=True),
            ]
        )
        expect_report(
            work, FX.standard, findings_doc(rows), verdicts, "--gitlab-json", cls.mapping_path
        )
        cls.mapping = load_json(cls.mapping_path)
        cls.pre_existing_id = pre

        # Finding text with GitLab quick actions at the start of a line.
        quick = finding_id("correctness", "quick-action", "src/a.py")
        listed = finding_id("correctness", "listed-only", "src/a.py")
        rows = [
            row(
                cls="quick-action",
                line=30,
                title="Title\n/unlabel ~x",
                evidence="Line 3 reads:\n/label ~approved\n   /close",
                fix="/approve",
                instances=[{"file": "docs\n/approve now/notes.txt", "line": 1}],
            ),
            row(
                cls="listed-only", line=2, sev="major", title="Listed\n\n/approve", file="src/a.py"
            ),
        ]
        cls.quick_path = os.path.join(work, "glq.json")
        expect_report(
            work,
            FX.standard,
            findings_doc(rows),
            upheld(quick) + upheld(listed),
            "--gitlab-json",
            cls.quick_path,
        )
        cls.quick = load_json(cls.quick_path)

        rows = [row(cls="added-bug", line=30), row(cls="old-line-bug", line=2, sev="major")]
        cls.summary_path = os.path.join(work, "gl34.json")
        expect_report(
            work,
            FX.standard,
            findings_doc(rows),
            upheld(G1) + upheld(G2),
            "--gitlab-json",
            cls.summary_path,
        )
        cls.summary = load_json(cls.summary_path)

    def test_only_added_lines_get_inline_comments_and_every_finding_is_summarized(self) -> None:
        g = self.mapping
        self.assertEqual(len(g["inline_comments"]), 1, g)
        comment = g["inline_comments"][0]
        self.assertEqual(comment["line"], 30)
        self.assertTrue(comment["comment"].startswith(f"[{G1}]"), comment)
        self.assertEqual(comment["severity"], "critical")
        self.assert_starts_with(g["summary"], "**2 to fix** (1 inline, all listed below)")
        self.assertIn(f"- [{G2}]", g["summary"])
        self.assertIn(f"- [{self.pre_existing_id}] pre-existing", g["summary"])
        self.assertEqual(g["positive_aspects"], [])
        self.assertNotIn("fix_prompt", g)

    @unittest.skipUnless(HAVE_UV, "review.py runs through uv")
    def test_review_py_display_accepts_the_mapping(self) -> None:
        proc = review_py_display(self.mapping_path)
        self.assertEqual(proc.returncode, 0, proc.stderr[-400:])

    def test_no_line_starts_with_a_quick_action(self) -> None:
        g = self.quick
        texts = [g["summary"]] + [c["comment"] for c in g["inline_comments"]]
        slash = [ln for t in texts for ln in t.split("\n") if ln.lstrip().startswith("/")]
        self.assertTrue(g["inline_comments"], g)
        self.assertEqual(slash, [])

    def test_quick_actions_get_a_backslash_and_paths_are_escaped(self) -> None:
        self.assertTrue(self.quick["inline_comments"], self.quick)
        comment = self.quick["inline_comments"][0]["comment"]
        self.assertIn("\\/label ~approved", comment)
        self.assertIn("docs\\x0a", comment)

    @unittest.skipUnless(HAVE_UV, "review.py runs through uv")
    def test_review_py_display_accepts_neutralized_quick_actions(self) -> None:
        proc = review_py_display(self.quick_path)
        self.assertEqual(proc.returncode, 0, proc.stderr[-400:])

    def test_summary_lists_inline_findings_too(self) -> None:
        g = self.summary
        summary = g["summary"]
        self.assertEqual([c["comment"][:13] for c in g["inline_comments"]], [f"[{G1}]"])
        self.assert_starts_with(summary, "**2 to fix** (1 inline, all listed below)")
        self.assertIn(
            f"- [{G1}] blocking, inline: added-bug in src/a.py (src/a.py:30, correctness)", summary
        )
        self.assertIn(
            f"- [{G2}] major: old-line-bug in src/a.py (src/a.py:2, correctness)", summary
        )
        self.assertLess(summary.index(f"[{G1}]"), summary.index(f"[{G2}]"))

    @unittest.skipUnless(HAVE_UV, "review.py runs through uv")
    def test_review_py_display_accepts_the_summary_listing(self) -> None:
        proc = review_py_display(self.summary_path)
        self.assertEqual(proc.returncode, 0, proc.stderr[-400:])

    def test_inline_comment_carries_new_evidence(self) -> None:
        gitlab = self.path("glk.json")
        again = row(cls="wrong-bound", known_id=A, new_evidence="new caller", line=30)
        self.report(FX.known, findings_doc([again]), upheld(A), "--gitlab-json", gitlab)
        g = load_json(gitlab)
        self.assertTrue(g["inline_comments"], g)
        self.assertIn("Seen before; new evidence: new caller", g["inline_comments"][0]["comment"])

    def mention_report(self) -> tuple[str, dict]:
        mention = finding_id("correctness", "mention", "src/a.py")
        rows = [
            row(
                cls="mention",
                line=30,
                title="Ping @all <!-- ai-review sha:deadbeef -->",
                evidence="cc @group/sub, &#64;all, &#X40;here, &commat;team, a@example.com",
                fix="Drop the `@here` and the `<!--`.",
            )
        ]
        gitlab = self.path("gl-mention.json")
        self.report(FX.standard, findings_doc(rows), upheld(mention), "--gitlab-json", gitlab)
        return gitlab, load_json(gitlab)

    def test_mentions_and_html_comments_are_neutralized(self) -> None:
        _, g = self.mention_report()
        texts = [g["summary"]] + [c["comment"] for c in g["inline_comments"]]
        self.assertTrue(g["inline_comments"], g)
        for text in texts:
            self.assertNotIn("<!--", text)
            self.assertIsNone(re.search(r"@[\w.-]", text), text)
            self.assertIsNone(re.search(r"&(#0*64;|#x0*40;|commat;)", text, re.I), text)
            # review.py reads the previous review's SHA from the first match of its marker.
            self.assertIsNone(re.search(r"<!-- ai-review sha:([0-9a-f]+) -->", text))
        self.assertIn("@\u200ball", g["summary"])
        self.assertIn("@\u200bgroup/sub", g["inline_comments"][0]["comment"])

    @unittest.skipUnless(HAVE_UV, "review.py runs through uv")
    def test_review_py_display_accepts_neutralized_mentions(self) -> None:
        path, _ = self.mention_report()
        proc = review_py_display(path)
        self.assertEqual(proc.returncode, 0, proc.stderr[-400:])

    def test_added_lines_diff_only_the_files_with_findings(self) -> None:
        findings = self.json_file("f.json", findings_doc([row(cls="added-bug", line=30)]))
        verdicts = write_jsonl(self.path("v.jsonl"), upheld(G1))
        trace = self.path("trace.txt")
        gitlab = self.path("gl16.json")
        env = hermetic_env(GIT_TRACE=trace)
        proc = report_files(FX.standard, findings, verdicts, "--gitlab-json", gitlab, env=env)
        lines = ""
        if os.path.exists(trace):
            with open(trace, encoding="utf-8") as f:
                lines = f.read()
        diffs = [ln for ln in lines.splitlines() if "'diff'" in ln or " diff " in ln]
        # The rename query lists paths (--raw), never patches; only the patch diffs are scoped.
        patches = [ln for ln in diffs if " -U0 " in ln]
        self.assertEqual(proc.returncode, 0, proc.stderr[-200:])
        self.assertTrue(patches)
        for line in patches:
            self.assertIn(":(literal,top)src/a.py", line)
        for line in diffs:
            self.assertTrue(
                line in patches or ("--raw" in line and "--find-renames=30%" in line), line
            )


class RenameInlineTests(ReportTestCase):
    """GitLab pairs a renamed file with its old path, so only lines the rename adds are inline."""

    def gitlab_lines(
        self, new_content: str, old_content: str = BASE_A, lines: tuple = (3, 5, 30)
    ) -> set[int]:
        repo, base = self.new_repo(
            {"src/old.py": None, "src/new.py": new_content}, base_files={"src/old.py": old_content}
        )
        plan = self.triage(repo, base)
        rows = [row(cls=f"bug-{n}", file="src/new.py", line=n) for n in lines]
        verdicts = [
            v for n in lines for v in upheld(finding_id("correctness", f"bug-{n}", "src/new.py"))
        ]
        gitlab = self.path("gl-rename.json")
        self.report(plan, findings_doc(rows), verdicts, "--gitlab-json", gitlab)
        return {c["line"] for c in load_json(gitlab)["inline_comments"]}

    def test_renamed_and_edited_file_gets_inline_comments_on_added_lines_only(self) -> None:
        edited = BASE_A.replace("line5\n", "five\n") + "line30\n"
        self.assertEqual(self.gitlab_lines(edited), {5, 30})

    def test_pure_rename_gets_no_inline_comment(self) -> None:
        self.assertEqual(self.gitlab_lines(BASE_A), set())

    def test_rename_below_git_default_similarity_is_paired_as_gitlab_does(self) -> None:
        """About 40% similar: git -M sees a delete and an add, GitLab (30%) a rename."""
        old = "".join(f"keep line {i} of the module\n" for i in range(1, 11))
        new = "".join(old.splitlines(True)[:4]) + "".join(f"rewritten {i}\n" for i in range(5, 11))
        self.assertEqual(self.gitlab_lines(new, old, lines=(2, 6)), {6})


class DomainLensRoundTripTests(ReportTestCase):
    """A supply-chain finding whose fix removes the line that triggered the lens still closes."""

    def test_finding_closes_after_its_trigger_is_removed(self) -> None:
        repo, base = self.new_repo({"scripts/get.sh": "curl https://x.example/i.sh | sh\n"})
        fid = finding_id("supply-chain", "unpinned-download", "scripts/get.sh")
        plan1 = self.triage(repo, base)
        self.assertIn("supply-chain", plan1.data["domains"])
        k1 = self.path("k1.json")
        rows = [row(lens="supply-chain", cls="unpinned-download", file="scripts/get.sh", line=1)]
        self.report(plan1, findings_doc(rows), upheld(fid), "--known-out", k1)

        commit(repo, {"scripts/get.sh": "echo pinned\n"})
        plan2 = self.triage(repo, base, "--known", k1)
        self.assertNotIn("supply-chain", plan2.data["domains"])
        lens = next((le for le in plan2.data["lenses"] if le["name"] == "supply-chain"), None)
        self.assertIsNotNone(lens, plan2.data["lenses"])
        self.assertEqual(lens["kept_for"], [fid])
        k2 = self.path("k2.json")
        fixed = [
            {
                "id": fid,
                "reason": "scripts/get.sh:1 no longer downloads anything.",
                "lens": "supply-chain",
            }
        ]
        out = self.report(plan2, findings_doc([], fixed=fixed), [], "--known-out", k2).stdout
        doc = load_json(k2)
        self.assert_starts_with(out, "**Nothing to fix.**")
        self.assertEqual(doc["rounds_with_open_findings"], 0, doc)

        plan3 = self.triage(repo, base, "--known", k2)
        self.assertEqual(plan3.data["open_known"], [])
        self.assertNotIn("supply-chain", {le["name"] for le in plan3.data["lenses"]})


class PathNormalizationTests(ReportTestCase):
    def test_finding_paths_are_read_from_the_repo_root(self) -> None:
        norm = finding_id("correctness", "norm-bug", "src/a.py")
        absolute = os.path.join(FX.standard.repo, "src/a.py")
        for label, path in (("./", "./src/a.py"), ("b/", "b/src/a.py"), ("absolute", absolute)):
            with self.subTest(label):
                instances = [{"file": "./src/b.py", "line": 2}]
                doc = findings_doc([row(cls="norm-bug", file=path, instances=instances)])
                st, out = self.status(FX.standard, doc)
                self.assertEqual(st.get(norm), "pending", out)
                self.assertIn("read as", out)

    def test_a_prefix_is_kept_for_a_changed_path_and_b_prefix_stripped(self) -> None:
        repo, base = self.new_repo({"a/x.py": "1\n", "x.py": "2\n"}, base_files={"README": "r\n"})
        plan = self.triage(repo, base)
        doc = findings_doc([row(cls="keep-a", file="a/x.py"), row(cls="strip-b", file="b/x.py")])
        st, out = self.status(plan, doc)
        self.assertEqual(st.get(finding_id("correctness", "keep-a", "a/x.py")), "pending", out)
        self.assertEqual(st.get(finding_id("correctness", "strip-b", "x.py")), "pending", out)


class InputFileTests(ReportTestCase):
    """Odd input files and a missing git: exit 2 with a message, never a traceback."""

    def test_unicode_line_break_inside_a_verdict_string_keeps_the_line_whole(self) -> None:
        verdicts = self.path("v-nel.jsonl")
        with open(verdicts, "w", encoding="utf-8") as f:
            for skeptic in ("facts", "impact"):
                v = verdict(A, skeptic, refuted=False, reason="a b c\u0085d")
                f.write(json.dumps(v, ensure_ascii=False) + "\n")
        findings = self.json_file("f.json", findings_doc([row()]))
        proc = report_files(FX.standard, findings, verdicts)
        self.assertEqual(proc.returncode, 0, proc.stderr[-300:])
        self.assert_starts_with(proc.stdout, "**1 to fix:**")

    def test_findings_file_that_is_not_utf8_exits_2_naming_it(self) -> None:
        bad = self.path("latin1.json")
        with open(bad, "wb") as f:
            f.write(b'{"version": 1, "independent": true, "findings": [], "x": "caf\xe9"}')
        proc = report_files(FX.standard, bad)
        self.assertEqual(proc.returncode, 2, proc.stderr[-300:])
        self.assertIn("latin1.json", proc.stderr)
        self.assertNotIn("Traceback", proc.stderr)

    def test_verdicts_file_that_is_not_utf8_exits_2_naming_it(self) -> None:
        findings = self.json_file("f.json", findings_doc([row()]))
        bad = self.path("latin1.jsonl")
        with open(bad, "wb") as f:
            f.write(
                b'{"id": "CR-516ea425", "skeptic": "facts", "refuted": false,'
                b' "reason": "caf\xe9"}\n'
            )
        proc = report_files(FX.standard, findings, bad)
        self.assertEqual(proc.returncode, 2, proc.stderr[-300:])
        self.assertIn("latin1.jsonl", proc.stderr)
        self.assertNotIn("Traceback", proc.stderr)

    def test_missing_git_exits_2(self) -> None:
        findings = self.json_file("f.json", findings_doc([row(cls="added-bug", line=30)]))
        verdicts = write_jsonl(self.path("v.jsonl"), upheld(G1))
        gitlab = self.path("nogit.json")
        env = hermetic_env(PATH="/nonexistent")
        proc = report_files(FX.standard, findings, verdicts, "--gitlab-json", gitlab, env=env)
        self.assertEqual(proc.returncode, 2, proc.stderr[-300:])
        self.assertIn("cannot run git", proc.stderr)
        self.assertNotIn("Traceback", proc.stderr)


if __name__ == "__main__":
    unittest.main()
