"""
Adhan Cast - plays the adhan on Google Cast speakers at Islamic prayer times.

Times come from your masjid's Masjidal schedule (adhan + iqama), with the
ISNA calculation as a fallback.

Run:   python app.py        (add --open to launch the dashboard in your browser)
Then:  http://localhost:8080
"""
from __future__ import annotations

import copy
import ctypes
import datetime as dt
import html as html_lib
import json
import logging
import math
import os
import re
import socket
import struct
import sys
import threading
import time
import urllib.request
import wave
import webbrowser
from collections import deque
from logging.handlers import RotatingFileHandler
from pathlib import Path
from urllib.parse import quote, urlencode
from zoneinfo import ZoneInfo

# pythonw.exe has no console; give libraries somewhere harmless to write.
if sys.stdout is None:
    sys.stdout = open(os.devnull, "w")
if sys.stderr is None:
    sys.stderr = open(os.devnull, "w")

from fastapi import Body, FastAPI, File, HTTPException, UploadFile
from fastapi.responses import FileResponse
from fastapi.staticfiles import StaticFiles

BASE = Path(__file__).resolve().parent
SOUNDS_DIR = BASE / "sounds"
STATIC_DIR = BASE / "static"
SETTINGS_FILE = BASE / "settings.json"
CACHE_FILE = BASE / "masjid_cache.json"
LOG_FILE = BASE / "adhan.log"
PID_FILE = BASE / "adhan.pid"
PORT = int(os.environ.get("ADHAN_PORT", "8080"))

PRAYERS = ["fajr", "dhuhr", "asr", "maghrib", "isha"]
NAMES = {"fajr": "Fajr", "dhuhr": "Dhuhr", "asr": "Asr", "maghrib": "Maghrib", "isha": "Isha", "jumuah": "Jumu'ah"}
FEED_KEYS = {"fajr": "fajr", "dhuhr": "zuhr", "asr": "asr", "maghrib": "maghrib", "isha": "isha"}
MASJIDAL_API = "https://masjidal.com/api/v1/time/range"
FEED_DAYS = 30
DEFAULT_ADHAN = "default-adhan-tone.wav"
DEFAULT_ALERT = "default-alert-chime.wav"
AUDIO_TYPES = {
    ".mp3": "audio/mpeg", ".m4a": "audio/mp4", ".aac": "audio/aac",
    ".wav": "audio/wav", ".ogg": "audio/ogg", ".flac": "audio/flac",
}
GRACE_SECONDS = 120  # an event may fire up to 2 min late; later than that it is logged as missed
TIME_RE = re.compile(r"^([01]?\d|2[0-3]):[0-5]\d$")
ID_RE = re.compile(r"^[A-Za-z0-9]{5,16}$")
MONTHS = {m: i for i, m in enumerate(
    ["jan", "feb", "mar", "apr", "may", "jun", "jul", "aug", "sep", "oct", "nov", "dec"], 1)}

SOUNDS_DIR.mkdir(exist_ok=True)

# ----------------------------------------------------------------- logging
RECENT: deque = deque(maxlen=60)


class _RecentHandler(logging.Handler):
    def emit(self, record):
        RECENT.appendleft({
            "t": dt.datetime.now().strftime("%b %d, %I:%M:%S %p"),
            "level": record.levelname,
            "msg": record.getMessage(),
        })


log = logging.getLogger("adhan")
log.setLevel(logging.INFO)
_fh = RotatingFileHandler(LOG_FILE, maxBytes=1_000_000, backupCount=2, encoding="utf-8")
_fh.setFormatter(logging.Formatter("%(asctime)s %(levelname)s %(message)s"))
log.addHandler(_fh)
log.addHandler(_RecentHandler())
log.addHandler(logging.StreamHandler(sys.stdout))


# ------------------------------------------------- prayer time calculation
# Port of the PrayTimes.org algorithm (ISNA: Fajr 15 deg, Isha 15 deg). Used as the fallback.
def _sin(d): return math.sin(math.radians(d))
def _cos(d): return math.cos(math.radians(d))
def _tan(d): return math.tan(math.radians(d))
def _asin(x): return math.degrees(math.asin(x))
def _acos(x): return math.degrees(math.acos(max(-1.0, min(1.0, x))))
def _atan2(y, x): return math.degrees(math.atan2(y, x))
def _acot(x): return math.degrees(math.atan(1.0 / x))
def _fix(a, b): return a - b * math.floor(a / b)


def _julian(y, m, d):
    if m <= 2:
        y -= 1
        m += 12
    a = math.floor(y / 100)
    b = 2 - a + math.floor(a / 4)
    return math.floor(365.25 * (y + 4716)) + math.floor(30.6001 * (m + 1)) + d + b - 1524.5


def _sun(jd):
    d = jd - 2451545.0
    g = _fix(357.529 + 0.98560028 * d, 360)
    q = _fix(280.459 + 0.98564736 * d, 360)
    lon = _fix(q + 1.915 * _sin(g) + 0.020 * _sin(2 * g), 360)
    e = 23.439 - 0.00000036 * d
    ra = _atan2(_cos(e) * _sin(lon), _cos(lon)) / 15
    eqt = q / 15 - _fix(ra, 24)
    decl = _asin(_sin(e) * _sin(lon))
    return decl, eqt


