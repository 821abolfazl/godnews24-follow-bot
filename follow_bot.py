# ربات خبررسان شخصی GodNews24 — نسخه‌ی دنبال‌کننده (follower bot)
#
# ربات جدا از ربات اصلی کانال: اعضا این ربات رو استارت می‌کنن، با /follow
# موضوع انتخاب می‌کنن و خبرهای مهم همون موضوع رو به‌صورت فوروارد دایرکت می‌گیرن.
# این ربات هیچ‌وقت توی کانال پست نمی‌ذاره و پروکسی هم نداره.
#
# راه‌اندازی:
#   1) توی BotFather یه ربات جدید بساز؛
#   2) توکن رو توی ریپو (عمومی) به اسم FOLLOW_BOT_TOKEN به‌عنوان Secret بذار
#      (هیچ‌وقت توی چت یا فایل نفرست)؛
#   3) ربات رو ادمین کانال @GodNews24 کن تا پست‌های کانال رو ببینه؛
#   4) ADMIN_CHAT_ID رو هم به‌عنوان Secret بذار.
#
# حریم خصوصی: ریپو عمومیه (برای اینکه حلقه‌ی ۲۴ ساعته‌ی Actions سقف دقیقه
# نداشته باشه). به همین دلیل followers.json روی دیسک رمزنگاری‌شده ذخیره می‌شه
# (Fernet، کلید از خود توکن ربات ساخته می‌شه) و هیچ‌وقت داده‌ی دنبال‌کننده‌ها
# به‌صورت متن ساده توی ریپو یا لاگ نمی‌ره. اگه FOLLOW_BOT_TOKEN نباشه، ربات
# با صدای بلند خطا می‌ده و هیچ فایل متنی نمی‌سازه.

import base64
import hashlib
import json
import os
import re
import subprocess
import sys
import time

import requests

BOT_VERSION = "2026-11-16-0.03"   # فقط توی لاگ و برای ادمین در /start نوشته می‌شه

TOKEN = os.environ.get("FOLLOW_BOT_TOKEN") or ""
ADMIN_CHAT_ID = os.environ.get("ADMIN_CHAT_ID") or ""
CHANNEL = os.environ.get("CHANNEL") or "@GodNews24"
LOOP_MINUTES = int(os.environ.get("LOOP_MINUTES") or 0)

STATE_FILE = "followers.json"
POLL_SECONDS = 2             # فاصله‌ی کوتاه بین دو دور؛ انتظار واقعی رو long polling انجام می‌ده
PENDING_TIMEOUT = 600        # ثانیه؛ اگه عضو تا این مدت جواب نده، انتظار لغو می‌شه
COMMIT_EVERY_SEC = 300       # حافظه حداکثر هر ۵ دقیقه توی گیت‌هاب کامیت می‌شه
MEMBER_CACHE_TTL = 600       # ثانیه؛ نتیجه‌ی عضویت کانال ۱۰ دقیقه کش می‌شه
SUBMIT_COOLDOWN_SEC = 60     # ثانیه؛ هر عضو حداکثر هر ۶۰ ثانیه یه خبر می‌فرسته
MEMBER_ALLOWED = {"creator", "administrator", "member", "restricted"}
_MEMBER_CACHE = {}           # chat_id -> (allowed, timestamp) — فقط توی حافظه، ذخیره نمی‌شه
_SUBMIT_LAST = {}            # chat_id -> timestamp آخرین ارسال خبر — فقط توی حافظه

# ====== موضوع‌ها ======
# کلید داخلی → برچسب فارسی (با ایموجی، همون ایموجی اول پست‌های کانال)
TOPICS = {
    "political": "🔴 سیاسی",
    "sport": "⚽ ورزشی",
    "gold": "🟡 طلا و دلار",
}
# ایموجی موضوع‌های سفارشی (موضوع جدید که می‌سازی، با این ایموجی نشون داده می‌شه)
CUSTOM_TOPIC_EMOJI = "📌"
TOPIC_NAMES = {
    "سیاسی": "political",
    "ورزشی": "sport",
    "طلا و دلار": "gold",
    "طلا": "gold",
    "دلار": "gold",
}

# کلمه‌های کلیدی — از ربات اصلی کانال (bot.py) برداشته شده، ساده‌شده:
POLITICS_KW = ["رئیس‌جمهور", "رییس‌جمهور", "رهبر انقلاب", "مجلس", "دولت", "وزیر",
               "تحریم", "مذاکره", "شورای امنیت", "ترامپ", "نتانیاهو", "بایدن",
               "سیاست", "انتخابات", "سپاه", "پزشکیان", "اسرائیل", "آمریکا",
               "سران", "نشست", "سفیر", "دیپلمات", "پارلمان", "قطعنامه", "ناتو",
               "سازمان ملل", "کاخ سفید", "کرملین", "ارتش", "نظامی", "آتش‌بس", "پوتین", "اوکراین"]
FLASH_KW = ["فوری", "عاجل", "انفجار", "حمله", "موشک", "زلزله", "کشته", "ترور"]
SPORT_KW = ["فوتبال", "لیگ", "جام جهانی", "بازیکن", "مربی", "تیم ملی", "المپیک",
            "والیبال", "کشتی", "بسکتبال", "پرسپولیس", "استقلال", "گل زد"]
GOLD_KW = ["طلا", "دلار", "ارز", "سکه"]


