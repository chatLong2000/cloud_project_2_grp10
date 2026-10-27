#!/usr/bin/env python3
"""
Edge gateway for the coffee machine fleet.

    serial ports (pty) -> CSV validation -> SQLite buffer -> MQTT/TLS batches

- One reader thread per serial port: reads CSV lines, validates them and
  stores valid readings in SQLite. Invalid lines are counted and logged.
- Silent devices are not detected here: the cloud does it from the
  received batches.
- A publisher thread sends the buffer every BATCH_INTERVAL_S as one JSON
  batch per device (at most BATCH_MAX_ROWS readings each, to stay under the
  128 KB IoT Core message limit), over MQTT/TLS (X.509 client cert, QoS 1).
  Rows are only deleted once the broker acknowledged them, so nothing is
  lost while the broker is unreachable (store and forward).

All settings come from environment variables (see Settings).
"""

import json
import logging
import os
import signal
import sqlite3
import threading
import time
from dataclasses import dataclass, field
from urllib.parse import unquote, urlsplit

import paho.mqtt.client as mqtt
import serial

log = logging.getLogger("edge")

CSV_FIELDS = [
    "timestamp",
    "device_id",
    "uptime_ms",
    "temperature_c",
    "power_consumption_mw",
    "heap_free_bytes",
    "device_status",
    "error_code",
]
VALID_STATUSES = {"off", "booting", "preheat", "standby", "brewing", "updating", "degraded", "error"}


# --- Settings ----------------------------------------------------------------


def _env_list(name, default):
    return [p.strip() for p in os.environ.get(name, default).split(",") if p.strip()]


@dataclass
class Settings:
    serial_ports: list = field(default_factory=lambda: _env_list(
        "SERIAL_PORTS", ",".join(f"/tmp/tty/dev{i}" for i in range(1, 6))))
    baudrate: int = int(os.environ.get("BAUDRATE", "115200"))
    db_path: str = os.environ.get("DB_PATH", "/data/buffer.db")
    batch_interval_s: float = float(os.environ.get("BATCH_INTERVAL_S", "10"))
    batch_max_rows: int = int(os.environ.get("BATCH_MAX_ROWS", "400"))  # per device
    mqtt_url: str = os.environ.get("MQTT_URL", "mqtt://localhost:1883")
    mqtt_username: str = os.environ.get("MQTT_USERNAME", "")
    mqtt_password: str = os.environ.get("MQTT_PASSWORD", "")
    mqtt_client_id: str = os.environ.get("MQTT_CLIENT_ID", "edge-01")
    mqtt_topic_prefix: str = os.environ.get("MQTT_TOPIC_PREFIX", "coffee")
    mqtt_ca: str = os.environ.get("MQTT_CA", "/certs/ca.crt")
    mqtt_cert: str = os.environ.get("MQTT_CERT", "/certs/edge.crt")
    mqtt_key: str = os.environ.get("MQTT_KEY", "/certs/edge.key")


# --- SQLite buffer -----------------------------------------------------------

SCHEMA = """
CREATE TABLE IF NOT EXISTS readings (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    device_id TEXT NOT NULL,
    payload TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS readings_device ON readings (device_id, id);
"""


def open_db(path):
    # One connection per thread; WAL lets readers and the publisher overlap.
    conn = sqlite3.connect(path, timeout=30, isolation_level=None)
    conn.execute("PRAGMA journal_mode=WAL")
    conn.execute("PRAGMA synchronous=NORMAL")
    return conn


def init_db(path):
    os.makedirs(os.path.dirname(path) or ".", exist_ok=True)
    conn = open_db(path)
    conn.executescript(SCHEMA)
    conn.close()


# --- CSV validation ----------------------------------------------------------


class ValidationError(ValueError):
    pass


