"""Traduce las peticiones HTTP a llamadas de los servicios."""
from __future__ import annotations

import time
from dataclasses import dataclass

from netwatch.services.devices import DeviceService
from netwatch.services.event_query import EventQueryService
from netwatch.services.export import CsvExporter
from netwatch.services.scanner import HostScanner
from netwatch.services.summary import SummaryService
from netwatch.web.http import Request, Response
from netwatch.web.router import Router
from netwatch.web.static import StaticFiles


@dataclass(frozen=True)
class Services:
    summary: SummaryService
    events: EventQueryService
    exporter: CsvExporter
    scanner: HostScanner
    devices: DeviceService


def register_pages(router: Router, static: StaticFiles) -> None:
    for path in ("/", "/index.html"):
        router.add("GET", path, static.page("index.html"))
    for path in ("/docs", "/docs.html"):
        router.add("GET", path, static.page("docs.html"))
    router.add("GET", "/favicon.ico", static.page("img/neto.png"))
    router.fallback("GET", static)


def register_api(router: Router, services: Services) -> None:
    @router.get("/api/summary")
    def summary(_request: Request) -> Response:
        return Response.json(services.summary.build())

    @router.get("/api/events")
    def events(request: Request) -> Response:
        try:
            return Response.json(services.events.page(request.query))
        except ValueError as exc:
            return Response.error(str(exc))

    @router.get("/api/export.csv")
    def export(_request: Request) -> Response:
        name = time.strftime("netwatch-%Y%m%d-%H%M.csv")
        return Response(200, services.exporter.export(), "text/csv; charset=utf-8",
                        {"Content-Disposition": f'attachment; filename="{name}"'})

    @router.post("/api/scan")
    def scan(request: Request) -> Response:
        try:
            data = request.json()
            return Response.json(services.scanner.scan(data.get("host"), data.get("ports")))
        except ValueError as exc:  # incluye JSON mal formado
            return Response.error(str(exc))
        except AttributeError:     # el cuerpo no es un objeto JSON
            return Response.error("petición inválida")

    @router.post("/api/alias")
    def alias(request: Request) -> Response:
        return _edit_device(request, lambda d: services.devices.set_alias(d["mac"], d.get("alias")))

    @router.post("/api/network")
    def network(request: Request) -> Response:
        return _edit_device(request, lambda d: services.devices.set_network(d["mac"], d.get("network")))


def _edit_device(request: Request, apply) -> Response:
    try:
        apply(request.json())
    except (ValueError, KeyError, TypeError, AttributeError):
        return Response.error("petición inválida")
    return Response.json({})
