#!/usr/bin/env python3
"""Create a submission zip from committed, tracked files only.

Run after committing the final README/docs changes:

    python scripts/package_submission.py

The archive is intentionally built with ``git archive`` rather than a recursive zip. That
means ignored local files such as .env, virtual environments, caches, and logs cannot be
included accidentally. The script refuses a dirty tracked worktree, so the archive always
matches a reviewable commit.
"""
from __future__ import annotations

import argparse
import subprocess
import sys
import zipfile
from pathlib import Path, PurePosixPath


REPO_ROOT = Path(__file__).resolve().parent.parent
DEFAULT_OUTPUT = REPO_ROOT / "dist" / "avis-servicing-agent-submission.zip"


def _run_git(*args: str) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        ["git", *args], cwd=REPO_ROOT, text=True, stdout=subprocess.PIPE,
        stderr=subprocess.PIPE, check=False,
    )


def ensure_clean_tracked_tree() -> None:
    result = _run_git("status", "--porcelain", "--untracked-files=no")
    if result.returncode:
        raise RuntimeError(result.stderr.strip() or "could not inspect Git status")
    if result.stdout.strip():
        raise RuntimeError(
            "tracked files are uncommitted; commit or stash them before packaging so the "
            "archive matches the reviewed final revision"
        )


def forbidden_members(names: list[str]) -> list[str]:
    """Return archive members prohibited by the brief or secret hygiene policy."""
    forbidden: list[str] = []
    blocked_parts = {".venv", ".venv-test", "venv", "__pycache__", ".git", "logs"}
    for name in names:
        path = PurePosixPath(name)
        if any(part in blocked_parts for part in path.parts):
            forbidden.append(name)
        elif path.name == ".env" or path.suffix in {".pyc", ".pyo"}:
            forbidden.append(name)
    return forbidden


def build_archive(output: Path) -> None:
    output.parent.mkdir(parents=True, exist_ok=True)
    result = _run_git("archive", "--format=zip", f"--output={output}", "HEAD")
    if result.returncode:
        raise RuntimeError(result.stderr.strip() or "git archive failed")

    with zipfile.ZipFile(output) as archive:
        blocked = forbidden_members(archive.namelist())
    if blocked:
        output.unlink(missing_ok=True)
        raise RuntimeError(f"refusing archive with prohibited member(s): {', '.join(blocked)}")


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--output", type=Path, default=DEFAULT_OUTPUT,
        help=f"archive destination (default: {DEFAULT_OUTPUT.relative_to(REPO_ROOT)})",
    )
    args = parser.parse_args(argv)

    try:
        ensure_clean_tracked_tree()
        output = args.output.resolve()
        build_archive(output)
    except RuntimeError as exc:
        print(f"Packaging aborted: {exc}", file=sys.stderr)
        return 2

    print(f"Submission archive created: {output}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
