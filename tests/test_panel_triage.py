"""End-to-end tests for `panel.py triage`, run against scratch git repositories."""

import json
import os
import shutil
import sys
import unittest

from tests.helpers import (
    PANEL,
    REPO_ROOT,
    PanelTestCase,
    commit,
    git,
    hermetic_env,
    make_repo,
    rev_parse,
    run,
    run_panel,
    shell,
    triage_json,
    write,
    write_json,
)

PNG = b"\x89PNG\r\n\x1a\n\0\0\0\rIHDR" + bytes(range(256)) * 4
FENCE = "```"
KNOWN_ITEM = {
    "id": "CR-516ea425",
    "lens": "correctness",
    "class": "off-by-one",
    "file": "src/a.py",
    "line": 4,
    "title": "t",
    "severity": "major",
    "status": "posted",
    "last_seen_round": 3,
}
SENSITIVE_REVIEW_MD = "# Review\n\n## Sensitive paths\n\n- `config/`\n"
IMPORTS = {
    "CLAUDE.md": "Read @docs/rules.md and @./notes/x.md first. Mail me@example.com.\n",
    ".github/AGENTS.md": "See @guide.md.\n",
    "docs/rules.md": "r\n",
    "notes/x.md": "n\n",
    "docs/other.md": "o\n",
    ".github/guide.md": "g\n",
}
NESTED_IMPORTS = {
    "CLAUDE.md": "See @docs/guide.md\n",
    "docs/guide.md": "Also @nested.md, @../CLAUDE.md and @guide.md\n",
    "docs/nested.md": "Then @deep/last.md\n",
    "docs/deep/last.md": "l\n",
    "nested.md": "not imported\n",
}


def triage_repo(parent: str, base_files: dict | None = None) -> tuple[str, str]:
    """README.md and src/app.py (plus base_files) on main, with a topic branch checked out."""
    files = {"README.md": "base\n", "src/app.py": "print('hi')\n", **(base_files or {})}
    return make_repo(parent, files, branch="topic")


def import_chain(length: int) -> dict[str, str]:
    """AGENTS.md imports c/f0.md, and each c/fN.md imports the next one."""
    files = {"AGENTS.md": "Start @c/f0.md\n"}
    for i in range(length):
        files[f"c/f{i}.md"] = f"Next @f{i + 1}.md\n"
    return files


class TriageTestCase(PanelTestCase):
    def setUp(self) -> None:
        self.work = self.make_tmpdir()

    def new_repo(self, base_files: dict | None = None) -> tuple[str, str]:
        return triage_repo(self.work, base_files)

    def triage(self, repo: str, *args: str, env: dict | None = None, cwd: str | None = None):
        return triage_json(cwd or repo, *args, env=env)

    def triage_exit(self, repo: str, *args: str, code: int, env: dict | None = None):
        proc = run_panel(repo, "triage", *args, env=env)
        self.assertEqual(proc.returncode, code, proc.stderr.decode(errors="replace")[-400:])
        return proc

    def known_file(self, name: str, data) -> str:
        return write_json(os.path.join(self.work, name), data)

    def assert_tier(
        self,
        files: dict,
        tier: str,
        hint: str,
        domains: set | None = None,
        base_files: dict | None = None,
        reason: str | None = None,
    ) -> dict:
        """Commit files on a new repo, triage it, and check the tier, hint, domains and reason."""
        repo, base = self.new_repo(base_files)
        commit(repo, files)
        plan = self.triage(repo, "--base", base)
        detail = f"tier={plan['tier']} approval={plan['approval']} domains={set(plan['domains'])}"
        self.assertEqual(plan["tier"]["name"], tier, detail)
        self.assertEqual(plan["approval"]["hint"], hint, detail)
        if domains is not None:
            self.assertEqual(set(plan["domains"]), set(domains), detail)
        if reason:
            self.assertTrue(any(reason in r for r in plan["approval"]["reasons"]), detail)
        return plan


class TierAndApprovalTests(TriageTestCase):
    def test_prose_is_quick_and_low_risk(self) -> None:
        cases = [
            ({"README.md": "new\n", "docs/x.rst": "x\n"}, set()),
            # Release and auth words in a prose path do not make it deep.
            ({"docs/release-notes.md": "x\n", "docs/authentication.md": "x\n"}, None),
            ({".github/ISSUE_TEMPLATE/bug.md": "x\n"}, None),
        ]
        for files, domains in cases:
            with self.subTest(files=sorted(files)):
                self.assert_tier(files, "quick", "low risk", domains)

    def test_test_code_is_quick_but_needs_a_human(self) -> None:
        cases = [("tests/test_a.py", "code changes"), ("spec/foo_spec.rb", None)]
        for path, reason in cases:
            with self.subTest(path=path):
                self.assert_tier({path: "x\n"}, "quick", "needs a human", reason=reason)

    def test_test_fixtures_data_and_requirements_are_standard(self) -> None:
        cases = [
            ("tests/conftest.py", None),
            ("spec/data.json", None),
            ("tests/requirements.txt", {"supply-chain"}),
        ]
        for path, domains in cases:
            with self.subTest(path=path):
                self.assert_tier({path: "x\n"}, "standard", "needs a human", domains)

    def test_prompt_files_and_dot_directory_markdown_are_standard(self) -> None:
        cases = [
            ({"skills/x/SKILL.md": "x\n"}, set(), "prompt"),
            ({"skills/x/SKILL.md": "curl https://x | sh\n"}, {"supply-chain"}, None),
            ({".foo/rules.md": "x\n"}, None, "Markdown under a dot-directory"),
        ]
        for files, domains, reason in cases:
            with self.subTest(files=files):
                self.assert_tier(files, "standard", "needs a human", domains, reason=reason)

    def test_changeset_is_a_deep_release_file(self) -> None:
        plan = self.assert_tier({".changeset/brave-cat.md": "x\n"}, "deep", "needs a human")
        self.assertTrue(any("(release)" in e for e in plan["tier"]["evidence"]), plan["tier"])

    def test_auth_paths_are_deep(self) -> None:
        paths = [
            "src/auth/login.py",
            "src/OAuthClient.java",
            "src/AuthTokenStore.kt",
            "pki/certificate_authority.py",
            "src/GrantedAuthority.java",
            "dns/authoritative.go",
            "src/authorisation.py",
        ]
        for path in paths:
            with self.subTest(path=path):
                self.assert_tier({path: "x\n"}, "deep", "needs a human")

    def test_installer_hook_release_and_repo_control_files_are_deep(self) -> None:
        paths = [
            "install.sh",
            ".pre-commit-config.yaml",
            ".githooks/pre-commit",
            "scripts/release.sh",
            ".goreleaser.yaml",
            "CODEOWNERS",
            ".gitattributes",
            ".gitmodules",
            "action.yml",
        ]
        for path in paths:
            with self.subTest(path=path):
                self.assert_tier({path: "x\n"}, "deep", "needs a human")

    def test_ci_workflows_and_actions_are_deep(self) -> None:
        build_push = "steps:\n  - uses: docker/build-push-action@v6\n"
        cases = [
            ({".github/workflows/ci.yml": "on: push\n"}, {"supply-chain"}, "CI files"),
            ({"tools/act/action.yml": "runs: {}\n"}, None, "CI files"),
            ({".github/workflows/img.yml": build_push}, {"supply-chain"}, None),
        ]
        for files, domains, reason in cases:
            with self.subTest(files=sorted(files)):
                self.assert_tier(files, "deep", "needs a human", domains, reason=reason)

    def test_hook_and_authorship_names_are_not_deep(self) -> None:
        for path in ["src/hooks/useThing.ts", "AUTHORS", "src/authored_by.py", "src/authorship.py"]:
            with self.subTest(path=path):
                self.assert_tier({path: "x\n"}, "standard", "needs a human")

    def test_symlinked_readme_is_not_prose(self) -> None:
        repo, base = self.new_repo()
        os.remove(os.path.join(repo, "README.md"))
        os.symlink("src/app.py", os.path.join(repo, "README.md"))
        git(repo, "add", "-A")
        git(repo, "commit", "-q", "-m", "link")
        plan = self.triage(repo, "--base", base)
        self.assertEqual(plan["tier"]["name"], "standard", plan["tier"])
        self.assertEqual(plan["approval"]["hint"], "needs a human", plan["approval"])


