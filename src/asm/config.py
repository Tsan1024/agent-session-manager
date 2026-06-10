from __future__ import annotations

import os
from dataclasses import dataclass
from pathlib import Path


@dataclass(frozen=True)
class AppPaths:
    asm_home: Path
    codex_home: Path
    registry_db: Path
    codex_hooks: Path
    asm_bin_dir: Path
    codex_hook_runner: Path


def resolve_paths() -> AppPaths:
    asm_home = Path(os.environ.get("ASM_HOME", Path.home() / ".asm")).expanduser()
    codex_home = Path(os.environ.get("CODEX_HOME", Path.home() / ".codex")).expanduser()
    return AppPaths(
        asm_home=asm_home,
        codex_home=codex_home,
        registry_db=asm_home / "registry.db",
        codex_hooks=codex_home / "hooks.json",
        asm_bin_dir=asm_home / "bin",
        codex_hook_runner=asm_home / "bin" / "asm-codex-hook",
    )
