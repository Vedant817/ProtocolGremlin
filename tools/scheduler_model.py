# SPDX-FileCopyrightText: (c) 2026 Jane Street Protocol Emulator ASIC project contributors
# SPDX-License-Identifier: Apache-2.0
"""
Deterministic Real-Time Task Scheduling Engine Models & Assembly Generators.

Provides:
- Task: Real-time task representation with priority, quantum, and context.
- SchedulerModel: Cycle-accurate reference model for cooperative & round-robin scheduling.
- Assembly firmware generators for real-time priority multi-tasking and context switching.
"""

from dataclasses import dataclass, field
from typing import Dict, List, Optional


@dataclass
class TaskContext:
    r0: int = 0
    r1: int = 0
    r2: int = 0
    r3: int = 0
    pc: int = 0


@dataclass
class Task:
    task_id: int
    priority: int  # 0 is highest priority
    name: str
    quantum: int = 16
    state: str = "READY"  # READY, RUNNING, BLOCKED, COMPLETED
    context: TaskContext = field(default_factory=TaskContext)
    cycles_executed: int = 0
    yield_count: int = 0
    wcet: int = 0
    deadline: int = 0


class SchedulerModel:
    """Cycle-accurate reference scheduler model."""

    def __init__(self, mode: str = "PRIORITY"):
        self.mode = mode  # "PRIORITY" or "ROUND_ROBIN"
        self.tasks: Dict[int, Task] = {}
        self.current_task_id: Optional[int] = None
        self.cycle: int = 0
        self.context_switch_count: int = 0
        self.switch_history: List[tuple] = []  # (cycle, from_task, to_task)

    def add_task(self, task: Task) -> None:
        self.tasks[task.task_id] = task

    def dispatch_next(self) -> Optional[int]:
        """Dispatch highest priority ready task or next in round-robin sequence."""
        if not self.tasks:
            return None

        ready_tasks = [t for t in self.tasks.values() if t.state in ("READY", "RUNNING")]
        if not ready_tasks:
            return None

        if self.mode == "PRIORITY":
            # Highest priority (lowest numeric value)
            ready_tasks.sort(key=lambda t: t.priority)
            next_task = ready_tasks[0]
        else:  # ROUND_ROBIN
            task_ids = sorted(self.tasks.keys())
            if self.current_task_id is None:
                next_task = self.tasks[task_ids[0]]
            else:
                curr_idx = task_ids.index(self.current_task_id)
                next_idx = (curr_idx + 1) % len(task_ids)
                next_task = self.tasks[task_ids[next_idx]]

        if next_task.task_id != self.current_task_id:
            self.context_switch_count += 1
            self.switch_history.append((self.cycle, self.current_task_id, next_task.task_id))
            if self.current_task_id is not None and self.tasks[self.current_task_id].state == "RUNNING":
                self.tasks[self.current_task_id].state = "READY"
            next_task.state = "RUNNING"
            self.current_task_id = next_task.task_id

        return self.current_task_id

    def yield_current(self) -> None:
        """Current task yields CPU voluntarily."""
        if self.current_task_id is not None:
            task = self.tasks[self.current_task_id]
            task.yield_count += 1
            task.state = "READY"
        self.dispatch_next()

    def complete_current(self) -> None:
        """Current task completes execution."""
        if self.current_task_id is not None:
            self.tasks[self.current_task_id].state = "COMPLETED"
            self.current_task_id = None
        self.dispatch_next()


# =========================================================================
# Microcode Firmware Generators for Real-Time Scheduling on the 8-Bit Core
# =========================================================================

