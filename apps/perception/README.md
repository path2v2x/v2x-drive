# Multi-Camera V2X Perception Pipeline

A real-time, multi-camera object detection and localization system. It ingests
video streams from wide-angle cameras, detects objects, projects reviewed image
contacts through a measured camera model and the map georeference, maintains
uncertainty-aware identities, and uploads structured V2X records.

This app is now merged into the V2X backend repo under `apps/perception`. The old
`path2v2x/co-perception` checkout is no longer required for Path PC deployment.

## Run from this repo

```bash
cd apps/perception
python3.10 -m venv .venv
. .venv/bin/activate
pip install --upgrade pip
pip install -r requirements.txt
python process_video.py
```

By default the runner reads these Kinesis Video Streams:

```text
v2x-backend-cam-ch1,v2x-backend-cam-ch2,v2x-backend-cam-ch3,v2x-backend-cam-ch4
```

Useful environment variables:

| Variable | Default | Purpose |
|---|---|---|
| `V2X_PERCEPTION_VIDEO_PATHS` | four Kinesis stream names | Comma-separated stream names or file paths. JSON arrays are also accepted. |
| `V2X_VIDEO_SESSION_API_BASE_URL` | empty | Optional read API base URL for fetching signed Kinesis HLS sessions without direct Kinesis IAM permission. |
| `V2X_PERCEPTION_MODEL_PATH` | `yolov8n.pt` | YOLO model path. Use `best.pt` or an absolute model path on the Path PC if needed. |
| `V2X_PERCEPTION_CONFIDENCE` | `0.5` | Detection confidence threshold. |
| `V2X_PERCEPTION_UPLOAD` | `false` | Set `true` to POST detections into the Objects DB API. |
| `V2X_PERCEPTION_UPLOAD_MIN_INTERVAL_SEC` | `1.0` | Minimum seconds between detection batch uploads when upload is enabled. |
| `V2X_DETECTIONS_ENDPOINT` | production detections API | POST target for uploaded detections. |
| `V2X_PERCEPTION_SHOW_LIVE` | `false` | Opens the OpenCV annotated preview window when running interactively. |
| `V2X_PERCEPTION_STREAM_PORT` | empty | When set, publishes per-camera MJPEG streams on this port. |
| `V2X_PERCEPTION_STREAM_HOST` | `0.0.0.0` | Bind host for the MJPEG stream server. |
| `V2X_PERCEPTION_STALE_SECONDS` | `15` | Maximum source-frame age before `/health` marks a camera stale and the service degraded. |
| `V2X_PERCEPTION_RECONNECT_INITIAL_SEC` | `1` | Initial HLS reconnect delay after a failed open/read. |
| `V2X_PERCEPTION_RECONNECT_MAX_SEC` | `30` | Maximum HLS reconnect delay; failed cameras remain retryable indefinitely. |
| `V2X_PERCEPTION_OPEN_TIMEOUT_MS` | `10000` | OpenCV/FFmpeg live-stream open timeout. |
| `V2X_PERCEPTION_READ_TIMEOUT_MS` | `10000` | OpenCV/FFmpeg live-stream read timeout. |
| `V2X_PERCEPTION_FRAME_IDENTITY_HISTORY` | `256` | Bounded recent-frame identity window retained across HLS reconnects. |
| `V2X_PERCEPTION_DUPLICATE_FRAME_LIMIT` | `90` | Consecutive replayed/repeated frames before forcing a renewed HLS session. |
| `V2X_PERCEPTION_OUTPUT_JSON` | empty | Optional path for writing detection records. |
| `V2X_PERCEPTION_OUTPUT_VIDEO` | empty | Optional path for writing an annotated video file. |
| `V2X_PERCEPTION_OUTPUT_IMAGE` | empty | Optional path for writing the latest annotated image. |

Path PC systemd deployment uses `scripts/launch-perception.sh` and
`scripts/systemd/v2x-perception.service` from the repo root.

When `V2X_PERCEPTION_STREAM_PORT=8090`, the live annotated streams are:

```text
http://<host>:8090/streams/ch1.mjpg
http://<host>:8090/streams/ch2.mjpg
http://<host>:8090/streams/ch3.mjpg
http://<host>:8090/streams/ch4.mjpg
```

`GET /health` reports service readiness plus per-camera source timestamps,
frame age, frame count, reconnect state, and the last read/open error. A cached
frame is never republished as fresh. Production acceptance requires schema-v2
event time derived from HLS `EXT-X-PROGRAM-DATE-TIME`; wall-clock fallback is
untrusted and cannot support historical replay or calibration.

