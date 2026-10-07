#!/usr/bin/env python3
"""
Coffee machine data simulator.

Generates simulated monitoring data for a smart espresso capsule machine
and streams it as CSV lines over a serial port (or stdout), at a fixed
sample interval.

device_status reflects the machine's actual operating lifecycle:
    off -> booting -> preheat -> standby <-> brewing
                         ^                      |
                         |                      v
                      booting <----------- updating
                         ^
                         |
                  standby -> off (occasional power-off cycle)

...with two fault overrides that take precedence over the lifecycle state
for a given tick: `degraded` (early warning) and `error` (active fault,
error_code bit(s) set). A configurable set of fault scenarios can be
injected on top of this normal lifecycle:

  - thermal_runaway     : heater cutoff failure, temperature climbs to critical
  - memory_leak_fota    : heap fails to reclaim after time spent "updating"
  - intermittent_errors : short self-recovering faults during brewing
  - silent_device       : device stops sending data for a while (stretch scenario)

Usage:
    python3 device_simulator.py [--config config.yaml] [--ground-truth ground_truth.log]

Requires: pyyaml (config parsing). pyserial is optional - if not installed,
or if serial.port is null in the config, output goes to stdout.
"""

import argparse
import random
import time

import yaml

from scenarios import (
    ThermalRunawayScenario,
    MemoryLeakScenario,
    IntermittentErrorScenario,
    SilentDeviceScenario,
    STATUS_OFF,
    STATUS_BOOTING,
    STATUS_PREHEAT,
    STATUS_STANDBY,
    STATUS_BREWING,
    STATUS_UPDATING,
    STATUS_DEGRADED,
    STATUS_ERROR,
)
from serial_writer import SerialWriter
from ground_truth import GroundTruthLogger


# --- Machine lifecycle ------------------------------------------------------


class MachineLifecycle:
    """
    Owns the machine's normal operating state and its transitions. Fault
    scenarios never modify this directly except via force_reboot(), which
    represents a fault interrupting normal operation.
    """

    def __init__(self, cfg, rng: random.Random):
        self.cfg = cfg
        self.rng = rng
        self.state = STATUS_BOOTING
        self.seconds_in_state = 0.0

        self._brew_timer = self._sample_range("standby_before_brew_seconds")
        self._update_timer = self._sample_range("standby_before_update_seconds")
        # The off-timer only starts counting once this power cycle's update
        # has happened, so exactly one update occurs per power-off cycle.
        self._update_pending = True
        self._off_timer = None
        self._off_duration = self._sample_range("off_duration_seconds")

    def _sample_range(self, prefix):
        return self.rng.uniform(self.cfg[f"{prefix}_min"], self.cfg[f"{prefix}_max"])

    def force_reboot(self, ground_truth, reason):
        ground_truth.log("lifecycle", f"forced_reboot ({reason})")
        self.state = STATUS_BOOTING
        self.seconds_in_state = 0.0

    def _transition(self, new_state, ground_truth):
        ground_truth.log("lifecycle", f"{self.state} -> {new_state}")
        self.state = new_state
        self.seconds_in_state = 0.0

    def tick(self, dt_seconds, current_temp_c, target_temp_c, ground_truth):
        """
        Advances the lifecycle by dt_seconds. current_temp_c is the
        temperature already computed for this tick (used to decide when
        preheat is done). Returns {"entered_updating": bool}.
        """
        self.seconds_in_state += dt_seconds
        entered_updating = False
        cfg = self.cfg

        if self.state == STATUS_OFF:
            if self.seconds_in_state >= self._off_duration:
                # A fresh power cycle starts: it will need its own update
                # before the next power-off is allowed.
                self._update_pending = True
                self._update_timer = self._sample_range("standby_before_update_seconds")
                self._transition(STATUS_BOOTING, ground_truth)

        elif self.state == STATUS_BOOTING:
            if self.seconds_in_state >= cfg["boot_duration_seconds"]:
                self._transition(STATUS_PREHEAT, ground_truth)

        elif self.state == STATUS_PREHEAT:
            if current_temp_c >= target_temp_c - 0.5 or self.seconds_in_state >= cfg["preheat_max_seconds"]:
                self._transition(STATUS_STANDBY, ground_truth)

        elif self.state == STATUS_STANDBY:
            self._brew_timer -= dt_seconds

            if self._update_pending:
                self._update_timer -= dt_seconds
            elif cfg["simulate_power_off"]:
                self._off_timer -= dt_seconds

            if self._update_pending and self._update_timer <= 0:
                self._transition(STATUS_UPDATING, ground_truth)
                entered_updating = True
            elif (not self._update_pending) and cfg["simulate_power_off"] and self._off_timer <= 0:
                self._off_duration = self._sample_range("off_duration_seconds")
                self._transition(STATUS_OFF, ground_truth)
            elif self._brew_timer <= 0:
                self._transition(STATUS_BREWING, ground_truth)

        elif self.state == STATUS_BREWING:
            if self.seconds_in_state >= cfg["brewing_duration_seconds"]:
                self._brew_timer = self._sample_range("standby_before_brew_seconds")
                self._transition(STATUS_STANDBY, ground_truth)

        elif self.state == STATUS_UPDATING:
            if self.seconds_in_state >= cfg["update_duration_seconds"]:
                if cfg["simulate_power_off"]:
                    # This power cycle's update is done; start counting down
                    # to the next power-off instead of scheduling another update.
                    self._update_pending = False
                    self._off_timer = self._sample_range("standby_before_off_seconds")
                else:
                    # No power-off cycle in play: keep updating periodically as before.
                    self._update_timer = self._sample_range("standby_before_update_seconds")
                self._transition(STATUS_BOOTING, ground_truth)  # post-update reboot

        return {"entered_updating": entered_updating}


