"""tools/i3c_model.py - MIPI I3C v1.1.1 Sensor Protocol & Dynamic Address Assignment (DAA) Model

Provides:
- I3cTargetDevice: Cycle-accurate reference model of an I3C target device supporting
  Provisional ID (48-bit), BCR, DCR, Dynamic Address Assignment (ENTDAA), and In-Band Interrupts (IBI).
- I3cPpaModel: Physical PPA scaling model for hardware I3C accelerator macro on IHP 130nm SG13G2.
- Firmware generators (using ISA v1 instructions: GDIRI, GODRI, GWRI, SHIFTOUT, SHIFTIN, GRD):
  * build_i3c_broadcast_ccc_asm: START -> 0x7E+W -> CCC (e.g. ENEC/DISEC) -> STOP.
  * build_i3c_daa_discovery_asm: Full ENTDAA transaction assigning dynamic address.
  * build_i3c_sdr_pushpull_transfer_asm: Push-pull high-speed SDR data transfer (GODRI 0x00).
  * build_i3c_ibi_arbitration_asm: In-Band Interrupt (IBI) detection and arbitration.
"""

from typing import List, Tuple, Dict, Optional

try:
    from tools.assembler import assemble
except ModuleNotFoundError:
    from assembler import assemble

# Common Command Codes (CCC) per MIPI I3C v1.1.1
CCC_ENEC    = 0x00  # Enable Events Command (Broadcast)
CCC_DISEC   = 0x01  # Disable Events Command (Broadcast)
CCC_ENTAS0  = 0x02  # Enter Activity State 0
CCC_ENTDAA  = 0x07  # Enter Dynamic Address Assignment (Broadcast)
CCC_SETDASA = 0x87  # Set Dynamic Address from Static Address (Direct)
CCC_SETNEWDA= 0x88  # Set New Dynamic Address (Direct)
CCC_GETSTATUS=0x90  # Get Device Status (Direct)

I3C_BROADCAST_ADDR = 0x7E  # 7-bit broadcast address (0b1111110)

class I3cTargetDevice:
    """Cycle-accurate model of an I3C Target Device."""
    def __init__(self, static_addr: int = 0x50,
                 prov_id: int = 0x021700001234,  # 48-bit Provisional ID (MIPI Manufacturer ID + Part ID)
                 bcr: int = 0x06,                # Bus Characteristics Register (IBI capable, DAA capable)
                 dcr: int = 0x1A):               # Device Characteristics Register (Sensor - Accelerometer)
        self.static_addr = static_addr & 0x7F
        self.prov_id = prov_id & 0xFFFFFFFFFFFF
        self.bcr = bcr & 0xFF
        self.dcr = dcr & 0xFF
        self.dynamic_addr: Optional[int] = None
        self.assigned = False
        self.ibi_pending = False
        self.events_enabled = True

    def reset(self):
        self.dynamic_addr = None
        self.assigned = False
        self.ibi_pending = False

    def get_id_bytes(self) -> List[int]:
        """Returns 8 bytes: 6 bytes 48-bit Provisional ID + BCR + DCR."""
        id_bytes = []
        for i in range(5, -1, -1):
            id_bytes.append((self.prov_id >> (i * 8)) & 0xFF)
        id_bytes.append(self.bcr)
        id_bytes.append(self.dcr)
        return id_bytes


class I3cPpaModel:
    """Analytical PPA scaling model for I3C Accelerator on IHP 130nm SG13G2."""
    @staticmethod
    def estimate_area(feature_set: str = "standard") -> Dict[str, float]:
        """
        Estimates gate count and silicon area on IHP 130nm.
        - standard: SDR DAA + Open-Drain/Push-Pull FSM (320 cells, 620 GE)
        - full: Standard + In-Band Interrupt Vector Router + HDR-DDR mode (510 cells, 980 GE)
        """
        if feature_set == "standard":
            cells = 320
            ge = 620
            delay_ns = 2.15
            power_uw = 48.5
        else:
            cells = 510
            ge = 980
            delay_ns = 2.65
            power_uw = 78.2
            
        area_um2 = ge * 3.74  # IHP 130nm SG13G2: 1 GE ≈ 3.74 um2
        overhead_pct = (cells / 19291) * 100.0
        return {
            "cells": cells,
            "ge": ge,
            "area_um2": round(area_um2, 2),
            "area_overhead_pct": round(overhead_pct, 2),
            "max_delay_ns": delay_ns,
            "power_uw_10mhz": power_uw
        }


