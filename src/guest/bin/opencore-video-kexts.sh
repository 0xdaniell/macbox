#!/bin/bash
# Adds reims-util's three video kexts to the VM's OpenCore image, from inside macOS, so an x86-64
# guest drives Reims's VideoToolbox (106b:1a03) and M2 scaler (106b:1a0b) devices. reims-util
# 8a8a852 (2026-10-29) and later put them in every image they make; an image an older reims-util
# made lacks them, and `reims-ctl update-opencore` would rebuild the whole image (dropping any other
# change to it), so this adds just the three, as reims-util's own entries do:
#   AppleVideoToolboxParavirtualization.kext, AppleM2ScalerParavirtDriver.kext: Apple's arm64 guest
#     drivers ported to x86-64; they link apv-arm64-port's IOSurface mapper, already in the image;
#   VtWaitPatch.kext: a Lilu plugin that keeps VideoToolbox's 10 s waits for the host from expiring
#     at once on x86-64 (without it every encode fails with -19092). It patches 25G241 only.
# Each loads on Darwin 25.6.x (macOS 26.7) alone (MinKernel 25.6.0, MaxKernel 25.6.99), and the script
# refuses another macOS build than 25G241 unless FORCE=1.
#
#   opencore-video-kexts.sh <dir with the three .kext> [<EFI partition>]   (run with sudo rights)
#
# The kexts come from reims-util's kext/ (github.com/steelbrain/reims-util), e.g.
#   git -C reims-util archive 8a8a852 kext/AppleM2ScalerParavirtDriver.kext \
#     kext/AppleVideoToolboxParavirtualization.kext kext/VtWaitPatch.kext | tar -x
# The EFI partition is OpenCore's own disk (the one whose EFI/OC/config.plist exists); without one
# it is found. config.plist is kept as config.plist.before-video beside it. Takes effect at the next
# boot, with the devices attached (MACBOX_VIDEO=on). Running it again changes nothing.
# An entry already in config.plist for one of the three is set to the values above (an entry that is
# disabled or has other paths or kernel limits would not load). Nothing on the EFI partition changes
# until the edited config.plist passes plutil and the new kexts are copied beside the old ones; then
# both are swapped in by renames, and a failure part way puts the old ones back.
set -euo pipefail
K="$(cd "$1" && pwd)"
NAMES=(AppleM2ScalerParavirtDriver AppleVideoToolboxParavirtualization VtWaitPatch)
for k in "${NAMES[@]}"; do [ -d "$K/$k.kext" ] || { echo "no $K/$k.kext" >&2; exit 1; }; done
build=$(sw_vers -buildVersion)
[ "$build" = 25G241 ] || [ "${FORCE:-}" = 1 ] || {
  echo "macOS build $build: VtWaitPatch patches 25G241 (macOS 26.7.1) only; FORCE=1 installs anyway" >&2; exit 1; }
M=$(mktemp -d /tmp/ocmnt.XXXX) T=$(mktemp /tmp/occonfig.XXXX)
O="$M/EFI/OC/Kexts" swapped=()
cleanup() {
  for k in "${NAMES[@]}"; do sudo rm -rf "$O/.$k.kext.new" 2>/dev/null || true; done
  sudo umount "$M" 2>/dev/null; rmdir "$M"; rm -f "$T"
}
trap cleanup EXIT
found=""
for part in ${2:-$(diskutil list | awk '/EFI/ {print $NF}')}; do
  sudo mount -t msdos "/dev/$part" "$M" 2>/dev/null || continue
  if [ -f "$M/EFI/OC/config.plist" ]; then found=$part; break; fi
  sudo umount "$M"
done
[ -n "$found" ] || { echo "no EFI partition with EFI/OC/config.plist" >&2; exit 1; }
P=/usr/libexec/PlistBuddy C="$M/EFI/OC/config.plist"

# 1. The edited config.plist, staged in /tmp and checked.
cp "$C" "$T"
n=0   # entries; macOS's bash 3.2 has no associative arrays, so an entry is found by a scan
while $P -c "Print :Kernel:Add:$n:BundlePath" "$T" >/dev/null 2>&1; do n=$((n + 1)); done
index_of() { local j=0; while [ $j -lt $n ]; do
  [ "$($P -c "Print :Kernel:Add:$j:BundlePath" "$T" 2>/dev/null)" = "$1" ] && { echo $j; return; }; j=$((j + 1)); done; }
for k in "${NAMES[@]}"; do
  i=$(index_of "$k.kext")
  if [ -n "$i" ]; then what="set in place (Kernel.Add.$i)"
  else i=$n; n=$((n + 1)); $P -c "Add :Kernel:Add:$i dict" "$T"; what="added as Kernel.Add.$i"; fi
  for kv in "Arch string x86_64" "BundlePath string $k.kext" "Comment string reims-vgpu-video $k" \
      "Enabled bool true" "ExecutablePath string Contents/MacOS/$k" "MaxKernel string 25.6.99" \
      "MinKernel string 25.6.0" "PlistPath string Contents/Info.plist"; do
    key=${kv%% *}
    $P -c "Delete :Kernel:Add:$i:$key" "$T" 2>/dev/null || true
    $P -c "Add :Kernel:Add:$i:$kv" "$T"
  done
  echo "$k: $what"
done
plutil -lint "$T" >/dev/null

# 2. The new kexts, copied beside the old ones (a full partition fails here, with nothing changed).
for k in "${NAMES[@]}"; do sudo rm -rf "$O/.$k.kext.new"; sudo cp -R "$K/$k.kext" "$O/.$k.kext.new"; done
[ -f "$C.before-video" ] || sudo cp -p "$C" "$C.before-video"
sudo cp "$T" "$C.new"

# 3. Swap in by renames; on a failure the old kexts and config.plist go back.
rollback() {
  echo "failed; putting the previous kexts and config.plist back" >&2
  for k in "${swapped[@]}"; do
    sudo rm -rf "$O/$k.kext"; [ -d "$O/.$k.kext.prev" ] && sudo mv "$O/.$k.kext.prev" "$O/$k.kext"
  done
  sudo rm -f "$C.new"
}
trap 'rollback' ERR
for k in "${NAMES[@]}"; do
  sudo rm -rf "$O/.$k.kext.prev"
  [ -d "$O/$k.kext" ] && sudo mv "$O/$k.kext" "$O/.$k.kext.prev"
  swapped+=("$k")
  sudo mv "$O/.$k.kext.new" "$O/$k.kext"
done
sudo mv "$C.new" "$C"
trap - ERR
for k in "${NAMES[@]}"; do sudo rm -rf "$O/.$k.kext.prev"; done
sync
echo "OpenCore on $found has the video kexts; they load at the next boot"
