"""
BS Track Remake Bot  -  1-bosqich
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
from http.server import BaseHTTPRequestHandler, HTTPServer

import numpy as np
from pydub import AudioSegment
from pedalboard import Pedalboard, Reverb, PeakFilter, LowShelfFilter, Limiter
from mutagen.id3 import ID3, TIT2, TPE1, APIC, ID3NoHeaderError

from telegram import Update, InlineKeyboardButton, InlineKeyboardMarkup
from telegram.error import BadRequest
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

EQ_BANDS = [60, 170, 310, 600, 1000, 3000, 6000, 12000, 16000]
EQ_LABELS = ["60 Hz", "170 Hz", "310 Hz", "600 Hz", "1 kHz", "3 kHz", "6 kHz", "12 kHz", "16 kHz"]

# Standart rejimlar (bu qiymatlarni xohlasangiz shu yerdan o'zgartirasiz)
# speed: 1.0 = asl tezlik | reverb: 0-100 % | bass: dB (-12..+12) | eq: 9 ta polosa dB
BASE_EQ = [2, -3, -3, 0, 0, 2, 5, 7, 8]   # EQ saytidagi standart sozlamangiz

PRESETS = {
    "reverb": {
        "title": "Reverb",
        "speed": 1.0, "reverb": 45, "bass": 0, "eq": BASE_EQ, "is_8d": False, "bitrate": "320k",
    },
    "slowed": {
        "title": "Slowed + Reverb",
        "speed": 0.90, "reverb": 40, "bass": 0, "eq": [2, -4, -4, 0, 0, 2, 5, 7, 8], "is_8d": False, "bitrate": "320k",
    },
    "bass": {
        "title": "Bass Boost",
        "speed": 1.0, "reverb": 25, "bass": 8, "eq": [10, 8, 4, 1, 0, 0, 0, 0, 0], "is_8d": False, "bitrate": "320k",
    },
    "8d": {
        "title": "8D Audio",
        "speed": 1.0, "reverb": 45, "bass": 2, "eq": [5, 3, 0, 0, 1, 2, 3, 2, 1], "is_8d": True, "bitrate": "320k",
    },
}

DEFAULT_CUSTOM = {
    "title": "Qo'lda sozlash",
    "speed": 1.0, "reverb": 45, "bass": 0, "eq": list(BASE_EQ), "is_8d": False, "bitrate": "320k",
}

logging.basicConfig(level=logging.INFO)
logger = logging.getLogger("bsbot")

BOT_TOKEN = os.environ["BOT_TOKEN"]

# ====================== HEALTH CHECK (Render / HF uchun) ======================
class Health(BaseHTTPRequestHandler):
    def do_GET(self):
        self.send_response(200)
        self.end_headers()
        self.wfile.write(b"OK")

    def do_HEAD(self):
        self.send_response(200)
        self.end_headers()

    def log_message(self, *args):
        pass


def run_health():
    port = int(os.environ.get("PORT", 7860))
    HTTPServer(("0.0.0.0", port), Health).serve_forever()


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


def apply_8d(samples: np.ndarray, sr: int) -> np.ndarray:
    """Ovoz boshni aylanib chiqqandek (teng quvvatli panning, ~8 soniyada bir aylanish)."""
    mono = (samples[0] + samples[1]) * 0.5
    t = np.arange(mono.shape[0], dtype=np.float32) / sr
    pan = np.sin(2 * np.pi * 0.125 * t)                  # -1 (chap) .. +1 (o'ng)
    angle = (pan + 1.0) * (np.pi / 4.0)                  # 0 .. pi/2
    left = mono * np.cos(angle) * 1.35
    right = mono * np.sin(angle) * 1.35
    # asl stereo'dan ozgina qo'shamiz, tabiiyroq chiqishi uchun
    left += samples[0] * 0.25
    right += samples[1] * 0.25
    return np.vstack([left, right]).astype(np.float32)


def render_audio(in_path: str, out_path: str, p: dict, title: str, artist: str):
    sound = AudioSegment.from_file(in_path)
    if len(sound) > MAX_DURATION_SEC * 1000:
        raise TooLong()
    sound = sound.set_channels(2).set_sample_width(2)
    sound = change_speed(sound, float(p.get("speed", 1.0)))
    sr = sound.frame_rate

    samples = np.frombuffer(sound.raw_data, dtype=np.int16).reshape(-1, 2).T.astype(np.float32)
    samples /= 32768.0
    del sound
    n_orig = samples.shape[1]
    ref_rms = float(np.sqrt(np.mean(samples ** 2))) + 1e-9   # asl qo'shiq balandligi

    # reverb dumi (oxirida kesilib qolmasligi uchun) - 2 soniya jimlik
    if p.get("reverb", 0) > 0:
        samples = np.pad(samples, ((0, 0), (0, sr * 2)))

    board = build_board(p)
    if len(board) > 0:
        samples = board(samples, sr)

    if p.get("is_8d"):
        samples = apply_8d(samples, sr)

    # Balandlikni asl qo'shiqqa tenglashtirish (effektlardan keyin tovush pasayib ketmasligi uchun)
    proc_rms = float(np.sqrt(np.mean(samples[:, :n_orig] ** 2))) + 1e-9
    gain = min(max(ref_rms / proc_rms, 0.25), 4.0)           # -12 dB ... +12 dB
    samples = samples * gain
    # kliplanishsiz: yumshoq limiter
    samples = Pedalboard([Limiter(threshold_db=-1.5, release_ms=100.0)])(samples, sr)
    peak = float(np.max(np.abs(samples))) if samples.size else 0.0
    if peak > 0.89:                      # mp3 kodlashda oshib ketmasligi uchun -1 dBFS zaxira
        samples *= 0.89 / peak

    pcm = (samples.T * 32767.0).astype(np.int16).tobytes()
    del samples
    out = AudioSegment(pcm, frame_rate=sr, sample_width=2, channels=2)
    del pcm
    out.export(out_path, format="mp3", bitrate=p.get("bitrate", "320k"))
    del out

    write_tags(out_path, title, artist)


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
    "Assalomu alaykum, {name}! Xush kelibsiz.\n\n"
    "Men BS Track musiqa botiman. Qo'shig'ingizni professional effektlar bilan "
    "yangicha ovozga keltirib beraman.\n\n"
    "Nimalar qila olaman:\n"
    "• Reverb, Slowed + Reverb, Bass Boost va 8D rejimlar\n"
    "• To'liq qo'lda sozlash: tezlik, reverb, bass va 9 polosali ekvalayzer\n"
    "• Qo'shiqqa o'zingiz xohlagan nomni berish va muqova qo'yish\n\n"
    "Boshlash uchun menga shunchaki qo'shiq (audio fayl) yuboring.\n"
    "Eslatma: qo'shiq {minutes} daqiqadan oshmasin va 20 MB dan katta bo'lmasin.\n\n"
    "Tez orada yana: ovozni olib tashlash (vocal remover)."
)

SAVE_HINT = (
    "Saqlash uchun: qo'shiqni bosib turing > «Forward» (boshqaga yoki Saved Messages'ga) "
    "yoki «Save to Music / Save to Files»."
)


def main_menu() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup([
        [InlineKeyboardButton("Reverb", callback_data="p:reverb"),
         InlineKeyboardButton("Slowed + Reverb", callback_data="p:slowed")],
        [InlineKeyboardButton("Bass Boost", callback_data="p:bass"),
         InlineKeyboardButton("8D Audio", callback_data="p:8d")],
        [InlineKeyboardButton("Qo'lda sozlash", callback_data="m:custom")],
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
        [InlineKeyboardButton(f"8D: {onoff(p['is_8d'])} (almashtirish)", callback_data="c:8d")],
        [InlineKeyboardButton("Ekvalayzer (EQ)", callback_data="c:eq")],
        [InlineKeyboardButton("Boshlang'ich holat", callback_data="c:reset"),
         InlineKeyboardButton("Tayyor", callback_data="c:done")],
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
async def start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not await check_access(update, context):
        return
    name = update.effective_user.first_name or "do'st"
    await update.message.reply_text(GREETING.format(name=name, minutes=MAX_MINUTES))


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
    context.user_data["custom"] = copy.deepcopy(DEFAULT_CUSTOM)

    await msg.reply_text(
        "Qo'shiq qabul qilindi. Qanday ishlov beramiz?",
        reply_markup=main_menu(),
    )


async def ask_name(query, context: ContextTypes.DEFAULT_TYPE):
    context.user_data["awaiting_name"] = True
    t, a = context.user_data.get("orig_title", ""), context.user_data.get("orig_artist", "")
    orig = f"{a} - {t}" if a and t else (t or "")

    rows = []
    if orig:
        context.user_data["orig_full"] = orig
        rows.append([InlineKeyboardButton("Asl nomni qoldirish", callback_data="n:keep")])
    rows.append([InlineKeyboardButton("Bekor qilish", callback_data="n:cancel")])

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

    if data == "n:cancel":
        ud["awaiting_name"] = False
        await safe_edit(query, "Bekor qilindi. Boshqa effekt tanlang:", main_menu())
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
                await asyncio.to_thread(render_audio, in_path, out_path, params, title, artist)

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
    except BadRequest as e:
        logger.exception("Telegram xatosi")
        if "too big" in str(e).lower():
            await status.edit_text("Fayl juda katta (20 MB dan oshiq). Kichikroq mp3 yuboring.")
        else:
            await status.edit_text("Telegram bilan xatolik yuz berdi. Qaytadan urinib ko'ring.")
    except Exception:
        logger.exception("Ishlov berishda xatolik")
        await status.edit_text(
            "Kechirasiz, ishlov berishda xatolik yuz berdi. Boshqa fayl bilan urinib ko'ring "
            "yoki keyinroq qaytadan yuboring."
        )


def main():
    threading.Thread(target=run_health, daemon=True).start()

    app = (
        Application.builder()
        .token(BOT_TOKEN)
        .read_timeout(60)
        .write_timeout(120)
        .connect_timeout(30)
        .build()
    )
    app.add_handler(CommandHandler("start", start))
    app.add_handler(CommandHandler("help", start))
    app.add_handler(CommandHandler("myid", myid_cmd))
    app.add_handler(CommandHandler("add", add_cmd))
    app.add_handler(CommandHandler("remove", remove_cmd))
    app.add_handler(CommandHandler("list", list_cmd))
    app.add_handler(MessageHandler(filters.AUDIO | filters.Document.AUDIO, handle_audio))
    app.add_handler(CallbackQueryHandler(button_handler))
    app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, handle_text))
    logger.info("Bot ishga tushdi")
    app.run_polling()


if __name__ == "__main__":
    main()
