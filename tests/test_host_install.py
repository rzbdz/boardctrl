"""The host installer preserves configuration which is not part of the program being upgraded."""

import subprocess
import sys
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]


def test_host_config_keeps_the_existing_announcement(tmp_path):
    """Run the installer's config fragment with shell facts supplied, never against a real host or service."""
    script = (ROOT / "deploy" / "bspd-install.sh").read_text()
    start = script.index('CONF=$SITE/hosts/$ID.yaml')
    end = script.index('# A token, if given', start)
    site = tmp_path / "site"
    conf = site / "hosts" / "test-host.yaml"
    conf.parent.mkdir(parents=True)
    conf.write_text("host: {id: test-host}\nserver:\n  announce:\n"
                    "    url: http://registry.test:8765\n    interval_s: 45\n"
                    "    addresses: [192.0.2.40]\n")
    prefix = tmp_path / "prefix"
    interpreter = prefix / "current/venv/bin/python"
    interpreter.parent.mkdir(parents=True)
    interpreter.symlink_to(sys.executable)
    subprocess.run(["bash", "-c", script[start:end]], check=True,
                   env={"PATH": "/usr/bin:/bin", "SITE": str(site), "ID": "test-host",
                        "MODEL": "test model", "BIND": "127.0.0.1", "PORT": "8765",
                        "BOARD": "", "RELAY": "none", "PREFIX": str(tmp_path / "prefix"),
                        "NEW": str(prefix / "current")})
    text = conf.read_text()
    import yaml
    config = yaml.safe_load(text)
    assert config["server"]["bind"] == "127.0.0.1"
    assert config["server"]["port"] == 8765
