"""
EinthuBot - Einthusan Premium Downloader for Jellyfin
"""

import os
import re
import json
import base64
import time
import shutil
import logging
import threading
from datetime import datetime
from pathlib import Path
from typing import Optional
from urllib.parse import quote_plus, urljoin

import requests
from bs4 import BeautifulSoup
from flask import Flask, render_template, jsonify, request as flask_request, Response
from flask_cors import CORS

os.umask(0o000)

# ── Config ─────────────────────────────────────────────────────────────────
class Config:
    EINTHUSAN_EMAIL    = os.getenv("EINTHUSAN_EMAIL", "")
    EINTHUSAN_PASSWORD = os.getenv("EINTHUSAN_PASSWORD", "")
    EINTHUSAN_BASE     = "https://einthusan.tv"
    SEERR_URL          = os.getenv("SEERR_URL", "http://localhost:5055")
    SEERR_API_KEY      = os.getenv("SEERR_API_KEY", "")
    JELLYFIN_URL       = os.getenv("JELLYFIN_URL", "http://jellyfin:8096")
    JELLYFIN_API_KEY   = os.getenv("JELLYFIN_API_KEY", "")
    DOWNLOAD_DIR       = os.getenv("DOWNLOAD_DIR", "/downloads/einthusan")
    POLL_INTERVAL      = int(os.getenv("POLL_INTERVAL", "120"))
    WEB_PORT           = int(os.getenv("WEB_PORT", "7878"))
    LANGUAGE           = os.getenv("EINTHUSAN_LANGUAGE", "hindi")
    TMDB_API_KEY       = os.getenv("TMDB_API_KEY", "")

# ── Logging ─────────────────────────────────────────────────────────────────
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s",
    handlers=[
        logging.StreamHandler(),
        logging.FileHandler("logs/einthubot.log"),
    ],
)
log = logging.getLogger("einthubot")

# ── State ────────────────────────────────────────────────────────────────────
activity_log: list[dict] = []
known_request_ids: set[int] = set()
downloads: dict[str, dict] = {}
download_counter = 0
cancel_flags: dict[str, bool] = {}
pause_flags: dict[str, bool] = {}
approved_request_ids: set[int] = set()
completed_request_ids: set[int] = set()

def new_download_id() -> str:
    global download_counter
    download_counter += 1
    return f"dl_{download_counter}"

def log_activity(kind: str, title: str, msg: str, status: str = "info"):
    entry = {
        "id":     len(activity_log),
        "time":   datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
        "kind":   kind,
        "title":  title,
        "msg":    msg,
        "status": status,
    }
    activity_log.insert(0, entry)
    if len(activity_log) > 200:
        activity_log.pop()
    log.info("[%s] %s — %s", kind, title, msg)

def set_permissions(path: str):
    try:
        os.chmod(path, 0o777)
    except Exception as e:
        log.warning("chmod failed on %s: %s", path, e)

def cleanup_download(dl_id: str, delete_files: bool = True):
    """Remove a download's tracking state, optionally deleting its files.

    When files are deleted the movie is gone, so the request is also
    un-tracked (dropped from approved/completed) to allow re-download.
    """
    dl = downloads.get(dl_id, {})
    if delete_files:
        filename = dl.get("filename", "")
        folder   = dl.get("folder", "")
        if filename:
            try: Path(filename).unlink(missing_ok=True)
            except Exception: pass
        if folder:
            try: Path(folder).rmdir()
            except Exception: pass
        request_id = dl.get("request_id")
        if request_id:
            approved_request_ids.discard(request_id)
            completed_request_ids.discard(request_id)
    downloads.pop(dl_id, None)

# ── TMDB lookup ───────────────────────────────────────────────────────────────
def get_tmdb_info(title: str, year: str) -> dict:
    if not Config.TMDB_API_KEY:
        return {"title": title, "year": year, "tmdb_id": None}
    try:
        params = {"api_key": Config.TMDB_API_KEY, "query": title, "language": "en-US"}
        if year:
            params["year"] = year
        r = requests.get("https://api.themoviedb.org/3/search/movie", params=params, timeout=10)
        r.raise_for_status()
        results = r.json().get("results", [])
        if not results:
            return {"title": title, "year": year, "tmdb_id": None}
        best       = results[0]
        tmdb_id    = best.get("id")
        tmdb_title = best.get("title", title)
        tmdb_year  = (best.get("release_date") or "")[:4] or year
        log.info("TMDB match: %s (%s) tmdb-%s", tmdb_title, tmdb_year, tmdb_id)
        return {"title": tmdb_title, "year": tmdb_year, "tmdb_id": tmdb_id}
    except Exception as e:
        log.error("TMDB lookup error: %s", e)
        return {"title": title, "year": year, "tmdb_id": None}


def get_tmdb_details_by_id(tmdb_id: int, media_type: str = "movie") -> dict:
    if not Config.TMDB_API_KEY or not tmdb_id:
        return {"title": "Unknown", "year": "", "poster": "", "original_title": ""}
    try:
        endpoint = "movie" if media_type == "movie" else "tv"
        r = requests.get(
            f"https://api.themoviedb.org/3/{endpoint}/{tmdb_id}",
            params={"api_key": Config.TMDB_API_KEY, "language": "en-US"},
            timeout=8,
        )
        if r.status_code == 200:
            td             = r.json()
            title          = td.get("title") or td.get("name") or "Unknown"
            original_title = td.get("original_title") or td.get("original_name") or title
            date           = td.get("release_date") or td.get("first_air_date") or ""
            year           = date[:4]
            poster         = td.get("poster_path", "")
            poster_url     = f"https://image.tmdb.org/t/p/w200{poster}" if poster else ""
            return {"title": title, "original_title": original_title, "year": year, "poster": poster_url}
    except Exception as e:
        log.warning("TMDB details fetch failed for %s: %s", tmdb_id, e)
    return {"title": "Unknown", "year": "", "poster": "", "original_title": ""}


def fetch_tmdb_artwork(tmdb_id, media_type: str, rating, overview):
    """Fetch poster/backdrop/genres from TMDB, filling rating/overview if missing.

    Returns (poster_path, backdrop_path, genres_str, rating, overview).
    """
    poster = backdrop = genres = ""
    if tmdb_id and Config.TMDB_API_KEY:
        endpoint = "movie" if media_type == "movie" else "tv"
        try:
            tr = requests.get(
                f"https://api.themoviedb.org/3/{endpoint}/{tmdb_id}",
                params={"api_key": Config.TMDB_API_KEY, "language": "en-US"},
                timeout=8,
            )
            if tr.status_code == 200:
                td       = tr.json()
                poster   = td.get("poster_path", "")
                backdrop = td.get("backdrop_path", "")
                genres   = ", ".join(g["name"] for g in td.get("genres", []))
                if not rating:
                    rating = td.get("vote_average")
                if not overview:
                    overview = (td.get("overview") or "")[:300]
        except Exception as te:
            log.warning("TMDB artwork fetch failed for %s: %s", tmdb_id, te)
    return poster, backdrop, genres, rating, overview