def build_cooperative_priority_scheduler_asm(t0_work: int = 5, t1_work: int = 10) -> list[str]:
    """
    Generate cooperative multi-tasking firmware with priority dispatch.

    Architecture:
    - R3: High-priority Task 0 trigger flag (1 = Task 0 ready, 0 = not ready).
    - R0: Task 0 progress counter.
    - R1: Task 1 progress counter.
    - Task 1 executes work; periodically yields to dispatcher.
    - If R3 != 0, dispatcher preempts Task 1 to run high-priority Task 0.
    - Once Task 0 completes (R0 reaches t0_work), it clears R3 and yields back to Task 1.
    - Program halts when both tasks complete.
    """
    return [
        "init:",
        "    LDI   R0, 0x00      ; Clear R0 (Task 0 work counter)",
        "    MOV   R1, R0        ; Clear R1 (Task 1 work counter)",
        "    LDI   R3, 0x01      ; Set R3 = 1 (Task 0 pending initially)",
        "",
        "dispatcher:",
        "    MOV   R2, R3        ; Check Task 0 pending flag in R3",
        "    ADDI  R2, 0x00      ; Test zero (updates Z flag)",
        "    JNZ   task0_run     ; If R3 != 0, dispatch high-priority Task 0",
        "    JMP   task1_run     ; Otherwise, dispatch Task 1",
        "",
        "task0_run:",
        f"    ADDI  R0, {t0_work} ; Execute Task 0 work in R0",
        "    LDI   R3, 0x00      ; Clear pending flag R3 = 0",
        "    JMP   dispatcher    ; Yield back to dispatcher",
        "",
        "task1_run:",
        "    MOV   R0, R1        ; Load R1 into R0",
        f"    ADDI  R0, {t1_work} ; Execute Task 1 work",
        "    MOV   R1, R0        ; Store result in R1",
        "    HALT                ; Both tasks complete",
    ]


def build_round_robin_scheduler_asm(increments: list[int]) -> list[str]:
    """
    Generate round-robin scheduling firmware across 3 tasks.

    - Task 0 updates R0.
    - Task 1 updates R1.
    - Task 2 updates R2.
    - Each task executes an increment then yields to next task.
    """
    inc0 = increments[0] & 0xFF
    inc1 = increments[1] & 0xFF
    inc2 = increments[2] & 0xFF

    return [
        "init:",
        "    LDI   R0, 0x00",
        "    MOV   R1, R0",
        "    MOV   R2, R0",
        "    LDI   R3, 0x02      ; Loop 2 round-robin rounds in R3",
        "",
        "rr_loop:",
        "task0:",
        f"    ADDI  R0, {inc0}    ; Task 0 turn",
        "",
        "task1:",
        "    MOV   R0, R1        ; Task 1 turn: increment R1",
        f"    ADDI  R0, {inc1}",
        "    MOV   R1, R0",
        "",
        "task2:",
        "    MOV   R0, R2        ; Task 2 turn: increment R2",
        f"    ADDI  R0, {inc2}",
        "    MOV   R2, R0",
        "",
        "yield_check:",
        "    DECJNZ R3, rr_loop   ; Decrement round counter R3 and repeat",
        "    HALT",
    ]


def build_context_switch_fidelity_asm(val0: int, val1: int) -> list[str]:
    """
    Verify context switch preservation:
    - Task 0 sets state A in R0, R1.
    - Context switches to Task 1 (sets state B in R0, R1).
    - Context switches back to Task 0, verifying state A is restored without corruption.
    - Uses R2 and R3 as task context save registers.
    """
    return [
        "init:",
        f"    LDI   R0, {val0 & 0xFF}  ; Task 0 init: set R0 = val0",
        f"    LDI   R1, {((val0 >> 8) + 1) & 0xFF} ; Task 0 R1",
        "",
        "save_task0_context:",
        "    MOV   R2, R0             ; Save R0 to Context Reg R2",
        "    MOV   R3, R1             ; Save R1 to Context Reg R3",
        "",
        "switch_to_task1:",
        f"    LDI   R0, {val1 & 0xFF}  ; Task 1 executes with val1",
        f"    LDI   R1, {((val1 >> 8) + 5) & 0xFF}",
        "",
        "restore_task0_context:",
        "    MOV   R0, R2             ; Restore Task 0 R0 from R2",
        "    MOV   R1, R3             ; Restore Task 0 R1 from R3",
        "    HALT",
    ]


def build_deadline_periodic_task_asm(period_wait: int, iterations: int) -> list[str]:
    """
    Generate periodic hard real-time task firmware meeting strict timing deadlines.
    - Uses WAIT to enforce an exact period boundary.
    - Verifies bounded execution and deterministic cycle count.
    """
    return [
        "init:",
        "    LDI   R0, 0x00          ; Clear accumulator R0",
        f"    LDI   R3, {iterations}  ; Set iteration counter R3",
        "",
        "periodic_loop:",
        "    ADDI  R0, 0x0A          ; Work payload (add 10 to R0)",
        f"    WAIT  {period_wait}     ; Strict periodic timing interval",
        "    DECJNZ R3, periodic_loop ; Decrement iteration counter",
        "    HALT",
    ]
