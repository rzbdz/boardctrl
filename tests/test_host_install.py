"""The host installer preserves configuration which is not part of the program being upgraded."""

import subprocess
import sys
from pathlib import Path

import yaml

ROOT = Path(__file__).resolve().parents[1]


def _run_config(site: Path, conf: Path, prefix: Path, **facts: str) -> None:
    """Run the installer's config fragment with shell facts supplied, never against a real host or service."""
    script = (ROOT / "deploy" / "bspd-install.sh").read_text()
    start = script.index('CONF=$SITE/hosts/$ID.yaml')
    end = script.index('# A token, if given', start)
    conf.parent.mkdir(parents=True, exist_ok=True)
    interpreter = prefix / "current/venv/bin/python"
    interpreter.parent.mkdir(parents=True, exist_ok=True)
    interpreter.symlink_to(sys.executable)
    env = {"PATH": "/usr/bin:/bin", "SITE": str(site), "ID": "test-host", "MODEL": "test model",
           "BIND": "127.0.0.1", "PORT": "8765", "BOARD": "", "RELAY": "none", "SITE_HOSTS": "",
           "PREFIX": str(prefix), "NEW": str(prefix / "current"), **facts}
    subprocess.run(["bash", "-c", script[start:end]], check=True, env=env)


def test_host_config_keeps_the_existing_announcement(tmp_path):
    site, conf, prefix = tmp_path / "site", tmp_path / "site/hosts/test-host.yaml", tmp_path / "prefix"
    conf.parent.mkdir(parents=True)
    conf.write_text("host: {id: test-host}\nserver:\n  announce:\n"
                    "    url: http://registry.test:8765\n    interval_s: 45\n"
                    "    addresses: [192.0.2.40]\n")
    _run_config(site, conf, prefix)
    config = yaml.safe_load(conf.read_text())
    assert config["server"]["bind"] == "127.0.0.1"
    assert config["server"]["port"] == 8765
    assert config["server"]["state_dir"] == "/var/lib/bsp"
    assert config["server"]["announce"] == {"url": "http://registry.test:8765", "interval_s": 45,
                                            "addresses": ["192.0.2.40"]}


def test_host_config_keeps_a_provider_section_it_does_not_register(tmp_path):
    """A site installs a provider by naming it under `modules:` (the key a file written before providers uses): its
    block is the site's, so reinstalling keeps it as it is — while what the installer knows about the machine (gpio,
    sdk, the DUT's tools) is rewritten in the same key."""
    site, conf, prefix = tmp_path / "site", tmp_path / "site/hosts/test-host.yaml", tmp_path / "prefix"
    conf.parent.mkdir(parents=True)
    conf.write_text("""host: {id: test-host}
modules:
  ec:
    kit: modules/ec/vendor/com260-control-kit
    targets: {pico2: {pins: {swclk: "J40:11", swdio: "J40:13"}, speed_hz: 500000}}
  dut:
    slots: {s1: {alias: board-1}}
    serial_log_dir: /srv/old-logs
""")
    _run_config(site, conf, prefix, BOARD="spacemit-k3-com260", RELAY="sysfs")
    config = yaml.safe_load(conf.read_text())
    assert config["modules"]["ec"]["kit"] == "modules/ec/vendor/com260-control-kit"
    assert config["modules"]["ec"]["targets"]["pico2"]["pins"] == {"swclk": "J40:11", "swdio": "J40:13"}
    assert config["modules"]["gpio"] == {"board": "spacemit-k3-com260", "backend": "sysfs", "hw_timeout_s": 3.0,
                                         "pinmux": True, "max_pulse_ms": 60000, "protected": [], "channels": {},
                                         "aliases": {}}
    assert config["modules"]["dut"]["serial_log_dir"].endswith("/.serial-mux/logs")     # the installer's
    assert config["modules"]["dut"]["slots"] == {"s1": {"alias": "board-1"}}            # the site's, kept
    assert config["modules"]["dut"]["hook_dirs"] == [str(site / "hooks")]
    assert config["host"] == {"id": "test-host", "description": "test model (test-host)"}