def parse_line(line):
    """Returns a typed reading dict, or raises ValidationError."""
    parts = line.split(",")
    if len(parts) != len(CSV_FIELDS):
        raise ValidationError(f"expected {len(CSV_FIELDS)} fields, got {len(parts)}")
    raw = dict(zip(CSV_FIELDS, parts))
    try:
        reading = {
            "timestamp": int(raw["timestamp"]),
            "device_id": raw["device_id"].strip(),
            "uptime_ms": int(raw["uptime_ms"]),
            "temperature_c": float(raw["temperature_c"]),
            "power_consumption_mw": float(raw["power_consumption_mw"]),
            "heap_free_bytes": int(raw["heap_free_bytes"]),
            "device_status": raw["device_status"].strip(),
            "error_code": int(raw["error_code"]),
        }
    except ValueError as exc:
        raise ValidationError(f"bad value: {exc}") from None

    if not reading["device_id"]:
        raise ValidationError("empty device_id")
    if reading["device_status"] not in VALID_STATUSES:
        raise ValidationError(f"unknown device_status {reading['device_status']!r}")
    if min(reading["uptime_ms"], reading["heap_free_bytes"], reading["error_code"]) < 0:
        raise ValidationError("negative counter")
    if reading["power_consumption_mw"] < 0:
        raise ValidationError("negative power")
    if not -40.0 <= reading["temperature_c"] <= 200.0:
        raise ValidationError(f"temperature out of range: {reading['temperature_c']}")
    return reading


def _now_ms():
    return int(time.time() * 1000)


# --- Serial readers ----------------------------------------------------------


class SerialReader:
    def __init__(self, port, settings, stop):
        self.port = port
        self.settings = settings
        self.stop = stop
        self.valid = 0
        self.invalid = 0

    def run(self):
        db = open_db(self.settings.db_path)
        while not self.stop.is_set():
            try:
                with serial.Serial(self.port, self.settings.baudrate, timeout=1) as ser:
                    log.info("reading %s", self.port)
                    self._read_loop(ser, db)
            except (serial.SerialException, OSError) as exc:
                log.debug("%s unavailable (%s), retrying", self.port, exc)
                self.stop.wait(2.0)
        db.close()

    def _read_loop(self, ser, db):
        buf = b""
        while not self.stop.is_set():
            chunk = ser.read(ser.in_waiting or 1)
            if not chunk:
                continue
            buf += chunk
            *lines, buf = buf.split(b"\n")
            for raw in lines:
                self._handle(raw, db)

    def _handle(self, raw, db):
        line = raw.decode("utf-8", errors="replace").strip()
        if not line or line.startswith("timestamp,"):
            return  # empty line or CSV header (re-sent on each simulator start)
        try:
            reading = parse_line(line)
        except ValidationError as exc:
            self.invalid += 1
            log.warning("%s: rejected line %r (%s)", self.port, line[:120], exc)
            return
        self.valid += 1
        db.execute(
            "INSERT INTO readings (device_id, payload) VALUES (?, ?)",
            (reading["device_id"], json.dumps(reading)),
        )


# --- MQTT publisher ----------------------------------------------------------


