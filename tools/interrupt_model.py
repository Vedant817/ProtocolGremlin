#!/usr/bin/env python3
"""Asynchronous Event Notification & Level/Edge Interrupt Controller Subsystem Model.

Provides cycle-accurate modeling of:
1. Multi-channel interrupt triggering (Rising Edge, Falling Edge, Active-High, Active-Low).
2. Priority arbitration (fixed-priority resolver).
3. Interrupt Mask (IMR) and Interrupt Pending (IPR) registers with W1C semantics.
4. Physical PPA trade-off scaling on IHP 130nm SG13G2 CMOS5L.
5. Assembly firmware generators for software and hardware-assisted event handling.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from enum import IntEnum
from typing import Dict, List, Optional, Tuple


class TriggerMode(IntEnum):
    """Interrupt trigger modes."""
    ACTIVE_HIGH = 0
    ACTIVE_LOW = 1
    RISING_EDGE = 2
    FALLING_EDGE = 3


@dataclass
class InterruptChannel:
    """Configuration and state for a single interrupt line."""
    channel_id: int
    trigger_mode: TriggerMode = TriggerMode.RISING_EDGE
    priority: int = 0          # 0 = highest priority
    enabled: bool = True
    pending: bool = False
    vector_address: int = 0x10
    prev_level: int = 0
    current_level: int = 0


class InterruptControllerModel:
    """Cycle-accurate model of an on-chip Interrupt Controller (HIC)."""

    def __init__(self, num_channels: int = 4):
        self.num_channels = num_channels
        self.channels: List[InterruptChannel] = [
            InterruptChannel(
                channel_id=i,
                trigger_mode=TriggerMode.RISING_EDGE,
                priority=i,
                vector_address=0x10 + (i * 4),
            )
            for i in range(num_channels)
        ]
        self.imr: int = (1 << num_channels) - 1   # All enabled by default
        self.ipr: int = 0                         # No pending interrupts initially
        self.serviced_log: List[int] = []

    def set_trigger_mode(self, channel_id: int, mode: TriggerMode) -> None:
        """Configure trigger sensitivity for a channel."""
        if 0 <= channel_id < self.num_channels:
            self.channels[channel_id].trigger_mode = mode

    def set_mask(self, imr_val: int) -> None:
        """Update Interrupt Mask Register."""
        self.imr = imr_val & ((1 << self.num_channels) - 1)
        for i in range(self.num_channels):
            self.channels[i].enabled = bool((self.imr >> i) & 1)

    def update_inputs(self, pin_levels: Dict[int, int]) -> None:
        """Sample external asynchronous inputs and evaluate trigger conditions."""
        for ch_id, ch in enumerate(self.channels):
            new_level = pin_levels.get(ch_id, ch.current_level)
            ch.prev_level = ch.current_level
            ch.current_level = new_level

            # Check trigger condition
            triggered = False
            if ch.trigger_mode == TriggerMode.RISING_EDGE:
                if ch.prev_level == 0 and ch.current_level == 1:
                    triggered = True
            elif ch.trigger_mode == TriggerMode.FALLING_EDGE:
                if ch.prev_level == 1 and ch.current_level == 0:
                    triggered = True
            elif ch.trigger_mode == TriggerMode.ACTIVE_HIGH:
                if ch.current_level == 1:
                    triggered = True
            elif ch.trigger_mode == TriggerMode.ACTIVE_LOW:
                if ch.current_level == 0:
                    triggered = True

            if triggered:
                ch.pending = True
                self.ipr |= (1 << ch_id)

    def get_highest_priority_pending(self) -> Optional[InterruptChannel]:
        """Priority resolver: returns the unmasked pending channel with highest priority."""
        active_pending = [
            ch for ch in self.channels
            if ch.pending and ch.enabled
        ]
        if not active_pending:
            return None
        # Lower priority integer = higher priority
        active_pending.sort(key=lambda c: c.priority)
        return active_pending[0]

    def acknowledge_interrupt(self, channel_id: int) -> Optional[int]:
        """Acknowledge interrupt, clear pending flag (W1C), and return vector address."""
        if 0 <= channel_id < self.num_channels:
            ch = self.channels[channel_id]
            ch.pending = False
            self.ipr &= ~(1 << channel_id)
            self.serviced_log.append(channel_id)
            return ch.vector_address
        return None

    def clear_pending(self, channel_id: int) -> None:
        """Write-1-to-Clear pending status."""
        if 0 <= channel_id < self.num_channels:
            self.channels[channel_id].pending = False
            self.ipr &= ~(1 << channel_id)


class InterruptPpaModel:
    """PPA trade-off model for Interrupt Controller implementations on IHP 130nm."""

    BASE_CORE_CELLS = 19291
    BASE_CORE_GE = 37832
    FREQ_HZ = 10_000_000

    @staticmethod
    def get_config_metrics(config: str) -> Dict[str, float]:
        """Return cells, GE, area, and latency for a given configuration."""
        if config == "software_microcode":
            return {
                "cells": 0,
                "ge": 0,
                "area_um2": 0.0,
                "area_overhead_pct": 0.0,
                "detection_latency_cycles": 1,
                "dispatch_latency_cycles": 15,  # polling + branch + save
                "speedup_vs_software": 1.0,
            }
        elif config == "hic_4channel":
            return {
                "cells": 145,
                "ge": 284,
                "area_um2": 449.5,
                "area_overhead_pct": (145 / InterruptPpaModel.BASE_CORE_CELLS) * 100,
                "detection_latency_cycles": 2,  # dual-rank sync
                "dispatch_latency_cycles": 9,   # sync + vector + save
                "speedup_vs_software": 15 / 9,   # 1.67x
            }
        elif config == "hic_8channel":
            return {
                "cells": 260,
                "ge": 512,
                "area_um2": 806.0,
                "area_overhead_pct": (260 / InterruptPpaModel.BASE_CORE_CELLS) * 100,
                "detection_latency_cycles": 2,
                "dispatch_latency_cycles": 9,
                "speedup_vs_software": 15 / 9,   # 1.67x
            }
        elif config == "hic_8channel_nested":
            return {
                "cells": 420,
                "ge": 830,
                "area_um2": 1302.0,
                "area_overhead_pct": (420 / InterruptPpaModel.BASE_CORE_CELLS) * 100,
                "detection_latency_cycles": 2,
                "dispatch_latency_cycles": 7,   # hardware stacking
                "speedup_vs_software": 15 / 7,   # 2.14x
            }
        else:
            raise ValueError(f"Unknown config: {config}")


# ==============================================================================
# Firmware Assembly Generators
# ==============================================================================

def build_edge_event_capture_asm(trigger_pin: int = 2, edge_mode: int = 1) -> str:
    """Generate firmware waiting on an edge event via WAITEDGE with cycle timestamp capture.
    
    edge_mode: 0=falling, 1=rising, 2=both
    Result in R0: 1 (event detected), R3: duration/timestamp
    """
    imm = (edge_mode << 3) | (trigger_pin & 0x7)
    return f"""
    GDIRI 0x00             ; Configure all GPIO pins as inputs
    LDI R0, 0              ; Initialize event count = 0
    WAITEDGE R3, {imm}      ; Low-power halt waiting for edge on pin {trigger_pin}
    GRD R1                 ; Sample GPIO status upon wake
    ADDI R0, 1             ; Increment event count = 1
    HALT                   ; Halt with R0=1, R3=duration
    """


def build_level_event_handler_asm(irq_pin: int = 2, ack_pin: int = 3) -> str:
    """Generate level-sensitive interrupt service firmware with peripheral handshake clear.
    
    Performs handshake: peripheral raises irq_pin -> core services -> core pulls ack_pin LOW.
    """
    oe_mask = 1 << ack_pin
    return f"""
    GDIRI {oe_mask}        ; Configure ack_pin as output, irq_pin as input
    GWRI {oe_mask}         ; ack_pin high (idle)
    LDI R0, 0              ; Event count
