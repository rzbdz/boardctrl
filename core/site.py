"""Profiles: the directories a board host reads its knowledge from, in priority order.

A **profile** is a directory with the same shape, whoever wrote it:

    <profile>/
      boardctrl.yaml   optional: which shipped profiles to use (`profiles: [...]`), settings
      boards/          pin maps of boards (a 40-pin header, a 26-pin header)
      socs/            SoC facts: boot-ROM fingerprints, download mode, USB ids, U-Boot identity
      hooks/           executables for probe and flash steps
      sdk/             families, servers, workspaces, instances
      duts/            board records: which provider fills each role
      hints/           notes a person or agent reads
      modules/         site plugins: `modules/<name>/module.py` with setup(ctx, api, config)

There is one mechanism, not two: every directory is just a profile, and later ones override earlier
ones file by file. The only difference between them is who wrote them and where they live:

  profiles/<name>/   in this repository, shipped with the release. Public hardware knowledge anyone
                     with the board can check: SoC boot-ROM strings, header pin maps, hooks for public
                     tool formats. Every profile here is read unless the host's settings name a subset.
  the site           outside the repository: one machine or one lab's own facts — its hosts, build
                     servers, SDK trees, boot and flash recipes, DUT records, credentials, and the
                     site plugins that adapt hardware nobody ships a driver for. Found at
                     $BOARDCTRL_SITE, ~/.config/boardctrl/site or /etc/boardctrl/site. A site is read
                     last, so it overrides a shipped profile rather than editing it.

The shipped profiles are their own examples, and `tests/fixtures/site/` shows the files only a site has
(hosts, DUT records, SDK trees, remotes, deploy targets, site plugins).
"""

from __future__ import annotations

from core import naming

import os
from pathlib import Path

REPO = Path(__file__).resolve().parents[1]
PROFILES = REPO / "profiles"


def site_dir() -> Path | None:
    """The site this client reads: $BOARDCTRL_SITE, ~/.config/boardctrl/site, /etc/boardctrl/site.
    The paths the command used before its rename are read when the new one does not exist."""
    home = Path.home()
    cands = [naming.get("BOARDCTRL_SITE"),
             str(home / ".config" / "boardctrl" / "site"), str(home / ".config" / "bsp" / "site"),
             "/etc/boardctrl/site", "/etc/bsp/site"]
    for c in cands:
        if c and Path(c).expanduser().is_dir():
            return Path(c).expanduser().resolve()
    return None


def site_settings(site: Path | None) -> dict:
    p = None
    if site:
        p = site / "boardctrl.yaml" if (site / "boardctrl.yaml").exists() else site / "bsp.yaml"
    if p and p.exists():
        import yaml

        return yaml.safe_load(p.read_text()) or {}
    return {}


def shipped(site: Path | None = None) -> list[Path]:
    """The profiles shipped in this repository that this host uses: all of them, or the subset its
    settings name (`profiles:` in the site's boardctrl.yaml; `packs:` is the older spelling)."""
    available = {p.name: p for p in sorted(PROFILES.iterdir()) if p.is_dir()} if PROFILES.is_dir() else {}
    settings = site_settings(site)
    wanted = settings.get("profiles", settings.get("packs"))
    if wanted is None:
        return list(available.values())
    missing = [w for w in wanted if w not in available]
    if missing:
        from core.errors import InvalidRequest

        raise InvalidRequest(f"the site selects unknown profile(s) {missing}",
                             hint=f"shipped profiles: {', '.join(available)}")
    return [available[w] for w in wanted]


def layers(site: Path | None = None, use_env: bool = True) -> list[Path]:
    """Every profile this host reads, lowest priority first: the shipped ones, then the site."""
    if site is None and use_env:
        site = site_dir()
    return shipped(site) + ([site] if site else [])


def layered(kind: str, site: Path | None = None) -> list[Path]:
    """Existing `kind` dirs (boards, socs, hooks, sdk, duts, hints, modules) across every profile, lowest first."""
    return [d for d in (root / kind for root in layers(site)) if d.is_dir()]


def hints(site: Path | None = None) -> list[tuple[str, str]]:
    """(profile/name, text) of every <profile>/hints/*.md: the shipped profiles first, then the site's."""
    if site is None:
        site = site_dir()
    out = []
    for root in layers(site, use_env=False):
        label = "site" if site and root == site else root.name
        d = root / "hints"
        if d.is_dir():
            out += [(f"{label}/{p.stem}", p.read_text(encoding="utf-8").strip()) for p in sorted(d.glob("*.md"))]
    return out
