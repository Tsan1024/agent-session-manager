# Agent Session Manager

Agent Session Manager (ASM) is a local-first registry for agent sessions. It records Codex session metadata, stores structured checkpoints, and helps identify the correct session to resume later.

## Installation

Create a local virtual environment and install ASM in editable mode:

```bash
python3 -m venv .venv
.venv/bin/python -m pip install -e .
```

Then use the installed entrypoint:

```bash
.venv/bin/asm ls
```

## MVP Commands

- `asm init`
- `asm start`
- `asm checkpoint`
- `asm checkpoint-current`
- `asm stop`
- `asm stop-current`
- `asm ls`
- `asm doctor`
- `asm current`
- `asm checkpoint-template`
- `asm checkpoint-prompt`

## Development

Run tests:

```bash
python3 -m unittest tests.test_cli
```

Run the CLI from the repository root:

```bash
PYTHONPATH=src python3 -m asm.cli init
```

## Recommended Codex Checkpoint Flow

Find the current best-matching session for this workspace:

```bash
./asm current
```

Print an empty checkpoint template:

```bash
./asm checkpoint-template
```

Print a Codex-ready prompt that asks for JSON only:

```bash
./asm checkpoint-prompt
```

Write a checkpoint from a JSON file:

```bash
./asm checkpoint --session <asm_session_id> --payload-file checkpoint.json
```

Write a checkpoint from stdin:

```bash
printf '%s' '{"title":"Demo","goal":"Capture progress","summary":"checkpoint stored","completed":[],"blockers":[],"next_actions":["run doctor"]}' \
  | ./asm checkpoint --session <asm_session_id>
```

Write a checkpoint to the best-matching current session without passing the session id:

```bash
printf '%s' '{"title":"Demo","goal":"Capture progress","summary":"checkpoint stored","completed":[],"blockers":[],"next_actions":["run doctor"]}' \
  | ./asm checkpoint-current
```

Write a final checkpoint and stop from stdin:

```bash
printf '%s' '{"summary":"final summary","completed":[],"blockers":[],"next_actions":["resume later"]}' \
  | ./asm stop --session <asm_session_id>
```

Stop the best-matching current session without passing the session id:

```bash
printf '%s' '{"summary":"final summary","completed":[],"blockers":[],"next_actions":["resume later"]}' \
  | ./asm stop-current
```

You can also stop the current session without a final payload:

```bash
./asm stop-current
```

Suggested Codex-side interaction:

1. Run `./asm checkpoint-prompt`.
2. Paste that prompt into Codex.
3. Copy the JSON-only response into a pipe or file.
4. Run `./asm current` to confirm the target session if needed.
5. Prefer `./asm checkpoint-current` for normal progress updates.
6. Prefer `./asm stop-current` for normal session shutdown.
7. Use `./asm checkpoint --session ...` or `./asm stop --session ...` when you need explicit control.

## Listing Sessions

List recent sessions globally:

```bash
./asm ls
```

Filter by status, branch, or agent:

```bash
./asm ls --status active
./asm ls --status stopped
./asm ls --branch feature/my-branch
./asm ls --project my-project
./asm ls --agent codex
```

Show goals in long output:

```bash
./asm ls --long
```

Explain why `doctor` recommended a session:

```bash
./asm doctor --explain
```

## Notes

- `ASM_HOME` overrides `~/.asm`
- `CODEX_HOME` overrides `~/.codex`
- relative `ASM_HOME` and `CODEX_HOME` values are resolved from the current working directory
- Codex integration is initialized through `asm init`
- Codex hooks call `asm codex-hook` and pass official hook JSON on `stdin`
