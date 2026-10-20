#!/bin/bash
# Starts NUM_DEVICES simulators. Each one writes CSV to its own virtual
# serial port (socat pty), bridged over TCP to the edge (port 5000 + i).
# If any process dies, the container exits and Docker restarts it.
set -eu

NUM_DEVICES="${NUM_DEVICES:-5}"
EDGE_HOST="${EDGE_HOST:-edge}"
mkdir -p /data /tmp/tty

for i in $(seq 1 "$NUM_DEVICES"); do
    tty="/tmp/tty/dev$i"

    # UART link: pty <-> TCP to the edge (retries until the edge is up).
    socat pty,link="$tty",raw,echo=0 tcp:"$EDGE_HOST":$((5000 + i)),retry=60,interval=2 &

    # Wait for the pty, otherwise the simulator falls back to stdout.
    while [ ! -e "$tty" ]; do sleep 0.2; done

    python3 device_simulator.py \
        --device-id "$(printf 'coffee-machine-%02d' "$i")" \
        --serial-port "$tty" \
        --seed "$i" \
        --ground-truth "/data/ground_truth_$i.log" &
done

wait -n
echo "a device process exited, restarting the container" >&2
exit 1
