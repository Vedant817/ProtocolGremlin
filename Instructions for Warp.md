Yes. Since you want **everything to run locally on your machine**, you can skip Warp Cloud entirely. Warp officially supports this with `oz agent run`: it operates directly on the current directory and local tools/files. ([Warp][1])

I would set it up like this.

### 1. Make your repo look roughly like this

```text
jane-street-asic/
│
├── AGENTS.md
├── PROJECT_MASTER_PLAN.md
├── README.md
│
├── src/
├── test/
├── formal/
├── firmware/
├── tools/
├── docs/
│
└── orchestrator/
    ├── WARP_MASTER_PROMPT.md
    ├── WARP_CYCLE_PROMPT.md
    ├── state.json
    ├── metrics.json
    ├── experiments.jsonl
    ├── queue.md
    ├── blocked.md
    ├── decisions.md
    └── best_checkpoint.json
```

A couple of distinctions matter here:

* `AGENTS.md` = persistent project instructions that Warp automatically reads.
* `PROJECT_MASTER_PLAN.md` = the large master document I generated.
* `WARP_MASTER_PROMPT.md` = the long autonomous-project prompt I gave you.
* A **Warp Agent Profile** is different from a `.md` file. `--profile` expects a Warp profile **ID**, not the path to a prompt file.

Warp recognizes `AGENTS.md` or `WARP.md` as project-level rules automatically. ([Warp][2])

### 2. Open the folder itself in Warp

On Windows PowerShell:

```powershell
cd C:\path\to\jane-street-asic
```

Make sure this is a Git repository:

```powershell
git status
```

If it isn't:

```powershell
git init
git add .
git commit -m "Initial Jane Street ASIC project setup"
```

Then, **inside Warp**, type:

```text
/init
```

This makes Warp index the project and recognize the project rules. ([Warp][3])

### 3. Verify the local Warp/Oz CLI

Run:

```powershell
oz --version
```

Then:

```powershell
oz whoami
```

If authentication is required:

```powershell
oz login
```

Warp says the bundled CLI normally reuses the account with which you're already signed into Warp. ([Warp][1])

Then check the models you have:

```powershell
oz model list
```

And your profiles:

```powershell
oz agent profile list
```

You'll get something conceptually like:

```text
Name             ID
---------------- ----------------------
Default          abcdef...
Coding           xyz123...
Full Terminal    qwerty...
```

Copy the ID of the profile you created/configured.

### 4. Do one manual local run first

Assuming your long prompt is here:

```text
orchestrator\WARP_MASTER_PROMPT.md
```

PowerShell:

```powershell
$prompt = Get-Content ".\orchestrator\WARP_MASTER_PROMPT.md" -Raw

oz agent run `
  --profile YOUR_PROFILE_ID `
  --cwd "$(Get-Location)" `
  --name "jane-street-asic-bootstrap" `
  --prompt $prompt
```

Replace:

```text
YOUR_PROFILE_ID
```

with the actual ID from:

```powershell
oz agent profile list
```

This is **local execution**. The agent works against your current machine and current directory. Warp explicitly recommends `oz agent run` for local development, direct file access, and immediate inspection. ([docs.warp.dev][1])

You do **not** need:

```text
oz agent run-cloud
```

and you do **not** need a Warp environment.

### 5. For the first run, don't start the endless loop yet

Let that initial agent complete.

It should ideally create/populate:

```text
orchestrator/state.json
orchestrator/metrics.json
orchestrator/experiments.jsonl
orchestrator/queue.md
orchestrator/decisions.md
orchestrator/best_checkpoint.json
```

And ideally begin:

```text
Tiny Tapeout template
        ↓
baseline build
        ↓
basic programmable core
        ↓
UART
        ↓
tests
```

Check what it changed:

```powershell
git status
git diff
```

And look at recent commits if you allowed the agent to commit:

```powershell
git log --oneline -10
```

---

## 6. Then create the short repeat-cycle prompt

Create:

```text
orchestrator/WARP_CYCLE_PROMPT.md
```

with this:

