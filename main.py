import os
import re
import sys
import json
import math
import html as htmllib
import time
import datetime
import requests

# ============================================================
# bs4/Selenium — FAQAT "send" rejimida kerak, "listen" rejimida kerak emas.
# Shuning uchun bu yerda emas, pastda shu kutubxonalar
# ishlatiladigan funksiyalar ichida import qilinadi — shunda
# "listen" rejimi ularni o'rnatish shart bo'lmasdan ham ishlay oladi.
# ============================================================

# ============================================================
# Vaqtni O'zbekistonga moslash
# ============================================================
os.environ['TZ'] = 'Asia/Tashkent'
if hasattr(time, 'tzset'):
    time.tzset()

# ============================================================
# Token va Chat ID — muhit o'zgaruvchisi sifatida beriladi
# GitHub Actions -> Settings -> Secrets and variables -> Actions
# BOT_TOKEN = botfather tokeni
# CHAT_ID   = kanal ID (masalan @dollorkurslariUZ yoki -100...)
# ============================================================
BOT_TOKEN = os.environ.get("BOT_TOKEN")
CHAT_ID = os.environ.get("CHAT_ID")
OWNER_ID = os.environ.get("OWNER_ID")  # faqat shu Telegram ID'ga ega odamga bot javob beradi
CALLME_USER = os.environ.get("CALLME_USER")  # Telegram @username (yoki +998...) — jiddiy xatoda shu orqali qo'ng'iroq qilinadi

HEADERS = {
    "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36",
    "Accept-Language": "uz,ru;q=0.9,en;q=0.8",
}

OFFSET_FILE = "offset.txt"


UZ_TZ = datetime.timezone(datetime.timedelta(hours=5))


def tashkent_now():
    """Hozirgi Toshkent vaqti. Serverda tzdata/TZ sozlanmagan bo'lsa ham to'g'ri ishlaydi
    (O'zbekistonda yozgi vaqtga o'tish yo'q, doim UTC+5)."""
    return datetime.datetime.now(UZ_TZ)


def wait_until_exact(hour, minute=0, max_late_minutes=120):
    """Aniq HH:MM:00 (Toshkent vaqti) gacha kutadi va True qaytaradi.
    GitHub ishni oldinroq boshlasa, aniq soniyagacha kutib turadi.

    Agar GitHub ishni kech boshlagan bo'lsa:
      - max_late_minutes (120 daqiqa) gacha kechiksa -> darhol yuboradi (True)
      - undan ko'p kechiksa -> False qaytaradi, post BOSHQA SOATDA tashlanmasligi uchun
        o'sha post o'tkazib yuboriladi."""
    now = tashkent_now()
    target = now.replace(hour=hour, minute=minute, second=0, microsecond=0)

    if target <= now:
        kechikish = (now - target).total_seconds()
        if kechikish > max_late_minutes * 60:
            print(f"⛔ Ish {int(kechikish // 60)} daqiqa kechikib boshlandi (limit {max_late_minutes} daqiqa) — "
                  f"post boshqa soatda tashlanmasligi uchun o'tkazib yuborildi.")
            return False
        print(f"⚠️ Ish {int(kechikish)} soniya kechikib boshlandi — darhol yuboriladi.")
        return True

    wait_seconds = (target - now).total_seconds()
    print(f"⏳ Aniq {hour:02d}:{minute:02d}:00 gacha {int(wait_seconds)} soniya kutilmoqda...")
    time.sleep(wait_seconds)
    print(f"🚀 Vaqt keldi: {tashkent_now().strftime('%H:%M:%S')} — yuborishni boshlaymiz.")
    return True


def parse_price(matn):
    """Narx matnini songa aylantiradi, kasr qismini (nuqta/vergul) HISOBGA OLADI.
    '11 887.37 so'm' -> 11887 ;  '11 850 so'm' -> 11850 ;  '11 887,37' -> 11887.
    (Avval nuqta tashlab yuborilib, '11 887.37' -> 1188737 bo'lib qolardi — Openbank xatosi.)"""
    if not matn:
        return 0
    t = str(matn).replace("\u00a0", " ")
    m = re.search(r"\d[\d ]*(?:[.,]\d+)?", t)
    if not m:
        return 0
    try:
        return int(round(float(m.group(0).replace(" ", "").replace(",", "."))))
    except ValueError:
        return 0


def tozalash(matn):
    """Matndan faqat raqamlarni ajratib oladi."""
    if not matn:
        return 0
    toza_son = "".join(filter(str.isdigit, str(matn)))
    return int(toza_son) if toza_son else 0


def tg_send(chat_id, text, parse_mode="HTML", reply_markup=None):
    data = {"chat_id": chat_id, "text": text, "parse_mode": parse_mode,
            "disable_web_page_preview": True}
    if reply_markup is not None:
        data["reply_markup"] = json.dumps(reply_markup)
    resp = requests.post(f"https://api.telegram.org/bot{BOT_TOKEN}/sendMessage", data=data)
    if resp.status_code != 200:
        print(f"❌ Telegram xatosi ({chat_id}): {resp.text}")
    return resp


# ============================================================
# 1) BARCHA BANKLARNING USD KURSI — bank.uz orqali
# Har bir bank ALOHIDA qayta ishlanadi: birontasi noto'g'ri
# chiqsa, faqat o'sha bank tashlab ketiladi, qolganlari yuboriladi.
# ============================================================
BANKUZ_URL = "https://bank.uz/uz/currency"

