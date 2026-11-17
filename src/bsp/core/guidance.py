"""Guidance a site carries, in levels: hints, SoC notes, SDK skills — found through the CLI, not guessed.

An agent starts from the boardlab skill (the DUT: locking, reset, console, boot, flash). What it needs next
belongs to the *hardware*: which SoC profile a board has, the notes that SoC's site carries, and the skill of the
SDK it is building. That knowledge changes with the site, so it is not in the client; it is asked for:

    bsp hints                         everything the configuration layers say (packs, then the site)
    bsp hints --soc <soc>             one SoC's notes, and its profile paths
    bsp hints --sdk <sdk>             an SDK's skill and its catalog entry
    bsp hints --dut <dut>             what applies to one board: its SoC's notes, its SDKs' skills
    bsp hints --list                  what there is to ask for, by level

Levels, lowest to highest priority (a later layer's file with the same name wins):

    <layer>/hints/*.md                notes for everyone working on this site
    <layer>/socs/<soc>/*.md           one SoC's notes (beside its soc.yaml)
    <layer>/skills/<id>.md            one SDK's skill (beside its catalog entry)

Only the configuration layers are read: the repository's packs and the site. Nothing here reaches a board, so an
agent can ask any time, and the answer is the same one every host has.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

from .site import layered, layers


def _read(path: Path) -> str:
    try:
        return path.read_text(encoding="utf-8").strip()
    except OSError:
        return ""


def _layer_name(root: Path, site: Path | None) -> str:
    return "site" if site is not None and root == site else root.name


def _docs(kind: str, site: Path | None = None) -> list[dict[str, str]]:
    """`<layer>/<kind>/**/*.md`, lowest layer first; a later file with the same relative name wins."""
    found: dict[str, dict[str, str]] = {}
    for root in layers(site):
        at = root / kind
        if not at.is_dir():
            continue
        for path in sorted(at.rglob("*.md")):
            rel = path.relative_to(root).as_posix()
            found[rel] = {"name": rel, "layer": _layer_name(root, site), "text": _read(path), "path": str(path)}
    return list(found.values())


def soc_docs(soc: str, site: Path | None = None) -> list[dict[str, str]]:
    """One SoC's notes, from every layer that has it (later wins on the same file name)."""
    out: dict[str, dict[str, str]] = {}
    for root in layered("socs", site):
        at = root / soc
        if not at.is_dir():
            continue
        for path in sorted(at.glob("*.md")):
            out[path.name] = {"name": f"{soc}/{path.name}", "layer": _layer_name(root.parent, site),
                              "text": _read(path), "path": str(path)}
    return list(out.values())


def sdk_skill(sdk: str, site: Path | None = None) -> dict[str, str] | None:
    """One SDK's skill (`<layer>/skills/<id>.md`), the highest layer's copy."""
    found = None
    for root in layers(site):
        path = root / "skills" / f"{sdk}.md"
        if path.is_file():
            found = {"name": sdk, "layer": _layer_name(root, site), "text": _read(path), "path": str(path)}
    return found


def level_listing(site: Path | None = None) -> dict[str, Any]:
    """What there is to ask for: hints, SoCs with notes, SDK skills."""
    socs = set()
    for root in layered("socs", site):
        for dir_ in sorted(p for p in root.iterdir() if p.is_dir()):
            if any(dir_.glob("*.md")) or (dir_ / "soc.yaml").exists():
                socs.add(dir_.name)
    skills = set()
    for root in layers(site):
        at = root / "skills"
        if at.is_dir():
            skills.update(p.stem for p in at.glob("*.md"))
    return {
        "hints": [doc["name"] for doc in _docs("hints", site)],
        "socs": sorted(socs),
        "skills": sorted(skills),
        "site": str(site) if site else None,
        "layers": [str(root) for root in layers(site)],
    }


def resolve(args: Any, site: Path | None = None) -> tuple[Any, str]:
    """What `bsp hints ...` answers: a list, and its text. `--dut` names a board on a host (asked of the host)."""
    if getattr(args, "hints_cmd", None) == "list":
        listing = level_listing(site)
        text = "\n".join([
            f"site: {listing['site'] or '(none found)'}",
            f"layers: {', '.join(listing['layers']) or '(none)'}",
            "hints: " + (", ".join(listing["hints"]) or "(none)"),
            "socs: " + (", ".join(listing["socs"]) or "(none)"),
            "skills: " + (", ".join(listing["skills"]) or "(none)"),
            "ask for one: bsp hints --soc <soc> | --sdk <sdk> | --dut <board> (the board's host says which apply)",
        ])
        return listing, text

    blocks: list[dict[str, str]] = []
    if getattr(args, "soc", None):
        blocks += soc_docs(args.soc, site)
        profile = []
        for root in layered("socs", site):
            if (root / args.soc / "soc.yaml").is_file():
                profile.append(str(root / args.soc / "soc.yaml"))
        if profile:
            blocks.append({"name": f"{args.soc}/soc.yaml", "layer": "", "text": "profile: " + ", ".join(profile), "path": profile[-1]})
    if getattr(args, "sdk", None):
        skill = sdk_skill(args.sdk, site)
        if skill:
            blocks.append(skill)
    if not blocks:
        blocks = _docs("hints", site)
    if not blocks:
        return [], "nothing configured for this (see `bsp hints --list`)"
    parts = []
    for block in blocks:
        title = f"## {block['name']}" + (f"  ({block['layer']})" if block.get("layer") else "")
        parts.append(f"{title}\n{block['text']}")
    return blocks, "\n\n".join(parts)
