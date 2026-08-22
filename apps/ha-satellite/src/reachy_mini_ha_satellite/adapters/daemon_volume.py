"""Driving the daemon's own coarse volume, which nothing here used to touch.

Change 0016's R7. The robot has two volume controls below this application and
it was reaching neither. The hardware mixer is one USB device with a single
`PCM` control that was measured already at `0.00dB`, its maximum — so there is
nothing to ask it for. The daemon has a second, coarse one, and it was found at
62 of 100 with nothing in this application aware it existed. A control sitting at
62 that the operator cannot reach is its own defect: it silently costs a third of
the level that the software boost is then asked to make up.

**It is not on the media interface.** `adapters/daemon.py` mirrors the SDK's
`MediaManager`, and the volume lives on the daemon's HTTP API instead — so this
is a small client for one endpoint rather than another protocol method. That is
also why it is best-effort: the application runs inside the daemon's own
environment, so the endpoint is normally right there, but a daemon that has moved
its port is a robot that should still answer to its wake word.

**Setting it plays a sound.** The daemon's own handler plays a test cue after
writing the value, which is helpful when a person is dragging a slider and is a
chirp at every start-up here. It is the daemon's behaviour rather than ours and
cannot be opted out of at the endpoint; an operator who does not want it sets
`daemon_api_url` to empty, which turns this off entirely.
"""

from __future__ import annotations

import http.client
import json
import logging
import urllib.request
from typing import Final
from urllib.parse import urlsplit

__all__ = ["MAX_DAEMON_VOLUME", "set_daemon_volume"]

# The loudest the daemon's own control goes. Its API takes 0-100.
MAX_DAEMON_VOLUME: Final = 100

# Where the setter lives, appended to the configured base.
_VOLUME_PATH: Final = "/api/volume/set"

# How long to wait. The daemon is on this machine, so a request that has not
# been answered in this long is not slow but wrong, and start-up should not be
# held up by it.
_TIMEOUT_SECONDS: Final = 5.0

# What may be addressed. The same allowlist `sounds.py` applies, for the same
# reason: this opens a URL that came out of configuration.
_FETCHABLE: Final = frozenset({"http", "https"})

_LOGGER: Final = logging.getLogger(__name__)


def set_daemon_volume(base_url: str, level: int = MAX_DAEMON_VOLUME) -> bool:
    """Ask the daemon to set its output volume.

    Args:
        base_url: Where the daemon serves its API, without a path. Empty turns
            this off and is reported as such.
        level: The level to set, from 0 to 100.

    Returns:
        Whether the daemon accepted it. `False` is logged rather than raised:
        a satellite that could not raise the volume is worth a line in the log
        and is not worth refusing to start over.
    """
    if not base_url:
        _LOGGER.info(
            "daemon volume: not set, because no daemon API address is configured",
        )
        return False
    if urlsplit(base_url).scheme not in _FETCHABLE:
        _LOGGER.warning(
            "daemon volume: refusing to address a %r URL",
            urlsplit(base_url).scheme,
        )
        return False

    payload = json.dumps({"volume": level}).encode("utf-8")
    request = urllib.request.Request(  # noqa: S310  # the scheme is checked against an http/https allowlist immediately above; the address is this application's own configuration, defaulting to the loopback interface the daemon serves on
        f"{base_url.rstrip('/')}{_VOLUME_PATH}",
        data=payload,
        headers={"Content-Type": "application/json"},
        method="POST",
    )
    try:
        with urllib.request.urlopen(request, timeout=_TIMEOUT_SECONDS) as response:  # noqa: S310  # as above
            ok = 200 <= int(response.status) < 300
    except (OSError, http.client.HTTPException) as error:
        # Every way this fails is the same to the caller: the coarse control is
        # wherever it was, the software boost still applies, and the robot works
        # — more quietly than it could.
        #
        # **Both arms are needed and neither is `urllib.error.HTTPError`.** That
        # one is already an `OSError`, so naming it would add nothing; what it
        # would hide is the gap. `urlopen` can also raise `BadStatusLine`,
        # `IncompleteRead` or `LineTooLong` from `http.client`, and those derive
        # from `Exception` rather than from `OSError` — so a daemon that answered
        # badly would escape this function, escape `VolumeService.start`, and
        # take the whole application down through the service-startup loop.
        # "Best-effort" would then be true of the sentence above and of nothing
        # else.
        _LOGGER.warning("daemon volume: could not set it to %d: %s", level, error)
        return False
    if ok:
        _LOGGER.info("daemon volume: set to %d", level)
    else:
        _LOGGER.warning("daemon volume: the daemon refused %d", level)
    return ok
