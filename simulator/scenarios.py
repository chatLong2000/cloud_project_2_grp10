"""
Fault scenario state machines for the coffee machine simulator.

error_code is an integer BITFIELD. Each bit flags one independent fault.
Multiple faults can be active at the same time -> their bits are combined
with a bitwise OR. error_code == 0 means no error.

Bit  Value  Meaning
---  -----  -------------------------------------------------------------
0    1      overheat        - heater failed to cut off, temperature runaway
1    2      out_of_memory   - heap exhausted (leak accumulated across one
                              or more firmware update sessions)
2    4      capsule_jam     - capsule mechanism jammed (transient, brewing only)
3    8      no_water        - water tank empty (transient, brewing only)
4    16     no_capsule      - no capsule detected (transient, brewing only)

Note: the "silent device" scenario is intentionally NOT represented in this
bitfield. When it is active the simulator emits no data at all, so there is
nothing to encode - the *absence* of data over a time window is the signal,
and it is expected to be detected by the acquisition/edge application
(gap detection), not by an error bit in a row that is never sent.

--------------------------------------------------------------------------
device_status is one of the machine's operating states, unless a fault
overrides it for the current tick:

  off | booting | preheat | standby | brewing | updating   (normal lifecycle)
  degraded | error                                          (fault overrides)

Precedence when reporting device_status for a given tick:
  error > degraded > (the actual lifecycle state)
"""

import random

# --- Bit assignment (see module docstring) -----------------------------
ERROR_BIT_OVERHEAT = 1 << 0        # 1
ERROR_BIT_OUT_OF_MEMORY = 1 << 1   # 2
ERROR_BIT_CAPSULE_JAM = 1 << 2     # 4
ERROR_BIT_NO_WATER = 1 << 3        # 8
ERROR_BIT_NO_CAPSULE = 1 << 4      # 16

# --- Lifecycle states ----------------------------------------------------
STATUS_OFF = "off"
STATUS_BOOTING = "booting"
STATUS_PREHEAT = "preheat"
STATUS_STANDBY = "standby"
STATUS_BREWING = "brewing"
STATUS_UPDATING = "updating"

# --- Fault override states ------------------------------------------------
STATUS_DEGRADED = "degraded"
STATUS_ERROR = "error"

# States during which the heater could plausibly be active/controlled -
# thermal runaway is only allowed to start or be evaluated in these states.
THERMAL_ALLOWED_STATES = {STATUS_PREHEAT, STATUS_STANDBY, STATUS_BREWING}

# States during which a low-heap "degraded" warning makes sense to surface
# (not while off/booting/updating - updating shows its own status instead).
HEAP_DEGRADED_ALLOWED_STATES = {STATUS_PREHEAT, STATUS_STANDBY, STATUS_BREWING}


