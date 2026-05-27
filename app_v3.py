import json
import logging
import os
import threading
from pathlib import Path
from queue import Queue, Empty

from dotenv import load_dotenv
from flask import Flask, Response, jsonify, request, send_file, stream_with_context

import pipeline_v3
from tools.reactive_registrar import get_stats as _get_reactive_stats, get_recent_insights

load_dotenv()

logging.basicConfig(level=logging.INFO)

app = Flask(__name__, static_folder="static")

ORCHESTRATOR_V3 = os.getenv("ORCHESTRATOR_V3_ADDRESS", "")
FRONTEND_DIR    = Path("output/v3")

_watchers: dict = {}   # job_id → {"queue": Queue, "thread": Thread}


# ── Static files ──────────────────────────────────────────────────────────────
@app.route("/")
def index():
    return send_file("static/v3.html")


@app.route("/v3/frontend/<int:job_id>")
def serve_frontend(job_id: int):
    path = FRONTEND_DIR / f"frontend_{job_id}.html"
    if not path.exists():
        return "Frontend not generated yet for this job.", 404
    if request.args.get("dl") == "1":
        return send_file(path, mimetype="text/html", as_attachment=True,
                         download_name="dapp-frontend.html")
    return send_file(path, mimetype="text/html")


# ── Platform stats & insights ─────────────────────────────────────────────────
@app.route("/api/v3/stats")
def stats():
    return jsonify(_get_reactive_stats())


@app.route("/api/v3/insights")
def insights():
    n = min(int(request.args.get("n", 10)), 50)
    return jsonify(get_recent_insights(n))


# ── Watch registration ────────────────────────────────────────────────────────
@app.route("/api/v3/watch", methods=["POST"])
def start_watch():
    data = request.get_json(silent=True) or {}
    job_id = data.get("jobId")
    if job_id is None:
        return jsonify({"error": "jobId required"}), 400

    job_id = int(job_id)
    orch   = data.get("orchestrator", ORCHESTRATOR_V3)
    if not orch:
        return jsonify({"error": "ORCHESTRATOR_V3_ADDRESS not configured"}), 500

    if job_id in _watchers and _watchers[job_id]["thread"].is_alive():
        return jsonify({"ok": True, "msg": "already watching"})

    q = Queue()
    t = threading.Thread(
        target=pipeline_v3.watch_job,
        args=(job_id, orch, q),
        daemon=True,
    )
    _watchers[job_id] = {"queue": q, "thread": t}
    t.start()

    return jsonify({"ok": True, "streamUrl": f"/api/v3/stream?jobId={job_id}"})


# ── SSE stream ────────────────────────────────────────────────────────────────
@app.route("/api/v3/stream")
def stream():
    try:
        job_id = int(request.args.get("jobId", ""))
    except (TypeError, ValueError):
        return jsonify({"error": "jobId required"}), 400

    if job_id not in _watchers:
        return jsonify({"error": "Not watching this job — call /api/v3/watch first"}), 404

    q = _watchers[job_id]["queue"]

    def generate():
        while True:
            try:
                event = q.get(timeout=25)
                yield f"data: {json.dumps(event)}\n\n"
            except Empty:
                yield "data: {\"type\":\"PING\"}\n\n"

    return Response(
        stream_with_context(generate()),
        mimetype="text/event-stream",
        headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"},
    )


if __name__ == "__main__":
    if not ORCHESTRATOR_V3:
        print("WARNING: ORCHESTRATOR_V3_ADDRESS not set in .env")
    if not pipeline_v3.PRIVATE_KEY:
        print("WARNING: DEPLOY_PRIVATE_KEY not set — contract/frontend fallback will not work")
    app.run(debug=False, port=5001)