# ====== رمزنگاری حافظه (Fernet؛ کلید از خود توکن ربات) ======
def _fernet():
    """کلید Fernet از sha256 توکن ربات ساخته می‌شه؛ پس Secret دومی لازم نیست.
    اگه توکن نباشه، با صدای بلند خطا می‌ده تا هیچ‌وقت متن ساده نوشته نشه."""
    token = os.environ.get("FOLLOW_BOT_TOKEN") or ""
    if not token:
        raise SystemExit("FOLLOW_BOT_TOKEN is not set — refusing to read/write plaintext state")
    from cryptography.fernet import Fernet
    key = base64.urlsafe_b64encode(hashlib.sha256(token.encode("utf-8")).digest())
    return Fernet(key)


def load_state():
    """حافظه رو از followers.json (رمزنگاری‌شده) می‌خونه؛ اگه فایل نباشه، خالی برمی‌گردونه."""
    state = {"users": {}, "seen_posts": [], "tg_offset": 0, "pending": {},
             "custom_topics": {}}
    if not os.path.exists(STATE_FILE):
        return state
    with open(STATE_FILE, encoding="utf-8") as f:
        raw = f.read().strip()
    try:
        plain = _fernet().decrypt(raw.encode("utf-8"))
    except SystemExit:
        raise
    except Exception as ex:
        raise SystemExit(f"cannot decrypt {STATE_FILE} (wrong FOLLOW_BOT_TOKEN?): {type(ex).__name__}")
    data = json.loads(plain.decode("utf-8"))
    if isinstance(data, dict):
        state.update(data)
    return state


def save_state(state):
    """حافظه رو رمزنگاری‌شده توی followers.json می‌نویسه. بدون توکن، اصلاً فایل نمی‌سازه."""
    state["seen_posts"] = state.get("seen_posts", [])[-500:]
    plain = json.dumps(state, ensure_ascii=False).encode("utf-8")
    token = _fernet().encrypt(plain).decode("ascii")
    with open(STATE_FILE, "w", encoding="utf-8") as f:
        f.write(token + "\n")


# ====== تلگرام ======
def api(method, **data):
    url = f"https://api.telegram.org/bot{TOKEN}/{method}"
    try:
        r = requests.post(url, data=data, timeout=30)
        if r.status_code == 429:   # محدودیت سرعت تلگرام: صبر و یک بار تلاش دوباره
            try:
                wait = int(r.json().get("parameters", {}).get("retry_after", 5))
            except Exception:
                wait = 5
            time.sleep(min(wait, 30))
            r = requests.post(url, data=data, timeout=30)
    except requests.RequestException as ex:   # قطعی/کندیِ شبکه نباید کل اجرا رو خراب کنه
        print("telegram network error:", method, type(ex).__name__)
        return None
    if not r.ok:
        print("telegram error:", method, r.status_code, r.text[:150])
        return None
    return r.json().get("result")


def api_get_updates(offset):
    # long polling: تلگرام تا ۳۰ ثانیه صبر می‌کنه و به‌محض رسیدن پیام جواب می‌ده،
    # پس دستورها تقریباً در لحظه (حدود ۱ ثانیه) جواب داده می‌شن، بدون اینکه
    # مدام درخواست بزنیم. مهلت درخواست باید بیشتر از long-poll باشه.
    try:
        r = requests.get(f"https://api.telegram.org/bot{TOKEN}/getUpdates",
                         params={"offset": offset, "timeout": 30,
                                 "allowed_updates": json.dumps(["message", "channel_post"])},
                         timeout=40)
        data = r.json()
    except Exception as ex:
        print("getUpdates error:", type(ex).__name__)
        return []
    if not data.get("ok"):
        print("getUpdates failed:", str(data)[:120])
        return []
    return data.get("result", [])


def send_message(chat_id, text, reply_markup=None):
    data = {"chat_id": chat_id, "text": text}
    if reply_markup:
        data["reply_markup"] = json.dumps(reply_markup, ensure_ascii=False)
    return api("sendMessage", **data)


def forward_to(chat_id, post_id):
    return api("forwardMessage", chat_id=chat_id, from_chat_id=CHANNEL, message_id=post_id)


def forward_message_to(chat_id, from_chat_id, message_id):
    """یه پیام عضو رو (متن/عکس/فیلم) همون‌طور که هست فوروارد می‌کنه."""
    return api("forwardMessage", chat_id=chat_id, from_chat_id=from_chat_id,
               message_id=message_id)


# ====== عضویت کانال (دروازه‌ی ربات) ======
JOIN_TEXT = "برای استفاده از ربات خبری، اول باید عضو کانال GodNews24 بشی 🙂"


def join_keyboard():
    return {"inline_keyboard": [[{"text": "📢 عضویت در کانال", "url": "https://t.me/GodNews24"}]]}


def _check_membership(chat_id):
    """عضویت کاربر توی کانال رو برمی‌گردونه (True = عضو). نتیجه ۱۰ دقیقه کش می‌شه.
    اگه خودِ فراخوانی getChatMember خطا بده (نه جواب left/kicked)، این بار اجازه می‌دیم
    و خطا رو لاگ می‌کنیم — fail open روی خطا، fail closed روی left/kicked."""
    now = time.time()
    cached = _MEMBER_CACHE.get(chat_id)
    if cached and now - cached[1] < MEMBER_CACHE_TTL:
        return cached[0]
    try:
        res = api("getChatMember", chat_id=CHANNEL, user_id=chat_id)
    except Exception as ex:
        print("getChatMember error:", type(ex).__name__)
        return True
    if res is None:
        print("getChatMember error: no result")
        return True
    allowed = (res or {}).get("status", "") in MEMBER_ALLOWED
    _MEMBER_CACHE[chat_id] = (allowed, now)
    return allowed


