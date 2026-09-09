"""Failsafe wrapper around any local server a browser-automation session needs up.

Not a connector, not shipped in `backglass/`. This is the missing half of the
2026-09-08 stack decision (memory: browser-automation-stack-decision.md): mcp-safari /
claude-in-chrome drive the browser, but *starting* the thing they point at (most often
`backglass dashboard`) was being done by hand — `nohup ... &` then a manual `pkill` after.
That leaves an orphaned server on a crash, an interrupt, or a forgotten cleanup step, and
an orphan on :8765 is exactly the kind of silent failure CLAUDE.md's `state` command exists
to catch — except nothing calls `state` mid-automation, so it would sit there until the
next session tripped over "address already in use".

Guarantees, each one the reason this exists rather than another `nohup` one-liner:

- The child is torn down on every exit path — success, exception, Ctrl-C, `kill`. First
  draft claimed a bare `try/finally` was enough; it isn't, and both halves of the actual
  fix were found by testing the interrupt path rather than reading the code and believing
  it:
  - Backgrounded (`cmd &`) is not a special case only in the obvious way. POSIX also has
    the shell set SIGINT to ignored for a backgrounded child *before exec*, so a plain
    `except KeyboardInterrupt` never fires there — confirmed with a two-line throwaway
    script (`kill -INT` on a backgrounded `time.sleep` loop: no exception, still alive).
    CPython's own startup, in turn, leaves SIGINT ignored if it was already ignored at
    exec, so it stays silently inert.
  - A bare SIGTERM — the harness's own teardown signal for anything it promotes to a
    background task — hits Python's default disposition, which the OS terminates on the
    spot. That happens beneath the interpreter: no exception is raised, so no `finally`,
    no `with` cleanup, no `atexit` runs. A `finally` block alone is only a guarantee
    against exceptions *raised in Python*, not against a signal killing the process out
    from under it.
  Both need an explicit `signal.signal(...)` handler that turns the signal into a raised
  exception the `with dashboard_up(...)` block can unwind through — installed at startup,
  which overrides whatever disposition was inherited from the parent shell.
- Startup is verified, not assumed. `nohup ... &; sleep N` believes the sleep; this polls
  the actual URL and also checks the process is still alive between polls, so a server
  that dies on startup fails fast with its own log instead of a 30-second wait for nothing.
- A 200 from the port is not proof it's *this* server. Tested by squatting :8765 with a
  plain `http.server` before starting the runner: the poll saw 200 from the wrong process,
  declared success, and would have handed a browser tool a URL pointing at someone else's
  server while backglass's own dashboard sat dead behind a bind error in the log nobody
  read. The health check now also requires the dashboard's own marker text in the body.
- `terminate()` targets the actual dashboard process, not a wrapper around it. Tested with
  `subprocess.Popen(["uv", "run", "backglass", "dashboard", ...])`: `uv run` forks rather
  than exec-replacing on this machine, so a SIGINT sent to *this script* (itself already one
  `uv run` layer down from the invoking shell) left a two-deep chain — this script's `uv
  run backglass dashboard` child, and *that* process's own backglass child — and killing
  only the immediate child orphaned the grandchild still holding the port. Confirmed with
  `ps -ef`: four processes deep for what should be one. Launching the venv interpreter
  directly (`.venv/bin/python3 -m backglass dashboard`, backglass has a `__main__.py`)
  removes the extra `uv run` hop entirely, so the process this script starts is the process
  serving the port, and `terminate()` reaches it in one step.
- The log is captured and surfaced on failure only. Silent on the happy path, because a
  healthy run printing a request log per poll is noise nobody reads.

Known limit, not fixed here because fixing it needs a different mechanism entirely:
**SIGKILL bypasses all of the above.** A `-9` (or a hard crash of the whole interpreter)
skips every signal handler and every `finally`, so the dashboard child is left running,
still holding the port, still passing the health check on the *next* run — which means a
future invocation will report "ready" against a process nothing here is tracking or will
tear down. `_healthy()`'s marker check guards against driving the *wrong* server; it does
not and cannot detect an orphaned *right* one. The only real fix is an out-of-process
supervisor or a pidfile a new run checks and reaps on start, which is more machinery than
a scratch/scripts/ tool warrants; `lsof -iTCP:8765` is the manual check until it is.

Usage:
    uv run python scripts/browser_task_runner.py                 # foreground, Ctrl-C tears down
    uv run python scripts/browser_task_runner.py --port 8766      # non-default port
    uv run python scripts/browser_task_runner.py --once -- curl -s http://127.0.0.1:8765/

The last form runs one command against the server and tears down immediately after,
for a scripted check rather than a live-drive session.
"""

from __future__ import annotations

import argparse
import signal
import subprocess
import sys
import tempfile
import time
import urllib.error
import urllib.request
from collections.abc import Iterator
from contextlib import contextmanager
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent


class _Interrupted(Exception):
    """Raised by the SIGINT/SIGTERM handler so cleanup runs through normal unwinding."""


