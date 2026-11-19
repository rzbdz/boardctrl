#!/usr/bin/env bash
# Install (or update) boardctrld on this machine from an unpacked tree, with a config generated from what this
# machine is: no site directory to write by hand. Run as root, from the tree it installs (boardlab runs it over ssh).
#
#   deploy/bspd-install.sh --id <host id> [--port 8765] [--bind 127.0.0.1] [--token <token> | --no-token] [--board auto|<board>]
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
ID="" PORT=8765 BIND=127.0.0.1 TOKEN="" BOARD=auto RELAY=auto SITE=/etc/boardctrl/site PREFIX=/opt/boardctrl CA="" UNINSTALL="" PURGE="" SITE_HOSTS="" NO_TOKEN=""
while [[ $# -gt 0 ]]; do
  case $1 in
    --id) ID=$2; shift 2 ;; --port) PORT=$2; shift 2 ;; --bind) BIND=$2; shift 2 ;; --token) TOKEN=$2; shift 2 ;;
    --board) BOARD=$2; shift 2 ;; --relay) RELAY=$2; shift 2 ;; --site) SITE=$2; shift 2 ;; --prefix) PREFIX=$2; shift 2 ;; --ca) CA=$2; shift 2 ;;
    --site-hosts) SITE_HOSTS=$2; shift 2 ;;
    --no-token) NO_TOKEN=1; shift ;;
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
  # The token it ran with is the one this install keeps. Whoever asks the host reads both files, and the two names have
  # different precedence, so the merged file names the one token under both names: the daemon and a sync then take the
  # same one. The new file's own token wins (an install was given one); the old file's fills in where it has none.
  if [[ -f /etc/bsp/hostd.env ]]; then
    mkdir -p /etc/boardctrl
    from_old() { (set -a; . /etc/bsp/hostd.env; set +a; printf '%s' "${BOARDCTRL_TOKEN:-${BSP_TOKEN:-}}"); }
    token=$( (set -a; . /etc/boardctrl/boardctrld.env 2>/dev/null; set +a; printf '%s' "${BOARDCTRL_TOKEN:-${BSP_TOKEN:-}}") )
    [[ -n $token ]] || token=$(from_old)
    if [[ -n $token ]]; then
      grep -qE '^BOARDCTRL_TOKEN=' /etc/boardctrl/boardctrld.env 2>/dev/null || printf 'BOARDCTRL_TOKEN=%s\n' "$token" >> /etc/boardctrl/boardctrld.env
      grep -qE '^BSP_TOKEN=' /etc/boardctrl/boardctrld.env 2>/dev/null || printf 'BSP_TOKEN=%s\n' "$token" >> /etc/boardctrl/boardctrld.env
    elif [[ -s /etc/bsp/hostd.env ]]; then
      cp -a /etc/bsp/hostd.env /etc/boardctrl/boardctrld.env
    fi
    chmod 600 /etc/boardctrl/boardctrld.env
    # Gone only once the new place holds a token: otherwise the old file is still the only one that has it.
    grep -qE '^(BOARDCTRL_TOKEN|BSP_TOKEN)=' /etc/boardctrl/boardctrld.env && rm -f /etc/bsp/hostd.env
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