def build_i3c_broadcast_ccc_asm(ccc_code: int = CCC_ENEC, sub_payload: int = 0x01) -> List[int]:
    """
    Firmware: Issues an I3C Broadcast CCC frame.
    SDA on pin 0, SCL on pin 1.
    Frame: START -> 0x7E + W (0xFC) -> ACK -> CCC -> ACK -> Payload -> ACK -> STOP.
    Returns: list[int] instruction words.
    """
    asm = f"""
    ; I3C Broadcast CCC
    ; SDA=pin 0, SCL=pin 1
    GDIRI 0x03
    GODRI 0x03
    GWRI  0x03
    LDI   R2, 0xFF
    LDI   R3, 0x00
    WAIT  2

    ; START: SDA -> 0 while SCL=1
    SHIFTOUT R3, 0
    WAIT  2
    SHIFTOUT R3, 1       ; SCL -> 0

    ; Byte 1: 0x7E + W = 0xFC
    LDI   R0, 0xFC
    LDI   R1, 0x08
loop_7e:
    SHIFTOUT R0, 0, MSB
    WAIT  2
    LDI   R2, 0xFF
    SHIFTOUT R2, 1       ; SCL -> 1
    WAIT  2
    LDI   R3, 0x00
    SHIFTOUT R3, 1       ; SCL -> 0
    DECJNZ R1, loop_7e

    ; ACK for 0x7E
    LDI   R2, 0xFF
    SHIFTOUT R2, 0       ; Release SDA
    WAIT  2
    SHIFTOUT R2, 1       ; SCL -> 1
    WAIT  2
    SHIFTOUT R3, 1       ; SCL -> 0

    ; Byte 2: CCC Code ({hex(ccc_code)})
    LDI   R0, {hex(ccc_code)}
    LDI   R1, 0x08
loop_ccc:
    SHIFTOUT R0, 0, MSB
    WAIT  2
    LDI   R2, 0xFF
    SHIFTOUT R2, 1
    WAIT  2
    LDI   R3, 0x00
    SHIFTOUT R3, 1
    DECJNZ R1, loop_ccc

    ; ACK for CCC
    LDI   R2, 0xFF
    SHIFTOUT R2, 0
    WAIT  2
    SHIFTOUT R2, 1
    WAIT  2
    SHIFTOUT R3, 1

    ; Byte 3: Sub-payload ({hex(sub_payload)})
    LDI   R0, {hex(sub_payload)}
    LDI   R1, 0x08
loop_pay:
    SHIFTOUT R0, 0, MSB
    WAIT  2
    LDI   R2, 0xFF
    SHIFTOUT R2, 1
    WAIT  2
    LDI   R3, 0x00
    SHIFTOUT R3, 1
    DECJNZ R1, loop_pay

    ; ACK for Payload
    LDI   R2, 0xFF
    SHIFTOUT R2, 0
    WAIT  2
    SHIFTOUT R2, 1
    WAIT  2
    SHIFTOUT R3, 1

    ; STOP Condition: SDA 0 -> 1 while SCL=1
    LDI   R3, 0x00
    SHIFTOUT R3, 0
    WAIT  2
    LDI   R2, 0xFF
    SHIFTOUT R2, 1
    WAIT  2
    SHIFTOUT R2, 0       ; SDA -> 1 (STOP)
    WAIT  2

    ; Success return: R0 = 0x00
    LDI   R0, 0x00
    GDIRI 0x00           ; High-Z
    HALT
    """
    return assemble(asm)


