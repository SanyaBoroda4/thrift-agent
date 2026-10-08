"""thrift-api in Azure (WO33 B1): run on the PC after `az login`. Idempotent — every step looks first and makes only
what is missing — and it touches only what it creates (names starting thrift-, tag project=thrift-agent) plus one
database (`thrift`) and one login (`thrift_app`) on the owner's EXISTING Postgres server; no other setting of the
subscription or the server changes.

    python deploy/azure/provision.py infra     # subscription, Postgres found, thrift-rg, storage, Function, budget,
                                               # the firewall rules (Claude Code runs this)
    python deploy/azure/provision.py db        # database thrift + login thrift_app — needs the server's admin: an Entra
                                               # token, else the admin password typed (the OWNER runs this one, in his
                                               # own terminal; the password is used once, never stored)
    python deploy/azure/provision.py finish    # app settings, the code deployed, the keys made, /health, a test message
    python deploy/azure/provision.py up        # all three, in a terminal where the owner can type
    python deploy/azure/provision.py deploy    # the Function's code again (after a change in api/)
    python deploy/azure/provision.py mode replay|live|off
    python deploy/azure/provision.py keys      # the URL and keys, for the Apps Script and the Mac's .env — shown only
                                               # in a real terminal (the owner's), never in a log
    python deploy/azure/provision.py cleanup   # the temporary firewall rule for this PC removed (end of setup)

Secrets are never printed (but by `keys`, in the owner's terminal) or committed: the function keys and thrift_app's
password stay in var/azure/ (git-ignored, this PC only). The Telegram token and the group's chat id are read from the
Mac's .env over SSH (read only) and handed to Azure's app settings through a temporary file, deleted at once."""
from __future__ import annotations

import getpass
import json
import os
import secrets
import shutil
import subprocess
import sys
import tempfile
import time
import urllib.request
from datetime import date
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
VAR = ROOT / "var" / "azure"
STATE = VAR / "state.json"            # the names chosen, the region, the server — reused on every run
SECRETS = VAR / "secrets.json"        # thrift_app's password (this PC only; git-ignored)
KEYS = VAR / "keys.txt"               # the function keys (this PC only; git-ignored)
TAGS = ["project=thrift-agent"]
RG = "thrift-rg"
MAC = os.getenv("THRIFT_MAC", "tatiana_sorokina@192.168.68.57")
MAC_ALIAS = os.getenv("THRIFT_MAC_ALIAS", "192.168.68.57")
FIREWALL_TMP = "thrift-setup-tmp"
AZURE_SERVICES = "AllowAllAzureServicesAndResourcesWithinAzureIps"


def say(line: str) -> None:
    print(line, flush=True)


def az_path() -> str:
    found = shutil.which("az")
    if found:
        return found
    local = Path(os.getenv("LOCALAPPDATA", "")) / "thrift-azcli" / "bin" / "az.cmd"
    if local.is_file():
        return str(local)
    sys.exit("The Azure CLI isn't installed: winget install Microsoft.AzureCLI")


def az(*args: str, parse: bool = True, check: bool = True):
    """One az command; its JSON parsed. Never echoed (some carry names, none carry secrets: those go via files)."""
    cmd = [az_path(), *args, "--only-show-errors"] + (["-o", "json"] if parse else [])
    r = subprocess.run(cmd, capture_output=True, text=True, encoding="utf-8", errors="replace")
    if r.returncode != 0:
        if check:
            raise SystemExit(f"az {' '.join(args[:3])} failed: {r.stderr.strip()[-500:]}")
        return None
    if not parse:
        return r.stdout
    return json.loads(r.stdout) if r.stdout.strip() else None


def load(path: Path) -> dict:
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {}


def save(path: Path, data: dict) -> None:
    VAR.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(data, indent=1), encoding="utf-8")


