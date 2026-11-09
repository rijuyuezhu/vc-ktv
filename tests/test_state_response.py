"""Large state snapshots use an opt-in wire compression, with unchanged JSON."""

import gzip
import json

from fastapi import Request

from vc_ktv.api.state_response import state_response


def request(accept_encoding):
    return Request(
        {
            "type": "http",
            "method": "GET",
            "path": "/api/state",
            "headers": [(b"accept-encoding", accept_encoding.encode())],
        }
    )


def test_large_snapshot_is_compressed_and_json_equivalent():
    data = {
        "queue": [
            {"id": i, "title": f"Example song {i}", "requester": "Singer"}
            for i in range(1000)
        ],
        "current": None,
    }
    original = state_response(request("identity"), data)
    compressed = state_response(request("gzip, deflate"), data)
    assert "content-encoding" not in original.headers
    assert compressed.headers["content-encoding"] == "gzip"
    assert compressed.headers["vary"] == "Accept-Encoding"
    assert int(compressed.headers["content-length"]) == len(compressed.body)
    assert len(compressed.body) * 4 < len(original.body)
    assert gzip.decompress(compressed.body) == original.body
    assert json.loads(gzip.decompress(compressed.body)) == data


def test_small_snapshot_and_disabled_gzip_remain_plain_json():
    data = {"queue": [], "current": None}
    for encoding in ("gzip", "gzip;q=0", "gzip;q=invalid", "identity"):
        response = state_response(request(encoding), data)
        assert "content-encoding" not in response.headers
        assert response.body == b'{"queue":[],"current":null}'