def build_movie_filename(title: str, year: str, tmdb_id: Optional[int]) -> tuple[Path, Path]:
    safe_title = re.sub(r'[^\w\s\-.]', '', title).strip()
    base       = f"{safe_title} ({year})" if year else safe_title
    if tmdb_id:
        base = f"{base} {{tmdb-{tmdb_id}}}"
    folder   = Path(Config.DOWNLOAD_DIR) / base
    filepath = folder / f"{base}.mp4"
    return folder, filepath

# ── Jellyfin client ───────────────────────────────────────────────────────────
class JellyfinClient:
    def __init__(self):
        self.base    = Config.JELLYFIN_URL.rstrip("/")
        self.headers = {
            "X-Emby-Token":  Config.JELLYFIN_API_KEY,
            "Content-Type":  "application/json",
        }

    def get_user_id(self) -> str:
        """Get the first admin user ID from Jellyfin."""
        try:
            r = requests.get(
                f"{self.base}/Users",
                headers=self.headers,
                timeout=10,
            )
            r.raise_for_status()
            users = r.json()
            if users:
                return users[0]["Id"]
        except Exception as e:
            log.error("Jellyfin get_user_id error: %s", e)
        return ""

    def get_movies(self) -> list[dict]:
        try:
            user_id = self.get_user_id()
            endpoint = f"{self.base}/Users/{user_id}/Items" if user_id else f"{self.base}/Items"
            r = requests.get(
                endpoint,
                params={
                    "IncludeItemTypes": "Movie",
                    "Recursive":        "true",
                    "Fields":           "Path,Overview,ProviderIds,ProductionYear,CommunityRating,Genres",
                    "SortBy":           "SortName",
                    "SortOrder":        "Ascending",
                },
                headers=self.headers,
                timeout=15,
            )
            r.raise_for_status()
            return r.json().get("Items", [])
        except Exception as e:
            log.error("Jellyfin get_movies error: %s", e)
            return []

    def delete_item(self, item_id: str) -> bool:
        try:
            r = requests.delete(
                f"{self.base}/Items/{item_id}",
                headers=self.headers,
                timeout=10,
            )
            return r.status_code in (200, 204)
        except Exception as e:
            log.error("Jellyfin delete error for %s: %s", item_id, e)
            return False

    def get_poster_url(self, item_id: str) -> str:
        return f"{self.base}/Items/{item_id}/Images/Primary?maxHeight=300&api_key={Config.JELLYFIN_API_KEY}"

