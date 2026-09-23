"""The Reminders/Notes bridge: queue semantics, the private HTTPS app, certificate pinning, settings safety, tool gating."""
import asyncio
import json
import dataclasses
import hashlib
import logging
import socket
import stat
import threading
import time

import httpx
import pytest

import icloud_mcp.bridge as bridge_mod
from icloud_mcp.bridge import BridgeError, MacBridge, build_bridge_app, ensure_tls, start_bridge_listener, validate_args
from icloud_mcp.config import Settings
from icloud_mcp.server import build_app, build_instructions, create_server

TOKEN = "tok-" + "a1b2c3d4" * 6            # 52 chars, not a placeholder


@pytest.fixture
def s(tmp_path, monkeypatch):
    for k, v in dict(ICLOUD_USERNAME="me@icloud.com", ICLOUD_APP_PASSWORD="aaaa-bbbb-cccc-dddd", MCP_PUBLIC_URL="https://mcp.example.net",
                     MCP_OWNER_PASSWORD="a-long-random-owner-password", DATA_DIR=str(tmp_path), ENABLE_REMINDERS="true", BRIDGE_TOKEN=TOKEN,
                     BRIDGE_JOB_TIMEOUT_SECONDS="5").items():
        monkeypatch.setenv(k, v)
    return Settings.from_env()


# ------------------------------------------------------------------ argument validation
@pytest.fixture
def rich_ops(monkeypatch):
    ops = {"demo": {"title": ("str", True, 10), "count": ("int", False, 50), "flag": ("bool", False, 0), "due": ("iso", False, 40)}}
    monkeypatch.setattr(bridge_mod, "OPS", ops)
    return ops


def test_arguments_are_validated_strictly(rich_ops):
    assert validate_args("demo", {"title": "hi", "count": 3, "flag": True, "due": "2026-09-21T15:00:00+02:00"}) == \
        {"title": "hi", "count": 3, "flag": True, "due": "2026-09-21T15:00:00+02:00"}
    assert validate_args("demo", {"title": "hi", "due": None})["due"] is None
    for bad, why in [({"title": "x" * 11}, "at most"), ({"title": 5}, "text"), ({"title": "a", "count": 51}, "between"), ({"title": "a", "count": True}, "between"),
                     ({"title": "a", "flag": "yes"}, "true or false"), ({"title": "a", "due": "tomorrow"}, "ISO"), ({"title": "a", "due": "2026-13-45"}, "ISO"),
                     ({"title": "a", "due": "2026-02-30"}, "ISO"), ({"title": "a", "due": "2026-09-21T25:00:00"}, "ISO"), ({"title": "a", "due": "2026-09-21T10:00:00+25:00"}, "ISO"), ({"title": "a", "extra": 1}, "does not take"),
                     ({}, "needs: title")]:
        with pytest.raises(BridgeError, match=why):
            validate_args("demo", bad)
    with pytest.raises(BridgeError, match="Unknown Mac operation"):
        validate_args("rm_rf", {})
    with pytest.raises(BridgeError, match="object"):
        validate_args("demo", ["title"])


def test_the_real_operation_table_is_exactly_the_reminders_and_notes_operations():
    assert set(bridge_mod.OPS) == {"reminder_lists", "reminders_list", "reminder_create", "reminder_update", "reminder_complete", "reminder_delete",
                                   "note_folders", "notes_list", "note_read", "note_create", "note_delete",
                                   "note_folder_create", "note_move"}
    assert validate_args("reminder_lists", None) == {} and validate_args("reminders_list", {"query": "x", "limit": 5})["limit"] == 5


# ------------------------------------------------------------------ the queue
def poll_in_thread(bridge, results, wait=5):
    def run():
        job = bridge.next_job({"host": "test-mac", "version": "9"}, wait)
        results.append(job)
    t = threading.Thread(target=run)
    t.start()
    return t


