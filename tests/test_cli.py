import json
import os
import sqlite3
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]


class AsmCliTests(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.home = Path(self.tmp.name) / "asm-home"
        self.codex_home = Path(self.tmp.name) / "codex-home"
        self.workspace = Path(self.tmp.name) / "workspace"
        self.workspace.mkdir()
        self.git_root = self.workspace / "repo"
        self.git_root.mkdir()
        self.env = os.environ.copy()
        self.env["ASM_HOME"] = str(self.home)
        self.env["CODEX_HOME"] = str(self.codex_home)
        self.env["ASM_TEST_GIT_ROOT"] = str(self.git_root)
        self.env["ASM_TEST_BRANCH"] = "feature/test"
        existing_pythonpath = self.env.get("PYTHONPATH")
        source_path = str(ROOT / "src")
        self.env["PYTHONPATH"] = (
            source_path if not existing_pythonpath else os.pathsep.join([source_path, existing_pythonpath])
        )

    def run_cli(self, *args: str, cwd: Path | None = None) -> subprocess.CompletedProcess[str]:
        return subprocess.run(
            [sys.executable, "-m", "asm.cli", *args],
            cwd=str(cwd or self.workspace),
            env=self.env,
            text=True,
            capture_output=True,
            check=False,
        )

    def test_init_creates_registry_and_codex_hook_file(self) -> None:
        result = self.run_cli("init")
        self.assertEqual(result.returncode, 0, result.stderr)

        db_path = self.home / "registry.db"
        self.assertTrue(db_path.exists())
        hooks_path = self.codex_home / "hooks.json"
        self.assertTrue(hooks_path.exists())
        self.assertTrue((self.home / "bin" / "asm-codex-hook").exists())

        with sqlite3.connect(db_path) as conn:
            tables = {
                row[0]
                for row in conn.execute(
                    "SELECT name FROM sqlite_master WHERE type = 'table'"
                )
            }
        self.assertIn("sessions", tables)
        self.assertIn("checkpoints", tables)

        hooks = json.loads(hooks_path.read_text())
        self.assertIn("SessionStart", hooks["hooks"])
        self.assertIn("UserPromptSubmit", hooks["hooks"])
        self.assertIn("Stop", hooks["hooks"])

    def test_start_creates_single_session_for_agent_ref(self) -> None:
        self.run_cli("init")

        first = self.run_cli(
            "start",
            "--agent",
            "codex",
            "--agent-session-ref",
            "sess_123",
            cwd=self.workspace,
        )
        second = self.run_cli(
            "start",
            "--agent",
            "codex",
            "--agent-session-ref",
            "sess_123",
            cwd=self.workspace,
        )

        self.assertEqual(first.returncode, 0, first.stderr)
        self.assertEqual(second.returncode, 0, second.stderr)
        self.assertEqual(first.stdout.strip(), second.stdout.strip())

        with sqlite3.connect(self.home / "registry.db") as conn:
            rows = list(
                conn.execute(
                    "SELECT id, status, branch, project FROM sessions WHERE agent = ? AND agent_session_ref = ?",
                    ("codex", "sess_123"),
                )
            )
        self.assertEqual(len(rows), 1)
        self.assertEqual(rows[0][1], "active")
        self.assertEqual(rows[0][2], "feature/test")
        self.assertEqual(rows[0][3], "repo")

    def test_first_checkpoint_requires_title_and_goal(self) -> None:
        self.run_cli("init")
        start = self.run_cli("start", "--agent", "codex", "--agent-session-ref", "sess_abc")
        session_id = start.stdout.strip()

        payload = json.dumps(
            {
                "summary": "current state",
                "completed": [],
                "blockers": [],
                "next_actions": ["next step"],
            }
        )
        result = self.run_cli("checkpoint", "--session", session_id, "--payload", payload)
        self.assertNotEqual(result.returncode, 0)
        self.assertIn("title and goal", result.stderr)

    def test_checkpoint_updates_session_fields_and_stop_marks_stopped(self) -> None:
        self.run_cli("init")
        start = self.run_cli("start", "--agent", "codex", "--agent-session-ref", "sess_stop")
        session_id = start.stdout.strip()

        payload = json.dumps(
            {
                "title": "Multi-image Embedding",
                "goal": "Implement fusion baseline",
                "summary": "baseline is in progress",
                "completed": ["wired eval harness"],
                "blockers": [],
                "next_actions": ["implement fusion"],
            }
        )
        checkpoint = self.run_cli("checkpoint", "--session", session_id, "--payload", payload)
        self.assertEqual(checkpoint.returncode, 0, checkpoint.stderr)

        stop_payload = json.dumps(
            {
                "summary": "session complete",
                "completed": ["implemented fusion"],
                "blockers": [],
                "next_actions": ["resume in codex"],
            }
        )
        stopped = self.run_cli("stop", "--session", session_id, "--payload", stop_payload)
        self.assertEqual(stopped.returncode, 0, stopped.stderr)

        with sqlite3.connect(self.home / "registry.db") as conn:
            row = conn.execute(
                "SELECT title, goal, latest_summary, status, ended_at FROM sessions WHERE id = ?",
                (session_id,),
            ).fetchone()
            checkpoint_count = conn.execute(
                "SELECT COUNT(*) FROM checkpoints WHERE session_id = ?",
                (session_id,),
            ).fetchone()[0]

        self.assertEqual(row[0], "Multi-image Embedding")
        self.assertEqual(row[1], "Implement fusion baseline")
        self.assertEqual(row[2], "session complete")
        self.assertEqual(row[3], "stopped")
        self.assertIsNotNone(row[4])
        self.assertEqual(checkpoint_count, 2)

    def test_checkpoint_accepts_payload_file(self) -> None:
        self.run_cli("init")
        start = self.run_cli("start", "--agent", "codex", "--agent-session-ref", "sess_file")
        session_id = start.stdout.strip()

        payload_path = Path(self.tmp.name) / "checkpoint.json"
        payload_path.write_text(
            json.dumps(
                {
                    "title": "File Payload Session",
                    "goal": "Load checkpoint from file",
                    "summary": "file payload stored",
                    "completed": [],
                    "blockers": [],
                    "next_actions": ["verify ls output"],
                }
            )
        )

        result = self.run_cli("checkpoint", "--session", session_id, "--payload-file", str(payload_path))
        self.assertEqual(result.returncode, 0, result.stderr)

        with sqlite3.connect(self.home / "registry.db") as conn:
            row = conn.execute(
                "SELECT title, goal, latest_summary FROM sessions WHERE id = ?",
                (session_id,),
            ).fetchone()
        self.assertEqual(row[0], "File Payload Session")
        self.assertEqual(row[1], "Load checkpoint from file")
        self.assertEqual(row[2], "file payload stored")

    def test_checkpoint_accepts_stdin_payload(self) -> None:
        self.run_cli("init")
        start = self.run_cli("start", "--agent", "codex", "--agent-session-ref", "sess_stdin")
        session_id = start.stdout.strip()

        payload = json.dumps(
            {
                "title": "Stdin Payload Session",
                "goal": "Load checkpoint from stdin",
                "summary": "stdin payload stored",
                "completed": ["created via stdin"],
                "blockers": [],
                "next_actions": ["verify doctor"],
            }
        )

        result = subprocess.run(
            [sys.executable, "-m", "asm.cli", "checkpoint", "--session", session_id],
            cwd=str(self.workspace),
            env=self.env,
            text=True,
            input=payload,
            capture_output=True,
            check=False,
        )
        self.assertEqual(result.returncode, 0, result.stderr)

        with sqlite3.connect(self.home / "registry.db") as conn:
            row = conn.execute(
                "SELECT title, goal, latest_summary FROM sessions WHERE id = ?",
                (session_id,),
            ).fetchone()
        self.assertEqual(row[0], "Stdin Payload Session")
        self.assertEqual(row[1], "Load checkpoint from stdin")
        self.assertEqual(row[2], "stdin payload stored")

    def test_ls_shows_resume_command_and_untitled_placeholder(self) -> None:
        self.run_cli("init")
        self.run_cli("start", "--agent", "codex", "--agent-session-ref", "sess_list")

        result = self.run_cli("ls")
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertIn("[untitled]", result.stdout)
        self.assertIn("codex resume sess_list", result.stdout)

    def test_ls_supports_filters_and_long_output(self) -> None:
        self.run_cli("init")

        first = self.run_cli("start", "--agent", "codex", "--agent-session-ref", "sess_filter_1")
        first_id = first.stdout.strip()
        payload = json.dumps(
            {
                "title": "Filtered Session",
                "goal": "Show goal in long output",
                "summary": "active filtered summary",
                "completed": [],
                "blockers": [],
                "next_actions": [],
            }
        )
        self.run_cli("checkpoint", "--session", first_id, "--payload", payload)

        second = self.run_cli("start", "--agent", "codex", "--agent-session-ref", "sess_filter_2")
        second_id = second.stdout.strip()
        second_payload = json.dumps(
            {
                "title": "Stopped Session",
                "goal": "Can be filtered by status",
                "summary": "stopped filtered summary",
                "completed": [],
                "blockers": [],
                "next_actions": [],
            }
        )
        self.run_cli("checkpoint", "--session", second_id, "--payload", second_payload)
        self.run_cli("stop", "--session", second_id)

        status_result = self.run_cli("ls", "--status", "active", "--long")
        self.assertEqual(status_result.returncode, 0, status_result.stderr)
        self.assertIn("Filtered Session", status_result.stdout)
        self.assertIn("goal: Show goal in long output", status_result.stdout)
        self.assertNotIn("Stopped Session", status_result.stdout)

        stopped_result = self.run_cli("ls", "--status", "stopped")
        self.assertEqual(stopped_result.returncode, 0, stopped_result.stderr)
        self.assertIn("Stopped Session", stopped_result.stdout)
        self.assertNotIn("Filtered Session", stopped_result.stdout)

        branch_result = self.run_cli("ls", "--branch", "feature/test", "--limit", "1")
        self.assertEqual(branch_result.returncode, 0, branch_result.stderr)
        lines = [line for line in branch_result.stdout.splitlines() if line.strip()]
        session_headers = [line for line in lines if line.startswith("asm_")]
        self.assertEqual(len(session_headers), 1)

    def test_ls_supports_project_filter(self) -> None:
        self.run_cli("init")

        primary = self.run_cli("start", "--agent", "codex", "--agent-session-ref", "sess_project_primary")
        primary_id = primary.stdout.strip()
        primary_payload = json.dumps(
            {
                "title": "Repo Session",
                "goal": "Belongs to the default repo project",
                "summary": "primary project session",
                "completed": [],
                "blockers": [],
                "next_actions": [],
            }
        )
        self.run_cli("checkpoint", "--session", primary_id, "--payload", primary_payload)

        alt_workspace = Path(self.tmp.name) / "alt-workspace"
        alt_workspace.mkdir()
        alt_env = self.env.copy()
        alt_env["ASM_TEST_GIT_ROOT"] = str(alt_workspace / "other-project")
        alt_env["ASM_TEST_BRANCH"] = "feature/other"
        (alt_workspace / "other-project").mkdir()
        alt_start = subprocess.run(
            [sys.executable, "-m", "asm.cli", "start", "--agent", "codex", "--agent-session-ref", "sess_project_other"],
            cwd=str(alt_workspace),
            env=alt_env,
            text=True,
            capture_output=True,
            check=False,
        )
        self.assertEqual(alt_start.returncode, 0, alt_start.stderr)
        alt_id = alt_start.stdout.strip()
        alt_payload = json.dumps(
            {
                "title": "Other Project Session",
                "goal": "Belongs to another project",
                "summary": "other project session",
                "completed": [],
                "blockers": [],
                "next_actions": [],
            }
        )
        alt_checkpoint = subprocess.run(
            [sys.executable, "-m", "asm.cli", "checkpoint", "--session", alt_id, "--payload", alt_payload],
            cwd=str(alt_workspace),
            env=alt_env,
            text=True,
            capture_output=True,
            check=False,
        )
        self.assertEqual(alt_checkpoint.returncode, 0, alt_checkpoint.stderr)

        result = self.run_cli("ls", "--project", "repo")
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertIn("Repo Session", result.stdout)
        self.assertNotIn("Other Project Session", result.stdout)

    def test_doctor_prefers_same_branch_and_explains_match(self) -> None:
        self.run_cli("init")
        start = self.run_cli("start", "--agent", "codex", "--agent-session-ref", "sess_doc")
        session_id = start.stdout.strip()
        payload = json.dumps(
            {
                "title": "Doc Session",
                "goal": "Find the right session",
                "summary": "doctor should recommend this session",
                "completed": [],
                "blockers": [],
                "next_actions": ["resume it"],
            }
        )
        self.run_cli("checkpoint", "--session", session_id, "--payload", payload)

        result = self.run_cli("doctor")
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertIn("Doc Session", result.stdout)
        self.assertIn("same git root", result.stdout)
        self.assertIn("same branch", result.stdout)
        self.assertIn("codex resume sess_doc", result.stdout)

    def test_doctor_explain_shows_scoring_breakdown(self) -> None:
        self.run_cli("init")
        start = self.run_cli("start", "--agent", "codex", "--agent-session-ref", "sess_doc_explain")
        session_id = start.stdout.strip()
        payload = json.dumps(
            {
                "title": "Explain Session",
                "goal": "Show doctor explain output",
                "summary": "doctor explain should show why",
                "completed": [],
                "blockers": [],
                "next_actions": [],
            }
        )
        self.run_cli("checkpoint", "--session", session_id, "--payload", payload)

        result = self.run_cli("doctor", "--explain")
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertIn("Explain Session", result.stdout)
        self.assertIn("why:", result.stdout)
        self.assertIn("+30 same git root", result.stdout)
        self.assertIn("+20 same branch", result.stdout)

    def test_codex_hook_uses_stdin_payload_for_start_prompt_and_stop(self) -> None:
        self.run_cli("init")

        start_payload = json.dumps(
            {
                "session_id": "codex_real_session",
                "cwd": str(self.workspace),
                "hook_event_name": "SessionStart",
                "source": "startup",
            }
        )
        start = subprocess.run(
            [sys.executable, "-m", "asm.cli", "codex-hook"],
            cwd=str(self.workspace),
            env=self.env,
            text=True,
            input=start_payload,
            capture_output=True,
            check=False,
        )
        self.assertEqual(start.returncode, 0, start.stderr)

        prompt_payload = json.dumps(
            {
                "session_id": "codex_real_session",
                "cwd": str(self.workspace),
                "hook_event_name": "UserPromptSubmit",
                "prompt": "continue",
            }
        )
        prompt = subprocess.run(
            [sys.executable, "-m", "asm.cli", "codex-hook"],
            cwd=str(self.workspace),
            env=self.env,
            text=True,
            input=prompt_payload,
            capture_output=True,
            check=False,
        )
        self.assertEqual(prompt.returncode, 0, prompt.stderr)

        stop_payload = json.dumps(
            {
                "session_id": "codex_real_session",
                "cwd": str(self.workspace),
                "hook_event_name": "Stop",
                "turn_id": "turn_123",
                "last_assistant_message": "done",
            }
        )
        stop = subprocess.run(
            [sys.executable, "-m", "asm.cli", "codex-hook"],
            cwd=str(self.workspace),
            env=self.env,
            text=True,
            input=stop_payload,
            capture_output=True,
            check=False,
        )
        self.assertEqual(stop.returncode, 0, stop.stderr)
        self.assertEqual(json.loads(stop.stdout), {"continue": True})

        with sqlite3.connect(self.home / "registry.db") as conn:
            row = conn.execute(
                "SELECT agent_session_ref, status, ended_at FROM sessions WHERE agent = ? AND agent_session_ref = ?",
                ("codex", "codex_real_session"),
            ).fetchone()
        self.assertEqual(row[0], "codex_real_session")
        self.assertEqual(row[1], "stopped")
        self.assertIsNotNone(row[2])

    def test_current_returns_best_matching_session_for_context(self) -> None:
        self.run_cli("init")
        start = self.run_cli("start", "--agent", "codex", "--agent-session-ref", "sess_current")
        session_id = start.stdout.strip()
        payload = json.dumps(
            {
                "title": "Current Session",
                "goal": "Find session for current workspace",
                "summary": "active in this repo",
                "completed": [],
                "blockers": [],
                "next_actions": ["checkpoint it"],
            }
        )
        self.run_cli("checkpoint", "--session", session_id, "--payload", payload)

        result = self.run_cli("current")
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertIn(session_id, result.stdout)
        self.assertIn("Current Session", result.stdout)
        self.assertIn("codex resume sess_current", result.stdout)

    def test_checkpoint_template_outputs_valid_json(self) -> None:
        result = self.run_cli("checkpoint-template")
        self.assertEqual(result.returncode, 0, result.stderr)
        payload = json.loads(result.stdout)

        self.assertEqual(payload["title"], "")
        self.assertEqual(payload["goal"], "")
        self.assertEqual(payload["summary"], "")
        self.assertEqual(payload["completed"], [])
        self.assertEqual(payload["blockers"], [])
        self.assertEqual(payload["next_actions"], [])

    def test_checkpoint_prompt_outputs_codex_ready_instruction(self) -> None:
        result = self.run_cli("checkpoint-prompt")
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertIn("Please output only JSON", result.stdout)
        self.assertIn('"title"', result.stdout)
        self.assertIn('"goal"', result.stdout)
        self.assertIn('"summary"', result.stdout)
        self.assertIn('"completed"', result.stdout)
        self.assertIn('"blockers"', result.stdout)
        self.assertIn('"next_actions"', result.stdout)

    def test_checkpoint_current_writes_to_best_matching_session(self) -> None:
        self.run_cli("init")
        start = self.run_cli("start", "--agent", "codex", "--agent-session-ref", "sess_current_cp")
        session_id = start.stdout.strip()

        payload = json.dumps(
            {
                "title": "Checkpoint Current",
                "goal": "Write without passing session id",
                "summary": "written through checkpoint-current",
                "completed": [],
                "blockers": [],
                "next_actions": ["verify latest summary"],
            }
        )
        result = subprocess.run(
            [sys.executable, "-m", "asm.cli", "checkpoint-current"],
            cwd=str(self.workspace),
            env=self.env,
            text=True,
            input=payload,
            capture_output=True,
            check=False,
        )
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertIn(session_id, result.stdout)

        with sqlite3.connect(self.home / "registry.db") as conn:
            row = conn.execute(
                "SELECT title, latest_summary FROM sessions WHERE id = ?",
                (session_id,),
            ).fetchone()
        self.assertEqual(row[0], "Checkpoint Current")
        self.assertEqual(row[1], "written through checkpoint-current")

    def test_checkpoint_current_errors_when_no_match_exists(self) -> None:
        self.run_cli("init")
        payload = json.dumps(
            {
                "title": "Missing Session",
                "goal": "Should fail",
                "summary": "no matching session",
                "completed": [],
                "blockers": [],
                "next_actions": [],
            }
        )
        result = subprocess.run(
            [sys.executable, "-m", "asm.cli", "checkpoint-current"],
            cwd=str(self.workspace),
            env=self.env,
            text=True,
            input=payload,
            capture_output=True,
            check=False,
        )
        self.assertNotEqual(result.returncode, 0)
        self.assertIn("No relevant sessions found", result.stderr)

    def test_stop_current_marks_best_matching_session_stopped_with_final_payload(self) -> None:
        self.run_cli("init")
        start = self.run_cli("start", "--agent", "codex", "--agent-session-ref", "sess_stop_current")
        session_id = start.stdout.strip()
        checkpoint_payload = json.dumps(
            {
                "title": "Stop Current",
                "goal": "Stop current session without passing id",
                "summary": "before final stop",
                "completed": [],
                "blockers": [],
                "next_actions": ["write final summary"],
            }
        )
        self.run_cli("checkpoint", "--session", session_id, "--payload", checkpoint_payload)

        final_payload = json.dumps(
            {
                "summary": "finalized through stop-current",
                "completed": ["captured final state"],
                "blockers": [],
                "next_actions": ["resume later if needed"],
            }
        )
        result = subprocess.run(
            [sys.executable, "-m", "asm.cli", "stop-current"],
            cwd=str(self.workspace),
            env=self.env,
            text=True,
            input=final_payload,
            capture_output=True,
            check=False,
        )
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertIn(session_id, result.stdout)

        with sqlite3.connect(self.home / "registry.db") as conn:
            row = conn.execute(
                "SELECT status, latest_summary, ended_at FROM sessions WHERE id = ?",
                (session_id,),
            ).fetchone()
        self.assertEqual(row[0], "stopped")
        self.assertEqual(row[1], "finalized through stop-current")
        self.assertIsNotNone(row[2])

    def test_stop_current_marks_best_matching_session_stopped_without_payload(self) -> None:
        self.run_cli("init")
        start = self.run_cli("start", "--agent", "codex", "--agent-session-ref", "sess_stop_current_no_payload")
        session_id = start.stdout.strip()
        checkpoint_payload = json.dumps(
            {
                "title": "Stop Current No Payload",
                "goal": "Allow stop-current without final payload",
                "summary": "ready to stop",
                "completed": [],
                "blockers": [],
                "next_actions": ["stop it"],
            }
        )
        self.run_cli("checkpoint", "--session", session_id, "--payload", checkpoint_payload)

        result = self.run_cli("stop-current")
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertIn(session_id, result.stdout)

        with sqlite3.connect(self.home / "registry.db") as conn:
            row = conn.execute(
                "SELECT status, latest_summary, ended_at FROM sessions WHERE id = ?",
                (session_id,),
            ).fetchone()
        self.assertEqual(row[0], "stopped")
        self.assertEqual(row[1], "ready to stop")
        self.assertIsNotNone(row[2])


if __name__ == "__main__":
    unittest.main()