def compute_times(day: dt.date, lat: float, lng: float, tzname: str,
                  fajr_angle=15.0, isha_angle=15.0, asr_factor=1) -> dict:
    tz = ZoneInfo(tzname)
    offset = dt.datetime(day.year, day.month, day.day, 12, tzinfo=tz).utcoffset().total_seconds() / 3600
    jd = _julian(day.year, day.month, day.day) - lng / (15 * 24)

    def midday(t):
        return _fix(12 - _sun(jd + t)[1], 24)

    def angle_time(angle, t, ccw=False):
        decl = _sun(jd + t)[0]
        h = _acos((-_sin(angle) - _sin(decl) * _sin(lat)) / (_cos(decl) * _cos(lat))) / 15
        noon = midday(t)
        return noon - h if ccw else noon + h

    def asr(t):
        decl = _sun(jd + t)[0]
        return angle_time(-_acot(asr_factor + _tan(abs(lat - decl))), t)

    raw = {
        "fajr": angle_time(fajr_angle, 5 / 24, ccw=True),
        "sunrise": angle_time(0.833, 6 / 24, ccw=True),
        "dhuhr": midday(12 / 24),
        "asr": asr(13 / 24),
        "maghrib": angle_time(0.833, 18 / 24),
        "isha": angle_time(isha_angle, 18 / 24),
    }
    base = dt.datetime(day.year, day.month, day.day, tzinfo=tz)
    return {k: base + dt.timedelta(minutes=round((h + offset - lng / 15) * 60)) for k, h in raw.items()}


def fmt(t: dt.datetime | None) -> str:
    return t.strftime("%I:%M %p").lstrip("0") if t else ""


# ----------------------------------------------------------- masjid feed
def parse_clock(s):
    """'6:39AM' / '2:00 PM' / '18:05' -> '06:39' / '14:00' / '18:05'."""
    s = re.sub(r"\s+", "", str(s or "")).upper()
    m = re.match(r"^(\d{1,2}):(\d{2})(AM|PM)?$", s)
    if not m:
        return None
    h, mi, ap = int(m[1]), int(m[2]), m[3]
    if ap == "PM" and h != 12:
        h += 12
    if ap == "AM" and h == 12:
        h = 0
    return f"{h:02d}:{mi:02d}" if h < 24 and mi < 60 else None


def parse_feed_date(s):
    """'Thursday, Oct 22, 2026' -> date (locale independent)."""
    m = re.search(r"([A-Za-z]{3})[A-Za-z]*\.?\s+(\d{1,2}),?\s+(\d{4})", str(s or ""))
    if not m or m[1].lower() not in MONTHS:
        return None
    return dt.date(int(m[3]), MONTHS[m[1].lower()], int(m[2]))


def http_get(url, timeout=20, limit=3_000_000):
    req = urllib.request.Request(url, headers={"User-Agent": "Mozilla/5.0 (Windows NT 10.0) AdhanCast/1.1",
                                               "Accept": "text/html,application/json,*/*"})
    with urllib.request.urlopen(req, timeout=timeout) as r:
        return r.read(limit).decode(r.headers.get_content_charset() or "utf-8", "replace")


def fetch_masjidal(mid, start: dt.date, days: int) -> dict:
    end = start + dt.timedelta(days=days - 1)
    url = f"{MASJIDAL_API}?" + urlencode({"masjid_id": mid, "from_date": start.isoformat(), "to_date": end.isoformat()})
    j = json.loads(http_get(url))
    if j.get("status") != "success":
        raise RuntimeError(f"Masjidal replied: {j.get('message') or j.get('status') or 'unknown error'}")
    data = j.get("data") or {}
    iq_by_date = {parse_feed_date(x.get("date")): x for x in data.get("iqamah") or []}
    out = {}
    for row in data.get("salah") or []:
        d = parse_feed_date(row.get("date"))
        if not d:
            continue
        iq = iq_by_date.get(d) or {}
        iqama = {p: parse_clock(iq.get(k)) for p, k in FEED_KEYS.items()}
        iqama["jummah1"] = parse_clock(iq.get("jummah1"))
        iqama["jummah2"] = parse_clock(iq.get("jummah2"))
        out[d.isoformat()] = {
            "sunrise": parse_clock(row.get("sunrise")),
            "adhan": {p: parse_clock(row.get(k)) for p, k in FEED_KEYS.items()},
            "iqama": iqama,
        }
    if not out:
        raise RuntimeError("Masjidal returned no times for this masjid")
    return out