class ThermalRunawayScenario:
    """
    Simulates a heater control-loop failure: temperature climbs steadily
    past the normal setpoint instead of being cut off. Crossing a warning
    threshold reports `degraded`; crossing a critical threshold reports
    `error` (overheat bit) for a few ticks and then triggers a reboot.
    """

    def __init__(self, cfg, rng: random.Random):
        self.cfg = cfg
        self.rng = rng
        self.active = False
        self.temp_offset_c = 0.0
        self._error_ticks_left = 0

    def tick(self, target_temp_c, lifecycle_state, ground_truth):
        """
        Returns (temp_offset_c, error_bits, forced_status_or_None, reboot_now).
        forced_status is "error", "degraded", or None (no opinion - caller
        should fall back to the lifecycle's own state).
        """
        cfg = self.cfg
        if not cfg["enabled"]:
            return 0.0, 0, None, False

        # Finishing an error countdown -> fire the reboot once, then reset.
        if self._error_ticks_left > 0:
            self._error_ticks_left -= 1
            reboot_now = self._error_ticks_left == 0
            offset = self.temp_offset_c
            if reboot_now:
                self.active = False
                self.temp_offset_c = 0.0
                ground_truth.log("thermal_runaway", "reboot_triggered")
            return offset, ERROR_BIT_OVERHEAT, STATUS_ERROR, reboot_now

        if not self.active:
            if lifecycle_state in THERMAL_ALLOWED_STATES and self.rng.random() < cfg[
                "trigger_probability_per_tick"
            ]:
                self.active = True
                self.temp_offset_c = 0.0
                ground_truth.log("thermal_runaway", "started")
            return 0.0, 0, None, False

        # Runaway in progress: temperature keeps climbing each tick.
        self.temp_offset_c += cfg["climb_rate_c_per_tick"]
        current_temp = target_temp_c + self.temp_offset_c

        if current_temp >= cfg["critical_temp_c"]:
            # error_duration_ticks counts THIS tick too, so subtract one now.
            self._error_ticks_left = cfg["error_duration_ticks"] - 1
            ground_truth.log("thermal_runaway", "overheat_triggered")
            reboot_now = self._error_ticks_left <= 0
            if reboot_now:
                self.active = False
                offset = self.temp_offset_c
                self.temp_offset_c = 0.0
                ground_truth.log("thermal_runaway", "reboot_triggered")
                return offset, ERROR_BIT_OVERHEAT, STATUS_ERROR, True
            return self.temp_offset_c, ERROR_BIT_OVERHEAT, STATUS_ERROR, False
        elif current_temp >= cfg["warn_temp_c"]:
            return self.temp_offset_c, 0, STATUS_DEGRADED, False

        return self.temp_offset_c, 0, None, False


class MemoryLeakScenario:
    """
    Simulates a memory leak specific to firmware update ("updating")
    sessions: if the device stays in the `updating` state for at least
    `leak_trigger_after_seconds`, one leak increment is applied (once per
    update session). The leak persists across the normal post-update
    reboot (a soft reboot doesn't fix it) - it is only cleared if the leak
    eventually accumulates enough to cross the critical threshold, which
    is treated as a hard crash that fully resets memory on reboot.
    """

    def __init__(self, cfg, rng: random.Random):
        self.cfg = cfg
        self.rng = rng
        self.leak_offset_bytes = 0
        self._seconds_in_update = 0.0
        self._leaked_this_session = False
        self._error_ticks_left = 0

    def tick(self, heap_before_leak, lifecycle_state, entered_updating, dt_seconds, ground_truth):
        """
        heap_before_leak: heap value computed from baseline + normal brew
        sawtooth, BEFORE the leak offset is subtracted.
        Returns (leak_offset_bytes, error_bits, forced_status_or_None, reboot_now).
        """
        cfg = self.cfg
        if not cfg["enabled"]:
            return 0, 0, None, False

        if entered_updating:
            self._seconds_in_update = 0.0
            self._leaked_this_session = False

        if lifecycle_state == STATUS_UPDATING:
            self._seconds_in_update += dt_seconds
            if not self._leaked_this_session and self._seconds_in_update >= cfg["leak_trigger_after_seconds"]:
                self.leak_offset_bytes += cfg["leak_amount_bytes"]
                self._leaked_this_session = True
                ground_truth.log(
                    "memory_leak_fota",
                    f"leak_applied after {self._seconds_in_update:.0f}s in update, "
                    f"leak_offset_bytes={self.leak_offset_bytes}",
                )

        # Finishing an error countdown -> fire the reboot once (hard crash: heap fully resets).
        if self._error_ticks_left > 0:
            self._error_ticks_left -= 1
            reboot_now = self._error_ticks_left == 0
            if reboot_now:
                self.leak_offset_bytes = 0
                ground_truth.log("memory_leak_fota", "reboot_triggered_heap_reset")
            return self.leak_offset_bytes, ERROR_BIT_OUT_OF_MEMORY, STATUS_ERROR, reboot_now

        effective_free = heap_before_leak - self.leak_offset_bytes
        if effective_free <= cfg["critical_free_bytes"]:
            # error_duration_ticks counts THIS tick too, so subtract one now.
            self._error_ticks_left = cfg["error_duration_ticks"] - 1
            ground_truth.log("memory_leak_fota", "out_of_memory_triggered")
            reboot_now = self._error_ticks_left <= 0
            if reboot_now:
                self.leak_offset_bytes = 0
                ground_truth.log("memory_leak_fota", "reboot_triggered_heap_reset")
                return 0, ERROR_BIT_OUT_OF_MEMORY, STATUS_ERROR, True
            return self.leak_offset_bytes, ERROR_BIT_OUT_OF_MEMORY, STATUS_ERROR, False
        elif effective_free <= cfg["warn_free_bytes"] and lifecycle_state in HEAP_DEGRADED_ALLOWED_STATES:
            return self.leak_offset_bytes, 0, STATUS_DEGRADED, False

        return self.leak_offset_bytes, 0, None, False