def test_host_config_takes_a_providers_section_from_the_site_and_keeps_a_local_one(tmp_path):
    """A provider's settings travel in the site's copy of this host's config: the installer takes them for the providers
    it does not register, whole, and never for its own. A section the machine already has wins."""
    site, conf, prefix = tmp_path / "site", tmp_path / "site/hosts/test-host.yaml", tmp_path / "prefix"
    shipped = tmp_path / "shipped"
    conf.parent.mkdir(parents=True)
    conf.parent.mkdir(parents=True, exist_ok=True)
    shipped.mkdir()
    conf.write_text("""host: {id: test-host}
modules:
  ec: {enabled: true}
  dut: {serial_log_dir: /srv/local}
""")
    (shipped / "test-host.yaml").write_text("""host: {id: test-host}
modules:
  ec:
    kit: modules/ec/vendor/kit
    targets: {pico2: {pins: {swclk: "J40:11", swdio: "J40:13"}}}
  dut: {serial_log_dir: /srv/from-the-site}
  other: {listened: true}
""")
    _run_config(site, conf, prefix, SITE_HOSTS=str(shipped))
    config = yaml.safe_load(conf.read_text())
    # The provider's whole section is the site's: the machine only turned it on.
    assert config["modules"]["ec"]["kit"] == "modules/ec/vendor/kit"
    assert config["modules"]["ec"]["targets"]["pico2"]["pins"] == {"swclk": "J40:11", "swdio": "J40:13"}
    # The installer's own sections are its own, wherever the site also names them.
    assert config["modules"]["dut"]["serial_log_dir"].endswith("/.serial-mux/logs")
    # A provider the machine set itself keeps its setting.
    assert config["modules"]["other"] == {"listened": True}


def test_host_config_takes_the_announcement_from_the_site_when_it_has_none(tmp_path):
    """Without `server.announce` the host never tells the registry where it is, goes stale there, and gets no catalog
    changes. The site's copy of the host file carries it; a machine's own announcement wins."""
    site, conf, prefix = tmp_path / "site", tmp_path / "site/hosts/test-host.yaml", tmp_path / "prefix"
    shipped = tmp_path / "shipped"
    conf.parent.mkdir(parents=True)
    shipped.mkdir()
    conf.write_text("host: {id: test-host}\nserver: {bind: 0.0.0.0, port: 8765}\n")
    (shipped / "test-host.yaml").write_text("host: {id: test-host}\nserver:\n  announce: {url: http://registry.test:8765, interval_s: 60}\n")
    _run_config(site, conf, prefix, SITE_HOSTS=str(shipped))
    assert yaml.safe_load(conf.read_text())["server"]["announce"] == {"url": "http://registry.test:8765", "interval_s": 60}
    # Its own announcement is not replaced by the site's.
    conf.write_text("host: {id: test-host}\nserver:\n  announce: {url: http://own.test:8765}\n")
    _run_config(site, conf, prefix / "again", SITE_HOSTS=str(shipped))
    assert yaml.safe_load(conf.read_text())["server"]["announce"] == {"url": "http://own.test:8765"}


def test_host_config_with_no_board_has_no_gpio_section(tmp_path):
    """A fresh file is written with the key hosts use now (`providers:`), and an old one keeps the key it has."""
    site, conf, prefix = tmp_path / "site", tmp_path / "site/hosts/test-host.yaml", tmp_path / "prefix"
    _run_config(site, conf, prefix)
    config = yaml.safe_load(conf.read_text())
    assert "modules" not in config, "a new file says providers"
    assert "gpio" not in config["providers"] and config["providers"]["sdk"] == {}
    assert conf == tmp_path / "site/hosts/test-host.yaml"

    old = tmp_path / "site/hosts/old.yaml"
    old.write_text("host: {id: old}\nmodules:\n  dut: {serial_log_dir: /srv/old}\n")
    _run_config(site, old, tmp_path / "prefix-old", ID="old")
    kept = yaml.safe_load(old.read_text())
    assert "providers" not in kept, "a file written with modules keeps saying modules"
    assert kept["modules"]["dut"]["serial_log_dir"].endswith("/.serial-mux/logs")


