from dataclasses import dataclass, field
from typing import Any, Dict, List, Optional

@dataclass
class Manifest:
    """Representation of a Manifest response."""
    raw_data: Dict[str, Any] = field(default_factory=dict)

    @property
    def fw(self) -> Optional[str]:
        """Get the firmware version from the manifest raw data."""
        return self.raw_data.get("fw")

@dataclass
class FreeDevice:
    """Representation of a free device."""
    id: int
    name: Optional[str] = None
    mac: Optional[str] = None
    
    @classmethod
    def from_dict(cls, data: Dict[str, Any]) -> "FreeDevice":
        return cls(
            id=data.get("id", -1),
            name=data.get("name"),
            mac=data.get("mac")
        )

@dataclass
class SipInfo:
    """SIP connection details."""
    sip_id: str
    sip_password: str

@dataclass
class CalledElement:
    """A called element, e.g. a station."""
    sip_id: str
    name: Optional[str] = None
    order: Optional[int] = None

@dataclass
class RemoteAccessInfo:
    """Remote access details including TURN/STUN and NGROK tunnels."""
    sip_id: str
    sip_password: str
    ngrok_url: str
    rtsp_url: str
    rtsp_port: int
    sip_tcp_url: str
    sip_tcp_port: int
    ws_port: int
    stun_turn_prefix: str
    stun_turn_user: str
    stun_turn_password: str
    stun_turn_hostname: str
    stun_turn_port: int

    @classmethod
    def from_dict(cls, data: Dict[str, Any]) -> "RemoteAccessInfo":
        return cls(
            sip_id=data.get("sipId", ""),
            sip_password=data.get("sipPassword", ""),
            ngrok_url=data.get("ngrokUrl", ""),
            rtsp_url=data.get("rtspUrl", ""),
            rtsp_port=int(data.get("rtspPort", 0)) if data.get("rtspPort") else 0,
            sip_tcp_url=data.get("sipTcpUrl", ""),
            sip_tcp_port=int(data.get("sipTcpPort", 0)) if data.get("sipTcpPort") else 0,
            ws_port=int(data.get("wsPort", 0)) if data.get("wsPort") else 0,
            stun_turn_prefix=data.get("stunTurnPrefix", ""),
            stun_turn_user=data.get("stunTurnUser", ""),
            stun_turn_password=data.get("stunTurnPassword", ""),
            stun_turn_hostname=data.get("stunTurnHostname", ""),
            stun_turn_port=int(data.get("stunTurnPort", 0)) if data.get("stunTurnPort") else 0,
        )

@dataclass
class ProvisioningInfo:
    """Provisioning details including SIP info, camera streams, and remote access."""
    sip_info: SipInfo
    rtsp_video_url: str
    http_video_url: str
    local_ip_address: str
    door_release_allowed: bool
    called_elements: List[CalledElement] = field(default_factory=list)
    remote_access: Optional[RemoteAccessInfo] = None
    raw_data: Dict[str, Any] = field(default_factory=dict)

    @classmethod
    def from_dict(cls, data: Dict[str, Any]) -> "ProvisioningInfo":
        sip_info = SipInfo(
            sip_id=data.get("sipId", ""),
            sip_password=data.get("sipPassword", "")
        )
        called_elements = []
        for element in data.get("calledElements", []):
            order_val = element.get("order")
            called_elements.append(
                CalledElement(
                    sip_id=element.get("sipId", ""),
                    name=element.get("name"),
                    order=int(order_val) if order_val is not None else None
                )
            )
        remote_data = data.get("remoteAccess")
        remote_access = RemoteAccessInfo.from_dict(remote_data) if remote_data else None

        return cls(
            sip_info=sip_info,
            rtsp_video_url=data.get("rtspVideoUrl", ""),
            http_video_url=data.get("httpVideoUrl", ""),
            local_ip_address=data.get("localIpAddress", ""),
            door_release_allowed=data.get("doorReleaseAllowed", False),
            called_elements=called_elements,
            remote_access=remote_access,
            raw_data=data
        )
