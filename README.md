# Vergil SDK

Python SDK for interacting with the Vergil station local API. Provides both synchronous and asynchronous clients with automatic developer-token authentication, typed response models, and real-time MQTT subscriptions over WebSocket.

## Installation

```bash
pip install vergil-sdk
```

Requires Python 3.10+.

## Quick start

```python
from vergil_sdk import VergilClient

with VergilClient(
    "http://192.168.1.10:8080",
    galleon_url="https://galleon.example.com",
    developer_token="dev_ey...",
) as client:
    health = client.health()
    print(health.status, health.id)

    metrics = client.get_metrics()
    print(metrics.computing)
```

The SDK exchanges your long-lived developer token for a short-lived station token automatically, and refreshes it before it expires. You never need to manage station tokens yourself.

## Authentication

There are two ways to authenticate:

### Developer token (recommended)

Obtain a developer token from the Galleon UI. It is valid for 1 year. Pass it along with `galleon_url` -- the SDK exchanges it for a 15-minute station token behind the scenes and refreshes it automatically. `station_id` is optional; when omitted the SDK auto-discovers it from the station's `/health` endpoint.

```python
client = VergilClient(
    "http://192.168.1.10:8080",
    galleon_url="https://galleon.example.com",
    developer_token="dev_ey...",
)
```

### Raw station token

If you already have a station JWT (e.g. from your own exchange flow), pass it directly. You are responsible for refreshing it before the 15-minute expiry.

```python
client = VergilClient(
    "http://192.168.1.10:8080",
    token="ey...",
)
```

## Sync vs. async

Every feature is available in both a synchronous and an asynchronous client. The API is identical -- the async variant prefixes calls with `await`.

```python
# Synchronous
from vergil_sdk import VergilClient

with VergilClient("http://...", developer_token="...") as client:
    status = client.get_crane_status()
```

```python
# Asynchronous
import asyncio
from vergil_sdk import AsyncVergilClient

async def main():
    async with AsyncVergilClient("http://...", developer_token="...") as client:
        status = await client.get_crane_status()

asyncio.run(main())
```

Both clients support context managers and can also be used without them (call `.close()` when done).

## API reference

### Health

```python
health = client.health()
# HealthStatus(status="ok", id="sta_abc123")
```

No authentication required. Use this to check if the station is reachable.

### Metrics

```python
metrics = client.get_metrics()
# Metrics(computing={"cpu": ..., "gpu": ..., "ram": ...}, last_report_ts="...")
```

Returns system-level metrics: CPU, GPU, RAM, storage, and network statistics.

### Sensors

```python
# All sensor data
sensors = client.get_sensors()
# SensorData(power={...}, battery={...})

# Individual sensors
power = client.get_power()      # dict
battery = client.get_battery()  # dict
```

### Crane control

```python
# Read status
status = client.get_crane_status()
print(status.status)  # "up", "down", "moving", "unknown", etc.
print(status.raw)     # full status dict from MQTT

# Send command
result = client.send_crane_command("crane_up")
# CraneCommandResult(command="crane_up")
```

Valid commands: `crane_up`, `crane_down`, `crane_stop`.

### Camera streams

```python
# List all camera RTSP proxies
streams = client.list_streams()
for stream in streams:
    print(stream.camera_id, stream.rtsp_uri, stream.port)

# Single camera
stream = client.get_stream("camera_front")
print(stream.rtsp_uri)  # rtsp://user:pass@0.0.0.0:10000
```

#### Live microphone audio

The mic endpoint returns a chunked `audio/ogg` (Opus) stream. The SDK returns the raw `httpx.Response` for you to consume:

```python
# Sync
resp = client.stream_mic()
with open("recording.ogg", "wb") as f:
    for chunk in resp.iter_bytes():
        f.write(chunk)
resp.close()

# Async
resp = await client.stream_mic()
async for chunk in resp.aiter_bytes():
    process(chunk)
await resp.aclose()
```

### Frigate events

```python
# List events with filters
result = client.list_events(
    camera="front_door",
    label="person",
    start=1700000000.0,
    end=1700003600.0,
    limit=50,
    offset=0,
)
print(result.count)
for event in result.events:
    print(event.id, event.camera, event.label, event.start_time)
    print(event.zones, event.data)

# Single event by ID
event = client.get_event("1700000000.123456-abc123")
```

All filter parameters are optional. Events are returned newest-first, with a default limit of 100 (max 1000).

### Video