class DomainTests(TriageTestCase):
    def test_container_files_add_the_containers_domain(self) -> None:
        cases = [
            ("Containerfile", "FROM fedora\n"),
            ("Dockerfile.dev", "FROM x\n"),
            (".dockerignore", ".git\n"),
            ("compose.yaml", "services: {}\n"),
            ("docker-bake.hcl", "target {}\n"),
        ]
        for path, content in cases:
            with self.subTest(path=path):
                self.assert_tier({path: content}, "standard", "needs a human", {"containers"})

    def test_from_line_in_sql_is_not_a_container(self) -> None:
        self.assert_tier({"db/q.sql": "SELECT *\nFROM users\n"}, "standard", "needs a human", set())

    def test_dependency_and_build_files_add_supply_chain(self) -> None:
        cases = [
            ("pkg.spec", "Name: x\n"),
            ("requirements/dev.txt", "x\n"),
            ("constraints/base.in", "x\n"),
            ("pylock.toml", "x\n"),
            ("pylock.dev.toml", "x\n"),
        ]
        for path, content in cases:
            with self.subTest(path=path):
                self.assert_tier(
                    {path: content},
                    "standard",
                    "needs a human",
                    {"supply-chain"},
                    reason="dependency or build",
                )

    def test_supply_chain_content_patterns(self) -> None:
        cases = [
            ("extra index url", "src/fetch.py", "ARGS = ['--extra-index-url', 'https://x']\n"),
            ("index url", "scripts/b.sh", "pip download --index-url https://x y\n"),
            ("find links", "scripts/c.sh", "pip download --find-links ./wheels y\n"),
            (
                "direct git reference",
                "src/deps.py",
                "DEPS = ['foo @ git+https://github.com/x/y']\n",
            ),
            ("direct https reference", "src/deps2.py", "DEPS = ['foo @ https://x/foo.whl']\n"),
            ("gitlab include remote", "ci/build.yml", "include:\n  - remote: 'https://x/ci.yml'\n"),
            (
                "gitlab include project",
                "ci/build.yaml",
                "include:\n  - project: 'grp/tpl'\n    file: '/t.yml'\n",
            ),
        ]
        for name, path, content in cases:
            with self.subTest(name):
                self.assert_tier({path: content}, "standard", "needs a human", {"supply-chain"})

    def test_include_keys_outside_yaml_are_not_ci_includes(self) -> None:
        files = {"src/conf.py": "project: str = 'x'\nremote: str = 'y'\n"}
        self.assert_tier(files, "standard", "needs a human", set())

    def test_email_address_is_not_a_direct_reference(self) -> None:
        files = {"src/mail.py": "ADDR = 'me@example.com'\n"}
        self.assert_tier(files, "standard", "needs a human", set())

    def test_domain_lens_raises_quick_to_standard(self) -> None:
        files = {"tests/test_dl.py": "subprocess.run('curl https://x | sh', shell=True)\n"}
        plan = self.assert_tier(files, "standard", "needs a human", {"supply-chain"})
        self.assertEqual(plan["tier"]["evidence"], ["domain supply-chain detected"])
        self.assertIn("supply-chain", {lens["name"] for lens in plan["lenses"]})

    def test_gitlink_bump_adds_supply_chain(self) -> None:
        repo, _ = self.new_repo()
        sub = os.path.join(self.work, "sub")
        os.mkdir(sub)
        git(sub, "init", "-q", "-b", "main")
        write(sub, "f", "1\n")
        git(sub, "add", "-A")
        git(sub, "commit", "-q", "-m", "s1")
        s1 = rev_parse(sub)
        write(sub, "f", "2\n")
        git(sub, "commit", "-qam", "s2")
        s2 = rev_parse(sub)
        git(repo, "checkout", "-q", "main")
        write(
            repo, ".gitmodules", '[submodule "sub"]\n\tpath = sub\n\turl = ../sub\n\tignore = all\n'
        )
        git(repo, "update-index", "--add", "--cacheinfo", f"160000,{s1},sub")
        git(repo, "add", ".gitmodules")
        git(repo, "commit", "-q", "-m", "sub")
        base = rev_parse(repo)
        git(repo, "checkout", "-q", "-b", "bump")
        git(repo, "update-index", "--cacheinfo", f"160000,{s2},sub")
        git(repo, "commit", "-q", "-m", "bump")
        git(self.work, "clone", "-q", sub, os.path.join(repo, "sub"))
        git(os.path.join(repo, "sub"), "checkout", "-q", s2)
        # The submodule is checked out at the head's commit, so the tree is clean.
        status = git(repo, "status", "--short", check=False).stdout
        self.assertEqual(git(repo, "diff", "--stat").stdout, b"", status)

        plan = self.triage(repo, "--base", base)
        self.assertEqual(plan["changed"]["count"], 1, plan["changed"])
        self.assertIn("supply-chain", plan["domains"])
        self.assertEqual(plan["approval"]["hint"], "needs a human")

    def test_domain_rule_without_categories_or_paths(self) -> None:
        """A lens rule may omit categories, paths or content; triage must not crash on it."""
        install = os.path.join(self.work, "install")
        shutil.copytree(
            REPO_ROOT / "skills",
            os.path.join(install, "skills"),
            ignore=shutil.ignore_patterns("__pycache__"),
        )
        panel = os.path.join(install, "skills/code-review-panel/scripts/panel.py")
        with open(panel, encoding="utf-8") as f:
            src = f.read()
        anchor = '    "containers": {\n'
        self.assertEqual(src.count(anchor), 1)
        extra = (
            '    "k8s": {"kind": "domain", "tiers": FULL_TIERS, "reproduce": True,\n'
            '            "paths": [("chart", re.compile(r"(^|/)Chart\\.yaml$"), None)]},\n'
            '    "rust": {"kind": "domain", "tiers": FULL_TIERS, "reproduce": True,\n'
            '             "content": [("unsafe", re.compile(r"\\bunsafe\\b"), None)]},\n'
            '    "containers": {\n'
        )
        with open(panel, "w", encoding="utf-8") as f:
            f.write(src.replace(anchor, extra))
        for name in ("k8s", "rust"):
            write(
                install,
                f"skills/code-review-panel/references/lenses/{name}.md",
                "# x\n\n## Stance\n\n## Grounding\n\n## Checklist\n\n## Output\n",
            )
        repo, base = self.new_repo()
        commit(repo, {"chart/Chart.yaml": "name: x\n", "src/lib.rs": "unsafe { x() }\n"})

        proc = run(repo, sys.executable, "-B", panel, "triage", "--base", base, check=False)
        self.assertEqual(proc.returncode, 0, proc.stderr.decode()[-400:])
        self.assertLessEqual({"k8s", "rust"}, set(json.loads(proc.stdout)["domains"]))


class ContentScanTests(TriageTestCase):
    def test_truncated_scan_assumes_every_domain(self) -> None:
        repo, base = self.new_repo()
        commit(repo, {"src/big.py": "".join(f"x{i} = {i}\n" for i in range(20_050))})
        plan = self.triage(repo, "--base", base)
        self.assertLessEqual({"supply-chain", "containers"}, set(plan["domains"]))
        self.assertLessEqual(
            {"supply-chain", "containers"}, {lens["name"] for lens in plan["lenses"]}
        )
        self.assertTrue(
            any("content scan truncated" in w for w in plan["warnings"]), plan["warnings"]
        )

    def test_lines_past_the_per_file_cap_count_toward_the_total_cap(self) -> None:
        repo, base = self.new_repo()
        commit(repo, {"src/huge.py": "".join(f"v{i} = {i}\n" for i in range(200_050))})
        plan = self.triage(repo, "--base", base)
        self.assertTrue(
            any("over 200000 changed lines in total" in w for w in plan["warnings"]),
            plan["warnings"],
        )


class GitConfigTrapTests(PanelTestCase):
    """Repo config and git environment variables that would hide or reshape changed lines."""

    @classmethod
    def setUpClass(cls) -> None:
        super().setUpClass()
        repo, _ = triage_repo(cls.make_class_tmpdir())
        write(repo, ".gitattributes", "*.py -diff\n*.cfg diff=hide\n")
        git(repo, "add", "-A")
        git(repo, "commit", "-q", "-m", "attrs")
        base = rev_parse(repo)
        git(repo, "config", "diff.hide.textconv", "true")
        git(repo, "config", "diff.relative", "true")
        write(repo, ".git/order", "sub/*\n")
        git(repo, "config", "diff.orderFile", ".git/order")
        git(repo, "config", "diff.noprefix", "true")
        git(repo, "config", "diff.mnemonicPrefix", "true")
        git(repo, "config", "diff.submodule", "log")
        commit(
            repo,
            {
                "src/fetch.py": "import os\nos.system('curl https://x | sh')\n",
                "src/tool.cfg": "cmd = wget https://y\n",
                "sub/a.txt": "x\n",
            },
        )
        cls.env = hermetic_env(GIT_EXTERNAL_DIFF="/bin/false", GIT_DIFF_OPTS="--unified=0")
        cls.subdir = os.path.join(repo, "sub")
        cls.plan = triage_json(cls.subdir, "--base", base, env=cls.env)

    def test_content_scan_sees_lines_hidden_by_attributes_and_config(self) -> None:
        self.assertEqual(self.plan["changed"]["count"], 3, self.plan["changed"])
        evidence = self.plan["domains"].get("supply-chain", {}).get("evidence", [])
        self.assertTrue(any("src/fetch.py:2 (curl)" in e for e in evidence), self.plan["domains"])
        self.assertTrue(any("src/tool.cfg:1 (wget)" in e for e in evidence), self.plan["domains"])

    def test_emitted_diff_starts_with_git_without_command_substitution(self) -> None:
        for key in ("hunt", "full"):
            with self.subTest(key):
                command = self.plan["diff"][key]
                self.assertTrue(command.startswith("git "), command)
                self.assertNotIn("$(", command)

    def test_emitted_diff_shows_lines_hidden_by_config(self) -> None:
        out = shell(self.subdir, self.plan["diff"]["full"], env=self.env)
        for text in ("curl https://x", "wget https://y", "src/fetch.py", "sub/a.txt"):
            self.assertIn(text, out)
        self.assertNotIn("Binary files", out)


