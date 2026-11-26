"""`boardctrl dut pipe`: a raw byte pipe between stdin/stdout and a board's serial-mux socket.

The bytes are serial-mux's own protocol (length-prefixed JSON), so any serial-mux client can drive
the console through it. On a remote host the same bridge runs there under ssh as `python3 -c`
(hosts always have Python >= 3.10); this file is the single copy of that code. Stdlib only.
"""

from __future__ import annotations

import os
import shlex

BRIDGE = r'''
import os, select, socket

def connect(target):
    """A Unix socket path, or a loopback "127.0.0.1:port"."""
    if target.startswith("/") or os.path.exists(target):
        s = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        s.connect(target)
        return s
    host, _, port = target.rpartition(":")
    return socket.create_connection((host or "127.0.0.1", int(port)), timeout=5)

def bridge(target, fin, fout, hold=False):
    """Copy fin -> socket and socket -> fout until either side closes.

    `hold` is for a stream nobody writes to (a trace channel a page only reads): the end of fin is then the end
    of its input, not the end of the session, so the socket is read until it closes by itself.
    """
    s = connect(target)
    try:
        while True:
            ready, _, _ = select.select([fin, s] if fin is not None else [s], [], [])
            if s in ready:
                data = s.recv(65536)
                if not data:
                    return 0
                while data:
                    data = data[os.write(fout, data):]
            if fin is not None and fin in ready:
                data = os.read(fin, 65536)
                if not data:
                    if not hold:
                        return 0
                    fin = None
                    continue
                s.sendall(data)
    finally:
        s.close()
'''

_ns: dict = {}
exec(compile(BRIDGE, "<bsp-pipe>", "exec"), _ns)      # noqa: S102 - our own constant source
bridge = _ns["bridge"]


def remote_command(sock_path: str, hold: bool = False) -> str:
    """Shell command that runs the bridge on a host, connected to the ssh session's stdin/stdout."""
    code = BRIDGE + "\nimport sys\nsys.exit(bridge(sys.argv[1], 0, 1, len(sys.argv) > 2 and sys.argv[2] == '1'))\n"
    return f"python3 -c {shlex.quote(code)} {shlex.quote(sock_path)}{' 1' if hold else ''}"


def reachable(target: str) -> bool:
    """Whether this machine can open `target` itself (a Unix socket that exists, or any loopback address)."""
    return not target.startswith("/") or os.path.exists(target)


def local(target: str, hold: bool = False) -> int:
    return bridge(target, 0, 1, hold) if reachable(target) else 1
