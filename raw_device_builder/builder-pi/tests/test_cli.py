# Copyright 2026 Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0

"""
Unit tests for the workshop-builder CLI argument parsing and routing.

Operations and DeviceManager are mocked, so no AWS/serial access happens. We
assert each subcommand parses and routes to the right operation, and that the
process exits non-zero on failure.
"""
import os
import sys

import pytest

HERE = os.path.dirname(os.path.abspath(__file__))
BUILDER_PI = os.path.abspath(os.path.join(HERE, ".."))
sys.path.insert(0, BUILDER_PI)

import cli  # noqa: E402


@pytest.fixture(autouse=True)
def _config_and_logs(monkeypatch, tmp_path):
    # Load a fixed config and keep logs in a temp dir (no /var/log writes).
    monkeypatch.setattr(cli, "load_config", lambda: {
        "api_endpoint": "https://api.example.com/prod",
        "region": "us-east-1",
    })
    monkeypatch.setenv("WORKSHOP_BUILDER_LOG_DIR", str(tmp_path / "logs"))


def test_parser_has_all_subcommands():
    parser = cli.build_parser()
    # Parse a representative invocation of each subcommand without error.
    for argv in (["scan"], ["register", "--serial", "X"], ["reflash", "--port", "/dev/ttyACM0"],
                 ["flash", "--port", "/dev/ttyACM0", "--bin", "/tmp/f.bin"],
                 ["deregister", "--serial", "X"], ["serial", "--port", "/dev/ttyACM0"],
                 ["service", "status"]):
        args = parser.parse_args(argv)
        assert args.command == argv[0]


def test_register_routes_to_operation(monkeypatch):
    called = {}

    def fake_register(config, serial, logger=None):
        called["serial"] = serial
        return {"success": True, "message": "ok", "buildId": None}

    monkeypatch.setattr(cli.operations, "register_device", fake_register)
    rc = cli.main(["register", "--serial", "FC22E0", "--no-wait", "--no-flash"])
    assert rc == 0
    assert called["serial"] == "FC22E0"


def test_register_without_serial_or_port_fails(monkeypatch):
    rc = cli.main(["register", "--no-wait", "--no-flash"])
    assert rc == 1  # cannot resolve serial -> failure exit code


def test_deregister_routes_to_operation(monkeypatch):
    called = {}

    def fake_deregister(config, serial, logger=None):
        called["serial"] = serial
        return {"success": True, "message": "ok"}

    monkeypatch.setattr(cli.operations, "deregister_device", fake_deregister)
    rc = cli.main(["deregister", "--serial", "FC22E0"])
    assert rc == 0
    assert called["serial"] == "FC22E0"


def test_failure_exit_code(monkeypatch):
    monkeypatch.setattr(cli.operations, "deregister_device",
                        lambda config, serial, logger=None: {"success": False, "message": "nope"})
    rc = cli.main(["deregister", "--serial", "FC22E0"])
    assert rc == 1


def test_exception_exits_nonzero(monkeypatch):
    def boom(*a, **k):
        raise RuntimeError("kaboom")
    monkeypatch.setattr(cli.operations, "deregister_device", boom)
    rc = cli.main(["deregister", "--serial", "FC22E0"])
    assert rc == 1
