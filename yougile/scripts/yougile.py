#!/usr/bin/env python3
"""
yougile.py - dependency-free CLI helper for the YouGile REST API v2.

Works with any Python 3.8+ (stdlib only). Every command prints JSON to stdout.
Errors go to stderr and produce a non-zero exit code.

Credentials resolution order:
  1. --key argument
  2. YOUGILE_API_KEY environment variable
  3. config file: YOUGILE_CONFIG if set, else the profile from --account / YOUGILE_ACCOUNT
     (~/.yougile/<name>.json), else the default ~/.yougile/credentials.json

Run `python3 yougile.py --help` for a command list.
"""

import argparse
import glob
import json
import os
import html
import secrets
import subprocess
import sys
import threading
import time
import webbrowser
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import urllib.error
import urllib.parse
import urllib.request

DEFAULT_BASE_URL = "https://ru.yougile.com/api-v2"
CONFIG_DIR = os.path.join(os.path.expanduser("~"), ".yougile")
DEFAULT_PROFILE = "credentials"  # ~/.yougile/credentials.json - the key used when no account is named
CONFIG_PATH = os.environ.get("YOUGILE_CONFIG", os.path.join(CONFIG_DIR, DEFAULT_PROFILE + ".json"))
KEY_URL = "https://ru.yougile.com/api-v2"  # page where a human can mint a key


# --------------------------------------------------------------------------- config


def load_config():
    try:
        with open(CONFIG_PATH, "r", encoding="utf-8") as fh:
            return json.load(fh)
    except (OSError, ValueError):
        return {}


def save_config(cfg):
    directory = os.path.dirname(CONFIG_PATH)
    if directory:
        os.makedirs(directory, exist_ok=True)
    with open(CONFIG_PATH, "w", encoding="utf-8") as fh:
        json.dump(cfg, fh, ensure_ascii=False, indent=2)
    try:
        os.chmod(CONFIG_PATH, 0o600)
    except OSError:
        pass
    return CONFIG_PATH


def select_profile(args):
    """Point CONFIG_PATH at the named account's file: --account wins over YOUGILE_ACCOUNT.

    An explicit YOUGILE_CONFIG path wins over both. Names are plain words, so a profile can
    never point outside ~/.yougile.
    """
    global CONFIG_PATH
    name = getattr(args, "account", None) or os.environ.get("YOUGILE_ACCOUNT")
    if os.environ.get("YOUGILE_CONFIG") or not name:
        return
    if not name.replace("-", "").replace("_", "").isalnum():
        die("Account name must be letters, digits, '-' or '_': %s" % name)
    CONFIG_PATH = os.path.join(CONFIG_DIR, name + ".json")


def profile_name(path=None):
    path = path or CONFIG_PATH
    base = os.path.basename(path)
    if os.path.dirname(os.path.abspath(path)) != os.path.abspath(CONFIG_DIR) or not base.endswith(".json"):
        return path
    name = base[:-5]
    return "default" if name == DEFAULT_PROFILE else name


def fetch_me(base_url, key):
    """Owner of a key: GET /users/me -> {id, realName, email}."""
    me = call("GET", "/users/me", base_url, key=key)
    return {"id": me.get("id"), "name": me.get("realName"), "email": me.get("email")}


def resolve_key(args):
    if getattr(args, "key", None):
        return args.key
    if os.environ.get("YOUGILE_API_KEY"):
        return os.environ["YOUGILE_API_KEY"]
    return load_config().get("apiKey")


def resolve_base_url(args):
    if getattr(args, "base_url", None):
        return args.base_url.rstrip("/")
    if os.environ.get("YOUGILE_BASE_URL"):
        return os.environ["YOUGILE_BASE_URL"].rstrip("/")
    return load_config().get("baseUrl", DEFAULT_BASE_URL).rstrip("/")


def mask(key):
    if not key:
        return None
    return key[:4] + "..." + key[-4:] if len(key) > 10 else "***"


# --------------------------------------------------------------------------- http


class ApiError(Exception):
    def __init__(self, status, body, url):
        self.status = status
        self.body = body
        self.url = url
        super().__init__("HTTP %s on %s: %s" % (status, url, body))


def call(method, path, base_url, key=None, query=None, body=None, timeout=60, retries=3, raw=None):
    """Perform one API call. `path` is relative to base_url, e.g. '/tasks'."""
    url = base_url.rstrip("/") + "/" + path.lstrip("/")
    if query:
        clean = {k: v for k, v in query.items() if v is not None}
        if clean:
            url += "?" + urllib.parse.urlencode(clean, doseq=True)

    data = None
    headers = {"Accept": "application/json"}
    if body is not None:
        data = json.dumps(body, ensure_ascii=False).encode("utf-8")
        headers["Content-Type"] = "application/json"
    if raw is not None:  # (bytes, content type) - e.g. a multipart upload
        data, headers["Content-Type"] = raw
    if key:
        headers["Authorization"] = "Bearer " + key

    last_error = None
    for attempt in range(retries):
        req = urllib.request.Request(url, data=data, headers=headers, method=method.upper())
        try:
            with urllib.request.urlopen(req, timeout=timeout) as resp:
                raw = resp.read().decode("utf-8") or "{}"
                try:
                    return json.loads(raw)
                except ValueError:
                    return {"raw": raw}
        except urllib.error.HTTPError as exc:
            raw = exc.read().decode("utf-8", "replace")
            # 429 = rate limit (YouGile allows ~50 requests/minute per company)
            if exc.code == 429 and attempt < retries - 1:
                time.sleep(2 ** attempt * 3)
                last_error = ApiError(exc.code, raw, url)
                continue
            raise ApiError(exc.code, raw, url)
        except urllib.error.URLError as exc:
            if attempt < retries - 1:
                time.sleep(1 + attempt)
                last_error = exc
                continue
            raise
    raise last_error


def api(args, method, path, query=None, body=None, raw=None):
    key = resolve_key(args)
    if not key:
        die(
            "No API key found. Run the setup flow first: "
            "`python3 yougile.py setup --key <KEY>` or set YOUGILE_API_KEY.",
            code=3,
        )
    return call(method, path, resolve_base_url(args), key=key, query=query, body=body, raw=raw)


