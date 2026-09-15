# tools/can_model.py - Cycle-accurate CAN 2.0A Controller physical layer model & firmware generator
#
# Implements ISO 11898-1 CAN physical and data link layer operations:
# - Open-drain wired-AND bus topology: Dominant (0) overrides Recessive (1).
# - Standard CAN 2.0A frame layout:
#     SOF (1 bit dominant)
#     11-bit Identifier (MSB-first)
#     RTR (0), IDE (0), r0 (0), 4-bit DLC
#     Data payload (8 bits per byte, MSB-first)
#     CRC-15 (poly 0x4599)
#     CRC Delimiter (1 bit recessive, unstuffed)
#     ACK Slot (1 bit, transmitter sends recessive, receiver pulls dominant)
#     ACK Delimiter (1 bit recessive)
#     End of Frame (EOF: 7 recessive bits)
# - Bit stuffing rule: after 5 consecutive identical bits in pre-ACK field,
#   1 complementary stuff bit is inserted/destuffed.
# - Non-destructive bitwise arbitration: node transmitting Recessive but detecting Dominant
#   loses arbitration and immediately ceases transmission.
#
# Part of the Jane Street Protocol Emulator ASIC verification suite.

from typing import List, Tuple, Optional


def compute_can_crc15(raw_bits: List[int]) -> int:
    """Standard CAN 2.0A 15-bit CRC calculation (poly 0x4599, init 0x0000)."""
    crc = 0
    poly = 0x4599
    for bit in raw_bits:
        crc_next = bit ^ ((crc >> 14) & 1)
        crc = (crc << 1) & 0x7FFF
        if crc_next:
            crc ^= poly
    return crc


def insert_can_bit_stuffing(raw_bits: List[int]) -> List[int]:
    """Apply standard CAN bit stuffing rule to a sequence of raw bits.
    Whenever 5 consecutive identical bits occur, a complementary stuff bit is inserted.
    """
    stuffed = []
    consecutive = 0
    current_val = -1

    for bit in raw_bits:
        if bit == current_val:
            consecutive += 1
            stuffed.append(bit)
            if consecutive == 5:
                stuff_bit = 1 - current_val
                stuffed.append(stuff_bit)
                current_val = stuff_bit
                consecutive = 1
        else:
            current_val = bit
            consecutive = 1
            stuffed.append(bit)

    return stuffed


def remove_can_bit_stuffing(stuffed_bits: List[int]) -> Tuple[List[int], bool]:
    """Destuff a bit stream per CAN ISO 11898.
    Returns (destuffed_bits, is_valid). If 6 consecutive identical bits appear, is_valid is False (Stuff Error).
    """
    destuffed = []
    consecutive = 0
    current_val = -1
    i = 0
    n = len(stuffed_bits)

    while i < n:
        bit = stuffed_bits[i]
        if bit == current_val:
            consecutive += 1
            destuffed.append(bit)
            if consecutive == 5:
                i += 1
                if i >= n:
                    break
                stuff_bit = stuffed_bits[i]
                if stuff_bit == current_val:
                    return destuffed, False
                current_val = stuff_bit
                consecutive = 1
        else:
            current_val = bit
            consecutive = 1
            destuffed.append(bit)
        i += 1

    return destuffed, True


