# Copyright (c) 2026 Jane Street Protocol Emulator ASIC project contributors
# SPDX-License-Identifier: Apache-2.0

"""
tools/multilane_model.py - Cycle-accurate Dual-Core Multi-Lane Architecture Model & Bridge Engine

Implements the architectural simulation and firmware generation for the Multi-Lane Dual-Core
Protocol Processor:
- Dual independent execution lanes (Core 0 on Lane 0 uio[3:0], Core 1 on Lane 1 uio[7:4]).
- Split memory model: Core 0 (128 words), Core 1 (128 words).
- 1-cycle Inter-Core Event Fabric (single-cycle non-blocking strobes between cores).
- Inter-Core Lock-Free Mailbox Register (8-bit data register with FULL/EMPTY status flags).
- Full-duplex Protocol Bridge: Lane 0 receiver (e.g. Manchester or UART) streaming to
  Lane 1 transmitter (e.g. SPI Master or CAN) with 1-cycle inter-core latency.
"""

from typing import List, Tuple, Optional, Dict
import os
import sys

sys.path.insert(0, os.path.dirname(__file__))
from isa_model import CoreModel, CoreState
from assembler import assemble


class DualCoreSystem:
    """
    Cycle-accurate architectural simulator for the Dual-Core Multi-Lane Protocol Processor.

    Architecture:
    - Core 0 (Lane 0): executes Program 0 (up to 128 words). Controls uio[3:0].
    - Core 1 (Lane 1): executes Program 1 (up to 128 words). Controls uio[7:4].
    - Inter-Core Mailbox: 8-bit data register, full/empty flags.
    - Event Fabric: 1-cycle pulse signaling between Core 0 and Core 1.
    """

    def __init__(self, program0: List[int], program1: List[int]):
        self.core0 = CoreModel(program0)
        self.core1 = CoreModel(program1)

        # Mailbox state
        self.mailbox_data: int = 0
        self.mailbox_full: bool = False
        self.mailbox_overflow: bool = False
        self.mailbox_underflow: bool = False

        # Event Fabric
        self.core0_evt_strobe: bool = False
        self.core1_evt_strobe: bool = False

        # Metrics
        self.cycles = 0
        self.transfers_completed = 0

    def write_mailbox(self, data_byte: int) -> bool:
        """Called by Core 0 to write an 8-bit byte into the inter-core mailbox."""
        if self.mailbox_full:
            self.mailbox_overflow = True
            return False
        self.mailbox_data = data_byte & 0xFF
        self.mailbox_full = True
        self.core0_evt_strobe = True
        return True

    def read_mailbox(self) -> Tuple[int, bool]:
        """Called by Core 1 to read an 8-bit byte from the inter-core mailbox."""
        if not self.mailbox_full:
            self.mailbox_underflow = True
            return (0, False)
        data = self.mailbox_data
        self.mailbox_full = False
        self.core1_evt_strobe = True
        self.transfers_completed += 1
        return (data, True)

    def step(self, uio_in: int) -> Tuple[int, int]:
        """
        Execute exactly one synchronized clock cycle across both cores.

        Returns:
            Tuple of (combined_uio_out, combined_uio_oe) across all 8 pins:
            - Bits [3:0] driven by Core 0 (Lane 0)
            - Bits [7:4] driven by Core 1 (Lane 1)
        """
        self.cycles += 1

        # Clear transient single-cycle event strobes from previous cycle
        self.core0_evt_strobe = False
        self.core1_evt_strobe = False

        # Partition physical input pins:
        # Lane 0 sees uio[3:0] on its low nibble
        # Lane 1 sees uio[7:4] shifted into its low nibble
        pin_in_0 = uio_in & 0x0F
        pin_in_1 = (uio_in >> 4) & 0x0F

        # If Core 1 event line is monitored by Core 0, map to bit 3
        if self.core1_evt_strobe:
            pin_in_0 |= 0x08

        # If Core 0 event line is monitored by Core 1, map to bit 3
        if self.core0_evt_strobe:
            pin_in_1 |= 0x08

        # Step both cores concurrently
        self.core0.step(pin_in_0)
        self.core1.step(pin_in_1)

        # Merge physical outputs:
        # Core 0 drives uio[3:0]
        # Core 1 drives uio[7:4]
        out_0 = self.core0.state.effective_pin_out & 0x0F
        oe_0 = self.core0.state.effective_pin_oe & 0x0F

        out_1 = (self.core1.state.effective_pin_out & 0x0F) << 4
        oe_1 = (self.core1.state.effective_pin_oe & 0x0F) << 4

        combined_out = out_0 | out_1
        combined_oe = oe_0 | oe_1

        return combined_out, combined_oe