class MasjidFeed:
    """Downloads a masjid's schedule from Masjidal, keeps it on disk, refreshes daily."""

    def __init__(self):
        self.lock = threading.Lock()
        self.cache = {}       # mid -> {"fetched": iso, "days": {iso_date: {...}}}
        self.attempt = {}     # mid -> last attempt (epoch)
        self.errors = {}
        self.busy = set()

    def load(self):
        try:
            if CACHE_FILE.exists():
                self.cache = json.loads(CACHE_FILE.read_text("utf-8"))
        except Exception as e:
            log.error(f"Could not read masjid_cache.json ({e}); will re-download")
            self.cache = {}

    def _save(self):
        tmp = CACHE_FILE.with_suffix(".tmp")
        tmp.write_text(json.dumps(self.cache), "utf-8")
        os.replace(tmp, CACHE_FILE)

    def store(self, mid, days):
        with self.lock:
            entry = self.cache.setdefault(mid, {"days": {}})
            entry["days"].update(days)
            entry["fetched"] = dt.datetime.now().isoformat(timespec="seconds")
            cutoff = (dt.date.today() - dt.timedelta(days=3)).isoformat()
            entry["days"] = {k: v for k, v in entry["days"].items() if k >= cutoff}
            self._save()

    def day(self, mid, d: dt.date):
        with self.lock:
            return copy.deepcopy(self.cache.get(mid, {}).get("days", {}).get(d.isoformat()))

    def info(self, mid, today: dt.date):
        with self.lock:
            e = self.cache.get(mid, {})
            ahead = [k for k in e.get("days", {}) if k >= today.isoformat()]
            fetched = e.get("fetched")
        upd = ""
        if fetched:
            f = dt.datetime.fromisoformat(fetched)
            upd = f.strftime("%b %d, ").replace(" 0", " ") + fmt(f)
        return {"updated": upd, "days": len(ahead), "last_day": max(ahead) if ahead else "",
                "error": self.errors.get(mid), "busy": mid in self.busy}

    def refresh(self, mid, today: dt.date):
        if mid in self.busy:
            return
        self.busy.add(mid)
        self.attempt[mid] = time.time()
        try:
            try:
                days = fetch_masjidal(mid, today, FEED_DAYS)
            except Exception:
                days = fetch_masjidal(mid, today, 7)  # in case long ranges are refused
            self.store(mid, days)
            self.errors.pop(mid, None)
            log.info(f"Masjid times updated ({len(days)} days)")
        except Exception as e:
            self.errors[mid] = f"Couldn't update masjid times: {e}"
            log.warning(self.errors[mid] + " (using stored or calculated times)")
        finally:
            self.busy.discard(mid)

    def maybe_refresh(self, mid, today: dt.date, force=False):
        if not mid or mid in self.busy:
            return
        with self.lock:
            e = self.cache.get(mid, {})
            ahead = sum(1 for k in e.get("days", {}) if k >= today.isoformat())
            fresh = (e.get("fetched", "")[:10] == dt.date.today().isoformat()
                     and today.isoformat() in e.get("days", {}) and ahead >= 7)
        if not force and (fresh or time.time() - self.attempt.get(mid, 0) < 1800):
            return
        threading.Thread(target=self.refresh, args=(mid, today), daemon=True).start()


feed = MasjidFeed()


def _find_masjid_id(text):
    m = re.search(r"masjid_?id[\"']?\s*(?:=|:|%3D)\s*[\"']?([A-Za-z0-9]{5,16})", text, re.I)
    return m[1] if m else None


def _page_name(html):
    t = re.search(r"<title[^>]*>(.*?)</title>", html, re.I | re.S)
    if not t:
        return ""
    parts = [p.strip() for p in re.split(r"\s+[|–—-]\s+", html_lib.unescape(re.sub(r"\s+", " ", t[1])))]
    parts = [p for p in parts if p and p.lower() not in ("home", "welcome", "home page")]
    return parts[0][:80] if parts else ""


def resolve_masjid(inp: str, today: dt.date):
    inp = (inp or "").strip()
    if not inp:
        raise ValueError("Paste a masjid website, Masjidal link or masjid ID")
    name = ""
    mid = _find_masjid_id(inp)
    if not mid and ID_RE.match(inp):
        mid = inp
    if not mid:
        url = inp if re.match(r"^https?://", inp, re.I) else "https://" + inp
        try:
            page = http_get(url)
        except Exception as e:
            raise ValueError(f"Couldn't open {url}: {e}")
        mid = _find_masjid_id(page)
        name = _page_name(page)
        if not mid:
            raise ValueError("That page doesn't show Masjidal prayer times (no masjid ID found). "
                             "Try the link behind its 'monthly calendar', or paste the masjid ID.")
    try:
        days = fetch_masjidal(mid, today, 2)
    except Exception as e:
        raise ValueError(f"Found masjid ID {mid}, but Masjidal returned no times: {e}")
    feed.store(mid, days)
    return {"id": mid, "name": name or f"Masjid {mid}", "source": inp[:300],
            "today": days.get(today.isoformat()) or next(iter(days.values()))}


# ----------------------------------------------------------- default sounds
def _write_tone(path: Path, notes, rate=22050):
    frames = bytearray()
    for freq, dur, gap in notes:
        for i in range(int(rate * dur)):
            t = i / rate
            env = min(1.0, t / 0.015) * math.exp(-3.0 * t / dur)
            s = (math.sin(2 * math.pi * freq * t) + 0.35 * math.sin(4 * math.pi * freq * t)
                 + 0.15 * math.sin(6 * math.pi * freq * t)) / 1.5
            frames += struct.pack("<h", int(0.6 * env * s * 32767))
        frames += b"\x00\x00" * int(rate * gap)
    with wave.open(str(path), "wb") as w:
        w.setnchannels(1)
        w.setsampwidth(2)
        w.setframerate(rate)
        w.writeframes(bytes(frames))


