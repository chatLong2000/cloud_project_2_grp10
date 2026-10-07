# Coffee Machine Data Simulator

Simulates monitoring data for a smart espresso capsule machine and streams
it as CSV over a serial port (or stdout), one line per sample. Runs on a
Raspberry Pi or any machine with Python 3 (an ESP32 port would need to be
rewritten in MicroPython/C - this is the Python/Raspberry Pi variant).

## Data format (CSV)

```
timestamp,device_id,uptime_ms,temperature_c,power_consumption_mw,heap_free_bytes,device_status,error_code
```

`timestamp` is a Unix epoch timestamp in milliseconds. `uptime_ms` is
milliseconds since the last reboot (resets to 0 the instant the device
enters `booting`).

### device_status

Reflects the machine's actual operating lifecycle, unless a fault
overrides it for that sample:

```
off -> booting -> preheat -> standby <-> brewing
                     ^                      |
                     |                      v
                  booting <----------- updating
                     ^
                     |
              standby -> off   (occasional power-off cycle)
```

- `off` - appliance switched off (heater off); the control board stays
  powered and keeps reporting.
- `booting` - control board (re)starting, e.g. after power-on or a reboot.
- `preheat` - heating from ambient up to the brew setpoint.
- `standby` - idle, at temperature, ready to brew.
- `brewing` - actively brewing (pump + heater engaged).
- `updating` - a simulated firmware update is in progress.
- `degraded` - early warning (approaching an overheat or low-memory
  threshold); overrides the lifecycle state above.
- `error` - an active fault, with `error_code` bit(s) set; overrides
  everything else.

Precedence for a given sample: **error > degraded > lifecycle state**.

### error_code

An integer **bitfield**. `0` = no error; multiple simultaneous faults are
OR-ed together (e.g. `17` = overheat (1) + no_capsule (16) at the same
time). See the bit table in `scenarios.py`'s module docstring.

## Fault scenarios simulated

| Scenario | What it simulates |
|---|---|
| `thermal_runaway` | Heater cutoff failure: temperature climbs past setpoint while the heater could be active (`preheat`/`standby`/`brewing`), crosses a warning then a critical threshold, forces `error` (overheat bit) then a reboot. |
| `memory_leak_fota` | If the machine stays in `updating` for at least `leak_trigger_after_seconds` (default 80s = 1:20), a fixed amount of heap fails to be reclaimed - once per update session. The leak **persists across the normal post-update reboot** (a soft reboot doesn't fix it) and only accumulates further on subsequent updates, until it crosses a critical threshold - which is treated as a hard crash (`out_of_memory` bit) that fully resets memory on reboot. |
| `intermittent_errors` | Short, self-recovering faults (capsule jam / no water / no capsule), only triggered while actively `brewing` - meant to look like noise with no build-up in other metrics. |
| `silent_device` (off by default) | The device stops emitting data entirely for a random window, simulating a lost connection or hang. No status/error bit is used for this - detecting a data gap is left to the acquisition/edge application. |

Each scenario can be toggled and tuned independently in `config.yaml`, and
they can fire simultaneously (their error bits combine).

## Setup

```bash
pip install -r requirements.txt --break-system-packages   # on Raspberry Pi OS
```

## Running

```bash
python3 device_simulator.py --config config.yaml --ground-truth ground_truth.log
```

- If `serial.port` in `config.yaml` is `null`, output goes to stdout (useful
  for testing without hardware, or piping into a file: `... > data.csv`).
- To send over a real serial port, set `serial.port` to e.g. `/dev/ttyUSB0`
  or `/dev/serial0` and install `pyserial`.
- Press `Ctrl+C` to stop.

## Ground truth log (instructor use)

`ground_truth.log` records every lifecycle transition and injected fault
event (scenario, action, timestamp) **separately** from the student-facing
data stream. Use it to verify whether a team's edge collector / dashboard
actually detected each injected anomaly - it is not meant to be shared
with students.

## Configuration

All timing, thresholds, and probabilities live in `config.yaml`:

- `random_seed`: set a fixed integer to make a run reproducible (e.g. to
  give every team the same fault sequence for fair comparison), or `null`
  for a different run each time.
- `sample_interval_seconds`: how often a row is emitted.
- `lifecycle`: normal operating timings (boot duration, brew/update
  frequency and duration, optional power-off cycle). Note:
  `update_duration_seconds` should stay comfortably above
  `memory_leak_fota.leak_trigger_after_seconds`, or the leak won't have
  time to fire during a given update.
- Per-scenario `enabled` flags let you turn faults on/off, e.g. enable
  `silent_device` only for a later, harder assignment.

## Files

```
config.yaml          - all tunable parameters
device_simulator.py  - main loop: machine lifecycle + physical value computation
scenarios.py         - fault scenario state machines + bitfield/status definitions
serial_writer.py      - CSV formatting + serial/stdout output
ground_truth.py       - instructor-only event logger
requirements.txt
```
