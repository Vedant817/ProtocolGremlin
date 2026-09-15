# tools/swd_model.py - Cycle-accurate ARM SWD (Serial Wire Debug) target model & firmware generator
#
# Implements ARM Debug Interface Architecture Specification ADIv5 (IHI0031A):
# - 2-wire interface: SWCLK (clock driven by host) and SWDIO (bidirectional data)
# - Line Reset: >= 50 continuous SWCLK rising edges with SWDIO=1
# - JTAG-to-SWD switching sequence: 0x79E7 (16 bits, LSB first: 0b0111_1001_1110_0111)
# - 8-bit packet header with even parity: APnDP, RnW, A[2:3], Parity, Stop(0), Park(1)
# - 3-bit ACK response: OK (001b), WAIT (010b), FAULT (100b)
# - 32-bit Data Phase (LSB-first) with even parity over 32 data bits
#
# Part of the Jane Street Protocol Emulator ASIC verification suite.

from typing import Tuple, List, Optional


def calculate_parity(val: int, num_bits: int) -> int:
    """Calculate even parity: returns 1 if odd number of 1s in val, 0 if even number of 1s.
    So (popcount(val) + parity) is always even.
    """
    count = 0
    for i in range(num_bits):
        if (val >> i) & 1:
            count += 1
    return count & 1


def build_swd_header(apndp: int, rnw: int, a2: int, a3: int) -> int:
    """Construct 8-bit ARM SWD packet header:
    Bit 0: Start = 1
    Bit 1: APnDP (0=DP, 1=AP)
    Bit 2: RnW (0=Write, 1=Read)
    Bit 3: A[2]
    Bit 4: A[3]
    Bit 5: Parity = (APnDP ^ RnW ^ A[2] ^ A[3])
    Bit 6: Stop = 0
    Bit 7: Park = 1
    """
    parity = (apndp ^ rnw ^ a2 ^ a3) & 1
    header = (1 << 0) | ((apndp & 1) << 1) | ((rnw & 1) << 2) | \
             ((a2 & 1) << 3) | ((a3 & 1) << 4) | (parity << 5) | \
             (0 << 6) | (1 << 7)
    return header


