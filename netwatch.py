#!/usr/bin/env python3
"""netwatch: monitor de red casera (sin dependencias, solo librería estándar).

Fase 1: inventario de equipos (ping sweep + tabla ARP).
Fase 2: caídas de red local / internet / DNS, cambios de IP pública.
Uso: python3 netwatch.py [--host 127.0.0.1] [--port 8765]
"""
import argparse
import csv
from datetime import date, timedelta
import io
import json
import os
import platform
import random
import re
import socket
import sqlite3
import struct
import subprocess
import threading
import time
import urllib.error
import urllib.parse
import urllib.request
import queue
from concurrent.futures import ThreadPoolExecutor
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

HERE = Path(__file__).parent
IS_MAC = platform.system() == "Darwin"
DB_PATH = Path(os.environ.get("NETWATCH_DB", HERE / "netwatch.db"))

PROBE_EVERY = 5        # segundos entre mediciones
SCAN_EVERY = 120       # segundos entre escaneos de la red
IP_EVERY = 300         # segundos entre consultas de IP pública
MISSES_TO_OFFLINE = 3  # escaneos fallidos antes de marcar un equipo como desconectado
FAILS_TO_ALERT = 3     # mediciones fallidas seguidas antes de registrar una caída
KEEP_DAYS = 30
DEV_EVERY = 30         # segundos entre pings a cada equipo
SPEED_EVERY = 3600     # segundos entre pruebas de velocidad
SPEED_DOWN = 10_000_000  # bytes que baja cada prueba
SPEED_UP = 3_000_000     # bytes que sube cada prueba
OUI_PATH = HERE / "oui.txt"
OUI = {}

# Alertas por Telegram: el token y el chat id se leen de variables de entorno (nunca van en el código).
TG_TOKEN = os.environ.get("TELEGRAM_TOKEN", "")
TG_CHAT = os.environ.get("TELEGRAM_CHAT_ID", "")
ALERT_KINDS = {"lan_down", "lan_up", "wan_down", "wan_up", "dns_down", "dns_up", "new_device", "ip_change"}
alerts = queue.Queue(maxsize=100)
initial = {"done": False}  # el primer escaneo no alerta de "equipo nuevo"
CONTRACTED = {"down": 0, "up": 0}  # Mbps de tu plan (0 = sin dato); ejemplo: {"down": 100, "up": 10}
EXTERNAL = ["1.1.1.1", "8.8.8.8"]

LABELS = {"lan": "Red local", "wan": "Internet", "dns": "DNS"}

db = sqlite3.connect(DB_PATH, check_same_thread=False)
db.row_factory = sqlite3.Row
lock = threading.Lock()
state = {k: {"fails": 0, "first_fail": None, "down_since": None} for k in LABELS}
rdns_pool = ThreadPoolExecutor(4)


def init_db():
    with lock:
        db.executescript("""
        CREATE TABLE IF NOT EXISTS devices(
            mac TEXT PRIMARY KEY, ip TEXT, hostname TEXT, alias TEXT,
            random_mac INTEGER, first_seen INTEGER, last_seen INTEGER,
            online INTEGER, misses INTEGER DEFAULT 0);
        CREATE TABLE IF NOT EXISTS probes(
            ts INTEGER, kind TEXT, target TEXT, ok INTEGER, ms REAL);
        CREATE INDEX IF NOT EXISTS probes_ts ON probes(ts);
        CREATE TABLE IF NOT EXISTS events(ts INTEGER, kind TEXT, detail TEXT);
        CREATE INDEX IF NOT EXISTS events_ts ON events(ts);
        CREATE TABLE IF NOT EXISTS meta(key TEXT PRIMARY KEY, value TEXT);
        CREATE TABLE IF NOT EXISTS speed(ts INTEGER, down REAL, up REAL);
        """)
        if "network" not in [r[1] for r in db.execute("PRAGMA table_info(devices)")]:
            db.execute("ALTER TABLE devices ADD COLUMN network TEXT")
        db.commit()


def q(sql, args=()):
    with lock:
        return db.execute(sql, args).fetchall()


def run(sql, args=()):
    with lock:
        db.execute(sql, args)
        db.commit()


def event(kind, detail):
    run("INSERT INTO events VALUES (?,?,?)", (int(time.time()), kind, detail))
    print(time.strftime("%H:%M:%S"), kind, detail, flush=True)
    if TG_TOKEN and TG_CHAT and kind in ALERT_KINDS and not (kind == "new_device" and not initial["done"]):
        try:
            alerts.put_nowait((int(time.time()), kind, detail))
        except queue.Full:
            pass


