"""FastAPI server for VEX AIM Benchmark Harness."""
from __future__ import annotations

import asyncio
import json
import os
import sys
from pathlib import Path

from fastapi import FastAPI, WebSocket, WebSocketDisconnect, Request
from fastapi.responses import HTMLResponse, StreamingResponse, JSONResponse
from fastapi.staticfiles import StaticFiles

STATIC_DIR = Path(__file__).parent / "static"
app = FastAPI(title="VEX AIM Benchmark Harness")
app.mount("/static", StaticFiles(directory=str(STATIC_DIR)), name="static")


class ConnectionManager:
    def __init__(self):
        self._active: list[WebSocket] = []

    async def connect(self, ws: WebSocket) -> None:
        await ws.accept()
        self._active.append(ws)

    def disconnect(self, ws: WebSocket) -> None:
        try: self._active.remove(ws)
        except ValueError: pass

    async def broadcast(self, data: dict) -> None:
        text = json.dumps(data)
        dead = []
        for ws in list(self._active):
            try: await ws.send_text(text)
            except Exception: dead.append(ws)
        for ws in dead: self.disconnect(ws)


manager = ConnectionManager()
harness = None
storage = None


@app.on_event("startup")
async def _startup() -> None:
    global harness, storage
    import config as cfg_module
    from harness import Harness
    from storage import Storage

    cfg = cfg_module.load()
    storage = Storage(cfg["paths"]["sessions_dir"])
    harness = Harness(cfg, storage)
    harness.broadcast = manager.broadcast
    asyncio.create_task(harness.initialize())


@app.on_event("shutdown")
async def _shutdown() -> None:
    # Release the camera so a restarted server can open it (a lingering process
    # holding it made the new server's camera fail).
    if harness is not None:
        harness.camera.close()


@app.get("/", response_class=HTMLResponse)
async def index() -> HTMLResponse:
    return HTMLResponse((STATIC_DIR / "index.html").read_text(encoding="utf-8"))


@app.websocket("/ws")
async def ws_endpoint(ws: WebSocket) -> None:
    await manager.connect(ws)
    try:
        while True:
            await ws.receive_text()
    except (WebSocketDisconnect, Exception):
        manager.disconnect(ws)


# ── Config ──────────────────────────────────────────────────────────

@app.get("/api/config")
async def api_get_config() -> dict:
    return {
        "robot_ip": harness._robot_ip,
        "robot_connected": harness.robot_connected,
        "robot_port": harness.robot_port,
        "models": harness.model_names,
        # Retired models included so their old experiments are still labeled correctly
        "model_ids": {**harness.cfg.get("retired_models", {}),
                      **{n: m.model_id for n, m in harness._models.items()}},
        "sessions_dir": str(storage.base),
        "status": harness.status,
        "busy": harness._busy,
        "live": harness.live,   # ids of the run/drive in progress, if any
        "trials_per_code": int(harness.cfg["benchmark"].get("trials_per_code", 3)),
    }

@app.post("/api/config/robot_ip")
async def api_set_robot_ip(request: Request) -> dict:
    body = await request.json()
    harness.set_robot_ip(body.get("robot_ip", ""))
    return {"ok": True}

@app.post("/api/config/sessions_dir")
async def api_set_sessions_dir(request: Request) -> dict:
    global storage
    from storage import Storage
    if harness.busy_reason():   # swapping storage mid-run would lose the trial
        return {"ok": False, "error": harness.busy_reason()}
    body = await request.json()
    d = body.get("sessions_dir", "").strip()
    if not d:
        return {"ok": False, "error": "empty path"}
    storage = Storage(d)
    harness.storage = storage
    return {"ok": True, "sessions_dir": str(storage.base)}

@app.post("/api/server/restart")
async def api_server_restart() -> dict:
    """Replace this process with a fresh copy of itself (same command line and log).
    Reconnects a camera that died with Mac sleep / USB drop — this process can't — and
    unlike stop + start it can't leave a stuck old process behind holding the camera."""
    if harness.busy_reason():
        return {"ok": False, "error": harness.busy_reason()}

    def _reexec():
        sys.stdout.flush(); sys.stderr.flush()
        # The listening socket isn't inheritable, so the new process can bind the port;
        # the camera and any threads stuck on it are released with the old process image.
        os.execv(sys.executable, [sys.executable, "-m", "uvicorn", *sys.argv[1:]])

    asyncio.get_running_loop().call_later(0.5, _reexec)   # let this response go out first
    return {"ok": True}


