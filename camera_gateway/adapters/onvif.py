"""Minimal, vendor-neutral ONVIF (Profile S) SOAP client.

Implements only what the gateway needs: device information, media service discovery,
media profiles and stream URIs. XML is parsed with defusedxml. The client never follows
redirects and never connects to a host other than the registered camera host: service
addresses and stream URIs advertised by the device are reduced to their path component.
"""

from __future__ import annotations

import base64
import hashlib
import logging
import os
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from typing import Any
from urllib.parse import urlsplit
from xml.etree.ElementTree import Element  # noqa: S405 - type only; parsing uses defusedxml
from xml.sax.saxutils import escape

import httpx
from defusedxml import ElementTree as SafeET
from defusedxml.common import DefusedXmlException

from camera_gateway.adapters.net import format_host
from camera_gateway.errors import (
    CameraAuthFailed,
    CameraProtocolError,
    CameraTimeout,
    CameraUnreachable,
)
from camera_gateway.models import OnvifDeviceInfo, OnvifProfile

log = logging.getLogger(__name__)

NS_SOAP = "http://www.w3.org/2003/05/soap-envelope"
NS_DEVICE = "http://www.onvif.org/ver10/device/wsdl"
NS_MEDIA = "http://www.onvif.org/ver10/media/wsdl"
NS_SCHEMA = "http://www.onvif.org/ver10/schema"
NS_WSSE = "http://docs.oasis-open.org/wss/2004/01/oasis-200401-wss-wssecurity-secext-1.0.xsd"
NS_WSU = "http://docs.oasis-open.org/wss/2004/01/oasis-200401-wss-wssecurity-utility-1.0.xsd"
# WS-Security token type URI, not a credential.
PASSWORD_DIGEST = (
    "http://docs.oasis-open.org/wss/2004/01/oasis-200401-wss-username-token-profile-1.0#PasswordDigest"  # noqa: E501, S105
)
MAX_RESPONSE_BYTES = 1024 * 1024
_AUTH_FAULTS = ("notauthorized", "not authorized", "authorityfailure", "failedauthentication")


@dataclass(frozen=True)
class OnvifCredentials:
    username: str
    password: str


def _local(tag: str) -> str:
    return tag.rsplit("}", 1)[-1]


def find(el: Element | None, *path: str) -> Element | None:
    """Namespace-agnostic descendant lookup by local-name path."""
    for name in path:
        if el is None:
            return None
        el = next((c for c in el.iter() if c is not el and _local(c.tag) == name), None)
    return el


def text(el: Element | None, *path: str) -> str | None:
    node = find(el, *path) if path else el
    if node is None or node.text is None:
        return None
    value = node.text.strip()
    return value or None


def _int(value: str | None) -> int | None:
    try:
        return int(value) if value is not None else None
    except ValueError:
        return None


def _float(value: str | None) -> float | None:
    try:
        return float(value) if value is not None else None
    except ValueError:
        return None


def ws_security_header(creds: OnvifCredentials, now: datetime, nonce: bytes | None = None) -> str:
    nonce = nonce if nonce is not None else os.urandom(16)
    created = now.astimezone(UTC).strftime("%Y-%m-%dT%H:%M:%S.000Z")
    digest = base64.b64encode(
        hashlib.sha1(nonce + created.encode() + creds.password.encode(), usedforsecurity=False).digest()
    ).decode()
    return (
        f'<wsse:Security xmlns:wsse="{NS_WSSE}" xmlns:wsu="{NS_WSU}" s:mustUnderstand="1">'
        "<wsse:UsernameToken>"
        f"<wsse:Username>{escape(creds.username)}</wsse:Username>"
        f'<wsse:Password Type="{PASSWORD_DIGEST}">{digest}</wsse:Password>'
        f"<wsse:Nonce>{base64.b64encode(nonce).decode()}</wsse:Nonce>"
        f"<wsu:Created>{created}</wsu:Created>"
        "</wsse:UsernameToken></wsse:Security>"
    )


