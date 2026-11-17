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
from datetime import datetime, timedelta, timezone

import requests

BOT_VERSION = "2026-11-16-0.18"   # فقط توی لاگ و برای ادمین در /start نوشته می‌شه

TOKEN = os.environ.get("FOLLOW_BOT_TOKEN") or ""
ADMIN_CHAT_ID = os.environ.get("ADMIN_CHAT_ID") or ""
CHANNEL = os.environ.get("CHANNEL") or "@GodNews24"
LOOP_MINUTES = int(os.environ.get("LOOP_MINUTES") or 0)

STATE_FILE = "followers.json"
POLL_SECONDS = 2             # فاصله‌ی کوتاه بین دو دور؛ انتظار واقعی رو long polling انجام می‌ده
PENDING_TIMEOUT = 600        # ثانیه؛ اگه عضو تا این مدت جواب نده، انتظار لغو می‌شه
COMMIT_EVERY_SEC = 300       # حافظه حداکثر هر ۵ دقیقه توی گیت‌هاب کامیت می‌شه
MEMBER_CACHE_TTL = 600       # ثانیه؛ نتیجه‌ی عضویت کانال ۱۰ دقیقه کش می‌شه
SWEEP_INTERVAL_SEC = 24 * 3600  # هر چند وقت یک بار عضویت دنبال‌کننده‌ها چک بشه (روزی یک بار)
SWEEP_WARN_TEXT = (
    "سلام! 👋 تو دیگه عضو کانال GodNews24 نیستی، برای همین خبرهای مهم دیگه برات نمیاد.\n"
    "اگه می‌خوای دوباره خبرها رو بگیری، دوباره عضو کانال شو 🙂"
)
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
# ایموجی ثابت هر موضوع اصلی (برای وقتی اسمش با /renametopic عوض می‌شه)
TOPIC_EMOJI = {
    "political": "🔴",
    "sport": "⚽",
    "gold": "🟡",
}


def _builtin_label(key, override=None):
    """برچسب یه موضوع اصلی با اسم دلخواه؛ اگه اسم جدید با ایموجی شروع بشه،
    همون برچسبه، وگرنه ایموجی ثابت موضوع جلوش می‌آد."""
    if override:
        if re.match(r"^[\U0001F300-\U0001FAFF\u2600-\u27BF\u2190-\u21FF\uFE0F]", override):
            return override
        return TOPIC_EMOJI[key] + " " + override
    return TOPICS[key]
# سنجاق فقط برای انتخاب موضوع سفارشیِ بدون ایموجی توی متن /broadcast تایپ می‌شه
# (مثلاً «📌 سینما»)؛ توی هیچ دکمه یا لیستی نشون داده نمی‌شه.
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
             "custom_topics": {}, "topic_forward_counts": {}, "topic_labels": {},
             "sent_messages": {}}
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
    for lst in state.get("sent_messages", {}).values():
        del lst[:-SENT_MESSAGES_CAP]
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


SENT_MESSAGES_CAP = 300   # برای هر کاربر، حداکثر چند آیدی پیام خبری نگه داشته بشه


def _record_sent(state, chat_id, message_id):
    """آیدی پیام خبری که برای کاربر فرستاده شده رو ذخیره می‌کنه
    تا /clear بتونه پاکش کنه؛ فقط جدیدترین‌ها نگه داشته می‌شن."""
    if not message_id:
        return
    sent = state.setdefault("sent_messages", {})
    lst = sent.setdefault(str(chat_id), [])
    lst.append(message_id)
    del lst[:-SENT_MESSAGES_CAP]


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


def _sweep_followers(state):
    """روزی یک بار عضویت همه‌ی دنبال‌کننده‌ها رو چک می‌کنه؛ کسی که از کانال
    رفته (left/kicked) رو از لیست حذف می‌کنه و بهش هشدار می‌ده که دوباره عضو
    بشه. خطای تلگرام = اون کاربر این دور رد می‌شه (هرگز بر اساس خطا حذف نمی‌شه).
    ادمین هیچ‌وقت حذف نمی‌شه. True یعنی حافظه تغییر کرده."""
    now = time.time()
    last = float(state.get("last_sweep_ts") or 0)
    if now - last < SWEEP_INTERVAL_SEC:
        return False
    state["last_sweep_ts"] = now
    users = state.get("users", {})
    removed = 0
    for uid in sorted(users):
        if uid == ADMIN_CHAT_ID:
            continue
        try:
            res = api("getChatMember", chat_id=CHANNEL, user_id=uid)
        except Exception as ex:
            print("sweep getChatMember error:", type(ex).__name__)
            continue
        if not res or (res or {}).get("status", "") not in ("left", "kicked"):
            continue
        try:
            send_message(uid, SWEEP_WARN_TEXT, reply_markup=join_keyboard())
        except Exception:
            pass
        users.pop(uid, None)
        removed += 1
        time.sleep(0.05)   # که به سقف سرعت تلگرام نخوریم
    if removed:
        print(f"sweep: {removed} follower(s) removed (left the channel)")
    return True


# ====== کیبوردها ======
ADMIN_BUTTONS = [
    [{"text": "👥 دنبال‌کننده‌ها"}, {"text": "📣 پیام به اعضا"}],
    [{"text": "🔧 تغییرات موضوع"}],
    [{"text": "🔑 کلمه‌های کلیدی"}],
]

# کیبورد موقع قدم‌های جادوگر (فقط لغو)
CANCEL_KEYBOARD = {"keyboard": [[{"text": "❌ لغو"}]], "resize_keyboard": True}


def _parse_keywords(text):
    """کلمه‌های کلیدی رو از متن می‌گیره؛ اگه کاما (، یا ,) باشه، با کاما جدا می‌شن
    و هر تکه می‌تونه یه عبارت چندکلمه‌ای باشه (مثلاً «شورای امنیت»)؛
    اگه کاما نباشه، با فاصله جدا می‌شن (رفتار قبلی)."""
    t = (text or "").strip()
    if not t:
        return []
    if "،" in t or "," in t:
        return [w.strip() for w in t.replace(",", "،").split("،") if w.strip()]
    return t.split()


def remove_topic_keyboard(state=None):
    """دکمه‌های موضوع‌های سفارشی برای حذف + موضوع‌های اصلی حذف‌شده
    برای برگردوندن (با ↩️)؛ اگه هیچ‌کدوم نیست None."""
    customs = list((state or {}).get("custom_topics", {}).values())
    disabled = [topic_label(state, k) for k in (state or {}).get("disabled_topics", []) if k in TOPICS]
    if not customs and not disabled:
        return None
    rows = []
    for i in range(0, len(customs), 2):
        rows.append([{"text": c.get("name", "")}
                     for c in customs[i:i + 2] if c.get("name")])
    for label in disabled:
        rows.append([{"text": "↩️ " + label}])
    rows.append([{"text": "❌ لغو"}])
    return {"keyboard": rows, "resize_keyboard": True}


def _topic_changes_keyboard():
    """کیبورد «تغییرات موضوع»: اضافه، حذف و ویرایش اسم موضوع + لغو."""
    rows = [[{"text": "➕ موضوع جدید"}, {"text": "➖ حذف موضوع"}],
            [{"text": "✏️ ویرایش اسم موضوع"}],
            [{"text": "❌ لغو"}]]
    return {"keyboard": rows, "resize_keyboard": True}


def rename_topic_keyboard(state=None):
    """دکمه‌های همه‌ی موضوع‌ها (اصلی فعال + سفارشی) برای ویرایش اسم؛
    اگه هیچ‌کدوم نیست None."""
    labels = [topic_label(state, k) for k in active_topics(state)]
    labels += [c.get("name", "")
               for c in (state or {}).get("custom_topics", {}).values() if c.get("name")]
    if not labels:
        return None
    rows = [[{"text": t} for t in labels[i:i + 2]] for i in range(0, len(labels), 2)]
    rows.append([{"text": "❌ لغو"}])
    return {"keyboard": rows, "resize_keyboard": True}


def active_topics(state=None):
    """کلید موضوع‌های اصلی که حذف نشدن (حذف‌شده‌ها توی state["disabled_topics"] هستن)."""
    disabled = set((state or {}).get("disabled_topics", []) or [])
    return [k for k in TOPICS if k not in disabled]


def main_keyboard(chat_id=None, state=None):
    """کیبورد اصلی؛ ادمین فقط دکمه‌های مدیریت رو می‌بینه
    (موضوع جدید، دنبال‌کننده‌ها و آمار، پیام به اعضا)،
    بقیه‌ی اعضا دکمه‌ی «📌 انتخاب موضوع» رو می‌بینن (موضوع‌ها داخل اونن).
    موضوع اصلی حذف‌شده توی هیچ کیبوردی نمیاد."""
    if chat_id is not None and ADMIN_CHAT_ID and str(chat_id) == ADMIN_CHAT_ID:
        rows = [list(r) for r in ADMIN_BUTTONS]
        rows.append([{"text": "❓ راهنما"}])
        return {"keyboard": rows, "resize_keyboard": True}
    rows = [[{"text": "📌 انتخاب موضوع"}]]
    rows.append([{"text": "📋 موضوع‌های من"}])
    rows.append([{"text": "🗑 پاک کردن همه‌ی خبرها"}])
    rows.append([{"text": "❓ راهنما"}])
    return {"keyboard": rows, "resize_keyboard": True}


def topic_keyboard(state=None):
    act = active_topics(state)
    rows = [[{"text": topic_label(state, k)} for k in act[i:i + 2]]
            for i in range(0, len(act), 2)]
    customs = list((state or {}).get("custom_topics", {}).values())
    for i in range(0, len(customs), 2):
        rows.append([{"text": c.get("name", "")}
                     for c in customs[i:i + 2] if c.get("name")])
    rows.append([{"text": "❌ لغو"}])
    return {"keyboard": rows, "resize_keyboard": True}


def _select_topics_keyboard(state, chat_id):
    """کیبورد «انتخاب موضوع»: همه‌ی موضوع‌های فعال؛ دنبال‌شده‌ها با ✅.
    زدن هر موضوع دنبال کردن رو روشن/خاموش می‌کنه (تاگل)؛ اگه موضوعی نیست None."""
    followed = set((state or {}).get("users", {}).get(str(chat_id), {}).get("topics", []))
    labels = []
    for k in active_topics(state):
        label = topic_label(state, k)
        labels.append(("✅ " + label) if k in followed else label)
    for c in (state or {}).get("custom_topics", {}).values():
        name = c.get("name", "")
        if not name:
            continue
        labels.append(("✅ " + name) if name in followed else name)
    if not labels:
        return None
    rows = [[{"text": t} for t in labels[i:i + 2]] for i in range(0, len(labels), 2)]
    rows.append([{"text": "❌ لغو"}])
    return {"keyboard": rows, "resize_keyboard": True}


