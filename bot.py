"""
BS Track Remake Bot + Mini App
- O'zbekcha salomlashish
- 4 ta standart rejim: Reverb, Slowed+Reverb, Bass Boost, 8D
- To'liq qo'lda sozlash: tezlik, reverb, bass, 8D, 9 polosali EQ
- Har safar nom so'raydi, oxiriga (BStrack) qo'shadi
- Muqovani logotipga almashtiradi (eskisini o'chiradi)
- Ruxsat (whitelist) tizimi
"""
import os
import re
import json
import copy
import asyncio
import logging
import tempfile
import threading
import traceback
import multiprocessing as mp
from http.server import BaseHTTPRequestHandler

import numpy as np
from pydub import AudioSegment
from pedalboard import Pedalboard, Reverb, PeakFilter, LowShelfFilter, LowpassFilter, Limiter
from mutagen.id3 import ID3, TIT2, TPE1, APIC, ID3NoHeaderError

from telegram import Update, InlineKeyboardButton, InlineKeyboardMarkup, WebAppInfo, MenuButtonWebApp, MessageEntity
from telegram.error import BadRequest, Conflict
from telegram.ext import (
    Application,
    CommandHandler,
    CallbackQueryHandler,
    MessageHandler,
    ContextTypes,
    filters,
)

# ====================== SOZLAMALAR ======================
BASE_DIR = os.path.dirname(os.path.abspath(__file__))
COVER_PATH = os.path.join(BASE_DIR, "cover.jpg")
THUMB_PATH = os.path.join(BASE_DIR, "cover_thumb.jpg")

TAG = "(BStrack)"                      # har bir qo'shiq nomi oxiriga qo'shiladi
MAX_MINUTES = int(os.environ.get("MAX_MINUTES", "8"))
MAX_DURATION_SEC = MAX_MINUTES * 60
TG_DOWNLOAD_LIMIT = 20 * 1024 * 1024   # Telegram bot fayl yuklab olish chegarasi
RENDER_TIMEOUT = int(os.environ.get("RENDER_TIMEOUT", "600"))   # soniya: shundan oshsa ishlov to'xtatiladi

EQ_BANDS = [60, 170, 310, 600, 1000, 3000, 6000, 12000, 16000]
EQ_LABELS = ["60 Hz", "170 Hz", "310 Hz", "600 Hz", "1 kHz", "3 kHz", "6 kHz", "12 kHz", "16 kHz"]

# Standart rejimlar (bu qiymatlarni xohlasangiz shu yerdan o'zgartirasiz)
# speed: 1.0 = asl tezlik | reverb: 0-100 % | bass: dB (-12..+12) | eq: 9 ta polosa dB
BASE_EQ = [2, -3, -3, 0, 0, 2, 5, 7, 8]   # EQ saytidagi standart sozlamangiz

PRESETS = {
    "reverb": {
        "title": "Reverb", "desc": "Xona makoni, yumshoq va keng ovoz.",
        "speed": 1.0, "reverb": 45, "bass": 0, "sub": 0, "eq": BASE_EQ, "is_8d": False, "bitrate": "320k",
    },
    "slowed": {
        "title": "Slowed + Reverb", "desc": "Sekinlashgan, chuqur va xayolchan ovoz.",
        "speed": 0.90, "reverb": 40, "bass": 0, "sub": 0, "eq": [2, -4, -4, 0, 0, 2, 5, 7, 8], "is_8d": False, "bitrate": "320k",
    },
    "bass": {
        "title": "Bass Boost", "desc": "Kuchli va yumaloq past chastotalar.",
        "speed": 1.0, "reverb": 25, "bass": 8, "sub": 0, "eq": [10, 8, 4, 1, 0, 0, 0, 0, 0], "is_8d": False, "bitrate": "320k",
    },
    "lowbass": {
        "title": "Lowbass", "desc": "Yumshoq bass: iliq, silliq va yoqimli past tovush. Qulog'ni charchatmaydi.",
        "speed": 1.0, "reverb": 10, "bass": 3, "sub": 0, "eq": [4, 3, 1, 0, 0, 0, 0, 0, 0], "is_8d": False, "bitrate": "320k",
    },
    "8d": {
        "title": "8D Audio", "desc": "Ovoz boshingiz atrofida aylanadi. Quloqchin bilan eshiting.",
        "speed": 1.0, "reverb": 22, "bass": 0, "sub": 0, "eq": [0, -1, -1, 0, 0, 1, 2, 2, 2], "is_8d": True, "bitrate": "320k",
    },
    "pitchup": {
        "title": "Pitch Up", "desc": "Tezroq va balandroq ton (nightcore uslubi).",
        "speed": 1.15, "reverb": 15, "bass": 0, "sub": 0, "eq": [0, -1, -1, 0, 0, 1, 2, 3, 3], "is_8d": False, "bitrate": "320k",
    },
}

DEFAULT_CUSTOM = {
    "title": "Qo'lda sozlash",
    "speed": 1.0, "reverb": 45, "bass": 0, "sub": 0, "eq": list(BASE_EQ), "is_8d": False, "bitrate": "320k",
}

logging.basicConfig(level=logging.INFO)
logger = logging.getLogger("bsbot")

BOT_TOKEN = os.environ["BOT_TOKEN"]

# ====================== MINI APP SERVERI ======================
# Bitta server ikki ishni qiladi: Mini App sahifasini beradi va uning so'rovlarini qabul qiladi.
# Xavfsizlik: har bir so'rovda Telegram imzosi (initData, BOT_TOKEN bilan HMAC) tekshiriladi va
# foydalanuvchi ruxsat ro'yxatida bo'lishi shart. Sahifaning o'zi ochiq, lekin ichida hech qanday sir yo'q.
import hmac
import hashlib
import time
import uuid
from urllib.parse import parse_qsl, unquote
from http.server import ThreadingHTTPServer

WEB_DIR = os.path.join(BASE_DIR, "webapp")
RENDER_LOCK = threading.Lock()       # bir vaqtda bitta ishlov (bot ham, Mini App ham)
BOT_APP = None                       # Application (post_init'da to'ldiriladi)
BOT_LOOP = None                      # botning asyncio sikli
WEB_MAX_BYTES = 25 * 1024 * 1024
JOBS = {}                            # job_id -> dict
JOBS_LOCK = threading.Lock()
_web_notified = set()


def validate_init_data(init_data: str):
    """Telegram initData imzosini tekshiradi. To'g'ri bo'lsa user dict, aks holda None."""
    if not init_data or len(init_data) > 4096:
        return None
    try:
        pairs = dict(parse_qsl(init_data, keep_blank_values=True))
        got_hash = pairs.pop("hash", "")
        if not got_hash:
            return None
        check = "\n".join(f"{k}={v}" for k, v in sorted(pairs.items()))
        secret = hmac.new(b"WebAppData", BOT_TOKEN.encode(), hashlib.sha256).digest()
        calc = hmac.new(secret, check.encode(), hashlib.sha256).hexdigest()
        if not hmac.compare_digest(calc, got_hash):
            return None
        if time.time() - int(pairs.get("auth_date", "0")) > 48 * 3600:
            return None
        user = json.loads(pairs.get("user", "{}"))
        if not isinstance(user, dict) or not isinstance(user.get("id"), int):
            return None
        return user
    except Exception:
        return None


