# Patches

`macbox build` applies these on top of the commits [versions.env](../versions.env) pins
(`container/build-reims.sh`). `macbox build --upstream` applies none of them.

Together they are **cohwl**, the build that has run day to day since 2026-11-05 on an Intel Arrow
Lake iGPU (Core Ultra 7 265, Mesa 26.2.4 ANV). Each series rebuilds that build's sources exactly:
metal2vulkan 1502180e, QEMU 82cef5aac4, and Reims 1f87b16 without its local build wiring (the
path of the metal2vulkan checkout, the `vendor/qemu` pointer). None of it is upstream yet.

Most of the new code sits behind a switch and does nothing until it is set. What macbox turns on
is in [docs/performance.md](../docs/performance.md#gpu-tuning-switches).

## metal2vulkan (on `development` at 0db5825a), 34 patches

| Patches | What they do |
|---|---|
| 0001 env-gated Arrow Lake-S facts | `M2V_ARL_FACTS=<list>` makes this GPU a measured identity: metal2vulkan then uses the faster paths its tables allow for each named driver fact. The list `macbox` sets (only on an Arrow Lake GPU) was checked fact by fact with metal2vulkan's GPU lanes: no lane fails with it that doesn't fail without it. Unset, nothing changes. |
| 0002 the release's pixel format table without a device | an accessor the Reims port needed |
| 0003 live switches (`M2V_LIVE`) | `name=value` lines in a file, re-read at most once a second, override driver facts (`facts_off=`, `facts_on=`) and a few runtime choices without a restart. For A/B measurements in one boot. |
| 0004 `host_rt_in_place` | a texture that lies over guest-RAM pages (the macOS desktop's framebuffer, the iOS Simulator's display surface) is rendered straight into those pages as a linear image, after a device check per memory kind, instead of being copied into a GPU image and back around every render pass. Those copies were 51% (simulator) and 67% (desktop) of all GPU time. Live switch `host_rt_in_place=1`. |
| 0005 ordering two imports of the same pages | a full memory barrier between two imports of the same guest pages, and a remapped memory whose imports follow their address keeps them: fixes a stale read 0004 exposed |
| 0006, 0007 pass timer | a diagnostic, off unless `M2V_PASS_TIMES` is set: GPU time per render pass and an image census |
| 0008 `tile_stored_only`, `tile_narrow_barriers` | live switches for two tile-memory experiments; off |
| 0009–0012 `surface_lazy` | a large surface's optimal image is kept beside its pages, and its bytes are brought up to date only when something reads them, instead of after every pass |
| 0013–0024, 0027–0034 Coherent (`surface_coherent`) | trap-coherent surfaces: QEMU write-protects or poisons a surface's guest pages (patches/qemu), so the GPU's image can stay ahead of the pages and the guest CPU's first touch of a page syncs exactly that page. This removes the remaining copies between guest RAM and the GPU. The later patches fix ordering cases the lanes found: copies back and in around held batches, writes that reach a held batch past later command buffers, and counters for each fallback. |
| 0025, 0026 flat32 | a flat dispatch whose grid fits counts in 32 bits (64 bits past 2^32 threads). The simulator's 1206x2622 masked-blit kernel went from 4.85 to 1.13 ms. Always on. |

## Reims vGPU (on 7198fe6), 25 patches

| Patches | What they do |
|---|---|
| 0001, 0002 `REIMS_VGPU_NON_APPLE_GPU` | an experiment, off unless set: the guest sees an Intel/AMD Mac's GPU instead of an Apple GPU family. No speed gain was measured, so macbox never sets it. **Never include `fbread`:** WindowServer's command buffers then fail and macOS resets the GPU. |
| 0003 port to metal2vulkan development 0db5825a | Reims's Vulkan platform on metal2vulkan's newer runtime. Upstream made the same move later (Reims c8ec271a). |
| 0004, 0005 `REIMS_VGPU_VBLANK`, `REIMS_VGPU_VBLANK_HZ` | re-raise an idle display's present interrupt once a refresh period; off. macOS turned out never to enable that interrupt bit, so macbox doesn't set it. |
| 0006–0008, 0016, 0018, 0020, 0021, 0024 census | the touch census: how often the guest CPU touches large IOSurfaces, its report script, boot-test guests and a content check. Off unless `REIMS_VGPU_CENSUS` names a directory. |
| 0009 the presented texture | tells metal2vulkan which texture the display presents (for `surface_lazy`) |
| 0010 tick experiments | logs the guest's enabled interrupt bits |
| 0011, 0014, 0019, 0022, 0023 Coherent hooks | the library's side of trap-coherent surfaces: tracks, syncs and host reads and writes, between metal2vulkan and QEMU's census |
| 0012 `REIMS_VGPU_PAGED_BUFFER_CACHE` | keeps paged textures' buffers; off (no reuse was measured) |
| 0013 `REIMS_VGPU_PRIVATE_SKIP_PAGE_UP` | skips a new Private texture's first page-up, which nothing can read; macbox sets it |
| 0015, 0017 NVMe boot tests | coherent pages through NVMe DMA, in the census's boot tests |
| 0025 upstream f6b7e927, video acceleration off | upstream's own commit, cherry-picked: `reims-ctl set --video-acceleration=false` leaves out the VideoToolbox and M2 scaler devices. macbox sets it, so the VM's devices are the ones it was measured with. |

## QEMU (`vendor/qemu` at bd95509, github.com/steelbrain/qemu-reims-vgpu), 9 patches

| Patches | What they do |
|---|---|
| 0001–0003, 0006–0009 the census | memslots move to a second mapping of guest RAM that only KVM uses, and userfaultfd markers there report the guest CPU's touches of tracked pages (`UFFD_USER_MODE_ONLY`: no capability or `/dev/userfaultfd` needed). QEMU's own loads, stores and DMA reach it too. Off unless `REIMS_VGPU_CENSUS` is set. |
| 0004, 0005 coherent pages | with `REIMS_VGPU_COHERENT=1` the same markers serve metal2vulkan's trap-coherent surfaces. A run that doesn't end well leaves `coherent.running` in the census directory, and every later start boots without coherent pages until it's removed (`macbox coherent reset`), so a fault there costs one crash, not a crash loop. |

## Licenses

Reims vGPU and metal2vulkan are `GPL-2.0-or-later OR LGPL-3.0-or-later`; these patches are offered
under the same terms. QEMU and its patches are GPL-2.0-or-later.

## Sending them upstream

Upstream's `development` branches have moved on since these bases (submission, sync and memory
tracking in metal2vulkan; a graphics device of Reims's own). The series are being rebased onto
them before anything is offered. flat32 (0025, 0026) is the first candidate: it applies to current
metal2vulkan and needs only a semantics version bump.