# ── Einthusan client ─────────────────────────────────────────────────────────
class EinthusanClient:
    def __init__(self):
        self.session = requests.Session()
        self.session.headers.update({
            "User-Agent": (
                "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 "
                "(KHTML, like Gecko) Chrome/124.0 Safari/537.36"
            ),
            "Referer": Config.EINTHUSAN_BASE,
        })
        self.logged_in = False

    CDN_FALLBACK_HOSTS = ["cdn1.einthusan.io", "cdn2.einthusan.io", "cdn3.einthusan.io"]

    def _cdn_hosts_from_page(self, html: str) -> list[str]:
        """The player page carries a base64 data-ejpingables attribute listing the
        real CDN hosts (cdnN.einthusan.io). Parse it, then append the known
        fallbacks so we always have candidates."""
        hosts = []
        m = re.search(r'data-ejpingables="([^"]+)"', html or "")
        if m:
            try:
                decoded = base64.b64decode(m.group(1) + "===").decode("utf-8", "ignore")
                hosts = re.findall(r'cdn\d+\.einthusan\.io', decoded)
            except Exception as e:
                log.warning("Could not decode data-ejpingables: %s", e)
        out = []
        for h in hosts + self.CDN_FALLBACK_HOSTS:
            if h not in out:
                out.append(h)
        return out

    def _resolve_cdn_url(self, url: str, page_html: str = "", referer: str = "") -> str:
        """data-mp4-link points at a decoy IP that refuses connections. The file
        lives on one of the cdnN.einthusan.io hosts - and not always the same one
        (UHD files are often only on cdn2) - so probe each candidate with a 1-byte
        range request and return the first that actually serves the file."""
        m = re.match(r'https?://[^/]+(/etv/content/.*)', url)
        if not m:
            log.info("CDN resolve: URL not in /etv/content/ form, leaving as-is: %s", url)
            return url
        path = m.group(1)
        for host in self._cdn_hosts_from_page(page_html):
            candidate = f"https://{host}{path}"
            try:
                r = self.session.get(candidate, timeout=10, stream=True,
                                     headers={"Range": "bytes=0-0",
                                              "Referer": referer or Config.EINTHUSAN_BASE})
                ctype = r.headers.get("Content-Type", "")
                r.close()
                if r.status_code in (200, 206) and "text/html" not in ctype:
                    log.info("CDN probe: %s -> HTTP %s %s (selected)", host, r.status_code, ctype)
                    return candidate
                log.info("CDN probe: %s -> HTTP %s %s (rejected)", host, r.status_code, ctype)
            except Exception as e:
                log.info("CDN probe: %s -> error: %s", host, e)
        log.warning("No CDN host serves %s - defaulting to cdn1 (download will likely fail)", path)
        return f"https://cdn1.einthusan.io{path}"

    def login(self) -> bool:
        if not Config.EINTHUSAN_EMAIL or not Config.EINTHUSAN_PASSWORD:
            log.error("Einthusan credentials not set.")
            return False
        try:
            r        = self.session.get(f"{Config.EINTHUSAN_BASE}/login/?lang=hindi")
            r.raise_for_status()
            soup     = BeautifulSoup(r.text, "html.parser")
            html_tag = soup.find("html")
            page_id  = html_tag.get("data-pageid", "") if html_tag else ""
            if not page_id:
                log.error("Could not find data-pageid on login page.")
                return False
            payload = {
                "xEvent":             "Login",
                "xJson":              json.dumps({"Email": Config.EINTHUSAN_EMAIL, "Password": Config.EINTHUSAN_PASSWORD}),
                "arcVersion":         "12",
                "appVersion":         "355",
                "tabID":              page_id,
                "gorilla.csrf.Token": page_id,
            }
            self.session.headers.update({
                "Referer":           f"{Config.EINTHUSAN_BASE}/login/?lang=hindi",
                "Content-Type":      "application/x-www-form-urlencoded",
                "X-Requested-With":  "XMLHttpRequest",
            })
            r2    = self.session.post(f"{Config.EINTHUSAN_BASE}/ajax/login/?lang=hindi", data=payload, timeout=15)
            data  = r2.json()
            event = data.get("Event", "")
            if event in ("redirect", "Redirect"):
                self.logged_in = True
                log.info("Logged in to Einthusan as %s", Config.EINTHUSAN_EMAIL)
                return True
            log.error("Login failed. Response: %s", data)
            return False
        except Exception as e:
            log.error("Login error: %s", e)
            return False

    def search(self, title: str, language: str = None) -> list[dict]:
        lang = language or Config.LANGUAGE
        try:
            url = f"{Config.EINTHUSAN_BASE}/movie/results/?lang={lang}&query={quote_plus(title)}"
            self.session.headers.update({"Referer": f"{Config.EINTHUSAN_BASE}/"})
            r    = self.session.get(url, timeout=15)
            r.raise_for_status()
            soup = BeautifulSoup(r.text, "html.parser")
            results = []
            for item in soup.select("#UIMovieSummary ul li"):
                try:
                    title_tag   = item.select_one("a.title")
                    if not title_tag:
                        continue
                    movie_title = title_tag.find("h3")
                    movie_title = movie_title.get_text(strip=True) if movie_title else title_tag.get_text(strip=True)
                    href        = title_tag.get("href", "")
                    info        = item.select_one(".info p")
                    year        = info.get_text(strip=True).split("\n")[0] if info else ""
                    year        = re.sub(r'[^\d]', '', year[:4]) if year else ""
                    img         = item.select_one(".block1 img")
                    thumb       = img["src"] if img else ""
                    if thumb.startswith("//"):
                        thumb = "https:" + thumb
                    # Detect UHD availability from search result badges
                    uhd = bool(item.select_one("i.ultrahd"))
                    hd  = bool(item.select_one("i.hd"))
                    results.append({
                        "title": movie_title, "year": year,
                        "url":   urljoin(Config.EINTHUSAN_BASE, href),
                        "thumb": thumb, "language": lang,
                        "uhd": uhd, "hd": hd,
                    })
                except Exception as ex:
                    log.warning("Error parsing result: %s", ex)
            log.info("Search '%s' returned %d results", title, len(results))
            return results
        except Exception as e:
            log.error("Search error for '%s': %s", title, e)
            return []

    def get_download_url(self, movie_url: str, prefer_uhd: bool = True) -> Optional[str]:
        try:
            self.session.headers.update({"Referer": Config.EINTHUSAN_BASE, "Content-Type": "text/html"})
            r     = self.session.get(movie_url, timeout=15)
            r.raise_for_status()
            match = re.search(r'/movie/watch/([^/?]+)', movie_url)
            if not match:
                return None
            movie_id    = match.group(1)
            lang_match  = re.search(r'lang=([^&]+)', movie_url)
            lang        = lang_match.group(1) if lang_match else Config.LANGUAGE

            # Check if UHD is available on the movie page
            soup_check = BeautifulSoup(r.text, "html.parser")
            has_uhd = bool(soup_check.select_one("i.ultrahd"))
            log.info("get_download_url: movie_id=%s lang=%s prefer_uhd=%s uhd_badge_on_page=%s",
                     movie_id, lang, prefer_uhd, has_uhd)

            # Try UHD first if available and preferred
            if prefer_uhd and has_uhd:
                try:
                    uhd_url = f"{Config.EINTHUSAN_BASE}/premium/movie/watch/{movie_id}/?lang={lang}&uhd=true"
                    log.info("UHD path: fetching %s", uhd_url)
                    self.session.headers.update({"Referer": movie_url})
                    r_uhd = self.session.get(uhd_url, timeout=15)
                    if r_uhd.status_code == 200 and "UIVideoPlayer" in r_uhd.text:
                        soup_uhd = BeautifulSoup(r_uhd.text, "html.parser")
                        mp4_tag = soup_uhd.find(attrs={"data-mp4-link": True})
                        if mp4_tag:
                            log.info("UHD path: raw data-mp4-link = %s", mp4_tag["data-mp4-link"])
                            final = self._resolve_cdn_url(mp4_tag["data-mp4-link"],
                                                          page_html=r_uhd.text, referer=uhd_url)
                            log.info("UHD path: final download URL = %s (is_uhd=True)", final)
                            return final, True
                        log.warning("UHD path: player page has no data-mp4-link - falling back to HD")
                    else:
                        log.warning("UHD path: page status=%s player_present=%s - falling back to HD",
                                    r_uhd.status_code, "UIVideoPlayer" in r_uhd.text)
                except Exception as ue:
                    log.warning("UHD fetch failed, falling back to HD: %s", ue)
            elif prefer_uhd:
                log.info("No UltraHD badge on movie page - using HD source")

            # Fall back to standard HD
            premium_url = f"{Config.EINTHUSAN_BASE}/premium/movie/watch/{movie_id}/?lang={lang}"
            log.info("HD path: fetching %s", premium_url)
            self.session.headers.update({"Referer": movie_url})
            r2   = self.session.get(premium_url, timeout=15)
            r2.raise_for_status()
            soup = BeautifulSoup(r2.text, "html.parser")
            mp4_tag = soup.find(attrs={"data-mp4-link": True})
            if mp4_tag:
                log.info("HD path: raw data-mp4-link = %s", mp4_tag["data-mp4-link"])
                final = self._resolve_cdn_url(mp4_tag["data-mp4-link"],
                                              page_html=r2.text, referer=premium_url)
                log.info("HD path: final download URL = %s (is_uhd=False)", final)
                return final, False
            hls_tag = soup.find(attrs={"data-hls-link": True})
            if hls_tag:
                log.info("HD path: no mp4 link, using HLS link %s", hls_tag["data-hls-link"])
                final = self._resolve_cdn_url(hls_tag["data-hls-link"],
                                              page_html=r2.text, referer=premium_url)
                log.info("HD path: final download URL = %s (is_uhd=False, hls)", final)
                return final, False
            log.error("No mp4 or hls link found on premium page for movie %s", movie_id)
            return None
        except Exception as e:
            log.error("Error fetching download URL: %s", e)
            return None

    def download(self, dl_id: str, title: str, download_url: str,
                 year: str = "", tmdb_id: Optional[int] = None,
                 resume_from: int = 0) -> str:
        folder, filename = build_movie_filename(title, year, tmdb_id)
        folder.mkdir(parents=True, exist_ok=True)
        set_permissions(str(folder))
        downloads[dl_id].update({
            "status":       "downloading",
            "filename":     str(filename),
            "folder":       str(folder),
            "download_url": download_url,
            "tmdb_id":      tmdb_id,
            "started":      datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
        })
        if resume_from == 0:
            log_activity("download", title, f"Starting → {filename.name}", "pending")
        else:
            log_activity("download", title, f"Resuming from {resume_from/1e6:.1f} MB", "pending")
        log.info("download: '%s' streaming from %s", title, download_url)
        try:
            headers = {}
            if resume_from > 0:
                headers["Range"] = f"bytes={resume_from}-"
            with self.session.get(download_url, stream=True, timeout=60, headers=headers) as r:
                r.raise_for_status()
                total_from_header            = int(r.headers.get("content-length", 0))
                total                        = total_from_header + resume_from
                downloads[dl_id]["total_mb"] = round(total / 1e6, 1)
                mode       = "ab" if resume_from > 0 else "wb"
                downloaded = resume_from
                with open(filename, mode) as f:
                    for chunk in r.iter_content(chunk_size=1024 * 256):
                        if cancel_flags.get(dl_id):
                            log_activity("download", title, "Download cancelled", "error")
                            return "cancelled"
                        if pause_flags.get(dl_id):
                            downloads[dl_id].update({
                                "status":    "paused",
                                "paused_at": downloaded,
                                "size_mb":   round(downloaded / 1e6, 1),
                                "progress":  int((downloaded / total) * 100) if total else 0,
                            })
                            log_activity("download", title, f"Paused at {downloaded/1e6:.1f} MB", "info")
                            return "paused"
                        if chunk:
                            f.write(chunk)
                            downloaded += len(chunk)
                            progress = int((downloaded / total) * 100) if total else 0
                            downloads[dl_id]["size_mb"]  = round(downloaded / 1e6, 1)
                            downloads[dl_id]["progress"] = progress
            set_permissions(str(filename))
            set_permissions(str(folder))
            request_id = downloads[dl_id].get("request_id")
            if request_id:
                completed_request_ids.add(request_id)
            downloads[dl_id].update({
                "status":   "completed",
                "progress": 100,
                "size_mb":  round(downloaded / 1e6, 1),
                "finished": datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
            })
            log_activity("download", title,
                         f"Downloaded {downloads[dl_id]['size_mb']} MB → {filename}", "success")
            return "completed"
        except Exception as e:
            downloads[dl_id].update({"status": "error", "error": str(e)})
            log_activity("download", title, f"Download failed: {e}", "error")
            return "error"


