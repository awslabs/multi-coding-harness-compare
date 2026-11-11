# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: Apache-2.0
import os
import secrets
from pathlib import Path


def access_token(root: Path) -> str:
    root.mkdir(parents=True, exist_ok=True)
    path = root / "ui-token"
    try:
        fd = os.open(path, os.O_CREAT | os.O_EXCL | os.O_WRONLY, 0o600)
    except FileExistsError:
        return path.read_text().strip()
    token = secrets.token_urlsafe(32)
    with os.fdopen(fd, "w") as file:
        file.write(token)
    return token