BROWSER_HEADERS = {
    "User-Agent": ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
                   "(KHTML, like Gecko) Chrome/124.0 Safari/537.36"),
    "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
    "Accept-Language": "uz,ru;q=0.9,en;q=0.8",
    "Upgrade-Insecure-Requests": "1",
    "Referer": "https://www.google.com/",
}


def _sahifa_togri(html):
    """Yuklangan matn haqiqiy bank.uz kurslar sahifasimi (bo'sh/bloklash sahifasi emasmi)."""
    return bool(html) and len(html) > 20000 and "Sotish" in html


def _fetch_requests():
    last = None
    for timeout in (15, 25):
        try:
            r = requests.get(BANKUZ_URL, headers=BROWSER_HEADERS, timeout=timeout)
            r.raise_for_status()
            if _sahifa_togri(r.text):
                return r.text
            last = f"sahifa to'liq emas (uzunligi {len(r.text)})"
        except requests.exceptions.RequestException as e:
            last = str(e)
        time.sleep(3)
    raise RuntimeError(last)


def _fetch_chrome():
    """Haqiqiy brauzer (headless Chrome) orqali ochish — oddiy so'rovni
    o'tkazmaydigan holatlarda ba'zan ishlaydi."""
    from selenium import webdriver
    from selenium.webdriver.chrome.service import Service
    from selenium.webdriver.chrome.options import Options
    from webdriver_manager.chrome import ChromeDriverManager

    options = Options()
    options.add_argument("--headless=new")
    options.add_argument("--no-sandbox")
    options.add_argument("--disable-dev-shm-usage")
    options.add_argument("--window-size=1920,1080")
    options.add_argument(f"user-agent={BROWSER_HEADERS['User-Agent']}")
    options.page_load_strategy = "eager"

    driver = webdriver.Chrome(service=Service(ChromeDriverManager().install()), options=options)
    driver.set_page_load_timeout(60)
    try:
        driver.get(BANKUZ_URL)
        time.sleep(3)
        html = driver.page_source
    finally:
        driver.quit()
    if not _sahifa_togri(html):
        raise RuntimeError(f"sahifa to'liq emas (uzunligi {len(html or '')})")
    return html


def _fetch_proxy():
    """Uchinchi urinish: ochiq proksi orqali (GitHub serveri bank.uz ga to'g'ridan-to'g'ri
    yetib bormasa). Bu usul tashqi xizmatga bog'liq, shuning uchun oxirgi chora."""
    from urllib.parse import quote
    r = requests.get("https://api.allorigins.win/raw?url=" + quote(BANKUZ_URL, safe=""),
                     headers=BROWSER_HEADERS, timeout=45)
    r.raise_for_status()
    if not _sahifa_togri(r.text):
        raise RuntimeError(f"sahifa to'liq emas (uzunligi {len(r.text)})")
    return r.text


def fetch_bankuz_html():
    """bank.uz sahifasini 3 xil yo'l bilan olishga urinadi. Qaysi biri birinchi
    ishlasa, o'shani qaytaradi: (html, usul_nomi). Hammasi muvaffaqiyatsiz bo'lsa,
    har bir usulning xatosini ko'rsatib RuntimeError chiqaradi."""
    xatolar = []
    for nom, func in (("oddiy so'rov", _fetch_requests),
                      ("Chrome brauzer", _fetch_chrome),
                      ("proksi", _fetch_proxy)):
        t0 = time.time()
        try:
            html = func()
            return html, nom
        except Exception as e:
            sabab = f"{nom}: {str(e)[:150]}"
            print(f"⚠️ {sabab} ({int(time.time() - t0)} soniya)")
            xatolar.append(sabab)
    raise RuntimeError("bank.uz ga hech qaysi usul bilan yetib bo'lmadi -> " + " | ".join(xatolar))


