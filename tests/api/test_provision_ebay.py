"""WO57 — deploy/azure/provision.py's `ebay` step: its pure parts (the .env reader, the verification token, the token
key, the settings it writes) and the step itself with `az` stood in — the two secrets made once and never again, the
keyset set whole or not at all, the consent link with its own function key, and nothing secret ever printed. Azure is
never called: every az command here is the stand-in's."""
import base64
import importlib.util
import json
import string

import pytest

from thrift_agent.config import ROOT

URL = "https://thrift-api-abc123.azurewebsites.net"
KEYSET = {"EBAY_CLIENT_ID": "Thrift-thrift-PRD-0123456789-abcdef01", "EBAY_CLIENT_SECRET": "PRD-0123456789ab-cdef-0123-4567-89ab",
          "EBAY_RUNAME": "Thrift_Shop-Thrift-thrift-abcdefg"}
FUNCTION_KEY = "fnKey_/+with==odd-characters=="


@pytest.fixture(scope="module")
def provision():
    spec = importlib.util.spec_from_file_location("thrift_provision", ROOT / "deploy" / "azure" / "provision.py")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


class Azure:
    """`az`, stood in: the app's settings and the one function key, as the step reads and writes them."""

    def __init__(self, deployed: bool = True):
        self.deployed, self.key, self.settings, self.commands = deployed, None, {}, []

    def az(self, *args, parse=True, check=True):
        self.commands.append(args)
        if args[:4] == ("functionapp", "function", "keys", "set"):
            assert args[args.index("--function-name") + 1] == "get_ebay_oauth_start"       # that function, no other
            assert args[args.index("--key-name") + 1] == "ebay" and "--key-value" not in args
            self.key = FUNCTION_KEY if self.deployed else None
            return None
        if args[:4] == ("functionapp", "function", "keys", "list"):
            return {"ebay": self.key} if self.key else None
        if args[:4] == ("functionapp", "config", "appsettings", "list"):
            return [{"name": name, "value": value} for name, value in self.settings.items()]
        raise AssertionError(f"an az command the eBay step shouldn't run: {args[:4]}")

    def set_settings(self, st, values):
        self.settings.update(values)


@pytest.fixture
def pc(provision, tmp_path, monkeypatch):
    """This PC's var/azure and private/.env in a scratch folder, Azure stood in."""
    azure = Azure()
    monkeypatch.setattr(provision, "VAR", tmp_path)
    monkeypatch.setattr(provision, "STATE", tmp_path / "state.json")
    monkeypatch.setattr(provision, "SECRETS", tmp_path / "secrets.json")
    monkeypatch.setattr(provision, "KEYS", tmp_path / "keys.txt")
    monkeypatch.setattr(provision, "EBAY_ENV_FILE", tmp_path / "private.env")
    monkeypatch.setattr(provision, "EBAY_SETTINGS_FILE", tmp_path / "settings.yaml")
    monkeypatch.setattr(provision, "az", azure.az)
    monkeypatch.setattr(provision, "set_settings", azure.set_settings)
    (tmp_path / "secrets.json").write_text(json.dumps({"thrift_app_password": "the-db-password"}), encoding="utf-8")
    (tmp_path / "keys.txt").write_text("API_URL=x\nmac=the-mac-key\n", encoding="utf-8")
    return azure


def state() -> dict:
    return {"app": "thrift-api-abc123", "url": URL, "subscription": "sub"}


def test_the_env_reader(provision, tmp_path):
    path = tmp_path / ".env"
    path.write_text("﻿# eBay's keyset\nEBAY_CLIENT_ID=abc-PRD\n  EBAY_CLIENT_SECRET = 'PRD-se=cret'  \n\n"
                    'export EBAY_RUNAME="Thrift_Shop-x"\nno equals sign here\nEBAY_ENV=\n=nameless\n', encoding="utf-8")
    assert provision.read_env(path) == {"EBAY_CLIENT_ID": "abc-PRD", "EBAY_CLIENT_SECRET": "PRD-se=cret",
                                        "EBAY_RUNAME": "Thrift_Shop-x", "EBAY_ENV": ""}
    assert provision.read_env(tmp_path / "not-there.env") == {}


