"""Tiny authenticated SMTP sink (127.0.0.1:1025) that stores every received message as a .eml file."""
import asyncio, pathlib, sys, time
from aiosmtpd.controller import Controller
from aiosmtpd.smtp import AuthResult

OUT = pathlib.Path("/tmp/icmcp-dev/smtp-out"); OUT.mkdir(parents=True, exist_ok=True)

class Sink:
    async def handle_DATA(self, server, session, envelope):
        n = len(list(OUT.glob("*.eml")))
        (OUT / f"{n:03d}.eml").write_bytes(envelope.content)
        (OUT / f"{n:03d}.rcpt").write_text("\n".join(envelope.rcpt_tos))
        return "250 OK"

def auth(server, session, envelope, mechanism, data):
    ok = data.password in (b"testpass", "testpass") if hasattr(data, "password") else False
    return AuthResult(success=ok, handled=True)

if __name__ == "__main__":
    c = Controller(Sink(), hostname="127.0.0.1", port=1025, authenticator=auth, auth_required=True, auth_require_tls=False)
    c.start(); print("smtp sink up", flush=True)
    while True: time.sleep(3600)