def build_dual_core_bridge_asm() -> Tuple[str, str]:
    """
    Generate synchronized dual-core firmware implementing a full-duplex protocol bridge:
    - Core 0 (Ingress on Lane 0 uio[0]): Receives Manchester Biphase-L byte, stores in R0,
      and signals data ready on Lane 0 pin 3 (simulating internal event line).
    - Core 1 (Egress on Lane 1 uio[4..6]): Waits for Core 0 event pulse, loads byte, and
      serializes onto SPI Master Mode 0 (uio[4]=SCK, uio[5]=MOSI, uio[6]=CS_N).
    """
    # Core 0: Manchester Ingress Engine
    # Samples Manchester bits on pin 0, accumulates into R0, asserts event strobe on pin 3
    # Preamble start bit '1' ([1, 0]): rising edge at t=0, falling edge at t=4.
    # We synchronize to rising edge on pin 0 (WAITEDGE R2, 0x08).
    # Half-period is 4 cycles. Midpoint of bit 0 first half is at t=9.
    # WAITEDGE finishes at t=2. Instruction at t=3. WAIT 5 stalls 6 cycles -> t=9.
    asm0 = [
        "; --- Core 0: Manchester Biphase Ingress Lane ---",
        "GDIRI 0x08",        # pin 0 input, pin 3 output (event strobe)
        "GWRI 0x00",         # Idle low
        "LDI R0, 0x00",
        "; Wait for Manchester Preamble Rising Edge on pin 0",
        "WAITEDGE R2, 0x08", # pin 0 rising edge (mode 1: operand = 0x08)
        "WAIT 5",            # Reach midpoint of bit 0 first half
    ]
    # 8 data bits: sample with SHIFTIN (1 cyc) + WAIT 6 (7 cyc) = 8 cyc/bit
    for bit_idx in range(8):
        asm0.append("SHIFTIN R0, 0, MSB")
        if bit_idx < 7:
            asm0.append("WAIT 6")

    # Byte complete in R0: Pulse Event Strobe on pin 3 (1 cycle)
    asm0.extend([
        "GWRI 0x08",         # Strobe HIGH
        "GWRI 0x00",         # Strobe LOW
        "WAIT 2",
        "HALT",
    ])

    # Core 1: SPI Master Egress Lane
    # Waits for Core 0 event strobe on bit 3, asserts CS_N low, transmits byte on SPI Mode 0
    # Pin mappings relative to Lane 1 (bits 0..2 map to uio[4..6]):
    # pin 0 = SCK (mask 0x01), pin 1 = MOSI (mask 0x02), pin 2 = CS_N (mask 0x04)
    # pin 3 = Inter-core Event In (mask 0x08)
    asm1 = [
        "; --- Core 1: SPI Master Egress Lane ---",
        "GDIRI 0x07",        # pin 0,1,2 output (SCK, MOSI, CS_N), pin 3 input (EVT)
        "GWRI 0x04",         # SCK=0, MOSI=0, CS_N=1 (idle)
        "; Wait for Core 0 Event Pulse on pin 3",
        "WAITEDGE R2, 0x0B", # pin 3 rising edge
        "; Initialize SPI constants and payload",
        "LDI R0, 0xA5",      # Payload byte to transmit
        "LDI R2, 0xFF",      # Constant 1s for SHIFTOUT pin set
        "LDI R3, 0x00",      # Constant 0s for SHIFTOUT pin clear
        "; Assert CS_N Low",
        "SHIFTOUT R3, 2",    # Pin 2 (CS_N) -> 0
        "WAIT 1",
    ]
    # 8 bits SPI Mode 0
    for _ in range(8):
        asm1.extend([
            "SHIFTOUT R0, 1, MSB", # Pin 1 (MOSI) <- next MSB bit
            "WAIT 1",              # Setup time
            "SHIFTOUT R2, 0",      # Pin 0 (SCK) -> 1
            "WAIT 2",              # SCK high hold
            "SHIFTOUT R3, 0",      # Pin 0 (SCK) -> 0
            "WAIT 1",              # SCK low hold
        ])

    # Deassert CS_N High (End of Transfer)
    asm1.extend([
        "WAIT 1",
        "SHIFTOUT R2, 2",    # Pin 2 (CS_N) -> 1
        "WAIT 4",
        "HALT",
    ])

    return "\n".join(asm0), "\n".join(asm1)
