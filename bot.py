import os
import gc
import logging
import tempfile
import re
import json
import numpy as np
from pydub import AudioSegment
from pedalboard import Pedalboard, Reverb, PeakFilter, LowShelfFilter

from telegram import Update, InlineKeyboardButton, InlineKeyboardMarkup
from telegram.ext import (
    Application,
    MessageHandler,
    CommandHandler,
    CallbackQueryHandler,
    ContextTypes,
    filters,
)

import threading
from http.server import BaseHTTPRequestHandler, HTTPServer


# ====================== HEALTH CHECK ======================
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
    port = int(os.environ.get("PORT", 10000))
    HTTPServer(("0.0.0.0", port), Health).serve_forever()


threading.Thread(target=run_health, daemon=True).start()
# ==========================================================


logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)

BOT_TOKEN = os.environ["8947877567:AAG39GwRrftBbPh4mW1B78BfFNClGyg1afo"]
MAX_DURATION_SECONDS = 10 * 60
EQ_BANDS = [60, 170, 310, 600, 1000, 3000, 6000, 12000, 16000]

# ====================== RUXSAT TIZIMI ======================
# ADMIN_ID  - sizning Telegram ID raqamingiz (Render > Environment ga yoziladi)
# ALLOWED_IDS - doimiy ruxsat berilganlar, vergul bilan: 111,222,333 (ixtiyoriy)
ADMIN_ID = int(os.environ.get("ADMIN_ID", "0") or 0)
if not ADMIN_ID:
    logger.warning("ADMIN_ID o'rnatilmagan! Faqat /myid ishlaydi.")

ENV_ALLOWED = {
    int(x) for x in os.environ.get("ALLOWED_IDS", "").replace(" ", "").split(",") if x.isdigit()
}
ALLOWED_FILE = "allowed_users.json"
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


def is_allowed(uid: int) -> bool:
    return uid == ADMIN_ID or uid in ENV_ALLOWED or uid in load_dynamic()


async def check_access(update, context) -> bool:
    user = update.effective_user
    if user and is_allowed(user.id):
        return True

    if update.callback_query:
        await update.callback_query.answer("Sizda ruxsat yo'q.", show_alert=True)
    elif update.message:
        await update.message.reply_text(
            "Kechirasiz, sizda bu botdan foydalanish uchun ruxsat yo'q.\n"
            f"Sizning ID raqamingiz: {user.id}\n"
            "Ruxsat olish uchun shu raqamni admin'ga yuboring."
        )
        # adminni bir marta xabardor qilish
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
    async def wrapper(update, context):
        if not update.effective_user or update.effective_user.id != ADMIN_ID:
            return
        return await func(update, context)
    return wrapper


async def myid_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text(f"Sizning ID raqamingiz: {update.effective_user.id}")


def _all_ids() -> set:
    return ENV_ALLOWED | load_dynamic()


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
        "Eslatma: bot qayta ishga tushsa (Render'da deploy/restart) /add orqali "
        "qo'shilganlar o'chib ketishi mumkin. Doimiy qilish uchun Render > Environment > "
        "ALLOWED_IDS ga quyidagini yozing:\n"
        f"{','.join(str(i) for i in sorted(_all_ids()))}"
    )
    try:
        await context.bot.send_message(uid, "Sizga botdan foydalanishga ruxsat berildi. /start bosing.")
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
        msg += "\n(Bu ID Render'dagi ALLOWED_IDS ichida ham bor, uni o'sha yerdan ham o'chiring.)"
    await update.message.reply_text(msg)


@admin_only
async def list_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    ids = sorted(_all_ids())
    if not ids:
        await update.message.reply_text("Hozircha faqat siz (admin) ruxsatga egasiz.")
        return
    await update.message.reply_text(
        "Ruxsat berilganlar:\n" + "\n".join(str(i) for i in ids)
    )
# ===========================================================

# ====================== PRESETLAR ======================
PRESETS = {
    "standart": {
        "speed": 1.0,
        "reverb": 50,
        "bass": 0,
        "eq": [2, -4, -4, 0, 0, 2, 5, 7, 8],
        "bitrate": "320k",
    },
    "slowed_reverb": {
        "speed": 0.90,
        "reverb": 40,
        "bass": 0,
        "eq": [2, -4, -4, 0, 0, 2, 5, 7, 8],
        "bitrate": "320k",
    },
    "bass_boost": {
        "speed": 1.0,
        "reverb": 25,
        "bass": 0,
        "eq": [4, 2, 2, 0, 0, 2, 3, 4, 3],
        "bitrate": "320k",
    },
    "pitch_up": {
        "speed": 1.18,
        "reverb": 30,
        "bass": 0,
        "eq": [3, 2, -2, 0, 0, 1, 2, 1, 0],
        "bitrate": "320k",
    },
    "pitch_down": {
        "speed": 0.88,
        "reverb": 35,
        "bass": 0,
        "eq": [2, 0, -1, 0, 0, 0, 0, 2, 4],
        "bitrate": "320k",
    },
    "eight_d": {
        "speed": 1.0,
        "reverb": 45,
        "bass": 20,
        "eq": [5, 3, 0, 0, 1, 2, 3, 2, 1],
        "bitrate": "320k",
        "is_8d": True,
    },
    "echo": {
        "speed": 0.5,
        "reverb": 50,
        "bass": 0,
        "eq": [4, 2, 0, 0, 0, 1, 2, 1, 0],
        "bitrate": "320k",
    },
}


