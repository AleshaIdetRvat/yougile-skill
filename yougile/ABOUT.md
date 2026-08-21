# About this skill

`yougile` lets an AI agent work with [YouGile](https://ru.yougile.com) task boards over its REST
API v2: find and read tasks, create and edit them, move them between columns, comment, and manage
projects, boards, columns and users.

## What is inside

- `SKILL.md` - the instruction the agent reads. Deliberately thin: auth check, the commands, and
  the handful of rules needed on every call.
- `scripts/yougile.py` - a dependency-free CLI over the API. Python 3.8+, standard library only,
  no pip install. Everything it prints is JSON.
- `references/auth.md` - getting a key, storing it, and what to do when permissions run out.
- `references/recipes.md` - playbooks for real requests ("find task X and add Y") plus the
  behaviour that bites: which fields merge and which get replaced, sticker states, colours,
  deadlines and timezones, subtasks, column order.
- `references/api.md` - endpoints, field shapes, error codes.

The agent loads `SKILL.md` up front and pulls a reference file only when the task needs it, so the
context cost stays small.

## First run

The agent asks for access once. Either you hand it your YouGile login and password - it fetches an
API key itself and stores only the key - or you paste a key you already made in the app
(`Ctrl + ~` → API settings). Administrator rights are not required: a plain employee account can
issue a key, and that key sees exactly what its owner sees, nothing more.

The key is stored in `~/.yougile/credentials.json` (mode 600) on the machine that runs the agent.
Every teammate goes through this once with their own account; nothing is shared between them.

## Requirements

`python3` and network access to the YouGile host. Nothing else - no pip packages, no Docker, no
Claude-specific tooling. Self-hosted installs work through
`setup --base-url https://<domain>/api-v2`.