def envelope(body: str, security: str = "") -> str:
    header = f"<s:Header>{security}</s:Header>" if security else ""
    return (
        '<?xml version="1.0" encoding="UTF-8"?>'
        f'<s:Envelope xmlns:s="{NS_SOAP}" xmlns:tds="{NS_DEVICE}" xmlns:trt="{NS_MEDIA}" '
        f'xmlns:tt="{NS_SCHEMA}">{header}<s:Body>{body}</s:Body></s:Envelope>'
    )


class OnvifClient:
    def __init__(
        self,
        host: str,
        port: int,
        device_service_path: str,
        credentials: OnvifCredentials | None,
        *,
        timeout_s: float,
        transport: httpx.AsyncBaseTransport | None = None,
    ) -> None:
        self._host = host
        self._base = f"http://{format_host(host)}:{port}"
        self._device_path = device_service_path
        self._creds = credentials
        self._clock_offset = timedelta(0)
        self._client = httpx.AsyncClient(
            timeout=httpx.Timeout(timeout_s),
            follow_redirects=False,
            transport=transport,
            auth=(httpx.DigestAuth(credentials.username, credentials.password) if credentials else None),
            headers={"User-Agent": "codestra-camera-gateway"},
        )

    async def __aenter__(self) -> OnvifClient:
        return self

    async def __aexit__(self, *exc: Any) -> None:
        await self._client.aclose()

    # -- transport -------------------------------------------------------------------

    async def _call(self, path: str, action: str, body: str, *, authenticate: bool = True) -> Element:
        security = ""
        if authenticate and self._creds is not None:
            security = ws_security_header(self._creds, datetime.now(UTC) + self._clock_offset)
        payload = envelope(body, security)
        headers = {"Content-Type": f'application/soap+xml; charset=utf-8; action="{action}"'}
        try:
            async with self._client.stream(
                "POST", self._base + path, content=payload.encode(), headers=headers
            ) as resp:
                raw = bytearray()
                async for chunk in resp.aiter_bytes():
                    raw.extend(chunk)
                    if len(raw) > MAX_RESPONSE_BYTES:
                        raise CameraProtocolError("ONVIF response exceeded size limit")
                status = resp.status_code
        except httpx.TimeoutException:
            raise CameraTimeout() from None
        except (httpx.ConnectError, httpx.NetworkError):
            raise CameraUnreachable() from None
        except httpx.HTTPError:
            raise CameraProtocolError("ONVIF transport error") from None

        if status in (401, 403):
            raise CameraAuthFailed()
        if 300 <= status < 400:
            raise CameraProtocolError("ONVIF endpoint attempted a redirect")
        root = self._parse(bytes(raw))
        fault = find(root, "Fault")
        if fault is not None:
            fault_text = " ".join(t.strip() for t in fault.itertext() if t.strip()).lower()
            if any(marker in fault_text for marker in _AUTH_FAULTS):
                raise CameraAuthFailed()
            raise CameraProtocolError("ONVIF device returned a SOAP fault")
        if status != 200:
            raise CameraProtocolError(f"ONVIF endpoint returned HTTP {status}")
        body_el = find(root, "Body")
        if body_el is None or len(body_el) == 0:
            raise CameraProtocolError("ONVIF response has no SOAP body")
        return body_el

    @staticmethod
    def _parse(raw: bytes) -> Element:
        if not raw.strip():
            raise CameraProtocolError("ONVIF response was empty")
        try:
            root = SafeET.fromstring(raw, forbid_dtd=True)
        except (SafeET.ParseError, DefusedXmlException, ValueError):
            raise CameraProtocolError("ONVIF response is not valid XML") from None
        if _local(root.tag) != "Envelope":
            raise CameraProtocolError("ONVIF response is not a SOAP envelope")
        return root

    def _local_path(self, url: str | None, fallback: str) -> str:
        """Keep only the path of a device-advertised URL (SSRF guard)."""
        if not url:
            return fallback
        parts = urlsplit(url.split()[0])
        if parts.scheme not in ("http", "https") or not parts.path.startswith("/"):
            return fallback
        return parts.path

    # -- operations ------------------------------------------------------------------

    async def sync_clock(self) -> None:
        """Estimate device clock offset so UsernameToken timestamps are accepted."""
        try:
            body = await self._call(
                self._device_path,
                f"{NS_DEVICE}/GetSystemDateAndTime",
                "<tds:GetSystemDateAndTime/>",
                authenticate=False,
            )
        except (CameraProtocolError, CameraAuthFailed):
            return
        utc = find(body, "UTCDateTime")
        if utc is None:
            return
        try:
            device_now = datetime(
                int(text(utc, "Year") or 0),
                int(text(utc, "Month") or 0),
                int(text(utc, "Day") or 0),
                int(text(utc, "Hour") or 0),
                int(text(utc, "Minute") or 0),
                int(text(utc, "Second") or 0),
                tzinfo=UTC,
            )
        except (TypeError, ValueError):
            return
        self._clock_offset = device_now - datetime.now(UTC)

    @property
    def clock_offset_seconds(self) -> float | None:
        return None if self._clock_offset is None else self._clock_offset.total_seconds()

    async def device_information(self) -> OnvifDeviceInfo:
        body = await self._call(
            self._device_path, f"{NS_DEVICE}/GetDeviceInformation", "<tds:GetDeviceInformation/>"
        )
        resp = find(body, "GetDeviceInformationResponse")
        if resp is None:
            raise CameraProtocolError("GetDeviceInformation response missing")
        return OnvifDeviceInfo(
            manufacturer=text(resp, "Manufacturer"),
            model=text(resp, "Model"),
            firmware_version=text(resp, "FirmwareVersion"),
            serial_number=text(resp, "SerialNumber"),
            hardware_id=text(resp, "HardwareId"),
        )

    async def media_service_path(self) -> str:
        body = await self._call(
            self._device_path,
            f"{NS_DEVICE}/GetCapabilities",
            "<tds:GetCapabilities><tds:Category>Media</tds:Category></tds:GetCapabilities>",
        )
        media = find(body, "Capabilities", "Media")
        xaddr = text(media, "XAddr") if media is not None else None
        if xaddr is None:
            raise CameraProtocolError("Device does not advertise an ONVIF media service")
        return self._local_path(xaddr, self._device_path)

    async def profiles(self, media_path: str) -> list[OnvifProfile]:
        body = await self._call(media_path, f"{NS_MEDIA}/GetProfiles", "<trt:GetProfiles/>")
        resp = find(body, "GetProfilesResponse")
        if resp is None:
            raise CameraProtocolError("GetProfiles response missing")
        result: list[OnvifProfile] = []
        for prof in (c for c in resp if _local(c.tag) == "Profiles"):
            token = prof.get("token")
            if not token:
                continue
            venc = find(prof, "VideoEncoderConfiguration")
            encoding = text(venc, "Encoding") if venc is not None else None
            enc_profile = None
            if venc is not None and encoding:
                enc_profile = text(venc, encoding.upper(), f"{encoding.upper()}Profile") or text(
                    venc, "H264", "H264Profile"
                )
            result.append(
                OnvifProfile(
                    token=token[:128],
                    ptz_configuration_token=(
                        find(prof, "PTZConfiguration").get("token")
                        if find(prof, "PTZConfiguration") is not None
                        else None
                    ),
                    name=text(prof, "Name"),
                    video_encoding=encoding,
                    width=_int(text(venc, "Resolution", "Width")) if venc is not None else None,
                    height=_int(text(venc, "Resolution", "Height")) if venc is not None else None,
                    fps=_float(text(venc, "RateControl", "FrameRateLimit")) if venc is not None else None,
                    bitrate_kbps=_int(text(venc, "RateControl", "BitrateLimit"))
                    if venc is not None
                    else None,
                    encoding_profile=enc_profile,
                )
            )
        if not result:
            raise CameraProtocolError("Device returned no usable media profiles")
        return result

    async def stream_path(self, media_path: str, profile_token: str) -> tuple[str | None, bool]:
        body = await self._call(
            media_path,
            f"{NS_MEDIA}/GetStreamUri",
            "<trt:GetStreamUri><trt:StreamSetup><tt:Stream>RTP-Unicast</tt:Stream>"
            "<tt:Transport><tt:Protocol>RTSP</tt:Protocol></tt:Transport></trt:StreamSetup>"
            f"<trt:ProfileToken>{escape(profile_token)}</trt:ProfileToken></trt:GetStreamUri>",
        )
        uri = text(body, "MediaUri", "Uri")
        if uri is None:
            return None, False
        parts = urlsplit(uri)
        if parts.scheme.lower() != "rtsp" or not parts.path.startswith("/"):
            raise CameraProtocolError("Device returned a non-RTSP stream URI")
        path = parts.path + (f"?{parts.query}" if parts.query else "")
        return path, (parts.hostname or "").lower() != self._host.lower()

    async def ptz_discover(self):
        """Only advertise absolute moves in the standard normalized coordinate spaces."""
        from camera_gateway.mission_models import PtzCapability

        ns = "http://www.onvif.org/ver20/ptz/wsdl"
        body = await self._call(
            self._device_path,
            f"{NS_DEVICE}/GetCapabilities",
            "<tds:GetCapabilities><tds:Category>PTZ</tds:Category></tds:GetCapabilities>",
        )
        xaddr = text(body, "Capabilities", "PTZ", "XAddr")
        if not xaddr:
            return PtzCapability(), None
        path = self._local_path(xaddr, self._device_path)
        profiles = await self.profiles(await self.media_service_path())
        profile = next((p for p in profiles if p.ptz_configuration_token), None)
        if profile is None:
            return PtzCapability(), None
        opts = await self._call(
            path,
            f"{ns}/GetConfigurationOptions",
            f'<p:GetConfigurationOptions xmlns:p="{ns}"><p:ConfigurationToken>'
            f"{escape(profile.ptz_configuration_token)}</p:ConfigurationToken></p:GetConfigurationOptions>",
        )

        def normalized(kind, uri, bounds):
            for node in opts.iter():
                if _local(node.tag) != kind or text(node, "URI") != uri:
                    continue
                if all(
                    _float(text(node, axis, "Min")) == lo and _float(text(node, axis, "Max")) == hi
                    for axis, lo, hi in bounds
                ):
                    return True
            return False

        base = "http://www.onvif.org/ver10/tptz/"
        presets = await self._call(
            path,
            f"{ns}/GetPresets",
            f'<p:GetPresets xmlns:p="{ns}">'
            f"<p:ProfileToken>{escape(profile.token)}</p:ProfileToken></p:GetPresets>",
        )
        import re

        tokens = [n.get("token") for n in presets.iter() if _local(n.tag) == "Preset"]
        return PtzCapability(
            supported=True,
            profile_token=profile.token,
            absolute_pan_tilt=normalized(
                "AbsolutePanTiltPositionSpace",
                base + "PanTiltSpaces/PositionGenericSpace",
                [("XRange", -1, 1), ("YRange", -1, 1)],
            ),
            absolute_zoom=normalized(
                "AbsoluteZoomPositionSpace", base + "ZoomSpaces/PositionGenericSpace", [("XRange", 0, 1)]
            ),
            presets=[t for t in tokens if t and re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9_-]{0,127}", t)][:256],
        ), path

    async def ptz_action(self, path, profile, command):
        ns = "http://www.onvif.org/ver20/ptz/wsdl"
        base = "http://www.onvif.org/ver10/tptz/"
        if command.action == "stop":
            action, extra = "Stop", "<p:PanTilt>true</p:PanTilt><p:Zoom>true</p:Zoom>"
        elif command.action == "preset":
            action, extra = "GotoPreset", f"<p:PresetToken>{escape(command.preset)}</p:PresetToken>"
        else:
            action, extra = "AbsoluteMove", "<p:Position>"
            if command.pan is not None:
                extra += (
                    f'<tt:PanTilt x="{command.pan}" y="{command.tilt}" '
                    f'space="{base}PanTiltSpaces/PositionGenericSpace"/>'
                )
            if command.zoom is not None:
                extra += f'<tt:Zoom x="{command.zoom}" space="{base}ZoomSpaces/PositionGenericSpace"/>'
            extra += "</p:Position>"
        response = await self._call(
            path,
            f"{ns}/{action}",
            f'<p:{action} xmlns:p="{ns}">'
            f"<p:ProfileToken>{escape(profile)}</p:ProfileToken>{extra}</p:{action}>",
        )
        if find(response, action + "Response") is None:
            raise CameraProtocolError("PTZ acknowledgement missing")