# --- Main simulator ---------------------------------------------------------


class DeviceSimulator:
    def __init__(self, cfg, ground_truth: GroundTruthLogger):
        self.cfg = cfg
        self.ground_truth = ground_truth
        seed = cfg.get("random_seed")
        self.rng = random.Random(seed)

        self.uptime_seconds = 0.0
        self.temperature_c = cfg["baseline"]["ambient_temp_c"]

        self.lifecycle = MachineLifecycle(cfg["lifecycle"], self.rng)
        self.thermal_runaway = ThermalRunawayScenario(cfg["thermal_runaway"], self.rng)
        self.memory_leak = MemoryLeakScenario(cfg["memory_leak_fota"], self.rng)
        self.intermittent = IntermittentErrorScenario(cfg["intermittent_errors"], self.rng)
        self.silent_device = SilentDeviceScenario(cfg["silent_device"], self.rng)

    def _compute_temperature(self, state, thermal_offset_c):
        base = self.cfg["baseline"]
        target = base["target_temp_c"]

        if state in (STATUS_OFF, STATUS_BOOTING):
            # Heater off: cool back toward ambient.
            self.temperature_c = max(
                base["ambient_temp_c"], self.temperature_c - base["cooling_rate_c_per_tick"]
            )
            return self.temperature_c

        if state == STATUS_PREHEAT:
            self.temperature_c = min(target, self.temperature_c + base["warmup_rate_c_per_tick"])
            return self.temperature_c + thermal_offset_c

        # standby / brewing / updating: hold near target with noise.
        noise = self.rng.uniform(-base["temp_noise_c"], base["temp_noise_c"])
        self.temperature_c = target + noise
        return self.temperature_c + thermal_offset_c

    def _compute_power(self, state):
        base = self.cfg["baseline"]
        if state in (STATUS_OFF, STATUS_BOOTING):
            power = base["off_power_mw"]
        elif state == STATUS_PREHEAT:
            power = base["heating_power_mw"]
        elif state == STATUS_BREWING:
            power = base["heating_power_mw"] + base["brewing_power_mw"]
        elif state == STATUS_UPDATING:
            power = base["idle_power_mw"]
        else:  # standby
            power = base["idle_power_mw"]

        noise = self.rng.uniform(-base["power_noise_mw"], base["power_noise_mw"])
        return max(0, power + noise)

    def _compute_heap_before_leak(self, state):
        base = self.cfg["baseline"]
        alloc = base["heap_brew_alloc_bytes"] if state == STATUS_BREWING else 0
        noise = self.rng.uniform(-base["heap_noise_bytes"], base["heap_noise_bytes"])
        return base["heap_baseline_bytes"] - alloc + noise

    def tick(self, dt_seconds):
        self.uptime_seconds += dt_seconds

        # 1. Silent device check first - if silent, no row is produced at all.
        if self.silent_device.tick(dt_seconds, self.ground_truth):
            return None

        state_before = self.lifecycle.state
        target_temp_c = self.cfg["baseline"]["target_temp_c"]

        # 2. Thermal runaway is evaluated against the state we were in
        #    during this interval (heater activity happens "during" it).
        thermal_offset_c, thermal_bits, thermal_status, thermal_reboot = self.thermal_runaway.tick(
            target_temp_c, state_before, self.ground_truth
        )

        # 3. Physical temperature for this tick, using the pre-transition state.
        temperature_c = self._compute_temperature(state_before, thermal_offset_c)

        # 4. Advance the lifecycle using the freshly computed temperature.
        lifecycle_flags = self.lifecycle.tick(dt_seconds, temperature_c, target_temp_c, self.ground_truth)

        # 5. Heap / memory leak, gated on the pre-transition state (what the
        #    machine was actually doing during this sampled interval).
        heap_before_leak = self._compute_heap_before_leak(state_before)
        leak_offset_bytes, leak_bits, leak_status, leak_reboot = self.memory_leak.tick(
            heap_before_leak, state_before, lifecycle_flags["entered_updating"], dt_seconds, self.ground_truth
        )

        # 6. Intermittent errors, brewing only.
        intermittent_bits, intermittent_status = self.intermittent.tick(state_before, self.ground_truth)

        # 7. A fault-triggered reboot overrides whatever the lifecycle decided this tick.
        if thermal_reboot:
            self.lifecycle.force_reboot(self.ground_truth, "overheat")
        if leak_reboot:
            self.lifecycle.force_reboot(self.ground_truth, "out_of_memory")

        # 8. uptime resets the instant the device (re)enters booting.
        if state_before != STATUS_BOOTING and self.lifecycle.state == STATUS_BOOTING:
            self.uptime_seconds = 0.0

        # 9. Combine error bits and resolve final status (error > degraded > lifecycle state).
        error_bits = thermal_bits | leak_bits | intermittent_bits
        if STATUS_ERROR in (thermal_status, leak_status, intermittent_status):
            status = STATUS_ERROR
        elif STATUS_DEGRADED in (thermal_status, leak_status):
            status = STATUS_DEGRADED
        else:
            status = self.lifecycle.state

        power_mw = self._compute_power(state_before)
        heap_free_bytes = max(0, heap_before_leak - leak_offset_bytes)

        return {
            "timestamp": _epoch_millis(),
            "device_id": self.cfg["device_id"],
            "uptime_ms": int(self.uptime_seconds * 1000),
            "temperature_c": round(temperature_c, 2),
            "power_consumption_mw": round(power_mw, 1),
            "heap_free_bytes": int(heap_free_bytes),
            "device_status": status,
            "error_code": error_bits,
        }


def _epoch_millis():
    return int(time.time() * 1000)


def load_config(path):
    with open(path, "r") as f:
        return yaml.safe_load(f)


def main():
    parser = argparse.ArgumentParser(description="Coffee machine data simulator")
    parser.add_argument("--config", default="config.yaml", help="Path to config YAML file")
    parser.add_argument(
        "--ground-truth", default="ground_truth.log", help="Path to instructor ground-truth log"
    )
    args = parser.parse_args()

    cfg = load_config(args.config)
    ground_truth = GroundTruthLogger(args.ground_truth)
    simulator = DeviceSimulator(cfg, ground_truth)
    writer = SerialWriter(cfg["serial"]["port"], cfg["serial"]["baudrate"])

    interval = cfg["sample_interval_seconds"]

    writer.write_header()
    try:
        while True:
            row = simulator.tick(interval)
            if row is not None:
                writer.write_row(row)
            time.sleep(interval)
    except KeyboardInterrupt:
        pass
    finally:
        writer.close()
        ground_truth.close()


if __name__ == "__main__":
    main()
