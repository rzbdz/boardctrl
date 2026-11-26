# Porting

boardctrl keeps hardware knowledge in data files and small hooks. Adding support for new hardware
rarely needs code.

## A new SoC

Create `socs/<soc>/soc.yaml` — in a shipped platform if the information is public, otherwise in your site:

```yaml
soc: soc-a
fingerprint:                     # boot-ROM console lines printed after a reset
  - "ROMA: boot start"
  - "ROMA: usb init"
download_mode:                   # how boardctrl recognises USB download (FDL) mode
  console: "download handler"    # console regex (optional)
  fastboot: "DFU download"       # fastboot product regex
usb_vids: ["1d6b"]               # vendor ids of the download/fastboot gadgets (probe filter)
images:                          # known-good bootloader images for probing (RAM only)
  fsbl: {file: fsbl.bin, sha256: "…"}
  uboot: {file: uboot.itb, sha256: "…"}
identify:                        # how probe reads a board's identity
  sequence:                      # stage an image, continue, wait for console/fastboot
    - {stage: fsbl, console: "SPL", fastboot: "Fastboot"}
    - {stage: uboot, console: "U-Boot 20", fastboot: "Fastboot"}
  fields:                        # regexes over the console output that follows
    product: "board_name = (.*?)\\s*$"
    serial: "serial# = (.*?)\\s*$"
    mem_gib: "DRAM:\\s*(\\d+) GiB"
  placeholders: {serial: ["0", "none"]}   # values meaning "not programmed"
  name: [product, mem, serial]            # parts of the proposed board name
media_boot:                      # optional: how a board whose FDL strap is held down boots its media
  name: rom-handoff              # shown in `boardctrl dut status`
  steps:                         # run after a reset into download mode; recipe steps (sdk.md)
    - {do: check, serial: "strap: nor"}            # the boot ROM banner: a board these steps suit
    - {do: getvar, name: rom-version, match: "^2"}
    - {do: stage, image: handoff}                  # a key in images, verified by sha256
    - {do: continue}
    - {do: wait, serial: "SPL 20", timeout: 60}
```

- `fingerprint` lets `boardctrl probe` tell SoCs apart; pick lines that appear on every cold boot and
  are specific to the boot ROM.
- `download_mode` is used by every `enter-fdl` step and by the human reset backend's
  auto-detection.
- Probe images are looked up in every layer, highest first, and checked against `sha256`. Ship
  only images you may redistribute in a shipped platform; otherwise keep them in the site, or list them there
  with a `source` and let `boardctrl images sync` fetch them (see [configuration.md](configuration.md#site-images)).
- `media_boot` is for SoCs that can boot a board's media from download mode. A plain reset
  (`boardctrl dut reset --no-fdl`) and recipe steps marked `when: fdl-not-strapped` reset the board into
  download mode and run these steps. Use `check` and `getvar … match` first, so a board the steps
  do not suit is refused before anything is staged. Steps may also run a hook (`do: hook`). Boards
  with an FDL line never need this. A DUT can bring its own `control.media_boot`, or switch it off
  with `control.media_boot: false`. `boardctrl dut status` shows how each board boots its media.

Then write boot recipes for the SDKs of that SoC ([sdk.md](sdk.md)).

## A new board host type

Describe its GPIO header in `boards/<id>.yaml`: one entry per header with its pins — a GPIO pin
gives its Linux GPIO number (`gpio: 83`), other pins a `kind` (`power`, `gnd`) and `label` —
plus optional alternate-function labels and where the kernel's pin-mux information lives. See
`tests/fixtures/site/boards/example-40pin.yaml` and the files in `platforms/spacemit/boards/`. Select it
with `providers.gpio.board` (or `modules.gpio.board`, the older key) in the host configuration.

GPIO access goes through a backend: `sysfs`, `gpiod` (installed automatically by `deploy.sh`) or `mock`. A new
backend implements the `GpioBackend` interface in `providers/gpio/backends/` and registers itself.

## A new reset mechanism

A reset mechanism is a provider of the `relay` role: subclass `ControlBackend`
(`core/dut/contracts.py`), implement `reset(log, ready)` (and `enter_fdl` if the board needs more than a
reset), and register it where the mechanism lives — the shipped ones are the `gpio-relay` in
`providers/gpio/relay.py` and `human` in `providers/human/provider.py`; yours is a provider directory in
your site. A record selects it with `services.relay.provider` (or the older `control.backend`).

## A new provider: console, download, flash or debug

A DUT's roles are filled by providers (`core/providers.py` holds the table; the record says which, under
`services:`). A provider of any role is a class with the role's interface — `ConsoleProvider`,
`DebugProvider`, `FlashProvider`, `DownloadPort`, `ControlBackend` (`core/dut/contracts.py`) — and a
factory `(ctx, spec, services) -> instance` registered as `PROVIDERS["<role>"].register("<name>", factory)`
from your provider's `setup()`. Say what it
offers (`DebugProvider.kinds`) and describe its own settings with `Field`s, so `boardctrl dut set` and the page offer
exactly that. A flash provider's `flash(package, log, timeout)` is handed the local file the host resolved (an
uploaded one or a downloaded one, its hash checked) and writes it; the built-in `hook` provider is the host's own
job, which unpacks a package and runs `hooks/flash/<flasher>`.