@app.post("/api/robot/connect")
async def api_robot_connect() -> dict:
    ok = await harness.reconnect_robot()
    return {"ok": ok, "connected": harness.robot_connected, "port": harness.robot_port}


# ── Camera ──────────────────────────────────────────────────────────

@app.get("/api/camera/stream")
async def camera_stream() -> StreamingResponse:
    async def _gen():
        loop = asyncio.get_running_loop()
        while True:
            # cv2 reads block — run them off the event loop
            frame = await loop.run_in_executor(None, harness.camera.get_latest_jpeg_bytes)
            if frame:
                yield b"--frame\r\nContent-Type: image/jpeg\r\n\r\n" + frame + b"\r\n"
            await asyncio.sleep(0.15)
    return StreamingResponse(_gen(), media_type="multipart/x-mixed-replace; boundary=frame")

@app.get("/api/camera/scan")
async def api_camera_scan() -> list:
    if harness.busy_reason():
        # Probing other devices mid-run can disrupt the live capture
        idx = harness.camera._idx
        return [] if idx is None else [{"index": idx, "label": f"Camera {idx} (in use)", "in_use": True}]
    loop = asyncio.get_event_loop()
    return await loop.run_in_executor(None, harness.scan_cameras)

@app.post("/api/camera/select")
async def api_camera_select(request: Request) -> dict:
    if harness.busy_reason():
        return {"ok": False, "error": harness.busy_reason()}
    body = await request.json()
    loop = asyncio.get_running_loop()
    if not await loop.run_in_executor(None, harness.set_camera_index, int(body.get("index", 0))):
        return {"ok": False, "error": "That camera isn't the overhead camera (needs "
                f"{harness.camera._req_w}×{harness.camera._req_h}) — see the System Log."}
    return {"ok": True}


# ── Games ────────────────────────────────────────────────────────────

@app.get("/api/games")
async def api_list_games() -> list:
    return storage.list_games()

@app.post("/api/games")
async def api_create_game(request: Request) -> dict:
    body = await request.json()
    return storage.create_game(body.get("name", "New Game"), body.get("prompt", ""))

@app.put("/api/games/{game_id}")
async def api_update_game(game_id: str, request: Request) -> dict:
    body = await request.json()
    return storage.update_game(game_id, name=body.get("name"), prompt=body.get("prompt"))

@app.get("/api/games/{game_id}")
async def api_get_game(game_id: str) -> dict:
    return storage._read_game(game_id)


# ── Experiments ──────────────────────────────────────────────────────

@app.get("/api/games/{game_id}/experiments")
async def api_list_experiments(game_id: str) -> list:
    return storage.list_experiments(game_id)

@app.post("/api/games/{game_id}/experiments")
async def api_create_experiment(game_id: str, request: Request):
    body = await request.json()
    if not (storage.base / "games" / game_id).is_dir():
        return JSONResponse({"ok": False, "error": f"No such game: {game_id}"}, status_code=404)
    if body.get("model") not in harness.model_names:
        return JSONResponse({"ok": False, "error": f"Unknown model: {body.get('model')}"}, status_code=400)
    if body.get("mode", "code") not in ("code", "drive"):
        return JSONResponse({"ok": False, "error": "mode must be 'code' or 'drive'"}, status_code=400)
    return storage.create_experiment(
        game_id, body.get("model", ""), body.get("mode", "code"),
        body.get("name", ""))

@app.get("/api/games/{game_id}/experiments/{exp_id}")
async def api_get_experiment(game_id: str, exp_id: str) -> dict:
    return storage._read_experiment(game_id, exp_id)


# ── Attempts ─────────────────────────────────────────────────────────

@app.get("/api/games/{game_id}/experiments/{exp_id}/attempts")
async def api_list_attempts(game_id: str, exp_id: str) -> list:
    return storage.list_attempts(game_id, exp_id)

@app.post("/api/games/{game_id}/experiments/{exp_id}/attempts/generate")
async def api_generate_attempt(game_id: str, exp_id: str) -> dict:
    err = harness.generate_blocker(game_id, exp_id)
    if err:
        return {"ok": False, "error": err}
    asyncio.create_task(harness.generate_attempt(game_id, exp_id))
    return {"ok": True}

