#!/usr/bin/env python3
"""
Worker: listen to Redis toWorker queue, fetch MP3 from Minio, run DEMUCS, upload results to Minio.
"""
import os
import sys
import json
import shlex
import platform
import tempfile
import shutil
import redis
import requests
from minio import Minio

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
        key = f"{platform.node()}.worker.info"
        redis_client.lpush("logging", f"{key}:{message}")
    except Exception:
        pass
    print("INFO:", message, file=sys.stdout)


def log_debug(message):
    try:
        key = f"{platform.node()}.worker.debug"
        redis_client.lpush("logging", f"{key}:{message}")
    except Exception:
        pass
    print("DEBUG:", message, file=sys.stdout)


# DEMUCS stem name -> API track name (bass -> base)
STEM_TO_TRACK = {"bass": "base", "drums": "drums", "vocals": "vocals", "other": "other"}


def run_demucs(input_path, output_dir, model="htdemucs"):
    """Run demucs.separate; output_dir will contain model_name/songname/*.mp3."""
    # Use the same interpreter as this worker (venv/pyenv), not bare `python3` from PATH.
    exe = shlex.quote(sys.executable)
    cmd = (
        f"{exe} -m demucs.separate --out {shlex.quote(output_dir)} --mp3 "
        f"-n {shlex.quote(model)} {shlex.quote(input_path)}"
    )
    log_debug(f"demucs command: {cmd}")
    ret = os.system(cmd)
    return ret == 0


def process_job(job):
    songhash = job.get("songhash")
    model = job.get("model") or "htdemucs"
    callback = job.get("callback")
    if not songhash:
        log_debug("job missing songhash")
        return

    log_info(f"processing songhash={songhash}")
    client = get_minio_client()
    ensure_buckets(client)

    tmpdir = tempfile.mkdtemp(prefix="demucs_")
    try:
        input_path = os.path.join(tmpdir, "input.mp3")
        output_dir = os.path.join(tmpdir, "output")

        # Download from Minio queue bucket
        try:
            client.fget_object(QUEUE_BUCKET, f"{songhash}.mp3", input_path)
        except Exception as e:
            log_debug(f"minio get error: {e}")
            return

        if not run_demucs(input_path, output_dir, model=model):
            log_debug(f"demucs failed for {songhash}")
            return

        # Find output: output_dir/model_name/input_name/*.mp3 (input_name is derived from input file)
        # For input "input.mp3" the track folder might be "input" (no extension in demucs output)
        model_out = os.path.join(output_dir, model)
        if not os.path.isdir(model_out):
            # try first subdir
            subdirs = [d for d in os.listdir(output_dir) if os.path.isdir(os.path.join(output_dir, d))]
            if subdirs:
                model_out = os.path.join(output_dir, subdirs[0])
        if not os.path.isdir(model_out):
            log_debug(f"no model output dir under {output_dir}")
            return

        track_dirs = [d for d in os.listdir(model_out) if os.path.isdir(os.path.join(model_out, d))]
        if not track_dirs:
            log_debug(f"no track dir under {model_out}")
            return
        track_dir = os.path.join(model_out, track_dirs[0])

        for stem, track_name in STEM_TO_TRACK.items():
            mp3_name = f"{stem}.mp3"
            local_path = os.path.join(track_dir, mp3_name)
            if not os.path.isfile(local_path):
                continue
            object_name = f"{songhash}-{track_name}.mp3"
            try:
                client.fput_object(OUTPUT_BUCKET, object_name, local_path)
                log_debug(f"uploaded {object_name}")
            except Exception as e:
                log_debug(f"upload error {object_name}: {e}")

        log_info(f"finished songhash={songhash}")

        if callback:
            url = callback.get("url")
            data = callback.get("data")
            if url and data is not None:
                try:
                    r = requests.post(url, json=data, timeout=10)
                    log_debug(f"callback {url} status={r.status_code}")
                except Exception as e:
                    log_debug(f"callback failed: {e}")
    finally:
        shutil.rmtree(tmpdir, ignore_errors=True)


def main():
    log_info("worker started")
    client = get_minio_client()
    ensure_buckets(client)
    log_info(
        f"blocking on Redis key '{WORKER_QUEUE_KEY}' (no output until a job arrives); "
        "enqueue via REST POST /apiv1/separate or worker/send-request.py"
    )

    while True:
        try:
            # blpop blocks until work is available; timeout=0 means block forever
            work = redis_client.blpop(WORKER_QUEUE_KEY, timeout=0)
            if not work:
                continue
            _, payload = work
            raw = payload.decode("utf-8") if isinstance(payload, bytes) else payload
            job = json.loads(raw)
            process_job(job)
        except KeyboardInterrupt:
            break
        except Exception as e:
            log_debug(f"worker loop error: {e}")
        sys.stdout.flush()
        sys.stderr.flush()


if __name__ == "__main__":
    main()
