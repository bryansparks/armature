"""The Status page's content: a host command's Markdown, refreshed on a background thread."""
from __future__ import annotations

import logging
import shutil
import tempfile
import threading
from collections.abc import Callable
from datetime import datetime
from pathlib import Path
from typing import Any

from sqlalchemy import Engine

from armature.server import hostcmd
from armature.server.config import StatusConfig
from armature.server.crypto import Keyring
from armature.server.db import utcnow

log = logging.getLogger(__name__)

MARKDOWN_MAX = 256 * 1024
ERROR_MAX = 500


class StatusHook:
    """Runs ``status.command`` every ``interval_seconds`` and keeps its latest redacted output."""

    def __init__(self, engine: Engine, keyring: Keyring, cfg: StatusConfig, *,
                 clock: Callable[[], datetime] = utcnow):
        self.engine = engine
        self.keyring = keyring
        self.cfg = cfg
        self._clock = clock
        self._lock = threading.Lock()
        self._stop = threading.Event()
        self._thread: threading.Thread | None = None
        self.markdown: str | None = None
        self.updated_at: datetime | None = None
        self.error: str | None = None

    def start(self) -> None:
        if self._thread is None:
            self._thread = threading.Thread(target=self._loop, name="armature-status", daemon=True)
            self._thread.start()

    def stop(self, timeout: float | None = None) -> None:
        self._stop.set()
        if self._thread is not None:
            self._thread.join(self.cfg.timeout_seconds + 10 if timeout is None else timeout)

    def _loop(self) -> None:
        while not self._stop.is_set():
            self.refresh()
            self._stop.wait(self.cfg.interval_seconds)

    def refresh(self) -> bool:
        """Run the command once. Keeps the last good output when this run fails, and returns False then."""
        markdown = error = None
        scratch = Path(tempfile.mkdtemp(prefix="armature-status-"))
        try:
            result = hostcmd.run(self.cfg.command, cwd=hostcmd.working_dir(self.cfg.cwd), env=hostcmd.minimal_env(),
                                 timeout=self.cfg.timeout_seconds, scratch=scratch, read_limit=MARKDOWN_MAX)
            if result.error:
                error = result.error
            elif result.timed_out:
                error = f"The status command did not finish within {self.cfg.timeout_seconds:g} s."
            elif result.exit_code != 0:
                lines = [line for line in result.stderr.splitlines() if line.strip()]
                error = f"The status command exited {result.exit_code}" + (f": {lines[-1]}" if lines else ".")
            else:
                markdown = result.stdout
            with self.engine.connect() as conn:
                secret_values = hostcmd.instance_secret_values(conn, self.keyring)
            if markdown is not None:
                markdown = hostcmd.cap(hostcmd.scrub(markdown, secret_values), MARKDOWN_MAX)
            if error is not None:
                error = hostcmd.cap(hostcmd.scrub(error, secret_values), ERROR_MAX)
        except Exception:
            log.exception("status refresh failed")
            markdown, error = None, "The status refresh failed. The server log says why."
        finally:
            shutil.rmtree(scratch, ignore_errors=True)
        with self._lock:
            if markdown is not None:
                self.markdown, self.updated_at = markdown, self._clock()
            self.error = error
        return error is None

    def snapshot(self) -> dict[str, Any]:
        with self._lock:
            return {"configured": True, "markdown": self.markdown, "updated_at": self.updated_at, "error": self.error}