# What this machine is: its board (for the GPIO header) from the device tree, matched against the platforms' boards.
MODEL=$(tr -d '\0' < /proc/device-tree/model 2>/dev/null || true)
if [[ $BOARD == auto ]]; then
  BOARD=$(MODEL="$MODEL" python3 - <<'EOF'
import os, re, pathlib
model = os.environ.get("MODEL", "").strip().lower()
best = ""
for f in sorted(pathlib.Path("platforms").glob("*/boards/*.yaml")):
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
# An interpreter that has the gpiod bindings: the program's own environment gets them, but a site provider that runs a
# wrapper with the host's python needs that python to have them, and the choice must not depend on whichever python3 is
# first on PATH (a tool's own venv, say, which is not the machine's python).
SYSTEM_PYTHON=${SYSTEM_PYTHON:-/usr/bin/python3}
[[ -x $SYSTEM_PYTHON ]] || SYSTEM_PYTHON=$(command -v python3)
gpiod_python() {
  local candidate
  for candidate in "$SYSTEM_PYTHON" "$(command -v python3)"; do
    [[ -n $candidate && -x $candidate ]] || continue
    "$candidate" -c 'import gpiod' 2>/dev/null && { printf '%s\n' "$candidate"; return 0; }
  done
  return 1
}
if [[ $RELAY == auto ]]; then
  if [[ -z $BOARD ]]; then RELAY=none; elif ls /dev/gpiochip* >/dev/null 2>&1 && gpiod_python >/dev/null; then RELAY=gpiod; else RELAY=sysfs; fi
fi
# Saying which python has gpiod matters when it is not the machine's own: the program's environment is given the
# bindings either way, but a provider that runs the system python has to know to install them there.
if [[ $RELAY == gpiod ]]; then
  host_python=$(gpiod_python || true)
  if [[ -n $host_python && $host_python != "$SYSTEM_PYTHON" ]]; then
    echo "--> gpiod is in $host_python, not $SYSTEM_PYTHON: a provider that runs the system python needs it there"
  elif [[ -n $host_python ]]; then
    echo "--> gpiod: $host_python has it, and the program's own environment gets it"
  fi
fi

# The code: a fresh venv beside the running one, swapped in when it is ready.
mkdir -p "$PREFIX"
NEW=$PREFIX/releases/$VERSION.$$
rm -rf "$NEW"; mkdir -p "$NEW"
cp -a . "$NEW/app"
echo "--> [2/6] creating the Python environment (a few minutes the first time)"
ticking python3 -m venv "$NEW/venv"
ticking pip_install -U "pip>=24" || true
echo "--> [3/6] installing boardctrl"
EXTRAS=server; [[ $RELAY == gpiod ]] && EXTRAS=server,gpiod
# Editable, from the tree beside it: boardctrl finds its platforms (board pin maps, SoC facts) beside its source.
ticking pip_install -e "$NEW/app[$EXTRAS]"
[[ -f "$NEW/app/providers/smtty/serial-mux/pyproject.toml" ]] && ticking pip_install -e "$NEW/app/providers/smtty/serial-mux"
ln -sfn "$NEW" "$PREFIX/current.new" && mv -Tf "$PREFIX/current.new" "$PREFIX/current"
for b in boardctrl boardctrld bsp bsp-hostd smtty serial-mux; do [[ -x $PREFIX/current/venv/bin/$b ]] && ln -sfn "$PREFIX/current/venv/bin/$b" /usr/local/bin/$b; done
# The old names are links to the same program, so a script or a unit written before the rename keeps working.
# Old releases: keep the one before, drop the rest.
ls -1dt "$PREFIX"/releases/* 2>/dev/null | tail -n +3 | xargs -r rm -rf

# The config, generated: the site's own hosts file for this id.
echo "--> [4/6] writing the config"
mkdir -p "$SITE/hosts" /etc/boardctrl /var/lib/bsp
CONF=$SITE/hosts/$ID.yaml
# What this installer knows about the machine is merged into what the file already holds. Reinstalling upgrades the
# program: it does not take the site's configuration away — neither the rendezvous settings nor a provider's own section
# (`providers.ec`, say), which this program does not register and so must not rewrite.
"$NEW/venv/bin/python" - "$CONF" "$ID" "${MODEL:-boardctrl host}" "$BIND" "$PORT" "$BOARD" "$RELAY" \
  "$PREFIX/current/venv/bin/smtty" "$PREFIX/current/venv/bin/serial-mux" \
  "$(getent passwd root | cut -d: -f6)/.serial-mux/logs" "$SITE/hooks" "${SITE_HOSTS:-}" <<'EOF'
import pathlib, sys, yaml

path, ident, model, bind, port, board, relay, smtty, serial_mux, log_dir, hooks, site_hosts = sys.argv[1:]
p = pathlib.Path(path)
old = (yaml.safe_load(p.read_text()) or {}) if p.is_file() else {}
cfg = dict(old)
cfg["host"] = {"id": ident, "description": f"{model} ({ident})"}
server = dict(cfg.get("server") or {})
server.update({"bind": bind, "port": int(port), "state_dir": "/var/lib/bsp"})
cfg["server"] = server
generated = {}
if board and relay != "none":
    generated["gpio"] = {"board": board, "backend": relay, "hw_timeout_s": 3.0, "pinmux": True,
                         "max_pulse_ms": 60000, "protected": [], "channels": {}, "aliases": {}}
generated["sdk"] = {}
# serial-mux keeps its sockets, runtime and logs under ~/.serial-mux of the user it runs as; bspd finds them beside
# serial_log_dir, so that stays serial-mux's own.
# Site hooks (possibly a scp/flashing step) take precedence over the generic platform hooks below them: without this,
# a synced flash recipe names a hook the running host cannot find after every reinstall.
# What the host loads is `providers:`; a file written before the name changed says `modules:`, and keeps saying it.
KEY = "providers" if "providers" in cfg or "modules" not in cfg else "modules"
modules = dict(cfg.get(KEY) or {})
generated["dut"] = {"smtty": smtty, "serial_mux": serial_mux, "serial_log_dir": log_dir, "fastboot": "fastboot",
                    "hook_dirs": [hooks], "probe": {"watch_s": 3.0, "pulse_ms": 1000}}
for name, block in generated.items():
    merged = dict(modules.get(name) or {}) if isinstance(modules.get(name), dict) else {}
    merged.update(block)
    modules[name] = merged
cfg[KEY] = modules
# The site's copy of this host's config (`--site-hosts`) may carry settings for providers the site installs — an EC's
# pins and ports, say. A provider's section travels with its code, in the site's repository; this program does not know
# those providers, so it takes them whole and never rewrites them. The machine's own section wins when it has one: a
# local setting is not silently replaced by what the site says.
if site_hosts:
    theirs = pathlib.Path(site_hosts) / f"{ident}.yaml"
    if theirs.is_file():
        try:
            document = yaml.safe_load(theirs.read_text()) or {}
        except (OSError, ValueError):
            document = {}
        their_modules = (document.get("providers") or document.get("modules")) if isinstance(document, dict) else None
        for name, block in (their_modules or {}).items() if isinstance(their_modules, dict) else []:
            if name in generated or not isinstance(block, dict):
                continue
            own = modules.get(name)
            if isinstance(own, dict) and set(own) - {"enabled"}:
                continue
            modules[name] = dict(block)
        cfg[KEY] = modules
        # Where the host tells the registry it is (`server.announce`): without it the host never announces, the
        # registry sees it go stale and stops sending it catalog changes. The site's copy fills it in when the machine's
        # own file has none.
        their_server = document.get("server") if isinstance(document, dict) else None
        if isinstance(their_server, dict) and isinstance(their_server.get("announce"), dict) and not server.get("announce"):
            server["announce"] = dict(their_server["announce"])
            cfg["server"] = server
tmp = p.with_name(p.name + ".new")
tmp.write_text("# Generated by bspd-install for %s; boardlab rewrites the parts it knows on every install and keeps\n"
               "# the rest (a site's provider section, its registry settings) as the file has them.\n%s"
               % (ident, yaml.safe_dump(cfg, default_flow_style=False, sort_keys=False)), encoding="utf-8")
tmp.replace(p)
EOF
# A token, if given (boardlab gives one); an existing one is kept otherwise.
mkdir -p /etc/boardctrl
if [[ -n $TOKEN ]]; then umask 077; printf 'BOARDCTRL_TOKEN=%s\nBSP_TOKEN=%s\n' "$TOKEN" "$TOKEN" > /etc/boardctrl/boardctrld.env; fi
# No token at all (`--no-token`): every request is accepted, so keep the port where only trusted machines reach it.
if [[ -n $NO_TOKEN ]]; then sed -i -E '/^(BOARDCTRL_TOKEN|BSP_TOKEN)=/d' /etc/boardctrl/boardctrld.env 2>/dev/null || true; fi
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
EnvironmentFile=-/etc/bsp/hostd.env
# Read after the old file, so this install's token is the one that counts.
EnvironmentFile=-/etc/boardctrl/boardctrld.env
Restart=always
RestartSec=2
TimeoutStopSec=10

[Install]
WantedBy=multi-user.target
EOF
systemctl daemon-reload
systemctl enable -q boardctrld.service
# A restart does not lose a lock or a console (both live outside this process: leases in the state directory,
# consoles and sessions as their own systemd units), but a job runs in a thread of this process, so restarting
# mid-flash cuts the image off. Wait for one to finish instead of killing it; a job that never ends is a reason to
# stop and ask, not to guess. BOARDCTRL_UPGRADE_WAIT is how long to wait for a job (default: 30 minutes).
wait_for_jobs() {
  local port budget=${BOARDCTRL_UPGRADE_WAIT:-1800} seen=""
  port=$(sed -n 's/^  port: *\([0-9]*\).*/\1/p' "$CONF" | head -1); port=${port:-8765}
  for _ in $(seq 1 "$budget"); do
    seen=$(curl -fsS -m 2 "http://127.0.0.1:$port/api/v1/tools/job_list" 2>/dev/null \
           || python3 -c "import urllib.request;print(urllib.request.urlopen('http://127.0.0.1:$port/api/v1/tools/job_list',timeout=2).read().decode())" 2>/dev/null) || return 0
    echo "$seen" | grep -q '"status": *"running"' || return 0
    echo "--> a job is running on this host; waiting for it before restarting (ctrl-c stops this install)"
    sleep 1
  done
  echo "!! jobs are still running after ${budget}s: not restarting boardctrld (a flash would be cut off)." >&2
  echo "!! finish or cancel them (\`boardctrl job list\`, \`boardctrl cancel <board>\`), then run this installer again." >&2
  exit 1
}
wait_for_jobs
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