def dur(s):
    s = int(s)
    if s < 60:
        return f"{s}s"
    if s < 3600:
        return f"{s // 60}m {s % 60}s"
    return f"{s // 3600}h {s % 3600 // 60}m"


# ---------- utilidades de red ----------

def ping(host, timeout=1.0):
    wait = str(int(timeout * 1000)) if IS_MAC else str(max(1, int(round(timeout))))
    t0 = time.time()
    try:
        r = subprocess.run(["ping", "-c", "1", "-W", wait, host],
                           capture_output=True, text=True, timeout=timeout + 2)
    except Exception:
        return False, None
    if r.returncode != 0:
        return False, None
    m = re.search(r"time[=<]([\d.]+)", r.stdout)
    return True, float(m.group(1)) if m else (time.time() - t0) * 1000


def dns_query(server, name="example.com", timeout=2.0):
    qid = random.randint(0, 65535)
    packet = struct.pack(">HHHHHH", qid, 0x0100, 1, 0, 0, 0)
    packet += b"".join(bytes([len(p)]) + p.encode() for p in name.split(".")) + b"\0"
    packet += struct.pack(">HH", 1, 1)
    s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    s.settimeout(timeout)
    t0 = time.time()
    try:
        s.sendto(packet, (server, 53))
        data, _ = s.recvfrom(512)
    except Exception:
        return False, None
    finally:
        s.close()
    rid, flags, _, answers = struct.unpack(">HHHH", data[:8])
    ok = rid == qid and (flags & 0xF) == 0 and answers > 0
    return ok, (time.time() - t0) * 1000 if ok else None


def local_ip():
    s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    try:
        s.connect(("8.8.8.8", 80))
        return s.getsockname()[0]
    except Exception:
        return None
    finally:
        s.close()


def gateway():
    try:
        if IS_MAC:
            out = subprocess.run(["route", "-n", "get", "default"],
                                 capture_output=True, text=True, timeout=3).stdout
            m = re.search(r"gateway:\s*(\S+)", out)
        else:
            out = subprocess.run(["ip", "route", "show", "default"],
                                 capture_output=True, text=True, timeout=3).stdout
            m = re.search(r"via\s+(\S+)", out)
        return m.group(1) if m else None
    except Exception:
        return None


def read_arp():
    found = {}
    outputs = []
    for cmd in (["arp", "-an"], ["ip", "neigh"]):
        try:
            outputs.append(subprocess.run(cmd, capture_output=True, text=True, timeout=5).stdout)
        except Exception:
            pass
    pats = [r"\((\d+\.\d+\.\d+\.\d+)\) at ([0-9a-fA-F:]+)", r"(\d+\.\d+\.\d+\.\d+) .*lladdr ([0-9a-fA-F:]+)"]
    for out in outputs:
        for line in out.splitlines():
            if "incomplete" in line:
                continue
            for p in pats:
                m = re.search(p, line)
                if m:
                    parts = m.group(2).split(":")
                    if len(parts) != 6:
                        continue
                    mac = ":".join(x.zfill(2) for x in parts).lower()
                    if mac.startswith(("ff:ff", "01:00:5e")):
                        continue
                    found[mac] = m.group(1)
    return found


def fetch(url, data=None, timeout=30):
    req = urllib.request.Request(url, data=data, headers={"User-Agent": "Mozilla/5.0 netwatch"})
    return urllib.request.urlopen(req, timeout=timeout).read()


def load_oui():
    """Fabricante por MAC: descarga la lista del IEEE una sola vez (oui.txt)."""
    try:
        if not OUI_PATH.exists():
            OUI_PATH.write_bytes(fetch("https://standards-oui.ieee.org/oui/oui.txt", timeout=60))
        for line in OUI_PATH.read_text(errors="ignore").splitlines():
            m = re.match(r"^([0-9A-F]{2}-[0-9A-F]{2}-[0-9A-F]{2})\s+\(hex\)\s+(.+)$", line)
            if m:
                OUI[m.group(1)] = m.group(2).strip()
    except Exception as e:
        print("oui:", e, flush=True)


def vendor(mac):
    return OUI.get(mac[:8].upper().replace(":", "-"))