def ensure_default_sounds():
    if not (SOUNDS_DIR / DEFAULT_ADHAN).exists():
        phrase = [(392.0, 0.9, 0.05), (440.0, 0.9, 0.05), (523.25, 1.6, 0.6)]
        _write_tone(SOUNDS_DIR / DEFAULT_ADHAN, phrase * 3)  # ~12 s placeholder
    if not (SOUNDS_DIR / DEFAULT_ALERT).exists():
        _write_tone(SOUNDS_DIR / DEFAULT_ALERT, [(659.25, 0.8, 0.05), (523.25, 1.4, 0.0)])


def list_sounds():
    return sorted((p.name for p in SOUNDS_DIR.iterdir()
                   if p.is_file() and p.suffix.lower() in AUDIO_TYPES), key=str.lower)


# ----------------------------------------------------------------- settings
def default_settings():
    def prayer():
        return {
            "enabled": True, "offset": 0, "sound": DEFAULT_ADHAN, "speaker": "", "volume": 0.5,
            "iqama": {"enabled": False, "mode": "iqama", "minutes": 0, "dir": "before", "time": "",
                      "sound": DEFAULT_ALERT, "speaker": "", "volume": 0.4},
        }
    return {
        "location": {"name": "Makkah", "lat": 21.4225, "lng": 39.8262, "timezone": "Asia/Riyadh"},
        "masjids": [],
        "active_masjid": "",
        "adhan_source": "masjid",
        "jumuah": {"enabled": False, "which": "jummah1", "minutes": 10, "sound": DEFAULT_ALERT,
                   "speaker": "", "volume": 0.5},
        "asr_method": "standard",
        "default_speaker": "",
        "keep_awake": True,
        "host_ip": "",
        "prayers": {p: prayer() for p in PRAYERS},
    }


def _migrate(raw):
    """v1 had a 'second' alert per prayer; it becomes the iqama alert. Jumu'ah became its own section."""
    j = raw.get("jumuah")
    if isinstance(j, str):
        raw["jumuah"] = {"which": j if j in ("jummah1", "jummah2") else "jummah1"}
    for c in (raw.get("prayers") or {}).values():
        if isinstance(c, dict) and "second" in c and "iqama" not in c:
            q = c.pop("second") or {}
            c["iqama"] = {
                "enabled": q.get("enabled", False),
                "mode": "at" if q.get("mode") == "at" else "adhan",
                "minutes": q.get("minutes", 15), "dir": "after", "time": q.get("time", ""),
                "sound": q.get("sound", DEFAULT_ALERT), "speaker": q.get("speaker", ""),
                "volume": q.get("volume", 0.4),
            }
    return raw


def _merge(base, over):
    for k, v in (over or {}).items():
        if k in base and isinstance(base[k], dict) and isinstance(v, dict):
            _merge(base[k], v)
        elif k in base:
            base[k] = v
    return base


def _num(v, lo, hi, kind=float, name="value"):
    try:
        v = kind(v)
    except (TypeError, ValueError):
        raise ValueError(f"{name} must be a number")
    return max(lo, min(hi, v))


def validate(s):
    loc = s["location"]
    loc["name"] = str(loc["name"])[:60]
    loc["lat"] = _num(loc["lat"], -90, 90, name="Latitude")
    loc["lng"] = _num(loc["lng"], -180, 180, name="Longitude")
    try:
        ZoneInfo(str(loc["timezone"]))
    except Exception:
        raise ValueError(f"Unknown time zone: {loc['timezone']}")
    masjids, seen = [], set()
    for m in s["masjids"] if isinstance(s["masjids"], list) else []:
        mid = str((m or {}).get("id", "")).strip()
        if ID_RE.match(mid) and mid not in seen:
            seen.add(mid)
            masjids.append({"id": mid, "name": str(m.get("name") or f"Masjid {mid}")[:80],
                            "source": str(m.get("source") or "")[:300]})
    s["masjids"] = masjids
    s["active_masjid"] = s["active_masjid"] if s["active_masjid"] in seen else ""
    s["adhan_source"] = "calculated" if s["adhan_source"] == "calculated" else "masjid"
    j = s["jumuah"]
    j["enabled"] = bool(j["enabled"])
    j["which"] = "jummah2" if j["which"] == "jummah2" else "jummah1"
    j["minutes"] = _num(j["minutes"], 0, 120, int, "Jumu'ah alert minutes")
    j["volume"] = round(_num(j["volume"], 0, 1), 2)
    j["sound"] = str(j["sound"] or DEFAULT_ALERT)
    j["speaker"] = str(j["speaker"] or "")
    s["asr_method"] = "hanafi" if s["asr_method"] == "hanafi" else "standard"
    s["default_speaker"] = str(s["default_speaker"] or "")
    s["host_ip"] = str(s["host_ip"] or "").strip()
    s["keep_awake"] = bool(s["keep_awake"])
    for p in PRAYERS:
        c = s["prayers"][p]
        c["enabled"] = bool(c["enabled"])
        c["offset"] = _num(c["offset"], -30, 30, int, f"{NAMES[p]} shift")
        c["volume"] = round(_num(c["volume"], 0, 1), 2)
        c["sound"] = str(c["sound"] or DEFAULT_ADHAN)
        c["speaker"] = str(c["speaker"] or "")
        q = c["iqama"]
        q["enabled"] = bool(q["enabled"])
        q["mode"] = q["mode"] if q["mode"] in ("iqama", "adhan", "at") else "iqama"
        q["minutes"] = _num(q["minutes"], 0, 240, int, f"{NAMES[p]} iqama-alert minutes")
        q["dir"] = "after" if q["dir"] == "after" else "before"
        q["time"] = str(q["time"] or "")
        if q["enabled"] and q["mode"] == "at" and not TIME_RE.match(q["time"]):
            raise ValueError(f"Set a time for the {NAMES[p]} iqama alert")
        q["volume"] = round(_num(q["volume"], 0, 1), 2)
        q["sound"] = str(q["sound"] or DEFAULT_ALERT)
        q["speaker"] = str(q["speaker"] or "")
    return s


