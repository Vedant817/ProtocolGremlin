; loop_demo.asm - ISA v0 smoke-test program.
;
; Exercises: LDI, GDIRI, GWR, WAIT, ADDI, DECJNZ/labels, ANDI/ORI/XORI, MOV,
; GRD, JZ/JNZ/JMP, SUBI, HALT. Used by test/test.py for differential testing
; against tools/isa_model.py. Not (yet) a real protocol - see
; orchestrator/queue.md for UART/SPI/I2C firmware, which comes later.

        LDI    R0, 0          ; R0 = value driven onto the GPIO bus
        LDI    R1, 5          ; R1 = loop counter
        GDIRI  0xFF           ; configure uio[7:0] as outputs

LOOP:
        GWR    R0             ; drive current counter value onto uio
        WAIT   3               ; hold it for a few cycles (deterministic timing)
        ADDI   R0, 1           ; counter++
        DECJNZ R1, LOOP        ; R1--; loop while R1 != 0

        ANDI   R0, 0x0F        ; exercise bitwise ops
        ORI    R0, 0x30
        XORI   R0, 0xFF
        MOV    R2, R0          ; exercise register-to-register move

        GDIRI  0x00            ; switch bus to input
        WAIT   1
        GRD    R3              ; sample the (externally driven) bus into R3

        GDIRI  0xFF            ; back to output for the shift-out exercise
        LDI    R1, 0xB6        ; arbitrary byte to shift out LSB-first on pin 0
        SHIFTOUT R1, 0
        WAIT   1
        SHIFTOUT R1, 0
        WAIT   1
        SHIFTOUT R1, 0

        GDIRI  0x00            ; switch back to input for the shift-in exercise
        WAIT   1
        SHIFTIN R3, 2          ; sample pin 2 into R3, bit by bit
        WAIT   1
        SHIFTIN R3, 2

        JZ     DONE            ; exercised for coverage even if not taken
        JNZ    CONT
DONE:
        JMP    HALT_LBL
CONT:
        SUBI   R2, 1
HALT_LBL:
        HALT