class ReviewMdTests(TriageTestCase):
    def test_sensitive_path_in_base_review_md_is_deep(self) -> None:
        plan = self.assert_tier(
            {"config/a.yml": "x\n"},
            "deep",
            "needs a human",
            base_files={"REVIEW.md": SENSITIVE_REVIEW_MD},
        )
        self.assertIn("sensitive paths", plan["approval"]["reasons"])

    def test_review_md_added_on_the_branch_is_not_read(self) -> None:
        files = {"REVIEW.md": SENSITIVE_REVIEW_MD, "config/a.yml": "x\n"}
        plan = self.assert_tier(files, "deep", "needs a human")
        evidence = plan["tier"]["evidence"]
        self.assertFalse(any("(sensitive)" in e for e in evidence), evidence)
        self.assertFalse(plan["review_md"]["present"])
        self.assertIn("REVIEW.md (agent)", evidence)

    def test_skipped_path_still_counts_for_tier_and_approval(self) -> None:
        repo, base = self.new_repo({"REVIEW.md": "## Skip\n\n- `gen/`\n", "gen/old.txt": "x\n"})
        commit(repo, {"gen/install.sh": "echo hi\n", "README.md": "more\n"})
        plan = self.triage(repo, "--base", base)
        self.assertEqual(plan["tier"]["name"], "deep", plan["tier"])
        self.assertTrue(
            any("gen/install.sh (installer)" in e for e in plan["tier"]["evidence"]), plan["tier"]
        )
        self.assertEqual(plan["changed"]["skipped"], ["gen/install.sh"])
        self.assertEqual(plan["changed"]["count"], 2)
        self.assertEqual(plan["approval"]["hint"], "needs a human")

    def test_skipped_prose_stays_quick_and_low_risk(self) -> None:
        repo, base = self.new_repo({"REVIEW.md": "## Skip\n\n- `docs/`\n", "docs/a.md": "x\n"})
        commit(repo, {"docs/a.md": "y\n"})
        plan = self.triage(repo, "--base", base)
        self.assertEqual(plan["tier"]["name"], "quick", plan["tier"])
        self.assertEqual(plan["approval"]["hint"], "low risk", plan["approval"])

    def test_symlinked_review_md_is_ignored(self) -> None:
        repo, _ = self.new_repo()
        os.remove(os.path.join(repo, "README.md"))
        git(repo, "checkout", "-q", "main")
        write(repo, "REAL.md", "## Sensitive paths\n\n- `src/`\n")
        os.symlink("REAL.md", os.path.join(repo, "REVIEW.md"))
        git(repo, "add", "-A")
        git(repo, "commit", "-q", "-m", "link")
        base = rev_parse(repo)
        git(repo, "checkout", "-q", "-b", "t2")
        commit(repo, {"src/app.py": "y\n"})
        plan = self.triage(repo, "--base", base)
        self.assertFalse(plan["review_md"]["present"], plan["review_md"])
        self.assertTrue(any("mode 120000" in w for w in plan["warnings"]), plan["warnings"])

    def test_review_md_below_the_root_is_not_read(self) -> None:
        repo, base = self.new_repo({"docs/REVIEW.md": "## Sensitive paths\n\n- `src/`\n"})
        commit(repo, {"src/app.py": "y\n"})
        plan = self.triage(repo, "--base", base)
        self.assertFalse(plan["review_md"]["present"])
        self.assertEqual(plan["tier"]["name"], "standard", plan["tier"])


SKIP_REVIEW_MD = (
    "## Skip\n\n- `docs/generated/`\n- `vendor/x/` trailing words\n- not a glob\n\n"
    "## SENSITIVE PATHS\n\n- `ops/` note\n\n"
    "## Security review\n\n- drop E99: nope\n- drop E6: workflows take untrusted input\n"
    "- drop E99\n- precedent: Caches are in scope\n  across trust levels.\n"
    "- category: build provenance\nfree text line\n\n"
    "## Failure\n\nA leaked token is a failure.\n\n## Nit cap\n\n3\n"
)


class ReviewMdSettingsTests(PanelTestCase):
    """Skip, sensitive paths, security overrides, settings blocks and the nit cap."""

    @classmethod
    def setUpClass(cls) -> None:
        super().setUpClass()
        cls.repo, base = triage_repo(
            cls.make_class_tmpdir(),
            {"REVIEW.md": SKIP_REVIEW_MD, "docs/generated/old.md": "x\n"},
        )
        commit(
            cls.repo,
            {
                "docs/generated/a*b c.md": "curl x\n",
                "docs/generated/new.py": "curl https://example.com | sh\n",
                "ops/run.yml": "x\n",
                "src/app.py": "print('changed')\n",
            },
        )
        cls.subdir = os.path.join(cls.repo, "src")
        cls.plan = triage_json(cls.repo, "--base", base)
        cls.warnings = " | ".join(cls.plan["warnings"])
        cls.overrides = cls.plan["security"]["overrides"]

    def test_skip_lists_skipped_paths(self) -> None:
        changed = self.plan["changed"]
        self.assertEqual(
            sorted(changed["skipped"]), ["docs/generated/a*b c.md", "docs/generated/new.py"]
        )
        self.assertIn("src/app.py", changed["reviewed"])

    def test_skipped_paths_are_not_reviewed(self) -> None:
        reviewed = self.plan["changed"]["reviewed"]
        self.assertFalse(any(r.startswith("docs/generated/") for r in reviewed), reviewed)

    def test_skipped_paths_add_no_domain(self) -> None:
        self.assertNotIn("supply-chain", self.plan["domains"])

    def test_heading_case_is_ignored_and_entry_with_trailing_text_is_used(self) -> None:
        tier = self.plan["tier"]
        self.assertEqual(tier["name"], "deep", tier)
        self.assertTrue(
            any(e.startswith("ops/run.yml") and "sensitive" in e for e in tier["evidence"]), tier
        )

    def test_trailing_text_after_an_entry_warns(self) -> None:
        self.assertIn("entry ignored", self.warnings)
        self.assertIn("entry still used", self.warnings)

    def test_unparsed_bullet_warns(self) -> None:
        self.assertIn("unparsed line: - not a glob", self.warnings)

    def test_drop_with_known_rule_id_is_parsed(self) -> None:
        self.assertEqual([d["id"] for d in self.overrides["drops"]], ["E6"], self.overrides)

    def test_drops_with_unknown_rule_ids_are_listed(self) -> None:
        self.assertEqual(self.overrides["unknown_ids"], ["E99", "E99"], self.overrides)

    def test_drop_with_unknown_rule_id_keeps_the_default(self) -> None:
        self.assertTrue(all(d["id"] != "E99" for d in self.overrides["drops"]), self.overrides)
        self.assertIn("unknown rule id E99; the default stays", self.warnings)

    def test_precedent_continuation_line_is_joined(self) -> None:
        self.assertEqual(
            self.overrides["precedents"],
            [{"id": "R1", "text": "Caches are in scope across trust levels."}],
        )

    def test_category_is_parsed(self) -> None:
        self.assertEqual(self.overrides["categories"], ["build provenance"])

    def test_settings_blocks_hold_text_and_ignored_lines(self) -> None:
        settings = self.plan["settings"]
        self.assertEqual(sorted(settings), ["other", "security"])
        for key in ("security", "other"):
            self.assertEqual(sorted(settings[key]), ["ignored", "text"], settings[key])

    def test_ignored_security_lines_are_listed_with_their_reason(self) -> None:
        ignored = self.plan["settings"]["security"]["ignored"]
        self.assertTrue(any("E99: nope (unknown id E99)" in i for i in ignored), ignored)
        self.assertTrue(any(i.startswith("- drop E99 (unknown") for i in ignored), ignored)
        self.assertTrue(any("free text line" in i for i in ignored), ignored)

    def test_nit_cap_sets_the_minor_budget(self) -> None:
        self.assertEqual(self.plan["review_md"]["nit_cap"], 3)
        self.assertEqual(self.plan["limits"]["max_minor_verified"], 6)

    def test_security_settings_hold_security_and_failure_sections(self) -> None:
        text = self.plan["settings"]["security"]["text"]
        for part in (
            "## Security review",
            "- drop E6: workflows take untrusted input",
            "## Failure",
            "A leaked token is a failure.",
        ):
            self.assertIn(part, text)

    def test_other_settings_hold_the_failure_section_only(self) -> None:
        other = self.plan["settings"]["other"]
        self.assertIn("## Failure", other["text"])
        self.assertIn("A leaked token is a failure.", other["text"])
        self.assertNotIn("Security review", other["text"])
        self.assertNotIn("drop E6", other["text"])
        self.assertEqual(other["ignored"], [])

    def test_hunt_excludes_skipped_paths(self) -> None:
        cases = [(self.repo, ["src/app.py"]), (self.subdir, ["src/app.py", "ops/run.yml"])]
        for cwd, expected in cases:
            with self.subTest(cwd=os.path.relpath(cwd, self.repo)):
                out = shell(cwd, self.plan["diff"]["hunt"])
                self.assertNotIn("docs/generated", out)
                for path in expected:
                    self.assertIn(path, out)

    def test_emitted_commands_carry_no_dash_c(self) -> None:
        commands = [self.plan["diff"][k] for k in ("full", "hunt", "log")]
        commands += [r["show"] for r in self.plan["rules"] if r.get("show")]
        self.assertTrue(commands)
        for command in commands:
            self.assertTrue(command.startswith("git "), command)
            self.assertNotIn(" -C ", command)

    def test_rule_show_command_runs_from_a_subdirectory(self) -> None:
        shown = [
            shell(self.subdir, r["show"]) for r in self.plan["rules"] if r["path"] == "REVIEW.md"
        ]
        self.assertEqual(shown, [SKIP_REVIEW_MD])

    def test_log_command_runs_from_a_subdirectory(self) -> None:
        out = shell(self.subdir, self.plan["diff"]["log"])
        self.assertEqual(out.count("\ncommit ") + out.startswith("commit "), 1, out[:300])


