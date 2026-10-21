#!/usr/bin/env python3
"""Deterministic half of the code-review-panel skill.

triage  Resolve the diff range, classify every changed path, pick the tier, the
        lenses and their skeptics, read REVIEW.md and the repo rules from the base
        ref, and print the review plan as JSON (or text).
report  Validate the merged findings, map security rows, assign stable ids, vote
        on the skeptic verdicts, rank, and print the Markdown report. Optionally
        write review.py JSON and the known findings file for the next round.

File formats live in references/report.md. Exit codes: 0 ok (warnings are in the
output), 2 usage, git or input error, 3 incomplete install, 4 disputed findings
without a resolution.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import posixpath
import re
import shlex
import subprocess
import sys
import tempfile
import unicodedata
from fnmatch import fnmatchcase
from pathlib import Path

if sys.version_info < (3, 10):
    sys.stderr.write("panel.py: Python 3.10 or newer is required\n")
    sys.exit(2)

PLAN_VERSION = 1
SKILL_DIR = Path(__file__).resolve().parent.parent
REFS_DIR = SKILL_DIR / "references"
LENS_DIR = REFS_DIR / "lenses"
SIBLING_REFS = SKILL_DIR.parent / "security-diff-review" / "references"
FINDER = SIBLING_REFS / "finder.md"
FILTER = SIBLING_REFS / "filter.md"
OWN_REFS = ("finder-brief.md", "skeptics.md", "report.md", "review-md.md")
# The security references' JSON examples, checked so a format change there cannot
# quietly break what the panel parses: a finding's keys, and a verdict's keys.
FINDER_KEYS = {
    "file",
    "line",
    "severity",
    "category",
    "description",
    "exploit_scenario",
    "recommendation",
    "confidence",
    "instances",
    "pre_existing",
}
VERDICT_KEYS = {"confidence", "excluded_by", "reason"}
FILTER_SCALE = (1, 10)
JSON_EXAMPLE_RE = re.compile(r"^```json\n(.*?)^```", re.M | re.S)
PERSONA_HEADINGS = ["Stance", "Grounding", "Checklist", "Output"]
PERSONA_HEADING_RE = re.compile(r"^## (\w+)\s*$", re.M)
RULE_ID_RE = re.compile(r"^- \*\*([EP]\d+)\.\*\* ", re.M)
REQUIRED_RULE_IDS = [f"E{n}" for n in range(1, 18)] + [f"P{n}" for n in range(1, 14)]

# --no-literal-pathspecs: with GIT_LITERAL_PATHSPECS=1, git would read an
# ':(exclude,literal,top)<p>' pathspec as one literal path and print nothing.
GIT = ["git", "-c", "core.quotePath=false", "--no-optional-locks", "--no-literal-pathspecs"]
# Config and attributes can hide changed lines from a plain git diff; these flags
# keep every path and line visible. The security skill's SKILL.md carries the same
# set, byte for byte.
DIFF_FLAGS = [
    "--no-renames",
    "--ignore-submodules=none",
    "--no-relative",
    "--no-ext-diff",
    "--no-textconv",
    "--no-color",
    "-O/dev/null",
]
GIT_UNSET = (
    "GIT_EXTERNAL_DIFF",
    "GIT_DIFF_OPTS",
    "GIT_LITERAL_PATHSPECS",
    "GIT_GLOB_PATHSPECS",
    "GIT_NOGLOB_PATHSPECS",
    "GIT_ICASE_PATHSPECS",
)
GIT_ENV = {k: v for k, v in os.environ.items() if k not in GIT_UNSET}

LIST_CAP = 200
EVIDENCE_CAP = 3
SKIP_GROUP_THRESHOLD = 200
# Pathspec text the emitted diff commands may carry, so a harness never refuses
# them for length; past it, excludes are dropped (more review, never less).
EXCLUDE_BUDGET = 16_384
# A NUL byte in a blob's first bytes makes it binary, as git itself decides,
# but read with cat-file so attributes cannot fake it either way.
BINARY_SNIFF = 8_000
# Binary formats with no code for a reviewer to read. Any other binary file, such
# as a .js with a NUL byte or an archive, leaves the review incomplete.
MEDIA_RE = re.compile(
    r"\.(png|jpe?g|gif|bmp|ico|icns|webp|avif|heic|tiff?|psd|xcf|woff2?|ttf|otf|eot"
    r"|mp3|mp4|m4a|wav|ogg|oga|flac|webm|mov|avi|mkv|pdf)$",
    re.I,
)
LINE_CAP = 2_000
FILE_LINE_CAP = 20_000
TOTAL_LINE_CAP = 200_000
IMPORT_CAP = 50
UNREAD_LINK = "an @path import link could not be read"
NIT_CAP_DEFAULT = 5
MAX_PER_LENS = 10
MAX_VERIFIED = 20
ROUND_CAP = 3
KEEP_CONFIDENCE = 8
NEARBY_LINES = 5
KNOWN_WINDOW = 20
AGENT_NOTE = "An agent's approval never counts as a human's."
FETCH_HINT = (
    "Fetch more history, for example: git fetch --unshallow origin "
    "(or git fetch --deepen=200 origin)"
)

ID_RE = re.compile(r"^CR-[0-9a-f]{8}$")
CLASS_RE = re.compile(r"^[a-z0-9]+(-[a-z0-9]+)*$")
SEVERITIES = ("blocking", "major", "minor")
KNOWN_STATUSES = ("posted", "declined", "pre-existing")
SKEPTICS = ("facts", "impact", "reproduce")
LINK_MODES = ("120000", "160000")


class PanelError(Exception):
    def __init__(self, message: str, code: int = 2) -> None:
        super().__init__(message)
        self.code = code


# ---------------------------------------------------------------------------
# Path categories. This table drives the tier, the approval hint and docs/tests
# eligibility only; lens selection lives in LENS_RULES.
# ---------------------------------------------------------------------------


def _any(*patterns: str, flags: int = 0) -> re.Pattern[str]:
    return re.compile("|".join(f"(?:{p})" for p in patterns), flags)


AGENT_RE = _any(
    r"(^|/)(claude|agents|agent|agents\.override|review|gemini|claude\.local|qwen|warp|crush)\.md$",
    r"(^|/)\.(claude|agents|codex|cursor|windsurf|junie|roo|kiro|continue|amazonq|gemini)/",
    r"(^|/)\.clinerules(/|$)",
    r"(^|/)\.(cursorrules|windsurfrules)$",
    r"(^|/)\.github/copilot-instructions\.md$",
    r"(^|/)\.github/(instructions|prompts|chatmodes)/",
    r"(^|/)\.mcp\.json$",
    r"(^|/)hooks/hooks\.json$",
    flags=re.I,
)
CI_RE = _any(
    r"^\.github/(workflows|actions)/",
    r"(^|/)\.gitlab-ci\.ya?ml$",
    r"^\.gitlab/",
    r"^\.tekton/",
    r"(^|/)Jenkinsfile$",
    r"^\.circleci/",
    r"(^|/)\.travis\.yml$",
    r"(^|/)azure-pipelines\.ya?ml$",
    r"^\.azure-pipelines/",
    r"^\.buildkite/",
    r"(^|/)\.drone\.yml$",
    r"(^|/)cloudbuild\.ya?ml$",
    r"(^|/)\.?zuul\.ya?ml$",
    r"(^|/)\.?zuul\.d/",
    r"^\.(forgejo|gitea)/workflows/",
    r"(^|/)action\.ya?ml$",
)
INSTALLER_RE = re.compile(r"(^|/)install[^/.]*(\.(sh|bash|py|ps1))?$", re.I)
HOOKS_RE = _any(
    r"(^|/)\.(githooks|husky|git-hooks)/",
    r"^hooks/",
    r"(^|/)\.pre-commit-(config|hooks)\.ya?ml$",
    r"(^|/)\.?lefthook[^/]*\.ya?ml$",
    r"(^|/)\.overcommit\.yml$",
)
RELEASE_RE = _any(
    r"(^|/)(release|publish)[^/]*(/|$)",
    r"(^|/)\.goreleaser[^/]*$",
    r"(^|/)\.releaserc[^/]*$",
    r"(^|/)release-please-config\.json$",
    r"(^|/)\.release-please-manifest\.json$",
    r"(^|/)\.changeset/",
)
REPO_CONTROL_RE = _any(r"(^|/)CODEOWNERS$", r"(^|/)\.gitattributes$", r"(^|/)\.gitmodules$")
PROMPT_RE = _any(r"(^|/)SKILL\.md$", r"(^|/)(skills|agents|commands|prompts)/.+\.md$", flags=re.I)
DEPS_RE = _any(
    r"(^|/)(uv\.lock|poetry\.lock|Pipfile(\.lock)?|Cargo\.lock|go\.sum|package-lock\.json"
    r"|npm-shrinkwrap\.json|pnpm-lock\.yaml|yarn\.lock|Gemfile(\.lock)?|composer\.(json|lock))$",
    r"\.lock$",
    r"(^|/)requirements[^/]*\.(txt|in)$",
    r"(^|/)constraints[^/]*\.txt$",
    r"(^|/)(requirements|constraints)/[^/]+\.(txt|in)$",
    r"(^|/)pylock(\.[^/]+)?\.toml$",
    r"(^|/)(pyproject\.toml|package\.json|go\.mod|go\.work(\.sum)?|Cargo\.toml|[^/]+\.gemspec"
    r"|setup\.(py|cfg)|MANIFEST\.in|build\.rs)$",
    r"(^|/)(uv\.toml|poetry\.toml|pdm\.toml|pip\.conf|\.pypirc|\.npmrc|\.yarnrc(\.yml)?"
    r"|bunfig\.toml)$",
    r"(^|/)\.yarn/",
    r"(^|/)\.cargo/config(\.toml)?$",
    r"(^|/)(vendor|third_party|node_modules)/",
    r"(^|/)(dependabot\.ya?ml|renovate\.json5?|\.renovaterc[^/]*)$",
    r"(^|/)(Makefile|GNUmakefile|[^/]+\.mk|CMakeLists\.txt|[^/]+\.cmake|meson\.build"
    r"|BUILD(\.bazel)?|[^/]+\.bzl|WORKSPACE(\.bazel)?|MODULE\.bazel|conanfile\.(py|txt)|pom\.xml"
    r"|(build|settings)\.gradle(\.kts)?|gradlew|mvnw|tox\.ini|noxfile\.py|[^/]+\.spec)$",
    r"(^|/)gradle/wrapper/",
    r"(^|/)\.mvn/",
)
CONTAINER_RE = _any(
    r"(^|/)([^/]*\.)?(Containerfile|Dockerfile)(\.[^/]*)?$",
    r"(^|/)[^/]*\.dockerignore$",
    r"(^|/)\.containerignore$",
    r"(^|/)(docker-)?compose[^/]*\.ya?ml$",
    r"(^|/)docker-bake[^/]*\.(hcl|json)$",
)
TESTS_RE = _any(
    r"(^|/)(tests?|__tests__)/",
    r"(^|/)test_[^/]+$",
    r"(^|/)[^/]+_test\.[^/]+$",
    r"(^|/)[^/]+\.(test|spec)\.[^/]+$",
    r"(^|/)spec/.+_spec\.rb$",
)
CONFTEST_RE = re.compile(r"(^|/)conftest\.py$")
PROSE_RE = re.compile(r"\.(md|rst|adoc)$", re.I)
PROSE_DOT_PREFIXES = (".github/ISSUE_TEMPLATE/", ".github/PULL_REQUEST_TEMPLATE/")
PROSE_DOT_FILES = (".github/pull_request_template.md",)
PROSE_MODES = ("000000", "100644", "100755")
AUTH_WORDS = frozenset(
    "oauth oidc saml sso jwt jwks rbac acl login logout password passwords passwd credential"
    " credentials creds secret secrets token tokens permission permissions sudoers keyring"
    " kerberos ldap mfa totp webauthn".split()
)
# Authorship words, not auth: authority and authorization stay auth.
AUTHOR_WORDS = frozenset(("author", "authors", "authored", "authorship"))
CAMEL_RE = re.compile(r"[A-Z]+(?=[A-Z][a-z])|[A-Z]?[a-z]+|[A-Z]+|\d+")

DEEP_CATEGORIES = (
    "unusual name",
    "agent",
    "ci",
    "installer",
    "hooks",
    "auth",
    "release",
    "repo control",
    "sensitive",
)


def auth_path(path: str) -> bool:
    for part in re.split(r"[/_.\-]+", path):
        for word in CAMEL_RE.findall(part) + [part]:
            word = word.lower()
            if word in AUTH_WORDS:
                return True
            if word.startswith(("auth", "oauth")) and word not in AUTHOR_WORDS:
                return True
    return False


def unusual_name(raw: bytes, path: str) -> bool:
    try:
        raw.decode("utf-8")
    except UnicodeDecodeError:
        return True
    for ch in path:
        if ch in "'\"\\" or unicodedata.category(ch) in ("Cc", "Cf", "Cs", "Zl", "Zp"):
            return True
    return False


def dot_segment(path: str) -> bool:
    """True when a path segment starts with "." outside the prose allow-list."""
    if path in PROSE_DOT_FILES:
        return False
    rest = path
    for prefix in PROSE_DOT_PREFIXES:
        if path.startswith(prefix):
            rest = path[len(prefix) :]
            break
    return any(seg.startswith(".") for seg in rest.split("/"))


class Change:
    __slots__ = (
        "path",
        "raw",
        "old_mode",
        "new_mode",
        "old_blob",
        "new_blob",
        "status",
        "cats",
        "skipped",
        "binary",
    )

    def __init__(
        self, path: str, raw: bytes, modes: tuple[str, str], blobs: tuple[str, str], status: str
    ) -> None:
        self.path = path
        self.raw = raw
        self.old_mode, self.new_mode = modes
        self.old_blob, self.new_blob = blobs
        self.status = status
        self.cats: set[str] = set()
        self.skipped = False
        self.binary: dict | None = None


def classify(change: Change, sensitive: list[str], imports: set[str]) -> set[str]:
    p = change.path
    modes = (change.old_mode, change.new_mode)
    cats: set[str] = set()
    if unusual_name(change.raw, p):
        cats.add("unusual name")
    if AGENT_RE.search(p) or p in imports:
        cats.add("agent")
    if CI_RE.search(p):
        cats.add("ci")
    if PROMPT_RE.search(p):
        cats.add("prompt")
    if any(m in LINK_MODES for m in modes):
        cats.add("link")
    if "160000" in modes:
        cats.add("gitlink")
    if DEPS_RE.search(p):
        cats.add("deps/build")
    if CONTAINER_RE.search(p):
        cats.add("container")
    if REPO_CONTROL_RE.search(p):
        cats.add("repo control")
    if any(glob_match(p, g) for g in sensitive):
        cats.add("sensitive")
    markdown = bool(PROSE_RE.search(p))
    if markdown and dot_segment(p) and not cats & {"agent", "prompt"}:
        cats.add("dot markdown")
    prose = (
        markdown
        and all(m in PROSE_MODES for m in modes)
        and not cats & {"agent", "prompt", "link", "dot markdown"}
        and not dot_segment(p)
    )
    if prose:
        cats.add("prose")
    else:
        if INSTALLER_RE.search(p):
            cats.add("installer")
        if HOOKS_RE.search(p):
            cats.add("hooks")
        if auth_path(p):
            cats.add("auth")
        if RELEASE_RE.search(p):
            cats.add("release")
    if (
        TESTS_RE.search(p)
        and not CONFTEST_RE.search(p)
        and not cats & {"deps/build", "container", "link"}
    ):
        cats.add("tests")
    return cats


# ---------------------------------------------------------------------------
# Lens rules. One entry per lens; a domain entry carries its own path and content
# rules and may reuse path categories. Adding a lens is one persona file under
# references/lenses/ plus one entry here.
# ---------------------------------------------------------------------------

ALL_TIERS = ("quick", "standard", "deep")
FULL_TIERS = ("standard", "deep")
# A content rule's third field excludes paths; this one excludes every path that is
# not YAML, so a rule meant for CI files never matches code or prose.
NOT_YAML = re.compile(r"^(?!.*\.ya?ml$)", re.I)

LENS_RULES: dict[str, dict] = {
    "security": {"kind": "core", "tiers": ALL_TIERS, "reproduce": True},
    "correctness": {"kind": "core", "tiers": ALL_TIERS, "reproduce": True, "docs_on_quick": True},
    "testing": {"kind": "core", "tiers": ALL_TIERS, "reproduce": True},
    "architecture": {"kind": "core", "tiers": FULL_TIERS, "reproduce": False},
    "performance": {"kind": "core", "tiers": FULL_TIERS, "reproduce": True},
    "supply-chain": {
        "kind": "domain",
        "tiers": FULL_TIERS,
        "reproduce": True,
        "categories": ("ci", "release", "deps/build"),
        "paths": [
            ("gitlink", None, "160000"),
            ("submodule config", re.compile(r"(^|/)\.gitmodules$"), None),
        ],
        "content": [
            ("build-system", re.compile(r"\[build-system\]"), None),
            ("build-backend", re.compile(r"build-backend"), None),
            ("urlretrieve", re.compile(r"\burlretrieve\b"), None),
            ("urlopen", re.compile(r"\burlopen\b"), None),
            ("curl", re.compile(r"\bcurl\b"), None),
            ("wget", re.compile(r"\bwget\b"), None),
            ("tarfile", re.compile(r"\btarfile\b"), None),
            ("zipfile", re.compile(r"\bzipfile\b"), None),
            ("extractall", re.compile(r"\bextractall\b"), None),
            ("unpack_archive", re.compile(r"\bunpack_archive\b"), None),
            ("hashlib", re.compile(r"\bhashlib\b"), None),
            ("sha256sum", re.compile(r"\bsha256sum\b"), None),
            ("--require-hashes", re.compile(r"--require-hashes"), None),
            (
                "package install",
                re.compile(r"\b(?:pip3?|uv(?:\s+pip)?|npm)\s+install\b|\bnpm\s+ci\b"),
                None,
            ),
            ("git fetch", re.compile(r"\bgit\s+(?:clone|fetch|submodule)\b"), None),
            ("twine", re.compile(r"\btwine\b"), None),
            ("publish", re.compile(r"\b(?:uv|cargo|npm|gem|poetry)\s+publish\b"), None),
            ("gh release", re.compile(r"\bgh\s+release\b"), None),
            ("cosign", re.compile(r"\bcosign\b"), None),
            ("build-push-action", re.compile(r"build-push-action"), None),
            ("image push", re.compile(r"\b(?:docker|podman|buildah)\s+push\b"), None),
            ("index url", re.compile(r"--(?:extra-)?index-url\b|--find-links\b"), None),
            ("direct reference", re.compile(r"[\w\]\s]@\s*(?:https?://|git\+)"), None),
            (
                "CI include",
                re.compile(
                    r"^\s*(?:-\s*)?(?:include\s*:\s*\{?\s*)?(?:remote|project)\s*:"
                    r"|^\s*(?:-\s*)?include\s*:\s*['\"]?https?://"
                ),
                NOT_YAML,
            ),
        ],
    },
    "containers": {
        "kind": "domain",
        "tiers": FULL_TIERS,
        "reproduce": True,
        "categories": ("container",),
        "paths": [],
        "content": [
            ("FROM line", re.compile(r"^FROM\s+\S+"), re.compile(r"\.sql$", re.I)),
            (
                "image tool",
                re.compile(
                    r"\b(?:docker|podman|buildah|skopeo|kaniko)\b[\"',\s]+"
                    r"(?:(?:buildx|image|container)[\"',\s]+)?(?:build|run|pull)\b"
                ),
                None,
            ),
        ],
    },
}
LENS_ORDER = {name: i for i, name in enumerate(LENS_RULES)}


def persona_path(lens: str) -> Path:
    return FINDER if lens == "security" else LENS_DIR / f"{lens}.md"


def facts_path(lens: str) -> Path:
    return FILTER if lens == "security" else LENS_DIR / f"{lens}.md"


# ---------------------------------------------------------------------------
# Install checks (exit 3): personas, the sibling security references and their
# contracts.
# ---------------------------------------------------------------------------


def read_install_file(path: Path) -> str:
    try:
        return path.read_text(encoding="utf-8")
    except OSError:
        raise PanelError(
            f"incomplete install: {path} is missing; the security pass and the lenses "
            "need both skills from this plugin",
            3,
        ) from None


def example_json(path: Path, text: str):
    """The one JSON example in a security reference, parsed; exit 3 when it is not there."""
    blocks = JSON_EXAMPLE_RE.findall(text)
    try:
        if len(blocks) == 1:
            return json.loads(blocks[0])
    except ValueError:
        pass
    raise PanelError(f"incomplete install: {path} needs exactly one JSON example that parses", 3)


def check_contracts(finder_text: str, filter_text: str) -> None:
    """Check the finder's output and the filter's verdict against what the panel reads:
    the keys of their JSON examples, the 1 to 10 scale, and the cutoff."""
    example = example_json(FINDER, finder_text)
    try:
        keys = set(example["findings"][0])
        done = example["analysis_summary"]["review_completed"]
    except (KeyError, IndexError, TypeError):
        keys, done = set(), None
    if keys != FINDER_KEYS or not isinstance(done, bool):
        raise PanelError(
            f"incomplete install: {FINDER}'s example has finding keys {sorted(keys)} and "
            f"review_completed {done!r}; this panel reads {sorted(FINDER_KEYS)} and a boolean",
            3,
        )
    example = example_json(FILTER, filter_text)
    keys = set(example) if isinstance(example, dict) else set()
    if keys != VERDICT_KEYS:
        raise PanelError(
            f"incomplete install: {FILTER}'s verdict example has keys {sorted(keys)}; this "
            f"panel reads {sorted(VERDICT_KEYS)}",
            3,
        )
    flat = " ".join(filter_text.split())
    low, high = FILTER_SCALE
    for wanted in (
        f"`confidence` is an integer from {low} to {high}",
        f"`confidence` is {KEEP_CONFIDENCE} or more",
        f"The cutoff is {KEEP_CONFIDENCE}.",
    ):
        if wanted not in flat:
            raise PanelError(
                f"incomplete install: {FILTER} no longer says '{wanted}', which this panel reads",
                3,
            )


def check_install() -> list[str]:
    """Return the rule ids filter.md defines; raise exit 3 on any mismatch."""
    for name in OWN_REFS:
        read_install_file(REFS_DIR / name)
    files = {p.stem for p in LENS_DIR.glob("*.md")}
    wanted = {name for name in LENS_RULES if name != "security"}
    if wanted - files:
        names = ", ".join(sorted(wanted - files))
        raise PanelError(f"incomplete install: no persona file for lens {names}", 3)
    if files - wanted:
        names = ", ".join(sorted(files - wanted))
        raise PanelError(f"incomplete install: persona file without a LENS_RULES entry: {names}", 3)
    for lens in LENS_RULES:
        path = persona_path(lens)
        heads = PERSONA_HEADING_RE.findall(read_install_file(path))
        if heads != PERSONA_HEADINGS:
            raise PanelError(
                f"incomplete install: {path} has headings {heads}, expected {PERSONA_HEADINGS}", 3
            )
    filter_text = read_install_file(FILTER)
    check_contracts(read_install_file(FINDER), filter_text)
    ids = RULE_ID_RE.findall(filter_text)
    missing = [i for i in REQUIRED_RULE_IDS if i not in ids]
    if missing:
        raise PanelError(f"incomplete install: {FILTER} lacks rule ids {', '.join(missing)}", 3)
    return ids


# ---------------------------------------------------------------------------
# Git
# ---------------------------------------------------------------------------


def decode(raw: bytes) -> str:
    return raw.decode("utf-8", "surrogateescape")


def popen(cmd: list[str], **kwargs) -> subprocess.Popen[bytes]:
    try:
        return subprocess.Popen(cmd, **kwargs)
    except OSError as exc:
        raise PanelError(f"cannot run git: {exc.strerror}") from None


class Git:
    def __init__(self, top: str) -> None:
        self.top = top

    def run(
        self, *args: str, check: bool = True, stdin: bytes | None = None
    ) -> subprocess.CompletedProcess[bytes]:
        cmd = [*GIT, "-C", self.top, *args]
        try:
            proc = subprocess.run(cmd, capture_output=True, env=GIT_ENV, input=stdin)
        except OSError as exc:
            raise PanelError(f"cannot run git: {exc.strerror}") from None
        if check and proc.returncode:
            err = decode(proc.stderr).strip().splitlines()
            raise PanelError(f"git {args[0]} failed: {err[-1] if err else proc.returncode}")
        return proc

    def text(self, *args: str) -> str:
        return decode(self.run(*args).stdout).strip()

    def commit(self, ref: str, what: str, hint: str = "") -> str:
        if ref.startswith("-"):
            raise PanelError(f"{what} ref {ref!r} starts with '-'")
        proc = self.run("rev-parse", "--verify", "--quiet", f"{ref}^{{commit}}", check=False)
        sha = decode(proc.stdout).strip()
        if proc.returncode or not sha:
            raise PanelError(f"{what} ref {ref!r} does not name a commit.{hint}")
        return sha

    def has_ref(self, ref: str) -> bool:
        proc = self.run("rev-parse", "--verify", "--quiet", f"{ref}^{{commit}}", check=False)
        return proc.returncode == 0

    def is_ancestor(self, a: str, b: str) -> bool:
        return self.run("merge-base", "--is-ancestor", a, b, check=False).returncode == 0


def toplevel(repo: str) -> str:
    cmd = [*GIT, "-C", repo, "rev-parse", "--show-toplevel"]
    try:
        proc = subprocess.run(cmd, capture_output=True, env=GIT_ENV)
    except OSError as exc:
        raise PanelError(f"cannot run git: {exc.strerror}") from None
    top = decode(proc.stdout).strip()
    if proc.returncode or not top:
        raise PanelError(f"{repo} is not inside a git work tree")
    return top


def resolve_range(git: Git, base: str | None, head: str | None) -> tuple[str, str, str]:
    """Return (base ref, head ref, source) by the shared resolution order.

    A head the caller did not give is CI_COMMIT_SHA when set, else HEAD.
    """
    env = os.environ
    head = head or env.get("CI_COMMIT_SHA") or "HEAD"
    if base:
        return base, head, "caller"
    if env.get("CI_MERGE_REQUEST_DIFF_BASE_SHA") and env.get("CI_COMMIT_SHA"):
        return env["CI_MERGE_REQUEST_DIFF_BASE_SHA"], head, "gitlab-ci"
    gh_base = env.get("GITHUB_BASE_REF")
    if gh_base and git.has_ref(f"refs/remotes/origin/{gh_base}"):
        return f"refs/remotes/origin/{gh_base}", head, "github-actions"
    if git.has_ref("refs/remotes/origin/HEAD"):
        return "refs/remotes/origin/HEAD", head, "origin-head"
    raise PanelError(
        "no base: pass --base REF, or set CI_MERGE_REQUEST_DIFF_BASE_SHA and CI_COMMIT_SHA, "
        "or set GITHUB_BASE_REF with refs/remotes/origin/<it> fetched, or create "
        "refs/remotes/origin/HEAD (git remote set-head origin --auto)"
    )


def raw_changes(git: Git, a: str, b: str) -> list[Change]:
    out = git.run("diff", "--raw", "-z", "--no-abbrev", *DIFF_FLAGS, a, b, "--").stdout
    parts = out.split(b"\0")
    changes = []
    i = 0
    while i + 1 < len(parts):
        meta = parts[i]
        fields = meta[1:].split(b" ")
        if not meta.startswith(b":") or len(fields) < 5:
            raise PanelError("unexpected git diff --raw output")
        raw = parts[i + 1]
        modes = (fields[0].decode(), fields[1].decode())
        blobs = (fields[2].decode(), fields[3].decode())
        changes.append(Change(decode(raw), raw, modes, blobs, fields[4][:1].decode()))
        i += 2
    return changes


def mark_binary(git: Git, changes: list[Change]) -> None:
    """Set Change.binary to {path, size, blob, media, deleted} when the blob holds a NUL byte.

    The head blob decides, or the old one for a deletion. One cat-file stream reads
    every blob; only the first BINARY_SNIFF bytes of each are kept.
    """
    wanted: dict[str, list[Change]] = {}
    for c in changes:
        mode, blob = c.new_mode, c.new_blob
        if mode == "000000":
            mode, blob = c.old_mode, c.old_blob
        if mode in ("100644", "100755"):
            wanted.setdefault(blob, []).append(c)
    if not wanted:
        return
    cmd = [*GIT, "-C", git.top, "cat-file", "--batch"]
    with tempfile.TemporaryFile() as inp, tempfile.TemporaryFile() as err:
        inp.write("".join(f"{blob}\n" for blob in wanted).encode())
        inp.seek(0)
        proc = popen(cmd, stdin=inp, stdout=subprocess.PIPE, stderr=err, env=GIT_ENV)
        done = False
        try:
            for blob, group in wanted.items():
                header = proc.stdout.readline().split()
                if len(header) != 3 or header[1] != b"blob" or decode(header[0]) != blob:
                    raise PanelError(f"git cat-file: cannot read blob {blob}")
                size = int(header[2])
                head = proc.stdout.read(min(size, BINARY_SNIFF))
                left = size - len(head) + 1
                while left > 0:
                    chunk = proc.stdout.read(min(left, 1 << 20))
                    if not chunk:
                        raise PanelError(f"git cat-file: blob {blob} ends early")
                    left -= len(chunk)
                if b"\0" in head:
                    for c in group:
                        c.binary = {
                            "path": c.path,
                            "size": size,
                            "blob": blob,
                            "media": bool(MEDIA_RE.search(c.path)),
                            "deleted": c.new_mode == "000000",
                        }
            done = True
        finally:
            if not done:
                proc.kill()
            proc.stdout.close()
            code = proc.wait()
        if code:
            err.seek(0)
            msg = decode(err.read()).strip().splitlines()
            raise PanelError(f"git cat-file failed: {msg[-1] if msg else code}")


C_ESCAPES = {
    ord("a"): 7,
    ord("b"): 8,
    ord("t"): 9,
    ord("n"): 10,
    ord("v"): 11,
    ord("f"): 12,
    ord("r"): 13,
    ord('"'): 34,
    ord("\\"): 92,
}
HUNK_RE = re.compile(rb"^@@ -(\d+)(?:,\d+)? \+(\d+)(?:,\d+)? @@")


def c_unquote(quoted: bytes) -> bytes:
    out = bytearray()
    i = 1
    while i < len(quoted) - 1:
        c = quoted[i]
        if c == 0x5C and i + 1 < len(quoted):
            nxt = quoted[i + 1]
            if nxt in C_ESCAPES:
                out.append(C_ESCAPES[nxt])
                i += 2
                continue
            out.append(int(quoted[i + 1 : i + 4], 8))
            i += 4
            continue
        out.append(c)
        i += 1
    return bytes(out)


def header_path(line: bytes) -> bytes:
    """Map a 'diff --git a/<p> b/<p>' header back to <p> (both sides are equal)."""
    rest = line[len(b"diff --git ") :].rstrip(b"\n")
    if rest.startswith(b'"'):
        end = 1
        while end < len(rest):
            if rest[end] == 0x5C:
                end += 2
                continue
            if rest[end] == 0x22:
                break
            end += 1
        return c_unquote(rest[: end + 1])[2:]
    n = (len(rest) - 5) // 2
    return rest[2 : 2 + n]


def iter_patch(git: Git, a: str, b: str, pathspecs: tuple[str, ...] = ()):
    """Yield (path, sign, line number, text bytes) for every changed line of a..b."""
    cmd = [
        *GIT,
        "-C",
        git.top,
        "diff",
        "-U0",
        # diff.interHunkContext adds context lines even under -U0.
        "--inter-hunk-context=0",
        "--text",
        "--src-prefix=a/",
        "--dst-prefix=b/",
        "--submodule=short",
        *DIFF_FLAGS,
        a,
        b,
        "--",
        *pathspecs,
    ]
    with tempfile.TemporaryFile() as err:
        proc = popen(cmd, stdout=subprocess.PIPE, stderr=err, env=GIT_ENV)
        done = False
        try:
            path = None
            header = False
            old = new = 0
            for line in proc.stdout:
                if line.startswith(b"diff --git "):
                    path = decode(header_path(line))
                    header = True
                    continue
                if line.startswith(b"@@"):
                    m = HUNK_RE.match(line)
                    if m:
                        old, new = int(m[1]), int(m[2])
                        header = False
                    continue
                if header or path is None:
                    continue
                sign = line[:1]
                if sign == b"+":
                    yield path, "+", new, line[1:].rstrip(b"\n").rstrip(b"\r")
                    new += 1
                elif sign == b"-":
                    yield path, "-", old, line[1:].rstrip(b"\n").rstrip(b"\r")
                    old += 1
                elif sign == b" ":
                    old += 1
                    new += 1
            done = True
        finally:
            if not done:
                proc.kill()
            proc.stdout.close()
            code = proc.wait()
        if code:
            err.seek(0)
            msg = decode(err.read()).strip().splitlines()
            raise PanelError(f"git diff failed: {msg[-1] if msg else code}")


# ---------------------------------------------------------------------------
# REVIEW.md and repo rule files, from the base ref only
# ---------------------------------------------------------------------------

HEADING_RE = re.compile(r"^(#{1,6})[ \t]+(.*?)[ \t#]*$")
FENCE_RE = re.compile(r"^ {0,3}(`{3,}|~{3,})")
GLOB_LINE_RE = re.compile(r"^[-*][ \t]+`([^`]+)`(.*)$")
BULLET_RE = re.compile(r"^[-*+][ \t]")
# The `## Security review` grammar is owned by security-diff-review's
# references/filter.md, section "Repo section"; this parser follows it.
DROP_RE = re.compile(r"^- drop ([EP]\d+): *(\S.*)$")
DROP_ANY_RE = re.compile(r"^- drop\s+([^\s:]+)")
PRECEDENT_RE = re.compile(r"^- precedent: *(\S.*)$")
CATEGORY_RE = re.compile(r"^- category: *(\S.*)$")
COMMENT_OR_TICKS_RE = re.compile(r"<!--|`+")


def fence_state(line: str, fence: str) -> tuple[str, bool]:
    """Follow fenced code blocks: (the fence open after this line, whether the line is fenced)."""
    m = FENCE_RE.match(line)
    if fence:
        if m and m.group(1)[0] == fence[0] and len(m.group(1)) >= len(fence):
            if not line.strip().strip(fence[0]):
                return "", True
        return fence, True
    if m:
        return m.group(1), True
    return "", False


def drop_comments(line: str, comment: bool) -> tuple[str | None, bool, bool]:
    """Remove HTML comments from a line: (what is left, or None when only a comment was
    there; whether a comment is still open after it; whether that one opened here)."""
    kept = []
    rest = line
    touched = comment
    opened_here = False
    while rest:
        if comment:
            end = rest.find("-->")
            if end < 0:
                break
            rest = rest[end + 3 :]
            comment = opened_here = False
            continue
        m = COMMENT_OR_TICKS_RE.search(rest)
        if not m:
            kept.append(rest)
            break
        if m.group() != "<!--":
            # A code span shows "<!--" as text, so it opens no comment; backticks with no
            # closing run of the same length are text too.
            close = re.compile(rf"(?<!`){m.group()}(?!`)").search(rest, m.end())
            stop = close.end() if close else m.end()
            kept.append(rest[:stop])
            rest = rest[stop:]
            continue
        kept.append(rest[: m.start()])
        rest = rest[m.end() :]
        comment = touched = opened_here = True
    text = "".join(kept)
    if touched and not text.strip():
        return None, comment, opened_here
    return text, comment, opened_here


def unfenced(lines: list[str]) -> list[str]:
    """The lines with fenced code blocks (examples) blanked, so no parser reads them."""
    out = []
    fence = ""
    for line in lines:
        fence, fenced = fence_state(line, fence)
        out.append("" if fenced else line)
    return out


def split_sections(text: str, warnings: list[str]) -> tuple[dict[str, list[str]], int | None]:
    """Map lowercased '##' headings to their lines (heading first), skipping fences, and
    give the line number of an HTML comment that never closes, or None.

    HTML comments outside fences are dropped, so a commented-out section or entry
    has no effect.
    """
    sections: dict[str, list[str]] = {}
    current: list[str] | None = None
    fence = ""
    comment = False
    opened = None
    for num, line in enumerate(text.splitlines(), 1):
        if not fence:
            kept, comment, opened_here = drop_comments(line, comment)
            if opened_here:
                opened = num
            if kept is None:
                continue
            line = kept
        fence, fenced = fence_state(line, fence)
        if not fenced:
            h = HEADING_RE.match(line)
            if h and len(h.group(1)) <= 2:
                current = None
                if len(h.group(1)) == 2:
                    key = " ".join(h.group(2).lower().split())
                    if key in sections:
                        warnings.append(f"REVIEW.md: duplicate section '## {h.group(2)}' ignored")
                    else:
                        current = sections.setdefault(key, [])
                if current is None:
                    continue
        if current is not None:
            current.append(line)
    for lines in sections.values():
        while lines and not lines[-1].strip():
            lines.pop()
    return sections, opened if comment else None


def glob_match(path: str, entry: str) -> bool:
    if entry.endswith("/"):
        return path.startswith(entry)
    return fnmatchcase(path, entry)


def parse_globs(name: str, lines: list[str], keep_trailing: bool, warnings: list[str]) -> list[str]:
    globs = []
    for line in unfenced(lines[1:]):
        stripped = line.strip()
        if not stripped:
            continue
        m = GLOB_LINE_RE.match(stripped)
        if m:
            if m.group(2).strip():
                if keep_trailing:
                    warnings.append(
                        f"REVIEW.md {name}: text after `{m.group(1)}` (entry still used)"
                    )
                else:
                    warnings.append(f"REVIEW.md {name}: text after `{m.group(1)}` (entry ignored)")
                    continue
            globs.append(m.group(1))
        elif BULLET_RE.match(stripped):
            warnings.append(f"REVIEW.md {name}: unparsed line: {stripped}")
    return globs


def parse_security(lines: list[str], rule_ids: list[str]) -> dict:
    drops, precedents, categories, unknown, unparsed = [], [], [], [], []
    last = None
    for line in unfenced(lines[1:]):
        if not line.strip():
            last = None
            continue
        if line.startswith("  ") and last is not None:
            last["text"] += " " + line.strip()
            continue
        last = None
        m = DROP_RE.match(line)
        if m and m.group(1) in rule_ids:
            drops.append({"id": m.group(1), "reason": m.group(2).strip()})
            continue
        if m:
            unknown.append(m.group(1))
            unparsed.append(line.strip())
            continue
        m = PRECEDENT_RE.match(line)
        if m:
            last = {"id": f"R{len(precedents) + 1}", "text": m.group(1).strip()}
            precedents.append(last)
            continue
        m = CATEGORY_RE.match(line)
        if m:
            categories.append(m.group(1).strip())
            continue
        m = DROP_ANY_RE.match(line)
        if m and m.group(1) not in rule_ids:
            unknown.append(m.group(1))
        unparsed.append(line.strip())
    return {
        "text": "\n".join(lines),
        "drops": drops,
        "precedents": precedents,
        "categories": categories,
        "unknown_ids": unknown,
        "unparsed": unparsed,
    }


def read_review_md(git: Git, base: str, rule_ids: list[str], warnings: list[str]) -> dict:
    review = {
        "present": False,
        "blob": None,
        "sensitive": [],
        "skip": [],
        "failure": None,
        "nit_cap": NIT_CAP_DEFAULT,
        "warnings": [],
        "security": None,
        "unclosed_comment": None,
    }
    out = git.run("ls-tree", "-z", base, "--", "REVIEW.md").stdout
    entry = next((e for e in out.split(b"\0") if e.endswith(b"\tREVIEW.md")), None)
    if entry is None:
        return review
    mode, kind, blob = decode(entry.split(b"\t", 1)[0]).split(" ")
    if mode != "100644" or kind != "blob":
        review["warnings"].append(f"REVIEW.md at the base ref has mode {mode}; ignored")
        warnings.extend(review["warnings"])
        return review
    text = decode(git.run("cat-file", "blob", blob).stdout)
    notes: list[str] = []
    sections, opened = split_sections(text, notes)
    review["present"] = True
    review["blob"] = blob
    review["unclosed_comment"] = opened
    if opened:
        notes.append(
            f"REVIEW.md: the HTML comment opened on line {opened} never closes, so nothing "
            "after it is read"
        )
    if "sensitive paths" in sections:
        review["sensitive"] = parse_globs(
            "## Sensitive paths", sections["sensitive paths"], True, notes
        )
    if "skip" in sections:
        review["skip"] = parse_globs("## Skip", sections["skip"], False, notes)
    if "failure" in sections:
        review["failure"] = "\n".join(sections["failure"])
    if "nit cap" in sections:
        values = [ln.strip() for ln in sections["nit cap"][1:] if ln.strip()]
        m = re.match(r"^(?:[-*][ \t]+)?(\d+)$", values[0]) if values else None
        if m:
            review["nit_cap"] = int(m.group(1))
        else:
            notes.append(f"REVIEW.md ## Nit cap: not an integer; using {NIT_CAP_DEFAULT}")
    if "security review" in sections:
        sec = parse_security(sections["security review"], rule_ids)
        review["security"] = sec
        for line in sec["unparsed"]:
            notes.append(f"REVIEW.md ## Security review: ignored line: {line}")
    entries = review["sensitive"] + review["skip"]
    if entries:
        listing = git.run("ls-tree", "-r", "-z", "--name-only", base).stdout.split(b"\0")
        names = [decode(n) for n in listing if n]
        for g in entries:
            if not any(glob_match(n, g) for n in names):
                notes.append(f"REVIEW.md: `{g}` matches no path at the base ref")
    review["warnings"] = notes
    warnings.extend(notes)
    return review


def rule_files(git: Git, base: str) -> list[dict]:
    rules = []
    for prefix in ("", ".github/", "docs/"):
        args = ["ls-tree", "-z", base] + (["--", prefix] if prefix else [])
        for entry in git.run(*args).stdout.split(b"\0"):
            if not entry:
                continue
            meta, raw = entry.split(b"\t", 1)
            path = decode(raw)
            name = posixpath.basename(path)
            if name not in ("REVIEW.md", "AGENTS.md", "CLAUDE.md") and not name.startswith(
                "CONTRIBUTING"
            ):
                continue
            mode, kind, blob = decode(meta).split(" ")
            if kind != "blob":
                continue
            item = {"path": path, "mode": mode, "blob": blob}
            show = path
            if mode == "120000":
                target = decode(git.run("cat-file", "blob", blob).stdout)
                item["target"] = target
                resolved = posixpath.normpath(posixpath.join(posixpath.dirname(path), target))
                if target.startswith("/") or resolved.startswith(".."):
                    item["show"] = None
                    rules.append(item)
                    continue
                item["resolved"] = resolved
                show = resolved
            # <rev>:<path> is relative to the root, so this needs no -C (see diff_command).
            item["show"] = shell_join(["git", "show", f"{base}:{show}"])
            rules.append(item)
    # A link to another listed rule file (CLAUDE.md -> AGENTS.md) is that file: one read.
    listed = {item["path"] for item in rules}
    for item in rules:
        resolved = item.get("resolved")
        if resolved in listed and resolved != item["path"]:
            item["same_as"] = resolved
            item["show"] = None
    return rules


IMPORT_RE = re.compile(r"(?:^|(?<=[\s(\[{'\"]))@([^\s`'\"()\[\]{}<>]+)", re.M)


def agent_imports(
    git: Git, base: str, rules: list[dict], warnings: list[str]
) -> tuple[set[str], list[str]]:
    """Paths the base CLAUDE.md and AGENTS.md import with @path, directly or through
    another import, each relative to the file that imports it, and the approval reasons
    when the walk could not finish (IMPORT_CAP reached, a link that cannot be read).

    An imported file is agent instructions too, and harnesses load imports recursively.
    Only Markdown files are read for further imports: '@scope/pkg' in an imported
    package.json is not an import. Over-matching only adds review.
    """
    found: set[str] = set()
    reasons: list[str] = []
    read: set[str] = set()
    queue: list[tuple[str, set[str]]] = []
    for item in rules:
        if posixpath.basename(item["path"]) not in ("CLAUDE.md", "AGENTS.md"):
            continue
        if item["mode"] == "120000":
            if not item.get("resolved"):
                continue
            proc = git.run("cat-file", "blob", f"{base}:{item['resolved']}", check=False)
            if proc.returncode:
                continue
            text = decode(proc.stdout)
            dirs = {posixpath.dirname(item["path"]), posixpath.dirname(item["resolved"])}
            read.add(item["resolved"])
        else:
            text = decode(git.run("cat-file", "blob", item["blob"]).stdout)
            dirs = {posixpath.dirname(item["path"])}
        read.add(item["path"])
        queue.append((text, dirs))
    # Breadth first; read keeps a cycle (a file importing its importer) from looping.
    for text, dirs in queue:
        for m in IMPORT_RE.finditer(text):
            token = m.group(1).rstrip(".,;:!?")
            if not token or token.startswith(("~", "/")):
                continue
            for d in sorted(dirs):
                target = posixpath.normpath(posixpath.join(d, token))
                if target in (".", "..") or target.startswith("../") or target in found:
                    continue
                places = [target]
                # An in-repo link that the import names directly imports the file it points
                # at: that file is instructions too. Chained or directory links are not
                # followed. ':(literal)' makes ls-tree read the path the way cat-file does.
                spec = f":(literal){target}"
                entry = git.run("ls-tree", "-z", base, "--", spec, check=False).stdout
                if entry.startswith(b"120000 "):
                    proc = git.run("cat-file", "blob", f"{base}:{target}", check=False)
                    link = decode(proc.stdout)
                    real = posixpath.normpath(posixpath.join(posixpath.dirname(target), link))
                    if proc.returncode:
                        # Fail closed: the file behind the link is unknown.
                        warnings.append(f"@path imports: cannot read the link {target}")
                        if UNREAD_LINK not in reasons:
                            reasons.append(UNREAD_LINK)
                    elif (
                        not link.startswith("/")
                        and real not in (".", "..")
                        and not real.startswith("../")
                    ):
                        places.append(real)
                for place in places:
                    if place in found:
                        continue
                    if len(found) >= IMPORT_CAP:
                        warnings.append(
                            f"@path imports: more than {IMPORT_CAP} files imported from "
                            "CLAUDE.md and AGENTS.md at the base ref; the rest are not "
                            "treated as agent instructions"
                        )
                        return found, reasons + ["too many @path imports to classify"]
                    found.add(place)
                source = places[-1]
                if source in read or not source.lower().endswith(".md"):
                    continue
                read.add(source)
                proc = git.run("cat-file", "blob", f"{base}:{source}", check=False)
                if not proc.returncode:
                    dirs_next = {posixpath.dirname(target), posixpath.dirname(source)}
                    queue.append((decode(proc.stdout), dirs_next))
    return found, reasons


# ---------------------------------------------------------------------------
# Known findings file
# ---------------------------------------------------------------------------


def read_utf8(path: str, what: str) -> str:
    try:
        return Path(path).read_text(encoding="utf-8")
    except OSError as exc:
        raise PanelError(f"cannot read {what} {path}: {exc.strerror}") from None
    except UnicodeDecodeError as exc:
        raise PanelError(f"{what} {path} is not UTF-8: byte {exc.start}") from None


def load_json(path: str, what: str):
    text = read_utf8(path, what)
    try:
        return json.loads(text)
    except json.JSONDecodeError as exc:
        raise PanelError(f"{what} {path} is not valid JSON: {exc}") from None


def is_int(value, minimum: int = 0) -> bool:
    return isinstance(value, int) and not isinstance(value, bool) and value >= minimum


def read_known(path: str) -> dict:
    data = load_json(path, "known findings file")
    if isinstance(data, list):
        known = {"version": 1, "round": 0, "rounds_with_open_findings": 0, "findings": data}
    elif isinstance(data, dict):
        known = data
        if known.get("version") != 1:
            raise PanelError(f"known findings file {path}: version must be 1")
        for key in ("round", "rounds_with_open_findings"):
            if not is_int(known.get(key)):
                raise PanelError(f"known findings file {path}: {key} must be an integer >= 0")
        if not isinstance(known.get("findings"), list):
            raise PanelError(f"known findings file {path}: findings must be a list")
    else:
        raise PanelError(f"known findings file {path}: expected an object or a list")
    items = []
    for i, item in enumerate(known["findings"]):
        where = f"known findings file {path}: findings[{i}]"
        if not isinstance(item, dict):
            raise PanelError(f"{where} is not an object")
        if not isinstance(item.get("id"), str) or not ID_RE.match(item["id"]):
            raise PanelError(f"{where}.id must look like CR-0123abcd")
        if item.get("status") not in KNOWN_STATUSES:
            raise PanelError(f"{where}.status must be one of {', '.join(KNOWN_STATUSES)}")
        seen = item.get("last_seen_round", known["round"])
        if not is_int(seen):
            raise PanelError(f"{where}.last_seen_round must be an integer >= 0")
        line = item.get("line", 0)
        instances = item.get("instances", [])
        if not isinstance(instances, list):
            raise PanelError(f"{where}.instances must be a list")
        items.append(
            {
                "id": item["id"],
                "lens": str(item.get("lens", "")),
                "class": str(item.get("class", "")),
                "file": str(item.get("file", "")),
                "line": line if is_int(line) else 0,
                "title": str(item.get("title", "")),
                "severity": str(item.get("severity", "")),
                "status": item["status"],
                "last_seen_round": seen,
                "instances": [
                    location(v, f"{where}.instances[{j}]") for j, v in enumerate(instances)
                ],
            }
        )
    return {
        "version": 1,
        "round": known["round"],
        "rounds_with_open_findings": known["rounds_with_open_findings"],
        "findings": items,
    }


def open_items(known: dict, round_no: int) -> list[dict]:
    return [
        i for i in known["findings"] if i["status"] == "posted" and i["last_seen_round"] == round_no
    ]


def in_work_tree(git: Git, path: Path) -> bool:
    """True when path lies in the reviewed work tree, where a change can write it.

    The git directory is outside the reach of a change, so it does not count.
    """
    top = Path(git.top).resolve()
    git_dir = Path(git.text("rev-parse", "--absolute-git-dir")).resolve()
    return path.is_relative_to(top) and not path.is_relative_to(git_dir)


# ---------------------------------------------------------------------------
# Triage
# ---------------------------------------------------------------------------


def shell_join(parts: list[str]) -> str:
    return " ".join(shlex.quote(p) for p in parts)


def diff_command(a: str, b: str, excludes: list[str], stat: bool = False) -> str:
    # Starts with git, no env wrapper, so a harness rule that allows git runs it as
    # written; --no-ext-diff already overrides GIT_EXTERNAL_DIFF. No -C either: a
    # harness can refuse git on a directory outside the ones it allows (the top level
    # when run from a subdirectory), and --no-relative plus the top-anchored excludes
    # give the same output from any directory of the work tree. It always ends with
    # "--" and the excludes, so a reader can append a path's entry from the plan's
    # changed.quoted_pathspecs to it.
    parts = [*GIT, "diff", *DIFF_FLAGS, "--text"]
    if stat:
        parts.append("--shortstat")
    parts += [a, b, "--", *excludes]
    return shell_join(parts)


def exclude(path: str) -> str:
    return f":(exclude,literal,top){path}"


def exclude_specs(
    changes: list[Change], hunt: list[Change], skip: list[str], warnings: list[str]
) -> list[str]:
    """Pathspecs that leave Skip paths and binary files out of the emitted diffs.

    A pathspec for <p> also hides everything under <p>/, so no exclude is emitted
    over a path the finders must read: such a skipped path is reviewed instead. The
    excludes stay within EXCLUDE_BUDGET; past it they are dropped, Skip first.
    """
    seen = {id(c) for c in changes}
    every = changes + [c for c in hunt if id(c) not in seen]

    def must_read() -> tuple[set[str], set[str]]:
        paths = {c.path for c in every if not c.skipped and not c.binary}
        parents = {p[:i] for p in paths for i, ch in enumerate(p) if ch == "/"}
        return paths, parents

    def hides(path: str, paths: set[str], parents: set[str]) -> bool:
        return path.rstrip("/") in parents or path in paths

    while True:
        paths, parents = must_read()
        over = {c.path for c in every if c.skipped and hides(c.path, paths, parents)}
        if not over:
            break
        for c in every:
            if c.path in over:
                c.skipped = False
        warnings.extend(
            f"Skip not applied to {p}: reviewed paths sit under it" for p in sorted(over)
        )

    def binary_specs() -> tuple[list[str], list[str]]:
        paths, parents = must_read()
        specs, kept = [], []
        for p in sorted({c.path for c in every if c.binary and not c.skipped}):
            if hides(p, paths, parents):
                kept.append(p)
            else:
                specs.append(exclude(p))
        return specs, kept

    paths, parents = must_read()
    skipped = [c.path for c in changes if c.skipped]
    if len(skipped) <= SKIP_GROUP_THRESHOLD:
        skip_specs = [exclude(p) for p in skipped]
    else:
        dirs = [g for g in skip if g.endswith("/") and not hides(g, paths, parents)]
        skip_specs = [exclude(d) for d in dirs]
        for p in skipped:
            if not any(p.startswith(d) for d in dirs):
                skip_specs.append(exclude(p))
    bin_specs, kept = binary_specs()

    def size(specs: list[str]) -> int:
        return len(shell_join(specs).encode("utf-8", "surrogateescape"))

    if skip_specs and size(skip_specs + bin_specs) > EXCLUDE_BUDGET:
        warnings.append("Skip not applied: too many skipped paths")
        for c in every:
            c.skipped = False
        skip_specs = []
        bin_specs, kept = binary_specs()
    warnings.extend(f"binary file {p} not excluded: reviewed paths sit under it" for p in kept)
    if size(bin_specs) > EXCLUDE_BUDGET:
        warnings.append("binary files not excluded: too many binary paths")
        bin_specs = []
    return skip_specs + bin_specs


def evidence_list(items: list[str], label: str) -> list[str]:
    shown = items[:EVIDENCE_CAP]
    if len(items) > EVIDENCE_CAP:
        shown.append(f"and {len(items) - EVIDENCE_CAP} more ({label})")
    return shown


def pick_tier(changes: list[Change]) -> tuple[str, list[str]]:
    if not changes:
        return "standard", ["empty change set"]
    deep: dict[str, list[str]] = {}
    for c in changes:
        for cat in DEEP_CATEGORIES:
            if cat in c.cats:
                deep.setdefault(cat, []).append(f"{c.path} ({cat})")
    if deep:
        evidence = []
        for cat in DEEP_CATEGORIES:
            evidence += evidence_list(deep.get(cat, []), cat)
        return "deep", evidence
    other = [c for c in changes if not c.cats & {"prose", "tests"}]
    if not other:
        return "quick", [f"{len(changes)} paths, all prose or tests"]
    labels = []
    for c in other:
        label = next(
            (
                k
                for k in ("prompt", "deps/build", "container", "link", "dot markdown")
                if k in c.cats
            ),
            "code",
        )
        labels.append(f"{c.path} ({label})")
    return "standard", evidence_list(labels, "not prose or tests")


def approval_hint(changes: list[Change], tier: str, round_cap: bool, extra: list[str]) -> dict:
    reasons = []
    cats = set().union(*(c.cats for c in changes)) if changes else set()
    if not changes:
        reasons.append("empty change set")
    if tier == "deep":
        present = [cat for cat in DEEP_CATEGORIES if cat in cats]
        reasons.append(f"deep tier ({', '.join(present)})")
    for cat, reason in (
        ("ci", "CI files"),
        ("sensitive", "sensitive paths"),
        ("agent", "agent instruction files"),
        ("prompt", "prompt files"),
        ("dot markdown", "Markdown under a dot-directory"),
        ("deps/build", "dependency or build changes"),
    ):
        if cat in cats:
            reasons.append(reason)
    if any("prose" not in c.cats for c in changes):
        reasons.append("code changes")
    if round_cap:
        reasons.append("round cap")
    reasons += extra
    return {
        "hint": "needs a human" if reasons else "low risk",
        "reasons": reasons,
        "note": AGENT_NOTE,
    }


def detect_domains(
    git: Git, a: str, b: str, changes: list[Change], excludes: list[str], warnings: list[str]
) -> dict:
    """Domain lenses from reviewed, non-prose, non-binary paths of a..b and their lines."""
    scanned = {
        c.path: c for c in changes if not c.skipped and not c.binary and "prose" not in c.cats
    }
    found: dict[str, dict] = {}

    def add(lens: str, evidence: str) -> None:
        entry = found.setdefault(lens, {"evidence": [], "more": 0})
        if len(entry["evidence"]) < EVIDENCE_CAP:
            entry["evidence"].append(evidence)
        else:
            entry["more"] += 1

    for lens, rule in LENS_RULES.items():
        if rule["kind"] != "domain":
            continue
        for c in scanned.values():
            for cat in rule.get("categories", ()):
                if cat in c.cats:
                    add(lens, f"{c.path} ({cat})")
            for label, regex, mode in rule.get("paths", ()):
                hit = (mode and mode in (c.old_mode, c.new_mode)) or (
                    regex is not None and regex.search(c.path)
                )
                if hit:
                    add(lens, f"{c.path} ({label})")
    content = [
        (lens, label, regex, exclude)
        for lens, rule in LENS_RULES.items()
        for label, regex, exclude in rule.get("content", ())
    ]
    truncated: list[str] = []

    def cut(note: str) -> None:
        if len(truncated) <= EVIDENCE_CAP:
            truncated.append(note)

    if scanned and content:
        per_file: dict[str, int] = {}
        total = 0
        # The excludes never cover a scanned path (exclude_specs keeps them clear).
        patch = iter_patch(git, a, b, tuple(excludes))
        try:
            for path, sign, num, raw in patch:
                if path not in scanned:
                    continue
                total += 1
                if total > TOTAL_LINE_CAP:
                    cut(f"over {TOTAL_LINE_CAP} changed lines in total")
                    break
                per_file[path] = per_file.get(path, 0) + 1
                if per_file[path] > FILE_LINE_CAP:
                    if per_file[path] == FILE_LINE_CAP + 1:
                        cut(f"{path}: over {FILE_LINE_CAP} changed lines")
                    continue
                if len(raw) > LINE_CAP:
                    cut(f"{path}:{num}: a line over {LINE_CAP} characters")
                    raw = raw[:LINE_CAP]
                text = raw.decode("utf-8", "replace")
                suffix = "" if sign == "+" else ", removed line"
                for lens, label, regex, exclude in content:
                    if exclude is not None and exclude.search(path):
                        continue
                    if regex.search(text):
                        add(lens, f"{path}:{num} ({label}{suffix})")
        finally:
            patch.close()
    if truncated:
        warnings.append(
            "content scan truncated (" + "; ".join(truncated[:EVIDENCE_CAP]) + "); "
            "every domain lens added"
        )
        for lens, rule in LENS_RULES.items():
            if rule["kind"] == "domain" and lens not in found:
                add(lens, "content scan truncated")
    return found


def plan_lenses(tier: str, domains: dict, kept: dict[str, list[str]]) -> list[dict]:
    """The lenses for the tier and domains, plus each lens in kept (lens: open known ids):
    only its own lens checks a known item, so it runs, in full, even when the change no
    longer triggers it or the tier dropped, and its kept_for names those items."""
    lenses = []
    for name, rule in LENS_RULES.items():
        triggered = tier in rule["tiers"] and (rule["kind"] != "domain" or name in domains)
        if not triggered and name not in kept:
            continue
        skeptics = ["facts", "impact"]
        if tier == "deep" and rule["reproduce"]:
            skeptics.append("reproduce")
        docs = tier == "quick" and rule.get("docs_on_quick") and name not in kept
        lens = {
            "name": name,
            "persona": str(persona_path(name)),
            "facts_persona": str(facts_path(name)),
            "mode": "docs" if docs else "normal",
            "skeptics": skeptics,
        }
        if name in kept:
            lens["kept_for"] = kept[name]
        lenses.append(lens)
    return lenses


def settings_block(sec: dict | None, failure: str | None) -> dict:
    """Settings blocks, verbatim: Security review and Failure for security, Failure for others."""
    parts = []
    ignored = []
    if sec:
        parts.append(sec["text"])
        unknown = set(sec["unknown_ids"])
        for line in sec["unparsed"]:
            m = DROP_ANY_RE.match(line)
            if m and m.group(1) in unknown:
                ignored.append(f"{line} (unknown id {m.group(1)})")
            else:
                ignored.append(f"{line} (not in the grammar)")
    if failure:
        parts.append(failure)
    return {
        "security": {"text": "\n\n".join(parts) if parts else "none", "ignored": ignored},
        "other": {"text": failure or "none", "ignored": []},
    }


def cmd_triage(args: argparse.Namespace) -> int:
    rule_ids = check_install()
    if args.wave < 1:
        raise PanelError("--wave must be at least 1")
    if args.round is not None and args.round < 1:
        raise PanelError("--round must be at least 1")
    top = toplevel(args.repo)
    git = Git(top)
    warnings: list[str] = []

    base_ref, head_ref, source = resolve_range(git, args.base, args.head)
    shallow = git.text("rev-parse", "--is-shallow-repository") == "true"
    hint = f" This is a shallow clone. {FETCH_HINT}" if shallow else ""
    base = git.commit(base_ref, "base", hint)
    head = git.commit(head_ref, "head", hint)
    proc = git.run("merge-base", base, head, check=False)
    merge_base = decode(proc.stdout).strip()
    if proc.returncode or not merge_base:
        note = " This is a shallow clone." if shallow else ""
        raise PanelError(f"no merge base between {base_ref} and {head_ref}.{note} {FETCH_HINT}")
    if shallow:
        warnings.append("shallow clone: history may be incomplete")
    extra_reasons = []
    checked_out = git.run("rev-parse", "--verify", "--quiet", "HEAD^{commit}", check=False)
    if decode(checked_out.stdout).strip() != head:
        warnings.append(
            f"reviewers read the work tree at HEAD, which is not the reviewed head {short(head)}"
        )
        extra_reasons.append("HEAD is not the reviewed head")

    since = None
    hunt_from = merge_base
    if args.since:
        since_sha = git.commit(args.since, "since")
        used = git.is_ancestor(merge_base, since_sha) and git.is_ancestor(since_sha, head)
        note = None
        if used:
            hunt_from = since_sha
        else:
            note = (
                f"{args.since} is not between the merge base and head (rebased?); "
                "the whole branch is the hunt range"
            )
            warnings.append(note)
        since = {"ref": args.since, "sha": since_sha, "used": used, "note": note}

    status = git.run("status", "--porcelain", "-z").stdout
    if status.strip(b"\0"):
        warnings.append("dirty tree: uncommitted changes are not reviewed")

    known = read_known(args.known) if args.known else None
    known_file = str(Path(args.known).resolve()) if args.known else None
    if known_file and in_work_tree(git, Path(known_file)):
        warnings.append(
            f"known findings file {known_file} is inside the reviewed work tree, "
            "where a change can edit it"
        )
        extra_reasons.append("known findings file inside the reviewed tree")
    if args.round is not None:
        if known is not None and args.round <= known["round"]:
            raise PanelError(
                f"--round {args.round} is not past round {known['round']} of the known "
                f"findings file {args.known}"
            )
        round_no = args.round
    else:
        round_no = known["round"] + 1 if known is not None else 1
    streak = known["rounds_with_open_findings"] if known is not None else 0
    open_known = []
    if known is not None:
        open_known = [
            {k: i[k] for k in ("id", "lens", "title", "file", "line", "severity")}
            for i in open_items(known, known["round"])
        ]
    # Declining the open items in the known file lifts the cap.
    round_cap = streak >= ROUND_CAP and bool(open_known)
    # "open" tells a finder which items it must list as still present or fixed; open
    # items go first, so the list cap drops closed ones before them.
    open_ids = {i["id"] for i in open_known}
    known_items = sorted(
        (
            {
                k: i[k]
                for k in ("id", "lens", "class", "file", "line", "title", "status", "instances")
            }
            | {"open": i["id"] in open_ids}
            for i in (known["findings"] if known is not None else [])
        ),
        key=lambda i: not i["open"],
    )
    if len(known_items) > LIST_CAP:
        warnings.append(
            f"known findings file has {len(known_items)} items; the plan lists the first {LIST_CAP}"
        )

    review = read_review_md(git, base, rule_ids, warnings)
    if review["unclosed_comment"]:
        extra_reasons.append("REVIEW.md has an unclosed comment")
    rules = rule_files(git, base)
    imports, import_reasons = agent_imports(git, base, rules, warnings)
    extra_reasons += import_reasons
    changes = raw_changes(git, merge_base, head)
    for c in changes:
        c.cats = classify(c, review["sensitive"], imports)
        c.skipped = any(glob_match(c.path, g) for g in review["skip"])
    hunt = changes
    if hunt_from != merge_base:
        hunt = raw_changes(git, hunt_from, head)
        for c in hunt:
            c.cats = classify(c, review["sensitive"], imports)
            c.skipped = any(glob_match(c.path, g) for g in review["skip"])
    mark_binary(git, changes if hunt is changes else changes + hunt)

    empty = None
    if not changes:
        empty = "change"
    elif not hunt:
        empty = "hunt"
    excludes = exclude_specs(changes, hunt, review["skip"], warnings)
    domains = detect_domains(git, hunt_from, head, hunt, excludes, warnings) if hunt else {}
    tier, tier_evidence = pick_tier(changes)
    if tier == "quick" and domains:
        # A domain lens never runs on quick, so a detected domain makes the change standard.
        tier = "standard"
        tier_evidence = [f"domain {name} detected" for name in domains]
    binary = [c.binary for c in changes if c.binary and not c.skipped]
    if binary:
        extra_reasons.insert(0, "binary files not reviewed")
    approval = approval_hint(changes, tier, round_cap, extra_reasons)
    kept: dict[str, list[str]] = {}
    for item in open_known:
        if item["lens"] in LENS_RULES:
            kept.setdefault(item["lens"], []).append(item["id"])
        else:
            warnings.append(
                f"known {item['id']} has lens {item['lens']!r}, which this panel does not "
                "have; it stays open"
            )
    lenses = plan_lenses(tier, domains, kept)

    # Not media and not deleted: code nobody read, so the review is incomplete.
    opaque_binary = sum(not b["media"] and not b["deleted"] for b in binary)
    reviewed = [c.path for c in changes if not c.skipped and not c.binary]
    # Shell-quoted here so no agent quotes a file name for a per-path diff. An unusual
    # name (a quote, a control character) gets none, and neither does a non-ASCII one,
    # which the ASCII-escaped plan would show escaped; both are read from the whole diff.
    plain = {c.path for c in changes if "unusual name" not in c.cats and c.path.isascii()}
    quoted_pathspecs = {
        p: shell_join([f":(literal,top){p}"]) for p in reviewed[:LIST_CAP] if p in plain
    }
    skipped = [c.path for c in changes if c.skipped]
    sec_section = review.pop("security")
    sec = sec_section or {
        "text": "none",
        "drops": [],
        "precedents": [],
        "categories": [],
        "unknown_ids": [],
        "unparsed": [],
    }
    for uid in sec["unknown_ids"]:
        warnings.append(f"REVIEW.md ## Security review: unknown rule id {uid}; the default stays")
    nit_cap = review["nit_cap"]
    plan = {
        "version": PLAN_VERSION,
        "repo": top,
        "skill_dir": str(SKILL_DIR),
        "base": {"ref": base_ref, "sha": base, "source": source},
        "head": {"ref": head_ref, "sha": head},
        "merge_base": merge_base,
        "since": since,
        "empty": empty,
        "warnings": warnings,
        "changed": {
            "count": len(changes),
            "reviewed": reviewed[:LIST_CAP],
            "quoted_pathspecs": quoted_pathspecs,
            "skipped": skipped[:LIST_CAP],
            "binary": binary[:LIST_CAP],
            "opaque_binary": opaque_binary,
            "truncated": any(len(x) > LIST_CAP for x in (reviewed, skipped, binary)),
        },
        "tier": {"name": tier, "evidence": tier_evidence},
        "approval": approval,
        "domains": domains,
        "lenses": lenses,
        "security": {
            "finder": str(FINDER),
            "filter": str(FILTER),
            "rule_ids": rule_ids,
            "overrides": sec,
        },
        "settings": settings_block(sec_section, review["failure"]),
        "review_md": review,
        "rules": rules,
        "diff": {
            "full": diff_command(merge_base, head, excludes),
            "hunt": diff_command(hunt_from, head, excludes),
            "check": diff_command(merge_base, head, excludes, stat=True),
            "log": shell_join([*GIT, "log", "--no-decorate", f"{merge_base}..{head}"]),
        },
        "round": round_no,
        "round_cap": round_cap,
        "rounds_with_open_findings": streak,
        "known_file": known_file,
        "known": known_items[:LIST_CAP],
        "open_known": open_known,
        "limits": {
            "max_findings_per_lens": MAX_PER_LENS,
            "max_verified": MAX_VERIFIED,
            "max_minor_verified": 2 * nit_cap,
            "wave": args.wave,
        },
    }
    if args.format == "text":
        sys.stdout.write(plan_text(plan))
    else:
        sys.stdout.write(json.dumps(plan, ensure_ascii=True, indent=2) + "\n")
    return 0


def esc(text: str) -> str:
    """Escape control and format characters so terminal output stays inert."""
    out = []
    for ch in text:
        if ch == " " or ch.isprintable():
            out.append(ch)
        else:
            code = ord(ch)
            out.append(f"\\x{code:02x}" if code < 0x100 else f"\\u{code:04x}")
    return "".join(out)


def plan_text(plan: dict) -> str:
    lines = [
        f"tier: {plan['tier']['name']}",
        *(f"  {e}" for e in plan["tier"]["evidence"]),
        f"approval: {plan['approval']['hint']}",
        *(f"  - {r}" for r in plan["approval"]["reasons"]),
        f"  {plan['approval']['note']}",
        f"base: {plan['base']['ref']} ({plan['base']['source']}) {plan['base']['sha']}",
        f"head: {plan['head']['ref']} {plan['head']['sha']}",
        f"merge base: {plan['merge_base']}",
    ]
    if plan["since"]:
        s = plan["since"]
        lines.append(f"since: {s['ref']} {s['sha']} ({'used' if s['used'] else 'not used'})")
    if plan["empty"]:
        lines.append(f"empty: {plan['empty']}")
    lines.append(f"changed: {plan['changed']['count']} paths")
    lines.append("lenses:")
    for lens in plan["lenses"]:
        kept = f" (kept for open {', '.join(lens['kept_for'])})" if lens.get("kept_for") else ""
        lines.append(f"  {lens['name']} ({lens['mode']}): {', '.join(lens['skeptics'])}{kept}")
    for name, dom in plan["domains"].items():
        more = f" and {dom['more']} more" if dom["more"] else ""
        lines.append(f"domain {name}: {'; '.join(dom['evidence'])}{more}")
    lines.append(f"round: {plan['round']}{' (round cap)' if plan['round_cap'] else ''}")
    for w in plan["warnings"]:
        lines.append(f"warning: {w}")
    lines.append(f"diff (hunt): {plan['diff']['hunt']}")
    return "".join(esc(ln) + "\n" for ln in lines)


# ---------------------------------------------------------------------------
# Report: findings, ids, verdicts and voting
# ---------------------------------------------------------------------------


def finding_id(lens: str, cls: str, file: str, line: int | None = None) -> str:
    parts = [lens, cls, file] + ([str(line)] if line is not None else [])
    key = "\0".join(parts).encode("utf-8", "surrogateescape")
    return "CR-" + hashlib.sha256(key).hexdigest()[:8]


def split_location(value, where: str) -> dict:
    if isinstance(value, dict):
        return location(value, where)
    if not isinstance(value, str) or not value:
        raise PanelError(f"{where} must be 'path:line'")
    path, sep, line = value.rpartition(":")
    if sep and path and line.isascii() and line.isdigit():
        return {"file": path, "line": int(line)}
    return {"file": value, "line": 0}


def location(value, where: str) -> dict:
    if not isinstance(value, dict) or not isinstance(value.get("file"), str) or not value["file"]:
        raise PanelError(f"{where} must be an object with file and line")
    line = value.get("line", 0)
    if not is_int(line):
        raise PanelError(f"{where}.line must be an integer >= 0")
    return {"file": value["file"], "line": line}


def need_str(row: dict, key: str, where: str, allow_empty: bool = False) -> str:
    value = row.get(key)
    if not isinstance(value, str) or (not allow_empty and not value.strip()):
        raise PanelError(f"{where}.{key} must be a non-empty string")
    return value


def first_sentence(text: str) -> str:
    sentence = re.split(r"(?<=[.!?])\s", text.strip(), maxsplit=1)[0]
    return sentence if len(sentence) <= 120 else sentence[:117].rstrip() + "..."


def common_extras(row: dict, out: dict, where: str) -> None:
    pre = row.get("pre_existing", False)
    if not isinstance(pre, bool):
        raise PanelError(f"{where}.pre_existing must be true or false")
    out["pre_existing"] = pre
    kid = row.get("known_id")
    if kid is not None and (not isinstance(kid, str) or not ID_RE.match(kid)):
        raise PanelError(f"{where}.known_id must look like CR-0123abcd")
    out["known_id"] = kid
    new = row.get("new_evidence")
    if new is not None and not isinstance(new, (str, bool)):
        raise PanelError(f"{where}.new_evidence must be a string")
    out["new_evidence"] = new or None


def map_security(row: dict, where: str, warnings: list[str]) -> dict | None:
    sev = str(row.get("severity", "")).upper()
    if sev == "LOW":
        warnings.append(f"{where}: LOW security finding dropped")
        return None
    if sev not in ("HIGH", "MEDIUM"):
        raise PanelError(f"{where}.severity must be HIGH or MEDIUM")
    category = need_str(row, "category", where)
    cls = re.sub(r"[^a-z0-9]+", "-", category.lower()).strip("-")
    if not 3 <= len(cls) <= 60:
        raise PanelError(f"{where}.category {category!r} maps to class {cls!r} (3-60 chars)")
    description = need_str(row, "description", where)
    loc = location({"file": row.get("file"), "line": row.get("line", 0)}, where)
    instances = row.get("instances", [])
    if not isinstance(instances, list):
        raise PanelError(f"{where}.instances must be a list")
    conf = row.get("confidence")
    out = {
        "lens": "security",
        "class": cls,
        "title": first_sentence(description),
        "description": description,
        "severity": "blocking" if sev == "HIGH" else "major",
        "file": loc["file"],
        "line": loc["line"],
        "evidence": need_str(row, "exploit_scenario", where, True),
        "fix": need_str(row, "recommendation", where, True),
        "instances": [
            split_location(v, f"{where}.instances[{j}]") for j, v in enumerate(instances)
        ],
        "finder_confidence": conf if isinstance(conf, (int, float)) else None,
    }
    common_extras(row, out, where)
    return out


def map_common(row: dict, where: str, lenses: set[str]) -> dict:
    lens = row.get("lens")
    if lens not in lenses:
        raise PanelError(f"{where}.lens {lens!r} is not a lens of this plan")
    cls = need_str(row, "class", where)
    if not CLASS_RE.match(cls) or not 3 <= len(cls) <= 60:
        raise PanelError(f"{where}.class {cls!r} must be a kebab slug of 3-60 chars")
    sev = row.get("severity")
    if sev not in SEVERITIES:
        raise PanelError(f"{where}.severity must be one of {', '.join(SEVERITIES)}")
    loc = location({"file": row.get("file"), "line": row.get("line", 0)}, where)
    instances = row.get("instances", [])
    if not isinstance(instances, list):
        raise PanelError(f"{where}.instances must be a list")
    out = {
        "lens": lens,
        "class": cls,
        "title": need_str(row, "title", where),
        "description": None,
        "severity": sev,
        "file": loc["file"],
        "line": loc["line"],
        "evidence": need_str(row, "evidence", where, True),
        "fix": need_str(row, "fix", where, True),
        "instances": [location(v, f"{where}.instances[{j}]") for j, v in enumerate(instances)],
        "finder_confidence": None,
    }
    common_extras(row, out, where)
    return out


class PathNormalizer:
    """Rewrite a finding's path to the repo-relative form git prints, with a warning.

    An absolute path under the repo top becomes relative, a leading './' goes, and a
    leading 'a/' or 'b/' (diff prefixes) goes when the rest is a changed path and the
    path itself is not.
    """

    def __init__(self, plan: dict, warnings: list[str]) -> None:
        self.plan = plan
        self.warnings = warnings
        top = plan["repo"].rstrip("/")
        self.tops = sorted({top, os.path.realpath(top)})
        self.changed: set[str] | None = None

    def changed_paths(self) -> set[str]:
        if self.changed is None:
            ch = self.plan.get("changed") or {}
            paths = set(ch.get("reviewed", [])) | set(ch.get("skipped", []))
            paths |= {b["path"] for b in ch.get("binary", [])}
            if ch.get("truncated"):
                git = Git(self.plan["repo"])
                head = self.plan["head"]["sha"]
                paths |= {c.path for c in raw_changes(git, self.plan["merge_base"], head)}
            self.changed = paths
        return self.changed

    def __call__(self, path: str, where: str) -> str:
        new = path
        for top in self.tops:
            if new.startswith(top + "/") and len(new) > len(top) + 1:
                new = new[len(top) + 1 :]
                break
        while new.startswith("./") and len(new) > 2:
            new = new[2:]
        if new[:2] in ("a/", "b/") and new not in self.changed_paths():
            if new[2:] in self.changed_paths():
                new = new[2:]
        if new != path:
            self.warnings.append(f"{where} {path!r} read as {new!r}")
        return new


def load_findings(path: str, plan: dict, warnings: list[str]) -> dict:
    data = load_json(path, "findings file")
    if not isinstance(data, dict) or data.get("version") != 1:
        raise PanelError(f"findings file {path}: expected an object with version 1")
    if not isinstance(data.get("independent"), bool):
        raise PanelError(f"findings file {path}: independent must be true or false")
    rows = data.get("findings")
    if not isinstance(rows, list):
        raise PanelError(f"findings file {path}: findings must be a list")
    for key in ("not_covered", "still_present", "unread"):
        value = data.get(key, [])
        if not isinstance(value, list) or not all(isinstance(v, str) for v in value):
            raise PanelError(f"findings file {path}: {key} must be a list of strings")
    fixed = data.get("fixed", [])
    if not isinstance(fixed, list) or not all(
        isinstance(f, dict) and all(isinstance(f.get(k), str) for k in ("id", "reason", "lens"))
        for f in fixed
    ):
        raise PanelError(
            f"findings file {path}: fixed must be a list of {{id, reason, lens}} objects"
        )
    candidates = data.get("candidates", len(rows))
    if not is_int(candidates):
        raise PanelError(f"findings file {path}: candidates must be an integer >= 0")
    lenses = {lens["name"] for lens in plan["lenses"]}
    norm = PathNormalizer(plan, warnings)
    mapped = []
    low = 0
    for i, row in enumerate(rows):
        where = f"findings[{i}]"
        if not isinstance(row, dict):
            raise PanelError(f"{where} is not an object")
        if row.get("lens") == "security" and "category" in row:
            out = map_security(row, where, warnings)
        else:
            out = map_common(row, where, lenses)
        if out is None:
            low += 1
            continue
        out["file"] = norm(out["file"], f"{where}.file")
        for j, inst in enumerate(out["instances"]):
            inst["file"] = norm(inst["file"], f"{where}.instances[{j}].file")
        mapped.append(out)
    return {
        "independent": data["independent"],
        "candidates": max(candidates, len(mapped) + low),
        "dropped_low": low,
        "not_covered": data.get("not_covered", []),
        "still_present": data.get("still_present", []),
        "fixed": [{k: f[k] for k in ("id", "reason", "lens")} for f in fixed],
        "unread": [norm(u, f"unread[{k}]") for k, u in enumerate(data.get("unread", []))],
        "rows": mapped,
    }


def warn_missing_at_head(plan: dict, rows: list[dict], warnings: list[str]) -> None:
    """Warn about finding paths that do not exist at head, which a finder may have made up.
    A finding on a caller outside the change is fine; one on no file at all is not."""
    paths = sorted({r["file"] for r in rows} | {i["file"] for r in rows for i in r["instances"]})
    paths = [p for p in paths if p and "\n" not in p and "\0" not in p]
    if not paths:
        return
    head, base = plan["head"]["sha"], plan["merge_base"]
    # Ask at head and at the merge base: a file the change deletes is a fair place.
    query = "".join(f"{c}:{p}\n" for p in paths for c in (head, base))
    try:
        out = Git(plan["repo"]).run(
            "cat-file",
            "--batch-check=%(objecttype)",
            check=False,
            stdin=query.encode("utf-8", "surrogateescape"),
        )
    except PanelError:
        out = None
    answers = decode(out.stdout).split("\n") if out is not None else []
    # Only a warning depends on it, so a git failure here never fails the report.
    if out is None or out.returncode or len(answers) < 2 * len(paths):
        warnings.append("could not check whether the finding paths exist at head")
        return
    for k, path in enumerate(paths):
        if all(a.endswith(" missing") for a in answers[2 * k : 2 * k + 2]):
            warnings.append(f"{path} does not exist at head {short(head)} or the merge base")


def rank_key(row: dict) -> tuple:
    return (
        SEVERITIES.index(row["severity"]),
        LENS_ORDER.get(row["lens"], len(LENS_ORDER)),
        row["file"],
        row["line"],
    )


def window_id(row: dict, items: list[dict], warnings: list[str]) -> str:
    """The id of the known item this row repeats, else the row's own id.

    A row takes a known item's id only within KNOWN_WINDOW lines of that item's place
    or of one of its instances (same lens and class), so a second bug of the same
    class in the same file is verified, not suppressed.
    """
    computed = finding_id(row["lens"], row["class"], row["file"])
    near = []
    for i in items:
        if i["id"] != computed and (i["lens"], i["class"]) != (row["lens"], row["class"]):
            continue
        # Only places in the row's file count: a reopened item can have moved its
        # primary place to another file, while its id still hashes the first one.
        places = [i] if i["file"] == row["file"] else []
        places += [p for p in i.get("instances", []) if p["file"] == row["file"]]
        near += [(abs(p["line"] - row["line"]), p["line"], i["id"]) for p in places]
    if not near:
        if any(i["id"] == computed for i in items):
            # The hash names a known item with no place in this file (a hand-made list,
            # or an item that moved): a finding here is kept apart, not suppressed.
            own = finding_id(row["lens"], row["class"], row["file"], row["line"])
            warnings.append(
                f"{own} ({row['class']}) at {row['file']}:{row['line']} kept apart from known "
                f"{computed}, which has no place in {row['file']}"
            )
            return own
        return computed
    distance, line, kid = min(near)
    if distance <= KNOWN_WINDOW:
        return kid
    own = finding_id(row["lens"], row["class"], row["file"], row["line"])
    warnings.append(
        f"{own} ({row['class']}) at {row['file']}:{row['line']} kept apart from known "
        f"{kid} at line {line}, more than {KNOWN_WINDOW} lines away"
    )
    return own


def assign_ids(rows: list[dict], known: dict | None, warnings: list[str]) -> None:
    items = known["findings"] if known else []
    known_ids = {i["id"]: i for i in items}
    for row in rows:
        kid = row["known_id"]
        if kid and kid in known_ids:
            row["id"] = kid
        else:
            row["id"] = window_id(row, items, warnings)
            if kid:
                warnings.append(
                    f"known_id {kid} is not in the known findings file; using {row['id']}"
                )
        row["status"] = None
        if row["id"] in known_ids:
            continue
        for item in known_ids.values():
            if (
                item["lens"] == row["lens"]
                and item["file"] == row["file"]
                and item["class"] != row["class"]
                and abs(item["line"] - row["line"]) <= NEARBY_LINES
            ):
                warnings.append(
                    f"{row['id']} ({row['class']}) may duplicate known {item['id']} "
                    f"({item['class']}) at {item['file']}:{item['line']}"
                )


def mark_statuses(rows: list[dict], known: dict | None, limits: dict, warnings: list[str]) -> None:
    known_ids = {i["id"] for i in known["findings"]} if known else set()
    seen: set[str] = set()
    for row in sorted(rows, key=rank_key):
        if row["id"] in seen:
            row["status"] = "duplicate"
        seen.add(row["id"])
    for row in rows:
        if row["status"] is None and row["id"] in known_ids and not row["new_evidence"]:
            row["status"] = "known"
    per_lens: dict[str, int] = {}
    minors = total = 0
    for row in sorted(rows, key=rank_key):
        if row["status"] is not None:
            continue
        if row["severity"] == "minor" and minors >= limits["max_minor_verified"]:
            # The report lists only the nit cap's minors, so the ones past their budget go
            # unverified without making the review incomplete; a nit cap of 0 verifies none.
            row["status"] = "minor-cap"
            continue
        per_lens[row["lens"]] = per_lens.get(row["lens"], 0) + 1
        over = per_lens[row["lens"]] > limits["max_findings_per_lens"]
        if over or total >= limits["max_verified"]:
            row["status"] = "over-cap"
            continue
        row["status"] = "pending"
        total += 1
        minors += row["severity"] == "minor"
    for lens, n in per_lens.items():
        if n > limits["max_findings_per_lens"]:
            warnings.append(
                f"lens {lens} reported {n} findings; the ones past "
                f"{limits['max_findings_per_lens']} are not verified"
            )
    rows_by_file: dict[str, list[dict]] = {}
    for row in rows:
        if row["status"] in ("pending", "over-cap", "minor-cap"):
            rows_by_file.setdefault(row["file"], []).append(row)
    for group in rows_by_file.values():
        for i, a in enumerate(group):
            for b in group[i + 1 :]:
                if a["lens"] != b["lens"] and abs(a["line"] - b["line"]) <= NEARBY_LINES:
                    warnings.append(
                        f"possible cross-lens duplicate: {a['id']} ({a['lens']}) and "
                        f"{b['id']} ({b['lens']}) near {a['file']}:{a['line']}"
                    )


def load_verdicts(path: str, warnings: list[str]) -> tuple[dict, dict]:
    # Split on "\n" only: str.splitlines() also splits on U+2028, U+2029 and U+0085,
    # which JSON allows raw inside a string.
    lines = read_utf8(path, "verdicts file").split("\n")
    verdicts: dict[str, dict] = {}
    resolutions: dict[str, dict] = {}
    for n, line in enumerate(lines, 1):
        line = line.removesuffix("\r")
        if not line.strip():
            continue
        where = f"verdicts file {path}, line {n}"
        try:
            obj = json.loads(line)
        except json.JSONDecodeError as exc:
            raise PanelError(f"{where}: not valid JSON: {exc}") from None
        if not isinstance(obj, dict) or not isinstance(obj.get("id"), str):
            raise PanelError(f"{where}: expected an object with an id")
        if "resolution" in obj:
            res = obj["resolution"]
            if not isinstance(res, dict) or not isinstance(res.get("keep"), bool):
                raise PanelError(f"{where}: resolution must be {{keep: bool, reason}}")
            resolutions[obj["id"]] = {"keep": res["keep"], "reason": str(res.get("reason", ""))}
            continue
        if obj.get("skeptic") not in SKEPTICS:
            raise PanelError(f"{where}: skeptic must be one of {', '.join(SKEPTICS)}")
        verdicts.setdefault(obj["id"], {})[obj["skeptic"]] = obj
    return verdicts, resolutions


def active_rule(rule, security: dict) -> bool:
    """True when rule names an exclusion this review applies: a filter.md id the base
    REVIEW.md did not drop, or one of its precedents R1..Rn."""
    if not isinstance(rule, str):
        return False
    overrides = security.get("overrides") or {}
    dropped = {d["id"] for d in overrides.get("drops", [])}
    if rule in security.get("rule_ids", []) and rule not in dropped:
        return True
    m = re.fullmatch(r"R([1-9]\d*)", rule)
    return bool(m) and int(m.group(1)) <= len(overrides.get("precedents", []))


def vote(
    row: dict, skeptics: list[str], verdicts: dict, security: dict, warnings: list[str]
) -> dict:
    """Vote on one finding: {result, pre_existing, notes, missing}."""
    got = verdicts.get(row["id"], {})
    missing = [s for s in skeptics if s not in got]
    votes: list[bool | None] = []
    notes = []
    facts = got.get("facts")
    if row["lens"] == "security":
        if facts is not None and "refuted" in facts:
            warnings.append(
                f"{row['id']}: a security facts verdict carries filter_confidence and "
                "excluded_by, not refuted; unparsable"
            )
            notes.append("facts: unparsable (refuted on a security facts verdict)")
            votes.append(None)
        elif facts is not None:
            conf = facts.get("filter_confidence")
            if conf is None:
                # filter.md's own key, when the orchestrator copied the verdict as is.
                conf = facts.get("confidence")
            excluded = facts.get("excluded_by")
            # filter.md scores 1 to 10; anything else, such as the finder's 0.95, is no
            # filter score and must not veto: the vote is unparsable, so the row is disputed.
            numeric = (
                isinstance(conf, int)
                and not isinstance(conf, bool)
                and FILTER_SCALE[0] <= conf <= FILTER_SCALE[1]
            )
            veto = None
            if excluded is not None and active_rule(excluded, security):
                # An exclusion is a veto, whatever the confidence says.
                veto = f"excluded by {excluded}"
            elif excluded is None and numeric and conf < KEEP_CONFIDENCE:
                veto = f"filter confidence {conf}"
            if veto:
                return {
                    "result": "rejected",
                    "pre_existing": False,
                    "notes": [f"facts: {veto} (veto)"],
                    "missing": missing,
                }
            if excluded is None and numeric:
                votes.append(True)
                notes.append(f"facts: filter confidence {conf}")
            elif excluded is not None:
                warnings.append(
                    f"{row['id']}: security facts verdict excluded_by {excluded!r} names no "
                    "rule of this review; unparsable"
                )
                notes.append(f"facts: excluded_by {excluded!r} is no rule of this review")
                votes.append(None)
            elif conf is not None:
                warnings.append(
                    f"{row['id']}: security facts verdict confidence {conf!r} is not an "
                    "integer from 1 to 10; unparsable"
                )
                notes.append(f"facts: confidence {conf!r} out of range")
                votes.append(None)
            else:
                warnings.append(f"{row['id']}: unparsable security facts verdict")
                votes.append(None)
        else:
            votes.append(None)
    else:
        votes.append(verdict_vote(row["id"], "facts", facts, notes, warnings))
    impact = got.get("impact")
    votes.append(verdict_vote(row["id"], "impact", impact, notes, warnings))
    pre = impact.get("pre_existing") is True if impact is not None else row["pre_existing"]
    if pre:
        notes.append("impact: pre-existing at the merge base")
    if "reproduce" in skeptics:
        rep = got.get("reproduce")
        result = rep.get("result") if rep else None
        if result in ("upheld", "refuted"):
            votes.append(result == "upheld")
            notes.append(f"reproduce: {result}")
        else:
            notes.append("reproduce: abstained" if rep else "reproduce: missing")
    if None in votes:
        outcome = "disputed"
    elif all(votes):
        outcome = "pre-existing" if pre else "confirmed"
    elif not any(votes):
        outcome = "rejected"
    else:
        outcome = "disputed"
    return {"result": outcome, "pre_existing": pre, "notes": notes, "missing": missing}


def verdict_vote(fid: str, name: str, verdict, notes: list[str], warnings: list[str]):
    if verdict is None:
        notes.append(f"{name}: missing")
        return None
    refuted = verdict.get("refuted")
    if not isinstance(refuted, bool):
        warnings.append(f"{fid}: unparsable {name} verdict")
        notes.append(f"{name}: unparsable")
        return None
    notes.append(f"{name}: {'refuted' if refuted else 'upheld'}")
    return not refuted


def apply_verdicts(rows: list[dict], plan: dict, verdicts: dict, resolutions: dict, warnings):
    skeptics = {lens["name"]: lens["skeptics"] for lens in plan["lenses"]}
    ids = {row["id"] for row in rows}
    for fid in sorted(set(verdicts) | set(resolutions)):
        if fid not in ids:
            warnings.append(f"verdicts name {fid}, which is not in the findings file")
    for row in rows:
        if row["status"] != "pending":
            continue
        result = vote(
            row,
            skeptics.get(row["lens"], ["facts", "impact"]),
            verdicts,
            plan.get("security") or {},
            warnings,
        )
        row["notes"] = result["notes"]
        row["missing"] = result["missing"]
        row["pre_existing"] = result["pre_existing"]
        status = result["result"]
        res = resolutions.get(row["id"])
        if status == "disputed" and res is not None:
            status = "resolved" if res["keep"] else "dismissed"
            row["resolution"] = res["reason"]
        elif res is not None:
            warnings.append(f"{row['id']} has a resolution but is {status}; resolution unused")
        row["status"] = status


# ---------------------------------------------------------------------------
# Report rendering
# ---------------------------------------------------------------------------


def place(row: dict) -> str:
    return f"{row['file']}:{row['line']}" if row["line"] else row["file"]


def md(text: str) -> str:
    """Escape control characters and keep continuation lines inside a list item."""
    return "\n  ".join(esc(part) for part in str(text).splitlines() or [""])


def short(sha: str) -> str:
    return sha[:12]


def item_block(row: dict) -> list[str]:
    lines = [f"**[{row['id']}] {md(row['title'])}** ({esc(place(row))}, {row['lens']})"]
    if row.get("description") and row["description"].strip() != row["title"].strip():
        lines.append(f"- Problem: {md(row['description'])}")
    lines.append(f"- Evidence: {md(row['evidence'])}")
    lines.append(f"- Fix: {md(row['fix'])}")
    if row["instances"]:
        lines.append("- Also at: " + ", ".join(esc(place(i)) for i in row["instances"]))
    new = row.get("new_evidence")
    if new:
        lines.append("- Seen before" + ("" if new is True else f"; new evidence: {md(new)}"))
    if row["status"] == "resolved":
        lines.append(f"- disputed, kept: {md(row.get('resolution') or 'no reason given')}")
    return lines + [""]


def id_list(rows: list[dict]) -> str:
    return "; ".join(f"[{r['id']}] {esc(r['title'])} ({esc(place(r))})" for r in rows)


def first_line(
    counted: list[dict],
    listed_minor: int,
    still_open: list,
    not_covered: list[str],
    unread: list[str],
    opaque: int,
    unverified: int,
) -> tuple[str, list[str]]:
    """The report's first line, and the coverage gaps it names."""
    gaps = []
    if not_covered:
        gaps.append(f"lenses not covered: {esc(', '.join(not_covered))}")
    if unread:
        gaps.append(f"{len(unread)} files not read")
    if opaque:
        gaps.append(f"{opaque} binary files not reviewed")
    if unverified:
        gaps.append(f"{unverified} findings not verified (over budget)")
    if counted:
        n = {s: sum(r["severity"] == s for r in counted) for s in SEVERITIES}
        minor = f"{n['minor']} minor"
        if listed_minor < n["minor"]:
            minor += f" ({listed_minor} listed)"
        line = f"**{len(counted)} to fix:** {n['blocking']} blocking, {n['major']} major, {minor}"
        # A gap, a failed security pass above all, must not read as a complete review.
        if gaps:
            line += "; review incomplete: " + "; ".join(gaps)
        return line, gaps
    if gaps:
        line = "**Review incomplete:** " + "; ".join(gaps)
        if still_open:
            line += f"; {len(still_open)} earlier findings still open"
        return line, gaps
    if still_open:
        return f"**Nothing new to fix;** {len(still_open)} earlier findings still open", gaps
    return "", gaps