def quality(rows):
    """rows: [(ms, ok)] en orden -> latencia media, jitter y % de pérdida."""
    if not rows:
        return None
    ms = [r[0] for r in rows if r[1] and r[0] is not None]
    diffs = [abs(a - b) for a, b in zip(ms, ms[1:])]
    return {"avg": round(sum(ms) / len(ms), 1) if ms else None,
            "jitter": round(sum(diffs) / len(diffs), 1) if diffs else None,
            "loss": round(100 * (1 - sum(r[1] for r in rows) / len(rows)), 1)}


def telegram_send(text):
    body = urllib.parse.urlencode({"chat_id": TG_CHAT, "text": text}).encode()
    fetch(f"https://api.telegram.org/bot{TG_TOKEN}/sendMessage", data=body, timeout=15)


def alert_loop():
    """Envía las alertas en orden; si no hay internet, las reintenta hasta que vuelva."""
    while True:
        ts, kind, detail = alerts.get()
        icon = "🔴" if kind.endswith("_down") else "🟢" if kind.endswith("_up") else "🆕" if kind == "new_device" else "🌐"
        text = f"{icon} {detail}\n🕐 {time.strftime('%d %b %H:%M', time.localtime(ts))}"
        while True:
            try:
                late = time.time() - ts > 60
                telegram_send(text + ("\n(enviado con retraso: no había conexión)" if late else ""))
                break
            except urllib.error.HTTPError as e:
                if e.code in (400, 401, 403, 404):  # token o chat id incorrectos: reintentar no sirve
                    print("telegram rechazó el mensaje:", e.code, flush=True)
                    break
                time.sleep(30)
            except Exception as e:
                print("telegram error:", e, flush=True)
                time.sleep(30)


def rdns(ip):
    try:
        return rdns_pool.submit(socket.gethostbyaddr, ip).result(timeout=2)[0]
    except Exception:
        return None


# ---------- fase 1: inventario ----------

def scan_once():
    ip = local_ip()
    if not ip:
        return
    base = ".".join(ip.split(".")[:3])
    with ThreadPoolExecutor(64) as pool:
        list(pool.map(lambda h: ping(h, 0.6), [f"{base}.{i}" for i in range(1, 255)]))
    found = {m: i for m, i in read_arp().items() if i.startswith(base + ".")}
    now = int(time.time())
    for mac, addr in found.items():
        row = q("SELECT online FROM devices WHERE mac=?", (mac,))
        if not row:
            host = rdns(addr)
            run("INSERT INTO devices(mac, ip, hostname, alias, random_mac, first_seen, last_seen, online, misses) "
                "VALUES (?,?,?,NULL,?,?,?,1,0)",
                (mac, addr, host, 1 if int(mac[:2], 16) & 2 else 0, now, now))
            extra = (f" · {vendor(mac)}" if vendor(mac) else "") + \
                    (" · MAC privada (típico de teléfonos y laptops)" if int(mac[:2], 16) & 2 else "")
            event("new_device", f"Equipo nuevo: {mac} en {addr}" + (f" ({host})" if host else "") + extra)
        else:
            if not row[0]["online"]:
                event("device_online", f"{mac} volvió a la red en {addr}")
            run("UPDATE devices SET ip=?, last_seen=?, online=1, misses=0 WHERE mac=?", (addr, now, mac))
    for r in q("SELECT mac, ip, alias, hostname, misses, online FROM devices"):
        if r["mac"] in found:
            continue
        misses = r["misses"] + 1
        went_off = misses >= MISSES_TO_OFFLINE and r["online"]
        run("UPDATE devices SET misses=?, online=? WHERE mac=?",
            (misses, 0 if misses >= MISSES_TO_OFFLINE else r["online"], r["mac"]))
        if went_off:
            name = r["alias"] or r["hostname"] or r["ip"]
            event("device_offline", f"{name} ({r['mac']}) se desconectó")


# ---------- fase 2: caídas ----------

def check(name, ok, now):
    s = state[name]
    if ok:
        if s["down_since"]:
            event(f"{name}_up", f"{LABELS[name]} restablecida tras {dur(now - s['down_since'])}")
            s["down_since"] = None
        s["fails"], s["first_fail"] = 0, None
        return
    s["fails"] += 1
    s["first_fail"] = s["first_fail"] or now
    if s["fails"] >= FAILS_TO_ALERT and not s["down_since"]:
        s["down_since"] = s["first_fail"]
        event(f"{name}_down", f"{LABELS[name]} sin respuesta")