def test_takeover_moves_the_old_token_and_reads_it_before_the_old_file():
    """A host taken over from `bspd` keeps the token it had, in the new place, and the old file is not left where a
    program reading both could pick the wrong one. The unit reads the old file first, so the new token counts."""
    script = (ROOT / "deploy" / "bspd-install.sh").read_text()
    start, end = script.index("if [[ -f /etc/systemd/system/$OLD_UNIT"), script.index('if [[ -n $UNINSTALL ]]')
    takeover = script[start:end]
    # The old token moves only where the new place has none: an empty new file must not leave the host without one.
    assert "cp -a /etc/bsp/hostd.env /etc/boardctrl/boardctrld.env" in takeover
    assert "BOARDCTRL_TOKEN|BSP_TOKEN)=" in takeover
    assert "rm -f /etc/bsp/hostd.env" in takeover


def test_the_takeover_keeps_the_old_token_when_the_new_file_has_none(tmp_path):
    """The installer creates an empty `/etc/boardctrl/boardctrld.env` when it is given no token. A host that still has
    the old file's token must keep it (its old unit read that file), not be left with an empty file and no token."""
    script = (ROOT / "deploy" / "bspd-install.sh").read_text()
    start, end = script.index("  if [[ -f /etc/bsp/hostd.env ]]; then"), script.index("if [[ -n $UNINSTALL ]]")
    # The takeover's own closing `fi` is not part of the fragment (only the `if` it contains is).
    body = script[start:end]
    inner = body.strip()
    fragment = inner[:inner.rfind("\nfi")]
    root = tmp_path / "etc"
    for case, new, old, expected in [
        # An empty new file: the old token is kept, under both names, so either reader takes it.
        ("empty new file", "", "BSP_TOKEN=A\n", "BOARDCTRL_TOKEN=A"),
        # A new file with its own token: the old one does not overwrite it.
        ("new file with a token", "BOARDCTRL_TOKEN=B\n", "BSP_TOKEN=A\n", "BOARDCTRL_TOKEN=B"),
        ("no new file", None, "BSP_TOKEN=A\n", "BSP_TOKEN=A"),
        # A new file with only one of the two names: the other is filled in with the same token, so either reader
        # takes it rather than reading a stale name from somewhere else.
        ("new file, one name", "BOARDCTRL_TOKEN=B\n", "", "BSP_TOKEN=B"),
    ]:
        for path in (root / "boardctrl", root / "bsp"):
            if path.exists():
                for f in path.iterdir():
                    f.unlink()
        (root / "boardctrl").mkdir(parents=True, exist_ok=True)
        (root / "bsp").mkdir(parents=True, exist_ok=True)
        if new is not None:
            (root / "boardctrl/boardctrld.env").write_text(new)
        (root / "bsp/hostd.env").write_text(old)
        body = fragment.replace("/etc/", f"{root}/")
        subprocess.run(["bash", "-c", body], check=True, env={"PATH": "/usr/bin:/bin"})
        read = (root / "boardctrl/boardctrld.env").read_text()
        assert expected in read, case
        assert not (root / "bsp/hostd.env").exists(), case
        # Both names say the same thing, and the old file's value where it has one: the daemon and a sync agree.
        values = {line.split("=", 1)[0]: line.split("=", 1)[1] for line in read.splitlines() if "=" in line}
        assert values.get("BOARDCTRL_TOKEN", values.get("BSP_TOKEN")) == values.get("BSP_TOKEN", values.get("BOARDCTRL_TOKEN")), case
    old = script.index("EnvironmentFile=-/etc/bsp/hostd.env")
    new = script.index("EnvironmentFile=-/etc/boardctrl/boardctrld.env")
    assert old < new