def choose(what: str, options: list[str], env: str) -> int:
    """The owner picks one (the WO: if there are several, ask): typed here, or — run where nobody can type — named in
    `env` (the number, or the name) after the list printed."""
    pick = os.getenv(env, "").strip()
    if pick:
        if pick.isdigit() and 1 <= int(pick) <= len(options):
            return int(pick) - 1
        hits = [i for i, o in enumerate(options) if o.split(" (")[0] == pick or pick in o]
        if len(hits) == 1:
            return hits[0]
        sys.exit(f"{env}={pick!r} names none of the {what} (or several)")
    say(f"\nSeveral {what}:")
    for i, o in enumerate(options, 1):
        say(f"  {i}. {o}")
    if not sys.stdin.isatty():
        sys.exit(f"Several {what}: set {env} to the number or the name and run it again.")
    while True:
        a = input(f"Which {what.rstrip('s')}? [1-{len(options)}] ").strip()
        if a.isdigit() and 1 <= int(a) <= len(options):
            return int(a) - 1


# ---------------------------------------------------------------- the steps

def account(st: dict) -> dict:
    acct = az("account", "show")
    if acct is None:
        sys.exit("Not signed in: run `az login` first.")
    subs = [s for s in az("account", "list") if s.get("state") == "Enabled"]
    if len(subs) > 1 and not st.get("subscription"):
        i = choose("subscriptions", [f"{s['name']} ({s['id']})" for s in subs], "THRIFT_AZ_SUBSCRIPTION")
        az("account", "set", "--subscription", subs[i]["id"], parse=False)
        acct = az("account", "show")
    st["subscription"] = acct["id"]
    az("account", "set", "--subscription", acct["id"], parse=False)
    st["user"] = acct["user"]["name"]
    say(f"✓ subscription: {acct['name']}")
    return st


def postgres(st: dict) -> dict:
    servers = az("postgres", "flexible-server", "list")
    if not servers:
        sys.exit("No Azure Database for PostgreSQL flexible server in this subscription: the WO expects the existing one.")
    names = [s["name"] for s in servers]
    if st.get("pg_server") not in names:
        i = 0 if len(servers) == 1 else choose("Postgres servers", [f"{s['name']} ({s['location']})" for s in servers],
                                               "THRIFT_PG_SERVER")
        st["pg_server"] = servers[i]["name"]
    srv = next(s for s in servers if s["name"] == st["pg_server"])
    st["pg_rg"] = srv["resourceGroup"]
    st["location"] = srv["location"].replace(" ", "").lower()
    st["pg_fqdn"] = srv["fullyQualifiedDomainName"]
    st["pg_admin"] = srv.get("administratorLogin")
    st["pg_entra"] = ((srv.get("authConfig") or {}).get("activeDirectoryAuth") or "").lower() == "enabled"
    say(f"✓ Postgres: {st['pg_server']} ({st['location']})")
    return st


def group(st: dict) -> None:
    if az("group", "exists", "-n", RG) is not True:
        az("group", "create", "-n", RG, "-l", st["location"], "--tags", *TAGS)
    say(f"✓ resource group: {RG} ({st['location']})")


def storage(st: dict) -> None:
    st.setdefault("storage", f"thriftst{secrets.token_hex(4)}")
    if az("storage", "account", "show", "-n", st["storage"], "-g", RG, check=False) is None:
        az("storage", "account", "create", "-n", st["storage"], "-g", RG, "-l", st["location"], "--sku", "Standard_LRS",
           "--kind", "StorageV2", "--min-tls-version", "TLS1_2", "--allow-blob-public-access", "false", "--tags", *TAGS)
    say(f"✓ storage account: {st['storage']}")


def function_app(st: dict) -> None:
    st.setdefault("app", f"thrift-api-{secrets.token_hex(3)}")
    if az("functionapp", "show", "-n", st["app"], "-g", RG, check=False) is None:
        az("functionapp", "create", "-n", st["app"], "-g", RG, "--storage-account", st["storage"],
           "--flexconsumption-location", st["location"], "--runtime", "python", "--runtime-version", "3.12",
           "--instance-memory", "512", "--maximum-instance-count", "10", "--tags", *TAGS)
    st["url"] = f"https://{st['app']}.azurewebsites.net"
    # Application Insights (made with the app): a 0.1 GB daily cap
    az("config", "set", "extension.use_dynamic_install=yes_without_prompt", parse=False)
    ai = az("monitor", "app-insights", "component", "show", "-g", RG, "--app", st["app"], check=False)
    if ai is not None:
        az("monitor", "app-insights", "component", "billing", "update", "-g", RG, "--app", st["app"], "--cap", "0.1",
           check=False)
    say(f"✓ Function app: {st['app']} (Flex Consumption, Python 3.12, 512 MB, no always-ready instances) — {st['url']}")


