# TJA470 doorphone protocol notes

What we know about how the Hager TJA470 talks to doorphone clients, gathered
from the library's own traffic, packet captures of the official Elcom Access
Android app, the device's web interface and its activity log.

Everything here was observed on a single installation (server version 8.2.2,
firmware/BSP 2.7.3, doorphone software 4.0.2, two outdoor stations). Statements
marked *(unverified)* are inferences rather than observations.

## Overview

The TJA470 is a gateway between the wired 2-wire doorphone bus (outdoor
stations, indoor units) and IP clients. An IP client (the Elcom Access app,
this library) uses three channels:

| Channel | Port | Purpose |
|---|---|---|
| HTTP API | 80 | pairing, provisioning, commands (camera, door), versions |
| WebSocket event bus | 80 | push events (current camera, incoming calls, call history) |
| SIP (Asterisk) | UDP 5060 | registration, incoming doorbell calls, audio |
| RTSP | 9099 | video from the currently selected outdoor station |

The server side runs on an OSGi platform (ProSyst mBS; responses carry an
`x-mbs-platform-state` header) in a Java servlet container.

## HTTP API

Base URL: `http://<host>/API`.

### Authentication and sessions

- The API uses HTTP Basic Auth with a local TJA470 user account.
- Sessions are tracked with a `JSESSIONID` cookie. The device sets it even on
  `401` responses, and accepts it on later requests without credentials.
- A request without a valid session gets `401` with an HTML login page
  (title "Hager Pilot OIDC"); retrying with Basic Auth succeeds.
- Sessions survive at least 2 minutes idle; with regular polling they do not
  expire. Restoring a saved `JSESSIONID` in a new client works.
- Sessions are held in memory: a server restart invalidates them
  *(unverified, expected for a servlet container)*.
- The device is usually addressed by IP. aiohttp's default cookie jar drops
  cookies from IP hosts, which made every request log in again and create a
  new server session (fixed in 0.1.8 by a runner-owned `CookieJar(unsafe=True)`).

### Endpoints used by doorphone clients

| Method | Path | Request | Response |
|---|---|---|---|
| GET | `/API/manifest` | | device info: `app`, `fw` (firmware/BSP version), `ref` (`TJA470`), `host`, `man`, `sn`, `addr`, `mac`, `remote`, `auth` |
| GET | `/API/runtime/provisioning/freedevices` | | unassigned `MOBILE_CLIENT` slots |
| POST | `/API/runtime/pairing/setuid` | slot id and client UID | pairs a UID with a slot |
| POST | `/API/runtime/provisioning` | `{"uid": "<uid>"}`, optionally `"version": "<last version>"` | `200` with provisioning info, or `304` if `version` is unchanged |
| POST | `/API/runtime/command/camera/switch/{uid}` | `{}` | `{"order": n}`, the new current camera position |
| GET | `/API/runtime/command/camera/current/{uid}` | | `{"order": n}`, the current camera position |
| POST | `/API/runtime/command/doorrelease/{id}` | `{}` | `204` |
| GET | `/API/runtime/platform/softwareversion` | | `{"softwareVersion": "4.0.2"}`, the doorphone software version |
| GET | `/API/runtime/platform/isalive?serialNumber=<sn>` | | `{"match": true}` |

Notes:

- **Provisioning** contains the client's own SIP id and password, the RTSP and
  MJPEG URLs (with a `${ipadress}` placeholder), `calledElements` (all mobile
  clients and outdoor stations with their SIP ids; outdoor stations have an
  `order`, which is their camera position), permissions (`doorCallAllowed`,
  `doorReleaseAllowed`, `cameraEventAllowed`, `imageMemoryAccess`), remote
  access details (ngrok endpoints, a separate SIP id, TURN credentials) and a
  `version` that changes when the configuration changes.
- **Camera position**: the device keeps one "current device" (camera
  position). `camera/switch` cycles to the next outdoor station; there is no
  observed "switch to position n" call, so clients cycle until the wanted
  `order` comes back. A switch takes about 1.1 s to answer.
