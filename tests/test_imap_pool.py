"""Reusing logged-in IMAP connections: fewer logins, never CLOSE, and nothing reused whose state is unknown."""
import dataclasses
import threading

import pytest

import icloud_mcp.mail as mail_mod
from icloud_mcp.config import Settings
from icloud_mcp.mail import MailError, MailService


class FakeImaplib:
    def __init__(self):
        self.state = "AUTH"


class FakeClient:
    made = []

    def __init__(self, *a, **kw):
        self._imap = FakeImaplib()
        self.calls = []
        self.alive = True
        self.caps = {b"UNSELECT"}
        FakeClient.made.append(self)

    def login(self, *a):
        self.calls.append("login")

    def logout(self):
        self.calls.append("logout")

    def select_folder(self, name, readonly=False):
        self.calls.append(f"select {name}")
        self._imap.state = "SELECTED"
        return {}

    def unselect_folder(self):
        self.calls.append("unselect")
        self._imap.state = "AUTH"

    def close_folder(self):                              # must never be called: it expunges every \Deleted message
        raise AssertionError("CLOSE used")

    def has_capability(self, cap):
        return cap.encode() in self.caps

    def noop(self):
        self.calls.append("noop")
        if not self.alive:
            raise OSError("connection reset")


@pytest.fixture
def svc(tmp_path, monkeypatch):
    FakeClient.made = []
    monkeypatch.setattr(mail_mod, "IMAPClient", FakeClient)
    for k, v in dict(ICLOUD_USERNAME="me@icloud.com", ICLOUD_APP_PASSWORD="aaaa-bbbb-cccc-dddd", DATA_DIR=str(tmp_path),
                     MCP_PUBLIC_URL="https://mcp.example.com", MCP_OWNER_PASSWORD="x" * 16).items():
        monkeypatch.setenv(k, v)
    return MailService(Settings.from_env())


def use(svc, **kw):
    with svc.imap(**kw) as c:
        c.select_folder("INBOX")
        return c


def test_a_connection_is_reused_and_left_with_no_folder_selected(svc):
    first = use(svc)
    second = use(svc)
    assert first is second and len(FakeClient.made) == 1
    assert first.calls.count("login") == 1 and first.calls.count("unselect") == 2 and "logout" not in first.calls


def test_a_failed_call_never_returns_its_connection_to_the_pool(svc):
    with pytest.raises(RuntimeError):
        with svc.imap() as c:
            c.select_folder("INBOX")
            raise RuntimeError("boom halfway")
    assert "logout" in c.calls
    assert use(svc) is not c and len(FakeClient.made) == 2


def test_a_session_that_died_while_idle_is_replaced(svc, monkeypatch):
    c = use(svc)
    c.alive = False
    clock = [1000.0]
    monkeypatch.setattr(mail_mod.time, "monotonic", lambda: clock[0])
    svc._pool = [(c, 1000.0 - 45)]                          # idle for 45 s: checked with NOOP, which fails
    fresh = use(svc)
    assert fresh is not c and "noop" in c.calls and "logout" in c.calls


def test_connections_idle_too_long_are_closed_not_reused(svc, monkeypatch):
    c = use(svc)
    monkeypatch.setattr(mail_mod.time, "monotonic", lambda: 10_000.0)
    svc._pool = [(c, 10_000.0 - svc.s.imap_idle_seconds - 1)]
    assert use(svc) is not c and "logout" in c.calls and "noop" not in c.calls


def test_a_server_without_unselect_gets_no_reuse(svc):
    with svc.imap() as c:
        c.caps = set()
        c.select_folder("INBOX")
    assert "logout" in c.calls and svc._pool == []


def test_pool_size_zero_and_fresh_always_log_in(svc):
    fresh = use(svc, fresh=True)
    assert "logout" in fresh.calls and svc._pool == []
    svc.s = dataclasses.replace(svc.s, imap_pool_size=0)
    a, b = use(svc), use(svc)
    assert a is not b and "logout" in a.calls and "logout" in b.calls


def test_parallel_calls_never_share_a_connection_and_the_pool_stays_capped(svc):
    held, gate = [], threading.Barrier(4)

    def work():
        with svc.imap() as c:
            gate.wait(5)
            held.append(c)
            c.select_folder("INBOX")
    threads = [threading.Thread(target=work) for _ in range(4)]
    [t.start() for t in threads]
    [t.join(5) for t in threads]
    assert len({id(c) for c in held}) == 4                     # four calls at once, four connections
    assert len(svc._pool) == svc.s.imap_pool_size == 2          # only two kept, the rest logged out
    assert sum("logout" in c.calls for c in held) == 2


def test_health_proves_a_real_login_and_close_pool_logs_everything_out(svc):
    pooled = use(svc)
    svc.health = MailService.health.__get__(svc)
    with svc.imap(fresh=True) as c:
        assert c is not pooled
    svc.close_pool()
    assert "logout" in pooled.calls and svc._pool == []


def test_login_failure_is_a_clear_mail_error(svc, monkeypatch):
    def bad_login(self, *a):
        raise OSError("AUTHENTICATIONFAILED")
    monkeypatch.setattr(FakeClient, "login", bad_login)
    with pytest.raises(MailError, match="login failed"):
        use(svc)


def test_settings_are_read_and_bounded(monkeypatch, svc):
    monkeypatch.setenv("IMAP_POOL_SIZE", "50")
    monkeypatch.setenv("IMAP_IDLE_SECONDS", "5")
    s = Settings.from_env()
    assert s.imap_pool_size == 8 and s.imap_idle_seconds == 30