def probe_cycle(pool, gw, now):
    pings = {"gateway": gw, **{h: h for h in EXTERNAL}} if gw else {h: h for h in EXTERNAL}
    dns_servers = {"gateway": gw, **{h: h for h in EXTERNAL}} if gw else {h: h for h in EXTERNAL}
    pf = {k: pool.submit(ping, v) for k, v in pings.items()}
    df = {k: pool.submit(dns_query, v) for k, v in dns_servers.items()}
    res = {}
    for k, f in pf.items():
        res[("ping", k)] = f.result()
    for k, f in df.items():
        res[("dns", k)] = f.result()
    for (kind, target), (ok, ms) in res.items():
        run("INSERT INTO probes VALUES (?,?,?,?,?)", (now, kind, target, 1 if ok else 0, ms))
    lan_ok = res.get(("ping", "gateway"), (True,))[0] if gw else True
    wan_ok = any(res[("ping", h)][0] for h in EXTERNAL)
    dns_ok = any(ok for (kind, _), (ok, _) in res.items() if kind == "dns")
    check("lan", lan_ok, now)
    check("wan", wan_ok or not lan_ok, now)       # si la red local cae, no culpar al internet
    check("dns", dns_ok or not wan_ok, now)       # si no hay internet, no culpar al DNS


def probe_loop():
    pool = ThreadPoolExecutor(12)
    gw, gw_at, last = None, 0, None
    while True:
        t0 = time.time()
        try:
            now = int(t0)
            if last and now - last > PROBE_EVERY * 4:
                event("monitor_gap", f"El monitor estuvo detenido {dur(now - last)} (¿equipo suspendido?)")
            if now - gw_at > 60:
                gw, gw_at = gateway(), now
            probe_cycle(pool, gw, now)
            last = now
        except Exception as e:
            print("probe error:", e, flush=True)
        time.sleep(max(0.5, PROBE_EVERY - (time.time() - t0)))


def device_loop():
    pool = ThreadPoolExecutor(16)
    while True:
        t0 = time.time()
        try:
            futs = [(d["mac"], pool.submit(ping, d["ip"], 1.0))
                    for d in q("SELECT mac, ip FROM devices WHERE online=1")]
            for mac, f in futs:
                ok, ms = f.result()
                run("INSERT INTO probes VALUES (?,?,?,?,?)", (int(t0), "dev", mac, 1 if ok else 0, ms))
        except Exception as e:
            print("device error:", e, flush=True)
        time.sleep(max(1, DEV_EVERY - (time.time() - t0)))


def speed_test():
    t0 = time.time()
    n = len(fetch(f"https://speed.cloudflare.com/__down?bytes={SPEED_DOWN}"))
    down = n * 8 / (time.time() - t0) / 1e6
    data = os.urandom(SPEED_UP)
    t1 = time.time()
    fetch("https://speed.cloudflare.com/__up", data=data)
    up = len(data) * 8 / (time.time() - t1) / 1e6
    return down, up


def speed_loop():
    time.sleep(30)
    while True:
        try:
            if not state["wan"]["down_since"]:
                down, up = speed_test()
                run("INSERT INTO speed VALUES (?,?,?)", (int(time.time()), down, up))
        except Exception as e:
            print("speed error:", e, flush=True)
        time.sleep(SPEED_EVERY)


def scan_loop():
    while True:
        try:
            scan_once()
            initial["done"] = True
        except Exception as e:
            print("scan error:", e, flush=True)
        time.sleep(SCAN_EVERY)


def ip_loop():
    last_purge = 0
    while True:
        try:
            ip = urllib.request.urlopen("https://api.ipify.org", timeout=5).read().decode().strip()
            row = q("SELECT value FROM meta WHERE key='public_ip'")
            if not row:
                event("ip_first", f"IP pública inicial: {ip}")
            elif row[0]["value"] != ip:
                event("ip_change", f"IP pública cambió: {row[0]['value']} → {ip}")
            run("INSERT OR REPLACE INTO meta VALUES ('public_ip', ?)", (ip,))
        except Exception:
            pass
        if time.time() - last_purge > 86400:
            cutoff = int(time.time()) - KEEP_DAYS * 86400
            run("DELETE FROM probes WHERE ts<?", (cutoff,))
            last_purge = time.time()
        time.sleep(IP_EVERY)


# ---------- API y dashboard ----------

