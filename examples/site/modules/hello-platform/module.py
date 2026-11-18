"""hello-platform: a whole platform's roles, answered with "hello".

This is the template for a site plugin — the shape an adapter for a real platform (an EC over two
GPIO pins, a vendor debug pod, a board farm's own API) takes, with the hardware left out. It
registers two roles:

  console  a byte stream with history and one-shot commands
  flash    writing an image or a package to the board

Every method below returns a made-up answer. Nothing here opens a port, takes a lock, starts a
process or looks at a pin: the point is the *shape* a platform fills, so a person can copy this
directory, rename it, and replace each `hello …` with their tool's own call.

A plugin is loaded by a board host from `<site>/modules/<name>/module.py`, and only when the host's
config names it (`modules: {hello-platform: {}}` or `plugins: [hello-platform]`). Its DUT record
then binds the providers it registers:

    services:
      console: {provider: hello-console, greeting: holá}
      flash:   {provider: hello-flash, expect_version: "V1.2"}

`setup(ctx, api, config)` receives the host config's `modules.hello-platform` section as `config`.
"""

from __future__ import annotations

from typing import Any

from core.errors import InvalidRequest
from core.providers import Field
from modules.dut.console import ConsoleProvider


# ------------------------------------------------------------------ console --
class HelloConsole(ConsoleProvider):
    """A console that answers "hello": history, a command, and a live stream, all made up.

    Inheriting `ConsoleProvider` gives the defaults (no socket, no daemon to start, reachable) and makes the
    rest of the contract explicit: a real adapter overrides exactly the methods its transport needs.
    """

    provider_name = "hello-console"

    @classmethod
    def fields(cls) -> list[Field]:
        """The values this provider reads from a DUT's `services.console` block (a page draws this form)."""
        return [Field("greeting", "the word it greets with", default="hello")]

    def __init__(self, spec: Any, config: dict[str, Any] | None = None):
        super().__init__(spec, config)
        self.greeting = self.config.get("greeting") or "hello"

    # The bytes a reader saw since a mark; a provider that cannot re-read returns nothing.
    def mark(self) -> Any:
        return 0

    def read_since(self, mark: Any) -> str:
        return ""

    def tail(self, lines: int = 50, grep: str | None = None) -> list[str]:
        """The last lines of the console (passive: no keystrokes)."""
        return [f"{self.greeting} from {self.spec.id}"]

    def range(self, since: str | None, until: str | None, grep: str | None = None, regex: bool = False,
              limit: int = 20000, first: bool = False) -> dict[str, Any]:
        """Lines stamped from `since` to `until`, optionally searched."""
        return {"lines": [f"{self.greeting} (no log kept)"], "matched": 0, "truncated": False}

    def send(self, text: str, pattern: str | None = None, fail: str | None = None, timeout: float = 60,
             mark: Any = None) -> dict[str, Any]:
        """Type `text`, wait for `pattern`, return what came back."""
        return {"sent": text, "output": f"{self.greeting} after {text!r}"}

    def wait(self, pattern: str | None, fail: str | None = None, timeout: float = 60, mark: Any = None) -> str:
        return f"{self.greeting} already"

    def run(self, command: str, expect: str, timeout: float) -> dict[str, Any]:
        """Run one shell command and return its output."""
        return {"command": command, "output": f"{self.greeting} to `{command}`"}

    def describe(self) -> dict[str, Any]:
        return {"provider": self.provider_name, "greeting": self.greeting}


# -------------------------------------------------------------------- flash --
class HelloFlash:
    """A flasher that reports a made-up success, with the settings a real one would take."""

    provider_name = "hello-flash"

    @classmethod
    def fields(cls) -> list[Field]:
        return [Field("expect_version", "the version the board should report after the flash", required=True)]

    def __init__(self, spec: Any, config: dict[str, Any] | None = None):
        self.spec = spec
        self.config = dict(config or {})

    def flash(self, package, *, log, timeout: float) -> dict[str, Any]:
        """Write `package` (a local file) to the board. The host never retries a flasher."""
        expect = self.config.get("expect_version")
        if not expect:
            # A real flasher needs to know what it is proving: refuse rather than report a success it cannot check.
            raise InvalidRequest(f"hello-flash on {self.spec.id} needs expect_version",
                                 hint='record it: services: {flash: {provider: hello-flash, expect_version: "V1.2"}}')
        log(f"hello: pretending to write {getattr(package, 'name', package)}")
        return {"written": str(getattr(package, "name", package)), "expect_version": expect,
                "runtime_verified": True, "message": "hello — nothing was actually written"}


# ------------------------------------------------------------------- setup --
def setup(ctx, api, config):
    """Register this plugin's providers; `config` is the host config's `modules.hello-platform` section."""
    api.provider("console", "hello-console", lambda c, spec, services: HelloConsole(spec, {**config, **(spec.raw.get("services") or {}).get("console", {})}),
                 title="hello console", about="a console that greets and echoes, with no hardware behind it")
    api.provider("flash", "hello-flash", lambda c, spec, services: HelloFlash(spec, {**config, **(spec.raw.get("services") or {}).get("flash", {})}),
                 title="hello flasher", about="a flasher that reports success without writing anything")


# `python -c "import module"` in this directory checks the file; keep this import quiet for that use.
if __name__ == "__main__":
    print("hello-platform: registers console/hello-console and flash/hello-flash")
