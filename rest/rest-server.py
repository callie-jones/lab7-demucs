#!/usr/bin/env python3
"""
REST API for music separation service.
Accepts separation requests, queues work via Redis, serves tracks from Minio.
"""
import os
import sys
import hashlib
import json
import platform
import redis
import jsonpickle
from flask import Flask, request, Response
from minio import Minio

app = Flask(__name__)

# Config from environment
REDIS_HOST = os.getenv("REDIS_HOST") or "localhost"
REDIS_PORT = int(os.getenv("REDIS_PORT") or "6379")
MINIO_HOST = os.getenv("MINIO_HOST") or "localhost"
MINIO_PORT = os.getenv("MINIO_PORT") or "9000"
MINIO_ACCESS = os.getenv("MINIO_ACCESS_KEY") or "rootuser"
MINIO_SECRET = os.getenv("MINIO_SECRET_KEY") or "rootpass123"
QUEUE_BUCKET = os.getenv("MINIO_QUEUE_BUCKET") or "queue"
OUTPUT_BUCKET = os.getenv("MINIO_OUTPUT_BUCKET") or "output"
WORKER_QUEUE_KEY = "toWorker"

redis_client = redis.StrictRedis(host=REDIS_HOST, port=REDIS_PORT, db=0)


def get_minio_client():
    endpoint = f"{MINIO_HOST}:{MINIO_PORT}"
    return Minio(
        endpoint,
        access_key=MINIO_ACCESS,
        secret_key=MINIO_SECRET,
        secure=False,
    )


def ensure_buckets(client):
    for bucket in (QUEUE_BUCKET, OUTPUT_BUCKET):
        if not client.bucket_exists(bucket):
            client.make_bucket(bucket)


def log_info(message):
    try:
        key = f"{platform.node()}.rest.info"
        redis_client.lpush("logging", f"{key}:{message}")
    except Exception:
        pass


def log_debug(message):
    try:
        key = f"{platform.node()}.rest.debug"
        redis_client.lpush("logging", f"{key}:{message}")
    except Exception:
        pass


def song_hash(mp3_bytes):
    """Return a short hash identifier for the song (match sample output length)."""
    h = hashlib.sha256(mp3_bytes).hexdigest()
    return h[:50]


@app.route("/", methods=["GET", "POST"])
def hello():
    # POST used by optional worker webhook (sample scripts point callback here)
    return "<h1>Music Separation Server</h1><p>Use a valid endpoint</p>"


@app.route("/apiv1/separate", methods=["POST"])
def separate():
    """
    Accept JSON with base64-encoded mp3, optional model and callback.
    Upload mp3 to Minio queue bucket, push job to Redis, return songhash.
    """
    try:
        data = jsonpickle.decode(request.get_data(as_text=True))
    except Exception as e:
        log_debug(f"separate decode error: {e}")
        return Response(json.dumps({"error": "Invalid JSON"}), status=400, mimetype="application/json")

    mp3_b64 = data.get("mp3")
    if not mp3_b64:
        return Response(json.dumps({"error": "Missing mp3"}), status=400, mimetype="application/json")

    try:
        mp3_bytes = __import__("base64").b64decode(mp3_b64)
    except Exception:
        return Response(json.dumps({"error": "Invalid base64 mp3"}), status=400, mimetype="application/json")

    model = data.get("model") or "htdemucs"
    callback = data.get("callback")

    h = song_hash(mp3_bytes)
    log_info(f"separate: songhash={h}")

    client = get_minio_client()
    ensure_buckets(client)

    # Store input in queue bucket
    from io import BytesIO
    client.put_object(QUEUE_BUCKET, f"{h}.mp3", BytesIO(mp3_bytes), length=len(mp3_bytes))

    job = {
        "songhash": h,
        "model": model,
        "callback": callback,
    }
    redis_client.rpush(WORKER_QUEUE_KEY, json.dumps(job))
    log_debug(f"enqueued job for {h}")

    return Response(
        json.dumps({"hash": h, "reason": "Song enqueued for separation"}),
        status=200,
        mimetype="application/json",
    )


@app.route("/apiv1/queue", methods=["GET"])
def queue():
    """Return list of songhashes currently in the worker queue (peek, no remove)."""
    entries = redis_client.lrange(WORKER_QUEUE_KEY, 0, -1)
    # Jobs are JSON strings; extract songhash for display
    queue_list = []
    for raw in entries:
        try:
            job = json.loads(raw.decode("utf-8") if isinstance(raw, bytes) else raw)
            queue_list.append(job.get("songhash", raw))
        except Exception:
            queue_list.append(str(raw))
    return Response(
        json.dumps({"queue": queue_list}),
        status=200,
        mimetype="application/json",
    )


TRACK_NAMES = {"base", "vocals", "drums", "other"}


@app.route("/apiv1/track/<songhash>/<track>", methods=["GET"])
def track(songhash, track):
    """
    Stream the requested track as binary MP3.
    track is one of: base, vocals, drums, other.
    """
    if track not in TRACK_NAMES:
        return Response(json.dumps({"error": f"Unknown track: {track}"}), status=400, mimetype="application/json")

    client = get_minio_client()
    object_name = f"{songhash}-{track}.mp3"
    response = None
    try:
        response = client.get_object(OUTPUT_BUCKET, object_name)
        data = response.read()
        return Response(data, mimetype="audio/mpeg")
    except Exception as e:
        # Log at INFO so Flask console shows why (Minio down vs missing object)
        log_info(f"track get failed bucket={OUTPUT_BUCKET} object={object_name}: {e}")
        return Response(
            json.dumps({"error": "Track not found", "detail": str(e)}),
            status=404,
            mimetype="application/json",
        )
    finally:
        if response is not None:
            response.close()
            release = getattr(response, "release_conn", None) or getattr(
                response, "release_connection", None
            )
            if callable(release):
                release()


@app.route("/apiv1/remove/<songhash>/<track>", methods=["GET", "DELETE"])
def remove_track(songhash, track):
    """Remove one track for the given songhash."""
    if track not in TRACK_NAMES:
        return Response(json.dumps({"error": f"Unknown track: {track}"}), status=400, mimetype="application/json")

    client = get_minio_client()
    object_name = f"{songhash}-{track}.mp3"
    try:
        client.remove_object(OUTPUT_BUCKET, object_name)
        return Response(json.dumps({"status": "removed"}), status=200, mimetype="application/json")
    except Exception as e:
        log_debug(f"remove error: {e}")
        return Response(json.dumps({"error": "Not found"}), status=404, mimetype="application/json")


@app.route("/apiv1/remove/<songhash>", methods=["GET", "DELETE"])
def remove_song(songhash):
    """Remove all tracks for the given songhash."""
    client = get_minio_client()
    removed = 0
    for t in TRACK_NAMES:
        try:
            client.remove_object(OUTPUT_BUCKET, f"{songhash}-{t}.mp3")
            removed += 1
        except Exception:
            pass
    return Response(
        json.dumps({"status": "removed", "count": removed}),
        status=200,
        mimetype="application/json",
    )


if __name__ == "__main__":
    port = int(os.getenv("PORT") or "5000")
    log_info(f"rest server starting on port {port}")
    app.run(host="0.0.0.0", port=port)