# ====== کیبوردها ======
ADMIN_BUTTONS = [
    [{"text": "👥 دنبال‌کننده‌ها"}, {"text": "📣 پیام به اعضا"}],
    [{"text": "➕ موضوع جدید"}, {"text": "➖ حذف موضوع"}],
]


def main_keyboard(chat_id=None):
    """کیبورد اصلی؛ برای ادمین دو ردیف دکمه‌ی ادمین هم اضافه می‌شه،
    بقیه‌ی اعضا دقیقاً همون منوی قبلی رو می‌بینن."""
    rows = [[{"text": TOPICS["political"]}, {"text": TOPICS["sport"]}],
            [{"text": TOPICS["gold"]}, {"text": "📋 موضوع‌های من"}]]
    if chat_id is not None and ADMIN_CHAT_ID and str(chat_id) == ADMIN_CHAT_ID:
        rows = rows + [list(r) for r in ADMIN_BUTTONS]
    rows.append([{"text": "❓ راهنما"}])
    return {"keyboard": rows, "resize_keyboard": True}


def topic_keyboard(state=None):
    rows = [[{"text": TOPICS["political"]}, {"text": TOPICS["sport"]}],
            [{"text": TOPICS["gold"]}]]
    customs = list((state or {}).get("custom_topics", {}).values())
    for i in range(0, len(customs), 2):
        rows.append([{"text": CUSTOM_TOPIC_EMOJI + " " + c.get("name", "")}
                     for c in customs[i:i + 2] if c.get("name")])
    rows.append([{"text": "❌ لغو"}])
    return {"keyboard": rows, "resize_keyboard": True}


def unfollow_keyboard(followed, state=None):
    customs = (state or {}).get("custom_topics", {})
    rows = [[{"text": topic_label(state, k)}] for k in followed
            if k in TOPICS or k in customs]
    rows.append([{"text": "همه"}, {"text": "❌ لغو"}])
    return {"keyboard": rows, "resize_keyboard": True}


def topic_label(state, key):
    """برچسب فارسی یه موضوع (اصلی یا سفارشی)؛ اگه ناشناخته بود خود کلید."""
    if key in TOPICS:
        return TOPICS[key]
    ct = (state or {}).get("custom_topics", {}).get(key)
    if ct:
        return CUSTOM_TOPIC_EMOJI + " " + (ct.get("name") or key)
    return key


# ====== دسته‌بندی پست کانال ======
def leading_emoji(text):
    """ایموجی اول متن پست (همون ایموجی‌ای که ربات اصلی اول خبر می‌ذاره)."""
    t = (text or "").lstrip()
    for e in ("🔴", "🚨", "🔥", "⚽", "🟡"):
        if t.startswith(e):
            return e
    return ""


def classify_post(text, custom_topics=None):
    """موضوع‌های یه پست کانال رو برمی‌گردونه (مجموعه‌ای از کلیدهای موضوع).
    فقط پست‌های مهم/موضوع‌دار برمی‌گردن؛ پست معمولی (🔵) هیچ موضوعی نمی‌گیره.
    موضوع‌های سفارشی با کلمه‌های کلیدی‌شون توی همون تیتر + اول متن چک می‌شن."""
    t = text or ""
    head = t[:600]   # تیتر + اول متن، برای کلمه‌های کلیدی کافیه
    lead = leading_emoji(t)
    out = set()
    if lead in ("🔴", "🚨", "🔥") or any(k in head for k in POLITICS_KW) or any(k in head for k in FLASH_KW):
        out.add("political")
    if lead == "⚽" or any(k in head for k in SPORT_KW):
        out.add("sport")
    if lead == "🟡" and any(k in head for k in GOLD_KW):
        out.add("gold")
    for key, ct in (custom_topics or {}).items():
        if any(kw and kw in head for kw in ct.get("keywords", [])):
            out.add(key)
    return out


def _norm_topic(raw, custom_topics=None):
    """«🔴 سیاسی» یا «سیاسی» یا «political» یا نام موضوع سفارشی → کلید داخلی؛ وگرنه None."""
    s = (raw or "").strip()
    s = re.sub(r"^[\U0001F300-\U0001FAFF\u2600-\u27BF\uFE0F\s]+", "", s).strip()
    if s in TOPIC_NAMES:
        return TOPIC_NAMES[s]
    if s in TOPICS:
        return s
    if s in (custom_topics or {}):
        return s
    return None


# ====== دستورهای اعضا ======
WELCOME_TEXT = (
    "سلام! 👋 من ربات خبررسان GodNews24 هستم.\n\n"
    "موضوع‌هایی که دوست داری رو انتخاب کن تا خبرهای مهم همون موضوع رو دایرکت برات بفرستم:\n"
    "🔴 سیاسی\n"
    "⚽ ورزشی\n"
    "🟡 طلا و دلار\n\n"
    "برای دنبال کردن، روی یه موضوع بزن یا /follow رو بفرست."
)

HELP_TEXT = (
    "راهنمای ربات خبررسان GodNews24:\n\n"
    "/follow — یه موضوع رو دنبال کن (سیاسی، ورزشی، طلا و دلار)\n"
    "/unfollow — یه موضوع رو لغو کن، یا همه رو با «همه»\n"
    "/topics — موضوع‌هایی که دنبال می‌کنی\n"
    "/send — خبر، عکس یا فیلم بفرست تا برای ادمین بره\n"
    "/help — همین راهنما\n\n"
    "خبرهای مهم موضوع‌هایی که دنبال می‌کنی، دایرکت برات میاد؛ "
    "بقیه‌ی خبرها فقط توی کانال @GodNews24 هستن.\n\n"
    "برای استفاده از ربات، باید عضو کانال @GodNews24 باشی 🙂"
)

