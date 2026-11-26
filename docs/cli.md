# CLI reference

`boardctrl` is one command for people and programs (`bsp` is its old name and still works). Every command accepts
`--help` (`boardctrl dut boot --help`).

## Global options

They may appear anywhere on the command line.

| Option | Meaning |
|---|---|
| `-r, --remote NAME[,NAME…]` | Board host(s) to talk to (names from `remotes.json`; unique prefixes work; `all` for every host) |
| `--url URL` | Talk to the host at this URL |
| `--token TOKEN` | API token for hosts that require one |
| `--json` | Machine-readable output (see [Output](#output-and-errors)) |
| `--stateless` | Ignore the saved default host and saved lease tokens; for parallel agents and CI |

How hosts are chosen without `-r`/`--url` is described in
[configuration.md](configuration.md#which-hosts-a-command-uses).

## Boards: `boardctrl dut`

| Command | Purpose |
|---|---|
| `boardctrl dut list [--sdk SDK] [--soc SOC] [--free]` | Boards on all reachable hosts, with their slots |
| `boardctrl dut status DUT` | Stage, lease, fastboot state, control lines, pending manual action, console tail |
| `boardctrl dut log DUT [--lines N] [--grep RE]` | Recent console lines, read passively |
| `boardctrl dut logs DUT [--since T] [--until T] [--grep TEXT [--regex]] [--limit N] [--first]` | The console log over a time range (`YYYY-MM-DD HH:MM:SS`), searched; says when it was capped |
| `boardctrl dut boot-marks DUT` | The console lines that start a boot stage on the board, from its SoC: the boot-ROM fingerprint as `rom`, then the SoC's `boot_marks` (id, tone, stage, pattern) |
| `boardctrl dut logs DUT --at POS\|--line N [--before N] [--after N] [--positions]` | A window of the log around one line: `POS` is `start`, `end`, `line:N` or a line's position `YYYY-MM-DD:OFFSET` (its day file and byte offset); every line comes with its number and position, and the answer says whether the log goes on before and after |
| `boardctrl dut logs DUT [--grep TEXT] [--hits [--max-hits N]] [--marks JSON] [--from-line N]` | Where lines are rather than their text: the numbers of matching lines, and of lines each mark (`[{"id", "pattern", "flags": "" or "i"}]`) matches, from line N on |
| `boardctrl dut acquire DUT [--holder NAME] [--purpose TEXT] [--ttl S]` | Lease a board (default 3600 s, at most 24 h); the token is remembered |
| `boardctrl dut renew DUT [--ttl S]` | Extend your lease from now; holder, purpose and token stay the same |
| `boardctrl dut release DUT [--force]` | Release it |
| `boardctrl dut reset DUT [--no-fdl] [--no-wait] [--timeout S]` | Reset into download mode and wait for it (`--no-fdl`: boot the board's own system; a board whose FDL is strapped boots its media through the SoC's media boot when it has one) |
| `boardctrl dut boot DUT --sdk SDK [--method load\|download] [--variant V] [--no-wait] [--timeout S]` | Run the SDK's boot recipe; streams progress, exit code is the result |
| `boardctrl dut boot DUT --files FILE… [--no-reset] [--first-wait S] [--later-wait S] [--port-timeout S] [--no-wait]` | Boot from RAM with local files in order (see [Booting local files](#booting-local-files)) |
| `boardctrl dut flash DUT --package PATH\|URL [--sha256 HEX] [--flasher NAME] [--plan] [--partitions A,B] [--timeout S] [--no-reset] [--no-wait]` | Write a package (images and their layout) to the board's media (see [Flashing a package](#flashing-a-package)) |
| `boardctrl dut stage DUT FILE [--no-continue] [--timeout S]` | Copy a local file to the host with scp (once, by sha256) and load it into RAM: `fastboot stage` + `continue` |
| `boardctrl dut present DUT` | Whether the board's download/fastboot port is on the USB bus |
| `boardctrl dut console DUT "CMD" [--expect RE] [--timeout S]` | Run one shell command on the console and print its output |
| `boardctrl dut console DUT` | Attach interactively over ssh (Ctrl-A Ctrl-X detaches, as in picocom) |
| `boardctrl dut pipe DUT` | Raw serial-mux protocol on stdin/stdout, for programs (see [Console pipe](#console-pipe)) |
| `boardctrl dut confirm DUT [NOTE]` | Tell a waiting reset/boot that the requested manual action is done |
| `boardctrl dut cancel DUT` | Stop the job running on a board (a boot, a flash, a reset waiting for a person) |
| `boardctrl job cancel ID [--reason TEXT]` | Stop any job by id, a probe or a rebuild too; a board's job takes the board's lease |
| `boardctrl dut logins` | `ssh` commands that open each board's console (if enabled on the host) |
| `boardctrl dut bind NAME --console TTY\|ALIAS --usb USB [--soc SOC] [--board B] [--description D] [--sdks LIST] [--fdl strapped\|button] [--host HOST] [--replace]` | Register a board from a console and a USB port; a person does the resets |
| `boardctrl dut add HOST --spec JSON\|@FILE [--replace]` | Register a board from a spec (what probe produces) |
| `boardctrl dut set DUT [--board/--soc/--description/--sdks/--usb/--reset-pin/--pulse-ms/--fdl/--fdl-pin]` | Edit a board |
| `boardctrl dut rename OLD NEW [--console]` | Rename a board (and its console alias with `--console`) |
| `boardctrl dut refresh DUT [--name NEW \| --rename] [--board B] [--soc S]` | Re-read the board in a slot after swapping it: reset into download mode, identify, update its board, SoC and model, and its download port if the cable moved; the lease stays |
| `boardctrl dut remove DUT [--force]` | Forget a board (its slot stays) |
| `boardctrl dut export [--dir DIR] [--host HOST]` | Registered boards as YAML; `--dir` writes one `DIR/<id>.yaml` per board, the format of `SITE/duts/` |

**Slots.** Every `DUT` above can also be named by its slot: `<host>/s3`, or `s3` with one host.
A slot is a serial console on a host (see [Slots](#slots-bsp-slot)), so a slot name keeps working
when the board in it is swapped and refreshed.

**Leases.** `reset`, `boot`, `stage`, `console "CMD"`, `renew`, `release` and `refresh` need your lease; `set` and `rename`
need it while the board is leased. The CLI passes the remembered token; in stateless mode use
`--lease TOKEN` or `BSP_LEASE`.

**Editing boards.** `set`, `rename` and `remove` work on boards registered on the host (by
`probe`, `bind` or `add`). Boards defined in `SITE/duts/` are edited in their file.

### Slots: `boardctrl slot`

Every serial console on a board host is a slot: each USB-UART adapter, in the physical USB port it
is plugged into, gets a slot and a serial-mux console of its own as soon as the host sees it. A
reset line, a download port and a board are attached to a slot as they are learned (by `probe`,
`bind` or `add`); a slot without them is still a console you can watch and type into, which is
what you want while wiring a new board or debugging. Removing a board leaves its slot, and the slot
remembers the reset lines, download ports and boards it had. When an adapter is unplugged, its slot
goes away if it held no board; a slot holding a board stays, offline, so the board shows as
unreachable until it comes back, is removed, or the host is rebuilt.

After re-cabling a host (consoles in other USB ports, boards moved), rebuild it:
`boardctrl probe --rebuild -r HOST --yes`. Every board the probe registered and every slot is forgotten.
Each adapter plugged in now gets a fresh slot, and every board is found again, trying the reset
lines boards used before first and giving each its old name back. Boards from the site's `duts/`
stay. It refuses to start while a board on the host is leased or busy.

| Command | Purpose |
|---|---|
| `boardctrl slot list [--free]` | Slots on all reachable hosts: console, online or not, board, reset line, download port |
| `boardctrl slot status SLOT` | One slot, with its history |
| `boardctrl slot console SLOT` / `boardctrl slot pipe SLOT` | Its console, interactively or for programs, with or without a board |
| `boardctrl slot scan` | Match each host's consoles to slots now (hosts also do this when cables change) |
| `boardctrl slot forget SLOT` | Forget a slot whose adapter is gone |

### Boards reset by a person

Boards with the `human` control backend (for example slots made with `boardctrl dut bind`) have no
reset wiring. `boardctrl dut reset` and `boardctrl dut boot` then print what to do and wait:

```console
$ boardctrl dut reset slot-1
slot-1 @ lab-1: reset needs a person, job reset-1793860731-1 (confirm with `boardctrl dut confirm slot-1`)
  [   0.0s] ACTION NEEDED: press RESET on slot-1 (board-x); it boots into download mode
  [   0.0s] waiting for `boardctrl dut confirm slot-1` or for the board to show it on its own (timeout 900s)
```

The wait ends when someone runs `boardctrl dut confirm slot-1`, or as soon as bsp sees the board in
download mode on its console or USB port (for a plain reset: the SoC's boot-ROM output on the
console). If the board is not actually in download mode after a confirmation, the next step
fails with a fastboot timeout. Scripts and agents should run such commands in the background.

While a RAM load runs, bsp keeps an eye out for that boot-ROM output: a person who presses the
download button a second time after the first press already worked sends the board back to its boot
ROM, and the load then starts over from its first `stage` step (a `dut boot --files` from its first
file) without asking anyone again. The board is already in download mode, so nothing else has to
happen, and a load that runs normally pays no extra time. The press is noticed while a step waits and
before the next one starts, or, when the console keeps no log, on the USB port: the board's download
device leaves the bus and comes back. After two restarts the load stops with a hardware error that
names what may be sending the board back (the button held too long, a loose reset line, a stage that
does not start). Only boards reset by a person are watched this way: on a relay the same state is a
defect to report, not to work around. A `download` recipe writes the board and is never started over
this way: a flash cut short fails with the error above, and running it again is a separate decision.

### Booting local files

`boardctrl dut boot DUT --files FSBL.bin u-boot-opensbi.itb Image.itb` boots a board from RAM with
files from your machine. Each local file is copied to the board host with scp once (by sha256;
unchanged files are not copied again), then the host puts the board into download
mode (skip with `--no-reset`) and for each file runs `fastboot stage` + `continue` and waits for
the download port to come back before the next one: `--first-wait` seconds after the first file
(default 8, while the boot ROM hands over), `--later-wait` after later ones (default 3), then up
to `--port-timeout` more (default 20). If the port does not return, the boot stops and names the
file. A `host:/absolute/path` file is read on the host itself (a Samba mount, say), and an
`http(s)://` file is downloaded by the host; neither passes through the registry. It runs as a
background job like `--sdk` boots, with the path and sha256 of every file in the job log.
Nothing is flashed.

Programs can create a ticket, copy the file with scp, then adopt it:
`POST /api/v1/artifacts/ticket?sha256=HEX&name=FILE` returns a path on the host; scp the file
there; `POST /api/v1/artifacts/adopt?ticket=ID&sha256=HEX&name=FILE` checks and caches it. Then
`dut_stage` or `dut_boot_files` takes the sha256. File bytes never go through an HTTP request
body, because large bodies crash Node on some RISC-V hosts.

### Flashing a package

`boardctrl dut flash DUT --package PKG` writes a package to the board's storage. `PKG` is a local
`.tar.gz`, `.tgz`, `.tar` or `.zip`, or a local directory (sent as a `.tar`), or an `http(s)://`
URL that the board host downloads itself (into its artifact cache, checked against `--sha256`
when given), or `ssh://[user@]host/absolute/path`: a file on one of the site's build servers
(`sdk/servers`, by its host or id, as its user), which the host copies with scp after hashing it
on the server, as it pulls SDK builds. Any other host, user or scheme is refused before anything
is fetched. A host takes packages up to 16 GiB by default
(`modules.dut.flash.max_package_bytes` in the host config). The host unpacks the package into a
fresh directory (entries reaching outside it, links and special files are refused; an archive
holding a single top-level directory unpacks to that directory), puts the board into download
mode (skip with `--no-reset`) and runs the flash hook `flash/<flasher>` on the directory (see
[hooks/README.md](../hooks/README.md)).

The host remembers which file each package URL gave, by the exact URL (query string included), so
flashing the same link again does not download it again. When the server gave an ETag or
Last-Modified, the host asks it every time with a conditional request: on "not modified" it uses
its copy without downloading, and a link that now gives another file (a `latest/` build) is
downloaded again. A server that gives neither is trusted for 24 hours after the download
(`modules.dut.flash.url_cache_ttl_s` in the host config, in seconds; `0` turns the cache off)
without being contacted; within that time a server the host cannot reach does not stop the flash
either. The job log says which copy it used, with the sha256 and its age. A `--sha256` other than
the copy's always downloads and checks. `--refresh`
downloads the URL again whatever the host remembers. `boot --files http(s)://...` uses the same
cache. The index is `.url-cache.json` in the host's artifact cache, so it survives a restart. A
file the host downloaded this way is deleted from the artifact cache once no URL within the TTL
gives it and no running job uses it; files uploaded to the host are never deleted this way. Two
flashes of the same URL at the same time download it once: the second waits for the first.

The flasher is `--flasher`, else the board's `flash.flasher`, else its SoC's
`flash.flasher`, else the host's (`providers.dut.flash.flasher`), else `spacemit-pkg` (the SpacemiT
package: `fastboot.yaml` and `partition_*.json` layouts, or a flash zip). A board whose SoC facts
name their flasher uses that one, so a site on another vendor's hardware sets it once per SoC.
Each level can add a `flash.fallback` list. Before anything is downloaded or the board is reset, the
host checks the candidates in that order and uses the first one that is ready (a hook can say its
vendor tool is missing; see hooks/README.md). The job's log names each one passed over and why. When
none is ready, the command fails at once and lists the reasons. A `--flasher` name is used or
refused, never replaced; a refusal lists the flashers this host has. The
flash runs as a background job of kind `flash` under your lease;
when the flasher reports progress the job has a percent (`boardctrl job get`, `boardctrl job list`) and the
CLI prints it as it comes. With `--json` the result is `{"job", "lines", "elapsed_s", "flasher",
"flashed"}`. `--timeout` (default 3600 s) bounds the flasher; when it runs out the flasher gets
SIGTERM first so it can let go of the USB device.

`--plan` only says what the package would write: the host fetches and unpacks it as above and asks
the flasher to read it, and no board is reset, put into download mode or written. It prints each
medium and its partitions (offset, size, the file each is written from); with `--json` the result
is `{"job", "lines", "flasher", "plan": {"package", "media", "partial", "notes"}}` (see
hooks/README.md for the plan's fields). It runs as a job of kind `flash-plan` that does not make
the board busy. `--partitions a,b` writes only those partitions (names as `--plan` gives them);
the flasher must declare it can (`spacemit-pkg` does), and a name the package does not have is
refused before the board is touched.

Over HTTP: upload the package as an artifact (see above), then `dut_flash` with `package` set to
its sha256, or to the URL (and `plan: true`, or `partitions`).

### Console pipe

`boardctrl dut pipe DUT` connects stdin and stdout to the board's serial-mux daemon and copies bytes
both ways until either side closes. The bytes are serial-mux's own protocol (4-byte length,
then JSON), so any serial-mux client can open a session through it; it is meant for programs
such as dashboards, not for typing. On a remote host the CLI runs a small Python bridge there
over the remote's `ssh` entry; on the host itself it connects to the socket directly. No lease
is needed: reading is open to everyone (as with `dut log`), and input is subject to the
console's serial-mux input gate. `boardctrl dut status` reports the socket as `console_socket` and
whether the daemon is up as `console_up`.

## Discovery and hosts

| Command | Purpose |
|---|---|
| `boardctrl hosts` | Reachable hosts, how they are reached, their modules and boards |
| `boardctrl -r REGISTRY registry list [--all]` | Hosts that announced themselves to a registry (see [configuration.md](configuration.md#host-registry)) |
| `boardctrl init` | First-run check: configuration directory, mode, hosts |
| `boardctrl probe --rebuild -r HOST [--yes] [--no-identify] [--sdks LIST]` | After re-cabling: forget the host's probed boards and slots, then rediscover every board (without `--yes`: say what would be forgotten) |
| `boardctrl probe [--slots S..] [--pins P..] [--known] [--register] [--sdks LIST] [--dry-run] [--yes] [--no-identify] [--replace]` | Discover the boards on free slots (pulses GPIO lines — see [getting-started.md](getting-started.md#option-a-probe-boards-wired-to-gpio-reset-lines)). Lines slots had before are tried first, so re-cabled or re-registered boards come back fast (`--known`: only those); a line that made several consoles speak is tried again at the end. `--register` (implied by `--yes`) registers each board on its host the moment it is found, named after what identify read, else after the board its slot had last |
| `boardctrl -r HOST probe --human [--rounds N] [--wait S] [--consoles C..] [--yes]` | Discover boards without reset wiring: a person puts one unregistered board at a time into download mode, and bsp pairs the console that speaks with the USB port that appears; each becomes a slot reset by a person, as with `dut bind` |
| `boardctrl remote list\|show\|add\|remove\|use` | Manage personal host entries |
| `boardctrl watch [--registry]` | Stream live events from the host(s), or with `--registry` from the registry (hosts announcing themselves, `registry.ports` when a host's cables change); `--json` prints one event per line for programs |
| `boardctrl tools` | Print the host's AI tool schemas in Anthropic format (OpenAI format: `GET /api/v1/tools?format=openai`) |
| `boardctrl hints` | Hints from every platform (the shipped ones, then the site) |
| `boardctrl images [list\|sync]` | Images hosts stage (probe images, media-boot helpers): where each comes from and whether the site has it; `sync` fetches the missing ones once into `<site>/.blobs/` (deploy does this) |
| `boardctrl install [--dir DIR] [--offline]` | Write `SKILL.md` for agents working in an SDK tree (see [agents.md](agents.md)) |

## SDKs: `boardctrl sdk`

| Command | Purpose |
|---|---|
| `boardctrl sdk list [--soc SOC]` | SDK targets, their SoC, workspace and boot-method status |
| `boardctrl sdk show SDK` | Build server, path, dev repos, components, artifacts, sharing |
| `boardctrl sdk status SDK` | Lease, branches, uncommitted files, recent builds |
| `boardctrl sdk plan SDK [COMPONENT] [--boot load\|download] [--variant V]` | Dry run: the build command or the boot steps |
| `boardctrl sdk acquire SDK --task NAME [--holder NAME] [--ttl S] [--branch B] [--projects LIST]` | Lease the workspace (default 8 h) and switch to branch `bsp/NAME` |
| `boardctrl sdk release SDK [--keep] [--force]` | Restore the owner's branch and stashed work |
| `boardctrl sdk build SDK COMPONENT [--clean] [--no-wait] [-v] [--timeout S]` | Build on the server; streams warnings and errors |
| `boardctrl sdk jobs SDK`, `boardctrl sdk job SDK ID [--follow] [--log]` | Build jobs |
| `boardctrl sdk exec SDK [--cwd DIR] [--read-only] [--timeout S] -- CMD…` | Run a command in the tree (cwd: first dev repo) |
| `boardctrl sdk get SDK PATH [-o FILE]`, `boardctrl sdk put SDK PATH FILE` | Read or write a file in the tree |

Commands that change the tree (`build`, `exec`, `put`) are refused while someone else holds the
workspace lease; hold it yourself while you work.

`exec` joins its arguments and runs them with `bash -c` on the build server, so quote once more
for words that must stay together: `boardctrl sdk exec SDK -- git commit -am "'usb: fix resume'"`.

See [sdk.md](sdk.md) for what acquire and release do to the tree.

## Host commands

These run on a board host; the CLI fetches their definitions from the host, so they are always
in sync with it. Add `-r HOST` when several hosts are reachable.

| Group | Commands |
|---|---|
| `boardctrl gpio` | `list`, `get`, `set`, `pulse`, `free`, `config`, `export`, `pinmux`, `alias add\|delete\|list\|resolve` |
| `boardctrl relay` | `on`, `off`, `toggle`, `pulse` |
| `boardctrl button` | `press`, `release`, `click` |
| `boardctrl board` | `info` |
| `boardctrl job` | `list`, `get` |
| `boardctrl events` | `recent` |
| `boardctrl status` | Service, provider and backend health |

The same commands are HTTP endpoints (`GET` for reads, `POST` for changes, under `/api/v1/`) and
AI tools; `GET /api/v1/tools` lists them and `/docs` serves the OpenAPI description.

## Output and errors

Text output is meant for people. With `--json` every command prints exactly one object:

```json
{"ok": true, "result": {"...": "..."}}
{"ok": false, "error": {"code": "not_found", "message": "no DUT 'example-bord' on any reachable host",
                        "hint": "did you mean: example-board? known: example-board@lab-1", "details": {}}}
```

`hint` names the next step (a likely typo, who holds a lease, which command to run); `details`
carries context such as the console tail or the end of a build log. Long-running commands print
progress lines to stderr in JSON mode.

| Exit code | Meaning |
|---|---|
| 0 | Success |
| 1 | The command failed |
| 2 | Usage error, or an ambiguous host for a state-changing command |
| 3 | No host reachable |
| 4 | Cancelled: the job the command followed was stopped on request (`error.code` is `cancelled`) |
| 130 | Interrupted |

**Cancelling a job.** `boardctrl dut cancel DUT` or `boardctrl job cancel ID` stops a job now, not at its next
step: the program it runs (fastboot, a flash or probe hook, an scp) is stopped with whatever it started (SIGTERM
to its process group, SIGKILL a few seconds later), and a wait (the console, the download port, a person) ends at
once. The job ends `cancelled`, and its error says the step it was in (`error.details.step`) and the last line it
printed. The board keeps its lease and is no longer busy.
