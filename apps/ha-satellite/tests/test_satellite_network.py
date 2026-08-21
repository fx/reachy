"""What the satellite announces about itself, and what it refuses to guess.

Home Assistant keys an ESPHome device on two things: the name it announces and
the hardware address it reports. The name is configuration with no default at
all — see `test_satellite_config.py`. The address has a default read from the
machine, and the difference is not an inconsistency: a hardware address does not
change when the software is repackaged, which is the thing ha-satellite REQ-040
forbids a default from depending on.

Every machine read is injected here, so none of these tests touches an
interface. Addresses come from the RFC 5737 documentation range, because this
repository is public.

Test module names are globally unique across the workspace — see the root
`AGENTS.md`.
"""

from __future__ import annotations

from typing import Final

import pytest

from reachy_mini_ha_satellite.adapters.network import (
    NetworkError,
    discover_network_identity,
)

# What a machine with one usable interface reports. The key is the address
# family; on Linux a hardware address arrives under `AF_PACKET`, which is why
# the module tries both spellings.
_ADDRESSES: Final[dict[str, dict[int, list[dict[str, str]]]]] = {
    "eth0": {
        2: [{"addr": "192.0.2.10"}],
        17: [{"addr": "02:00:5E:10:00:00"}],
    },
    "wlan0": {2: [{"addr": "198.51.100.5"}]},
}


def _addresses(interface: str) -> dict[int, list[dict[str, str]]]:
    """Stand in for reading an interface's addresses.

    Args:
        interface: Which interface.

    Returns:
        What the machine would report.

    Raises:
        KeyError: For an interface that is not there, which is what the real
            call does.
    """
    return _ADDRESSES[interface]


def _ipv4(interface: str) -> str | None:
    """Stand in for reading an interface's IPv4 address.

    Args:
        interface: Which interface.

    Returns:
        The address, or `None`.
    """
    entries = _ADDRESSES.get(interface, {}).get(2)
    return entries[0]["addr"] if entries else None


class TestDiscoveringWhatToAnnounce:
    """The ordinary path, with nothing configured."""

    def test_it_takes_the_default_route_s_interface(self) -> None:
        """Which is the one Home Assistant is reaching the robot on."""
        identity = discover_network_identity(
            default_interface=lambda: "eth0",
            ipv4=_ipv4,
            addresses=_addresses,
        )

        assert identity.interface == "eth0"
        assert identity.ip_address == "192.0.2.10"

    def test_the_hardware_address_is_lower_cased(self) -> None:
        """Home Assistant matches on it, so one spelling is worth having."""
        identity = discover_network_identity(
            default_interface=lambda: "eth0",
            ipv4=_ipv4,
            addresses=_addresses,
        )

        assert identity.mac_address == "02:00:5e:10:00:00"

    def test_a_configured_interface_wins(self) -> None:
        """A robot with two interfaces is a thing an operator decides about."""
        identity = discover_network_identity(
            interface="wlan0",
            mac_address="02:00:5E:99:00:00",
            default_interface=lambda: "eth0",
            ipv4=_ipv4,
            addresses=_addresses,
        )

        assert identity.interface == "wlan0"
        assert identity.ip_address == "198.51.100.5"

    def test_a_configured_hardware_address_wins(self) -> None:
        """Which is how an existing installation's identity is pinned."""
        identity = discover_network_identity(
            mac_address="02:00:5E:99:00:00",
            default_interface=lambda: "eth0",
            ipv4=_ipv4,
            addresses=_addresses,
        )

        assert identity.mac_address == "02:00:5e:99:00:00"


class TestRefusingToGuess:
    """Each of these would produce a device Home Assistant cannot use."""

    def test_no_default_interface_is_refused(self) -> None:
        """There is nothing to announce on."""
        with pytest.raises(NetworkError, match="NETWORK_INTERFACE"):
            discover_network_identity(
                default_interface=lambda: None,
                ipv4=_ipv4,
                addresses=_addresses,
            )

    def test_an_interface_with_no_address_is_refused(self) -> None:
        """Home Assistant would be told to connect to nothing."""
        with pytest.raises(NetworkError, match="no IPv4 address"):
            discover_network_identity(
                interface="eth0",
                default_interface=lambda: "eth0",
                ipv4=lambda _: None,
                addresses=_addresses,
            )

    def test_an_interface_with_no_hardware_address_is_refused(self) -> None:
        """An empty one would collide with every other empty one."""
        with pytest.raises(NetworkError, match="MAC_ADDRESS"):
            discover_network_identity(
                interface="wlan0",
                default_interface=lambda: "wlan0",
                ipv4=_ipv4,
                addresses=_addresses,
            )

    def test_an_interface_that_is_not_there_is_refused(self) -> None:
        """Reading it raises, and the refusal says what to set instead."""
        with pytest.raises(NetworkError, match="MAC_ADDRESS"):
            discover_network_identity(
                interface="missing0",
                default_interface=lambda: "missing0",
                ipv4=lambda _: "203.0.113.7",
                addresses=_addresses,
            )
