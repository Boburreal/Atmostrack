import os
import telebot

# 1. BOT_TOKEN ni xavfsiz o'qib olish (KeyError bermaydi)
BOT_TOKEN = os.environ.get("BOT_TOKEN", "").strip()

# Agarda BOT_TOKEN topilmasa, konsolga tushunarli ogohlantirish beradi
if not BOT_TOKEN:
    print("⚠️ OGOHLANTIRISH: BOT_TOKEN Render Environment Variables ichida topilmadi!")

bot = telebot.TeleBot(BOT_TOKEN)

# 2. ALLOWED_USERS ni o'qish va ro'yxatga (list) o'tkazish
ALLOWED_USERS_RAW = os.environ.get("ALLOWED_USERS", "")

# Agar Environment'da belgilanmagan bo'lsa, o'zingizning Telegram ID'ingizni shu yerga zaxira sifatida yozib qo'ying:
BACKUP_ADMIN_ID = 123456789  # <--- Shu yerga O'ZINGIZNING Telegram ID'ingizni yozing!

if ALLOWED_USERS_RAW:
    # Render'dagi vergul bilan ajratilgan ID'larni ro'yxat qiladi
    ALLOWED_USERS = [int(i.strip()) for i in ALLOWED_USERS_RAW.split(",") if i.strip().isdigit()]
else:
    # Agarda Render'da kiritilmagan bo'lsa, zaxira ID'dan foydalanadi
    ALLOWED_USERS = [BACKUP_ADMIN_ID]

print(f"✅ Ruxsat berilgan foydalanuvchilar ID ro'yxati: {ALLOWED_USERS}")

# 3. Barcha xabarlarni tekshirish va bloklash filteri
@bot.message_handler(func=lambda message: True)
def handle_messages(message):
    user_id = message.from_user.id

    # Agar foydalanuvchi ID'si ruxsat etilganlar ro'yxatida bo'lmasa:
    if user_id not in ALLOWED_USERS:
        bot.send_message(
            message.chat.id, 
            "⛔️ **Kirish taqiqlangan!**\nUshbu bot hozirda yopiq test rejimida va faqat adminlar uchun ochiq."
        )
        return  # Kod buyrug'ni bajarishdan to'xtaydi

    # --- Ruxsat berilgan foydalanuvchilar uchun asosiy kodingiz ---
    if message.text == "/start":
        # Web App tugmasi
        markup = telebot.types.InlineKeyboardMarkup()
        web_app_btn = telebot.types.InlineKeyboardButton(
            text="🎛 Interfeysni ochish", 
            web_app=telebot.types.WebAppInfo(url="https://atmos-backent.onrender.com") # O'zingizning Web App URL'ingiz
        )
        markup.add(web_app_btn)
        
        bot.send_message(
            message.chat.id, 
            f"Xush kelibsiz {message.from_user.first_minute if hasattr(message.from_user, 'first_minute') else ''}! Botdan foydalanishingiz mumkin.", 
            reply_markup=markup
        )

if __name__ == "__main__":
    print("Bot muvaffaqiyatli ishga tushdi...")
    bot.infinity_polling()