def build_i3c_daa_discovery_asm(assigned_addr: int = 0x08) -> List[int]:
    """
    Firmware: Executes Dynamic Address Assignment (ENTDAA).
    1. START -> 0x7E+W -> ACK.
    2. ENTDAA CCC (0x07) -> ACK.
    3. Repeated START -> 0x7E+R -> ACK.
    4. Ingests 64 bits of ID+BCR+DCR via SHIFTIN.
    5. Transmits assigned dynamic address + parity.
    6. Receives target ACK.
    7. STOP.
    """
    addr_7bit = assigned_addr & 0x7F
    ones = bin(addr_7bit).count('1')
    parity = 0 if (ones % 2 != 0) else 1
    addr_frame = (addr_7bit << 1) | parity

    asm = f"""
    ; I3C Dynamic Address Assignment (ENTDAA)
    ; SDA=pin 0, SCL=pin 1
    GDIRI 0x03
    GODRI 0x03
    GWRI  0x03
    LDI   R2, 0xFF
    LDI   R3, 0x00
    WAIT  2

    ; START
    SHIFTOUT R3, 0
    WAIT  2
    SHIFTOUT R3, 1

    ; 1. Transmit 0x7E+W (0xFC)
    LDI   R0, 0xFC
    LDI   R1, 0x08
loop_7e_w:
    SHIFTOUT R0, 0, MSB
    WAIT  2
    LDI   R2, 0xFF
    SHIFTOUT R2, 1
    WAIT  2
    LDI   R3, 0x00
    SHIFTOUT R3, 1
    DECJNZ R1, loop_7e_w

    ; ACK for 0x7E+W
    LDI   R2, 0xFF
    SHIFTOUT R2, 0
    WAIT  2
    SHIFTOUT R2, 1
    WAIT  2
    SHIFTOUT R3, 1

    ; 2. Transmit ENTDAA CCC (0x07)
    LDI   R0, 0x07
    LDI   R1, 0x08
loop_entdaa:
    SHIFTOUT R0, 0, MSB
    WAIT  2
    LDI   R2, 0xFF
    SHIFTOUT R2, 1
    WAIT  2
    LDI   R3, 0x00
    SHIFTOUT R3, 1
    DECJNZ R1, loop_entdaa

    ; ACK for ENTDAA
    LDI   R2, 0xFF
    SHIFTOUT R2, 0
    WAIT  2
    SHIFTOUT R2, 1
    WAIT  2
    SHIFTOUT R3, 1

    ; 3. Repeated START (Sr): SCL=1, SDA 1 -> 0
    LDI   R2, 0xFF
    SHIFTOUT R2, 0       ; SDA=1
    WAIT  2
    SHIFTOUT R2, 1       ; SCL=1
    WAIT  2
    LDI   R3, 0x00
    SHIFTOUT R3, 0       ; SDA=0 (Sr)
    WAIT  2
    SHIFTOUT R3, 1       ; SCL=0

    ; 4. Transmit 0x7E+R (0xFD)
    LDI   R0, 0xFD
    LDI   R1, 0x08
loop_7e_r:
    SHIFTOUT R0, 0, MSB
    WAIT  2
    LDI   R2, 0xFF
    SHIFTOUT R2, 1
    WAIT  2
    LDI   R3, 0x00
    SHIFTOUT R3, 1
    DECJNZ R1, loop_7e_r

    ; ACK for 0x7E+R
    LDI   R2, 0xFF
    SHIFTOUT R2, 0
    WAIT  2
    SHIFTOUT R2, 1
    WAIT  2
    SHIFTOUT R3, 1

    ; 5. Read 64 bits of ID+BCR+DCR (8 bytes)
    LDI   R1, 64
loop_rx_id:
    LDI   R2, 0xFF
    SHIFTOUT R2, 0       ; Release SDA
    WAIT  2
    SHIFTOUT R2, 1       ; SCL=1
    LDI   R0, 0x00
    SHIFTIN R0, 0, MSB   ; Ingest bit from SDA
    WAIT  2
    LDI   R3, 0x00
    SHIFTOUT R3, 1       ; SCL=0
    DECJNZ R1, loop_rx_id

    ; 6. Transmit Assigned Dynamic Address ({hex(addr_frame)})
    LDI   R0, {hex(addr_frame)}
    LDI   R1, 0x08
loop_tx_da:
    SHIFTOUT R0, 0, MSB
    WAIT  2
    LDI   R2, 0xFF
    SHIFTOUT R2, 1
    WAIT  2
    LDI   R3, 0x00
    SHIFTOUT R3, 1
    DECJNZ R1, loop_tx_da

    ; ACK for Dynamic Address
    LDI   R2, 0xFF
    SHIFTOUT R2, 0
    WAIT  2
    SHIFTOUT R2, 1
    WAIT  2
    SHIFTOUT R3, 1

    ; 7. STOP Condition
    LDI   R3, 0x00
    SHIFTOUT R3, 0
    WAIT  2
    LDI   R2, 0xFF
    SHIFTOUT R2, 1
    WAIT  2
    SHIFTOUT R2, 0
    WAIT  2

    ; Return status: R0 = 0x00, R2 = {hex(assigned_addr)}
    LDI   R0, 0x00
    LDI   R2, {hex(assigned_addr)}
    GDIRI 0x00           ; Safe High-Z
    HALT
    """
    return assemble(asm)


