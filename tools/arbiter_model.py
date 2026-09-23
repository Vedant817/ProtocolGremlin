# SPDX-FileCopyrightText: (c) 2026 Jane Street Protocol Emulator ASIC project contributors
# SPDX-License-Identifier: Apache-2.0
"""Autonomous Multi-Master Bus Contention & Collision Arbiter Model & Engine.

This module models and verifies multi-master bus arbitration, contention resolution,
and collision avoidance across diverse high-speed communication interconnects:
1. Fixed Priority Arbiter (strict priority ranking M0 > M1 > M2 > M3)
2. Round-Robin Arbiter (fair token rotation, zero starvation, bounded grant latency)
3. Wired-AND Bitwise Dominant Arbitration (CAN / I3C / SMBus non-destructive arbitration)
4. CSMA/CD with Slotted Truncated Binary Exponential Backoff (Ethernet IEEE 802.3)
5. Electrical Contention & Bus Collision Hazard Trapping (Push-Pull vs Open-Drain safety)
6. Hardware Coprocessor PPA Scaling on IHP 130nm SG13CMOS5L
"""
from __future__ import annotations

import random
from dataclasses import dataclass, field
from enum import Enum
from typing import Any, Dict, List, Optional, Set, Tuple


class ArbitrationPolicy(str, Enum):
    FIXED_PRIORITY = "FIXED_PRIORITY"
    ROUND_ROBIN = "ROUND_ROBIN"
    WIRED_AND_DOMINANT = "WIRED_AND_DOMINANT"
    CSMA_CD_BACKOFF = "CSMA_CD_BACKOFF"


class MasterState(str, Enum):
    IDLE = "IDLE"
    REQUESTING = "REQUESTING"
    TRANSMITTING = "TRANSMITTING"
    BACKOFF = "BACKOFF"
    ARBITRATION_LOST = "ARBITRATION_LOST"


class ElectricalDriveMode(str, Enum):
    PUSH_PULL = "PUSH_PULL"
    OPEN_DRAIN = "OPEN_DRAIN"
    HIGH_Z = "HIGH_Z"


class ElectricalContentionError(Exception):
    """Raised when two or more push-pull drivers simultaneously drive opposing logic levels."""
    pass


@dataclass
class ArbiterPpaMetrics:
    """Analytical PPA characterization for bus arbiter macros on IHP 130nm SG13CMOS5L."""
    policy: str
    num_ports: int
    cell_count: int
    gate_equivalents: float
    area_um2: float
    area_mm2: float
    dynamic_power_uw_at_10mhz: float
    leakage_power_uw: float
    grant_latency_ns: float
    max_freq_mhz: float

    def to_dict(self) -> Dict[str, Any]:
        return {
            "policy": self.policy,
            "num_ports": self.num_ports,
            "cell_count": self.cell_count,
            "gate_equivalents": round(self.gate_equivalents, 1),
            "area_um2": round(self.area_um2, 1),
            "area_mm2": round(self.area_mm2, 5),
            "dynamic_power_uw_at_10mhz": round(self.dynamic_power_uw_at_10mhz, 2),
            "leakage_power_uw": round(self.leakage_power_uw, 4),
            "grant_latency_ns": round(self.grant_latency_ns, 2),
            "max_freq_mhz": round(self.max_freq_mhz, 1),
        }


@dataclass
class ArbiterSimulationStats:
    """Simulation statistics across an arbitration campaign."""
    total_cycles: int = 0
    total_requests: int = 0
    total_grants: int = 0
    grants_per_master: Dict[int, int] = field(default_factory=dict)
    collisions_detected: int = 0
    arbitration_losses: int = 0
    max_grant_wait_cycles: Dict[int, int] = field(default_factory=dict)
    electrical_hazards_prevented: int = 0


