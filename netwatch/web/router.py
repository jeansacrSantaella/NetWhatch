"""Registra rutas y handlers sin modificar el servidor."""
from __future__ import annotations

from typing import Callable, Dict, Optional, Tuple

from netwatch.web.http import Request, Response

Handler = Callable[[Request], Response]


class Router:
    def __init__(self):
        self._routes: Dict[Tuple[str, str], Handler] = {}
        self._fallbacks: Dict[str, Handler] = {}

    def add(self, method: str, path: str, handler: Handler) -> None:
        self._routes[(method.upper(), path)] = handler

    def get(self, path: str) -> Callable[[Handler], Handler]:
        return self._decorator("GET", path)

    def post(self, path: str) -> Callable[[Handler], Handler]:
        return self._decorator("POST", path)

    def fallback(self, method: str, handler: Handler) -> None:
        """Handler para rutas no registradas de un método (p. ej. archivos estáticos)."""
        self._fallbacks[method.upper()] = handler

    def dispatch(self, request: Request) -> Response:
        handler: Optional[Handler] = self._routes.get((request.method, request.path)) \
            or self._fallbacks.get(request.method)
        return handler(request) if handler else Response.text("not found", 404)

    def _decorator(self, method: str, path: str) -> Callable[[Handler], Handler]:
        def register(handler: Handler) -> Handler:
            self.add(method, path, handler)
            return handler
        return register