```text
Read these files before doing anything:

- AGENTS.md
- PROJECT_MASTER_PLAN.md
- orchestrator/state.json
- orchestrator/metrics.json
- orchestrator/queue.md
- orchestrator/experiments.jsonl
- orchestrator/decisions.md
- orchestrator/best_checkpoint.json
- docs/architecture.md if present
- docs/isa.md if present
- docs/verification.md if present
- docs/ppa.md if present

You are the next autonomous engineering cycle of the Jane Street
Protocol Emulator ASIC project.

Do NOT restart the project.

Continue from the repository's current verified state.

First:

1. inspect git status and recent commits;
2. run the current fast regression/baseline;
3. inspect the current metrics and work queue;
4. identify the highest-value bounded problem;
5. define a measurable experiment;
6. implement it;
7. verify it;
8. run synthesis/PPA when relevant;
9. adversarially review the result;
10. accept or revert based on evidence.

If the explicit work queue is empty, enter RESEARCH_AND_PROOF mode.

In RESEARCH_AND_PROOF mode, select the highest-value activity from:

- find untested behavior;
- strengthen differential verification;
- add randomized tests;
- add formal properties;
- mutation-test the verification system;
- attack reset behavior;
- attack protocol timing boundaries;
- test UART/SPI/I2C corner cases;
- run synthesis;
- reduce area;
- improve timing;
- reduce protocol firmware size;
- compare ISA encodings;
- compare architectural alternatives;
- investigate multi-lane execution;
- investigate JTAG/SWD/PS2;
- investigate protocol bridging;
- investigate sniff/replay;
- investigate deterministic fault injection;
- improve reproducibility;
- improve documentation;
- challenge the novelty of the current architecture.

Do not perform busywork.

Every task requires:

- hypothesis;
- measurable expected result;
- tests;
- final ACCEPT / REJECT / BLOCKED decision.

Before ending this run:

- update state.json;
- update metrics.json;
- append experiments.jsonl;
- update queue.md;
- update best_checkpoint.json if applicable;
- document blocked tasks;
- leave the repository recoverable;
- commit accepted changes.

A completed agent invocation does NOT mean the project is finished.

Always leave a clearly prioritized next objective for the following local run.
```

This short prompt is what you'll repeatedly run. The large master prompt doesn't need to be re-sent every time because the durable context is in the repo.

---

# 7. Now make it keep running locally

Create this file in the **repo root**:

```text
run-warp-local.ps1
```

Put this inside:

```powershell
$ErrorActionPreference = "Continue"

$ProjectRoot = $PSScriptRoot

Set-Location $ProjectRoot

# ---------------------------------------------
# CONFIGURATION
# ---------------------------------------------

$ProfileId = "PUT_YOUR_WARP_PROFILE_ID_HERE"

$MasterPromptFile = Join-Path $ProjectRoot "orchestrator\WARP_MASTER_PROMPT.md"
$CyclePromptFile  = Join-Path $ProjectRoot "orchestrator\WARP_CYCLE_PROMPT.md"

$LogDir = Join-Path $ProjectRoot "orchestrator\logs"

$SleepBetweenRunsSeconds = 30
$FailureBackoffSeconds = 120

# ---------------------------------------------
# INITIALIZATION
# ---------------------------------------------

New-Item -ItemType Directory -Force -Path $LogDir | Out-Null

Write-Host ""
Write-Host "=============================================="
Write-Host " Jane Street ASIC - Local Warp Orchestrator"
Write-Host "=============================================="
Write-Host ""
Write-Host "Project: $ProjectRoot"
Write-Host "Profile: $ProfileId"
Write-Host ""

# ---------------------------------------------
# MAIN LOOP
# ---------------------------------------------

while ($true) {

    # Explicit kill switch
    if (Test-Path (Join-Path $ProjectRoot "HALT")) {
        Write-Host ""
        Write-Host "HALT file detected."
        Write-Host "Stopping autonomous engineering loop."
        break
    }

    $Timestamp = Get-Date -Format "yyyy-MM-dd_HH-mm-ss"

    $StateFile = Join-Path $ProjectRoot "orchestrator\state.json"

    # First run gets full master prompt.
    # Subsequent runs get the smaller continuation prompt.
    if (-not (Test-Path $StateFile)) {

        Write-Host ""
        Write-Host "Starting INITIAL MASTER RUN..."

        $Prompt = Get-Content $MasterPromptFile -Raw

        $RunName = "asic-bootstrap-$Timestamp"

    }
    else {

        Write-Host ""
        Write-Host "Starting CONTINUOUS IMPROVEMENT CYCLE..."

        $Prompt = Get-Content $CyclePromptFile -Raw

        $RunName = "asic-cycle-$Timestamp"
    }

    $LogFile = Join-Path $LogDir "$RunName.log"

    Write-Host "Run: $RunName"
    Write-Host "Log: $LogFile"
    Write-Host ""

    try {

        oz agent run `
            --profile $ProfileId `
            --cwd $ProjectRoot `
            --name $RunName `
            --prompt $Prompt `
            2>&1 | Tee-Object -FilePath $LogFile

        $ExitCode = $LASTEXITCODE

    }
    catch {

        Write-Host "Warp invocation threw an exception:"
        Write-Host $_

        $ExitCode = 1
    }

    if ($ExitCode -eq 0) {

        Write-Host ""
        Write-Host "Cycle completed successfully."

        Write-Host "Sleeping $SleepBetweenRunsSeconds seconds before next cycle..."

        Start-Sleep -Seconds $SleepBetweenRunsSeconds

    }
    else {

        Write-Host ""
        Write-Host "Cycle exited with code: $ExitCode"

        Write-Host "Backing off for $FailureBackoffSeconds seconds..."

        Start-Sleep -Seconds $FailureBackoffSeconds
    }
}

