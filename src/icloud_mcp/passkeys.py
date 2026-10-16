"""Passkeys (WebAuthn) for the owner's dashboard sign-in.

The owner adds a passkey after signing in once with the owner password; from then on the device's own unlock (Face ID,
Touch ID, a security key) signs them in. Only public keys are stored, in DATA_DIR/passkeys.json (mode 600). The relying
party is the host of MCP_PUBLIC_URL, so a passkey works only on this server's address, and the browser refuses to use it
anywhere else, which is what makes it phishing-resistant.

Needs the optional `webauthn` package (`pip install "icloud-mcp-server[passkey]"`; the Docker image has it). Without it the
dashboard offers the owner password only.
"""
from __future__ import annotations

import json
import os
import secrets
import tempfile
import threading
import time
from dataclasses import asdict, dataclass
from pathlib import Path
from typing import Any
from urllib.parse import urlsplit

try:                                    # optional extra
    import webauthn
    from webauthn.helpers import bytes_to_base64url, base64url_to_bytes
    from webauthn.helpers.structs import (AuthenticatorSelectionCriteria, PublicKeyCredentialDescriptor, ResidentKeyRequirement,
                                          UserVerificationRequirement)
except ImportError:                     # pragma: no cover - exercised only where the extra is missing
    webauthn = None

FILE = "passkeys.json"
_CHALLENGE_TTL = 300
_MAX_KEYS = 10


class PasskeyError(Exception):
    pass


def available() -> bool:
    return webauthn is not None


@dataclass
class Passkey:
    id: str                 # credential id, base64url
    public_key: str         # COSE public key, base64url
    sign_count: int
    name: str
    created_at: float
    last_used_at: float | None = None


