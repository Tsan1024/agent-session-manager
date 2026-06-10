from __future__ import annotations

import os
import socket
import subprocess
from dataclasses import dataclass
from pathlib import Path


@dataclass(frozen=True)
class ShellContext:
    path: str
    git_root: str | None
    project: str | None
    branch: str | None
    hostname: str
    tmux_session: str | None


def current_context(cwd: Path | None = None) -> ShellContext:
    cwd = (cwd or Path.cwd()).resolve()
    git_root = os.environ.get("ASM_TEST_GIT_ROOT") or _run_git(cwd, "rev-parse", "--show-toplevel")
    branch = os.environ.get("ASM_TEST_BRANCH") or _run_git(cwd, "branch", "--show-current")
    project = Path(git_root).name if git_root else cwd.name
    return ShellContext(
        path=str(cwd),
        git_root=git_root,
        project=project,
        branch=branch,
        hostname=socket.gethostname(),
        tmux_session=os.environ.get("TMUX_SESSION") or os.environ.get("TMUX"),
    )


def _run_git(cwd: Path, *args: str) -> str | None:
    try:
        result = subprocess.run(
            ["git", *args],
            cwd=str(cwd),
            capture_output=True,
            text=True,
            check=False,
        )
    except FileNotFoundError:
        return None
    if result.returncode != 0:
        return None
    value = result.stdout.strip()
    return value or None
