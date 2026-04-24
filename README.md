# Vergil SDK

Python SDK for interacting with the Vergil station local API. Provides both synchronous and asynchronous clients backed by cached OAuth 2.1 credentials, typed response models, and real-time MQTT subscriptions over WebSocket.

## Installation

```bash
pip install vergil-sdk
```

Requires Python 3.10+.

## Quick start

First, sign in once from your terminal:

```bash
vergil login --galleon-url https://galleon.example.com --station-id sta_abc123
```

This opens your browser for consent and writes refreshable credentials to `~/.config/vergil/credentials.json` (macOS: `~/Library/Application Support/vergil/`).

Then use the SDK — it loads and refreshes the cached credentials automatically:

```python
from vergil_sdk import VergilClient

with VergilClient(
    "http://192.168.1.10:8082",
    galleon_url="https://galleon.example.com",
    station_id="sta_abc123",
) as client:
    health = client.health()
    print(health.status, health.id)

    metrics = client.get_metrics()
    print(metrics.computing)
```

## Authentication

### `vergil login` (recommended)

The `vergil` CLI drives Galleon's OAuth 2.1 flow and caches the resulting tokens:

```bash
# Loopback (opens browser, handles callback on 127.0.0.1)
vergil login --galleon-url https://galleon.example.com --station-id sta_abc123

# Device authorization (headless — prints a URL and user code)
vergil login --galleon-url https://galleon.example.com --station-id sta_abc123 --device

# Show cached identity
vergil whoami --galleon-url https://galleon.example.com --station-id sta_abc123

# Sign out
vergil logout --galleon-url https://galleon.example.com --station-id sta_abc123
```

The SDK reads the cached credential, silently refreshes the short-lived access token when it's near expiry, and raises `AuthenticationError("no credentials — run `vergil login`")` if the cache is empty or the refresh is rejected.

```python
from vergil_sdk import VergilClient

with VergilClient(
    "http://192.168.1.10:8082",
    galleon_url="https://galleon.example.com",
    station_id="sta_abc123",
) as client:
    ...
```

### Raw station token

If you already hold a station JWT (e.g. issued by your own OAuth client), pass it directly. You manage expiry yourself.

```python
client = VergilClient(
    "http://192.168.1.10:8082",
    token="ey...",
)
```

## Sync vs. async

Every feature is available in both a synchronous and an asynchronous client. The API is identical — the async variant prefixes calls with `await`.

```python
# Synchronous
from vergil_sdk import VergilClient

with VergilClient(
    "http://...",
    galleon_url="https://galleon.example.com",
    station_id="sta_abc123",
) as client:
    status = client.get_crane_status()
```

```python
# Asynchronous
import asyncio
from vergil_sdk import AsyncVergilClient

async def main():
    async with AsyncVergilClient(
        "http://...",
        galleon_url="https://galleon.example.com",
        station_id="sta_abc123",
    ) as client:
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

No authentication required.

### Metrics

```python
metrics = client.get_metrics()
# Metrics(computing={"cpu": ..., "gpu": ..., "ram": ...}, last_report_ts="...")
```

### Sensors

```python
sensors = client.get_sensors()
# SensorData(power={...}, battery={...})

power = client.get_power()
battery = client.get_battery()
```

### Crane control

```python
status = client.get_crane_status()
print(status.status)
print(status.raw)

result = client.send_crane_command("crane_up")
```

Valid commands: `crane_up`, `crane_down`, `crane_stop`.

### Camera streams

```python
streams = client.list_streams()
for stream in streams:
    print(stream.camera_id, stream.rtsp_uri, stream.port)

stream = client.get_stream("camera_front")
```

#### Live microphone audio

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

### Siren

The cambox has two independent outputs: a **light** (`slow` / `fast` / `strobe`) and a **megaphone** (`continue` / `police` / `ambulance`).

```python
status = client.get_siren()
print(status.status, status.error)

client.set_siren_light("strobe", True)
client.set_siren_light("strobe", False)

client.set_siren_megaphone("police", True)
client.set_siren_megaphone("police", False)
```

### Speakers

Play audio through the station host's speakers. Only one playback source is active at a time — starting a new one terminates the previous.

```python
# Upload a complete file
client.play_audio("alert.wav")
client.play_audio(wav_bytes, content_type="audio/wav", filename="alert.wav")