def test_an_offline_helper_gives_a_clear_error_immediately():
    b = MacBridge(timeout=2)
    with pytest.raises(BridgeError, match="has not connected since the server started"):
        b.call("reminder_lists")
    b.next_job({"host": "m"}, 0)                                   # one poll makes it online...
    b.last_seen = time.time() - 300                                # ...and five minutes of silence makes it offline again
    with pytest.raises(BridgeError, match="last seen 5 minute"):
        b.call("reminder_lists")


def test_a_call_travels_to_the_helper_and_the_answer_comes_back():
    b = MacBridge(timeout=5)
    b.next_job({"host": "test-mac", "version": "9", "os": "macOS 15"}, 0)     # helper is online
    got = []
    t = poll_in_thread(b, got)
    out = []
    caller = threading.Thread(target=lambda: out.append(b.call("reminder_lists")))
    caller.start()
    t.join(3)
    job = got[0]
    assert job.op == "reminder_lists" and job.args == {}
    assert b.complete(job.id, True, [{"id": "1", "name": "Home"}], "") is True
    caller.join(3)
    assert out == [[{"id": "1", "name": "Home"}]]
    assert b.status()["helper"]["host"] == "test-mac" and b.status()["online"] is True


def test_a_failure_reported_by_the_mac_becomes_a_tool_error():
    b = MacBridge(timeout=5)
    b.next_job({}, 0)
    got = []
    t = poll_in_thread(b, got)
    errors = []
    def caller():
        try:
            b.call("reminder_lists")
        except BridgeError as e:
            errors.append(str(e))
    c = threading.Thread(target=caller)
    c.start(); t.join(3)
    b.complete(got[0].id, False, None, "macOS has not allowed this helper to control the app")
    c.join(3)
    assert errors == ["macOS has not allowed this helper to control the app"]


def test_a_helper_that_never_answers_times_out_and_the_job_is_cancelled():
    b = MacBridge(timeout=1)
    b.next_job({}, 0)
    started = time.monotonic()
    with pytest.raises(BridgeError, match="did not answer within 1s"):
        b.call("reminder_lists")
    assert time.monotonic() - started < 3
    assert b.next_job({}, 0) is None                               # the cancelled job is never handed out late


def test_results_for_unknown_or_finished_jobs_are_ignored():
    b = MacBridge(timeout=5)
    assert b.complete("nope", True, [], "") is False
    b.next_job({}, 0)
    got = []
    t = poll_in_thread(b, got)
    c = threading.Thread(target=lambda: b.call("reminder_lists"))
    c.start(); t.join(3)
    assert b.complete(got[0].id, True, [], "") is True
    assert b.complete(got[0].id, True, ["again"], "") is False     # answered once only
    c.join(3)


def test_jobs_are_served_first_in_first_out_and_status_counts_waiting_jobs():
    b = MacBridge(timeout=5)
    b.next_job({}, 0)
    def submit():
        try:
            b.call("reminder_lists")
        except BridgeError:
            pass
    threads = [threading.Thread(target=submit) for _ in range(3)]
    for th in threads:
        th.start(); time.sleep(0.05)
    assert b.status()["jobs_waiting"] == 3
    first = b.next_job({}, 1)
    second = b.next_job({}, 1)
    assert first.deadline <= second.deadline                        # created in order, served in order
    for j in (first, second):
        b.complete(j.id, True, [], "")
    third = b.next_job({}, 1)
    b.complete(third.id, True, [], "")
    for th in threads:
        th.join(3)


def test_a_helper_busy_with_a_long_job_still_counts_as_online():
    b = MacBridge(timeout=60)
    b.next_job({}, 0)
    got = []
    t = poll_in_thread(b, got)
    c = threading.Thread(target=lambda: b.call("reminder_lists"), daemon=True)
    c.start(); t.join(3)
    b.last_seen = time.time() - 120                                 # no polls for two minutes: it is busy running the job
    assert b.status()["online"] is True


# ------------------------------------------------------------------ the private HTTPS app (over plain ASGI here; TLS is tested below)
async def client(s, bridge):
    return httpx.AsyncClient(transport=httpx.ASGITransport(app=build_bridge_app(bridge, s)), base_url="http://bridge")


AUTH = {"Authorization": f"Bearer {TOKEN}"}