def get_all_bank_rates():
    from bs4 import BeautifulSoup  # faqat shu funksiya chaqirilganda kerak

    html_text, usul = fetch_bankuz_html()
    print(f"✅ bank.uz sahifasi '{usul}' usuli bilan olindi ({len(html_text)} belgi).")
    soup = BeautifulSoup(html_text, "html.parser")

    container = soup.find(id="best_USD") or soup
    html = str(container)

    # ">Sotish<" — orasida bo'sh joy/yangi qator bo'lishi mumkin, shuning uchun
    # oddiy .find() emas, whitespace'ga chidamli regex ishlatamiz.
    match = re.search(r">\s*Sotish\s*<", html)

    if match is None:
        # muqobil urinish: BeautifulSoup orqali matni aynan "Sotish" bo'lgan
        # (boshqa so'z ichida emas, mustaqil) tegni qidiramiz.
        sotish_tag = None
        for tag in container.find_all(True):
            if tag.get_text(strip=True) == "Sotish" and len(tag.find_all(True)) == 0:
                sotish_tag = tag
                break
        if sotish_tag is None:
            raise ValueError(
                f"bank.uz sahifa tuzilishi o'zgargan bo'lishi mumkin "
                f"(javob uzunligi={len(html_text)}, usul={usul})"
            )
        split_idx = html.find(str(sotish_tag))
    else:
        split_idx = match.start()

    buy_html, sell_html = html[:split_idx], html[split_idx:]

    def extract_bank_value(a_tag, full_html):
        """Bitta bank narxini TOPISH UCHUN 3 TA USUL bilan urinadi.
        Har bir topilgan qiymat 5 000 - 30 000 so'm oralig'ida bo'lishi shart
        (1 dollar uchun mantiqiy narx); aks holda keyingi usulga o'tiladi.
        Uchalasi ham muvaffaqiyatsiz bo'lsagina None qaytaradi (bank o'tkazib yuboriladi)."""

        def togri(v):
            return 5000 <= v <= 30000

        narx_re = r"(\d[\d\s\u00a0]*(?:[.,]\d+)?)\s*so'm"

        # 1-urinish: elementdan keyingi eng yaqin "so'm" so'zini qidirish
        try:
            price_node = a_tag.find_next(string=re.compile(r"so'm"))
            val = parse_price(price_node)
            if togri(val):
                return val
        except Exception:
            pass

        # 2-urinish: ota (parent) elementning ichidagi butun matndan raqam qidirish
        try:
            parent = a_tag.find_parent()
            if parent:
                m = re.search(narx_re, parent.get_text(" ", strip=True))
                if m and togri(parse_price(m.group(1))):
                    return parse_price(m.group(1))
        except Exception:
            pass

        # 3-urinish: xom HTML ichida link joylashgan joydan keyingi matndan qidirish
        try:
            idx = full_html.find(str(a_tag))
            if idx != -1:
                m = re.search(narx_re, full_html[idx: idx + 400])
                if m and togri(parse_price(m.group(1))):
                    return parse_price(m.group(1))
        except Exception:
            pass

        return None

    def extract(piece_html):
        piece_soup = BeautifulSoup(piece_html, "html.parser")
        out = {}
        for a in piece_soup.find_all("a", href=re.compile(r"/currency/bank/")):
            name = a.get_text(strip=True)
            if not name:
                continue

            val = extract_bank_value(a, piece_html)
            if val is None:
                print(f"⚠️ '{name}': 3 usul bilan ham narx topilmadi, o'tkazib yuborildi.")
                continue

            href = a.get("href", "")
            full_url = "https://bank.uz" + href if href.startswith("/") else href
            if name not in out:
                out[name] = {"val": val, "url": full_url}
        return out

    buy = extract(buy_html)
    sell = extract(sell_html)

    banks = []
    skipped = []
    for name in set(buy.keys()) | set(sell.keys()):
        if name in buy and name in sell:
            banks.append({
                "name": name,
                "buy_num": buy[name]["val"],
                "sell_num": sell[name]["val"],
                "url": buy[name]["url"],
            })
        else:
            skipped.append(name)

    banks.sort(key=lambda r: r["buy_num"], reverse=True)
    return banks, skipped


# ============================================================
# 2) OLTIN NARXI — cbu.uz (Markaziy bank)
# ============================================================
GOLD_URL = "https://cbu.uz/uz/banknotes-coins/gold-bars/prices/"
GOLD_WEIGHTS = [5, 10, 20, 50, 100]


def parse_gold_text(text):
    """Sahifa matnidan quyma oltinning SOTISH narxlarini oladi (5, 10, 20, 50, 100 gramm).
    XPath ishlatilmaydi: jadvalga yangi ustun/qator qo'shilsa ham buzilmaydi.
    Qaytaradi: ["9028000", "18057000", ...] yoki None."""
    text = text.replace("\u00a0", " ")
    pattern = re.compile(r"(\d{1,3})\s*(?:gramm|gram|грамм)\b[^\d]{0,20}(\d[\d ]{4,}\d)", re.I)
    topilgan = {}
    for m in pattern.finditer(text):
        og = int(m.group(1))
        narx = tozalash(m.group(2))
        if og in GOLD_WEIGHTS and og not in topilgan and narx > 100000:
            topilgan[og] = narx   # har bir og'irlik uchun birinchi raqam = sotish narxi
    if all(w in topilgan for w in GOLD_WEIGHTS):
        return [str(topilgan[w]) for w in GOLD_WEIGHTS]
    return None


def get_gold_prices(driver_path=None):
    """1-usul: oddiy so'rov (tez). 2-usul: Chrome brauzer. Ikkalasida ham matn regex bilan o'qiladi."""
    from bs4 import BeautifulSoup

    # 1-usul: oddiy so'rov
    try:
        r = requests.get(GOLD_URL, headers=BROWSER_HEADERS, timeout=25)
        r.raise_for_status()
        natija = parse_gold_text(BeautifulSoup(r.text, "html.parser").get_text(" "))
        if natija:
            print("🥇 Oltin narxi oddiy so'rov bilan olindi.")
            return natija
        print("⚠️ Oltin: oddiy so'rovda jadval topilmadi, Chrome bilan urinib ko'raman.")
    except Exception as e:
        print(f"⚠️ Oltin: oddiy so'rov xatosi: {str(e)[:150]}")

    # 2-usul: Chrome brauzer
    try:
        from selenium import webdriver
        from selenium.webdriver.chrome.service import Service
        from selenium.webdriver.chrome.options import Options
        from selenium.webdriver.common.by import By
        from webdriver_manager.chrome import ChromeDriverManager

        options = Options()
        options.add_argument("--headless=new")
        options.add_argument("--no-sandbox")
        options.add_argument("--disable-dev-shm-usage")
        options.add_argument("--window-size=1920,1080")
        options.add_argument(f"user-agent={BROWSER_HEADERS['User-Agent']}")
        options.page_load_strategy = "eager"

        path = driver_path or ChromeDriverManager().install()
        driver = webdriver.Chrome(service=Service(path), options=options)
        driver.set_page_load_timeout(80)
        try:
            driver.get(GOLD_URL)
            for _ in range(8):  # jadval yuklanishini 16 soniyagacha kutamiz
                time.sleep(2)
                natija = parse_gold_text(driver.find_element(By.TAG_NAME, "body").text)
                if natija:
                    print("🥇 Oltin narxi Chrome bilan olindi.")
                    return natija
        finally:
            driver.quit()
        print("⚠️ Oltin: Chrome bilan ham jadval topilmadi.")
    except Exception as e:
        print(f"⚠️ Oltin: Chrome xatosi: {str(e)[:150]}")
    return None


