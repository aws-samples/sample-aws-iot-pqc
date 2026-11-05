# Copyright 2026 Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0
"""
Unit tests for the Builder-Pi GUI's network exposure controls (security ticket
D570691687): loopback-only bind, no runtime config writes, Host allowlist and
JSON-only POSTs. Uses Flask's test client; no network listener is started.
"""
import json
import os
import sys

import pytest

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, os.path.abspath(os.path.join(HERE, "..")))


@pytest.fixture(scope="module")
def app_module(tmp_path_factory):
    cfg = tmp_path_factory.mktemp("cfg") / "config.json"
    # An old Pi config: the legacy bind keys must be ignored, never honoured.
    cfg.write_text(json.dumps({
        "api_endpoint": "https://example.invalid/prod", "region": "us-east-1",
        "firmware_bucket": "pqc-workshop-firmware-123456789012",
        "serial_baud_rate": 115200, "gui_host": "0.0.0.0", "gui_port": 9999,
    }))
    os.environ["BUILDER_CONFIG"] = str(cfg)
    import app  # noqa: E402  (reads BUILDER_CONFIG at import)
    return app


@pytest.fixture
def client(app_module):
    return app_module.app.test_client()


LOCAL = {"Host": "127.0.0.1:8080"}


def test_bind_is_loopback_and_not_configurable(app_module):
    assert app_module.GUI_HOST == "127.0.0.1"
    assert app_module.GUI_PORT == 8080
    # The legacy config asked for 0.0.0.0:9999; the module constants win.
    assert app_module.config.get("gui_host") == "0.0.0.0"
    assert app_module.ALLOWED_HOSTS == {"127.0.0.1:8080", "localhost:8080"}


def test_bind_uses_the_constants(app_module, monkeypatch):
    """The __main__ block passes GUI_HOST/GUI_PORT to app.run, not config."""
    src = open(app_module.__file__, encoding="utf-8").read()
    assert "app.run(host=GUI_HOST, port=GUI_PORT, debug=False)" in src
    assert "0.0.0.0" not in src.replace("# ", "")  # no wildcard bind literal in code


@pytest.mark.parametrize("method", ["GET", "POST"])
def test_config_route_is_gone(client, method):
    resp = client.open("/api/config", method=method, headers=LOCAL, json={"firmware_bucket": "x"})
    assert resp.status_code in (404, 405)


def test_config_cannot_be_changed_over_http(app_module, client):
    before = dict(app_module.config)
    client.post("/api/config", headers=LOCAL, json={"firmware_bucket": "attacker-fw-bucket"})
    assert app_module.config == before


@pytest.mark.parametrize("host", [
    "192.168.1.75:8080",          # the Pi's LAN address
    "10.42.0.1:8080",             # the workshop AP address
    "attacker.example:8080",      # DNS rebinding to 127.0.0.1
    "127.0.0.1",                  # wrong port form
])
def test_foreign_host_header_is_refused(client, host):
    assert client.get("/api/devices", headers={"Host": host}).status_code == 403
    assert client.post("/api/reflash", headers={"Host": host},
                       json={"port": "/dev/ttyACM0"}).status_code == 403


@pytest.mark.parametrize("host", ["127.0.0.1:8080", "localhost:8080"])
def test_loopback_host_is_allowed(client, host):
    assert client.get("/api/devices", headers={"Host": host}).status_code == 200


@pytest.mark.parametrize("path", ["/api/register", "/api/reflash", "/api/deregister", "/api/scan"])
def test_post_without_json_content_type_is_refused(client, path):
    # A cross-site HTML form can only send these simple content types.
    for ctype in ("application/x-www-form-urlencoded", "text/plain", "multipart/form-data"):
        resp = client.post(path, headers={**LOCAL, "Content-Type": ctype}, data="port=/dev/ttyACM0")
        assert resp.status_code == 415, (path, ctype)


def test_post_with_non_object_json_is_a_clean_400(client):
    resp = client.post("/api/reflash", headers=LOCAL, json=["/dev/ttyACM0"])
    assert resp.status_code == 400


def test_no_cors_headers_are_granted(client):
    resp = client.options("/api/reflash", headers={
        **LOCAL, "Origin": "https://attacker.example",
        "Access-Control-Request-Method": "POST",
        "Access-Control-Request-Headers": "content-type"})
    assert "Access-Control-Allow-Origin" not in resp.headers
