"""`bsp hosts`: each reachable host with its modules, DUTs and artifact cache, and the ones it could not reach."""
from types import SimpleNamespace

from frontends.cli import dutcli


def test_hosts_report_their_cache():
    cache = {"path": "/var/cache/x", "used": 10, "entries": 2, "held": 0, "max_bytes": 100, "ttl_s": 60, "min_free_bytes": 5, "free": 1000}
    hosts = [
        SimpleNamespace(id="h1", transport=SimpleNamespace(label="http"), status={"modules": {"dut": {"duts": ["a"], "cache": cache}}}),
        SimpleNamespace(id="h2", transport=SimpleNamespace(label="ssh"), status={"modules": {"dut": {"duts": []}}}),
    ]
    fleet = SimpleNamespace(hosts=lambda: hosts, errors={"h3": "connection refused"}, mode="remote")
    out, text = dutcli.hosts(fleet, None)
    assert out == [
        {"host": "h1", "via": "http", "modules": ["dut"], "duts": ["a"], "cache": cache},
        {"host": "h2", "via": "ssh", "modules": ["dut"], "duts": [], "cache": None},
        {"host": "h3", "error": "connection refused"},
    ]
    assert "UNREACHABLE" in text
