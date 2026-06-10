# Agent Session Manager MVP Design

## Goal

Build a local-first Agent Session Manager (ASM) that records and indexes agent sessions so a user can later identify which agent session to resume without opening the agent first.

## Product Definition

- ASM is an independent CLI application and SQLite registry.
- Agent-specific integrations are adapters layered on top of ASM Core.
- For MVP, `session = task`.
- MVP integrates with Codex first.
- ASM does not resume sessions itself. It identifies the correct session and surfaces the native agent resume command.

## User Problem

The user runs multiple agent sessions on one Mac across:

- multiple terminal windows
- multiple projects
- multiple branches
- multiple agents

After some time, the user often forgets which agent session corresponds to the work they want to continue. Existing agent history is too local and too weakly indexed for quick recovery.

## MVP Scope

### Included

- local SQLite registry in `~/.asm/registry.db`
- session metadata capture
- structured checkpoints written by the agent
- global session listing
- current-context session recommendation
- Codex integration via hooks
- non-destructive setup via `asm init`

### Excluded

- multi-agent support beyond Codex
- task grouping across sessions
- semantic search / embeddings
- web dashboard
- archive / export
- automatic agent-side summary generation

## Core Concepts

### Session

A session is the primary entity in ASM. In MVP, each session is treated as one task.

### Checkpoint

A checkpoint is a structured progress record attached to a session.

### Agent Session Reference

Each ASM session stores the agent's native session identifier so the user can return to that agent's native resume flow.

For Codex:

- `agent = "codex"`
- `agent_session_ref = Codex SESSION_ID`
- `resume_command = codex resume <SESSION_ID>`

## Data Model

### sessions

- `id TEXT PRIMARY KEY`
- `agent TEXT NOT NULL`
- `agent_session_ref TEXT NOT NULL`
- `resume_command TEXT NOT NULL`
- `title TEXT`
- `goal TEXT`
- `latest_summary TEXT`
- `path TEXT NOT NULL`
- `git_root TEXT`
- `project TEXT`
- `branch TEXT`
- `hostname TEXT`
- `tmux_session TEXT`
- `status TEXT NOT NULL CHECK(status IN ('active', 'stopped'))`
- `started_at TEXT NOT NULL`
- `ended_at TEXT`
- `updated_at TEXT NOT NULL`

Constraints:

- `UNIQUE(agent, agent_session_ref)`

### checkpoints

- `id TEXT PRIMARY KEY`
- `session_id TEXT NOT NULL REFERENCES sessions(id) ON DELETE CASCADE`
- `kind TEXT NOT NULL`
- `title TEXT`
- `goal TEXT`
- `summary TEXT NOT NULL`
- `completed_json TEXT NOT NULL`
- `blockers_json TEXT NOT NULL`
- `next_actions_json TEXT NOT NULL`
- `created_at TEXT NOT NULL`

## Command Surface

### `asm init`

Responsibilities:

- create ASM home directories
- initialize SQLite schema
- install Codex hook configuration
- merge into existing Codex config non-destructively

### `asm start`

Standard path:

- triggered automatically by Codex integration

Responsibilities:

- create or upsert a session for a Codex `SESSION_ID`
- capture:
  - current path
  - git root
  - current branch
  - hostname
  - tmux session
  - timestamps

Behavior:

- `title` and `goal` may be empty initially
- one Codex `SESSION_ID` maps to one ASM session

### `asm checkpoint`

Responsibilities:

- append a structured checkpoint to a known session
- update:
  - `title` if provided
  - `goal` if provided
  - `latest_summary`
  - `updated_at`

Rules:

- session target must be explicit
- no fuzzy session inference
- the first effective checkpoint must include `title` and `goal`

### `asm stop`

Standard path:

- triggered automatically by Codex integration

Responsibilities:

- mark session as `stopped`
- optionally write a final checkpoint
- set `ended_at`
- update `updated_at`

### `asm ls`

Purpose:

- global session view

Behavior:

- list sessions globally
- default to the most recently updated 20 sessions
- do not depend on the current directory
- show `resume_command` directly
- show `[untitled]` when no title has been captured yet

### `asm doctor`

Purpose:

- recommend the most relevant sessions for the current shell context

Inputs:

- current path
- git root
- branch

Behavior:

- use explainable rule-based ranking
- surface the top matches and why they matched
- show `resume_command`

## Checkpoint Protocol

The agent is the source of semantic summaries. ASM stores the payload without rewriting it.

First effective checkpoint payload:

```json
{
  "title": "Multi-image Embedding",
  "goal": "Implement the multi-image fusion baseline and validate recall on MMEB.",
  "summary": "MMEB baseline is running and the fusion baseline is next.",
  "completed": [],
  "blockers": [],
  "next_actions": []
}
```

Rules:

- first checkpoint must include `title` and `goal`
- later checkpoints may omit `title` and `goal`
- `summary`, `completed`, `blockers`, and `next_actions` are always written

## Codex Integration

### Integration Shape

- Codex hooks automatically call `asm start` and `asm stop`
- Codex hook events are handled through a dedicated `asm codex-hook` command that reads the official JSON payload from `stdin`
- checkpoint writing stays semi-manual in MVP

### Hook Strategy

`asm init` installs ASM-owned commands into Codex `hooks.json` without overwriting unrelated user hooks.

Installation rules:

- merge existing config by default
- allow `--force` to overwrite ASM-owned entries
- keep other user entries intact
- use Codex's official JSON payload fields such as `session_id`, `cwd`, and `hook_event_name`

### Resume Strategy

ASM never replays Codex context itself.

Instead it stores and displays:

- `agent_session_ref`
- `resume_command`

The user resumes via native Codex command:

```bash
codex resume <SESSION_ID>
```

## Ranking Rules For `asm doctor`

Initial scoring weights:

- same path: high weight
- same git root: medium weight
- same branch: medium weight
- active session: small bonus
- recent update: recency bonus

The result should be explainable. Example reasons:

- same working directory
- same git root
- same branch
- updated recently

## UX Rules

- everyday work stays inside Codex
- ASM should be mostly invisible during normal usage
- user-facing commands used most often:
  - `asm ls`
  - `asm doctor`

## Implementation Constraints

- use only local storage
- no network dependency
- prefer standard library and zero-dependency setup
- support alternate homes via env vars so tests can run in temp directories
- environment-provided homes should be treated carefully; prefer absolute paths for stable hook installation

## Success Criteria

MVP is successful when a user can:

1. run `asm init`
2. use Codex normally
3. run `asm ls` and see recent sessions globally
4. run `asm doctor` in a project directory and get useful recommended sessions
5. copy the shown `codex resume <SESSION_ID>` command and continue the correct session
