"""The application control the roles reach as `python -m reachy_mini.apps`.

One verb matters here — `status --json <application>` — because that is what the
shared check registry's `application.running` check is asked through. `start` and
`stop` exist so that the module answers the way the real one is expected to
rather than erroring on a verb an operator might try.

What "running" means in the container is what the daemon recorded at its last
start; see `reachy_mini.daemon`. That is why installing a wheel is not enough to
make this report the application running, and why the `app_install` role
notifying a daemon restart is observable rather than merely tidy.
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

from reachy_mini import STATE_PATH


def _running() -> dict[str, str]:
    """Read what the daemon recorded at its last start.

    Returns:
        The version by distribution name, empty when the daemon has not started
        since this boot or wrote something unreadable.
    """
    state = Path(STATE_PATH)
    if not state.exists():
        return {}
    try:
        decoded = json.loads(state.read_text(encoding="utf-8"))
    except ValueError:
        return {}
    applications = decoded.get("applications") if isinstance(decoded, dict) else None
    if not isinstance(applications, dict):
        return {}
    return {str(name): str(version) for name, version in applications.items()}


def _status(application: str) -> dict[str, object]:
    """Say whether the daemon is running an application.

    Args:
        application: The distribution to ask about.

    Returns:
        The report, in the shape the check registry's adapter reads.
    """
    running = _running()
    if application in running:
        return {
            "running": True,
            "detail": f"running under the daemon at {running[application]}",
        }
    return {
        "running": False,
        "detail": (
            "the daemon did not pick this application up at its last start; it "
            "has to be restarted after the application is installed"
        ),
    }


def main(argv: list[str] | None = None) -> int:
    """Answer one application-control verb.

    Args:
        argv: The arguments, for a caller that is not the command line.

    Returns:
        The exit status. Non-zero only when the verb could not be answered,
        because a caller cannot tell "this application is stopped" from "this
        command did not run" any other way.
    """
    parser = argparse.ArgumentParser(prog="reachy_mini.apps")
    verbs = parser.add_subparsers(dest="verb", required=True)
    for verb in ("status", "start", "stop"):
        command = verbs.add_parser(verb)
        command.add_argument("--json", action="store_true", dest="as_json")
        command.add_argument("application")
    arguments = parser.parse_args(argv)
    if not arguments.application:
        parser.error("an application must be named")
    if arguments.verb == "status":
        report: dict[str, object] = _status(arguments.application)
    else:
        # The container has nothing to start or stop: what is running is what the
        # daemon picked up, so the honest answer is the current state rather than
        # a claim to have changed it.
        report = _status(arguments.application)
    sys.stdout.write(json.dumps(report))
    return 0


if __name__ == "__main__":
    sys.exit(main())
