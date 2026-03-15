import sys
import subprocess
import threading
import time
from urllib.parse import urlencode, urlparse, urlunparse, parse_qs

from starlette.status import HTTP_303_SEE_OTHER
from starlette.applications import Starlette
from starlette.config import Config
from starlette.responses import JSONResponse, RedirectResponse
from starlette.routing import Route
from starlette.templating import Jinja2Templates
from starlette.background import BackgroundTask

from yt_dlp import YoutubeDL, version

templates = Jinja2Templates(directory="templates")
config = Config(".env")

app_defaults = {
    "YDL_FORMAT": config("YDL_FORMAT", cast=str, default="bestvideo+bestaudio/best"),
    "YDL_EXTRACT_AUDIO_FORMAT": config("YDL_EXTRACT_AUDIO_FORMAT", default=None),
    "YDL_EXTRACT_AUDIO_QUALITY": config(
        "YDL_EXTRACT_AUDIO_QUALITY", cast=str, default="192"
    ),
    "YDL_RECODE_VIDEO_FORMAT": config("YDL_RECODE_VIDEO_FORMAT", default=None),
    "YDL_OUTPUT_TEMPLATE": config(
        "YDL_OUTPUT_TEMPLATE",
        cast=str,
        default="/youtube-dl/%(title).200s [%(id)s].%(ext)s",
    ),
    "YDL_ARCHIVE_FILE": config("YDL_ARCHIVE_FILE", default=None),
    "YDL_UPDATE_TIME": config("YDL_UPDATE_TIME", cast=bool, default=True),
}


async def dl_queue_list(request):
    return templates.TemplateResponse(
        "index.html", {"request": request, "ytdlp_version": version.__version__}
    )


# In-memory list of currently running downloads (thread-safe)
_download_jobs = []
_download_jobs_lock = threading.Lock()
_next_job_id = 0


async def redirect(request):
    return RedirectResponse(url="/youtube-dl")


async def queue_list(request):
    """Return JSON list of currently running downloads."""
    with _download_jobs_lock:
        jobs = list(_download_jobs)
    return JSONResponse({"jobs": jobs})


def normalize_youtube_url(url):
    """Trim YouTube URLs to only the video identifier (e.g. ?v=VIDEO_ID)."""
    try:
        parsed = urlparse(url)
        netloc_lower = (parsed.netloc or "").lower()
        # youtube.com/watch?v=...
        if "youtube.com" in netloc_lower and parsed.path.rstrip("/") == "/watch":
            qs = parse_qs(parsed.query)
            if "v" in qs:
                video_id = qs["v"][0]
                new_query = urlencode({"v": video_id})
                return urlunparse(
                    (parsed.scheme, parsed.netloc, parsed.path, "", new_query, "")
                )
        # youtu.be/VIDEO_ID
        if "youtu.be" in netloc_lower and parsed.path:
            return urlunparse((parsed.scheme, parsed.netloc, parsed.path, "", "", ""))
    except Exception:
        pass
    return url


async def q_put(request):
    form = await request.form()
    raw_url = (form.get("url") or "").strip()
    url = normalize_youtube_url(raw_url) if raw_url else ""
    ui = form.get("ui")
    options = {"format": form.get("format")}

    if not url:
        return JSONResponse(
            {"success": False, "error": "/q called without a 'url' in form data"}
        )

    global _next_job_id
    with _download_jobs_lock:
        job_id = _next_job_id
        _next_job_id += 1
        job = {
            "id": job_id,
            "url": url,
            "format": options.get("format", "bestvideo"),
            "status": "downloading",
            "started_at": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
        }
        _download_jobs.append(job)

    task = BackgroundTask(download_with_tracking, job_id, url, options)

    print("Added url " + url + " to the download queue")

    if not ui:
        return JSONResponse(
            {"success": True, "url": url, "options": options}, background=task
        )
    return RedirectResponse(
        url="/youtube-dl?added=" + url, status_code=HTTP_303_SEE_OTHER, background=task
    )


async def update_route(scope, receive, send):
    task = BackgroundTask(update)

    return JSONResponse({"output": "Initiated package update"}, background=task)


def update():
    try:
        output = subprocess.check_output(
            [sys.executable, "-m", "pip", "install", "--upgrade", "yt-dlp"]
        )

        print(output.decode("utf-8"))
    except subprocess.CalledProcessError as e:
        print(e.output)