class Outcome:
    """Everything the Markdown, the review.py JSON and the known file need."""

    def __init__(self) -> None:
        self.counted: list[dict] = []
        self.listed: list[dict] = []
        self.hidden_minors: list[dict] = []
        self.pre_existing: list[dict] = []
        self.still_open: list[dict] = []
        self.unchecked: list[dict] = []
        self.unmentioned: list[dict] = []
        self.fixed: list[dict] = []
        # Known ids seen this round: reported, still present, unchecked or unmentioned.
        self.seen: set[str] = set()
        self.by_status: dict[str, list[dict]] = {}
        self.approval: dict = {}
        self.first: str = ""
        self.gaps: list[str] = []


def unchecked_known(plan: dict, data: dict, known: dict) -> list[dict]:
    """Open known items no finder of their lens checked: the lens is not in this plan, it
    is listed in not_covered, or the item's file or an instance's file is listed in unread.
    They count as still present, never as fixed."""
    planned = {lens["name"] for lens in plan["lenses"]}
    failed = set(data["not_covered"])
    unread = set(data["unread"])
    return [
        i
        for i in open_items(known, known["round"])
        if i["lens"] not in planned
        or i["lens"] in failed
        or i["file"] in unread
        or any(inst["file"] in unread for inst in i["instances"])
    ]