ADMIN_HELP_EXTRA = (
    "\n\nدستورهای ادمین:\n"
    "/followers — آمار و لیست دنبال‌کننده‌ها\n"
    "/addtopic — اضافه کردن موضوع جدید: /addtopic <نام> <کلمه‌ی کلیدی> ...\n"
    "/removetopic — حذف یه موضوع سفارشی: /removetopic <نام>\n"
    "/broadcast — پیام به دنبال‌کننده‌ها: /broadcast <متن> (به همه) یا /broadcast <ایموجی موضوع> <متن> (فقط همون موضوع)؛ مثلاً /broadcast 🔴 سیاسی سلام"
)


def welcome_text(is_admin):
    """متن خوش‌آمد؛ نسخه‌ی ربات فقط برای ادمین نشون داده می‌شه."""
    return WELCOME_TEXT + f"\n\nنسخه‌ی ربات: {BOT_VERSION}" if is_admin else WELCOME_TEXT


def help_text(is_admin):
    """راهنما برای اعضا؛ دستورهای ادمین فقط برای ادمین نشون داده می‌شن."""
    return HELP_TEXT + ADMIN_HELP_EXTRA if is_admin else HELP_TEXT


def _record_sender(state, chat_id, frm):
    """نام و یوزرنیم فرستنده رو از update ذخیره می‌کنه تا لیست /followers اسم داشته باشه."""
    users = state.setdefault("users", {})
    u = users.setdefault(chat_id, {"topics": []})
    first = (frm.get("first_name") or "").strip()
    last = (frm.get("last_name") or "").strip()
    name = " ".join(x for x in (first, last) if x)
    if name:
        u["name"] = name
    uname = (frm.get("username") or "").strip()
    if uname:
        u["username"] = uname


def _sender_display(msg):
    """«علی (@ali123)» یا «علی» یا «@ali123» یا «کاربر» — برای خط خبر ارسالی."""
    frm = msg.get("from") or {}
    first = (frm.get("first_name") or "").strip()
    last = (frm.get("last_name") or "").strip()
    name = " ".join(x for x in (first, last) if x)
    uname = (frm.get("username") or "").strip()
    if name and uname:
        return f"{name} (@{uname})"
    if uname:
        return f"@{uname}"
    return name or "کاربر"


def _broadcast_target(arg, custom_topics=None):
    """اگه متن با «ایموجی + نام موضوع» شروع بشه، (کلید موضوع، بقیه‌ی متن) رو
    برمی‌گردونه؛ وگرنه (None, کل متن) — یعنی پیام همگانی. اسم خالی موضوع
    به‌تنهایی انتخاب‌کننده نیست، پس پیامتی که با «سیاسی ...» شروع بشه به همه
    می‌رسه، نه فقط به سیاسی‌ها. فاصله‌ی بین ایموجی و نام مهم نیست."""
    s = (arg or "").strip()
    labels = [(k, v) for k, v in TOPICS.items()]
    for k, ct in (custom_topics or {}).items():
        labels.append((k, CUSTOM_TOPIC_EMOJI + " " + (ct.get("name") or k)))
    for key, label in labels:
        for cand in (label, label.replace(" ", "")):
            if s == cand:
                return key, ""
            if s.startswith(cand + " "):
                return key, s[len(cand) + 1:]
    return None, s


def _broadcast(state, text, topic=None):
    """متن رو برای دنبال‌کننده‌ها می‌فرسته (همه، یا فقط یه موضوع).
    برمی‌گردونه (تعداد موفق، تعداد ناموفق)؛ ناموفق فقط وقتیه که فراخوانی تلگرام
    خطا بده یا هیچ جوابی نده."""
    targets = []
    for uid, u in state.get("users", {}).items():
        ts = u.get("topics", [])
        if not ts:
            continue
        if topic is None or topic in ts:
            targets.append(uid)
    sent = failed = 0
    for uid in sorted(targets):
        try:
            res = send_message(uid, text)
        except Exception:
            res = None
        if res is None:
            failed += 1
        else:
            sent += 1
        time.sleep(0.05)   # که به سقف سرعت تلگرام نخوریم
    return sent, failed


def _submit_news(state, msg, chat_id, direct_text=None):
    """خبر/عکس/فیلم یه عضو رو برای ادمین می‌فرسته: اول یه خط معرفی، بعد خود پیام
    با forwardMessage تا مدیا حفظ بشه (اگه متن مستقیم داده شده باشه، همون متن
    فرستاده می‌شه). حداکثر ۱ ارسال هر ۶۰ ثانیه برای هر عضو. پیام‌های خود ادمین
    هیچ‌وقت برای خودش فوروارد نمی‌شن."""
    if chat_id == ADMIN_CHAT_ID:
        send_message(chat_id, "این دستور رو نمی‌شناسم. /help رو بزن تا راهنما رو ببینی.",
                     reply_markup=main_keyboard(chat_id))
        return True
    if not ADMIN_CHAT_ID:
        send_message(chat_id, "ارسال خبر فعلاً ممکن نیست.", reply_markup=main_keyboard(chat_id))
        return True
    now = time.time()
    last = _SUBMIT_LAST.get(chat_id, 0)
    if now - last < SUBMIT_COOLDOWN_SEC:
        send_message(chat_id, "لطفاً کمی صبر کن و بعد دوباره بفرست 🙂",
                     reply_markup=main_keyboard(chat_id))
        return True
    _SUBMIT_LAST[chat_id] = now
    send_message(ADMIN_CHAT_ID, f"📩 خبر ارسالی از {_sender_display(msg)}")
    if direct_text is not None:
        send_message(ADMIN_CHAT_ID, direct_text)
    else:
        forward_message_to(ADMIN_CHAT_ID, chat_id, msg.get("message_id"))
    send_message(chat_id, "خبرت رسید و برای ادمین فرستاده شد ✅",
                 reply_markup=main_keyboard(chat_id))
    return True


