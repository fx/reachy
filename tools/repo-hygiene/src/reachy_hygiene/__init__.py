"""Repository hygiene gates.

The generic environment-leak scanner that keeps values belonging to somebody's
environment — private addresses, internal hostnames, email addresses — out of a
public repository whose history is not practically rewritable once pushed.

Run it over a range with `just leak-scan BASE HEAD`, or directly with
`python -m reachy_hygiene --base BASE --head HEAD`.
"""

from __future__ import annotations

__all__: list[str] = []
