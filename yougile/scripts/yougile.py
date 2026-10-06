#!/usr/bin/env python3
"""
yougile.py - dependency-free CLI helper for the YouGile REST API v2.

Works with any Python 3.8+ (stdlib only). Every command prints JSON to stdout.
Errors go to stderr and produce a non-zero exit code.

Credentials resolution order:
  1. --key argument
  2. YOUGILE_API_KEY environment variable
  3. config file (default ~/.yougile/credentials.json, override with YOUGILE_CONFIG)

Run `python3 yougile.py --help` for a command list.
"""

import argparse
import json
import os
import sys
import time
import urllib.error
import urllib.parse
import urllib.request

DEFAULT_BASE_URL = "https://ru.yougile.com/api-v2"
CONFIG_PATH = os.environ.get(
    "YOUGILE_CONFIG", os.path.join(os.path.expanduser("~"), ".yougile", "credentials.json")
)
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
        "configPath": CONFIG_PATH,
        "baseUrl": resolve_base_url(args),
        "companyId": cfg.get("companyId"),
    }
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


def cmd_setup(args):
    key = args.key
    base_url = resolve_base_url(args)
    try:
        call("GET", "/users", base_url, key=key, query={"limit": 1})
    except ApiError as exc:
        die(
            "Key rejected by YouGile (HTTP %s). Nothing was saved." % exc.status,
            code=4,
            hint="Ask the user to re-copy the key from %s" % KEY_URL,
        )
    cfg = load_config()
    cfg.update({"apiKey": key, "baseUrl": base_url, "savedAt": int(time.time())})
    if args.company_id:
        cfg["companyId"] = args.company_id
    path = save_config(cfg)
    out({"ok": True, "saved": path, "keyPreview": mask(key), "baseUrl": base_url})


def read_password(args):
    """Password from --password, --password-stdin, or YOUGILE_PASSWORD.

    Prefer stdin/env: an argument is visible in shell history and in `ps` output.
    The password is used for this call only and is never written to the config file.
    """
    if getattr(args, "password_stdin", False):
        pw = sys.stdin.readline().rstrip("\n")
        if pw:
            return pw
    if getattr(args, "password", None):
        return args.password
    if os.environ.get("YOUGILE_PASSWORD"):
        return os.environ["YOUGILE_PASSWORD"]
    die("No password given: use --password, --password-stdin or YOUGILE_PASSWORD.", code=3)


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
    creds = {"login": args.login, "password": read_password(args)}

    company_id = args.company_id
    if not company_id:
        companies = call("POST", "/auth/companies", base_url, body=creds)
        items = companies.get("content", [])
        if not items:
            die("No companies found for this account.", code=4)
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

    try:
        call("GET", "/users", base_url, key=key, query={"limit": 1})  # verify it works
    except ApiError as exc:
        die("Key was issued but rejected on verification (HTTP %s). Nothing saved." % exc.status, code=4)

    cfg = load_config()
    cfg.update({"apiKey": key, "baseUrl": base_url, "companyId": company_id, "savedAt": int(time.time())})
    path = save_config(cfg)
    out({
        "ok": True,
        "saved": path,
        "companyId": company_id,
        "reusedExistingKey": reused,
        "keyPreview": mask(key),
        "note": "Key stored; password was not saved. The key is bound to this company.",
    })


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
    out({"ok": True, "removed": "apiKey", "configPath": CONFIG_PATH})


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
    sub = p.add_subparsers(dest="command", required=True)

    def add_conn(sp):
        """Allow --key/--base-url after the subcommand too, without clobbering globals."""
        sp.add_argument("--key", default=argparse.SUPPRESS, help=argparse.SUPPRESS)
        sp.add_argument("--base-url", default=argparse.SUPPRESS, help=argparse.SUPPRESS)

    s = sub.add_parser("status", help="Show whether a key is stored and works")
    add_conn(s)
    s.set_defaults(func=cmd_status)

    s = sub.add_parser("setup", help="Save an API key the user pasted")
    s.add_argument("--key", required=True)
    s.add_argument("--base-url", default=argparse.SUPPRESS)
    s.add_argument("--company-id")
    s.set_defaults(func=cmd_setup)

    s = sub.add_parser("login", help="Get a key from login+password and save it")
    s.add_argument("--login", required=True)
    s.add_argument("--password")
    s.add_argument("--password-stdin", action="store_true", help="Read the password from stdin")
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
