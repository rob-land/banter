"""GroupMe API — gallery, albums, image / file / audio upload + download."""

import json
import logging
import mimetypes
import time
import urllib.error
import urllib.parse
import urllib.request
from pathlib import Path

from ..constants import APP_VERSION, GROUPME_FILE, GROUPME_IMAGE

log = logging.getLogger(__name__)


class MediaMixin:
    # ── gallery (all images sent in a group) ──
    def get_gallery(self, gid, before: str = None, after: str = None,
                    limit: int = 100):
        """Return messages that contain images, newest first.
        `before` / `after` are ISO-8601 gallery_ts strings for pagination."""
        params = {"limit": limit, "acceptFiles": "1"}
        if before:
            params["before"] = before
        if after:
            params["after"] = after
        r = self._req("GET", f"/conversations/{gid}/gallery",
                      params=params)
        resp = r.get("response", {})
        log.debug("get_gallery raw type=%s", type(resp).__name__)
        if isinstance(resp, dict):
            return resp.get("messages", [])
        if isinstance(resp, list):
            return resp
        return []

    # ── Albums (undocumented v3) ──
    # Captured from web.groupme.com on 2026-05-03. Albums live under
    # /v3/conversations/{cid}/albums/... and accept group ids for cid.

    def create_album(self, gid, title: str, cover_url: str = ""):
        """Create a new album. Returns the album dict on success.

        The web client sends `cover_attachment_id` carrying a media
        URL (despite the field name). Empty string means no cover —
        the server then auto-fills cover_image_url from the first
        media item added to the album."""
        body = {"title": title, "cover_attachment_id": cover_url}
        r = self._req("POST",
                       f"/conversations/{gid}/albums/create",
                       data=body)
        return r.get("response") if self._ok(r) else None

    def add_to_album(self, gid, album_id: str, media: list):
        """Add media items to an album. `media` is a list of
        ``{"media_url": str, "media_type": "image" | "video",
            "media_source": "album"}`` dicts."""
        r = self._req("POST",
                       f"/conversations/{gid}/albums/media",
                       data=media,
                       params={"album_id": album_id})
        return r.get("response") if self._ok(r) else None

    def get_album(self, gid, album_id: str, per_page: int = 100):
        """Fetch an album's metadata and its media. Returns the
        ``response.album`` dict (with `attachments` list nested
        inside) or None on failure."""
        r = self._req("GET",
                       f"/conversations/{gid}/albums/{album_id}/media",
                       params={"per_page": per_page})
        if not self._ok(r):
            return None
        return (r.get("response") or {}).get("album")

    def get_albums(self, gid, per_page: int = 50):
        """List the albums in a conversation. Each entry is the album
        metadata dict (no media list — call `get_album` to fetch a
        single album's contents).

        GET /v3/conversations/{gid}/albums?per_page=N"""
        r = self._req("GET",
                       f"/conversations/{gid}/albums",
                       params={"per_page": per_page})
        if not self._ok(r):
            return []
        return (r.get("response") or {}).get("albums") or []

    # ── image upload ──
    def upload_image(self, file_path: str):
        ext = Path(file_path).suffix.lower()
        ct_map = {".jpg": "image/jpeg", ".jpeg": "image/jpeg",
                  ".png": "image/png",  ".gif": "image/gif",
                  ".webp": "image/webp"}
        content_type = ct_map.get(ext, "image/jpeg")

        url = f"{GROUPME_IMAGE}/pictures?token={self.token}"
        log.debug("upload_image: %s  content-type=%s", file_path, content_type)

        with open(file_path, "rb") as f:
            data = f.read()
        log.debug("upload_image: %d bytes to upload", len(data))

        req = urllib.request.Request(url, data=data)
        req.add_header("Content-Type", content_type)
        req.add_header("User-Agent", f"GroupMe-GNOME/{APP_VERSION}")
        try:
            with urllib.request.urlopen(req, timeout=60) as resp:
                raw = resp.read().decode()
                log.debug("upload_image: response %d  body=%s", resp.status, raw[:300])
                r = json.loads(raw)
                img_url = r.get("payload", {}).get("url")
                log.debug("upload_image: image URL = %s", img_url)
                return img_url
        except urllib.error.HTTPError as e:
            raw = ""
            try:
                raw = e.read().decode()
            except Exception:
                pass
            log.debug("upload_image: HTTP %d  %s", e.code, raw[:200])
            log.error("Image upload failed: HTTP %d", e.code)
            return None
        except Exception as e:
            log.debug("upload_image: exception %s", e)
            log.exception("Image upload exception")
            return None

    # ── file upload (non-image attachments) ──
    #
    # Recovered from web.groupme.com (2026-04-30). Three steps:
    #   1. POST file.groupme.com/v1/{cid}/files?name=<urlencoded>
    #      — raw bytes, Content-Type set to the file's MIME type.
    #      Server returns a JSON envelope with the file_id (which
    #      doubles as the upload job id).
    #   2. GET .../uploadStatus?job=<file_id>&cnt=N  — poll until
    #      `status == "completed"`. cnt is a sequential 0,1,2…
    #      counter the web client increments per poll.
    #   3. Caller attaches `{type:"file", file_id:<id>}` to a message
    #      body and POSTs it via the standard /groups/{gid}/messages.
    #
    # The metadata (file_name / file_size / mime_type) is fetched
    # separately from the receive side via `get_file_data` — the
    # message attachment itself only carries the id.

    UPLOAD_POLL_INTERVAL_S = 1.0
    UPLOAD_POLL_TIMEOUT_S  = 60   # ~60 polls; web client allows much longer

    def upload_file(self, cid: str, file_path: str):
        """Upload a non-image file. `cid` is the same conversation_id
        used for edit/delete/pin: `group_id` for groups, `<lo>+<hi>`
        for DMs. Returns the file_id on success, or None on any
        failure. Blocking — call from a worker thread."""
        path = Path(file_path)
        name = path.name
        # Best-effort MIME guess; default to octet-stream which the
        # server accepts for arbitrary binaries.
        mime, _ = mimetypes.guess_type(str(path))
        mime    = mime or "application/octet-stream"

        try:
            data = path.read_bytes()
        except Exception as e:
            log.debug("upload_file: read failed %s: %s", file_path, e)
            return None

        url = (f"{GROUPME_FILE}/v1/{cid}/files"
               f"?name={urllib.parse.quote(name)}")
        log.debug("upload_file: %s  mime=%s  bytes=%d", name, mime, len(data))

        req = urllib.request.Request(url, data=data, method="POST")
        req.add_header("Content-Type",     mime)
        req.add_header("X-Access-Token",   self.token or "")
        req.add_header("X-Requested-With", "GroupMeWeb/1.2.3")
        req.add_header("User-Agent",       f"GroupMe-GNOME/{APP_VERSION}")

        try:
            with urllib.request.urlopen(req, timeout=120) as resp:
                raw = resp.read().decode()
                log.debug("upload_file: post→ %d  body=%s",
                    resp.status, raw[:300])
        except urllib.error.HTTPError as e:
            try:
                err_body = e.read().decode()[:200]
            except Exception:
                err_body = ""
            log.debug("upload_file: HTTP %d  %s", e.code, err_body)
            return None
        except Exception as e:
            log.debug("upload_file: exception %s", e)
            return None

        # Server response shape (recovered): {"status_url": "...", "file_id": "..."}
        # We try each common key, then fall back to parsing job= out of
        # status_url.
        try:
            payload = json.loads(raw) if raw.strip() else {}
        except Exception:
            payload = {}
        file_id = (payload.get("file_id") or
                   payload.get("job_id")   or "")
        status_url = payload.get("status_url") or ""
        if not file_id and status_url:
            try:
                qs = urllib.parse.parse_qs(
                    urllib.parse.urlparse(status_url).query)
                file_id = (qs.get("job") or [""])[0]
            except Exception:
                file_id = ""
        if not file_id:
            log.debug("upload_file: could not extract file_id from %s", payload)
            return None

        # Step 2: poll until completed.
        if not status_url:
            status_url = (f"{GROUPME_FILE}/v1/{cid}/uploadStatus"
                          f"?job={file_id}")

        deadline = time.time() + self.UPLOAD_POLL_TIMEOUT_S
        cnt = 0
        while time.time() < deadline:
            poll_url = f"{status_url}&cnt={cnt}" \
                if "cnt=" not in status_url else status_url
            req = urllib.request.Request(poll_url)
            req.add_header("X-Access-Token",   self.token or "")
            req.add_header("X-Requested-With", "GroupMeWeb/1.2.3")
            try:
                with urllib.request.urlopen(req, timeout=15) as r:
                    s_raw = r.read().decode()
                s = json.loads(s_raw) if s_raw.strip() else {}
            except Exception as e:
                log.debug("upload_file: status poll failed: %s", e)
                s = {}
            status = s.get("status", "")
            log.debug("upload_file: poll cnt=%d  status=%s", cnt, status)
            if status == "completed":
                return s.get("file_id") or file_id
            if status in ("failed", "error"):
                return None
            cnt += 1
            time.sleep(self.UPLOAD_POLL_INTERVAL_S)
        log.debug("upload_file: timed out waiting for completion")
        return None

    def get_file_data(self, cid: str, file_ids: list):
        """Resolve {file_name, file_size, mime_type} for one or more
        file_ids via POST file.groupme.com/v1/{cid}/fileData. Returns a
        dict mapping file_id → file_data dict. Empty dict on failure."""
        if not file_ids:
            return {}
        url  = f"{GROUPME_FILE}/v1/{cid}/fileData"
        body = json.dumps({"file_ids": list(file_ids)}).encode("utf-8")
        req  = urllib.request.Request(url, data=body, method="POST")
        req.add_header("Content-Type",     "application/json")
        req.add_header("X-Access-Token",   self.token or "")
        req.add_header("X-Requested-With", "GroupMeWeb/1.2.3")
        try:
            with urllib.request.urlopen(req, timeout=15) as r:
                raw = r.read().decode()
            entries = json.loads(raw) if raw.strip() else []
        except Exception as e:
            log.debug("get_file_data: failed: %s", e)
            return {}
        out = {}
        for ent in entries or []:
            fid = ent.get("file_id")
            fd  = ent.get("file_data") or {}
            if fid and fd:
                out[fid] = fd
        return out

    def file_download_url(self, cid: str, file_id: str) -> str:
        """Build a download URL for a file attachment.

        Verified against web.groupme.com 2026-04-30. The query param
        is `access_token`, NOT `token` like elsewhere in the API.
        `_dl=<unix_ms>` is a cache buster the web client adds; harmless
        to include and helps bypass any intermediary cache."""
        return (f"{GROUPME_FILE}/v1/{cid}/files/{file_id}"
                f"?access_token={urllib.parse.quote(self.token or '')}"
                f"&_dl={int(time.time() * 1000)}")

    def download_audio(self, url: str, dest_path: str) -> bool:
        """Download a voice-note (audio attachment) from m.groupme.com.

        Voice notes are `type:"audio"` attachments hosted at
        `m.groupme.com/uploads/{upload_id}/original.m4a`. Auth uses a
        `Cookie: token=<access_token>` header rather than the usual
        `?access_token=` query (different from file.groupme.com).
        m.groupme.com responds with a 301 to a signed Azure CDN URL
        valid ~24h; urllib follows the redirect transparently and
        Azure's query-string SAS handles auth on the second hop.

        Streams in 64 KB chunks. Returns True on success."""
        if not url:
            return False
        req = urllib.request.Request(url)
        req.add_header("Cookie",         f"token={self.token or ''}")
        req.add_header("User-Agent",     f"GroupMe-GNOME/{APP_VERSION}")
        req.add_header("X-Requested-With", "GroupMeWeb/1.2.3")
        try:
            with urllib.request.urlopen(req, timeout=30) as resp, \
                 open(dest_path, "wb") as out:
                while True:
                    chunk = resp.read(64 * 1024)
                    if not chunk:
                        break
                    out.write(chunk)
            log.debug("download_audio: ok (%s)", url)
            return True
        except urllib.error.HTTPError as e:
            log.debug("download_audio: HTTP %d", e.code)
            return False
        except Exception as e:
            log.debug("download_audio: exception %s", e)
            return False

    def download_file(self, cid: str, file_id: str, dest_path: str) -> bool:
        """Stream an authenticated file attachment to `dest_path`.
        Returns True on success.

        Streams in 64 KB chunks so large attachments don't fully
        buffer in memory. The request carries the token in both the
        query string (via file_download_url) and the X-Access-Token
        header — the server accepts either; harmless to send both."""
        url = self.file_download_url(cid, file_id)
        log.debug("download_file: %s → %s", file_id, dest_path)
        req = urllib.request.Request(url)
        req.add_header("X-Access-Token",   self.token or "")
        req.add_header("X-Requested-With", "GroupMeWeb/1.2.3")
        req.add_header("User-Agent",       f"GroupMe-GNOME/{APP_VERSION}")
        try:
            with urllib.request.urlopen(req, timeout=120) as resp, \
                 open(dest_path, "wb") as out:
                while True:
                    chunk = resp.read(64 * 1024)
                    if not chunk:
                        break
                    out.write(chunk)
            log.debug("download_file: ok")
            return True
        except urllib.error.HTTPError as e:
            log.debug("download_file: HTTP %d", e.code)
            return False
        except Exception as e:
            log.debug("download_file: exception %s", e)
            return False