def _add_custom_topic(state, name, keywords):
    """موضوع سفارشی اضافه می‌کنه؛ برمی‌گردونه (موفق؟، پیام)."""
    if not name or not keywords:
        return False, ("برای اضافه کردن موضوع:\n/addtopic <نام موضوع> <کلمه‌ی کلیدی ۱> [کلمه‌ی کلیدی ۲ ...]\nمثلاً: /addtopic سینما فیلم سینما بازیگر")
    customs = state.setdefault("custom_topics", {})
    if name in TOPIC_NAMES or name in TOPICS:
        return False, f"«{name}» موضوع اصلیه و نمی‌شه دوباره اضافه‌ش کرد."
    if name in customs:
        return False, f"موضوع «{name}» از قبل هست."
    customs[name] = {"name": name, "keywords": list(keywords)}
    return True, f"موضوع «{name}» اضافه شد ✅\nکلمه‌های کلیدی: {', '.join(keywords)}"


def _remove_custom_topic(state, name):
    """موضوع سفارشی حذف می‌کنه و از لیست همه‌ی دنبال‌کننده‌ها درش می‌آره؛
    برمی‌گردونه (موفق؟، پیام)."""
    if not name:
        return False, "برای حذف موضوع:\n/removetopic <نام موضوع>"
    if name in TOPIC_NAMES or name in TOPICS:
        return False, "موضوع‌های اصلی رو نمی‌شه حذف کرد."
    customs = state.setdefault("custom_topics", {})
    if name not in customs:
        return False, "چنین موضوعی نداریم."
    del customs[name]
    for u in state.get("users", {}).values():
        if name in u.get("topics", []):
            u["topics"].remove(name)
    return True, f"موضوع «{name}» حذف شد."


def _send_followers(state, chat_id):
    """آمار + لیست دنبال‌کننده‌ها رو برای ادمین می‌فرسته (فقط ادمین)."""
    if chat_id != ADMIN_CHAT_ID:
        send_message(chat_id, "این دستور فقط برای ادمینه.", reply_markup=main_keyboard(chat_id))
        return True
    keys = list(TOPICS) + list(state.get("custom_topics", {}))
    counts = {k: 0 for k in keys}
    total = 0
    listed = []
    for uid, u in state.get("users", {}).items():
        ts = [k for k in u.get("topics", []) if k in counts]
        if not ts:
            continue
        total += 1
        for k in ts:
            counts[k] += 1
        name = (u.get("name") or "").strip() or uid
        uname = (u.get("username") or "").strip()
        who = f"{name} (@{uname})" if uname else name
        tstr = "، ".join(topic_label(state, k) for k in ts)
        listed.append((name.lower(), f"• {who} — {tstr}"))
    lines = "\n".join(f"{topic_label(state, k)}: {counts[k]}" for k in keys)
    out = f"دنبال‌کننده‌ها (کل: {total} نفر):\n{lines}"
    if listed:
        listed.sort(key=lambda row: row[0])
        out += "\n\n" + "\n".join(row[1] for row in listed[:30])
        if len(listed) > 30:
            out += f"\nو {len(listed) - 30} نفر دیگر"
    send_message(chat_id, out, reply_markup=main_keyboard(chat_id))
    return True


def _follow(state, chat_id, topic):
    users = state.setdefault("users", {})
    u = users.setdefault(chat_id, {"topics": []})
    if topic in u["topics"]:
        return False
    u["topics"].append(topic)
    return True


def _unfollow(state, chat_id, topic):
    users = state.get("users", {})
    u = users.get(chat_id)
    if not u or topic not in u.get("topics", []):
        return False
    u["topics"].remove(topic)
    return True


