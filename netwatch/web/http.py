"""Petición y respuesta independientes de http.server, para poder probar los endpoints sin red."""
from __future__ import annotations

import json
from dataclasses import dataclass, field
from typing import Any, Dict, List

JSON = "application/json"
TEXT = "text/plain; charset=utf-8"


@dataclass
class Request:
    method: str
    path: str
    query: Dict[str, List[str]] = field(default_factory=dict)
    body: bytes = b""

    def json(self) -> Any:
        return json.loads(self.body or b"{}")


@dataclass
class Response:
    status: int
    body: bytes
    content_type: str
    headers: Dict[str, str] = field(default_factory=dict)

    @classmethod
    def json(cls, data: Any, status: int = 200) -> "Response":
        return cls(status, json.dumps(data).encode(), JSON)

    @classmethod
    def text(cls, text: str, status: int = 200) -> "Response":
        return cls(status, text.encode(), TEXT)

    @classmethod
    def error(cls, message: str, status: int = 400) -> "Response":
        return cls.json({"error": message}, status)
