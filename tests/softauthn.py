"""A software passkey for tests: makes the browser's WebAuthn answers (attestation 'none', ES256) for a given origin."""
import base64
import hashlib
import json
import os
import struct

import cbor2
from cryptography.hazmat.primitives import hashes
from cryptography.hazmat.primitives.asymmetric import ec


def b64u(b: bytes) -> str:
    return base64.urlsafe_b64encode(b).rstrip(b"=").decode()


def unb64u(s: str) -> bytes:
    return base64.urlsafe_b64decode(s + "=" * (-len(s) % 4))


class SoftAuthenticator:
    def __init__(self, origin: str, rp_id: str):
        self.origin, self.rp_id = origin, rp_id
        self.key = ec.generate_private_key(ec.SECP256R1())
        self.cred_id = os.urandom(16)
        self.count = 0

    def _cose(self) -> bytes:
        n = self.key.public_key().public_numbers()
        return cbor2.dumps({1: 2, 3: -7, -1: 1, -2: n.x.to_bytes(32, "big"), -3: n.y.to_bytes(32, "big")})

    def _client_data(self, kind: str, challenge: str, origin: str | None) -> bytes:
        return json.dumps({"type": kind, "challenge": challenge, "origin": origin or self.origin, "crossOrigin": False}).encode()

    def create(self, options: dict, origin: str | None = None) -> dict:
        rp_hash = hashlib.sha256(self.rp_id.encode()).digest()
        flags = 0x01 | 0x04 | 0x40                       # user present, user verified, attested credential data
        aaguid = b"\x00" * 16
        cred = aaguid + struct.pack(">H", len(self.cred_id)) + self.cred_id + self._cose()
        auth_data = rp_hash + bytes([flags]) + struct.pack(">I", self.count) + cred
        att = cbor2.dumps({"fmt": "none", "attStmt": {}, "authData": auth_data})
        cd = self._client_data("webauthn.create", options["challenge"], origin)
        return {"id": b64u(self.cred_id), "rawId": b64u(self.cred_id), "type": "public-key", "clientExtensionResults": {},
                "response": {"clientDataJSON": b64u(cd), "attestationObject": b64u(att), "transports": ["internal"]}}

    def get(self, options: dict, origin: str | None = None) -> dict:
        self.count += 1
        rp_hash = hashlib.sha256(self.rp_id.encode()).digest()
        auth_data = rp_hash + bytes([0x01 | 0x04]) + struct.pack(">I", self.count)
        cd = self._client_data("webauthn.get", options["challenge"], origin)
        sig = self.key.sign(auth_data + hashlib.sha256(cd).digest(), ec.ECDSA(hashes.SHA256()))
        return {"id": b64u(self.cred_id), "rawId": b64u(self.cred_id), "type": "public-key", "clientExtensionResults": {},
                "response": {"clientDataJSON": b64u(cd), "authenticatorData": b64u(auth_data), "signature": b64u(sig)}}
