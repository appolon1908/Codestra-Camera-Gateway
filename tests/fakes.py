"""Test doubles for subprocess execution, ONVIF devices and JPEG frames."""

from __future__ import annotations

import json
import struct
from collections.abc import Callable
from dataclasses import dataclass, field

import httpx

from camera_gateway.adapters.process import ProcessResult

SECRET = "s3cr3t:P@ss/w0rd"
TOKEN = "t" * 40
TENANT = "tenant-a"
OTHER_TENANT = "tenant-b"
PASSWORD_REF = "openbao://kv/cameras/lobby#password"


def make_jpeg(width: int = 1920, height: int = 1080) -> bytes:
    app0 = b"\xff\xe0" + struct.pack(">H", 16) + b"JFIF\x00\x01\x01\x00\x00\x01\x00\x01\x00\x00"
    sof0 = (
        b"\xff\xc0" + struct.pack(">HBHHB", 17, 8, height, width, 3) + b"\x01\x22\x00\x02\x11\x01\x03\x11\x01"
    )
    return b"\xff\xd8" + app0 + sof0 + b"\xff\xd9"


def ffprobe_json(codec: str = "h264", profile: str = "Main", width: int = 1920, height: int = 1080) -> bytes:
    return json.dumps(
        {
            "streams": [
                {
                    "codec_type": "video",
                    "codec_name": codec,
                    "profile": profile,
                    "width": width,
                    "height": height,
                    "avg_frame_rate": "25/1",
                    "pix_fmt": "yuvj420p",
                }
            ]
        }
    ).encode()


@dataclass
class FakeRunner:
    """Records argv; returns a scripted result (or raises) per binary name."""

    results: dict[str, ProcessResult | Exception] = field(default_factory=dict)
    calls: list[list[str]] = field(default_factory=list)
    binaries_available: bool = True

    def ok_snapshot(self, jpeg: bytes | None = None) -> None:
        self.results["ffmpeg"] = ProcessResult(0, jpeg if jpeg is not None else make_jpeg(), "")

    def ok_probe(self, payload: bytes | None = None) -> None:
        self.results["ffprobe"] = ProcessResult(0, payload if payload is not None else ffprobe_json(), "")

    def fail(self, binary: str, stderr: str, code: int = 1) -> None:
        self.results[binary] = ProcessResult(code, b"", stderr)

    async def run(self, argv: list[str], timeout_s: float, max_stdout: int) -> ProcessResult:
        self.calls.append(argv)
        result = self.results.get(argv[0])
        if result is None:
            raise AssertionError(f"unexpected process {argv[0]}")
        if isinstance(result, Exception):
            raise result
        return result

    def available(self, binary: str) -> bool:
        return self.binaries_available

    def url_of(self, index: int = -1) -> str:
        argv = self.calls[index]
        return argv[argv.index("-i") + 1]


# -- ONVIF device simulator ------------------------------------------------------------

DEVICE_INFO = """<?xml version="1.0"?>
<s:Envelope xmlns:s="http://www.w3.org/2003/05/soap-envelope"
 xmlns:tds="http://www.onvif.org/ver10/device/wsdl"><s:Body>
<tds:GetDeviceInformationResponse><tds:Manufacturer>EZVIZ</tds:Manufacturer>
<tds:Model>CS-H8c</tds:Model><tds:FirmwareVersion>V5.3.8</tds:FirmwareVersion>
<tds:SerialNumber>BD1234567</tds:SerialNumber><tds:HardwareId>88</tds:HardwareId>
</tds:GetDeviceInformationResponse></s:Body></s:Envelope>"""

CAPABILITIES = """<?xml version="1.0"?>
<s:Envelope xmlns:s="http://www.w3.org/2003/05/soap-envelope"
 xmlns:tds="http://www.onvif.org/ver10/device/wsdl" xmlns:tt="http://www.onvif.org/ver10/schema">
<s:Body><tds:GetCapabilitiesResponse><tds:Capabilities><tt:Media>
<tt:XAddr>http://10.99.99.99/onvif/Media</tt:XAddr></tt:Media></tds:Capabilities>
</tds:GetCapabilitiesResponse></s:Body></s:Envelope>"""

