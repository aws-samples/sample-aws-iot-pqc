# Copyright 2026 Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0

"""Builder-Pi configuration load/save (single source, shared by GUI + CLI)."""
import json
import os

# config.json lives one level up from this package (builder-pi/config.json).
_DEFAULT_CONFIG_FILE = os.path.join(
    os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "config.json"
)


def config_path():
    """Path to the builder config file (override with BUILDER_CONFIG env var)."""
    return os.environ.get("BUILDER_CONFIG", _DEFAULT_CONFIG_FILE)


def load_config(path=None):
    with open(path or config_path(), "r", encoding="utf-8") as f:
        return json.load(f)


def save_config(config, path=None):
    with open(path or config_path(), "w", encoding="utf-8") as f:
        json.dump(config, f, indent=2)
