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
            "description: Generate ASM final checkpoint JSON for finalize-current",
            "---",
            "Generate the final structured checkpoint for ASM session recording.",
            "This prompt is only for end-of-session recording and should not affect normal Q&A.",
            "Please output only JSON for `asm finalize-current`.",
            "Do not add markdown fences, prose, or commentary.",
            "Do not include greetings, options, or follow-up suggestions.",
            "Prefer factual completion/status statements over advice.",
            "If the task is not fully complete, state the current status honestly.",
            "",
            "{",
            '  "summary": "",',
            '  "completed": [],',
            '  "blockers": [],',
            '  "next_actions": []',
            "}",
            "",
            "Requirements:",
            "- `summary`: one sentence stating what was completed or the current conclusion/status.",
            "- `completed`: concrete finished work only.",
            "- `blockers`: real blockers only; otherwise use an empty list.",
            "- `next_actions`: specific next steps only; otherwise use an empty list.",
        ]
    )
    finish_prompt = "\n".join(
        [
            "---",
            "description: Generate a final ASM checkpoint and write it with asm finalize-current",
            "---",
            "You are finishing the current Codex task for ASM recording.",
            "Do not change the user's code or continue solving the task.",
            "Do not exit the agent session.",
            "",
            "Your job in this turn is:",
            "1. Infer the best final ASM checkpoint JSON from the current conversation and repository state.",
            "2. Write that JSON to a temporary file.",
            "3. Run `asm finalize-current --payload-file <that file>`.",
            "4. Remove the temporary file if the write succeeded.",
            "5. Reply with one short confirmation that ASM was finalized, and remind the user they can `/quit` separately if they want to exit Codex.",
            "",
            "Hard constraints:",
            "- Do not use `asm stop` or `asm stop-current` for this flow.",
            "- Only use `asm finalize-current --payload-file <file>`.",
            "- If the finalize command fails because ASM storage under `~/.asm` is not writable, request permission and retry that exact finalize command.",
            "- Do not claim success unless `asm finalize-current` actually succeeds.",
            "",
            "JSON requirements:",
            "- Include only: `summary`, `completed`, `blockers`, `next_actions`.",
            "- `summary` must be one sentence stating what was completed or the current conclusion/status.",
            "- `completed` must list concrete finished work only.",
            "- `blockers` must list real blockers only; otherwise use an empty list.",
            "- `next_actions` must list specific next steps only; otherwise use an empty list.",
            "",
            "Do not ask the user follow-up questions in this turn unless the finalize command fails.",
        ]
    )
    (paths.codex_prompts_dir / "asm-checkpoint.md").write_text(checkpoint_prompt + "\n")
    (paths.codex_prompts_dir / "asm-final.md").write_text(final_prompt + "\n")
    (paths.codex_prompts_dir / "asm-finish.md").write_text(finish_prompt + "\n")
