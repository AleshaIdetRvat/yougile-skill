# YouGile REST API v2 - reference

Base URL: `https://ru.yougile.com/api-v2` (equivalently `https://yougile.com/api-v2`).
Self-hosted: `https://<your-domain>/api-v2`.
Auth: `Authorization: Bearer <API_KEY>`, bodies and responses are JSON.
Interactive docs (authoritative when this file is unclear): https://ru.yougile.com/api-v2

Contents:

1. Auth and keys
2. Data model
3. Projects
4. Boards
5. Columns
6. Tasks
7. Task chat (comments)
8. Users and departments
9. Chats, stickers, webhooks
10. Pagination, limits, errors

---

## 1. Auth and keys

These three endpoints are the only ones that do **not** take a Bearer token.

| Method | Path | Body | Returns |
| --- | --- | --- | --- |
| POST | `/auth/companies` | `{"login","password","name"?}` | `{"content":[{"id","name","isAdmin"}],"paging":{...}}` |
| POST | `/auth/keys` | `{"login","password","companyId"}` | `{"key":"..."}` |
| POST | `/auth/keys/get` | `{"login","password","companyId"?}` | a bare JSON array `[{"key","timestamp","companyId"}]`, **not** `{"content":[...]}` |
| DELETE | `/auth/keys/{key}` | - | revokes that key |

Notes:

- **Any member can issue a key, not just administrators.** Verified live: a non-admin account
  listed its companies, called `/auth/keys` and got a working key. `isAdmin` in the
  `/auth/companies` response is informational.
- A key carries **the permissions of the account that issued it**, not company-wide access. A
  non-admin key sees only that person's projects; anything else answers `404`.
- `/auth/keys/get` is scoped to the login that asks: it returns that user's own keys, not every
  key in the company.
- Up to 30 keys per company; keys do not expire. Before minting a new one, check `/auth/keys/get`
  and reuse a live key for that company - that is what `yougile.py login` does by default.
- A key is bound to one company, so the company id does not need to be tracked separately once the
  key is stored.
- Keys can also be created in the app UI: `Ctrl + ~` (or the ⚙️ icon on the projects page) → API settings.
- Company id can also be read in the app with `Ctrl + Alt + Q` (`Ctrl + Option + Q` on macOS).

## 2. Data model

```
company
└── project        (title, users: {userId: role})
    └── board      (projectId, title, stickers)
        └── column (boardId, title, color 1..16)
            └── task (columnId, title, description, assigned, deadline, checklists, stickers, ...)
                └── chat messages (comments), keyed by the task id
```

A task always belongs to a column. Subtasks are tasks referenced through the parent's `subtasks`.

## 3. Projects

| Method | Path | Notes |
| --- | --- | --- |
| GET | `/projects` | query: `title`, `limit`, `offset`, `includeDeleted` |
| POST | `/projects` | `{"title":"...", "users": {"<userId>":"admin|manager|worker"}}` |
| GET | `/projects/{id}` | |
| PUT | `/projects/{id}` | `{"title"?, "users"?, "deleted"?}` |

## 4. Boards

| Method | Path | Notes |
| --- | --- | --- |
| GET | `/boards` | query: `title`, `projectId`, `limit`, `offset`, `includeDeleted` |
| POST | `/boards` | `{"title","projectId","stickers"?}` |
| GET | `/boards/{id}` | |
| PUT | `/boards/{id}` | `{"title"?, "projectId"? (move), "deleted"?}` |

## 5. Columns

| Method | Path | Notes |
| --- | --- | --- |
| GET | `/columns` | query: `title`, `boardId`, `limit`, `offset`, `includeDeleted` |
| POST | `/columns` | `{"title","boardId","color"? 1..16}` |
| GET | `/columns/{id}` | |
| PUT | `/columns/{id}` | `{"title"?, "color"?, "boardId"? (move), "deleted"?}` |

There is no `position` / `order` / `index` field. Columns come back newest-first and the board
renders them in that order, so a newly created column appears **at the far left**. Create them in
reverse of the desired left-to-right order; existing order can only be changed by dragging in the
app.

