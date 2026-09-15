---
name: yougile
description: Work with YouGile (yougile.com / ru.yougile.com) task boards through its REST API v2 - list and search tasks, create and edit tasks, move them between columns, comment on them, and manage projects, boards, columns and users. Includes a guided first-run flow that walks the user through issuing an API key and stores it for later. Use this skill whenever the user mentions YouGile, Юджайл, a YouGile board/project/task, a task code like "SAI-515", or asks to create/update/close tasks in their YouGile workspace - even if they do not say "API".
---

# YouGile REST API v2

A dependency-free helper (`scripts/yougile.py`, Python 3.8+, stdlib only) plus the knowledge needed
to drive the YouGile API directly. Everything the script prints is JSON, so its output can be read,
filtered and chained without extra parsing rules.

## Step 0 - always check auth first

```bash
python3 scripts/yougile.py status
```

- `"hasKey": true, "ok": true` → the token works, go straight to the task.
- `"hasKey": false`, or `"ok": false` with 401/403 → read `references/auth.md` and run the flow
  there. Never guess a key and never keep retrying a rejected one.

Short version if the user is ready to hand over credentials: login and password go through
`login --password-stdin` (the script gets the key itself, admin rights are **not** required); an
already-issued key goes through `setup --key`. The key lands in `~/.yougile/credentials.json`.

## Core commands

All commands accept `--key` and `--base-url`; self-hosted installs need
`--base-url https://<domain>/api-v2` (save it once via `setup --base-url ...`).

```bash
# structure: projects -> boards -> columns -> tasks
python3 scripts/yougile.py projects list --all
python3 scripts/yougile.py boards list --project-id <projectId> --all
python3 scripts/yougile.py columns list --board-id <boardId> --all

# tasks
python3 scripts/yougile.py tasks list --column-id <columnId> --all
python3 scripts/yougile.py tasks list --title "деплой"          # substring search, case-insensitive
python3 scripts/yougile.py tasks get --id <taskId|SAI-515>
python3 scripts/yougile.py tasks create --title "Починить логин" --column-id <columnId> \
    --description "Падает на проде" --assigned <userId> --deadline-ms 1767225600000
python3 scripts/yougile.py tasks update --id <taskId> --title "Новое название" --description "..."
python3 scripts/yougile.py tasks move --id <taskId> --column-id <otherColumnId>
python3 scripts/yougile.py tasks complete --id <taskId>
python3 scripts/yougile.py tasks comments --id <taskId>
python3 scripts/yougile.py tasks comment --id <taskId> --text "Готово, проверьте"   # plain, shown verbatim
python3 scripts/yougile.py tasks comment --id <taskId> --text "Готово" --html "<b>Готово</b>"  # formatted

# people
python3 scripts/yougile.py users list --all
```

Anything the wrappers do not cover goes through the raw escape hatch, which reuses the same auth,
retries and error messages:

```bash
python3 scripts/yougile.py request GET /group-chats --query limit=10
python3 scripts/yougile.py request PUT /tasks/<id> --data '{"archived":true}'
```

Create/update bodies also accept `--json '{...}'` for fields the flags do not expose
(`checklists`, `stickers`, `timeTracking`, `subtasks`, `color`, ...). Flags win over `--json`.

## Rules that save round trips

- **IDs, not names.** Tasks live in columns; to create one you need a `columnId`. Resolve it by
  walking projects → boards → columns and reuse it for the rest of the conversation.
- **`projectId` is not a task filter.** The API rejects it. Filter by `columnId`, `assignedTo` or
  `title`; to cover a project, collect its columns and iterate.
- **`PUT` replaces arrays but merges `stickers`.** Sending one checklist or one subtask deletes the
  others - read, append, send the whole list. See `references/recipes.md`.
- **Lists paginate** as `{"content": [...], "paging": {...}}`. Pass `--all` for everything,
  `--limit` for a sample. YouGile's `offset` paging drops and duplicates rows, so `--all` asks for
  one 1000-row page instead of walking offsets; do not force a small `--limit` alongside it.
- **Search returns archived tasks and subtasks.** `archived: true` does not hide anything, and
  subtasks have no `columnId` - check both before reporting results.
- **400 and 404 are often permission errors**, not bad requests or missing objects. Do not retry;
  see the table in `references/auth.md`.
- **Rate limit ≈ 50 requests/minute per company.** The script retries 429 with backoff; batch work
  sequentially rather than in parallel fan-out.
- **Nothing is truly deleted** - `--deleted true` is the delete, `--archived true` archives. Prefer
  archiving unless the user asked for deletion, and confirm destructive or mass changes first.
- **Write actions echo `{"id": ...}` only.** Report what changed in words, not raw JSON.
- **The API cannot @-mention anyone.** `@Name` in a comment stays plain text - no highlight, no
  notification. If the user asks to tag someone, post the comment and tell them to add the tag in
  the app. See `references/recipes.md`.

## Where to look next

| File | Read it when |
| --- | --- |
| `references/auth.md` | no key, key rejected, or a call fails on permissions |
| `references/recipes.md` | acting on a task: search, comment, stickers, deadlines, subtasks, board layout |
| `references/api.md` | you need an endpoint, a field shape or an error code the wrappers do not cover |

If an endpoint's exact shape is uncertain, the live interactive documentation at
https://ru.yougile.com/api-v2 is authoritative - check there rather than guessing field names.

## Portability

The skill only assumes `python3` and network access to the YouGile host; no pip installs, no
Claude-specific tools. Any agent that can run a shell command can use it. Without a shell, an agent
can still work from `references/api.md` and issue plain HTTP requests with
`Authorization: Bearer <key>`.