class Settings:
    def __init__(self):
        self.lock = threading.Lock()
        self.data = default_settings()

    def load(self):
        if SETTINGS_FILE.exists():
            try:
                raw = _migrate(json.loads(SETTINGS_FILE.read_text("utf-8")))
                self.data = validate(_merge(default_settings(), raw))
                self.save(self.data)
            except Exception as e:
                log.error(f"Could not read settings.json ({e}); using defaults")
        else:
            self.save(self.data)

    def get(self):
        with self.lock:
            return copy.deepcopy(self.data)

    def save(self, data):
        tmp = SETTINGS_FILE.with_suffix(".tmp")
        tmp.write_text(json.dumps(data, indent=2), "utf-8")
        os.replace(tmp, SETTINGS_FILE)
        with self.lock:
            self.data = data


settings = Settings()


# ------------------------------------------------------------ Google Cast
class CastManager:
    def __init__(self):
        self.lock = threading.Lock()
        self.zconf = None
        self.browser = None
        self.error = None

    def start(self):
        """Start (or restart) continuous mDNS discovery of Cast devices and groups."""
        try:
            import zeroconf
            from pychromecast.discovery import CastBrowser, SimpleCastListener
            with self.lock:
                if self.browser:
                    try:
                        self.browser.stop_discovery()
                    except Exception:
                        pass
                if self.zconf is None:
                    self.zconf = zeroconf.Zeroconf()
                self.browser = CastBrowser(SimpleCastListener(), self.zconf)
                self.browser.start_discovery()
            self.error = None
        except Exception as e:
            self.error = f"Speaker discovery failed: {e}"
            log.error(self.error)

    def devices(self):
        b = self.browser
        if not b:
            return []
        out = [{"name": ci.friendly_name, "model": ci.model_name, "type": ci.cast_type, "host": ci.host}
               for ci in list(b.devices.values()) if ci.friendly_name]
        return sorted(out, key=lambda d: (d["type"] == "group", d["name"].lower()))

    def _find(self, name):
        b = self.browser
        if not b:
            return None
        for ci in list(b.devices.values()):
            if (ci.friendly_name or "").lower() == name.lower():
                return ci
        return None

    def _connect(self, name):
        import pychromecast
        info = self._find(name)
        if info is None:
            time.sleep(8)  # discovery may still be catching up
            info = self._find(name)
        if info is None:
            raise RuntimeError("not found on the network (try Rescan in the dashboard)")
        cast = pychromecast.get_chromecast_from_cast_info(info, self.zconf, tries=2, retry_wait=3, timeout=15)
        cast.wait(timeout=20)
        return cast

    def play(self, name, url, content_type, volume, label):
        try:
            cast = self._connect(name)
        except Exception as e:
            log.error(f"{label}: couldn't connect to {name}: {e}")
            return
        try:
            prev = cast.status.volume_level if cast.status else None
            if volume is not None:
                cast.set_volume(float(volume))
            mc = cast.media_controller
            mc.play_media(url, content_type, title=label)
            mc.block_until_active(timeout=20)
            log.info(f"{label}: sent to {name}")
            start, seen = time.time(), False
            while time.time() - start < 900:
                state = mc.status.player_state if mc.status else "UNKNOWN"
                if state in ("PLAYING", "BUFFERING"):
                    if not seen:
                        log.info(f"{label}: playing on {name}")
                    seen = True
                elif seen and state in ("IDLE", "UNKNOWN"):
                    break
                elif not seen and time.time() - start > 45:
                    log.warning(f"{label}: {name} never started playing. Check that it can reach {url} "
                                f"(Windows Firewall must allow Python on Private networks).")
                    break
                time.sleep(1)
            if prev is not None and volume is not None:
                time.sleep(1)
                cast.set_volume(prev)  # put the speaker back the way we found it
        except Exception as e:
            log.error(f"{label}: playback on {name} failed: {e}")
        finally:
            try:
                cast.disconnect(timeout=5)
            except Exception:
                pass

    def stop(self, name):
        try:
            cast = self._connect(name)
            cast.quit_app()
            cast.disconnect(timeout=5)
            log.info(f"Stopped {name}")
        except Exception as e:
            log.error(f"Couldn't stop {name}: {e}")