def budget(st: dict) -> None:
    if az("consumption", "budget", "show-with-rg", "-g", RG, "-n", "thrift-budget", check=False) is None:
        start = date.today().replace(day=1)
        notes = {"actual80": {"enabled": True, "operator": "GreaterThan", "threshold": 80, "contactEmails": [st["user"]],
                              "thresholdType": "Actual"},
                 "forecast100": {"enabled": True, "operator": "GreaterThan", "threshold": 100,
                                 "contactEmails": [st["user"]], "thresholdType": "Forecasted"}}
        with tempfile.NamedTemporaryFile("w", suffix=".json", delete=False, encoding="utf-8") as f:
            json.dump(notes, f)
        try:
            az("consumption", "budget", "create-with-rg", "-g", RG, "-n", "thrift-budget", "--amount", "5",
               "--category", "cost", "--time-grain", "monthly",
               "--time-period", f"start-date={start.isoformat()}", f"end-date={start.replace(year=start.year + 5)}",
               "--notifications", f"@{f.name}")
        finally:
            os.unlink(f.name)
    say(f"✓ budget: $5 a month on {RG}, emails to {st['user']} at 80% and on a 100% forecast")


def my_ip() -> str:
    with urllib.request.urlopen("https://api.ipify.org", timeout=15) as r:
        return r.read().decode().strip()


def firewall(st: dict) -> None:
    rules = {r["name"]: r for r in az("postgres", "flexible-server", "firewall-rule", "list", "-g", st["pg_rg"],
                                       "-n", st["pg_server"]) or []}
    if AZURE_SERVICES not in rules and not any(r["startIpAddress"] == "0.0.0.0" == r["endIpAddress"]
                                               for r in rules.values()):
        az("postgres", "flexible-server", "firewall-rule", "create", "-g", st["pg_rg"], "-n", st["pg_server"],
           "--rule-name", AZURE_SERVICES, "--start-ip-address", "0.0.0.0", "--end-ip-address", "0.0.0.0")
        say("✓ firewall: Allow Azure services (added)")
    else:
        say("✓ firewall: Azure services already allowed")
    ip = my_ip()
    az("postgres", "flexible-server", "firewall-rule", "create", "-g", st["pg_rg"], "-n", st["pg_server"],
       "--rule-name", FIREWALL_TMP, "--start-ip-address", ip, "--end-ip-address", ip)
    st["tmp_rule"] = True
    say(f"✓ firewall: {FIREWALL_TMP} for this PC (removed by `cleanup`)")


def admin_connection(st: dict):
    """The admin's connection for the one-time setup: an Entra token when the signed-in user is the server's Entra
    admin, else the admin password typed here (never stored)."""
    import psycopg
    if st.get("pg_entra"):
        admins = az("postgres", "flexible-server", "ad-admin", "list", "-g", st["pg_rg"], "-s", st["pg_server"],
                    check=False) or []
        me = next((a for a in admins if (a.get("principalName") or "").lower() == st["user"].lower()), None)
        if me:
            token = az("account", "get-access-token", "--resource-type", "oss-rdbms")["accessToken"]
            return psycopg.connect(host=st["pg_fqdn"], dbname="postgres", user=me["principalName"], password=token,
                                   sslmode="require", autocommit=True)
    if not sys.stdin.isatty():
        raise SystemExit("The server's admin password is needed: run this step in your own terminal —\n"
                         "    .venv\\Scripts\\python deploy\\azure\\provision.py db")
    pw = getpass.getpass(f"Postgres admin password for {st['pg_admin']}@{st['pg_server']} (typed here, never stored): ")
    return psycopg.connect(host=st["pg_fqdn"], dbname="postgres", user=st["pg_admin"], password=pw, sslmode="require",
                           autocommit=True)