class SwdTarget:
    """Independent cycle-accurate emulation model of an ARM SW-DP (Debug Port) target.

    State machine tracks SWCLK transitions, parses packet headers, and drives
    ACK, read data words, and parity on SWDIO.
    """

    STATE_RESET = 0
    STATE_IDLE = 1
    STATE_HEADER = 2
    STATE_TRN_TO_TARGET = 3
    STATE_ACK = 4
    STATE_READ_DATA = 5
    STATE_READ_PARITY = 6
    STATE_TRN_TO_HOST = 7

    ACK_OK = 0b001
    ACK_WAIT = 0b010
    ACK_FAULT = 0b100

    def __init__(
        self,
        dpidr: int = 0x0BA01477,  # Default: ARM Cortex-M0/M3/M4 DPIDR
        pullup: bool = True
    ):
        self.dpidr = dpidr
        self.pullup = pullup

        # DP Registers
        self.registers = {
            0x0: dpidr,       # DPIDR (Read-only)
            0x4: 0x00000000,  # CTRL/STAT
            0x8: 0x00000000,  # SELECT
            0xC: 0x00000000   # RDBUFF
        }

        # Protocol state machine
        self.state = self.STATE_RESET
        self.line_reset_count = 0
        self.swd_active = False
        self.last_swclk = 0
        self.transactions_completed = 0
        self.last_header_received: Optional[int] = None
        self.header_bits: List[int] = []
        self.rnw: int = 1
        self.reg_addr: int = 0
        self.bit_index: int = 0
        self.target_drive_val: Optional[int] = None

        # Fault injection
        self.forced_ack: Optional[int] = None
        self.inject_data_parity_err: bool = False

    def step(self, swclk: int, swdio_host: int, host_oe: int) -> int:
        """Step the SWD target state machine by one cycle.

        Args:
            swclk: current SWCLK level (0 or 1)
            swdio_host: value driven by host on SWDIO (0 or 1)
            host_oe: whether host is driving SWDIO (1=host drives, 0=host released)

        Returns:
            Current SWDIO line value (0 or 1) taking into account target drive and pullup.
        """
        # Detect clock transitions
        rising_edge = (swclk == 1 and self.last_swclk == 0)
        falling_edge = (swclk == 0 and self.last_swclk == 1)
        self.last_swclk = swclk

        if rising_edge:
            self._handle_rising_edge(swdio_host, host_oe)

        if falling_edge:
            self._handle_falling_edge()

        # Determine SWDIO line value
        if self.target_drive_val is not None:
            return self.target_drive_val
        elif host_oe:
            return swdio_host
        else:
            return 1 if self.pullup else 0

    def _handle_rising_edge(self, swdio_host: int, host_oe: int):
        effective_swdio = swdio_host if host_oe else (1 if self.pullup else 0)

        # Track line reset (consecutive 1s)
        if effective_swdio == 1:
            self.line_reset_count += 1
            if self.line_reset_count >= 50:
                self.swd_active = True
                self.state = self.STATE_IDLE
                self.target_drive_val = None
        else:
            # Line reset sequence interrupted by a 0
            if self.line_reset_count >= 50:
                # Was in reset, now idle
                self.state = self.STATE_IDLE
            self.line_reset_count = 0

        # State machine processing on rising edge (sampling)
        if self.state == self.STATE_IDLE:
            if effective_swdio == 1 and self.line_reset_count < 50:
                # Start bit detected!
                self.state = self.STATE_HEADER
                self.header_bits = [1]

        elif self.state == self.STATE_HEADER:
            self.header_bits.append(effective_swdio)
            if len(self.header_bits) == 8:
                # Header complete: [Start, APnDP, RnW, A2, A3, Parity, Stop, Park]
                start = self.header_bits[0]
                apndp = self.header_bits[1]
                rnw = self.header_bits[2]
                a2 = self.header_bits[3]
                a3 = self.header_bits[4]
                parity = self.header_bits[5]
                stop = self.header_bits[6]
                park = self.header_bits[7]

                expected_parity = (apndp ^ rnw ^ a2 ^ a3) & 1
                header_byte = sum(b << i for i, b in enumerate(self.header_bits))
                self.last_header_received = header_byte

                if start == 1 and stop == 0 and park == 1 and parity == expected_parity:
                    self.rnw = rnw
                    self.reg_addr = (a2 << 2) | (a3 << 3)
                    self.state = self.STATE_TRN_TO_TARGET
                else:
                    # Invalid header: return to IDLE
                    self.state = self.STATE_IDLE

        elif self.state == self.STATE_TRN_TO_TARGET:
            # Turnaround complete on this rising edge; next will be ACK bit 0
            self.state = self.STATE_ACK
            self.bit_index = 0

        elif self.state == self.STATE_ACK:
            # Host sampled ACK[bit_index] on this rising edge
            self.bit_index += 1
            if self.bit_index == 3:
                ack = self.forced_ack if self.forced_ack is not None else self.ACK_OK
                if ack == self.ACK_OK and self.rnw == 1:
                    self.state = self.STATE_READ_DATA
                    self.bit_index = 0
                else:
                    self.state = self.STATE_TRN_TO_HOST

        elif self.state == self.STATE_READ_DATA:
            # Host sampled DATA[bit_index] on this rising edge
            self.bit_index += 1
            if self.bit_index == 32:
                self.state = self.STATE_READ_PARITY

        elif self.state == self.STATE_READ_PARITY:
            # Host sampled Parity bit on this rising edge
            self.state = self.STATE_TRN_TO_HOST

        elif self.state == self.STATE_TRN_TO_HOST:
            # Turnaround complete on this rising edge; target releases bus
            self.state = self.STATE_IDLE
            self.target_drive_val = None
            self.transactions_completed += 1

    def _handle_falling_edge(self):
        # State machine processing on falling edge (driving output)
        if self.state == self.STATE_TRN_TO_TARGET:
            # Prepare to drive ACK bit 0 on falling edge
            ack = self.forced_ack if self.forced_ack is not None else self.ACK_OK
            self.target_drive_val = (ack >> 0) & 1

        elif self.state == self.STATE_ACK:
            ack = self.forced_ack if self.forced_ack is not None else self.ACK_OK
            if self.bit_index < 3:
                self.target_drive_val = (ack >> self.bit_index) & 1
            else:
                if ack == self.ACK_OK and self.rnw == 1:
                    # Prepare data bit 0
                    data_word = self.registers.get(self.reg_addr, self.dpidr)
                    self.target_drive_val = (data_word >> 0) & 1
                else:
                    self.target_drive_val = None

        elif self.state == self.STATE_READ_DATA:
            data_word = self.registers.get(self.reg_addr, self.dpidr)
            if self.bit_index < 32:
                self.target_drive_val = (data_word >> self.bit_index) & 1
            else:
                # Prepare parity bit
                par = calculate_parity(data_word, 32)
                if self.inject_data_parity_err:
                    par ^= 1
                self.target_drive_val = par

        elif self.state == self.STATE_READ_PARITY:
            # Data parity driven, next is turnaround: release bus
            self.target_drive_val = None

        elif self.state == self.STATE_TRN_TO_HOST:
            self.target_drive_val = None


