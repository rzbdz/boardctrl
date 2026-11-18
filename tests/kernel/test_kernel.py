"""The kernel's extension points: usage claims on the GPIO service, sessions, site plugins and role providers."""

from __future__ import annotations

from dataclasses import dataclass

import json
from pathlib import Path
from typing import Iterable

import pytest

from bsp.core.errors import Conflict, Forbidden, InvalidRequest, NotFound
from bsp.core.plugins import Plugins
from bsp.core.providers import PROVIDERS, describe
from bsp.core.sessions import Session, Sessions, unit_name
from bsp.dut import services as bindings
from bsp.dut.catalog import spec_from_dict


# ----------------------------------------------------------------- gpio claims
def test_a_usage_claims_pins_and_others_cannot_touch_them(make_service):
    svc = make_service(claims_path=None)
    claim = svc.claim("swd", {"swclk": "P11", "swdio": "P13"})
    assert set(claim.pins) == {"swclk", "swdio"}
    assert svc.get("P11")["usage"] == "swd"
    with pytest.raises(Conflict):
        svc.configure("P11", {"mode": "relay"})
    with pytest.raises(Conflict):
        svc.alias_add("DUT1/reset", "P11")
    with pytest.raises(Conflict):
        svc.set_level("P13", "high")
    with pytest.raises(Conflict):          # another usage cannot take a claimed pin
        svc.claim("jtag", {"tck": "P11"})
    assert [c["usage"] for c in svc.claims()] == ["swd"]
    assert claim.release() == ["J40:11", "J40:13"]
    svc.configure("P11", {"mode": "relay"})   # free again


def test_claim_refuses_protected_pins_and_two_roles_on_one_pin(make_service):
    svc = make_service(protected=["P11"])
    with pytest.raises(Forbidden):
        svc.claim("swd", {"swclk": "P11"})
    with pytest.raises(InvalidRequest):
        svc.claim("swd", {"swclk": "P13", "swdio": "P13"})


def test_claims_survive_a_restart(make_service, tmp_path):
    path = tmp_path / "claims.json"
    make_service(claims_path=path).claim("swd", {"swclk": "P11"})
    again = make_service(claims_path=path)
    assert again.get("P11")["usage"] == "swd"
    assert again.claim_view("swd", "swclk=P11")["pins"] == {"swclk": "J40:11"}   # its own claim re-taken


def test_claim_pin_parsing(make_service):
    svc = make_service()
    with pytest.raises(InvalidRequest):
        svc.claim_view("swd", "swclk")
    with pytest.raises(InvalidRequest):
        svc.claim_view("swd", "")


# -------------------------------------------------------------------- sessions
class FakeSystemd:
    """The process table a session acts on, without systemd."""

    def __init__(self):
        self.units: dict[str, dict] = {}

    def start(self, unit, command, env=None):
        self.units[unit] = {"command": command, "env": env or {}, "active": True, "pid": 4242}

    def stop(self, unit):
        if unit in self.units:
            self.units[unit]["active"] = False

    def reset(self, unit):
        self.units.pop(unit, None)

    def active(self, unit):
        return bool(self.units.get(unit, {}).get("active"))

    def pid(self, unit):
        return self.units.get(unit, {}).get("pid")


def test_a_session_starts_waits_for_ready_and_is_stopped_by_reaping(tmp_path):
    systemd = FakeSystemd()
    sessions = Sessions(tmp_path / "sessions", systemd)
    s = sessions.start("k1-a", "debug", "fake", ["fake-server"], endpoint="127.0.0.1:3334", lease="tok")
    assert s.endpoint == "127.0.0.1:3334" and s.lease == "tok"
    assert systemd.units[s.unit]["command"] == ["fake-server"]
    assert sessions.get("k1-a", "debug").role == "debug"
    # starting twice keeps the same unit (a reader does not restart the server under another reader's feet)
    assert sessions.start("k1-a", "debug", "fake", ["fake-server"], endpoint="x").unit == s.unit
    assert sessions.reap("k1-a") == ["debug"]
    assert sessions.get("k1-a", "debug") is None