def database(st: dict) -> None:
    if az("postgres", "flexible-server", "db", "show", "-g", st["pg_rg"], "-s", st["pg_server"], "-d", "thrift",
          check=False) is None:
        az("postgres", "flexible-server", "db", "create", "-g", st["pg_rg"], "-s", st["pg_server"], "-d", "thrift")
    sec = load(SECRETS)
    sec.setdefault("thrift_app_password", secrets.token_urlsafe(30))
    save(SECRETS, sec)
    from psycopg import sql
    with admin_connection(st) as conn:
        exists = conn.execute("SELECT 1 FROM pg_roles WHERE rolname='thrift_app'").fetchone()
        verb = "ALTER" if exists else "CREATE"
        conn.execute(sql.SQL(verb + " ROLE thrift_app LOGIN PASSWORD {}").format(sql.Literal(sec["thrift_app_password"])))
        try:
            conn.execute("GRANT thrift_app TO CURRENT_USER")      # Azure's admin isn't a superuser: needed to hand over
        except Exception:  # noqa: BLE001 — granted already
            pass
        conn.execute("ALTER DATABASE thrift OWNER TO thrift_app")
    import psycopg
    with psycopg.connect(host=st["pg_fqdn"], dbname="thrift", user="thrift_app", password=sec["thrift_app_password"],
                         sslmode="require", autocommit=True) as conn:
        conn.execute("SELECT 1")
    say("✓ database thrift, login thrift_app (owner of thrift only; TLS required)")


def mac_secret(name: str) -> str:
    """One value from the Mac's .env, read over SSH (read only), never printed."""
    r = subprocess.run(["ssh", "-o", "BatchMode=yes", "-o", "ConnectTimeout=15", "-o", f"HostKeyAlias={MAC_ALIAS}", MAC,
                        f"grep -m1 '^{name}=' ~/thrift-agent/.env | cut -d= -f2-"], capture_output=True, text=True)
    value = r.stdout.strip().strip('"').strip("'")
    if r.returncode != 0 or not value:
        raise SystemExit(f"couldn't read {name} from the Mac's .env (is the Mac reachable at {MAC}?)")
    return value


def app_settings(st: dict, mode: str | None = None) -> None:
    sec = load(SECRETS)
    import yaml
    ops = (yaml.safe_load((ROOT / "private" / "settings.yaml").read_text(encoding="utf-8")) or {}).get("telegram", {})
    values = {"DATABASE_URL": f"postgresql://thrift_app:{sec['thrift_app_password']}@{st['pg_fqdn']}:5432/thrift"
                              "?sslmode=require",
              "TELEGRAM_BOT_TOKEN": mac_secret("TELEGRAM_BOT_TOKEN"),
              "TELEGRAM_GROUP_CHAT_ID": mac_secret("TELEGRAM_CHAT_ID"),
              "TELEGRAM_OPS_CHAT_ID": str(ops.get("ops_chat_id") or mac_secret("TELEGRAM_OPS_CHAT_ID")),
              "SALES_MODE": mode or st.get("mode", "replay"), "TZ_NAME": "America/New_York"}
    st["mode"] = values["SALES_MODE"]
    fd, path = tempfile.mkstemp(suffix=".json")
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as f:
            json.dump([{"name": k, "value": v, "slotSetting": False} for k, v in values.items()], f)
        az("functionapp", "config", "appsettings", "set", "-g", RG, "-n", st["app"], "--settings", f"@{path}", parse=False)
    finally:
        os.unlink(path)
    say(f"✓ app settings: DATABASE_URL, TELEGRAM_* (from the Mac's .env), SALES_MODE={st['mode']}, TZ_NAME")


def deploy_code(st: dict) -> None:
    func = shutil.which("func") or str(Path(os.getenv("APPDATA", "")) / "npm" / "func.cmd")
    r = subprocess.run([func, "azure", "functionapp", "publish", st["app"], "--python"], cwd=ROOT / "api",
                       capture_output=True, text=True, encoding="utf-8", errors="replace")
    if r.returncode != 0:
        raise SystemExit(f"the deploy failed: {(r.stdout + r.stderr)[-1500:]}")
    say(f"✓ deployed api/ to {st['app']}")


