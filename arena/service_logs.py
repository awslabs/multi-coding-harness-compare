# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: Apache-2.0
"""Keep active service logs bounded; archive closed segments with run artifacts."""
import fcntl
import io
import os
import sys
import uuid


class ArtifactLog(io.TextIOBase):
    def __init__(self, root, role, max_bytes=5 * 1024 * 1024):
        self.root, self.role, self.max_bytes = root, role, max_bytes

    @property
    def encoding(self):
        return "utf-8"

    def write(self, text):
        if not text:
            return 0
        path = self.root / (self.role + ".log")
        with (self.root / (self.role + ".log.lock")).open("a") as lock:
            fcntl.flock(lock, fcntl.LOCK_EX)
            if path.exists() and path.stat().st_size >= self.max_bytes:
                logs = self.root / "logs"
                logs.mkdir(mode=0o700, exist_ok=True)
                path.rename(logs / (self.role + "-" + uuid.uuid4().hex + ".log"))
            fd = os.open(path, os.O_CREAT | os.O_WRONLY | os.O_APPEND | os.O_NOFOLLOW, 0o600)
            with os.fdopen(fd, "a", encoding="utf-8") as output:
                output.write(text)
        return len(text)


def configure_logs(config):
    role = os.environ.get("MCHC_SERVICE_LOG")
    if role in ("api", "worker"):
        sys.stdout = sys.stderr = ArtifactLog(config.root, role)