Each live camera is read by an isolated worker, so an FFmpeg open/read timeout
on one feed cannot block the other feeds. Kinesis stream names are resolved
again on every reconnect, producing a fresh signed session URL. A literal
`http://` or `https://` input has no resolver and is therefore retried as-is;
use stream names plus `V2X_VIDEO_SESSION_API_BASE_URL` for renewable sessions.
Recent sparse content identities survive reconnects, so replayed terminal HLS
frames do not advance freshness or event time. Persistent repeats force another
bounded-backoff reconnect. Source exceptions are sanitized before logging or
exposure in `/health`; signed URLs and query strings are never included.

Batch upload success requires both an HTTP success status and complete
item-level acceptance (`ok=true`, `failed=0`, and one successful result per
item). A partial ingest response is reported as a failed upload, while the
attempt still consumes the configured rate-limit interval to avoid retry storms.

Use the dependency-light Phase 3 gate against both the local origin and the
candidate public tunnel. It samples health and detection summaries twice,
requires advancing/fresh ch1-ch4 capture and event timestamps, parses two
complete JPEG frames per MJPEG stream, and rejects identical frame hashes:

```bash
python tools/verify_live_feeds.py http://127.0.0.1:8090
python tools/verify_live_feeds.py https://<candidate-perception-host>
```

The verifier samples raw capture timestamps three seconds apart because the
measured live fragment cadence is about 2.002 seconds. It separately polls each
raw capture counter for at most ten seconds and each camera's explicit
inference counter for at most ten seconds, so fragment-boundary jitter or a normal
two-worker inference phase is not mistaken for a stalled camera. Health fails
closed when any inference result is more than ten seconds old. The verifier
still requires two distinct complete JPEGs per camera, the unchanged 15-second
capture/event maximum age, trusted matched media clocks, and the unchanged
-1,000/+10,000 ms decode-latency bounds.

The verifier refuses base URLs or stream templates containing credentials,
queries, or fragments and prints only per-camera timestamps and SHA-256 hashes.

Cross-camera vehicle association is fail-closed by default. Camera-local tracker
IDs remain stable within a feed, but detections from different cameras (or a
new local track on the same camera) receive distinct global IDs until an
independently validated vehicle-ReID/geometric linker is deployed. Set
`V2X_PERCEPTION_CROSS_CAMERA_VEHICLE_ASSOCIATION=true` only inside an explicit
candidate gate; the legacy 30 m/40 s spatial heuristic is not acceptance-grade.

## Acceptance-grade calibration and mapping

The later `pitch_yaw_minimize.py`, flat-earth diagrams, and legacy CSV examples
in this README document the imported co-perception prototype only. They are not
an acceptance or deployment workflow: they share nominal intrinsics, assume
zero distortion and 7 m height, fit only pitch/yaw, and do not provide an
independent holdout.

Use the fail-closed offline chain instead:

1. `tools/export_detection_corpus.py` freezes a sanitized, paginated, hash-bound
   API window and reconciles it with the timeline count.
2. `tools/build_detection_observation_ledger.py` retains pixels and provenance
   while quarantining stored GPS/local-XZ as a derived baseline.
3. `tools/verify_historical_correlation.py` reconstructs the exact archived HLS
   frame for each trusted event.
4. `tools/apply_ground_contact_reviews.py` accepts only named-human wheel/road
   contacts bound to that retained frame and verifier report.
5. `tools/propose_detection_tracklets.py`, `tools/apply_tracklet_reviews.py`,
   and `tools/freeze_track_split.py` create reviewed whole-object tracklets and
   an immutable later-day holdout without promoting model identity to truth.
6. `tools/fit_detection_factor_graph.py` enforces per-camera excitation and
   split-leakage gates, then optionally runs the bounded diagnostic trajectory
   fit around a surveyed static solution and surveyed lane map.

The diagnostic fit never edits camera configuration and always reports
`acceptance_eligible=false`. Production still requires measured per-camera
intrinsics/distortion, surveyed 6-DoF/static/lane evidence, locked held-out
reprojection, whole-track bootstrap, RTK same-car truth, and all-four-camera UE5
visual proof. Stored GPS, current actor positions, and lane-snapped locations
must never be used as optimizer labels.

---

## Table of Contents

