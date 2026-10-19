# Copyright 2026 Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0

"""
builder_core — shared implementation for the Builder-Pi.

The Flask GUI (app.py) and the workshop-builder CLI (cli.py) both drive device
operations through this package, so behavior cannot drift between them.

Modules:
  config      — load/save the builder config.json
  aws         — boto3 session + SigV4-signed API Gateway calls (SSM instance role)
  logging     — structured, timestamped logging to stdout + a persistent file
  operations  — the discrete device operations (register, reflash, flash,
                deregister, wait-for-build, read-serial, service control)
"""
