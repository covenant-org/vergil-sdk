"""Exercise every vergil-sdk method and print a summary table.

Prerequisite — run once to cache OAuth credentials:
    vergil login --galleon-url https://galleon.example.com --station-id sta_abc

Usage:
    export VERGIL_STATION_URL="http://<station-ip>:8080"
    export VERGIL_GALLEON_URL="https://galleon.example.com"
    export VERGIL_STATION_ID="sta_abc123"
    python tool_smoke_test.py
"""

import os
from pathlib import Path

from vergil_sdk import VergilClient
from vergil_sdk.exceptions import VergilError

OUTPUT_DIR = Path(os.environ.get("VERGIL_SMOKE_OUTPUT_DIR", "/tmp/vergil-smoke"))


def run(label, fn):
    try:
        result = fn()
        return label, "ok", _summarize(result)
    except VergilError as e:
        return label, "error", f"{type(e).__name__}: {e}"


def _summarize(r):
    if r is None:
        return ""
    if hasattr(r, "count"):
        return f"count={r.count}"
    if hasattr(r, "status"):
        return f"status={r.status}"
    if hasattr(r, "camera_id"):
        return f"camera={r.camera_id}"
    if hasattr(r, "command"):
        return f"command={r.command}"
    if isinstance(r, dict):
        keys = ", ".join(list(r.keys())[:4]) or "empty"
        return f"keys: {keys}"
    if isinstance(r, list):
        return f"len={len(r)}"
    return str(r)[:60]


def _download_row(fn):
    try:
        path = Path(fn())
        size = path.stat().st_size
        return "ok", f"{path} ({size} bytes)"
    except VergilError as e:
        return "error", f"{type(e).__name__}: {e}"


def main():
    OUTPUT_DIR.mkdir(parents=True, exist_ok=True)
    station_url = os.environ["VERGIL_STATION_URL"]
    galleon_url = os.environ["VERGIL_GALLEON_URL"]
    station_id = os.environ["VERGIL_STATION_ID"]

    with VergilClient(
        station_url,
        galleon_url=galleon_url,
        station_id=station_id,
    ) as client:
        rows = [
            run("health", client.health),
            run("get_metrics", client.get_metrics),
            run("get_sensors", client.get_sensors),
            run("get_power", client.get_power),
            run("get_battery", client.get_battery),
            run("get_crane_status", client.get_crane_status),
            run("list_streams", client.list_streams),
        ]

        streams = client.list_streams()
        camera_id = streams[0].camera_id if streams else None

        if camera_id:
            rows.append(
                run("get_stream", lambda: client.get_stream(camera_id)))

        events_list = client.list_events(limit=3)
        rows.append(("list_events", "ok", f"count={events_list.count}"))
        if events_list.events:
            evt_id = events_list.events[0].id
            rows.append(run("get_event", lambda: client.get_event(evt_id)))

        reviews = client.list_review_segments(limit=3)
        rows.append(("list_review_segments", "ok", f"count={reviews.count}"))
        if reviews.reviewsegments:
            rid = reviews.reviewsegments[0].id
            rows.append(run("get_review_segment",
                        lambda: client.get_review_segment(rid)))
            clip_path = OUTPUT_DIR / f"clip-{rid}.mp4"
            rows.append((
                "download_clip_to",
                *_download_row(lambda: client.download_clip_to(rid, str(clip_path))),
            ))

        # Frigate camera (may differ from streams camera_id)
        frigate_cam = events_list.events[0].camera if events_list.events else camera_id
        if frigate_cam and events_list.events:
            start = events_list.events[0].start_time
            end = events_list.events[0].end_time or (start + 3)
            segs = client.list_segments(
                frigate_cam, start=start - 60, end=end + 60)
            rows.append(("list_segments", "ok", f"count={segs.count}"))
            video_path = OUTPUT_DIR / f"video-{frigate_cam}-{int(start)}.mp4"
            rows.append((
                "download_video_to",
                *_download_row(lambda: client.download_video_to(
                    frigate_cam, start, end, str(video_path))),
            ))

        rows.append(run("send_crane_command",
                    lambda: client.send_crane_command("crane_stop")))

    width = max(len(r[0]) for r in rows)
    print(f"{'Tool'.ljust(width)}  Status  Detail")
    print(f"{'-' * width}  ------  ------")
    for name, status, detail in rows:
        print(f"{name.ljust(width)}  {status.ljust(6)}  {detail}")


if __name__ == "__main__":
    main()
