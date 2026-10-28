"""Sirve archivos estáticos y bloquea rutas fuera de su carpeta."""
from __future__ import annotations

from pathlib import Path
from typing import Optional

from netwatch.web.http import Request, Response

PREFIX = "/static/"
CONTENT_TYPES = {
    ".html": "text/html; charset=utf-8",
    ".css": "text/css; charset=utf-8",
    ".js": "text/javascript; charset=utf-8",
    ".png": "image/png",
    ".svg": "image/svg+xml",
    ".json": "application/json",
}


class StaticFiles:
    def __init__(self, root: Path):
        self._root = root.resolve()

    def file(self, relative: str) -> Optional[Response]:
        path = (self._root / relative).resolve()
        if self._root not in path.parents or not path.is_file():
            return None
        content_type = CONTENT_TYPES.get(path.suffix.lower())
        if not content_type:
            return None
        return Response(200, path.read_bytes(), content_type, {"Cache-Control": "no-cache"})

    def page(self, relative: str):
        """Sirve un archivo fijo para una ruta (p. ej., / -> index.html)."""
        return lambda _request: self.file(relative) or Response.text("not found", 404)

    def __call__(self, request: Request) -> Response:
        if request.path.startswith(PREFIX):
            found = self.file(request.path[len(PREFIX):])
            if found:
                return found
        return Response.text("not found", 404)
