"""
Handles formatting rows as CSV and writing them to a serial port, or to
stdout when no port is configured (useful for development/testing without
a real serial connection, or for piping into a file / socat virtual port).
"""

import sys

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


class SerialWriter:
    def __init__(self, port, baudrate):
        self._serial = None
        if port:
            try:
                import serial  # pyserial

                self._serial = serial.Serial(port, baudrate=baudrate)
            except ImportError:
                print(
                    "WARNING: pyserial not installed, falling back to stdout. "
                    "Install with: pip install pyserial",
                    file=sys.stderr,
                )
            except Exception as exc:
                print(
                    f"WARNING: could not open serial port {port} ({exc}), "
                    "falling back to stdout.",
                    file=sys.stderr,
                )

    def write_header(self):
        self._write_line(",".join(CSV_FIELDS))

    def write_row(self, row: dict):
        line = ",".join(str(row[field]) for field in CSV_FIELDS)
        self._write_line(line)

    def _write_line(self, line: str):
        if self._serial is not None:
            self._serial.write((line + "\n").encode("utf-8"))
        else:
            print(line, flush=True)

    def close(self):
        if self._serial is not None:
            self._serial.close()
