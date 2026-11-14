"""Tiny HTTP status server. 200 until a finished run fails, then 503."""

import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

_OK = b"ok\n"
_FAIL = b"fail\n"


class HealthState:
    """Last finished run. ``None`` means no run has finished yet, which is healthy."""

    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._last_ok: bool | None = None

    def mark(self, ok: bool) -> None:
        with self._lock:
            self._last_ok = ok

    def status_code(self) -> int:
        with self._lock:
            failed = self._last_ok is False
        return 503 if failed else 200


class HealthServer:
    """Bind all IPv4 interfaces. ``close`` returns within about a second."""

    def __init__(self, port: int, state: HealthState) -> None:
        self._httpd = _Server(("0.0.0.0", port), _handler_for(state))
        self._httpd.timeout = 0.5
        self.port = int(self._httpd.server_address[1])
        self._stop = False
        self._thread = threading.Thread(target=self._serve, name="questdb-backup-health", daemon=True)

    def start(self) -> None:
        self._thread.start()

    def close(self) -> None:
        self._stop = True
        if self._thread.is_alive():
            self._thread.join(timeout=2)
        self._httpd.server_close()

    def _serve(self) -> None:
        while not self._stop:
            self._httpd.handle_request()


class _Server(ThreadingHTTPServer):
    allow_reuse_address = True
    daemon_threads = True


def _handler_for(state: HealthState) -> type[BaseHTTPRequestHandler]:
    class Handler(BaseHTTPRequestHandler):
        def do_GET(self) -> None:
            _respond(self, state, send_body=True)

        def do_HEAD(self) -> None:
            _respond(self, state, send_body=False)

        def log_message(self, format: str, *args: object) -> None:
            return

    return Handler


def _respond(handler: BaseHTTPRequestHandler, state: HealthState, *, send_body: bool) -> None:
    code = state.status_code()
    body = _OK if code == 200 else _FAIL
    handler.send_response(code)
    handler.send_header("Content-Type", "text/plain; charset=utf-8")
    handler.send_header("Content-Length", str(len(body)))
    handler.end_headers()
    if send_body:
        handler.wfile.write(body)