class PasskeyStore:
    def __init__(self, data_dir: str, public_url: str, rp_name: str = "iCloud connector"):
        parts = urlsplit(public_url)
        self.rp_id = parts.hostname or "localhost"
        self.origin = f"{parts.scheme}://{parts.netloc}"
        self.rp_name = rp_name
        self.path = Path(data_dir) / FILE
        self._lock = threading.Lock()
        self._challenges: dict[str, tuple[bytes, str, float]] = {}    # ticket -> (challenge, purpose, expires)
        self._user_id = b""
        self._keys: list[Passkey] = []
        self._load()

    # -- storage ---------------------------------------------------------------------------
    def _load(self) -> None:
        try:
            data = json.loads(self.path.read_text())
        except (OSError, ValueError):
            data = {}
        self._user_id = base64url_to_bytes(data["user_id"]) if available() and data.get("user_id") else secrets.token_bytes(16)
        self._keys = []
        for k in data.get("keys", []) if isinstance(data, dict) else []:
            try:
                self._keys.append(Passkey(**{f: k[f] for f in ("id", "public_key", "sign_count", "name", "created_at")},
                                          last_used_at=k.get("last_used_at")))
            except (KeyError, TypeError):
                continue

    def _save(self) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        fd, tmp = tempfile.mkstemp(dir=self.path.parent, prefix=".passkeys-", suffix=".tmp")
        try:
            with os.fdopen(fd, "w") as f:
                json.dump({"user_id": bytes_to_base64url(self._user_id), "keys": [asdict(k) for k in self._keys]}, f, indent=1)
            os.chmod(tmp, 0o600)
            os.replace(tmp, self.path)
        finally:
            if os.path.exists(tmp):
                os.unlink(tmp)

    def list(self) -> list[Passkey]:
        with self._lock:
            return list(self._keys)

    def remove(self, key_id: str) -> Passkey | None:
        with self._lock:
            found = next((k for k in self._keys if k.id == key_id), None)
            if found:
                self._keys = [k for k in self._keys if k.id != key_id]
                self._save()
            return found

    # -- challenges ------------------------------------------------------------------------
    def _ticket(self, challenge: bytes, purpose: str) -> str:
        now = time.time()
        self._challenges = {t: c for t, c in self._challenges.items() if c[2] > now}
        ticket = secrets.token_urlsafe(18)
        self._challenges[ticket] = (challenge, purpose, now + _CHALLENGE_TTL)
        return ticket

    def _take(self, ticket: str, purpose: str) -> bytes:
        c = self._challenges.pop(ticket, None)              # single use
        if c is None or c[1] != purpose or c[2] < time.time():
            raise PasskeyError("This passkey request has expired. Try again.")
        return c[0]

    # -- registration ----------------------------------------------------------------------
    def registration_options(self) -> dict[str, Any]:
        if not available():
            raise PasskeyError("Passkeys need the optional webauthn package on the server.")
        with self._lock:
            if len(self._keys) >= _MAX_KEYS:
                raise PasskeyError(f"At most {_MAX_KEYS} passkeys. Remove one first.")
            opts = webauthn.generate_registration_options(
                rp_id=self.rp_id, rp_name=self.rp_name, user_id=self._user_id, user_name="owner", user_display_name="Owner",
                authenticator_selection=AuthenticatorSelectionCriteria(resident_key=ResidentKeyRequirement.PREFERRED,
                                                                       user_verification=UserVerificationRequirement.REQUIRED),
                exclude_credentials=[PublicKeyCredentialDescriptor(id=base64url_to_bytes(k.id)) for k in self._keys])
            return {"ticket": self._ticket(opts.challenge, "register"), "publicKey": json.loads(webauthn.options_to_json(opts))}

    def register(self, ticket: str, credential: dict[str, Any], name: str) -> Passkey:
        with self._lock:
            challenge = self._take(ticket, "register")
            try:
                v = webauthn.verify_registration_response(credential=credential, expected_challenge=challenge,
                                                          expected_rp_id=self.rp_id, expected_origin=self.origin,
                                                          require_user_verification=True)
            except Exception as e:  # noqa: BLE001 - the library raises several types; the owner needs one clear message
                raise PasskeyError("The browser's answer could not be verified. Nothing was saved.") from e
            key = Passkey(id=bytes_to_base64url(v.credential_id), public_key=bytes_to_base64url(v.credential_public_key),
                          sign_count=v.sign_count, name=(name.strip()[:40] or f"Passkey {len(self._keys) + 1}"),
                          created_at=time.time())
            if any(k.id == key.id for k in self._keys):
                raise PasskeyError("This passkey is already registered.")
            self._keys.append(key)
            self._save()
            return key

    # -- sign-in ---------------------------------------------------------------------------
    def authentication_options(self) -> dict[str, Any]:
        if not available():
            raise PasskeyError("Passkeys need the optional webauthn package on the server.")
        with self._lock:
            if not self._keys:
                raise PasskeyError("No passkey has been added yet. Sign in with the owner password first.")
            opts = webauthn.generate_authentication_options(
                rp_id=self.rp_id, user_verification=UserVerificationRequirement.REQUIRED,
                allow_credentials=[PublicKeyCredentialDescriptor(id=base64url_to_bytes(k.id)) for k in self._keys])
            return {"ticket": self._ticket(opts.challenge, "authenticate"), "publicKey": json.loads(webauthn.options_to_json(opts))}

    def authenticate(self, ticket: str, credential: dict[str, Any]) -> Passkey:
        with self._lock:
            challenge = self._take(ticket, "authenticate")
            key = next((k for k in self._keys if k.id == str(credential.get("id", ""))), None)
            if key is None:
                raise PasskeyError("This passkey is not registered on this server.")
            try:
                v = webauthn.verify_authentication_response(
                    credential=credential, expected_challenge=challenge, expected_rp_id=self.rp_id, expected_origin=self.origin,
                    credential_public_key=base64url_to_bytes(key.public_key), credential_current_sign_count=key.sign_count,
                    require_user_verification=True)
            except Exception as e:  # noqa: BLE001
                raise PasskeyError("The passkey could not be verified.") from e
            key.sign_count, key.last_used_at = v.new_sign_count, time.time()
            self._save()
            return key
