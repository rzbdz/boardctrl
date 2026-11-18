# Porting

boardctrl keeps hardware knowledge in data files and small hooks. Adding support for new hardware
rarely needs code.

## A new SoC

Create `socs/<soc>/soc.yaml` — in a shipped profile if the information is public, otherwise in your site:

```yaml
soc: soc-a
fingerprint:                     # boot-ROM console lines printed after a reset
  - "ROMA: boot start"
  - "ROMA: usb init"
download_mode:                   # how bsp recognises USB download (FDL) mode
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
  only images you may redistribute in a shipped profile; otherwise keep them in the site, or list them there
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
`examples/site/boards/example-40pin.yaml` and the files in `profiles/spacemit/boards/`. Select it
with `modules.gpio.board` in the host configuration.

GPIO access goes through a backend: `sysfs`, `gpiod` (installed automatically by `deploy.sh`) or `mock`. A new
backend implements the `GpioBackend` interface in `src/bsp/gpio_relay/backends/` and registers
itself.

## A new reset mechanism

Providers of the `relay` role live in `src/bsp/dut/control.py`: subclass `ControlBackend`, implement
`reset(log, ready)` (and `enter_fdl` if the board needs more than a reset), and register it with
`@CONTROL_BACKENDS.register("name")`. A record selects it with `services.relay.provider` (or the older
`control.backend`).

## A new provider: console, download, flash or debug

A DUT's roles are filled by providers (`src/bsp/core/providers.py`; the record says which, under `services:`).
A provider of any role is a class with the role's interface — `ConsoleProvider` (`dut/console.py`),
`DebugProvider` (`dut/debug.py`), `FlashProvider` (`dut/flash.py`), the download shape — and a factory
`(ctx, spec, services) -> instance` registered as `PROVIDERS["<role>"].register("<name>", factory)`. Say what it
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

## A site plugin

Hardware nobody bought a driver for is a site plugin: a directory `modules/<name>/` in the site (or in a shipped profile) with a
`module.py` that defines `setup(ctx, api, config)`. The API registers providers
(`api.provider("debug", "my-swd", factory, title=…, about=…)`), services for other modules, commands
(`api.command(Command(CommandSpec(…), handler))`) and FastAPI routers. The `title` and `about` a provider registers
with are the words a page shows for it: the instance carries them, so `debug status` and the debug page name the
path as the plugin does, never by its registered name. Select the plugin in the host config by naming it
under `modules:` (its own section is passed as `config`), or in `plugins:`. It is loaded at host start; one that
fails says so in `boardctrl status` and never takes the host down.

    <site>/modules/<name>/module.py      def setup(ctx, api, config)
    <site>/modules/<name>/…              whatever it ships (bundles, data files)

Code belongs in the site, not in a shipped profile: a profile carries public knowledge, a site
carries what belongs to one lab.

## Hooks

Procedures that do not fit recipe steps — a vendor flash tool, a different way to list consoles
— are hooks: executables with a JSON-in, JSON-out contract, searched in the site, then the shipped profiles,
then the core. See [hooks/README.md](../hooks/README.md).

## Profiles

A shipped profile is a directory under `profiles/` with a `profile.yaml` and any of `socs/`,
`boards/`, `hooks/` and `hints/`:

```yaml
name: acme
description: ACME A1 SoC profile and evaluation-board header map
provides:
  socs: [a1]
  boards: [acme-a1-evb]
```

Shipped profiles contain only public knowledge: anything a person who buys the hardware can
observe or download. A site chooses them with `profiles:` in `boardctrl.yaml`. Contributions of new profiles are welcome;
see [CONTRIBUTING.md](../CONTRIBUTING.md).
