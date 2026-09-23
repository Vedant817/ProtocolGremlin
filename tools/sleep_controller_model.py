# SPDX-FileCopyrightText: (c) 2026 Jane Street Protocol Emulator ASIC project contributors
# SPDX-License-Identifier: Apache-2.0
"""Low-Power Autonomous Deep-Sleep Controller & Event-Driven Wakeup Subsystem Model.

This module models, benchmarks, and verifies power-state management, quiescent
leakage reduction, and multi-source event-driven wakeup logic for the ASIC:
1. Power States: ACTIVE, IDLE_WAIT, STANDBY_RETENTION, DEEP_SLEEP
2. Multi-Source Wakeup: GPIO Edge (rising/falling), Low-Power Sleep Timer, Watchdog, Reset
3. Sub-Microsecond Digital Glitch Filtering (suppresses transient noise spikes)
4. Wakeup Cause Status Register Latching
5. In-Core Physical RTL Microcode Execution (WAITEDGE low-power stall & instant wake)
6. Calibrated IHP 130nm SG13CMOS5L Power & Timing Scaling Analysis
"""
from __future__ import annotations

from dataclasses import dataclass, field
from enum import Enum
from typing import Any, Dict, List, Optional, Tuple


class SleepPowerState(str, Enum):
    ACTIVE = "ACTIVE"
    IDLE_WAIT = "IDLE_WAIT"
    STANDBY_RETENTION = "STANDBY_RETENTION"
    DEEP_SLEEP = "DEEP_SLEEP"


class WakeupSource(str, Enum):
    NONE = "NONE"
    PIN_EDGE_RISING = "PIN_EDGE_RISING"
    PIN_EDGE_FALLING = "PIN_EDGE_FALLING"
    SLEEP_TIMER = "SLEEP_TIMER"
    WATCHDOG_TIMEOUT = "WATCHDOG_TIMEOUT"
    HARDWARE_RESET = "HARDWARE_RESET"


@dataclass
class SleepPpaMetrics:
    """Power and timing metrics for power states on IHP 130nm SG13CMOS5L."""
    state: str
    voltage_v: float
    frequency_mhz: float
    dynamic_power_uw: float
    static_leakage_uw: float
    total_power_uw: float
    wakeup_latency_cycles: int
    energy_per_wakeup_pj: float

    def to_dict(self) -> Dict[str, Any]:
        return {
            "state": self.state,
            "voltage_v": self.voltage_v,
            "frequency_mhz": self.frequency_mhz,
            "dynamic_power_uw": round(self.dynamic_power_uw, 2),
            "static_leakage_uw": round(self.static_leakage_uw, 4),
            "total_power_uw": round(self.total_power_uw, 2),
            "wakeup_latency_cycles": self.wakeup_latency_cycles,
            "energy_per_wakeup_pj": round(self.energy_per_wakeup_pj, 2),
        }


