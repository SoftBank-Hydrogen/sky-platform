"""Exclusive access to a deployment server's local state directory."""

from __future__ import annotations

import fcntl
from pathlib import Path


class StateDirectoryLock:
    """Hold an OS lock for the lifetime of one server process."""

    def __init__(self, root: Path):
        self.root = root.resolve()
        self.file = None

    def __enter__(self):
        self.root.mkdir(mode=0o700, parents=True, exist_ok=True)
        self.root.chmod(0o700)
        self.file = (self.root / '.server.lock').open('a+')
        self.file_path = self.root / '.server.lock'
        self.file_path.chmod(0o600)
        try:
            fcntl.flock(self.file, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            self.file.close()
            self.file = None
            raise RuntimeError(f'이미 다른 Sky 서버가 이 상태 디렉터리를 사용 중입니다: {self.root}') from None
        return self.root

    def __exit__(self, *_):
        if self.file is not None:
            fcntl.flock(self.file, fcntl.LOCK_UN)
            self.file.close()
            self.file = None
