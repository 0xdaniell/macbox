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
# Each loads on Darwin 25.6.x (macOS 26.7) alone (MinKernel 25.6.0, MaxKernel 25.99.99).
#
#   opencore-video-kexts.sh <dir with the three .kext> [<EFI partition>]   (run with sudo rights)
#
# The kexts come from reims-util's kext/ (github.com/steelbrain/reims-util), e.g.
#   git -C reims-util archive 8a8a852 kext/AppleM2ScalerParavirtDriver.kext \
#     kext/AppleVideoToolboxParavirtualization.kext kext/VtWaitPatch.kext | tar -x
# The EFI partition is OpenCore's own disk (the one whose EFI/OC/config.plist exists); without one
# it is found. config.plist is kept as config.plist.before-video beside it. Takes effect at the next
# boot, with the devices attached (MACBOX_VIDEO=on). Running it again changes nothing.
set -euo pipefail
K="$(cd "$1" && pwd)"
NAMES=(AppleM2ScalerParavirtDriver AppleVideoToolboxParavirtualization VtWaitPatch)
for k in "${NAMES[@]}"; do [ -d "$K/$k.kext" ] || { echo "no $K/$k.kext" >&2; exit 1; }; done
M=$(mktemp -d /tmp/ocmnt.XXXX)
trap 'sudo umount "$M" 2>/dev/null; rmdir "$M"' EXIT
found=""
for part in ${2:-$(diskutil list | awk '/EFI/ {print $NF}')}; do
  sudo mount -t msdos "/dev/$part" "$M" 2>/dev/null || continue
  if [ -f "$M/EFI/OC/config.plist" ]; then found=$part; break; fi
  sudo umount "$M"
done
[ -n "$found" ] || { echo "no EFI partition with EFI/OC/config.plist" >&2; exit 1; }
P=/usr/libexec/PlistBuddy C="$M/EFI/OC/config.plist"
[ -f "$C.before-video" ] || sudo cp -p "$C" "$C.before-video"
n=0 have=""
while b=$($P -c "Print :Kernel:Add:$n:BundlePath" "$C" 2>/dev/null); do have="$have $b"; n=$((n + 1)); done
for k in "${NAMES[@]}"; do
  sudo rm -rf "$M/EFI/OC/Kexts/$k.kext"; sudo cp -R "$K/$k.kext" "$M/EFI/OC/Kexts/"
  case " $have " in *" $k.kext "*) echo "$k: in config.plist already"; continue ;; esac
  sudo $P -c "Add :Kernel:Add:$n dict" \
    -c "Add :Kernel:Add:$n:Arch string x86_64" \
    -c "Add :Kernel:Add:$n:BundlePath string $k.kext" \
    -c "Add :Kernel:Add:$n:Comment string reims-vgpu-video $k" \
    -c "Add :Kernel:Add:$n:Enabled bool true" \
    -c "Add :Kernel:Add:$n:ExecutablePath string Contents/MacOS/$k" \
    -c "Add :Kernel:Add:$n:MaxKernel string 25.99.99" \
    -c "Add :Kernel:Add:$n:MinKernel string 25.6.0" \
    -c "Add :Kernel:Add:$n:PlistPath string Contents/Info.plist" "$C"
  echo "$k: added as Kernel.Add.$n"; n=$((n + 1))
done
plutil -lint "$C" >/dev/null
sync
echo "OpenCore on $found has the video kexts; they load at the next boot"