# ── Jellyseerr client ────────────────────────────────────────────────────────
class SeerrClient:
    def __init__(self):
        self.base    = Config.SEERR_URL.rstrip("/")
        self.headers = {"X-Api-Key": Config.SEERR_API_KEY, "Content-Type": "application/json"}

    def get_all_requests(self) -> list[dict]:
        try:
            r = requests.get(f"{self.base}/api/v1/request?take=100&sort=added", headers=self.headers, timeout=10)
            r.raise_for_status()
            return r.json().get("results", [])
        except Exception as e:
            log.error("Seerr fetch error: %s", e)
            return []

    def get_pending_requests(self) -> list[dict]:
        try:
            r = requests.get(f"{self.base}/api/v1/request?filter=pending&take=50&sort=added", headers=self.headers, timeout=10)
            r.raise_for_status()
            return r.json().get("results", [])
        except Exception as e:
            log.error("Seerr fetch error: %s", e)
            return []

    def mark_available(self, request_id: int):
        try:
            requests.post(f"{self.base}/api/v1/request/{request_id}/available", headers=self.headers, timeout=10)
        except Exception as e:
            log.warning("Could not mark request %d available: %s", request_id, e)

    def delete_request_by_tmdb(self, tmdb_id: int) -> bool:
        """Find and delete a Seerr request matching a TMDB ID."""
        try:
            reqs = self.get_all_requests()
            for req in reqs:
                media = req.get("media", {})
                if media.get("tmdbId") == tmdb_id:
                    rid = req.get("id")
                    r = requests.delete(
                        f"{self.base}/api/v1/request/{rid}",
                        headers=self.headers,
                        timeout=10,
                    )
                    if r.status_code in (200, 204):
                        log.info("Deleted Seerr request #%s for tmdb-%s", rid, tmdb_id)
                        return True
            log.warning("No Seerr request found for tmdb-%s", tmdb_id)
            return False
        except Exception as e:
            log.error("Seerr delete error: %s", e)
            return False


# ── Smart title matching ──────────────────────────────────────────────────────
def match_title(request_title: str, request_year: str,
                results: list[dict],
                original_title: str = "") -> Optional[dict]:
    if not results:
        return None

    def normalize(s: str) -> str:
        return re.sub(r'[^\w\s]', '', s.lower()).strip()

    rl  = normalize(request_title)
    rl2 = normalize(original_title) if original_title else ""
    ry  = request_year.strip() if request_year else ""

    log.info("Matching '%s' (%s) against %d results", request_title, ry, len(results))

    def title_score(res):
        rt = normalize(res["title"])
        if rt == rl:                           return 100
        if rl2 and rt == rl2:                  return 90
        if rl in rt or rt in rl:               return 50
        if rl2 and (rl2 in rt or rt in rl2):   return 40
        return 10

    if ry:
        year_matches = [r for r in results if r.get("year", "").strip() == ry]
        if year_matches:
            log.info("Found %d year-exact matches for %s", len(year_matches), ry)
            best = max(year_matches, key=title_score)
            log.info("Year-match winner: '%s' (%s)", best["title"], best.get("year"))
            return best

        close_matches = [r for r in results
                         if r.get("year","").strip()
                         and abs(int(ry) - int(r["year"].strip())) <= 1]
        if close_matches:
            best = max(close_matches, key=title_score)
            log.info("Close-year winner: '%s' (%s)", best["title"], best.get("year"))
            return best

        log.warning("No year match found for %s — falling back to title only", ry)

    best = max(results, key=title_score)
    log.info("Title-only fallback winner: '%s' (%s)", best["title"], best.get("year"))
    return best


# ── Watcher ───────────────────────────────────────────────────────────────────
einthusan = EinthusanClient()
seerr     = SeerrClient()
jellyfin  = JellyfinClient()


def run_download(dl_id: str, title: str, dl_url: str,
                 year: str = "", tmdb_id: Optional[int] = None,
                 resume_from: int = 0):
    log.info("run_download %s: '%s' url=%s", dl_id, title, dl_url)
    while True:
        result = einthusan.download(dl_id, title, dl_url, year=year, tmdb_id=tmdb_id, resume_from=resume_from)
        if result == "paused":
            while pause_flags.get(dl_id) and not cancel_flags.get(dl_id):
                time.sleep(0.5)
            if cancel_flags.get(dl_id):
                cleanup_download(dl_id)
                log_activity("download", title, "Cancelled after pause", "error")
                break
            resume_from                = downloads[dl_id].get("paused_at", 0)
            pause_flags[dl_id]         = False
            downloads[dl_id]["status"] = "downloading"
            log_activity("download", title, f"Resuming from {resume_from/1e6:.1f} MB", "info")
            continue
        if result == "cancelled":
            cleanup_download(dl_id)
            break
        break


