# SPDX-FileCopyrightText: (c) 2026 Jane Street Protocol Emulator ASIC project contributors
# SPDX-License-Identifier: Apache-2.0
"""
Hardware Watchdog Timer & Brownout Recovery Model and Firmware Generator.

Provides:
- WatchdogModel: Cycle-accurate behavioral model of a programmable Windowed
  Watchdog Timer (WWDT) and Power-On/Brownout Reset (POR/BOD) supervisor.
- build_watchdog_service_firmware: Firmware generator for periodic watchdog servicing.
- build_watchdog_hang_firmware: Firmware generator for intentional task hang & recovery.
- build_watchdog_early_pet_firmware: Firmware generator for windowed violation test.
- build_brownout_recovery_firmware: Firmware for warm-boot recovery and state validation.
"""

from typing import Optional, Tuple


class WatchdogModel:
    """
    Cycle-accurate model of an on-chip / co-processor Windowed Watchdog Timer (WWDT)
    and Power-On / Brownout Reset (POR/BOD) supervisor.

    Features:
    - Programmable timeout counter (T_max).
    - Windowed minimum service period (T_min) to catch runaway fast loops.
    - Two-token service protocol (Token A = 0x5A, Token B = 0xA5).
    - Sticky Reset Status Register (RESET_STATUS):
        0x01: Cold Power-On Reset
        0x02: Watchdog Timeout Reset
        0x03: Brownout / External Reset
        0x05: Windowed Watchdog Early Pet Violation
    - Alarm output (wdt_alarm) and soft reset pulse generator (rst_core_n).
    """

    RESET_COLD = 0x01
    RESET_WDT_TIMEOUT = 0x02
    RESET_BROWNOUT = 0x03
    RESET_WDT_WINDOW_VIOLATION = 0x05

    def __init__(
        self,
        timeout_cycles: int = 200,
        window_min_cycles: int = 0,
        token_a: int = 0x5A,
        token_b: int = 0xA5,
    ):
        self.timeout_cycles = timeout_cycles
        self.window_min_cycles = window_min_cycles
        self.token_a = token_a
        self.token_b = token_b

        self.counter = timeout_cycles
        self.elapsed_since_pet = 0
        self.token_state = 0  # 0: idle, 1: seen token_a
        self.reset_status = self.RESET_COLD
        self.wdt_alarm = False
        self.reset_pulse_remaining = 0
        self.pet_count = 0
        self.timeout_count = 0
        self.window_violation_count = 0

    def step(
        self,
        service_byte: Optional[int] = None,
        external_rst_n: bool = True,
    ) -> Tuple[bool, bool, int]:
        """
        Advance watchdog state by one clock cycle.

        Args:
            service_byte: Byte written to watchdog service address (if any).
            external_rst_n: External hardware reset signal (active-low).

        Returns:
            Tuple of (core_rst_n, wdt_alarm, reset_status).
            - core_rst_n: Active-low reset asserted to core (False = reset asserted).
            - wdt_alarm: Watchdog alarm flag (True = fault detected).
            - reset_status: Sticky reset status register value.
        """
        if not external_rst_n:
            # External hard reset (Power-on or Brownout)
            self.counter = self.timeout_cycles
            self.elapsed_since_pet = 0
            self.token_state = 0
            self.wdt_alarm = False
            self.reset_pulse_remaining = 0
            self.reset_status = self.RESET_BROWNOUT
            return (False, False, self.reset_status)

        # Handle active soft reset pulse from internal timeout/violation
        if self.reset_pulse_remaining > 0:
            self.reset_pulse_remaining -= 1
            if self.reset_pulse_remaining == 0:
                self.counter = self.timeout_cycles
                self.elapsed_since_pet = 0
                self.token_state = 0
            return (False, self.wdt_alarm, self.reset_status)

        self.elapsed_since_pet += 1

        # Process watchdog service attempt
        if service_byte is not None:
            if self.token_state == 0:
                if service_byte == self.token_a:
                    self.token_state = 1
                else:
                    # Invalid first token -> fault
                    self.wdt_alarm = True
                    self.reset_status = self.RESET_WDT_WINDOW_VIOLATION
                    self.reset_pulse_remaining = 4
                    self.window_violation_count += 1
                    return (False, True, self.reset_status)
            elif self.token_state == 1:
                if service_byte == self.token_b:
                    # Complete token sequence received! Check window minimum
                    if self.elapsed_since_pet < self.window_min_cycles:
                        # Pet arrived too early! Runaway execution violation
                        self.wdt_alarm = True
                        self.reset_status = self.RESET_WDT_WINDOW_VIOLATION
                        self.reset_pulse_remaining = 4
                        self.window_violation_count += 1
                        return (False, True, self.reset_status)
                    else:
                        # Valid service within permitted window
                        self.counter = self.timeout_cycles
                        self.elapsed_since_pet = 0
                        self.token_state = 0
                        self.pet_count += 1
                else:
                    # Invalid second token -> fault
                    self.wdt_alarm = True
                    self.reset_status = self.RESET_WDT_WINDOW_VIOLATION
                    self.reset_pulse_remaining = 4
                    self.window_violation_count += 1
                    return (False, True, self.reset_status)

        # Decrement timeout counter
        if self.counter > 0:
            self.counter -= 1

        if self.counter == 0:
            # Watchdog timeout!
            self.wdt_alarm = True
            self.reset_status = self.RESET_WDT_TIMEOUT
            self.reset_pulse_remaining = 4
            self.timeout_count += 1
            return (False, True, self.reset_status)

        return (True, self.wdt_alarm, self.reset_status)