def summary():
    now = int(time.time())
    devices = [dict(r) for r in q("SELECT * FROM devices")]
    devices.sort(key=lambda d: (-d["online"], tuple(int(x) for x in (d["ip"] or "0.0.0.0").split("."))))
    events = [dict(r) for r in q("SELECT * FROM events ORDER BY ts DESC LIMIT 60")]

    per = {}
    for r in q("SELECT kind, target, ms, ok FROM probes WHERE ts>? AND kind IN ('ping','dev') ORDER BY ts",
               (now - 3600,)):
        per.setdefault((r["kind"], r["target"]), []).append((r["ms"], r["ok"]))
    qual = {t: quality(v) for (k, t), v in per.items() if k == "ping"}
    drops = {}
    for e in q("SELECT detail FROM events WHERE kind='device_offline' AND ts>?", (now - 86400,)):
        m = re.search(r"\(([0-9a-f:]{17})\)", e["detail"])
        if m:
            drops[m.group(1)] = drops.get(m.group(1), 0) + 1
    for d in devices:
        d["vendor"] = vendor(d["mac"])
        d["q"] = quality(per.get(("dev", d["mac"]), []))
        d["drops"] = drops.get(d["mac"], 0)
    speed = [{"ts": r["ts"], "down": r["down"], "up": r["up"]}
             for r in q("SELECT * FROM speed ORDER BY ts DESC LIMIT 48")][::-1]

    slots = [now // 600 - 143 + i for i in range(144)]
    agg = {"lan": {}, "wan": {}, "dns": {}}
    rows = q("SELECT ts/600 AS b, kind, target, AVG(ok) AS a FROM probes "
             "WHERE ts>? AND kind IN ('ping','dns') GROUP BY b, kind, target",
             (slots[0] * 600,))
    for r in rows:
        if r["kind"] == "dns":
            key = "dns"
        elif r["target"] == "gateway":
            key = "lan"
        else:
            key = "wan"
        agg[key][r["b"]] = max(agg[key].get(r["b"], 0), r["a"])
    ribbon, uptime = {}, {}
    for k in agg:
        vals = [agg[k].get(s) for s in slots]
        ribbon[k] = [None if v is None else round(v, 3) for v in vals]
        seen = [v for v in vals if v is not None]
        uptime[k] = round(100 * sum(seen) / len(seen), 2) if seen else None

    series = {}
    for r in q("SELECT target, ts/60 AS m, AVG(ms) AS ms, MIN(ok) AS ok FROM probes "
               "WHERE ts>? AND kind='ping' GROUP BY target, m ORDER BY m", (now - 3600,)):
        series.setdefault(r["target"], []).append([r["m"] * 60, r["ms"], r["ok"]])

    hours = [0] * 24
    for r in q("SELECT strftime('%H', ts, 'unixepoch', 'localtime') AS h, COUNT(*) AS c FROM events "
               "WHERE kind IN ('lan_down','wan_down','dns_down') GROUP BY h"):
        hours[int(r["h"])] = r["c"]

    pub = q("SELECT value FROM meta WHERE key='public_ip'")
    return {
        "now": now,
        "checks": {k: {"label": LABELS[k], "down_since": state[k]["down_since"]} for k in LABELS},
        "ribbon_start": slots[0] * 600, "ribbon": ribbon, "uptime": uptime,
        "series": series, "hours": hours, "devices": devices, "events": events,
        "quality": qual, "speed": speed, "contracted": CONTRACTED,
        "public_ip": pub[0]["value"] if pub else None,
    }


def export_csv():
    """Resumen completo en un solo CSV, en bloques separados por una línea vacía."""
    d = summary()
    buf = io.StringIO()
    w = csv.writer(buf)
    ts = lambda t: time.strftime("%Y-%m-%d %H:%M:%S", time.localtime(t)) if t else ""
    num = lambda v: "" if v is None else v

    w.writerow(["# Resumen"])
    w.writerow(["generado", ts(d["now"])])
    w.writerow(["ip_publica", d["public_ip"] or ""])
    w.writerow(["velocidad_contratada_bajada_mbps", CONTRACTED["down"] or ""])
    w.writerow(["velocidad_contratada_subida_mbps", CONTRACTED["up"] or ""])

    w.writerow([])
    w.writerow(["# Estado y disponibilidad 24 h"])
    w.writerow(["servicio", "estado", "caido_desde", "disponibilidad_24h_pct"])
    for k, c in d["checks"].items():
        w.writerow([c["label"], "caido" if c["down_since"] else "ok", ts(c["down_since"]), num(d["uptime"][k])])

    w.writerow([])
    w.writerow(["# Calidad ultima hora"])
    w.writerow(["destino", "latencia_media_ms", "jitter_ms", "perdida_pct"])
    for t, v in d["quality"].items():
        w.writerow([t, num(v["avg"]), num(v["jitter"]), v["loss"]])

    w.writerow([])
    w.writerow(["# Equipos"])
    w.writerow(["alias", "nombre_host", "fabricante", "ip", "mac", "mac_aleatoria", "estado", "primera_vez",
                "ultima_vez", "red", "latencia_ms", "jitter_ms", "perdida_pct", "caidas_24h"])
    for x in d["devices"]:
        v = x["q"] or {}
        w.writerow([x["alias"] or "", x["hostname"] or "", x["vendor"] or "", x["ip"], x["mac"],
                    "si" if x["random_mac"] else "no", "conectado" if x["online"] else "desconectado",
                    ts(x["first_seen"]), ts(x["last_seen"]), x["network"] or "", num(v.get("avg")), num(v.get("jitter")),
                    num(v.get("loss")), x["drops"]])

    w.writerow([])
    w.writerow(["# Pruebas de velocidad"])
    w.writerow(["fecha", "bajada_mbps", "subida_mbps"])
    for r in q("SELECT * FROM speed ORDER BY ts DESC"):
        w.writerow([ts(r["ts"]), round(r["down"], 2), round(r["up"], 2)])

    w.writerow([])
    w.writerow(["# Caidas por hora del dia"])
    w.writerow(["hora", "caidas"])
    for h, c in enumerate(d["hours"]):
        w.writerow([f"{h:02d}:00", c])

    w.writerow([])
    w.writerow(["# Eventos (hasta 5000, mas recientes primero)"])
    w.writerow(["fecha", "tipo", "detalle"])
    for r in q("SELECT * FROM events ORDER BY ts DESC LIMIT 5000"):
        w.writerow([ts(r["ts"]), r["kind"], r["detail"]])

    return ("\ufeff" + buf.getvalue()).encode("utf-8")  # BOM para que Excel respete los acentos


def event_page(params):
    """Filtra y pagina el historial completo de eventos."""
    try:
        page = int(params.get("page", ["0"])[0])
        size = int(params.get("size", ["10"])[0])
    except ValueError as exc:
        raise ValueError("page y size deben ser números enteros") from exc
    if page < 0 or size not in (10, 25, 50):
        raise ValueError("page o size fuera de rango")

    def day_timestamp(value):
        if not value:
            return None
        parsed = date.fromisoformat(value)
        if parsed.isoformat() != value:
            raise ValueError("fecha inválida")
        try:
            return int(time.mktime(parsed.timetuple()))
        except (OverflowError, OSError) as exc:
            raise ValueError("fecha fuera de rango") from exc

    start = day_timestamp(params.get("from", [""])[0])
    end_date = params.get("to", [""])[0]
    end = day_timestamp(end_date)
    if end is not None:
        try:
            end = int(time.mktime((date.fromisoformat(end_date) + timedelta(days=1)).timetuple()))
        except (OverflowError, OSError) as exc:
            raise ValueError("fecha fuera de rango") from exc
    if start is not None and end is not None and end <= start:
        raise ValueError("el rango de fechas no es válido")

    kind = params.get("kind", [""])[0]
    clauses, args = [], []
    if start is not None:
        clauses.append("ts>=?")
        args.append(start)
    if end is not None:
        clauses.append("ts<?")
        args.append(end)
    if kind:
        clauses.append("kind=?")
        args.append(kind)
    where = " WHERE " + " AND ".join(clauses) if clauses else ""

    kinds = [row["kind"] for row in q("SELECT DISTINCT kind FROM events ORDER BY kind")]
    total = q("SELECT COUNT(*) AS total FROM events" + where, args)[0]["total"]
    pages = (total + size - 1) // size
    page = min(page, max(0, pages - 1))
    events = [dict(row) for row in q(
        "SELECT ts, kind, detail FROM events" + where +
        " ORDER BY ts DESC LIMIT ? OFFSET ?", (*args, size, page * size))]
    return {"events": events, "kinds": kinds, "total": total, "page": page, "pages": pages}


class Handler(BaseHTTPRequestHandler):
    def log_message(self, *a):
        pass

    def send(self, code, body, ctype, extra=None):
        self.send_response(code)
        self.send_header("Content-Type", ctype)
        for k, v in (extra or {}).items():
            self.send_header(k, v)
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def do_GET(self):
        parsed_url = urllib.parse.urlsplit(self.path)
        if parsed_url.path in ("/", "/index.html"):
            self.send(200, (HERE / "dashboard.html").read_bytes(), "text/html; charset=utf-8")
        elif parsed_url.path == "/dashboard.css":
            self.send(200, (HERE / "dashboard.css").read_bytes(), "text/css; charset=utf-8")
        elif parsed_url.path == "/api/events":
            try:
                data = event_page(urllib.parse.parse_qs(parsed_url.query, keep_blank_values=True))
            except ValueError as exc:
                body = json.dumps({"error": str(exc)}).encode()
                return self.send(400, body, "application/json")
            self.send(200, json.dumps(data).encode(), "application/json")
        elif parsed_url.path == "/api/export.csv":
            name = time.strftime("netwatch-%Y%m%d-%H%M.csv")
            self.send(200, export_csv(), "text/csv; charset=utf-8",
                      {"Content-Disposition": f'attachment; filename="{name}"'})
        elif parsed_url.path == "/api/summary":
            self.send(200, json.dumps(summary()).encode(), "application/json")
        else:
            self.send(404, b"not found", "text/plain")

    def do_POST(self):
        if self.path not in ("/api/alias", "/api/network"):
            return self.send(404, b"not found", "text/plain")
        try:
            data = json.loads(self.rfile.read(int(self.headers.get("Content-Length", 0))))
            alias = (data.get("alias") or "").strip()[:60] or None
            if self.path == "/api/network":
                net = data.get("network") if data.get("network") in ("5 GHz", "2.4 GHz", "Cable") else None
                run("UPDATE devices SET network=? WHERE mac=?", (net, data["mac"]))
            else:
                run("UPDATE devices SET alias=? WHERE mac=?", (alias, data["mac"]))
            self.send(200, b"{}", "application/json")
        except Exception:
            self.send(400, b"bad request", "text/plain")


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--host", default="127.0.0.1", help="usa 0.0.0.0 para verlo desde otro equipo")
    ap.add_argument("--port", type=int, default=8765)
    ap.add_argument("--test-telegram", action="store_true", help="envía un mensaje de prueba y termina")
    ap.add_argument("--find-chat-id", action="store_true", help="muestra tu chat id (antes escríbele algo a tu bot)")
    ap.add_argument("--reset", action="store_true", help="borra los datos acumulados y empieza de cero")
    args = ap.parse_args()
    if args.find_chat_id or args.test_telegram:
        try:
            if args.find_chat_id:
                res = json.loads(fetch(f"https://api.telegram.org/bot{TG_TOKEN}/getUpdates")).get("result", [])
                ids = {m["message"]["chat"]["id"]: m["message"]["chat"].get("first_name", "") for m in res if "message" in m}
                print(ids or "No hay mensajes: escríbele algo a tu bot y vuelve a correr esto.")
            else:
                telegram_send("✅ netwatch conectado: las alertas funcionan.")
                print("Mensaje de prueba enviado.")
        except Exception as e:
            print("Error:", e, "(revisa TELEGRAM_TOKEN y TELEGRAM_CHAT_ID)")
        return
    init_db()
    if args.reset:
        for t in ("devices", "probes", "events", "speed", "meta"):
            run(f"DELETE FROM {t}")
        print("Datos anteriores borrados.", flush=True)
    initial["done"] = bool(q("SELECT 1 FROM devices LIMIT 1"))  # base vacía: el primer escaneo no alerta
    for fn in (scan_loop, probe_loop, ip_loop, device_loop, speed_loop, load_oui):
        threading.Thread(target=fn, daemon=True).start()
    if TG_TOKEN and TG_CHAT:
        threading.Thread(target=alert_loop, daemon=True).start()
        print("Alertas de Telegram activadas", flush=True)
    else:
        print("Alertas de Telegram desactivadas (faltan TELEGRAM_TOKEN y TELEGRAM_CHAT_ID)", flush=True)
    print(f"netwatch en http://{args.host}:{args.port}  (base de datos: {DB_PATH})", flush=True)
    ThreadingHTTPServer((args.host, args.port), Handler).serve_forever()


if __name__ == "__main__":
    main()