def upload_file(args, path):
    """Upload a file to YouGile; returns its relative url (`/user-data/<id>/<name>`)."""
    if not os.path.isfile(path):
        die("No such file: %s" % path)
    name = os.path.basename(path)
    boundary = "----yougile%d" % int(time.time() * 1000)
    with open(path, "rb") as f:
        content = f.read()
    data = (
        ("--%s\r\nContent-Disposition: form-data; name=\"file\"; filename=\"%s\"\r\n"
         "Content-Type: application/octet-stream\r\n\r\n" % (boundary, name)).encode("utf-8")
        + content
        + ("\r\n--%s--\r\n" % boundary).encode("utf-8")
    )
    url = api(args, "POST", "/upload-file", raw=(data, "multipart/form-data; boundary=" + boundary))["url"]
    return "/" + url.split("://", 1)[1].split("/", 1)[1] if "://" in url else url


MAX_LIMIT = 1000  # server caps a page at 1000 rows


def paginate(args, path, query, fetch_all, limit):
    """List rows, following pagination when fetch_all is set.

    YouGile's offset pagination is not reliable: walking a list in small pages
    returns one row twice and silently drops another. So `--all` asks for the
    largest page the server allows (1000) and only falls back to offset walking
    beyond that, deduplicating by id on the way.
    """
    query = dict(query or {})
    limit = limit or (MAX_LIMIT if fetch_all else 50)
    query["limit"] = limit
    offset = 0
    items = []
    seen = set()
    while True:
        query["offset"] = offset
        page = api(args, "GET", path, query=query)
        if isinstance(page, list):
            page = {"content": page, "paging": {"count": len(page), "next": False}}
        chunk = page.get("content") or []
        paging = page.get("paging") or {}
        if not fetch_all:
            return page
        for row in chunk:
            key = row.get("id") if isinstance(row, dict) else None
            if key is not None:
                if key in seen:
                    continue
                seen.add(key)
            items.append(row)
        if not paging.get("next") or not chunk:
            break
        offset += len(chunk)
    return {"content": items, "paging": {"count": len(items), "limit": limit, "offset": 0}}


# --------------------------------------------------------------------------- output


def out(obj):
    print(json.dumps(obj, ensure_ascii=False, indent=2))


def die(message, code=1, hint=None):
    payload = {"ok": False, "error": message}
    if hint:
        payload["hint"] = hint
    print(json.dumps(payload, ensure_ascii=False, indent=2), file=sys.stderr)
    sys.exit(code)


def parse_json_arg(value, flag):
    if value is None:
        return None
    try:
        return json.loads(value)
    except ValueError as exc:
        die("%s must be valid JSON: %s" % (flag, exc))


def kv_pairs(pairs):
    result = {}
    for item in pairs or []:
        if "=" not in item:
            die("Expected key=value, got: %s" % item)
        k, v = item.split("=", 1)
        result[k] = v
    return result


# --------------------------------------------------------------------------- auth commands


def cmd_status(args):
    cfg = load_config()
    key = resolve_key(args)
    info = {
        "hasKey": bool(key),
        "keyPreview": mask(key),
        "source": (
            "--key" if getattr(args, "key", None)
            else "env:YOUGILE_API_KEY" if os.environ.get("YOUGILE_API_KEY")
            else "config" if cfg.get("apiKey") else None
        ),
        "account": profile_name(),
        "configPath": CONFIG_PATH,
        "baseUrl": resolve_base_url(args),
        "companyId": cfg.get("companyId"),
    }
    if cfg.get("owner") and info["source"] == "config":
        info["owner"] = cfg["owner"]
    if not key:
        info["nextStep"] = "Ask the user for an API key (see SKILL.md onboarding), then run `setup --key <KEY>`."
        out(info)
        return
    try:
        probe = call("GET", "/users", resolve_base_url(args), key=key, query={"limit": 1})
        info["ok"] = True
        info["probe"] = {"users": (probe.get("paging") or {}).get("count")}
    except ApiError as exc:
        info["ok"] = False
        info["status"] = exc.status
        info["error"] = exc.body
        if exc.status in (401, 403):
            info["nextStep"] = "Key rejected. Ask the user for a fresh key and re-run setup."
    out(info)


def store_key(base_url, key, company_id=None, force=False):
    """Verify a key, then save it with its owner into the selected profile.

    Refuses to replace a working key of another person: a profile is one person's key, and
    overwriting it silently is how messages end up sent under the wrong name.
    """
    try:
        owner = fetch_me(base_url, key)
    except ApiError as exc:
        die(
            "Key rejected by YouGile (HTTP %s). Nothing was saved." % exc.status,
            code=4,
            hint="Ask the user to re-copy the key from %s" % KEY_URL,
        )
    cfg = load_config()
    old_key = cfg.get("apiKey")
    if old_key and old_key != key and not force:
        try:
            old_owner = fetch_me(cfg.get("baseUrl", base_url), old_key)
        except (ApiError, urllib.error.URLError):
            old_owner = None  # a dead key is fine to replace
        if old_owner and old_owner.get("id") != owner.get("id"):
            die(
                "Profile '%s' already holds a working key of %s; the new key belongs to %s. "
                "Nothing was saved." % (profile_name(), old_owner.get("name"), owner.get("name")),
                code=7,
                hint="Save it under its own name with --account <name>, or pass --force if the "
                "user asked to replace that key.",
            )
    cfg.update({"apiKey": key, "baseUrl": base_url, "owner": owner, "savedAt": int(time.time())})
    if company_id:
        cfg["companyId"] = company_id
    return save_config(cfg), owner


def cmd_setup(args):
    key = args.key
    base_url = resolve_base_url(args)
    path, owner = store_key(base_url, key, args.company_id, args.force)
    out({"ok": True, "account": profile_name(), "saved": path, "owner": owner,
         "keyPreview": mask(key), "baseUrl": base_url})


