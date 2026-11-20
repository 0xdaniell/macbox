#!/bin/bash
# Runs in macOS Recovery's Terminal, against the installed Data volume. `macbox recovery-fix --ssh`
# and `--user` type it there, after three lines that set MACBOX_USER, MACBOX_KEY and MACBOX_MKUSER.
# It is typed into `cat` (no shell sees it as it's typed), with comment lines and indentation left
# out: keep it to plain ASCII without tabs, and to tools Recovery has.
#
# Always: Metal's error mode (what plain `macbox recovery-fix` types), and the Keyboard Setup
#   Assistant pre-answered for QEMU's USB keyboard (ANSI).
# --ssh:  a first-boot LaunchDaemon. Once the user exists (Setup Assistant makes it), it writes
#   this host's SSH key into their authorized_keys, turns Remote Login on and removes itself,
#   so `macbox ssh-bootstrap` isn't needed.
# --user: also the administrator, made offline, with a password asked here (experimental).
set -eu
: "${MACBOX_USER:?}" "${MACBOX_KEY:?}" "${MACBOX_MKUSER:=0}" "${MACBOX_VOLUMES:=/Volumes}"
say() { echo "macbox: $*"; }

D=
for v in "$MACBOX_VOLUMES"/*Data; do
  [ -d "$v/private/var/db/dslocal/nodes/Default" ] && D=$v
done
[ -n "$D" ] || { say "no installed Data volume in $MACBOX_VOLUMES (is macOS installed?)"; exit 1; }
say "Data volume: $D"
P="$D/Library/Preferences"

# Metal's error mode (docs/setup.md, step 6).
defaults write "$P/.GlobalPreferences" METAL_ERROR_MODE -int 5
chmod 644 "$P/.GlobalPreferences.plist"
say "METAL_ERROR_MODE 5"

# The Keyboard Setup Assistant: QEMU's USB keyboard (vendor 1575, product 1) is ANSI (40).
KT="$P/com.apple.keyboardtype.plist"
if [ -e "$KT" ]; then say "keyboard type already set"
else
  /usr/libexec/PlistBuddy -c 'Add :keyboardtype dict' -c 'Add :keyboardtype:1-1575-0 integer 40' "$KT" >/dev/null
  chown 0:0 "$KT"; chmod 644 "$KT"
  say "keyboard type: ANSI"
fi

# The administrator, offline (--user).
if [ "$MACBOX_MKUSER" = 1 ]; then
  DS="$D/private/var/db/dslocal/nodes/Default"
  U="/Local/Default/Users/$MACBOX_USER"
  if dscl -f "$DS" localhost -read "$U" >/dev/null 2>&1; then say "user $MACBOX_USER exists already"; exit 1; fi
  if dscl -f "$DS" localhost -list /Local/Default/Users UniqueID | awk '$2 == 501 {f=1} END {exit f ? 0 : 1}'; then
    say "a user with UID 501 exists already"; exit 1
  fi
  while :; do
    printf 'macbox: password for %s: ' "$MACBOX_USER"; read -rs PW; echo
    printf 'macbox: again: '; read -rs PW2; echo
    [ -n "$PW" ] && [ "$PW" = "$PW2" ] && break
    say "empty, or the two differ; once more"
  done
  dscl -f "$DS" localhost -create "$U"
  dscl -f "$DS" localhost -create "$U" UserShell /bin/zsh
  dscl -f "$DS" localhost -create "$U" RealName "$MACBOX_USER"
  dscl -f "$DS" localhost -create "$U" UniqueID 501
  dscl -f "$DS" localhost -create "$U" PrimaryGroupID 20
  dscl -f "$DS" localhost -create "$U" NFSHomeDirectory "/Users/$MACBOX_USER"
  dscl -f "$DS" localhost -passwd "$U" "$PW"
  unset PW PW2
  dscl -f "$DS" localhost -append /Local/Default/Groups/admin GroupMembership "$MACBOX_USER"
  # A home from the User Template, and no first-login assistant. cocoon-macos reports that macOS 26
  # may still show Setup Assistant: if it does, go through it without making another user.
  H="$D/Users/$MACBOX_USER"; S=${D% - Data}
  mkdir -p "$H"
  if [ -d "$S/System/Library/User Template/Non_localized" ]; then
    cp -R "$S/System/Library/User Template/Non_localized/." "$H/"
    cp -R "$S/System/Library/User Template/English.lproj/." "$H/" 2>/dev/null || true
  fi
  touch "$H/.skipbuddy" "$D/private/var/db/.AppleSetupDone"
  chown -R 501:20 "$H"
  say "user $MACBOX_USER (UID 501, admin)"
fi

# The first-boot SSH daemon (--ssh). It waits for the user's home, so Setup Assistant can make them.
if [ -n "$MACBOX_KEY" ] && [ "$MACBOX_KEY" != none ]; then
  mkdir -p "$D/Library/Scripts" "$D/Library/LaunchDaemons"
  F="$D/Library/Scripts/macbox-firstboot.sh"
  cat >"$F" <<EOF
#!/bin/bash
# macbox: SSH for $MACBOX_USER at first boot (written in Recovery by macbox recovery-fix --ssh).
exec >>/var/log/macbox-firstboot.log 2>&1
until /usr/bin/dscl . -read "/Users/$MACBOX_USER" >/dev/null 2>&1 && [ -d "/Users/$MACBOX_USER" ]; do sleep 10; done
echo "\$(date) $MACBOX_USER exists"
H="/Users/$MACBOX_USER"; G=\$(/usr/bin/id -g "$MACBOX_USER")
mkdir -p "\$H/.ssh"
grep -qxF '$MACBOX_KEY' "\$H/.ssh/authorized_keys" 2>/dev/null || echo '$MACBOX_KEY' >>"\$H/.ssh/authorized_keys"
chown -R "$MACBOX_USER:\$G" "\$H/.ssh"; chmod 700 "\$H/.ssh"; chmod 600 "\$H/.ssh/authorized_keys"
/bin/launchctl enable system/com.openssh.sshd
/bin/launchctl bootstrap system /System/Library/LaunchDaemons/ssh.plist 2>/dev/null || true
/bin/launchctl print system/com.openssh.sshd >/dev/null && echo "\$(date) Remote Login on" || exit 1
rm -f /Library/LaunchDaemons/local.macbox.firstboot.plist "\$0"
EOF
  L="$D/Library/LaunchDaemons/local.macbox.firstboot.plist"
  cat >"$L" <<EOF
<?xml version="1.0" encoding="UTF-8"?>
<!DOCTYPE plist PUBLIC "-//Apple//DTD PLIST 1.0//EN" "http://www.apple.com/DTDs/PropertyList-1.0.dtd">
<plist version="1.0"><dict>
<key>Label</key><string>local.macbox.firstboot</string>
<key>ProgramArguments</key><array><string>/bin/bash</string><string>/Library/Scripts/macbox-firstboot.sh</string></array>
<key>RunAtLoad</key><true/>
</dict></plist>
EOF
  # launchd ignores a daemon that isn't root's or that others can write.
  chown 0:0 "$F" "$L"; chmod 755 "$F"; chmod 644 "$L"
  plutil -lint "$L" >/dev/null
  say "first-boot SSH for $MACBOX_USER"
fi
say "done"