casts = CastManager()


def lan_ip():
    s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    try:
        s.connect(("10.255.255.255", 1))  # no packet is sent; just picks the outbound interface
        return s.getsockname()[0]
    except Exception:
        return "127.0.0.1"
    finally:
        s.close()


def host_ip():
    return settings.get()["host_ip"] or lan_ip()


def play_sound(speaker, sound, volume, label):
    if not speaker:
        log.warning(f"{label}: no speaker chosen. Pick one in the dashboard.")
        return
    if not (SOUNDS_DIR / sound).exists():
        fallback = DEFAULT_ALERT if any(w in label.lower() for w in ("iqama", "jumu")) else DEFAULT_ADHAN
        log.warning(f"{label}: sound '{sound}' is missing; using {fallback}")
        sound = fallback
    url = f"http://{host_ip()}:{PORT}/sounds/{quote(sound)}"
    ctype = AUDIO_TYPES.get(Path(sound).suffix.lower(), "audio/mpeg")
    threading.Thread(target=casts.play, args=(speaker, url, ctype, volume, label), daemon=True).start()


# ---------------------------------------------------------------- schedule
def label_of(ev):
    return f"{NAMES[ev['prayer']]} {({'adhan': 'adhan', 'iqama': 'iqama alert', 'jumuah': 'alert'})[ev['kind']]}"


def day_plan(s, day: dt.date):
    """Adhan and iqama times for one day: masjid schedule where available, else calculated."""
    loc = s["location"]
    tz = ZoneInfo(loc["timezone"])
    calc = compute_times(day, loc["lat"], loc["lng"], loc["timezone"],
                         asr_factor=2 if s["asr_method"] == "hanafi" else 1)
    m = feed.day(s["active_masjid"], day) if s["active_masjid"] else None

    def at(hhmm):
        h, mi = map(int, hhmm.split(":"))
        return dt.datetime(day.year, day.month, day.day, h, mi, tzinfo=tz)

    adhan, src = {}, {}
    for p in PRAYERS + ["sunrise"]:
        mt = None
        if m and s["adhan_source"] == "masjid":
            mt = m.get("sunrise") if p == "sunrise" else m["adhan"].get(p)
        adhan[p] = at(mt) if mt else calc[p]
        src[p] = "masjid" if mt else "calculated"
    iqama = {p: (at(m["iqama"][p]) if m and m["iqama"].get(p) else None) for p in PRAYERS}
    jumuah = {k: (at(m["iqama"][k]) if m and m["iqama"].get(k) else None) for k in ("jummah1", "jummah2")}
    return {"adhan": adhan, "src": src, "iqama": iqama, "jumuah": jumuah, "have_masjid": m is not None,
            "friday": day.weekday() == 4}


def build_events(s, day):
    plan = day_plan(s, day)
    tz = ZoneInfo(s["location"]["timezone"])
    events = []
    for p in PRAYERS:
        c = s["prayers"][p]
        a = plan["adhan"][p] + dt.timedelta(minutes=c["offset"])
        speaker = c["speaker"] or s["default_speaker"]
        events.append({"prayer": p, "kind": "adhan", "at": a, "src": plan["src"][p], "enabled": c["enabled"],
                       "sound": c["sound"], "speaker": speaker, "volume": c["volume"], "iqama_skipped": False})
        q = c["iqama"]
        skipped = False
        if q["mode"] == "at":
            sat = None
            if TIME_RE.match(q["time"]):
                h, m = map(int, q["time"].split(":"))
                sat = dt.datetime(day.year, day.month, day.day, h, m, tzinfo=tz)
        else:
            base = plan["iqama"][p] if q["mode"] == "iqama" else a
            if base is None:  # no masjid iqama time for this prayer today: skip the alert
                sat = None
                skipped = q["enabled"]
                events[-1]["iqama_skipped"] = skipped
            else:
                delta = dt.timedelta(minutes=q["minutes"])
                sat = base - delta if q["dir"] == "before" else base + delta
        events.append({"prayer": p, "kind": "iqama", "at": sat, "enabled": q["enabled"] and sat is not None,
                       "src": plan["src"][p], "sound": q["sound"], "speaker": q["speaker"] or speaker,
                       "volume": q["volume"], "skipped": skipped})
    j = s["jumuah"]
    jt = plan["jumuah"][j["which"]] if plan["friday"] else None
    jat = jt - dt.timedelta(minutes=j["minutes"]) if jt else None
    events.append({"prayer": "jumuah", "kind": "jumuah", "at": jat, "enabled": j["enabled"] and jat is not None,
                   "src": "masjid", "sound": j["sound"], "speaker": j["speaker"] or s["default_speaker"],
                   "volume": j["volume"], "skipped": j["enabled"] and plan["friday"] and jt is None})
    return plan, events


