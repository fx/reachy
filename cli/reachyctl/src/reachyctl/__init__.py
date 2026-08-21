"""The command-line tool for operating a Reachy Mini running this stack.

`probe` is implemented; `doctor` arrives in change 0008, and `deploy`, `config`
and `app` in 0009. `bench` is a registered name with no body yet.

The tool is a thin layer over `reachy_session_client`, which holds the one
implementation of the robot link's client half. It is not a second protocol
path: reachyctl REQ-057 requires the probe to establish its session with the
same implementation the robot application uses, so that a probe run says
something about the protocol rather than about the probe.

See `docs/specs/reachyctl/` for what this package is required to do.
"""

from __future__ import annotations

from reachyctl.exits import ExitCode
from reachyctl.output import OutputFormat, Report, Reporter

__all__ = ["ExitCode", "OutputFormat", "Report", "Reporter"]