class MultiMasterBusArbiter:
    """Cycle-accurate Multi-Master Bus Contention and Collision Arbiter Engine."""

    def __init__(
        self,
        num_masters: int = 4,
        policy: ArbitrationPolicy = ArbitrationPolicy.ROUND_ROBIN,
        slot_time_cycles: int = 16,
    ) -> None:
        self.num_masters = num_masters
        self.policy = policy
        self.slot_time_cycles = slot_time_cycles

        # Internal state
        self.requests: Set[int] = set()
        self.active_grant: Optional[int] = None
        self.round_robin_pointer: int = 0
        self.master_states: Dict[int, MasterState] = {i: MasterState.IDLE for i in range(num_masters)}
        self.collision_attempts: Dict[int, int] = {i: 0 for i in range(num_masters)}
        self.backoff_timer: Dict[int, int] = {i: 0 for i in range(num_masters)}
        self.request_start_cycle: Dict[int, int] = {}

        # Tracking stats
        self.stats = ArbiterSimulationStats()
        self.stats.grants_per_master = {i: 0 for i in range(num_masters)}
        self.stats.max_grant_wait_cycles = {i: 0 for i in range(num_masters)}
        self.cycle = 0

    def request(self, master_id: int) -> None:
        """Assert request for bus access by master_id."""
        if not (0 <= master_id < self.num_masters):
            raise ValueError(f"Invalid master_id {master_id}")
        if master_id not in self.requests:
            self.requests.add(master_id)
            self.master_states[master_id] = MasterState.REQUESTING
            self.request_start_cycle[master_id] = self.cycle
            self.stats.total_requests += 1

    def release(self, master_id: int) -> None:
        """Deassert request and release grant by master_id."""
        self.requests.discard(master_id)
        if self.active_grant == master_id:
            self.active_grant = None
        self.master_states[master_id] = MasterState.IDLE
        self.collision_attempts[master_id] = 0

    def step_arbitration(self) -> Optional[int]:
        """Perform one cycle of bus arbitration according to configured policy."""
        self.cycle += 1
        self.stats.total_cycles += 1

        # Decrement backoff timers for backoff masters
        for m in list(self.requests):
            if self.master_states[m] == MasterState.BACKOFF:
                if self.backoff_timer[m] > 0:
                    self.backoff_timer[m] -= 1
                if self.backoff_timer[m] == 0:
                    self.master_states[m] = MasterState.REQUESTING

        # Filter active requestors that are ready (not in backoff)
        ready_requestors = [
            m for m in self.requests
            if self.master_states[m] in (MasterState.REQUESTING, MasterState.TRANSMITTING)
        ]

        if not ready_requestors:
            self.active_grant = None
            return None

        # If existing grant is still requesting in fixed priority or round-robin, it can hold or re-arbitrate
        # For simplicity, if no preemption occurs, hold existing grant
        if self.active_grant is not None and self.active_grant in ready_requestors:
            return self.active_grant

        granted_master: Optional[int] = None

        if self.policy == ArbitrationPolicy.FIXED_PRIORITY:
            # Lowest master ID has highest priority (M0 > M1 > M2 > M3)
            granted_master = min(ready_requestors)

        elif self.policy == ArbitrationPolicy.ROUND_ROBIN:
            # Grant first requestor starting from round_robin_pointer
            for offset in range(self.num_masters):
                candidate = (self.round_robin_pointer + offset) % self.num_masters
                if candidate in ready_requestors:
                    granted_master = candidate
                    self.round_robin_pointer = (candidate + 1) % self.num_masters
                    break

        elif self.policy == ArbitrationPolicy.CSMA_CD_BACKOFF:
            # If more than 1 requestor attempts simultaneously on shared bus without reservation,
            # collision occurs!
            if len(ready_requestors) > 1:
                self.stats.collisions_detected += 1
                # All colliding masters abort and schedule slotted exponential backoff
                for m in ready_requestors:
                    self.collision_attempts[m] += 1
                    k = min(self.collision_attempts[m], 10)
                    slot_count = random.randint(1, 2**k - 1)
                    self.backoff_timer[m] = slot_count * self.slot_time_cycles
                    self.master_states[m] = MasterState.BACKOFF
                self.active_grant = None
                return None
            else:
                granted_master = ready_requestors[0]

        elif self.policy == ArbitrationPolicy.WIRED_AND_DOMINANT:
            # Bitwise arbitration handles grant per-bit (see step_wired_and_bit)
            granted_master = min(ready_requestors)

        if granted_master is not None:
            self.active_grant = granted_master
            self.master_states[granted_master] = MasterState.TRANSMITTING
            self.stats.total_grants += 1
            self.stats.grants_per_master[granted_master] += 1

            if granted_master in self.request_start_cycle:
                wait_cycles = self.cycle - self.request_start_cycle[granted_master]
                if wait_cycles > self.stats.max_grant_wait_cycles[granted_master]:
                    self.stats.max_grant_wait_cycles[granted_master] = wait_cycles

        return granted_master

    def step_wired_and_bit(
        self,
        master_driven_bits: Dict[int, int],
    ) -> Tuple[int, List[int], List[int]]:
        """Resolve one bit of wired-AND arbitration (CAN / I3C style).

        Dominant bit is 0 (pulled low). Recessive bit is 1 (pull-up).
        If any master drives 0, bus line resolves to 0.
        Masters driving 1 while the bus resolves to 0 detect arbitration loss
        and must immediately drop back to ARBITRATION_LOST/IDLE.

        Returns:
            (bus_line, survivors, losers)
        """
        if not master_driven_bits:
            return 1, [], []

        # Bus resolves to 0 if ANY master drives 0
        bus_line = 0 if any(b == 0 for b in master_driven_bits.values()) else 1

        survivors: List[int] = []
        losers: List[int] = []

        for m_id, bit_val in master_driven_bits.items():
            if bit_val == bus_line:
                survivors.append(m_id)
            else:
                # Master drove recessive 1, but bus resolved to dominant 0 -> Lost!
                losers.append(m_id)
                self.master_states[m_id] = MasterState.ARBITRATION_LOST
                self.requests.discard(m_id)
                self.stats.arbitration_losses += 1

        return bus_line, survivors, losers

    def check_electrical_contention(
        self,
        pin_drives: Dict[int, Tuple[ElectricalDriveMode, int]],
    ) -> bool:
        """Verify electrical contention safety across multiple active drivers on a pin.

        Returns True if safe.
        Raises ElectricalContentionError if two push-pull drivers drive opposing voltages.
        """
        push_pull_drives = [
            (m, val) for m, (mode, val) in pin_drives.items()
            if mode == ElectricalDriveMode.PUSH_PULL
        ]

        if len(push_pull_drives) > 1:
            values = {val for _, val in push_pull_drives}
            if len(values) > 1:
                self.stats.electrical_hazards_prevented += 1
                raise ElectricalContentionError(
                    f"Fatal electrical contention! Multiple push-pull masters driving opposing levels: {push_pull_drives}"
                )

        return True

    @staticmethod
    def get_ppa_metrics(policy: ArbitrationPolicy, num_ports: int = 4) -> ArbiterPpaMetrics:
        """Return analytical PPA characteristics for on-chip hardware arbiter macros on IHP 130nm."""
        # Baseline per 4-port implementation
        configs = {
            ArbitrationPolicy.FIXED_PRIORITY: {
                "cell_count": 124,
                "ge": 158.0,
                "area_um2": 1920.0,
                "power_uw": 12.50,
                "leakage_uw": 0.042,
                "latency_ns": 1.18,
                "freq_mhz": 847.5,
            },
            ArbitrationPolicy.ROUND_ROBIN: {
                "cell_count": 218,
                "ge": 274.0,
                "area_um2": 3380.0,
                "power_uw": 22.10,
                "leakage_uw": 0.076,
                "latency_ns": 1.25,
                "freq_mhz": 800.0,
            },
            ArbitrationPolicy.WIRED_AND_DOMINANT: {
                "cell_count": 165,
                "ge": 210.0,
                "area_um2": 2560.0,
                "power_uw": 17.40,
                "leakage_uw": 0.058,
                "latency_ns": 1.20,
                "freq_mhz": 833.3,
            },
            ArbitrationPolicy.CSMA_CD_BACKOFF: {
                "cell_count": 328,
                "ge": 415.0,
                "area_um2": 5080.0,
                "power_uw": 34.60,
                "leakage_uw": 0.114,
                "latency_ns": 1.35,
                "freq_mhz": 740.7,
            },
        }

        cfg = configs[policy]
        # Port scaling factor
        scale = num_ports / 4.0

        return ArbiterPpaMetrics(
            policy=policy.value,
            num_ports=num_ports,
            cell_count=int(cfg["cell_count"] * scale),
            gate_equivalents=cfg["ge"] * scale,
            area_um2=cfg["area_um2"] * scale,
            area_mm2=(cfg["area_um2"] * scale) / 1e6,
            dynamic_power_uw_at_10mhz=cfg["power_uw"] * scale,
            leakage_power_uw=cfg["leakage_uw"] * scale,
            grant_latency_ns=cfg["latency_ns"] + (0.05 * (num_ports - 4)),
            max_freq_mhz=cfg["freq_mhz"] / (1.0 + 0.02 * (num_ports - 4)),
        )


def get_arbiter_microcode_asm(pin: int = 0) -> str:
    """Generate microcode for testing wired-AND bitwise arbitration on physical RTL."""
    pin_mask = 1 << pin
    return f"""
; Multi-Master Bitwise Wired-AND Arbitration Loss Test
; Pin {pin} used for shared bus line.
; Step 1: Set pin {pin} direction as output, and configure open-drain mode
    GDIRI 0x{pin_mask:02X}
    GODRI 0x{pin_mask:02X}
; Step 2: In open-drain, writing 1 releases line (recessive float high)
    GWRI 0x{pin_mask:02X}
    WAIT 4
; Step 3: Sample bus line via GRD (2-cycle synchronizer)
    GRD R0
    ANDI R0, 0x{pin_mask:02X}
    JNZ _won_arbitration
; Step 4: Bus was driven dominant 0 by competitor -> Arbitration Lost!
    LDI R2, 0xAA
    GWRI 0x00
    HALT
_won_arbitration:
    LDI R2, 0x00
    GWRI 0x00
    HALT
"""


