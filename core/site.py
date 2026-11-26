"""Layers: the directories a board host reads its knowledge from, in priority order.

A **layer** is a directory with the same shape, whoever wrote it:

    <layer>/
      boardctrl.yaml   optional: settings for this layer
      boards/          pin maps of boards (a 40-pin header, a 26-pin header)
      socs/            SoC facts: boot-ROM fingerprints, download mode, USB ids, U-Boot identity
      hooks/           executables for probe and flash steps
      sdk/             families, servers, workspaces, instances
      duts/            board records: which provider fills each role
      hints/           notes a person or agent reads
      providers/       provider code: `providers/<name>/provider.py` with setup(ctx, api, config)

There is one mechanism, not two: every directory is a layer, and later ones override earlier ones
file by file. The only difference between them is who wrote them and where they live:

  platforms/<name>/  in this repository, shipped with the release. Public hardware knowledge anyone
                     with the board can check: SoC boot-ROM strings, header pin maps, hooks for
                     public tool formats. Every platform here is read.
  the site           outside the repository: one machine or one lab's own facts — its hosts, build
                     servers, SDK trees, boot and flash recipes, DUT records, credentials, and the
                     providers that adapt hardware nobody ships a driver for. Found at
                     $BOARDCTRL_SITE, ~/.config/boardctrl/site or /etc/boardctrl/site. A site is read
                     last, so it overrides a shipped platform rather than editing it.

The shipped platforms are their own examples, and `tests/fixtures/site/` shows the files only a site has
(hosts, DUT records, SDK trees, remotes, deploy targets, providers).
"""

from __future__ import annotations

from core import naming

import os
from pathlib import Path

REPO = Path(__file__).resolve().parents[1]
PLATFORMS = REPO / "platforms"


def site_dir() -> Path | None:
    """The site this client reads: $BOARDCTRL_SITE, ~/.config/boardctrl/site, /etc/boardctrl/site."""
    home = Path.home()
    cands = [naming.get("BOARDCTRL_SITE"),
             str(home / ".config" / "boardctrl" / "site"), "/etc/boardctrl/site"]
    for c in cands:
        if c and Path(c).expanduser().is_dir():
            return Path(c).expanduser().resolve()
    return None


def site_settings(site: Path | None) -> dict:
    p = None
    if site:
        p = site / "boardctrl.yaml"
    if p and p.exists():
        import yaml

        return yaml.safe_load(p.read_text()) or {}
    return {}


def shipped() -> list[Path]:
    """The platforms shipped in this repository, all of them, lowest priority first."""
    return [p for p in sorted(PLATFORMS.iterdir()) if p.is_dir()] if PLATFORMS.is_dir() else []


def layers(site: Path | None = None, use_env: bool = True) -> list[Path]:
    """Every platform layer this host reads, lowest priority first: the shipped ones, then the site."""
    if site is None and use_env:
        site = site_dir()
    return shipped() + ([site] if site else [])


def layered(kind: str, site: Path | None = None) -> list[Path]:
    """Existing `kind` dirs (boards, socs, hooks, sdk, duts, hints, providers) across every layer, lowest first."""
    return [d for d in (root / kind for root in layers(site)) if d.is_dir()]


def hints(site: Path | None = None) -> list[tuple[str, str]]:
    """(platform/name, text) of every <layer>/hints/*.md: the shipped platforms first, then the site's."""
    if site is None:
        site = site_dir()
    out = []
    for root in layers(site, use_env=False):
        label = "site" if site and root == site else root.name
        d = root / "hints"
        if d.is_dir():
            out += [(f"{label}/{p.stem}", p.read_text(encoding="utf-8").strip()) for p in sorted(d.glob("*.md"))]
    return out
