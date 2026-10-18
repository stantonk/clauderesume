# claude-resume

Search **all** your Claude Code sessions from anywhere, and resume one from the directory it was
started in. (`claude --resume` only shows sessions from the exact current directory.)

Single-file, zero-dependency Python run via `uv`.

## Install

```sh
ln -s "$PWD/claude_resume.py" ~/.local/bin/claude-resume
```

## Use

```sh
claude-resume                  # list every session, newest first
claude-resume webhook retry    # only sessions whose title or conversation contain all terms
```

With [fzf](https://github.com/junegunn/fzf) installed, you pick in fzf: type to fuzzy-filter by
date, title and folder, with a preview of the folder's
README.md, or the session's conversation when it has none. Enter `cd`s into that
session's directory and runs `claude --resume <id>`; Esc quits. Search terms on the command line
narrow the list by full conversation text first.

Without fzf, results show 20 at a time: press enter for more, type a number to resume, or `q` to quit. When output isn't a terminal
(e.g. `claude-resume | grep webshop`) everything prints at once.

Sessions are indexed into SQLite (FTS5, trigram) at `~/.config/claude-resume/claude-resume.db`;
each run only re-reads transcripts that changed. Once indexed, a session's conversation stays
searchable even after Claude Code deletes its transcript.

Deleted sessions are hidden unless you pass `--all`. Sessions marked `(deleted)` had their transcript removed by Claude Code's cleanup (default: after
30 days). They're rebuilt from the prompts in `~/.claude/history.jsonl`, so you can still search
them and see where they ran, but they can't be resumed. To keep transcripts, set
`"cleanupPeriodDays": 36500` in `~/.claude/settings.json`.

## Summaries

While the picker is open, sessions without a summary, or whose transcript changed since it was
summarized, get a short title from a model, shown in place of the original title. Each is saved
as soon as it arrives; quitting early just leaves the rest for the next run. Most sessions are
summarized once, since only the ones you're still working in change.
Deleted sessions aren't summarized, but keep any summary they got before their transcript went away.

`claude-resume --summarize` does the same in the foreground with progress and errors (the picker
stays silent so it doesn't garble the screen).

Configure the provider in `~/.config/claude-resume/claude-resume.conf`:

```ini
[summarize]
provider = claude   # default; runs `claude -p` with your existing login, no API key needed
model = haiku       # default for claude
```

```ini
[summarize]
provider = ollama
model = qwen3:8b                 # required for ollama
url = http://localhost:11434     # default
```

`provider = none` turns summaries off.

## Test

```sh
uv run python -m unittest
```