class ReviewMdCommentTests(PanelTestCase):
    """HTML comments and fenced examples in REVIEW.md are not settings."""

    REVIEW_MD = (
        "# Review\n\n<!--\n## Skip\n\n- `src/`\n-->\n\n## Sensitive paths\n\n- `config/`\n\n"
        f"{FENCE}text\n- `src/`\n{FENCE}\n\n"
        "<!-- a comment\n## Security review\n- drop E7: in a comment\n-->\n\n"
        f"## Security review\n\n{FENCE}markdown\n- drop E6: an example only\n- `src/`\n{FENCE}\n"
        "- precedent: Real one.\n<!--\n- drop E8: commented out\n-->\n"
    )

    @classmethod
    def setUpClass(cls) -> None:
        super().setUpClass()
        repo, base = triage_repo(
            cls.make_class_tmpdir(), {"REVIEW.md": cls.REVIEW_MD, "config/a.yml": "x\n"}
        )
        commit(repo, {"src/app.py": "changed\n"})
        cls.plan = triage_json(repo, "--base", base)

    def test_skip_inside_an_html_comment_is_not_applied(self) -> None:
        self.assertEqual(self.plan["review_md"]["skip"], [])
        self.assertIn("src/app.py", self.plan["changed"]["reviewed"])
        self.assertEqual(self.plan["changed"]["skipped"], [])

    def test_fenced_glob_is_not_a_sensitive_path(self) -> None:
        self.assertEqual(self.plan["review_md"]["sensitive"], ["config/"])
        self.assertEqual(self.plan["tier"]["name"], "standard", self.plan["tier"])

    def test_fenced_and_commented_security_lines_are_ignored(self) -> None:
        overrides = self.plan["security"]["overrides"]
        self.assertEqual(overrides["drops"], [])
        self.assertEqual(overrides["unknown_ids"], [])
        self.assertEqual(overrides["unparsed"], [])
        self.assertEqual([p["text"] for p in overrides["precedents"]], ["Real one."])


class ReviewMdCodeSpanTests(TriageTestCase):
    """A `<!--` in a code span is text; a comment that never closes is flagged, not silent."""

    def plan_with(self, review_md: str) -> dict:
        repo, base = self.new_repo({"REVIEW.md": review_md, "billing/pay.py": "x\n"})
        commit(repo, {"billing/pay.py": "y\n"})
        return self.triage(repo, "--base", base)

    def test_comment_opener_in_a_code_span_keeps_later_sections(self) -> None:
        for span in ("`<!--`", "``a <!-- b``", "`` ` <!-- ``"):
            with self.subTest(span=span):
                plan = self.plan_with(
                    f"## Failure\n\nNever emit a raw {span} in output.\n\n"
                    "## Sensitive paths\n\n- `billing/`\n"
                )
                self.assertEqual(plan["review_md"]["sensitive"], ["billing/"])
                self.assertEqual(plan["tier"]["name"], "deep", plan["tier"])
                self.assertIsNone(plan["review_md"]["unclosed_comment"])

    def test_unclosed_comment_warns_and_needs_a_human(self) -> None:
        plan = self.plan_with(
            "## Failure\n\nData loss.\n\nNever emit a raw <!-- in output.\n\n"
            "## Sensitive paths\n\n- `billing/`\n"
        )
        self.assertEqual(plan["review_md"]["sensitive"], [])
        self.assertEqual(plan["review_md"]["unclosed_comment"], 5)
        self.assertIn(
            "REVIEW.md: the HTML comment opened on line 5 never closes, so nothing after it "
            "is read",
            plan["warnings"],
        )
        self.assertIn("REVIEW.md has an unclosed comment", plan["approval"]["reasons"])
        self.assertEqual(plan["approval"]["hint"], "needs a human")

    def test_unclosed_comment_line_is_where_the_open_one_starts(self) -> None:
        plan = self.plan_with("<!-- a\nb --> x <!-- c\n## Sensitive paths\n\n- `billing/`\n")
        self.assertEqual(plan["review_md"]["unclosed_comment"], 2)

    def test_closed_comment_after_a_code_span_is_still_dropped(self) -> None:
        plan = self.plan_with(
            "## Failure\n\nSee `x` <!-- hidden\n## Skip\n\n- `billing/`\n-->\n\n"
            "## Sensitive paths\n\n- `billing/`\n"
        )
        self.assertEqual(plan["review_md"]["skip"], [])
        self.assertEqual(plan["review_md"]["sensitive"], ["billing/"])
        self.assertIsNone(plan["review_md"]["unclosed_comment"])


UNUSUAL_NAMES = [
    b"a'b.md",
    b'a"b.md',
    b"a\tb.md",
    b"a\\b.md",
    b"a\nb.md",
    b"bad\xff.md",
    "x‮b.md".encode(),
]
QUOTE_NAME = "src/x';touch PWNED;'.py"


class UnusualNameTests(TriageTestCase):
    """Names with quotes, backslashes, control or bidi characters, or bytes that are not UTF-8."""

    @classmethod
    def setUpClass(cls) -> None:
        super().setUpClass()
        work = cls.make_class_tmpdir()
        cls.unusual = {}
        for name in UNUSUAL_NAMES:
            repo, base = triage_repo(work)
            commit(repo, {name: "x\n"})
            cls.unusual[name] = (repo, base, triage_json(repo, "--base", base))
        cls.quote_repo, base = triage_repo(work)
        commit(cls.quote_repo, {QUOTE_NAME: "x = 1\n", "src/ok.py": "y = 2\n"})
        cls.quote_plan = triage_json(cls.quote_repo, "--base", base)

    def test_unusual_name_makes_the_tier_deep(self) -> None:
        for name, (_, _, plan) in self.unusual.items():
            with self.subTest(name=name):
                self.assertEqual(plan["tier"]["name"], "deep", plan["tier"])

    def test_unusual_name_gets_no_quoted_pathspec(self) -> None:
        for name, (_, _, plan) in self.unusual.items():
            with self.subTest(name=name):
                self.assertEqual(plan["changed"]["quoted_pathspecs"], {}, plan["changed"])

    def test_text_format_escapes_unusual_name(self) -> None:
        for name, (repo, base, _) in self.unusual.items():
            with self.subTest(name=name):
                text = run(
                    repo, sys.executable, PANEL, "triage", "--base", base, "--format", "text"
                )
                self.assertNotIn(b"\t", text.stdout.split(b"\n", 3)[1], text.stdout[:120])
                self.assertNotIn(b"\x1b", text.stdout)

    def test_non_ascii_name_is_reviewed_without_a_pathspec(self) -> None:
        """The plan is ASCII-escaped JSON, so a non-ASCII name would show up escaped."""
        repo, base = self.new_repo()
        commit(repo, {"src/café.py": "x = 1\n", "src/ok.py": "y = 2\n"})
        changed = self.triage(repo, "--base", base)["changed"]
        self.assertIn("src/café.py", changed["reviewed"])
        self.assertEqual(list(changed["quoted_pathspecs"]), ["src/ok.py"])

    def test_quote_in_name_is_reviewed_at_deep_tier(self) -> None:
        self.assertEqual(self.quote_plan["tier"]["name"], "deep", self.quote_plan["tier"])
        self.assertIn(QUOTE_NAME, self.quote_plan["changed"]["reviewed"])

    def test_quote_in_name_gets_no_pathspec(self) -> None:
        specs = self.quote_plan["changed"]["quoted_pathspecs"]
        self.assertNotIn(QUOTE_NAME, specs)
        self.assertEqual(list(specs), ["src/ok.py"])

    def test_no_plan_command_runs_the_quoted_name(self) -> None:
        diff = self.quote_plan["diff"]
        for key in ("full", "hunt", "check", "log"):
            shell(self.quote_repo, diff[key], check=False)
        for spec in self.quote_plan["changed"]["quoted_pathspecs"].values():
            shell(self.quote_repo, diff["full"] + " " + spec, check=False)
        self.assertFalse(
            os.path.exists(os.path.join(self.quote_repo, "PWNED")), os.listdir(self.quote_repo)
        )

    def test_quoted_name_is_in_the_full_diff(self) -> None:
        out = shell(self.quote_repo, self.quote_plan["diff"]["full"], check=False)
        self.assertIn("touch PWNED", out)