def show_keys(st: dict) -> None:
    """The URL and the keys, for the owner to paste — only into a real terminal (his), never a log or a pipe."""
    if not sys.stdout.isatty():
        raise SystemExit("The keys are shown only in your own terminal: .venv\\Scripts\\python deploy\\azure\\provision.py keys")
    k = dict(line.split("=", 1) for line in KEYS.read_text(encoding="utf-8").splitlines() if "=" in line)
    say("\nGoogle Apps Script → Project Settings → Script properties:")
    say(f"  API_URL  {k['API_URL']}")
    say(f"  API_KEY  {k['gmail']}")
    say("\nThe Mac (Terminal), then restart the worker and the poster:")
    say(f"  printf 'THRIFT_API_URL={k['API_URL']}\\nTHRIFT_API_KEY={k['mac']}\\n' >> ~/thrift-agent/.env")
    say("\nThe dashboard (bookmark it on the phone):")
    say(f"  {k['dashboard_url']}\n")


def keys(st: dict) -> dict:
    out = {}
    for name in ("mac", "gmail", "dashboard"):
        got = az("functionapp", "keys", "list", "-g", RG, "-n", st["app"]) or {}
        have = (got.get("functionKeys") or {}).get(name)
        if not have:
            have = az("functionapp", "keys", "set", "-g", RG, "-n", st["app"], "--key-type", "functionKeys",
                      "--key-name", name)["value"]
        out[name] = have
    VAR.mkdir(parents=True, exist_ok=True)
    KEYS.write_text("\n".join([f"API_URL={st['url']}", *(f"{k}={v}" for k, v in out.items()),
                               f"dashboard_url={st['url']}/dashboard?code={out['dashboard']}"]) + "\n", encoding="utf-8")
    say(f"✓ function keys mac, gmail, dashboard — in {KEYS.relative_to(ROOT)} (this PC only; open it in your own terminal)")
    return out


def call(st: dict, key: str, method: str, path: str, body: dict | None = None) -> dict:
    req = urllib.request.Request(st["url"] + path, method=method, headers={"x-functions-key": key,
                                                                           "Content-Type": "application/json"},
                                 data=json.dumps(body).encode() if body is not None else None)
    with urllib.request.urlopen(req, timeout=60) as r:
        return json.loads(r.read() or b"{}")


def check(st: dict, k: dict) -> None:
    for attempt in range(12):                      # a cold start after the deploy
        try:
            h = call(st, k["mac"], "GET", "/health")
            break
        except Exception as e:  # noqa: BLE001
            if attempt == 11:
                raise SystemExit(f"GET /health didn't answer: {e}") from None
            time.sleep(10)
    say(f"✓ health: db {h.get('db')}, mode {h.get('mode')}")
    sent = call(st, k["mac"], "POST", "/test-message", {"chat": "ops"})
    say(f"✓ test message to the ops chat: {'sent' if sent.get('sent') else 'not sent'} (mode {sent.get('mode')})")


def cleanup(st: dict) -> None:
    az("postgres", "flexible-server", "firewall-rule", "delete", "-g", st["pg_rg"], "-n", st["pg_server"],
       "--rule-name", FIREWALL_TMP, "--yes", parse=False, check=False)
    st["tmp_rule"] = False
    say(f"✓ firewall rule {FIREWALL_TMP} removed")


def main(argv: list[str]) -> None:
    what = argv[1] if len(argv) > 1 else "up"
    st = load(STATE)
    if what in ("up", "infra"):
        account(st)
        postgres(st)
        save(STATE, st)
        group(st)
        storage(st)
        function_app(st)
        save(STATE, st)
        budget(st)
        firewall(st)
        save(STATE, st)
    if what in ("up", "db"):
        database(st)
        save(STATE, st)
    if what in ("up", "finish"):
        app_settings(st)
        deploy_code(st)
        k = keys(st)
        save(STATE, st)
        check(st, k)
    if what in ("up", "infra", "db", "finish"):
        pass
    elif what == "deploy":
        deploy_code(st)
    elif what == "mode" and len(argv) > 2 and argv[2] in ("replay", "live", "off"):
        app_settings(st, argv[2])
    elif what == "cleanup":
        cleanup(st)
    elif what == "keys":
        show_keys(st)
    else:
        sys.exit(__doc__)
    save(STATE, st)


if __name__ == "__main__":
    main(sys.argv)