def test_a_session_whose_server_dies_at_once_is_an_error(tmp_path):
    class Dead(FakeSystemd):
        def start(self, unit, command, env=None):
            self.units[unit] = {"command": command, "active": False, "pid": None}

    with pytest.raises(Exception) as e:
        Sessions(tmp_path, Dead()).start("k1-a", "console", "fake", ["x"], timeout=5)
    assert "exited at once" in str(e.value)


def test_a_server_that_comes_up_without_answering_is_stopped_not_left_running(tmp_path):
    """
    A tool can start and never answer (a kit supervisor whose port stays shut): its unit would keep running with no
    record, holding the ports, the pins and its own lock, and `debug stop` could not find it.
    """
    class Never(FakeSystemd):
        started: list[str] = []

        def start(self, unit, command, env=None):
            super().start(unit, command, env)
            self.started.append(unit)

    systemd = Never()
    stopped: list[str] = []

    class Watch(Never):
        def stop(self, unit):
            stopped.append(unit)
            super().stop(unit)

    with pytest.raises(Exception) as e:
        Sessions(tmp_path, Watch()).start("k1-a", "debug", "fake", ["serve"], endpoint=None, timeout=0.3)
    assert "did not come up" in str(e.value)
    unit = unit_name("k1-a", "debug")
    assert stopped == [unit]                                    # it was stopped, not left restarting


def test_a_session_outliving_its_lease_is_reaped_rather_than_handed_on(tmp_path):
    """
    A debug server runs under a lease. When that lease runs out the board is someone else's, and the next holder must
    not inherit a session pointed at the old holder's breakpoint: reading it reaps it.
    """
    live = {"tok-a": True}

    class Stopped(FakeSystemd):
        stops: list[str] = []

        def stop(self, unit):
            self.stops.append(unit)
            super().stop(unit)

    systemd = Stopped()
    sessions = Sessions(tmp_path / "sessions", systemd, held=lambda token: bool(token) and live.get(token, False))
    started = sessions.start("k1-a", "debug", "fake", ["serve"], endpoint="127.0.0.1:3334", lease="tok-a")
    assert sessions.get("k1-a", "debug") is not None                 # the holder's own lease is live
    live["tok-a"] = False                                           # it ran out
    assert sessions.get("k1-a", "debug") is None
    assert systemd.stops == [started.unit]                           # the server was stopped, not handed on
    assert sessions.list() == []


def test_reaping_after_the_lease_ended_still_reports_what_it_stopped(tmp_path):
    """
    The lease is released and the sessions are reaped right after — the order the DUT module uses. The release has
    already made the lease dead, so reading the records through the usual filter stops and forgets the session
    before `reap` sees it: `dut release` would then answer "nothing was stopped" although the server was taken down,
    and the caller (a page, an agent) would have no idea the debugger went with the board.
    """
    live = {"tok-a": True}

    class Stopped(FakeSystemd):
        stops: list[str] = []

        def stop(self, unit):
            self.stops.append(unit)
            super().stop(unit)

    systemd = Stopped()
    sessions = Sessions(tmp_path / "sessions", systemd, held=lambda token: bool(token) and live.get(token, False))
    sessions.start("k1-a", "debug", "fake", ["serve"], endpoint="127.0.0.1:3334", lease="tok-a")
    live["tok-a"] = False                                        # the lease is released...
    assert sessions.reap("k1-a") == ["debug"]                    # ...and the release reaps its sessions
    assert systemd.stops == [unit_name("k1-a", "debug")]         # once, not twice
    assert sessions.list() == []


def test_stop_needs_the_lease_that_started_it(tmp_path):
    sessions = Sessions(tmp_path, FakeSystemd())
    sessions.start("k1-a", "debug", "fake", ["s"], endpoint="e", lease="mine")
    with pytest.raises(Conflict):
        sessions.stop("k1-a", "debug", lease="other")
    assert sessions.stop("k1-a", "debug", lease="mine")["stopped"] is True


# --------------------------------------------------------------------- plugins
PLUGIN = '''
def setup(ctx, api, config):
    api.service("plugin_seen", config)

    def factory(ctx2, spec, services):
        return {"dut": spec.id, "config": config}

    api.provider("debug", "fake-swd", factory, title="fake SWD")
'''

BROKEN = '''
def setup(ctx, api):
    raise RuntimeError("no hardware here")
'''