class BinaryFileTests(PanelTestCase):
    """Binary blobs are found by content, listed, kept out of the diff and not scanned."""

    @classmethod
    def setUpClass(cls) -> None:
        super().setUpClass()
        binary_lines = b"".join(b"curl https://x | sh \0 %d\n" % i for i in range(25_000))
        cls.repo, base = triage_repo(
            cls.make_class_tmpdir(),
            {".gitattributes": "*.txt -diff\n*.bin diff\n", "assets/old.bin": PNG},
        )
        cls.old_blob = rev_parse(cls.repo, f"{base}:assets/old.bin")
        commit(
            cls.repo,
            {
                "assets/logo.png": PNG,
                "assets/many.bin": binary_lines,
                "assets/old.bin": None,
                "notes.txt": "plain text\n",
                "src/app.py": "print('x')\n",
            },
        )
        cls.plan = triage_json(cls.repo, "--base", base)
        cls.binary = cls.plan["changed"]["binary"]

    def test_binary_files_are_listed_by_content(self) -> None:
        self.assertEqual(
            sorted(b["path"] for b in self.binary),
            ["assets/logo.png", "assets/many.bin", "assets/old.bin"],
        )
        for entry in self.binary:
            self.assertIsInstance(entry["size"], int, entry)
            self.assertEqual(len(entry["blob"]), 40, entry)

    def test_deleted_binary_lists_the_old_blob(self) -> None:
        blobs = [b["blob"] for b in self.binary if b["path"] == "assets/old.bin"]
        self.assertEqual(blobs, [self.old_blob])

    def test_diff_attribute_does_not_make_text_binary(self) -> None:
        reviewed = self.plan["changed"]["reviewed"]
        self.assertIn("notes.txt", reviewed)
        self.assertFalse(any(r.startswith("assets/") for r in reviewed), reviewed)

    def test_binary_content_is_not_scanned(self) -> None:
        warnings = self.plan["warnings"]
        self.assertFalse(any("content scan truncated" in w for w in warnings), warnings)
        self.assertNotIn("supply-chain", self.plan["domains"])

    def test_binary_files_need_a_human(self) -> None:
        self.assertIn("binary files not reviewed", self.plan["approval"]["reasons"])

    def test_binary_files_are_excluded_from_the_diff_commands(self) -> None:
        for name in ("logo.png", "many.bin", "old.bin"):
            self.assertIn(f"':(exclude,literal,top)assets/{name}'", self.plan["diff"]["full"])

    def test_binary_bytes_are_not_in_the_emitted_diff(self) -> None:
        out = shell(os.path.join(self.repo, "src"), self.plan["diff"]["full"], check=False)
        self.assertNotIn("assets/", out)
        self.assertIn("plain text", out)
        self.assertNotIn("\0", out)

    def test_media_flag_comes_from_the_extension(self) -> None:
        self.assertEqual(
            {b["path"]: b["media"] for b in self.binary},
            {"assets/logo.png": True, "assets/many.bin": False, "assets/old.bin": False},
        )

    def test_deleted_flag_and_opaque_count(self) -> None:
        self.assertEqual(
            {b["path"]: b["deleted"] for b in self.binary},
            {"assets/logo.png": False, "assets/many.bin": False, "assets/old.bin": True},
        )
        self.assertEqual(self.plan["changed"]["opaque_binary"], 1)


class ExcludeTests(TriageTestCase):
    """Skip and binary excludes, the byte budget on them, and literal pathspecs."""

    def test_skip_entry_over_a_reviewed_path_is_not_applied(self) -> None:
        repo, base = self.new_repo(
            {"REVIEW.md": "## Skip\n\n- `third_party/lib`\n", "third_party/lib": "old\n"}
        )
        os.remove(os.path.join(repo, "third_party/lib"))
        commit(repo, {"third_party/lib/x.c": 'int evil(void) { return system("curl x"); }\n'})
        plan = self.triage(repo, "--base", base)
        self.assertIn("third_party/lib", plan["changed"]["reviewed"])
        self.assertNotIn("third_party/lib", plan["changed"]["skipped"])
        self.assertNotIn("exclude,literal,top)third_party/lib", plan["diff"]["full"])
        self.assertTrue(
            any("third_party/lib" in w and "Skip" in w for w in plan["warnings"]),
            plan["warnings"],
        )
        out = shell(repo, plan["diff"]["full"], check=False)
        self.assertIn("third_party/lib/x.c", out)
        self.assertIn("evil", out)

    def test_binary_path_over_a_reviewed_path_is_not_excluded(self) -> None:
        repo, base = self.new_repo({"data": PNG})
        os.remove(os.path.join(repo, "data"))
        commit(repo, {"data/x.c": "int main(void) { return 0; }\n"})
        plan = self.triage(repo, "--base", base)
        self.assertEqual([b["path"] for b in plan["changed"].get("binary", [])], ["data"])
        self.assertNotIn("exclude,literal,top)data'", plan["diff"]["full"])
        self.assertTrue(
            any("data" in w and "binary" in w.lower() for w in plan["warnings"]), plan["warnings"]
        )
        self.assertIn("data/x.c", shell(repo, plan["diff"]["full"], check=False))

    def test_skip_over_the_byte_budget_is_dropped(self) -> None:
        repo, base = self.new_repo({"REVIEW.md": "## Skip\n\n- `gen/*.txt`\n", "gen/a.txt": "x\n"})
        generated = {f"gen/generated-file-{i:05d}.txt": f"{i}\n" for i in range(2500)}
        commit(repo, {**generated, "src/app.py": "b\n"})
        plan = self.triage(repo, "--base", base)
        full = plan["diff"]["full"]
        self.assertLess(len(full), 2000)
        self.assertTrue(full.endswith(" --"), full[-200:])
        self.assertTrue(
            any("Skip not applied: too many skipped paths" in w for w in plan["warnings"]),
            plan["warnings"],
        )
        self.assertEqual(plan["changed"]["skipped"], [])
        self.assertTrue(any(r.startswith("gen/") for r in plan["changed"]["reviewed"]))

    def test_directory_skip_collapses_under_the_budget(self) -> None:
        repo, base = self.new_repo({"REVIEW.md": "## Skip\n\n- `gen/`\n", "gen/a.txt": "x\n"})
        generated = {f"gen/generated-file-{i:05d}.txt": f"{i}\n" for i in range(2500)}
        commit(repo, {**generated, "src/app.py": "b\n"})
        plan = self.triage(repo, "--base", base)
        full = plan["diff"]["full"]
        self.assertTrue(full.endswith(" -- ':(exclude,literal,top)gen/'"), full[-200:])
        self.assertFalse(any("Skip not applied" in w for w in plan["warnings"]), plan["warnings"])
        self.assertEqual(len(plan["changed"]["skipped"]), 200)

    def test_binary_excludes_over_the_budget_are_dropped_with_a_warning(self) -> None:
        repo, base = self.new_repo()
        long = "a-rather-long-directory-name-for-binary-assets/and-a-long-file-name"
        commit(repo, {**{f"{long}-{i:04d}.bin": PNG for i in range(400)}, "src/app.py": "b\n"})
        plan = self.triage(repo, "--base", base)
        self.assertLess(len(plan["diff"]["full"]), 2000)
        self.assertTrue(
            any("binary" in w.lower() and "too many" in w for w in plan["warnings"]),
            plan["warnings"],
        )
        self.assertEqual(len(plan["changed"].get("binary", [])), 200)
        self.assertTrue(plan["changed"]["truncated"])

    def test_literal_pathspecs_env_does_not_turn_excludes_into_paths(self) -> None:
        repo, base = self.new_repo({"REVIEW.md": "## Skip\n\n- `gen/`\n", "gen/old.txt": "x\n"})
        commit(
            repo, {"gen/x.txt": "skipped\n", "src/fetch.py": "os.system('curl https://x | sh')\n"}
        )
        env = hermetic_env(GIT_LITERAL_PATHSPECS="1")
        plan = self.triage(repo, "--base", base, env=env)
        self.assertIn("supply-chain", plan["domains"])
        out = shell(repo, plan["diff"]["hunt"], env=env, check=False)
        self.assertIn("src/fetch.py", out)
        self.assertNotIn("gen/x.txt", out)
        for key in ("full", "hunt", "check", "log"):
            self.assertIn(" --no-literal-pathspecs ", plan["diff"][key])


