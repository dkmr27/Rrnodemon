#!/bin/sh
#
# Install or update Rrnodemon. Run as root from this folder:
#   sudo ./install.sh              install, or update an existing install
#   sudo ./install.sh --uninstall  remove it (keeps /etc/rrnodemon and the user)
#
# It installs rrnodemon.py in /opt/rrnodemon, a config in /etc/rrnodemon
# (never overwriting one already there), a user rrnodemon that may use the
# serial ports, and a service: systemd where there is one, else the
# Slackware rc script. rnsd's own config is left to you; the end of the
# output says what to change.
#
# For testing: DESTDIR=/some/dir installs the files under that folder,
# DRY_RUN=1 prints the user, package and service commands instead of
# running them, and INIT=systemd|slackware|none picks the service type.

set -e

DESTDIR=${DESTDIR:-}
DRY_RUN=${DRY_RUN:-}
OPT=$DESTDIR/opt/rrnodemon
ETC=$DESTDIR/etc/rrnodemon
USER_NAME=rrnodemon
HERE=$(cd "$(dirname "$0")" && pwd)

say() { echo "==> $*"; }
warn() { echo "warning: $*" >&2; }
die() { echo "error: $*" >&2; exit 1; }
# Commands that change the system outside the installed files
run() {
  if [ -n "$DRY_RUN" ]; then echo "    (dry run) $*"; else "$@"; fi
}

if [ -z "$DRY_RUN" ] && [ "$(id -u)" != 0 ]; then
  die "run it as root: sudo $0 $*"
fi
[ -r "$HERE/rrnodemon.py" ] || die "run it from the Rrnodemon folder (rrnodemon.py not found)"

# systemd, or Slackware-style rc scripts (INIT=systemd|slackware|none to choose)
if [ -n "$INIT" ]; then
  :
elif [ -d /run/systemd/system ]; then
  INIT=systemd
elif [ -d /etc/rc.d ] && [ -f /etc/slackware-version -o -f /etc/rc.d/rc.M ]; then
  INIT=slackware
else
  INIT=none
fi

uninstall() {
  case $INIT in
    systemd)
      run systemctl disable --now rrnodemon 2>/dev/null || true
      rm -f "$DESTDIR/etc/systemd/system/rrnodemon.service"
      run systemctl daemon-reload
      ;;
    slackware)
      if [ -x "$DESTDIR/etc/rc.d/rc.rrnodemon" ]; then
        run "$DESTDIR/etc/rc.d/rc.rrnodemon" stop || true
      fi
      rm -f "$DESTDIR/etc/rc.d/rc.rrnodemon" "$DESTDIR/etc/logrotate.d/rrnodemon"
      for f in rc.local rc.local_shutdown; do
        if [ -f "$DESTDIR/etc/rc.d/$f" ]; then sed -i '/rc\.rrnodemon/d' "$DESTDIR/etc/rc.d/$f"; fi
      done
      ;;
  esac
  rm -rf "$OPT"
  say "Rrnodemon removed. Left in place: $ETC (config, password) and the user $USER_NAME."
  say "Point rnsd's RNode interface back at the serial port and restart rnsd."
}

if [ "$1" = "--uninstall" ]; then
  uninstall
  exit 0
elif [ -n "$1" ]; then
  echo "usage: $0 [--uninstall]"
  exit 1
fi

# ---- Python and its modules
command -v python3 >/dev/null 2>&1 || die "python3 not found"
python3 - <<'EOF' || die "Python 3.7 or later is needed"
import sys
sys.exit(sys.version_info < (3, 7))
EOF
missing=""
python3 -c "import serial" 2>/dev/null || missing="$missing pyserial"
python3 -c "import paho.mqtt.client" 2>/dev/null || missing="$missing paho-mqtt"
if [ -n "$missing" ]; then
  if command -v apt-get >/dev/null 2>&1; then
    say "Installing Python modules:$missing"
    pkgs=""
    case $missing in *pyserial*) pkgs="$pkgs python3-serial" ;; esac
    case $missing in *paho*) pkgs="$pkgs python3-paho-mqtt" ;; esac
    run apt-get install -y $pkgs
  else
    die "Python modules missing:$missing. Install them (e.g. pip3 install$missing) and run this again."
  fi
fi

# ---- The user, in the group that owns the serial ports
port_group=""
for dev in /dev/ttyACM* /dev/ttyUSB*; do
  if [ -e "$dev" ]; then port_group=$(stat -c %G "$dev"); break; fi
done
if [ -z "$port_group" ]; then
  for g in dialout uucp; do
    if getent group "$g" >/dev/null 2>&1; then port_group=$g; break; fi
  done
fi
if id "$USER_NAME" >/dev/null 2>&1; then
  say "User $USER_NAME exists"
  if [ -n "$port_group" ] && ! id -nG "$USER_NAME" | tr ' ' '\n' | grep -qx "$port_group"; then
    say "Adding $USER_NAME to group $port_group (serial ports)"
    run usermod -a -G "$port_group" "$USER_NAME"
  fi
