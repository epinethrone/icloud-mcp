"""An SMS-only conversation is refused before it is queued (the Mac refuses it on approval anyway, after the owner already
approved), and the refusal names the same person's iMessage conversation that may be sent to."""
import pytest

from icloud_mcp.bridge import BridgeError
from icloud_mcp.config import Settings
from icloud_mcp.imessage import IMessageError, IMessageService
from icloud_mcp.outbox import Outbox

CHATS = {"chats": [
    {"chat_id": "+31600000001", "participants": ["+31600000001"], "name": "", "services": ["SMS"]},
    {"chat_id": "anna@example.org", "participants": ["anna@example.org"], "name": "", "services": ["iMessage"]},
    {"chat_id": "chat900", "participants": ["anna@example.org", "+31600000002"], "name": "Hiking crew", "group": True,
     "services": ["iMessage"]},
    {"chat_id": "+31600000003", "participants": ["+31600000003"], "name": "", "services": ["SMS"]},
]}


class Contacts:
    def _people(self):
        return [{"uid": "u-anna", "name": "Anna Example", "emails": [{"address": "anna@example.org"}],
                 "phones": [{"number": "+31 6 00000001"}]},
                {"uid": "u-ben", "name": "Ben Example", "emails": [], "phones": [{"number": "+31 6 00000003"}]}]


class Bridge:
    def __init__(self):
        self.calls = []

    def call(self, op, args=None):
        self.calls.append((op, args))
        if op == "imessage_read":
            c = next((x for x in CHATS["chats"] if x["chat_id"] == args["chat_id"]), None)
            if c is None:
                raise BridgeError(f"no conversation with chat_id '{args['chat_id']}'")
            return {"chat": {**c, "participants": sorted(c["participants"])}, "messages": [], "complete": True}
        if op == "imessage_chats":
            return {"chats": [{**c, "last_message_at": None, "last_text": ""} for c in CHATS["chats"]]}
        return {"status": "sent", "message_id": "m1"}


@pytest.fixture
def s(tmp_path, monkeypatch):
    for k, v in dict(ICLOUD_USERNAME="me@icloud.com", ICLOUD_APP_PASSWORD="aaaa-bbbb-cccc-dddd", DATA_DIR=str(tmp_path),
                     MCP_PUBLIC_URL="https://mcp.example.com", MCP_OWNER_PASSWORD="x" * 16, BRIDGE_TOKEN="t" * 40,
                     ENABLE_IMESSAGE="true", IMESSAGE_ALLOW_SEND="true",
                     IMESSAGE_SEND_ALLOWLIST="+31600000001,anna@example.org,+31600000003").items():
        monkeypatch.setenv(k, v)
    return Settings.from_env()


def svc(s, tmp_path):
    b = Bridge()
    return IMessageService(s, b, Contacts()), b, Outbox(str(tmp_path), 3600, 20, filename="imessage-outbox.json")


def test_sms_only_chat_is_refused_and_names_the_imessage_chat(s, tmp_path):
    m, b, box = svc(s, tmp_path)
    with pytest.raises(IMessageError) as e:
        m.send("See you tonight", chat_id="+31600000001", outbox=box, public_url=s.public_url)
    msg = str(e.value)
    assert "exists only as SMS" in msg and "Nothing was sent or queued" in msg
    assert "chat_id 'anna@example.org'" in msg and "chat900" not in msg       # her 1:1 iMessage chat, never the group
    assert not box.pending() and not [op for op, _ in b.calls if op == "imessage_send"]


def test_sms_only_chat_without_an_imessage_chat_gets_the_general_hint(s, tmp_path):
    m, b, box = svc(s, tmp_path)
    with pytest.raises(IMessageError, match="ask the owner for their Apple ID address"):
        m.send("Hi", chat_id="+31600000003", outbox=box, public_url=s.public_url)
    assert not box.pending()


def test_an_imessage_chat_still_queues(s, tmp_path):
    m, b, box = svc(s, tmp_path)
    r = m.send("See you tonight", chat_id="anna@example.org", outbox=box, public_url=s.public_url)
    assert r["status"] == "queued_for_owner_approval" and "services" not in r["to"]


def test_an_alternative_not_on_the_allowlist_is_not_offered(s, tmp_path, monkeypatch):
    monkeypatch.setenv("IMESSAGE_SEND_ALLOWLIST", "+31600000001")
    m, b, box = svc(Settings.from_env(), tmp_path)
    with pytest.raises(IMessageError) as e:
        m.send("Hi", chat_id="+31600000001", outbox=box, public_url=s.public_url)
    assert "anna@example.org" not in str(e.value) and "Apple ID" in str(e.value)
