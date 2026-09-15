# Playbooks and behaviour that bites

Everything here was checked against a live workspace, including how it renders in the YouGile app.

## Write semantics: what merges, what replaces

The single most expensive mistake is assuming `PUT` merges everything. It does not:

| Field | On `PUT` | Consequence |
| --- | --- | --- |
| `stickers` (object) | **merged** per key | set one sticker without touching the others |
| `subtasks` (array) | **replaced** | `GET` first, append, send the full list |
| `checklists` (array) | **replaced** | same - sending one checklist deletes the rest |
| `assigned` (array) | **replaced** | `[]` clears the assignees |
| scalars (`title`, `description`, `columnId`, ...) | overwritten | send only what changes |

Removing one sticker: send `{"stickers": {"<stickerId>": ""}}` (or `null`). Both work.

## Find a task

`--title` is a plain **case-insensitive substring** match over the whole workspace the key can see:

- `карта` and `КАРТА` both find "Карта магазинов"; `рта маг` finds it too - matching ignores word
  boundaries.
- No wildcards: `карта*` matches nothing. Extra spaces are literal, so `Карта  магазинов` fails.
- Pick a short distinctive fragment and normalise the user's spacing before searching.

Two traps in the results:

- **Subtasks come back too, and they have no `columnId`** (it is absent, not null). Never index
  `task["columnId"]` blindly.
- **Archived tasks still appear.** `archived: true` does not hide a task from the API - filter it
  yourself. Only `deleted: true` hides it (and `includeDeleted=true` brings it back).

To say *where* a task lives, walk back up: `columnId` → `columns get` → `boardId` → `boards get` →
`projectId`. Cache that chain for the rest of the conversation instead of re-walking it.

## Add information to a task the user pointed at

Prefer a comment - it is additive, timestamped and cannot destroy anything:

```bash
python3 scripts/yougile.py tasks comment --id SAI-515 --text "Проверил, воспроизводится на проде"
```

```bash
python3 scripts/yougile.py tasks comment --id SAI-515 \
    --text "Готово. Проверьте на стенде" --html "<p><b>Готово.</b> Проверьте на стенде</p>"
```

- **Never put HTML into `text` / `--text`** - the app shows it verbatim, raw `<p><b>` tags on
  screen. Formatting renders only from `textHtml` (`--html`); `text` is the plain fallback shown
  in notifications, so keep it a readable plain version with line breaks. Send both.
- **A sent message cannot be edited.** `PUT /chats/{taskId}/messages/{id}` rejects `text` and
  `textHtml` (`property text should not exist`). Fix a broken one by posting a corrected copy and
  sending `{"deleted": true}` to the old one.
- The API read-back returns only `text`, so do not "verify" formatting
  by re-reading the message - it will look like it was dropped when it was not.
- `label` pins a short tag on the message ("важно") and shows up next to it in the app.
- **Mentions cannot be created through the API.** A real mention made in the app is stored as a
  chunk next to the text:
  `"properties": {"params": {"chunks": [{"type": "user", "replacement": "@Федор", "data": {"userId": "<id>"}}]}}`.
  `POST` rejects `properties` and `mentions` (`property ... should not exist`); `@Name` in `text`
  and mention-like markup in `textHtml` are saved with `chunks: []` - plain text, nobody is
  notified. When the user wants someone tagged, post the message without pretending the tag works
  and ask the user to add a one-line mention from the app. To check whether a message really
  tags someone, read it back and look for a `type: "user"` chunk.

Editing `description` **overwrites** it. To append, read first:

```bash
old=$(python3 scripts/yougile.py tasks get --id SAI-515 | python3 -c "import json,sys;print(json.load(sys.stdin).get('description') or '')")
python3 scripts/yougile.py tasks update --id SAI-515 --description "$old<br>Дополнение: ..."
```

Descriptions accept a subset of HTML and are stored as HTML.

## Set a custom sticker (badge)

A sticker value must be a **state id**, never free text - `{"stickers":{"<id>":"привет"}}` fails
with `For those IDs stickers statuses were not found`. Resolve the names first:

```bash
python3 scripts/yougile.py request GET /string-stickers --query limit=100   # id, name, states[].id/.name
python3 scripts/yougile.py tasks update --id SAI-515 --json '{"stickers":{"<stickerId>":"<stateId>"}}'
```