def summarize(
    plan: dict, data: dict, rows: list[dict], known: dict | None, warnings: list[str]
) -> Outcome:
    out = Outcome()
    for row in sorted(rows, key=rank_key):
        out.by_status.setdefault(row["status"], []).append(row)
    kept = out.by_status.get("confirmed", []) + out.by_status.get("resolved", [])
    kept.sort(key=rank_key)
    out.counted = [r for r in kept if not r["pre_existing"]]
    out.pre_existing = out.by_status.get("pre-existing", []) + [
        r for r in kept if r["pre_existing"]
    ]
    nit_cap = plan["review_md"]["nit_cap"]
    minors = [r for r in out.counted if r["severity"] == "minor"]
    out.hidden_minors = minors[nit_cap:]
    out.listed = [r for r in out.counted if r not in out.hidden_minors]
    if known is not None:
        out.unchecked = unchecked_known(plan, data, known)
        reported = {r["id"] for r in rows}
        seen = set(data["still_present"]) | reported | {i["id"] for i in out.unchecked}
        open_now = {i["id"]: i for i in open_items(known, known["round"])}
        fixed: dict[str, str] = {}
        for f in sorted(data.get("fixed", []), key=lambda f: f["id"]):
            fid, item = f["id"], open_now.get(f["id"])
            if item is None:
                warnings.append(f"fixed {fid} is not an open known item; ignored")
            elif f["lens"] != item["lens"]:
                # Only an item's own lens checks it, so another lens cannot close it.
                warnings.append(
                    f"fixed {fid} comes from lens {f['lens']}, not its lens {item['lens']}; ignored"
                )
            elif fid in seen:
                warnings.append(
                    f"{fid} is listed as fixed but also reported, still present or not "
                    "checked; it stays open"
                )
            else:
                fixed[fid] = f["reason"]
        # Only a finder that says so closes an item: one nobody mentioned stays open.
        out.fixed = [dict(i, reason=fixed[fid]) for fid, i in open_now.items() if fid in fixed]
        out.unmentioned = [i for fid, i in open_now.items() if fid not in seen | fixed.keys()]
        seen |= {i["id"] for i in out.unmentioned}
        out.seen = seen
        counted_ids = {r["id"] for r in out.counted}
        out.still_open = [
            i
            for i in known["findings"]
            if i["status"] == "posted" and i["id"] in seen and i["id"] not in counted_ids
        ]
    not_covered = data["not_covered"]
    opaque = plan["changed"].get("opaque_binary")
    if opaque is None:
        # A plan from before the count: every binary file it lists counts as not reviewed.
        opaque = len(plan["changed"].get("binary", []))
    out.first, out.gaps = first_line(
        out.counted,
        len(minors) - len(out.hidden_minors),
        out.still_open,
        not_covered,
        data["unread"],
        opaque=opaque,
        unverified=len(out.by_status.get("over-cap", [])),
    )
    reasons = list(plan["approval"]["reasons"])
    if not_covered:
        reasons.append(f"lenses not covered: {', '.join(not_covered)}")
    if data["unread"]:
        reasons.append("files not read")
    if out.counted:
        reasons.append(f"{len(out.counted)} findings to fix")
    unverified = out.by_status.get("over-cap", [])
    if unverified:
        reasons.append(f"{len(unverified)} findings not verified")
    if out.still_open:
        reasons.append(f"{len(out.still_open)} earlier findings still open")
    out.approval = {
        "hint": "needs a human" if reasons else plan["approval"]["hint"],
        "reasons": reasons,
    }
    return out