def test_the_verification_token_and_the_token_key(provision):
    tokens = {provision.ebay_verification_token() for _ in range(20)}
    assert len(tokens) == 20                                                   # random
    for token in tokens:                                                       # eBay: 32–80 of letters, digits, _ and -
        assert len(token) == 64 and set(token) <= set(string.ascii_letters + string.digits + "_-")
    key = provision.fernet_key()
    assert len(base64.urlsafe_b64decode(key)) == 32 and key != provision.fernet_key()
    fernet = pytest.importorskip("cryptography.fernet")
    assert fernet.Fernet(key).decrypt(fernet.Fernet(key).encrypt(b"a refresh token")) == b"a refresh token"


def test_the_two_secrets_are_made_once(provision):
    sec = {"thrift_app_password": "p"}
    assert provision.ebay_secrets(sec) is True and set(sec) == {"thrift_app_password", "ebay_verification_token",
                                                                "ebay_token_key"}
    before = dict(sec)
    assert provision.ebay_secrets(sec) is False and sec == before              # never again: the stored token stays readable


def test_the_settings_before_and_after_the_keyset_exists(provision):
    sec = {"ebay_verification_token": "v" * 64, "ebay_token_key": "k" * 44}
    always = {"EBAY_VERIFICATION_TOKEN": "v" * 64, "EBAY_TOKEN_KEY": "k" * 44,
              "EBAY_DELETION_ENDPOINT": f"{URL}/ebay/account-deletion"}
    assert provision.ebay_settings(URL + "/", sec, {}) == (always, ["EBAY_CLIENT_ID", "EBAY_CLIENT_SECRET", "EBAY_RUNAME"])
    # half a keyset would turn eBay on and broken: none of it is set, the missing name is said
    half = {k: v for k, v in KEYSET.items() if k != "EBAY_CLIENT_SECRET"}
    assert provision.ebay_settings(URL, sec, {**half, "EBAY_ENV": "sandbox"}) == (
        {**always, "EBAY_ENV": "sandbox"}, ["EBAY_CLIENT_SECRET"])
    values, missing = provision.ebay_settings(URL, sec, {**KEYSET, "EBAY_API": "listing", "OTHER": "x"}, FUNCTION_KEY)
    assert missing == [] and values == {
        **always, **KEYSET, "EBAY_API": "listing",
        "EBAY_START_LINK": f"{URL}/ebay/oauth/start?code=fnKey_%2F%2Bwith%3D%3Dodd-characters%3D%3D"}
    assert set(values) <= set(provision.EBAY_SETTINGS)


def test_the_step_works_before_ebays_keys_exist_and_prints_no_secret(provision, pc, tmp_path, capsys):
    st = state()
    provision.ebay(st)
    sec = json.loads((tmp_path / "secrets.json").read_text(encoding="utf-8"))
    assert sec["thrift_app_password"] == "the-db-password"                     # the other secrets kept
    assert pc.settings == {"EBAY_VERIFICATION_TOKEN": sec["ebay_verification_token"], "EBAY_TOKEN_KEY": sec["ebay_token_key"],
                           "EBAY_DELETION_ENDPOINT": f"{URL}/ebay/account-deletion",
                           "EBAY_START_LINK": f"{URL}/ebay/oauth/start?code=fnKey_%2F%2Bwith%3D%3Dodd-characters%3D%3D"}
    assert st["ebay"] == {"settings": sorted(pc.settings), "missing": ["EBAY_CLIENT_ID", "EBAY_CLIENT_SECRET", "EBAY_RUNAME"]}
    assert "ebay_start=" + FUNCTION_KEY in (tmp_path / "keys.txt").read_text(encoding="utf-8")
    said = capsys.readouterr().out
    assert "not in private/.env yet: EBAY_CLIENT_ID, EBAY_CLIENT_SECRET, EBAY_RUNAME" in said
    assert f"{URL}/ebay/account-deletion" in said
    assert not any(secret in said for secret in (sec["ebay_verification_token"], sec["ebay_token_key"], FUNCTION_KEY,
                                                 "fnKey", "the-db-password"))
    # the keyset arrives: the same two secrets, the keyset added
    (tmp_path / "private.env").write_text("\n".join(f"{k}={v}" for k, v in KEYSET.items()) + "\nEBAY_ENV=production\n",
                                          encoding="utf-8")
    provision.ebay(st)
    assert json.loads((tmp_path / "secrets.json").read_text(encoding="utf-8")) == sec
    assert {k: pc.settings[k] for k in KEYSET} == KEYSET and pc.settings["EBAY_ENV"] == "production"
    assert pc.settings["EBAY_TOKEN_KEY"] == sec["ebay_token_key"] and st["ebay"]["missing"] == []
    said = capsys.readouterr().out
    assert "not in private/.env" not in said and not any(value in said for value in KEYSET.values())
    assert sum(1 for c in pc.commands if c[:4] == ("functionapp", "function", "keys", "set")) == 1   # the key made once