def read_password(args):
    """Password from --password, --password-stdin, or YOUGILE_PASSWORD.

    Prefer stdin/env: an argument is visible in shell history and in `ps` output.
    The password is used for this call only and is never written to the config file.
    """
    if getattr(args, "dialog", False):
        return ask_dialog("Пароль от YouGile для %s" % args.login, hidden=True)
    if getattr(args, "password_stdin", False):
        pw = sys.stdin.readline().rstrip("\n")
        if pw:
            return pw
    if getattr(args, "password", None):
        return args.password
    if os.environ.get("YOUGILE_PASSWORD"):
        return os.environ["YOUGILE_PASSWORD"]
    die("No password given: use --password, --password-stdin or YOUGILE_PASSWORD.", code=3)


def osa_quote(text):
    return '"' + text.replace("\\", "\\\\").replace('"', '\\"') + '"'


def run_osascript(script):
    """Run AppleScript and return what it printed. The answer comes back over a pipe, so it
    never shows up in argv, the terminal or the agent's tool output."""
    if sys.platform != "darwin":
        die(
            "--dialog needs macOS (a system dialog via osascript).",
            code=3,
            hint="Without a GUI pass the password on stdin: login --login <email> --password-stdin, "
            "or ask the user for a ready key and run setup --key.",
        )
    try:
        res = subprocess.run(["osascript", "-e", script], stdin=subprocess.DEVNULL,
                             stdout=subprocess.PIPE, stderr=subprocess.PIPE, timeout=300)
    except subprocess.TimeoutExpired:
        die("Nobody answered the dialog within 5 minutes. Nothing was sent.", code=8)
    if res.returncode != 0:
        if b"-128" in res.stderr:  # the user pressed Cancel
            die("The user cancelled the dialog. Nothing was sent.", code=8)
        die("osascript failed: %s" % res.stderr.decode("utf-8", "replace").strip(), code=8)
    return res.stdout.decode("utf-8").rstrip("\n")


def ask_dialog(prompt, default="", hidden=False):
    answer = run_osascript(
        'text returned of (display dialog %s default answer %s%s with title "YouGile" '
        'buttons {"Отмена", "OK"} default button "OK" cancel button "Отмена" with icon note)'
        % (osa_quote(prompt), osa_quote(default), " with hidden answer" if hidden else "")
    )
    if not answer:
        die("Empty answer in the dialog. Nothing was sent.", code=8)
    return answer


def choose_dialog(prompt, names):
    picked = run_osascript(
        'set r to choose from list {%s} with title "YouGile" with prompt %s '
        'OK button name "Выбрать" cancel button name "Отмена"\n'
        "if r is false then error number -128\nreturn item 1 of r"
        % (", ".join(osa_quote(n) for n in names), osa_quote(prompt))
    )
    return names.index(picked)


def cmd_companies(args):
    base_url = resolve_base_url(args)
    body = {"login": args.login, "password": read_password(args)}
    if args.company_name:
        body["name"] = args.company_name
    out(call("POST", "/auth/companies", base_url, body=body))


def existing_key(base_url, creds, company_id):
    """Return a live key already issued for this company, if there is one.

    A company is capped at 30 keys, so reusing beats minting a new one on every run.
    """
    try:
        payload = dict(creds)
        payload["companyId"] = company_id
        found = call("POST", "/auth/keys/get", base_url, body=payload)
    except ApiError:
        return None
    items = found if isinstance(found, list) else (found.get("content") or [])
    for item in items:
        if isinstance(item, dict) and item.get("key") and not item.get("deleted"):
            if not item.get("companyId") or item.get("companyId") == company_id:
                return item["key"]
    return None


def cmd_login(args):
    """Full flow: pick company -> reuse or create key -> verify -> save the key only."""
    base_url = resolve_base_url(args)
    if args.web:
        return web_login(args, base_url)
    if not args.login:
        if not args.dialog:
            die("Pass --login <email>, or --dialog to ask the user in a system dialog.", code=3)
        args.login = ask_dialog("Логин (почта) в YouGile")
    creds = {"login": args.login, "password": read_password(args)}

    company_id = args.company_id
    if not company_id:
        companies = call("POST", "/auth/companies", base_url, body=creds)
        items = companies.get("content", [])
        if not items:
            die("No companies found for this account.", code=4)
        if len(items) > 1 and args.dialog and not args.pick_first:
            # the user picks the company in a dialog too, so the password is not asked twice
            names = ["%s (%s)" % (c.get("name"), (c.get("id") or "")[:8]) for c in items]
            items = [items[choose_dialog("Какую компанию YouGile подключить?", names)]]
        if len(items) > 1 and not args.pick_first:
            out({
                "ok": False,
                "needsChoice": True,
                "companies": [
                    {"id": c.get("id"), "name": c.get("name"), "isAdmin": c.get("isAdmin")}
                    for c in items
                ],
                "hint": "Ask the user which company to use, then re-run with --company-id <id>.",
            })
            sys.exit(5)
        company_id = items[0]["id"]

    out(obtain_and_store(base_url, creds, company_id, args))


def obtain_and_store(base_url, creds, company_id, args):
    """Reuse or mint a key for the company, verify it and save it; returns the result to print."""
    key, reused = None, False
    if not args.new:
        key = existing_key(base_url, creds, company_id)
        reused = bool(key)
    if not key:
        payload = dict(creds)
        payload["companyId"] = company_id
        created = call("POST", "/auth/keys", base_url, body=payload)
        key = created.get("key") or (created.get("content") or {}).get("key")
    if not key:
        die("Could not obtain a key for company %s." % company_id, code=4)

    path, owner = store_key(base_url, key, company_id, args.force)
    return {
        "ok": True,
        "account": profile_name(),
        "saved": path,
        "owner": owner,
        "companyId": company_id,
        "reusedExistingKey": reused,
        "keyPreview": mask(key),
        "note": "Key stored; password was not saved. The key is bound to this company.",
    }


# --------------------------------------------------------------------------- web login