# ============================================================
LAST_RATES_FILE = "last_rates.json"


def call_owner(text="Внимание! Бот курсов валют не отправил пост. Проверьте Телеграм."):
    """Egasiga TELEGRAM orqali ovozli qo'ng'iroq qiladi (CallMeBot bepul xizmati orqali).
    Oddiy telefon (GSM) qo'ng'irog'i emas: qo'ng'iroq Telegram ilovasida jiringlaydi.
    CALLME_USER berilmagan bo'lsa, hech narsa qilmaydi."""
    if not CALLME_USER:
        print("ℹ️ CALLME_USER yo'q, qo'ng'iroq qilinmadi.")
        return
    try:
        r = requests.get(
            "http://api.callmebot.com/start.php",
            params={"user": CALLME_USER, "text": text, "lang": "ru-RU-Standard-A", "rpt": 2},
            timeout=30,
        )
        print(f"📞 Qo'ng'iroq so'rovi yuborildi (status {r.status_code}).")
    except Exception as e:
        print(f"⚠️ Qo'ng'iroq qilib bo'lmadi: {str(e)[:120]}")


def notify_owner(text, call=False):
    """Xatolik bo'lsa egasiga (OWNER_ID) shaxsiy xabar yuboradi.
    call=True bo'lsa (faqat jiddiy xatolarda), qo'shimcha ravishda Telegram orqali qo'ng'iroq ham qiladi.
    Egasi botga kamida bir marta /start bosgan bo'lishi kerak."""
    if BOT_TOKEN and OWNER_ID:
        try:
            tg_send(OWNER_ID, text, parse_mode=None)
        except Exception as e:
            print(f"⚠️ Egasiga xabar yuborib bo'lmadi: {e}")
    else:
        print("ℹ️ OWNER_ID yo'q, egasiga xabar yuborilmadi.")
    if call:
        call_owner()


def load_last_rates():
    try:
        with open(LAST_RATES_FILE) as f:
            return json.load(f)
    except (FileNotFoundError, ValueError):
        return {}


def save_last_rates(best_buy, best_sell):
    """Oxirgi YUBORILGAN kursni va bugun nechta post yuborilganini saqlaydi."""
    bugun = tashkent_now().strftime('%Y-%m-%d')
    eski = load_last_rates()
    posts_today = eski.get("posts_today", 0) + 1 if eski.get("posts_date") == bugun else 1
    with open(LAST_RATES_FILE, "w") as f:
        json.dump({"best_buy": best_buy, "best_sell": best_sell,
                   "time": tashkent_now().strftime('%d.%m.%Y %H:%M'),
                   "posts_date": bugun, "posts_today": posts_today}, f)


# ============================================================
# KUZATUV: kurslarni tarixga yozish (rates_log.csv) va
# faqat kurs o'zgarganda post tashlash
# ============================================================
RATES_LOG_FILE = "rates_log.csv"
WATCH_MIN_CHANGE = 20      # eng yaxshi xarid/sotuv kamida shuncha so'mga o'zgarsa post tashlanadi
MAX_POSTS_PER_DAY = 5      # kuniga eng ko'pi bilan shuncha post (kanal to'lib ketmasligi uchun)


def append_rates_log(banks):
    """Har bir bankning kursini vaqti bilan rates_log.csv ga qo'shib boradi.
    Keyin shu fayldan kurslar qaysi soatda o'zgarishini tahlil qilish mumkin."""
    yangi_fayl = not os.path.exists(RATES_LOG_FILE)
    vaqt = tashkent_now().strftime('%Y-%m-%d %H:%M')
    with open(RATES_LOG_FILE, "a", encoding="utf-8") as f:
        if yangi_fayl:
            f.write("time,bank,buy,sell\n")
        for r in banks:
            nom = r["name"].replace(",", " ")
            f.write(f"{vaqt},{nom},{r['buy_num']},{r['sell_num']}\n")


