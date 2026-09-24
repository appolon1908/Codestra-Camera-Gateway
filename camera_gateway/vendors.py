"""Vendor conventions layered on top of the vendor-neutral ONVIF/RTSP abstractions.

A vendor profile only supplies *defaults* (stream paths, ports). Everything remains
overridable per camera, and the protocol adapters never branch on vendor.
"""

from __future__ import annotations

from dataclasses import dataclass

from camera_gateway.models import StreamVariant, Vendor


@dataclass(frozen=True)
class VendorProfile:
    vendor: Vendor
    description: str
    rtsp_paths: dict[StreamVariant, str]
    default_username: str | None = None
    notes: str = ""


# EZVIZ (Hikvision-family firmware, e.g. H8c): RTSP must be enabled on the device
# (EZVIZ app -> device settings -> LAN/local service / RTSP). Username is "admin" and the
# password is the device verification code printed on the label. ONVIF is available on
# firmware that exposes it; when enabled, ONVIF GetStreamUri takes precedence.
EZVIZ = VendorProfile(
    vendor=Vendor.ezviz,
    description="EZVIZ cameras (e.g. H8c) using Hikvision-style RTSP paths",
    rtsp_paths={
        StreamVariant.main: "/h264/ch1/main/av_stream",
        StreamVariant.sub: "/h264/ch1/sub/av_stream",
    },
    default_username="admin",
    notes="Enable RTSP in the EZVIZ app; password is the device verification code.",
)

GENERIC = VendorProfile(
    vendor=Vendor.generic,
    description="Any ONVIF Profile S / RTSP camera; stream path configured or discovered",
    rtsp_paths={},
)

PROFILES: dict[Vendor, VendorProfile] = {Vendor.ezviz: EZVIZ, Vendor.generic: GENERIC}


def default_rtsp_path(vendor: Vendor, stream: StreamVariant) -> str | None:
    return PROFILES[vendor].rtsp_paths.get(stream)