def approve_and_download(title: str, year: str,
                         original_title: str = "",
                         request_id: Optional[int] = None):
    log_activity("seerr", title, f"Searching Einthusan for '{title}' ({year})…", "info")
    search_titles = [title]
    if original_title and original_title.lower() != title.lower():
        search_titles.append(original_title)
    all_results = []
    for t in search_titles:
        results = einthusan.search(t)
        if results:
            all_results.extend(results)
            log_activity("search", title, f"Found {len(results)} results for '{t}'", "info")
    seen, deduped = set(), []
    for r in all_results:
        if r["url"] not in seen:
            seen.add(r["url"])
            deduped.append(r)
    if not deduped:
        log_activity("search", title, "Not found on Einthusan", "error")
        return False
    best = match_title(title, year, deduped, original_title=original_title)
    if not best:
        log_activity("search", title, "No suitable match found", "error")
        return False
    log_activity("search", title, f"Matched: '{best['title']}' ({best.get('year','?')})", "info")
    dl_url_result = einthusan.get_download_url(best["url"])
    dl_url = dl_url_result[0] if isinstance(dl_url_result, tuple) else dl_url_result
    is_uhd = dl_url_result[1] if isinstance(dl_url_result, tuple) else False
    if not dl_url:
        log_activity("download", title, "Could not find download link", "error")
        return False
    tmdb  = get_tmdb_info(best["title"], best.get("year", year))
    dl_id = new_download_id()
    downloads[dl_id] = {
        "id": dl_id, "title": tmdb["title"], "year": tmdb["year"],
        "tmdb_id": tmdb["tmdb_id"], "status": "queued",
        "progress": 0, "size_mb": 0, "total_mb": 0,
        "queued": datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
        "request_id": request_id,
    }
    cancel_flags[dl_id] = False
    pause_flags[dl_id]  = False
    log_activity("download", tmdb["title"],
                 f"TMDB: {tmdb['title']} ({tmdb['year']}) tmdb-{tmdb['tmdb_id']}", "info")
    threading.Thread(
        target=run_download,
        args=(dl_id, tmdb["title"], dl_url),
        kwargs={"year": tmdb["year"], "tmdb_id": tmdb["tmdb_id"]},
        daemon=True,
    ).start()
    if request_id:
        approved_request_ids.add(request_id)
    return True


def process_request(req: dict):
    rid        = req.get("id")
    media      = req.get("media", {})
    media_type = media.get("mediaType", req.get("type", "movie"))
    if media_type != "movie":
        return
    tmdb_id = media.get("tmdbId")
    if tmdb_id and Config.TMDB_API_KEY:
        details        = get_tmdb_details_by_id(tmdb_id, "movie")
        title          = details["title"]
        year           = details["year"]
        original_title = details.get("original_title", "")
    else:
        title = "Unknown"
        year  = ""
        original_title = ""
    log_activity("seerr", title, f"New request #{rid} detected", "info")
    approve_and_download(title, year, original_title=original_title, request_id=rid)
    known_request_ids.add(rid)


def watcher_loop():
    while not einthusan.login():
        log_activity("system", "EinthuBot", "Login failed — retrying in 60s", "error")
        time.sleep(60)
    log_activity("system", "EinthuBot",
                 f"Watcher started. Polling every {Config.POLL_INTERVAL}s", "success")
    while True:
        try:
            # 1. Check for new pending requests
            reqs = seerr.get_pending_requests()
            for req in reqs:
                rid = req.get("id")
                if rid not in known_request_ids and rid not in approved_request_ids:
                    process_request(req)

            # 2. Check all Seerr requests and sync status with Jellyfin
            all_reqs = seerr.get_all_requests()
            jf_movies = jellyfin.get_movies()
            jf_tmdb_ids = set()
            for m in jf_movies:
                tmdb_id = m.get("ProviderIds", {}).get("Tmdb")
                if tmdb_id:
                    jf_tmdb_ids.add(int(tmdb_id))

            for req in all_reqs:
                rid    = req.get("id")
                media  = req.get("media", {})
                tmdb_id = media.get("tmdbId")
                status  = media.get("status")  # 3=partial, 4=available, 5=processing

                # If movie is now in Jellyfin but Seerr doesn't know yet
                if tmdb_id and int(tmdb_id) in jf_tmdb_ids:
                    # Mark as completed in our tracking
                    if rid not in completed_request_ids:
                        completed_request_ids.add(rid)
                        log.info("Auto-completed request %d (tmdb-%s found in Jellyfin)", rid, tmdb_id)
                    # Tell Seerr it's available if not already
                    if status not in (3, 4, 5):
                        seerr.mark_available(rid)
                        log_activity("seerr", "Jellyseerr",
                                     f"Marked available: tmdb-{tmdb_id}", "success")

        except Exception as e:
            log_activity("system", "EinthuBot", f"Watcher error: {e}", "error")
        time.sleep(Config.POLL_INTERVAL)


# ── Flask Web UI ──────────────────────────────────────────────────────────────
app = Flask(__name__)
app.config["TEMPLATES_AUTO_RELOAD"] = True
CORS(app)

@app.route("/")
def index():
    return render_template("index.html")

@app.route("/api/activity")
def api_activity():
    return jsonify(activity_log[:100])

@app.route("/api/downloads")
def api_downloads():
    return jsonify(list(downloads.values()))

@app.route("/api/library")
def api_library():
    """Return all movies in Jellyfin library with TMDB poster paths."""
    try:
        movies = jellyfin.get_movies()
        result = []
        for m in movies:
            item_id = m.get("Id", "")
            tmdb_id = m.get("ProviderIds", {}).get("Tmdb")
            path    = m.get("Path", "")
            folder  = str(Path(path).parent) if path else ""

            rating   = m.get("CommunityRating")
            overview = (m.get("Overview") or "")[:300]
            tmdb_poster, tmdb_backdrop, tmdb_genres, rating, overview = fetch_tmdb_artwork(
                tmdb_id, "movie", rating, overview)

            result.append({
                "id":           item_id,
                "title":        m.get("Name", "Unknown"),
                "year":         m.get("ProductionYear", ""),
                "tmdb_id":      int(tmdb_id) if tmdb_id else None,
                "poster":       jellyfin.get_poster_url(item_id),
                "tmdb_poster":  tmdb_poster,
                "tmdb_backdrop": tmdb_backdrop,
                "path":         path,
                "folder":       folder,
                "rating":       rating,
                "overview":     overview,
                "genres":       tmdb_genres or ", ".join(m.get("Genres", [])),
            })
        return jsonify(result)
    except Exception as e:
        log.error("api_library error: %s", e)
        return jsonify({"error": str(e)}), 500

@app.route("/api/library/delete", methods=["POST"])
def api_library_delete():
    """
    Delete a movie from Jellyfin, delete files from disk,
    and optionally clear the Seerr request.
    """
    data          = flask_request.get_json(force=True)
    item_id       = data.get("item_id", "")
    folder        = data.get("folder", "")
    tmdb_id       = data.get("tmdb_id")
    title         = data.get("title", "")
    delete_seerr  = data.get("delete_seerr", True)

    if not item_id:
        return jsonify({"ok": False, "error": "No item_id"}), 400

    results = {}

    # 1. Delete from Jellyfin
    jf_ok = jellyfin.delete_item(item_id)
    results["jellyfin"] = jf_ok
    if jf_ok:
        log_activity("library", title, "Removed from Jellyfin library", "info")
    else:
        log_activity("library", title, "Failed to remove from Jellyfin", "error")

    # 2. Delete files from disk
    if folder:
        try:
            folder_path = Path(folder)
            if folder_path.exists():
                shutil.rmtree(str(folder_path))
                results["files"] = True
                log_activity("library", title, f"Deleted folder: {folder}", "info")
            else:
                results["files"] = False
                log_activity("library", title, "Folder not found on disk", "error")
        except Exception as e:
            results["files"] = False
            log_activity("library", title, f"Failed to delete files: {e}", "error")
    else:
        results["files"] = False

    # 3. Clear Seerr request
    if delete_seerr and tmdb_id:
        seerr_ok = seerr.delete_request_by_tmdb(tmdb_id)
        results["seerr"] = seerr_ok
        if seerr_ok:
            log_activity("library", title, "Cleared Seerr request", "success")
        else:
            log_activity("library", title, "No matching Seerr request found", "info")

    log_activity("library", title, "Delete complete", "success" if jf_ok else "error")
    return jsonify({"ok": jf_ok, "results": results})

