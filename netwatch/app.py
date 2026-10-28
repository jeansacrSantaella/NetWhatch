"""Conecta los componentes concretos de la aplicación."""
from __future__ import annotations

from netwatch.alerts.telegram import TelegramClient, TelegramNotifier
from netwatch.config import Settings
from netwatch.core.events import EventBus, console_log
from netwatch.core.scheduler import Scheduler
from netwatch.monitors.connectivity import ConnectivityMonitor
from netwatch.monitors.device_quality import DeviceQualityMonitor
from netwatch.monitors.inventory import InventoryScanner
from netwatch.monitors.public_ip import PublicIpMonitor
from netwatch.monitors.retention import RetentionTask
from netwatch.monitors.speed import SpeedMonitor, SpeedTester
from netwatch.network.vendors import VendorLookup
from netwatch.services.devices import DeviceService
from netwatch.services.event_query import EventQueryService
from netwatch.services.export import CsvExporter
from netwatch.services.scanner import HostScanner
from netwatch.services.summary import SummaryService
from netwatch.storage.database import Database
from netwatch.storage.repositories import Repositories
from netwatch.web.api import Services, register_api, register_pages
from netwatch.web.router import Router
from netwatch.web.server import serve
from netwatch.web.static import StaticFiles


class Application:
    def __init__(self, settings: Settings):
        self.settings = settings
        self.db = Database(settings.db_path)
        self.db.migrate()
        self.repos = Repositories.on(self.db)

        self.bus = EventBus(self.repos.events)
        self.bus.subscribe(console_log)
        self.notifier = None
        if settings.telegram_enabled:
            client = TelegramClient(settings.telegram_token, settings.telegram_chat)
            self.notifier = TelegramNotifier(client, settings.alert_kinds)
            self.bus.subscribe(self.notifier)

        self.vendors = VendorLookup(settings.oui_path)
        self.connectivity = ConnectivityMonitor(
            self.repos.probes, self.bus, settings.external_hosts, settings.probe_every, settings.fails_to_alert)
        self.scheduler = Scheduler([
            InventoryScanner(self.repos.devices, self.bus, self.vendors, settings.scan_every,
                             settings.misses_to_offline),
            self.connectivity,
            PublicIpMonitor(self.repos.meta, self.bus, settings.ip_every),
            RetentionTask(self.repos.probes, settings.keep_days),
            DeviceQualityMonitor(self.repos.devices, self.repos.probes, settings.device_every),
            SpeedMonitor(SpeedTester(settings.speed_down_bytes, settings.speed_up_bytes), self.repos.speed,
                         lambda: self.connectivity.is_up("wan"), settings.speed_every, settings.speed_delay),
            self.vendors,
        ])
        self.router = self._build_router()

    def _build_router(self) -> Router:
        summary = SummaryService(self.repos, self.vendors, self.connectivity.status, self.settings.contracted)
        services = Services(
            summary=summary,
            events=EventQueryService(self.repos.events),
            exporter=CsvExporter(summary, self.repos),
            scanner=HostScanner(self.repos.devices, self.vendors),
            devices=DeviceService(self.repos.devices),
        )
        router = Router()
        register_api(router, services)
        register_pages(router, StaticFiles(self.settings.static_dir))
        return router

    def reset_data(self) -> None:
        self.db.clear()

    def run(self, host: str, port: int) -> None:
        self.scheduler.start()
        if self.notifier:
            self.notifier.start()
            print("Alertas de Telegram activadas", flush=True)
        else:
            print("Alertas de Telegram desactivadas (faltan TELEGRAM_TOKEN y TELEGRAM_CHAT_ID)", flush=True)
        print(f"netwatch en http://{host}:{port}  (base de datos: {self.settings.db_path})", flush=True)
        serve(host, port, self.router)
