"""
Disk-backed storage for the VEX AIM Benchmark Harness.

Directory layout:
  {base}/
  └── games/
      └── game_001/
          ├── meta.json          {name, created_at}
          ├── prompt.txt
          └── experiments/
              └── exp_001/
                  ├── config.json   {model, mode, name, created_at}
                  ├── start_image.jpg   first image of the game (reused by every attempt)
                  ├── game_prompt.txt   game description frozen at attempt 1
                  └── attempts/
                      └── attempt_001/
                          ├── meta.json    {created_at}
                          ├── input/
                          │   ├── prompt.txt
                          │   └── img_001.jpg ...
                          ├── output.txt   (generated code)
                          └── runs/
                              └── run_001/
                                  ├── frames/   frame_001.jpg ...
                                  ├── telemetry.json         (last sample)
                                  ├── telemetry_series.json  (all samples, t = seconds from start)
                                  ├── console.log
                                  └── summary.json
"""
from __future__ import annotations

import base64
import json
from datetime import datetime
from pathlib import Path


def _now() -> str:
    return datetime.now().isoformat(timespec="seconds")


def _next_id(parent: Path, prefix: str) -> str:
    # max+1 rather than count+1 so a deleted/missing id can't cause a collision
    parent.mkdir(parents=True, exist_ok=True)
    nums = []
    for d in parent.iterdir():
        if d.is_dir() and d.name.startswith(prefix):
            try:
                nums.append(int(d.name[len(prefix):]))
            except ValueError:
                pass
    return f"{prefix}{max(nums, default=0) + 1:03d}"