@app.route("/api/requests")
def api_requests():
    try:
        reqs   = seerr.get_all_requests()
        result = []
        for req in reqs:
            media      = req.get("media", {})
            tmdb_id    = media.get("tmdbId")
            media_type = media.get("mediaType", req.get("type", "movie"))
            details    = get_tmdb_details_by_id(tmdb_id, media_type)
            title      = details["title"]
            year       = details["year"]
            poster     = details["poster"]
            # req.status:   1=pending, 2=approved, 3=declined, 4=partially_available, 5=available
            # media.status: 1=unknown, 2=pending,  3=processing, 4=partially_available, 5=available
            req_status   = req.get("status", 1)
            media_status = req.get("media", {}).get("status", 1)
            # media_status takes full priority — check partial BEFORE available
            if media_status == 4:
                status = "partial"
            elif media_status == 5:
                status = "available"
            elif media_status == 3:
                status = "processing"
            elif req_status == 5:
                status = "available"
            elif req_status == 4:
                status = "partial"
            elif req_status == 3:
                status = "declined"
            elif req_status == 2:
                status = "approved"
            else:
                status = "pending"
            raw_status = req_status
            rid                 = req.get("id")
            einthubot_approved  = rid in approved_request_ids
            einthubot_completed = rid in completed_request_ids
            matching_dl         = None
            matching_status     = None
            for dl in downloads.values():
                if dl.get("request_id") == rid:
                    matching_dl     = dl.get("id")
                    matching_status = dl.get("status")
                    break
            result.append({
                "id": rid, "title": title, "year": year, "poster": poster,
                "tmdb_id": tmdb_id, "status": status, "raw_status": raw_status,
                "media_type": media_type,
                "requested_by": req.get("requestedBy", {}).get("displayName", ""),
                "created_at": req.get("createdAt", ""),
                "einthubot_approved":  einthubot_approved,
                "einthubot_completed": einthubot_completed,
                "download_id":         matching_dl,
                "download_status":     matching_status,
                "original_title":      details.get("original_title", ""),
            })
        return jsonify(result)
    except Exception as e:
        log.error("api_requests error: %s", e)
        return jsonify({"error": str(e)}), 500

@app.route("/api/approve", methods=["POST"])
def api_approve():
    data           = flask_request.get_json(force=True)
    title          = data.get("title", "")
    year           = data.get("year", "")
    original_title = data.get("original_title", "")
    request_id     = data.get("request_id")
    if not title:
        return jsonify({"ok": False, "error": "No title"}), 400
    if request_id in approved_request_ids:
        return jsonify({"ok": False, "error": "Already approved"}), 400
    threading.Thread(
        target=approve_and_download,
        args=(title, year),
        kwargs={"original_title": original_title, "request_id": request_id},
        daemon=True,
    ).start()
    return jsonify({"ok": True})

@app.route("/api/search")
def api_search():
    q    = flask_request.args.get("q", "").strip()
    lang = flask_request.args.get("lang", Config.LANGUAGE)
    if not q:
        return jsonify({"results": [], "error": "No query"})
    if not einthusan.logged_in:
        einthusan.login()
    results = einthusan.search(q, language=lang)
    return jsonify({"results": results})

@app.route("/api/download", methods=["POST"])
def api_download():
    data      = flask_request.get_json(force=True)
    title     = data.get("title", "")
    movie_url = data.get("url", "")
    year      = data.get("year", "")
    if not movie_url:
        return jsonify({"ok": False, "error": "No URL provided"}), 400
    dl_id = new_download_id()
    downloads[dl_id] = {
        "id": dl_id, "title": title, "year": year,
        "status": "queued", "progress": 0, "size_mb": 0, "total_mb": 0,
        "queued": datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
    }
    cancel_flags[dl_id] = False
    pause_flags[dl_id]  = False
    def _do():
        dl_url_result = einthusan.get_download_url(movie_url)
        dl_url = dl_url_result[0] if isinstance(dl_url_result, tuple) else dl_url_result
        is_uhd = dl_url_result[1] if isinstance(dl_url_result, tuple) else False
        if not dl_url:
            downloads[dl_id]["status"] = "error"
            log_activity("download", title, "No download URL found", "error")
            return
        tmdb = get_tmdb_info(title, year)
        downloads[dl_id]["title"]   = tmdb["title"]
        downloads[dl_id]["year"]    = tmdb["year"]
        downloads[dl_id]["tmdb_id"] = tmdb["tmdb_id"]
        log_activity("download", tmdb["title"],
                     f"TMDB: {tmdb['title']} ({tmdb['year']}) tmdb-{tmdb['tmdb_id']}", "info")
        run_download(dl_id, tmdb["title"], dl_url, year=tmdb["year"], tmdb_id=tmdb["tmdb_id"])
    threading.Thread(target=_do, daemon=True).start()
    log_activity("download", title, "Queued for download", "pending")
    return jsonify({"ok": True, "dl_id": dl_id})

@app.route("/api/pause/<dl_id>", methods=["POST"])
def api_pause(dl_id):
    if dl_id not in downloads:
        return jsonify({"ok": False, "error": "Not found"}), 404
    if downloads[dl_id]["status"] != "downloading":
        return jsonify({"ok": False, "error": "Not downloading"}), 400
    pause_flags[dl_id] = True
    downloads[dl_id]["status"] = "pausing"
    return jsonify({"ok": True})

@app.route("/api/resume/<dl_id>", methods=["POST"])
def api_resume(dl_id):
    if dl_id not in downloads:
        return jsonify({"ok": False, "error": "Not found"}), 404
    if downloads[dl_id]["status"] != "paused":
        return jsonify({"ok": False, "error": "Not paused"}), 400
    pause_flags[dl_id] = False
    return jsonify({"ok": True})

@app.route("/api/cancel/<dl_id>", methods=["POST"])
def api_cancel(dl_id):
    if dl_id not in downloads:
        return jsonify({"ok": False, "error": "Not found"}), 404
    dl = downloads[dl_id]
    cancel_flags[dl_id] = True
    pause_flags[dl_id]  = False
    cleanup_download(dl_id)
    log_activity("download", dl.get("title", ""), "Cancelled and removed", "error")
    return jsonify({"ok": True})