def build_can_frame_bits(
    can_id: int,
    data_bytes: List[int],
    rtr: int = 0
) -> Tuple[List[int], int, List[int]]:
    """Generate the bit sequence for a CAN 2.0A standard 11-bit identifier frame.
    Returns (stuffed_pre_ack_bits, crc15, post_ack_bits).
    Pre-ACK bits undergo bit stuffing (SOF through CRC15).
    """
    raw_bits = []

    # SOF: 1 dominant bit (0)
    raw_bits.append(0)

    # 11-bit Identifier (MSB first)
    for b in range(10, -1, -1):
        raw_bits.append((can_id >> b) & 1)

    # RTR: 0 for data frame, 1 for remote frame
    raw_bits.append(rtr & 1)

    # IDE: 0 for standard 11-bit format
    raw_bits.append(0)

    # r0: reserved bit (dominant 0)
    raw_bits.append(0)

    # DLC: 4 bits (data length code)
    dlc = len(data_bytes) & 0xF
    for b in range(3, -1, -1):
        raw_bits.append((dlc >> b) & 1)

    # Data Bytes (MSB first per byte)
    for byte_val in data_bytes:
        for b in range(7, -1, -1):
            raw_bits.append((byte_val >> b) & 1)

    # CRC-15 calculation
    crc15 = compute_can_crc15(raw_bits)
    for b in range(14, -1, -1):
        raw_bits.append((crc15 >> b) & 1)

    # Apply bit stuffing across SOF through CRC15
    stuffed_bits = insert_can_bit_stuffing(raw_bits)

    # Post-ACK bits: CRC Delimiter (1) + ACK Delimiter (1) + End of Frame (7x 1s)
    post_ack = [1] * 9

    return stuffed_bits, crc15, post_ack


class CanReceiverModel:
    """Independent cycle-accurate Python CAN bus receiver model."""

    def __init__(self, bit_period: int = 8, pin: int = 4):
        self.bit_period = bit_period
        self.pin = pin
        self.decoded_id: Optional[int] = None
        self.decoded_data: List[int] = []
        self.stuff_error = False

    def decode_stream(self, samples: List[int]) -> Tuple[Optional[int], List[int], bool]:
        """Decode sampled bit stream."""
        destuffed, valid = remove_can_bit_stuffing(samples)
        if not valid or len(destuffed) < 19:
            return None, [], False

        # Parse header
        sof = destuffed[0]
        if sof != 0:
            return None, [], False

        can_id = 0
        for i in range(1, 12):
            can_id = (can_id << 1) | destuffed[i]

        dlc = 0
        for i in range(15, 19):
            dlc = (dlc << 1) | destuffed[i]

        data = []
        offset = 19
        for _ in range(dlc):
            if offset + 8 > len(destuffed):
                break
            b_val = 0
            for k in range(8):
                b_val = (b_val << 1) | destuffed[offset + k]
            data.append(b_val)
            offset += 8

        return can_id, data, True