A provider whose server outlives one command uses the session service (`core/sessions.py`):
`services("sessions").start(dut, role, provider, command, endpoint=…, lease=…, ready=…)`. The session is a
transient systemd unit under the board's lease, so releasing the board stops it.

`boardctrl debug …` asks the host for all of this and knows no provider: `list` and `status` relay what a board
reports, `start`/`stop` open and close the session, `action` runs what the provider lists in `kinds`, and `pipe`
bridges the session's bytes on stdin/stdout for programs (boardlab's debug page reads a trace that way). A provider
whose path carries a trace channel reports the session's endpoint as the DUT's console socket, so the same bridge
serves a console and a trace.

## A provider

Hardware nobody bought a driver for is a site provider: a directory `providers/<name>/` in the site with a
`provider.py` that defines `setup(ctx, api, config)` (a site that still spells it `modules/<name>/module.py`
is read too). The API registers implementations (`api.provider("debug", "my-swd", factory, title=…, about=…)`),
services for other providers, commands (`api.command(Command(CommandSpec(…), handler))`), HTTP routers, and
what runs when the host starts and stops. The `title` and `about` a provider registers with are the words a
page shows for it: the instance carries them, so `debug status` and the debug page name the path as the
provider does, never by its registered name. A host loads every provider it names under `providers:` (or
`modules:`) with that section as `config`, and each one's `setup` runs before any startup. One that fails says
so in `boardctrl status` and never takes the host down.

    <site>/providers/<name>/provider.py      def setup(ctx, api, config)
    <site>/providers/<name>/…                whatever it ships (bundles, data files, a tool)

Code belongs in the site: a platform carries public knowledge, a site carries what belongs to one lab.
A provider may be a consumer too — it reads another's service (`ctx.service("gpio")`) rather than its files.

## Hooks

Procedures that do not fit recipe steps — a vendor flash tool, a different way to list consoles
— are hooks: executables with a JSON-in, JSON-out contract, searched in the site, then the shipped platforms,
then the core. See [hooks/README.md](../hooks/README.md).

## Platforms

A shipped platform is a directory under `platforms/` with a `platform.yaml` and any of `socs/`,
`boards/`, `hooks/` and `hints/`:

```yaml
name: acme
description: ACME A1 SoC facts and evaluation-board header map
provides:
  socs: [a1]
  boards: [acme-a1-evb]
```

Shipped platforms contain only public knowledge: anything a person who buys the hardware can observe or
download. Every platform in `platforms/` is read; a site overrides one file by file. Contributions of new
platforms are welcome; see [CONTRIBUTING.md](../CONTRIBUTING.md).