def fold(plan: dict, data: dict, rows: list[dict], out: Outcome, warnings) -> list[str]:
    lines = ["<details><summary>How this was reviewed</summary>", ""]
    tier = plan["tier"]
    lines.append(f"- Tier: {tier['name']} ({esc('; '.join(tier['evidence']))})")
    b, h = plan["base"], plan["head"]
    lines.append(
        f"- Range: base {esc(b['ref'])} ({b['source']}) {short(b['sha'])}, head "
        f"{esc(h['ref'])} {short(h['sha'])}, merge base {short(plan['merge_base'])}"
    )
    reasons = "; ".join(out.approval["reasons"])
    lines.append(
        f"- Approval hint: {out.approval['hint']}"
        + (f" ({esc(reasons)})" if reasons else "")
        + f". {AGENT_NOTE}"
    )
    lenses = ", ".join(f"{lens['name']} ({', '.join(lens['skeptics'])})" for lens in plan["lenses"])
    lines.append(f"- Lenses and skeptics: {lenses}")
    if rows or data["candidates"]:
        for row in sorted(rows, key=rank_key):
            if row.get("notes"):
                lines.append(f"  - {row['id']}: {esc(', '.join(row['notes']))}")
        st = out.by_status
        count = {k: len(v) for k, v in st.items()}
        kept = len(out.counted) + len(out.pre_existing)
        low = data.get("dropped_low", 0)
        # Every candidate lands in one bucket; the merge folded the rest into instances.
        buckets = [
            (count.get("confirmed", 0), "confirmed"),
            (count.get("pre-existing", 0), "pre-existing"),
            (count.get("resolved", 0), "disputed kept"),
            (count.get("rejected", 0), "rejected"),
            (count.get("dismissed", 0), "disputed dismissed"),
            (count.get("known", 0), "known not repeated"),
            (count.get("over-cap", 0), "not verified over budget"),
            (count.get("minor-cap", 0), "minors not verified over the nit cap"),
            (low, "LOW security dropped"),
            (data["candidates"] - len(rows) - low, "merged duplicates"),
        ]
        hidden = len(out.hidden_minors)
        note = f" ({hidden} of them minors over the nit cap, not listed)" if hidden else ""
        lines.append(
            f"- Kept {kept} of {data['candidates']} candidates{note}: "
            + ", ".join(f"{n} {label}" for n, label in buckets)
        )
        for label, group in (
            ("Rejected", st.get("rejected", [])),
            ("Disputed, dismissed", st.get("dismissed", [])),
            ("Known, not repeated", st.get("known", [])),
            ("Minors over the nit cap", out.hidden_minors),
            ("Not verified (over budget)", st.get("over-cap", [])),
            ("Minors not verified (over the nit cap)", st.get("minor-cap", [])),
        ):
            if group:
                lines.append(f"  - {label}: {id_list(group)}")
    if out.fixed:
        fixed = "; ".join(
            f"[{i['id']}] {esc(i['title'])} ({esc(place(i))}): {esc(i['reason'])}"
            for i in out.fixed
        )
        lines.append(f"- Fixed since the last round: {fixed}")
    if out.unchecked:
        unchecked = id_list(out.unchecked)
        lines.append(
            f"- Not checked this round (lens not run or not covered, or file not read): {unchecked}"
        )
    if out.unmentioned:
        lines.append(f"- Kept open, no finder listed them as fixed: {id_list(out.unmentioned)}")
    rules = [
        f"{esc(r['path'])} (blob {r['blob'][:12]}"
        + (f", link to {esc(r['target'])}" if r.get("target") else "")
        + ")"
        for r in plan["rules"]
    ]
    lines.append(f"- Repo rules read: {', '.join(rules) if rules else 'none'}")
    sec = plan["security"]["overrides"]
    if plan["review_md"]["present"] and sec["text"] != "none":
        drops = ", ".join(d["id"] for d in sec["drops"]) or "none"
        lines.append(
            f"- Security overrides: dropped {drops}; {len(sec['precedents'])} repo precedents; "
            f"categories: {esc(', '.join(sec['categories'])) or 'none'}"
        )
    if plan["settings"]["security"]["ignored"]:
        ignored = plan["settings"]["security"]["ignored"]
        lines.append(f"- Ignored lines: {esc('; '.join(ignored))}")
    gaps = []
    if plan["changed"]["skipped"]:
        gaps.append("skipped paths: " + esc(", ".join(plan["changed"]["skipped"])))
    binary = [b["path"] for b in plan["changed"].get("binary", [])]
    if binary:
        gaps.append("binary files not reviewed: " + esc(", ".join(binary)))
    if data.get("unread"):
        gaps.append("files not read: " + esc(", ".join(data["unread"])))
    gaps += [esc(n) for n in data.get("not_covered", [])]
    if data.get("independent") is False:
        gaps.append("checks were not independent")
    if gaps:
        lines.append(f"- Not covered: {'; '.join(gaps)}")
    # Triage warnings explain the run (a shallow clone, a dirty tree), so they are notes,
    # not gaps; the ones that weaken the review also add an approval reason.
    if plan["warnings"]:
        lines.append(f"- Notes: {esc('; '.join(plan['warnings']))}")
    if warnings:
        lines.append(f"- Report warnings: {esc('; '.join(warnings))}")
    lines.append(f"- Round: {plan['round']}")
    return lines + ["", "</details>"]


