from dataclasses import dataclass, field
from typing import Any, Dict, List, Optional

@dataclass
class Manifest:
    """Representation of a Manifest response."""
    raw_data: Dict[str, Any] = field(default_factory=dict)

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

@dataclass
class ProvisioningInfo:
    """Provisioning details including SIP info and camera streams."""
    sip_info: SipInfo
    rtsp_video_url: str
    called_elements: List[CalledElement] = field(default_factory=list)
    raw_data: Dict[str, Any] = field(default_factory=dict)

    @classmethod
    def from_dict(cls, data: Dict[str, Any]) -> "ProvisioningInfo":
        sip_info = SipInfo(
            sip_id=data.get("sipId", ""),
            sip_password=data.get("sipPassword", "")
        )
        called_elements = []
        for element in data.get("calledElements", []):
            called_elements.append(
                CalledElement(
                    sip_id=element.get("sipId", ""),
                    name=element.get("name")
                )
            )
        return cls(
            sip_info=sip_info,
            rtsp_video_url=data.get("rtspVideoUrl", ""),
            called_elements=called_elements,
            raw_data=data
        )
