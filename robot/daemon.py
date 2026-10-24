"""
Persistent robot daemon — one process, one WebSocket connection.

Reads JSON commands from stdin (one per line), writes JSON results to stdout.
Stays alive so the vex library's WebSocket connection is maintained between
commands (fixes the stale-status problem when reconnecting each time).

Usage:
    python3 robot/daemon.py <robot_ip>

Protocol:
    stdin:  {"cmd": "move", "distance_mm": 200, "angle_deg": 0, "velocity": 55}
    stdout: {"done": true, "distance_mm": 200}

    stdin:  {"cmd": "status"}
    stdout: {"heading": 36.3, "position": {...}, "battery": 27, "has_ball": false}
"""
import os
import sys
import json
import time
import base64
from pathlib import Path

# stdout carries the JSON protocol, but the vex library print()s banners and
# warnings ("Welcome to the AIM Websocket Python Client…", "ws_status reconnecting")
# that would be read as replies. Keep a private handle on the real stdout for the
# protocol and point fd 1 (and so every print) at stderr.
_PROTO = os.fdopen(os.dup(1), "w", buffering=1)
os.dup2(2, 1)

BENCH_DIR = Path(__file__).parent.parent
sys.path.insert(0, str(BENCH_DIR))


def _make_robot(robot_ip: str):
    settings = BENCH_DIR / "vex" / "settings.json"
    settings.write_text(json.dumps({"connection": {"host": robot_ip}}, indent=4))
    from vex import Robot, AiVision, Tagdesc, KickType, TurnType  # noqa: F401
    robot = Robot()
    time.sleep(0.5)
    robot.vision.model_detection(True)
    robot.vision.tag_detection(True)
    return robot


def _execute(cmd: dict, robot) -> dict:
    from vex import AiVision, Tagdesc, KickType, TurnType
    import cv2

    name = cmd.get("cmd", "")
    time.sleep(0.3)   # let ws_status refresh after movement settles

    if name == "status":
        s = robot.status
        rob = s["robot"]
        return {
            "heading":   rob["heading"],
            "position":  {"x": rob["robot_x"], "y": rob["robot_y"]},
            "battery":   rob["battery"],
            "has_ball":  robot.has_sports_ball(),
            "has_barrel": robot.has_any_barrel(),
        }

    if name == "scan":
        ai_objs = list(robot.vision.get_data(AiVision.ALL_AIOBJS))
        tags = []
        for o in robot.vision.get_data(AiVision.ALL_TAGS, 24):   # every tag id, not just 1-6
            tags.append({
                "tag_id":  o.id,
                "width":   o.width,
                "height":  o.height,
                "centerX": o.centerX,
                "centerY": o.centerY,
            })
        return {
            "ai_objects": [
                {"class": o.classname, "width": o.width, "height": o.height, "centerX": o.centerX}
                for o in ai_objs
            ],
            "tags": tags,
            "note": "screen 320px wide; center=160px. cx<160 means left, cx>160 right. cy<120 means far/high.",
        }

    if name == "move":
        dist  = float(cmd.get("distance_mm", 200))
        angle = float(cmd.get("angle_deg",   0))
        vel   = float(cmd.get("velocity",    55))
        robot.move_for(dist, angle, vel)
        return {"done": True, "distance_mm": dist, "angle_deg": angle}

    if name == "turn":
        direction = TurnType.RIGHT if cmd.get("direction", "right") == "right" else TurnType.LEFT
        angle = float(cmd.get("angle_deg", 45))
        vel   = float(cmd.get("velocity",  45))
        robot.turn_for(direction, angle, vel)
        return {"done": True}

    if name == "kick":
        kt_map = {"soft": KickType.SOFT, "medium": KickType.MEDIUM, "hard": KickType.HARD}
        kt = kt_map.get(str(cmd.get("strength", "hard")).lower(), KickType.HARD)
        robot.kicker.kick(kt)
        return {"done": True}

    if name == "place":
        robot.kicker.place()
        return {"done": True}

    if name == "stop":
        robot.stop_all_movement()
        return {"done": True}

    if name == "has_ball":
        return {"has_ball": robot.has_sports_ball()}

    if name == "photo":
        cam_idx = int(cmd.get("camera_index", 0))
        cap = cv2.VideoCapture(cam_idx)
        for _ in range(15):
            cap.read()
        ret, frame = cap.read()
        cap.release()
        if ret:
            _, buf = cv2.imencode(".jpg", frame, [cv2.IMWRITE_JPEG_QUALITY, 75])
            b64 = base64.b64encode(buf.tobytes()).decode()
            return {"photo_b64": b64, "width": frame.shape[1], "height": frame.shape[0]}
        return {"error": "camera capture failed"}

    return {"error": f"unknown command: {name}"}


if __name__ == "__main__":
    robot_ip = sys.argv[1] if len(sys.argv) > 1 else "192.168.5.100"
    try:
        robot = _make_robot(robot_ip)
        _PROTO.write(json.dumps({"ready": True}) + "\n")
        _PROTO.flush()
    except Exception as exc:
        _PROTO.write(json.dumps({"error": str(exc)}) + "\n")
        _PROTO.flush()
        sys.exit(1)

    for raw in sys.stdin:
        raw = raw.strip()
        if not raw:
            continue
        try:
            cmd = json.loads(raw)
            result = _execute(cmd, robot)
        except Exception as exc:
            result = {"error": str(exc)}
        _PROTO.write(json.dumps(result) + "\n")
        _PROTO.flush()
