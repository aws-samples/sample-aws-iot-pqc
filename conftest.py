"""
Shared pytest fixtures for the workshop's Python unit tests.

Guarantees tests never touch real AWS: dummy credentials + us-east-1 are set
before boto3/moto import, and a `mock_aws` fixture is provided for convenience.
"""
import os
import sys

import pytest

# Dummy credentials so a stray boto3 call can never reach a real account.
os.environ.setdefault("AWS_ACCESS_KEY_ID", "testing")
os.environ.setdefault("AWS_SECRET_ACCESS_KEY", "testing")
os.environ.setdefault("AWS_SESSION_TOKEN", "testing")
os.environ.setdefault("AWS_SECURITY_TOKEN", "testing")
os.environ.setdefault("AWS_DEFAULT_REGION", "us-east-1")
os.environ.setdefault("AWS_REGION", "us-east-1")


@pytest.fixture
def aws_credentials():
    """No-op marker fixture; env is already set at import time."""
    return {
        "region": os.environ["AWS_REGION"],
        "account": "087195661091",
    }


@pytest.fixture
def mocked_aws():
    """Activate moto's unified AWS mock for the duration of a test."""
    from moto import mock_aws

    with mock_aws():
        yield


def load_handler_module(index_path, module_name):
    """
    Import a Lambda ``index.py`` by file path under a unique module name.

    Lambda handler files are all named ``index.py`` in sibling directories, so
    importing them by plain name collides. This loads each explicitly and adds
    its directory to sys.path first (so a handler importing local siblings
    works).
    """
    import importlib.util

    index_dir = os.path.dirname(os.path.abspath(index_path))
    if index_dir not in sys.path:
        sys.path.insert(0, index_dir)
    spec = importlib.util.spec_from_file_location(module_name, index_path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module
