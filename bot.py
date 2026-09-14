import os
import gc
import logging
import tempfile

import numpy as np
from pydub import AudioSegment
from pedalboard import Pedalboard, Reverb, PeakFilter, LowShelfFilter

from telegram import Update
from telegram.ext import Application, MessageHandler, CommandHandler, ContextTypes, filters

logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)

BOT_TOKEN = os.environ["BOT_TOKEN"]

# Free-tier Render instances have only 512MB RAM. Long files blow past that
# during processing, so we cap accepted audio length.
MAX_DURATION_SECONDS = 7 * 60

# 9-band EQ center frequencies (Hz) — matches the second website exactly
EQ_BANDS = [60, 170, 310, 600, 1000, 3000, 6000, 12000, 16000]

DEFAULTS = {
    "speed": 0.95,
    "reverb": 40,                          # 0-100 %
    "bass": 0,                             # 0-100 % (site 1's "Bass boost")
    "eq": [8, 6, 2, 0, 0, 0, 0, 0, 0],      # dB per band (site 2's "Bass Boost" preset)
    "bitrate": "192k",
}

HELP_TEXT = (
    "Qo'shiqni (audio fayl) menga yuboring. Sozlamalarni caption (izoh) qismida bering:\n\n"
    "speed=0.95 reverb=40 bass=0 eq=8,6,2,0,0,0,0,0,0 bitrate=320\n\n"
    "— speed: 0.5–1.5 (1 = o'zgarmaydi, kichikroq = sekinroq)\n"
    "— reverb: 0–100 (%)\n"
    "— bass: 0–100 (% past boost, 60Hz atrofida)\n"
    "— eq: 9 ta dB qiymat vergul bilan, tartib bo'yicha 60/170/310/600/1k/3k/6k/12k/16k Hz, "
    "har biri -12..+12 oralig'ida\n"
    "— bitrate: 128, 192, 256 yoki 320\n\n"
    "Caption bo'sh bo'lsa standart sozlamalar ishlatiladi:\n"
    f"speed={DEFAULTS['speed']} reverb={DEFAULTS['reverb']} bass={DEFAULTS['bass']} "
    f"eq={','.join(map(str, DEFAULTS['eq']))} bitrate=192\n\n"
    "Faqat o'zgartirmoqchi bo'lgan qiymatlarni yozsangiz ham bo'ladi, masalan:\n"
    "reverb=60 bitrate=320"
)


def parse_params(caption):
    params = dict(DEFAULTS)
    params["eq"] = list(DEFAULTS["eq"])
    if not caption:
        return params
    for token in caption.split():
        if "=" not in token:
            continue
        key, val = token.split("=", 1)
        key = key.strip().lower()
        val = val.strip()
        try:
            if key == "speed":
                params["speed"] = max(0.5, min(1.5, float(val)))
            elif key == "reverb":
                params["reverb"] = max(0, min(100, float(val)))
            elif key == "bass":
                params["bass"] = max(0, min(100, float(val)))
            elif key == "eq":
                vals = [max(-12, min(12, float(x))) for x in val.split(",")]
                if len(vals) == 9:
                    params["eq"] = vals
            elif key == "bitrate":
                b = val.replace("k", "").replace("kbps", "")
                if b in ("128", "192", "256", "320"):
                    params["bitrate"] = f"{b}k"
        except ValueError:
            continue
    return params


def change_speed(sound: AudioSegment, speed: float) -> AudioSegment:
    """Classic vinyl-style speed change (also shifts pitch), same as site 1's Speed slider."""
    new_frame_rate = int(sound.frame_rate * speed)
    shifted = sound._spawn(sound.raw_data, overrides={"frame_rate": new_frame_rate})
    return shifted.set_frame_rate(sound.frame_rate)


def build_board(params: dict) -> Pedalboard:
    board = Pedalboard([])

    if params["bass"] > 0:
        gain_db = (params["bass"] / 100.0) * 12.0
        board.append(LowShelfFilter(cutoff_frequency_hz=100, gain_db=gain_db))

    for freq, gain_db in zip(EQ_BANDS, params["eq"]):
        if gain_db != 0:
            board.append(PeakFilter(cutoff_frequency_hz=freq, gain_db=gain_db, q=1.0))

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
    """
    Memory-conscious version: process one channel at a time and drop
    intermediate arrays as soon as they're no longer needed, so peak RAM
    stays well under Render's 512MB free-tier limit.
    """
    sound = AudioSegment.from_file(in_path)
    sound = change_speed(sound, params["speed"])

    channels = sound.channels
    frame_rate = sound.frame_rate
    sample_width = sound.sample_width
    raw = sound.raw_data
    del sound
    gc.collect()

    board = build_board(params)

    # int16 -> float32 in [-1, 1], shape (channels, num_frames)
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
        processed_int16 = np.stack(processed_channels, axis=-1)  # interleaved
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


async def start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text(HELP_TEXT)


async def handle_audio(update: Update, context: ContextTypes.DEFAULT_TYPE):
    msg = update.message
    file_obj = msg.audio or msg.voice or msg.document
    if file_obj is None:
        return
    duration = getattr(file_obj, "duration", None)
    if duration and duration > MAX_DURATION_SECONDS:
        await msg.reply_text(
            f"Kechirasiz, bu qo'shiq juda uzun ({duration // 60}:{duration % 60:02d}). "
            f"Server xotirasi cheklangan bo'lgani uchun {MAX_DURATION_SECONDS // 60} "
            "daqiqagacha bo'lgan fayllarni qabul qilaman."
        )
        return

    params = parse_params(msg.caption)

    status = await msg.reply_text("Ishlov berilmoqda, biroz kuting...")

    tg_file = await context.bot.get_file(file_obj.file_id)
    with tempfile.TemporaryDirectory() as tmp:
        in_path = os.path.join(tmp, "input")
        out_path = os.path.join(tmp, "output.mp3")
        await tg_file.download_to_drive(in_path)
        try:
            apply_effects(in_path, out_path, params)
        except Exception as e:
            logger.exception("processing failed")
            await status.edit_text(f"Xatolik chiqdi: {e}")
            return

        summary = (
            f"speed={params['speed']} reverb={params['reverb']} bass={params['bass']} "
            f"eq={','.join(map(str, params['eq']))} bitrate={params['bitrate']}"
        )
        with open(out_path, "rb") as f:
            await msg.reply_audio(audio=f, caption=summary)
        await status.delete()


def main():
    app = Application.builder().token(BOT_TOKEN).build()
    app.add_handler(CommandHandler("start", start))
    app.add_handler(CommandHandler("help", start))
    app.add_handler(MessageHandler(filters.AUDIO | filters.VOICE | filters.Document.AUDIO, handle_audio))
    logger.info("Bot ishga tushdi")
    app.run_polling()


if __name__ == "__main__":
    main()
