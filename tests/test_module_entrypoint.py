"""The package entrypoint must launch once without touching real event data."""

import runpy

import pytest
import uvicorn

from vc_ktv.application import factory


@pytest.mark.parametrize(
    ("host", "port", "expected_host", "expected_port"),
    [(None, None, "127.0.0.1", 8778), ("0.0.0.0", "9012", "0.0.0.0", 9012)],
)
def test_python_module_entrypoint(
    monkeypatch, host, port, expected_host, expected_port
):
    app = object()
    calls = []
    monkeypatch.setattr(factory, "create_app", lambda: app)
    monkeypatch.setattr(
        uvicorn, "run", lambda *args, **kwargs: calls.append((args, kwargs))
    )
    for name, value in (("VC_KTV_HOST", host), ("VC_KTV_PORT", port)):
        if value is None:
            monkeypatch.delenv(name, raising=False)
        else:
            monkeypatch.setenv(name, value)

    runpy.run_module("vc_ktv", run_name="__main__")

    assert calls == [((app,), {"host": expected_host, "port": expected_port})]
