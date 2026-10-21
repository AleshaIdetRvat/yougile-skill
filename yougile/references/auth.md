# Getting and keeping access

Read this when `status` says there is no key, when a key is rejected, or when a call fails on
permissions. Everything else is in `api.md` (endpoints) and `recipes.md` (playbooks).

## Pick the path - do not make the user run commands

The user should never have to copy a command into a terminal: a `!` command in Claude Code cannot
read keyboard input, and switching terminals is friction. Choose in this order:

1. **The user already pasted login and password in the chat** → Path B now, no questions.
2. **Default - the web page** (Path A, any OS): tell the user in one line that a YouGile login page
   is about to open in their browser, then run it. Over ssh it works too, with a port forward.
3. If the page cannot reach the user, ask, offering a ready key (Path C) or login and password in
   the chat (Path B):

> Чтобы я мог работать с вашим YouGile, нужен доступ. Два варианта:
>
> **1. Логин и пароль от YouGile** прямо сюда — я сам получу API-ключ и сохраню только его;
> пароль нигде не сохраняется. Права администратора не нужны.
>
> **2. Готовый ключ** — YouGile → `Ctrl + ~` (или ⚙️ на странице проектов) → настройки API →
> создать ключ, и прислать его сюда.

Decide which profile the key goes to before running anything (see "Several accounts" below): the
user's own key on a machine where `default` already belongs to someone else (e.g. a bot) goes to
`--account <name>`.

## Path A - a login page in the browser (any OS)

```bash
python3 scripts/yougile.py login --web [--login user@example.com] [--account <name>]
```

- The script serves a one-time page on `127.0.0.1` (random port, random token in the address;
  without the token - `403`), opens it with the system browser and prints to stderr one JSON line:
  `{"waiting": true, "url": ..., "browserOpened": ...}`.
- The page asks for email and password (hidden field, sent by POST, never in the address). A wrong
  password is shown on the page and the user simply retries. With several companies the second
  step lists them - the password is not asked twice.
- Success: the key is saved into the profile (mode 600), the page says "Готово, можно вернуться к
  агенту", the server stops at once (a repeat visit gets `410`) and stdout gets the same JSON as any
  `login`. The page's "Отмена" or `--timeout` seconds (default 600) without an answer → exit `8`,
  nothing saved. Another person's key in the profile → exit `7`, the page says the key was not saved.
- **Run it as a background Bash command** (`run_in_background`) and end the turn with one line
  ("открыл страницу входа в YouGile, жду"): the command finishes by itself when the user is done or
  after the timeout, and its completion wakes the session. Then read its output.
- `"browserOpened": false` (Linux without a display, an ssh session, `--no-browser`): give the user
  the `url` and the `hint` from that line. Over ssh the user first runs
  `ssh -L <port>:127.0.0.1:<port> <host>` on their own computer, then opens the same address there.
- The password lives only in the script's memory for the duration of the flow: not in argv, not in
  the address, not in any output; responses carry `Cache-Control: no-store`.

### Fallback on macOS - system dialog

```bash
python3 scripts/yougile.py login --dialog [--login user@example.com] [--account <name>]
```

- System windows (`osascript`) ask for the login, the password (hidden) and, with several
  companies, the company. Cancel or 5 minutes without an answer → exit `8`, nothing sent.
  Answers reach the script over a pipe - not in `ps` output or in what the script prints.
- Run it in the foreground with a long timeout (up to 5 minutes). Not macOS → exit `3`.

## Path B - login and password sent in the chat

If the user typed them into the chat, that is their decision - use them at once. Do not refuse,
do not warn about chat safety, do not ask them to use a terminal instead.

```bash
python3 scripts/yougile.py login --login user@example.com --password-stdin [--account <name>] <<'PW'
<password exactly as sent>
PW
```

- The password goes on stdin via a quoted heredoc (`<<'PW'` - no `$`/backslash expansion), never
  as `--password` and never inside `echo`/`printf` arguments.
- Never repeat the password in replies, notes, commits or task text.
- After success: one line - "ключ сохранён; пароль был в переписке - при желании смените его в
  YouGile". Nothing more on the topic.

Common to A and B:

- One company → the key is issued, verified and saved in one step.
- Several companies without a dialog → exit `5` with `{"needsChoice": true, "companies": [...]}`.
  **Show the list and ask which company**, then re-run with `--company-id <id>` (the password from
  the chat may be reused for that re-run). `--pick-first` only when the user already said so.
- An existing key for that company is reused instead of minting a new one (cap: 30 per company).
  Pass `--new` to force a fresh one.