def build_can_tx_asm(
    can_id: int,
    data_byte: int,
    bit_period: int = 8,
    pin: int = 4,
    check_arbitration_on_id: bool = True
) -> str:
    """Generate assembly firmware to transmit an 11-bit CAN frame with bit stuffing and arbitration check.

    Uses hardware open-drain (GODRI) on pin:
    - Dominant (0) driven via GWRI 0x00
    - Recessive (1) driven via GWRI (1 << pin)
    - Cycle-exact timing: every bit cell is EXACTLY bit_period clock cycles.
    """
    pin_mask = 1 << pin
    normal_wait = max(0, bit_period - 2)

    stuffed_bits, crc15, post_ack = build_can_frame_bits(can_id, [data_byte])

    asm = []
    asm.append("; --- CAN 2.0A Physical Layer TX Firmware ---")
    asm.append(f"GDIRI 0x{pin_mask:02X}")   # Pin output enabled
    asm.append(f"GODRI 0x{pin_mask:02X}")   # Open-drain enabled
    asm.append(f"GWRI 0x{pin_mask:02X}")    # Idle recessive (high)
    asm.append("LDI R2, 0x00")              # Default status: SUCCESS (0x00)
    asm.append("WAIT 4")                    # Bus idle settling

    arb_check_limit = 14 if check_arbitration_on_id else 0

    # Transmit stuffed bits (SOF, ID, RTR, IDE, r0, DLC, Data, CRC15)
    for idx, bit in enumerate(stuffed_bits):
        if bit == 0:
            # Dominant bit: 1 (GWRI) + (bit_period - 2) (WAIT) = bit_period cycles
            asm.append("GWRI 0x00")
            if normal_wait > 0:
                asm.append(f"WAIT {normal_wait}")
        else:
            # Recessive bit
            if idx <= arb_check_limit:
                # Cycle-exact arbitration check:
                # 1 (GWRI) + 4 (WAIT 3) + 1 (GRD) + 1 (ANDI) + 1 (JZ) = 8 cycles!
                asm.append(f"GWRI 0x{pin_mask:02X}")
                sample_wait = max(0, (bit_period // 2) - 1)
                if sample_wait > 0:
                    asm.append(f"WAIT {sample_wait}")
                asm.append("GRD R3")
                asm.append(f"ANDI R3, 0x{pin_mask:02X}")
                asm.append(f"JZ arb_lost_{idx}")
            else:
                asm.append(f"GWRI 0x{pin_mask:02X}")
                if normal_wait > 0:
                    asm.append(f"WAIT {normal_wait}")

    # CRC Delimiter (1 recessive bit)
    asm.append(f"GWRI 0x{pin_mask:02X}")
    if normal_wait > 0:
        asm.append(f"WAIT {normal_wait}")

    # ACK Slot: transmitter drives recessive, checks if receiver pulls dominant (0)
    # 1 (GWRI) + 4 (WAIT 3) + 1 (GRD) + 1 (ANDI) + 1 (JNZ) = 8 cycles!
    asm.append(f"GWRI 0x{pin_mask:02X}")
    ack_wait = max(0, (bit_period // 2) - 1)
    if ack_wait > 0:
        asm.append(f"WAIT {ack_wait}")
    asm.append("GRD R3")
    asm.append(f"ANDI R3, 0x{pin_mask:02X}")
    asm.append("JNZ ack_error")

    # ACK Delimiter (1 recessive bit) + End of Frame (7 recessive bits) = 8 bits
    for _ in range(8):
        asm.append(f"GWRI 0x{pin_mask:02X}")
        if normal_wait > 0:
            asm.append(f"WAIT {normal_wait}")

    # Success halt
    asm.append("LDI R2, 0x00")
    asm.append("WAIT 2")
    asm.append("HALT")

    # Error handlers
    asm.append("ack_error:")
    asm.append("LDI R2, 0xAE")  # 0xAE = ACK Error
    asm.append("HALT")

    if check_arbitration_on_id:
        for idx in range(arb_check_limit + 1):
            if idx < len(stuffed_bits) and stuffed_bits[idx] == 1:
                asm.append(f"arb_lost_{idx}:")
                asm.append(f"GWRI 0x{pin_mask:02X}")  # Release bus immediately
                asm.append("LDI R2, 0xAA")            # 0xAA = Arbitration Lost
                asm.append("HALT")

    return "\n".join(asm) + "\n"


def build_can_rx_asm(
    can_id: int,
    data_byte: int,
    bit_period: int = 8,
    pin: int = 4
) -> str:
    """Generate assembly firmware for receiving an 11-bit CAN frame and asserting ACK.

    Synchronizes to SOF falling edge via WAITEDGE.
    Samples the 8 data bits into R0 via SHIFTIN MSB.
    At ACK slot, asserts Dominant (GWRI 0x00) for 1 bit period.
    Returns bus to recessive and halts with R0 = data_byte, R2 = 0x00.
    """
    pin_mask = 1 << pin
    stride_wait = max(0, bit_period - 2)

    stuffed_bits, crc15, post_ack = build_can_frame_bits(can_id, [data_byte])

    # Find the indices of the 8 data bits within stuffed_bits
    # In raw_bits: SOF (0), ID (1..11), RTR (12), IDE (13), r0 (14), DLC (15..18), Data (19..26), CRC (27..41)
    # We trace which stuffed bit corresponds to raw_bits[19..26]
    data_stuffed_indices = []
    consecutive = 0
    current_val = -1
    raw_idx = 0

    # Build raw_bits
    raw_bits = [0]
    for b in range(10, -1, -1):
        raw_bits.append((can_id >> b) & 1)
    raw_bits.extend([0, 0, 0, 0, 0, 0, 1])  # RTR, IDE, r0, DLC=1
    for b in range(7, -1, -1):
        raw_bits.append((data_byte >> b) & 1)
    for b in range(14, -1, -1):
        raw_bits.append((crc15 >> b) & 1)

    stuffed_map = []  # maps stuffed_idx -> raw_idx (or -1 if stuff bit)
    for bit in raw_bits:
        if bit == current_val:
            consecutive += 1
            stuffed_map.append(raw_idx)
            if consecutive == 5:
                stuff_bit = 1 - current_val
                stuffed_map.append(-1)  # stuff bit
                current_val = stuff_bit
                consecutive = 1
        else:
            current_val = bit
            consecutive = 1
            stuffed_map.append(raw_idx)
        raw_idx += 1

    # Find start and end of data bits
    first_data_stuffed_idx = stuffed_map.index(19)
    last_data_stuffed_idx = stuffed_map.index(26)
    total_stuffed = len(stuffed_map)

    asm = []
    asm.append("; --- CAN 2.0A Physical Layer RX Firmware ---")
    asm.append(f"GDIRI 0x{pin_mask:02X}")   # Pin output enabled
    asm.append(f"GODRI 0x{pin_mask:02X}")   # Open-drain enabled
    asm.append(f"GWRI 0x{pin_mask:02X}")    # Recessive (listening)
    asm.append("LDI R0, 0x00")              # Data accumulator
    asm.append("LDI R2, 0x00")              # Status: 0x00 SUCCESS

    # Wait for SOF falling edge
    asm.append(f"WAITEDGE R3, 0x{pin:02X}")

    # Delay from SOF falling edge (t=2) to first data bit sample point:
    # Each bit is bit_period cycles.
    # From SOF (bit 0) to first_data_stuffed_idx is first_data_stuffed_idx * bit_period cycles.
    # Initial wait delay:
    cycles_to_first_data = (first_data_stuffed_idx * bit_period) + (bit_period // 2) - 2
    # Break into WAIT instructions (max 255 per WAIT)
    rem = cycles_to_first_data
    while rem > 255:
        asm.append("WAIT 254")
        rem -= 255
    if rem > 1:
        asm.append(f"WAIT {rem - 1}")

    # Sample the data bits
    curr_stuffed_idx = first_data_stuffed_idx
    for b in range(19, 27):
        target_stuffed_idx = stuffed_map.index(b)
        stride = (target_stuffed_idx - curr_stuffed_idx) * bit_period
        if stride > 0:
            stride_sub = stride - 2
            if stride_sub > 0:
                asm.append(f"WAIT {stride_sub}")
        asm.append(f"SHIFTIN R0, {pin}, MSB")
        curr_stuffed_idx = target_stuffed_idx

    # Delay to ACK slot:
    # Total stuffed bits through CRC15 is total_stuffed.
    # CRC delimiter is 1 bit.
    # ACK slot is at (total_stuffed + 1) bits from start.
    bits_to_ack = (total_stuffed + 1) - curr_stuffed_idx
    cycles_to_ack = (bits_to_ack * bit_period) - 2
    rem = cycles_to_ack
    while rem > 255:
        asm.append("WAIT 254")
        rem -= 255
    if rem > 1:
        asm.append(f"WAIT {rem - 1}")

    # Assert Dominant (GWRI 0x00) for ACK slot (1 bit period)
    asm.append("GWRI 0x00")
    if stride_wait > 0:
        asm.append(f"WAIT {stride_wait}")

    # Release bus back to recessive
    asm.append(f"GWRI 0x{pin_mask:02X}")
    asm.append("WAIT 4")
    asm.append("HALT")

    return "\n".join(asm) + "\n"
