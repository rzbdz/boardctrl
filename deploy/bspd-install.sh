#!/usr/bin/env bash
# Install (or update) boardctrld on this machine from an unpacked tree, with a config generated from what this
# machine is: no site directory to write by hand. Run as root, from the tree it installs (boardlab runs it over ssh).
#
#   deploy/bspd-install.sh --id <host id> [--port 8765] [--bind 127.0.0.1] [--token <token>] [--board auto|<board>]
#                          [--relay auto|sysfs|gpiod|none] [--site <dir>] [--prefix /opt/boardctrl]
#   deploy/bspd-install.sh --uninstall [--purge]
#
# The file keeps its old name (boardlab on older lines runs it); an older bspd install on the machine is taken over:
# its service stops, its site moves to /etc/boardctrl/site, its state is kept.
#
# Idempotent: each run rebuilds the venv beside the old one, swaps it in, rewrites the unit and restarts the service.
# The host config goes to <site>/hosts/<id>.yaml (default site /etc/boardctrl/site, made here; an existing site is
# kept and only that file rewritten). The token goes to /etc/boardctrl/boardctrld.env (0600). Prints
# `BOARDCTRLD-READY <id> <port> <version>` and, for older readers, `BSPD-READY <id> <port> <version>`.
# Each stage starts with a line `--> [n/N] <what>`, and long ones print a line at least every few seconds, so whoever
# watches the install (boardlab's page) sees how far it is.
set -euo pipefail
cd "$(dirname "$0")/.."
ID="" PORT=8765 BIND=127.0.0.1 TOKEN="" BOARD=auto RELAY=auto SITE=/etc/boardctrl/site PREFIX=/opt/boardctrl CA="" UNINSTALL="" PURGE=""
while [[ $# -gt 0 ]]; do
  case $1 in
    --id) ID=$2; shift 2 ;; --port) PORT=$2; shift 2 ;; --bind) BIND=$2; shift 2 ;; --token) TOKEN=$2; shift 2 ;;
    --board) BOARD=$2; shift 2 ;; --relay) RELAY=$2; shift 2 ;; --site) SITE=$2; shift 2 ;; --prefix) PREFIX=$2; shift 2 ;; --ca) CA=$2; shift 2 ;;
    --uninstall) UNINSTALL=1; shift ;; --purge) PURGE=1; shift ;;
    *) echo "unknown option $1" >&2; exit 2 ;;
  esac
done
[[ $(id -u) == 0 ]] || { echo "run as root (it installs a system service and drives GPIO)" >&2; exit 1; }

# An older install (bspd under /opt/bspd, unit bsp-hostd, site /etc/bsp/site) is taken over, not duplicated: its
# service is stopped and disabled, its site and state are kept (the site moves to the new place), and the old
# unit is removed. Its data stays where it was until every command has been run with the new names once.
OLD_PREFIX=/opt/bspd OLD_SITE=/etc/bsp/site OLD_UNIT=bsp-hostd.service
TAKEOVER=""
if [[ -f /etc/systemd/system/$OLD_UNIT || -d $OLD_PREFIX/current || -d /etc/bsp ]]; then
  TAKEOVER=1
  echo "--> taking over the older bspd install (site $OLD_SITE, $OLD_PREFIX)"
  systemctl disable --now bsp-hostd.service 2>/dev/null || systemctl disable --now bsp-hostd 2>/dev/null || true
  if [[ ! -d $SITE && -d $OLD_SITE ]]; then
    mkdir -p "$(dirname "$SITE")"
    cp -a "$OLD_SITE" "$SITE"
    echo "--> site kept and copied to $SITE"
  fi
fi