## 6. Tasks

| Method | Path | Notes |
| --- | --- | --- |
| GET | `/tasks` | filters: `columnId`, `assignedTo`, `title`, `limit`, `offset`, `includeDeleted` |
| POST | `/tasks` | only `title` is required - `columnId` is optional (a task without one is a subtask, see below) |
| GET | `/tasks/{id}` | `{id}` accepts the uuid **or** the human code, e.g. `SAI-515` |
| PUT | `/tasks/{id}` | partial update - send only the fields that change |

`POST`/`PUT` on tasks, projects, boards and columns answer with `{"id": "..."}` only - never the
updated object. To report what changed, re-`GET` the object (or just echo the fields you sent).

Setting `completed: true` also **moves the task into the board's final column**, so a task can end
up somewhere other than where you left it. Verified live: a task moved to "В работе" and then
completed landed in "Готово".

**There is no `projectId` filter for tasks.** Collect the project's columns and query per column.

Task fields worth knowing:

| Field | Type | Meaning |
| --- | --- | --- |
| `title` | string | task name |
| `description` | string | body text (HTML subset supported) |
| `columnId` | string | current column; change it to move the task, including to another board or project. Absent on subtasks, and it cannot be set to `null` later |
| `archived` | bool | archived out of the board |
| `completed` | bool | done |
| `deleted` | bool | soft delete |
| `assigned` | string[] | user ids |
| `deadline` | object | `{"deadline": <unix ms>, "startDate"?: <unix ms>, "withTime"?: bool, "deadlineType"?}` |
| `timeTracking` | object | `{"plan": <hours>, "work": <hours>}` |
| `checklists` | array | `[{"title":"...","items":[{"title":"...","isCompleted":false}]}]` |
| `subtasks` | string[] | ids of child tasks; **replaced** wholesale on `PUT`. A child created without `columnId` lives only inside the parent; one that has a `columnId` also stays a card on the board |
| `stickers` | object | `{"<stickerId>":"<stateId>"}`; **merged** per key on `PUT`. The value must be a real state id - free text is rejected with `For those IDs stickers statuses were not found`. Clear one with `""` or `null` |
| `color` | string | one of `task-red`, `task-yellow`, `task-green`, `task-turquoise`, `task-blue`, `task-violet`, `task-pink`, `task-gray`, `task-primary`. No `task-orange`, and a colour cannot be cleared - `null`, `""` and `task-none` are all rejected |
| `idTaskCommon` / `idTaskProject` | string | human-readable codes shown in the UI |

All timestamps are Unix epoch **milliseconds**, and the app renders them in the viewer's local
timezone - epoch midnight UTC shows as "03:00" in Moscow. Use `withTime: false` for a date-only
deadline; convert the user's local wall clock for a timed one.

`assigned` and `checklists` are replaced wholesale on `PUT` too; only `stickers` merges. There is
no `parentId` field and no way to order tasks within a column.

`archived: true` does **not** hide a task from `GET /tasks` - filter it out yourself. Only
`deleted: true` hides it, and `includeDeleted=true` brings it back.

Examples:

```bash
# create
curl -s -X POST https://ru.yougile.com/api-v2/tasks \
  -H "Authorization: Bearer $YOUGILE_API_KEY" -H "Content-Type: application/json" \
  -d '{"title":"Починить логин","columnId":"<columnId>","description":"Падает на проде"}'

# move + complete
curl -s -X PUT https://ru.yougile.com/api-v2/tasks/<taskId> \
  -H "Authorization: Bearer $YOUGILE_API_KEY" -H "Content-Type: application/json" \
  -d '{"columnId":"<doneColumnId>","completed":true}'
```

## 7. Task chat (comments)

A task's discussion is a chat whose id equals the task id.

