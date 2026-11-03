"""ONVIF WS-Discovery probe (UDP multicast). Operator/dev use only; disabled by default."""

from __future__ import annotations

import asyncio
import socket
import uuid

from defusedxml import ElementTree as SafeET
from defusedxml.common import DefusedXmlException

from camera_gateway.adapters.onvif import _local, text
from camera_gateway.models import DiscoveredDevice

MULTICAST_ADDR = ("239.255.255.250", 3702)
MAX_DATAGRAM = 64 * 1024
MAX_DEVICES = 256

PROBE_TEMPLATE = (
    '<?xml version="1.0" encoding="UTF-8"?>'
    '<e:Envelope xmlns:e="http://www.w3.org/2003/05/soap-envelope" '
    'xmlns:w="http://schemas.xmlsoap.org/ws/2004/08/addressing" '
    'xmlns:d="http://schemas.xmlsoap.org/ws/2005/04/discovery" '
    'xmlns:dn="http://www.onvif.org/ver10/network/wsdl">'
    "<e:Header><w:MessageID>uuid:{message_id}</w:MessageID>"
    "<w:To>urn:schemas-xmlsoap-org:ws:2005:04:discovery</w:To>"
    "<w:Action>http://schemas.xmlsoap.org/ws/2005/04/discovery/Probe</w:Action></e:Header>"
    "<e:Body><d:Probe><d:Types>dn:NetworkVideoTransmitter</d:Types></d:Probe></e:Body>"
    "</e:Envelope>"
)


def parse_probe_matches(data: bytes) -> list[DiscoveredDevice]:
    try:
        root = SafeET.fromstring(data, forbid_dtd=True)
    except (SafeET.ParseError, DefusedXmlException, ValueError):
        return []
    devices = []
    for match in (el for el in root.iter() if _local(el.tag) == "ProbeMatch"):
        xaddrs = (text(match, "XAddrs") or "").split()
        if not xaddrs:
            continue
        devices.append(
            DiscoveredDevice(
                endpoint_reference=text(match, "EndpointReference", "Address"),
                xaddrs=xaddrs[:8],
                scopes=(text(match, "Scopes") or "").split()[:32],
                types=(text(match, "Types") or "").split()[:8],
            )
        )
    return devices


class _Collector(asyncio.DatagramProtocol):
    def __init__(self) -> None:
        self.devices: dict[str, DiscoveredDevice] = {}

    def datagram_received(self, data: bytes, addr: tuple[str, int]) -> None:
        if len(self.devices) >= MAX_DEVICES:
            return
        for dev in parse_probe_matches(data[:MAX_DATAGRAM]):
            self.devices.setdefault(dev.endpoint_reference or dev.xaddrs[0], dev)


async def ws_discover(timeout_s: float, target: tuple[str, int] = MULTICAST_ADDR) -> list[DiscoveredDevice]:
    loop = asyncio.get_running_loop()
    sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM, socket.IPPROTO_UDP)
    sock.setsockopt(socket.IPPROTO_IP, socket.IP_MULTICAST_TTL, 1)
    sock.bind(("0.0.0.0", 0))  # noqa: S104 - ephemeral port for unicast probe replies
    transport, protocol = await loop.create_datagram_endpoint(_Collector, sock=sock)
    try:
        transport.sendto(PROBE_TEMPLATE.format(message_id=uuid.uuid4()).encode(), target)
        await asyncio.sleep(timeout_s)
    finally:
        transport.close()
    return list(protocol.devices.values())
