import shutil
import subprocess
import time
from pathlib import Path

from db import Job, Outbox, SessionLocal, utcnow
from settings import storage_dir


class Permanent(Exception):
    pass


def run(cmd: list[str], timeout: int = 600) -> str:
    completed = subprocess.run(cmd, capture_output=True, text=True, timeout=timeout)
    if completed.returncode != 0:
        tail = (completed.stderr or completed.stdout or "ffmpeg failed")[-800:]
        raise RuntimeError(tail)
    return completed.stderr


def has_video(source: Path) -> bool:
    if shutil.which("ffprobe") is None:
        raise Permanent("ffprobe is not installed")
    completed = subprocess.run(
        [
            "ffprobe",
            "-v",
            "error",
            "-select_streams",
            "v:0",
            "-show_entries",
            "stream=codec_name",
            "-of",
            "csv=p=0",
            str(source),
        ],
        capture_output=True,
        text=True,
        timeout=30,
    )
    return completed.returncode == 0 and bool(completed.stdout.strip())


def write_rendition(source: Path, dest: Path, height: int) -> None:
    if shutil.which("ffmpeg") is None:
        raise Permanent("ffmpeg is not installed")
    dest.mkdir(parents=True, exist_ok=True)
    run(
        [
            "ffmpeg",
            "-y",
            "-i",
            str(source),
            "-vf",
            f"scale=-2:{height}",
            "-c:v",
            "libx264",
            "-preset",
            "veryfast",
            "-crf",
            "23",
            "-an",
            "-f",
            "hls",
            "-hls_time",
            "4",
            "-hls_playlist_type",
            "vod",
            "-hls_segment_filename",
            str(dest / "seg_%03d.ts"),
            str(dest / "index.m3u8"),
        ]
    )


def transcode(job: Job) -> None:
    source = Path(job.source_path)
    if not source.is_file():
        raise Permanent("source file is missing")
    if not has_video(source):
        raise Permanent("file is not a video")
    root = storage_dir() / "outputs" / str(job.id)
    if root.exists():
        shutil.rmtree(root)
    write_rendition(source, root / "480p", 480)
    write_rendition(source, root / "720p", 720)
    (root / "master.m3u8").write_text(
        "\n".join(
            [
                "#EXTM3U",
                "#EXT-X-VERSION:3",
                "#EXT-X-STREAM-INF:BANDWIDTH=800000,RESOLUTION=854x480",
                "480p/index.m3u8",
                "#EXT-X-STREAM-INF:BANDWIDTH=2500000,RESOLUTION=1280x720",
                "720p/index.m3u8",
                "",
            ]
        )
    )


def handle(outbox_id: int) -> None:
    db = SessionLocal()
    try:
        outbox = db.get(Outbox, outbox_id)
        if outbox is None or outbox.published_at is not None:
            return
        job = db.get(Job, outbox.job_id)
        if job is None or job.status in {"ready", "failed", "expired"}:
            outbox.published_at = utcnow()
            db.commit()
            return
        if job.status not in {"uploaded", "transcoding"}:
            return
        job.status = "transcoding"
        job.attempts += 1
        db.commit()
        try:
            transcode(job)
        except Permanent as exc:
            job.status = "failed"
            job.error = str(exc)[:800]
            outbox.published_at = utcnow()
            db.commit()
            print(f"job {job.id} failed: {job.error}")
            return
        except Exception as exc:
            message = str(exc)[:800]
            if job.attempts >= 3:
                job.status = "failed"
                job.error = message
                outbox.published_at = utcnow()
            else:
                job.status = "uploaded"
                job.error = message
            db.commit()
            print(f"job {job.id} attempt {job.attempts}: {message}")
            return
        job.status = "ready"
        job.error = None
        job.playlist_url = f"/api/videos/{job.id}/media/master.m3u8"
        outbox.published_at = utcnow()
        db.commit()
        print(f"job {job.id} ready")
    finally:
        db.close()


def main() -> None:
    storage_dir().joinpath("outputs").mkdir(parents=True, exist_ok=True)
    print("worker waiting for uploaded videos")
    while True:
        db = SessionLocal()
        try:
            ids = [
                row.id
                for row in db.query(Outbox)
                .filter(Outbox.published_at.is_(None), Outbox.event == "video.uploaded")
                .all()
            ]
        finally:
            db.close()
        for outbox_id in ids:
            handle(outbox_id)
        time.sleep(2)


if __name__ == "__main__":
    main()