class Publisher:
    def __init__(self, settings, stop):
        self.settings = settings
        self.stop = stop
        self.client = mqtt.Client(
            callback_api_version=mqtt.CallbackAPIVersion.VERSION2,
            client_id=settings.mqtt_client_id,
            clean_session=False,
        )
        self.broker = parse_mqtt_url(settings.mqtt_url)
        username = self.broker["username"] or settings.mqtt_username
        password = self.broker["password"] or settings.mqtt_password
        if username:
            self.client.username_pw_set(username, password or None)
        if self.broker["tls"]:
            # CA / client certificate are optional: without a CA file the
            # system trust store is used, without a client cert only the
            # username/password authenticates the edge.
            has_cert = os.path.exists(settings.mqtt_cert) and os.path.exists(settings.mqtt_key)
            self.client.tls_set(
                ca_certs=settings.mqtt_ca if os.path.exists(settings.mqtt_ca) else None,
                certfile=settings.mqtt_cert if has_cert else None,
                keyfile=settings.mqtt_key if has_cert else None,
            )
        self.client.on_connect = self._on_connect
        self.client.on_disconnect = lambda c, u, f, rc, p: log.warning("MQTT disconnected (%s)", rc)
        self.client.reconnect_delay_set(min_delay=1, max_delay=30)

    @staticmethod
    def _on_connect(client, userdata, flags, reason_code, properties):
        if reason_code.is_failure:
            log.error("MQTT connection refused: %s (check MQTT_URL / credentials)", reason_code)
        else:
            log.info("MQTT connected")

    def run(self):
        s = self.settings
        b = self.broker
        log.info("MQTT broker %s:%d (tls=%s)", b["host"], b["port"], b["tls"])
        self.client.connect_async(b["host"], b["port"], keepalive=30)
        self.client.loop_start()
        db = open_db(s.db_path)
        while not self.stop.wait(s.batch_interval_s):
            if not self.client.is_connected():
                pending = db.execute("SELECT COUNT(*) FROM readings").fetchone()[0]
                log.warning("MQTT offline, %d rows kept in buffer", pending)
                continue
            self._flush(db)
        self.client.loop_stop()
        self.client.disconnect()
        db.close()

    def _flush(self, db):
        s = self.settings
        readings = db.execute(
            """
            SELECT id, device_id, payload FROM (
                SELECT id, device_id, payload,
                       ROW_NUMBER() OVER (PARTITION BY device_id ORDER BY id) AS n
                FROM readings
            ) WHERE n <= ? ORDER BY id
            """,
            (s.batch_max_rows,),
        ).fetchall()

        by_device = {}
        for row_id, device_id, payload in readings:
            by_device.setdefault(device_id, {"readings": [], "ids": []})
            by_device[device_id]["readings"].append(json.loads(payload))
            by_device[device_id]["ids"].append(row_id)

        for device_id, batch in by_device.items():
            body = json.dumps({
                "edge_id": s.mqtt_client_id,
                "device_id": device_id,
                "sent_at": _now_ms(),
                "readings": batch["readings"],
            })
            info = self.client.publish(f"{s.mqtt_topic_prefix}/{device_id}/batch", body, qos=1)
            try:
                info.wait_for_publish(timeout=5)
            except (RuntimeError, ValueError) as exc:
                log.warning("publish for %s failed: %s", device_id, exc)
                continue
            if not info.is_published():
                log.warning("publish for %s not acknowledged, will retry", device_id)
                continue
            # PUBACK received: the broker has it, drop it from the buffer.
            _delete_ids(db, "readings", batch["ids"])
            log.info("sent %s: %d readings", device_id, len(batch["readings"]))


def parse_mqtt_url(url):
    """
    mqtt://[user:password@]host[:1883]   plain TCP
    mqtts://[user:password@]host[:8883]  TLS
    """
    parts = urlsplit(url)
    schemes = {"mqtt": (False, 1883), "tcp": (False, 1883), "mqtts": (True, 8883), "ssl": (True, 8883)}
    if parts.scheme not in schemes or not parts.hostname:
        raise ValueError(f"MQTT_URL must look like mqtt://host:1883 or mqtts://host:8883, got {url!r}")
    tls, default_port = schemes[parts.scheme]
    return {
        "host": parts.hostname,
        "port": parts.port or default_port,
        "tls": tls,
        "username": unquote(parts.username or ""),
        "password": unquote(parts.password or ""),
    }


def _delete_ids(db, table, ids):
    if ids:
        db.execute(f"DELETE FROM {table} WHERE id IN ({','.join('?' * len(ids))})", ids)


# --- Main --------------------------------------------------------------------


def main():
    logging.basicConfig(
        level=os.environ.get("LOG_LEVEL", "INFO"),
        format="%(asctime)s %(levelname)s %(threadName)s: %(message)s",
    )
    settings = Settings()
    parse_mqtt_url(settings.mqtt_url)  # fail fast on a malformed URL
    init_db(settings.db_path)

    stop = threading.Event()
    signal.signal(signal.SIGTERM, lambda *_: stop.set())
    signal.signal(signal.SIGINT, lambda *_: stop.set())

    threads = []
    for port in settings.serial_ports:
        reader = SerialReader(port, settings, stop)
        threads.append(threading.Thread(target=reader.run, name=f"reader-{os.path.basename(port)}"))
    threads.append(threading.Thread(target=Publisher(settings, stop).run, name="mqtt"))

    for t in threads:
        t.start()
    log.info("edge started: %d serial ports, batches every %.0f s", len(settings.serial_ports), settings.batch_interval_s)
    for t in threads:
        t.join()


if __name__ == "__main__":
    main()
