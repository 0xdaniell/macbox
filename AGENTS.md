# For an agent setting up macbox

You're asked to give this Linux host a macOS VM with Xcode, iOS Simulators and T3 Code's Device
panel. You can do almost all of it yourself, including the steps in macOS's GUI. Work through
`./macbox next` until every line is ticked; [docs/setup.md](docs/setup.md) explains each step.

If macbox is already installed and you're asked to update it, skip to [Updating](#updating).

## Sharing improvements upstream

macbox welcomes contributions of any kind from users and coding agents: issues, ideas,
new features, bug fixes, performance work, documentation, experiments, and more.
If you identify or create a useful improvement while working with a user, point them
to [CONTRIBUTING.md](.github/CONTRIBUTING.md), and offer to prepare an
[issue](https://github.com/TUNA-NOPE/macbox/issues/new/choose) or
[pull request](https://github.com/TUNA-NOPE/macbox/pulls).
Do not publish an issue or PR on the user's behalf without their authorization;
include what was actually tested, and be clear about anything unverified.

## Ground rules

- **Ask the user before you change the host itself:** packages, groups (`usermod`), sysctl,
  Docker's installation. `./macbox doctor` says what's missing; show them the commands.
- **Never ask for a password in chat, and never put one in a command line.** The macOS user's
  password is typed into the VM. Have the user type it into the VM's screen, or, if they give it
  to you through a channel they choose, pipe it: `./macbox type -` reads stdin.
- **An Apple Account is the user's.** macbox never signs in to one. Downloading Xcode's `.xip` needs
  a sign-in at developer.apple.com: ask the user to download it and tell you the path.
- **Apple's license:** macOS's license allows it only on Apple hardware. Make sure the user knows
  before you install it (README, "Disclaimer").
- `./macbox build` takes 20 to 60 minutes and installing macOS another 30 to 60. Run them in the
  background and report progress, rather than waiting in silence.

## The GUI steps, as an agent

The VM's screen is `./macbox screenshot /tmp/s.png`; look at the PNG. Coordinates are the guest's
pixels at the current resolution (`./macbox status`).

```sh
./macbox screenshot /tmp/s.png           # look
./macbox click 960 540                   # left click; add right or double
./macbox type 'Disk Utility'             # US keyboard layout
./macbox key ret                         # ret, tab, esc, space, up, down, meta-space, meta-q, ctrl-c ...
./macbox scroll 960 540 5                # 5 notches down (negative: up)
./macbox wait 120 /tmp/s.png             # wait up to 120 s for the screen to change, then a screenshot
```

Take a screenshot after every action and check that it did what you meant before the next one.
Installing macOS restarts the VM several times; `wait` is the way to sit through it.

The sequence:
1. `./macbox create`, `./macbox boot-picker on`, `./macbox up`.
2. Recovery: Disk Utility → View → Show All Devices → "QEMU HARDDISK Media" → Erase (APFS, GUID) →
   quit → Reinstall macOS → the disk → agree → wait (30 to 60 min, several restarts; leave OpenCore's
   menu to its default).
3. When Setup Assistant or a broken desktop shows: `./macbox down && ./macbox up`, stop OpenCore's
   countdown with an arrow key, choose Recovery, Utilities → Terminal, then `./macbox recovery-fix`.
   It reboots; choose macOS.
4. Setup Assistant: country, keyboard, skip the Apple Account, an administrator named `dev`. The
   user picks the password and types it, or gives it to you as above. Turn off Location Services,
   analytics, Screen Time and Siri.
5. Open Terminal (`./macbox key meta-space`, type `Terminal`, `ret`), then `./macbox ssh-bootstrap`;
   the password again, then `ssh macbox true`.
6. Optional, in System Settings > General > Sharing: the (i) next to Remote Login, "Allow full
   disk access for remote users" on.

From there everything is over SSH: `./macbox down`, `./macbox disk nvme`,
`./macbox boot-picker off`, `./macbox up`, `./macbox provision` (sudo asks for the password once,
in an interactive terminal: if you have none, ask the user to run that one command), the user's
`.xip` with `./macbox xcode <path>`, `./macbox pool`, `./macbox t3 register` (then open the Device
panel once and `./macbox t3 fix-hub`), `./macbox agents install`.

## When you're done

`./macbox next` shows every step ticked. Check it end to end from an Expo app's checkout:

```sh
mac-sim up && mac-sim run && mac-sim shot /tmp/home.png && mac-sim down
```

Look at `/tmp/home.png`. If the user runs T3 Code, open the simulator in their Device panel with
`device_open`, using the ids `mac-sim t3` prints.

## Updating

You're asked to bring an existing install to the latest version. The user's VM, simulators and
config stay; only macbox's checkout, its build and its links change.

1. Find the checkout: `readlink -f "$(command -v macbox)"` points into it. `git status` there: if
   there are local changes, show them to the user and ask before you stash or drop anything.
2. `git log -1 --format=%h` (note it, to go back), then `git pull --ff-only`. Read what changed:
   `git log --oneline ORIG_HEAD..`.
3. `./macbox agents install`: tools, skills and user units are links into the checkout, and files
   can move between versions (they moved to `src/` in November 2026).
4. `./macbox doctor`. Anything it reports about the host: show the user the commands and ask.
5. If `git diff --stat ORIG_HEAD -- src/versions.env src/patches` shows anything, run
   `./macbox build` in the background (20 to 60 minutes) and report progress. The running VM keeps
   its QEMU until it restarts.
6. Config: compare `~/.config/macbox/macbox.env` with the defaults in docs/setup.md ("2. Config
   and checks"). Values `macbox init` wrote earlier stay as they were: `MACBOX_COHERENT=off` there
   is what `init` wrote before the default became `auto` (on with an Arrow Lake iGPU). Tell the user what differs and ask before you change it.
7. Ask the user when the VM may restart: it closes the simulators and apps running in macOS. Then
   `./macbox down && ./macbox up`.
8. Check: `./macbox status`, `./macbox coherent status`, `./macbox next` (every line still
   ticked), and the end-to-end check under "When you're done". Tell the user the old and new
   commit, and what the release changed.

If the new build misbehaves, go back: point `qemu/current` in
`$MACBOX_HOME/home/.local/share/reims-ctl/qemu/` at the previous build (they stay installed), or
`git checkout <old commit>` and `./macbox build`, then restart.