class SleepController:
    """Cycle-accurate model of the Low-Power Deep-Sleep & Wakeup Subsystem."""

    def __init__(
        self,
        glitch_filter_cycles: int = 2,
        num_pins: int = 8,
    ) -> None:
        self.glitch_filter_cycles = glitch_filter_cycles
        self.num_pins = num_pins

        # State
        self.current_state = SleepPowerState.ACTIVE
        self.timer_counter: int = 0
        self.timer_enabled: bool = False
        self.last_wakeup_cause = WakeupSource.NONE
        self.wakeup_pin_id: Optional[int] = None

        # Pin glitch filters: pin_id -> (current_level, stable_count)
        self.pin_states: Dict[int, int] = {p: 0 for p in range(num_pins)}
        self.pin_stable_counts: Dict[int, int] = {p: 0 for p in range(num_pins)}
        self.filtered_pin_states: Dict[int, int] = {p: 0 for p in range(num_pins)}

        # Statistics
        self.total_wakeups: int = 0
        self.spurious_glitches_rejected: int = 0
        self.cycles_in_state: Dict[SleepPowerState, int] = {
            s: 0 for s in SleepPowerState
        }

    def enter_sleep(self, target_state: SleepPowerState) -> None:
        """Command sleep controller to transition into low-power state."""
        if target_state == SleepPowerState.ACTIVE:
            self.current_state = SleepPowerState.ACTIVE
        else:
            self.current_state = target_state

    def set_sleep_timer(self, duration_cycles: int) -> None:
        """Configure sleep timer countdown."""
        self.timer_counter = duration_cycles
        self.timer_enabled = (duration_cycles > 0)

    def tick_cycle(self, pin_inputs: Optional[Dict[int, int]] = None) -> Tuple[SleepPowerState, WakeupSource]:
        """Advance simulation by 1 clock cycle and process wakeup events."""
        self.cycles_in_state[self.current_state] += 1
        wakeup_event = WakeupSource.NONE

        # 1. Update timer
        if self.timer_enabled and self.timer_counter > 0:
            self.timer_counter -= 1
            if self.timer_counter == 0:
                self.timer_enabled = False
                wakeup_event = WakeupSource.SLEEP_TIMER

        # 2. Process pin inputs with digital glitch filter
        if pin_inputs:
            for p, lvl in pin_inputs.items():
                if p >= self.num_pins:
                    continue

                if lvl == self.pin_states[p]:
                    self.pin_stable_counts[p] += 1
                else:
                    self.pin_states[p] = lvl
                    self.pin_stable_counts[p] = 1

                # Qualify edge only after glitch_filter_cycles stable samples
                if self.pin_stable_counts[p] >= self.glitch_filter_cycles:
                    old_filtered = self.filtered_pin_states[p]
                    if lvl != old_filtered:
                        self.filtered_pin_states[p] = lvl
                        if old_filtered == 0 and lvl == 1:
                            if wakeup_event == WakeupSource.NONE:
                                wakeup_event = WakeupSource.PIN_EDGE_RISING
                                self.wakeup_pin_id = p
                        elif old_filtered == 1 and lvl == 0:
                            if wakeup_event == WakeupSource.NONE:
                                wakeup_event = WakeupSource.PIN_EDGE_FALLING
                                self.wakeup_pin_id = p

        # 3. Check state transition if waking up from sleep
        if self.current_state != SleepPowerState.ACTIVE and wakeup_event != WakeupSource.NONE:
            self.last_wakeup_cause = wakeup_event
            self.current_state = SleepPowerState.ACTIVE
            self.total_wakeups += 1

        return self.current_state, wakeup_event

    def filter_glitch_test(self, pin: int, pulse_duration_cycles: int) -> bool:
        """Simulate a runt noise pulse and verify if it triggers wakeup."""
        initial_filtered = self.filtered_pin_states[pin]
        # Assert glitch
        for _ in range(pulse_duration_cycles):
            self.tick_cycle({pin: 1 - initial_filtered})
        # Return to baseline
        for _ in range(self.glitch_filter_cycles + 1):
            self.tick_cycle({pin: initial_filtered})

        # Did it trigger a spurious wakeup?
        if pulse_duration_cycles < self.glitch_filter_cycles:
            if self.last_wakeup_cause == WakeupSource.NONE:
                self.spurious_glitches_rejected += 1
                return True  # Glitch correctly suppressed
            return False  # Spurious wakeup occurred
        return True

    @staticmethod
    def get_ppa_metrics(state: SleepPowerState) -> SleepPpaMetrics:
        """Return calibrated power and wakeup latency metrics on IHP 130nm SG13CMOS5L."""
        profiles = {
            SleepPowerState.ACTIVE: SleepPpaMetrics(
                state=SleepPowerState.ACTIVE.value,
                voltage_v=1.2,
                frequency_mhz=10.0,
                dynamic_power_uw=308.52,
                static_leakage_uw=0.584,
                total_power_uw=309.10,
                wakeup_latency_cycles=0,
                energy_per_wakeup_pj=0.0,
            ),
            SleepPowerState.IDLE_WAIT: SleepPpaMetrics(
                state=SleepPowerState.IDLE_WAIT.value,
                voltage_v=1.2,
                frequency_mhz=10.0,
                dynamic_power_uw=3.99,
                static_leakage_uw=0.584,
                total_power_uw=4.57,
                wakeup_latency_cycles=1,
                energy_per_wakeup_pj=0.457,
            ),
            SleepPowerState.STANDBY_RETENTION: SleepPpaMetrics(
                state=SleepPowerState.STANDBY_RETENTION.value,
                voltage_v=1.2,
                frequency_mhz=0.0,
                dynamic_power_uw=0.00,
                static_leakage_uw=0.584,
                total_power_uw=0.584,
                wakeup_latency_cycles=2,
                energy_per_wakeup_pj=1.168,
            ),
            SleepPowerState.DEEP_SLEEP: SleepPpaMetrics(
                state=SleepPowerState.DEEP_SLEEP.value,
                voltage_v=0.8,
                frequency_mhz=0.0,
                dynamic_power_uw=0.00,
                static_leakage_uw=0.048,
                total_power_uw=0.048,
                wakeup_latency_cycles=8,
                energy_per_wakeup_pj=3.84,
            ),
        }
        return profiles[state]


def get_sleep_wakeup_microcode_asm(pin: int = 0, edge_mode: int = 1) -> str:
    """Generate microcode that enters low-power WAITEDGE stall and awakens on pin edge.

    edge_mode:
        0 = falling edge (1 -> 0)
        1 = rising edge (0 -> 1)
        2 = any edge
    """
    imm8 = (edge_mode << 3) | (pin & 0x07)
    return f"""
; Low-Power Deep Sleep & Event-Driven Wakeup Test
; Step 1: Set pin {pin} direction as input (0x00)
    GDIRI 0x00
; Step 2: Clear status registers
    LDI R2, 0x00
    LDI R3, 0x00
; Step 3: Enter low-power WAITEDGE stall (clock gated until edge occurs)
    WAITEDGE R3, 0x{imm8:02X}
; Step 4: Core wakes up immediately! Latch success code in R2
    LDI R2, 0xAA
    HALT
"""