@app.get("/api/games/{game_id}/experiments/{exp_id}/attempts/{attempt_id}/code")
async def api_get_code(game_id: str, exp_id: str, attempt_id: str) -> dict:
    return {"code": storage.get_attempt_code(game_id, exp_id, attempt_id) or ""}

@app.get("/api/games/{game_id}/experiments/{exp_id}/attempts/{attempt_id}/reasoning")
async def api_get_reasoning(game_id: str, exp_id: str, attempt_id: str) -> dict:
    return {"reasoning": storage.get_attempt_reasoning(game_id, exp_id, attempt_id)}

@app.get("/api/games/{game_id}/experiments/{exp_id}/attempts/{attempt_id}/harness_log")
async def api_get_harness_log(game_id: str, exp_id: str, attempt_id: str) -> dict:
    return {"text": storage.get_attempt_harness_log(game_id, exp_id, attempt_id)}

@app.get("/api/games/{game_id}/experiments/{exp_id}/attempts/{attempt_id}/trace")
async def api_get_trace(game_id: str, exp_id: str, attempt_id: str) -> dict:
    return {"text": storage.get_attempt_trace(game_id, exp_id, attempt_id)}

@app.get("/api/games/{game_id}/experiments/{exp_id}/attempts/{attempt_id}/input")
async def api_get_input(game_id: str, exp_id: str, attempt_id: str) -> dict:
    return {
        "prompt_text": storage.get_attempt_input_text(game_id, exp_id, attempt_id),
        "images": storage.get_attempt_input_images(game_id, exp_id, attempt_id),
    }


# ── Runs ─────────────────────────────────────────────────────────────

@app.post("/api/games/{game_id}/experiments/{exp_id}/attempts/{attempt_id}/run")
async def api_start_run(game_id: str, exp_id: str, attempt_id: str) -> dict:
    err = harness.run_blocker(game_id, exp_id, attempt_id)
    if err:
        return {"ok": False, "error": err}
    asyncio.create_task(harness.run_attempt(game_id, exp_id, attempt_id))
    return {"ok": True}

@app.post("/api/run/stop")
async def api_stop_run() -> dict:
    asyncio.create_task(harness.emergency_stop())
    return {"ok": True}

@app.get("/api/games/{game_id}/experiments/{exp_id}/attempts/{attempt_id}/runs/{run_id}/console")
async def api_get_console(game_id: str, exp_id: str, attempt_id: str, run_id: str) -> dict:
    return {"text": storage.get_run_console(game_id, exp_id, attempt_id, run_id)}

@app.get("/api/games/{game_id}/experiments/{exp_id}/attempts/{attempt_id}/runs/{run_id}/frames")
async def api_get_frames(game_id: str, exp_id: str, attempt_id: str, run_id: str) -> list:
    return storage.get_run_frames(game_id, exp_id, attempt_id, run_id)

@app.get("/api/games/{game_id}/experiments/{exp_id}/attempts/{attempt_id}/runs/{run_id}/telemetry")
async def api_get_telemetry(game_id: str, exp_id: str, attempt_id: str, run_id: str) -> dict:
    return storage.get_run_telemetry(game_id, exp_id, attempt_id, run_id)

@app.get("/api/games/{game_id}/experiments/{exp_id}/attempts/{attempt_id}/runs/{run_id}/summary")
async def api_get_summary(game_id: str, exp_id: str, attempt_id: str, run_id: str) -> dict:
    return storage.get_run_summary(game_id, exp_id, attempt_id, run_id)


# ── Drive mode ───────────────────────────────────────────────────────

@app.post("/api/drive/start")
async def api_drive_start(request: Request) -> dict:
    if harness.busy_reason():
        return {"ok": False, "error": harness.busy_reason()}
    body = await request.json()
    try:
        max_turns = int(body.get("max_turns", 100))
        assert 1 <= max_turns <= 1000
    except (ValueError, TypeError, AssertionError):
        return {"ok": False, "error": "Max turns must be a whole number between 1 and 1000."}
    if not (storage.base / "games" / str(body.get("game_id")) / "experiments" / str(body.get("exp_id"))).is_dir():
        return {"ok": False, "error": "No such experiment."}
    asyncio.create_task(harness.start_drive_session(
        body["game_id"], body["exp_id"], max_turns=max_turns))
    return {"ok": True}