def _open_select_picker(state, chat_id, prompt):
    """پیام «انتخاب موضوع» رو با کیبورد تیک‌دار می‌فرسته؛ اگه موضوعی نباشه False."""
    kb = _select_topics_keyboard(state, chat_id)
    if kb is None:
        send_message(chat_id, "هنوز موضوعی نیست 🙂",
                     reply_markup=main_keyboard(chat_id, state))
        return False
    state.setdefault("pending", {})[chat_id] = {"cmd": "/select", "ts": time.time()}
    send_message(chat_id, prompt, reply_markup=kb)
    return True


def unfollow_keyboard(followed, state=None):
    customs = (state or {}).get("custom_topics", {})
    rows = [[{"text": topic_label(state, k)}] for k in followed
            if k in TOPICS or k in customs]
    rows.append([{"text": "همه"}, {"text": "❌ لغو"}])
    return {"keyboard": rows, "resize_keyboard": True}


def topic_label(state, key):
    """برچسب فارسی یه موضوع (اصلی یا سفارشی)؛ اگه ناشناخته بود خود کلید.
    موضوع سفارشی با همون نام خودش نشون داده می‌شه، بدون هیچ ایموجی اضافه.
    اسم موضوع اصلی رو می‌شه با /renametopic عوض کرد (state["topic_labels"])."""
    if key in TOPICS:
        return _builtin_label(key, (state or {}).get("topic_labels", {}).get(key))
    ct = (state or {}).get("custom_topics", {}).get(key)
    if ct:
        return ct.get("name") or key
    return key


# ====== دسته‌بندی پست کانال ======
def classify_post(text, custom_topics=None, extra_keywords=None, disabled=None,
                  removed_keywords=None):
    """موضوع‌های یه پست کانال رو برمی‌گردونه (مجموعه‌ای از کلیدهای موضوع).
    فقط پست‌های مهم/موضوع‌دار برمی‌گردن؛ پست معمولی (🔵) هیچ موضوعی نمی‌گیره.
    موضوع‌های سفارشی با کلمه‌های کلیدی‌شون توی همون تیتر + اول متن چک می‌شن.
    نسخه‌ی ۰.۱۸: ایموجی موضوع (🔴 🚨 🔥 ⚽ 🟡) هر جای تیتر + اول متن باشه
    کافیه — لازم نیست اولِ پست باشه.
    extra_keywords: کلمه‌های اضافه‌ای که ادمین برای موضوع‌های اصلی گذاشته؛
    disabled: موضوع‌های اصلی حذف‌شده که اصلاً تشخیص داده نمی‌شن؛
    removed_keywords: کلمه‌های داخلی حذف‌شده‌ی موضوع‌های اصلی."""
    t = text or ""
    head = t[:600]   # تیتر + اول متن، برای کلمه‌های کلیدی کافیه
    out = set()
    dis = set(disabled or [])
    extra = extra_keywords or {}
    removed = removed_keywords or {}

    def eff(base, key):
        rem = set(removed.get(key, []))
        return [k for k in base if k not in rem]

    if "political" not in dis and (
            any(e in head for e in ("🔴", "🚨", "🔥")) or any(k in head for k in eff(POLITICS_KW, "political"))
            or any(k in head for k in eff(FLASH_KW, "political"))
            or any(kw and kw in head for kw in extra.get("political", []))):
        out.add("political")
    if "sport" not in dis and (
            "⚽" in head or any(k in head for k in eff(SPORT_KW, "sport"))
            or any(kw and kw in head for kw in extra.get("sport", []))):
        out.add("sport")
    if "gold" not in dis and "🟡" in head and (
            any(k in head for k in eff(GOLD_KW, "gold"))
            or any(kw and kw in head for kw in extra.get("gold", []))):
        out.add("gold")
    for key, ct in (custom_topics or {}).items():
        if any(kw and kw in head for kw in ct.get("keywords", [])):
            out.add(key)
    return out


_EMOJI_LEAD = re.compile(r"^[\U0001F300-\U0001FAFF\u2600-\u27BF\u2190-\u21FF\uFE0F]\s*")


def _norm_topic(raw, custom_topics=None, topic_labels=None):
    """«🔴 سیاسی» یا «سیاسی» یا «political» یا نام موضوع سفارشی → کلید داخلی؛ وگرنه None.
    اسم جدید موضوع اصلی (بعد از /renametopic) هم از topic_labels تشخیص داده می‌شه؛
    اسم قدیمی موضوع اصلی همچنان به‌عنوان نام مستعار کار می‌کنه.
    ایموجی‌های اول رو یکی‌یکی برمی‌داره تا وقتی باقی‌مونده یه موضوع شناخته‌شده بشه؛
    این‌طوری اسم‌هایی که خودشون با ایموجی شروع می‌شن (مثلاً «🚨 جنگ» یا «🔴») هم درست تشخیص داده می‌شن."""
    s = (raw or "").strip()
    customs = custom_topics or {}
    labels = topic_labels or {}

    def resolve(x):
        if x in TOPIC_NAMES:
            return TOPIC_NAMES[x]
        if x in TOPICS:
            return x
        if x in customs:
            return x
        for k, name in labels.items():
            if x == name:
                return k
        return None

    got = resolve(s)
    if got:
        return got
    while True:
        s2 = _EMOJI_LEAD.sub("", s, count=1)
        if s2 == s:
            return None
        s = s2.strip()
        got = resolve(s)
        if got:
            return got


# ====== دستورهای اعضا ======
def _welcome_topics_lines(state=None):
    """خط‌های موضوع‌های فعال برای متن خوش‌آمد (اصلی + سفارشی، بدون ایموجی اضافه)."""
    lines = [topic_label(state, k) for k in active_topics(state)]
    lines += [c.get("name", "")
              for c in (state or {}).get("custom_topics", {}).values() if c.get("name")]
    return lines


WELCOME_INTRO = (
    "سلام! 👋 به ربات خبررسان GodNews24 خوش اومدی!\n\n"
    "من ربات شخصی کانال @GodNews24 هستم؛ خبرهای مهم کانال رو دایرکت برات می‌فرستم. "
    "موضوع‌هایی که دوست داری رو انتخاب کن تا خبرهای مهم همون موضوع برات بیاد:\n"
)

WELCOME_OUTRO = ("\n\nبرای دنبال کردن، دکمه‌ی «📌 انتخاب موضوع» رو بزن یا /follow رو بفرست.\n"
                 "اگه خبر، عکس یا فیلمی داری، با /send برام بفرست تا برای ادمین بره.\n"
                 "راهنما: /help")

ADMIN_WELCOME_TEXT = (
    "سلام! 👋 این ربات خبررسان GodNews24 رو مدیریت می‌کنی.\n\n"
    "از دکمه‌های پایین می‌تونی موضوع‌ها رو تغییر بدی "
    "(موضوع جدید، حذف موضوع، ویرایش اسم موضوع)، "
    "دنبال‌کننده‌ها و آمار رو ببینی و به اعضا پیام بدی."
)

HELP_TEXT = (
    "راهنمای ربات خبررسان GodNews24:\n\n"
    "📌 این ربات چیه؟\n"
    "ربات شخصی کانال @GodNews24؛ خبرهای مهم موضوع‌هایی که دنبال می‌کنی رو دایرکت برات می‌فرسته. "
    "بقیه‌ی خبرها فقط توی کانال منتشر می‌شن.\n\n"
    "/select — انتخاب موضوع‌ها: دکمه‌ی «📌 انتخاب موضوع» رو بزن؛ موضوع‌های دنبال‌شده ✅ دارن و زدن دوباره‌شون لغوشون می‌کنه\n"
    "/follow — دنبال کردن مستقیم یه موضوع: بنویس /follow سیاسی\n"
    "/topics — موضوع‌هایی که دنبال می‌کنی\n"
    "/unfollow — لغو دنبال کردن یه موضوع (یا «همه» برای لغو همه)\n"
    "/send — خبر، عکس یا فیلم بفرست تا برای ادمین بره\n"
    "/clear — پاک کردن همه‌ی خبرهایی که ربات برات فرستاده\n"
    "/help — همین راهنما؛ برای راهنمای یه دستور خاص بنویس: /help /follow\n\n"
    "برای استفاده از ربات، باید عضو کانال @GodNews24 باشی 🙂"
)

ADMIN_HELP_TEXT = (
    "راهنمای مدیریت ربات خبررسان GodNews24:\n\n"
    "/followers — آمار و لیست دنبال‌کننده‌ها؛ برای هر موضوع چند نفر دنبال می‌کنن و چندتا خبر براشون ارسال شده\n"
    "/managetopics — تغییرات موضوع: اضافه کردن موضوع جدید، حذف موضوع و ویرایش اسم موضوع (دکمه‌ی «🔧 تغییرات موضوع»)\n"
    "/addtopic — اضافه کردن موضوع جدید: /addtopic <نام> <کلمه‌ی کلیدی> ...؛ اگه موضوع از قبل باشه، کلمه‌ها بهش اضافه می‌شن\n"
    "/removetopic — حذف یه موضوع سفارشی: /removetopic <نام>\n"
    "/renametopic — ویرایش اسم یه موضوع (اصلی یا سفارشی): /renametopic (انتخاب از لیست) یا /renametopic <نام>\n"
    "/keywords — لیست کلمه‌های کلیدی همه‌ی موضوع‌ها (یا یه موضوع: /keywords <نام>)؛ از همون‌جا با دکمه‌ها می‌تونی کلمه اضافه یا حذف کنی\n"
    "/addkeyword — اضافه کردن کلمه‌ی کلیدی به یه موضوع: /addkeyword (یا /addkeyword <نام موضوع> <کلمه> ...)\n"
    "/removekeyword — حذف یه کلمه‌ی کلیدی با انتخاب از لیست: /removekeyword (یا /removekeyword <نام موضوع>)\n"
    "/broadcast — پیام به دنبال‌کننده‌ها: اول گیرنده رو انتخاب کن (به همه یا فقط یه موضوع)، بعد متن رو بفرست؛ یا مستقیم: /broadcast <متن> (به همه) یا /broadcast <ایموجی موضوع> <متن> (فقط همون موضوع)\n\n"
    "برای راهنمای یه دستور خاص بنویس: /help /broadcast"
)

# دستورهای اعضا (برای /help <دستور>؛ بقیه فقط برای ادمینه)
MEMBER_COMMANDS = {"/follow", "/select", "/topics", "/unfollow", "/send", "/clear", "/help"}