def test_a_site_plugin_registers_a_provider_and_takes_its_config(tmp_path):
    site = tmp_path / "site"
    (site / "modules" / "fake").mkdir(parents=True)
    (site / "modules" / "fake" / "module.py").write_text(PLUGIN)
    (site / "modules" / "broken").mkdir(parents=True)
    (site / "modules" / "broken" / "module.py").write_text(BROKEN)

    class Ctx:
        services: dict = {}

    plugins = Plugins([site / "modules"])
    assert plugins.available() == {"fake": site / "modules" / "fake", "broken": site / "modules" / "broken"}
    plugins.load(["fake", "broken", "missing"], Ctx(), {"fake": {"pins": "J40:11"}})
    assert "fake-swd" in PROVIDERS["debug"].names()
    assert Ctx.services["plugin_seen"] == {"pins": "J40:11"}
    assert "no hardware here" in plugins.errors["broken"]
    assert "missing" in plugins.errors and plugins.errors["missing"].startswith("no plugin directory")
    summary = plugins.summary()
    assert summary["loaded"]["fake"]["providers"] == ["debug/fake-swd"]


def test_providers_report_their_roles():
    from bsp.dut import console_providers, debug_providers  # noqa: F401 - register the built-ins

    described = describe()
    assert set(described) == {"relay", "console", "download", "flash", "debug"}
    assert "serial-mux" in described["console"]["names"]
    assert "none" in described["debug"]["names"]


# -------------------------------------------------------------- role bindings
SPEC = {"id": "k1-a", "host": "h", "board": "b", "console": {"alias": "tty-a"},
        "fastboot": {"usb": "usb:2-1.4"}, "control": {"backend": "human", "reset": {"pin": "P11"}}}


def test_old_records_bind_the_default_providers():
    spec = spec_from_dict(dict(SPEC))
    b = bindings.bindings(spec)
    assert b["console"]["provider"] == "serial-mux" and b["console"]["alias"] == "tty-a"
    assert b["download"]["provider"] == "fastboot" and b["download"]["usb"] == "usb:2-1.4"
    assert b["relay"]["provider"] == "human" and b["relay"]["reset"] == {"pin": "P11"}
    assert b["debug"]["provider"] == "none"


def test_a_record_can_name_every_provider():
    spec = spec_from_dict({**SPEC, "services": {
        "console": {"provider": "trace", "port": 19022}, "flash": {"provider": "my-flasher"},
        "debug": {"provider": "my-swd", "target": "probe0"}, "download": {"provider": "none"}}})
    b = bindings.bindings(spec)
    assert b["console"] == {"provider": "trace", "port": 19022}
    assert b["debug"]["target"] == "probe0"
    assert bindings.describe(spec)["flash"] == {"provider": "my-flasher"}


def test_dut_set_fields_become_the_services_block():
    spec = spec_from_dict(dict(SPEC))
    block = bindings.services_from_fields(spec, debug_provider="my-swd", console_provider="trace")
    assert block["debug"] == {"provider": "my-swd"} and block["console"] == {"provider": "trace"}
    assert bindings.services_from_fields(spec, board="x") is None


def test_a_guarded_backend_still_says_where_a_line_is():
    """The timeout guard wraps every call a service makes; a site plugin that drives a line itself needs the chip and
    offset behind it, and asked the guard, which used to forward only the driving methods."""
    from bsp.core.errors import HardwareError
    from bsp.gpio.backends.base import GpioBackend, LineStatus
    from bsp.gpio.backends.guarded import GuardedBackend

    class Located(GpioBackend):
        name = "located"

        def locate(self, gpio):
            return ("/dev/gpiochip3", gpio - 96)

        def set_output(self, gpio, value): ...
        def set_input(self, gpio): ...
        def free(self, gpio): ...
        def status(self, gpio): return LineStatus(claimed=False, direction=None, value=None)

    assert GuardedBackend(Located()).locate(118) == ("/dev/gpiochip3", 22)

    class Drives(GpioBackend):
        name = "drives"

        def set_output(self, gpio, value): ...
        def set_input(self, gpio): ...
        def free(self, gpio): ...
        def status(self, gpio): return LineStatus(claimed=False, direction=None, value=None)

    # One that cannot say refuses, saying which backend and which line: a caller then configures chip and line itself.
    with pytest.raises(HardwareError, match="drives.*GPIO118"):
        GuardedBackend(Drives()).locate(118)