async def test_every_bridge_route_needs_the_token(s):
    async with await client(s, MacBridge()) as c:
        for method, path in (("GET", "/bridge/ping"), ("POST", "/bridge/poll"), ("POST", "/bridge/result")):
            assert (await c.request(method, path)).status_code == 401
            assert (await c.request(method, path, headers={"Authorization": "Bearer wrong"})).status_code == 401
        assert (await c.get("/bridge/ping", headers=AUTH)).status_code == 200


async def test_repeated_wrong_tokens_lock_the_bridge_out_even_for_the_right_one(s):
    async with await client(s, MacBridge()) as c:
        for _ in range(20):
            assert (await c.get("/bridge/ping", headers={"Authorization": "Bearer nope"})).status_code == 401
        assert (await c.get("/bridge/ping", headers=AUTH)).status_code == 429


async def test_poll_and_result_round_trip_over_http(s):
    b = MacBridge(timeout=5)
    async with await client(s, b) as c:
        r = await c.post("/bridge/poll", headers=AUTH, json={"wait": 0, "host": "m"})
        assert r.status_code == 204                                 # idle, and now the helper counts as online
        caller = threading.Thread(target=lambda: b.call("reminder_lists"))
        caller.start()
        job = None
        for _ in range(50):
            r = await c.post("/bridge/poll", headers=AUTH, json={"wait": 1, "host": "m"})
            if r.status_code == 200:
                job = r.json(); break
        assert job and job["op"] == "reminder_lists" and job["args"] == {} and job["seconds"] >= 1
        done = await c.post("/bridge/result", headers=AUTH, json={"job": job["id"], "ok": True, "result": []})
        assert done.json() == {"accepted": True}
        caller.join(3)


async def test_oversized_and_malformed_requests_are_rejected_not_crashed(s):
    async with await client(s, MacBridge()) as c:
        assert (await c.post("/bridge/poll", headers=AUTH, content=b"x" * 2_000_000)).status_code == 400
        assert (await c.post("/bridge/poll", headers=AUTH, content=b"not json")).status_code == 400
        assert (await c.post("/bridge/result", headers=AUTH, json={"nope": 1})).status_code == 400


async def test_the_token_never_reaches_the_log(s, caplog):
    caplog.set_level(logging.DEBUG)
    async with await client(s, MacBridge()) as c:
        await c.get("/bridge/ping", headers={"Authorization": "Bearer super-secret-wrong-token"})
        await c.get("/bridge/ping", headers=AUTH)
    assert "super-secret-wrong-token" not in caplog.text and TOKEN not in caplog.text
    assert any("refused" in r.getMessage() for r in caplog.records)


async def test_the_public_app_serves_no_bridge_routes(s):
    mcp, _ = create_server(s)
    app = build_app(s, mcp)
    async with app.router.lifespan_context(app):
        async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="https://mcp.example.net") as c:
            for path in ("/bridge/ping", "/bridge/poll", "/bridge/result"):
                assert (await c.get(path, headers=AUTH)).status_code in (404, 405)
                assert (await c.post(path, headers=AUTH, json={})).status_code in (404, 405)


# ------------------------------------------------------------------ TLS certificate
def test_certificate_is_created_once_private_and_stable(tmp_path):
    cert, key, fp = ensure_tls(str(tmp_path))
    assert stat.S_IMODE((tmp_path / "bridge-key.pem").stat().st_mode) == 0o600
    cert2, key2, fp2 = ensure_tls(str(tmp_path))                     # a restart must not change the pinned fingerprint
    assert (cert, key, fp) == (cert2, key2, fp2) and len(fp) == 64
    assert (tmp_path / "bridge_fingerprint.txt").read_text().strip() == fp


# ------------------------------------------------------------------ settings safety
@pytest.mark.parametrize("token,why", [("", "BRIDGE_TOKEN"), ("short", "BRIDGE_TOKEN"), ("change-me-" + "x" * 40, "BRIDGE_TOKEN")])
def test_bridge_refuses_to_start_with_a_missing_weak_or_placeholder_token(s, token, why):
    with pytest.raises(SystemExit, match=why):
        dataclasses.replace(s, bridge_token=token).validate_for_server()


