"""SoC facts (<layer>/socs/<soc>/soc.yaml, merged across layers: the shipped platforms, then the site):
everything boardctrl knows about a SoC is data.

    soc: <id>
    fingerprint: [regex, ...]          boot-ROM console lines after a reset (probe: best match wins)
    download_mode: {console: regex, fastboot: regex}   what "FDL / download mode" looks like
    usb_vids: [hex, ...]               USB vendor ids of its download / fastboot gadgets
    images: {key: {file, sha256, source}}   images the host stages (probe, media boot); each resolves to the
                                       highest layer holding the file, else to the site's fetched copy
                                       (`bsp images sync` fetches it from `source` into <site>/.blobs/<sha256>/)
    media_boot: {name, steps}          booting its media when FDL is strapped (see mediaboot.py)
    identify: {sequence, fields, placeholders, name}   see hooks/probe/identify
    boot_marks: [{id, title, pattern, flags, stage, tone}, ...]
                                       console lines that start a boot stage, for a log reader to mark: added to
                                       the `rom` mark the fingerprint makes, or replacing fields of one by id.
                                       Missing fields: title = id, flags "" ("i" is case-blind), stage 3, tone
                                       loader (rom, spl, firmware, loader or kernel). A pattern is read by
                                       Python and JavaScript both: no lookbehind, named groups or inline flags.
                                       A mark that breaks these is left out with a warning.
"""

from __future__ import annotations

import logging
import re
from pathlib import Path
from typing import Any

log = logging.getLogger(__name__)

TONES = ("rom", "spl", "firmware", "loader", "kernel")

# What any board prints coming out of reset, profile or not.
BOOT_BANNERS = (r"U-Boot( SPL)? 20\d\d", r"U-Boot (SPL|TPL)\b", r"Trying to boot from \w", r"Hit any key to stop autoboot",
                r"OpenSBI v?\d", r"\b(NOTICE|INFO|WARNING|ERROR):\s+BL(1|2|31|32|33)\b",   # TF-A's stages
                r"Linux version \d", r"Booting Linux", r"Starting kernel",
                r"\bBoot ?ROM\b", r"\bROM: ", r"\bDDR\b.*\b(init|training)\b", r"\bDDR V(ersion )?\d")


def _merge(a: dict, b: dict) -> dict:
    out = dict(a)
    for k, v in b.items():
        out[k] = _merge(out[k], v) if isinstance(v, dict) and isinstance(out.get(k), dict) else v
    return out


class SocFacts:
    """Every SoC's facts, lowest layer first (a later layer's file wins)."""
    def __init__(self, directories: "Path | list[Path] | None"):
        dirs = [Path(d) for d in (directories if isinstance(directories, (list, tuple)) else
                                  [directories] if directories else [])]
        self.profiles: dict[str, dict[str, Any]] = {}
        self.dirs: dict[str, list[Path]] = {}
        import yaml

        for d in dirs:                                   # lowest layer first
            for p in sorted(Path(d).glob("*/soc.yaml")) if Path(d).is_dir() else []:
                data = yaml.safe_load(p.read_text()) or {}
                soc = data.get("soc") or p.parent.name
                self.profiles[soc] = _merge(self.profiles.get(soc, {"soc": soc}), data)
                self.dirs.setdefault(soc, []).append(p.parent)
        for soc, prof in self.profiles.items():        # resolve image files, highest layer wins
            for img in (prof.get("images") or {}).values():
                for d in reversed(self.dirs[soc]):
                    if (d / img.get("file", "")).is_file():
                        img["path"] = str(d / img["file"])
                        break
                else:
                    # fetched by `bsp images sync` into a layer's content-addressed cache (<layer>/.blobs/<sha256>/<file>)
                    for d in reversed(self.dirs[soc]):
                        cached = d.parent.parent / ".blobs" / str(img.get("sha256", "")).lower() / img.get("file", "")
                        if img.get("sha256") and cached.is_file():
                            img["path"] = str(cached)
                            break

        self.marks = {soc: _boot_marks(soc, prof) for soc, prof in self.profiles.items()}

    def boot_marks(self, soc: str | None) -> list[dict[str, Any]]:
        """The lines that start a boot stage on this SoC: its boot ROM (from the fingerprint), then `boot_marks`."""
        return [dict(m) for m in self.marks.get(soc or "", [])]

    def get(self, soc: str | None) -> dict[str, Any]:
        return self.profiles.get(soc or "", {})

    def detect(self, text: str) -> tuple[str | None, int]:
        """SoC whose boot-ROM fingerprint matches `text` best (None when nothing matches)."""
        best, best_n = None, 0
        for soc, p in self.profiles.items():
            n = sum(1 for rx in p.get("fingerprint", []) if re.search(rx, text))
            if n > best_n:
                best, best_n = soc, n
        return best, best_n

    def download_mode(self, soc: str | None) -> dict[str, str]:
        return dict(self.get(soc).get("download_mode") or {})

    def in_download_mode(self, console_text: str, fastboot_product: str) -> bool:
        """Does any profile recognise this console text / fastboot product as download mode?"""
        for p in self.profiles.values():
            dm = p.get("download_mode") or {}
            if dm.get("console") and re.search(dm["console"], console_text):
                return True
            if dm.get("fastboot") and re.search(dm["fastboot"], fastboot_product or ""):
                return True
        return False

    def looks_like_boot(self, text: str) -> bool:
        """Does this console text look like a board coming out of reset (boot ROM, boot loader or kernel banner),
        rather than a shell someone is typing in? Any SoC's fingerprint, download-mode or boot-stage line counts,
        and the usual boot banners of boards bsp has no profile for."""
        if self.detect(text)[0] or self.in_download_mode(text, ""):
            return True
        for p in self.profiles.values():
            for stage in (p.get("identify") or {}).get("sequence") or []:
                if stage.get("console") and re.search(stage["console"], text):
                    return True
        return any(re.search(rx, text) for rx in BOOT_BANNERS)

    def usb_vids(self) -> list[str]:
        return sorted({v.lower() for p in self.profiles.values() for v in p.get("usb_vids", [])})