WEB_CSS = """
:root { color-scheme: light dark; --bg: #f6f7f9; --card: #fff; --fg: #1d2330; --muted: #667085;
  --line: #d0d5dd; --accent: #2f6fed; --err-bg: #fdecec; --err-fg: #b42318; --ok: #12805c; }
@media (prefers-color-scheme: dark) { :root { --bg: #14161b; --card: #1d2027; --fg: #e8eaee;
  --muted: #98a2b3; --line: #3a3f4b; --accent: #5b8cff; --err-bg: #3a1d1d; --err-fg: #ffb4ab; --ok: #4fd1a5; } }
* { box-sizing: border-box; }
body { margin: 0; min-height: 100vh; display: flex; align-items: center; justify-content: center;
  padding: 16px; background: var(--bg); color: var(--fg); font: 16px/1.45 system-ui, -apple-system,
  "Segoe UI", Roboto, sans-serif; }
main { width: 100%; max-width: 400px; background: var(--card); border: 1px solid var(--line);
  border-radius: 12px; padding: 28px 24px; }
h1 { font-size: 20px; margin: 0 0 6px; }
p { margin: 0 0 16px; color: var(--muted); overflow-wrap: anywhere; }
label { display: block; font-size: 14px; margin: 14px 0 6px; }
input[type=email], input[type=password] { width: 100%; padding: 10px 12px; font: inherit;
  color: inherit; background: transparent; border: 1px solid var(--line); border-radius: 8px; }
.company { display: flex; gap: 10px; align-items: center; padding: 10px 12px; margin: 8px 0;
  border: 1px solid var(--line); border-radius: 8px; cursor: pointer; }
.row { display: flex; gap: 10px; margin-top: 20px; }
button { flex: 1; padding: 10px 14px; font: inherit; border-radius: 8px; cursor: pointer;
  border: 1px solid var(--accent); background: var(--accent); color: #fff; }
button.ghost { flex: 0 0 auto; background: transparent; color: var(--muted); border-color: var(--line); }
.error { background: var(--err-bg); color: var(--err-fg); border-radius: 8px; padding: 10px 12px;
  margin: 0 0 12px; font-size: 14px; }
.done h1 { color: var(--ok); }
"""


def web_page(title, body, extra_class=""):
    return (
        '<!doctype html><html lang="ru"><head><meta charset="utf-8">'
        '<meta name="viewport" content="width=device-width, initial-scale=1">'
        "<title>%s</title><style>%s</style></head><body><main class=\"%s\">%s</main></body></html>"
        % (html.escape(title), WEB_CSS, extra_class, body)
    ).encode("utf-8")


class WebLogin:
    """State of one `login --web` run. The password lives only here, in memory, and only until
    the key is saved, the user cancels or the server stops."""

    def __init__(self, args, base_url):
        self.args, self.base_url = args, base_url
        self.token = secrets.token_urlsafe(24)
        self.lock = threading.Lock()
        self.done = threading.Event()
        self.finished = False
        self.result = None   # printed on success
        self.fail = None     # (exit code, message or None when die() already printed it)
        self.pending = None  # between the password and the company choice: {step, creds, companies}

    def action(self, name):
        return "/%s/%s" % (self.token, name)

    def form(self, login="", error=None):
        err = '<div class="error">%s</div>' % html.escape(error) if error else ""
        return web_page("Вход в YouGile", (
            "<h1>Вход в YouGile</h1>"
            "<p>Агент получит API-ключ и сохранит в профиль «%s» только его. Пароль никуда "
            "не записывается.</p>%s"
            '<form method="post" action="%s">'
            '<label for="login">Почта</label>'
            '<input id="login" name="login" type="email" autocomplete="username" required value="%s"%s>'
            '<label for="password">Пароль</label>'
            '<input id="password" name="password" type="password" autocomplete="current-password" required%s>'
            '<div class="row"><button type="submit">Войти</button>'
            '<button class="ghost" type="submit" formaction="%s" formnovalidate>Отмена</button></div>'
            "</form>"
        ) % (html.escape(profile_name()), err, self.action("login"), html.escape(login),
             "" if login else " autofocus", " autofocus" if login else "", self.action("cancel")))

    def choice(self):
        rows = "".join(
            '<label class="company"><input type="radio" name="company" value="%d" required%s>'
            "<span>%s</span></label>"
            % (i, " checked" if i == 0 else "", html.escape(c.get("name") or c.get("id") or "?"))
            for i, c in enumerate(self.pending["companies"])
        )
        return web_page("Выбор компании", (
            "<h1>Какую компанию подключить?</h1>"
            "<p>У этой почты несколько компаний в YouGile. Ключ привязывается к одной.</p>"
            '<form method="post" action="%s"><input type="hidden" name="step" value="%s">%s'
            '<div class="row"><button type="submit">Подключить</button>'
            '<button class="ghost" type="submit" formaction="%s" formnovalidate>Отмена</button></div>'
            "</form>"
        ) % (self.action("company"), self.pending["step"], rows, self.action("cancel")))

    def finish(self, result=None, fail=None):
        self.finished, self.result, self.fail, self.pending = True, result, fail, None

    def submit_login(self, fields):
        login = fields.get("login", "").strip()
        creds = {"login": login, "password": fields.get("password", "")}
        if not login or not creds["password"]:
            return self.form(login, "Введите почту и пароль.")
        company_id = self.args.company_id
        if not company_id:
            try:
                items = call("POST", "/auth/companies", self.base_url, body=creds).get("content") or []
            except ApiError as exc:
                if exc.status in (400, 401, 403, 404):
                    return self.form(login, "YouGile не принял почту или пароль (HTTP %s). "
                                     "Проверьте и попробуйте ещё раз." % exc.status)
                return self.form(login, "YouGile ответил ошибкой HTTP %s. Попробуйте ещё раз." % exc.status)
            except (urllib.error.URLError, OSError):
                return self.form(login, "Нет связи с YouGile. Попробуйте ещё раз.")
            if not items:
                return self.form(login, "У этой почты нет компаний в YouGile.")
            if len(items) > 1 and not self.args.pick_first:
                self.pending = {"step": secrets.token_urlsafe(12), "creds": creds, "companies": items}
                return self.choice()
            company_id = items[0]["id"]
        return self.issue(creds, company_id)

    def submit_company(self, fields):
        if not self.pending or fields.get("step") != self.pending["step"]:
            return self.form("", "Шаг устарел — войдите заново.")
        try:
            company = self.pending["companies"][int(fields.get("company", ""))]
        except (ValueError, IndexError):
            return self.choice()
        return self.issue(self.pending["creds"], company["id"])

    def issue(self, creds, company_id):
        try:
            result = obtain_and_store(self.base_url, creds, company_id, self.args)
        except ApiError as exc:
            self.pending = None
            return self.form(creds["login"], "YouGile не выдал ключ (HTTP %s). Попробуйте ещё раз."
                             % exc.status)
        except (urllib.error.URLError, OSError):
            self.pending = None
            return self.form(creds["login"], "Нет связи с YouGile. Попробуйте ещё раз.")
        except SystemExit as exc:  # die() already printed why (another person's key, key rejected)
            self.finish(fail=(exc.code, None))
            return web_page("Ключ не сохранён", (
                "<h1>Ключ не сохранён</h1><p>Причина у агента. Вернитесь к нему — "
                "эту вкладку можно закрыть.</p>"))
        self.finish(result=result)
        owner = (result.get("owner") or {}).get("name") or ""
        return web_page("Готово", (
            "<h1>Готово</h1><p>Ключ сохранён в профиль «%s»%s. Можно закрыть вкладку и "
            "вернуться к агенту.</p>"
        ) % (html.escape(profile_name()), " (владелец — %s)" % html.escape(owner) if owner else ""),
            "done")

    def cancel(self):
        self.finish(fail=(8, "The user cancelled on the login page. Nothing was saved."))
        return web_page("Отменено", "<h1>Отменено</h1><p>Ничего не сохранено. Можно вернуться к агенту.</p>")