# راهنمای جزئی هر دستور (برای /help <دستور>)
COMMAND_HELP = {
    "/follow": ("دنبال کردن یه موضوع:\n\n"
                "خبرهای مهم موضوع‌هایی که دنبال می‌کنی، دایرکت برات میاد.\n\n"
                "روش‌ها:\n"
                "• دکمه‌ی «📌 انتخاب موضوع» رو بزن و از لیست انتخاب کن\n"
                "• مستقیم بنویس: /follow سیاسی"),
    "/select": ("انتخاب موضوع‌ها:\n\n"
                "دکمه‌ی «📌 انتخاب موضوع» رو بزن؛ همه‌ی موضوع‌ها توی یه لیست میان و موضوع‌هایی که دنبال می‌کنی ✅ دارن.\n"
                "زدن یه موضوع دنبال کردنش رو روشن می‌کنه و زدن دوباره‌ش لغوش می‌کنه."),
    "/topics": ("موضوع‌هایی که الان دنبال می‌کنی رو نشون می‌ده.\n\n"
                "برای دیدنش بنویس /topics یا دکمه‌ی «📋 موضوع‌های من» رو بزن."),
    "/unfollow": ("لغو دنبال کردن یه موضوع:\n\n"
                  "• بنویس /unfollow و از لیست انتخاب کن\n"
                  "• مستقیم بنویس: /unfollow سیاسی\n"
                  "• برای لغو همه‌ی موضوع‌ها بنویس: «همه»"),
    "/send": ("ارسال خبر برای ادمین:\n\n"
              "خبر، عکس یا فیلمت رو بفرست تا برای ادمین بره؛ بعد از تایید توی کانال @GodNews24 منتشر می‌شه.\n"
              "کافیه بنویسی /send و بعد پیامت رو بفرستی."),
    "/clear": ("پاک کردن همه‌ی خبرها:\n\n"
               "همه‌ی خبرهایی که ربات تا حالا برات فرستاده (خبرهای موضوع‌ها و پیام‌های ادمین) از چتت پاک می‌شن.\n\n"
               "بنویس /clear یا دکمه‌ی «🗑 پاک کردن همه‌ی خبرها» رو بزن."),
    "/help": ("راهنمای ربات.\n\n"
              "برای راهنمای یه دستور خاص بنویس: /help /follow"),
    "/followers": ("آمار و لیست دنبال‌کننده‌ها:\n\n"
                   "برای هر موضوع نشون می‌ده چند نفر دنبالش می‌کنن و تا حالا چندتا خبر براشون ارسال شده.\n"
                   "بنویس /followers یا دکمه‌ی «👥 دنبال‌کننده‌ها» رو بزن."),
    "/managetopics": ("تغییرات موضوع:\n\n"
                      "اضافه کردن موضوع جدید، حذف موضوع و ویرایش اسم موضوع، همه از یه جا.\n\n"
                      "• دکمه‌ی «🔧 تغییرات موضوع» رو بزن و یکی رو انتخاب کن\n"
                      "• یا مستقیم بنویس: /managetopics"),
    "/addtopic": ("اضافه کردن موضوع جدید:\n\n"
                  "• از «🔧 تغییرات موضوع» بعد «➕ موضوع جدید»: اول نام موضوع رو بفرست، بعد کلمه‌های کلیدی رو\n"
                  "• مستقیم: /addtopic <نام> <کلمه‌ی کلیدی> ...\n"
                  "• اگه موضوع از قبل باشه، کلمه‌ها بهش اضافه می‌شن؛ برای موضوع اصلی هم با همین دستور کلمه اضافه می‌شه."),
    "/removetopic": ("حذف یه موضوع سفارشی:\n\n"
                     "• از «🔧 تغییرات موضوع» بعد «➖ حذف موضوع» و انتخاب از لیست\n"
                     "• مستقیم: /removetopic <نام>\n"
                     "موضوع اصلی حذف‌شده رو می‌تونی از همون لیست برگردونی."),
    "/renametopic": ("ویرایش اسم یه موضوع (اصلی یا سفارشی):\n\n"
                     "• از «🔧 تغییرات موضوع» بعد «✏️ ویرایش اسم موضوع» و انتخاب از لیست، بعد اسم جدید رو بفرست\n"
                     "• مستقیم: /renametopic (انتخاب از لیست) یا /renametopic <نام>\n"
                     "اسم موضوع اصلی عوض می‌شه ولی کلید، کلمه‌های کلیدی و تشخیص خبرهاش همون می‌مونه؛ "
                     "برای موضوع سفارشی، دنبال‌کننده‌ها و آمار ارسالش هم با اسم جدید می‌مونه."),
    "/keywords": ("لیست کلمه‌های کلیدی همه‌ی موضوع‌ها (یا یه موضوع: /keywords <نام>).\n\n"
                  "از همون‌جا با دکمه‌های «➕ اضافه کردن کلمه‌ی کلیدی» و «➖ حذف کلمه‌ی کلیدی» می‌تونی کلمه اضافه یا حذف کنی."),
    "/addkeyword": ("اضافه کردن کلمه‌ی کلیدی به یه موضوع:\n\n"
                    "• بنویس /addkeyword و اول موضوع رو انتخاب کن، بعد کلمه‌ها رو بفرست\n"
                    "• مستقیم: /addkeyword <نام موضوع> <کلمه> ...\n"
                    "کلمه‌ها رو می‌تونی با کاما جدا کنی؛ هر کدوم می‌تونه چند کلمه باشه."),
    "/removekeyword": ("حذف یه کلمه‌ی کلیدی:\n\n"
                       "• بنویس /removekeyword و اول موضوع، بعد کلمه رو از لیست انتخاب کن\n"
                       "• مستقیم: /removekeyword <نام موضوع>"),
    "/broadcast": ("پیام به دنبال‌کننده‌ها:\n\n"
                   "• با دکمه‌ی «📣 پیام به اعضا»: اول گیرنده رو انتخاب کن («👥 به همه» یا یه موضوع)، بعد متن رو بفرست\n"
                   "• مستقیم: /broadcast <متن> (به همه) یا /broadcast <ایموجی موضوع> <متن> (فقط همون موضوع)؛ مثلاً /broadcast 🔴 سیاسی سلام\n"
                   "• برای موضوع سفارشی اولش اسم کاملش رو بنویس (مثلاً 🚨 جنگ)"),
}


def welcome_text(is_admin, state=None):
    """متن خوش‌آمد؛ ادمین متن مدیریت رو می‌بینه و نسخه‌ی ربات فقط برای ادمین نشون داده می‌شه.
    متن اعضا موضوع‌های فعال (اصلی + سفارشی) رو هم لیست می‌کنه."""
    if is_admin:
        return ADMIN_WELCOME_TEXT + f"\n\nنسخه‌ی ربات: {BOT_VERSION}"
    return WELCOME_INTRO + "\n".join(_welcome_topics_lines(state)) + WELCOME_OUTRO


def help_text(is_admin, arg=None):
    """راهنما؛ ادمین فقط دستورهای مدیریت رو می‌بینه، اعضا فقط دستورهای خودشون رو.
    با آرگومان (/help <دستور>) راهنمای جزئی همون دستور رو می‌ده."""
    if arg:
        cmd = "/" + arg.lstrip("/").split()[0]
        detail = COMMAND_HELP.get(cmd)
        if detail:
            if is_admin or cmd in MEMBER_COMMANDS:
                return detail
            return "این دستور فقط برای ادمینه."
    return ADMIN_HELP_TEXT if is_admin else HELP_TEXT


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


def _broadcast_target_keyboard(state=None):
    """کیبورد انتخاب گیرنده‌ی پیام به اعضا: «به همه» + هر موضوع فعال؛
    ادمین اول گیرنده رو انتخاب می‌کنه، بعد متن پیام رو می‌فرسته."""
    labels = [topic_label(state, k) for k in active_topics(state)]
    labels += [c.get("name", "")
               for c in (state or {}).get("custom_topics", {}).values() if c.get("name")]
    rows = [[{"text": t} for t in labels[i:i + 2]] for i in range(0, len(labels), 2)]
    rows.insert(0, [{"text": "👥 به همه"}])
    rows.append([{"text": "❌ لغو"}])
    return {"keyboard": rows, "resize_keyboard": True}


def _broadcast_target(arg, custom_topics=None, topic_labels=None):
    """اگه متن با «ایموجی + نام موضوع» شروع بشه، (کلید موضوع، بقیه‌ی متن) رو
    برمی‌گردونه؛ وگرنه (None, کل متن) — یعنی پیام همگانی. اسم خالی موضوع
    به‌تنهایی انتخاب‌کننده نیست، پس پیامتی که با «سیاسی ...» شروع بشه به همه
    می‌رسه، نه فقط به سیاسی‌ها. فاصله‌ی بین ایموجی و نام مهم نیست.
    موضوع سفارشی با ایموجی اولِ اسم خودش (مثلاً «🚨 جنگ») یا با 📌 انتخاب می‌شه؛
    موضوع سفارشی بدون ایموجی فقط با 📌 (مثلاً «📌 سینما»).
    اسم جدید موضوع اصلی (بعد از /renametopic) هم با همون ایموجی + نام جدید انتخاب می‌شه."""
    s = (arg or "").strip()
    labels = [(k, _builtin_label(k, (topic_labels or {}).get(k))) for k in TOPICS]
    for k, ct in (custom_topics or {}).items():
        name = ct.get("name") or k
        if _EMOJI_LEAD.match(name):
            labels.append((k, name))
        labels.append((k, CUSTOM_TOPIC_EMOJI + " " + name))
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
            _record_sent(state, uid, res.get("message_id"))
        time.sleep(0.05)   # که به سقف سرعت تلگرام نخوریم
    return sent, failed


def _clear_news(state, chat_id):
    """همه‌ی خبرهایی که ربات برای این کاربر فرستاده (فورواردهای کانال و
    پیام‌های ادمین) رو از چتش پاک می‌کنه؛ برمی‌گردونه (تعداد پاک‌شده،
    تعداد ناموفق). پیام‌هایی که دیگه قابل پاک کردن نیستن (مثلاً کاربر
    خودش پاکشون کرده) فقط شمرده می‌شن و لیست خالی می‌شه."""
    ids = (state.get("sent_messages") or {}).get(str(chat_id), [])
    if not ids:
        return 0, 0
    deleted = failed = 0
    for mid in ids:
        try:
            res = api("deleteMessage", chat_id=chat_id, message_id=mid)
        except Exception:
            res = None
        if res is None:
            failed += 1
        else:
            deleted += 1
        time.sleep(0.05)
    state.setdefault("sent_messages", {})[str(chat_id)] = []
    return deleted, failed


