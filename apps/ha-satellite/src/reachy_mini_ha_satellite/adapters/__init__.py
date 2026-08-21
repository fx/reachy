"""The far side of the ports: everything that actually touches the robot.

Nothing in here is imported by the behaviour layer. `main.py` is the one module
that knows which adapter is in use, which is what lets the same behaviour run
against a robot, against a fake, and — for perception — against two different
detectors without noticing.

The Reachy Mini SDK is **not** a dependency of this package and must not become
one. Importing any part of it executes `reachy_mini/__init__.py`, which
transitively imports `reachy_mini.vision.face_tracking`, which does `import gi`
— so an ordinary import drags in PyGObject and the whole GStreamer stack, which
a continuous integration runner has not got and architecture REQ-005 says the
test suite must not need. Two mechanisms keep that true:

* Everything the daemon offers is reached through the narrow protocols in
  `daemon.py`, which the application is *handed* an implementation of. On the
  robot that implementation is the SDK's own `ReachyMini`; in the test suite it
  is a fake. Neither the adapters nor their tests import the SDK to use it.
* The one place that genuinely needs SDK code — the local face detector — loads
  a single module **by file path**, inside the function that needs it, so the
  package around it never executes. See `perception_local.py`, which explains
  the bypass in full.
"""
