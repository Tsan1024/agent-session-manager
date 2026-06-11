# Agent Session Manager

Agent Session Manager (ASM) is a local-first registry for agent sessions. It records Codex session metadata, stores structured checkpoints, and helps identify the correct session to resume later.

ASM is already adapted for Codex as its first supported agent runtime:

- `asm init` installs Codex hooks and prompt files
- imported Codex sessions use native `codex resume <session_id>` commands
- ASM can auto-sync from native Codex transcript files under `~/.codex/sessions`
- `checkpoint-current`, `finalize-current`, and `stop-current` can operate against the current Codex session

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

If you want a global `asm` command on your `PATH`, add a small wrapper under a user bin directory that already exists in `PATH`, for example `~/.local/bin/asm`, and point it at the ASM virtual environment:

```bash
#!/bin/sh
exec /path/to/agent-session-manager/.venv/bin/python -m asm.cli "$@"
```

ASM runtime data is global by default:

- registry database: `~/.asm/registry.db`
- Codex prompts/hooks: `~/.codex/`
- CLI wrapper: any directory on your shell `PATH`, for example `~/.local/bin`

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
- `asm import-codex`
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

Backfill sessions directly from native Codex transcript files:

```bash
./asm import-codex
./asm import-codex --limit 50
./asm import-codex --codex-home ~/.codex
```

For Codex-focused workflows, query commands also perform a conservative automatic transcript sync before reading ASM results:

```bash
./asm ls
./asm search "importer"
./asm current --explain
./asm doctor
```

Write-through commands also auto-sync before selecting the current Codex session:

```bash
./asm checkpoint-current
./asm finalize-current
./asm stop-current
```

Filter by status, branch, or agent:

```bash
./asm ls --status active
./asm ls --status stopped
./asm ls --branch feature/my-branch
./asm ls --project my-project
./asm ls --agent codex
```

Choose how `scope` is displayed:

```bash
./asm ls --scope project
./asm ls --scope path
./asm current --scope path
./asm doctor --scope path
./asm search "knowledge_preprocess" --scope path
```

Examples:

- `--scope project`: `knowledge_preprocess @ fix/doc_line_break`
- `--scope path`: `/Users/bytedance/Data/Code/PythonProject/knowledge_preprocess @ fix/doc_line_break`

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
- `asm search` is keyword contains-match across `title`, `initial_prompt`, `goal`, `latest_summary`, `project`, `branch`, and `path`

## Codex Alignment Without Full Hook Dependence

ASM can now import native Codex session files from `~/.codex/sessions/**/*.jsonl`.

What the importer can reliably recover:

- Codex session id
- working directory (`cwd`)
- session start timestamp
- latest transcript timestamp
- first user message as `initial_prompt`
- last assistant message as a low-priority fallback summary

What the importer should not be trusted to recover as a stable interface:

- git branch at session start
- explicit lifecycle events equivalent to official hooks
- stable transcript semantics across Codex releases

Because Codex documents transcript paths as convenience data rather than a stable hook interface, the recommended model is hybrid:

- use hooks for stable realtime capture
- use `asm import-codex` for backfill, reconciliation, and lower-touch auto-collection

Current practical behavior:

- `asm init` still installs Codex hooks by default
- hooks are no longer required for ASM to be useful with Codex
- if hooks are missing, ASM can still auto-sync from native Codex transcripts and support `ls`, `search`, `current`, `doctor`, `checkpoint-current`, `finalize-current`, and `stop-current`
- hooks remain the higher-quality path for realtime lifecycle capture and structured summaries

Import behavior is conservative by design:

- imported sessions use the native Codex session id as `agent_session_ref`
- imported sessions get `resume_command = codex resume <session_id>`
- imported metadata fills missing ASM fields only
- imported transcript summaries do not overwrite agent-authored checkpoint summaries
- imported session provenance is stored via transcript path metadata, not by pretending imports are agent checkpoints
- `ls`, `search`, `current`, `doctor`, `checkpoint-current`, `finalize-current`, and `stop-current` auto-sync recent Codex transcript files before matching sessions
- if the imported transcript session id matches the current `CODEX_THREAD_ID`, ASM marks that imported session as `active` and aligns it with the current shell context
- imported historical sessions now also attempt to backfill `branch` from the transcript working directory when that directory is still a live git worktree
- this means `checkpoint-current` and `finalize-current` can operate on the current Codex session even when no Codex hooks were installed, as long as the native transcript exists
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
