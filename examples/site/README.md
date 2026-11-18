# Example site

A complete site showing every file format boardctrl reads. Real sites live outside the boardctrl
repository (`$BOARDCTRL_SITE`, `~/.config/boardctrl/site` or `/etc/boardctrl/site`), usually in a
private git repo of their own. Copy this directory and replace every value.

A site is a **profile** like the ones shipped in `profiles/`: the same directories, read last so it
overrides them. For the files a profile can carry, the shipped profiles are the examples
(`profiles/spacemit/boards`, `socs`, `hooks`, `hints`); this directory shows the ones only a site has:
`remotes.json`, `hosts/`, `deploy/`, `duts/`, `sdk/` and `modules/`. It is made up except where it says
otherwise — `modules/hello-platform` is a working example of a site plugin, and the board it serves is a
real one from the shipped `profiles/spacemit` profile.
Addresses use the documentation range 192.0.2.0/24. The test suite runs against it.

| path | read by | what |
|---|---|---|
| `boardctrl.yaml` | all | site settings: which shipped profiles to use |
| `remotes.json` | CLI | board hosts: url, fallback urls, ssh target |
| `hosts/<id>.yaml` | bsp-hostd | modules of one board host |
| `deploy/<id>.env` | `deploy/deploy.sh <id>` | how to reach and install a host |
| `boards/*.yaml` | gpio | header pin maps of host controller boards |
| `sdk/families/*.yaml` | CLI + hosts | build styles (make targets, env script + function, tree script) |
| `sdk/servers/*.yaml` | CLI | build servers (ssh, path aliases, job environment) |
| `sdk/workspaces/*.yaml` | CLI | source trees shared by several SDK targets (unit of locking) |
| `sdk/instances/*.yaml` | CLI + hosts | SDK targets: workspace, family, SoC, artifacts, boot recipes |
| `socs/<soc>/soc.yaml` | hosts | boot-ROM fingerprint, download mode, probe images, identity patterns |
| `duts/*.yaml` | hosts | static boards (probed ones live in the host state dir) |
| `hooks/<kind>/<name>` | hosts | site hooks, e.g. a vendor flash tool wrapper |
| `.bspignore` | `deploy.sh`, `install-client.sh` | tar exclude patterns: site files not to ship to hosts / clients |
| `modules/<name>/module.py` | boardctrld | site plugins: providers, commands, routes |
| `hints/*.md` | `bsp hints`, `bsp install` | free text for agents and people (credentials conventions, lab rules); added to generated SKILL.md files |
