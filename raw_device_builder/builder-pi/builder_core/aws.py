# Copyright 2026 Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0

"""
AWS helpers shared by the Builder-Pi GUI and CLI.

The Builder-Pi authenticates to the device-builder REST API with its own SSM
managed-instance role via SigV4 — no long-lived secrets. SSM rotates the
instance credentials frequently, so a fresh session is created per call.
"""
import json

import boto3
import requests


def fresh_session(region):
    """A boto3 session using the current (rotating) instance credentials."""
    return boto3.session.Session(region_name=region)


def sigv4_post(api_url, payload, region, timeout=30):
    """
    POST a JSON payload to an API Gateway endpoint, SigV4-signed with the
    instance role. Returns the requests.Response.
    """
    from botocore.auth import SigV4Auth
    from botocore.awsrequest import AWSRequest

    session = fresh_session(region)
    credentials = session.get_credentials().get_frozen_credentials()

    body = json.dumps(payload)
    aws_request = AWSRequest(
        method="POST", url=api_url, data=body,
        headers={"Content-Type": "application/json"},
    )
    SigV4Auth(credentials, "execute-api", region).add_auth(aws_request)
    return requests.post(api_url, data=body, headers=dict(aws_request.headers), timeout=timeout)