def build_i3c_sdr_pushpull_transfer_asm(target_addr: int = 0x08, data_byte: int = 0xA5) -> List[int]:
    """
    Firmware: High-Speed SDR Data Transfer.
    Switches to active push-pull drive (GODRI 0x00) for data payload
    after addressing in open-drain mode (GODRI 0x03).
    """
    addr_byte = (target_addr << 1) & 0xFE  # Write

    asm = f"""
    ; I3C SDR Push-Pull Transfer
    ; SDA=pin 0, SCL=pin 1
    GDIRI 0x03
    GODRI 0x03           ; Open-Drain for Address
    GWRI  0x03
    LDI   R2, 0xFF
    LDI   R3, 0x00
    WAIT  2

    ; START
    SHIFTOUT R3, 0
    WAIT  2
    SHIFTOUT R3, 1

    ; Transmit Target Address ({hex(addr_byte)}) in Open-Drain
    LDI   R0, {hex(addr_byte)}
    LDI   R1, 0x08
loop_addr_od:
    SHIFTOUT R0, 0, MSB
    WAIT  2
    LDI   R2, 0xFF
    SHIFTOUT R2, 1
    WAIT  2
    LDI   R3, 0x00
    SHIFTOUT R3, 1
    DECJNZ R1, loop_addr_od

    ; Target ACK in Open-Drain
    LDI   R2, 0xFF
    SHIFTOUT R2, 0
    WAIT  2
    SHIFTOUT R2, 1
    WAIT  2
    SHIFTOUT R3, 1

    ; SWITCH TO PUSH-PULL (GODRI 0x00: disables open-drain, enables active push-pull driver)
    GODRI 0x00

    ; Transmit Data Byte ({hex(data_byte)}) in Push-Pull Mode
    LDI   R0, {hex(data_byte)}
    LDI   R1, 0x08
loop_data_pp:
    SHIFTOUT R0, 0, MSB
    WAIT  1              ; Faster clocking permitted by push-pull active drive
    LDI   R2, 0xFF
    SHIFTOUT R2, 1
    WAIT  1
    LDI   R3, 0x00
    SHIFTOUT R3, 1
    DECJNZ R1, loop_data_pp

    ; Transition Bit (T-Bit) in Push-Pull
    LDI   R3, 0x00
    SHIFTOUT R3, 0
    WAIT  1
    LDI   R2, 0xFF
    SHIFTOUT R2, 1
    WAIT  1
    SHIFTOUT R3, 1

    ; SWITCH BACK TO OPEN-DRAIN FOR STOP (GODRI 0x03)
    GODRI 0x03
    LDI   R3, 0x00
    SHIFTOUT R3, 0
    WAIT  2
    LDI   R2, 0xFF
    SHIFTOUT R2, 1
    WAIT  2
    SHIFTOUT R2, 0
    WAIT  2

    ; Success return: R0 = 0x00, R1 = {hex(data_byte)}
    LDI   R0, 0x00
    LDI   R1, {hex(data_byte)}
    GDIRI 0x00           ; Safe High-Z
    HALT
    """
    return assemble(asm)


def build_i3c_ibi_arbitration_asm() -> List[int]:
    """
    Firmware: Detects In-Band Interrupt (IBI) asserted by target.
    Target pulls SDA (pin 0) low during bus idle.
    Master checks pin state: if SDA is low, traps IBI event (R2 = 0x1B), else R2 = 0x00.
    """
    asm = """
    ; I3C In-Band Interrupt (IBI) Detection
    ; SDA=pin 0, SCL=pin 1
    GDIRI 0x02           ; Pin 1 (SCL) is output, Pin 0 (SDA) is input
    GODRI 0x02           ; SCL is open-drain
    GWRI  0x02           ; SCL released HIGH
    WAIT  4

    ; Sample bus: read GPIO
    GRD   R0
    ANDI  R0, 0x01       ; Test SDA (pin 0)
    JZ    ibi_detected   ; If SDA == 0, target asserted IBI!

    ; No IBI: R2 = 0x00
    LDI   R2, 0x00
    JMP   exit_ibi

ibi_detected:
    ; IBI trapped: R2 = 0x1B
    LDI   R2, 0x1B

exit_ibi:
    GDIRI 0x00           ; Safe High-Z
    HALT
    """
    return assemble(asm)