# Stream chunks (HTTP/1.1 chunked upload — for CLI clients)
def chunks():
    with open("long.ogg", "rb") as f:
        while (buf := f.read(8192)):
            yield buf

client.stream_audio(chunks(), content_type="audio/ogg")
```

For browser-based two-way audio, build the page URL — the static HTML reads the JWT from `?token=` and forwards it to the underlying WebSockets:

```python
token = client._token_mgr.get_token()  # or your own JWT
print(client.speakers_mic_page_url(token))     # browser → station mic
print(client.speakers_duplex_page_url(token))  # both directions
```

#### Speaker mic over WebSocket

For programmatic clients, push binary audio frames directly:

```python
import asyncio
from vergil_sdk import SpeakerMicSession

async def main():
    async with SpeakerMicSession(
        "http://192.168.1.10:8080",
        galleon_url="https://galleon.example.com",
        station_id="sta_abc123",
    ) as spk:
        await spk.send(opus_chunk)         # one frame
        await spk.send_stream(chunk_iter)  # async iterable of frames

asyncio.run(main())
```

Any container `decodebin` recognizes works (e.g. `audio/webm;codecs=opus`).

#### Mic stream as webm/opus

`stream_mic()` returns ogg, which Chrome/Edge `MediaSource` can't consume. The webm variant is a per-connection WebSocket:

```python
import asyncio
from vergil_sdk import MicWebmStream

async def main():
    async with MicWebmStream(
        "http://192.168.1.10:8080",
        galleon_url="https://galleon.example.com",
        station_id="sta_abc123",
    ) as mic:
        async for chunk in mic:
            process(chunk)  # audio/webm;codecs=opus bytes

asyncio.run(main())
```

### Frigate events

```python
result = client.list_events(
    camera="front_door",
    label="person",
    start=1700000000.0,
    end=1700003600.0,
    limit=50,
)
for event in result.events:
    print(event.id, event.camera, event.label)

event = client.get_event("1700000000.123456-abc123")
```

### Video

```python
mp4_bytes = client.download_clip("review_abc123")
path = client.download_clip_to("review_abc123", "clip.mp4")

segments = client.list_segments(
    camera="front_door",
    start=1700000000.0,
    end=1700003600.0,
)

mp4_bytes = client.download_video(
    camera="front_door",
    start=1700000000.0,
    end=1700003600.0,
)
path = client.download_video_to(
    camera="front_door",
    start=1700000000.0,
    end=1700003600.0,
    path="recording.mp4",
)
```

### Real-time MQTT (WebSocket)

```python
import asyncio
from vergil_sdk import MQTTSubscription

async def main():
    async with MQTTSubscription(
        "http://192.168.1.10:8082",
        galleon_url="https://galleon.example.com",
        station_id="sta_abc123",
    ) as mqtt:
        ack = await mqtt.subscribe(["telem/#", "sensors/power"])
        async for msg in mqtt:
            print(msg.topic, msg.payload, msg.timestamp)

asyncio.run(main())
```

## Error handling

```python
from vergil_sdk import (
    VergilError, AuthenticationError, BadRequestError,
    NotFoundError, ServerError, ConnectionError,
)

try:
    client.get_event("nonexistent")
except NotFoundError as e:
    print(e, e.status_code)
except AuthenticationError:
    print("Run `vergil login` to sign in again.")
```

| Exception | HTTP status | When |
|---|---|---|
| `AuthenticationError` | 401 | Missing, expired, or invalid token; refresh failed |
| `BadRequestError` | 400 | Invalid parameters |
| `NotFoundError` | 404 | Resource does not exist |
| `ServerError` | 5xx | Internal station error |
| `ConnectionError` | — | Station unreachable |

## Configuration options

`VergilClient` / `AsyncVergilClient` accept:

| Parameter | Type | Default | Description |
|---|---|---|---|
| `base_url` | `str` | *(required)* | Station API URL |
| `galleon_url` | `str` | `None` | Galleon server URL — enables OAuth credential loading |
| `station_id` | `str` | `None` | Target station ID (recommended; auto-discovered from `/health` if omitted) |
| `token` | `str` | `None` | Raw station JWT (alternative to OAuth) |
| `credentials_path` | `Path` | platformdirs default | Override credentials cache file |
| `interactive` | `bool` | `False` | If True, auto-run `vergil login` when cache is missing |
| `timeout` | `float` | `30.0` | Default request timeout in seconds |
