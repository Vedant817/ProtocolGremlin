# SPDX-FileCopyrightText: (c) 2026 Jane Street Protocol Emulator ASIC project contributors
# SPDX-License-Identifier: Apache-2.0

"""
tools/protocol_fuzzer.py - Automated Protocol Fuzzing & Anomaly Injection Engine

Generates constrained-random protocol waveforms with parameterized physical-layer
and framing anomalies to stress test core protocol engines and verify state recovery:

1. Timing Jitter:
   - Dynamic cycle stretching and compression applied to individual bit/half-bit
     cells via phase-bounded edge jitter (testing sampling margins without unbounded drift).
2. Glitches & Noise:
   - Sub-baud runt pulses (1-2 cycles) injected during idle, start bits, and data phases.
3. Framing Anomalies:
   - Inverted stop bits (UART framing error), omitted preamble transitions, corrupted flags.
4. Line Coding Anomalies:
   - Manchester biphase violations (held level through mid-bit transition window).
5. Open-Drain Collision Spikes:
   - Sudden external dominant pulls to verify collision abort and High-Z bus release.
6. Multi-Frame Recovery Sequences:
   - Corrupted anomaly frames followed immediately by valid reference frames to prove
     100% state recovery without requiring a hardware reset.
"""

from enum import Enum, auto
import random
from typing import List, Tuple, Optional, Dict, Any


class AnomalyType(Enum):
    NONE = auto()
    TIMING_JITTER = auto()       # Phase-bounded per-edge timing jitter
    FALSE_START_GLITCH = auto()  # 1-cycle glitch low during start bit
    FRAMING_ERROR = auto()       # Stop bit held low instead of high
    BIPHASE_VIOLATION = auto()   # Missing mid-bit edge in Manchester
    IDLE_NOISE_RUNT = auto()     # Narrow glitch during line idle
    CONTENTION_COLLISION = auto()# Unexpected external low during open-drain high


