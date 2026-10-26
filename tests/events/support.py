import json
import os
import shutil
import socket
import tempfile
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer


class DatagramReader:
    """A Unix datagram receiver, optionally slow."""

    def __init__(self, delay_s: float = 0.0):
        self.dir = tempfile.mkdtemp(prefix="aev")
        self.path = os.path.join(self.dir, "e.sock")
        self.sock = socket.socket(socket.AF_UNIX, socket.SOCK_DGRAM)
        self.sock.setsockopt(socket.SOL_SOCKET, socket.SO_RCVBUF, 1 << 20)
        self.sock.bind(self.path)
        self.sock.settimeout(0.2)
        self.delay_s = delay_s
        self.events: list[dict] = []
        self.raw: list[bytes] = []
        self._stop = False
        self._thread = threading.Thread(target=self._loop, daemon=True)
        self._thread.start()

    @property
    def endpoint(self) -> str:
        return f"unix:{self.path}"

    def _loop(self):
        while not self._stop:
            try:
                data = self.sock.recv(1 << 17)
            except (TimeoutError, OSError):
                continue
            self.raw.append(data)
            self.events.append(json.loads(data))
            if self.delay_s:
                time.sleep(self.delay_s)

    def wait_for(self, count: int, timeout: float = 5.0) -> list[dict]:
        deadline = time.monotonic() + timeout
        while len(self.events) < count and time.monotonic() < deadline:
            time.sleep(0.01)
        return self.events

    def close(self):
        self._stop = True
        self._thread.join(1)
        self.sock.close()
        shutil.rmtree(self.dir, ignore_errors=True)


class HttpReceiver:
    """Collects NDJSON POSTs. Fails the first `fail_first` requests with 503,
    and any body over `max_body` bytes with 413."""

    def __init__(self, fail_first: int = 0, max_body: int | None = None):
        receiver = self
        self.requests: list[tuple[dict, list[dict]]] = []
        self.rejected: list[tuple[int, bytes]] = []
        self.sizes: list[int] = []
        self.fail_first = fail_first
        self.max_body = max_body

        class Handler(BaseHTTPRequestHandler):
            def do_POST(self):
                body = self.rfile.read(int(self.headers["Content-Length"]))
                status = None
                if receiver.fail_first > 0:
                    receiver.fail_first -= 1
                    status = 503
                elif receiver.max_body is not None and len(body) > receiver.max_body:
                    status = 413
                if status is not None:
                    receiver.rejected.append((status, body))
                    self.send_response(status)
                    self.end_headers()
                    return
                receiver.sizes.append(len(body))
                lines = [json.loads(line) for line in body.decode().splitlines() if line]
                receiver.requests.append((dict(self.headers), lines))
                self.send_response(202)
                self.end_headers()

            def log_message(self, *args):
                pass

        self.server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        self._thread = threading.Thread(target=self.server.serve_forever, daemon=True)
        self._thread.start()

    @property
    def endpoint(self) -> str:
        return f"http://127.0.0.1:{self.server.server_address[1]}/events"

    @property
    def events(self) -> list[dict]:
        return [e for _, lines in self.requests for e in lines]

    def close(self):
        self.server.shutdown()
        self.server.server_close()


def closed_port() -> int:
    s = socket.socket()
    s.bind(("127.0.0.1", 0))
    port = s.getsockname()[1]
    s.close()
    return port
