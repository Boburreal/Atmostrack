import os
import gc
import logging
import tempfile
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


# ====================== HEALTH CHECK (Render uchun) ======================
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
# ========================================================================


logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)

BOT_TOKEN = os.environ["BOT_TOKEN"]

# 10 daqiqagacha ruxsat beramiz
MAX_DURATION_SECONDS = 10 * 60

# 9-band Equalizer chastotalari
EQ_BANDS = [60, 170, 310, 600, 1000, 3000, 6000, 12000, 16000]

# Standart sozlamalar (endi speed = 1.0)
DEFAULTS = {
    "speed": 1.0,
    "reverb": 40,
    "bass": 0,
    "eq": [2, -4, -4, 0, 0, +2, +5, +7, +8],
    "bitrate": "192k",
}

# Turli rejimlar uchun tayyor sozlamalar
PRESETS = {
    "standart": {
        "speed": 1.0,
        "reverb": 40,
        "bass": 0,
        "eq": [2, -4, -4, 0, 0, +2, +5, +7, +8],
        "bitrate": "192k",
    },
    "slowed_reverb": {
        "speed": 0.82,          # Sekinroq
        "reverb": 55,           # Ko‘proq reverb
        "bass": 0,
        "eq": [+2, -4, -4, 0, 0, +2, +3, +5, +7],
        "bitrate": "192k",
    },
    "bass_boost": {
        "speed": 1.0,
        "reverb": 25,
        "bass": 60,             # Kuchli bass
        "eq": [10, 8, 4, 1, 0, 0, 0, 0, 0],
        "bitrate": "192k",
    },
}


def change_speed(sound: AudioSegment, speed: float) -> AudioSegment:
    """Tezlikni o‘zgartirish (pitch ham o‘zgaradi)"""
    new_frame_rate = int(sound.frame_rate * speed)
    shifted = sound._spawn(sound.raw_data, overrides={"frame_rate": new_frame_rate})
    return shifted.set_frame_rate(sound.frame_rate)


def build_board(params: dict) -> Pedalboard:
    board = Pedalboard([])

    # Bass
    if params["bass"] > 0:
        gain_db = (params["bass"] / 100.0) * 12.0
        board.append(LowShelfFilter(cutoff_frequency_hz=100, gain_db=gain_db))

    # Equalizer
    for freq, gain_db in zip(EQ_BANDS, params["eq"]):
        if gain_db != 0:
            board.append(PeakFilter(cutoff_frequency_hz=freq, gain_db=gain_db, q=1.0))

    # Reverb
    if params["reverb"] > 0:
        wet = params["reverb"] / 100.0
        board.append(Reverb(
            room_size=min(0.9, 0.3 + wet * 0.6),
            damping=0.5,
            wet_level=wet,
            dry_level=1 - wet * 0.5,
            width=1.0,
        ))
    return board


def apply_effects(in_path: str, out_path: str, params: dict):
    """Xotirani tejab ishlov berish"""
    sound = AudioSegment.from_file(in_path)
    sound = change_speed(sound, params["speed"])

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
    for ch in range(channels):
        one = board(samples[ch:ch + 1], frame_rate)
        np.clip(one, -1.0, 1.0, out=one)
        one *= 32767.0
        processed_channels.append(one[0].astype(np.int16))
        del one
    del samples
    gc.collect()

    if channels == 2:
        processed_int16 = np.stack(processed_channels, axis=-1)
        pcm_bytes = processed_int16.tobytes()
    else:
        pcm_bytes = processed_channels[0].tobytes()
    del processed_channels
    gc.collect()

    out_sound = AudioSegment(
        pcm_bytes,
        frame_rate=frame_rate,
        sample_width=sample_width,
        channels=channels,
    )
    del pcm_bytes
    out_sound.export(out_path, format="mp3", bitrate=params["bitrate"])
    del out_sound
    gc.collect()


# ====================== HANDLERLAR ======================

async def start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    text = (
        "Salom! Men musiqaga effekt beradigan botman.\n\n"
        "Shunchaki audio fayl yuboring.\n"
        "Keyin kerakli rejimni tugmadan tanlaysiz."
    )
    await update.message.reply_text(text)


async def handle_audio(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Audio kelganda ishlaydi"""
    msg = update.message
    file_obj = msg.audio or msg.voice or msg.document

    if file_obj is None:
        return

    # Uzunlikni tekshirish
    duration = getattr(file_obj, "duration", None)
    if duration and duration > MAX_DURATION_SECONDS:
        await msg.reply_text(
            f"Kechirasiz, bu qo'shiq juda uzun ({duration // 60}:{duration % 60:02d}).\n"
            f"Hozircha {MAX_DURATION_SECONDS // 60} daqiqagacha bo'lgan fayllarni qabul qilaman."
        )
        return

    # Foydalanuvchi ma'lumotlarini saqlab qo'yamiz
    context.user_data["file_id"] = file_obj.file_id
    context.user_data["duration"] = duration

    # Tugmalar
    keyboard = [
        [
            InlineKeyboardButton("Standart", callback_data="preset_standart"),
            InlineKeyboardButton("Slowed + Reverb", callback_data="preset_slowed_reverb"),
        ],
        [
            InlineKeyboardButton("Bass Boost", callback_data="preset_bass_boost"),
        ],
        # Keyinchalik shu yerga 8D, Pitch va boshqalarni qo'shamiz
    ]
    reply_markup = InlineKeyboardMarkup(keyboard)

    await msg.reply_text(
        "Audio qabul qilindi.\n\nQanday ishlov beramiz?",
        reply_markup=reply_markup
    )


async def button_handler(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Tugma bosilganda ishlaydi"""
    query = update.callback_query
    await query.answer()  # Tugma bosilganini bildiradi

    data = query.data
    if not data.startswith("preset_"):
        return

    preset_name = data.replace("preset_", "")
    params = PRESETS.get(preset_name)

    if not params:
        await query.edit_message_text("Noma'lum rejim.")
        return

    file_id = context.user_data.get("file_id")
    if not file_id:
        await query.edit_message_text("Audio topilmadi. Qaytadan yuboring.")
        return

    # Status xabar
    await query.edit_message_text("Ishlov berilmoqda, biroz kuting...")

    try:
        tg_file = await context.bot.get_file(file_id)

        with tempfile.TemporaryDirectory() as tmp:
            in_path = os.path.join(tmp, "input")
            out_path = os.path.join(tmp, "output.mp3")

            await tg_file.download_to_drive(in_path)
            apply_effects(in_path, out_path, params)

            # Natijani yuborish
            summary = (
                f"Rejim: {preset_name}\n"
                f"speed={params['speed']}  reverb={params['reverb']}  "
                f"bass={params['bass']}"
            )

            with open(out_path, "rb") as f:
                await context.bot.send_audio(
                    chat_id=query.message.chat_id,
                    audio=f,
                    caption=summary
                )

        # Eski xabarni o'chirish
        await query.message.delete()

    except Exception as e:
        logger.exception("Xatolik")
        await query.edit_message_text(f"Xatolik yuz berdi:\n{e}")


def main():
    app = Application.builder().token(BOT_TOKEN).build()

    app.add_handler(CommandHandler("start", start))
    app.add_handler(CommandHandler("help", start))
    app.add_handler(MessageHandler(filters.AUDIO | filters.VOICE | filters.Document.AUDIO, handle_audio))
    app.add_handler(CallbackQueryHandler(button_handler))

    logger.info("Bot ishga tushdi")
    app.run_polling()


if __name__ == "__main__":
    main()