def web_handler(flow, port):
    hosts = {"127.0.0.1:%d" % port, "localhost:%d" % port}

    class Handler(BaseHTTPRequestHandler):
        server_version = "yougile-login"
        sys_version = ""

        def log_message(self, *a):  # request lines carry the token; nothing goes to the log
            pass

        def send(self, code, body=b""):
            self.send_response(code)
            self.send_header("Content-Type", "text/html; charset=utf-8")
            self.send_header("Content-Length", str(len(body)))
            self.send_header("Cache-Control", "no-store")
            self.send_header("Pragma", "no-cache")
            self.send_header("Referrer-Policy", "no-referrer")
            self.send_header("X-Content-Type-Options", "nosniff")
            self.send_header("Content-Security-Policy", "default-src 'none'; style-src 'unsafe-inline'; "
                             "form-action 'self'; base-uri 'none'; frame-ancestors 'none'")
            self.send_header("Connection", "close")
            self.end_headers()
            self.wfile.write(body)
            self.wfile.flush()
            self.close_connection = True

        def route(self):
            """The step name after the token, or an HTTP error code."""
            if self.headers.get("Host") not in hosts:  # a page on another site rebinding a name to us
                return 403
            parts = self.path.split("?", 1)[0].split("/")
            if len(parts) < 2 or not secrets.compare_digest(parts[1], flow.token):
                return 403
            if flow.finished:
                return 410
            return "/".join(parts[2:])

        def do_GET(self):
            with flow.lock:
                step = self.route()
                if isinstance(step, int):
                    return self.send(step, web_page("Нет доступа", "<h1>Ссылка не действует</h1>"))
                if step != "":
                    return self.send(404, web_page("Нет такой страницы", "<h1>Нет такой страницы</h1>"))
                self.send(200, flow.form(flow.args.login or ""))

        def do_POST(self):
            with flow.lock:
                step = self.route()
                if isinstance(step, int):
                    return self.send(step, web_page("Нет доступа", "<h1>Ссылка не действует</h1>"))
                try:
                    size = int(self.headers.get("Content-Length") or 0)
                except ValueError:
                    size = -1
                if not 0 <= size <= 65536:
                    return self.send(413)
                raw = self.rfile.read(size).decode("utf-8", "replace")
                fields = {k: v[0] for k, v in urllib.parse.parse_qs(raw, keep_blank_values=True).items()}
                if step == "login":
                    page = flow.submit_login(fields)
                elif step == "company":
                    page = flow.submit_company(fields)
                elif step == "cancel":
                    page = flow.cancel()
                else:
                    return self.send(404, web_page("Нет такой страницы", "<h1>Нет такой страницы</h1>"))
                self.send(200, page)
            if flow.finished:
                flow.done.set()

    return Handler


def web_login(args, base_url):
    """Serve a one-time login page on 127.0.0.1 and wait until the user finishes it."""
    flow = WebLogin(args, base_url)
    server = ThreadingHTTPServer(("127.0.0.1", 0), None)
    port = server.server_address[1]
    server.RequestHandlerClass = web_handler(flow, port)
    server.daemon_threads = True
    threading.Thread(target=server.serve_forever, daemon=True).start()

    url = "http://127.0.0.1:%d/%s/" % (port, flow.token)
    headless = bool(os.environ.get("SSH_CONNECTION")) or (
        sys.platform.startswith("linux") and not (os.environ.get("DISPLAY") or os.environ.get("WAYLAND_DISPLAY")))
    opened = False
    if not args.no_browser and not headless:
        try:
            opened = webbrowser.open(url)
        except Exception:
            opened = False
    info = {"waiting": True, "url": url, "browserOpened": opened, "timeoutSec": args.timeout,
            "account": profile_name()}
    if not opened:
        info["hint"] = ("Give the user this address. On this machine: open it in a browser. Over ssh: "
                        "first run `ssh -L %d:127.0.0.1:%d <this host>` on their computer, then open "
                        "the same address there." % (port, port))
    print(json.dumps(info, ensure_ascii=False), file=sys.stderr, flush=True)

    finished = flow.done.wait(args.timeout)
    if finished and args.linger:
        time.sleep(args.linger)
    server.shutdown()
    server.server_close()
    if not finished:
        die("Nobody finished the login page within %d seconds. Nothing was saved." % args.timeout, code=8)
    if flow.fail:
        code, message = flow.fail
        if message:
            die(message, code=code)
        sys.exit(code)
    out(flow.result)