- The output names the key owner (`owner`) - confirm in one line ("подключился к «Acme» как
  Алексей, ключ в профиле lesha") and get on with the task.

## Path C - the user pastes a key

```bash
python3 scripts/yougile.py setup --key "<KEY>" [--account <name>]
```

`setup` verifies the key before writing anything, so a typo fails loudly instead of surfacing later
as a confusing 401.

## Several accounts

One profile = one person's key: `~/.yougile/credentials.json` is `default`, `~/.yougile/<name>.json`
is `<name>` (letters, digits, `-`, `_`). Select with `--account <name>` or `YOUGILE_ACCOUNT`;
`YOUGILE_CONFIG=<path>` still wins over both.

```bash
python3 scripts/yougile.py accounts            # profiles, cached owners, which one is selected
python3 scripts/yougile.py accounts --check    # ask YouGile whose each key is
python3 scripts/yougile.py --account lesha whoami
```

- YouGile shows the **key owner** as the author of every comment, card and edit. A bot that skips
  its own messages will skip a message sent with its key even if a human asked for it. So before
  writing on a person's behalf: `whoami` for the profile you will use.
- `login`/`setup` refuse (exit `7`) to replace a working key of a different person in a profile;
  save the new key under another `--account`, or `--force` only if the user asked to replace it.
- `whoami` caches the owner into the profile (`owner` field) so `accounts` and the `Posted as`
  note of `tasks comment` can name it without a request. Keys themselves are never printed.

## Who can issue a key

Verified against a live non-admin account, so do not repeat the folklore that this needs an admin:

- **Any member can issue a key.** A plain employee listed companies, called `/auth/keys`, got a
  working key. `isAdmin: false` is information, not a blocker - never refuse the flow because of
  it, and never send the user to their admin before trying.
- **A key carries its owner's permissions, not the company's.** The employee's key saw only the
  projects they belong to; a project they were not a member of answered `404` on every endpoint.
- **`/auth/keys/get` is per-user** - it returns only the keys that this login issued, so one
  employee cannot read another's key.
- Escalate to an admin only if `/auth/keys` itself answers 401/403.

## Handling the password

The password is a means to get the key, never a stored credential:

- Pass it via `--web`, `--dialog` or `--password-stdin` (or `YOUGILE_PASSWORD`); never `--password`.
- Never write it to a file, a note, a task description or a log, and do not repeat it back in chat.
- Keep it only for the login flow; do not stash it "for later" beyond the company-choice re-run.

On success the key lands in the selected profile file (mode 600). Resolution order is
`--key` → `YOUGILE_API_KEY` → the profile file (`YOUGILE_CONFIG` → `--account`/`YOUGILE_ACCOUNT`
→ `~/.yougile/credentials.json`). It is plain
JSON - convenient, not a secret store. `python3 scripts/yougile.py forget` deletes it.
Revoke a key for good with `request DELETE /auth/keys/<key>`.

## When permissions run out

YouGile does not answer `403` for most refusals. Two shapes to recognise, both verified live:

| What you see | What it usually means |
| --- | --- |
| `400` + `Not enough permissions` / `Не удалось создать колонку` | the call is fine, the account is not allowed to do it |
| `404` on an object the user insists exists | not missing - just outside this account's access |

**Do not retry, and do not tell the user the task does not exist.** Work out which case it is, then
say what is blocked and what would unblock it.

What a plain employee (non-admin) key could and could not do:

| Works | Blocked |
| --- | --- |
| read projects/boards/columns/tasks they belong to | anything in a project they are not a member of (`404`) |
| create, edit, move, complete, comment on tasks there | inviting or editing users (`400 Not enough permissions`; `403` on `PUT /users/{id}`) |
| create a project - **pass `--users '{"<ownId>":"admin"}'`** | adding a column to a board they only participate in (`400`) |
| list webhooks | |

Creating a project without `users` is a trap: the call succeeds, then the creator gets `404` on
their own project because it has no members. Only an admin can clean that up.

When the wall is real, hand the user something actionable instead of an error dump:

> Здесь не хватает прав: ваш аккаунт — участник компании, но не администратор, а эта операция
> (например, добавить пользователя или колонку на чужой доске) доступна только администратору.
> Варианты: попросите администратора компании сделать это, либо выдать вам права администратора,
> либо добавить вас в нужный проект — после этого я всё сделаю сам. Список администраторов видно в
> YouGile: значок ⚙️ рядом с названием компании → «Сотрудники».
