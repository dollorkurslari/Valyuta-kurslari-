"""
Doimiy ishlaydigan server (Railway va h.k.) uchun bot.

Nima qiladi:
  1) Har kuni 08:00, 13:00 va 19:00 (Toshkent vaqti) da kurslarni kanalga tashlaydi.
     Ma'lumotni 2 daqiqa OLDIN olib qo'yadi va aniq soatning 00-soniyasida yuboradi.
  2) /start ga va kanalga admin qilib qo'shilganiga darhol javob beradi.
  3) Ishga tushganda egasiga server bank.uz, cbu.uz va Telegramga yetib bora olishini yozadi.

Ishga tushirish:  python scheduler.py
Kerakli muhit o'zgaruvchilari: BOT_TOKEN, CHAT_ID, OWNER_ID  (ixtiyoriy: CALLME_USER, POST_ON_START=1)
"""
import os
import sys
import time
import datetime
import threading

import requests

import main as bot

SLOTS = [8, 13, 19]          # post soatlari (Toshkent vaqti)
PREFETCH_SECONDS = 120       # ma'lumotni post vaqtidan necha soniya oldin olishni boshlash
MAX_RETRIES = 2              # post chiqmasa, necha marta qayta urinish (5 daqiqa oralig'ida)


def log(*args):
    print(*args, flush=True)


def next_slot(now):
    """now dan keyingi eng yaqin post vaqtini qaytaradi: (soat, datetime)."""
    for day in (0, 1):
        for h in SLOTS:
            t = (now + datetime.timedelta(days=day)).replace(hour=h, minute=0, second=0, microsecond=0)
            if t > now:
                return h, t


def self_check():
    """Server kerakli saytlarga yetib bora oladimi — natijani egasiga yuboradi."""
    checks = [
        ("bank.uz", "https://bank.uz/uz/currency"),
        ("cbu.uz (oltin)", bot.GOLD_URL),
        ("Telegram", "https://api.telegram.org"),
    ]
    qatorlar = []
    for nom, url in checks:
        t0 = time.time()
        try:
            r = requests.get(url, headers=bot.BROWSER_HEADERS, timeout=25)
            holat = f"{r.status_code}, {len(r.text)} belgi, {time.time() - t0:.1f} soniya"
            if nom == "bank.uz":
                holat += " — kurslar sahifasi TO'LIQ" if bot._sahifa_togri(r.text) else " — sahifa to'liq EMAS"
            qatorlar.append(f"✅ {nom}: {holat}")
        except Exception as e:
            qatorlar.append(f"❌ {nom}: {str(e)[:100]}")

    h, t = next_slot(bot.tashkent_now())
    matn = ("🚂 Bot serverda ishga tushdi.\n" + "\n".join(qatorlar) +
            f"\nKeyingi post: {t.strftime('%d.%m %H:%M')} (Toshkent vaqti)")
    log(matn)
    bot.notify_owner(matn)


def listener_loop():
    """/start va admin hodisalarini doimiy tinglaydi (har 3 soniyada)."""
    while True:
        try:
            bot.run_listen()
        except Exception as e:
            log(f"⚠️ listen xatosi: {str(e)[:150]}")
        time.sleep(3)


def main():
    if not bot.BOT_TOKEN or not bot.CHAT_ID:
        log("❌ BOT_TOKEN yoki CHAT_ID berilmagan. Railway -> Variables bo'limiga qo'shing.")
        while True:          # qayta-qayta qulab tushmasligi uchun kutib turamiz
            time.sleep(3600)

    threading.Thread(target=listener_loop, daemon=True).start()
    self_check()

    if os.environ.get("POST_ON_START") == "1":
        log("🧪 POST_ON_START=1: sinov posti darhol yuboriladi.")
        try:
            bot.run_send()
        except Exception as e:
            log(f"❌ Sinov posti xatosi: {e}")

    last_slot = None
    while True:
        now = bot.tashkent_now()
        after = now if last_slot is None else max(now, last_slot + datetime.timedelta(seconds=1))
        h, t = next_slot(after)
        log(f"⏳ Keyingi post: {t.strftime('%d.%m %H:%M')} (hozir {now.strftime('%H:%M:%S')})")

        wake = t - datetime.timedelta(seconds=PREFETCH_SECONDS)
        while True:
            left = (wake - bot.tashkent_now()).total_seconds()
            if left <= 0:
                break
            time.sleep(min(left, 300))

        ok = False
        for urinish in range(MAX_RETRIES + 1):
            try:
                ok = bot.run_send(target_hour=h, fetch_first=True)
            except Exception as e:
                log(f"❌ Kutilmagan xato: {e}")
                bot.notify_owner(f"⚠️ Kutilmagan xato: {str(e)[:200]}", call=(urinish == MAX_RETRIES))
            if ok:
                break
            if urinish < MAX_RETRIES:
                log(f"🔁 {urinish + 1}-urinish muvaffaqiyatsiz. 5 daqiqadan keyin qayta uriniladi.")
                time.sleep(300)
        last_slot = t
        time.sleep(5)


if __name__ == "__main__":
    try:
        sys.stdout.reconfigure(line_buffering=True)
    except Exception:
        pass
    main()
