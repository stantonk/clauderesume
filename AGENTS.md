# Agent instructions

## Changes

- Work on a branch and open a pull request; `main` is protected.
- Keep each pull request to one bounded behavior.
- Follow Test Driven Development: `uv run python -m unittest`

## Code

- `claude_resume.py` is a single file that uses only the standard library and runs with
  `uv run --script`. Don't add dependencies.

## Privacy

This repo is public, and the tool reads private Claude Code transcripts.

- Use invented data in tests, README examples, commit messages and pull requests. Never copy
  anything from a real `~/.claude` folder: no prompts, titles, paths, session IDs or timestamps.
- Commit with a GitHub noreply email, not a personal address.
- Before opening a pull request, check the diff for real names, home paths and email addresses.