- [Architecture Overview](#architecture-overview)
- [Repository Structure](#repository-structure)
- [Setup](#setup)
- [Camera Calibration](#camera-calibration)
- [Running the Pipeline](#running-the-pipeline)

---

## Architecture Overview

The system is composed of three layers that work sequentially: calibration, detection/projection, and aggregation/upload.

```
┌─────────────────────────────────────────────────────────────────┐
│                        CALIBRATION LAYER                        │
│                                                                 │
│  validate.py  ──────►  pitch_yaw_minimize.py                    │
│  (extracts u,v          (scipy Nelder-Mead minimization         │
│   pixel coords          finds optimal pitch & yaw angles        │
│   from reference        by minimizing avg Euclidean error       │
│   images/frames)        across all calibration points)          │
│                                  │                              │
│                                  ▼                              │
│                     calibration_errors.csv                      │
│                     (optimal pitch, yaw per camera)             │
└──────────────────────────────┬──────────────────────────────────┘
                               │ pitch_deg, yaw_deg
                               ▼
┌─────────────────────────────────────────────────────────────────┐
│                       DETECTION LAYER                           │
│                   VideoObjectDetector                           │
│                     (process_video.py)                          │
│                                                                 │
│  Video Frame                                                    │
│      │                                                          │
│      ▼                                                          │
│  YOLOv8 Detection  ──►  Bounding Box  ──►  Bottom-Center (u,v) │
│                                                   │             │
│                         Intrinsic Matrix (K)      │             │
│                         + Distortion Coeffs       │             │
│                                   │               │             │
│                                   ▼               ▼             │
│                         Undistort pixel  ──►  Camera Ray        │
│                                                   │             │
│                         Pitch/Yaw Rotation (Rx·Ry)│             │
│                                                   ▼             │
│                                          World Ray (dx,dy,dz)   │
│                                                   │             │
│                         Ground Plane Intersection (t = H / dy)  │
│                                                   │             │
│                                                   ▼             │
│                                         Local (X, Z) in meters  │
│                                                   │             │
│                         Heading rotation + flat-earth approx    │
│                                                   │             │
│                                                   ▼             │
│                                         GPS (Latitude, Longitude)│
└──────────────────────────────┬──────────────────────────────────┘
                               │ Per-camera detections
                               ▼
┌─────────────────────────────────────────────────────────────────┐
│                      AGGREGATION LAYER                          │
│                    MultiCameraPipeline                          │
│                     (process_video.py)                          │
│                                                                 │
│  Camera 1 detections ──┐                                        │
│  Camera 2 detections ──┼──► Haversine deduplication            │
│  Camera N detections ──┘    (merge if distance < threshold)     │
│                                        │                        │
│                                        ▼                        │
│                             Temporal track assignment           │
│                             (consistent global IDs)             │
│                                        │                        │
│                                        ▼                        │
│                              V2X API Upload / JSON output        │
└─────────────────────────────────────────────────────────────────┘
```

### How the pieces fit together

**`calibration_flow.md`** documents the full mathematical model — the intrinsic matrix $K$, pixel-to-ray projection, rotation matrices, ground plane intersection, and the cost function being minimized. Read this first to understand the geometry.

**`pitch_yaw_minimize.py`** implements the calibration optimizer. You populate its `calibration_points` list with `(u, v)` pixel coordinates paired with their known real-world `(X, Z)` positions (measured on the ground relative to the camera pole). The script runs a Nelder-Mead minimization to find the pitch and yaw angles that minimize average reprojection error across all points, then writes `calibration_errors.csv`.

**`validate.py`** is the data-extraction companion to the optimizer. It spins up a `MultiCameraPipeline` against static images or video frames from a specific camera view and runs detection, letting you visually confirm that detected pixel coordinates correspond to ground-truth positions before feeding them into the minimizer.

**`process_video.py`** is the production runtime. It contains two classes:
- `VideoObjectDetector` — wraps a single camera stream. Runs YOLOv8, applies the calibrated pitch/yaw, and produces GPS-tagged detection records.
- `MultiCameraPipeline` — orchestrates multiple detectors, synchronizes frames, deduplicates overlapping detections using Haversine distance, maintains global track IDs across frames, and handles upload to the V2X API.

---

## Repository Structure

```
.
├── process_video.py          # Core pipeline: VideoObjectDetector + MultiCameraPipeline
├── calibration
    ├── validate.py               # Calibration validation runner
    └── pitch_yaw_minimize.py     # Camera angle optimizer (scipy Nelder-Mead)
├── requirements.txt          # Python dependencies
├── docs
    ├── calibration_flow.md       # Mathematical reference for the calibration model
    └── video_pipeline.md         # Mathematical reference for the full V2X pipeline
└── camera_views/
    └── ch1/
        └── center/           # Reference images/frames used by validate.py
```

---

## Setup

### 1. Install Conda

If you don't have Conda installed, download [Miniconda](https://docs.conda.io/en/latest/miniconda.html) and follow the installer instructions for your OS.

### 2. Create the environment

```bash
conda create -n v2x-pipeline python=3.10 -y
conda activate v2x-pipeline
```

### 3. Install dependencies

```bash
pip install -r requirements.txt
```

> **Note:** YOLOv8 (`ultralytics`) will automatically download model weights (e.g., `yolov8n.pt`) on first use if they are not already present locally. Ensure you have an internet connection on first run.

### 4. (Optional) GPU support

If you have a CUDA-capable GPU, install the matching PyTorch build before installing the rest of the requirements:

```bash
# Example for CUDA 11.8 — adjust the index URL for your CUDA version
pip install torch torchvision --index-url https://download.pytorch.org/whl/cu118
pip install -r requirements.txt
```

---

## Camera Calibration

Calibration must be completed before running the production pipeline. The goal is to determine the precise **pitch** and **yaw** angle of each physical camera.

### Step 1 — Collect reference frames

Place representative images or short video clips from a camera view into the appropriate `camera_views/` subdirectory (e.g., `camera_views/ch1/center/`).

### Step 2 — Extract pixel coordinates with `validate.py`

Run `validate.py` to open a live detection window over your reference frames. Note the `(u, v)` pixel coordinates of objects whose real-world `(X, Z)` positions (in meters, relative to the camera pole) you have measured on the ground.

```bash
python -m calibration.validate
```

Update the `VideoObjectDetector` constructor arguments in `validate.py` to match your camera's known parameters:

| Parameter | Description |
|---|---|
| `K` | Intrinsic matrix (focal length, principal point) |
| `camera_height` | Height of camera above ground in meters |
| `pitch_deg` / `yaw_deg` | Initial angle estimates (refined by optimizer) |
| `heading_deg` | True compass heading of the camera |
| `origin_lat` / `origin_lon` | GPS coordinates of the camera pole |

### Step 3 — Run the angle optimizer

Add the `(u, v, true_X, true_Z)` pairs collected in Step 2 to the `calibration_points` list in `pitch_yaw_minimize.py`, then run:

```bash
python calibration/pitch_yaw_minimize.py
```

The script will print the optimal pitch and yaw angles and write a `calibration_errors.csv` showing per-point reprojection errors. Aim for an average error below ~0.5 meters for reliable GPS output.

```
✅ MULTI-POINT CALIBRATION COMPLETE
----------------------------------------
Optimal Pitch: -XX.XX degrees
Optimal Yaw:   -XX.XX degrees
Average Error: X.XX meters per point
----------------------------------------
```

### Step 4 — Update production parameters

Copy the optimal `pitch_deg` and `yaw_deg` values into the corresponding `VideoObjectDetector` constructor call in `process_video.py`.

---

## Running the Pipeline

Once calibration is complete, run the main pipeline against your live or recorded video streams:

```python
from process_video import MultiCameraPipeline, VideoObjectDetector
import numpy as np

K = np.array([
    [1325.4,      0, 1280.0],
    [     0, 1325.4,  960.0],
    [     0,      0,      1]
], dtype=np.float64)

base_lat = 37.91560117034595
base_lon = -122.33478756387032

cam1 = VideoObjectDetector(
    model_path='yolov8n.pt',
    conf=0.3,
    K=K,
    dist_coeffs=None,
    camera_height=7.0,
    pitch_deg=-103.63,   # <-- from calibration
    yaw_deg=-166.80,     # <-- from calibration
    heading_deg=200.0,
    device_id="cam-001-ch1",
    origin_lat=base_lat,
    origin_lon=base_lon,
    city="Richmond",
    state="CA",
    country="USA"
)

pipeline = MultiCameraPipeline(detectors=[cam1])

pipeline.process_streams(
    video_paths=["path/to/stream1.mp4"],
    show_live=True,
    upload=False,          # Set True to push to V2X API
    output_json="output.json",
    output_video="output.mp4",
    output_image=None,
    output_validate=False
)
```

### `process_streams` parameters

| Parameter | Type | Description |
|---|---|---|
| `video_paths` | `list[str]` | One path per camera stream, in the same order as `detectors` |
| `show_live` | `bool` | Display annotated frames in a live OpenCV window |
| `upload` | `bool` | Upload detection records to the V2X API |
| `output_json` | `str \| None` | Path to write all detections as JSON |
| `output_video` | `str \| None` | Path to write annotated output video |
| `output_image` | `str \| None` | Path to write a single annotated frame |
| `output_validate` | `bool` | Print per-frame detection details for debugging |

### Adding more cameras

Instantiate one `VideoObjectDetector` per camera with its own calibrated parameters, then pass all detectors to `MultiCameraPipeline`. Overlapping detections between cameras are automatically merged using Haversine distance thresholding.

```python
pipeline = MultiCameraPipeline(detectors=[cam1, cam2, cam3])
pipeline.process_streams(video_paths=["ch1.mp4", "ch2.mp4", "ch3.mp4"], ...)
```