def change_speed(sound: AudioSegment, speed: float) -> AudioSegment:
    new_frame_rate = int(sound.frame_rate * speed)
    shifted = sound._spawn(sound.raw_data, overrides={"frame_rate": new_frame_rate})
    return shifted.set_frame_rate(sound.frame_rate)


def build_board(params: dict) -> Pedalboard:
    board = Pedalboard([])
    if params.get("bass", 0) > 0:
        gain_db = (params["bass"] / 100.0) * 12.0
        board.append(LowShelfFilter(cutoff_frequency_hz=100, gain_db=gain_db))
    for freq, gain_db in zip(EQ_BANDS, params.get("eq", [0]*9)):
        if gain_db != 0:
            board.append(PeakFilter(cutoff_frequency_hz=freq, gain_db=gain_db, q=1.0))
    if params.get("reverb", 0) > 0:
        wet = params["reverb"] / 100.0
        board.append(Reverb(
            room_size=min(0.9, 0.3 + wet * 0.6),
            damping=0.5,
            wet_level=wet,
            dry_level=1 - wet * 0.5,
            width=1.0,
        ))
    return board


def apply_8d_effect(samples: np.ndarray, frame_rate: int) -> np.ndarray:
    """Kuchliroq 8D effekt"""
    if samples.shape[0] == 1:
        samples = np.vstack([samples, samples])

    num_frames = samples.shape[1]
    t = np.arange(num_frames) / frame_rate

    # Tezroq va kuchliroq aylanısh (0.25 Hz)
    pan = np.sin(2 * np.pi * 0.25 * t)

    # Kuchliroq pan
    left = samples[0] * (0.5 - 0.5 * pan) + samples[1] * 0.15
    right = samples[1] * (0.5 + 0.5 * pan) + samples[0] * 0.15

    # Biroz reverb hissi uchun
    return np.vstack([left, right])


def apply_effects(in_path: str, out_path: str, params: dict, trim: tuple = None):
    sound = AudioSegment.from_file(in_path)

    # Trim (kesish)
    if trim:
        start_ms, end_ms = trim
        sound = sound[start_ms:end_ms]

    sound = change_speed(sound, params.get("speed", 1.0))

    channels = sound.channels
    frame_rate = sound.frame_rate
    sample_width = sound.sample_width
    raw = sound.raw_data
    del sound
    gc.collect()

    board = build_board(params)

    samples = np.frombuffer(raw, dtype=np.int16).astype(np.float32)
    del raw
    if channels == 2:
        samples = samples.reshape((-1, 2)).T
    else:
        samples = samples.reshape((1, -1))
    samples *= (1.0 / 32768.0)

    processed_channels = []
    for ch in range(samples.shape[0]):
        one = board(samples[ch:ch + 1], frame_rate)
        np.clip(one, -1.0, 1.0, out=one)
        processed_channels.append(one[0])
        del one
    processed = np.vstack(processed_channels)
    del processed_channels, samples
    gc.collect()

    if params.get("is_8d"):
        processed = apply_8d_effect(processed, frame_rate)

    processed = np.clip(processed, -1.0, 1.0)
    processed = (processed * 32767.0).astype(np.int16)

    if processed.shape[0] == 2:
        pcm_bytes = processed.T.tobytes()
        channels = 2
    else:
        pcm_bytes = processed[0].tobytes()
        channels = 1
    del processed
    gc.collect()

    out_sound = AudioSegment(pcm_bytes, frame_rate=frame_rate, sample_width=sample_width, channels=channels)
    del pcm_bytes
    out_sound.export(out_path, format="mp3", bitrate=params.get("bitrate", "192k"))
    del out_sound
    gc.collect()


def parse_time(t: str) -> int:
    """0:30 yoki 1:45 ni millisekundga o'tkazadi"""
    parts = t.strip().split(":")
    if len(parts) == 2:
        return (int(parts[0]) * 60 + int(parts[1])) * 1000
    elif len(parts) == 1:
        return int(parts[0]) * 1000
    return 0


# ====================== HANDLERLAR ======================

