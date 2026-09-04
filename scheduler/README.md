# Daily schedule — URL QC

The daily QC run is driven by the Windows Task Scheduler task **`URL QC Daily`**,
which runs `run_daily.bat` (→ `python url_qc.py --no-bifrost-api --email`).

**Triggers (two):**
- **Daily at 07:00 IST.**
- **At logon** (2-min delay) — so it also runs each time you turn the machine on and
  sign in. `MultipleInstancesPolicy = IgnoreNew` stops the two triggers double-running.

**Throttle:** because the logon trigger can fire on every sign-in, `run_daily.bat` calls
`scheduler/throttle_check.ps1` first and **skips the run if the last actual run started
less than 6 hours ago** (logged as a `Skipped ...` line in `logs\run.log`). The last-run
time is tracked in `logs\last_run.txt`, stamped only when a run actually proceeds — so
repeated logons don't keep pushing the window. Change the window by editing the
`-MinHours 6` argument in `run_daily.bat`.

## Files
- `URL-QC-Daily.task.xml` — exported task definition (source of truth).
- `apply-schedule.ps1` — registers the task from the XML and enables wake-to-run +
  battery/catch-up settings + wake timers on the active power plan.

## Run-even-when-idle behaviour
The task is configured to survive an inactive machine:

| Setting | Value | Effect |
|---|---|---|
| `WakeToRun` | true | Wakes the PC from **sleep/hibernate** at 07:00 to run. |
| `StartWhenAvailable` | true | If a scheduled run was **missed**, runs it when the machine next comes on. |
| `StopIfGoingOnBatteries` | false | Doesn't kill the run if you unplug. |
| `DisallowStartIfOnBatteries` | false | Starts even on battery. |
| Wake timers (powercfg) | Enabled AC **and** battery | Required for `WakeToRun` to actually fire. |

## Important limitation
**Nothing here can run while the PC is fully powered OFF (shut down / no power).**
`WakeToRun` only wakes from Sleep or Hibernate. For runs while the laptop is truly
off, the job must run on an always-on machine or a cloud environment.

## Re-apply on a new machine
```powershell
powershell -ExecutionPolicy Bypass -File scheduler\apply-schedule.ps1
```
(The XML pins the current user's SID and the repo path — adjust if either changes.)