else
  say "Adding user $USER_NAME${port_group:+, in group $port_group (serial ports)}"
  run useradd -r -M -d /nonexistent -s /bin/false ${port_group:+-G "$port_group"} "$USER_NAME"
fi
[ -n "$port_group" ] || warn "could not tell which group owns the serial ports; add $USER_NAME to it by hand"

# ---- Files
updating=""
[ -f "$OPT/rrnodemon.py" ] && updating=1
say "Installing $OPT/rrnodemon.py"
mkdir -p "$OPT" "$ETC"
cp "$HERE/rrnodemon.py" "$OPT/rrnodemon.py"
chmod 644 "$OPT/rrnodemon.py"
new_config=""
if [ -f "$ETC/rrnodemon.conf" ]; then
  say "Keeping $ETC/rrnodemon.conf"
else
  say "Installing $ETC/rrnodemon.conf (edit it next)"
  cp "$HERE/rrnodemon.conf.example" "$ETC/rrnodemon.conf"
  new_config=1
fi
chmod 640 "$ETC/rrnodemon.conf"
run chgrp "$USER_NAME" "$ETC/rrnodemon.conf"
if [ -f "$ETC/mqtt_password" ]; then
  chmod 600 "$ETC/mqtt_password"
  run chown "$USER_NAME" "$ETC/mqtt_password"
fi

# ---- Service. A new install waits for the config to be edited; an update
# restarts what is running.
case $INIT in
  systemd)
    say "Installing the systemd service"
    unit=$DESTDIR/etc/systemd/system/rrnodemon.service
    mkdir -p "$(dirname "$unit")"
    # The user is already in the serial port group
    grep -v '^SupplementaryGroups=\|^# The user needs the serial port' "$HERE/rrnodemon.service" > "$unit"
    run systemctl daemon-reload
    run systemctl enable rrnodemon
    if [ -z "$new_config" ]; then run systemctl restart rrnodemon; fi
    start_cmd="systemctl start rrnodemon"
    log_cmd="journalctl -u rrnodemon -f"
    ;;
  slackware)
    say "Installing /etc/rc.d/rc.rrnodemon"
    rc=$DESTDIR/etc/rc.d
    mkdir -p "$rc"
    cp "$HERE/rc.rrnodemon" "$rc/rc.rrnodemon"
    chmod 755 "$rc/rc.rrnodemon"
    for pair in "rc.local:start" "rc.local_shutdown:stop"; do
      f=$rc/${pair%%:*}
      action=${pair#*:}
      if [ ! -f "$f" ]; then
        printf '#!/bin/sh\n' > "$f"
        chmod 755 "$f"
      fi
      if ! grep -q 'rc\.rrnodemon' "$f"; then
        say "Adding rc.rrnodemon $action to ${f#$DESTDIR}"
        printf 'if [ -x /etc/rc.d/rc.rrnodemon ]; then /etc/rc.d/rc.rrnodemon %s; fi\n' "$action" >> "$f"
      fi
    done
    if [ -d "$DESTDIR/etc/logrotate.d" ]; then
      printf '/var/log/rrnodemon.log {\n  weekly\n  rotate 4\n  compress\n  missingok\n  copytruncate\n}\n' \
        > "$DESTDIR/etc/logrotate.d/rrnodemon"
    fi
    if [ -z "$new_config" ]; then run "$rc/rc.rrnodemon" restart; fi
    start_cmd="/etc/rc.d/rc.rrnodemon start"
    log_cmd="tail -f /var/log/rrnodemon.log"
    ;;
  *)
    warn "neither systemd nor Slackware rc scripts found: no service installed"
    start_cmd="sudo -u $USER_NAME python3 /opt/rrnodemon/rrnodemon.py -c /etc/rrnodemon/rrnodemon.conf"
    log_cmd=""
    ;;
esac

# ---- What is left to do
echo
if [ -n "$updating" ] && [ -z "$new_config" ]; then
  say "Rrnodemon updated and restarted. Log: $log_cmd"
  exit 0
fi
cat <<EOF
Rrnodemon is installed. To finish:

1. Edit /etc/rrnodemon/rrnodemon.conf: the RNode's port, the MQTT broker,
   and the observer id.
2. If the broker needs a password, put it in a file of its own:
     sh -c 'umask 077; cat > /etc/rrnodemon/mqtt_password'
     chown $USER_NAME /etc/rrnodemon/mqtt_password
   and set password_file = /etc/rrnodemon/mqtt_password in the config.
3. Start it:  $start_cmd
4. In rnsd's config, change the RNode interface's port to
     port = tcp://127.0.0.1
   keeping its radio settings, and restart rnsd.
${log_cmd:+
Log: $log_cmd}
EOF