def _submit_news(state, msg, chat_id, direct_text=None):
    """خبر/عکس/فیلم یه عضو رو برای ادمین می‌فرسته: اول یه خط معرفی، بعد خود پیام
    با forwardMessage تا مدیا حفظ بشه (اگه متن مستقیم داده شده باشه، همون متن
    فرستاده می‌شه). حداکثر ۱ ارسال هر ۶۰ ثانیه برای هر عضو. پیام‌های خود ادمین
    هیچ‌وقت برای خودش فوروارد نمی‌شن."""
    if chat_id == ADMIN_CHAT_ID:
        send_message(chat_id, "این دستور رو نمی‌شناسم. /help رو بزن تا راهنما رو ببینی.",
                     reply_markup=main_keyboard(chat_id, state))
        return True
    if not ADMIN_CHAT_ID:
        send_message(chat_id, "ارسال خبر فعلاً ممکن نیست.", reply_markup=main_keyboard(chat_id, state))
        return True
    now = time.time()
    last = _SUBMIT_LAST.get(chat_id, 0)
    if now - last < SUBMIT_COOLDOWN_SEC:
        send_message(chat_id, "لطفاً کمی صبر کن و بعد دوباره بفرست 🙂",
                     reply_markup=main_keyboard(chat_id, state))
        return True
    _SUBMIT_LAST[chat_id] = now
    send_message(ADMIN_CHAT_ID, f"📩 خبر ارسالی از {_sender_display(msg)}")
    if direct_text is not None:
        send_message(ADMIN_CHAT_ID, direct_text)
    else:
        forward_message_to(ADMIN_CHAT_ID, chat_id, msg.get("message_id"))
    send_message(chat_id, "خبرت رسید و برای ادمین فرستاده شد ✅",
                 reply_markup=main_keyboard(chat_id, state))
    return True


def _add_custom_topic(state, name, keywords):
    """موضوع سفارشی اضافه می‌کنه؛ برمی‌گردونه (موفق؟، پیام)."""
    if not name or not keywords:
        return False, ("برای اضافه کردن موضوع:\n/addtopic <نام موضوع> <کلمه‌ی کلیدی ۱> [کلمه‌ی کلیدی ۲ ...]\nمثلاً: /addtopic سینما فیلم سینما بازیگر")
    customs = state.setdefault("custom_topics", {})
    if name in TOPIC_NAMES or name in TOPICS:
        return False, f"«{name}» موضوع اصلیه و نمی‌شه دوباره اضافه‌ش کرد."
    if not re.sub(r"[\U0001F300-\U0001FAFF\u2600-\u27BF\u2190-\u21FF\uFE0F\s]", "", name):
        return False, "نام موضوع نمی‌تونه فقط ایموجی باشه؛ یه نام با حروف بفرست."
    if name in customs:
        return False, f"موضوع «{name}» از قبل هست."
    customs[name] = {"name": name, "keywords": list(keywords)}
    return True, f"موضوع «{name}» اضافه شد ✅\nکلمه‌های کلیدی: {', '.join(keywords)}"


def _builtin_hardcoded(key):
    return {"political": POLITICS_KW + FLASH_KW, "sport": SPORT_KW, "gold": GOLD_KW}.get(key, [])


def _topic_keywords(state, key):
    """همه‌ی کلمه‌های کلیدی فعال یه موضوع: برای موضوع اصلی، کلمه‌های داخلی
    (به‌جز حذف‌شده‌ها) + اضافه‌شده‌ها؛ برای سفارشی، کلمه‌هاش."""
    if key in TOPICS:
        removed = set((state.get("topic_removed_keywords") or {}).get(key, []))
        extras = (state.get("topic_extra_keywords") or {}).get(key, [])
        return [kw for kw in _builtin_hardcoded(key) if kw not in removed] + list(extras)
    ct = (state.get("custom_topics") or {}).get(key)
    return list(ct.get("keywords", [])) if ct else []


def _keyword_keyboard(state, key):
    """دکمه‌های کلمه‌های کلیدی یه موضوع برای حذف (دو تا در هر ردیف) + لغو."""
    kws = _topic_keywords(state, key)
    rows = [[{"text": kw} for kw in kws[i:i + 2]] for i in range(0, len(kws), 2)]
    rows.append([{"text": "❌ لغو"}])
    return {"keyboard": rows, "resize_keyboard": True}


def _keywords_text(state, key=None):
    """متن لیست کلمه‌های کلیدی؛ بدون آرگومان همه‌ی موضوع‌ها، با آرگومان فقط اون موضوع."""
    keys = [key] if key else active_topics(state) + list(state.get("custom_topics", {}))
    lines = ["🔑 کلمه‌های کلیدی موضوع‌ها:", ""]
    for k in keys:
        label = topic_label(state, k)
        kws = _topic_keywords(state, k)
        lines.append(f"{label}:")
        lines.append("، ".join(kws) if kws else "—")
        if k in TOPICS:
            extras = (state.get("topic_extra_keywords") or {}).get(k, [])
            removed = (state.get("topic_removed_keywords") or {}).get(k, [])
            if extras:
                lines.append("➕ اضافه‌شده: " + "، ".join(extras))
            if removed:
                lines.append("➖ حذف‌شده: " + "، ".join(removed))
        lines.append("")
    lines.append("موضوع‌های اصلی با ایموجی‌شون (🔴 ⚽ 🟡) هر جای پست هم تشخیص داده می‌شن.")
    lines.append("برای حذف یه کلمه: /removekeyword")
    return "\n".join(lines).rstrip()


def _remove_keyword(state, key, kw):
    """یه کلمه‌ی کلیدی رو از یه موضوع حذف می‌کنه؛ برمی‌گردونه (موفق؟، پیام).
    کلمه‌ی داخلی موضوع اصلی به لیست حذف‌شده‌ها می‌ره (با /addtopic برمی‌گرده)؛
    آخرین کلمه‌ی موضوع سفارشی قابل حذف نیست."""
    label = topic_label(state, key)
    if key in TOPICS:
        extras = (state.get("topic_extra_keywords") or {}).get(key, [])
        if kw in extras:
            extras.remove(kw)
            return True, f"«{kw}» از موضوع «{label}» حذف شد."
        removed = state.setdefault("topic_removed_keywords", {}).setdefault(key, [])
        if kw in removed:
            return False, "این کلمه قبلاً حذف شده."
        if kw in _builtin_hardcoded(key):
            removed.append(kw)
            return True, f"«{kw}» از موضوع «{label}» حذف شد."
        return False, "چنین کلمه‌ای توی این موضوع نیست."
    ct = (state.get("custom_topics") or {}).get(key)
    if not ct:
        return False, "چنین موضوعی نداریم."
    kws = ct.get("keywords", [])
    if kw not in kws:
        return False, "چنین کلمه‌ای توی این موضوع نیست."
    if len(kws) <= 1:
        return False, "آخرین کلمه‌ی موضوع رو نمی‌شه حذف کرد؛ اگه موضوع رو نمی‌خوای، خود موضوع رو حذف کن."
    kws.remove(kw)
    return True, f"«{kw}» از موضوع «{label}» حذف شد."


def _add_builtin_keywords(state, key, keywords):
    """کلمه‌های کلیدی اضافه به یه موضوع اصلی اضافه می‌کنه؛
    برمی‌گردونه (موفق؟، پیام)."""
    if key not in TOPICS:
        return False, "چنین موضوعی نداریم."
    if key in (state.get("disabled_topics") or []):
        return False, f"موضوع «{topic_label(state, key)}» حذف شده؛ اول برگردونش، بعد کلمه اضافه کن."
    hardcoded = _builtin_hardcoded(key)
    extras = state.setdefault("topic_extra_keywords", {}).setdefault(key, [])
    removed = (state.get("topic_removed_keywords") or {}).get(key, [])
    restored = [kw for kw in (keywords or []) if kw and kw in removed]
    for kw in restored:
        removed.remove(kw)
    new = [kw for kw in (keywords or []) if kw and kw not in extras and kw not in hardcoded]
    if not new and not restored:
        return False, "این کلمه‌ها از قبل بودن."
    extras.extend(new)
    parts = []
    if new:
        parts.append(f"به موضوع «{topic_label(state, key)}» این کلمه‌ها اضافه شد: {', '.join(new)} ✅")
    if restored:
        parts.append(f"این کلمه‌ها برگشتن: {', '.join(restored)} ✅")
    return True, "\n".join(parts)


def _add_custom_keywords(state, key, keywords):
    """کلمه‌های کلیدی به یه موضوع سفارشی اضافه می‌کنه؛
    برمی‌گردونه (موفق؟، پیام). کلمه‌های تکراری نادیده گرفته می‌شن."""
    ct = (state.get("custom_topics") or {}).get(key)
    if not ct:
        return False, "چنین موضوعی نداریم."
    kws = ct.setdefault("keywords", [])
    added = [kw for kw in (keywords or []) if kw and kw not in kws]
    if not added:
        return False, "این کلمه‌ها از قبل توی این موضوع هستن."
    kws.extend(added)
    label = ct.get("name") or key
    return True, f"به موضوع «{label}» این کلمه‌ها اضافه شد: {', '.join(added)} ✅"


def _keywords_keyboard(state=None):
    """کیبورد خروجی /keywords: فقط دو عمل کلمه‌ی کلیدی (اضافه و حذف)
    + بازگشت به منوی اصلی؛ بقیه‌ی دستورهای مدیریت اینجا نیستن."""
    rows = [[{"text": "➕ اضافه کردن کلمه‌ی کلیدی"}, {"text": "➖ حذف کلمه‌ی کلیدی"}],
            [{"text": "🔙 منوی اصلی"}]]
    return {"keyboard": rows, "resize_keyboard": True}


def _disable_builtin(state, key):
    """یه موضوع اصلی رو حذف می‌کنه (نرم؛ قابل برگردوندن) و دنبال کردنش رو
    از همه‌ی دنبال‌کننده‌ها درمی‌آره؛ برمی‌گردونه (موفق؟، پیام)."""
    if key not in TOPICS:
        return False, "چنین موضوعی نداریم."
    disabled = state.setdefault("disabled_topics", [])
    if key in disabled:
        return False, f"موضوع «{topic_label(state, key)}» قبلاً حذف شده."
    disabled.append(key)
    affected = 0
    for u in state.get("users", {}).values():
        if key in u.get("topics", []):
            u["topics"].remove(key)
            affected += 1
    return True, f"موضوع «{topic_label(state, key)}» حذف شد؛ دنبال کردن برای {affected} نفر لغو شد."