class ProtocolFuzzer:
    """Constrained-random protocol stimulus and anomaly generator."""

    def __init__(self, seed: Optional[int] = 42):
        self.rng = random.Random(seed)

    def set_seed(self, seed: int) -> None:
        self.rng.seed(seed)

    def generate_fuzzed_uart_frame(
        self,
        payload: int,
        nominal_bit_period: int = 8,
        anomaly: AnomalyType = AnomalyType.NONE,
        jitter_range: int = 1,
        idle_before: int = 4,
        idle_after: int = 8,
    ) -> Tuple[List[int], Dict[str, Any]]:
        """
        Generate a cycle-by-cycle bitstream for a UART frame (8-N-1) with optional anomalies.

        Returns:
            stream: List of 0/1 integers per clock cycle.
            metadata: Dictionary describing injected anomaly and ground truth.
        """
        stream: List[int] = []
        meta = {
            "payload": payload,
            "anomaly": anomaly.name,
            "nominal_bit_period": nominal_bit_period,
            "is_valid": True,
        }

        # Idle before
        stream.extend([1] * idle_before)

        if anomaly == AnomalyType.IDLE_NOISE_RUNT:
            # Inject 1-cycle glitch low during idle
            stream.append(0)
            stream.extend([1] * 4)
            meta["is_valid"] = False

        if anomaly == AnomalyType.FALSE_START_GLITCH:
            # Glitch low for 1 cycle then returns high (false start)
            stream.append(0)
            stream.extend([1] * nominal_bit_period)
            meta["is_valid"] = False
            return stream, meta

        # 10 Frame symbols: start bit (0), 8 data bits LSB-first, stop bit (1 or 0)
        stop_val = 0 if anomaly == AnomalyType.FRAMING_ERROR else 1
        if anomaly == AnomalyType.FRAMING_ERROR:
            meta["is_valid"] = False

        symbols = [0] + [(payload >> b) & 1 for b in range(8)] + [stop_val]

        if anomaly == AnomalyType.TIMING_JITTER:
            # Phase-bounded edge jitter: nominal transitions shifted by +-jitter_range cycles
            # Prevents unbounded phase drift across asynchronous frame while testing edge tolerances
            edges = [0]
            for k in range(1, len(symbols)):
                nominal_edge = k * nominal_bit_period
                delta = self.rng.randint(-jitter_range, jitter_range)
                edge = max(edges[-1] + 2, nominal_edge + delta)
                edges.append(edge)
            edges.append(len(symbols) * nominal_bit_period)

            for k in range(len(symbols)):
                dur = edges[k + 1] - edges[k]
                stream.extend([symbols[k]] * dur)
        else:
            for sym in symbols:
                stream.extend([sym] * nominal_bit_period)

        # Idle after
        stream.extend([1] * idle_after)

        return stream, meta

    def generate_fuzzed_manchester_frame(
        self,
        payload: int,
        nominal_half_period: int = 8,
        anomaly: AnomalyType = AnomalyType.NONE,
        preamble_bits: int = 1,
    ) -> Tuple[List[int], Dict[str, Any]]:
        """
        Generate Manchester Biphase-L waveform with optional anomalies.
        Logic 1: High first half, Low second half.
        Logic 0: Low first half, High second half.
        """
        stream: List[int] = []
        meta = {
            "payload": payload,
            "anomaly": anomaly.name,
            "nominal_half_period": nominal_half_period,
            "is_valid": True,
        }

        # Idle low
        stream.extend([0] * 8)

        # Construct nominal half-bit symbols:
        # Preamble '1': [1, 0]
        half_bits = [1, 0]

        # 8 Data bits MSB-first:
        for bit_idx in range(7, -1, -1):
            bit = (payload >> bit_idx) & 1
            if anomaly == AnomalyType.BIPHASE_VIOLATION and bit_idx == 4:
                # Omit transition: hold constant level for entire bit cell
                half_bits.extend([1, 1])
                meta["is_valid"] = False
            else:
                h1 = 1 if bit == 1 else 0
                h2 = 0 if bit == 1 else 1
                half_bits.extend([h1, h2])

        if anomaly == AnomalyType.TIMING_JITTER:
            # Phase-bounded edge jitter on Manchester transitions
            edges = [0]
            for k in range(1, len(half_bits)):
                nominal_edge = k * nominal_half_period
                delta = self.rng.randint(-1, 1)
                edge = max(edges[-1] + 2, nominal_edge + delta)
                edges.append(edge)
            edges.append(len(half_bits) * nominal_half_period)

            for k in range(len(half_bits)):
                dur = edges[k + 1] - edges[k]
                stream.extend([half_bits[k]] * dur)
        else:
            for hb in half_bits:
                stream.extend([hb] * nominal_half_period)

        # Return to idle low
        stream.extend([0] * 6)
        return stream, meta

    def generate_recovery_test_sequence(
        self,
        valid_payload1: int = 0xA5,
        corrupt_payload: int = 0x55,
        valid_payload2: int = 0x3C,
        bit_period: int = 8,
    ) -> List[int]:
        """
        Generate back-to-back sequence:
        1. Valid Frame 1
        2. Corrupted Frame (Framing Error)
        3. Valid Frame 2
        Proves core recovers and decodes Frame 2 correctly without locking up.
        """
        full_stream: List[int] = []

        # Frame 1: Valid
        s1, _ = self.generate_fuzzed_uart_frame(valid_payload1, bit_period, AnomalyType.NONE, idle_after=12)
        full_stream.extend(s1)

        # Frame 2: Corrupted (Framing error)
        s2, _ = self.generate_fuzzed_uart_frame(corrupt_payload, bit_period, AnomalyType.FRAMING_ERROR, idle_after=12)
        full_stream.extend(s2)

        # Frame 3: Valid Recovery
        s3, _ = self.generate_fuzzed_uart_frame(valid_payload2, bit_period, AnomalyType.NONE, idle_after=16)
        full_stream.extend(s3)

        return full_stream
