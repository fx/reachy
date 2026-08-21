"""The address and hardware identity the satellite announces over the network.

Home Assistant keys an ESPHome device on two things: the name it announces and
the hardware address it reports. The name is configuration with no default —
ha-satellite REQ-040, and `config.py` explains at length why. The address is
configuration too, but with a default that is read from the machine, and that is
a deliberate difference rather than an inconsistency: a hardware address does not
change when the software is repackaged, which is the exact thing REQ-040 forbids
a default from depending on. It changes when the network hardware changes, and
an operator who moves a robot to a new board wants to be able to pin the old
value — so the setting exists, the discovered value is reported in the resolved
configuration, and pinning it is one line.

Everything here is injected. The two functions that read the machine are
parameters with defaults, so the whole of this module is exercised against a
mapping rather than against whatever interfaces the runner happens to have.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import TYPE_CHECKING, Any, Final

import netifaces

from reachy_mini_ha_satellite.esphome.util import (
    get_default_interface,
    get_default_ipv4,
)

if TYPE_CHECKING:
    from collections.abc import Callable

__all__ = [
    "NetworkError",
    "NetworkIdentity",
    "discover_network_identity",
]

# Which address family carries a hardware address. `AF_LINK` is netifaces2's
# portable spelling and is what a BSD reports under; on Linux the key in the
# mapping is `AF_PACKET`. Both are tried, in that order, because the robot is
# Linux and a development machine may not be.
_HARDWARE_FAMILIES: Final[tuple[int, ...]] = (
    int(netifaces.AF_LINK),
    int(netifaces.AF_PACKET),
)


class NetworkError(RuntimeError):
    """The machine cannot say what this robot should announce itself as."""


@dataclass(frozen=True, slots=True)
class NetworkIdentity:
    """What the satellite tells the network about itself.

    Attributes:
        interface: The interface the other two were read from.
        ip_address: The IPv4 address the mDNS advertisement points at.
        mac_address: The hardware address Home Assistant keys the device on,
            lower-cased and colon-separated.
    """

    interface: str
    ip_address: str
    mac_address: str


def _hardware_address(
    interface: str,
    addresses: Callable[[str], Any],
) -> str | None:
    """Read one interface's hardware address.

    Args:
        interface: Which interface.
        addresses: How to read an interface's addresses.

    Returns:
        The address, lower-cased, or `None` when the interface has none.
    """
    try:
        found = addresses(interface)
    except (KeyError, OSError, ValueError):
        return None
    for family in _HARDWARE_FAMILIES:
        entries = found.get(family)
        if not entries:
            continue
        address = entries[0].get("addr")
        if address:
            return str(address).lower()
    return None


def discover_network_identity(
    *,
    interface: str = "",
    mac_address: str = "",
    default_interface: Callable[[], str | None] = get_default_interface,
    ipv4: Callable[[str], str | None] = get_default_ipv4,
    addresses: Callable[[str], Any] = netifaces.ifaddresses,
) -> NetworkIdentity:
    """Work out what to announce, taking any part of it that was configured.

    Args:
        interface: The configured interface, or blank to use the one the
            default route goes out of.
        mac_address: The configured hardware address, or blank to read it from
            the interface.
        default_interface: How to find the default route's interface.
        ipv4: How to read an interface's IPv4 address.
        addresses: How to read an interface's addresses.

    Returns:
        The interface, address and hardware address to announce.

    Raises:
        NetworkError: If any of the three cannot be determined. Refusing here
            is deliberate: a satellite that advertised itself at no address, or
            under an empty hardware address, would be discovered by Home
            Assistant and then be a device that cannot be reached or that
            collides with every other such device.
    """
    chosen = interface.strip() or (default_interface() or "")
    if not chosen:
        message = (
            "no default network interface was found, so there is nothing to "
            "announce on. Set REACHY_SATELLITE_NETWORK_INTERFACE to the "
            "interface Home Assistant reaches this robot on."
        )
        raise NetworkError(message)

    address = ipv4(chosen)
    if not address:
        message = (
            f"the interface {chosen!r} has no IPv4 address, so Home Assistant "
            f"would be told to connect to nothing. Bring the interface up, or "
            f"set REACHY_SATELLITE_NETWORK_INTERFACE to one that is."
        )
        raise NetworkError(message)

    hardware = mac_address.strip().lower() or _hardware_address(chosen, addresses)
    if not hardware:
        message = (
            f"the interface {chosen!r} reports no hardware address. Home "
            f"Assistant keys the device on it, so set "
            f"REACHY_SATELLITE_MAC_ADDRESS to the address the previous "
            f"installation announced."
        )
        raise NetworkError(message)

    return NetworkIdentity(
        interface=chosen,
        ip_address=str(address),
        mac_address=hardware,
    )