def _restore_builtin(state, key):
    """یه موضوع اصلی حذف‌شده رو برمی‌گردونه؛ برمی‌گردونه (موفق؟، پیام)."""
    if key not in TOPICS:
        return False, "چنین موضوعی نداریم."
    disabled = state.setdefault("disabled_topics", [])
    if key not in disabled:
        return False, f"موضوع «{topic_label(state, key)}» حذف نشده بود."
    disabled.remove(key)
    return True, f"موضوع «{topic_label(state, key)}» برگشت ✅"


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


def _rename_custom_topic(state, old, new):
    """اسم یه موضوع سفارشی رو عوض می‌کنه؛ دنبال‌کننده‌ها و آمار ارسال هم
    با اسم جدید می‌مونن (جای موضوع توی لیست‌ها عوض نمی‌شه)؛
    برمی‌گردونه (موفق؟، پیام)."""
    customs = state.get("custom_topics", {})
    if not old or old not in customs:
        return False, "چنین موضوعی نداریم."
    new = (new or "").strip()
    if not new:
        return False, "اسم جدید رو بفرست."
    if new == old:
        return False, "اسم جدید با قبلی یکیه."
    if new in _reserved_topic_names(state, except_key=old):
        return False, f"موضوع «{new}» از قبل هست."
    if not re.sub(r"[\U0001F300-\U0001FAFF\u2600-\u27BF\u2190-\u21FF\uFE0F\s]", "", new):
        return False, "نام موضوع نمی‌تونه فقط ایموجی باشه؛ یه نام با حروف بفرست."
    rebuilt = {}
    for k, v in customs.items():
        if k == old:
            entry = dict(v)
            entry["name"] = new
            rebuilt[new] = entry
        else:
            rebuilt[k] = v
    state["custom_topics"] = rebuilt
    for u in state.get("users", {}).values():
        ts = u.get("topics", [])
        if old in ts:
            u["topics"] = [new if t == old else t for t in ts]
    counts = state.get("topic_forward_counts", {})
    if old in counts:
        counts[new] = counts.pop(old)
    return True, (f"اسم موضوع «{old}» به «{new}» عوض شد ✅\n"
                  "دنبال‌کننده‌ها و آمار ارسالش سر جاشون مونده.")


def _builtin_name_part(state, key):
    """نام فعلی یه موضوع اصلی بدون ایموجی (برای مقایسه‌ی اسم جدید)."""
    override = (state or {}).get("topic_labels", {}).get(key)
    if override:
        return _EMOJI_LEAD.sub("", override, count=1).strip() or override
    parts = TOPICS[key].split(None, 1)
    return parts[1] if len(parts) > 1 else TOPICS[key]


def _reserved_topic_names(state, except_key=None):
    """همه‌ی اسم‌ها و برچسب‌های فعلی موضوع‌ها (به‌جز موضوعی که داره ویرایش می‌شه)؛
    برای اینکه اسم جدید با هیچ موضوع دیگه‌ای قاطی نشه. اسم قدیمی خود همون
    موضوع مستثناست تا بشه اسمش رو به حالت اول برگردوند."""
    names = set()
    for nm, k in TOPIC_NAMES.items():
        if k != except_key:
            names.add(nm)
    for k in TOPICS:
        if k != except_key:
            names.add(k)
            names.add(topic_label(state, k))
            names.add(_builtin_name_part(state, k))
    for k, ct in (state or {}).get("custom_topics", {}).items():
        if k == except_key:
            continue
        names.add(k)
        if ct.get("name"):
            names.add(ct["name"])
    return names


def _rename_builtin_topic(state, key, new):
    """اسم یه موضوع اصلی رو عوض می‌کنه (فقط برچسب؛ کلید، کلمه‌ها و تشخیص
    خبرهاش عوض نمی‌شن)؛ برمی‌گردونه (موفق؟، پیام)."""
    if key not in TOPICS:
        return False, "چنین موضوعی نداریم."
    new = (new or "").strip()
    if not new:
        return False, "اسم جدید رو بفرست."
    cur_label = topic_label(state, key)
    cur_name = _builtin_name_part(state, key)
    if new == cur_label or new == cur_name:
        return False, "اسم جدید با قبلی یکیه."
    if not re.sub(r"[\U0001F300-\U0001FAFF\u2600-\u27BF\u2190-\u21FF\uFE0F\s]", "", new):
        return False, "نام موضوع نمی‌تونه فقط ایموجی باشه؛ یه نام با حروف بفرست."
    reserved = _reserved_topic_names(state, except_key=key)
    if new in reserved or _builtin_label(key, new) in reserved:
        return False, f"موضوع «{new}» از قبل هست."
    state.setdefault("topic_labels", {})[key] = new
    return True, f"اسم موضوع «{cur_label}» به «{_builtin_label(key, new)}» عوض شد ✅"


def _rename_topic(state, key, new):
    """اسم یه موضوع (اصلی یا سفارشی) رو عوض می‌کنه؛ برمی‌گردونه (موفق؟، پیام)."""
    if key in TOPICS:
        return _rename_builtin_topic(state, key, new)
    return _rename_custom_topic(state, key, new)