| Method | Path | Notes |
| --- | --- | --- |
| GET | `/chats/{taskId}/messages` | query: `limit`, `offset`, `includeSystem` |
| POST | `/chats/{taskId}/messages` | `{"text":"...", "textHtml"?: "...", "label"?: "..."}`; no way to @-mention - `properties`/`mentions` are rejected, `@Name` stays plain text |
| GET | `/chats/{taskId}/messages/{messageId}` | |
| PUT | `/chats/{taskId}/messages/{messageId}` | `{"deleted": true}`, `label`; **`text`/`textHtml` are rejected** - a sent message cannot be edited |

## 8. Users and departments

| Method | Path | Notes |
| --- | --- | --- |
| GET | `/users` | query: `email`, `projectId`, `limit`, `offset` |
| POST | `/users` | invite: `{"email","isAdmin"?}` |
| GET | `/users/{id}` | |
| PUT | `/users/{id}` | `{"isAdmin"?}` |
| DELETE | `/users/{id}` | removes the user from the company |
| GET/POST | `/departments` | `{"title","parentId"?,"users"?}` |
| GET/PUT | `/departments/{id}` | |

## 9. Chats, stickers, webhooks

| Method | Path | Notes |
| --- | --- | --- |
| GET/POST | `/group-chats` | group chats; `PUT /group-chats/{id}` to rename/archive |
| GET/POST | `/string-stickers` | text stickers, states inside `states[]` |
| GET/PUT | `/string-stickers/{id}` | |
| GET/POST | `/sprint-stickers` | sprint stickers; states carry `begin`/`end` |

Only these two sticker types exist in API v2 - `/stickers`, `/number-stickers`, `/text-stickers`,
`/date-stickers` and similar guesses all 404. Boards can carry badges of other types created in the
app: their ids show up in `board.stickers.custom` and their values on tasks (e.g. `"дашборд"`,
`"3"`), but `GET /string-stickers/{id}` answers 404 for them and the API cannot read, create or
edit them. A sticker is only visible on a board if that board enables it:
`PUT /boards/{id}` with `{"stickers":{"custom":{"<stickerId>":true}}}`.

Chat messages accept `textHtml` alongside `text`: the app renders the HTML, while `text` is the
plain fallback and is shown **verbatim** - HTML put into `text` appears as raw tags. Reading a message back returns only `text`, so formatting looks dropped when it is
not. `label` pins a short tag on the message.
| GET/POST | `/webhooks` | `{"url":"https://...","event":"task-*"}` - subscribe to events |
| PUT | `/webhooks/{id}` | enable/disable, change url |

Webhooks POST the event payload to your URL; useful events cover task created/updated/moved and
chat messages. Check the live docs for the current event list before relying on a specific name.

## 10. Pagination, limits, errors

List responses:

```json
{"content": [ ... ], "paging": {"count": 50, "limit": 50, "offset": 0, "next": true}}
```

Page with `limit` (default 50, max 1000) and `offset`; keep going while `paging.next` is true.

**Offset paging is unreliable.** Verified against a live column of 41 tasks: walking it with
`limit=5/10/20` returns one row twice and silently drops another, at every page size. A single
request with `limit=1000` returns the complete, correct list. So ask for one big page and only fall
back to `offset` past 1000 rows - and dedupe by `id` when you do. Above 1000 rows, prefer a
narrower filter (`columnId`, `assignedTo`, `title`) over paging.

Rate limit: about **50 requests per minute per company**. On `429` back off and retry; do not fan
out parallel requests.

| Status | Meaning | What to do |
| --- | --- | --- |
| 400 | bad body/params **or missing rights** | check the body first; `Not enough permissions` and `Не удалось создать колонку` are permission refusals wearing a 400 |
| 401 | missing/invalid key | re-run onboarding, get a fresh key |
| 403 | key valid, no rights | seen on `PUT /users/{id}`; most other refusals arrive as 400 or 404 |
| 404 | unknown path or id **or no access** | objects outside the account's permissions are hidden as 404, so do not tell the user the object does not exist |
| 429 | rate limited | wait and retry with backoff |
| 5xx | server side | retry once or twice, then report to the user |