def render(plan: dict, data: dict, rows: list[dict], out: Outcome, warnings) -> str:
    lines = [out.first or nothing_line(data, out), ""]
    for sev, title in (("blocking", "Blocking"), ("major", "Major"), ("minor", "Minor")):
        group = [r for r in out.listed if r["severity"] == sev]
        if group:
            lines += [f"### {title}", ""]
            for row in group:
                lines += item_block(row)
    if out.still_open:
        lines += ["### Still open from earlier rounds", ""]
        lines += [f"- [{i['id']}] {esc(i['title'])} ({esc(place(i))})" for i in out.still_open]
        lines.append("")
    if out.pre_existing:
        lines += ["### Pre-existing (not counted)", ""]
        for row in out.pre_existing:
            lines += item_block(row)
    lines += fold(plan, data, rows, out, warnings)
    return "\n".join(lines) + "\n"


def nothing_line(data: dict, out: Outcome) -> str:
    if not data["candidates"]:
        return "**Nothing to fix.** No lens reported a finding."
    capped = len(out.by_status.get("minor-cap", []))
    tail = f" {capped} minor findings past the nit cap were not verified." if capped else ""
    if out.pre_existing:
        return (
            "**Nothing to fix.** No finding this change introduces survived verification; "
            f"{len(out.pre_existing)} pre-existing findings are listed, not counted.{tail}"
        )
    if capped:
        return (
            f"**Nothing to fix.** None of the {data['candidates']} candidate findings was "
            f"kept;{tail}"
        )
    return (
        f"**Nothing to fix.** None of the {data['candidates']} candidate findings "
        "survived verification."
    )