```python
# Download a detection clip by review ID
mp4_bytes = client.download_clip("review_abc123")

# ...or save directly to disk
path = client.download_clip_to("review_abc123", "clip.mp4")

# List recording segments for a camera and time range
segments = client.list_segments(
    camera="front_door",
    start=1700000000.0,
    end=1700003600.0,
)
for seg in segments.segments:
    print(seg.camera, seg.start_time, seg.end_time, seg.duration)

# Download concatenated video for a time range
mp4_bytes = client.download_video(
    camera="front_door",
    start=1700000000.0,
    end=1700003600.0,
)

# ...or save directly to disk
path = client.download_video_to(
    camera="front_door",
    start=1700000000.0,
    end=1700003600.0,
    path="recording.mp4",
)
```

Video downloads use a longer timeout (120s) to accommodate segment concatenation on the station.

### Real-time MQTT (WebSocket)

Subscribe to MQTT topics and receive messages in real time over WebSocket. Supports the same two authentication modes as the HTTP client.

```python
import asyncio
from vergil_sdk import MQTTSubscription

async def main():
    async with MQTTSubscription(
        "http://192.168.1.10:8080",
        galleon_url="https://galleon.example.com",
        developer_token="dev_ey...",
    ) as mqtt:
        # Subscribe to topic patterns (MQTT wildcards supported)
        ack = await mqtt.subscribe(["telem/#", "sensors/power"])
        print(ack.action, ack.topics)

        # Iterate over incoming messages
        async for msg in mqtt:
            print(msg.topic, msg.payload, msg.timestamp)

        # Unsubscribe when needed
        await mqtt.unsubscribe(["sensors/power"])

asyncio.run(main())
```

The `MQTTSubscription` accepts `http://` or `ws://` URLs -- `http://` is automatically converted to `ws://` (and `https://` to `wss://`).

MQTT topic wildcards are supported:
- `+` matches a single level (e.g. `sensors/+` matches `sensors/power` and `sensors/battery`)
- `#` matches all remaining levels (e.g. `telem/#` matches `telem/crane/status` and `telem/gps`)

## Error handling

All API errors raise typed exceptions that extend `VergilError`:

```python
from vergil_sdk import (
    VergilError,
    AuthenticationError,
    BadRequestError,
    NotFoundError,
    ServerError,
    ConnectionError,
)

try:
    client.get_event("nonexistent")
except NotFoundError as e:
    print(e)              # "event not found"
    print(e.status_code)  # 404
except AuthenticationError:
    print("Token expired or invalid")
except ConnectionError:
    print("Station is unreachable")
except VergilError as e:
    print(f"API error {e.status_code}: {e}")
```

| Exception | HTTP status | When |
|---|---|---|
| `AuthenticationError` | 401 | Missing, expired, or invalid token; token exchange failure |
| `BadRequestError` | 400 | Invalid parameters or request body |
| `NotFoundError` | 404 | Resource does not exist |
| `ServerError` | 5xx | Internal station error |
| `ConnectionError` | -- | Station is unreachable |

## Response models

All responses are returned as frozen dataclasses with typed fields:

| Model | Fields |
|---|---|
| `HealthStatus` | `status: str`, `id: str` |
| `Metrics` | `computing: dict`, `last_report_ts: str \| None` |
| `SensorData` | `power: dict`, `battery: dict` |
| `CraneStatus` | `raw: dict`, property `status: str` |
| `CraneCommandResult` | `command: str` |
| `Stream` | `camera_id: str`, `rtsp_uri: str`, `port: int` |
| `FrigateEvent` | `raw: dict`, properties `id`, `camera`, `label`, `start_time`, `end_time`, `zones`, `data` |
| `FrigateEventList` | `events: list[FrigateEvent]`, `count: int` |
| `RecordingSegment` | `camera: str`, `start_time: float`, `end_time: float`, `duration: float`, `path: str` |
| `SegmentList` | `segments: list[RecordingSegment]`, `count: int` |
| `MQTTMessage` | `topic: str`, `payload: Any`, `timestamp: str` |
| `MQTTAck` | `action: str`, `topics: list[str]` |

## Configuration options

Both `VergilClient` and `AsyncVergilClient` accept:

| Parameter | Type | Default | Description |
|---|---|---|---|
| `base_url` | `str` | *(required)* | Station API URL, e.g. `http://192.168.1.10:8080` |
| `developer_token` | `str` | `None` | Long-lived developer token (1-year expiry) |
| `galleon_url` | `str` | `None` | Galleon server URL (required with `developer_token`) |
| `station_id` | `str` | `None` | Target station ID (optional; auto-discovered from `/health` if omitted) |
| `token` | `str` | `None` | Raw station JWT (alternative to developer token) |
| `timeout` | `float` | `30.0` | Default request timeout in seconds |