poll_loop:
    GRD R1                 ; Read input pins
    MOV R2, R1
    ANDI R2, {1 << irq_pin}
    JZ poll_loop           ; Spin until irq_pin goes high

    ; Interrupt Service Routine (ISR) body
    ADDI R0, 42            ; Perform task: R0 = 42
    GWRI 0x00              ; Pulse ack_pin LOW to acknowledge peripheral
    WAIT 4                 ; Hold ACK pulse for 6 cycles
    GWRI {oe_mask}         ; Release ACK pin back high
    LDI R2, 0              ; Status OK
    HALT                   ; Clean halt
    """


def build_priority_event_dispatcher_asm(prio0_pin: int = 0, prio1_pin: int = 1) -> str:
    """Generate priority dispatcher prioritizing prio0_pin over prio1_pin.
    
    If both are asserted simultaneously, ISR0 executes first.
    R0 tracks ISR0 executions (+10), R1 tracks ISR1 executions (+5).
    """
    return f"""
    GDIRI 0x00             ; All inputs
    LDI R0, 0              ; Task 0 execution counter
    LDI R1, 0              ; Task 1 execution counter
poll_irq:
    GRD R2                 ; Read external IRQ lines
    MOV R3, R2
    ANDI R3, {1 << prio0_pin}
    JNZ isr_prio0          ; Priority 0 check (Highest)

    MOV R3, R2
    ANDI R3, {1 << prio1_pin}
    JNZ isr_prio1          ; Priority 1 check (Lower)

    JMP poll_irq           ; Wait for event

isr_prio0:
    ADDI R0, 10            ; Service high priority task
    JMP exit_isr

isr_prio1:
    ADDI R1, 5             ; Service lower priority task
    JMP exit_isr

exit_isr:
    LDI R2, 0              ; Success status
    HALT                   ; Return
    """


def build_nested_context_preservation_asm() -> str:
    """Generate firmware verifying register context preservation across simulated ISR execution.
    
    Background task loads R0=0x42, R1=0x11.
    ISR saves R0->R2, R1->R3, executes with R0=0x99, R1=0x88, then restores context.
    Final state asserts R0=0x42, R1=0x11.
    """
    return """
    GDIRI 0x00             ; Inputs
    ; Background Task Execution
    LDI R0, 0x42           ; Background state R0 = 0x42
    LDI R1, 0x11           ; Background state R1 = 0x11

    ; Simulated Interrupt Entry: Context Save
    MOV R2, R0             ; Save R0 into R2
    MOV R3, R1             ; Save R1 into R3

    ; Execute Interrupt Service Routine (ISR)
    LDI R0, 0x99           ; ISR clobbers R0
    LDI R1, 0x88           ; ISR clobbers R1
    ADDI R0, 1             ; R0 = 0x9A

    ; Simulated Interrupt Exit: Context Restore
    MOV R0, R2             ; Restore R0 = 0x42
    MOV R1, R3             ; Restore R1 = 0x11

    HALT                   ; Verify R0=0x42, R1=0x11
    """
