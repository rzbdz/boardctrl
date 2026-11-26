# Security

## Threat model

A board host controls hardware: it can reset and flash every attached board, drive GPIO lines and
type into serial consoles, which usually give a root shell on the board. Treat access to a board
host's API like ssh access to the boards.

| Surface | Default | Recommendation |
|---|---|---|
| `boardctrld` HTTP API | Listens on `127.0.0.1`; no token | When you set `server.bind` to a network address, also set a token: `BOARDCTRL_TOKEN=…` in `/etc/boardctrl/hostd.env` on the host (read by the service, never shipped by `deploy.sh`), or `server.auth_token` in the host file if your site repository is private. State-changing requests then need `Authorization: Bearer TOKEN` or `X-BSP-Token: TOKEN`. **All `GET` requests stay open**, including board lists, events and console output (`dut log`, the console tail in `dut status`); restrict network access if console output is sensitive |
| UPnP port mappings | Off | Do not expose a board host to the internet; use a VPN or ssh instead |
| Console logins (`ssh boardctrl-<dut>@host`) | Off | They need no password by design. Enable them only on networks where everyone may use the boards. The login gives the board's console and nothing else: no shell on the host, no forwarding |
| Deployment | ssh as root to the board host | Use ssh keys. `SSHPASS` is supported for password-only hosts and never stored by boardctrl |
| Build-server access | The host's root key is authorised on a build-server account (`grant-build-access.sh`) | Use a dedicated account with access to the SDK trees only |
| Hints and site files | Copied to hosts and clients | Never put secrets in hints or `remotes.json`; keep the host token in `/etc/boardctrl/hostd.env` |
| Client token | — | Pass it with `--token`, the `BOARDCTRL_TOKEN` environment variable, or store it per host with `boardctrl remote add NAME URL --token TOKEN` (in `~/.config/boardctrl/remotes.json`) |

The CLI keeps lease tokens in `~/.config/boardctrl/leases.json` (mode 0600). Lease tokens coordinate
people and agents; they are not an access-control mechanism.

## Reporting a vulnerability

Please report vulnerabilities privately through GitHub's
[private vulnerability reporting](https://github.com/rzbdz/boardctrl/security/advisories/new)
rather than in a public issue. Include the affected version or commit, the configuration needed to
reproduce, and the impact. We aim to acknowledge reports within a week.