class DiffCommandTests(PanelTestCase):
    """The plan's diff commands end with --, check is the full diff, and specs scope one path."""

    @classmethod
    def setUpClass(cls) -> None:
        super().setUpClass()
        repo, base = triage_repo(cls.make_class_tmpdir())
        commit(repo, {"src/app.py": "one\n", "src/other.py": "two\n"})
        cls.subdir = os.path.join(repo, "src")
        cls.plan = triage_json(repo, "--base", base)
        cls.diff = cls.plan["diff"]

    def test_full_and_hunt_end_with_double_dash(self) -> None:
        self.assertTrue(self.diff["full"].endswith(" --"), self.diff)
        self.assertTrue(self.diff["hunt"].endswith(" --"), self.diff)

    def test_check_is_the_full_command_with_shortstat(self) -> None:
        check = self.diff["check"]
        self.assertEqual(check.replace(" --shortstat", ""), self.diff["full"])
        self.assertIn(f" --shortstat {self.plan['merge_base']} ", check)

    def test_check_command_runs(self) -> None:
        self.assertIn("2 files changed", shell(self.subdir, self.diff["check"], check=False))

    def test_literal_top_pathspec_selects_one_path(self) -> None:
        out = shell(self.subdir, self.diff["full"] + " ':(literal,top)src/other.py'", check=False)
        self.assertIn("src/other.py", out)
        self.assertNotIn("src/app.py", out)

    def test_pathspecs_are_quoted_per_path(self) -> None:
        specs = self.plan["changed"]["quoted_pathspecs"]
        self.assertEqual(specs.get("src/other.py"), "':(literal,top)src/other.py'", specs)

    def test_pathspec_from_the_plan_runs(self) -> None:
        spec = self.plan["changed"]["quoted_pathspecs"].get("src/other.py", "x")
        out = shell(self.subdir, self.diff["full"] + " " + spec, check=False)
        self.assertIn("src/other.py", out)
        self.assertNotIn("src/app.py", out)


class BaseResolutionTests(TriageTestCase):
    def ci_repo(self) -> tuple[str, str, str]:
        repo, base = self.new_repo()
        tip = commit(repo, {"src/app.py": "ci\n"})
        return repo, base, tip

    def test_gitlab_ci_variables_set_base_and_head(self) -> None:
        repo, base, tip = self.ci_repo()
        git(repo, "checkout", "-q", "main")
        env = hermetic_env(CI_MERGE_REQUEST_DIFF_BASE_SHA=base, CI_COMMIT_SHA=tip)
        plan = self.triage(repo, env=env)
        self.assertEqual(plan["base"]["source"], "gitlab-ci", plan["base"])
        self.assertEqual(plan["base"]["sha"], base)
        self.assertEqual(plan["head"]["sha"], tip)
        self.assertEqual(plan["changed"]["count"], 1)
        self.assertIn(plan["empty"], (None, False, ""))

    def test_github_base_ref_resolves_through_origin(self) -> None:
        repo, base, _ = self.ci_repo()
        git(repo, "update-ref", "refs/remotes/origin/main", base)
        plan = self.triage(repo, env=hermetic_env(GITHUB_BASE_REF="main"))
        self.assertEqual(plan["base"]["source"], "github-actions", plan["base"])

    def test_github_base_ref_without_a_remote_branch_exits_2(self) -> None:
        repo, base, _ = self.ci_repo()
        git(repo, "update-ref", "refs/remotes/origin/main", base)
        self.triage_exit(repo, code=2, env=hermetic_env(GITHUB_BASE_REF="nope"))

    def test_no_base_source_exits_2_naming_the_base_flag(self) -> None:
        repo, base, _ = self.ci_repo()
        git(repo, "update-ref", "refs/remotes/origin/main", base)
        proc = self.triage_exit(repo, code=2)
        self.assertIn(b"--base", proc.stderr)

    def test_origin_head_is_the_last_fallback(self) -> None:
        repo, base, _ = self.ci_repo()
        git(repo, "update-ref", "refs/remotes/origin/main", base)
        git(repo, "symbolic-ref", "refs/remotes/origin/HEAD", "refs/remotes/origin/main")
        plan = self.triage(repo)
        self.assertEqual(plan["base"]["source"], "origin-head", plan["base"])

    def test_head_defaults_to_ci_commit_sha_with_a_caller_base(self) -> None:
        repo, base, tip = self.ci_repo()
        git(repo, "checkout", "-q", "main")
        plan = self.triage(repo, "--base", base, env=hermetic_env(CI_COMMIT_SHA=tip))
        self.assertEqual(plan["head"]["sha"], tip, plan["head"])
        self.assertEqual(plan["changed"]["count"], 1)

    def test_head_not_checked_out_warns_and_needs_a_human(self) -> None:
        repo, base, tip = self.ci_repo()
        git(repo, "checkout", "-q", "main")
        plan = self.triage(repo, "--base", base, env=hermetic_env(CI_COMMIT_SHA=tip))
        warning = "reviewers read the work tree at HEAD, which is not the reviewed head"
        self.assertTrue(any(warning in w for w in plan["warnings"]), plan["warnings"])
        self.assertIn("HEAD is not the reviewed head", plan["approval"]["reasons"])

    def test_head_checked_out_does_not_warn(self) -> None:
        repo, base, _ = self.ci_repo()
        plan = self.triage(repo, "--base", base)
        self.assertFalse(
            any("not the reviewed head" in w for w in plan["warnings"]), plan["warnings"]
        )

    def test_shallow_clone_without_a_merge_base_exits_2_with_a_fetch_hint(self) -> None:
        src, _ = self.new_repo()
        commit(src, {"src/app.py": "s\n"})
        git(src, "checkout", "-q", "main")
        commit(src, {"README.md": "main moves\n"})
        git(src, "checkout", "-q", "topic")
        clone = os.path.join(self.work, "shallow")
        url = "file://" + src
        git(self.work, "clone", "-q", "--depth", "1", "--no-single-branch", url, clone)
        proc = self.triage_exit(clone, "--base", "origin/main", "--head", "origin/topic", code=2)
        self.assertIn(b"fetch", proc.stderr)

    def test_shallow_clone_without_the_base_commit_suggests_unshallow(self) -> None:
        src, base = self.new_repo()
        commit(src, {"src/app.py": "s\n"})
        clone = os.path.join(self.work, "shallow-single")
        url = "file://" + src
        git(self.work, "clone", "-q", "--depth", "1", "--single-branch", "-b", "topic", url, clone)
        proc = self.triage_exit(clone, "--base", base, code=2)
        self.assertIn(b"shallow", proc.stderr)
        self.assertIn(b"git fetch --unshallow", proc.stderr)


class SinceAndEmptyRangeTests(TriageTestCase):
    def test_empty_change(self) -> None:
        repo, base = self.new_repo()
        plan = self.triage(repo, "--base", base)
        self.assertEqual(plan["empty"], "change")
        self.assertEqual(plan["tier"]["name"], "standard")
        self.assertEqual(plan["approval"]["hint"], "needs a human")
        self.assertIn("empty change set", plan["approval"]["reasons"])

    def test_empty_hunt(self) -> None:
        repo, base = self.new_repo()
        head = commit(repo, {"src/app.py": "z\n"})
        plan = self.triage(repo, "--base", base, "--since", head)
        self.assertEqual(plan["empty"], "hunt")
        self.assertTrue(plan["since"]["used"], plan["since"])

    def since_repo(self) -> tuple[str, str, str]:
        repo, base = self.new_repo()
        first = commit(repo, {".github/workflows/ci.yml": "on: push\n"})
        commit(repo, {"src/app.py": "two\n"})
        return repo, base, first

    def test_since_narrows_the_hunt_while_the_tier_reads_the_whole_range(self) -> None:
        repo, base, first = self.since_repo()
        plan = self.triage(repo, "--base", base, "--since", first)
        self.assertTrue(plan["since"]["used"], plan["since"])
        self.assertEqual(plan["tier"]["name"], "deep", plan["tier"])
        self.assertNotIn("supply-chain", plan["domains"])
        self.assertIn(first, plan["diff"]["hunt"])

    def test_since_that_is_not_an_ancestor_falls_back_to_the_whole_range(self) -> None:
        repo, base, _ = self.since_repo()
        git(repo, "checkout", "-q", "-b", "other", base)
        other = commit(repo, {"x.txt": "o\n"})
        git(repo, "checkout", "-q", "topic")
        plan = self.triage(repo, "--base", base, "--since", other)
        self.assertFalse(plan["since"]["used"], plan["since"])
        self.assertTrue(plan["since"]["note"], plan["since"])
        self.assertEqual(plan["diff"]["hunt"], plan["diff"]["full"])

    def test_since_from_before_a_rebase_falls_back_to_the_whole_range(self) -> None:
        repo, _, _ = self.since_repo()
        old_tip = rev_parse(repo)
        git(repo, "checkout", "-q", "main")
        new_main = commit(repo, {"MAIN.txt": "moved\n"})
        git(repo, "checkout", "-q", "topic")
        git(repo, "rebase", "-q", "main")
        plan = self.triage(repo, "--base", new_main, "--since", old_tip)
        self.assertFalse(plan["since"]["used"], plan["since"])
        self.assertIn("rebased", plan["since"]["note"] or "")
        self.assertEqual(plan["diff"]["hunt"], plan["diff"]["full"])