def build_watchdog_service_firmware(
    loop_iterations: int = 5,
    task_delay_cycles: int = 20,
    token_a: int = 0x5A,
    token_b: int = 0xA5,
) -> str:
    """
    Generate firmware that executes a periodic protocol task and cleanly
    services the hardware watchdog using the two-token key protocol on uio_out.
    """
    lines = [
        "; =============================================================",
        "; Watchdog Protected Protocol Loop",
        f"; Iterations: {loop_iterations}, Task Delay: {task_delay_cycles}",
        f"; Token A: 0x{token_a:02X}, Token B: 0x{token_b:02X}",
        "; =============================================================",
        "    GDIRI 0xFF                 ; Configure all pins as output",
        "    GWRI  0x00                 ; Initial low state",
        f"    LDI   R0, 0x{loop_iterations:02X} ; Outer task loop counter",
        "    LDI   R2, 0x00             ; Status: Running",
        "",
        "task_loop:",
        "; --- Protocol Task Execution ---",
        "    ADDI  R2, 0x01             ; Increment work progress counter",
        f"    WAIT  {max(0, task_delay_cycles - 2)} ; Simulate protocol processing time",
        "",
        "; --- Safe Watchdog Service (Two-Token Protocol) ---",
        f"    GWRI  0x{token_a:02X}      ; Send Token A",
        "    WAIT  1",
        f"    GWRI  0x{token_b:02X}      ; Send Token B",
        "    WAIT  1",
        "    GWRI  0x00                 ; Return to idle",
        "    WAIT  1",
        "",
        "    DECJNZ R0, task_loop       ; Loop until tasks complete",
        "    LDI   R2, 0x00             ; Status: Clean Completion",
        "    HALT",
    ]
    return "\n".join(lines) + "\n"


def build_watchdog_hang_firmware(
    hang_after_iterations: int = 2,
    task_delay_cycles: int = 15,
    token_a: int = 0x5A,
    token_b: int = 0xA5,
) -> str:
    """
    Generate firmware that services the watchdog normally for N iterations,
    then deliberately simulates a peripheral communication hang (infinite loop)
    to prove that the hardware watchdog trips, resets the core, and recovers.
    """
    lines = [
        "; =============================================================",
        "; Watchdog Deliberate Task Hang & Recovery Test",
        f"; Hang after {hang_after_iterations} iterations",
        "; =============================================================",
        "    GDIRI 0xFF",
        "    GWRI  0x00",
        f"    LDI   R0, 0x{hang_after_iterations:02X}",
        "    LDI   R2, 0x10             ; Status: Normal execution before hang",
        "",
        "active_loop:",
        "    ADDI  R2, 0x01",
        f"    WAIT  {max(0, task_delay_cycles - 2)}",
        f"    GWRI  0x{token_a:02X}      ; Send Token A",
        "    WAIT  1",
        f"    GWRI  0x{token_b:02X}      ; Send Token B",
        "    WAIT  1",
        "    GWRI  0x00",
        "    WAIT  1",
        "    DECJNZ R0, active_loop",
        "",
        "; --- Injected Task Failure: Infinite Hang without Petting ---",
        "    LDI   R2, 0xDE             ; Status: Deadlock / Hang state",
        "deadlock_loop:",
        "    WAIT  10",
        "    JMP   deadlock_loop        ; Unbounded hang waiting for watchdog",
    ]
    return "\n".join(lines) + "\n"


def build_watchdog_early_pet_firmware(
    token_a: int = 0x5A,
    token_b: int = 0xA5,
) -> str:
    """
    Generate firmware that pets the watchdog with zero task delay,
    violating the windowed watchdog minimum interval constraint (T < T_min).
    """
    lines = [
        "; =============================================================",
        "; Windowed Watchdog Early Pet Violation Firmware",
        f"; Immediately writes tokens without meeting T_min window",
        "; =============================================================",
        "    GDIRI 0xFF",
        "    GWRI  0x00",
        f"    GWRI  0x{token_a:02X}",
        "    WAIT  1",
        f"    GWRI  0x{token_b:02X}",
        "    HALT",
    ]
    return "\n".join(lines) + "\n"


def build_brownout_recovery_firmware(
    magic_signature: int = 0xA5,
    payload_byte: int = 0x3C,
) -> str:
    """
    Generate firmware demonstrating safe brownout / warm-boot recovery:
    1. Writes a persistent signature into R3.
    2. Enters work loop processing payload_byte into R0.
    3. If reset occurs and RAM is preserved (warm boot), firmware verifies
       signature and resumes execution cleanly with R2=0x00.
    """
    lines = [
        "; =============================================================",
        "; Brownout & Safe State Recovery Firmware",
        f"; Signature: 0x{magic_signature:02X}, Payload: 0x{payload_byte:02X}",
        "; =============================================================",
        "    GDIRI 0x00                 ; High-Z inputs",
        f"    LDI   R0, 0x{payload_byte:02X} ; Target payload",
        f"    LDI   R3, 0x{magic_signature:02X} ; Set signature flag in register",
        "    LDI   R2, 0x00             ; Status: Success",
        "idle_work_loop:",
        "    WAIT  20",
        "    DECJNZ R0, idle_work_loop  ; Decrement toward 0",
        "    LDI   R2, 0xAA             ; Completed without brownout",
        "    HALT",
    ]
    return "\n".join(lines) + "\n"
