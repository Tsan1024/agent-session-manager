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
        checkpoint_prompt = self.codex_home / "prompts" / "asm-checkpoint.md"
        final_prompt = self.codex_home / "prompts" / "asm-final.md"
        finish_prompt = self.codex_home / "prompts" / "asm-finish.md"
        self.assertTrue(checkpoint_prompt.exists())
        self.assertTrue(final_prompt.exists())
        self.assertTrue(finish_prompt.exists())

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
        self.assertIn("Generate ASM checkpoint JSON", checkpoint_prompt.read_text())
        self.assertIn("Generate ASM final checkpoint JSON", final_prompt.read_text())
        self.assertIn("only for end-of-session recording", final_prompt.read_text())
        self.assertIn("asm finalize-current", final_prompt.read_text())
        self.assertIn("asm finalize-current --payload-file", finish_prompt.read_text())
        self.assertIn("Do not exit the agent session", finish_prompt.read_text())
        self.assertIn("Do not use `asm stop` or `asm stop-current`", finish_prompt.read_text())
        self.assertIn("request permission and retry", finish_prompt.read_text())

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
        stopped = self.run_cli("finalize", "--session", session_id, "--payload", stop_payload)
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
        self.assertIn("goal   Show goal in long output", status_result.stdout)
        self.assertNotIn("Stopped Session", status_result.stdout)

        stopped_result = self.run_cli("ls", "--status", "stopped")
        self.assertEqual(stopped_result.returncode, 0, stopped_result.stderr)
        self.assertIn("Stopped Session", stopped_result.stdout)
        self.assertNotIn("Filtered Session", stopped_result.stdout)

        branch_result = self.run_cli("ls", "--branch", "feature/test", "--limit", "1")
        self.assertEqual(branch_result.returncode, 0, branch_result.stderr)
        lines = [line for line in branch_result.stdout.splitlines() if line.strip()]
        session_headers = [line for line in lines if line.startswith("[asm_")]
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

    def test_ls_hides_noisy_historical_goal_and_prefers_initial_prompt(self) -> None:
        self.run_cli("init")
        start = self.run_cli("start", "--agent", "codex", "--agent-session-ref", "sess_noisy_history")
        session_id = start.stdout.strip()

        with sqlite3.connect(self.home / "registry.db") as conn:
            conn.execute(
                """
                UPDATE sessions
                SET title = ?, initial_prompt = ?, goal = ?, latest_summary = ?, status = 'stopped'
                WHERE id = ?
                """,
                (
                    "问题本质： 我做的修正： 改动文件：",
                    "我想测试一下插件性能",
                    "问题本质： 我做的修正： 改动文件： 验证结果：",
                    "历史 summary",
                    session_id,
                ),
            )
            conn.commit()

        result = self.run_cli("ls", "--long")
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertIn("我想测试一下插件性能", result.stdout)
        self.assertNotIn("问题本质： 我做的修正： 改动文件：", result.stdout)
        self.assertNotIn("验证结果：", result.stdout)
        self.assertNotIn("goal   ", result.stdout)

    def test_current_hides_noisy_historical_title_when_no_hint_exists(self) -> None:
        self.run_cli("init")
        start = self.run_cli("start", "--agent", "codex", "--agent-session-ref", "sess_noisy_title_only")
        session_id = start.stdout.strip()

        with sqlite3.connect(self.home / "registry.db") as conn:
            conn.execute(
                """
                UPDATE sessions
                SET title = ?, goal = ?, status = 'stopped'
                WHERE id = ?
                """,
                (
                    "我建议按这个顺序收敛： 1. 把 finalize-current 做成标准退出命令",
                    "你现在可以直接重新试： asm ls --long",
                    session_id,
                ),
            )
            conn.commit()

        result = self.run_cli("current")
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertIn("[untitled]", result.stdout)
        self.assertNotIn("我建议按这个顺序收敛", result.stdout)
        self.assertNotIn("你现在可以直接重新试", result.stdout)

    def test_ls_long_truncates_last_summary_for_readability(self) -> None:
        self.run_cli("init")
        start = self.run_cli("start", "--agent", "codex", "--agent-session-ref", "sess_long_last")
        session_id = start.stdout.strip()
        payload = json.dumps(
            {
                "title": "Long Last Session",
                "goal": "Keep ls readable",
                "summary": (
                    "已验证 ASM CLI 可通过 python3 -m asm.cli 正常运行，ls/current/doctor/checkpoint-template/"
                    "checkpoint-prompt 均通过。确认当前问题是 asm 主命令未进入 PATH。随后确认当前目录是 "
                    "viking_pipeline_orchestrator 仓库，现阶段主要承载 old_idp 历史模块归档，而非已成型 orchestrator。"
                ),
                "completed": [],
                "blockers": [],
                "next_actions": [],
            }
        )
        self.run_cli("checkpoint", "--session", session_id, "--payload", payload)

        result = self.run_cli("ls", "--long")
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertIn("last   ", result.stdout)
        self.assertIn("...", result.stdout)

    def test_ls_default_hides_agent_line_for_compactness(self) -> None:
        self.run_cli("init")
        start = self.run_cli("start", "--agent", "codex", "--agent-session-ref", "sess_compact_ls")
        session_id = start.stdout.strip()
        payload = json.dumps(
            {
                "title": "Compact Session",
                "goal": "Keep default ls compact",
                "summary": "compact listing should omit the agent row",
                "completed": [],
                "blockers": [],
                "next_actions": [],
            }
        )
        self.run_cli("checkpoint", "--session", session_id, "--payload", payload)

        result = self.run_cli("ls")
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertIn(session_id, result.stdout)
        self.assertNotIn("  agent  codex", result.stdout)

    def test_ls_uses_absolute_timestamp_format(self) -> None:
        self.run_cli("init")
        start = self.run_cli("start", "--agent", "codex", "--agent-session-ref", "sess_absolute_time")
        session_id = start.stdout.strip()

        with sqlite3.connect(self.home / "registry.db") as conn:
            conn.execute(
                "UPDATE sessions SET updated_at = ? WHERE id = ?",
                ("2026-06-11T13:52:03+08:00", session_id),
            )
            conn.commit()

        result = self.run_cli("ls")
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertIn("2026-06-11 13:52", result.stdout)

    def test_search_matches_initial_prompt_hint(self) -> None:
        self.run_cli("init")

        start_payload = json.dumps(
            {
                "session_id": "codex_search_hint",
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
                "session_id": "codex_search_hint",
                "cwd": str(self.workspace),
                "hook_event_name": "UserPromptSubmit",
                "prompt": "我想测试一下插件性能",
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

        result = self.run_cli("search", "插件性能")
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertIn("我想测试一下插件性能", result.stdout)

    def test_search_matches_goal_and_summary(self) -> None:
        self.run_cli("init")
        start = self.run_cli("start", "--agent", "codex", "--agent-session-ref", "sess_search_summary")
        session_id = start.stdout.strip()
        payload = json.dumps(
            {
                "title": "Old IDP Analysis",
                "goal": "梳理 old_idp 模块",
                "summary": "分析 orchestrator 应该如何组织",
                "completed": [],
                "blockers": [],
                "next_actions": [],
            }
        )
        self.run_cli("checkpoint", "--session", session_id, "--payload", payload)

        goal_result = self.run_cli("search", "old_idp")
        self.assertEqual(goal_result.returncode, 0, goal_result.stderr)
        self.assertIn("Old IDP Analysis", goal_result.stdout)

        summary_result = self.run_cli("search", "orchestrator")
        self.assertEqual(summary_result.returncode, 0, summary_result.stderr)
        self.assertIn("Old IDP Analysis", summary_result.stdout)

    def test_search_respects_project_and_branch_filters(self) -> None:
        self.run_cli("init")
        primary = self.run_cli("start", "--agent", "codex", "--agent-session-ref", "sess_search_primary")
        primary_id = primary.stdout.strip()
        primary_payload = json.dumps(
            {
                "title": "Plugin Search Session",
                "goal": "测试插件性能",
                "summary": "主仓库中的插件性能测试",
                "completed": [],
                "blockers": [],
                "next_actions": [],
            }
        )
        self.run_cli("checkpoint", "--session", primary_id, "--payload", primary_payload)

        alt_workspace = Path(self.tmp.name) / "alt-workspace"
        alt_workspace.mkdir()
        alt_env = self.env.copy()
        alt_env["ASM_TEST_GIT_ROOT"] = str(alt_workspace / "another-repo")
        alt_env["ASM_TEST_BRANCH"] = "feature/other"
        (alt_workspace / "another-repo").mkdir()
        alt_start = subprocess.run(
            [sys.executable, "-m", "asm.cli", "start", "--agent", "codex", "--agent-session-ref", "sess_search_alt"],
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
                "title": "Other Search Session",
                "goal": "测试插件性能",
                "summary": "另一个项目中的插件性能测试",
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

        result = self.run_cli("search", "插件性能", "--project", "repo", "--branch", "feature/test")
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertIn("Plugin Search Session", result.stdout)
        self.assertNotIn("Other Search Session", result.stdout)

    def test_search_explain_shows_matched_fields(self) -> None:
        self.run_cli("init")
        start = self.run_cli("start", "--agent", "codex", "--agent-session-ref", "sess_search_explain")
        session_id = start.stdout.strip()
        payload = json.dumps(
            {
                "title": "Plugin Performance Session",
                "goal": "测试插件性能",
                "summary": "插件性能分析结果",
                "completed": [],
                "blockers": [],
                "next_actions": [],
            }
        )
        self.run_cli("checkpoint", "--session", session_id, "--payload", payload)

        result = self.run_cli("search", "插件性能", "--explain")
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertIn("why", result.stdout)
        self.assertIn("matched goal", result.stdout)
        self.assertIn("matched latest_summary", result.stdout)

    def test_search_without_explain_keeps_output_compact(self) -> None:
        self.run_cli("init")
        start = self.run_cli("start", "--agent", "codex", "--agent-session-ref", "sess_search_compact")
        session_id = start.stdout.strip()
        payload = json.dumps(
            {
                "title": "Compact Search Session",
                "goal": "测试插件性能",
                "summary": "插件性能测试",
                "completed": [],
                "blockers": [],
                "next_actions": [],
            }
        )
        self.run_cli("checkpoint", "--session", session_id, "--payload", payload)

        result = self.run_cli("search", "插件性能")
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertNotIn("matched goal", result.stdout)

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

    def test_user_prompt_submit_autofills_title_and_goal_for_clear_task(self) -> None:
        self.run_cli("init")

        start_payload = json.dumps(
            {
                "session_id": "codex_prompt_task",
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
                "session_id": "codex_prompt_task",
                "cwd": str(self.workspace),
                "hook_event_name": "UserPromptSubmit",
                "prompt": "请实现 IDP plugin 的重试逻辑并补上相关测试",
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

        with sqlite3.connect(self.home / "registry.db") as conn:
            row = conn.execute(
                "SELECT title, goal FROM sessions WHERE agent = ? AND agent_session_ref = ?",
                ("codex", "codex_prompt_task"),
            ).fetchone()
        self.assertIsNotNone(row[0])
        self.assertIn("IDP plugin", row[1])

    def test_user_prompt_submit_requests_clarification_for_ambiguous_prompt(self) -> None:
        self.run_cli("init")

        start_payload = json.dumps(
            {
                "session_id": "codex_prompt_ambiguous",
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
                "session_id": "codex_prompt_ambiguous",
                "cwd": str(self.workspace),
                "hook_event_name": "UserPromptSubmit",
                "prompt": "继续",
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
        response = json.loads(prompt.stdout)
        self.assertIn("systemMessage", response)
        self.assertIn("一句话说明这次要处理的具体任务目标", response["systemMessage"])

    def test_user_prompt_submit_requests_clarification_for_advice_style_prompt(self) -> None:
        self.run_cli("init")

        start_payload = json.dumps(
            {
                "session_id": "codex_prompt_advice",
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
                "session_id": "codex_prompt_advice",
                "cwd": str(self.workspace),
                "hook_event_name": "UserPromptSubmit",
                "prompt": "你觉得我应该做什么",
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
        response = json.loads(prompt.stdout)
        self.assertIn("systemMessage", response)

        with sqlite3.connect(self.home / "registry.db") as conn:
            row = conn.execute(
                "SELECT title, goal, clarification_requested FROM sessions WHERE agent = ? AND agent_session_ref = ?",
                ("codex", "codex_prompt_advice"),
            ).fetchone()
        self.assertIsNone(row[0])
        self.assertIsNone(row[1])
        self.assertEqual(row[2], 1)

    def test_user_prompt_submit_requests_clarification_for_weak_testing_intent(self) -> None:
        self.run_cli("init")

        start_payload = json.dumps(
            {
                "session_id": "codex_prompt_test_intent",
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
                "session_id": "codex_prompt_test_intent",
                "cwd": str(self.workspace),
                "hook_event_name": "UserPromptSubmit",
                "prompt": "我想测试asm",
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
        response = json.loads(prompt.stdout)
        self.assertIn("systemMessage", response)

        with sqlite3.connect(self.home / "registry.db") as conn:
            row = conn.execute(
                "SELECT title, initial_prompt, goal, clarification_requested FROM sessions WHERE agent = ? AND agent_session_ref = ?",
                ("codex", "codex_prompt_test_intent"),
            ).fetchone()
        self.assertIsNone(row[0])
        self.assertEqual(row[1], "我想测试asm")
        self.assertIsNone(row[2])
        self.assertEqual(row[3], 1)

    def test_user_prompt_submit_requests_clarification_for_long_exploratory_intent(self) -> None:
        self.run_cli("init")

        start_payload = json.dumps(
            {
                "session_id": "codex_prompt_long_intent",
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
                "session_id": "codex_prompt_long_intent",
                "cwd": str(self.workspace),
                "hook_event_name": "UserPromptSubmit",
                "prompt": "我想测试一下插件性能",
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
        response = json.loads(prompt.stdout)
        self.assertIn("systemMessage", response)

        with sqlite3.connect(self.home / "registry.db") as conn:
            row = conn.execute(
                "SELECT title, initial_prompt, goal, clarification_requested FROM sessions WHERE agent = ? AND agent_session_ref = ?",
                ("codex", "codex_prompt_long_intent"),
            ).fetchone()
        self.assertIsNone(row[0])
        self.assertEqual(row[1], "我想测试一下插件性能")
        self.assertIsNone(row[2])
        self.assertEqual(row[3], 1)

    def test_user_prompt_submit_accepts_exploratory_prefix_with_explicit_assignment(self) -> None:
        self.run_cli("init")

        start_payload = json.dumps(
            {
                "session_id": "codex_prompt_explicit_assignment",
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
                "session_id": "codex_prompt_explicit_assignment",
                "cwd": str(self.workspace),
                "hook_event_name": "UserPromptSubmit",
                "prompt": "我想让你实现 IDP plugin 的重试逻辑并补上相关测试",
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
        self.assertEqual(prompt.stdout, "")

        with sqlite3.connect(self.home / "registry.db") as conn:
            row = conn.execute(
                "SELECT title, goal, clarification_requested FROM sessions WHERE agent = ? AND agent_session_ref = ?",
                ("codex", "codex_prompt_explicit_assignment"),
            ).fetchone()
        self.assertIsNotNone(row[0])
        self.assertIn("IDP plugin", row[1])
        self.assertEqual(row[2], 0)

    def test_codex_stop_hook_writes_fallback_final_checkpoint_when_no_agent_final_exists(self) -> None:
        self.run_cli("init")

        start_payload = json.dumps(
            {
                "session_id": "codex_auto_checkpoint",
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

        with sqlite3.connect(self.home / "registry.db") as conn:
            session_id = conn.execute(
                "SELECT id FROM sessions WHERE agent = ? AND agent_session_ref = ?",
                ("codex", "codex_auto_checkpoint"),
            ).fetchone()[0]

        payload = json.dumps(
            {
                "title": "Auto Final Session",
                "goal": "Let Stop hook write a final checkpoint",
                "summary": "semantic context exists",
                "completed": [],
                "blockers": [],
                "next_actions": [],
            }
        )
        checkpoint = self.run_cli("checkpoint", "--session", session_id, "--payload", payload)
        self.assertEqual(checkpoint.returncode, 0, checkpoint.stderr)

        stop_payload = json.dumps(
            {
                "session_id": "codex_auto_checkpoint",
                "cwd": str(self.workspace),
                "hook_event_name": "Stop",
                "turn_id": "turn_final",
                "stop_hook_active": False,
                "last_assistant_message": "Completed the requested changes and verified the tests pass.",
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
                "SELECT status, latest_summary, latest_summary_source FROM sessions WHERE id = ?",
                (session_id,),
            ).fetchone()
            checkpoint_rows = [
                (value[0], value[1])
                for value in conn.execute(
                    "SELECT kind, source FROM checkpoints WHERE session_id = ? ORDER BY created_at ASC",
                    (session_id,),
                ).fetchall()
            ]
        self.assertEqual(row[0], "stopped")
        self.assertEqual(row[1], "Completed the requested changes and verified the tests pass.")
        self.assertEqual(row[2], "fallback")
        self.assertEqual(checkpoint_rows, [("progress", "agent"), ("final", "fallback")])

    def test_codex_stop_hook_creates_minimal_final_checkpoint_without_prior_checkpoint(self) -> None:
        self.run_cli("init")

        start_payload = json.dumps(
            {
                "session_id": "codex_auto_minimal",
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

        stop_payload = json.dumps(
            {
                "session_id": "codex_auto_minimal",
                "cwd": str(self.workspace),
                "hook_event_name": "Stop",
                "turn_id": "turn_minimal",
                "stop_hook_active": False,
                "last_assistant_message": "Implemented the IDP plugin changes and updated the wiring.",
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
                "SELECT status, latest_summary, latest_summary_source, title, goal FROM sessions WHERE agent = ? AND agent_session_ref = ?",
                ("codex", "codex_auto_minimal"),
            ).fetchone()
            checkpoint_rows = conn.execute(
                "SELECT kind, source, summary FROM checkpoints WHERE session_id = (SELECT id FROM sessions WHERE agent = ? AND agent_session_ref = ?)",
                ("codex", "codex_auto_minimal"),
            ).fetchall()
        self.assertEqual(row[0], "stopped")
        self.assertEqual(row[1], "Implemented the IDP plugin changes and updated the wiring.")
        self.assertEqual(row[2], "fallback")
        self.assertIsNone(row[3])
        self.assertIsNone(row[4])
        self.assertEqual(checkpoint_rows, [("final", "fallback", "Implemented the IDP plugin changes and updated the wiring.")])

    def test_codex_stop_hook_condenses_chatty_message_into_short_summary(self) -> None:
        self.run_cli("init")

        start_payload = json.dumps(
            {
                "session_id": "codex_condense_stop",
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

        chatty_message = """请给我具体文档。

可以直接用任一种方式：
- 发文件路径，比如 `docs/architecture.md`
- 发文件名，我来在仓库里找
- 直接贴文档内容

如果你愿意，我会按这个方式帮你理解：
- 先讲这份文档在说什么
- 再拆核心概念和术语
- 再梳理流程/架构/时序
"""
        stop_payload = json.dumps(
            {
                "session_id": "codex_condense_stop",
                "cwd": str(self.workspace),
                "hook_event_name": "Stop",
                "turn_id": "turn_condense",
                "stop_hook_active": False,
                "last_assistant_message": chatty_message,
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

        with sqlite3.connect(self.home / "registry.db") as conn:
            row = conn.execute(
                "SELECT title, goal, latest_summary FROM sessions WHERE agent = ? AND agent_session_ref = ?",
                ("codex", "codex_condense_stop"),
            ).fetchone()

        self.assertIsNone(row[0])
        self.assertIsNone(row[1])
        self.assertLessEqual(len(row[2]), 100)
        self.assertNotIn("docs/architecture.md", row[2])
        self.assertNotIn("可以直接用任一种方式", row[2])

    def test_codex_stop_hook_prefers_conclusion_over_follow_up_suggestions(self) -> None:
        self.run_cli("init")

        start_payload = json.dumps(
            {
                "session_id": "codex_stop_conclusion",
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

        stop_payload = json.dumps(
            {
                "session_id": "codex_stop_conclusion",
                "cwd": str(self.workspace),
                "hook_event_name": "Stop",
                "turn_id": "turn_stop_conclusion",
                "stop_hook_active": False,
                "last_assistant_message": (
                    "已确认 asm 能跑，当前问题不是 ASM 坏了，而是 asm 主命令没有进 PATH。 "
                    "我实际做的测试结果：发现本机已有 ASM 相关目录，用源码入口直接跑 CLI，自检通过。 "
                    "如果你要，我也可以继续把这个任务细化成测试哪些能力，比如只测查询类命令。"
                ),
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

        with sqlite3.connect(self.home / "registry.db") as conn:
            row = conn.execute(
                "SELECT title, goal, latest_summary FROM sessions WHERE agent = ? AND agent_session_ref = ?",
                ("codex", "codex_stop_conclusion"),
            ).fetchone()

        self.assertIn("已确认 asm 能跑", row[2])
        self.assertNotIn("如果你要", row[2])
        self.assertNotIn("细化成测试哪些能力", row[2])
        self.assertLessEqual(len(row[2]), 100)

    def test_codex_stop_hook_does_not_override_agent_final_checkpoint(self) -> None:
        self.run_cli("init")

        start_payload = json.dumps(
            {
                "session_id": "codex_agent_final",
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

        final_payload = json.dumps(
            {
                "title": "Agent Final",
                "goal": "Finalize with agent-authored summary",
                "summary": "agent-authored final summary",
                "completed": ["wrote the final checkpoint"],
                "blockers": [],
                "next_actions": [],
            }
        )
        finalize = subprocess.run(
            [sys.executable, "-m", "asm.cli", "finalize-current"],
            cwd=str(self.workspace),
            env=self.env,
            text=True,
            input=final_payload,
            capture_output=True,
            check=False,
        )
        self.assertEqual(finalize.returncode, 0, finalize.stderr)

        stop_payload = json.dumps(
            {
                "session_id": "codex_agent_final",
                "cwd": str(self.workspace),
                "hook_event_name": "Stop",
                "turn_id": "turn_agent_final",
                "stop_hook_active": False,
                "last_assistant_message": "this fallback summary should not replace the agent final",
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

        with sqlite3.connect(self.home / "registry.db") as conn:
            row = conn.execute(
                "SELECT latest_summary, latest_summary_source, status FROM sessions WHERE agent = ? AND agent_session_ref = ?",
                ("codex", "codex_agent_final"),
            ).fetchone()
            checkpoint_rows = conn.execute(
                "SELECT kind, source, summary FROM checkpoints WHERE session_id = (SELECT id FROM sessions WHERE agent = ? AND agent_session_ref = ?)",
                ("codex", "codex_agent_final"),
            ).fetchall()

        self.assertEqual(row[0], "agent-authored final summary")
        self.assertEqual(row[1], "agent")
        self.assertEqual(row[2], "stopped")
        self.assertEqual(checkpoint_rows, [("final", "agent", "agent-authored final summary")])

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

    def test_current_explain_shows_scoring_breakdown(self) -> None:
        self.run_cli("init")
        start = self.run_cli("start", "--agent", "codex", "--agent-session-ref", "sess_current_explain")
        session_id = start.stdout.strip()
        payload = json.dumps(
            {
                "title": "Current Explain Session",
                "goal": "Explain current selection",
                "summary": "current explain should show why",
                "completed": [],
                "blockers": [],
                "next_actions": [],
            }
        )
        self.run_cli("checkpoint", "--session", session_id, "--payload", payload)

        result = self.run_cli("current", "--explain")
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertIn("Current Explain Session", result.stdout)
        self.assertIn("why:", result.stdout)
        self.assertIn("+30 same git root", result.stdout)
        self.assertIn("+20 same branch", result.stdout)

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

    def test_readme_uses_finalize_current_as_standard_shutdown_flow(self) -> None:
        readme = (ROOT / "README.md").read_text()
        self.assertIn("asm-final", readme)
        self.assertIn("asm-finish", readme)
        self.assertIn("./asm finalize-current", readme)
        self.assertIn("normal Codex Q&A is unchanged", readme)
        self.assertIn("does not exit the agent session", readme)
        self.assertIn("still keeps the base session record and resume command", readme)
        self.assertIn("recommended one-step end-of-session flow", readme)
        self.assertIn("does not accept a final payload", readme)

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

    def test_ls_long_shows_summary_source(self) -> None:
        self.run_cli("init")
        start = self.run_cli("start", "--agent", "codex", "--agent-session-ref", "sess_ls_source")
        session_id = start.stdout.strip()

        payload = json.dumps(
            {
                "title": "List Source",
                "goal": "Show summary source in ls output",
                "summary": "agent summary for ls",
                "completed": [],
                "blockers": [],
                "next_actions": [],
            }
        )
        self.run_cli("checkpoint", "--session", session_id, "--payload", payload)

        result = self.run_cli("ls", "--long")
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertIn("from", result.stdout)
        self.assertIn("agent", result.stdout)

    def test_finalize_current_allows_untitled_session_when_only_summary_is_available(self) -> None:
        self.run_cli("init")
        start = self.run_cli("start", "--agent", "codex", "--agent-session-ref", "sess_finalize_untitled")
        session_id = start.stdout.strip()

        final_payload = json.dumps(
            {
                "summary": "captured the current status without a formal task title",
                "completed": ["saved the latest state"],
                "blockers": [],
                "next_actions": ["clarify the task later if needed"],
            }
        )
        result = subprocess.run(
            [sys.executable, "-m", "asm.cli", "finalize-current"],
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
                "SELECT title, goal, latest_summary, latest_summary_source, status FROM sessions WHERE id = ?",
                (session_id,),
            ).fetchone()
        self.assertIsNone(row[0])
        self.assertIsNone(row[1])
        self.assertEqual(row[2], "captured the current status without a formal task title")
        self.assertEqual(row[3], "agent")
        self.assertEqual(row[4], "stopped")

    def test_ls_long_keeps_untitled_sessions_visible(self) -> None:
        self.run_cli("init")
        start = self.run_cli("start", "--agent", "codex", "--agent-session-ref", "sess_untitled_visible")
        session_id = start.stdout.strip()

        final_payload = json.dumps(
            {
                "summary": "stopped without a clarified task",
                "completed": [],
                "blockers": [],
                "next_actions": [],
            }
        )
        finalize = self.run_cli("finalize", "--session", session_id, "--payload", final_payload)
        self.assertEqual(finalize.returncode, 0, finalize.stderr)

        result = self.run_cli("ls", "--long")
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertIn(session_id, result.stdout)
        self.assertIn("[untitled]", result.stdout)
        self.assertIn("stopped without a clarified task", result.stdout)

    def test_ls_prefers_initial_prompt_when_title_is_missing(self) -> None:
        self.run_cli("init")

        start_payload = json.dumps(
            {
                "session_id": "codex_initial_prompt_card",
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
                "session_id": "codex_initial_prompt_card",
                "cwd": str(self.workspace),
                "hook_event_name": "UserPromptSubmit",
                "prompt": "我想测试一下插件性能",
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

        result = self.run_cli("ls", "--long")
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertIn("我想测试一下插件性能", result.stdout)
        self.assertNotIn("[untitled]", result.stdout)

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

    def test_finalize_current_writes_agent_final_checkpoint_and_stops_session(self) -> None:
        self.run_cli("init")
        start = self.run_cli("start", "--agent", "codex", "--agent-session-ref", "sess_finalize_current")
        session_id = start.stdout.strip()
        checkpoint_payload = json.dumps(
            {
                "title": "Finalize Current",
                "goal": "Finalize current session without passing id",
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
            [sys.executable, "-m", "asm.cli", "finalize-current"],
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
                "SELECT status, latest_summary, latest_summary_source, ended_at FROM sessions WHERE id = ?",
                (session_id,),
            ).fetchone()
            checkpoint_rows = conn.execute(
                "SELECT kind, source, summary FROM checkpoints WHERE session_id = ? ORDER BY created_at ASC",
                (session_id,),
            ).fetchall()
        self.assertEqual(row[0], "stopped")
        self.assertEqual(row[1], "finalized through stop-current")
        self.assertEqual(row[2], "agent")
        self.assertIsNotNone(row[3])
        self.assertEqual(
            checkpoint_rows,
            [
                ("progress", "agent", "before final stop"),
                ("final", "agent", "finalized through stop-current"),
            ],
        )

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
                "SELECT status, latest_summary, latest_summary_source, ended_at FROM sessions WHERE id = ?",
                (session_id,),
            ).fetchone()
            checkpoint_count = conn.execute(
                "SELECT COUNT(*) FROM checkpoints WHERE session_id = ?",
                (session_id,),
            ).fetchone()[0]
        self.assertEqual(row[0], "stopped")
        self.assertEqual(row[1], "ready to stop")
        self.assertEqual(row[2], "agent")
        self.assertIsNotNone(row[3])
        self.assertEqual(checkpoint_count, 1)

    def test_stop_current_rejects_payload_arguments(self) -> None:
        self.run_cli("init")
        result = self.run_cli("stop-current", "--payload", '{"summary":"should fail"}')
        self.assertNotEqual(result.returncode, 0)
        self.assertIn("unrecognized arguments", result.stderr)


if __name__ == "__main__":
    unittest.main()
