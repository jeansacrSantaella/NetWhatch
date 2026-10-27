# netwatch

Monitor de red casera. Solo usa la librería estándar de Python 3.9+, sin instalar nada.

## Qué hace

- **Equipos:** cada 2 minutos hace un barrido de la subred /24 y lee la tabla ARP. Guarda IP, MAC, nombre, primera y última vez visto. Avisa cuando aparece un equipo nuevo o uno se conecta o desconecta.
- **Caídas:** cada 5 segundos hace ping al router, a 1.1.1.1 y a 8.8.8.8, y consultas DNS a los mismos tres. Distingue tres fallas:
  - Red local: el router no responde.
  - Internet: no responde ningún destino externo.
  - DNS: hay internet pero no resuelven nombres.
- **IP pública:** la consulta cada 5 minutos y registra cada cambio.
- **Pausas del monitor:** si el equipo se suspende, lo registra como pausa y no como caída de la red.
- **Dashboard:** cinta de 24 horas por falla, latencia de la última hora, tabla de equipos, caídas por hora del día y eventos.
- **Interfaz:** `dashboard.html` contiene la estructura y el comportamiento; `dashboard.css` contiene los estilos.
- **Paginación:** las tablas de equipos y eventos recientes permiten elegir 10, 25 o 50 filas por página.
- **Eventos:** filtra el historial por fecha inicial/final y tipo de evento; los filtros se pueden combinar.
- **Dashboard:** las secciones se acomodan en una cuadrícula adaptable; puedes reordenarlas arrastrando y expandirlas a pantalla completa. El orden y el tema claro/nocturno se guardan en el navegador.

## Uso

```bash
python3 netwatch.py                  # http://127.0.0.1:8765
python3 netwatch.py --host 0.0.0.0   # para verlo desde el teléfono
```

En Mac, para que no se suspenda mientras mide: `caffeinate -i python3 netwatch.py`

Córrelo directo en el sistema y no en Docker. En Mac, Docker no ve la red local y no podría escanear equipos.

Los datos viven en `netwatch.db` (SQLite) junto al script y se conservan 30 días de mediciones. Los eventos y los equipos se conservan siempre.

## Calidad, velocidad y fabricantes

- **Jitter y pérdida:** la tabla de calidad usa la última hora de pings al router, 1.1.1.1 y 8.8.8.8.
- **Por equipo:** cada 30 segundos hace ping a cada equipo conectado y calcula latencia, jitter, pérdida y caídas de las últimas 24 horas. Algunos equipos no responden a ping aunque estén conectados: se muestran como "sin respuesta a ping".
- **Prueba de velocidad:** cada hora baja unos 10 MB y sube unos 3 MB con los servidores de Cloudflare. Gasta cerca de 300 MB al día; puedes cambiarlo con `SPEED_EVERY`, `SPEED_DOWN` y `SPEED_UP` al inicio de `netwatch.py`.
- **Fabricante:** la primera vez descarga la lista del IEEE a `oui.txt` junto al script. Si no hay internet al arrancar, bórrala si quedó vacía y reinicia.

## Alertas por Telegram (gratis)

1. En Telegram habla con **@BotFather**, escribe `/newbot` y copia el token que te da. No lo compartas ni lo subas a GitHub.
2. Abre un chat con tu bot nuevo y mándale cualquier mensaje.
3. Obtén tu chat id:
   ```bash
   export TELEGRAM_TOKEN="tu-token"
   python3 netwatch.py --find-chat-id
   ```
4. Define también el chat id y prueba:
   ```bash
   export TELEGRAM_CHAT_ID="tu-chat-id"
   python3 netwatch.py --test-telegram
   ```
5. Arranca el monitor en la misma terminal (con las dos variables definidas) y las alertas quedan activas.

Avisa de: caídas y recuperaciones de red local, internet y DNS, equipos nuevos y cambios de IP pública. Para cambiar qué avisa, edita `ALERT_KINDS` al inicio de `netwatch.py`. Si no hay internet, los mensajes quedan en cola y salen al volver la conexión, con la hora real del evento. Tras un `--reset`, el primer escaneo no avisa de equipos nuevos.

## Límites

- No mide consumo por equipo (fase 3).
- El equipo donde corre no aparece en su propia lista.
- Con `--host 0.0.0.0`, cualquiera en tu red puede ver el dashboard y cambiar alias.
- Para medir bien las caídas, déjalo en un equipo siempre encendido y conectado por cable.