@app.route("/api/remove/<dl_id>", methods=["POST"])
def api_remove(dl_id):
    data        = flask_request.get_json(force=True) or {}
    delete_file = data.get("delete_file", False)
    if dl_id not in downloads:
        return jsonify({"ok": False, "error": "Not found"}), 404
    dl = downloads[dl_id]
    cancel_flags[dl_id] = True
    pause_flags[dl_id]  = False
    if delete_file:
        log_activity("download", dl.get("title", ""), "File deleted", "info")
    cleanup_download(dl_id, delete_files=delete_file)
    return jsonify({"ok": True})

@app.route("/api/status")
def api_status():
    return jsonify({
        "logged_in":      einthusan.logged_in,
        "seerr_url":      Config.SEERR_URL,
        "download_dir":   Config.DOWNLOAD_DIR,
        "poll_interval":  Config.POLL_INTERVAL,
        "language":       Config.LANGUAGE,
        "tmdb_enabled":   bool(Config.TMDB_API_KEY),
        "jellyfin_enabled": bool(Config.JELLYFIN_API_KEY),
        "activity_count": len(activity_log),
        "tmdb_api_key": Config.TMDB_API_KEY,
    })

@app.route("/api/retry_login", methods=["POST"])
def api_retry_login():
    ok = einthusan.login()
    return jsonify({"ok": ok})


@app.route("/partials/<name>")
def partial(name):
    allowed = {"home", "search", "requests", "downloads", "library", "activity", "settings"}
    if name not in allowed:
        return "Not found", 404
    return render_template(f"partials/{name}.html")

@app.route("/api/settings", methods=["GET"])
def api_settings_get():
    """Return current settings from environment (safe — masks secrets)."""
    return jsonify({
        "einthusan_email":    Config.EINTHUSAN_EMAIL,
        "einthusan_password": "••••••••" if Config.EINTHUSAN_PASSWORD else "",
        "einthusan_language": Config.LANGUAGE,
        "download_dir":       Config.DOWNLOAD_DIR,
        "poll_interval":      Config.POLL_INTERVAL,
        "jellyfin_url":       Config.JELLYFIN_URL,
        "jellyfin_api_key":   "••••••••" if Config.JELLYFIN_API_KEY else "",
        "seerr_url":          Config.SEERR_URL,
        "seerr_api_key":      "••••••••" if Config.SEERR_API_KEY else "",
        "tmdb_api_key":       "••••••••" if Config.TMDB_API_KEY else "",
    })


@app.route("/api/settings", methods=["POST"])
def api_settings_save():
    """Save settings to .env file."""
    data = flask_request.get_json(force=True) or {}

    env_path = Path("/app/.env")
    # Also check the mounted location
    if not env_path.exists():
        env_path = Path(".env")

    # Read existing .env if it exists
    existing = {}
    if env_path.exists():
        for line in env_path.read_text().splitlines():
            line = line.strip()
            if line and not line.startswith("#") and "=" in line:
                k, _, v = line.partition("=")
                existing[k.strip()] = v.strip()

    # Map of field name -> env var name
    field_map = {
        "einthusan_email":    "EINTHUSAN_EMAIL",
        "einthusan_password": "EINTHUSAN_PASSWORD",
        "einthusan_language": "EINTHUSAN_LANGUAGE",
        "download_dir":       "DOWNLOAD_DIR",
        "poll_interval":      "POLL_INTERVAL",
        "jellyfin_url":       "JELLYFIN_URL",
        "jellyfin_api_key":   "JELLYFIN_API_KEY",
        "seerr_url":          "SEERR_URL",
        "seerr_api_key":      "SEERR_API_KEY",
        "tmdb_api_key":       "TMDB_API_KEY",
    }

    # Update values — skip masked placeholder values
    for field, env_key in field_map.items():
        val = data.get(field, "")
        if val and val != "••••••••":
            existing[env_key] = str(val)

    # Write back to .env
    lines = ["# EinthuBot configuration - auto-generated by Settings UI\n"]
    sections = {
        "Einthusan Premium Account": ["EINTHUSAN_EMAIL", "EINTHUSAN_PASSWORD", "EINTHUSAN_LANGUAGE"],
        "Jellyseerr":                ["SEERR_URL", "SEERR_API_KEY"],
        "Jellyfin":                  ["JELLYFIN_URL", "JELLYFIN_API_KEY"],
        "TMDB":                      ["TMDB_API_KEY"],
        "Download Settings":         ["DOWNLOAD_DIR", "POLL_INTERVAL"],
        "Web UI":                    ["WEB_PORT"],
    }
    written = set()
    for section, keys in sections.items():
        lines.append(f"\n# ── {section} ──────────────────────────────────\n")
        for key in keys:
            if key in existing:
                lines.append(f"{key}={existing[key]}\n")
                written.add(key)
    # Write any remaining keys
    remaining = {k: v for k, v in existing.items() if k not in written}
    if remaining:
        lines.append("\n# ── Other ──────────────────────────────────\n")
        for k, v in remaining.items():
            lines.append(f"{k}={v}\n")

    try:
        env_path.write_text("".join(lines))
        log.info("Settings saved to %s", env_path)
        log_activity("system", "EinthuBot", "Settings saved via Web UI", "success")
        return jsonify({"ok": True, "path": str(env_path)})
    except Exception as e:
        log.error("Failed to save .env: %s", e)
        return jsonify({"ok": False, "error": str(e)}), 500


@app.route("/api/settings/reveal", methods=["GET"])
def api_settings_reveal():
    """Return actual secret values — only call when user explicitly requests reveal."""
    return jsonify({
        "ok": True,
        "values": {
            "einthusan_password": Config.EINTHUSAN_PASSWORD,
            "jellyfin_api_key":   Config.JELLYFIN_API_KEY,
            "seerr_api_key":      Config.SEERR_API_KEY,
            "tmdb_api_key":       Config.TMDB_API_KEY,
        }
    })


@app.route("/api/shows")
def api_shows():
    """Return all TV shows in Jellyfin library."""
    try:
        user_id = jellyfin.get_user_id()
        r = requests.get(
            f"{jellyfin.base}/Users/{user_id}/Items",
            headers=jellyfin.headers,
            params={
                "IncludeItemTypes": "Series",
                "Recursive":        "true",
                "Fields":           "Path,Overview,ProviderIds,ProductionYear,CommunityRating,Genres",
                "SortBy":           "SortName",
                "SortOrder":        "Ascending",
            },
            timeout=15,
        )
        r.raise_for_status()
        shows = r.json().get("Items", [])
        result = []
        for s in shows:
            item_id  = s.get("Id", "")
            tmdb_id  = s.get("ProviderIds", {}).get("Tmdb")
            tvdb_id  = s.get("ProviderIds", {}).get("Tvdb")
            rating   = s.get("CommunityRating")
            overview = (s.get("Overview") or "")[:300]
            tmdb_poster, tmdb_backdrop, tmdb_genres, rating, overview = fetch_tmdb_artwork(
                tmdb_id, "tv", rating, overview)
            result.append({
                "id":           item_id,
                "title":        s.get("Name", "Unknown"),
                "year":         s.get("ProductionYear", ""),
                "tmdb_id":      int(tmdb_id) if tmdb_id else None,
                "tvdb_id":      tvdb_id,
                "poster":       jellyfin.get_poster_url(item_id),
                "tmdb_poster":  tmdb_poster,
                "tmdb_backdrop": tmdb_backdrop,
                "rating":       rating,
                "overview":     overview,
                "genres":       tmdb_genres or ", ".join(s.get("Genres", [])),
                "type":         "show",
            })
        return jsonify(result)
    except Exception as e:
        log.error("api_shows error: %s", e)
        return jsonify({"error": str(e)}), 500