PROFILES = """<?xml version="1.0"?>
<s:Envelope xmlns:s="http://www.w3.org/2003/05/soap-envelope"
 xmlns:trt="http://www.onvif.org/ver10/media/wsdl" xmlns:tt="http://www.onvif.org/ver10/schema">
<s:Body><trt:GetProfilesResponse>
<trt:Profiles token="Profile_1" fixed="true"><tt:Name>mainStream</tt:Name>
<tt:VideoEncoderConfiguration token="VE1"><tt:Name>VE1</tt:Name><tt:Encoding>H264</tt:Encoding>
<tt:Resolution><tt:Width>1920</tt:Width><tt:Height>1080</tt:Height></tt:Resolution>
<tt:RateControl><tt:FrameRateLimit>25</tt:FrameRateLimit><tt:BitrateLimit>2048</tt:BitrateLimit></tt:RateControl>
<tt:H264><tt:GovLength>50</tt:GovLength><tt:H264Profile>Main</tt:H264Profile></tt:H264>
</tt:VideoEncoderConfiguration></trt:Profiles>
<trt:Profiles token="Profile_2" fixed="true"><tt:Name>subStream</tt:Name>
<tt:VideoEncoderConfiguration token="VE2"><tt:Name>VE2</tt:Name><tt:Encoding>H264</tt:Encoding>
<tt:Resolution><tt:Width>640</tt:Width><tt:Height>360</tt:Height></tt:Resolution>
<tt:RateControl><tt:FrameRateLimit>15</tt:FrameRateLimit><tt:BitrateLimit>512</tt:BitrateLimit></tt:RateControl>
<tt:H264><tt:H264Profile>Baseline</tt:H264Profile></tt:H264>
</tt:VideoEncoderConfiguration></trt:Profiles>
</trt:GetProfilesResponse></s:Body></s:Envelope>"""


def stream_uri(uri: str) -> str:
    return f"""<?xml version="1.0"?>
<s:Envelope xmlns:s="http://www.w3.org/2003/05/soap-envelope"
 xmlns:trt="http://www.onvif.org/ver10/media/wsdl" xmlns:tt="http://www.onvif.org/ver10/schema">
<s:Body><trt:GetStreamUriResponse><trt:MediaUri><tt:Uri>{uri}</tt:Uri>
<tt:InvalidAfterConnect>false</tt:InvalidAfterConnect></trt:MediaUri>
</trt:GetStreamUriResponse></s:Body></s:Envelope>"""


AUTH_FAULT = """<?xml version="1.0"?>
<s:Envelope xmlns:s="http://www.w3.org/2003/05/soap-envelope"
 xmlns:ter="http://www.onvif.org/ver10/error"><s:Body><s:Fault>
<s:Code><s:Value>s:Sender</s:Value><s:Subcode><s:Value>ter:NotAuthorized</s:Value></s:Subcode></s:Code>
<s:Reason><s:Text xml:lang="en">Sender not Authorized</s:Text></s:Reason></s:Fault></s:Body></s:Envelope>"""


@dataclass
class OnvifDevice:
    """httpx transport simulating an ONVIF device. Override per action to inject faults."""

    overrides: dict[str, Callable[[httpx.Request], httpx.Response]] = field(default_factory=dict)
    requests: list[httpx.Request] = field(default_factory=list)

    @staticmethod
    def action(request: httpx.Request) -> str:
        ctype = request.headers.get("content-type", "")
        return ctype.split('action="', 1)[-1].rstrip('"').rsplit("/", 1)[-1]

    def handler(self, request: httpx.Request) -> httpx.Response:
        self.requests.append(request)
        action = self.action(request)
        if action in self.overrides:
            return self.overrides[action](request)
        if action == "GetSystemDateAndTime":
            return httpx.Response(500)
        body = {
            "GetDeviceInformation": DEVICE_INFO,
            "GetCapabilities": CAPABILITIES,
            "GetProfiles": PROFILES,
        }.get(action)
        if action == "GetStreamUri":
            if b"Profile_1" in request.content:
                body = stream_uri("rtsp://admin:leaked@192.168.1.50:554/Streaming/Channels/101")
            else:
                body = stream_uri("rtsp://10.0.0.9:554/Streaming/Channels/102?transportmode=unicast")
        if body is None:
            return httpx.Response(400)
        return httpx.Response(200, text=body, headers={"content-type": "application/soap+xml"})

    def transport(self) -> httpx.MockTransport:
        return httpx.MockTransport(self.handler)