def test_before_the_code_is_deployed_the_link_waits(provision, pc, capsys):
    pc.deployed = False
    st = state()
    provision.ebay(st)
    assert "EBAY_START_LINK" not in pc.settings and "EBAY_VERIFICATION_TOKEN" in pc.settings
    assert "the function get_ebay_oauth_start isn't in Azure yet" in capsys.readouterr().out
    with pytest.raises(SystemExit):
        provision.ebay({})                                                     # nothing provisioned at all


def test_the_shops_username_comes_from_the_env_file_else_the_settings(provision, pc, tmp_path, capsys, monkeypatch):
    pytest.importorskip("yaml")
    assert provision.ebay_seller({}) == {}                                     # neither file names it: left unset
    (tmp_path / "settings.yaml").write_text("marketplaces:\n  ebay:\n    shop: thrift_shop_ny\n", encoding="utf-8")
    assert provision.ebay_seller({"A": "1"}) == {"A": "1", "EBAY_SELLER": "thrift_shop_ny"}
    assert provision.ebay_seller({"EBAY_SELLER": "from_env"}) == {"EBAY_SELLER": "from_env"}
    provision.ebay(state())
    assert pc.settings["EBAY_SELLER"] == "thrift_shop_ny" and "thrift_shop_ny" not in capsys.readouterr().out
    monkeypatch.setattr(provision.sys.stdout, "isatty", lambda: True, raising=False)
    provision.ebay_show(state())
    shown = capsys.readouterr().out
    assert all(f"{URL}/ebay/{path}" in shown for path in ("account-deletion", "oauth/callback", "oauth/declined",
                                                           "privacy", "oauth/start?code="))
    assert pc.settings["EBAY_VERIFICATION_TOKEN"] in shown                     # here, and only here


def test_show_is_for_the_owners_terminal_and_status_names_only(provision, pc, capsys, monkeypatch):
    st = state()
    provision.ebay(st)
    capsys.readouterr()
    with pytest.raises(SystemExit) as refused:                                 # pytest's stdout is no terminal
        provision.ebay_show(st)
    assert "only in your own terminal" in str(refused.value) and capsys.readouterr().out == ""
    monkeypatch.setattr(provision, "call", lambda st, key, method, path, body=None: {
        "configured": False, "connected": False, "environment": "production", "ended": 0})
    provision.ebay_status(st)
    said = capsys.readouterr().out
    assert "EBAY_VERIFICATION_TOKEN ✓" in said and "EBAY_CLIENT_ID —" in said and "configured False" in said
    assert not any(value in said for value in pc.settings.values())


def test_the_command_line(provision, pc, tmp_path, capsys):
    (tmp_path / "state.json").write_text(json.dumps(state()), encoding="utf-8")
    provision.main(["provision.py", "ebay"])
    assert json.loads((tmp_path / "state.json").read_text(encoding="utf-8"))["ebay"]["settings"]
    with pytest.raises(SystemExit):
        provision.main(["provision.py", "ebay", "show"])
    with pytest.raises(SystemExit) as usage:
        provision.main(["provision.py", "ebay", "everything"])
    assert "provision.py ebay" in str(usage.value)
