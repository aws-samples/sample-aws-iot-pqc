# Copyright 2026 Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0

"""
Structured, timestamped logging for Builder-Pi operations.

Every operation logs to stdout/stderr AND appends to a persistent per-command
log file, so an SSM `send-command` transcript is a self-describing record of
what ran and how it ended.
"""
import datetime
import os
import sys

LOG_DIR = os.environ.get("WORKSHOP_BUILDER_LOG_DIR", "/var/log/workshop-builder")


def _timestamp():
    return datetime.datetime.now(datetime.timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


class OperationLogger:
    """Logs a single operation to stdout/stderr and a per-command file."""

    def __init__(self, command, log_dir=None, stream=None):
        self.command = command
        self.stream = stream or sys.stdout
        self.err_stream = sys.stderr
        self._log_dir = log_dir if log_dir is not None else LOG_DIR
        self._file = None
        # Best-effort file logging; never fail the operation because the log
        # directory is not writable (e.g. in a dev checkout).
        try:
            os.makedirs(self._log_dir, exist_ok=True)
            self._file = open(os.path.join(self._log_dir, f"{command}.log"), "a", encoding="utf-8")  # nosemgrep: open-never-closed
        except OSError:
            self._file = None

    def _emit(self, level, msg, stream):
        line = f"{_timestamp()} [{self.command}] {level}: {msg}"
        print(line, file=stream, flush=True)
        if self._file:
            self._file.write(line + "\n")
            self._file.flush()

    def info(self, msg):
        self._emit("INFO", msg, self.stream)

    def warn(self, msg):
        self._emit("WARN", msg, self.err_stream)

    def error(self, msg):
        self._emit("ERROR", msg, self.err_stream)

    def close(self):
        if self._file:
            self._file.close()
            self._file = None
