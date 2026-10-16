"""Passkeys: only public keys stored, bound to the server's own address, each challenge usable once."""
import json
import os

import pytest

from icloud_mcp.passkeys import PasskeyError, PasskeyStore

from softauthn import SoftAuthenticator

pytest.importorskip("webauthn")
BASE = "https://mcp.example.com"


def registered(tmp_path):
    store = PasskeyStore(str(tmp_path), BASE)
    dev = SoftAuthenticator(BASE, "mcp.example.com")
    opts = store.registration_options()
    key = store.register(opts["ticket"], dev.create(opts["publicKey"]), "Test phone")
    return store, dev, key


def test_register_then_sign_in(tmp_path):
    store, dev, key = registered(tmp_path)
    assert key.name == "Test phone" and [k.id for k in store.list()] == [key.id]
    assert oct(os.stat(tmp_path / "passkeys.json").st_mode & 0o777) == "0o600"
    saved = json.loads((tmp_path / "passkeys.json").read_text())
    assert set(saved["keys"][0]) == {"id", "public_key", "sign_count", "name", "created_at", "last_used_at"}   # no private key
    opts = store.authentication_options()
    assert opts["publicKey"]["allowCredentials"][0]["id"] == key.id
    used = store.authenticate(opts["ticket"], dev.get(opts["publicKey"]))
    assert used.id == key.id and used.sign_count == 1 and used.last_used_at
    # after a restart the same key still works
    again = PasskeyStore(str(tmp_path), BASE)
    opts = again.authentication_options()
    assert again.authenticate(opts["ticket"], dev.get(opts["publicKey"])).sign_count == 2


def test_another_origin_is_refused(tmp_path):
    store, dev, _ = registered(tmp_path)
    opts = store.authentication_options()
    with pytest.raises(PasskeyError):
        store.authenticate(opts["ticket"], dev.get(opts["publicKey"], origin="https://evil.example.net"))


def test_a_challenge_works_once(tmp_path):
    store, dev, _ = registered(tmp_path)
    opts = store.authentication_options()
    answer = dev.get(opts["publicKey"])
    store.authenticate(opts["ticket"], answer)
    with pytest.raises(PasskeyError, match="expired"):
        store.authenticate(opts["ticket"], answer)


def test_unknown_key_and_no_keys(tmp_path):
    store = PasskeyStore(str(tmp_path), BASE)
    with pytest.raises(PasskeyError, match="No passkey"):
        store.authentication_options()
    store, dev, _ = registered(tmp_path)
    other = SoftAuthenticator(BASE, "mcp.example.com")
    opts = store.authentication_options()
    with pytest.raises(PasskeyError, match="not registered"):
        store.authenticate(opts["ticket"], other.get(opts["publicKey"]))


def test_remove(tmp_path):
    store, _, key = registered(tmp_path)
    assert store.remove(key.id).name == "Test phone"
    assert store.list() == [] and store.remove(key.id) is None