Write-Host ""
Write-Host "Local Warp orchestrator stopped."
```

Then edit:

```powershell
$ProfileId = "PUT_YOUR_WARP_PROFILE_ID_HERE"
```

and paste your actual profile ID.

---

# 8. Run it

From the project root:

```powershell
Set-ExecutionPolicy -Scope Process Bypass
```

Then:

```powershell
.\run-warp-local.ps1
```

Now the behavior becomes:

```text
Warp run #1
   ↓
read master plan
   ↓
work
   ↓
test
   ↓
persist state
   ↓
exit
   ↓
30 sec
   ↓
Warp run #2
   ↓
read previous state
   ↓
next task
   ↓
work
   ↓
verify
   ↓
persist
   ↓
exit
   ↓
30 sec
   ↓
Warp run #3
   ↓
...
```

And it keeps going until you stop it.

---

# 9. How to stop it safely

From another terminal:

```powershell
New-Item HALT
```

The current agent can finish its cycle, and before the next iteration the wrapper detects:

```text
HALT
```

and exits.

Restart later:

```powershell
Remove-Item HALT
.\run-warp-local.ps1
```

Because state is persisted in Git/files, it continues rather than starting over.

That's exactly what you want.

---

# 10. Make sure Windows doesn't sleep

Because this is **local**, your machine must remain:

```text
powered on
+
logged in
+
network connected
+
not sleeping
```

For a multi-day run on Windows, go to:

```text
Settings
→ System
→ Power & battery
→ Screen and sleep
```

and temporarily configure sleep appropriately while plugged in.

You can let the screen turn off. You only need to prevent the computer itself from sleeping.

---

# 11. One important Windows/WSL choice

For this ASIC project, you'll probably want Linux tooling eventually:

```text
iverilog
verilator
yosys
symbiyosys
OpenROAD
LibreLane
Tiny Tapeout tooling
```

You have two reasonable setups.

**Option A — recommended initially**

```text
Windows
│
├── Warp
├── repo
├── local Warp Agent
│
└── WSL / Docker
       │
       └── ASIC tools
```

The agent operates on the Windows repo and invokes Linux jobs through scripts/WSL/Docker.

This preserves Warp's native local repository indexing.

**Option B**

Put the whole repo inside WSL and run there.

This is excellent for the ASIC tooling, but Warp currently documents that **WSL sessions have the same limitations as SSH for some native Warp features**, including codebase indexing/native file tools and native diffs. It can still work via shell tools, but the overall agent experience is less rich. ([Warp][4])

So I would start with **Option A**.

Later we can make commands like:

```powershell
wsl bash scripts/test.sh
wsl bash scripts/synth.sh
wsl bash scripts/formal.sh
wsl bash scripts/pnr.sh
```

and Warp can execute those locally.

---

# 12. What I would do right now

Don't immediately launch the infinite loop.

First run:

```powershell
oz agent profile list
```

Copy the profile ID.

Then run the master prompt manually once:

```powershell
$prompt = Get-Content ".\orchestrator\WARP_MASTER_PROMPT.md" -Raw

oz agent run `
    --profile "YOUR_PROFILE_ID" `
    --cwd "$(Get-Location)" `
    --name "asic-first-run" `
    --prompt $prompt
```

Watch what it does.

If it correctly:

```text
reads master plan
✓
understands challenge
✓
creates state
✓
uses git sanely
✓
runs commands
✓
starts implementation
✓
runs verification
✓
updates queue
✓
doesn't delete/randomly rewrite things
✓
```

then run:

```powershell
.\run-warp-local.ps1
```

and you now have the local continuous system.

One billing detail: Warp explicitly says `oz agent run` is a **local agent run**, so it does **not consume Cloud Agent Credits**; it uses your local machine rather than a Warp-hosted cloud environment. Your normal model/agent usage can still be subject to whatever Warp plan/credits you have. ([Warp][5])

If you want, the next useful step is for me to give you the **exact Windows + WSL ASIC toolchain installation** (`iverilog + Verilator + Yosys + SymbiYosys + Tiny Tapeout + LibreLane`) so the Warp agent actually has the compilers/verifiers it needs rather than only being able to write RTL.

[1]: https://docs.warp.dev/reference/cli?utm_source=chatgpt.com "Oz CLI | Reference | Warp"
[2]: https://docs.warp.dev/knowledge-and-collaboration/warp-drive/ai-objects?utm_source=chatgpt.com "AI-Integrated Objects | Warp"
[3]: https://docs.warp.dev/code/overview?utm_source=chatgpt.com "Overview | Warp"
[4]: https://docs.warp.dev/code/ssh-feature-support?utm_source=chatgpt.com "Feature Support over SSH | Warp"
[5]: https://docs.warp.dev/support-and-community/plans-and-billing/credits?utm_source=chatgpt.com "Credits | Support & Community | Warp"
