# Baselines

Results worth comparing against, committed here so that a change can be held to them:

```sh
macbox bench scale --n 1,2,3,4,5,6 --scale-x 1 --mix all-busy --out /tmp/r
macbox bench compare src/bench/baselines/<name>.json /tmp/r/scale-<stamp>.json
```

Format: a result file of the bench as it wrote it (`schema` 1, `"publishable": true`), nothing edited by hand.
Name it `<kind>-<mix>-<scale>x-<build id>.json` (for example `scale-all-busy-1x-<build id>.json`), so a
comparison says what it compares. Only results with `"publishable": true` belong here: the bench scrubs host
names, accounts, addresses and paths on write, and `publishable` says whether anything slipped through.

A baseline is only a baseline for the setup that made it (`env`: macOS and runtime, device type, host kernel and
Mesa, the live switches). `compare` prints the mix and scale of both and warns when they differ; it does not
compare across setups for you. Keep one baseline per (kind, mix, scale), replace it when the setup changes on
purpose, and say why in the commit.

`golden.json` is different: the pixel hashes of GlassBench's static screen per (macOS, runtime, device type, size),
written by `macbox bench golden` and read by the `pixels` gate. Re-record it after a macOS or runtime update that
changes how text or glass renders; a hash that changed without such an update is a rendering regression.

There are none yet: the first ones come from a run on the VM that has the build you want to hold.