def _install_signal_handlers() -> None:
    def _raise(signum: int, _frame: object) -> None:
        raise _Interrupted(signal.Signals(signum).name)

    # Explicit registration, not reliance on Python's default SIGINT->KeyboardInterrupt
    # conversion: that conversion only happens if SIGINT wasn't already SIG_IGN at
    # startup, which it is for a backgrounded (`cmd &`) child under POSIX shells. Calling
    # signal.signal() here always overrides the inherited disposition. SIGTERM has no
    # such conversion at all by default — see the module docstring.
    signal.signal(signal.SIGINT, _raise)
    signal.signal(signal.SIGTERM, _raise)


DEFAULT_PORT = 8765
STARTUP_TIMEOUT_S = 30
POLL_INTERVAL_S = 1.0
# A path this app's own base.html emits (backglass/web/templates/base.html), not a
# generic 200 — see the port-squatting note above. First attempt used the literal text
# "Backglass": wrong, because a plain `python -m http.server` run from this repo's own
# root also directory-lists a folder named "backglass" and matched. This asset path is
# specific enough that nothing squatting the port by accident will happen to emit it.
HEALTH_MARKER = b"/static/dashboard.css"


class StartupFailed(RuntimeError):
    pass


def _healthy(url: str) -> bool:
    try:
        with urllib.request.urlopen(url, timeout=2) as resp:  # noqa: S310 (localhost only)
            if resp.status != 200:
                return False
            return HEALTH_MARKER in resp.read()
    except (urllib.error.URLError, ConnectionError, TimeoutError):
        return False


@contextmanager
def dashboard_up(
    port: int = DEFAULT_PORT, timeout: float = STARTUP_TIMEOUT_S
) -> Iterator[str]:
    """Start `backglass dashboard`, block until it answers, yield its URL, then kill it.

    Raises StartupFailed — with the server's own stderr/stdout attached — if the process
    exits before becoming healthy, or if it never becomes healthy within `timeout`. Either
    way, nothing is left running: the process group is checked and killed in `finally`
    even on that raise.
    """
    venv_python = REPO_ROOT / ".venv" / "bin" / "python3"
    if not venv_python.exists():
        raise StartupFailed(
            f"{venv_python} missing — run `uv sync` first (docs/10-tech-stack.md)"
        )

    url = f"http://127.0.0.1:{port}/"
    log_path = Path(tempfile.mkstemp(prefix="backglass-dashboard-", suffix=".log")[1])
    log_file = log_path.open("w")
    # The venv interpreter directly, not `uv run backglass dashboard`: see the
    # process-depth note in the module docstring for why an extra `uv run` hop here
    # orphans the real server on interrupt instead of killing it.
    proc = subprocess.Popen(
        [str(venv_python), "-m", "backglass", "dashboard", "--port", str(port)],
        cwd=REPO_ROOT,
        stdout=log_file,
        stderr=subprocess.STDOUT,
    )
    try:
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            if proc.poll() is not None:
                log_file.close()
                raise StartupFailed(
                    f"dashboard exited during startup (code {proc.returncode}):\n"
                    f"{log_path.read_text()}"
                )
            if _healthy(url):
                break
            time.sleep(POLL_INTERVAL_S)
        else:
            log_file.close()
            raise StartupFailed(
                f"dashboard did not answer {url} within {timeout}s:\n{log_path.read_text()}"
            )
        yield url
    finally:
        if proc.poll() is None:
            proc.terminate()
            try:
                proc.wait(timeout=5)
            except subprocess.TimeoutExpired:
                proc.kill()
                proc.wait(timeout=5)
        if not log_file.closed:
            log_file.close()
        log_path.unlink(missing_ok=True)


def main() -> int:
    _install_signal_handlers()
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--port", type=int, default=DEFAULT_PORT)
    parser.add_argument("--timeout", type=float, default=STARTUP_TIMEOUT_S)
    parser.add_argument(
        "--once",
        action="store_true",
        help="run COMMAND (after --) against the server, then tear down immediately",
    )
    parser.add_argument("command", nargs=argparse.REMAINDER)
    args = parser.parse_args()

    try:
        with dashboard_up(port=args.port, timeout=args.timeout) as url:
            print(f"{url} ready (pid managed, will be killed on exit)")
            if args.once:
                cmd = [c for c in args.command if c != "--"]
                if not cmd:
                    print("--once needs a command after --", file=sys.stderr)
                    return 2
                result = subprocess.run(cmd)
                return result.returncode
            # Foreground: block here until interrupted; browser tools drive `url`
            # from outside this process in the meantime. _Interrupted (not
            # KeyboardInterrupt) is what actually fires — see _install_signal_handlers.
            try:
                while True:
                    time.sleep(3600)
            except _Interrupted as e:
                print(f"\nreceived {e}, tearing down", file=sys.stderr)
            return 0
    except StartupFailed as e:
        print(f"startup failed: {e}", file=sys.stderr)
        return 1
    except _Interrupted as e:
        # Caught here too: the signal can land during dashboard_up's own startup poll,
        # before the `with` body (and its inner try/except) is even entered.
        print(f"\nreceived {e} during startup, tore down", file=sys.stderr)
        return 130


if __name__ == "__main__":
    raise SystemExit(main())