class Storage:
    def __init__(self, base_dir: str | Path):
        self.base = Path(base_dir)
        (self.base / "games").mkdir(parents=True, exist_ok=True)

    # ── Internal paths ────────────────────────────────────────────── #

    def _game_dir(self, gid: str) -> Path:
        return self.base / "games" / gid

    def _exp_dir(self, gid: str, eid: str) -> Path:
        return self._game_dir(gid) / "experiments" / eid

    def _attempt_dir(self, gid: str, eid: str, aid: str) -> Path:
        return self._exp_dir(gid, eid) / "attempts" / aid

    def _run_dir(self, gid: str, eid: str, aid: str, rid: str) -> Path:
        return self._attempt_dir(gid, eid, aid) / "runs" / rid

    # ── Games ─────────────────────────────────────────────────────── #

    def list_games(self) -> list[dict]:
        d = self.base / "games"
        return [self._read_game(p.name) for p in sorted(d.iterdir()) if p.is_dir()]

    def create_game(self, name: str, prompt: str = "") -> dict:
        gid = _next_id(self.base / "games", "game_")
        gdir = self._game_dir(gid)
        gdir.mkdir(parents=True)
        (gdir / "experiments").mkdir()
        (gdir / "meta.json").write_text(json.dumps({"name": name, "created_at": _now()}, indent=2))
        (gdir / "prompt.txt").write_text(prompt)
        return self._read_game(gid)

    def update_game(self, gid: str, name: str | None = None, prompt: str | None = None) -> dict:
        gdir = self._game_dir(gid)
        if prompt is not None:
            (gdir / "prompt.txt").write_text(prompt)
        if name is not None:
            mf = gdir / "meta.json"
            meta = json.loads(mf.read_text()) if mf.exists() else {}
            meta["name"] = name
            mf.write_text(json.dumps(meta, indent=2))
        return self._read_game(gid)

    def _read_game(self, gid: str) -> dict:
        gdir = self._game_dir(gid)
        mf = gdir / "meta.json"
        meta = json.loads(mf.read_text()) if mf.exists() else {}
        pf = gdir / "prompt.txt"
        ed = gdir / "experiments"
        n_exp = len([d for d in ed.iterdir() if d.is_dir()]) if ed.exists() else 0
        return {
            "id": gid,
            "name": meta.get("name", gid),
            "prompt": pf.read_text() if pf.exists() else "",
            "created_at": meta.get("created_at", ""),
            "n_experiments": n_exp,
        }

    # ── Experiments ───────────────────────────────────────────────── #

    def list_experiments(self, gid: str) -> list[dict]:
        d = self._game_dir(gid) / "experiments"
        if not d.exists():
            return []
        return [self._read_experiment(gid, p.name) for p in sorted(d.iterdir()) if p.is_dir()]

    def create_experiment(self, gid: str, model: str, mode: str = "code",
                          name: str = "") -> dict:
        eid = _next_id(self._game_dir(gid) / "experiments", "exp_")
        eDir = self._exp_dir(gid, eid)
        eDir.mkdir(parents=True)
        (eDir / "attempts").mkdir()
        n = int(eid.split("_")[1])
        cfg = {"model": model, "mode": mode,
               "name": name or f"Experiment {n}", "created_at": _now()}
        (eDir / "config.json").write_text(json.dumps(cfg, indent=2))
        return self._read_experiment(gid, eid)

    def _read_experiment(self, gid: str, eid: str) -> dict:
        eDir = self._exp_dir(gid, eid)
        cf = eDir / "config.json"
        cfg = json.loads(cf.read_text()) if cf.exists() else {}
        ad = eDir / "attempts"
        n_attempts = len([d for d in ad.iterdir() if d.is_dir()]) if ad.exists() else 0
        return {
            "id": eid,
            "game_id": gid,
            "model": cfg.get("model", ""),
            "mode": cfg.get("mode", "code"),
            "name": cfg.get("name", eid),
            "created_at": cfg.get("created_at", ""),
            "n_attempts": n_attempts,
        }

    def save_experiment_start_image(self, gid: str, eid: str, b64: str) -> None:
        (self._exp_dir(gid, eid) / "start_image.jpg").write_bytes(base64.b64decode(b64))

    def get_experiment_start_image(self, gid: str, eid: str) -> str | None:
        """Base64 of the experiment's first game image, or None."""
        f = self._exp_dir(gid, eid) / "start_image.jpg"
        if not f.exists():
            # Experiments created before start_image.jpg existed: attempt 1's first input image
            f = self._attempt_dir(gid, eid, "attempt_001") / "input" / "img_001.jpg"
        return base64.b64encode(f.read_bytes()).decode() if f.exists() else None

    def save_experiment_game_prompt(self, gid: str, eid: str, text: str) -> None:
        (self._exp_dir(gid, eid) / "game_prompt.txt").write_text(text)

    def get_experiment_game_prompt(self, gid: str, eid: str) -> str | None:
        f = self._exp_dir(gid, eid) / "game_prompt.txt"
        return f.read_text() if f.exists() else None

    def save_attempt_reasoning(self, gid: str, eid: str, aid: str, text: str) -> None:
        (self._attempt_dir(gid, eid, aid) / "reasoning.txt").write_text(text)

    def get_attempt_reasoning(self, gid: str, eid: str, aid: str) -> str:
        f = self._attempt_dir(gid, eid, aid) / "reasoning.txt"
        return f.read_text() if f.exists() else ""

    def save_attempt_trace(self, gid: str, eid: str, aid: str, text: str) -> None:
        (self._attempt_dir(gid, eid, aid) / "trace.txt").write_text(text)

    def get_attempt_trace(self, gid: str, eid: str, aid: str) -> str:
        f = self._attempt_dir(gid, eid, aid) / "trace.txt"
        return f.read_text() if f.exists() else ""

    def append_attempt_harness_log(self, gid: str, eid: str, aid: str, text: str) -> None:
        f = self._attempt_dir(gid, eid, aid) / "harness.log"
        with f.open("a") as fp:
            fp.write(text + "\n")

    def get_attempt_harness_log(self, gid: str, eid: str, aid: str) -> str:
        f = self._attempt_dir(gid, eid, aid) / "harness.log"
        return f.read_text() if f.exists() else ""

    # ── Attempts ──────────────────────────────────────────────────── #

    def list_attempts(self, gid: str, eid: str) -> list[dict]:
        d = self._exp_dir(gid, eid) / "attempts"
        if not d.exists():
            return []
        return [self._read_attempt(gid, eid, p.name) for p in sorted(d.iterdir()) if p.is_dir()]

    def create_attempt(self, gid: str, eid: str) -> dict:
        aid = _next_id(self._exp_dir(gid, eid) / "attempts", "attempt_")
        aDir = self._attempt_dir(gid, eid, aid)
        aDir.mkdir(parents=True)
        (aDir / "input").mkdir()
        (aDir / "runs").mkdir()
        (aDir / "meta.json").write_text(json.dumps({"created_at": _now()}, indent=2))
        return self._read_attempt(gid, eid, aid)

    def _read_attempt(self, gid: str, eid: str, aid: str) -> dict:
        aDir = self._attempt_dir(gid, eid, aid)
        mf = aDir / "meta.json"
        meta = json.loads(mf.read_text()) if mf.exists() else {}
        rd = aDir / "runs"
        runs = sorted([d.name for d in rd.iterdir() if d.is_dir()]) if rd.exists() else []
        return {
            "id": aid,
            "exp_id": eid,
            "game_id": gid,
            "created_at": meta.get("created_at", ""),
            "has_code": (aDir / "output.txt").exists(),
            "n_runs": len(runs),
            "runs": runs,
        }

    def save_attempt_input(self, gid: str, eid: str, aid: str,
                           prompt_text: str,
                           images: list[tuple[str, str]]) -> None:
        input_dir = self._attempt_dir(gid, eid, aid) / "input"
        input_dir.mkdir(exist_ok=True)
        # An attempt can be regenerated after a failed LLM call — drop stale images
        for old in input_dir.glob("img_*.*"):
            old.unlink()
        (input_dir / "prompt.txt").write_text(prompt_text)
        for i, (b64, media_type) in enumerate(images):
            ext = "jpg" if "jpeg" in media_type else media_type.split("/")[-1]
            (input_dir / f"img_{i+1:03d}.{ext}").write_bytes(base64.b64decode(b64))

    def save_attempt_code(self, gid: str, eid: str, aid: str, code: str) -> None:
        (self._attempt_dir(gid, eid, aid) / "output.txt").write_text(code)

    def get_attempt_code(self, gid: str, eid: str, aid: str) -> str | None:
        f = self._attempt_dir(gid, eid, aid) / "output.txt"
        return f.read_text() if f.exists() else None

    def get_attempt_input_text(self, gid: str, eid: str, aid: str) -> str:
        f = self._attempt_dir(gid, eid, aid) / "input" / "prompt.txt"
        return f.read_text() if f.exists() else ""

    def get_attempt_input_images(self, gid: str, eid: str, aid: str) -> list[dict]:
        input_dir = self._attempt_dir(gid, eid, aid) / "input"
        if not input_dir.exists():
            return []
        imgs = []
        for p in sorted(input_dir.glob("img_*.*")):
            imgs.append({
                "name": p.name,
                "b64": base64.b64encode(p.read_bytes()).decode(),
                "media_type": "image/jpeg",
            })
        return imgs

    # ── Runs ──────────────────────────────────────────────────────── #

    def create_run(self, gid: str, eid: str, aid: str) -> str:
        rd = self._attempt_dir(gid, eid, aid) / "runs"
        rid = _next_id(rd, "run_")
        (rd / rid / "frames").mkdir(parents=True)
        return rid

    def save_run_results(self, gid: str, eid: str, aid: str, rid: str,
                         frames: list[tuple[str, str]],
                         telemetry_data: dict,
                         console_log: str,
                         summary: dict,
                         telemetry_series: list[dict] | None = None,
                         frame_times: list[float] | None = None) -> None:
        run_dir = self._run_dir(gid, eid, aid, rid)
        frames_dir = run_dir / "frames"
        frames_dir.mkdir(exist_ok=True)
        for i, (b64, mt) in enumerate(frames):
            ext = "jpg" if "jpeg" in mt else mt.split("/")[-1]
            (frames_dir / f"frame_{i+1:03d}.{ext}").write_bytes(base64.b64decode(b64))
        (run_dir / "telemetry.json").write_text(json.dumps(telemetry_data, indent=2))
        if frame_times is not None:
            (run_dir / "frames.json").write_text(json.dumps(frame_times))
        if telemetry_series is not None:
            (run_dir / "telemetry_series.json").write_text(json.dumps(telemetry_series))
        (run_dir / "console.log").write_text(console_log)
        (run_dir / "summary.json").write_text(json.dumps(summary, indent=2))

    def get_run_summary(self, gid: str, eid: str, aid: str, rid: str) -> dict:
        f = self._run_dir(gid, eid, aid, rid) / "summary.json"
        return json.loads(f.read_text()) if f.exists() else {}

    def get_run_console(self, gid: str, eid: str, aid: str, rid: str) -> str:
        f = self._run_dir(gid, eid, aid, rid) / "console.log"
        return f.read_text() if f.exists() else ""

    def get_run_telemetry(self, gid: str, eid: str, aid: str, rid: str) -> dict:
        f = self._run_dir(gid, eid, aid, rid) / "telemetry.json"
        return json.loads(f.read_text()) if f.exists() else {}

    def get_run_telemetry_series(self, gid: str, eid: str, aid: str, rid: str) -> list[dict]:
        f = self._run_dir(gid, eid, aid, rid) / "telemetry_series.json"
        return json.loads(f.read_text()) if f.exists() else []

    def get_run_frames(self, gid: str, eid: str, aid: str, rid: str) -> list[dict]:
        run_dir = self._run_dir(gid, eid, aid, rid)
        fd = run_dir / "frames"
        if not fd.exists():
            return []
        tf = run_dir / "frames.json"
        times = json.loads(tf.read_text()) if tf.exists() else []
        frames = [{"name": p.name,
                   "b64": base64.b64encode(p.read_bytes()).decode()}
                  for p in sorted(fd.glob("frame_*.*"))]
        if len(times) == len(frames):   # older runs have no timestamps
            for f, t in zip(frames, times):
                f["t"] = t
        return frames