async def start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not await check_access(update, context):
        return
    await update.message.reply_text(
        "Salom! Men musiqaga atmosfera effekt beradigan botman.\n\n"
        "Shunchaki audio fayl yuboring.\n"
        "Keyin kerakli rejimni tugmadan tanlaysiz."
    )


async def handle_audio(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not await check_access(update, context):
        return
    msg = update.message
    file_obj = msg.audio or msg.voice or msg.document
    if not file_obj:
        return

    duration = getattr(file_obj, "duration", None)
    if duration and duration > MAX_DURATION_SECONDS:
        await msg.reply_text(
            f"Kechirasiz, bu qo'shiq juda uzun ({duration // 60}:{duration % 60:02d}).\n"
            f"Hozircha {MAX_DURATION_SECONDS // 60} daqiqagacha qabul qilaman."
        )
        return

    # Eski holatlarni tozalash
    context.user_data.clear()
    context.user_data["file_id"] = file_obj.file_id
    context.user_data["custom"] = {
        "speed": 1.0,
        "reverb": 50,
        "bass": 0,
        "eq": [2, -4, -4, 0, 0, 2, 5, 7, 8],
        "bitrate": "320k",
    }

    keyboard = [
        [InlineKeyboardButton("Standart", callback_data="preset_standart"),
         InlineKeyboardButton("Slowed + Reverb", callback_data="preset_slowed_reverb")],
        [InlineKeyboardButton("Bass Boost", callback_data="preset_bass_boost"),
         InlineKeyboardButton("Echo / Delay", callback_data="preset_echo")],
        [InlineKeyboardButton("Pitch Up", callback_data="preset_pitch_up"),
         InlineKeyboardButton("Pitch Down", callback_data="preset_pitch_down")],
        [InlineKeyboardButton("8D Audio", callback_data="preset_eight_d")],
        [InlineKeyboardButton("Sozlamalar", callback_data="custom_menu"),
         InlineKeyboardButton("Kesish (Trim)", callback_data="trim_start")],
    ]
    await msg.reply_text("Audio qabul qilindi.\n\nQanday ishlov beramiz?", reply_markup=InlineKeyboardMarkup(keyboard))


async def button_handler(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not await check_access(update, context):
        return
    query = update.callback_query
    await query.answer()
    data = query.data

    # ===== PRESETLAR =====
    if data.startswith("preset_"):
        preset_name = data.replace("preset_", "")
        params = PRESETS.get(preset_name)
        if not params:
            await query.edit_message_text("Noma'lum rejim.")
            return
        await process_audio(query, context, params)
        return

    # ===== SOZLAMALAR MENYUSI =====
    if data == "custom_menu":
        await show_custom_menu(query, context)
        return

    if data.startswith("set_speed_"):
        speed = float(data.replace("set_speed_", ""))
        context.user_data["custom"]["speed"] = speed
        await show_custom_menu(query, context)
        return

    if data.startswith("set_reverb_"):
        reverb = int(data.replace("set_reverb_", ""))
        context.user_data["custom"]["reverb"] = reverb
        await show_custom_menu(query, context)
        return

    if data.startswith("set_bass_"):
        bass = int(data.replace("set_bass_", ""))
        context.user_data["custom"]["bass"] = bass
        await show_custom_menu(query, context)
        return

    if data == "custom_process":
        params = context.user_data.get("custom", PRESETS["standart"])
        await process_audio(query, context, params)
        return

    # ===== KESISH (TRIM) =====
    if data == "trim_start":
        context.user_data["waiting_trim"] = True
        await query.edit_message_text(
            "Kesish uchun vaqt oralig'ini yozing.\n\n"
            "Format: `0:30-1:45`\n"
            "Masalan: `0:00-1:00` yoki `1:20-2:50`",
            parse_mode="Markdown"
        )
        return


async def show_custom_menu(query, context):
    custom = context.user_data.get("custom", {})
    speed = custom.get("speed", 1.0)
    reverb = custom.get("reverb", 50)
    bass = custom.get("bass", 0)

    text = (
        f"Sozlamalar:\n\n"
        f"Speed: **{speed}x**\n"
        f"Reverb: **{reverb}%**\n"
        f"Bass: **{bass}%**\n\n"
        f"Kerakli qiymatni tanlang:"
    )

    keyboard = [
        [InlineKeyboardButton("Speed:", callback_data="ignore")],
        [
            InlineKeyboardButton("0.6x", callback_data="set_speed_0.6"),
            InlineKeyboardButton("0.75x", callback_data="set_speed_0.75"),
            InlineKeyboardButton("0.85x", callback_data="set_speed_0.85"),
        ],
        [
            InlineKeyboardButton("1.0x", callback_data="set_speed_1.0"),
            InlineKeyboardButton("1.15x", callback_data="set_speed_1.15"),
            InlineKeyboardButton("1.30x", callback_data="set_speed_1.30"),
            InlineKeyboardButton("1.50x", callback_data="set_speed_1.50"),
        ],
        [InlineKeyboardButton("Reverb:", callback_data="ignore")],
        [
            InlineKeyboardButton("20%", callback_data="set_reverb_20"),
            InlineKeyboardButton("40%", callback_data="set_reverb_40"),
            InlineKeyboardButton("60%", callback_data="set_reverb_60"),
            InlineKeyboardButton("80%", callback_data="set_reverb_80"),
        ],
        [InlineKeyboardButton("Bass:", callback_data="ignore")],
        [
            InlineKeyboardButton("0%", callback_data="set_bass_0"),
            InlineKeyboardButton("10%", callback_data="set_bass_10"),
            InlineKeyboardButton("20%", callback_data="set_bass_20"),
            InlineKeyboardButton("30%", callback_data="set_bass_30"),
            InlineKeyboardButton("40%", callback_data="set_bass_40"),
        ],
        [InlineKeyboardButton("Tayyor — Ishlov berish", callback_data="custom_process")],
    ]
    await query.edit_message_text(text, reply_markup=InlineKeyboardMarkup(keyboard), parse_mode="Markdown")


async def process_audio(query, context, params, trim=None):
    file_id = context.user_data.get("file_id")
    if not file_id:
        await query.edit_message_text("Audio topilmadi. Qaytadan yuboring.")
        return

    await query.edit_message_text("Ishlov berilmoqda, biroz kuting...")

    try:
        tg_file = await context.bot.get_file(file_id)
        with tempfile.TemporaryDirectory() as tmp:
            in_path = os.path.join(tmp, "input")
            out_path = os.path.join(tmp, "output.mp3")
            await tg_file.download_to_drive(in_path)
            apply_effects(in_path, out_path, params, trim=trim)

            summary = (
                f"speed={params.get('speed')}  reverb={params.get('reverb')}  "
                f"bass={params.get('bass')}  bitrate={params.get('bitrate')}"
            )
            with open(out_path, "rb") as f:
                await context.bot.send_audio(chat_id=query.message.chat_id, audio=f, caption=summary)
        await query.message.delete()
    except Exception as e:
        logger.exception("Xatolik")
        await query.edit_message_text(f"Xatolik yuz berdi:\n{e}")


async def handle_text(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Kesish uchun matn kiritilganda"""
    if not is_allowed(update.effective_user.id):
        return
    if not context.user_data.get("waiting_trim"):
        return

    text = update.message.text.strip()
    match = re.match(r"(\d+:\d+|\d+)\s*-\s*(\d+:\d+|\d+)", text)
    if not match:
        await update.message.reply_text(
            "Noto'g'ri format.\nTo'g'ri yozing: `0:30-1:45`",
            parse_mode="Markdown"
        )
        return

    start_ms = parse_time(match.group(1))
    end_ms = parse_time(match.group(2))

    if end_ms <= start_ms:
        await update.message.reply_text("Tugash vaqti boshlanishidan katta bo'lishi kerak.")
        return

    context.user_data["waiting_trim"] = False
    params = context.user_data.get("custom", PRESETS["standart"])

    # Trim bilan ishlov berish
    status = await update.message.reply_text("Kesilmoqda va ishlov berilmoqda...")
    file_id = context.user_data.get("file_id")

    try:
        tg_file = await context.bot.get_file(file_id)
        with tempfile.TemporaryDirectory() as tmp:
            in_path = os.path.join(tmp, "input")
            out_path = os.path.join(tmp, "output.mp3")
            await tg_file.download_to_drive(in_path)
            apply_effects(in_path, out_path, params, trim=(start_ms, end_ms))

            with open(out_path, "rb") as f:
                await update.message.reply_audio(audio=f, caption=f"Kesilgan: {text}")
        await status.delete()
    except Exception as e:
        await status.edit_text(f"Xatolik: {e}")


def main():
    app = Application.builder().token(8947877567:AAG39GwRrftBbPh4mW1B78BfFNClGyg1afo).build()
    app.add_handler(CommandHandler("start", start))
    app.add_handler(CommandHandler("help", start))
    app.add_handler(CommandHandler("myid", myid_cmd))
    app.add_handler(CommandHandler("add", add_cmd))
    app.add_handler(CommandHandler("remove", remove_cmd))
    app.add_handler(CommandHandler("list", list_cmd))
    app.add_handler(MessageHandler(filters.AUDIO | filters.VOICE | filters.Document.AUDIO, handle_audio))
    app.add_handler(CallbackQueryHandler(button_handler))
    app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, handle_text))
    logger.info("Bot ishga tushdi")
    app.run_polling()


if __name__ == "__main__":
    main()