@app.route("/api/shows/<show_id>/seasons")
def api_show_seasons(show_id):
    """Return seasons for a show."""
    try:
        user_id = jellyfin.get_user_id()
        r = requests.get(
            f"{jellyfin.base}/Shows/{show_id}/Seasons",
            headers=jellyfin.headers,
            params={"userId": user_id, "Fields": "Overview,PrimaryImageAspectRatio,ChildCount,RecursiveItemCount"},
            timeout=10,
        )
        r.raise_for_status()
        seasons = r.json().get("Items", [])
        result = []
        for s in seasons:
            sid = s.get("Id", "")
            result.append({
                "id":           sid,
                "name":         s.get("Name", ""),
                "index":        s.get("IndexNumber", 0),
                "overview":     (s.get("Overview") or "")[:200],
                "poster":       f"/api/proxy/image/{sid}",
                "episode_count": s.get("ChildCount") or s.get("RecursiveItemCount", 0),
            })
        return jsonify(result)
    except Exception as e:
        log.error("api_show_seasons error: %s", e)
        return jsonify({"error": str(e)}), 500


@app.route("/api/shows/<show_id>/seasons/<season_id>/episodes")
def api_season_episodes(show_id, season_id):
    """Return episodes for a season."""
    try:
        user_id = jellyfin.get_user_id()
        r = requests.get(
            f"{jellyfin.base}/Shows/{show_id}/Episodes",
            headers=jellyfin.headers,
            params={
                "seasonId": season_id,
                "userId":   user_id,
                "Fields":   "Overview,Path,RunTimeTicks,MediaSources",
            },
            timeout=10,
        )
        r.raise_for_status()
        episodes = r.json().get("Items", [])
        result = []
        for e in episodes:
            eid     = e.get("Id", "")
            runtime = e.get("RunTimeTicks", 0)
            mins    = int(runtime / 600000000) if runtime else 0
            result.append({
                "id":       eid,
                "name":     e.get("Name", ""),
                "index":    e.get("IndexNumber", 0),
                "overview": (e.get("Overview") or "")[:200],
                "runtime":  mins,
                "path":     e.get("Path", ""),
                "thumb":    f"/api/proxy/image/{eid}",
            })
        return jsonify(result)
    except Exception as e:
        log.error("api_season_episodes error: %s", e)
        return jsonify({"error": str(e)}), 500


@app.route("/api/shows/delete", methods=["POST"])
def api_show_delete():
    """Delete a show, season, or episode from Jellyfin and disk."""
    data    = flask_request.get_json(force=True)
    item_id = data.get("item_id", "")
    if not item_id:
        return jsonify({"ok": False, "error": "No item_id"}), 400
    try:
        ok = jellyfin.delete_item(item_id)
        if ok:
            log_activity("library", "Jellyfin", f"Deleted item {item_id}", "success")
        return jsonify({"ok": ok})
    except Exception as e:
        log.error("api_show_delete error: %s", e)
        return jsonify({"ok": False, "error": str(e)}), 500


@app.route("/api/proxy/image/<item_id>")
def proxy_image(item_id):
    """Proxy Jellyfin images so browser can access internal Docker URLs."""
    # Try Primary first, then Thumb, then Backdrop
    for image_type in ["Primary", "Thumb", "Backdrop"]:
        try:
            url = f"{jellyfin.base}/Items/{item_id}/Images/{image_type}"
            params = {"maxHeight": "400", "api_key": Config.JELLYFIN_API_KEY}
            r = requests.get(url, params=params, timeout=10)
            if r.status_code == 200:
                return Response(r.content, content_type=r.headers.get('Content-Type', 'image/jpeg'))
        except Exception:
            continue
    return Response(status=404)


@app.route("/api/storage")
def api_storage():
    """Calculate total disk usage of all media files in Jellyfin library."""
    try:
        user_id = jellyfin.get_user_id()
        total_bytes = 0

        # Get all movies
        r = requests.get(
            f"{jellyfin.base}/Users/{user_id}/Items",
            headers=jellyfin.headers,
            params={
                "IncludeItemTypes": "Movie",
                "Recursive": "true",
                "Fields": "Path,MediaSources",
            },
            timeout=15,
        )
        if r.status_code == 200:
            for item in r.json().get("Items", []):
                for src in item.get("MediaSources", []):
                    sz = src.get("Size", 0)
                    if sz:
                        total_bytes += sz

        # Get all episodes
        r2 = requests.get(
            f"{jellyfin.base}/Users/{user_id}/Items",
            headers=jellyfin.headers,
            params={
                "IncludeItemTypes": "Episode",
                "Recursive": "true",
                "Fields": "Path,MediaSources",
            },
            timeout=15,
        )
        if r2.status_code == 200:
            for item in r2.json().get("Items", []):
                for src in item.get("MediaSources", []):
                    sz = src.get("Size", 0)
                    if sz:
                        total_bytes += sz

        # Format size
        if total_bytes >= 1_099_511_627_776:
            size_str = f"{total_bytes/1_099_511_627_776:.1f} TB"
        elif total_bytes >= 1_073_741_824:
            size_str = f"{total_bytes/1_073_741_824:.1f} GB"
        elif total_bytes >= 1_048_576:
            size_str = f"{total_bytes/1_048_576:.1f} MB"
        else:
            size_str = f"{total_bytes/1024:.1f} KB"

        return jsonify({"ok": True, "bytes": total_bytes, "size": size_str})
    except Exception as e:
        log.error("api_storage error: %s", e)
        return jsonify({"ok": False, "size": "—", "bytes": 0}), 500


# ── Entry point ───────────────────────────────────────────────────────────────
if __name__ == "__main__":
    Path("logs").mkdir(exist_ok=True)
    Path(Config.DOWNLOAD_DIR).mkdir(parents=True, exist_ok=True)
    t = threading.Thread(target=watcher_loop, daemon=True)
    t.start()
    log.info("EinthuBot Web UI → http://0.0.0.0:%d", Config.WEB_PORT)
    app.run(host="0.0.0.0", port=Config.WEB_PORT, debug=False)