def sanitize_params(raw: dict) -> dict:
    """Mini App yuborgan qiymatlarni tekshiradi va chegaralaydi (zararli qiymatlardan himoya)."""
    base = None
    key = raw.get("preset")
    if key:
        if key not in PRESETS:
            raise ValueError("Noma'lum rejim")
        base = copy.deepcopy(PRESETS[key])
    else:
        c = raw.get("custom") or {}
        base = copy.deepcopy(DEFAULT_CUSTOM)
        base["speed"] = round(clamp(float(c.get("speed", 1.0)), 0.5, 1.5), 2)
        base["reverb"] = int(clamp(int(c.get("reverb", 0)), 0, 100))
        base["bass"] = int(clamp(int(c.get("bass", 0)), -12, 12))
        base["sub"] = int(clamp(int(c.get("sub", 0)), 0, 100))
        base["is_8d"] = bool(c.get("is_8d", False))
        eq = c.get("eq") or [0] * 9
        if len(eq) != 9:
            raise ValueError("EQ noto'g'ri")
        base["eq"] = [int(clamp(int(v), -12, 12)) for v in eq]
    return base


def public_config() -> dict:
    presets = []
    for k, v in PRESETS.items():
        presets.append({"key": k, **{f: v[f] for f in ("title", "desc", "speed", "reverb", "bass", "sub", "eq", "is_8d")}})
    return {
        "presets": presets,
        "defaults": {f: DEFAULT_CUSTOM[f] for f in ("speed", "reverb", "bass", "sub", "eq", "is_8d")},
        "eq_labels": EQ_LABELS,
        "tag": TAG,
        "max_minutes": MAX_MINUTES,
        "max_mb": WEB_MAX_BYTES // (1024 * 1024),
    }


def _set_job(job_id, **kw):
    with JOBS_LOCK:
        if job_id in JOBS:
            JOBS[job_id].update(kw)


def _cleanup_jobs():
    now = time.time()
    with JOBS_LOCK:
        for k in [k for k, v in JOBS.items() if now - v["t"] > 3600]:
            JOBS.pop(k, None)


def run_web_job(job_id, uid, in_path, params, full_name):
    artist, title = split_name(full_name)
    tmpdir = os.path.dirname(in_path)
    try:
        out_path = os.path.join(tmpdir, "output.mp3")
        _set_job(job_id, status="queued")
        with RENDER_LOCK:
            _set_job(job_id, status="processing")
            render_in_subprocess(in_path, out_path, params, title, artist, RENDER_TIMEOUT)
        _set_job(job_id, status="sending")

        async def _send():
            thumb = open(THUMB_PATH, "rb") if os.path.exists(THUMB_PATH) else None
            try:
                with open(out_path, "rb") as f:
                    await BOT_APP.bot.send_audio(
                        chat_id=uid, audio=f, filename=safe_filename(full_name), title=title,
                        performer=artist or None, thumbnail=thumb,
                        caption=f"Tayyor: {params.get('title', '')}",
                        read_timeout=120, write_timeout=300, connect_timeout=30,
                    )
                await BOT_APP.bot.send_message(uid, SAVE_HINT)
            finally:
                if thumb:
                    thumb.close()

        asyncio.run_coroutine_threadsafe(_send(), BOT_LOOP).result(timeout=420)
        _set_job(job_id, status="done")
    except TooLong:
        _set_job(job_id, status="error", message=f"Qo'shiq juda uzun. {MAX_MINUTES} daqiqagacha qabul qilinadi.")
    except RenderTimeout:
        _set_job(job_id, status="error", message="Ishlov juda uzoq davom etdi. Qisqaroq qo'shiq bilan urinib ko'ring.")
    except RenderKilled:
        _set_job(job_id, status="error", message="Server xotirasi yetmadi. Qisqaroq qo'shiq bilan urinib ko'ring.")
    except Exception as e:
        logger.exception("Web ishlovida xatolik")
        _set_job(job_id, status="error", message="Ishlov berishda xatolik yuz berdi. Boshqa fayl bilan urinib ko'ring.")
    finally:
        try:
            import shutil
            shutil.rmtree(tmpdir, ignore_errors=True)
        except Exception:
            pass


