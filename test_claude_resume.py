import io
import json
import os
import re
import sqlite3
import stat
import tempfile
import time
import unittest
from contextlib import redirect_stderr
from contextlib import redirect_stdout
from pathlib import Path
from subprocess import CompletedProcess
from unittest import mock

import claude_resume


class ClaudeResumeTest(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.config = Path(self._tmp.name, "claude")
        self.projects = self.config / "projects"
        self.xdg = Path(self._tmp.name, "xdg")
        self.db_path = self.xdg / "claude-resume" / "claude-resume.db"
        self.root = "/Users/alex/src/webshop"
        self.write_conf("[summarize]\nprovider = none\n")

    def tearDown(self):
        self._tmp.cleanup()

    def test_finds_sessions_from_every_project_most_recent_first(self):
        self.write_session(self.root, "a3af957e-3098-435b-b7da-3394d6eb8600",
                           [user_prompt(self.root, "add retry to webhook sender")], mtime=1000)
        sub = self.root + "/checkout-api"
        self.write_session(sub, "6e2738aa-7c4c-4b94-b9af-05c33c3a42ab",
                           [user_prompt(sub, "fix timezone bug in invoice dates")], mtime=3000)
        other = "/Users/alex/src/dotfiles"
        self.write_session(other, "2404c54b-f619-476f-98b3-20021d029c93",
                           [user_prompt(other, "explain the build script")], mtime=2000)

        sessions = self.search()

        self.assertEqual([(s["id"], s["cwd"]) for s in sessions], [
            ("6e2738aa-7c4c-4b94-b9af-05c33c3a42ab", sub),
            ("2404c54b-f619-476f-98b3-20021d029c93", other),
            ("a3af957e-3098-435b-b7da-3394d6eb8600", self.root),
        ])

    def test_skips_sessions_without_messages(self):
        self.write_session(self.root, "49e97bfd-4da6-4500-abdf-69f99c6f2db4", [
            {"type": "ai-title", "aiTitle": "Empty", "sessionId": "49e97bfd-4da6-4500-abdf-69f99c6f2db4"},
        ])

        self.assertEqual(self.search(), [])

    def test_title_prefers_custom_then_latest_ai_title_then_first_prompt(self):
        self.write_session(self.root, "a", [
            {"type": "ai-title", "aiTitle": "Old title", "sessionId": "a"},
            {"type": "ai-title", "aiTitle": "Webhook retry logic", "sessionId": "a"},
            user_prompt(self.root, "add retries"),
        ])
        self.write_session(self.root, "b", [
            {"type": "ai-title", "aiTitle": "Invoice timezones", "sessionId": "b"},
            {"type": "custom-title", "customTitle": "invoice-timezone-fix", "sessionId": "b"},
            user_prompt(self.root, "fix it"),
        ])
        self.write_session(self.root, "c", [
            user_prompt(self.root, "<command-name>/init</command-name>"),
            user_prompt(self.root, "explain the build script"),
        ])

        titles = {s["id"]: s["title"] for s in self.search()}

        self.assertEqual(titles, {
            "a": "Webhook retry logic",
            "b": "invoice-timezone-fix",
            "c": "explain the build script",
        })

    def test_search_matches_all_terms_case_insensitively_in_user_and_assistant_text(self):
        self.write_session(self.root, "a", [
            user_prompt(self.root, "add retry to Webhook sender"),
            assistant_reply(self.root, "I'll add exponential backoff to the sender."),
        ])
        self.write_session(self.root, "b", [user_prompt(self.root, "webhook docs")])

        self.assertEqual([s["id"] for s in self.search(["WEBHOOK", "backoff"])], ["a"])
        self.assertEqual({s["id"] for s in self.search(["webhook"])}, {"a", "b"})

    def test_search_matches_terms_shorter_than_a_trigram(self):
        self.write_session(self.root, "a", [assistant_reply(self.root, "I'll add exponential backoff to the sender.")])
        self.write_session(self.root, "b", [user_prompt(self.root, "webhook docs")])

        self.assertEqual([s["id"] for s in self.search(["ff"])], ["a"])

    def test_search_ignores_tool_results_and_sidechains(self):
        self.write_session(self.root, "a", [
            user_prompt(self.root, "read the env file"),
            tool_result(self.root, "API_TOKEN=abc123"),
            dict(assistant_reply(self.root, "token in subagent"), isSidechain=True),
        ])

        self.assertEqual(self.search(["token"]), [])

    def test_snippet_shows_context_around_first_term(self):
        self.write_session(self.root, "a", [
            user_prompt(self.root, "add retry to the webhook\nsender then add backoff"),
        ])
        session = self.search()[0]

        self.assertIn("webhook sender", claude_resume.snippet(session, ["SENDER"]))

    def test_resync_picks_up_messages_added_to_a_transcript(self):
        records = [user_prompt(self.root, "add retry to webhook sender")]
        self.write_session(self.root, "a3af957e-3098-435b-b7da-3394d6eb8600", records, mtime=1000)
        self.search()
        records.append(assistant_reply(self.root, "I'll add exponential backoff to the sender."))
        self.write_session(self.root, "a3af957e-3098-435b-b7da-3394d6eb8600", records, mtime=2000)

        self.assertEqual([s["id"] for s in self.search(["backoff"])], ["a3af957e-3098-435b-b7da-3394d6eb8600"])

    def test_indexed_conversation_stays_searchable_after_transcript_is_deleted(self):
        self.write_session(self.root, "a3af957e-3098-435b-b7da-3394d6eb8600", [
            {"type": "ai-title", "aiTitle": "Webhook retry logic",
             "sessionId": "a3af957e-3098-435b-b7da-3394d6eb8600"},
            user_prompt(self.root, "add retry to webhook sender"),
            assistant_reply(self.root, "I'll add exponential backoff to the sender."),
        ])
        self.search()
        next(self.projects.glob("*/a3af957e-3098-435b-b7da-3394d6eb8600.jsonl")).unlink()

        sessions = self.search(["backoff"])

        self.assertEqual([(s["id"], s["title"], s["deleted"]) for s in sessions],
                         [("a3af957e-3098-435b-b7da-3394d6eb8600", "Webhook retry logic", True)])

    def test_lists_sessions_whose_transcript_was_deleted_from_prompt_history(self):
        self.write_session(self.root, "a3af957e-3098-435b-b7da-3394d6eb8600",
                           [user_prompt(self.root, "add retry to webhook sender")], mtime=1770000000)
        research = "/Users/alex/src/search-prototype"
        self.write_history([
            history_entry("/clear", 1767258000000, research, "21d34e80-b880-408b-8595-5ef60a196b92"),
            history_entry("what's left on the migration", 1767258006328, research, "21d34e80-b880-408b-8595-5ef60a196b92"),
            history_entry("where are the cached results?", 1767258072008, research,
                          "21d34e80-b880-408b-8595-5ef60a196b92"),
            history_entry("add retry to webhook sender", 1769000000000, self.root,
                          "a3af957e-3098-435b-b7da-3394d6eb8600"),
        ])

        sessions = self.search()

        self.assertEqual([(s["id"], s["deleted"]) for s in sessions], [
            ("a3af957e-3098-435b-b7da-3394d6eb8600", False),
            ("21d34e80-b880-408b-8595-5ef60a196b92", True),
        ])
        deleted = sessions[1]
        self.assertEqual((deleted["cwd"], deleted["title"], deleted["mtime"]),
                         (research, "what's left on the migration", 1767258072.008))
        self.assertEqual([s["id"] for s in self.search(["cached", "results"])],
                         ["21d34e80-b880-408b-8595-5ef60a196b92"])

    def test_help_names_the_clauderesume_command(self):
        with redirect_stdout(io.StringIO()) as out:
            claude_resume.main(["--help"])

        self.assertTrue(out.getvalue().startswith("usage: clauderesume [--all] [search terms...]\n"))
        self.assertIn("       clauderesume --summarize\n", out.getvalue())

    def test_main_keeps_its_index_in_the_config_directory(self):
        self.write_session(self.root, "a3af957e-3098-435b-b7da-3394d6eb8600",
                           [user_prompt(self.root, "add retry to webhook sender")])

        self.run_main(["q"])

        sessions = claude_resume.search(claude_resume.open_db(self.db_path), [])
        self.assertEqual([s["id"] for s in sessions], ["a3af957e-3098-435b-b7da-3394d6eb8600"])

    def test_main_keeps_its_index_private_to_the_user(self):
        self.write_session(self.root, "a3af957e-3098-435b-b7da-3394d6eb8600",
                           [user_prompt(self.root, "add retry to webhook sender")])

        self.run_main(["q"])

        self.assertEqual(stat.S_IMODE(self.db_path.parent.stat().st_mode), 0o700)

    def test_main_resumes_chosen_session_from_its_original_directory_when_run_elsewhere(self):
        sub = self.root + "/checkout-api"
        self.write_session(self.root, "a3af957e-3098-435b-b7da-3394d6eb8600",
                           [user_prompt(self.root, "webhook docs")], mtime=1000)
        self.write_session(sub, "6e2738aa-7c4c-4b94-b9af-05c33c3a42ab",
                           [user_prompt(sub, "timezone tests")], mtime=2000)

        out, _, execvp, chdir, _ = self.run_main(["1"], ["timezone"])

        self.assertIn("[~/src/webshop/checkout-api]", out)
        self.assertNotIn("webhook docs", out)
        chdir.assert_called_once_with(sub)
        execvp.assert_called_once_with("claude", ["claude", "--resume", "6e2738aa-7c4c-4b94-b9af-05c33c3a42ab"])

    def test_main_marks_deleted_sessions_and_refuses_to_resume_them(self):
        research = "/Users/alex/src/search-prototype"
        self.write_history([history_entry("what's left on the migration", 1767258006328, research,
                                          "21d34e80-b880-408b-8595-5ef60a196b92")])

        with redirect_stderr(io.StringIO()) as err:
            out, _, execvp, chdir, status = self.run_main(["1"], ["--all"])

        self.assertRegex(out, r"what's left on the migration +\[~/src/search-prototype\]  \(deleted\)")
        self.assertIn("transcript was deleted", err.getvalue())
        self.assertEqual(status, 1)
        chdir.assert_not_called()
        execvp.assert_not_called()

    def test_main_hides_deleted_sessions_unless_asked_for_all(self):
        self.write_session(self.root, "a3af957e-3098-435b-b7da-3394d6eb8600",
                           [user_prompt(self.root, "add retry to webhook sender")], mtime=1770000000)
        self.write_history([history_entry("what's left on the migration", 1767258006328, "/Users/alex/src/search-prototype",
                                          "21d34e80-b880-408b-8595-5ef60a196b92")])

        default, _, _, _, _ = self.run_main(["q"])
        everything, _, _, _, _ = self.run_main(["q"], ["--all"])

        self.assertEqual(listed_numbers(default), [1])
        self.assertNotIn("what's left on the migration", default)
        self.assertEqual(listed_numbers(everything), [1, 2])
        self.assertIn("what's left on the migration", everything)

    def test_main_says_when_only_hidden_deleted_sessions_match(self):
        self.write_history([history_entry("what's left on the migration", 1767258006328, "/Users/alex/src/search-prototype",
                                          "21d34e80-b880-408b-8595-5ef60a196b92")])

        with redirect_stderr(io.StringIO()) as err:
            _, _, _, _, status = self.run_main([], ["migration"])

        self.assertEqual(status, 1)
        self.assertIn("1 deleted session matches; use --all to show it", err.getvalue())

    def test_main_pages_twenty_sessions_at_a_time_and_resumes_from_a_later_page(self):
        ids = self.write_numbered_sessions(25)

        out, prompts, execvp, _, _ = self.run_main(["", "23"])

        self.assertEqual(len(prompts), 2)
        self.assertIn("enter for more", prompts[0])
        self.assertNotIn("enter for more", prompts[1])
        self.assertEqual(listed_numbers(out), list(range(1, 26)))
        execvp.assert_called_once_with("claude", ["claude", "--resume", ids[22]])

    def test_main_shows_only_first_page_when_choosing_from_it(self):
        ids = self.write_numbered_sessions(25)

        out, _, execvp, _, _ = self.run_main(["2"])

        self.assertEqual(listed_numbers(out), list(range(1, 21)))
        execvp.assert_called_once_with("claude", ["claude", "--resume", ids[1]])

    def test_main_warns_and_pauses_before_paging_when_fzf_is_missing(self):
        self.write_numbered_sessions(3)

        with redirect_stderr(io.StringIO()) as err:
            self.run_main(["q"])

        self.assertIn("fzf isn't installed", err.getvalue())
        self.sleep.assert_called_once_with(1)

    def test_main_does_not_warn_about_fzf_when_it_is_installed(self):
        self.write_numbered_sessions(3)

        with mock.patch("subprocess.run", return_value=CompletedProcess([], 130, stdout="")), \
                redirect_stderr(io.StringIO()) as err:
            self.run_main([], fzf="/opt/homebrew/bin/fzf")

        self.assertNotIn("fzf", err.getvalue())
        self.sleep.assert_not_called()

    def test_main_quits_on_q(self):
        self.write_numbered_sessions(25)

        _, prompts, execvp, _, status = self.run_main(["q"])

        self.assertEqual((len(prompts), status), (1, 0))
        execvp.assert_not_called()

    def test_main_prints_every_session_without_prompting_when_not_a_terminal(self):
        self.write_numbered_sessions(25)

        with mock.patch.dict(os.environ, self.env()), \
                mock.patch("sys.stdin.isatty", return_value=False), \
                mock.patch("builtins.input") as prompt, \
                redirect_stdout(io.StringIO()) as out:
            claude_resume.main([])

        self.assertEqual(listed_numbers(out.getvalue()), list(range(1, 26)))
        prompt.assert_not_called()

    def test_rows_align_folders_by_padding_or_truncating_titles(self):
        self.write_session(self.root, "a", [user_prompt(self.root, "looks good")], mtime=2000)
        self.write_session(self.root, "b", [user_prompt(
            self.root, "the nightly export job fails when a customer has no billing address")],
            mtime=1000)

        with mock.patch.dict(os.environ, {"HOME": "/Users/alex"}):
            rows = [claude_resume.format_row(s) for s in self.search()]

        self.assertEqual({row.index("[~/src/webshop]") for row in rows}, {18 + 50 + 2})
        self.assertIn("the nightly export job fails when a customer has …", rows[1])

    def test_main_picks_with_fzf_when_installed_and_resumes_the_selection(self):
        sub = self.root + "/checkout-api"
        self.write_session(self.root, "a3af957e-3098-435b-b7da-3394d6eb8600",
                           [user_prompt(self.root, "add retry to webhook sender")], mtime=1000)
        self.write_session(sub, "6e2738aa-7c4c-4b94-b9af-05c33c3a42ab",
                           [user_prompt(sub, "fix timezone bug in invoice dates")], mtime=2000)
        picked = "a3af957e-3098-435b-b7da-3394d6eb8600\t1970-01-01 00:16  add retry to webhook sender\n"

        with mock.patch("subprocess.run", return_value=CompletedProcess([], 0, stdout=picked)) as fzf:
            _, prompts, execvp, chdir, _ = self.run_main([], fzf="/opt/homebrew/bin/fzf")

        lines = fzf.call_args.kwargs["input"].splitlines()
        self.assertEqual([line.split("\t")[0] for line in lines],
                         ["6e2738aa-7c4c-4b94-b9af-05c33c3a42ab", "a3af957e-3098-435b-b7da-3394d6eb8600"])
        self.assertIn("fix timezone bug in invoice dates", lines[0].split("\t")[1])
        self.assertEqual(prompts, [])
        chdir.assert_called_once_with(self.root)
        execvp.assert_called_once_with("claude", ["claude", "--resume", "a3af957e-3098-435b-b7da-3394d6eb8600"])

    def test_main_does_nothing_when_fzf_is_cancelled(self):
        self.write_session(self.root, "a3af957e-3098-435b-b7da-3394d6eb8600",
                           [user_prompt(self.root, "add retry to webhook sender")])

        with mock.patch("subprocess.run", return_value=CompletedProcess([], 130, stdout="")):
            _, _, execvp, _, status = self.run_main([], fzf="/opt/homebrew/bin/fzf")

        self.assertEqual(status, 0)
        execvp.assert_not_called()

    def test_preview_shows_folder_and_each_message_on_its_own_line(self):
        self.write_session(self.root, "a3af957e-3098-435b-b7da-3394d6eb8600", [
            {"type": "ai-title", "aiTitle": "Webhook retry logic",
             "sessionId": "a3af957e-3098-435b-b7da-3394d6eb8600"},
            user_prompt(self.root, "add retry to webhook sender"),
            assistant_reply(self.root, "I'll add exponential backoff to the sender."),
        ])
        self.search()

        with mock.patch.dict(os.environ, self.env()), redirect_stdout(io.StringIO()) as out:
            claude_resume.main(["--preview", "a3af957e-3098-435b-b7da-3394d6eb8600"])

        lines = out.getvalue().splitlines()
        self.assertEqual(lines[0], "Webhook retry logic")
        self.assertIn("~/src/webshop", lines[1])
        self.assertEqual(lines[-2:], ["add retry to webhook sender", "I'll add exponential backoff to the sender."])

    def test_preview_shows_the_folders_readme_instead_of_the_conversation_when_it_has_one(self):
        project = Path(self._tmp.name, "checkout-api")
        project.mkdir()
        readme = "# checkout-api\n\nOrder and payment endpoints for the webshop.\n"
        (project / "README.md").write_text(readme)
        self.write_session(str(project), "a3af957e-3098-435b-b7da-3394d6eb8600",
                           [user_prompt(str(project), "add retry to webhook sender")])
        self.search()

        with mock.patch.dict(os.environ, self.env()), redirect_stdout(io.StringIO()) as out:
            claude_resume.main(["--preview", "a3af957e-3098-435b-b7da-3394d6eb8600"])

        self.assertEqual(out.getvalue(), readme)

    def test_summarize_asks_claude_haiku_once_per_session_and_shows_the_summary(self):
        self.write_conf("[summarize]\nprovider = claude\n")
        self.write_session(self.root, "a3af957e-3098-435b-b7da-3394d6eb8600", [
            {"type": "ai-title", "aiTitle": "Webhook retry logic",
             "sessionId": "a3af957e-3098-435b-b7da-3394d6eb8600"},
            user_prompt(self.root, "add retry to webhook sender"),
            assistant_reply(self.root, "I'll add exponential backoff to the sender."),
        ])

        with mock.patch("subprocess.run", return_value=CompletedProcess(
                [], 0, stdout="Add exponential backoff to webhooks\n")) as claude:
            self.run_main([], ["--summarize"])
            self.run_main([], ["--summarize"])

        claude.assert_called_once()
        self.assertEqual(claude.call_args.args[0],
                         ["claude", "-p", "--model", "haiku", "--no-session-persistence", "--tools", "",
                          "--system-prompt", claude_resume.SUMMARY_PROMPT])
        self.assertEqual(claude.call_args.kwargs["cwd"], tempfile.gettempdir())
        self.assertRegex(claude.call_args.kwargs["input"],
                         r"(?s)^<transcript>\n.*I'll add exponential backoff to the sender\.\n</transcript>")
        self.assertIn("\nFolder: ~/src/webshop\n", claude.call_args.kwargs["input"])
        session = self.search(["backoff to webhooks"])[0]
        self.assertEqual(session["summary"], "Add exponential backoff to webhooks")
        self.assertIn("Add exponential backoff to webhooks", claude_resume.format_row(session))

    def test_summary_survives_new_messages_in_the_transcript(self):
        self.write_conf("[summarize]\nprovider = claude\n")
        records = [user_prompt(self.root, "add retry to webhook sender")]
        self.write_session(self.root, "a3af957e-3098-435b-b7da-3394d6eb8600", records, mtime=1000)
        with mock.patch("subprocess.run", return_value=CompletedProcess([], 0, stdout="Webhook retry sender")):
            self.run_main([], ["--summarize"])
        records.append(assistant_reply(self.root, "I'll add exponential backoff to the sender."))
        self.write_session(self.root, "a3af957e-3098-435b-b7da-3394d6eb8600", records, mtime=2000)

        self.assertEqual([s["summary"] for s in self.search(["backoff"])], ["Webhook retry sender"])

    def test_summarize_uses_ollama_when_configured(self):
        self.write_session(self.root, "a3af957e-3098-435b-b7da-3394d6eb8600",
                           [user_prompt(self.root, "add retry to webhook sender")])
        self.write_conf("[summarize]\nprovider = ollama\nmodel = qwen3:8b\nurl = http://localhost:11434\n")
        reply = io.BytesIO(json.dumps({"model": "qwen3:8b", "response": "Webhook retry sender",
                                       "done": True}).encode())

        with mock.patch("urllib.request.urlopen", return_value=reply) as urlopen:
            self.run_main([], ["--summarize"])

        request = urlopen.call_args.args[0]
        body = json.loads(request.data)
        self.assertEqual(request.full_url, "http://localhost:11434/api/generate")
        self.assertEqual((body["model"], body["system"], body["stream"], body["think"]),
                         ("qwen3:8b", claude_resume.SUMMARY_PROMPT, False, False))
        self.assertRegex(body["prompt"], r"(?s)^<transcript>\n.*add retry to webhook sender\n</transcript>")
        self.assertEqual([s["summary"] for s in self.search()], ["Webhook retry sender"])

    def test_summarize_requires_a_model_for_ollama(self):
        self.write_session(self.root, "a3af957e-3098-435b-b7da-3394d6eb8600",
                           [user_prompt(self.root, "add retry to webhook sender")])
        self.write_conf("[summarize]\nprovider = ollama\n")

        with redirect_stderr(io.StringIO()) as err:
            _, _, _, _, status = self.run_main([], ["--summarize"])

        self.assertEqual(status, 1)
        self.assertIn("model", err.getvalue())

    def test_main_summarizes_while_the_picker_waits_for_a_choice(self):
        self.write_conf("[summarize]\nprovider = claude\n")
        self.write_session(self.root, "a3af957e-3098-435b-b7da-3394d6eb8600",
                           [user_prompt(self.root, "add retry to webhook sender")])

        with mock.patch("subprocess.run", return_value=CompletedProcess([], 0, stdout="Webhook retry sender")):
            self.run_main([lambda: self.wait_for_summaries() or "q"])

        self.assertEqual([s["summary"] for s in self.search()], ["Webhook retry sender"])

    def test_summarize_again_only_for_transcripts_changed_since_their_summary(self):
        self.write_conf("[summarize]\nprovider = claude\n")
        records = [user_prompt(self.root, "add retry to webhook sender")]
        self.write_session(self.root, "a3af957e-3098-435b-b7da-3394d6eb8600", records, mtime=1000)
        self.write_session(self.root, "6e2738aa-7c4c-4b94-b9af-05c33c3a42ab",
                           [user_prompt(self.root, "fix timezone bug in invoice dates")], mtime=1000)
        with mock.patch("subprocess.run", return_value=CompletedProcess([], 0, stdout="Webhook retry sender")):
            self.run_main([], ["--summarize"])
        records.append(assistant_reply(self.root, "I'll add exponential backoff to the sender."))
        self.write_session(self.root, "a3af957e-3098-435b-b7da-3394d6eb8600", records, mtime=2000)

        with mock.patch("subprocess.run", return_value=CompletedProcess(
                [], 0, stdout="Webhook retries with backoff")) as claude:
            self.run_main([], ["--summarize"])

        claude.assert_called_once()
        self.assertIn("exponential backoff", claude.call_args.kwargs["input"])
        self.assertEqual({s["id"]: s["summary"] for s in self.search()}, {
            "a3af957e-3098-435b-b7da-3394d6eb8600": "Webhook retries with backoff",
            "6e2738aa-7c4c-4b94-b9af-05c33c3a42ab": "Webhook retry sender",
        })

    def test_summarize_skips_deleted_sessions_but_keeps_their_earlier_summary(self):
        self.write_conf("[summarize]\nprovider = claude\n")
        self.write_session(self.root, "a3af957e-3098-435b-b7da-3394d6eb8600",
                           [user_prompt(self.root, "add retry to webhook sender")], mtime=1000)
        self.write_history([history_entry("what's left on the migration", 1767258006328, "/Users/alex/src/search-prototype",
                                          "21d34e80-b880-408b-8595-5ef60a196b92")])
        with mock.patch("subprocess.run", return_value=CompletedProcess(
                [], 0, stdout="Webhook retry sender")) as first:
            self.run_main([], ["--summarize"])
        (self.projects / "-Users-alex-src-webshop" / "a3af957e-3098-435b-b7da-3394d6eb8600.jsonl").unlink()

        with mock.patch("subprocess.run") as second:
            self.run_main([], ["--summarize"])

        first.assert_called_once()
        self.assertIn("add retry to webhook sender", first.call_args.kwargs["input"])
        second.assert_not_called()
        self.assertEqual({s["id"]: s["summary"] for s in self.search()}, {
            "a3af957e-3098-435b-b7da-3394d6eb8600": "Webhook retry sender",
            "21d34e80-b880-408b-8595-5ef60a196b92": None,
        })

    def test_opens_an_index_created_before_summaries_were_tracked(self):
        self.write_conf("[summarize]\nprovider = claude\n")
        old = sqlite3.connect(self.db_path)
        old.executescript("""
            CREATE TABLE sessions (id TEXT PRIMARY KEY, cwd TEXT NOT NULL, title TEXT NOT NULL, summary TEXT,
                                   mtime REAL NOT NULL, deleted INTEGER NOT NULL, source_mtime REAL);
            CREATE VIRTUAL TABLE search USING fts5(title, summary, text, tokenize='trigram');
            INSERT INTO sessions VALUES ('a3af957e-3098-435b-b7da-3394d6eb8600',
                '/Users/alex/src/webshop', 'add retry to webhook sender', NULL, 1000, 0, 1000);
            INSERT INTO search (rowid, title, summary, text)
                VALUES (1, 'add retry to webhook sender', NULL, 'add retry to webhook sender');
        """)
        old.close()
        self.write_session(self.root, "a3af957e-3098-435b-b7da-3394d6eb8600",
                           [user_prompt(self.root, "add retry to webhook sender")], mtime=1000)

        with mock.patch("subprocess.run", return_value=CompletedProcess([], 0, stdout="Webhook retry sender")):
            self.run_main([], ["--summarize"])

        self.assertEqual([s["summary"] for s in self.search()], ["Webhook retry sender"])

    def test_summaries_can_be_turned_off(self):
        self.write_session(self.root, "a3af957e-3098-435b-b7da-3394d6eb8600",
                           [user_prompt(self.root, "add retry to webhook sender")])

        with mock.patch("subprocess.run") as claude, redirect_stderr(io.StringIO()) as err:
            _, _, _, _, status = self.run_main([], ["--summarize"])

        self.assertEqual(status, 0)
        self.assertIn("provider = none", err.getvalue())
        claude.assert_not_called()

    def test_summarize_rejects_an_unknown_provider(self):
        self.write_session(self.root, "a3af957e-3098-435b-b7da-3394d6eb8600",
                           [user_prompt(self.root, "add retry to webhook sender")])
        self.write_conf("[summarize]\nprovider = openai\n")

        with redirect_stderr(io.StringIO()) as err:
            _, _, _, _, status = self.run_main([], ["--summarize"])

        self.assertEqual(status, 1)
        self.assertIn("unknown provider 'openai'", err.getvalue())

    def test_summarize_stops_at_the_first_failure_without_saving(self):
        self.write_conf("[summarize]\nprovider = claude\n")
        self.write_session(self.root, "a3af957e-3098-435b-b7da-3394d6eb8600",
                           [user_prompt(self.root, "add retry to webhook sender")])

        with mock.patch("subprocess.run", return_value=CompletedProcess(
                [], 1, stdout="", stderr="Invalid API key")), \
                redirect_stderr(io.StringIO()) as err:
            _, _, _, _, status = self.run_main([], ["--summarize"])

        self.assertEqual(status, 1)
        self.assertIn("Invalid API key", err.getvalue())
        self.assertEqual([s["summary"] for s in self.search()], [None])

    def search(self, terms=()):
        conn = claude_resume.open_db(self.db_path)
        claude_resume.sync(conn, self.config)
        return claude_resume.search(conn, list(terms))

    def env(self):
        return {"CLAUDE_CONFIG_DIR": str(self.config), "XDG_CONFIG_HOME": str(self.xdg), "HOME": "/Users/alex"}

    def wait_for_summaries(self):
        conn = claude_resume.open_db(self.db_path)
        for _ in range(100):
            if not claude_resume.needs_summary(conn):
                return
            time.sleep(0.05)

    def write_numbered_sessions(self, count):
        ids = [f"{n:08x}-3098-435b-b7da-3394d6eb8600" for n in range(count)]
        for n, session_id in enumerate(ids):
            self.write_session(self.root, session_id, [user_prompt(self.root, f"refactor module {n}")],
                               mtime=2000000 - n)
        return ids

    def run_main(self, answers, argv=(), fzf=None):
        prompts = []

        def answer(prompt):
            prompts.append(prompt)
            reply = answers[len(prompts) - 1]
            return reply() if callable(reply) else reply

        with mock.patch.dict(os.environ, self.env()), \
                mock.patch("shutil.which", return_value=fzf), \
                mock.patch("sys.stdin.isatty", return_value=True), \
                mock.patch("builtins.input", side_effect=answer), \
                mock.patch("os.chdir") as chdir, \
                mock.patch("os.execvp") as execvp, \
                mock.patch("time.sleep") as self.sleep, \
                redirect_stdout(TerminalOutput()) as out:
            status = claude_resume.main(list(argv))
        return out.getvalue(), prompts, execvp, chdir, status

    def write_session(self, cwd, session_id, records, mtime=None):
        project_dir = self.projects / re.sub(r"[^A-Za-z0-9]", "-", cwd)
        project_dir.mkdir(parents=True, exist_ok=True)
        path = project_dir / f"{session_id}.jsonl"
        path.write_text("".join(json.dumps(r) + "\n" for r in records))
        if mtime is not None:
            os.utime(path, (mtime, mtime))

    def write_conf(self, text):
        self.db_path.parent.mkdir(parents=True, exist_ok=True)
        self.db_path.with_suffix(".conf").write_text(text)

    def write_history(self, entries):
        self.config.mkdir(parents=True, exist_ok=True)
        self.config.joinpath("history.jsonl").write_text("".join(json.dumps(e) + "\n" for e in entries))


class TerminalOutput(io.StringIO):
    def isatty(self):
        return True


def user_prompt(cwd, text):
    return {"parentUuid": None, "isSidechain": False, "type": "user",
            "message": {"role": "user", "content": text},
            "uuid": "cd1db99e-7bec-4b9d-a1f2-c08b51ea7836", "timestamp": "2026-01-05T09:12:03.512Z",
            "cwd": cwd, "sessionId": "2404c54b-f619-476f-98b3-20021d029c93", "gitBranch": "main"}


def assistant_reply(cwd, text):
    return {"parentUuid": "cd1db99e-7bec-4b9d-a1f2-c08b51ea7836", "isSidechain": False, "type": "assistant",
            "message": {"role": "assistant", "content": [{"type": "thinking", "thinking": ""},
                                                         {"type": "text", "text": text}]},
            "timestamp": "2026-01-05T09:12:07.231Z", "cwd": cwd}


def listed_numbers(output):
    return [int(line.split()[0]) for line in output.splitlines() if line.split() and line.split()[0].isdigit()]


def history_entry(display, timestamp_ms, project, session_id):
    return {"display": display, "pastedContents": {}, "timestamp": timestamp_ms,
            "project": project, "sessionId": session_id}


def tool_result(cwd, text):
    return {"parentUuid": "8f5d80c0-0d7c-4a7d-af1e-25525d341448", "isSidechain": False, "type": "user",
            "message": {"role": "user", "content": [{"tool_use_id": "toolu_01Qm7cJ2vXn9RbT4kLw8pZs3",
                                                     "type": "tool_result", "content": text}]},
            "timestamp": "2026-01-05T09:12:05.104Z", "cwd": cwd}


if __name__ == "__main__":
    unittest.main()