def render_empty(plan: dict, warnings: list[str]) -> tuple[str, Outcome]:
    """The report for a run that reviewed nothing: an empty change, hunt or a round cap."""
    b, h = plan["base"], plan["head"]
    open_known = plan.get("open_known", [])
    streak = plan.get("rounds_with_open_findings", ROUND_CAP)
    if plan["empty"] == "change":
        first = f"**No changes between {esc(b['ref'])} and {esc(h['ref'])}.**"
    elif plan["round_cap"]:
        first = f"**Round cap reached:** {len(open_known)} open findings after {streak} rounds"
    else:
        first = f"**No new commits since {esc(plan['since']['ref'])}.**"
    reasons = list(plan["approval"]["reasons"])
    if open_known:
        reasons.append(f"{len(open_known)} earlier findings still open")
    out = Outcome()
    out.approval = {
        "hint": "needs a human" if reasons else plan["approval"]["hint"],
        "reasons": reasons,
    }
    out.first = first
    out.still_open = open_known
    lines = [first, ""]
    if plan["round_cap"] and plan["empty"] != "change":
        lines += [
            f"Findings stayed open for {streak} rounds in a row, so the panel did not review "
            "again. Please reconsider the approach with a human reviewer before the next round.",
            "",
        ]
    if open_known:
        lines += ["### Still open from earlier rounds", ""]
        lines += [f"- [{i['id']}] {esc(i['title'])} ({esc(place(i))})" for i in open_known]
        lines.append("")
    data = {"candidates": 0, "not_covered": [], "independent": None}
    lines += fold(plan, data, [], out, warnings)
    return "\n".join(lines) + "\n", out