- **Door release** releases the door of the currently selected outdoor
  station. The Elcom Access app sends its own SIP id as `{id}`
  (`doorrelease/6000`), so `{id}` is most likely meant to identify the
  requesting client. The device does not validate it: `doorrelease/1`, which
  matches no client, also opens the door. Whether the server enforces the
  client's `doorReleaseAllowed` permission (by `{id}`, session or user) or
  leaves that to the app is untested.
- **Commands succeed even when the bus link is broken**: `camera/switch`
  returns a new `order` and `doorrelease` returns `204` although nothing
  happens physically (see [Known failure mode](#known-failure-mode)). The
  responses only confirm that the server accepted the command.

## Event bus (WebSocket)

`ws://<host>/remote/events/?topics=[com/hager/doorphone/runtime/rest/*]`

Authenticated with the session cookie. Each message is a JSON text frame:

```json
{
  "topic": "com/hager/doorphone/runtime/rest/currentDevice/UPDATED",
  "properties": {
    "subscription.id": "...",
    "com.prosyst.mbs.services.remote.event.sequence.number": 0,
    "event.topics": "com/hager/doorphone/runtime/rest/currentDevice/UPDATED",
    "value": {"order": 0}
  }
}
```

The server also sends WebSocket ping frames.

Observed topics (below `com/hager/doorphone/runtime/rest/`):

| Topic | `value` | When |
|---|---|---|
| `currentDevice/UPDATED` | `{"order": n}` | camera position changed or (re)activated; about 1.1 s after `camera/switch`, also when a call comes in |
| `INCOMINGCALL/{id}` | `null` | a doorbell call starts |
| `callhistory/CREATED/{id}` | `null` | a call history entry is created (at ring) |
| `callhistory/UPDATED/{id}` | `null` | the call history entry changes (e.g. call ended unanswered, about 45 s after the ring) |

The Elcom Access app shows its ringing screen based on `INCOMINGCALL`, not on
the SIP `INVITE`.

The web interface uses the same endpoint with other topics, e.g.
`com/hager/osgi/fw/services/application/ApplicationEvent/*`,
`com/hager/server/alert/AlertEvent/ADD`,
`com/hg/osgi/fwk/system/remoteAccessEvent/UPDATED`.

## SIP

- Server: Asterisk PBX 18.10.1 on UDP 5060, digest auth (realm `asterisk`,
  MD5, `qop=auth`), credentials from provisioning.
- Clients register with `Expires: 120`; this library re-registers every 60 s.
- Asterisk sends `OPTIONS` to the registered contact every 4 s and expects
  `200 OK`.
- Doorbell calls arrive as `INVITE` from the outdoor station's SIP id
  (e.g. `sip:4000@<host>`, `sip:4001@<host>`; the ids are in
  `calledElements`).
- The remote-access path uses a separate SIP id (the client's id + 1).
- The Elcom Access app answers an `INVITE` with `500 Call Error` when it was
  in the background, and with nothing at all when it is open, so calls cannot
  be accepted from it (observed with app version current in October 2026).

### This library's SIP client

`TJA470SipPhone` registers with the credentials from provisioning, receives
doorbell calls and makes outgoing calls to other SIP ids (other clients or
outdoor stations). Audio can be sent and received raw, or converted to a
format usable from a browser over a WebSocket. It is designed for use from a
Home Assistant integration whose card shows the active call and offers answer,
reject, hang up, open door at position and switch camera.

## Video

### RTSP

`rtsp://<host>:9099/high` (from provisioning), served by a GStreamer RTSP
server: H.264 Baseline, 640x480, 15 fps, `a=type:broadcast`. The stream is the
same for all clients and is not tied to a client UID.

What the stream shows:

| State | Stream content | SDP bandwidth |
|---|---|---|
| idle | still placeholder: camera icon in circular arrows on dark blue | `b=AS:2141` |
| outdoor station video active | live camera picture | `b=AS:6594` |
| switched, but no video arrives | uniform dark grey (every pixel luma 32), for at least 30 s | |

The live picture appears after a switch, during a doorbell call, and when the
Elcom Access app opens (see [Open questions](#open-questions)). When the
selected source changes, the app tears down and re-establishes the RTSP
session.

### MJPEG

`http://<host>:8021/mjpg/high` is advertised in provisioning. The port refused
connections while the video was idle *(only checked once)*.

## Elcom Access app sequences

App start (live view opens automatically):

1. `GET camera/current/{uid}`, `GET platform/softwareversion`
2. RTSP `OPTIONS`/`DESCRIBE`/`SETUP`/`PLAY`
3. `POST provisioning` with the last `version` (`200` or `304`)
4. `GET platform/isalive?serialNumber=...` (first try `401`, then with auth)
5. WebSocket subscription to `com/hager/doorphone/runtime/rest/*`
6. `currentDevice/UPDATED` arrives about 1.6 s after start; the app reconnects
   RTSP and gets live video
7. SIP `REGISTER`

Switch camera: `POST camera/switch/{uid}` → `{"order": n}`, then
`currentDevice/UPDATED {"order": n}` about 1.1 s later.

Door release: `POST doorrelease/{own sip id}` (sent twice per button press).

Doorbell ring with the app open: `currentDevice/UPDATED`,
`callhistory/CREATED/{id}` and `INCOMINGCALL/{id}` on the event bus, plus a
SIP `INVITE` (not visible in the capture). Camera switching works during the
ring.

The app uses client UIDs of the form `android-<uuid>`.

## Web interface and administration

The web interface (`/rootapp/`) uses many more endpoints, for example
`/API/administration/*`, `/API/backup/*`, `/API/cloud/status`,
`/API/knx/individualaddress`, `/API/odt/updateAdmin/update/*`. Not used by
this library; listed for reference:

- **Restart:** `PUT /m2m/fim/items/fim:system:HagerPilotService/operations/reboot`
  with `{"arguments": []}` *(not tried)*.
- **Activity log:** `POST /v1/easy/reporting/generatereport?language=en_EN`
  with `{"filter": "all", "isdevice": false, "REPORT_TYPE": "ActivityLogger", "REPORT_FORMAT": "TEXT"}`,
  followed by a GET under `/v1/easy/reporting/` to fetch it *(not tried; exact
  path unknown)*.
- `/doorphone/index.html` returned `503`.

The activity log records server starts and stops (`Server started`,
`Server stopped`, `Server restart requested by administrator`), version
updates, bus connection changes and remote-access changes. A server restart
takes about 10 minutes until the server is online again.

## Known failure mode

Observed September–October 2026:

- On 2026-09-16 the server stopped and restarted on its own (no "requested by
  administrator" entry; likely an automatic update). The last doorbell call
  reached the integration the day before.
- Afterwards the IP side worked normally (API, SIP registration and keepalives,
  RTSP placeholder), but nothing crossed between the bus and IP: no `INVITE`
  on doorbell rings, `camera/switch` produced only the uniform grey picture,
  `doorrelease` returned `204` without opening the door. The wired indoor units
  kept working.
- A server restart from the web interface did not fix it. Power-cycling the
  KNX/2-wire bus supply (the TJA470 itself kept running) did.
- After recovery, server restarts from the web interface, with and without
  this library's client active, did not reproduce the failure. Re-pairing a
  client had fixed the same symptoms in the past *(probably because a
  configuration change re-initialises the bus link; unverified)*.

Clients cannot detect this from command responses. Possible signals: a
firmware or software version change, a server restart (the session cookie
stops working), or no `currentDevice/UPDATED` event / no live video after a
switch *(unverified)*.

## Open questions

- What turns the camera on when the Elcom Access app starts without a switch:
  `GET camera/current`, the event bus subscription, or the RTSP connection.
- Whether `doorReleaseAllowed` is enforced by the server, and whether
  `{id}` in `doorrelease/{id}` plays any role in that.
- The SIP `INVITE` and answer flow as used by the app (incoming SIP requests
  were not captured).
- Whether the event bus reports call end, answer or door release explicitly.