def handle_message(state, msg):
    """یه پیام خصوصی عضو رو جواب می‌ده. True یعنی حافظه تغییر کرده."""
    chat = msg.get("chat") or {}
    chat_id = str(chat.get("id"))
    if not chat_id:
        return False
    text = (msg.get("text") or "").strip()
    has_media = "photo" in msg or "video" in msg
    if not text and not has_media:
        return False
    # دروازه‌ی عضویت: فقط اعضای کانال می‌تونن از ربات استفاده کنن
    if not _check_membership(chat_id):
        send_message(chat_id, JOIN_TEXT, reply_markup=join_keyboard())
        return False
    _record_sender(state, chat_id, msg.get("from") or {})
    pending = state.setdefault("pending", {})
    entry = pending.get(chat_id)
    if entry and time.time() - float(entry.get("ts") or 0) > PENDING_TIMEOUT:
        pending.pop(chat_id, None)
        entry = None

    if text in ("لغو", "❌ لغو"):
        if entry:
            pending.pop(chat_id, None)
            send_message(chat_id, "لغو شد.", reply_markup=main_keyboard(chat_id))
        return True

    if text == "/start":
        pending.pop(chat_id, None)
        send_message(chat_id, welcome_text(chat_id == ADMIN_CHAT_ID), reply_markup=main_keyboard(chat_id))
        return True
    if text == "/help" or text == "❓ راهنما":
        pending.pop(chat_id, None)
        send_message(chat_id, help_text(chat_id == ADMIN_CHAT_ID),
                     reply_markup=main_keyboard(chat_id))
        return True
    if text == "/topics" or text == "📋 موضوع‌های من":
        pending.pop(chat_id, None)
        topics = state.get("users", {}).get(chat_id, {}).get("topics", [])
        customs = state.get("custom_topics", {})
        shown = [k for k in topics if k in TOPICS or k in customs]
        if not shown:
            send_message(chat_id, "هنوز هیچ موضوعی رو دنبال نمی‌کنی.\nبا /follow یه موضوع انتخاب کن 🙂",
                         reply_markup=main_keyboard(chat_id))
        else:
            lines = "\n".join("• " + topic_label(state, k) for k in shown)
            send_message(chat_id, "موضوع‌هایی که دنبال می‌کنی:\n" + lines,
                         reply_markup=main_keyboard(chat_id))
        return True
    if text == "/followers":
        return _send_followers(state, chat_id)
    if text == "👥 دنبال‌کننده‌ها":
        return _send_followers(state, chat_id)
    if text == "📣 پیام به اعضا":
        if chat_id != ADMIN_CHAT_ID:
            send_message(chat_id, "این دستور فقط برای ادمینه.", reply_markup=main_keyboard(chat_id))
            return True
        pending[chat_id] = {"cmd": "/broadcast", "ts": time.time()}
        send_message(chat_id, "متن پیام رو بفرست:", reply_markup=main_keyboard(chat_id))
        return True
    if text == "➕ موضوع جدید":
        if chat_id != ADMIN_CHAT_ID:
            send_message(chat_id, "این دستور فقط برای ادمینه.", reply_markup=main_keyboard(chat_id))
            return True
        pending[chat_id] = {"cmd": "/addtopic", "ts": time.time()}
        send_message(chat_id, "نام موضوع و کلمه‌های کلیدی رو بفرست",
                     reply_markup=main_keyboard(chat_id))
        return True
    if text == "➖ حذف موضوع":
        if chat_id != ADMIN_CHAT_ID:
            send_message(chat_id, "این دستور فقط برای ادمینه.", reply_markup=main_keyboard(chat_id))
            return True
        pending[chat_id] = {"cmd": "/removetopic", "ts": time.time()}
        send_message(chat_id, "نام موضوعی که می‌خوای حذف بشه رو بفرست:",
                     reply_markup=main_keyboard(chat_id))
        return True
    if text == "/addtopic" or text.startswith("/addtopic "):
        if chat_id != ADMIN_CHAT_ID:
            send_message(chat_id, "این دستور فقط برای ادمینه.", reply_markup=main_keyboard(chat_id))
            return True
        parts = text.split()
        ok, reply = _add_custom_topic(state, parts[1] if len(parts) > 1 else "",
                                      parts[2:])
        send_message(chat_id, reply, reply_markup=main_keyboard(chat_id))
        return True
    if text == "/removetopic" or text.startswith("/removetopic "):
        if chat_id != ADMIN_CHAT_ID:
            send_message(chat_id, "این دستور فقط برای ادمینه.", reply_markup=main_keyboard(chat_id))
            return True
        parts = text.split(None, 1)
        ok, reply = _remove_custom_topic(state, parts[1].strip() if len(parts) > 1 else "")
        send_message(chat_id, reply, reply_markup=main_keyboard(chat_id))
        return True
    if text == "/broadcast" or text.startswith("/broadcast "):
        if chat_id != ADMIN_CHAT_ID:
            send_message(chat_id, "این دستور فقط برای ادمینه.", reply_markup=main_keyboard(chat_id))
            return True
        if text == "/broadcast":
            pending[chat_id] = {"cmd": "/broadcast", "ts": time.time()}
            send_message(chat_id, "متن پیام رو بفرست:", reply_markup=main_keyboard(chat_id))
            return True
        arg = text.split(None, 1)[1].strip()
        topic, msg = _broadcast_target(arg, state.get("custom_topics", {}))
        if topic and not msg:
            send_message(chat_id, "متن پیام رو هم بنویس:\n/broadcast <ایموجی موضوع> <متن>",
                         reply_markup=main_keyboard(chat_id))
            return True
        if topic:
            sent, failed = _broadcast(state, msg, topic)
        else:
            sent, failed = _broadcast(state, arg)
        send_message(chat_id, f"به {sent} نفر رسید، {failed} نفر ناموفق بود.",
                     reply_markup=main_keyboard(chat_id))
        return True
    if text == "/send" or text.startswith("/send "):
        if text == "/send":
            pending[chat_id] = {"cmd": "/send", "ts": time.time()}
            send_message(chat_id, "خبر، عکس یا فیلمت رو بفرست 🙂", reply_markup=main_keyboard(chat_id))
            return True
        arg = text.split(None, 1)[1].strip()
        if not arg:
            pending[chat_id] = {"cmd": "/send", "ts": time.time()}
            send_message(chat_id, "خبر، عکس یا فیلمت رو بفرست 🙂", reply_markup=main_keyboard(chat_id))
            return True
        return _submit_news(state, msg, chat_id, direct_text=arg)
    if text == "/follow":
        pending[chat_id] = {"cmd": "/follow", "ts": time.time()}
        send_message(chat_id, "کدوم موضوع رو می‌خوای دنبال کنی؟", reply_markup=topic_keyboard(state))
        return True
    if text.startswith("/follow "):
        topic = _norm_topic(text.split(None, 1)[1], state.get("custom_topics", {}))
        if not topic:
            send_message(chat_id, "این موضوع رو نمی‌شناسم. موضوع‌ها: سیاسی، ورزشی، طلا و دلار",
                         reply_markup=main_keyboard(chat_id))
            return True
        if _follow(state, chat_id, topic):
            send_message(chat_id, f"دنبال کردن «{topic_label(state, topic)}» فعال شد ✅\nخبرهای مهمش دایرکت برات میاد.",
                         reply_markup=main_keyboard(chat_id))
        else:
            send_message(chat_id, f"قبلاً «{topic_label(state, topic)}» رو دنبال می‌کردی 🙂",
                         reply_markup=main_keyboard(chat_id))
        return True
    if text == "/unfollow":
        topics = state.get("users", {}).get(chat_id, {}).get("topics", [])
        if not topics:
            send_message(chat_id, "هیچ موضوعی رو دنبال نمی‌کنی که لغوش کنی 🙂",
                         reply_markup=main_keyboard(chat_id))
            return True
        pending[chat_id] = {"cmd": "/unfollow", "ts": time.time()}
        send_message(chat_id, "کدوم موضوع رو لغو کنم؟", reply_markup=unfollow_keyboard(topics, state))
        return True
    if text.startswith("/unfollow "):
        arg = text.split(None, 1)[1].strip()
        if arg == "همه":
            state.get("users", {}).get(chat_id, {})["topics"] = []
            send_message(chat_id, "دنبال کردن همه‌ی موضوع‌ها لغو شد.", reply_markup=main_keyboard(chat_id))
            return True
        topic = _norm_topic(arg, state.get("custom_topics", {}))
        if not topic:
            send_message(chat_id, "این موضوع رو نمی‌شناسم.", reply_markup=main_keyboard(chat_id))
            return True
        if _unfollow(state, chat_id, topic):
            send_message(chat_id, f"دنبال کردن «{topic_label(state, topic)}» لغو شد.", reply_markup=main_keyboard(chat_id))
        else:
            send_message(chat_id, f"«{topic_label(state, topic)}» رو دنبال نمی‌کردی.", reply_markup=main_keyboard(chat_id))
        return True
    if entry:
        # جوابِ دستوری که منتظر آرگومان بود (/follow، /unfollow، /broadcast یا /send)
        cmd = entry["cmd"]
        pending.pop(chat_id, None)
        if cmd == "/follow":
            topic = _norm_topic(text, state.get("custom_topics", {}))
            if not topic:
                send_message(chat_id, "این موضوع رو نمی‌شناسم. موضوع‌ها: سیاسی، ورزشی، طلا و دلار",
                             reply_markup=main_keyboard(chat_id))
                return True
            if _follow(state, chat_id, topic):
                send_message(chat_id, f"دنبال کردن «{topic_label(state, topic)}» فعال شد ✅\nخبرهای مهمش دایرکت برات میاد.",
                             reply_markup=main_keyboard(chat_id))
            else:
                send_message(chat_id, f"قبلاً «{topic_label(state, topic)}» رو دنبال می‌کردی 🙂",
                             reply_markup=main_keyboard(chat_id))
            return True
        if cmd == "/unfollow":
            if text == "همه":
                state.get("users", {}).get(chat_id, {})["topics"] = []
                send_message(chat_id, "دنبال کردن همه‌ی موضوع‌ها لغو شد.", reply_markup=main_keyboard(chat_id))
                return True
            topic = _norm_topic(text, state.get("custom_topics", {}))
            if not topic:
                send_message(chat_id, "این موضوع رو نمی‌شناسم.", reply_markup=main_keyboard(chat_id))
                return True
            if _unfollow(state, chat_id, topic):
                send_message(chat_id, f"دنبال کردن «{topic_label(state, topic)}» لغو شد.", reply_markup=main_keyboard(chat_id))
            else:
                send_message(chat_id, f"«{topic_label(state, topic)}» رو دنبال نمی‌کردی.", reply_markup=main_keyboard(chat_id))
            return True
        if cmd == "/broadcast":
            if not text:
                pending[chat_id] = {"cmd": "/broadcast", "ts": time.time()}
                send_message(chat_id, "متن پیام رو بفرست:", reply_markup=main_keyboard(chat_id))
                return True
            topic, msg = _broadcast_target(text, state.get("custom_topics", {}))
            if topic and not msg:
                send_message(chat_id, "متن پیام رو هم بنویس:\n/broadcast <ایموجی موضوع> <متن>",
                             reply_markup=main_keyboard(chat_id))
                return True
            if topic:
                sent, failed = _broadcast(state, msg, topic)
            else:
                sent, failed = _broadcast(state, text)
            send_message(chat_id, f"به {sent} نفر رسید، {failed} نفر ناموفق بود.",
                         reply_markup=main_keyboard(chat_id))
            return True
        if cmd == "/send":
            return _submit_news(state, msg, chat_id)
        if cmd == "/addtopic":
            parts = text.split()
            ok, reply = _add_custom_topic(state, parts[0] if parts else "", parts[1:])
            send_message(chat_id, reply, reply_markup=main_keyboard(chat_id))
            return True
        if cmd == "/removetopic":
            ok, reply = _remove_custom_topic(state, text.strip())
            send_message(chat_id, reply, reply_markup=main_keyboard(chat_id))
            return True
    # زدن دکمه‌ی موضوع روی کیبورد اصلی = دنبال کردن همون موضوع
    topic = _norm_topic(text, state.get("custom_topics", {}))
    if topic:
        if _follow(state, chat_id, topic):
            send_message(chat_id, f"دنبال کردن «{topic_label(state, topic)}» فعال شد ✅\nخبرهای مهمش دایرکت برات میاد.",
                         reply_markup=main_keyboard(chat_id))
        else:
            send_message(chat_id, f"قبلاً «{topic_label(state, topic)}» رو دنبال می‌کردی 🙂",
                         reply_markup=main_keyboard(chat_id))
        return True
    if text.startswith("/"):
        send_message(chat_id, "این دستور رو نمی‌شناسم. /help رو بزن تا راهنما رو ببینی.",
                     reply_markup=main_keyboard(chat_id))
        return True
    # هر پیام دیگه (متن، عکس، فیلم) = ارسال خبر برای ادمین
    return _submit_news(state, msg, chat_id)


