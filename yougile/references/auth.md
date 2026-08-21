# Getting and keeping access

Read this when `status` says there is no key, when a key is rejected, or when a call fails on
permissions. Everything else is in `api.md` (endpoints) and `recipes.md` (playbooks).

## Ask the user first

Offer both paths and wait for a reply. Do not start the task without a key.

> Чтобы я мог работать с вашим YouGile, нужен доступ. Два варианта:
>
> **1. Логин и пароль от YouGile** — я сам получу API-ключ: покажу список ваших компаний, вы
> выберете нужную, дальше я создам ключ и сохраню только его. Пароль нигде не сохраняется.
> Права администратора не нужны — подойдёт обычный аккаунт сотрудника.
>
> **2. Готовый ключ** — если не хотите передавать пароль: откройте YouGile → `Ctrl + ~` (или
> шестерёнку ⚙️ на странице проектов) → настройки API → создайте ключ. Либо через
> https://ru.yougile.com/api-v2 → раздел **Auth key** → `POST /auth/companies` → `POST /auth/keys`.
> Пришлите ключ сюда.
>
> Ключ работает от вашего имени и открывает ровно то, что видите вы сами — не больше. Всё равно
> не публикуйте его в общих чатах и репозиториях: при утечке ключ можно отозвать и выпустить новый.

## Path A - login and password

```bash
# password on stdin keeps it out of shell history and `ps` output
printf '%s' "$PASSWORD" | python3 scripts/yougile.py login --login user@example.com --password-stdin
```

- One company → the key is issued, verified and saved in one step.
- Several companies → the command exits `5` with `{"needsChoice": true, "companies": [...]}` and
  does nothing else. **Show that list and ask which company**, then re-run with `--company-id <id>`.
  Do not pick for them; `--pick-first` is only for unattended runs where the user already said so.
- An existing key for that company is reused instead of minting a new one (cap: 30 per company).
  Pass `--new` to force a fresh one.
- Confirm in one line ("подключился к компании «Acme», ключ сохранён") and get on with the task.

## Path B - the user pastes a key

```bash
python3 scripts/yougile.py setup --key "<KEY>"
```

`setup` verifies the key before writing anything, so a typo fails loudly instead of surfacing later
as a confusing 401.

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

- Pass it via `--password-stdin` (or `YOUGILE_PASSWORD`); avoid `--password` on shared machines.
- Never write it to a file, a note, a task description or a log, and do not repeat it back in chat.
- Keep it only for the login flow. If the flow needs a re-run (company choice), ask the user to
  resend it - do not stash it "for later".

On success the key lands in `~/.yougile/credentials.json` (mode 600). Resolution order is
`--key` → `YOUGILE_API_KEY` → that file (override the path with `YOUGILE_CONFIG`). It is plain
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