def _update_job_progress(job_id, progress_dict):
    """Update a job's progress fields from yt-dlp progress hook."""
    with _download_jobs_lock:
        for j in _download_jobs:
            if j["id"] == job_id:
                j["status"] = progress_dict.get("status", j.get("status", "downloading"))
                if "downloaded_bytes" in progress_dict:
                    j["downloaded_bytes"] = progress_dict["downloaded_bytes"]
                if "total_bytes" in progress_dict:
                    j["total_bytes"] = progress_dict["total_bytes"]
                elif "total_bytes_estimate" in progress_dict:
                    j["total_bytes"] = progress_dict["total_bytes_estimate"]
                if "speed" in progress_dict:
                    j["speed"] = progress_dict["speed"]
                if "eta" in progress_dict:
                    j["eta"] = progress_dict["eta"]
                break


def get_ydl_options(request_options, job_id=None):
    request_vars = {
        "YDL_EXTRACT_AUDIO_FORMAT": None,
        "YDL_RECODE_VIDEO_FORMAT": None,
    }

    requested_format = request_options.get("format", "bestvideo")

    if requested_format in ["aac", "flac", "mp3", "m4a", "opus", "vorbis", "wav"]:
        request_vars["YDL_EXTRACT_AUDIO_FORMAT"] = requested_format
    elif requested_format == "bestaudio":
        request_vars["YDL_EXTRACT_AUDIO_FORMAT"] = "best"
    elif requested_format in ["mp4", "flv", "webm", "ogg", "mkv", "avi"]:
        request_vars["YDL_RECODE_VIDEO_FORMAT"] = requested_format

    ydl_vars = app_defaults | request_vars

    postprocessors = []

    if ydl_vars["YDL_EXTRACT_AUDIO_FORMAT"]:
        postprocessors.append(
            {
                "key": "FFmpegExtractAudio",
                "preferredcodec": ydl_vars["YDL_EXTRACT_AUDIO_FORMAT"],
                "preferredquality": ydl_vars["YDL_EXTRACT_AUDIO_QUALITY"],
            }
        )

    if ydl_vars["YDL_RECODE_VIDEO_FORMAT"]:
        postprocessors.append(
            {
                "key": "FFmpegVideoConvertor",
                "preferedformat": ydl_vars["YDL_RECODE_VIDEO_FORMAT"],
            }
        )

    opts = {
        "format": ydl_vars["YDL_FORMAT"],
        "postprocessors": postprocessors,
        "outtmpl": ydl_vars["YDL_OUTPUT_TEMPLATE"],
        "download_archive": ydl_vars["YDL_ARCHIVE_FILE"],
        "updatetime": ydl_vars["YDL_UPDATE_TIME"] == "True",
    }
    if job_id is not None:
        opts["progress_hooks"] = [lambda d, jid=job_id: _update_job_progress(jid, d)]
    return opts


def download(url, request_options):
    with YoutubeDL(get_ydl_options(request_options)) as ydl:
        ydl.download([url])


def download_with_tracking(job_id, url, request_options):
    """Run download with progress tracking; remove job when done."""
    opts = get_ydl_options(request_options, job_id=job_id)
    try:
        with YoutubeDL(opts) as ydl:
            try:
                info = ydl.extract_info(url, download=False)
                if info and info.get("title"):
                    with _download_jobs_lock:
                        for j in _download_jobs:
                            if j["id"] == job_id:
                                j["title"] = info.get("title", "")[:200]
                                break
            except Exception:
                pass
            ydl.download([url])
    finally:
        with _download_jobs_lock:
            for i, j in enumerate(_download_jobs):
                if j["id"] == job_id:
                    _download_jobs.pop(i)
                    break


routes = [
    Route("/", endpoint=redirect),
    Route("/youtube-dl", endpoint=dl_queue_list),
    Route("/youtube-dl/q", endpoint=q_put, methods=["POST"]),
    Route("/youtube-dl/queue", endpoint=queue_list),
    Route("/youtube-dl/update", endpoint=update_route, methods=["PUT"]),
]

app = Starlette(debug=True, routes=routes)

print("Updating youtube-dl to the newest version")
update()