# ====== پست‌های کانال ======
def handle_channel_post(state, post):
    """یه پست کانال رو دسته‌بندی می‌کنه و برای دنبال‌کننده‌های موضوعش فوروارد می‌کنه.
    True یعنی حافظه تغییر کرده. آیدی دنبال‌کننده‌ها هیچ‌وقت لاگ نمی‌شه."""
    chat = post.get("chat") or {}
    username = (chat.get("username") or "").lstrip("@")
    if username and username != CHANNEL.lstrip("@"):
        return False
    post_id = post.get("message_id")
    if not post_id:
        return False
    seen = state.setdefault("seen_posts", [])
    if post_id in seen:
        return False
    text = post.get("text") or post.get("caption") or ""
    topics = classify_post(text, state.get("custom_topics", {}))
    seen.append(post_id)
    if not topics:
        return True   # پست معمولی: نه فوروارد می‌شه، نه دوباره بررسی می‌شه
    targets = set()
    for uid, u in state.get("users", {}).items():
        if set(u.get("topics", [])) & topics:
            targets.add(uid)
    sent = 0
    for uid in sorted(targets):
        if forward_to(uid, post_id):
            sent += 1
        time.sleep(0.05)   # که به سقف سرعت تلگرام نخوریم
    print(f"forwarded post {post_id} topics={sorted(topics)} to {sent} follower(s)")
    return True


