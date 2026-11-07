"""Encode a NetEase web-API form request for its standard QR login endpoints.

Uses the public NetEase RSA key and AES-CBC envelope. No password is involved.
"""

from __future__ import annotations

import base64
import json
import secrets
import string

from Crypto.Cipher import AES
from Crypto.PublicKey import RSA
from Crypto.Util.Padding import pad

_IV = b"0102030405060708"
_PRESET_KEY = b"0CoJUm6Qyw8W8jud"
_PUBLIC_KEY = RSA.import_key(
    "-----BEGIN PUBLIC KEY-----\n"
    "MIGfMA0GCSqGSIb3DQEBAQUAA4GNADCBiQKBgQDgtQn2JZ34ZC28NWYpAUd98iZ37BUrX/aKzmFbt7clFSs6sXqHauqKWqdtLkF2KexO40H1YTX8z2lSgBBOAxLsvaklV8k4cBFK9snQXE9/DDaFt6Rr7iVZMldczhC0JNgTz+SHXT6CBHuX3e9SdB1Ua44oncaTWz7OBGLbCiK45wIDAQAB\n"
    "-----END PUBLIC KEY-----"
)


def encode_weapi_form(values: dict) -> dict[str, str]:
    """Two AES-CBC layers and raw RSA public-key encryption of the session key."""
    secret = "".join(
        secrets.choice(string.ascii_letters + string.digits) for _ in range(16)
    ).encode()
    plaintext = json.dumps(values, separators=(",", ":"), ensure_ascii=False).encode()
    first = base64.b64encode(
        AES.new(_PRESET_KEY, AES.MODE_CBC, _IV).encrypt(pad(plaintext, 16))
    )
    params = base64.b64encode(
        AES.new(secret, AES.MODE_CBC, _IV).encrypt(pad(first, 16))
    ).decode()
    key_int = int.from_bytes(secret[::-1], "big")
    enc_sec_key = f"{pow(key_int, _PUBLIC_KEY.e, _PUBLIC_KEY.n):0256x}"
    return {"params": params, "encSecKey": enc_sec_key}