class IntermittentErrorScenario:
    """
    Short, self-recovering faults (capsule jam / no water / no capsule)
    with no build-up in other metrics. Only meaningful - and only allowed
    to trigger - while the machine is actively `brewing`.
    """

    _BIT_BY_NAME = {
        "capsule_jam": ERROR_BIT_CAPSULE_JAM,
        "no_water": ERROR_BIT_NO_WATER,
        "no_capsule": ERROR_BIT_NO_CAPSULE,
    }

    def __init__(self, cfg, rng: random.Random):
        self.cfg = cfg
        self.rng = rng
        self._active_bit = 0
        self._ticks_left = 0

    def tick(self, lifecycle_state, ground_truth):
        """Returns (error_bits, forced_status_or_None)."""
        cfg = self.cfg
        if not cfg["enabled"]:
            return 0, None

        if self._ticks_left > 0:
            self._ticks_left -= 1
            bit = self._active_bit
            if self._ticks_left == 0:
                ground_truth.log("intermittent_error", f"recovered bit={bit}")
                self._active_bit = 0
            return bit, STATUS_ERROR

        if lifecycle_state == STATUS_BREWING and self.rng.random() < cfg["trigger_probability_per_tick"]:
            names = list(cfg["bit_weights"].keys())
            weights = list(cfg["bit_weights"].values())
            chosen = self.rng.choices(names, weights=weights, k=1)[0]
            self._active_bit = self._BIT_BY_NAME[chosen]
            # duration_ticks counts THIS tick too, so subtract one now.
            self._ticks_left = self.rng.randint(cfg["duration_ticks_min"], cfg["duration_ticks_max"]) - 1
            bit = self._active_bit
            ground_truth.log("intermittent_error", f"started bit={bit} ({chosen})")
            if self._ticks_left <= 0:
                ground_truth.log("intermittent_error", f"recovered bit={bit}")
                self._active_bit = 0
            return bit, STATUS_ERROR

        return 0, None


class SilentDeviceScenario:
    """
    Stretch scenario: the device stops emitting any data for a window of
    time, simulating a connectivity loss or a hard hang. No error bit or
    status is used - detection of "no data for too long" is left to the
    acquisition / edge application.
    """

    def __init__(self, cfg, rng: random.Random):
        self.cfg = cfg
        self.rng = rng
        self._silent_seconds_left = 0.0

    def tick(self, dt_seconds, ground_truth):
        cfg = self.cfg
        if not cfg["enabled"]:
            return False

        if self._silent_seconds_left > 0:
            self._silent_seconds_left -= dt_seconds
            if self._silent_seconds_left <= 0:
                ground_truth.log("silent_device", "resumed")
            return True

        if self.rng.random() < cfg["trigger_probability_per_tick"]:
            self._silent_seconds_left = self.rng.uniform(
                cfg["duration_seconds_min"], cfg["duration_seconds_max"]
            )
            ground_truth.log(
                "silent_device", f"started duration_seconds={self._silent_seconds_left:.1f}"
            )
            return True

        return False