def test_the_gpio_backend_is_chosen_by_a_python_that_has_gpiod_not_whatever_is_first_on_path(tmp_path):
    """A host whose first python3 is a tool's venv without gpiod (serial-mux's, say) still gets the gpiod backend when
    the machine's own python has the bindings: the backend needs them, and a site plugin may run the system python."""
    bin = tmp_path / "bin"
    bin.mkdir()
    fake = bin / "python3"                                    # first on PATH, no gpiod
    fake.write_text("#!/bin/sh\nexit 1\n")
    fake.chmod(0o755)
    system = tmp_path / "system-python"
    system.write_text("#!/bin/sh\nexit 0\n")                  # the machine's python, with gpiod
    system.chmod(0o755)
    script = (ROOT / "deploy" / "bspd-install.sh").read_text()
    start = script.index('[[ $BOARD == none ]] && BOARD=""')
    end = script.index("# The code: a fresh venv", start)
    run = subprocess.run(
        # `ls` stands in for the gpio character devices, which this machine may not have.
        ["bash", "-c", f'ls() {{ return 0; }}; {script[start:end].strip()}; echo "RELAY=$RELAY"'],
        env={"PATH": f"{bin}:/usr/bin:/bin", "BOARD": "spacemit-k3-com260", "RELAY": "auto",
             "SYSTEM_PYTHON": str(system)}, capture_output=True, text=True)
    assert run.returncode == 0, run.stderr
    assert "RELAY=gpiod" in run.stdout, run.stdout
    assert str(system) in run.stdout                              # it says which python has the bindings



def test_a_gpio_backend_the_host_file_states_is_kept_over_detection(tmp_path):
    """gpiod and sysfs cannot share a line: a line exported through sysfs is busy for gpiod, so every reset fails with
    EBUSY. A backend the site's copy of the host file (or the machine's own file) names is kept on reinstall, even where
    gpiod would be detected; `--relay` still chooses, and a file that names none leaves it to detection."""
    system = tmp_path / "system-python"
    system.write_text("#!/bin/sh\nexit 0\n")                  # gpiod importable: detection alone would say gpiod
    system.chmod(0o755)
    shipped, site = tmp_path / "shipped", tmp_path / "site"
    (shipped).mkdir()
    (site / "hosts").mkdir(parents=True)
    script = (ROOT / "deploy" / "bspd-install.sh").read_text()
    start = script.index('[[ $BOARD == none ]] && BOARD=""')
    end = script.index("# The code: a fresh venv", start)

    def relay(relay="auto", site_hosts=""):
        run = subprocess.run(
            ["bash", "-c", f'ls() {{ return 0; }}; {script[start:end].strip()}; echo "RELAY=$RELAY"'],
            env={"PATH": "/usr/bin:/bin", "BOARD": "spacemit-k3-com260", "RELAY": relay, "ID": "test-host",
                 "SITE": str(site), "SITE_HOSTS": site_hosts, "SYSTEM_PYTHON": str(system)},
            capture_output=True, text=True)
        assert run.returncode == 0, run.stderr
        return run.stdout.strip().splitlines()[-1]

    assert relay() == "RELAY=gpiod"                                         # nothing stated: detected
    (site / "hosts/test-host.yaml").write_text(
        "host: {id: test-host}\nproviders:\n  ec:\n    backend: other\n  gpio:\n    board: b\n    backend: gpiod\n")
    (shipped / "test-host.yaml").write_text(
        "host: {id: test-host}\nproviders:\n  gpio:\n    board: b\n    backend: sysfs   # lines stay exported\n")
    assert relay(site_hosts=str(shipped)) == "RELAY=sysfs"                  # the site's copy decides
    (site / "hosts/test-host.yaml").write_text("host: {id: test-host}\nmodules:\n  gpio_relay:\n    backend: sysfs\n")
    assert relay() == "RELAY=sysfs"                                         # the machine's own (older) file
    assert relay(relay="gpiod", site_hosts=str(shipped)) == "RELAY=gpiod"   # --relay still chooses