def next_event(s, now):
    for day in (now.date(), now.date() + dt.timedelta(days=1)):
        _, evs = build_events(s, day)
        evs = sorted((e for e in evs if e["enabled"] and e["at"] > now), key=lambda e: e["at"])
        if evs:
            e = evs[0]
            return {"label": label_of(e), "at": e["at"].isoformat(), "display": fmt(e["at"]),
                    "day": "today" if day == now.date() else "tomorrow", "speaker": e["speaker"]}
    return None


def _keep_awake(on):
    if os.name != "nt":
        return
    ES_CONTINUOUS, ES_SYSTEM_REQUIRED = 0x80000000, 0x00000001
    ctypes.windll.kernel32.SetThreadExecutionState(ES_CONTINUOUS | (ES_SYSTEM_REQUIRED if on else 0))


class Scheduler(threading.Thread):
    def __init__(self):
        super().__init__(daemon=True, name="scheduler")
        self.fired = set()
        self.day = None
        self.started = time.time()

    def run(self):
        while True:
            try:
                self.tick()
            except Exception as e:
                log.exception(f"Scheduler error: {e}")
            time.sleep(10)

    def tick(self):
        s = settings.get()
        _keep_awake(s["keep_awake"])
        now = dt.datetime.now(ZoneInfo(s["location"]["timezone"]))
        if now.date() != self.day:
            self.day = now.date()
            self.fired.clear()
        feed.maybe_refresh(s["active_masjid"], now.date())
        _, events = build_events(s, now.date())
        for ev in events:
            if not ev["enabled"]:
                if ev["kind"] == "jumuah" and ev["skipped"] and now.hour >= 10 and "jumuah-skip" not in self.fired:
                    self.fired.add("jumuah-skip")
                    log.warning("No time listed for the chosen Jumu'ah today, so the Jumu'ah alert is skipped")
                continue
            key = (ev["prayer"], ev["kind"], ev["at"].isoformat())
            late = (now - ev["at"]).total_seconds()
            if key in self.fired or late < 0:
                continue
            self.fired.add(key)
            if late <= GRACE_SECONDS:
                if ev["kind"] == "adhan" and ev["src"] == "calculated" and s["active_masjid"] \
                        and s["adhan_source"] == "masjid":
                    log.warning(f"{label_of(ev)}: masjid times unavailable, used the calculated time")
                if ev.get("iqama_skipped"):
                    log.warning(f"{NAMES[ev['prayer']]}: no masjid iqama time today, so its iqama alert is skipped")
                play_sound(ev["speaker"], ev["sound"], ev["volume"], label_of(ev))
            elif ev["at"].timestamp() > self.started:
                log.warning(f"Missed {label_of(ev)} at {fmt(ev['at'])}. The PC was probably asleep.")


# --------------------------------------------------------------------- API
app = FastAPI(title="Adhan Cast")
app.mount("/sounds", StaticFiles(directory=SOUNDS_DIR), name="sounds")


def _now(s):
    return dt.datetime.now(ZoneInfo(s["location"]["timezone"]))


@app.get("/")
def index():
    return FileResponse(STATIC_DIR / "index.html")


@app.get("/api/state")
def api_state():
    s = settings.get()
    now = _now(s)
    plan, events = build_events(s, now.date())
    rows = []
    for p in PRAYERS:
        ad = next(e for e in events if e["prayer"] == p and e["kind"] == "adhan")
        iq = next(e for e in events if e["prayer"] == p and e["kind"] == "iqama")
        rows.append({
            "prayer": p,
            "adhan_at": fmt(ad["at"]), "adhan_src": ad["src"], "adhan_on": ad["enabled"],
            "adhan_past": ad["at"] <= now,
            "iqama": fmt(plan["iqama"][p]),
            "alert_at": fmt(iq["at"]), "alert_on": iq["enabled"], "alert_skip": iq["skipped"],
            "alert_past": bool(iq["at"] and iq["at"] <= now),
        })
    fri = now.date() + dt.timedelta(days=(4 - now.weekday()) % 7)
    fplan, fevents = build_events(s, fri)
    fe = fevents[-1]
    jumuah = {"day": "Today" if fri == now.date() else fri.strftime("%a, %b %d").replace(" 0", " "),
              "jummah1": fmt(fplan["jumuah"]["jummah1"]), "jummah2": fmt(fplan["jumuah"]["jummah2"]),
              "alert_at": fmt(fe["at"]), "on": fe["enabled"], "skip": fe["skipped"],
              "past": bool(fe["at"] and fe["at"] <= now)}
    masjid = None
    if s["active_masjid"]:
        name = next((m["name"] for m in s["masjids"] if m["id"] == s["active_masjid"]), s["active_masjid"])
        masjid = {"id": s["active_masjid"], "name": name, **feed.info(s["active_masjid"], now.date())}
    return {
        "date": now.strftime("%A, %B %d, %Y").replace(" 0", " "),
        "sunrise": fmt(plan["adhan"]["sunrise"]),
        "friday": plan["friday"],
        "jumuah": jumuah,
        "have_masjid_today": plan["have_masjid"],
        "masjid": masjid,
        "rows": rows,
        "next": next_event(s, now),
        "settings": s,
        "speakers": casts.devices(),
        "cast_error": casts.error,
        "sounds": list_sounds(),
        "log": list(RECENT),
        "host_ip_auto": lan_ip(),
    }