# ---------------------------------------------------------------------------
# review.py JSON and the known findings file
# ---------------------------------------------------------------------------


def renamed_blobs(git: Git, a: str, b: str) -> dict[str, tuple[str, str]]:
    """Map each path paired as a rename the way GitLab's MR diff pairs it to (old blob,
    new blob): Gitaly's commit diff passes --find-renames=30%, not git's default 50%."""
    flags = [f for f in DIFF_FLAGS if f != "--no-renames"]
    out = git.run(
        "diff",
        "--raw",
        "-z",
        "--no-abbrev",
        RENAME_SIMILARITY,
        "--diff-filter=R",
        *flags,
        a,
        b,
        "--",
    )
    parts = out.stdout.split(b"\0")
    pairs = {}
    i = 0
    while i + 2 < len(parts):
        fields = parts[i][1:].split(b" ")
        if not parts[i].startswith(b":") or len(fields) < 5:
            raise PanelError("unexpected git diff --raw output")
        pairs[decode(parts[i + 2])] = (fields[2].decode(), fields[3].decode())
        i += 3
    return pairs


def added_lines(plan: dict, files: set[str]) -> set[tuple[str, int]]:
    if not files:
        return set()
    git = Git(plan["repo"])
    a, b = plan["merge_base"], plan["head"]["sha"]
    # GitLab's MR diff pairs renames, and an inline comment must sit on a line that pair
    # adds; diffed without renames, every line of a renamed file would look added.
    renamed = renamed_blobs(git, a, b)
    lines = set()
    for path in sorted(files & renamed.keys()):
        old, new = renamed[path]
        lines |= {(path, num) for _, sign, num, _ in iter_patch(git, old, new) if sign == "+"}
    plain = sorted(files - renamed.keys())
    if plain:
        specs = tuple(f":(literal,top){f}" for f in plain)
        lines |= {
            (path, num)
            for path, sign, num, _ in iter_patch(git, a, b, specs)
            if sign == "+" and path in files
        }
    return lines