class Web(BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.1"
    server_version = "BSTrack"

    def log_message(self, *args):
        pass

    # --- yordamchilar ---
    def _send(self, code, body: bytes, ctype="application/json; charset=utf-8", extra=None):
        self.send_response(code)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        self.send_header("X-Content-Type-Options", "nosniff")
        for k, v in (extra or {}).items():
            self.send_header(k, v)
        self.end_headers()
        if self.command != "HEAD":
            self.wfile.write(body)

    def _json(self, code, obj):
        self._send(code, json.dumps(obj, ensure_ascii=False).encode("utf-8"))

    def _file(self, name, ctype):
        path = os.path.join(WEB_DIR, name) if name == "index.html" else os.path.join(BASE_DIR, name)
        try:
            with open(path, "rb") as f:
                data = f.read()
        except OSError:
            self._send(404, b"Not found", "text/plain; charset=utf-8")
            return
        extra = {"Cache-Control": "public, max-age=3600"} if ctype.startswith("image") else {}
        self._send(200, data, ctype, extra)

    def _user(self):
        return validate_init_data(self.headers.get("X-Init-Data", ""))

    # --- yo'llar ---
    def do_HEAD(self):
        self.do_GET()

    def do_GET(self):
        path = self.path.split("?", 1)[0]
        if path in ("/", "/index.html"):
            return self._file("index.html", "text/html; charset=utf-8")
        if path == "/health":
            return self._send(200, b"BS Track server is running.", "text/plain; charset=utf-8")
        if path == "/cover.jpg":
            return self._file("cover.jpg", "image/jpeg")
        if path == "/cover_thumb.jpg":
            return self._file("cover_thumb.jpg", "image/jpeg")
        if path == "/api/me":
            user = self._user()
            if not user:
                return self._json(401, {"ok": False, "error": "telegram"})
            if not is_allowed(user["id"]):
                self._notify_admin_once(user)
                return self._json(200, {"ok": True, "allowed": False, "id": user["id"]})
            return self._json(200, {"ok": True, "allowed": True, "id": user["id"],
                                    "name": user.get("first_name", ""), "config": public_config()})
        if path.startswith("/api/job/"):
            user = self._user()
            if not user or not is_allowed(user["id"]):
                return self._json(403, {"ok": False})
            with JOBS_LOCK:
                job = JOBS.get(path.rsplit("/", 1)[-1])
                if not job or job["uid"] != user["id"]:
                    return self._json(404, {"ok": False})
                return self._json(200, {"ok": True, "status": job["status"], "message": job.get("message", "")})
        self._send(404, b"Not found", "text/plain; charset=utf-8")

    def _notify_admin_once(self, user):
        uid = user["id"]
        if ADMIN_ID and BOT_LOOP and uid not in _web_notified and uid not in _notified:
            _web_notified.add(uid)
            text = (f"Mini App'da ruxsat so'ralmoqda:\n{user.get('first_name', '')} (@{user.get('username', '')})\n"
                    f"ID: {uid}\n\nRuxsat berish: /add {uid}")
            asyncio.run_coroutine_threadsafe(BOT_APP.bot.send_message(ADMIN_ID, text), BOT_LOOP)

    def do_POST(self):
        path = self.path.split("?", 1)[0]
        if path != "/api/render":
            return self._send(404, b"Not found", "text/plain; charset=utf-8")
        user = self._user()
        if not user:
            return self._json(401, {"ok": False, "error": "Telegram ichida oching."})
        if not is_allowed(user["id"]):
            return self._json(403, {"ok": False, "error": "Sizda ruxsat yo'q."})

        try:
            length = int(self.headers.get("Content-Length", "0"))
        except ValueError:
            length = 0
        if length <= 0:
            return self._json(400, {"ok": False, "error": "Fayl yuborilmadi."})
        if length > WEB_MAX_BYTES:
            return self._json(413, {"ok": False, "error": f"Fayl juda katta ({WEB_MAX_BYTES // (1024 * 1024)} MB gacha)."})

        _cleanup_jobs()
        with JOBS_LOCK:
            busy = [j for j in JOBS.values() if j["uid"] == user["id"] and j["status"] in ("queued", "processing", "sending")]
        if busy:
            return self._json(429, {"ok": False, "error": "Oldingi qo'shiq hali ishlanmoqda. Tugashini kuting."})

        try:
            params = sanitize_params(json.loads(unquote(self.headers.get("X-Params", "{}"))))
            raw_name = unquote(self.headers.get("X-Name", "")).strip()
        except Exception:
            return self._json(400, {"ok": False, "error": "Sozlamalar noto'g'ri."})
        if not raw_name or len(raw_name) > 100:
            return self._json(400, {"ok": False, "error": "Nom 1 dan 100 belgigacha bo'lsin."})

        import tempfile as _tf
        tmpdir = _tf.mkdtemp(prefix="web_")
        in_path = os.path.join(tmpdir, "input")
        try:
            remaining = length
            with open(in_path, "wb") as f:
                while remaining > 0:
                    chunk = self.rfile.read(min(65536, remaining))
                    if not chunk:
                        raise ConnectionError("upload uzildi")
                    f.write(chunk)
                    remaining -= len(chunk)
        except Exception:
            import shutil
            shutil.rmtree(tmpdir, ignore_errors=True)
            return self._json(400, {"ok": False, "error": "Fayl yuklanmadi. Qaytadan urinib ko'ring."})

        job_id = uuid.uuid4().hex
        with JOBS_LOCK:
            JOBS[job_id] = {"uid": user["id"], "status": "queued", "t": time.time()}
        threading.Thread(
            target=run_web_job, args=(job_id, user["id"], in_path, params, with_tag(raw_name)), daemon=True
        ).start()
        self._json(202, {"ok": True, "job": job_id})


def run_health():
    port = int(os.environ.get("PORT", 7860))
    srv = ThreadingHTTPServer(("0.0.0.0", port), Web)
    srv.daemon_threads = True
    srv.serve_forever()


# ====================== RUXSAT TIZIMI ======================
# ADMIN_ID    - sizning Telegram ID raqamingiz (Environment'ga yoziladi)
# ALLOWED_IDS - doimiy ruxsat berilganlar, vergul bilan: 111,222 (ixtiyoriy)
ADMIN_ID = int(os.environ.get("ADMIN_ID", "0") or 0)
if not ADMIN_ID:
    logger.warning("ADMIN_ID o'rnatilmagan! Faqat /myid ishlaydi.")

ENV_ALLOWED = {
    int(x) for x in os.environ.get("ALLOWED_IDS", "").replace(" ", "").split(",") if x.isdigit()
}
ALLOWED_FILE = os.path.join(BASE_DIR, "allowed_users.json")
_notified = set()


def load_dynamic() -> set:
    try:
        with open(ALLOWED_FILE, "r", encoding="utf-8") as f:
            return {int(x) for x in json.load(f)}
    except Exception:
        return set()


def save_dynamic(ids: set):
    try:
        with open(ALLOWED_FILE, "w", encoding="utf-8") as f:
            json.dump(sorted(ids), f)
    except Exception:
        logger.exception("Ruxsat ro'yxatini saqlab bo'lmadi")


def all_ids() -> set:
    return ENV_ALLOWED | load_dynamic()


def is_allowed(uid: int) -> bool:
    return uid == ADMIN_ID or uid in all_ids()


async def check_access(update: Update, context: ContextTypes.DEFAULT_TYPE) -> bool:
    user = update.effective_user
    if user and is_allowed(user.id):
        return True

    if update.callback_query:
        await update.callback_query.answer("Sizda ruxsat yo'q.", show_alert=True)
    elif update.message:
        await update.message.reply_text(
            "Kechirasiz, sizda bu botdan foydalanish uchun hozircha ruxsat yo'q.\n"
            f"Sizning ID raqamingiz: {user.id}\n"
            "Ruxsat olish uchun shu raqamni admin'ga yuboring."
        )
        if ADMIN_ID and user.id not in _notified:
            _notified.add(user.id)
            try:
                await context.bot.send_message(
                    ADMIN_ID,
                    f"Ruxsat so'ralmoqda:\n{user.full_name} (@{user.username})\n"
                    f"ID: {user.id}\n\nRuxsat berish: /add {user.id}",
                )
            except Exception:
                pass
    return False


def admin_only(func):
    async def wrapper(update: Update, context: ContextTypes.DEFAULT_TYPE):
        if not update.effective_user or update.effective_user.id != ADMIN_ID:
            return
        return await func(update, context)
    return wrapper


async def myid_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text(f"Sizning ID raqamingiz: {update.effective_user.id}")


@admin_only
async def add_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not context.args or not context.args[0].isdigit():
        await update.message.reply_text("Format: /add 123456789")
        return
    uid = int(context.args[0])
    ids = load_dynamic()
    ids.add(uid)
    save_dynamic(ids)
    await update.message.reply_text(
        f"Ruxsat berildi: {uid}\n\n"
        "Eslatma: bot qayta ishga tushsa, /add orqali qo'shilganlar o'chib ketishi mumkin. "
        "Doimiy qilish uchun Environment > ALLOWED_IDS ga quyidagini yozing:\n"
        f"{','.join(str(i) for i in sorted(all_ids()))}"
    )
    try:
        await context.bot.send_message(uid, "Sizga botdan foydalanishga ruxsat berildi. /start ni bosing.")
    except Exception:
        pass


@admin_only
async def remove_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not context.args or not context.args[0].isdigit():
        await update.message.reply_text("Format: /remove 123456789")
        return
    uid = int(context.args[0])
    ids = load_dynamic()
    ids.discard(uid)
    save_dynamic(ids)
    msg = f"Ruxsat olib tashlandi: {uid}"
    if uid in ENV_ALLOWED:
        msg += "\n(Bu ID Environment'dagi ALLOWED_IDS ichida ham bor, uni o'sha yerdan ham o'chiring.)"
    await update.message.reply_text(msg)


@admin_only
async def list_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    ids = sorted(all_ids())
    if not ids:
        await update.message.reply_text("Hozircha faqat siz (admin) ruxsatga egasiz.")
        return
    await update.message.reply_text("Ruxsat berilganlar:\n" + "\n".join(str(i) for i in ids))


# ====================== AUDIO ISHLOV ======================
class TooLong(Exception):
    pass


def change_speed(sound: AudioSegment, speed: float) -> AudioSegment:
    """Tezlik va tonni birga o'zgartiradi (slowed effekti shunday bo'ladi)."""
    if abs(speed - 1.0) < 1e-6:
        return sound
    new_rate = int(sound.frame_rate * speed)
    shifted = sound._spawn(sound.raw_data, overrides={"frame_rate": new_rate})
    return shifted.set_frame_rate(sound.frame_rate)


def build_board(p: dict) -> Pedalboard:
    """Tartib sizning qo'lda ishlash tartibingizdagidek:
    1) bass va reverb (slowedandreverb.studio), 2) ekvalayzer (EQ sayti)."""
    board = Pedalboard([])
    bass = p.get("bass", 0)
    if bass != 0:
        board.append(LowShelfFilter(cutoff_frequency_hz=100, gain_db=float(bass), q=0.7))
    rev = p.get("reverb", 0)
    if rev > 0:
        wet = rev / 100.0
        board.append(Reverb(
            room_size=min(0.9, 0.3 + wet * 0.6),
            damping=0.5,
            wet_level=wet,
            dry_level=1 - wet * 0.5,
            width=1.0,
        ))
    for freq, gain in zip(EQ_BANDS, p.get("eq", [0] * 9)):
        if gain != 0:
            board.append(PeakFilter(cutoff_frequency_hz=freq, gain_db=float(gain), q=1.0))
    return board


CHUNK_SEC = 10          # qo'shiq bo'laklab ishlanadi (xotirani tejash uchun)
STAGE_SCALE = 0.5       # oraliq saqlashda -6 dB zaxira
PEAK_CEILING = 0.89     # chiqish cho'qqisi chegarasi (~ -1 dBFS)


class Rotator8D:
    """Haqiqiy 3D (binaural) aylanish. Oddiy 8D'dagidek faqat balandlikni chapga-o'ngga surmaydi:
    1) ITD - tovush bir quloqqa ikkinchisidan sal oldin yetib boradi (~0.66 ms gacha),
    2) bosh soyasi - uzoq quloqda yuqori chastotalar so'nadi va sal past eshitiladi,
    3) old/orqa - orqada aylanganda tovush biroz xira va uzoqroq eshitiladi,
    4) bass (160 Hz dan past) markazda qoladi - u yo'nalishni sezdirmaydi, shuning uchun
       chap-o'ng tebranish va bass "sakrashi" bo'lmaydi,
    5) asl stereo kengligi (side) saqlanadi.
    Bo'laklab ishlashi uchun filtr holatlari va kechikish tarixi bo'laklar orasida saqlanadi."""

    ITD_MAX = 0.00066        # soniya
    HIST = 128               # kechikish tarixi (namuna)
    SPEED_HZ = 0.08          # to'liq aylanish ~12,5 soniyada
    SIDE_GAIN = 0.7

    def __init__(self, sr: int):
        self.sr = sr
        self.pos = 0
        self.low_board = Pedalboard([LowpassFilter(160.0), LowpassFilter(160.0)])
        self.shadow_board = Pedalboard([LowpassFilter(3200.0)])
        self.hist_l = np.zeros(self.HIST, dtype=np.float32)
        self.hist_r = np.zeros(self.HIST, dtype=np.float32)

    def _delay(self, sig, d, hist):
        n = sig.shape[0]
        buf = np.concatenate([hist, sig])
        pos = np.arange(n, dtype=np.float64) + self.HIST - d
        i0 = np.floor(pos).astype(np.int64)
        fr = (pos - i0).astype(np.float32)
        i1 = np.minimum(i0 + 1, buf.shape[0] - 1)
        out = buf[i0] * (1.0 - fr) + buf[i1] * fr
        return out.astype(np.float32), buf[-self.HIST:].copy()

    def process(self, x: np.ndarray) -> np.ndarray:
        sr = self.sr
        left, right = x[0], x[1]
        mid = (left + right) * 0.5
        side = (left - right) * 0.5
        n = mid.shape[0]

        low = self.low_board(mid[None, :], sr, reset=False)[0]
        high = mid - low
        shadow = self.shadow_board(high[None, :], sr, reset=False)[0]

        t = (np.arange(n, dtype=np.float64) + self.pos) / sr
        self.pos += n
        theta = 2.0 * np.pi * self.SPEED_HZ * t
        lat = np.sin(theta).astype(np.float32)           # +1 = to'liq o'ng, -1 = to'liq chap
        back = np.maximum(0.0, -np.cos(theta)).astype(np.float32)   # 0 = old, 1 = orqa
        to_r = np.maximum(0.0, lat)                       # manba o'ngda: chap quloq uzoq
        to_l = np.maximum(0.0, -lat)                      # manba chapda: o'ng quloq uzoq

        w_l = np.clip(0.7 * to_r + 0.5 * back, 0.0, 0.9)
        w_r = np.clip(0.7 * to_l + 0.5 * back, 0.0, 0.9)
        g_l = 1.0 - 0.3 * to_r + 0.1 * to_l - 0.08 * back
        g_r = 1.0 - 0.3 * to_l + 0.1 * to_r - 0.08 * back
        ear_l = g_l * ((1.0 - w_l) * high + w_l * shadow)
        ear_r = g_r * ((1.0 - w_r) * high + w_r * shadow)

        ear_l, self.hist_l = self._delay(ear_l, self.ITD_MAX * to_r * sr, self.hist_l)
        ear_r, self.hist_r = self._delay(ear_r, self.ITD_MAX * to_l * sr, self.hist_r)

        out_l = low + ear_l + self.SIDE_GAIN * side
        out_r = low + ear_r - self.SIDE_GAIN * side
        return np.vstack([out_l, out_r]).astype(np.float32)


class SubBass:
    """Haqiqiy sub-bass (Lowbass). Oddiy bass tovushni shunchaki baland qiladi, bu esa yangi past chastota yaratadi:
    1) 110 Hz dan past qism ajratiladi (kick + bass chizig'i),
    2) oktava pastga tushiriladi (chastota bo'luvchi: 80 Hz -> 40 Hz) va silliqlanadi - chuqur "gumburlash",
    3) yengil to'yintirish (saturatsiya) 2-3 garmonika qo'shadi - kichik dinamikda va telefonda ham eshitiladi.
    Holat bo'laklar orasida saqlanadi."""

    def __init__(self, sr: int, amount: float):
        self.sr = sr
        self.amt = max(0.0, min(1.0, amount / 100.0))
        self.lp_in = Pedalboard([LowpassFilter(110.0), LowpassFilter(110.0)])
        self.lp_sub = Pedalboard([LowpassFilter(70.0), LowpassFilter(70.0)])
        self.lp_env = Pedalboard([LowpassFilter(18.0)])
        self.lp_harm = Pedalboard([LowpassFilter(220.0), LowpassFilter(220.0)])
        self.state = 1.0
        self.prev = 0.0

    def process(self, x: np.ndarray) -> np.ndarray:
        sr = self.sr
        mid = ((x[0] + x[1]) * 0.5).astype(np.float32)
        low = self.lp_in(mid[None, :], sr, reset=False)[0]

        # oktava pastga: musbat tomonga o'tishlarda holat almashadi (80 Hz -> 40 Hz kvadrat to'lqin)
        ext = np.concatenate([[self.prev], low])
        cross = (ext[:-1] <= 0.0) & (ext[1:] > 0.0)
        parity = (np.cumsum(cross) & 1).astype(np.float32)
        sq = (self.state * np.where(parity > 0, -1.0, 1.0)).astype(np.float32)
        if int(cross.sum()) & 1:
            self.state = -self.state
        self.prev = float(low[-1])

        env = self.lp_env(np.abs(low)[None, :].astype(np.float32), sr, reset=False)[0] * 1.8
        sub = self.lp_sub(sq[None, :], sr, reset=False)[0] * np.minimum(env, 0.6) * 1.6

        drive = np.tanh(low * 4.0) * 0.5
        harm = self.lp_harm(drive[None, :].astype(np.float32), sr, reset=False)[0]

        add = (sub * 1.0 + harm * 0.6 + low * 0.5) * self.amt * 1.6
        return (x + add[None, :]).astype(np.float32)


def build_pre_board_8d(p: dict) -> Pedalboard:
    """8D uchun aylanishdan OLDIN: bass va ekvalayzer."""
    board = Pedalboard([])
    bass = p.get("bass", 0)
    if bass != 0:
        board.append(LowShelfFilter(cutoff_frequency_hz=100, gain_db=float(bass), q=0.7))
    for freq, gain in zip(EQ_BANDS, p.get("eq", [0] * 9)):
        if gain != 0:
            board.append(PeakFilter(cutoff_frequency_hz=freq, gain_db=float(gain), q=1.0))
    return board


def build_post_board_8d(p: dict) -> Pedalboard:
    """8D uchun aylanishdan KEYIN: yengil xona (reverb) - atrofdagi makon hissi uchun."""
    board = Pedalboard([])
    rev = p.get("reverb", 0)
    if rev > 0:
        wet = min(rev, 60) / 100.0
        board.append(Reverb(room_size=min(0.9, 0.35 + wet * 0.5), damping=0.6,
                            wet_level=wet, dry_level=1 - wet * 0.4, width=1.0))
    return board


def render_audio(in_path: str, out_path: str, p: dict, title: str, artist: str):
    """Bo'laklab ishlov berish: xotira kam sarflanadi (Render bepul tarifi uchun muhim)."""
    sound = AudioSegment.from_file(in_path)
    if len(sound) > MAX_DURATION_SEC * 1000:
        raise TooLong()
    sound = sound.set_channels(2).set_sample_width(2)
    sound = change_speed(sound, float(p.get("speed", 1.0)))
    sr = sound.frame_rate

    data = np.frombuffer(sound.raw_data, dtype=np.int16).reshape(-1, 2)   # nusxasiz ko'rinish
    n_total = data.shape[0]
    step = sr * CHUNK_SEC

    # 1) asl qo'shiq balandligi (RMS)
    sumsq = 0.0
    for i in range(0, n_total, step):
        c = data[i:i + step].astype(np.float32) / 32768.0
        sumsq += float(np.sum(c.astype(np.float64) ** 2))
    ref_rms = (sumsq / (n_total * 2)) ** 0.5 + 1e-9

    # 2) effektlar (oddiy: bass > reverb > EQ; 8D: bass+EQ > aylanish > reverb), bo'laklab
    is_8d = bool(p.get("is_8d"))
    if is_8d:
        pre_board = build_pre_board_8d(p)
        rotator = Rotator8D(sr)
        post_board = build_post_board_8d(p)
    else:
        board = build_board(p)
    subbass = SubBass(sr, float(p.get("sub", 0))) if p.get("sub", 0) > 0 else None
    tail = sr * 2 if p.get("reverb", 0) > 0 else 0        # reverb dumi uchun 2 soniya
    out_len = n_total + tail
    stage = np.empty((out_len, 2), dtype=np.int16)
    sumsq2 = 0.0
    for i in range(0, out_len, step):
        j = min(i + step, out_len)
        chunk = np.zeros((j - i, 2), dtype=np.float32)
        real_end = min(j, n_total)
        if real_end > i:
            chunk[: real_end - i] = data[i:real_end].astype(np.float32) / 32768.0
        chunk = np.ascontiguousarray(chunk.T)
        if subbass is not None:
            chunk = subbass.process(chunk)
        if is_8d:
            if len(pre_board) > 0:
                chunk = pre_board(chunk, sr, reset=False)
            chunk = rotator.process(chunk)
            if len(post_board) > 0:
                chunk = post_board(chunk, sr, reset=False)
        elif len(board) > 0:
            chunk = board(chunk, sr, reset=False)
        valid = max(0, real_end - i)
        if valid:
            sumsq2 += float(np.sum(chunk[:, :valid].astype(np.float64) ** 2))
        stage[i:j] = (np.clip(chunk * STAGE_SCALE, -1.0, 1.0).T * 32767.0).astype(np.int16)
    del data, sound
    proc_rms = (sumsq2 / (n_total * 2)) ** 0.5 + 1e-9

    # 3) balandlikni asl qo'shiqqa tenglashtirish. Limiter o'zi balandlikni oshirib yuboradi,
    #    shuning uchun uni o'tkazgach qayta o'lchab, asl qo'shiq balandligiga qaytaramiz.
    gain = min(max(ref_rms / proc_rms, 0.25), 4.0)                  # -12 dB ... +12 dB
    gain_total = gain / STAGE_SCALE
    limiter = Pedalboard([Limiter(threshold_db=-1.5, release_ms=100.0)])
    sumsq3 = 0.0
    for i in range(0, out_len, step):
        j = min(i + step, out_len)
        c = stage[i:j].astype(np.float32) / 32768.0 * gain_total
        c = np.ascontiguousarray(c.T)
        c = limiter(c, sr, reset=False)
        c = np.clip(c, -1.0, 1.0)
        valid = max(0, min(j, n_total) - i)
        if valid:
            sumsq3 += float(np.sum(c[:, :valid].astype(np.float64) ** 2))
        stage[i:j] = (c.T * 32767.0).astype(np.int16)
    post_rms = (sumsq3 / (n_total * 2)) ** 0.5 + 1e-9
    final = min(ref_rms / post_rms, PEAK_CEILING)                    # cho'qqilar -1 dBFS dan oshmasin
    for i in range(0, out_len, step):
        j = min(i + step, out_len)
        stage[i:j] = (stage[i:j].astype(np.float32) * final).astype(np.int16)

    out = AudioSegment(stage.tobytes(), frame_rate=sr, sample_width=2, channels=2)
    del stage
    out.export(out_path, format="mp3", bitrate=p.get("bitrate", "320k"))
    del out

    write_tags(out_path, title, artist)


class RenderTimeout(Exception):
    pass


class RenderKilled(Exception):
    """Jarayon tizim tomonidan o'chirildi (ko'pincha xotira yetmaganidan)."""


def _render_worker(in_path, out_path, p, title, artist):
    """Alohida jarayonda ishlaydi. Chiqish kodi: 0 - yaxshi, 3 - juda uzun, 1 - xato."""
    try:
        render_audio(in_path, out_path, p, title, artist)
    except TooLong:
        os._exit(3)
    except Exception:
        traceback.print_exc()
        os._exit(1)
    os._exit(0)


def render_in_subprocess(in_path, out_path, p, title, artist, timeout):
    """Ishlovni alohida jarayonda bajaradi: qotib qolsa o'chiriladi, xotira yetmasa
    bot o'zi tirik qoladi va foydalanuvchiga xabar beradi."""
    ctx = mp.get_context("spawn")
    proc = ctx.Process(target=_render_worker, args=(in_path, out_path, p, title, artist))
    proc.start()
    proc.join(timeout)
    if proc.is_alive():
        proc.terminate()
        proc.join(5)
        if proc.is_alive():
            proc.kill()
            proc.join()
        raise RenderTimeout()
    code = proc.exitcode
    if code == 0 and os.path.exists(out_path):
        return
    if code == 3:
        raise TooLong()
    if code is not None and code < 0:
        raise RenderKilled(f"signal {-code}")
    raise RuntimeError(f"render jarayoni xato bilan tugadi (kod {code})")


def write_tags(path: str, title: str, artist: str):
    """Nom, ijrochi va muqovani yozadi. Eski muqova qayta kodlash paytida allaqachon yo'qolgan."""
    try:
        tags = ID3(path)
        tags.delete()
        tags = ID3()
    except ID3NoHeaderError:
        tags = ID3()
    tags.add(TIT2(encoding=3, text=title))
    if artist:
        tags.add(TPE1(encoding=3, text=artist))
    if os.path.exists(COVER_PATH):
        with open(COVER_PATH, "rb") as f:
            tags.add(APIC(encoding=3, mime="image/jpeg", type=3, desc="Cover", data=f.read()))
    else:
        logger.warning("cover.jpg topilmadi! Muqova qo'yilmadi. GitHub'ga cover.jpg yuklang.")
    tags.save(path, v2_version=3)


# ====================== NOM YORDAMCHILARI ======================
def with_tag(name: str) -> str:
    """Oxiriga (BStrack) qo'shadi. Allaqachon bo'lsa, ikki marta qo'shmaydi."""
    name = name.strip()
    name = re.sub(r"\s*\(\s*bstrack\s*\)\s*$", "", name, flags=re.IGNORECASE).strip()
    return f"{name} {TAG}".strip()


def split_name(full: str):
    """'Artist - Nom (BStrack)' -> (artist, 'Nom (BStrack)')"""
    if " - " in full:
        artist, title = full.split(" - ", 1)
        return artist.strip(), title.strip()
    return "", full.strip()


def safe_filename(name: str) -> str:
    name = re.sub(r'[\\/:*?"<>|]', "", name).strip()
    return (name[:110] or "track") + ".mp3"


# ====================== MATNLAR VA TUGMALAR ======================
GREETING = (
    "🇺🇿 Assalomu alaykum, {name}!\n"
    "🎧 Men BSTrack botiman: Qo'shig'ingizni chiroyli effektga keltirib beraman. "
    "Originalini eshitgingiz kelmay qoladi 😁💯\n\n"
    "😉 Nimalar qila olaman:\n"
    "📌 Reverb va Slowed + Reverb, bu eng Top effekt 🚀\n"
    "📌 Bass Boost va Lowbass, Kuchli bass va muloyim bass 🚀\n"
    "📌 8D Audio, tovush boshingiz atrofida aylanadi (quloqchin taqing!) 🚀\n"
    "📌 Pitch Up, ovoz balandlashadi. Tezlik 🚀\n"
    "📌 Qo'lda sozlash 🚀\n"
    "📌 Nomlash (Tag editor) 🚀\n\n"
    "‼️ Qo'shiq {minutes} daqiqadan oshmasin va {max_mb} MB dan katta bo'lmasin (bot ham charchaydi 😅).\n\n"
    "👀 Tez orada: ovozni olib tashlash (vocal remover), xonanda dam olib turadi 😄\n\n"
    "Boshlash uchun Web sahifaga o'ting 😉"
)

# Premium (maxsus) emodzilar. Bu yerga «oddiy emodzi: custom_emoji_id» ko'rinishida qo'shiladi.
# Bo'sh bo'lsa, oddiy emodzilar ko'rinadi. ID olish yo'li: README/yo'riqnoma.
CUSTOM_EMOJI = {
    # "🎧": "5368324170671202286",
}


def emoji_entities(text: str):
    """Matndagi emodzilarni premium emodzilarga almashtiradigan entity'lar (UTF-16 bo'yicha)."""
    ents = []
    for ch, cid in CUSTOM_EMOJI.items():
        start = 0
        while True:
            i = text.find(ch, start)
            if i < 0:
                break
            off = len(text[:i].encode("utf-16-le")) // 2
            ents.append(MessageEntity(type=MessageEntity.CUSTOM_EMOJI, offset=off,
                                      length=len(ch.encode("utf-16-le")) // 2, custom_emoji_id=cid))
            start = i + len(ch)
    return ents or None


SAVE_HINT = (
    "Saqlash uchun: qo'shiqni bosib turing > «Forward» (boshqaga yoki Saved Messages'ga) "
    "yoki «Save to Music / Save to Files»."
)


def main_menu() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup([
        [InlineKeyboardButton("Reverb", callback_data="p:reverb"),
         InlineKeyboardButton("Slowed + Reverb", callback_data="p:slowed")],
        [InlineKeyboardButton("Bass Boost", callback_data="p:bass"),
         InlineKeyboardButton("Lowbass", callback_data="p:lowbass")],
        [InlineKeyboardButton("8D Audio", callback_data="p:8d"),
         InlineKeyboardButton("Pitch Up", callback_data="p:pitchup")],
        [InlineKeyboardButton("Qo'lda sozlash", callback_data="m:custom")],
        [InlineKeyboardButton("Bekor qilish", callback_data="n:cancel")],
    ])


def onoff(v: bool) -> str:
    return "yoqilgan" if v else "o'chiq"


def fmt_db(v) -> str:
    return f"{v:+d} dB" if v else "0 dB"


def custom_text(p: dict) -> str:
    return (
        "Qo'lda sozlash\n\n"
        f"Tezlik: {p['speed']:.2f}x\n"
        f"Reverb: {p['reverb']}%\n"
        f"Bass: {fmt_db(p['bass'])}\n"
        f"Sub-bass: {p['sub']}%\n"
        f"8D: {onoff(p['is_8d'])}\n\n"
        "«−» va «+» tugmalari bilan o'zgartiring, tayyor bo'lgach «Tayyor» ni bosing."
    )


def custom_menu(p: dict) -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup([
        [InlineKeyboardButton("Tezlik −", callback_data="c:speed:-"),
         InlineKeyboardButton(f"{p['speed']:.2f}x", callback_data="noop"),
         InlineKeyboardButton("Tezlik +", callback_data="c:speed:+")],
        [InlineKeyboardButton("Reverb −", callback_data="c:reverb:-"),
         InlineKeyboardButton(f"{p['reverb']}%", callback_data="noop"),
         InlineKeyboardButton("Reverb +", callback_data="c:reverb:+")],
        [InlineKeyboardButton("Bass −", callback_data="c:bass:-"),
         InlineKeyboardButton(fmt_db(p['bass']), callback_data="noop"),
         InlineKeyboardButton("Bass +", callback_data="c:bass:+")],
        [InlineKeyboardButton("Sub-bass −", callback_data="c:sub:-"),
         InlineKeyboardButton(f"{p['sub']}%", callback_data="noop"),
         InlineKeyboardButton("Sub-bass +", callback_data="c:sub:+")],
        [InlineKeyboardButton(f"8D: {onoff(p['is_8d'])} (almashtirish)", callback_data="c:8d")],
        [InlineKeyboardButton("Ekvalayzer (EQ)", callback_data="c:eq")],
        [InlineKeyboardButton("Boshlang'ich holat", callback_data="c:reset"),
         InlineKeyboardButton("Tayyor", callback_data="c:done")],
        [InlineKeyboardButton("Orqaga", callback_data="c:back")],
    ])


def eq_text() -> str:
    return "Ekvalayzer\n\nHar bir polosani «−» va «+» bilan o'zgartiring (−12 dan +12 dB gacha)."


def eq_menu(p: dict) -> InlineKeyboardMarkup:
    rows = []
    for i, label in enumerate(EQ_LABELS):
        rows.append([
            InlineKeyboardButton("−", callback_data=f"e:{i}:-"),
            InlineKeyboardButton(f"{label}: {fmt_db(p['eq'][i])}", callback_data="noop"),
            InlineKeyboardButton("+", callback_data=f"e:{i}:+"),
        ])
    rows.append([InlineKeyboardButton("EQ ni nolga tushirish", callback_data="e:reset"),
                 InlineKeyboardButton("Orqaga", callback_data="e:back")])
    return InlineKeyboardMarkup(rows)


async def safe_edit(query, text: str, markup=None):
    try:
        await query.edit_message_text(text, reply_markup=markup)
    except BadRequest as e:
        if "not modified" not in str(e).lower():
            raise


def clamp(v, lo, hi):
    return max(lo, min(hi, v))


# ====================== HANDLERLAR ======================
def webapp_url() -> str:
    url = os.environ.get("WEBAPP_URL") or os.environ.get("RENDER_EXTERNAL_URL") or ""
    return url.rstrip("/")


def studio_markup():
    url = webapp_url()
    if not url.startswith("https://"):
        return None
    return InlineKeyboardMarkup([[InlineKeyboardButton("🎛 Web sahifani ochish", web_app=WebAppInfo(url=url))]])


async def start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not await check_access(update, context):
        return
    name = update.effective_user.first_name or "do'st"
    markup = studio_markup()
    text = GREETING.format(name=name, minutes=MAX_MINUTES, max_mb=WEB_MAX_BYTES // (1024 * 1024))
    if not markup:
        text += "\n\n⚠️ Studiya manzili sozlanmagan (Render'da WEBAPP_URL ni kiriting)."
    await update.message.reply_text(text, reply_markup=markup, entities=emoji_entities(text))


async def redirect_to_studio(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Audio yoki matn yuborilsa, hamma ish Studiyada ekanini eslatadi."""
    if not await check_access(update, context):
        return
    await update.message.reply_text(
        "🎛 Hamma ish endi Studiyada: qo'shiqni shu yerdan emas, pastdagi tugma orqali yuklang.",
        reply_markup=studio_markup(),
    )


async def handle_audio(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not await check_access(update, context):
        return
    msg = update.message
    file_obj = msg.audio or msg.document
    if not file_obj:
        return

    if msg.document and not (msg.document.mime_type or "").startswith("audio"):
        await msg.reply_text("Iltimos, audio fayl (mp3, m4a, wav...) yuboring.")
        return

    size = getattr(file_obj, "file_size", 0) or 0
    if size > TG_DOWNLOAD_LIMIT:
        await msg.reply_text(
            "Kechirasiz, fayl juda katta. Telegram bot 20 MB gacha fayl qabul qiladi.\n"
            "Iltimos, mp3 formatdagi kichikroq fayl yuboring."
        )
        return

    duration = getattr(file_obj, "duration", None)
    if duration and duration > MAX_DURATION_SEC:
        await msg.reply_text(
            f"Kechirasiz, qo'shiq juda uzun ({duration // 60}:{duration % 60:02d}). "
            f"Hozircha {MAX_MINUTES} daqiqagacha qabul qilaman."
        )
        return

    context.user_data.clear()
    context.user_data["file_id"] = file_obj.file_id
    context.user_data["orig_title"] = getattr(file_obj, "title", None) or ""
    context.user_data["orig_artist"] = getattr(file_obj, "performer", None) or ""
    context.user_data["orig_filename"] = getattr(file_obj, "file_name", None) or ""
    context.user_data["custom"] = copy.deepcopy(DEFAULT_CUSTOM)

    await msg.reply_text(
        "Qo'shiq qabul qilindi. Qanday ishlov beramiz?",
        reply_markup=main_menu(),
    )


async def ask_name(query, context: ContextTypes.DEFAULT_TYPE):
    context.user_data["awaiting_name"] = True
    t, a = context.user_data.get("orig_title", ""), context.user_data.get("orig_artist", "")
    orig = f"{a} - {t}" if a and t else (t or "")
    if not orig:
        # audio ichida nom yo'q bo'lsa, fayl nomidan olamiz
        fn = context.user_data.get("orig_filename", "")
        fn = re.sub(r"\.[A-Za-z0-9]{2,5}$", "", fn).replace("_", " ")
        orig = re.sub(r"\s+", " ", fn).strip()

    rows = []
    if orig:
        context.user_data["orig_full"] = orig
        rows.append([InlineKeyboardButton("✅ Asl nomni qoldirish", callback_data="n:keep")])
    rows.append([InlineKeyboardButton("Orqaga", callback_data="n:back"),
                 InlineKeyboardButton("Bekor qilish", callback_data="n:cancel")])

    text = (
        "Qo'shiq nomini yozing.\n"
        f"Masalan: Artist - Qo'shiq nomi\n\n"
        f"Oxiriga {TAG} ni o'zim qo'shaman."
    )
    if orig:
        text += f"\n\nAsl nomi: {orig}"
    await safe_edit(query, text, InlineKeyboardMarkup(rows))


async def button_handler(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not await check_access(update, context):
        return
    query = update.callback_query
    await query.answer()
    data = query.data
    ud = context.user_data

    if data == "noop":
        return

    if not ud.get("file_id") and data not in ("noop",):
        await safe_edit(query, "Qo'shiq topilmadi. Iltimos, qo'shiqni qaytadan yuboring.")
        return

    # --- standart rejimlar ---
    if data.startswith("p:"):
        key = data[2:]
        if key not in PRESETS:
            await safe_edit(query, "Noma'lum rejim.")
            return
        ud["params"] = copy.deepcopy(PRESETS[key])
        ud["name_back"] = "main"
        await ask_name(query, context)
        return

    # --- qo'lda sozlash ---
    if data == "m:custom":
        await safe_edit(query, custom_text(ud["custom"]), custom_menu(ud["custom"]))
        return

    if data.startswith("c:"):
        p = ud.setdefault("custom", copy.deepcopy(DEFAULT_CUSTOM))
        parts = data.split(":")
        action = parts[1]
        sign = 1 if len(parts) > 2 and parts[2] == "+" else -1

        if action == "speed":
            p["speed"] = round(clamp(p["speed"] + 0.05 * sign, 0.5, 1.5), 2)
        elif action == "reverb":
            p["reverb"] = clamp(p["reverb"] + 5 * sign, 0, 100)
        elif action == "bass":
            p["bass"] = clamp(p["bass"] + 2 * sign, -12, 12)
        elif action == "sub":
            p["sub"] = clamp(p.get("sub", 0) + 10 * sign, 0, 100)
        elif action == "back":
            await safe_edit(query, "Qanday ishlov beramiz?", main_menu())
            return
        elif action == "8d":
            p["is_8d"] = not p["is_8d"]
        elif action == "reset":
            ud["custom"] = copy.deepcopy(DEFAULT_CUSTOM)
            p = ud["custom"]
        elif action == "eq":
            await safe_edit(query, eq_text(), eq_menu(p))
            return
        elif action == "done":
            ud["params"] = copy.deepcopy(p)
            ud["name_back"] = "custom"
            await ask_name(query, context)
            return

        await safe_edit(query, custom_text(p), custom_menu(p))
        return

    # --- ekvalayzer ---
    if data.startswith("e:"):
        p = ud.setdefault("custom", copy.deepcopy(DEFAULT_CUSTOM))
        parts = data.split(":")
        if parts[1] == "reset":
            p["eq"] = [0] * 9
        elif parts[1] == "back":
            await safe_edit(query, custom_text(p), custom_menu(p))
            return
        else:
            i = int(parts[1])
            sign = 1 if parts[2] == "+" else -1
            p["eq"][i] = clamp(p["eq"][i] + sign, -12, 12)
        await safe_edit(query, eq_text(), eq_menu(p))
        return

    # --- nom ---
    if data == "n:keep":
        orig = ud.get("orig_full", "")
        if not orig:
            await safe_edit(query, "Asl nom topilmadi. Iltimos, nomni yozib yuboring.")
            return
        ud["awaiting_name"] = False
        await run_job(query.message, context, orig)
        return

    if data == "n:back":
        ud["awaiting_name"] = False
        if ud.get("name_back") == "custom":
            p = ud.setdefault("custom", copy.deepcopy(DEFAULT_CUSTOM))
            await safe_edit(query, custom_text(p), custom_menu(p))
        else:
            await safe_edit(query, "Qanday ishlov beramiz?", main_menu())
        return

    if data == "n:cancel":
        ud["awaiting_name"] = False
        await safe_edit(query, "Bekor qilindi. Xohlasangiz, boshqa effekt tanlang:", main_menu())
        return

    if data == "again":
        await safe_edit(query, "Qanday ishlov beramiz?", main_menu())
        return


async def handle_text(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not await check_access(update, context):
        return
    ud = context.user_data
    if not ud.get("awaiting_name"):
        await update.message.reply_text("Boshlash uchun menga qo'shiq (audio fayl) yuboring.")
        return

    name = (update.message.text or "").strip()
    if not name:
        return
    if len(name) > 100:
        await update.message.reply_text("Nom juda uzun (100 belgigacha). Qisqaroq yozing.")
        return

    ud["awaiting_name"] = False
    await run_job(update.message, context, name)


def locked_render(*args):
    with RENDER_LOCK:
        render_in_subprocess(*args)


JOB_LOCK = asyncio.Semaphore(1)   # bir vaqtda bitta qo'shiq (xotirani tejash uchun)


async def run_job(msg, context: ContextTypes.DEFAULT_TYPE, raw_name: str):
    """msg - javob yoziladigan xabar (chat aniqlash uchun)."""
    ud = context.user_data
    file_id = ud.get("file_id")
    params = ud.get("params")
    if not file_id or not params:
        await msg.reply_text("Qo'shiq topilmadi. Iltimos, qaytadan yuboring.")
        return

    chat_id = msg.chat_id
    full_name = with_tag(raw_name)
    artist, title = split_name(full_name)

    queue_note = " Navbat kutilmoqda..." if JOB_LOCK.locked() else ""
    status = await context.bot.send_message(chat_id, f"Ishlov berilmoqda, biroz kuting...{queue_note}")

    try:
        async with JOB_LOCK:
            tg_file = await context.bot.get_file(file_id)
            with tempfile.TemporaryDirectory() as tmp:
                in_path = os.path.join(tmp, "input")
                out_path = os.path.join(tmp, "output.mp3")
                await tg_file.download_to_drive(in_path)
                await asyncio.to_thread(locked_render, in_path, out_path, params, title, artist, RENDER_TIMEOUT)

                thumb = open(THUMB_PATH, "rb") if os.path.exists(THUMB_PATH) else None
                try:
                    with open(out_path, "rb") as f:
                        await context.bot.send_audio(
                            chat_id=chat_id,
                            audio=f,
                            filename=safe_filename(full_name),
                            title=title,
                            performer=artist or None,
                            thumbnail=thumb,
                            caption=f"Tayyor: {params.get('title', '')}",
                            read_timeout=120,
                            write_timeout=300,
                            connect_timeout=30,
                        )
                finally:
                    if thumb:
                        thumb.close()

        try:
            await status.delete()
        except Exception:
            pass

        await context.bot.send_message(
            chat_id,
            SAVE_HINT + "\n\nShu qo'shiqqa boshqa effekt berasizmi?",
            reply_markup=InlineKeyboardMarkup(
                [[InlineKeyboardButton("Boshqa effekt", callback_data="again")]]
            ),
        )

    except TooLong:
        await status.edit_text(f"Qo'shiq juda uzun. Hozircha {MAX_MINUTES} daqiqagacha qabul qilaman.")
    except RenderTimeout:
        logger.error("Ishlov vaqti tugadi (%s s)", RENDER_TIMEOUT)
        await status.edit_text(
            "Ishlov juda uzoq davom etdi va to'xtatildi. Qisqaroq qo'shiq bilan urinib ko'ring."
        )
        await notify_admin(context, f"Ishlov vaqti tugadi ({RENDER_TIMEOUT} s). Server sekin bo'lishi mumkin.")
    except RenderKilled as e:
        logger.error("Ishlov jarayoni o'chirildi: %s", e)
        await status.edit_text(
            "Server xotirasi yetmadi. Qisqaroq yoki kichikroq qo'shiq bilan urinib ko'ring."
        )
        await notify_admin(context, f"Ishlov jarayoni tizim tomonidan o'chirildi ({e}). Xotira yetmagan bo'lishi mumkin.")
    except BadRequest as e:
        logger.exception("Telegram xatosi")
        if "too big" in str(e).lower():
            await status.edit_text("Fayl juda katta (20 MB dan oshiq). Kichikroq mp3 yuboring.")
        else:
            await status.edit_text("Telegram bilan xatolik yuz berdi. Qaytadan urinib ko'ring.")
    except Exception as e:
        logger.exception("Ishlov berishda xatolik")
        await status.edit_text(
            "Kechirasiz, ishlov berishda xatolik yuz berdi. Boshqa fayl bilan urinib ko'ring "
            "yoki keyinroq qaytadan yuboring."
        )
        await notify_admin(context, f"Ishlov xatosi: {type(e).__name__}: {str(e)[:300]}")


async def notify_admin(context: ContextTypes.DEFAULT_TYPE, text: str):
    if not ADMIN_ID:
        return
    try:
        await context.bot.send_message(ADMIN_ID, "Diqqat: " + text)
    except Exception:
        pass


async def on_error(update, context: ContextTypes.DEFAULT_TYPE):
    err = context.error
    if isinstance(err, Conflict):
        logger.error(
            "CONFLICT: shu token bilan boshqa bot nusxasi ham ishlayapti! "
            "Render'da faqat bitta servis ishlashi kerak (eski servislarni o'chiring)."
        )
    else:
        logger.error("Kutilmagan xato: %s", err, exc_info=err)


async def _post_init(app):
    global BOT_APP, BOT_LOOP
    BOT_APP = app
    BOT_LOOP = asyncio.get_running_loop()
    url = webapp_url()
    if url.startswith("https://"):
        try:
            await app.bot.set_chat_menu_button(
                menu_button=MenuButtonWebApp(text="Studio", web_app=WebAppInfo(url=url)))
        except Exception:
            logger.exception("Menu tugmasini o'rnatib bo'lmadi")


def main():
    threading.Thread(target=run_health, daemon=True).start()

    app = (
        Application.builder()
        .token(BOT_TOKEN)
        .read_timeout(60)
        .write_timeout(120)
        .connect_timeout(30)
        .post_init(_post_init)
        .build()
    )
    app.add_handler(CommandHandler("start", start))
    app.add_handler(CommandHandler("help", start))
    app.add_handler(CommandHandler("myid", myid_cmd))
    app.add_handler(CommandHandler("add", add_cmd))
    app.add_handler(CommandHandler("remove", remove_cmd))
    app.add_handler(CommandHandler("list", list_cmd))
    app.add_handler(MessageHandler(filters.AUDIO | filters.Document.AUDIO, redirect_to_studio))
    app.add_handler(CallbackQueryHandler(button_handler))
    app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, redirect_to_studio))
    app.add_error_handler(on_error)
    logger.info("Bot ishga tushdi")
    app.run_polling(drop_pending_updates=True)


if __name__ == "__main__":
    main()
