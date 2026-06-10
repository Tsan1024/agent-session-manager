from __future__ import annotations

import json
from pathlib import Path

from .config import AppPaths


def asm_hook_definition(command: str) -> dict[str, object]:
    return {
        "hooks": {
            "SessionStart": [
                {
                    "matcher": "startup|resume|clear|compact",
                    "hooks": [
                        {
                            "type": "command",
                            "command": command,
                            "statusMessage": "ASM recording session start",
                        }
                    ],
                }
            ],
            "UserPromptSubmit": [
                {
                    "hooks": [
                        {
                            "type": "command",
                            "command": command,
                            "statusMessage": "ASM updating session activity",
                        }
                    ],
                }
            ],
            "Stop": [
                {
                    "hooks": [
                        {
                            "type": "command",
                            "command": command,
                            "statusMessage": "ASM recording session stop",
                        }
                    ],
                }
            ],
        }
    }


def install_codex_hooks(paths: AppPaths, force: bool = False) -> None:
    paths.codex_home.mkdir(parents=True, exist_ok=True)
    command = str(paths.codex_hook_runner)
    _install_hook_runner(paths.codex_hook_runner)
    _install_prompt_files(paths)

    if paths.codex_hooks.exists():
        raw = json.loads(paths.codex_hooks.read_text() or "{}")
    else:
        raw = {}

    hooks = raw.setdefault("hooks", {})
    desired = asm_hook_definition(command)["hooks"]
    for hook_name, groups in desired.items():
        existing_groups = hooks.setdefault(hook_name, [])
        if not isinstance(existing_groups, list):
            if not force:
                continue
            existing_groups = []
            hooks[hook_name] = existing_groups
        for group in groups:
            if group not in existing_groups:
                existing_groups.append(group)

    paths.codex_hooks.write_text(json.dumps(raw, indent=2, sort_keys=True) + "\n")


def _install_hook_runner(target: Path) -> None:
    target.parent.mkdir(parents=True, exist_ok=True)
    repo_root = Path(__file__).resolve().parents[2]
    script = "\n".join(
        [
            "#!/bin/sh",
            f'PYTHONPATH="{repo_root / "src"}${{PYTHONPATH:+:$PYTHONPATH}}" exec python3 -m asm.cli codex-hook "$@"',
            "",
        ]
    )
    target.write_text(script)
    target.chmod(0o755)


def _install_prompt_files(paths: AppPaths) -> None:
    paths.codex_prompts_dir.mkdir(parents=True, exist_ok=True)
    checkpoint_prompt = "\n".join(
        [
            "---",
            "description: Generate ASM checkpoint JSON",
            "---",
            "Please output only JSON that matches this ASM checkpoint schema.",
            "Do not add markdown fences, prose, or commentary.",
            "",
            "{",
            '  "title": "",',
            '  "goal": "",',
            '  "summary": "",',
            '  "completed": [],',
            '  "blockers": [],',
            '  "next_actions": []',
            "}",
            "",
            "Keep title short. Keep summary to one sentence.",
        ]
    )
    final_prompt = "\n".join(
        [
            "---",
            "description: Generate ASM final checkpoint JSON",
            "---",
            "Please output only JSON for a final ASM checkpoint.",
            "Do not add markdown fences, prose, or commentary.",
            "",
            "{",
            '  "summary": "",',
            '  "completed": [],',
            '  "blockers": [],',
            '  "next_actions": []',
            "}",
            "",
            "Use one sentence for summary.",
        ]
    )
    (paths.codex_prompts_dir / "asm-checkpoint.md").write_text(checkpoint_prompt + "\n")
    (paths.codex_prompts_dir / "asm-final.md").write_text(final_prompt + "\n")