def _send_followers(state, chat_id):
    """آمار + لیست دنبال‌کننده‌ها رو برای ادمین می‌فرسته (فقط ادمین)."""
    if chat_id != ADMIN_CHAT_ID:
        send_message(chat_id, "این دستور فقط برای ادمینه.", reply_markup=main_keyboard(chat_id, state))
        return True
    keys = active_topics(state) + list(state.get("custom_topics", {}))
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
    _reset_forward_counts_if_new_day(state)
    fcounts = state.get("topic_forward_counts", {}) or {}
    flines = "\n".join(f"{topic_label(state, k)}: {fcounts.get(k, 0)}" for k in keys)
    out = (f"دنبال‌کننده‌ها (کل: {total} نفر):\n{lines}\n\n"
           f"📊 خبرهای ارسال‌شده برای هر موضوع:\n{flines}")
    if listed:
        listed.sort(key=lambda row: row[0])
        out += "\n\n" + "\n".join(row[1] for row in listed[:30])
        if len(listed) > 30:
            out += f"\nو {len(listed) - 30} نفر دیگر"
    send_message(chat_id, out, reply_markup=main_keyboard(chat_id, state))
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
            send_message(chat_id, "لغو شد.", reply_markup=main_keyboard(chat_id, state))
        return True

    if text == "/start":
        pending.pop(chat_id, None)
        send_message(chat_id, welcome_text(chat_id == ADMIN_CHAT_ID, state), reply_markup=main_keyboard(chat_id, state))
        return True
    if text == "/help" or text.startswith("/help ") or text == "❓ راهنما":
        pending.pop(chat_id, None)
        arg = text.split(None, 1)[1].strip() if text.startswith("/help ") else None
        send_message(chat_id, help_text(chat_id == ADMIN_CHAT_ID, arg),
                     reply_markup=main_keyboard(chat_id, state))
        return True
    if text == "/topics" or text == "📋 موضوع‌های من":
        pending.pop(chat_id, None)
        topics = state.get("users", {}).get(chat_id, {}).get("topics", [])
        customs = state.get("custom_topics", {})
        shown = [k for k in topics if k in TOPICS or k in customs]
        if not shown:
            send_message(chat_id, "هنوز هیچ موضوعی رو دنبال نمی‌کنی.\nبا /follow یه موضوع انتخاب کن 🙂",
                         reply_markup=main_keyboard(chat_id, state))
        else:
            lines = "\n".join("• " + topic_label(state, k) for k in shown)
            send_message(chat_id, "موضوع‌هایی که دنبال می‌کنی:\n" + lines,
                         reply_markup=main_keyboard(chat_id, state))
        return True
    if text == "/clear" or text == "🗑 پاک کردن همه‌ی خبرها":
        pending.pop(chat_id, None)
        deleted, failed = _clear_news(state, chat_id)
        if deleted == 0 and failed == 0:
            send_message(chat_id, "خبری برای پاک کردن نیست 🙂",
                         reply_markup=main_keyboard(chat_id, state))
        else:
            reply = f"✅ {deleted} خبر پاک شد."
            if failed:
                reply += f"\n{failed} خبر دیگه قابل پاک کردن نبود (مثلاً قبلاً پاک شده بود)."
            send_message(chat_id, reply, reply_markup=main_keyboard(chat_id, state))
        return True
    if text == "🔙 منوی اصلی":
        pending.pop(chat_id, None)
        send_message(chat_id, "منوی اصلی:",
                     reply_markup=main_keyboard(chat_id, state))
        return True
    if text == "📌 انتخاب موضوع" or text == "/select":
        pending.pop(chat_id, None)
        _open_select_picker(state, chat_id,
                            "موضوع‌هایی که می‌خوای دنبال کنی رو انتخاب کن؛\nموضوع‌های دنبال‌شده ✅ دارن و زدن دوباره‌شون لغوشون می‌کنه:")
        return True
    if text == "/followers":
        return _send_followers(state, chat_id)
    if text == "👥 دنبال‌کننده‌ها":
        return _send_followers(state, chat_id)
    if text == "📣 پیام به اعضا":
        if chat_id != ADMIN_CHAT_ID:
            send_message(chat_id, "این دستور فقط برای ادمینه.", reply_markup=main_keyboard(chat_id, state))
            return True
        pending[chat_id] = {"cmd": "/broadcast", "step": "target", "ts": time.time()}
        send_message(chat_id, "پیام به کی برسه؟",
                     reply_markup=_broadcast_target_keyboard(state))
        return True
    if text == "🔧 تغییرات موضوع" or text == "/managetopics":
        if chat_id != ADMIN_CHAT_ID:
            send_message(chat_id, "این دستور فقط برای ادمینه.", reply_markup=main_keyboard(chat_id, state))
            return True
        pending[chat_id] = {"cmd": "/managetopics", "ts": time.time()}
        send_message(chat_id, "چی رو می‌خوای تغییر بدی؟",
                     reply_markup=_topic_changes_keyboard())
        return True
    if text == "➕ موضوع جدید":
        if chat_id != ADMIN_CHAT_ID:
            send_message(chat_id, "این دستور فقط برای ادمینه.", reply_markup=main_keyboard(chat_id, state))
            return True
        pending[chat_id] = {"cmd": "/addtopic", "step": "name", "ts": time.time()}
        send_message(chat_id, "نام موضوع جدید چیه؟",
                     reply_markup=CANCEL_KEYBOARD)
        return True
    if text == "➖ حذف موضوع":
        if chat_id != ADMIN_CHAT_ID:
            send_message(chat_id, "این دستور فقط برای ادمینه.", reply_markup=main_keyboard(chat_id, state))
            return True
        kb = remove_topic_keyboard(state)
        if kb is None:
            send_message(chat_id, "هیچ موضوع سفارشی‌ای نداری که حذفش کنی 🙂",
                         reply_markup=main_keyboard(chat_id, state))
            return True
        pending[chat_id] = {"cmd": "/removetopic", "step": "pick", "ts": time.time()}
        send_message(chat_id, "کدوم موضوع رو حذف کنم؟\n(موضوع حذف‌شده رو بزن تا برگرده)",
                     reply_markup=kb)
        return True
    if text == "✏️ ویرایش اسم موضوع":
        if chat_id != ADMIN_CHAT_ID:
            send_message(chat_id, "این دستور فقط برای ادمینه.", reply_markup=main_keyboard(chat_id, state))
            return True
        kb = rename_topic_keyboard(state)
        if kb is None:
            send_message(chat_id, "هنوز موضوعی نداری که اسمش رو عوض کنی 🙂",
                         reply_markup=main_keyboard(chat_id, state))
            return True
        pending[chat_id] = {"cmd": "/renametopic", "step": "pick", "ts": time.time()}
        send_message(chat_id, "اسم کدوم موضوع رو عوض کنم؟",
                     reply_markup=kb)
        return True
    if text == "/renametopic" or text.startswith("/renametopic "):
        if chat_id != ADMIN_CHAT_ID:
            send_message(chat_id, "این دستور فقط برای ادمینه.", reply_markup=main_keyboard(chat_id, state))
            return True
        parts = text.split(None, 1)
        if len(parts) == 1:
            kb = rename_topic_keyboard(state)
            if kb is None:
                send_message(chat_id, "هنوز موضوعی نداری که اسمش رو عوض کنی 🙂",
                             reply_markup=main_keyboard(chat_id, state))
                return True
            pending[chat_id] = {"cmd": "/renametopic", "step": "pick", "ts": time.time()}
            send_message(chat_id, "اسم کدوم موضوع رو عوض کنم؟",
                         reply_markup=kb)
            return True
        key = _norm_topic(parts[1].strip(), state.get("custom_topics", {}), state.get("topic_labels", {}))
        if not key or (key not in TOPICS and key not in state.get("custom_topics", {})):
            send_message(chat_id, "چنین موضوعی نداریم.",
                         reply_markup=main_keyboard(chat_id, state))
            return True
        if key in (state.get("disabled_topics") or []):
            send_message(chat_id, "این موضوع حذف شده.",
                         reply_markup=main_keyboard(chat_id, state))
            return True
        pending[chat_id] = {"cmd": "/renametopic", "step": "newname", "topic": key,
                            "ts": time.time()}
        send_message(chat_id, f"اسم جدید «{topic_label(state, key)}» چیه؟",
                     reply_markup=CANCEL_KEYBOARD)
        return True
    if text == "/addtopic" or text.startswith("/addtopic "):
        if chat_id != ADMIN_CHAT_ID:
            send_message(chat_id, "این دستور فقط برای ادمینه.", reply_markup=main_keyboard(chat_id, state))
            return True
        parts = text.split(None, 2)
        name_raw = parts[1] if len(parts) > 1 else ""
        kws = _parse_keywords(parts[2]) if len(parts) > 2 else []
        key = _norm_topic(name_raw, state.get("custom_topics", {}), state.get("topic_labels", {}))
        if key in TOPICS:
            # موضوع اصلی: کلمه‌ی کلیدی اضافه می‌شه؛ بدون کلمه = برگردوندن
            if key in (state.get("disabled_topics") or []):
                ok, reply = _restore_builtin(state, key)
                if ok and kws:
                    _ok2, reply2 = _add_builtin_keywords(state, key, kws)
                    reply = reply + "\n" + reply2
            elif not kws:
                reply = (f"«{topic_label(state, key)}» موضوع اصلیه؛ برای اضافه کردن کلمه‌ی کلیدی، "
                         f"کلمه‌ها رو هم بفرست:\n/addtopic {name_raw} <کلمه> ...")
            else:
                ok, reply = _add_builtin_keywords(state, key, kws)
        else:
            existing = _norm_topic(name_raw, state.get("custom_topics", {}), state.get("topic_labels", {}))
            if existing in (state.get("custom_topics", {}) or {}):
                if kws:
                    ok, reply = _add_custom_keywords(state, existing, kws)
                else:
                    ok, reply = False, (f"موضوع «{name_raw}» از قبل هست؛ "
                                         f"برای اضافه کردن کلمه‌ی کلیدی، کلمه‌ها رو هم بفرست:\n"
                                         f"/addtopic {name_raw} <کلمه> ...")
            else:
                ok, reply = _add_custom_topic(state, name_raw, kws)
        send_message(chat_id, reply, reply_markup=main_keyboard(chat_id, state))
        return True
    if text == "/removetopic" or text.startswith("/removetopic "):
        if chat_id != ADMIN_CHAT_ID:
            send_message(chat_id, "این دستور فقط برای ادمینه.", reply_markup=main_keyboard(chat_id, state))
            return True
        parts = text.split(None, 1)
        name_raw = parts[1].strip() if len(parts) > 1 else ""
        key = _norm_topic(name_raw, state.get("custom_topics", {}), state.get("topic_labels", {}))
        if key in TOPICS:
            ok, reply = _disable_builtin(state, key)
        else:
            ok, reply = _remove_custom_topic(state, name_raw)
        send_message(chat_id, reply, reply_markup=main_keyboard(chat_id, state))
        return True
    if text == "/keywords" or text.startswith("/keywords ") or text == "🔑 کلمه‌های کلیدی":
        if chat_id != ADMIN_CHAT_ID:
            send_message(chat_id, "این دستور فقط برای ادمینه.", reply_markup=main_keyboard(chat_id, state))
            return True
        parts = text.split(None, 1)
        key = None
        if text.startswith("/keywords "):
            key = _norm_topic(parts[1].strip(), state.get("custom_topics", {}), state.get("topic_labels", {}))
            if not key or (key in TOPICS and key in (state.get("disabled_topics") or [])):
                send_message(chat_id, "چنین موضوعی نداریم.", reply_markup=main_keyboard(chat_id, state))
                return True
        send_message(chat_id, _keywords_text(state, key), reply_markup=_keywords_keyboard(state))
        return True
    if text == "/removekeyword" or text.startswith("/removekeyword ") or text == "➖ حذف کلمه‌ی کلیدی":
        if chat_id != ADMIN_CHAT_ID:
            send_message(chat_id, "این دستور فقط برای ادمینه.", reply_markup=main_keyboard(chat_id, state))
            return True
        parts = text.split(None, 1)
        key = None
        if text.startswith("/removekeyword "):
            key = _norm_topic(parts[1].strip(), state.get("custom_topics", {}), state.get("topic_labels", {}))
            if not key or key not in active_topics(state) + list(state.get("custom_topics", {})):
                send_message(chat_id, "چنین موضوعی نداریم.", reply_markup=main_keyboard(chat_id, state))
                return True
        if key:
            label = topic_label(state, key)
            pending[chat_id] = {"cmd": "/removekeyword", "step": "keyword", "topic": key,
                                "ts": time.time()}
            send_message(chat_id, f"کدوم کلمه‌ی «{label}» رو حذف کنم؟",
                         reply_markup=_keyword_keyboard(state, key))
        else:
            pending[chat_id] = {"cmd": "/removekeyword", "step": "topic", "ts": time.time()}
            send_message(chat_id, "کلمه‌ی کدوم موضوع رو می‌خوای حذف کنی؟",
                         reply_markup=topic_keyboard(state))
        return True
    if text == "/addkeyword" or text.startswith("/addkeyword ") or text == "➕ اضافه کردن کلمه‌ی کلیدی":
        if chat_id != ADMIN_CHAT_ID:
            send_message(chat_id, "این دستور فقط برای ادمینه.", reply_markup=main_keyboard(chat_id, state))
            return True
        if text == "/addkeyword" or text == "➕ اضافه کردن کلمه‌ی کلیدی":
            pending[chat_id] = {"cmd": "/addkeyword", "step": "topic", "ts": time.time()}
            send_message(chat_id, "کلمه رو به کدوم موضوع اضافه کنم؟",
                         reply_markup=topic_keyboard(state))
            return True
        parts = text.split(None, 2)
        key = _norm_topic(parts[1].strip(), state.get("custom_topics", {}), state.get("topic_labels", {})) if len(parts) > 1 else None
        if not key or key not in active_topics(state) + list(state.get("custom_topics", {})):
            send_message(chat_id, "چنین موضوعی نداریم.", reply_markup=main_keyboard(chat_id, state))
            return True
        kws = _parse_keywords(parts[2]) if len(parts) > 2 else []
        if not kws:
            label = topic_label(state, key)
            pending[chat_id] = {"cmd": "/addkeyword", "step": "keywords", "topic": key,
                                "ts": time.time()}
            send_message(chat_id, f"کلمه‌های کلیدی جدید برای «{label}» رو بفرست (با کاما جدا کن؛ هر کدوم می‌تونه چند کلمه باشه):",
                         reply_markup=CANCEL_KEYBOARD)
            return True
        if key in TOPICS:
            ok, reply = _add_builtin_keywords(state, key, kws)
        else:
            ok, reply = _add_custom_keywords(state, key, kws)
        send_message(chat_id, reply, reply_markup=main_keyboard(chat_id, state))
        return True
    if text == "/broadcast" or text.startswith("/broadcast "):
        if chat_id != ADMIN_CHAT_ID:
            send_message(chat_id, "این دستور فقط برای ادمینه.", reply_markup=main_keyboard(chat_id, state))
            return True
        if text == "/broadcast":
            pending[chat_id] = {"cmd": "/broadcast", "step": "target", "ts": time.time()}
            send_message(chat_id, "پیام به کی برسه؟",
                         reply_markup=_broadcast_target_keyboard(state))
            return True
        arg = text.split(None, 1)[1].strip()
        topic, msg = _broadcast_target(arg, state.get("custom_topics", {}), state.get("topic_labels", {}))
        if topic and not msg:
            send_message(chat_id, "متن پیام رو هم بنویس:\n/broadcast <ایموجی موضوع> <متن>",
                         reply_markup=main_keyboard(chat_id, state))
            return True
        if topic:
            sent, failed = _broadcast(state, msg, topic)
        else:
            sent, failed = _broadcast(state, arg)
        send_message(chat_id, f"به {sent} نفر رسید، {failed} نفر ناموفق بود.",
                     reply_markup=main_keyboard(chat_id, state))
        return True
    if text == "/send" or text.startswith("/send "):
        if text == "/send":
            pending[chat_id] = {"cmd": "/send", "ts": time.time()}
            send_message(chat_id, "خبر، عکس یا فیلمت رو بفرست 🙂", reply_markup=main_keyboard(chat_id, state))
            return True
        arg = text.split(None, 1)[1].strip()
        if not arg:
            pending[chat_id] = {"cmd": "/send", "ts": time.time()}
            send_message(chat_id, "خبر، عکس یا فیلمت رو بفرست 🙂", reply_markup=main_keyboard(chat_id, state))
            return True
        return _submit_news(state, msg, chat_id, direct_text=arg)
    if text == "/follow":
        _open_select_picker(state, chat_id, "کدوم موضوع رو می‌خوای دنبال کنی؟")
        return True
    if text.startswith("/follow "):
        topic = _norm_topic(text.split(None, 1)[1], state.get("custom_topics", {}), state.get("topic_labels", {}))
        if not topic:
            topics_list = "، ".join(topic_label(state, k) for k in active_topics(state))
            send_message(chat_id, f"این موضوع رو نمی‌شناسم. موضوع‌ها: {topics_list}",
                         reply_markup=main_keyboard(chat_id, state))
            return True
        if topic in (state.get("disabled_topics") or []):
            send_message(chat_id, "این موضوع حذف شده.",
                         reply_markup=main_keyboard(chat_id, state))
            return True
        if _follow(state, chat_id, topic):
            send_message(chat_id, f"دنبال کردن «{topic_label(state, topic)}» فعال شد ✅\nخبرهای مهمش دایرکت برات میاد.",
                         reply_markup=main_keyboard(chat_id, state))
        else:
            send_message(chat_id, f"قبلاً «{topic_label(state, topic)}» رو دنبال می‌کردی 🙂",
                         reply_markup=main_keyboard(chat_id, state))
        return True
    if text == "/unfollow":
        topics = state.get("users", {}).get(chat_id, {}).get("topics", [])
        if not topics:
            send_message(chat_id, "هیچ موضوعی رو دنبال نمی‌کنی که لغوش کنی 🙂",
                         reply_markup=main_keyboard(chat_id, state))
            return True
        pending[chat_id] = {"cmd": "/unfollow", "ts": time.time()}
        send_message(chat_id, "کدوم موضوع رو لغو کنم؟", reply_markup=unfollow_keyboard(topics, state))
        return True
    if text.startswith("/unfollow "):
        arg = text.split(None, 1)[1].strip()
        if arg == "همه":
            state.get("users", {}).get(chat_id, {})["topics"] = []
            send_message(chat_id, "دنبال کردن همه‌ی موضوع‌ها لغو شد.", reply_markup=main_keyboard(chat_id, state))
            return True
        topic = _norm_topic(arg, state.get("custom_topics", {}), state.get("topic_labels", {}))
        if not topic:
            send_message(chat_id, "این موضوع رو نمی‌شناسم.", reply_markup=main_keyboard(chat_id, state))
            return True
        if _unfollow(state, chat_id, topic):
            send_message(chat_id, f"دنبال کردن «{topic_label(state, topic)}» لغو شد.", reply_markup=main_keyboard(chat_id, state))
        else:
            send_message(chat_id, f"«{topic_label(state, topic)}» رو دنبال نمی‌کردی.", reply_markup=main_keyboard(chat_id, state))
        return True
    if entry:
        # جوابِ دستوری که منتظر آرگومان بود (/follow، /unfollow، /broadcast یا /send)
        cmd = entry["cmd"]
        pending.pop(chat_id, None)
        if cmd == "/removekeyword":
            if entry.get("step") == "topic":
                topic = _norm_topic(text, state.get("custom_topics", {}), state.get("topic_labels", {}))
                if not topic or topic not in active_topics(state) + list(state.get("custom_topics", {})):
                    send_message(chat_id, "این موضوع رو نمی‌شناسم؛ روی یکی از دکمه‌ها بزن:",
                                 reply_markup=topic_keyboard(state))
                    pending[chat_id] = entry
                    return True
                label = topic_label(state, topic)
                entry["step"] = "keyword"
                entry["topic"] = topic
                entry["ts"] = time.time()
                pending[chat_id] = entry
                send_message(chat_id, f"کدوم کلمه‌ی «{label}» رو حذف کنم؟",
                             reply_markup=_keyword_keyboard(state, topic))
                return True
            # قدم کلمه: متن پیام (دکمه یا تایپ) باید دقیقاً یکی از کلمه‌ها باشه
            topic = entry.get("topic")
            if not topic or text not in _topic_keywords(state, topic):
                send_message(chat_id, "این کلمه توی این موضوع نیست؛ روی یکی از دکمه‌ها بزن:",
                             reply_markup=_keyword_keyboard(state, topic))
                pending[chat_id] = entry
                return True
            ok, reply = _remove_keyword(state, topic, text)
            send_message(chat_id, reply, reply_markup=main_keyboard(chat_id, state))
            return True
        if cmd == "/addkeyword":
            if entry.get("step") == "topic":
                topic = _norm_topic(text, state.get("custom_topics", {}), state.get("topic_labels", {}))
                if not topic or topic not in active_topics(state) + list(state.get("custom_topics", {})):
                    send_message(chat_id, "این موضوع رو نمی‌شناسم؛ روی یکی از دکمه‌ها بزن:",
                                 reply_markup=topic_keyboard(state))
                    pending[chat_id] = entry
                    return True
                label = topic_label(state, topic)
                entry["step"] = "keywords"
                entry["topic"] = topic
                entry["ts"] = time.time()
                pending[chat_id] = entry
                send_message(chat_id, f"کلمه‌های کلیدی جدید برای «{label}» رو بفرست (با کاما جدا کن؛ هر کدوم می‌تونه چند کلمه باشه):",
                             reply_markup=CANCEL_KEYBOARD)
                return True
            # قدم کلمه: کلمه‌های جدید رو به موضوع اضافه می‌کنه
            topic = entry.get("topic")
            kws = _parse_keywords(text)
            if not topic or not kws:
                send_message(chat_id, "کلمه‌ی کلیدی‌ای نفرستادی؛ کلمه‌های کلیدی رو بفرست:",
                             reply_markup=CANCEL_KEYBOARD)
                pending[chat_id] = entry
                return True
            if topic in TOPICS:
                ok, reply = _add_builtin_keywords(state, topic, kws)
            else:
                ok, reply = _add_custom_keywords(state, topic, kws)
            send_message(chat_id, reply, reply_markup=main_keyboard(chat_id, state))
            return True
        if cmd == "/select":
            # زدن یه موضوع توی «انتخاب موضوع»: دنبال کردن روشن/خاموش می‌شه (تاگل)
            t = text[2:].strip() if text.startswith("✅ ") else text
            topic = _norm_topic(t, state.get("custom_topics", {}), state.get("topic_labels", {}))
            if not topic or (topic not in TOPICS and topic not in state.get("custom_topics", {})):
                send_message(chat_id, "این موضوع رو نمی‌شناسم؛ روی یکی از دکمه‌ها بزن:",
                             reply_markup=_select_topics_keyboard(state, chat_id)
                             or main_keyboard(chat_id, state))
                pending[chat_id] = entry
                return True
            if topic in (state.get("disabled_topics") or []):
                send_message(chat_id, "این موضوع حذف شده.",
                             reply_markup=_select_topics_keyboard(state, chat_id)
                             or main_keyboard(chat_id, state))
                pending[chat_id] = entry
                return True
            entry["ts"] = time.time()
            pending[chat_id] = entry
            label = topic_label(state, topic)
            if topic in (state.get("users", {}).get(chat_id, {}).get("topics", [])):
                _unfollow(state, chat_id, topic)
                send_message(chat_id, f"دنبال کردن «{label}» لغو شد.",
                             reply_markup=_select_topics_keyboard(state, chat_id))
            else:
                _follow(state, chat_id, topic)
                send_message(chat_id, f"دنبال کردن «{label}» فعال شد ✅\nخبرهای مهمش دایرکت برات میاد.",
                             reply_markup=_select_topics_keyboard(state, chat_id))
            return True
        if cmd == "/unfollow":
            if text == "همه":
                state.get("users", {}).get(chat_id, {})["topics"] = []
                send_message(chat_id, "دنبال کردن همه‌ی موضوع‌ها لغو شد.", reply_markup=main_keyboard(chat_id, state))
                return True
            topic = _norm_topic(text, state.get("custom_topics", {}), state.get("topic_labels", {}))
            if not topic:
                send_message(chat_id, "این موضوع رو نمی‌شناسم.", reply_markup=main_keyboard(chat_id, state))
                return True
            if _unfollow(state, chat_id, topic):
                send_message(chat_id, f"دنبال کردن «{topic_label(state, topic)}» لغو شد.", reply_markup=main_keyboard(chat_id, state))
            else:
                send_message(chat_id, f"«{topic_label(state, topic)}» رو دنبال نمی‌کردی.", reply_markup=main_keyboard(chat_id, state))
            return True
        if cmd == "/broadcast":
            if entry.get("step") == "target":
                if not text:
                    send_message(chat_id, "پیام به کی برسه؟",
                                 reply_markup=_broadcast_target_keyboard(state))
                    pending[chat_id] = entry
                    return True
                if text == "👥 به همه":
                    entry["step"] = "text"
                    entry["topic"] = None
                    entry["ts"] = time.time()
                    pending[chat_id] = entry
                    send_message(chat_id, "متن پیام رو بفرست (به همه‌ی اعضا):",
                                 reply_markup=CANCEL_KEYBOARD)
                    return True
                topic = _norm_topic(text, state.get("custom_topics", {}), state.get("topic_labels", {}))
                if not topic or topic not in active_topics(state) + list(state.get("custom_topics", {})):
                    send_message(chat_id, "این موضوع رو نمی‌شناسم؛ روی یکی از دکمه‌ها بزن:",
                                 reply_markup=_broadcast_target_keyboard(state))
                    pending[chat_id] = entry
                    return True
                entry["step"] = "text"
                entry["topic"] = topic
                entry["ts"] = time.time()
                pending[chat_id] = entry
                send_message(chat_id, f"متن پیام برای «{topic_label(state, topic)}» رو بفرست:",
                             reply_markup=CANCEL_KEYBOARD)
                return True
            if not text:
                # قدم متن ولی متن نفرستاده (مثلاً عکس) — دوباره همون قدم رو بخواه
                topic = entry.get("topic")
                if topic:
                    prompt = f"متن پیام برای «{topic_label(state, topic)}» رو بفرست:"
                else:
                    prompt = "متن پیام رو بفرست (به همه‌ی اعضا):"
                send_message(chat_id, prompt, reply_markup=CANCEL_KEYBOARD)
                pending[chat_id] = entry
                return True
            topic = entry.get("topic")
            if topic:
                sent, failed = _broadcast(state, text, topic)
            else:
                sent, failed = _broadcast(state, text)
            send_message(chat_id, f"به {sent} نفر رسید، {failed} نفر ناموفق بود.",
                         reply_markup=main_keyboard(chat_id, state))
            return True
        if cmd == "/send":
            return _submit_news(state, msg, chat_id)
        if cmd == "/addtopic":
            if entry.get("step") == "keywords":
                kws = _parse_keywords(text)
                if not kws:
                    send_message(chat_id, "کلمه‌ی کلیدی‌ای نفرستادی؛ کلمه‌های کلیدی رو بفرست:",
                                 reply_markup=CANCEL_KEYBOARD)
                    pending[chat_id] = entry
                    return True
                if entry.get("builtin_key"):
                    ok, reply = _add_builtin_keywords(state, entry["builtin_key"], kws)
                else:
                    ok, reply = _add_custom_topic(state, entry.get("name", ""), kws)
                send_message(chat_id, reply, reply_markup=main_keyboard(chat_id, state))
                return True
            if not entry.get("step"):
                # جریان قدیمی: کل پیام «نام کلمه۱ کلمه۲ ...» بود
                parts = text.split()
                ok, reply = _add_custom_topic(state, parts[0] if parts else "", parts[1:])
                send_message(chat_id, reply, reply_markup=main_keyboard(chat_id, state))
                return True
            # قدم اول جادوگر: کل پیام، نام موضوعه (حتی چندکلمه‌ای و حتی با ایموجی اولش)
            name = text.strip()
            key = _norm_topic(name, state.get("custom_topics", {}), state.get("topic_labels", {}))
            if key in TOPICS:
                if key in (state.get("disabled_topics") or []):
                    ok, reply = _restore_builtin(state, key)
                    send_message(chat_id, reply, reply_markup=main_keyboard(chat_id, state))
                    return True
                entry["step"] = "keywords"
                entry["builtin_key"] = key
                entry["ts"] = time.time()
                pending[chat_id] = entry
                send_message(chat_id, f"«{topic_label(state, key)}» موضوع اصلیه؛ کلمه‌های کلیدی که می‌خوای بهش اضافه بشه رو بفرست (با کاما جدا کن؛ هر کدوم می‌تونه چند کلمه باشه):",
                             reply_markup=CANCEL_KEYBOARD)
                return True
            if not name or name in state.get("custom_topics", {}):
                send_message(chat_id, "این نام رو نمی‌شه اضافه کرد؛ یه نام دیگه بفرست:",
                             reply_markup=CANCEL_KEYBOARD)
                pending[chat_id] = entry
                return True
            entry["step"] = "keywords"
            entry["name"] = name
            entry["ts"] = time.time()
            pending[chat_id] = entry
            send_message(chat_id, "کلمه‌های کلیدی رو بفرست (با کاما جدا کن؛ هر کدوم می‌تونه چند کلمه باشه):",
                         reply_markup=CANCEL_KEYBOARD)
            return True
        if cmd == "/removetopic":
            if entry.get("step") == "pick":
                topic = _norm_topic(text, state.get("custom_topics", {}), state.get("topic_labels", {}))
                if topic in (state.get("disabled_topics") or []):
                    ok, reply = _restore_builtin(state, topic)
                elif topic in TOPICS:
                    ok, reply = _disable_builtin(state, topic)
                elif topic in state.get("custom_topics", {}):
                    ok, reply = _remove_custom_topic(state, topic)
                else:
                    kb = remove_topic_keyboard(state) or main_keyboard(chat_id, state)
                    send_message(chat_id, "این موضوع رو نمی‌شناسم؛ روی یکی از دکمه‌ها بزن:",
                                 reply_markup=kb)
                    pending[chat_id] = entry
                    return True
                send_message(chat_id, reply, reply_markup=main_keyboard(chat_id, state))
                return True
            ok, reply = _remove_custom_topic(state, text.strip())
            send_message(chat_id, reply, reply_markup=main_keyboard(chat_id, state))
            return True
        if cmd == "/renametopic":
            if entry.get("step") == "pick":
                topic = _norm_topic(text, state.get("custom_topics", {}), state.get("topic_labels", {}))
                if not topic or (topic not in TOPICS and topic not in state.get("custom_topics", {})):
                    kb = rename_topic_keyboard(state) or main_keyboard(chat_id, state)
                    send_message(chat_id, "این موضوع رو نمی‌شناسم؛ روی یکی از دکمه‌ها بزن:",
                                 reply_markup=kb)
                    pending[chat_id] = entry
                    return True
                if topic in (state.get("disabled_topics") or []):
                    kb = rename_topic_keyboard(state) or main_keyboard(chat_id, state)
                    send_message(chat_id, "این موضوع حذف شده؛ یه موضوع دیگه رو انتخاب کن:",
                                 reply_markup=kb)
                    pending[chat_id] = entry
                    return True
                entry["step"] = "newname"
                entry["topic"] = topic
                entry["ts"] = time.time()
                pending[chat_id] = entry
                send_message(chat_id, f"اسم جدید «{topic_label(state, topic)}» چیه؟",
                             reply_markup=CANCEL_KEYBOARD)
                return True
            # قدم اسم جدید
            old = entry.get("topic")
            if not old or (old not in TOPICS and old not in state.get("custom_topics", {})):
                send_message(chat_id, "اون موضوع دیگه نیست.",
                             reply_markup=main_keyboard(chat_id, state))
                return True
            new = text.strip()
            if not new:
                send_message(chat_id, "اسم جدید رو بفرست:",
                             reply_markup=CANCEL_KEYBOARD)
                pending[chat_id] = entry
                return True
            ok, reply = _rename_topic(state, old, new)
            if not ok:
                send_message(chat_id, reply + "\nیه اسم دیگه بفرست:",
                             reply_markup=CANCEL_KEYBOARD)
                pending[chat_id] = entry
                return True
            send_message(chat_id, reply, reply_markup=main_keyboard(chat_id, state))
            return True
    # زدن دکمه‌ی موضوع روی کیبورد اصلی = دنبال کردن همون موضوع
    topic = _norm_topic(text, state.get("custom_topics", {}), state.get("topic_labels", {}))
    if topic:
        if topic in (state.get("disabled_topics") or []):
            send_message(chat_id, "این موضوع حذف شده.",
                         reply_markup=main_keyboard(chat_id, state))
            return True
        if _follow(state, chat_id, topic):
            send_message(chat_id, f"دنبال کردن «{topic_label(state, topic)}» فعال شد ✅\nخبرهای مهمش دایرکت برات میاد.",
                         reply_markup=main_keyboard(chat_id, state))
        else:
            send_message(chat_id, f"قبلاً «{topic_label(state, topic)}» رو دنبال می‌کردی 🙂",
                         reply_markup=main_keyboard(chat_id, state))
        return True
    if text.startswith("/"):
        send_message(chat_id, "این دستور رو نمی‌شناسم. /help رو بزن تا راهنما رو ببینی.",
                     reply_markup=main_keyboard(chat_id, state))
        return True
    # هر پیام دیگه (متن، عکس، فیلم) = ارسال خبر برای ادمین
    return _submit_news(state, msg, chat_id)