def cmd_keys_list(args):
    base_url = resolve_base_url(args)
    body = {"login": args.login, "password": read_password(args)}
    if args.company_id:
        body["companyId"] = args.company_id
    out(call("POST", "/auth/keys/get", base_url, body=body))


def cmd_forget(args):
    cfg = load_config()
    cfg.pop("apiKey", None)
    cfg.pop("companyId", None)
    save_config(cfg)
    out({"ok": True, "removed": "apiKey", "account": profile_name(), "configPath": CONFIG_PATH})


def cmd_whoami(args):
    """Whose key is this: the name and id YouGile shows as the author of everything it sends."""
    key = resolve_key(args)
    if not key:
        die("No API key in profile '%s' (%s)." % (profile_name(), CONFIG_PATH), code=3)
    owner = fetch_me(resolve_base_url(args), key)
    source = ("--key" if getattr(args, "key", None)
              else "env:YOUGILE_API_KEY" if os.environ.get("YOUGILE_API_KEY") else "config")
    if source == "config":
        cfg = load_config()
        if cfg.get("owner") != owner:  # remembered so `accounts` and comments can name the owner offline
            cfg["owner"] = owner
            save_config(cfg)
    out({"account": profile_name() if source == "config" else None, "source": source,
         "configPath": CONFIG_PATH if source == "config" else None, "owner": owner})


def cmd_accounts(args):
    """Every key file in ~/.yougile; --check asks YouGile whose each key is."""
    rows = []
    for path in sorted(glob.glob(os.path.join(CONFIG_DIR, "*.json"))):
        try:
            with open(path, "r", encoding="utf-8") as fh:
                cfg = json.load(fh)
        except (OSError, ValueError):
            continue
        if not isinstance(cfg, dict) or not cfg.get("apiKey"):
            continue
        row = {"account": profile_name(path), "configPath": path,
               "companyId": cfg.get("companyId"), "owner": cfg.get("owner"),
               "selected": os.path.abspath(path) == os.path.abspath(CONFIG_PATH)}
        if args.check:
            try:
                row["owner"] = fetch_me(cfg.get("baseUrl", DEFAULT_BASE_URL), cfg["apiKey"])
                row["ok"] = True
            except ApiError as exc:
                row["ok"], row["status"] = False, exc.status
        rows.append(row)
    rows.sort(key=lambda r: (r["account"] != "default", r["account"]))
    out({"accounts": rows,
         "hint": "Pick one with --account <name> (or YOUGILE_ACCOUNT); without it 'default' is used."})


# --------------------------------------------------------------------------- resource commands


def cmd_projects(args):
    if args.action == "list":
        out(paginate(args, "/projects", {"title": args.title}, args.all, args.limit))
    elif args.action == "get":
        out(api(args, "GET", "/projects/%s" % args.id))
    elif args.action == "create":
        body = {"title": args.title}
        if args.users:
            body["users"] = parse_json_arg(args.users, "--users")
        body.update(parse_json_arg(args.json, "--json") or {})
        out(api(args, "POST", "/projects", body=body))
    elif args.action == "update":
        body = parse_json_arg(args.json, "--json") or {}
        if args.title:
            body["title"] = args.title
        if args.deleted is not None:
            body["deleted"] = args.deleted
        out(api(args, "PUT", "/projects/%s" % args.id, body=body))


def cmd_boards(args):
    if args.action == "list":
        out(paginate(args, "/boards", {"title": args.title, "projectId": args.project_id}, args.all, args.limit))
    elif args.action == "get":
        out(api(args, "GET", "/boards/%s" % args.id))
    elif args.action == "create":
        body = {"title": args.title, "projectId": args.project_id}
        body.update(parse_json_arg(args.json, "--json") or {})
        out(api(args, "POST", "/boards", body=body))
    elif args.action == "update":
        body = parse_json_arg(args.json, "--json") or {}
        if args.title:
            body["title"] = args.title
        if args.deleted is not None:
            body["deleted"] = args.deleted
        out(api(args, "PUT", "/boards/%s" % args.id, body=body))


def cmd_columns(args):
    if args.action == "list":
        out(paginate(args, "/columns", {"title": args.title, "boardId": args.board_id}, args.all, args.limit))
    elif args.action == "get":
        out(api(args, "GET", "/columns/%s" % args.id))
    elif args.action == "create":
        body = {"title": args.title, "boardId": args.board_id}
        if args.color is not None:
            body["color"] = args.color
        body.update(parse_json_arg(args.json, "--json") or {})
        out(api(args, "POST", "/columns", body=body))
    elif args.action == "update":
        body = parse_json_arg(args.json, "--json") or {}
        if args.title:
            body["title"] = args.title
        if args.color is not None:
            body["color"] = args.color
        if args.deleted is not None:
            body["deleted"] = args.deleted
        out(api(args, "PUT", "/columns/%s" % args.id, body=body))


