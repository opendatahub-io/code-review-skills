"""Shared fixtures for the panel.py tests.

Every command runs in a hermetic git environment, and every scratch directory comes from
tempfile and is removed by the test, class or module that made it.
"""

import json
import os
import shutil
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path
from typing import Any

REPO_ROOT = Path(__file__).resolve().parents[1]
PANEL = REPO_ROOT / "skills/code-review-panel/scripts/panel.py"
REVIEW_PY = REPO_ROOT / "skills/gitlab-code-review/scripts/review.py"
HAVE_UV = shutil.which("uv") is not None


def hermetic_env(**overrides: str) -> dict[str, str]:
    """os.environ without git, GitLab CI or GitHub variables, with a fixed git identity."""
    env = {k: v for k, v in os.environ.items() if not k.startswith(("GIT_", "CI_", "GITHUB_"))}
    env.update(
        GIT_CONFIG_GLOBAL="/dev/null",
        GIT_CONFIG_NOSYSTEM="1",
        GIT_AUTHOR_NAME="t",
        GIT_AUTHOR_EMAIL="t@example.com",
        GIT_COMMITTER_NAME="t",
        GIT_COMMITTER_EMAIL="t@example.com",
        PYTHONDONTWRITEBYTECODE="1",
    )
    env.update(overrides)
    return env


def run(
    cwd: str | Path,
    *args: str | bytes | Path,
    env: dict[str, str] | None = None,
    check: bool = True,
    text: bool = False,
) -> subprocess.CompletedProcess:
    proc = subprocess.run(
        list(args),
        cwd=cwd,
        env=hermetic_env() if env is None else env,
        capture_output=True,
        text=text,
    )
    if check and proc.returncode:
        raise AssertionError(f"{args!r} exited {proc.returncode}: {proc.stderr!r}")
    return proc


def git(cwd: str | Path, *args: str | bytes, check: bool = True) -> subprocess.CompletedProcess:
    return run(cwd, "git", *args, check=check)


def rev_parse(cwd: str | Path, ref: str = "HEAD") -> str:
    return git(cwd, "rev-parse", ref).stdout.decode().strip()


def shell(
    cwd: str | Path, command: str, env: dict[str, str] | None = None, check: bool = True
) -> str:
    """Run a command the plan emits, as an agent would, and return its output."""
    return run(cwd, "sh", "-c", command, env=env, check=check).stdout.decode("utf-8", "replace")


def write(repo: str | Path, path: str | bytes, content: str | bytes = b"x\n") -> None:
    full = os.path.join(os.fsencode(repo), os.fsencode(path))
    os.makedirs(os.path.dirname(full), exist_ok=True)
    with open(full, "wb") as f:
        f.write(content if isinstance(content, bytes) else content.encode())


def commit(repo: str | Path, files: dict, msg: str = "change") -> str:
    """Write each file (None deletes it), commit everything, and return the new HEAD."""
    for path, content in files.items():
        if content is None:
            os.remove(os.path.join(os.fsencode(repo), os.fsencode(path)))
        else:
            write(repo, path, content)
    git(repo, "add", "-A")
    git(repo, "commit", "-q", "--allow-empty", "-m", msg)
    return rev_parse(repo)


def make_repo(parent: str, base_files: dict, branch: str | None = None) -> tuple[str, str]:
    """A new repository under parent with base_files committed on main; returns (repo, base)."""
    repo = tempfile.mkdtemp(dir=parent)
    git(repo, "init", "-q", "-b", "main")
    for path, content in base_files.items():
        write(repo, path, content)
    git(repo, "add", "-A")
    git(repo, "commit", "-q", "-m", "base")
    base = rev_parse(repo)
    if branch:
        git(repo, "checkout", "-q", "-b", branch)
    return repo, base


def run_panel(
    cwd: str | Path, *args: str | Path, env: dict[str, str] | None = None, text: bool = False
) -> subprocess.CompletedProcess:
    return run(cwd, sys.executable, PANEL, *args, env=env, check=False, text=text)


def triage_json(cwd: str | Path, *args: str, env: dict[str, str] | None = None) -> dict:
    proc = run_panel(cwd, "triage", *args, env=env)
    if proc.returncode:
        stderr = proc.stderr.decode(errors="replace")
        raise AssertionError(f"triage exited {proc.returncode}: {stderr}")
    return json.loads(proc.stdout)


def review_py_display(path: str) -> subprocess.CompletedProcess:
    """Run gitlab-code-review's review.py directly, so its uv shebang picks the interpreter."""
    return subprocess.run([str(REVIEW_PY), "display", path], capture_output=True, text=True)


def write_json(path: str, data: Any) -> str:
    with open(path, "w", encoding="utf-8") as f:
        json.dump(data, f)
    return path


def load_json(path: str) -> Any:
    with open(path, encoding="utf-8") as f:
        return json.load(f)


def new_tmpdir() -> str:
    return os.path.realpath(tempfile.mkdtemp(prefix="panel-test-"))


class PanelTestCase(unittest.TestCase):
    def make_tmpdir(self) -> str:
        path = new_tmpdir()
        self.addCleanup(shutil.rmtree, path)
        return path

    @classmethod
    def make_class_tmpdir(cls) -> str:
        path = new_tmpdir()
        cls.addClassCleanup(shutil.rmtree, path)
        return path

    def assert_starts_with(self, text: str, prefix: str, limit: int = 600) -> None:
        if not text.startswith(prefix):
            self.fail(f"{text[:limit]!r} does not start with {prefix!r}")
