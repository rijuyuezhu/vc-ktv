"""The QR login form encodes (not logs) the exact API payload."""

import base64

from Crypto.Cipher import AES
from Crypto.Util.Padding import unpad

from vc_ktv.netease_weapi import _IV, _PRESET_KEY, _PUBLIC_KEY, encode_weapi_form


def test_weapi_encrypt_decrypt_round_trip(monkeypatch):
    monkeypatch.setattr("vc_ktv.netease_weapi.secrets.choice", lambda _alphabet: "A")
    payload = {"type": 1, "key": "this_is_a_temporary_qr_key", "noCheckToken": True}
    form = encode_weapi_form(payload)
    assert set(form) == {"params", "encSecKey"}
    assert len(form["encSecKey"]) == 256
    assert (
        form["encSecKey"]
        == f"{pow(int.from_bytes(b'A' * 16, 'big'), _PUBLIC_KEY.e, _PUBLIC_KEY.n):0256x}"
    )
    second = unpad(
        AES.new(b"A" * 16, AES.MODE_CBC, _IV).decrypt(base64.b64decode(form["params"])),
        16,
    )
    first = unpad(
        AES.new(_PRESET_KEY, AES.MODE_CBC, _IV).decrypt(base64.b64decode(second)), 16
    )
    assert (
        first.decode()
        == '{"type":1,"key":"this_is_a_temporary_qr_key","noCheckToken":true}'
    )
    assert "this_is_a_temporary_qr_key" not in str(form)
