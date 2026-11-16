import base64
import hashlib
import json
import os
import re
import struct
from unittest.mock import MagicMock
from urllib.parse import parse_qs, urlparse

import cbor2
from cryptography.hazmat.primitives import hashes
from cryptography.hazmat.primitives.asymmetric import ec, rsa
from webauthn.helpers import bytes_to_base64url


CLEAR_GH_PAYLOAD = {
    "ip": "154.160.1.1",
    "country": "GH",
    "privacy": {
        "vpn": False,
        "proxy": False,
        "tor": False,
        "relay": False,
        "hosting": False,
    },
}


def make_mock_urlopen(body_bytes: bytes, status: int = 200):
    response = MagicMock()
    response.status = status
    response.read.return_value = body_bytes
    response.__enter__.return_value = response
    response.__exit__.return_value = False
    return response


def extract_token_from_email_body(body: str) -> str:
    match = re.search(r"https?://[^\s]+", body)
    if not match:
        raise AssertionError(f"No URL found in email body: {body}")
    parsed = urlparse(match.group(0))
    tokens = parse_qs(parsed.query).get("token")
    if not tokens:
        raise AssertionError(f"No token query parameter in URL: {match.group(0)}")
    return tokens[0]


def _b64url_uint(value: int) -> str:
    length = max(1, (value.bit_length() + 7) // 8)
    return base64.urlsafe_b64encode(value.to_bytes(length, "big")).rstrip(b"=").decode(
        "ascii"
    )


def _make_rsa_jwk_and_private_key(kid: str):
    private_key = rsa.generate_private_key(public_exponent=65537, key_size=2048)
    pub_numbers = private_key.public_key().public_numbers()
    jwk = {
        "kty": "RSA",
        "kid": kid,
        "use": "sig",
        "alg": "RS256",
        "n": _b64url_uint(pub_numbers.n),
        "e": _b64url_uint(pub_numbers.e),
    }
    return private_key, {"keys": [jwk]}


def _build_webauthn_registration_payload(options: dict, *, rp_id="localhost", origin="http://localhost:5173"):
    private_key = ec.generate_private_key(ec.SECP256R1())
    pub_nums = private_key.public_key().public_numbers()
    cose_key = {
        1: 2,
        3: -7,
        -1: 1,
        -2: pub_nums.x.to_bytes(32, "big"),
        -3: pub_nums.y.to_bytes(32, "big"),
    }
    cred_id = os.urandom(32)
    rp_hash = hashlib.sha256(rp_id.encode("utf-8")).digest()
    flags = bytes([0x41])  # UP + AT
    sign_count = struct.pack(">I", 1)
    aaguid = b"\x00" * 16
    cred_len = struct.pack(">H", len(cred_id))
    auth_data = (
        rp_hash + flags + sign_count + aaguid + cred_len + cred_id + cbor2.dumps(cose_key)
    )
    att_obj = cbor2.dumps({"fmt": "none", "attStmt": {}, "authData": auth_data})
    client_data = json.dumps(
        {
            "type": "webauthn.create",
            "challenge": options["challenge"],
            "origin": origin,
        }
    ).encode("utf-8")
    cred_id_b64 = bytes_to_base64url(cred_id)
    credential = {
        "id": cred_id_b64,
        "rawId": cred_id_b64,
        "type": "public-key",
        "response": {
            "clientDataJSON": bytes_to_base64url(client_data),
            "attestationObject": bytes_to_base64url(att_obj),
            "transports": ["internal"],
        },
    }
    return private_key, cred_id, credential


def _build_webauthn_assertion_payload(
    private_key,
    cred_id: bytes,
    options: dict,
    *,
    sign_count: int = 2,
    user_handle: bytes | None = None,
    rp_id: str = "localhost",
    origin: str = "http://localhost:5173",
):
    rp_hash = hashlib.sha256(rp_id.encode("utf-8")).digest()
    auth_data = rp_hash + bytes([0x01]) + struct.pack(">I", sign_count)
    client_data = json.dumps(
        {
            "type": "webauthn.get",
            "challenge": options["challenge"],
            "origin": origin,
        }
    ).encode("utf-8")
    signature = private_key.sign(
        auth_data + hashlib.sha256(client_data).digest(),
        ec.ECDSA(hashes.SHA256()),
    )
    cred_id_b64 = bytes_to_base64url(cred_id)
    return {
        "id": cred_id_b64,
        "rawId": cred_id_b64,
        "type": "public-key",
        "response": {
            "clientDataJSON": bytes_to_base64url(client_data),
            "authenticatorData": bytes_to_base64url(auth_data),
            "signature": bytes_to_base64url(signature),
            "userHandle": bytes_to_base64url(user_handle) if user_handle else None,
        },
    }
