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
           "BIND": "127.0.0.1", "PORT": "8765", "BOARD": "", "RELAY": "none",
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


def test_host_config_keeps_a_plugin_section_it_does_not_register(tmp_path):
    """A site installs a plugin by naming it in modules: its block is the site's, so reinstalling keeps it as it is —
    while what the installer knows about the machine (gpio, sdk, the dut module's tools) is rewritten."""
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


def test_host_config_with_no_board_has_no_gpio_section(tmp_path):
    site, conf, prefix = tmp_path / "site", tmp_path / "site/hosts/test-host.yaml", tmp_path / "prefix"
    _run_config(site, conf, prefix)
    config = yaml.safe_load(conf.read_text())
    assert "gpio" not in config["modules"] and config["modules"]["sdk"] == {}