def build_swd_read_dpidr_asm(
    swclk_pin: int = 4,
    swdio_pin: int = 5,
    do_line_reset: bool = True
) -> str:
    """Generate assembly firmware to read ARM SWD DPIDR (Debug Port Identification Register).

    Protocol flow:
    1. Initialize GPIO directions (SWCLK and SWDIO as outputs).
    2. (Optional) Line reset: 52 cycles with SWDIO=1.
    3. 2 idle cycles (SWDIO=0).
    4. Packet Request Header: 0xA5 (Start=1, APnDP=0, RnW=1, A2=0, A3=0, Parity=1, Stop=0, Park=1).
    5. Turnaround cycle: SWDIO switches to input.
    6. ACK phase: Host clocks 3 cycles, samples ACK into R2.
    7. 32-bit Data phase: Host clocks 32 cycles, shifts D[7:0] into R0, D[15:8] into R1,
       D[23:16] into R2, D[31:24] into R3.
    8. Parity phase: 1 clock cycle.
    9. Turnaround cycle: SWDIO switches back to output.
    10. 4 idle clock cycles.
    11. HALT.

    Final register state:
    - R0: DPIDR[7:0]
    - R1: DPIDR[15:8]
    - R2: DPIDR[23:16]
    - R3: DPIDR[31:24]
    """
    clk_mask = 1 << swclk_pin
    dio_mask = 1 << swdio_pin
    both_mask = clk_mask | dio_mask

    asm = []
    asm.append("; --- ARM SWD DPIDR Read Firmware ---")
    asm.append(f"GDIRI 0x{both_mask:02X}")
    asm.append(f"GWRI 0x{dio_mask:02X}")  # SWCLK=0, SWDIO=1

    if do_line_reset:
        # Line reset: 52 clocks with SWDIO=1
        asm.append("LDI R0, 52")
        asm.append("line_reset_loop:")
        asm.append(f"GWRI 0x{both_mask:02X}")  # SWCLK=1, SWDIO=1
        asm.append("WAIT 1")
        asm.append(f"GWRI 0x{dio_mask:02X}")   # SWCLK=0, SWDIO=1
        asm.append("WAIT 1")
        asm.append("DECJNZ R0, line_reset_loop")

        # 2 idle cycles (SWDIO=0)
        asm.append("LDI R0, 2")
        asm.append("idle_loop_pre:")
        asm.append(f"GWRI 0x{clk_mask:02X}")   # SWCLK=1, SWDIO=0
        asm.append("WAIT 1")
        asm.append("GWRI 0x00")                # SWCLK=0, SWDIO=0
        asm.append("WAIT 1")
        asm.append("DECJNZ R0, idle_loop_pre")

    # Send 8-bit Header: 0xA5 (LSB-first: 1, 0, 1, 0, 0, 1, 0, 1)
    header_byte = build_swd_header(apndp=0, rnw=1, a2=0, a3=0)  # 0xA5
    for bit_idx in range(8):
        bit = (header_byte >> bit_idx) & 1
        low_val = dio_mask if bit else 0
        high_val = both_mask if bit else clk_mask
        asm.append(f"GWRI 0x{low_val:02X}")
        asm.append(f"GWRI 0x{high_val:02X}")  # SWCLK=1, target samples
        asm.append("WAIT 1")
        asm.append(f"GWRI 0x{low_val:02X}")
        asm.append("WAIT 1")

    # Turnaround cycle (Trn): Host switches SWDIO to input
    asm.append(f"GDIRI 0x{clk_mask:02X}")     # SWCLK output, SWDIO input
    asm.append(f"GWRI 0x{clk_mask:02X}")      # SWCLK=1
    asm.append("WAIT 1")
    asm.append("GWRI 0x00")                   # SWCLK=0
    asm.append("WAIT 1")

    # 3-bit ACK sampling into R2
    asm.append("LDI R2, 0")
    for _ in range(3):
        asm.append(f"GWRI 0x{clk_mask:02X}")  # SWCLK=1
        asm.append("WAIT 1")
        asm.append(f"SHIFTIN R2, {swdio_pin}, LSB")
        asm.append("GWRI 0x00")               # SWCLK=0
        asm.append("WAIT 1")

    # Read 32 Data bits:
    # 8 bits into R0 (bits 7:0) using R3 as loop counter
    asm.append("LDI R0, 0")
    asm.append("LDI R3, 8")
    asm.append("r0_read_loop:")
    asm.append(f"GWRI 0x{clk_mask:02X}")
    asm.append("WAIT 1")
    asm.append(f"SHIFTIN R0, {swdio_pin}, LSB")
    asm.append("GWRI 0x00")
    asm.append("DECJNZ R3, r0_read_loop")

    # 8 bits into R1 (bits 15:8) using R3 as loop counter
    asm.append("LDI R1, 0")
    asm.append("LDI R3, 8")
    asm.append("r1_read_loop:")
    asm.append(f"GWRI 0x{clk_mask:02X}")
    asm.append("WAIT 1")
    asm.append(f"SHIFTIN R1, {swdio_pin}, LSB")
    asm.append("GWRI 0x00")
    asm.append("DECJNZ R3, r1_read_loop")

    # 8 bits into R2 (bits 23:16) using R3 as loop counter
    asm.append("LDI R2, 0")
    asm.append("LDI R3, 8")
    asm.append("r2_read_loop:")
    asm.append(f"GWRI 0x{clk_mask:02X}")
    asm.append("WAIT 1")
    asm.append(f"SHIFTIN R2, {swdio_pin}, LSB")
    asm.append("GWRI 0x00")
    asm.append("DECJNZ R3, r2_read_loop")

    # 8 bits into R3 (bits 31:24) unrolled so no register is clobbered
    asm.append("LDI R3, 0")
    for _ in range(8):
        asm.append(f"GWRI 0x{clk_mask:02X}")
        asm.append("WAIT 1")
        asm.append(f"SHIFTIN R3, {swdio_pin}, LSB")
        asm.append("GWRI 0x00")

    # 1 Data Parity bit clock cycle
    asm.append(f"GWRI 0x{clk_mask:02X}")
    asm.append("WAIT 1")
    asm.append("GWRI 0x00")
    asm.append("WAIT 1")

    # Turnaround cycle: Host switches SWDIO back to output
    asm.append(f"GDIRI 0x{both_mask:02X}")
    asm.append(f"GWRI 0x{clk_mask:02X}")
    asm.append("WAIT 1")
    asm.append("GWRI 0x00")
    asm.append("WAIT 1")

    # 4 Idle clock cycles (SWDIO=0)
    for _ in range(4):
        asm.append(f"GWRI 0x{clk_mask:02X}")
        asm.append("WAIT 1")
        asm.append("GWRI 0x00")
        asm.append("WAIT 1")

    asm.append("HALT")
    return "\n".join(asm) + "\n"