# ====== پست‌های کانال ======
def _today_tehran():
    """تاریخ امروز به وقت تهران (برای صفر شدن روزانه‌ی آمار ارسال)."""
    return (datetime.now(timezone.utc) + timedelta(hours=3, minutes=30)).strftime("%Y-%m-%d")


def _reset_forward_counts_if_new_day(state):
    """اگه روز عوض شده باشه، آمار ارسالِ موضوع‌ها صفر می‌شه؛ از صبح، با شروع
    خبرهای کانال، شمارش از اول شروع می‌شه (نسخه‌ی ۰.۱۸)."""
    today = _today_tehran()
    if state.get("topic_forward_day") != today:
        state["topic_forward_counts"] = {}
        state["topic_forward_day"] = today


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
    topics = classify_post(text, state.get("custom_topics", {}),
                           state.get("topic_extra_keywords", {}),
                           state.get("disabled_topics", []),
                           state.get("topic_removed_keywords", {}))
    seen.append(post_id)
    if not topics:
        return True   # پست معمولی: نه فوروارد می‌شه، نه دوباره بررسی می‌شه
    targets = set()
    for uid, u in state.get("users", {}).items():
        if set(u.get("topics", [])) & topics:
            targets.add(uid)
    sent = 0
    sent_uids = set()
    for uid in sorted(targets):
        res = forward_to(uid, post_id)
        if res:
            sent += 1
            sent_uids.add(uid)
            _record_sent(state, uid, res.get("message_id"))
        time.sleep(0.05)   # که به سقف سرعت تلگرام نخوریم
    if sent_uids:
        # برای هر موضوعی که دست‌کم یه دنبال‌کننده‌ش خبر رو گرفته، یکی به شمارنده‌ش اضافه می‌شه
        _reset_forward_counts_if_new_day(state)
        counts = state.setdefault("topic_forward_counts", {})
        users = state.get("users", {})
        for t in topics:
            if any(t in (users.get(uid) or {}).get("topics", []) for uid in sent_uids):
                counts[t] = counts.get(t, 0) + 1
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
    changed = process_updates(state)
    if _sweep_followers(state):
        changed = True
    if changed:
        save_state(state)
    return state


