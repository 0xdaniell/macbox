# Benchmarks

How many simulators does this host run at once, each one smooth and responsive? `macbox bench` answers that
and guards every change against it: one suite, one result file per run, one comparison between runs. The older
single-purpose tools it builds on (`simfps`, GlassBench, GenieBench) are described [at the end](#the-tools).

```sh
macbox bench tools                       # once: copy the bench to the Mac, build simfps and GlassBench there
macbox bench check                       # what this host and the VM offer
macbox bench scale --n 1,2,3,4,5,6       # the scaling sweep (the centrepiece)
macbox bench compare baseline.json new.json
macbox bench chart new.json              # an HTML/SVG chart next to it
DRY=1 macbox bench scale --n 1,2,3       # the whole control flow against stubs: no VM, no simulator, no GPU
```

Requirements: Python 3.8+ on the host (standard library only; no Pillow, no numpy), the VM running, `ssh` to it.
Unit tests, no VM needed: `cd src/bench && python3 -m unittest discover -s tests -t .` (the pure parts and the run's safety)
and `python3 -m unittest tests.test_dry` (the dry runs with injected faults, a couple of minutes).

## What a run does, and what it never touches

The bench has a **device set of its own** (`simctl --set`, a directory with a marker file), so it never sees the
pool's simulators or anyone's lease; every `simctl` the guest helper starts goes through one function that adds
`--set` and refuses a set without the marker. It creates its own devices, and at the end shuts down and deletes
all of them. The 3x device is the stock iPhone Pro; the **1x type** is the macbox 1x device type `macbox pool`
makes (same identifier, reused when it exists; made by the bench when it does not, and then CoreSimulatorService
must restart to load it, which closes every booted simulator: the bench refuses that unless nothing is booted
and `--allow-restart` is given). It starts no tracer. It reads `/proc`, `/sys`, the kernel journal and the VM's
census and log; it writes only its result file, and, with `ab --knob live:KEY=VALUE`, the one line in the VM's live
switch file (restored byte for byte on every way out).

Safety, in every run: the **hard deadline** (`--deadline`, default 90 minutes) stops the run 60 s early, writes the
result so far and runs the cleanup; **one exit trap** (normal end, error, SIGTERM/SIGINT, the deadline) kills the
helpers it started (explicit pids, never a bare `wait`), shuts down and deletes the bench's simulators and
restores every knob; a **guard** refuses to boot more simulators when the guest's available memory (free +
speculative + inactive + purgeable pages) would fall under
`--min-guest-free-gib` (default 1.5, after the estimated cost of the new simulators), the guest reports memory
pressure level 2 or more, the host's available memory is under `--min-host-free-gib` (6), or the disk under
`--min-disk-gib` (12): the sweep stops there, it does not swap the VM to death. A sweep that ran no step at all
(a refused guard, foreign simulators) is a FAIL with a non-zero exit. Simulators running outside the bench (a
`launchd_sim` that is not the bench's own) refuse the run (`--allow-foreign` to measure anyway, recorded). Before each
window the guest must settle: `settle` waits until the processes outside the simulators and the helper use less than
`--settle-bg` % of a core, for at most `--settle-max` s; the step records `settle.settled`, `bg_pct` and `waited_s`,
and the SCALE line names the N whose guest did not settle.

## Commands

| | |
|---|---|
| `scale` | N = 1, 2, 3... simulators (`--n 1,2,3,4,6,8`, `--scale-x 1\|3`, `--mix`), per simulator numbers, the maximum sustainable N per quality target. Stops early when a floor is breached (`--floor`, default `fps>=20`), a gate fails or the system turns unstable. |
| `idle` | The VM with no simulator: QEMU, GPU and memory standing cost against `--bar-qemu-cores` (0.15). |
| `lifecycle` | `--cycles` times: create, boot, install, launch, first app frame, shut down, resume, first frame, delete (medians); a per-cycle leak gate; `--storm N`: N simulators booted at once. |
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
`--probe-method auto|openurl|appearance|cmd|none`, `--slim` (the pool's Slim configuration through simslim on the bench's simulators: without it they are stock simulators, which cost more; it needs simslim in the guest, `macbox-guest tools`, and whether simslim finds devices of a custom device set is untested: the run stops with that message if it does not), `--quiesce` (disables `mediaanalysisd` in each simulator, as
some old harnesses did; recorded, off by default), `--no-observer-check`, `--no-golden`, `--shots N`.
Every run prints one PASS/FAIL/SKIP/INFO line per section and exits 0, 1 (a section failed) or 2 (the run could not
go on). The JSON has the same lines under `sections`.

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
started: their CPU is `system.guest.observer_cpu_pct`. RSS counts shared pages in each process: it overstates what leaving a simulator out would
free. `system.guest`: `total_cpu_pct`, `sims_cpu_pct` (the trees), `other_cpu_pct` (everything else), `sims_rss_mb`.

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
  touches Coherent's surfaces. `observer` measures that at the first N; the guest CPU of the simulators includes it
  only for the helper's own tree, which is excluded from the simulators' trees and reported as `observer_cpu_pct`.
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

A run on the iOS Simulator, from the host:

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