class KnownFileTests(TriageTestCase):
    def dirty_repo(self) -> tuple[str, str]:
        repo, base = self.new_repo()
        commit(repo, {"src/app.py": "k\n"})
        write(repo, "src/app.py", "dirty\n")
        return repo, base

    def known_doc(self, open_rounds: int, *items: dict) -> dict:
        return {
            "version": 1,
            "round": 3,
            "rounds_with_open_findings": open_rounds,
            "findings": list(items),
        }

    def test_round_cap_is_set_after_three_rounds_with_open_findings(self) -> None:
        repo, base = self.dirty_repo()
        for rounds, cap in ((3, True), (2, False)):
            with self.subTest(rounds_with_open_findings=rounds):
                known = self.known_file(f"k{rounds}.json", self.known_doc(rounds, KNOWN_ITEM))
                plan = self.triage(repo, "--base", base, "--known", known)
                self.assertIs(plan["round_cap"], cap)
                self.assertEqual(plan["round"], 4)
                self.assertIs("round cap" in plan["approval"]["reasons"], cap, plan["approval"])
                if cap:
                    self.assertEqual(plan["approval"]["hint"], "needs a human")
                self.assertEqual(len(plan["open_known"]), 1)

    def test_dirty_tree_warns(self) -> None:
        repo, base = self.dirty_repo()
        known = self.known_file("k2.json", self.known_doc(2, KNOWN_ITEM))
        plan = self.triage(repo, "--base", base, "--known", known)
        self.assertTrue(any("dirty tree" in w for w in plan["warnings"]), plan["warnings"])

    def test_bare_list_reads_as_round_zero(self) -> None:
        repo, base = self.dirty_repo()
        known = self.known_file("bare.json", [dict(KNOWN_ITEM, last_seen_round=0)])
        plan = self.triage(repo, "--base", base, "--known", known)
        self.assertFalse(plan["round_cap"])
        self.assertEqual(plan["round"], 1)
        self.assertEqual(len(plan["open_known"]), 1, plan["open_known"])

    def declined_known_tree(self) -> tuple[str, str, str, str]:
        """A repo, a declined known file outside it, and a copy inside its work tree."""
        repo, base = self.new_repo()
        commit(repo, {"src/app.py": "k\n"})
        doc = self.known_doc(3, dict(KNOWN_ITEM, status="declined"))
        outside = self.known_file("declined.json", doc)
        inside = os.path.join(repo, "known.json")
        shutil.copy(outside, inside)
        return repo, base, outside, inside

    def test_declining_open_items_lifts_the_round_cap(self) -> None:
        repo, base, outside, _ = self.declined_known_tree()
        plan = self.triage(repo, "--base", base, "--known", outside)
        self.assertIs(plan["round_cap"], False)
        self.assertNotIn("round cap", plan["approval"]["reasons"])
        self.assertEqual(plan["open_known"], [])

    def test_known_file_inside_the_work_tree_needs_a_human(self) -> None:
        repo, base, _, inside = self.declined_known_tree()
        link = os.path.join(self.work, "link.json")
        os.symlink(inside, link)
        reason = "known findings file inside the reviewed tree"
        with self.subTest("inside"):
            plan = self.triage(repo, "--base", base, "--known", inside)
            self.assertIn(reason, plan["approval"]["reasons"])
            self.assertTrue(
                any("inside the reviewed work tree" in w for w in plan["warnings"]),
                plan["warnings"],
            )
        with self.subTest("link into the tree"):
            plan = self.triage(repo, "--base", base, "--known", link)
            self.assertIn(reason, plan["approval"]["reasons"])

    def test_known_file_outside_the_work_tree_or_in_git_dir_is_fine(self) -> None:
        repo, base, outside, _ = self.declined_known_tree()
        in_git = os.path.join(repo, ".git", "known.json")
        shutil.copy(outside, in_git)
        for name, path in (("outside", outside), (".git", in_git)):
            with self.subTest(name):
                plan = self.triage(repo, "--base", base, "--known", path)
                self.assertNotIn(
                    "known findings file inside the reviewed tree", plan["approval"]["reasons"]
                )

    def test_known_file_that_is_not_utf8_exits_2_naming_it(self) -> None:
        repo, base = self.new_repo()
        commit(repo, {"src/app.py": "k\n"})
        bad = os.path.join(self.work, "latin1.json")
        with open(bad, "wb") as f:
            f.write(
                b'{"version": 1, "round": 1, "rounds_with_open_findings": 0, "findings": [],'
                b' "x": "caf\xe9"}'
            )
        proc = self.triage_exit(repo, "--base", base, "--known", bad, code=2)
        self.assertIn(b"latin1.json", proc.stderr)
        self.assertNotIn(b"Traceback", proc.stderr)

    def test_round_not_past_the_known_round_exits_2(self) -> None:
        repo, base = self.new_repo()
        commit(repo, {"src/app.py": "k\n"})
        known = self.known_file("k3.json", self.known_doc(0))
        proc = self.triage_exit(repo, "--base", base, "--known", known, "--round", "3", code=2)
        self.assertIn(b"--round", proc.stderr)

    def test_round_past_the_known_round_is_used(self) -> None:
        repo, base = self.new_repo()
        commit(repo, {"src/app.py": "k\n"})
        known = self.known_file("k3.json", self.known_doc(0))
        plan = self.triage(repo, "--base", base, "--known", known, "--round", "4")
        self.assertEqual(plan["round"], 4)

    def test_plan_lists_every_known_item(self) -> None:
        repo, base = self.new_repo()
        commit(repo, {"src/app.py": "k\n"})
        items = [
            dict(
                KNOWN_ITEM,
                status="declined",
                last_seen_round=3,
                instances=[{"file": "src/b.py", "line": 9}],
            ),
            dict(KNOWN_ITEM, id="CR-00000001", status="pre-existing", last_seen_round=1),
        ]
        known = self.known_file("kd.json", self.known_doc(1, *items))
        listed = self.triage(repo, "--base", base, "--known", known).get("known", [])
        self.assertEqual([k["status"] for k in listed], ["declined", "pre-existing"])
        fields = ["class", "file", "id", "instances", "lens", "line", "open", "status", "title"]
        for item in listed:
            self.assertEqual(sorted(item), fields)
        self.assertEqual(listed[0]["instances"], [{"file": "src/b.py", "line": 9}])

    def test_open_items_keep_their_lens_when_the_tier_drops(self) -> None:
        """Only an item's own lens checks it, so a quick tier still runs architecture for it."""
        repo, base = self.new_repo()
        commit(repo, {"README.md": "docs only\n"})
        arch = dict(KNOWN_ITEM, id="CR-a0000001", lens="architecture")
        perf = dict(KNOWN_ITEM, id="CR-b0000001", lens="performance", status="declined")
        known = self.known_file("kl.json", self.known_doc(1, KNOWN_ITEM, arch, perf))
        plan = self.triage(repo, "--base", base, "--known", known)
        lenses = {le["name"]: le for le in plan["lenses"]}
        self.assertEqual(plan["tier"]["name"], "quick", plan["tier"])
        self.assertEqual(lenses["architecture"]["kept_for"], ["CR-a0000001"])
        self.assertEqual(lenses["architecture"]["mode"], "normal")
        # Correctness runs on quick anyway, but in full, since it has an open item to check.
        self.assertEqual(lenses["correctness"]["kept_for"], ["CR-516ea425"])
        self.assertEqual(lenses["correctness"]["mode"], "normal")
        self.assertNotIn("performance", lenses)
        text = run_panel(repo, "triage", "--base", base, "--known", known, "--format", "text")
        self.assertIn(
            b"architecture (normal): facts, impact (kept for open CR-a0000001)", text.stdout
        )

    def test_docs_mode_stays_without_an_open_item(self) -> None:
        repo, base = self.new_repo()
        commit(repo, {"README.md": "docs only\n"})
        lenses = {le["name"]: le for le in self.triage(repo, "--base", base)["lenses"]}
        self.assertEqual(lenses["correctness"]["mode"], "docs")
        self.assertNotIn("architecture", lenses)


