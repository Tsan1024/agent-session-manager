# Agent Session Manager

Agent Session Manager (ASM) is a local-first registry for agent sessions. It records Codex session metadata, stores structured checkpoints, and helps identify the correct session to resume later.

## Why ASM Exists

Modern agent workflows are fragmented across multiple terminals, projects, branches, and tools. A few hours later, you often remember the work itself, but not which agent session it belonged to.

Native agent history is usually local to one tool and too weak for global recovery. ASM exists to solve that recovery problem with a tool-agnostic, local-first registry:

- it records the minimal metadata needed to find the right session again
- it keeps optional structured summaries when the agent can provide them
- it does not replace native agent resume flows; it points you back to them

The design goal is simple: when you have several Codex, Claude, or other agent sessions in flight, ASM should help you answer "which session should I resume?" without reopening every agent first.

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
- `asm finalize`
- `asm finalize-current`
- `asm stop`
- `asm stop-current`
- `asm ls`
- `asm search`
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

Explain why that session was selected:

```bash
./asm current --explain
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

Write a final agent checkpoint and stop from stdin:

```bash
printf '%s' '{"summary":"final summary","completed":[],"blockers":[],"next_actions":["resume later"]}' \
  | ./asm finalize --session <asm_session_id>
```

Write a final agent checkpoint to the best-matching current session and stop it:

```bash
printf '%s' '{"summary":"final summary","completed":[],"blockers":[],"next_actions":["resume later"]}' \
  | ./asm finalize-current
```

`./asm finalize-current` writes the final structured summary into ASM and stops the ASM session record. It does not exit the Codex session itself.

You can also stop the current session without writing a final checkpoint:

```bash
./asm stop-current
```

`./asm stop-current` does not accept a final payload. Use `./asm finalize-current` when you want to write a final structured summary.

Suggested Codex-side interaction:

1. Run `./asm checkpoint-prompt`.
2. Paste that prompt into Codex.
3. Copy the JSON-only response into a pipe or file.
4. Run `./asm current` to confirm the target session if needed.
5. Prefer `./asm checkpoint-current` for normal progress updates.
6. Prefer `./asm finalize-current` for normal session shutdown with a high-quality agent summary.
7. Use `./asm stop-current` only when you want to stop without writing a structured final checkpoint.
8. Use `./asm checkpoint --session ...`, `./asm finalize --session ...`, or `./asm stop --session ...` when you need explicit control.
9. Exit the agent session separately, for example with `/quit`.

Codex Stop hook behavior:

- if a session does not already have an agent-authored final checkpoint, the Codex `Stop` hook will write a fallback final checkpoint using a condensed form of `last_assistant_message`
- if an agent-authored final checkpoint already exists, the `Stop` hook will not overwrite it
- fallback summaries are a safety net only; prefer explicit `finalize-current`

Codex prompt integration:

- `asm init` installs prompt files into `~/.codex/prompts/`
- use them inside Codex to generate JSON more naturally:
  - `asm-checkpoint`
  - `asm-final`
  - `asm-finish`
- `asm-finish` is the recommended one-step end-of-session flow inside Codex
- `asm-finish` writes the final checkpoint into ASM, but does not exit the agent session
- `asm-final` is an end-of-session prompt only; it should be invoked explicitly when you want to record a final structured summary
- `asm-final` does not exit the agent session
- normal Codex Q&A is unchanged; ASM does not inject this prompt into ordinary turns
- if you do not invoke `asm-final`, ASM still keeps the base session record and resume command; you just will not get a high-quality agent-authored final summary
- if the task was never clarified, the session may remain `[untitled]`; this is intentional and better than inventing an incorrect goal
- for unclear tasks, ASM also stores the first user prompt as a hint and may show that hint in listings instead of `[untitled]`
- listings also suppress obviously noisy historical assistant-style titles/goals and prefer a hint or `[untitled]` for display

Recommended Codex flow after `asm init`:

1. In Codex, invoke the installed checkpoint prompt.
2. Let Codex output JSON only.
3. Pipe that JSON to `./asm checkpoint-current`.
4. At the end, prefer invoking `asm-finish`.
5. Let Codex write the final checkpoint into ASM for you.
6. Exit Codex separately with `/quit` if you want to end the agent session.

Fallback manual end-of-session flow:

1. Invoke `asm-final`.
2. Let Codex output JSON only.
3. Pipe that JSON to `./asm finalize-current`.
4. Exit Codex separately with `/quit` if you want to end the agent session.

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

Search by free-text keyword:

```bash
./asm search "插件性能"
./asm search "old_idp"
./asm search "orchestrator" --project my-project --branch feature/test
./asm search "插件性能" --explain
```

Show goals in long output:

```bash
./asm ls --long
```

Notes:

- default `asm ls` is intentionally compact for fast scanning
- `asm ls --long` may truncate long `goal` and `last` fields for readability
- listings use absolute timestamps in `YYYY-MM-DD HH:MM` format

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