# Gitaly's similarity for MR diffs (internal/gitaly/service/diff/commit_diff.go).
RENAME_SIMILARITY = "--find-renames=30%"
GITLAB_SEVERITY = {"blocking": "critical", "major": "major", "minor": "minor"}
# A line whose first non-space character is "/" is a GitLab quick action (/approve,
# /label, /close) that GitLab runs as the poster.
QUICK_ACTION_RE = re.compile(r"(^|[\r\n])([^\S\r\n]*)/")
# "@name" notifies people ("@all" a whole project), and so does "&#64;name", which
# Markdown turns into "@" first; "<!--" can open a fake "<!-- ai-review sha:... -->"
# marker that review.py reads before its own. A zero-width space breaks each of them and
# stays invisible, in code spans too. GitLab's reference pattern is (?<!\w)@ with Ruby's
# ASCII-only \w, applied per rendered text node, so an "@" preceded by anything but an
# ASCII letter or digit notifies (".@all", "x/@all", "a-@all", "é@all" and "_foo_@all"
# included — a closing "_" emphasis is markup, and it leaves the "@" at the start of a
# rendered text node) and gets one; only a mid-word one ("a@b.com", "pkg@1.0",
# "lib/x@2/y.py") must survive verbatim, or commands copied from the comment fail on it.
# The lookahead mirrors GitLab's username start char ([a-zA-Z0-9_.]): "@é" or "@-x" is
# not a username, so breaking it would only corrupt the text. re.ASCII because re.I
# alone also folds [a-zA-Z] onto "İ", "ı", "ſ" and "K" (U+212A), which GitLab's
# ASCII-only \w does not treat as word characters, so those would exempt the "@".
MENTION_RE = re.compile(
    r"(?<![a-zA-Z0-9])@(?=[a-zA-Z0-9_.])|&(?=#0*64;|#x0*40;|commat;)", re.I | re.ASCII
)
ZWSP = "\u200b"


def gitlab_text(text: str) -> str:
    """Neutralize mentions and HTML comments, and escape the "/" that starts a line, so
    no finding text notifies anyone, forges review.py's marker, or runs a quick action."""
    text = MENTION_RE.sub(lambda m: m.group() + ZWSP, text).replace("<!--", "<" + ZWSP + "!--")
    return QUICK_ACTION_RE.sub(lambda m: f"{m.group(1)}{m.group(2)}\\/", text)


def gitlab_comment(row: dict) -> str:
    parts = [f"[{row['id']}] **{row['title']}** ({row['lens']})"]
    if row.get("description") and row["description"].strip() != row["title"].strip():
        parts.append(row["description"])
    parts.append(f"Evidence: {row['evidence']}")
    parts.append(f"Fix: {row['fix']}")
    if row["instances"]:
        parts.append("Also at: " + ", ".join(esc(place(i)) for i in row["instances"]))
    new = row.get("new_evidence")
    if new:
        parts.append("Seen before" + ("" if new is True else f"; new evidence: {new}"))
    if row["status"] == "resolved":
        parts.append(f"Disputed, kept: {row.get('resolution') or 'no reason given'}")
    return gitlab_text("\n\n".join(parts))


def gitlab_json(plan: dict, out: Outcome, first: str) -> dict:
    added = added_lines(plan, {r["file"] for r in out.listed if r["line"]})
    inline = [r for r in out.listed if (r["file"], r["line"]) in added]
    lines = []
    if out.counted:
        head = f"**{len(out.counted)} to fix** ({len(inline)} inline, all listed below)"
        if out.gaps:
            head += "; review incomplete: " + "; ".join(out.gaps)
        lines.append(head)
    else:
        lines.append(first)
    lines.append("")
    # The inline ones too: review.py drops an inline comment on a line it already
    # reviewed, and the finding must still reach the MR.
    for r in out.listed:
        mark = ", inline" if r in inline else ""
        lines.append(
            f"- [{r['id']}] {r['severity']}{mark}: {r['title']} ({esc(place(r))}, {r['lens']})"
        )
    for r in out.hidden_minors:
        lines.append(f"- [{r['id']}] minor, over the nit cap: {r['title']} ({esc(place(r))})")
    for r in out.by_status.get("over-cap", []):
        lines.append(f"- [{r['id']}] not verified (over budget): {r['title']} ({esc(place(r))})")
    for r in out.pre_existing:
        lines.append(f"- [{r['id']}] pre-existing, not counted: {r['title']} ({esc(place(r))})")
    for i in out.still_open:
        lines.append(
            f"- [{i['id']}] still open from an earlier round: {i['title']} ({esc(place(i))})"
        )
    reasons = "; ".join(out.approval["reasons"])
    lines += [
        "",
        f"Approval hint: {out.approval['hint']}" + (f" ({reasons})" if reasons else "") + ".",
        AGENT_NOTE,
    ]
    return {
        "summary": gitlab_text("\n".join(lines).strip() + "\n"),
        "positive_aspects": [],
        "inline_comments": [
            {
                "file": r["file"],
                "line": r["line"],
                "severity": GITLAB_SEVERITY[r["severity"]],
                "comment": gitlab_comment(r),
            }
            for r in inline
        ],
    }


# Statuses of a row whose places join the known item it repeats.
KEPT_KNOWN = ("known", "confirmed", "resolved", "pre-existing")


def merge_instances(item: dict, places: list[dict]) -> None:
    """Add the places the item does not list yet to its instances."""
    have = {(item["file"], item["line"])} | {(p["file"], p["line"]) for p in item["instances"]}
    for p in places:
        if (p["file"], p["line"]) not in have:
            item["instances"].append({"file": p["file"], "line": p["line"]})
            have.add((p["file"], p["line"]))


def next_known(plan: dict, data: dict, rows: list[dict], out: Outcome, known: dict | None) -> dict:
    round_no = plan["round"]
    items = [dict(i, instances=list(i["instances"])) for i in known["findings"]] if known else []
    by_id = {i["id"]: i for i in items}
    # summarize() decided which known items were seen; the rest closed this round.
    for fid in sorted(out.seen):
        if fid in by_id:
            by_id[fid]["last_seen_round"] = round_no
    for row in rows:
        if row["id"] in by_id and row["status"] in KEPT_KNOWN:
            merge_instances(by_id[row["id"]], row["instances"])
    for row in out.counted + out.pre_existing:
        if row["id"] in by_id:
            known_item = by_id[row["id"]]
            known_item["last_seen_round"] = round_no
            if row in out.pre_existing:
                if known_item["status"] != "declined":
                    known_item["status"] = "pre-existing"
            elif known_item["status"] != "declined" or row.get("new_evidence"):
                # Counted again: open, even when declined, since new evidence reopens it.
                # The row's place becomes the primary one; the old one stays an instance.
                old = {"file": known_item["file"], "line": known_item["line"]}
                known_item.update(
                    status="posted",
                    file=row["file"],
                    line=row["line"],
                    severity=row["severity"],
                    title=row["title"],
                )
                merge_instances(known_item, [old] + row["instances"])
                primary = (known_item["file"], known_item["line"])
                known_item["instances"] = [
                    p for p in known_item["instances"] if (p["file"], p["line"]) != primary
                ]
            continue
        item = {
            "id": row["id"],
            "lens": row["lens"],
            "class": row["class"],
            "file": row["file"],
            "line": row["line"],
            "title": row["title"],
            "severity": row["severity"],
            "status": "pre-existing" if row in out.pre_existing else "posted",
            "last_seen_round": round_no,
            "instances": [],
        }
        merge_instances(item, row["instances"])
        items.append(item)
        by_id[row["id"]] = item
    still_open = any(i["status"] == "posted" and i["last_seen_round"] == round_no for i in items)
    previous = known["rounds_with_open_findings"] if known else 0
    if previous >= ROUND_CAP and not open_items(known, known["round"]):
        # Declining the open items lifted the round cap, so the count starts over.
        previous = 0
    return {
        "version": 1,
        "round": round_no,
        "rounds_with_open_findings": previous + 1 if still_open or out.counted else 0,
        "findings": items,
    }


def write_json(path: str, data: dict) -> None:
    try:
        Path(path).write_text(json.dumps(data, ensure_ascii=True, indent=2) + "\n", "utf-8")
    except OSError as exc:
        raise PanelError(f"cannot write {path}: {exc.strerror}") from None


def status_line(row: dict) -> str:
    cell = row["status"]
    if row.get("missing") and cell not in ("resolved", "dismissed", "rejected"):
        cell += f" (missing {', '.join(row['missing'])})"
    title = esc(row["title"].replace("\t", " ").replace("\n", " "))
    return f"{row['id']}\t{cell}\t{row['lens']}\t{esc(place(row))}\t{title}"


def load_plan(path: str) -> dict:
    plan = load_json(path, "plan file")
    if not isinstance(plan, dict) or plan.get("version") != PLAN_VERSION:
        raise PanelError(f"plan file {path}: not a panel.py triage plan (version {PLAN_VERSION})")
    for key in ("base", "head", "merge_base", "tier", "approval", "lenses", "review_md", "limits"):
        if key not in plan:
            raise PanelError(f"plan file {path}: missing {key}")
    return plan


def cmd_report(args: argparse.Namespace) -> int:
    plan = load_plan(args.plan)
    warnings: list[str] = []
    # The known file is the one triage read, so its round cap and ids match this plan.
    known = read_known(plan["known_file"]) if plan.get("known_file") else None
    if known is not None and known["round"] >= plan["round"]:
        raise PanelError(
            f"known findings file {plan['known_file']} is at round {known['round']}, not "
            f"before this plan's round {plan['round']}: a report already wrote it for this "
            "round; run triage again"
        )

    if plan.get("empty") or plan.get("round_cap"):
        # Nothing was reviewed, so the findings file only adds a warning.
        try:
            rows = load_findings(args.findings, plan, warnings)["rows"]
        except PanelError as exc:
            warnings.append(f"{exc}; ignored, this run had nothing to review")
            rows = []
        if rows:
            warnings.append("findings ignored: this run had nothing to review")
        text, out = render_empty(plan, warnings)
        if args.status:
            return 0
        if args.gitlab_json:
            write_json(args.gitlab_json, gitlab_json(plan, out, out.first))
        if args.known_out:
            unchanged = known or {
                "version": 1,
                "round": 0,
                "rounds_with_open_findings": 0,
                "findings": [],
            }
            write_json(args.known_out, unchanged)
        sys.stdout.write(text)
        return 0

    data = load_findings(args.findings, plan, warnings)
    rows = data["rows"]
    warn_missing_at_head(plan, rows, warnings)
    assign_ids(rows, known, warnings)
    mark_statuses(rows, known, plan["limits"], warnings)
    if args.verdicts or not args.status:
        verdicts, resolutions = (
            load_verdicts(args.verdicts, warnings) if args.verdicts else ({}, {})
        )
        apply_verdicts(rows, plan, verdicts, resolutions, warnings)
    if args.status:
        for row in sorted(rows, key=rank_key):
            sys.stdout.write(status_line(row) + "\n")
        for w in warnings:
            sys.stdout.write(f"warning: {esc(w)}\n")
        return 0
    dups = sorted({r["id"] for r in rows if r["status"] == "duplicate"})
    if dups:
        raise PanelError(f"duplicate ids after the merge: {', '.join(dups)}")
    open_ids = sorted(r["id"] for r in rows if r["status"] in ("pending", "disputed"))
    if open_ids:
        raise PanelError(f"disputed ids without a resolution: {', '.join(open_ids)}", 4)
    out = summarize(plan, data, rows, known, warnings)
    text = render(plan, data, rows, out, warnings)
    if args.gitlab_json:
        write_json(args.gitlab_json, gitlab_json(plan, out, out.first or nothing_line(data, out)))
    if args.known_out:
        write_json(args.known_out, next_known(plan, data, rows, out, known))
    sys.stdout.write(text)
    return 0


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="panel.py", description=__doc__.split("\n\n")[0])
    sub = parser.add_subparsers(dest="command", required=True)
    t = sub.add_parser("triage", help="print the review plan")
    t.add_argument("--repo", default=".", help="a directory inside the repository")
    t.add_argument("--base", help="base ref (default: CI variables, then origin/HEAD)")
    t.add_argument("--head", help="head ref (default: CI_COMMIT_SHA when set, else HEAD)")
    t.add_argument("--since", help="hunt only in commits after this ref")
    t.add_argument("--round", type=int, help="round number (default: known round + 1)")
    t.add_argument("--known", help="known findings file from the previous round")
    t.add_argument("--wave", type=int, default=3, help="subagents per wave (default: 3)")
    t.add_argument("--format", choices=("json", "text"), default="json")
    # No prefix matching: an old "--known" must fail, never pass as --known-out.
    r = sub.add_parser("report", help="vote, rank and print the report", allow_abbrev=False)
    r.add_argument("--plan", required=True, help="triage output")
    r.add_argument("--findings", required=True, help="merged findings file")
    r.add_argument("--verdicts", help="skeptic verdicts, one JSON object per line")
    r.add_argument("--known-out", help="write the next known findings file here")
    r.add_argument("--gitlab-json", help="also write gitlab-code-review JSON here")
    r.add_argument("--status", action="store_true", help="print one status line per finding")
    return parser


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    try:
        if args.command == "triage":
            return cmd_triage(args)
        return cmd_report(args)
    except PanelError as exc:
        sys.stderr.write(f"panel.py: {esc(str(exc))}\n")
        return exc.code


if __name__ == "__main__":
    sys.exit(main())
