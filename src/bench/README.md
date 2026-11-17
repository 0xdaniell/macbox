# Benchmarks

How many simulators does this host run at once, each one smooth and responsive? `macbox bench` answers that
and guards every change against it: one suite, one result file per run, one comparison between runs. The older
single-purpose tools it builds on (`simfps`, GlassBench, GenieBench) are described [at the end](#the-tools).

```sh
macbox bench tools                       # once: copy the bench to the Mac, build simfps and GlassBench there
macbox bench check                       # what this host and the VM offer
macbox bench seed                        # once: the production seed every bench simulator is cloned from (kept between runs)
macbox bench scale --n 1,2,3,4,5,6       # the scaling sweep (the centrepiece)
macbox bench cleanup                     # after a run that could not clean up (the next run refuses to start until then)
macbox bench compare baseline.json new.json
macbox bench chart new.json              # an HTML/SVG chart next to it
DRY=1 macbox bench scale --n 1,2,3       # the whole control flow against stubs: no VM, no simulator, no GPU
```

Requirements: Python 3.8+ on the host (standard library only; no Pillow, no numpy), the VM running, `ssh` to it.
Unit tests, no VM needed: `cd src/bench && python3 -m unittest discover -s tests -t .` (the pure parts and the run's safety)
and `python3 -m unittest tests.test_dry` (the dry runs with injected faults, a couple of minutes).

## What a run does, and what it never touches

The bench has a **device set of its own** (`simctl --set`, a directory with a marker file, the same one for every run so
its seed is kept), so it never sees the pool's simulators or anyone's lease; every `simctl` the guest helper starts goes
through one function that adds `--set` and refuses a set without the marker. The one exception is the clone out of
Simlock's source set (below), which is read-only for Simlock. At the end of a run the bench shuts down and deletes every
device it made except the seed. The 3x device is the stock iPhone Pro; the **1x type** is the macbox 1x device type
`macbox pool` makes (same identifier, reused when it exists; made by the bench when it does not, and then
CoreSimulatorService must restart to load it, which closes every booted simulator: the bench refuses that unless nothing
is booted and `--allow-restart` is given). It starts no tracer. It reads `/proc`, `/sys`, the kernel journal and the VM's
census and log; it writes only its result file, and, with `ab --knob live:KEY=VALUE`, the one line in the VM's live
switch file (restored byte for byte on every way out).

### Bench simulators are production simulators

A fresh stock device is not what an agent gets: it runs stock services (a first boot keeps the vCPUs busy for minutes),
costs about 1.8 GB of guest disk (five of them took the guest's disk, then the host's, to the storage guard's limit and
stopped the VM) and keeps the Home Screen widgets, one of which retries a service Slim turns off in a tight loop
(~3 cores per "idle" simulator, for ever). So the bench makes simulators the way Simlock does:

1. **One seed per device type**, kept shut down in the bench's set between runs: `simctl --set <Simlock's source set>
   clone <source> <seed> <bench set>` (the source is Simlock's own device of that type: widgets trimmed, not Slim; Simlock's
   set is only ever cloned from, never written, booted, shut down or deleted, and its config is not read), registered with
   `sim-idle-guard`, one boot, `simslim --set <bench set> on <seed> --profile <profile>` (it reboots the device),
   a check that its `disabled.plist` has at least 100 services off (168 today), the bench app installed, a real settle
   (the guest quiet for two samples in a row and the widgets' `chronod/icons` present, `--seed-settle-max` 600 s) and a
   shutdown. A seed that fails any step is deleted again, never left to be reused.
2. **Every bench simulator is `simctl clone <seed>`** (an APFS clone: no disk cost) with the seed's `disabled.plist` copied
   into its own `/private/var/tmp/com.apple.CoreSimulator.SimDevice.<udid>` folder (mode 0700) before its first boot, and
   checked there; the app is already in it.
3. The seed is **reused** by later runs (its Slim services, its widgets and the app's build are checked: a seed made for
   another build of the bench app is refused, not silently reinstalled). `macbox bench seed --rebuild-seed` (or
   `--rebuild-seed` on any run) makes a new one, which you need after `macbox bench tools` rebuilt the app.

Simlock is an **optional** integration. Without a source device of the wanted type (`macbox bench check` lists the types
it finds) the run stops and says so. Two explicit ways out: `--scale-x 3` (the stock iPhone Pro type,
which Simlock usually has a source for), or **`--stock-devices`**: fresh stock devices, created one at a time with the
host-disk guard in front of each, for a device type Simlock has no source for or a scenario that needs a stock device.
The result records `env.guest.device_source` (`stock devices` or clones of a seed) and `compare` warns when two results
were made differently. Defaults of the Mac's paths: `~/.simlock04/devices/ios-sources` (`--source-set`),
`~/go/bin/simslim` (`--simslim`), the only `.json` in `~/.config/simslim/` (`--slim-profile`; several or none: the run stops and asks for the flag), `~/.local/bin/sim-idle-guard`.

### Safety, in every run that boots simulators

The **hard deadline** (`--deadline`, default 90 minutes) stops the run 60 s early, writes the result so far and runs
the cleanup; **one exit trap** (normal end, error, SIGTERM/SIGINT, the deadline, a monitor's abort) kills the helpers it
started (explicit pids, never a bare `wait`), shuts down and deletes the bench's simulators and restores every knob.

- **Idle guard.** The Mac's `sim-idle-guard` shuts down every simulator nobody used for 15 minutes, in any device set.
  Every bench simulator (and the seed while it is booted) is registered with `sim-idle-guard keep <udid> <minutes>` for the
  run's length (the deadline plus 5 minutes, at most 240; renewed before every window) and `~/.sim-keep/<udid>` is removed
  by the cleanup. A guard that is not installed is recorded in the result (`idle_guard`, a section) and the run goes on; a
  guard that refuses `keep` stops it.
- **Disk.** `--disk-path` (a path on the filesystem that holds the VM's storage and that your storage guard watches) must
  have `--disk-start-gib` 18 free to start, `--disk-boot-gib` 14 to boot another simulator (checked before **every** boot:
  simulators boot one after the other, the lifecycle's boot storm is the one exception), and under `--disk-abort-gib` 11 the
  run is aborted and cleaned up even while it waits. A disk reading that fails refuses.
- **Guest memory.** Before every boot: the guest's available memory (free + speculative + inactive + purgeable pages) after
  the estimated cost of the new simulators stays over `--min-guest-free-gib` (1.5), no memory pressure level 2 or more,
  **swap-outs have not grown** since the run began, the **compressor** holds at most `--max-compressor-mb` (2048), the
  host's available memory is over `--min-host-free-gib` (6). The sweep stops there; it does not swap the VM to death.
- **A monitor** runs on its own thread while the run waits in a long call (a boot, a settle, a window): the host disk, the
  VM's QEMU process (the same pid **and** start time) always; the Mac (answers over ssh twice in a row, no simulator that is
  not the bench's, swap-outs, compressor) outside measurement windows, whose own load it must not add to. A breach aborts
  the run from that thread: the partial result is written, the cleanup runs, the cleanup's verdict is added and the process
  exits with 8 (disk), 9 (VM), 7 (someone else's simulator: **yield**, `--allow-foreign` to measure anyway) or 10 (guest
  memory).
- **Settle fails closed.** Before a window the guest must settle: every process except the load and its observers (the
  renderers `SimMetalHost`/`backboardd`/`SimRenderServer`, GlassBench, `simfps`, WindowServer, python, ps, ssh, matched by
  name against the whole command, and the helper's own tree) uses less than `--settle-bg` % of a core for two samples in a
  row. A settle that reaches `--settle-max` (300 s) **fails the run** (a `settle` FAIL section with the top processes by CPU
  at that moment); nothing is measured on top of an unsettled guest.
- **The count of booted simulators.** Before every window (scale steps, A/B arms, soak, the observer window, idle) the guest
  counts the Booted devices of the bench's own set and the run fails on any mismatch with the expected number (a
  simulator the idle guard or someone shut down would otherwise be measured as if it ran). A step's `booted` is that
  measured count, `expected_booted` the expected one.
- **The frame counter fails the window.** A simulator for which `simfps` returned nothing or an error, or an animating one
  from whose surfaces no counter could be read, fails the `counter` gate (and so the step, the run and the exit code).
- **Leftovers.** A run takes the set with a lock and **refuses to start** while an earlier run left a device other than the
  seed, a booted or never-completed seed, a lock, or overrides/keep files of a deleted device; `macbox bench cleanup` removes
  them (`--seeds` also deletes the seeds). The cleanup looks again after deleting and says what it could not remove
  (`CLEANUP FAIL`, exit code **20**, the lock stays); "cleanup done" is only printed for a verified one.
- A sweep that ran no step at all (a refused guard, foreign simulators) is a FAIL with a non-zero exit.

## Commands

| | |
|---|---|
| `seed` | Make the production seed for the device type (or check and report the one that is there); `--rebuild-seed` makes a new one. |
| `cleanup` | Shut down and delete what an earlier run left in the bench's set (verified; exit 20 when something stays); `--seeds` deletes the seeds too. |
| `scale` | N = 1, 2, 3... simulators (`--n 1,2,3,4,6,8`, `--scale-x 1\|3`, `--mix`), per simulator numbers, the maximum sustainable N per quality target. Stops early when a floor is breached (`--floor`, default `fps>=20`), a gate fails or the system turns unstable. |
| `idle` | The VM with no simulator: QEMU, GPU and memory standing cost against `--bar-qemu-cores` (0.15). |
| `lifecycle` | `--cycles` times: create, boot, install, launch, first app frame, shut down, resume, first frame, clone, boot the clone, delete (medians of the timings below); a per-cycle leak gate; `--storm N`: N simulators booted at once. |
| `soak` | `--soak-n` busy simulators for `--soak-minutes`: slope of QEMU's RSS and of the guest's memory, and the frame rate at the start against the end. |
| `build` | Build timing: the bench tools (swiftc) cold and warm; `--xcodebuild '<args>'` times your own project cold and warm (GlassBench is not an Xcode project, so the section is SKIP without it). |
| `panel` | The Device panel's stream: pluggable (see [The panel section](#the-panel-section)); SKIP without `--panel-cmd`. |
| `ab` | A B B A inside one window for one knob: ADOPT or CLOSE against a threshold fixed before the run. |
| `golden` | Records the pixel hash of the static reference screen for this setup in `baselines/golden.json`. |
| `compare A.json B.json` | What moved, per N, beyond the noise (`--noise 0.04`); exit status 1 on a regression. |
| `chart R.json [-o out.html]` | A self-contained HTML page of SVG charts: per-simulator fps and p95 against N, input proxy, QEMU CPU and GPU, guest CPU and memory, GPU ms per frame, fairness. No script, no external file. |
| `report R.json` | The PASS/FAIL lines of a result. |
| `tools [--no-build]` / `check` | Copy the bench to the guest and build it / what is there and what is missing. |

Common options: `--out DIR` (results, default `bench-results/`), `--secs` (window length, default 40),
`--nominal-fps 60`, `--tag key=value` (recorded, for example `mesa=26.2.4` when Mesa is not a package),
`--probe-method auto|openurl|appearance|cmd|none`, `--stock-devices`, `--rebuild-seed`, `--source-set`, `--simslim`, `--slim-profile`, `--seed-settle-max` (see above), `--disk-path`, `--disk-start-gib`, `--disk-boot-gib`, `--disk-abort-gib`, `--max-compressor-mb`, `--set-name` (the bench's device set, default `bench`), `--quiesce` (disables `mediaanalysisd` in each simulator, as
some old harnesses did; recorded, off by default), `--no-observer-check`, `--no-golden`, `--shots N`.
Every run prints one PASS/FAIL/SKIP/INFO line per section and exits 0, 1 (a section failed), 2 (the run could not
go on), 20 (the cleanup could not delete everything) or, when a monitor aborted it, 7, 8, 9 or 10 (above). The JSON has the same lines under `sections`.

### Workload mixes (`scale --mix`)

Agents' simulators are not all animating.

| mix | what each simulator runs | what it measures |
|---|---|---|
| `all-busy` (default) | GlassBench scrolling, input marker on | every simulator at full rate; frames and input of each |
| `one-busy` | simulator 0 animates; the others show GlassBench `-static` and get an input probe every `--probe-every` s | whether a busy simulator makes the others sluggish (their input proxy), and what an idle-but-ready simulator costs |
| `agent` | per simulator, staggered: launch the app, let it scroll, screenshot, terminate, idle (`--agent-period`, 12 s) | launch to first frame, screenshot time, and the frame rate inside the motion stretches |
| `idle` | booted, nothing launched, no observer | the pure standing cost of a booted simulator: the slope of each resource over N |

## What is measured: exact definitions

Everything is per simulator first; system numbers come with it, never instead of it. A result never holds only an
average.

**Frames** (per animating simulator; GlassBench draws a 16-bit counter strip, see below). `simfps --strip` polls the
simulator's framebuffer surface at about 2 kHz and reads the counter from every new surface.
- *distinct frames*: surfaces whose counter differs from the previous distinct one. `fps` = (n - 1) / (last - first).
- `p50_ms`, `p95_ms`, `p99_ms`, `max_ms`: percentiles of the intervals between consecutive distinct frames
  (`sorted[min(n-1, int((n-1)*p))]`, the rule of simfps and GlassBench itself). `over2x`: intervals longer than
  2 / `--nominal-fps`. `longest_gap_s`: the longest interval, seconds (the "stall").
- `strip`: `repeats` (same counter on another surface: normal, the compositor makes more surfaces than the app
  ticks), `lost` (ticks that never reached the screen), `backwards` (an older counter after a newer one),
  `jumps` (forward by more than 1000), `unreadable` (references not found), `span_ticks` (ticks the counter moved).
- `app_tick_fps`: GlassBench's own tick rate from its `pace.json` (the app, before the compositor), `surface_fps`:
  surface changes per second as simfps counts them. Three layers, so a loss can be placed.
- The agent mix reports the same over the stretches of motion only (a gap above 250 ms ends a stretch).

**Input latency proxy** (`input_p50_ms`, `input_p95_ms`, `input_max_ms`, `input_misses`, `input_call_p50_ms`).
It is a proxy and says so. Without Simlock or agent-device, the guest injects an event with `simctl`: by default
`simctl openurl glassbench://toggle`, which flips a 30 pt marker (white/black) in GlassBench `-probe`; the fallback is
`simctl ui appearance dark|light`, which flips the same marker; `--probe-cmd` may run any command that flips it (a
real tap through your own tool). The first N calibrates which method is answered (3 tries each) and the result
records it. For each injection: *t0* = the wall clock just before the command is started in the guest; the answer
is the first new surface after t0 whose marker (luma above or below 127 at a fixed point) differs from the last
surface before t0; latency = that surface's time - t0. A probe not answered in `--probe-timeout` (2 s) is a **miss
and counts at the timeout** in p50/p95, never dropped. The number includes starting `simctl` (its own duration is
`input_call_p50_ms`), CoreSimulator, the app's handling and one composite. It excludes the Device panel's stream, the
viewer and a real finger's HID path. Read it as a trend over N and between builds, not as an absolute touch latency;
the default `responsive` bar (150 ms) wants one calibration at N=1 on your hardware before you trust it. p95 over
few probes is coarse: a 40 s window gives 16 probes per simulator.

**Guest CPU and memory of a simulator** (`guest_cpu_pct`, `guest_rss_mb`). From `ps` snapshots in the guest at the
window's start and end: the CPU time of every process in the simulator's tree over the window, in % of one core
(CPU time has a 10 ms resolution; a process that started or ended inside the window is counted only if it
was in both snapshots, so very short-lived processes are undercounted), and the RSS of the tree at the end. The
tree is every process whose command line names the simulator's UDID (`launchd_sim`, the per-device helpers) and
all their descendants, never the bench's own helper, its ssh and shell ancestors, or the `simfps`/`simctl`/`ps` it
started: their CPU is `system.guest.observer_cpu_pct` (the larger of the helper tree's CPU between the two `ps` snapshots and the CPU time of the helper's reaped children, `simfps` and the probes' `simctl`, over the window). RSS counts shared pages in each process: it overstates what leaving a simulator out would
free. `system.guest`: `total_cpu_pct`, `sims_cpu_pct` (the trees), `other_cpu_pct` (everything else), `sims_rss_mb`.

**Lifecycle timings** (`lifecycle`, seconds, medians over `--cycles`; each cycle makes its own device the way a lease does:
a clone of the seed; with `--stock-devices` a fresh stock device, and then `create_s`, `install_s` and one more clone of the
installed device are timed too): `clone_s` (`simctl clone` of the seed + the Slim disables copied + the idle-guard keep);
`cold_boot_s` (the clone's first `boot` + `bootstatus`); `cold_launch_call_s` (the `launch` call);
`cold_first_frame_s` (from the launch call to the first counter value `simfps` reads on the new surface); `shutdown_s`
(shutting the booted device down), `resume_boot_s` and `resume_first_frame_s` (the same two again: "resume" here is
shutdown then boot, not Simlock's parking); `delete_s` (`simctl delete` of the device and its overrides folder; stock
path only: `clone_boot_s`, the boot of a clone of the installed device, deleted untimed). The boot storm
(`--storm N`) clones N simulators and boots them at once.

**Guest memory** (`system.guest`): `mem_used_mb` = active + wired + compressor pages; `mem_free_mb` = free +
speculative; `mem_avail_mb` is what the guard uses: free + speculative + inactive + purgeable (`vm_stat` pages; 4 KiB
when the header does not say, 16 KiB on arm64); `compressed_mb`; `swapins_d`, `swapouts_d`, `pageouts_d` = the counters' change over the window;
`pressure_max` = the highest `kern.memorystatus_vm_pressure_level` sampled every 2 s (1 normal, 2 warning, 4
critical).

**QEMU CPU** (`system`): from `/proc/<qemu pid>/task/*/stat` over the window, in cores: `qemu_user_cores` = host-side
user time (utime - guest_time), `qemu_guest_cores` = time in guest mode (the vCPUs running the guest),
`qemu_sys_cores` = kernel time; `qemu_cpu_cores` their sum; `qemu_top_threads` the six busiest thread classes (digits
folded: `CPU N/KVM` is `vCPU`).

**GPU** (`system`): `gpu_busy_pct` = the change of `drm-engine-render` summed over QEMU's DRM clients (fdinfo,
each client once) divided by the wall time; `gpu_ms_per_frame` = that GPU time divided by the distinct frames of all
animating simulators in the window. i915 only (xe states cycles, not ns: the field is null, never converted by a guess).

**Host memory and swap** (`system.host`): `mem_avail_gb`, `qemu_rss_mb`, `cgroup_mem_gb`, `cgroup_swap_mb` and its
change over the window, `cgroup_events` (high/max/oom_kill deltas), `pswpin_pages`/`pswpout_pages` (system wide),
`psi_mem_some_pct` (system) and `cgroup_psi_some_pct` (the VM's cgroup).

**Stability** (`stability`): new `.ips`/`.crash` files in the guest's DiagnosticReports during the window,
simulators no longer Booted at its end, kernel GPU error lines (`journalctl -k`: GPU hang, fence expired, context
reset, xe job timeouts and faults), QEMU still the same process. Any one makes the step unstable and ends the sweep.

**Fairness** (`fairness`, per N): Jain's index over the distinct fps of the animating simulators (1.0 = equal, 1/n =
one gets everything) and min/max.

**Maximum sustainable N** (`summary.max_n`, one per target). A target is `name: fps>=55, p95<=25` over per
simulator metrics (`fps p50 p95 p99 max over2x stall input_p50 input_p95 cpu rss`). N passes when every simulator
that has a value for each condition meets it, with its gates ok and the system stable. The maximum is the largest N
such that it and **every smaller measured N** passed: the first failure ends it (a pass above a failure is luck, not
sustainability). A condition no simulator has a value for makes the target unknown (`null`), never a pass.
Defaults: `smooth60: fps>=55, p95<=25`, `smooth30: fps>=30, p95<=50`, `responsive: input_p95<=150, stall<0.3`.

**Standing cost per simulator** (`summary.standing_cost`): the least-squares slope of each system number over N, with
its intercept and R². Use `--mix idle` for the pure cost of a booted simulator.

## Correctness gates

They run with every scenario; a FAIL fails the run and stops a sweep, but the result with every smaller N is
written first. A gate that cannot be read is SKIP, shown as such, never PASS.

| gate | PASS when |
|---|---|
| `strip` | no frame out of order, no counter jump, the counter advanced no more than the app's own tick log says, under 1% of samples unreadable. Lost ticks are the fps metric, not a gate failure. |
| `tears` | no screenshot of the scrolling GlassBench shows a torn band (card spacing and gap heights, the old `tears.py` rule, standard library PNG decoding); SKIP when no screenshot shows cards |
| `pixels` | GlassBench `-static` screenshots agree with each other and match the stored golden (`baselines/golden.json`, key: macOS, runtime, device type, size), judged once on a freshly launched static app before the first window (a window with input probes flips the marker, so later static screens are not judged); SKIP with the pictures agreeing when no golden is stored: `macbox bench golden` records it |
| `coherent_census` / `coherent_process` | in the census reports written since the run began: `overwrite_risk` and `sync_errors` stayed 0, and `late_copies_in_busy`, `late_beside_unplanned`, `plan_left_planned` did not rise. Deltas, not absolute counts. Reports come once a minute, so a short step may have none: SKIP. |
| `qemu_log` | no new `command buffer failed` line in QEMU's log (stamp waits are counted in the detail) |
| `gpu_hang` | the hang flag did not appear |
| `kernel_gpu` | no kernel GPU error line since the step began (SKIP when the journal is not readable) |
| `counter` | `simfps` answered with a result for every simulator of the window and every animating simulator's surfaces gave a readable frame counter (a counter that answers nothing or an error fails the window, it is not a missing metric) |
| `memory` | the VM's own cgroup swapped no more than 50 MB (SKIP when the cgroup is unreadable: other projects' swapping on the host is not this run's); guest swap-outs fail it with `--fail-on-swap` |
| `run_*` | every scenario, after its body: `run_coherent_census`, `run_coherent_process`, `run_qemu_log`, `run_gpu_hang` and `run_kernel_gpu` since the run began, and `run_crash_reports` (a new `.ips` on the guest since setup). They fail the exit code of `idle`, `lifecycle`, `soak`, `ab` too. |
| `leak_*` | `lifecycle`: QEMU RSS and guest memory grow less than `--leak-rss-mb` / `--leak-guest-mb` per create/boot/delete cycle (least-squares slope); `soak`: less than `--soak-rss-mb-min` / `--soak-guest-mb-min` per minute |
| `observer` | reading the strip does not move the surface frame rate by more than 4% (a second window of the first N without the strip read); otherwise the distinct-frame numbers are lower bounds |

## A/B discipline

`ab` runs one comparison per lever and nothing else.

```sh
macbox bench ab --knob live:group_batches=16 --n 3 --metric total_fps --threshold 0.04
macbox bench ab --set-cmd 'macbox x on' --unset-cmd 'macbox x off' --n 3 --metric worst_p95
```

The rule is printed and stored **before** the run. Simulators boot once; then the arms run interleaved A B B A in
one window (`--arm-settle` between them, `--secs` each). Pair 1 = the first A with the first B, pair 2 = the second
B with the second A. **ADOPT** only if the mean effect of the two pairs exceeds the threshold (default 4%, the
run-to-run noise on this stack; fixed on the command line before the run), **both pairs agree in sign**, and no tail
metric (default `worst_p95`, `worst_p99` for the fps metrics) worsens by more than `--tail-tol` (4%). Anything else,
including a gate failure or instability in any arm, is **CLOSE**, with no re-run. Metrics: `total_fps` (sum of the
simulators' distinct fps), `min_fps`, `worst_p95`, `worst_p99`, `input_p95`, `gpu_ms_per_frame`, `qemu_cores`. The
decision line (`AB <knob>: ADOPT|CLOSE | metric A a1/a2 B b1/b2 | effect, pairs, bar, reasons`) is a section of the result
file. A knob that needs a VM restart cannot be A/B'd here (the arms share one boot, by design).
`--knob live:KEY=VALUE` edits one line of the VM's live switch file and puts the file back whole on every exit.

## The panel section

The Device panel needs Simlock and T3 Code, which the bench does not depend on. `panel --panel-cmd 'cmd'` runs your
command with `BENCH_PANEL_SECS` set and reads one JSON object from its last stdout line:
`{"frames": [[t_seconds, counter], ...]}`: every frame a viewer received through the panel and the counter
GlassBench's strip showed in it (the strip survives H.264). The bench then applies the same distinct-frame metrics and
strip gate and judges `--panel-fps` / `--panel-p95` (55 / 25). Without a command the section is SKIP.

## The result file

`<out>/<kind>-<UTC stamp>.json`, `schema` 1: `kind`, `id`, `created`, `params` (the command line), `env` (`build_id`,
`switches` read from the live switch file, `qemu_env` (REIMS_VGPU_* names and values), `guest` macOS / Xcode /
runtime / device type, `host` kernel / Mesa / CPU, free-form `tags`), `sections` (the PASS/FAIL lines), `gates` (the
run's roll-up), `data` (for `scale`: `mix`, `scale_x`, `steps[]` with `sims[]`, `system`, `stability`, `gates`,
`targets`, `fairness`, and `summary`). The file is written after every step, so a run that dies keeps its data. Nothing in it
names a host, an account or a path: strings are scrubbed on write (home paths to `~`, host and ssh names to `<host>`,
IPv4 addresses to `<ip>`), and `publishable` says whether anything slipped through (`problems` lists it).

`baselines/` holds results worth comparing against (see its README); `golden.json` holds the pixel hashes.

## Limits of the measurements

- The observer is not free: `simfps` polls at 2 kHz and `--strip` reads each new surface with the CPU, which also
  touches Coherent's surfaces. `observer` measures that at the first N; the simulators' guest CPU does not include it
  (the helper's own tree is excluded from every simulator's tree and reported as `observer_cpu_pct`), but the
  observer still takes CPU the simulators would otherwise have.
- Probes run `simctl` in the guest every 2.5 s per simulator, a small steady load that agents also make.
- Windows are 40 s: slow drifts need `soak`; a stall rarer than once per window is invisible.
- GPU numbers need DRM fdinfo of QEMU's process to be readable by the user running the bench.

## The tools

The tools behind the numbers in [docs/performance.md](../../docs/performance.md). `macbox bench tools` copies and builds
them; by hand, inside macOS, once:

```sh
tar -C bench -cf - . | ssh macbox 'mkdir -p ~/.macbox/bench && tar -C ~/.macbox/bench -xf -'
ssh macbox 'cd ~/.macbox/bench && ./build.sh'
```

| | |
|---|---|
| `simfps <udid> <seconds> [label] [--strip]` | counts the frames a simulator's screen produces, straight from CoreSimulator's framebuffer (no encoder in the way); one JSON line with fps and frame intervals. `--strip` also reads GlassBench's frame counter from each new picture (its synced reads slow the 2 kHz poll, so count frames without it). Environment, used by the bench: `SIMFPS_SET` (a device set of its own), `SIMFPS_START_AT` (attach, then count from this unix second, so several simfps share one window), `SIMFPS_PROBE=x,y` (read this point's luma, in points of a 402 pt screen), `SIMFPS_STAMPS=path` (one line per new surface: unix seconds, counter or -1, probe luma or -1; the first line is the screen as it stands at the start) |
| `GlassBench.app` | the iOS load: a table scrolling by itself under Liquid Glass bars and a floating glass toolbar. It draws a **frame strip** at the top left: a 16-bit counter that advances every display-link tick, as 18 squares of 20 pt from (21, 120) pt (white and black references, then bits 0-15), large enough to survive H.264, so any viewer can tell unique frames from repeats and count the ones lost on the way. Its own tick intervals go to `Documents/pace.json` in its container every 5 s. `-nostrip` leaves the strip out, `-static` animates nothing (a fixed picture), `-probe` adds the input marker: a 30 pt square at (21, 160) pt that flips white/black on a tap or a `glassbench://toggle` URL |
| `GenieBench.app` | the desktop load: a window that minimizes and restores, for counting the Genie animation's frames |
| `scroll-local.html` | a page for Safari that scrolls itself under a blurred floating bar, with no network |

A by-hand run on the iOS Simulator (a **fresh stock device**: about 1.8 GB of guest disk and a minutes-long first boot, not what an
agent's simulator is; `macbox bench scale` makes production simulators and is what to use for numbers):

```sh
# a simulator of its own for the bench, so the pool is left alone (1x: the macbox 1x device type)
U=$(ssh macbox 'xcrun simctl create "macbox bench 1x" dev.macbox.CoreSimulator.SimDeviceType.iPhone-17-Pro-1x')
ssh macbox "xcrun simctl boot $U && xcrun simctl install $U ~/.macbox/bench/GlassBench.app && xcrun simctl launch $U dev.macbox.glassbench"
sleep 10    # warm-up
ssh macbox "~/.macbox/bench/simfps $U 15 glass-1x"
ssh macbox "xcrun simctl delete $U"
```

Measure with nothing else animating (`mac-sim status`), Simulator.app closed, two runs or more
after a warm-up. GPU time per frame on the host: QEMU's `drm-engine-render` in
`/proc/<qemu pid>/fdinfo/*` (`docker top macbox` gives the pid), divided by the frames.