class InstallCheckTests(PanelTestCase):
    """Triage exits 3 when the installed skills no longer match what the panel reads."""

    def triage_install(self, edit=None) -> tuple[int, str]:
        """Copy both skills, apply edit(install root), and triage a small change."""
        work = self.make_tmpdir()
        install = os.path.join(work, "install")
        shutil.copytree(
            REPO_ROOT / "skills",
            os.path.join(install, "skills"),
            ignore=shutil.ignore_patterns("__pycache__"),
        )
        if edit:
            edit(install)
        repo, base = triage_repo(work)
        commit(repo, {"src/app.py": "changed\n"})
        panel = os.path.join(install, "skills/code-review-panel/scripts/panel.py")
        proc = run(repo, sys.executable, "-B", panel, "triage", "--base", base, check=False)
        return proc.returncode, proc.stderr.decode(errors="replace")

    @staticmethod
    def rewrite(relpath: str, old: str, new: str):
        def edit(install: str) -> None:
            path = os.path.join(install, "skills", relpath)
            with open(path, encoding="utf-8") as f:
                text = f.read()
            if text.count(old) != 1:
                raise AssertionError(f"{old!r} is not in {relpath} exactly once")
            with open(path, "w", encoding="utf-8") as f:
                f.write(text.replace(old, new))

        return edit

    def test_unchanged_install_triages(self) -> None:
        code, err = self.triage_install()
        self.assertEqual(code, 0, err[-400:])

    def test_mismatches_exit_3(self) -> None:
        finder = "security-diff-review/references/finder.md"
        filt = "security-diff-review/references/filter.md"
        cases = {
            "security skill missing": lambda root: shutil.rmtree(
                os.path.join(root, "skills/security-diff-review")
            ),
            "finding key renamed": self.rewrite(finder, '"exploit_scenario":', '"scenario":'),
            "finding key added": self.rewrite(
                finder, '"instances": ["src/admin.py:17"],', '"instances": [], "cwe": "CWE-89",'
            ),
            "review_completed missing": self.rewrite(finder, '"review_completed"', '"done"'),
            "verdict key renamed": self.rewrite(filt, '"excluded_by": null,', '"rule": null,'),
            "scale changed": self.rewrite(
                filt, "`confidence` is an integer from 1 to 10", "`confidence` is 0 to 1"
            ),
            "cutoff sentence changed": self.rewrite(filt, "The cutoff is 8.", "The cutoff is 9."),
            "cutoff changed": self.rewrite(filt, "is 8 or more.", "is 9 or more."),
            "second JSON example": self.rewrite(
                filt, "## Verdict\n", "## Verdict\n\n```json\n{}\n```\n"
            ),
            "rule id removed": self.rewrite(filt, "- **E7.** ", "- E7. "),
            "persona heading lost": self.rewrite(
                "code-review-panel/references/lenses/testing.md", "## Grounding", "## Ground"
            ),
            "extra lens file": lambda root: shutil.copy(
                os.path.join(root, "skills/code-review-panel/references/lenses/testing.md"),
                os.path.join(root, "skills/code-review-panel/references/lenses/extra.md"),
            ),
        }
        for label, edit in cases.items():
            with self.subTest(label):
                code, err = self.triage_install(edit)
                self.assertEqual(code, 3, err[-400:])
                self.assertIn("incomplete install", err)


class FilterRuleTests(TriageTestCase):
    def test_plan_records_the_filter_rule_ids(self) -> None:
        repo, base = self.new_repo()
        commit(repo, {"src/app.py": "k\n"})
        rule_ids = self.triage(repo, "--base", base)["security"].get("rule_ids", [])
        for rule_id in ("E1", "E6", "P13"):
            self.assertIn(rule_id, rule_ids)


class AgentRuleFileTests(TriageTestCase):
    def test_agent_instruction_files_are_deep(self) -> None:
        cases = [
            ("AGENTS.md", None),
            ("pkg/AGENTS.md", None),
            ("AGENT.md", None),
            (".junie/guidelines.md", None),
            (".claude/settings.json", None),
            (".github/copilot-instructions.md", None),
            ("QWEN.md", "agent instruction files"),
            ("WARP.md", "agent instruction files"),
            ("pkg/CRUSH.md", "agent instruction files"),
        ]
        for path, reason in cases:
            with self.subTest(path=path):
                self.assert_tier({path: "x\n"}, "deep", "needs a human", reason=reason)

    def test_files_imported_by_base_rule_files_are_agent_files(self) -> None:
        for path in ["docs/rules.md", "notes/x.md", ".github/guide.md"]:
            with self.subTest(path=path):
                plan = self.assert_tier(
                    {path: "changed\n"},
                    "deep",
                    "needs a human",
                    base_files=IMPORTS,
                    reason="agent instruction files",
                )
                self.assertIn(f"{path} (agent)", plan["tier"]["evidence"])

    def test_markdown_that_is_not_imported_stays_quick(self) -> None:
        files = {"docs/other.md": "changed\n"}
        self.assert_tier(files, "quick", "low risk", base_files=IMPORTS)

    def test_email_address_is_not_an_import(self) -> None:
        files = {"example.com": "changed\n"}
        self.assert_tier(files, "standard", "needs a human", base_files=IMPORTS)

    def linked_claude_md_plan(self) -> dict:
        repo, _ = self.new_repo(
            {"AGENTS.md": "Rules. See @docs/linked.md.\n", "docs/linked.md": "l\n"}
        )
        git(repo, "checkout", "-q", "main")
        os.symlink("AGENTS.md", os.path.join(repo, "CLAUDE.md"))
        git(repo, "add", "-A")
        git(repo, "commit", "-q", "-m", "link")
        base = rev_parse(repo)
        git(repo, "checkout", "-q", "-b", "t3")
        commit(repo, {"docs/linked.md": "changed\n"})
        return self.triage(repo, "--base", base)

    def test_claude_md_symlink_to_agents_md_is_the_same_rule_file(self) -> None:
        rules = {r["path"]: r for r in self.linked_claude_md_plan()["rules"]}
        self.assertEqual(rules.get("CLAUDE.md", {}).get("same_as"), "AGENTS.md", rules)
        self.assertIsNone(rules["CLAUDE.md"].get("show"))
        self.assertTrue(rules.get("AGENTS.md", {}).get("show"), rules)
        self.assertNotIn("same_as", rules["AGENTS.md"])

    def test_import_through_a_symlinked_agents_md_is_followed(self) -> None:
        plan = self.linked_claude_md_plan()
        self.assertEqual(plan["tier"]["name"], "deep", plan["tier"])

    def test_nested_imports_resolve_relative_to_the_importer(self) -> None:
        for path in ["docs/nested.md", "docs/deep/last.md"]:
            with self.subTest(path=path):
                plan = self.assert_tier(
                    {path: "changed\n"},
                    "deep",
                    "needs a human",
                    base_files=NESTED_IMPORTS,
                    reason="agent instruction files",
                )
                self.assertIn(f"{path} (agent)", plan["tier"]["evidence"])

    def test_file_named_like_an_unreached_import_stays_quick(self) -> None:
        files = {"nested.md": "changed\n"}
        self.assert_tier(files, "quick", "low risk", base_files=NESTED_IMPORTS)

    def test_import_chain_within_the_cap_is_followed(self) -> None:
        plan = self.assert_tier(
            {"c/f44.md": "changed\n"},
            "deep",
            "needs a human",
            base_files=import_chain(45),
            reason="agent instruction files",
        )
        self.assertFalse(any("@path imports" in w for w in plan["warnings"]), plan["warnings"])

    def test_import_chain_past_the_cap_follows_the_first_50(self) -> None:
        self.assert_tier(
            {"c/f40.md": "changed\n"},
            "deep",
            "needs a human",
            base_files=import_chain(60),
            reason="agent instruction files",
        )

    def test_import_chain_past_the_cap_needs_a_human(self) -> None:
        plan = self.assert_tier(
            {"c/f55.md": "changed\n"},
            "quick",
            "needs a human",
            base_files=import_chain(60),
            reason="too many @path imports",
        )
        self.assertTrue(
            any("@path imports" in w and "50" in w for w in plan["warnings"]), plan["warnings"]
        )

    def test_scoped_package_names_in_an_imported_file_are_not_imports(self) -> None:
        scoped = ", ".join(f'"@scope/p{i}": "1"' for i in range(55))
        base_files = {
            "CLAUDE.md": "Read @package.json and @docs/guide.md\n",
            "docs/guide.md": "See @nested.md\n",
            "docs/nested.md": "x\n",
            "package.json": '{"devDependencies": {' + scoped + "}}\n",
        }
        plan = self.assert_tier(
            {"docs/nested.md": "changed\n"},
            "deep",
            "needs a human",
            base_files=base_files,
            reason="agent instruction files",
        )
        self.assertFalse(any("@path imports" in w for w in plan["warnings"]), plan["warnings"])


if __name__ == "__main__":
    unittest.main()