def _portable(pattern: str) -> str | None:
    """Why a pattern is not one Python and JavaScript read alike, or None. Groups are plain, `(?:`, `(?=` or `(?!`:
    no lookbehind or named groups (`(?<`, `(?P`), and no inline flags (`(?i)`), which a combined pattern cannot
    carry. It must also compile."""
    for bad in ("(?<", "(?P"):              # anywhere, even escaped: a mark is better plain than ambiguous
        if bad in pattern:
            return f"`{bad}`: no lookbehind or named groups"
    i, escaped, in_class = 0, False, False
    while i < len(pattern):
        ch = pattern[i]
        if escaped:
            escaped = False
        elif ch == "\\":
            escaped = True
        elif in_class:
            in_class = ch != "]"
        elif ch == "[":
            in_class = True
            if pattern[i + 1:i + 2] == "]":       # a `]` first in a class is a literal
                i += 1
        elif ch == "(" and pattern[i + 1:i + 2] == "?" and pattern[i + 2:i + 3] not in (":", "=", "!"):
            return f"`{pattern[i:i + 4]}`: only (?:, (?= and (?! groups"
        i += 1
    try:
        re.compile(pattern)
    except re.error as e:
        return str(e)
    return None


def _boot_marks(soc: str, prof: dict[str, Any]) -> list[dict[str, Any]]:
    """`rom` from the fingerprint, then each `boot_marks` entry: a new id is added, a known one has the fields it
    gives replaced. A mark that does not hold up is left out, with a warning."""
    marks: dict[str, dict[str, Any]] = {}
    prints = []
    for rx in prof.get("fingerprint") or []:
        why = _portable(str(rx))
        if why:
            log.warning("soc %s: fingerprint %r is left out of the rom mark: %s", soc, rx, why)
        else:
            prints.append(f"(?:{rx})")
    if prints:
        marks["rom"] = {"id": "rom", "title": "Boot ROM", "pattern": "|".join(prints), "flags": "", "stage": 0,
                         "tone": "rom"}
    for raw in prof.get("boot_marks") or []:
        if not isinstance(raw, dict) or not isinstance(raw.get("id"), str) or not raw["id"]:
            log.warning("soc %s: boot mark %r is left out: it needs an id", soc, raw)
            continue
        mid = raw["id"]
        base = marks.get(mid) or {"id": mid, "title": mid, "flags": "", "stage": 3, "tone": "loader"}
        mark = {**base, **{k: raw[k] for k in ("title", "pattern", "flags", "stage", "tone") if raw.get(k) is not None}}
        why = (None if isinstance(mark.get("pattern"), str) and mark["pattern"] else "it needs a pattern") \
            or _portable(mark["pattern"]) \
            or (None if mark["flags"] in ("", "i") else f"flags {mark['flags']!r} is not \"\" or \"i\"") \
            or (None if isinstance(mark["stage"], int) and not isinstance(mark["stage"], bool)
                else f"stage {mark['stage']!r} is not a number") \
            or (None if mark["tone"] in TONES else f"tone {mark['tone']!r} is not one of {', '.join(TONES)}")
        if why:
            log.warning("soc %s: boot mark %r is left out: %s", soc, mid, why)
            continue
        marks[mid] = {"id": mid, "title": str(mark["title"]), "pattern": mark["pattern"], "flags": mark["flags"],
                      "stage": mark["stage"], "tone": mark["tone"]}
    return list(marks.values())