def cmd_tasks(args):
    if args.action == "list":
        query = {
            "columnId": args.column_id,
            "assignedTo": args.assigned_to,
            "title": args.title,
            "includeDeleted": "true" if args.include_deleted else None,
        }
        out(paginate(args, "/tasks", query, args.all, args.limit))
    elif args.action == "get":
        out(api(args, "GET", "/tasks/%s" % args.id))
    elif args.action == "create":
        body = {"title": args.title, "columnId": args.column_id}
        if args.description:
            body["description"] = args.description
        if args.assigned:
            body["assigned"] = args.assigned
        if args.deadline_ms:
            body["deadline"] = {"deadline": args.deadline_ms, "withTime": bool(args.with_time)}
        body.update(parse_json_arg(args.json, "--json") or {})
        out(api(args, "POST", "/tasks", body=body))
    elif args.action == "update":
        body = parse_json_arg(args.json, "--json") or {}
        if args.title:
            body["title"] = args.title
        if args.description is not None:
            body["description"] = args.description
        if args.column_id:
            body["columnId"] = args.column_id
        if args.assigned:
            body["assigned"] = args.assigned
        if args.completed is not None:
            body["completed"] = args.completed
        if args.archived is not None:
            body["archived"] = args.archived
        if args.deleted is not None:
            body["deleted"] = args.deleted
        if args.deadline_ms:
            body["deadline"] = {"deadline": args.deadline_ms, "withTime": bool(args.with_time)}
        if not body:
            die("Nothing to update: pass --title/--column-id/--completed/... or --json '{...}'")
        out(api(args, "PUT", "/tasks/%s" % args.id, body=body))
    elif args.action == "move":
        out(api(args, "PUT", "/tasks/%s" % args.id, body={"columnId": args.column_id}))
    elif args.action == "complete":
        out(api(args, "PUT", "/tasks/%s" % args.id, body={"completed": True}))
    elif args.action == "comments":
        out(api(args, "GET", "/chats/%s/messages" % args.id, query={"limit": args.limit or 50}))
    elif args.action == "comment":
        body = {"text": args.text}
        if args.image:
            # An image the user can click to enlarge is an attachment: a `/root/#file:<url>` line
            # in `text`, like the app sends. <img> in textHtml shows but cannot be enlarged.
            urls = [upload_file(args, path) for path in args.image]
            body["text"] = "\n \n".join([t for t in [args.text] if t] + ["/root/#file:" + u for u in urls])
            if args.html:
                sys.stderr.write("Note: --html is ignored with --image - an attachment goes in plain text.\n")
                args.html = None
        elif not args.text:
            die("Nothing to send: pass --text and/or --image")
        # `text` is shown verbatim - markup there lands on screen as raw tags. Formatting
        # renders only from `textHtml`, so `--html` sends it and `text` stays the plain fallback.
        if args.html:
            body["textHtml"] = args.html
        body.update(parse_json_arg(args.json, "--json") or {})
        result = api(args, "POST", "/chats/%s/messages" % args.id, body=body)
        from_file = not (getattr(args, "key", None) or os.environ.get("YOUGILE_API_KEY"))
        owner = (load_config().get("owner") or {}) if from_file else {}
        sys.stderr.write("Posted as %s (account '%s').\n" % (
            owner.get("name") or "the key owner - run `whoami` to see who",
            profile_name() if from_file else "--key/env"))
        # A real mention is a chunk in properties.params, which POST rejects; "@Name" in the
        # text is saved as plain text and notifies nobody.
        if "@" in (args.text or ""):
            sys.stderr.write(
                "Note: '@Name' was sent as plain text - the API cannot create a mention, nobody is "
                "notified. Ask the user to add the tag in the app.\n"
            )
        out(result)


def cmd_users(args):
    if args.action == "list":
        out(paginate(args, "/users", {"email": args.email}, args.all, args.limit))
    elif args.action == "get":
        out(api(args, "GET", "/users/%s" % args.id))


def cmd_request(args):
    query = kv_pairs(args.query)
    body = parse_json_arg(args.data, "--data")
    out(api(args, args.method, args.path, query=query or None, body=body))


# --------------------------------------------------------------------------- parser