@app.put("/api/settings")
def api_save(payload: dict = Body(...)):
    try:
        new = validate(_merge(default_settings(), _migrate(payload)))
    except (ValueError, KeyError, TypeError) as e:
        raise HTTPException(400, str(e))
    old = settings.get()
    settings.save(new)
    log.info("Settings saved")
    if new["active_masjid"] and new["active_masjid"] != old["active_masjid"]:
        feed.maybe_refresh(new["active_masjid"], _now(new).date(), force=True)
    return {"ok": True, "settings": new}


@app.post("/api/masjids/lookup")
def api_masjid_lookup(payload: dict = Body(...)):
    s = settings.get()
    try:
        return resolve_masjid(str(payload.get("input") or ""), _now(s).date())
    except ValueError as e:
        raise HTTPException(400, str(e))


@app.post("/api/masjids/refresh")
def api_masjid_refresh():
    s = settings.get()
    if not s["active_masjid"]:
        raise HTTPException(400, "No masjid selected")
    today = _now(s).date()
    feed.refresh(s["active_masjid"], today)
    info = feed.info(s["active_masjid"], today)
    if info["error"]:
        raise HTTPException(502, info["error"])
    return info


@app.post("/api/speakers/refresh")
def api_rescan():
    casts.start()
    time.sleep(4)
    return {"speakers": casts.devices(), "cast_error": casts.error}


@app.post("/api/test")
def api_test(payload: dict = Body(...)):
    speaker = str(payload.get("speaker") or "")
    if not speaker:
        raise HTTPException(400, "Choose a speaker first")
    play_sound(speaker, str(payload.get("sound") or DEFAULT_ADHAN), payload.get("volume"), "Test")
    return {"ok": True}


@app.post("/api/stop")
def api_stop(payload: dict = Body(default={})):
    s = settings.get()
    names = {payload.get("speaker")} if payload.get("speaker") else (
        {s["default_speaker"]}
        | {c["speaker"] for c in s["prayers"].values()}
        | {c["iqama"]["speaker"] for c in s["prayers"].values()} | {s["jumuah"]["speaker"]})
    names.discard("")
    names.discard(None)
    for n in names:
        threading.Thread(target=casts.stop, args=(n,), daemon=True).start()
    return {"ok": True, "stopped": sorted(names)}


@app.post("/api/sounds")
async def api_upload(file: UploadFile = File(...)):
    name = Path(file.filename or "").name
    ext = Path(name).suffix.lower()
    if ext not in AUDIO_TYPES:
        raise HTTPException(400, "Use an MP3, M4A, AAC, WAV, OGG or FLAC file")
    safe = re.sub(r"[^A-Za-z0-9._ -]", "_", name).strip() or f"sound{ext}"
    data = await file.read()
    if len(data) > 50 * 1024 * 1024:
        raise HTTPException(400, "File is larger than 50 MB")
    (SOUNDS_DIR / safe).write_bytes(data)
    log.info(f"Added sound {safe}")
    return {"name": safe, "sounds": list_sounds()}


@app.delete("/api/sounds/{name}")
def api_delete_sound(name: str):
    name = Path(name).name
    if name in (DEFAULT_ADHAN, DEFAULT_ALERT):
        raise HTTPException(400, "The default sounds can't be deleted")
    s = settings.get()
    used = [NAMES[p] for p, c in s["prayers"].items() if name in (c["sound"], c["iqama"]["sound"])] \
        + (["Jumu'ah"] if s["jumuah"]["sound"] == name else [])
    if used:
        raise HTTPException(400, f"In use by {', '.join(used)}. Pick another sound there first.")
    path = SOUNDS_DIR / name
    if path.exists():
        path.unlink()
        log.info(f"Deleted sound {name}")
    return {"sounds": list_sounds()}


# -------------------------------------------------------------------- main
def _port_free(port):
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
        try:
            s.bind(("0.0.0.0", port))
            return True
        except OSError:
            return False


def main():
    url = f"http://localhost:{PORT}"
    if not _port_free(PORT):
        log.warning(f"Port {PORT} is in use. Adhan Cast is probably already running; open {url}")
        if "--open" in sys.argv:
            webbrowser.open(url)
        return
    PID_FILE.write_text(str(os.getpid()))
    ensure_default_sounds()
    settings.load()
    feed.load()
    casts.start()
    Scheduler().start()
    log.info(f"Adhan Cast started. Dashboard: {url}  (speakers fetch audio from {host_ip()}:{PORT})")
    if "--open" in sys.argv:
        threading.Timer(2.5, lambda: webbrowser.open(url)).start()
    import uvicorn
    uvicorn.run(app, host="0.0.0.0", port=PORT, log_config=None, access_log=False)


if __name__ == "__main__":
    main()
