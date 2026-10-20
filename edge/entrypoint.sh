#!/bin/sh
# Recreates one virtual serial port (pty) per device: each socat listens on
# a TCP port fed by the devices container and exposes the stream as a pty
# that edge.py reads like a real UART.
set -eu

NUM_DEVICES="${NUM_DEVICES:-5}"
BASE_PORT="${BASE_PORT:-5001}"
mkdir -p /tmp/tty

ports=""
for i in $(seq 1 "$NUM_DEVICES"); do
    tty="/tmp/tty/dev$i"
    (
        while true; do
            socat pty,link="$tty",raw,echo=0 tcp-listen:$((BASE_PORT + i - 1)),reuseaddr || true
            sleep 1
        done
    ) &
    ports="${ports:+$ports,}$tty"
done

export SERIAL_PORTS="${SERIAL_PORTS:-$ports}"
exec python3 -u edge.py