def process_updates(state):
    """getUpdates رو می‌خونه و پیام‌های اعضا + پست‌های کانال رو پردازش می‌کنه."""
    updates = api_get_updates(state.get("tg_offset", 0))
    changed = False
    for u in updates:
        state["tg_offset"] = u["update_id"] + 1
        changed = True
        if "message" in u:
            if handle_message(state, u["message"]):
                changed = True
        elif "channel_post" in u:
            if handle_channel_post(state, u["channel_post"]):
                changed = True
    return changed


def run_once():
    state = load_state()
    if process_updates(state):
        save_state(state)
    return state


# ====== گیت‌هاب: کامیت حافظه + صدا زدن اجرای بعدی ======
def commit_state():
    """حافظه‌ی رمزنگاری‌شده رو توی گیت‌هاب کامیت می‌کنه (فقط وقتی توی Actions هستیم)."""
    if not os.environ.get("GITHUB_ACTIONS"):
        return
    def git(*args):
        return subprocess.run(["git", *args], capture_output=True, text=True)
    try:
        git("config", "user.name", "news-bot")
        git("config", "user.email", "bot@users.noreply.github.com")
        _pull = git("pull", "--rebase", "--autostash")
        if _pull.returncode != 0:
            print("git pull --rebase failed (ignored):", (_pull.stderr or "")[:200])
        git("add", STATE_FILE)
        if git("diff", "--cached", "--quiet").returncode != 0:
            git("commit", "-m", "update followers")
            r = git("push")
            if r.returncode != 0:
                print("git push failed:", r.stderr[:200])
    except Exception as ex:
        print("commit error:", repr(ex))


def dispatch_next():
    """اجرای بعدی رو خودش صدا می‌زنه (به زمان‌بندی دیرکردیِ گیت‌هاب وابسته نمی‌مونیم)."""
    repo, tok = os.environ.get("GITHUB_REPOSITORY"), os.environ.get("GITHUB_TOKEN")
    if not (os.environ.get("GITHUB_ACTIONS") and repo and tok):
        return
    ref = os.environ.get("GITHUB_REF_NAME", "main")
    wf = os.environ.get("GITHUB_WORKFLOW_REF", "").split("@")[0].rsplit("/", 1)[-1] or "follow.yml"
    try:
        r = requests.post(f"https://api.github.com/repos/{repo}/actions/workflows/{wf}/dispatches",
                          headers={"Authorization": f"Bearer {tok}", "Accept": "application/vnd.github+json"},
                          json={"ref": ref, "inputs": {"mode": "auto"}}, timeout=20)
        print("اجرای بعدی صدا زده شد:", r.status_code)
    except Exception as ex:
        print("dispatch error:", repr(ex))


def main():
    print(f"نسخه‌ی ربات دنبال‌کننده: {BOT_VERSION}")
    if not TOKEN:
        print("FOLLOW_BOT_TOKEN is not set — refusing to run (state must never be plaintext)")
        raise SystemExit(1)
    loop_minutes = LOOP_MINUTES
    end = time.time() + loop_minutes * 60 if loop_minutes > 0 else 0
    last_commit = 0.0
    while True:
        try:
            run_once()
        except SystemExit:
            raise
        except Exception as ex:
            print("خطا در این دور:", repr(ex))
        if time.time() - last_commit >= COMMIT_EVERY_SEC:
            commit_state()
            last_commit = time.time()
        if loop_minutes <= 0:
            break
        if time.time() >= end:
            break
        time.sleep(POLL_SECONDS)
    commit_state()
    if loop_minutes > 0:
        dispatch_next()


if __name__ == "__main__":
    main()