def run_watch(post=True):
    """python main.py log    -> faqat tarixga yozadi, kanalga hech narsa yubormaydi
    python main.py watch  -> tarixga yozadi VA kurs o'zgargan bo'lsagina kanalga post tashlaydi"""
    print("🔎 Kurslar tekshirilmoqda...")
    try:
        banks, skipped = get_all_bank_rates()
    except Exception as e:
        print(f"❌ bank.uz'dan ma'lumot olib bo'lmadi: {e}")
        return

    if len(banks) < 5:
        print(f"❌ Faqat {len(banks)} ta bank topildi, tarixga yozilmadi.")
        return

    append_rates_log(banks)
    print(f"📝 {len(banks)} ta bank kursi {RATES_LOG_FILE} ga yozildi.")

    if not post:
        print("ℹ️ Faqat yozish rejimi: kanalga post yuborilmaydi.")
        return

    ey_x_val = max(r["buy_num"] for r in banks)
    ey_s_val = min(r["sell_num"] for r in banks)
    oldingi = load_last_rates()

    if oldingi.get("best_buy") is not None:
        farq = max(abs(ey_x_val - oldingi["best_buy"]), abs(ey_s_val - oldingi["best_sell"]))
        if farq < WATCH_MIN_CHANGE:
            print(f"➖ Kurs deyarli o'zgarmadi (farq {farq} so'm < {WATCH_MIN_CHANGE}), post tashlanmaydi.")
            return

    if oldingi.get("posts_date") == tashkent_now().strftime('%Y-%m-%d') and oldingi.get("posts_today", 0) >= MAX_POSTS_PER_DAY:
        print(f"⛔ Bugun {MAX_POSTS_PER_DAY} ta post yuborilgan, limit tugadi.")
        return

    print("📈 Kurs o'zgargan — post tashlanmoqda...")
    run_send()


def farq_belgisi(yangi, eski):
    """Oldingi yuborilgan kursga nisbatan o'zgarish: ▲ +40 yoki ▼ -20."""
    if eski is None:
        return ""
    farq = yangi - eski
    if farq > 0:
        return f" (▲ +{farq})"
    if farq < 0:
        return f" (▼ {farq})"
    return " (➖ 0)"


def get_cbu_usd_rate():
    """Markaziy bankning rasmiy USD kursi. Olinmasa None qaytaradi (xabar shundoq ham yuboriladi)."""
    try:
        resp = requests.get("https://cbu.uz/oz/arkhiv-kursov-valyut/json/USD/",
                            headers=HEADERS, timeout=20)
        resp.raise_for_status()
        rate = float(resp.json()[0]["Rate"])
        return f"{rate:,.2f}".replace(",", " ")
    except Exception as e:
        print(f"⚠️ MB kursini olishda xatolik: {e}")
        return None