For the sticker to be visible on the board it must also be enabled there:
`PUT /boards/{id}` with `{"stickers": {"custom": {"<stickerId>": true}}}` (board `stickers` also
carries the built-in `deadline` / `assignee` / `stopwatch` toggles).

Sprint stickers live at `/sprint-stickers` and their states carry `begin`/`end` dates.

**Not every badge is reachable.** Only `string-stickers` and `sprint-stickers` exist in API v2;
`/stickers`, `/number-stickers`, `/text-stickers` and friends are 404. Boards can carry stickers of
other types created in the app - you will see their ids in `board.stickers.custom` and their values
on tasks (e.g. `"дашборд"`, `"3"`), but `GET /string-stickers/{id}` answers 404 for them and there
is no way to create or edit them over the API. Say so instead of guessing.

## Create a task the way a human would

```bash
python3 scripts/yougile.py tasks create --title "Починить логин" --column-id <columnId> \
  --description "Падает на проде" --assigned <userId> --deadline-ms <ms> --json '{"color":"task-red"}'
```

- Colours are a fixed set: `task-red`, `task-yellow`, `task-green`, `task-turquoise`, `task-blue`,
  `task-violet`, `task-pink`, `task-gray`, `task-primary`. Anything else is `Неверное значение
  color` - there is no `task-orange`. **A colour cannot be removed** once set: `null`, `""` and
  `task-none` are all rejected.
- `deadline` is `{"deadline": <ms>, "startDate"?: <ms>, "withTime"?: bool}`. The app renders it in
  the viewer's local timezone, so epoch-midnight-UTC shows up as "03:00" in Moscow. For a date with
  no time, send `withTime: false`; for a real time, convert the user's **local** wall clock.
- `timeTracking` is `{"plan": <hours>, "work": <hours>}`, numbers not strings.
- `checklists` is `[{"title": "...", "items": [{"title": "...", "isCompleted": false}]}]` - the item
  key is `title`, not `name`.

## Subtasks

`columnId` is **optional** on `POST /tasks`, despite what the endpoint table implies. That is the
difference between the two kinds of child task:

> **`tasks create` cannot make one.** The wrapper always sends `columnId`, and omitting the flag
> sends it as `null`, which YouGile rejects with `400 Недопустимое значение columnId: null`. Use
> the raw escape hatch instead — `request POST /tasks --data '{"title": "...", "description":
> "..."}'` — and the task is created with no column at all. Verified 11.09.2026.

- **Real subtask** - create it *without* `columnId`, then add its id to the parent's `subtasks`.
  It lives inside the parent and never appears as a card on the board.
- **Linked task** - a task that already sits in a column and is also listed in `subtasks`. It stays
  a card on the board *and* shows under the parent. Usually not what "add a subtask" means.

There is no `parentId` field, and `columnId` cannot be set to `null` later - decide at creation.
A task created with no `columnId` and never linked to a parent is invisible in the UI: link it
straight away.

A real subtask is a **full task**, not a checklist row: it carries its own `description` (newlines
included) and its own comments via `tasks comment`, and once linked it is issued a project code
(`ID-3307` while orphaned, `BOT-477` after linking). Closing one is `completed: true` - it has no
column to move it to. Order cannot be set over the API, so put the sequence in the titles.

## Build a board

Columns are returned newest-first and the board draws them in that order, so **a new column lands
at the far left**. Create them in reverse of the order you want to see:

```bash
for t in "Готово" "В работе" "Бэклог"; do   # → Бэклог | В работе | Готово on screen
  python3 scripts/yougile.py columns create --title "$t" --board-id <boardId>
done
```

There is no `position`/`order`/`index` field on columns or tasks - ordering cannot be fixed
afterwards over the API, only by dragging in the app. Say that rather than trying.

## Closing a task

`complete` sets `completed: true` **and moves the task into the board's final column**. If the user
asked for "mark done but leave it where it is", that is not available - tell them where it landed.

## Reporting back

Write actions answer with `{"id": ...}` only. Report what you sent (title, column, code like
`PRI-39`), not raw JSON, and re-`get` only when the user needs the resulting state confirmed.