if [[ -n $UNINSTALL ]]; then
  for u in boardctrld.service bsp-hostd.service; do systemctl disable --now "$u" 2>/dev/null || true; rm -f "/etc/systemd/system/$u"; done
  systemctl daemon-reload
  for b in boardctrl boardctrld bsp bsp-hostd smtty serial-mux; do [[ $(readlink -f /usr/local/bin/$b 2>/dev/null) == $PREFIX/* ]] && rm -f /usr/local/bin/$b; done
  rm -rf "$PREFIX" /etc/boardctrl/boardctrld.env /etc/bsp/hostd.env
  [[ -n $PURGE ]] && rm -rf /etc/boardctrl /etc/bsp /var/lib/bsp
  echo "BOARDCTRLD-REMOVED"
  echo "BSPD-REMOVED"      # what older boardds parse
  exit 0
fi
[[ $ID =~ ^[A-Za-z0-9][A-Za-z0-9_.-]{0,62}$ ]] || { echo "--id <host id> is required (letters, digits, _ . -)" >&2; exit 2; }
command -v python3 >/dev/null || { echo "python3 is required (>= 3.10)" >&2; exit 1; }
python3 -c 'import sys; sys.exit(sys.version_info < (3, 10))' || { echo "python3 >= 3.10 is required, found $(python3 -V)" >&2; exit 1; }
python3 -c 'import venv, ensurepip' 2>/dev/null || { echo "python3-venv is missing: apt install python3-venv" >&2; exit 1; }

VERSION=$(cat REVISION 2>/dev/null || git rev-parse --short HEAD 2>/dev/null || echo dev)
echo "--> bspd $VERSION as $ID on $BIND:$PORT"
export PYTHONUNBUFFERED=1
# A long command, with a line every few seconds while it runs quiet.
ticking() {
  "$@" & local pid=$! n=0
  while kill -0 "$pid" 2>/dev/null; do sleep 1; n=$((n + 1)); (( n % 5 )) || echo "    ... ${n}s"; done
  wait "$pid"
}
# pip without progress bars, a line per package (its errors still go to stderr).
pip_install() {
  "$NEW/venv/bin/pip" install --disable-pip-version-check --progress-bar off "$@" \
    | awk '/^(Collecting|Building wheel|Installing collected|Successfully installed)/ { print "    " $0; fflush() }'
}

echo "--> [1/6] looking at this machine (board, GPIO header)"

# What this machine is: its board (for the GPIO header) from the device tree, matched against the packs' boards.
MODEL=$(tr -d '\0' < /proc/device-tree/model 2>/dev/null || true)
if [[ $BOARD == auto ]]; then
  BOARD=$(MODEL="$MODEL" python3 - <<'EOF'
import os, re, pathlib
model = os.environ.get("MODEL", "").strip().lower()
best = ""
for f in sorted(pathlib.Path("packs").glob("*/boards/*.yaml")):
    text = f.read_text()
    name = (re.search(r"^name:\s*(.+)$", text, re.M) or [None, ""])[1].strip().strip('"').lower()
    if model and name and (name in model or model in name):
        best = f.stem
        break
print(best)
EOF
)
  [[ -n $BOARD ]] && echo "--> board: $BOARD ($MODEL)" || echo "--> no known GPIO header for '${MODEL:-this machine}': no relay lines (human reset only)"
fi
[[ $BOARD == none ]] && BOARD=""
if [[ $RELAY == auto ]]; then
  if [[ -z $BOARD ]]; then RELAY=none; elif ls /dev/gpiochip* >/dev/null 2>&1 && python3 -c 'import gpiod' 2>/dev/null; then RELAY=gpiod; else RELAY=sysfs; fi
fi

# The code: a fresh venv beside the running one, swapped in when it is ready.
mkdir -p "$PREFIX"
NEW=$PREFIX/releases/$VERSION.$$
rm -rf "$NEW"; mkdir -p "$NEW"
cp -a . "$NEW/app"
echo "--> [2/6] creating the Python environment (a few minutes the first time)"
ticking python3 -m venv "$NEW/venv"
ticking pip_install -U "pip>=24" || true
echo "--> [3/6] installing bsp"
EXTRAS=server; [[ $RELAY == gpiod ]] && EXTRAS=server,gpiod
# Editable, from the tree beside it: bsp finds its packs (board pin maps, SoC profiles) next to its source.
ticking pip_install -e "$NEW/app[$EXTRAS]"
[[ -f "$NEW/app/third_party/serial-mux/pyproject.toml" ]] && ticking pip_install -e "$NEW/app/third_party/serial-mux"
ln -sfn "$NEW" "$PREFIX/current.new" && mv -Tf "$PREFIX/current.new" "$PREFIX/current"
for b in bsp smtty serial-mux; do [[ -x $PREFIX/current/venv/bin/$b ]] && ln -sfn "$PREFIX/current/venv/bin/$b" /usr/local/bin/$b; done
# Old releases: keep the one before, drop the rest.
ls -1dt "$PREFIX"/releases/* 2>/dev/null | tail -n +3 | xargs -r rm -rf

# The config, generated: the site's own hosts file for this id.
echo "--> [4/6] writing the config"
mkdir -p "$SITE/hosts" /etc/boardctrl /var/lib/bsp
CONF=$SITE/hosts/$ID.yaml
{
  echo "# Generated by bspd-install for $ID; boardlab rewrites it on every install. Changes go in boardlab's settings."
  echo "host: {id: $ID, description: \"${MODEL:-bsp host} ($ID)\"}"
  echo "server:"
  # Reinstalling upgrades bspd, not the site's rendezvous settings. Keep any existing announcement verbatim.
  "$NEW/venv/bin/python" - "$CONF" "$BIND" "$PORT" <<'EOF'
import pathlib, sys, yaml
p = pathlib.Path(sys.argv[1])
server = {"bind": sys.argv[2], "port": int(sys.argv[3]), "state_dir": "/var/lib/bsp"}
if p.is_file():
    announce = (yaml.safe_load(p.read_text()) or {}).get("server", {}).get("announce")
    if announce:
        server["announce"] = announce
print(yaml.safe_dump({"server": server}, default_flow_style=False), end="")
EOF
  echo "modules:"
  if [[ -n $BOARD && $RELAY != none ]]; then
    echo "  gpio: {board: $BOARD, backend: $RELAY, hw_timeout_s: 3.0, pinmux: true, max_pulse_ms: 60000, protected: [], channels: {}, aliases: {}}"
  fi
  echo "  sdk: {}"
  echo "  dut:"
  echo "    smtty: $PREFIX/current/venv/bin/smtty"
  echo "    serial_mux: $PREFIX/current/venv/bin/serial-mux"
  # serial-mux keeps its sockets, runtime and logs under ~/.serial-mux of the user it runs as; bspd finds them beside
  # serial_log_dir, so that stays serial-mux's own.
  echo "    serial_log_dir: $(getent passwd root | cut -d: -f6)/.serial-mux/logs"
  echo "    fastboot: fastboot"
  # Site hooks (possibly a scp/flashing step) take precedence over the generic pack hooks below them.
  # Without this, a synced flash recipe names a hook the running host cannot find after every reinstall.
  echo "    hook_dirs: [$SITE/hooks]"
  echo "    probe: {watch_s: 3.0, pulse_ms: 1000}"
} > "$CONF.new"
mv -f "$CONF.new" "$CONF"
# A token, if given (boardlab gives one); an existing one is kept otherwise.
mkdir -p /etc/boardctrl
if [[ -n $TOKEN ]]; then umask 077; printf 'BOARDCTRL_TOKEN=%s\nBSP_TOKEN=%s\n' "$TOKEN" "$TOKEN" > /etc/boardctrl/boardctrld.env; fi
[[ -f /etc/boardctrl/boardctrld.env ]] || { umask 077; : > /etc/boardctrl/boardctrld.env; }
chmod 600 /etc/boardctrl/boardctrld.env
# A certificate this host must trust (the network's own CA, say): installed beside the system's before anything is
# downloaded from hosts it signs.
if [[ -n $CA ]]; then
  name="boardlab-extra-$(basename "$CA" .crt).crt"
  if [[ $CA == http://* || $CA == https://* ]]; then
    curl -fsSL --connect-timeout 15 "$CA" -o "/usr/local/share/ca-certificates/$name" || { echo "!! could not fetch the certificate $CA" >&2; exit 1; }
  else
    cp "$CA" "/usr/local/share/ca-certificates/$name"
  fi
  update-ca-certificates >/dev/null
  echo "--> certificate: $(basename "$CA") is trusted here"
fi
command -v fastboot >/dev/null || echo "WARNING: fastboot is not installed: boards cannot be loaded or flashed (apt install fastboot)" >&2

echo "--> [5/6] starting boardctrld"
cat > /etc/systemd/system/boardctrld.service <<EOF
[Unit]
Description=boardctrld: this machine's boards (reset lines, consoles, download ports, debug), driven by boardlab
After=network-online.target
Wants=network-online.target

[Service]
Type=simple
WorkingDirectory=$PREFIX/current/app
ExecStart=$PREFIX/current/venv/bin/boardctrld --config $CONF
Environment=PYTHONUNBUFFERED=1
Environment=PATH=$PREFIX/tools:/usr/local/sbin:/usr/local/bin:/usr/sbin:/usr/bin:/sbin:/bin
Environment=BOARDCTRL_SITE=$SITE
# Read too, so a hook or a program written for the old name still sees the site.
Environment=BSP_SITE=$SITE
EnvironmentFile=-/etc/boardctrl/boardctrld.env
EnvironmentFile=-/etc/bsp/hostd.env
Restart=always
RestartSec=2
TimeoutStopSec=10

[Install]
WantedBy=multi-user.target
EOF
systemctl daemon-reload
systemctl enable -q boardctrld.service
systemctl restart boardctrld.service
# The old unit is gone once the new one serves: a machine does not answer to two names at once.
if [[ -n $TAKEOVER ]]; then rm -f /etc/systemd/system/bsp-hostd.service; systemctl daemon-reload; fi
echo "--> [6/6] checking health"
for _ in $(seq 1 30); do
  if curl -fsS -m 2 "http://127.0.0.1:$PORT/api/v1/health" >/dev/null 2>&1 || python3 -c "import urllib.request,sys; urllib.request.urlopen('http://127.0.0.1:$PORT/api/v1/health', timeout=2)" 2>/dev/null; then
    echo "BOARDCTRLD-READY $ID $PORT $VERSION"
    echo "BSPD-READY $ID $PORT $VERSION"      # the line older boardds parse
    exit 0
  fi
  sleep 1
done
echo "!! boardctrld did not come up" >&2
journalctl -u boardctrld --no-pager -n 30 >&2 || true
exit 1