# ============================================================
# REJIM 1: "send" — kurslarni kanalga yuborish
# (GitHub Actions'da 08:00, 13:00, 19:00 da cron orqali chaqiriladi)
# ============================================================
def run_send(target_hour=None, fetch_first=False):
    if not BOT_TOKEN or not CHAT_ID:
        print("❌ BOT_TOKEN yoki CHAT_ID muhit o'zgaruvchisi topilmadi.")
        return False

    # agar aniq soat berilgan bo'lsa (masalan 9, 13, 19) —
    # shu soatning aniq 00-soniyasigacha kutib turamiz, keyin yuboramiz.
    if target_hour is not None and not fetch_first:
        if not wait_until_exact(int(target_hour)):
            notify_owner(f"⚠️ {int(target_hour):02d}:00 dagi post o'tkazib yuborildi: GitHub ishni 2 soatdan ko'p kechiktirdi.", call=True)
            return False

    print("💱 bank.uz orqali barcha banklar kursi olinmoqda...")
    try:
        banks, skipped = get_all_bank_rates()
    except Exception as e:
        print(f"❌ bank.uz'dan ma'lumot olishda xatolik: {e}")
        notify_owner(f"⚠️ Kurs kanalga yuborilmadi.\nSabab: bank.uz'dan ma'lumot olib bo'lmadi.\n{e}", call=True)
        return False

    print(f"✅ {len(banks)} ta bank topildi.")
    set_rates_cache(banks)
    if skipped:
        print(f"⚠️ O'tkazib yuborildi ({len(skipped)} ta): {', '.join(skipped)}")

    if len(banks) < 5:
        print("❌ Juda kam bank topildi, ehtimol sayt tuzilishi o'zgargan. Xabar yuborilmadi.")
        notify_owner(f"⚠️ Kurs kanalga yuborilmadi.\nSabab: faqat {len(banks)} ta bank topildi (sayt tuzilishi o'zgargan bo'lishi mumkin).", call=True)
        return False

    print("🥇 Oltin narxi olinmoqda...")
    gold_values = get_gold_prices()
    if not gold_values:
        notify_owner("⚠️ Oltin narxi olinmadi (cbu.uz XPath o'zgargan bo'lishi mumkin). Xabar oltin narxisiz yuborildi.")

    mb_kursi = get_cbu_usd_rate()

    ey_x_val = max(r["buy_num"] for r in banks)
    ey_s_val = min(r["sell_num"] for r in banks)
    ey_x = f"{ey_x_val:,}".replace(",", " ")
    ey_s = f"{ey_s_val:,}".replace(",", " ")

    # oldingi yuborilgan kursga nisbatan o'zgarish
    oldingi = load_last_rates()
    xarid_farq = farq_belgisi(ey_x_val, oldingi.get("best_buy"))
    sotuv_farq = farq_belgisi(ey_s_val, oldingi.get("best_sell"))

    # eng yaxshi narx(lar)ni beruvchi bank nomlari (bir nechta bank teng bo'lishi mumkin)
    eng_yahshi_xarid_banklar = ", ".join(sorted({r["name"] for r in banks if r["buy_num"] == ey_x_val}))
    eng_yahshi_sotuv_banklar = ", ".join(sorted({r["name"] for r in banks if r["sell_num"] == ey_s_val}))

    xabar = f"<b>🏦 KUNLIK VALYUTA NARXLARI ($)</b>\n"
    if mb_kursi:
        xabar += f"🏛 <b>Markaziy bank kursi:</b> {mb_kursi} so'm\n"
    xabar += f"— — — — — — — — — — — — — — —\n"
    xabar += f"🏛 Bank nomi | Xarid | Sotuv \n— — — — — — — — — — — — — — —\n"
    for r in banks:
        buy_str = f"{r['buy_num']:,}".replace(",", " ")
        sell_str = f"{r['sell_num']:,}".replace(",", " ")
        # eng yuqori xarid yoki eng past sotuv narxini beradigan bank(lar) — ⭐ bilan ajralib turadi
        icon = "⭐" if (r["buy_num"] == ey_x_val or r["sell_num"] == ey_s_val) else "🔹"
        xabar += f"{icon} <a href='{r['url']}'>{r['name']}</a> | {buy_str} | {sell_str}\n"
    xabar += f"— — — — — — — — — — — — — — —\n"
    xabar += (f"<blockquote>⭐ Eng yaxshi xarid: {ey_x} so'm{xarid_farq} — {eng_yahshi_xarid_banklar}\n"
              f"⭐ Eng yaxshi sotuv: {ey_s} so'm{sotuv_farq} — {eng_yahshi_sotuv_banklar}</blockquote>\n")

    if gold_values:
        # cbu.uz'dan kelgan matnni tozalab, valyuta jadvali kabi "9 000 000" formatiga solamiz
        gramlar = ["5 gramm", "10 gramm", "20 gramm", "50 gramm", "100 gramm"]
        xabar += f"\n<b>💰 Quyma oltin narxlari:</b>\n"
        for gram_nomi, xom_qiymat in zip(gramlar, gold_values):
            son = tozalash(xom_qiymat)
            formatlangan = f"{son:,}".replace(",", " ") if son else xom_qiymat
            xabar += f"🟡 {gram_nomi}: {formatlangan} so'm\n"

    if skipped:
        xabar += f"\n<i>⚠️ Ushbu banklar o'tkazib yuborildi: {', '.join(skipped)}</i>\n"

    xabar += f"\n🕒 <b>Yangilandi:</b> @@VAQT@@\n📢 @dollarkursiuzz"

    # server rejimi: ma'lumot oldindan olingan, endi aniq soniyagacha kutib yuboramiz
    if target_hour is not None and fetch_first:
        if not wait_until_exact(int(target_hour)):
            notify_owner(f"⚠️ {int(target_hour):02d}:00 dagi post o'tkazib yuborildi: ish juda kech boshlandi.", call=True)
            return False
    xabar = xabar.replace("@@VAQT@@", tashkent_now().strftime('%d.%m.%Y %H:%M'))

    print("📤 Telegramga yuborilmoqda...")
    resp = tg_send(CHAT_ID, xabar)
    if resp.status_code != 200:
        notify_owner(f"⚠️ Kurs kanalga yuborilmadi.\nSabab: Telegram xatosi: {resp.text}", call=True)
        return False

    # faqat muvaffaqiyatli yuborilgandan keyin keyingi safar solishtirish uchun eslab qolamiz
    save_last_rates(ey_x_val, ey_s_val)
    print(f"✅ Yuborildi: {len(banks)} ta bank kursi. O'tkazib yuborilganlar: {len(skipped)} ta.")
    return True


# ============================================================
# BOT: /start, tugmalar va hisoblagich (dollar <-> so'm)
# ============================================================
CHANNEL_USERNAME = "@dollarkursiuzz"
BTN_USD = "💵 Dollar → So'm"
BTN_UZS = "💰 So'm → Dollar"
KEYBOARD = {
    "keyboard": [[{"text": BTN_USD}, {"text": BTN_UZS}]],
    "resize_keyboard": True,
    "is_persistent": True,
}

USER_STATE = {}                          # foydalanuvchi qaysi tugmani bosgani (xotirada)
RATES_CACHE = {"ts": 0.0, "banks": None}
RATES_CACHE_TTL = 600                    # kurslarni 10 daqiqa eslab turamiz


def set_rates_cache(banks):
    RATES_CACHE["ts"] = time.time()
    RATES_CACHE["banks"] = banks