def build_parser():
    p = argparse.ArgumentParser(prog="yougile.py", description="YouGile REST API v2 helper")
    p.add_argument("--key", help="API key (overrides env and config)")
    p.add_argument("--base-url", help="API base URL, default %s" % DEFAULT_BASE_URL)
    p.add_argument("--account", help="Key profile ~/.yougile/<name>.json (default: credentials.json); "
                   "also YOUGILE_ACCOUNT")
    sub = p.add_subparsers(dest="command", required=True)

    def add_conn(sp):
        """Allow --key/--base-url/--account after the subcommand too, without clobbering globals."""
        sp.add_argument("--key", default=argparse.SUPPRESS, help=argparse.SUPPRESS)
        sp.add_argument("--base-url", default=argparse.SUPPRESS, help=argparse.SUPPRESS)
        sp.add_argument("--account", default=argparse.SUPPRESS, help=argparse.SUPPRESS)

    s = sub.add_parser("status", help="Show whether a key is stored and works")
    add_conn(s)
    s.set_defaults(func=cmd_status)

    s = sub.add_parser("setup", help="Save an API key the user pasted")
    s.add_argument("--key", required=True)
    s.add_argument("--base-url", default=argparse.SUPPRESS)
    s.add_argument("--account", default=argparse.SUPPRESS, help=argparse.SUPPRESS)
    s.add_argument("--company-id")
    s.add_argument("--force", action="store_true", help="Replace another person's key in this profile")
    s.set_defaults(func=cmd_setup)

    s = sub.add_parser("login", help="Get a key from login+password and save it")
    s.add_argument("--login", help="Email; with --dialog it is asked in the dialog when omitted")
    s.add_argument("--password")
    s.add_argument("--password-stdin", action="store_true", help="Read the password from stdin")
    s.add_argument("--dialog", action="store_true",
                   help="macOS: ask the user in a system dialog (hidden password, company choice)")
    s.add_argument("--web", action="store_true",
                   help="Any OS: a one-time page on 127.0.0.1 in the user's browser (email, hidden "
                   "password, company choice); blocks until done - run it in the background")
    s.add_argument("--timeout", type=int, default=600,
                   help="--web: seconds to wait for the user (default 600), then exit 8")
    s.add_argument("--no-browser", action="store_true",
                   help="--web: only print the address, do not open a browser")
    s.add_argument("--linger", type=float, default=0, help=argparse.SUPPRESS)  # tests: keep answering 410
    s.add_argument("--force", action="store_true", help="Replace another person's key in this profile")
    s.add_argument("--company-id")
    s.add_argument("--pick-first", action="store_true", help="Use the first company without asking")
    s.add_argument("--new", action="store_true", help="Always mint a new key instead of reusing one")
    add_conn(s)
    s.set_defaults(func=cmd_login)

    s = sub.add_parser("companies", help="List companies for login+password")
    s.add_argument("--login", required=True)
    s.add_argument("--password")
    s.add_argument("--password-stdin", action="store_true")
    s.add_argument("--company-name")
    add_conn(s)
    s.set_defaults(func=cmd_companies)

    s = sub.add_parser("keys", help="List existing API keys for login+password")
    s.add_argument("--login", required=True)
    s.add_argument("--password")
    s.add_argument("--password-stdin", action="store_true")
    s.add_argument("--company-id")
    add_conn(s)
    s.set_defaults(func=cmd_keys_list)

    s = sub.add_parser("forget", help="Delete the stored key from the config file")
    add_conn(s)
    s.set_defaults(func=cmd_forget)

    s = sub.add_parser("whoami", help="Name and id of the key owner (the author YouGile shows)")
    add_conn(s)
    s.set_defaults(func=cmd_whoami)

    s = sub.add_parser("accounts", help="List key profiles in ~/.yougile")
    s.add_argument("--check", action="store_true", help="Ask YouGile whose each key is")
    add_conn(s)
    s.set_defaults(func=cmd_accounts)

    def add_common(sp):
        sp.add_argument("--limit", type=int, help="Page size (default 50; with --all, 1000)")
        sp.add_argument("--all", action="store_true", help="Follow pagination and return everything")

    s = sub.add_parser("projects")
    s.add_argument("action", choices=["list", "get", "create", "update"])
    s.add_argument("--id")
    s.add_argument("--title")
    s.add_argument("--users", help='JSON map {"userId":"admin|manager|worker"}')
    s.add_argument("--deleted", type=lambda v: v.lower() == "true")
    s.add_argument("--json", help="Extra body fields as JSON")
    add_common(s)
    add_conn(s)
    s.set_defaults(func=cmd_projects)

    s = sub.add_parser("boards")
    s.add_argument("action", choices=["list", "get", "create", "update"])
    s.add_argument("--id")
    s.add_argument("--title")
    s.add_argument("--project-id")
    s.add_argument("--deleted", type=lambda v: v.lower() == "true")
    s.add_argument("--json")
    add_common(s)
    add_conn(s)
    s.set_defaults(func=cmd_boards)

    s = sub.add_parser("columns")
    s.add_argument("action", choices=["list", "get", "create", "update"])
    s.add_argument("--id")
    s.add_argument("--title")
    s.add_argument("--board-id")
    s.add_argument("--color", type=int, help="1..16")
    s.add_argument("--deleted", type=lambda v: v.lower() == "true")
    s.add_argument("--json")
    add_common(s)
    add_conn(s)
    s.set_defaults(func=cmd_columns)

    s = sub.add_parser("tasks")
    s.add_argument(
        "action",
        choices=["list", "get", "create", "update", "move", "complete", "comments", "comment"],
    )
    s.add_argument("--id")
    s.add_argument("--title")
    s.add_argument("--description")
    s.add_argument("--column-id")
    s.add_argument("--assigned", nargs="*", help="User IDs")
    s.add_argument("--assigned-to", help="Filter: user ID")
    s.add_argument("--deadline-ms", type=int, help="Unix timestamp in milliseconds")
    s.add_argument("--with-time", action="store_true")
    s.add_argument("--completed", type=lambda v: v.lower() == "true")
    s.add_argument("--archived", type=lambda v: v.lower() == "true")
    s.add_argument("--deleted", type=lambda v: v.lower() == "true")
    s.add_argument("--include-deleted", action="store_true")
    s.add_argument(
        "--text",
        help="Comment text for `comment`: PLAIN text, shown verbatim (no HTML here); "
        "'@Name' is not a mention - the API cannot tag users",
    )
    s.add_argument("--html", help="Formatted body for `comment` (textHtml); keep --text as its plain version")
    s.add_argument(
        "--image", action="append", metavar="FILE",
        help="Attach an image to `comment` (repeatable); it can be clicked to enlarge in the app",
    )
    s.add_argument("--json")
    add_common(s)
    add_conn(s)
    s.set_defaults(func=cmd_tasks)

    s = sub.add_parser("users")
    s.add_argument("action", choices=["list", "get"])
    s.add_argument("--id")
    s.add_argument("--email")
    add_common(s)
    add_conn(s)
    s.set_defaults(func=cmd_users)

    s = sub.add_parser("request", help="Raw call to any endpoint")
    s.add_argument("method", choices=["GET", "POST", "PUT", "DELETE", "get", "post", "put", "delete"])
    s.add_argument("path", help="e.g. /tasks or /group-chats")
    s.add_argument("--query", nargs="*", help="key=value pairs")
    s.add_argument("--data", help="JSON request body")
    add_conn(s)
    s.set_defaults(func=cmd_request)

    return p


def main(argv=None):
    parser = build_parser()
    args = parser.parse_args(argv)
    select_profile(args)
    try:
        args.func(args)
    except ApiError as exc:
        hint = None
        if exc.status in (401, 403) and "/auth/" in exc.url:
            hint = ("Login or password rejected, or the account is not an admin of that company. "
                    "Ask the user to re-check the credentials - do not retry in a loop.")
        elif exc.status in (401, 403):
            hint = "The key is missing, wrong or revoked. Run `status`, then re-run onboarding."
        elif exc.status == 429:
            hint = "Rate limit: YouGile allows about 50 requests per minute per company. Slow down."
        elif exc.status == 400:
            hint = ("Bad body - or a permission failure: YouGile reports 'not enough rights' as 400 "
                    "(e.g. 'Not enough permissions', 'Не удалось создать колонку'), not 403. "
                    "If the body matches references/api.md, the account lacks rights on that "
                    "object. Do not retry - tell the user what to ask their admin for.")
        elif exc.status == 404:
            hint = ("Wrong path or unknown id - or the object is outside this account's "
                    "permissions: YouGile hides such objects behind 404 rather than 403. "
                    "Check the endpoint at %s" % KEY_URL)
        die("HTTP %s from %s: %s" % (exc.status, exc.url, exc.body), code=2, hint=hint)
    except urllib.error.URLError as exc:
        die("Network error: %s" % exc, code=6)
    except BrokenPipeError:
        try:
            sys.stdout.close()
        except Exception:
            pass
        os._exit(0)


if __name__ == "__main__":
    main()