class _Busy:
    """A backend whose lines someone else holds, as a stale sysfs export or another program looks."""

    name = "busy"
    consumer = "bsp"

    def __init__(self, free: Iterable[int] = ()):
        from bsp.gpio.backends.mock import MockBackend

        self.inner = MockBackend()
        self.free = set(free)

    def __getattr__(self, item):
        return getattr(self.inner, item)

    def status(self, gpio):
        from bsp.gpio.backends.base import LineStatus

        if gpio in self.free:
            return self.inner.status(gpio)
        return LineStatus(gpio, True, "in", None, "sysfs")


def test_a_claim_refuses_a_line_someone_else_holds(make_service):
    """A stale export is found when the claim is taken, not later as an EBUSY inside the feature's server."""
    from bsp.gpio.backends.mock import MockBackend

    svc = make_service()
    svc.backend = _Busy(free=[62])
    with pytest.raises(Conflict) as e:
        svc.claim("swd", {"swclk": "J40:11", "swdio": "J40:13"})
    assert "held by sysfs" in e.value.message and "J40:11" in e.value.message
    assert svc.claims() == []                    # nothing half-taken
    svc.backend = MockBackend()                  # the export released: the same claim now succeeds
    assert svc.claim("swd", {"swclk": "J40:11", "swdio": "J40:13"}).usage == "swd"

def test_two_boards_with_no_download_port_and_no_console_do_not_clash():
    """A board can have neither (a bare debug link on header pins): "no usb, no console" is not a port two boards share."""
    from bsp.dut.catalog import shared_console, shared_usb

    @dataclass
    class Board:
        id: str
        usb: str | None = None
        console_alias: str | None = None

    bare, other = Board("ec"), Board("ec2")
    assert not shared_usb(bare, other) and not shared_console(bare, other)
    # A port that is there is still one board's alone.
    assert shared_usb(Board("a", usb="usb:2-1.4"), Board("b", usb="usb:2-1.4"))
    assert shared_console(Board("a", console_alias="s1"), Board("b", console_alias="s1"))



def test_a_start_whose_lease_ended_while_it_was_prepared_refuses(tmp_path):
    """
    `debug start` checks the lease, then makes a unit; `dut release` deletes the lease and stops the board's
    sessions. If the release lands between those two steps the old start would bring a server up for a board that
    is now someone else's — which is exactly what the lease exists to prevent. The start re-checks the lease under
    the lock a release takes, and refuses.
    """
    live = {"tok-a": True}
    sessions = Sessions(tmp_path / "sessions", FakeSystemd(), held=lambda token: bool(token) and live.get(token, False))
    live["tok-a"] = False                                        # the lease ended before the unit was made
    with pytest.raises(Conflict):
        sessions.start("k1-a", "debug", "fake", ["serve"], endpoint="127.0.0.1:1", lease="tok-a")
    assert sessions.list() == [] and sessions._records() == []


def test_a_record_that_cannot_be_read_is_forgotten_by_reap_stale(tmp_path):
    """A crash can leave a half-written record; the cleanup that promises to forget those must actually forget them."""
    sessions = Sessions(tmp_path / "sessions", FakeSystemd())
    (tmp_path / "sessions").mkdir(exist_ok=True)
    broken = tmp_path / "sessions" / "broken.debug.json"
    broken.write_text("{not json")
    wrong = tmp_path / "sessions" / "wrong.debug.json"
    wrong.write_text('{"dut": ["k1-a"], "role": "debug"}')
    assert sessions.reap_stale() == []
    assert not broken.exists() and not wrong.exists()


def test_a_session_whose_stop_did_not_take_is_kept_and_not_reported_stopped(tmp_path):
    """A stop can be refused or hang. Reporting it stopped would hide a server still holding the pins under a lease
    nobody has any more; the record stays, so a later release or reap can try again."""
    class Stubborn(FakeSystemd):
        def stop(self, unit):
            pass                                   # systemd refused: the unit is still active

    systemd = Stubborn()
    sessions = Sessions(tmp_path / "sessions", systemd)
    sessions.start("k1-a", "debug", "fake", ["serve"], endpoint="127.0.0.1:1")
    assert sessions.reap("k1-a") == []
    assert sessions.list()[0].role == "debug"      # still there, to be stopped again
