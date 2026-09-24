"""The Mac helper keeps one pinned HTTPS connection to the real bridge server, re-reads its config only when the file changes,
and serves repeated Notes listings from a short cache that any Notes write clears."""
import json
import os
import time

import pytest

from test_bridge import TOKEN, live, s  # noqa: F401  (fixtures)
from test_mac_helper import helper


@pytest.fixture
def link(monkeypatch):
    fresh = helper.Link()
    monkeypatch.setattr(helper, "_LINK", fresh)
    made = []
    real = helper._connect

    def counting(cfg, timeout):
        made.append(1)
        return real(cfg, timeout)
    monkeypatch.setattr(helper, "_connect", counting)
    return fresh, made


def test_polls_reuse_one_pinned_connection(live, link):  # noqa: F811
    _, cfg = live
    fresh, made = link
    for _ in range(3):
        status, _ = helper.request(cfg, "GET", "/bridge/ping", timeout=5)
        assert status == 200
    assert len(made) == 1 and fresh.conn.auto_open == 0              # one handshake; http.client may not reconnect by itself


def test_a_dead_connection_is_replaced_through_the_pinning_path(live, link):  # noqa: F811
    _, cfg = live
    fresh, made = link
    helper.request(cfg, "GET", "/bridge/ping", timeout=5)
    fresh.conn.sock.close()                                          # the server (or the network) dropped it
    status, _ = helper.request(cfg, "GET", "/bridge/ping", timeout=5)
    assert status == 200 and len(made) == 2
    bad = dict(cfg, fingerprint="sha256:" + "00" * 32)
    with pytest.raises(helper.HelperError, match="pinned fingerprint"):
        helper.request(bad, "GET", "/bridge/ping", timeout=5)        # a changed config gets a new, checked connection


def test_config_is_read_again_only_when_the_file_changes(tmp_path, monkeypatch):
    path = tmp_path / "config.json"
    path.write_text(json.dumps({"server": "https://127.0.0.1:1", "token": "t" * 40, "fingerprint": "ab"}))
    os.chmod(path, 0o600)
    monkeypatch.setattr(helper, "_CFG", {"key": None, "cfg": None})
    reads = []
    real = helper.load_config
    monkeypatch.setattr(helper, "load_config", lambda p=None: reads.append(1) or real(p))
    helper.current_config(str(path))
    helper.current_config(str(path))
    assert len(reads) == 1
    time.sleep(0.01)
    path.write_text(json.dumps({"server": "https://127.0.0.1:2", "token": "t" * 40, "fingerprint": "ab"}))
    assert helper.current_config(str(path))["server"].endswith(":2") and len(reads) == 2


def test_notes_listings_are_cached_briefly_and_writes_clear_them(monkeypatch):
    monkeypatch.setattr(helper, "_NOTES_CACHE", {})
    calls = []
    monkeypatch.setattr(helper, "run_one", lambda op, args, timeout=60, extra=None: calls.append(op) or (True, [op], None))
    helper.run_op("notes_list", {"query": "x"})
    helper.run_op("notes_list", {"query": "x"})
    assert calls == ["notes_list"]
    helper.run_op("notes_list", {"query": "y"})                      # other arguments: their own entry
    helper.run_op("note_update", {"id": "1"})
    helper.run_op("notes_list", {"query": "x"})
    assert calls == ["notes_list", "notes_list", "note_update", "notes_list"]
    t = time.monotonic() + helper._NOTES_LIST_SECONDS + 1
    monkeypatch.setattr(helper.time, "monotonic", lambda: t)
    helper.run_op("notes_list", {"query": "x"})
    assert calls[-1] == "notes_list" and len(calls) == 5