# ====== گیت‌هاب: کامیت حافظه + صدا زدن اجرای بعدی ======
def commit_state():
    """حافظه‌ی رمزنگاری‌شده رو توی گیت‌هاب کامیت می‌کنه (فقط وقتی توی Actions هستیم).
    اگه pull با تداخل مواجه بشه، یا فایل نشونه‌های تداخل داشته باشه، یا رمزگشایی
    نشه، این دور کامیت نمی‌شه — فایل خراب هیچ‌وقت کامیت نمی‌شه؛ همون خطایی که
    ربات رو از کار می‌نداخت (فایل درگیرِ تداخل رمزگشایی نمی‌شه)."""
    if not os.environ.get("GITHUB_ACTIONS"):
        return
    def git(*args):
        return subprocess.run(["git", *args], capture_output=True, text=True)
    try:
        git("config", "user.name", "news-bot")
        git("config", "user.email", "bot@users.noreply.github.com")
        _pull = git("pull", "--rebase", "--autostash")
        if _pull.returncode != 0:
            print("git pull --rebase failed, skipping state commit")
            git("rebase", "--abort")
            return
        try:
            with open(STATE_FILE, encoding="utf-8") as f:
                content = f.read()
        except OSError:
            print("followers.json not found, skipping state commit")
            return
        if "<<<<<<<" in content or ">>>>>>>" in content:
            print("merge markers found in followers.json, skipping commit")
            return
        try:
            _fernet().decrypt(content.strip().encode("utf-8"))
        except Exception:
            print("followers.json does not decrypt, skipping commit")
            return
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
