"""CalDAV sessions never upgrade to HTTP/3: in containers the QUIC upgrade iCloud advertises fails and costs a retry or the call."""
import pytest

import caldav_fakes
import icloud_mcp.cal as cal_mod
from icloud_mcp.cal import CalendarService, _no_http3
from icloud_mcp.config import Settings


class NoTicker:
    def add(self, fn):
        pass


@pytest.fixture
def s(tmp_path, monkeypatch):
    for k, v in dict(ICLOUD_USERNAME="me@icloud.com", ICLOUD_APP_PASSWORD="aaaa-bbbb-cccc-dddd", DATA_DIR=str(tmp_path),
                     MCP_PUBLIC_URL="https://mcp.example.com", MCP_OWNER_PASSWORD="x" * 16).items():
        monkeypatch.setenv(k, v)
    monkeypatch.setattr(cal_mod, "TICKER", NoTicker())
    return Settings.from_env()


def test_every_pooled_caldav_connection_has_http3_off(s, monkeypatch):
    server, made = caldav_fakes.Server(password=s.app_password), []

    class Client(cal_mod.caldav.DAVClient):
        def __init__(self, **kw):
            super().__init__(**kw)
            self.session.request = server
            made.append(self)
    monkeypatch.setattr(cal_mod.caldav, "DAVClient", Client)
    CalendarService(s).list_calendars()
    assert made
    for client in made:
        adapter = client.session.get_adapter("https://caldav.icloud.com/")
        assert adapter._disable_http3 is True                         # niquests keeps the switch only on the adapter
        assert adapter._disable_http2 is False and adapter._disable_http1 is False


def test_a_client_without_the_knob_still_works():
    class Bare:
        pass
    _no_http3(Bare())                                                 # no session attribute: logged at debug, never raised
