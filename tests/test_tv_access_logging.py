"""No TV-control bearer or short-lived pairing capability in access logs."""

import logging

from vc_ktv.infra.log_filter import RedactURLSecrets


def test_legacy_tv_tokens_and_pairing_links_are_redacted():
    filter_ = RedactURLSecrets()
    for raw, expected in (
        ("/api/tv/state?token=secret&x=1", "/api/tv/state?token=[redacted]&x=1"),
        ("/tv.html?pair=123456-abc", "/tv.html?pair=[redacted]"),
        ("/api/tv/next", "/api/tv/next"),
        (
            "/api/tv/state?token=foo&pair=bar",
            "/api/tv/state?token=[redacted]&pair=[redacted]",
        ),
    ):
        record = logging.LogRecord(
            "uvicorn.access",
            logging.INFO,
            "file",
            1,
            "%s - %s %s HTTP/%s %s",
            ("client", "GET", raw, "1.1", 200),
            None,
        )
        assert filter_.filter(record)
        assert record.args[2] == expected
        assert "secret" not in str(record.getMessage())