def get_rates_for_converter():
    """(banklar, necha_daqiqa_oldin) qaytaradi. Yangi bo'lmasa bank.uz dan qayta oladi.
    Hech narsa olib bo'lmasa (None, None)."""
    now = time.time()
    if RATES_CACHE["banks"] and now - RATES_CACHE["ts"] < RATES_CACHE_TTL:
        return RATES_CACHE["banks"], int((now - RATES_CACHE["ts"]) // 60)
    try:
        banks, _ = get_all_bank_rates()
        if len(banks) >= 5:
            set_rates_cache(banks)
            return banks, 0
    except Exception as e:
        print(f"⚠️ Hisoblagich uchun kurs olib bo'lmadi: {str(e)[:150]}")
    if RATES_CACHE["banks"]:
        return RATES_CACHE["banks"], int((now - RATES_CACHE["ts"]) // 60)
    return None, None


def detect_kind(text):
    """Matndan nima hisoblash kerakligini aniqlaydi: 'usd' ($ -> so'm) yoki 'uzs' (so'm -> $)."""
    t = text.lower()
    if "$" in t or re.search(r"dollar|dollor|usd", t):
        return "usd"
    if re.search(r"so['‘’`]?m|\bsum\b|ming|\bmin\b|mln|million|milyon|uzs", t):
        return "uzs"
    return None


def parse_amount(text):
    """'2', '2.5', '100$', '400000', '400 000', '400 ming', '1.5 mln' -> float yoki None."""
    t = text.lower().replace("\u00a0", " ")
    if re.match(r"\s*-", t):
        return None                          # manfiy son qabul qilinmaydi
    mult = 1
    if re.search(r"mln|million|milyon", t):
        mult = 1_000_000
    elif re.search(r"ming|\bmin\b|\d\s*k\b", t):
        mult = 1000
    m = re.search(r"\d[\d ]*(?:[.,]\d+)*", t)
    if not m:
        return None
    sraw = m.group(0).strip().replace(" ", "")
    if re.fullmatch(r"\d{1,3}([,.]\d{3}){2,}", sraw):
        sraw = re.sub(r"[,.]", "", sraw)    # 1,500,000 yoki 1.500.000
    elif re.fullmatch(r"\d{1,3},\d{3}", sraw) and mult == 1:
        sraw = sraw.replace(",", "")        # 1,500
    else:
        sraw = sraw.replace(",", ".")       # 2,5
    try:
        v = float(sraw) * mult
    except ValueError:
        return None
    return v if 0 < v <= 1_000_000_000 else None


def _fmt(n):
    return f"{int(n):,}".replace(",", " ")


def _fmt_amount(v):
    return _fmt(v) if float(v).is_integer() else f"{v:.2f}".rstrip("0").rstrip(".")


def convert_reply(kind, amount):
    """Hisoblash natijasi matni (HTML). Dollar -> so'm: eng YUQORI xarid kursi.
    So'm -> dollar: eng PAST sotuv kursi."""
    banks, yosh = get_rates_for_converter()
    if not banks:
        return "😔 Hozir bank kurslarini ola olmadim. Birozdan keyin qayta urinib ko'ring."

    yangilik = "🕒 Kurslar hozirgina olindi" if yosh == 0 else f"🕒 Kurslar {yosh} daqiqa oldin olingan"

    if kind == "usd":
        b = max(banks, key=lambda r: r["buy_num"])
        jami = round(amount * b["buy_num"])
        return (f"💵 <b>{_fmt_amount(amount)} dollar</b> = <b>{_fmt(jami)} so'm</b>\n\n"
                f"🏦 Bank: <b>{htmllib.escape(b['name'])}</b> (eng yuqori xarid kursi)\n"
                f"📈 Kurs: 1 dollar = {_fmt(b['buy_num'])} so'm\n\n"
                f"<i>Bank dollaringizni shu kurs bo'yicha sotib oladi.</i>\n{yangilik}")

    b = min(banks, key=lambda r: r["sell_num"])
    dollar = math.floor(amount / b["sell_num"] * 100) / 100
    return (f"💰 <b>{_fmt_amount(amount)} so'm</b> = <b>{dollar:.2f} dollar</b>\n\n"
            f"🏦 Bank: <b>{htmllib.escape(b['name'])}</b> (eng arzon sotuv kursi)\n"
            f"📉 Kurs: 1 dollar = {_fmt(b['sell_num'])} so'm\n\n"
            f"<i>Bank sizga dollarni shu kurs bo'yicha sotadi.</i>\n{yangilik}")


def welcome_text(first_name):
    ism = htmllib.escape(first_name or "do'stim")
    return (f"👋 Assalomu alaykum, <b>{ism}</b>!\n\n"
            "Men O'zbekiston banklaridagi dollar kurslarini kuzatib boradigan botman.\n\n"
            "<b>Nimalar qila olaman:</b>\n"
            "💵 <b>Dollar → So'm</b>: dollaringizni so'mga hisoblab beraman va "
            "eng yuqori narx beradigan bankni ko'rsataman\n"
            "💰 <b>So'm → Dollar</b>: so'mingizga necha dollar olishingizni hisoblab beraman va "
            "eng arzon sotadigan bankni ko'rsataman\n\n"
            f"📢 {CHANNEL_USERNAME} kanalida kunlik dollar kurslari va quyma oltin narxlari "
            "har kuni yangilanib turadi.\n\n"
            "Quyidagi tugmalardan birini tanlang 👇")


def handle_private_message(msg):
    """Shaxsiy chatdagi xabarlar: /start, tugmalar va summa kiritish. Hamma foydalanuvchi uchun."""
    if msg.get("chat", {}).get("type") != "private":
        return
    chat_id = msg["chat"]["id"]
    user_id = msg.get("from", {}).get("id", chat_id)
    text = (msg.get("text") or "").strip()

    if text.startswith("/start") or text.startswith("/help"):
        USER_STATE.pop(user_id, None)
        tg_send(chat_id, welcome_text(msg.get("from", {}).get("first_name")), reply_markup=KEYBOARD)
        return

    if text == BTN_USD:
        USER_STATE[user_id] = "usd"
        tg_send(chat_id, "💵 Necha dollar? Raqam yozing, masalan: <b>2</b> yoki <b>100</b>",
                reply_markup=KEYBOARD)
        return

    if text == BTN_UZS:
        USER_STATE[user_id] = "uzs"
        tg_send(chat_id, "💰 Necha so'm? Raqam yozing, masalan: <b>400000</b> yoki <b>400 ming</b>",
                reply_markup=KEYBOARD)
        return

    if text.startswith("/"):
        return

    amount = parse_amount(text)
    kind = detect_kind(text) or USER_STATE.get(user_id)
    if amount is None or kind is None:
        tg_send(chat_id, "Tugmalardan birini tanlang va raqam yozing 👇", reply_markup=KEYBOARD)
        return
    tg_send(chat_id, convert_reply(kind, amount), reply_markup=KEYBOARD)


# ============================================================
# REJIM 2: "listen" — /start va kanalga admin qilib qo'shilganini tinglash
# (GitHub Actions'da har 2-3 daqiqada chaqiriladi)
# ============================================================
def ensure_no_webhook():
    """Botda webhook yoqilgan bo'lsa, getUpdates ishlamaydi (409 xatosi).
    Webhook bor bo'lsa, uni o'chiradi. True qaytarsa — webhook o'chirildi."""
    try:
        info = requests.get(f"https://api.telegram.org/bot{BOT_TOKEN}/getWebhookInfo", timeout=20).json()
        url = (info.get("result") or {}).get("url") or ""
        if not url:
            print("ℹ️ Webhook yo'q.")
            return False
        host = re.sub(r"^https?://", "", url).split("/")[0]
        print(f"🧹 Botda webhook topildi ({host}) — o'chirilmoqda...")
        r = requests.get(f"https://api.telegram.org/bot{BOT_TOKEN}/deleteWebhook", timeout=20)
        print(f"🧹 deleteWebhook javobi: {r.text[:120]}")
        return bool(r.json().get("ok"))
    except Exception as e:
        print(f"⚠️ Webhook'ni tekshirib bo'lmadi: {str(e)[:120]}")
        return False


def run_listen():
    if not BOT_TOKEN:
        print("❌ BOT_TOKEN topilmadi.")
        return

    try:
        with open(OFFSET_FILE) as f:
            offset = int(f.read().strip())
    except (FileNotFoundError, ValueError):
        offset = 0

    resp = requests.get(
        f"https://api.telegram.org/bot{BOT_TOKEN}/getUpdates",
        params={
            "offset": offset,
            "timeout": 0,
            "allowed_updates": json.dumps(["message", "my_chat_member"]),
        },
        timeout=20,
    )
    data = resp.json()
    if not data.get("ok"):
        print(f"❌ getUpdates xatosi: {data}")
        if data.get("error_code") == 409 and "webhook" in str(data.get("description", "")).lower():
            ensure_no_webhook()
        return

    updates = data.get("result", [])
    if updates:
        print(f"📥 {len(updates)} ta yangi hodisa.")

    for u in updates:
        offset = u["update_id"] + 1

        # shaxsiy chatdagi xabarlar (/start, tugmalar, summa) — hamma foydalanuvchi uchun
        msg = u.get("message")
        if msg and msg.get("text") and msg.get("chat", {}).get("type") == "private":
            try:
                handle_private_message(msg)
            except Exception as e:
                print(f"⚠️ Xabarni qayta ishlashda xato: {str(e)[:150]}")

        # kanalga admin qilib qo'shilganda — FAQAT egasi qo'shgan bo'lsa xabar beradi
        cm = u.get("my_chat_member")
        if cm:
            chat = cm["chat"]
            new_status = cm["new_chat_member"]["status"]
            actor_id = str(cm["from"]["id"])

            if OWNER_ID and actor_id != str(OWNER_ID):
                print(f"⛔ Begona odam botni admin qildi (id={actor_id}) — e'tiborsiz qoldirildi.")
                continue

            if new_status == "administrator":
                tg_send(
                    chat["id"],
                    "✅ Admin qilib qo'shganingiz uchun rahmat!\n"
                    "Endi har kuni 08:00, 13:00, 19:00 da kurslarni shu yerga yuboraman.",
                )
                print(f"✅ Admin xabari yuborildi: {chat['id']} ({chat.get('title')})")

    with open(OFFSET_FILE, "w") as f:
        f.write(str(offset))


# ============================================================
# KIRISH NUQTASI
# python main.py send    -> kurslarni yuborish
# python main.py listen  -> /start va admin hodisalarini tinglash
# ============================================================
if __name__ == "__main__":
    mode = sys.argv[1] if len(sys.argv) > 1 else "send"
    if mode == "send":
        # python main.py send        -> darhol yuboradi (qo'lda test uchun)
        # python main.py send 8      -> aniq 08:00:00 gacha kutib, keyin yuboradi
        hour_arg = sys.argv[2] if len(sys.argv) > 2 else None
        run_send(target_hour=hour_arg)
    elif mode == "listen":
        run_listen()
    elif mode == "log":
        run_watch(post=False)
    elif mode == "watch":
        run_watch(post=True)
    else:
        print(f"❌ Noma'lum rejim: {mode}. 'send', 'listen', 'log' yoki 'watch' dan birini bering.")
