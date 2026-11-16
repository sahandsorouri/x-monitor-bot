"""Offline tests: no token, no network."""

import asyncio
import importlib
import sys
from pathlib import Path
from types import SimpleNamespace as NS

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))


def load(monkeypatch, tmp_path, **env):
    monkeypatch.setenv("DATA_DIR", str(tmp_path))
    for key in ("ALLOWED_USER_IDS", "WATCHED_ACCOUNTS", "DIGEST_CHAT_ID", "ADMIN_CHAT_ID"):
        monkeypatch.setenv(key, env.get(key, ""))
    import main
    return importlib.reload(main)


def test_allowlist_is_default_deny(monkeypatch, tmp_path):
    main = load(monkeypatch, tmp_path, ALLOWED_USER_IDS="111, 222, x")
    assert main.ALLOWED_USER_IDS == {111, 222}
    assert main.is_allowed(111)
    assert not main.is_allowed(333)
    assert not main.is_allowed(None)
    main = load(monkeypatch, tmp_path)
    assert main.ALLOWED_USER_IDS == frozenset()
    assert not main.is_allowed(111), "an empty list answers nobody"


def test_gated_handler_ignores_strangers(monkeypatch, tmp_path):
    main = load(monkeypatch, tmp_path, ALLOWED_USER_IDS="111")
    calls = []

    async def handler(update, context):
        calls.append(update.effective_user.id)

    wrapped = main.gated(handler)
    for uid in (111, 999):
        update = NS(effective_user=NS(id=uid, is_bot=False), callback_query=None)
        asyncio.run(wrapped(update, None))
    asyncio.run(wrapped(NS(effective_user=None, callback_query=None), None))
    assert calls == [111]


def test_watchlist_from_env(monkeypatch, tmp_path):
    main = load(monkeypatch, tmp_path, WATCHED_ACCOUNTS="@Example_Account, other_account,,example_account, bad handle")
    assert main.WATCHED_ACCOUNTS == ["example_account", "other_account"]
    config = {"-100": ["already_here"]}
    assert main.seed_config(config, "-100", main.WATCHED_ACCOUNTS)
    assert config["-100"] == ["already_here", "example_account", "other_account"]
    assert not main.seed_config(config, "-100", main.WATCHED_ACCOUNTS), "second run adds nothing"
    assert not main.seed_config(config, "", ["x"])


def test_digest_chat_must_be_numeric(monkeypatch, tmp_path):
    assert load(monkeypatch, tmp_path, DIGEST_CHAT_ID="-1001").DIGEST_CHAT_ID == "-1001"
    assert load(monkeypatch, tmp_path, DIGEST_CHAT_ID="your-chat").DIGEST_CHAT_ID == ""


def test_no_admin_chat_means_no_alarm_target(monkeypatch, tmp_path):
    assert load(monkeypatch, tmp_path).ADMIN_CHAT_ID is None
    assert load(monkeypatch, tmp_path, ADMIN_CHAT_ID="42").ADMIN_CHAT_ID == 42


def test_parse_username(monkeypatch, tmp_path):
    main = load(monkeypatch, tmp_path)
    assert main.parse_username("example_account") == "example_account"
    assert main.parse_username("@Example_Account") == "example_account"
    assert main.parse_username("https://x.com/example_account/status/1") == "example_account"
    assert main.parse_username("https://x.com/home") is None
    assert main.parse_username("two words") is None


def test_hold_reason(monkeypatch, tmp_path):
    main = load(monkeypatch, tmp_path)
    assert main._hold_reason({"_repost": True}, "5", True, 0) == "source switch"
    assert main._hold_reason({"_repost": True}, "5", False, 10) is None
    assert main._hold_reason({}, "5", False, 10) == "own post older than newest seen"
    assert main._hold_reason({}, "50", False, 10) is None