def build_swd_switch_sequence_asm(
    swclk_pin: int = 4,
    swdio_pin: int = 5
) -> str:
    """Generate firmware to execute ARM JTAG-to-SWD switching sequence (0x79E7):
    1. Line reset (>= 50 clocks high).
    2. 16-bit sequence 0x79E7 (LSB first: 0b0111_1001_1110_0111).
    3. Line reset (>= 50 clocks high).
    4. At least 4 idle cycles (SWDIO=0).
    5. HALT.
    """
    clk_mask = 1 << swclk_pin
    dio_mask = 1 << swdio_pin
    both_mask = clk_mask | dio_mask

    asm = []
    asm.append("; --- ARM JTAG-to-SWD Switching Firmware ---")
    asm.append(f"GDIRI 0x{both_mask:02X}")
    asm.append(f"GWRI 0x{dio_mask:02X}")

    # 1. Line reset: 52 clocks with SWDIO=1
    asm.append("LDI R0, 52")
    asm.append("reset1_loop:")
    asm.append(f"GWRI 0x{both_mask:02X}")
    asm.append("WAIT 1")
    asm.append(f"GWRI 0x{dio_mask:02X}")
    asm.append("WAIT 1")
    asm.append("DECJNZ R0, reset1_loop")

    # 2. 16-bit sequence 0x79E7 (LSB first: 0xE7 then 0x79)
    seq_16 = 0x79E7
    for bit_idx in range(16):
        bit = (seq_16 >> bit_idx) & 1
        low_val = dio_mask if bit else 0
        high_val = both_mask if bit else clk_mask
        asm.append(f"GWRI 0x{low_val:02X}")
        asm.append(f"GWRI 0x{high_val:02X}")
        asm.append("WAIT 1")
        asm.append(f"GWRI 0x{low_val:02X}")
        asm.append("WAIT 1")

    # 3. Second Line reset: 52 clocks with SWDIO=1
    asm.append("LDI R0, 52")
    asm.append("reset2_loop:")
    asm.append(f"GWRI 0x{both_mask:02X}")
    asm.append("WAIT 1")
    asm.append(f"GWRI 0x{dio_mask:02X}")
    asm.append("WAIT 1")
    asm.append("DECJNZ R0, reset2_loop")

    # 4. 4 idle cycles (SWDIO=0)
    asm.append("LDI R0, 4")
    asm.append("idle_loop:")
    asm.append(f"GWRI 0x{clk_mask:02X}")
    asm.append("WAIT 1")
    asm.append("GWRI 0x00")
    asm.append("WAIT 1")
    asm.append("DECJNZ R0, idle_loop")

    asm.append("HALT")
    return "\n".join(asm) + "\n"