def test_bridge_token_must_differ_from_the_owner_password(s):
    with pytest.raises(SystemExit, match="differ"):
        dataclasses.replace(s, bridge_token=s.owner_password + "x" * 20, owner_password=s.owner_password + "x" * 20).validate_for_server()


def test_the_bridge_is_off_by_default_and_needs_no_token_then(s, monkeypatch):
    monkeypatch.delenv("ENABLE_REMINDERS"); monkeypatch.delenv("BRIDGE_TOKEN")
    off = Settings.from_env()
    assert off.bridge_enabled is False and off.enable_notes is False
    off.validate_for_server()


# ------------------------------------------------------------------ tools and instructions
def names(settings):
    async def go():
        mcp, _ = create_server(settings)
        return {t.name: t for t in await mcp.list_tools()}
    return asyncio.run(go())


def test_bridge_tools_exist_only_when_enabled(s):
    on = names(s)
    assert {"mac_helper_status", "reminders_lists"} <= set(on)
    off = names(dataclasses.replace(s, enable_reminders=False))
    assert "reminders_lists" not in off and "mac_helper_status" not in off
    assert all(v.get("description") for t in (on["reminders_lists"], on["mac_helper_status"]) for v in (getattr(t, "input_schema", None) or t.inputSchema).get("properties", {}).values())


def test_a_tool_call_with_no_helper_says_so_plainly(s):
    async def go():
        mcp, _ = create_server(s)
        with pytest.raises(Exception, match="has not connected since the server started"):
            await mcp.call_tool("reminders_lists", {})
        status = await mcp.call_tool("mac_helper_status", {})
        return status.content[0].text
    assert '"online": false' in asyncio.run(go())


def test_reminders_list_passes_the_new_arguments_and_shapes_the_answer(s):
    seen = []

    async def go(answer):
        mcp, _ = create_server(s)
        mcp._icloud_bridge.call = lambda op, args=None: seen.append((op, args)) or answer
        result = await mcp.call_tool("reminders_list", {"list_id": "L1", "query": "milk", "refresh": True, "limit": 5})
        return json.loads(result.content[0].text) if result.content else result
    cached = {"reminders": [{"id": "a"}], "cached": [{"list": "Big", "list_id": "L1", "age_seconds": 90, "refreshing": False}]}
    out = asyncio.run(go(cached))
    assert seen[-1] == ("reminders_list", {"list_id": "L1", "query": "milk", "refresh": True, "limit": 5})
    assert out["count"] == 1 and out["cached"][0]["age_seconds"] == 90
    assert asyncio.run(go([{"id": "a"}, {"id": "b"}]))["count"] == 2                            # an older helper still answers with a bare list
    assert "include_completed" not in {k for t_ in [names(s)["reminders_list"]] for k in (getattr(t_, "input_schema", None) or t_.inputSchema)["properties"]}


def test_instructions_mention_the_mac_only_when_enabled(s):
    assert "REMINDERS / NOTES" in build_instructions(s) and "offline, tell the user" in build_instructions(s)
    assert "REMINDERS / NOTES" not in build_instructions(dataclasses.replace(s, enable_reminders=False))


# ------------------------------------------------------------------ end to end: real TLS listener + the real helper code + a fake osascript
def free_port():
    sock = socket.socket()
    sock.bind(("127.0.0.1", 0))
    port = sock.getsockname()[1]
    sock.close()
    return port


@pytest.fixture
def live(tmp_path, s):
    b = MacBridge(timeout=5)
    cert, key, fp = ensure_tls(str(tmp_path))
    port = free_port()
    server = start_bridge_listener(build_bridge_app(b, s), port, cert, key, host="127.0.0.1")
    for _ in range(100):
        if server.started:
            break
        time.sleep(0.05)
    yield b, {"server": f"https://127.0.0.1:{port}", "token": TOKEN, "fingerprint": fp}
    server.should_exit = True
