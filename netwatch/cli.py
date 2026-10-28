"""Opciones de línea de comandos."""
from __future__ import annotations

import argparse

from netwatch.alerts.telegram import TelegramClient
from netwatch.config import Settings


def parse_args(argv=None) -> argparse.Namespace:
    ap = argparse.ArgumentParser(prog="netwatch", description="Monitor de red casera.")
    ap.add_argument("--host", default="127.0.0.1", help="usa 0.0.0.0 para verlo desde otro equipo")
    ap.add_argument("--port", type=int, default=8765)
    ap.add_argument("--test-telegram", action="store_true", help="envía un mensaje de prueba y termina")
    ap.add_argument("--find-chat-id", action="store_true", help="muestra tu chat id (antes escríbele algo a tu bot)")
    ap.add_argument("--reset", action="store_true", help="borra los datos acumulados y empieza de cero")
    return ap.parse_args(argv)


def telegram_command(settings: Settings, find_chat_id: bool) -> None:
    client = TelegramClient(settings.telegram_token, settings.telegram_chat)
    try:
        if find_chat_id:
            ids = client.chat_ids()
            print(ids or "No hay mensajes: escríbele algo a tu bot y vuelve a correr esto.")
        else:
            client.send("✅ netwatch conectado: las alertas funcionan.")
            print("Mensaje de prueba enviado.")
    except Exception as e:
        print("Error:", e, "(revisa TELEGRAM_TOKEN y TELEGRAM_CHAT_ID)")


def main(argv=None) -> None:
    args = parse_args(argv)
    settings = Settings.from_env()
    if args.find_chat_id or args.test_telegram:
        return telegram_command(settings, args.find_chat_id)

    from netwatch.app import Application  # solo al servir: abre la base de datos

    app = Application(settings)
    if args.reset:
        app.reset_data()
        print("Datos anteriores borrados.", flush=True)
    app.run(args.host, args.port)
