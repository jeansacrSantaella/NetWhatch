"""Adaptador entre http.server y el Router."""
from __future__ import annotations

import urllib.parse
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

from netwatch.web.http import Request, Response
from netwatch.web.router import Router


def make_handler(router: Router):
    class Handler(BaseHTTPRequestHandler):
        def log_message(self, *args):
            pass  # el dashboard pide /api/summary cada 5 s: no llenar la consola

        def do_GET(self):
            self._handle("GET")

        def do_POST(self):
            self._handle("POST")

        def _handle(self, method: str) -> None:
            url = urllib.parse.urlsplit(self.path)
            length = int(self.headers.get("Content-Length", 0) or 0)
            request = Request(method, url.path, urllib.parse.parse_qs(url.query, keep_blank_values=True),
                              self.rfile.read(length) if length else b"")
            try:
                response = router.dispatch(request)
            except Exception as e:
                print("http error:", e, flush=True)
                response = Response.error("error interno", 500)
            self._send(response)

        def _send(self, response: Response) -> None:
            self.send_response(response.status)
            self.send_header("Content-Type", response.content_type)
            for k, v in response.headers.items():
                self.send_header(k, v)
            self.send_header("Content-Length", str(len(response.body)))
            self.end_headers()
            self.wfile.write(response.body)

    return Handler


def serve(host: str, port: int, router: Router) -> None:
    ThreadingHTTPServer((host, port), make_handler(router)).serve_forever()
