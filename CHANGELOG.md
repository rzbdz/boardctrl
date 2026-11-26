# Changelog

All notable changes to this project are documented here. The format follows
[Keep a Changelog](https://keepachangelog.com/en/1.1.0/), and the project uses
[Semantic Versioning](https://semver.org/) from 1.0 on.

## [Unreleased]

### Added
- `dut flash --partitions a,b` unpacks the package for those partitions only: its layouts, `fastboot.yaml`, what that
  stages and the images written — a several-gigabyte package no longer lands on the host's disk to write one image.
  A directory uploaded as it is still comes whole.
- A provider declares the services it reads (`REQUIRES = ("gpio", "smtty")` in its `provider.py`). Every setup has
  run before they are checked, so a provider whose service the host does not offer is degraded, keeps what it
  registered, and says in its status which service it waits for — where the host config that caused it is fixed —
  instead of failing the first time a board is used.
- `dut boot-marks DUT`: the console lines that start a boot stage on a board, from its SoC: the boot-ROM
  `fingerprint` as the `rom` mark, then an optional `boot_marks:` list in soc.yaml
  ({id, title, pattern, flags, stage, tone}) that adds marks or changes one by id. A pattern that does not
  compile, or that uses lookbehind, named groups or inline flags, is left out with a warning.
- `dut logs`: every line comes with its number (0-based across the kept day files, with the log's `total`) and its
  position (`<day>:<byte offset>`, stable while that day is kept). `--at POS` / `--line N` with `--before` and
  `--after` return a window of the log around one line, read around it rather than from the start of the day;
  `--hits` returns the numbers of matching lines, `--marks` the lines each pattern marks (a boot's banners), and
  `--from-line` searches only from a line on. The host keeps a line index per day file and extends it as the
  file grows.
- `dut flash --package URL` and `dut boot --files URL`: the host reuses what a URL gave within
  `modules.dut.flash.url_cache_ttl_s` (default 24 h, 0 = off) instead of downloading it again, then asks the
  server with a conditional request. `--refresh` (`refresh` over HTTP) downloads it anyway; a `--sha256` other
  than the cached file's always downloads. Files downloaded this way leave the artifact cache once expired and
  unused.
- `boardctrl job cancel ID`: stop any job by id (probes and rebuilds too; a board's job takes its lease).
  A cancel stops what the job runs at once: fastboot, hooks and copies are stopped with what they started
  (their process group gets SIGTERM, then SIGKILL), and waits on the console, the download port or a person
  end. The job ends `cancelled` with the step it was in. Following a cancelled job is no longer reported as a
  success: the CLI says `error.code: cancelled` and exits 4.
- serial-mux sends a connecting client the newest 5000 lines and at most 512 KB of history
  (`history_lines`, `history_bytes`), not its whole scrollback.
- Host environment: a top-level `env:` map in `hosts/<host>.yaml`, and `env:NAME` anywhere in
  the file to take a value from boardctrld's environment. Hooks get it as `req["env"]`, in their
  process environment, and `${NAME}` / `${NAME:-default}` in their `args`.
- Boot recipe step `do: send`: type one line on the console and optionally wait for a regex,
  without the console's echo of the line satisfying the wait or failure pattern.
- Hook requests carry `env`, `network` (`modules.dut.network`: board subnet, host and export
  addresses), and `product_dir` (`modules.dut.product_dir`, or the artifact directory);
  boot-recipe hooks also get `sdk` and `artifacts_dir`.
- Slots (`bsp slot list|status|console|pipe|scan|forget`): every serial console on a host is a slot
  with a serial-mux console of its own, usable before any board is registered. Boards, reset lines
  and download ports are attached to slots; removing a board leaves its slot and its history.
  Any DUT can be named by its slot (`<host>/s3`).
- `bsp dut refresh`: re-read the board in a slot after swapping it (identify, update board, SoC,
  model, and the download port if its cable moved), keeping the lease; `--rename` names it after
  what identify read.
- `bsp probe --register` registers each board the moment it is found; `--slots` probes only some
  slots; `--known` only tries reset lines slots had before. Hosts tell the registry about new
  boards and slots at once.

- `bsp dut logs`: a console's log over a time range, searched by text or regex, capped and saying so.
- `bsp probe --rebuild -r HOST`: after re-cabling, forget the host's probed boards and slots and
  rediscover every board, trying the reset lines they used before first and keeping their names.
  A slot whose adapter is unplugged goes away unless a board is registered on it.
- The registry shares its site's SoC profiles, hooks and their images: hosts that announce there
  follow them as a layer above their site, and `bsp catalog sync` brings them to a client.
- Media boot: a board whose FDL strap is held down can boot its own media when its SoC profile (or
  the DUT) declares `media_boot` steps, run from download mode. A plain reset
  (`bsp dut reset --no-fdl`) and recipe steps marked `when: fdl-not-strapped` use them; `bsp dut
  status` shows it. New recipe steps: `check` (the console matches, else stop) and `boot-media`.
- `bsp images list|sync`: SoC images a site lists with a `source` and `sha256` are fetched once into
  `<site>/.blobs/<sha256>/`; `deploy.sh` syncs them before shipping the site.

### Changed
- A boot says where each SDK artifact came from: one line per file with its sha256, a fresh copy from the
  build server or a host cache hit, the file's time on the build server, and the build job that wrote it
  (or a WARNING when the latest successful build did not rewrite it, or a build is still running). The
  boot result carries the same facts (`artifacts.<role>.source`, `built`, `build`). The host's copy is
  used only when its sha256 and size match the build server's current file. `boardctrl sdk build` ends with
  one `artifact <file> sha256 <12 hex> <size> built <time> job <id>` line per artifact.
- Probe tries the reset lines slots had before first (`probe.pin_order`), presses a line that made
  several consoles speak again (`probe.retry_ambiguous`), skips consoles whose adapter is gone,
  and says why a slot found nothing. `bsp probe --yes` registers each board as it is found.
- Interactive consoles (`bsp dut console`, console logins) leave with Ctrl-A Ctrl-X, as in picocom;
  Ctrl+] and Ctrl-C now go to the board (serial-mux's smtty reads picocom's keys).
- `smtty --send --wait` timeouts keep the command's output and exit code as scripts expect;
  `--advice` (with `--tail-lines`) adds the console's last lines and how to go on, for an AI driving it.
- The tree is `core/`, `providers/`, `frontends/`, `platforms/`, `hooks/`. `core/` is the kernel: the
  DUT manager, its five role contracts, the SDK catalogue, the host registry and the loader. A provider
  binds one external system to a role (`providers/gpio`, `smtty`, `fastboot`, `human`), and a site's own
  code has the same shape (`providers/<name>/provider.py`). What shipped as `profiles/` is `platforms/`.
  A host's config names what it loads under `providers:`; `modules:`, `module.py` and
  `modules.dut.*` are still read, so a site or a script does not have to move in one day.

### Fixed
- A board reset by a person that goes back to its boot ROM mid-load — the download button pressed a second time
  while a RAM load's steps (or `dut boot --files`) are staging — now starts the load over from its first `stage`
  step (or first file) instead of timing out: the board is already in download mode, so nobody is asked again.
  It is noticed inside a step's own waits, before the next step runs, and on the USB port when the console keeps
  no log; a load that runs normally pays no extra time. Two replays are allowed; one more is reported as a
  hardware error naming the likely causes. Only a load is replayed: a `download` recipe writes the board, so its
  writes are never repeated behind its author's back.
- A media boot (a strapped board's reset) from a board left in a later stage, such as U-Boot's fastboot after a
  flash, resets the board into the boot ROM's download mode first instead of running the ROM's steps against U-Boot.
- Probe no longer says a SoC has no identify section when it skipped identify because no download
  port appeared.
- The SpacemiT flash hook skips a medium U-Boot reports as `NULL` (no SPI NOR on a UFS board)
  instead of flashing a NOR layout there and failing after the block layout was written.
- Boot and flash recipes no longer fail on a board that is already in download mode: either the
  boot ROM's console banner or its USB download device now counts as being in download mode.

### Added
- git-registry (`modules.registry.git`): the registry commits every catalog change to a git
  repository and validates commits pushed there by others before taking them in.
- `bsp catalog refresh`; hosts report site edits the registry's catalog overrides (not in effect).
- The SDK catalog in the registry (`bsp catalog …`): versioned documents with history and rollback,
  conflict checks, validation of every change against the whole catalog, distribution to hosts
  without restarts; the site's `sdk/` is the seed.
- Hosts watch for serial and USB devices coming and going (`dut.ports`) and tell the registry at
  once (`registry.ports`); `bsp watch --registry [--json]` streams the registry's events.
- `bsp probe --human`: find boards without reset wiring by asking a person to put each one into
  download mode, pairing its console with the USB port that appears.
- `bsp dut renew`: extend a lease from now, keeping its token.
- `bsp dut pipe`: the board's serial-mux protocol on stdin/stdout, for programs; `dut status`
  reports `console_socket` and `console_up`.
- Artifact upload (`PUT /api/v1/artifacts/<sha256>`), streamed over HTTP and the ssh tunnel.
- `bsp dut stage` and `bsp dut present`: load a local file into RAM through the download port,
  and check whether the port is on the bus.
- `bsp dut boot --files`: boot from RAM with local files in order.
- Host registry: the `registry` module, `server.announce` on board hosts, and `"registry"` in
  `remotes.json` (or `$BSP_REGISTRY`) so clients find announced hosts.

## [0.1.0b0] - 2026-11-05

First public beta.

### Added
- Board leases, board registry and discovery (`bsp probe`), with SoC identification from
  boot-ROM output.
- Remote SDK workspaces on build servers: leases that park the owner's work on a task branch,
  detached builds, remote file access and commands.
- Boot recipes for RAM boot (`load`) and flashing (`download`), including hook steps for vendor
  flash tools and a `nudge` option for prompts printed without a newline.
- GPIO relay module with a web console: header pin map, channel modes, aliases, pin-mux checks.
- `bsp dut bind` and the `human` control backend: boards without reset wiring, reset by a person
  on request and resumed with `bsp dut confirm` or automatic download-mode detection.
- Console logins: `ssh boardctrl-<dut>@<host>` opens a board's console (opt-in).
- Multi-host CLI over HTTP or ssh tunnels, `--json` output and stateless mode for agents.
- Agent skills, `bsp install` and `bsp hints`; the end-to-end acceptance test in `tests/e2e-material/`.
- Configuration layers: core, hardware packs (`packs/spacemit` for SpacemiT K1 and K3) and an
  external site directory; `examples/site/` documents every format.

[Unreleased]: https://github.com/rzbdz/boardctrl/compare/v0.1.0b0...HEAD
[0.1.0b0]: https://github.com/rzbdz/boardctrl/releases/tag/v0.1.0b0
