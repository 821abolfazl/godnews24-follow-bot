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

BOT_VERSION = "2026-11-16-0.01"   # توی لاگ و /start نوشته می‌شه تا معلوم باشه کدوم نسخه در حال اجراست

TOKEN = os.environ.get("FOLLOW_BOT_TOKEN") or ""
ADMIN_CHAT_ID = os.environ.get("ADMIN_CHAT_ID") or ""
CHANNEL = os.environ.get("CHANNEL") or "@GodNews24"
LOOP_MINUTES = int(os.environ.get("LOOP_MINUTES") or 0)

STATE_FILE = "followers.json"
POLL_SECONDS = 10            # هر چند ثانیه getUpdates خونده بشه (در حالت حلقه)
PENDING_TIMEOUT = 600        # ثانیه؛ اگه عضو تا این مدت جواب نده، انتظار لغو می‌شه
COMMIT_EVERY_SEC = 300       # حافظه حداکثر هر ۵ دقیقه توی گیت‌هاب کامیت می‌شه

# ====== موضوع‌ها ======
# کلید داخلی → برچسب فارسی (با ایموجی، همون ایموجی اول پست‌های کانال)
TOPICS = {
    "political": "🔴 سیاسی",
    "sport": "⚽ ورزشی",
    "gold": "🟡 طلا و دلار",
}
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
    state = {"users": {}, "seen_posts": [], "tg_offset": 0, "pending": {}}
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
    try:
        r = requests.get(f"https://api.telegram.org/bot{TOKEN}/getUpdates",
                         params={"offset": offset, "timeout": 0,
                                 "allowed_updates": json.dumps(["message", "channel_post"])},
                         timeout=20)
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


# ====== کیبوردها ======
def main_keyboard():
    rows = [[{"text": TOPICS["political"]}, {"text": TOPICS["sport"]}],
            [{"text": TOPICS["gold"]}, {"text": "📋 موضوع‌های من"}],
            [{"text": "❓ راهنما"}]]
    return {"keyboard": rows, "resize_keyboard": True}


def topic_keyboard():
    rows = [[{"text": TOPICS["political"]}, {"text": TOPICS["sport"]}],
            [{"text": TOPICS["gold"]}],
            [{"text": "❌ لغو"}]]
    return {"keyboard": rows, "resize_keyboard": True}


def unfollow_keyboard(followed):
    rows = [[{"text": TOPICS[k]}] for k in followed if k in TOPICS]
    rows.append([{"text": "همه"}, {"text": "❌ لغو"}])
    return {"keyboard": rows, "resize_keyboard": True}


# ====== دسته‌بندی پست کانال ======
def leading_emoji(text):
    """ایموجی اول متن پست (همون ایموجی‌ای که ربات اصلی اول خبر می‌ذاره)."""
    t = (text or "").lstrip()
    for e in ("🔴", "🚨", "🔥", "⚽", "🟡"):
        if t.startswith(e):
            return e
    return ""


def classify_post(text):
    """موضوع‌های یه پست کانال رو برمی‌گردونه (مجموعه‌ای از کلیدهای TOPICS).
    فقط پست‌های مهم/موضوع‌دار برمی‌گردن؛ پست معمولی (🔵) هیچ موضوعی نمی‌گیره."""
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
    return out


def _norm_topic(raw):
    """«🔴 سیاسی» یا «سیاسی» یا «political» → کلید داخلی؛ وگرنه None."""
    s = (raw or "").strip()
    s = re.sub(r"^[\U0001F300-\U0001FAFF\u2600-\u27BF\uFE0F\s]+", "", s).strip()
    if s in TOPIC_NAMES:
        return TOPIC_NAMES[s]
    if s in TOPICS:
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
    "/help — همین راهنما\n\n"
    "خبرهای مهم موضوع‌هایی که دنبال می‌کنی، دایرکت برات میاد؛ "
    "بقیه‌ی خبرها فقط توی کانال @GodNews24 هستن."
)


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
    text = (msg.get("text") or "").strip()
    if not chat_id or not text:
        return False
    pending = state.setdefault("pending", {})
    entry = pending.get(chat_id)
    if entry and time.time() - float(entry.get("ts") or 0) > PENDING_TIMEOUT:
        pending.pop(chat_id, None)
        entry = None

    if text in ("لغو", "❌ لغو"):
        if entry:
            pending.pop(chat_id, None)
            send_message(chat_id, "لغو شد.", reply_markup=main_keyboard())
        return True

    if text == "/start":
        pending.pop(chat_id, None)
        send_message(chat_id, WELCOME_TEXT, reply_markup=main_keyboard())
        return True
    if text == "/help" or text == "❓ راهنما":
        pending.pop(chat_id, None)
        send_message(chat_id, HELP_TEXT, reply_markup=main_keyboard())
        return True
    if text == "/topics" or text == "📋 موضوع‌های من":
        pending.pop(chat_id, None)
        topics = state.get("users", {}).get(chat_id, {}).get("topics", [])
        if not topics:
            send_message(chat_id, "هنوز هیچ موضوعی رو دنبال نمی‌کنی.\nبا /follow یه موضوع انتخاب کن 🙂",
                         reply_markup=main_keyboard())
        else:
            lines = "\n".join("• " + TOPICS[k] for k in topics if k in TOPICS)
            send_message(chat_id, "موضوع‌هایی که دنبال می‌کنی:\n" + lines,
                         reply_markup=main_keyboard())
        return True
    if text == "/followers":
        if chat_id != ADMIN_CHAT_ID:
            send_message(chat_id, "این دستور فقط برای ادمینه.", reply_markup=main_keyboard())
            return True
        counts = {k: 0 for k in TOPICS}
        total = 0
        for u in state.get("users", {}).values():
            ts = u.get("topics", [])
            if ts:
                total += 1
            for k in ts:
                if k in counts:
                    counts[k] += 1
        lines = "\n".join(f"{TOPICS[k]}: {counts[k]}" for k in TOPICS)
        send_message(chat_id, f"دنبال‌کننده‌ها (کل: {total} نفر):\n{lines}",
                     reply_markup=main_keyboard())
        return True
    if text == "/follow":
        pending[chat_id] = {"cmd": "/follow", "ts": time.time()}
        send_message(chat_id, "کدوم موضوع رو می‌خوای دنبال کنی؟", reply_markup=topic_keyboard())
        return True
    if text.startswith("/follow "):
        topic = _norm_topic(text.split(None, 1)[1])
        if not topic:
            send_message(chat_id, "این موضوع رو نمی‌شناسم. موضوع‌ها: سیاسی، ورزشی، طلا و دلار",
                         reply_markup=main_keyboard())
            return True
        if _follow(state, chat_id, topic):
            send_message(chat_id, f"دنبال کردن «{TOPICS[topic]}» فعال شد ✅\nخبرهای مهمش دایرکت برات میاد.",
                         reply_markup=main_keyboard())
        else:
            send_message(chat_id, f"قبلاً «{TOPICS[topic]}» رو دنبال می‌کردی 🙂",
                         reply_markup=main_keyboard())
        return True
    if text == "/unfollow":
        topics = state.get("users", {}).get(chat_id, {}).get("topics", [])
        if not topics:
            send_message(chat_id, "هیچ موضوعی رو دنبال نمی‌کنی که لغوش کنی 🙂",
                         reply_markup=main_keyboard())
            return True
        pending[chat_id] = {"cmd": "/unfollow", "ts": time.time()}
        send_message(chat_id, "کدوم موضوع رو لغو کنم؟", reply_markup=unfollow_keyboard(topics))
        return True
    if text.startswith("/unfollow "):
        arg = text.split(None, 1)[1].strip()
        if arg == "همه":
            state.get("users", {}).get(chat_id, {})["topics"] = []
            send_message(chat_id, "دنبال کردن همه‌ی موضوع‌ها لغو شد.", reply_markup=main_keyboard())
            return True
        topic = _norm_topic(arg)
        if not topic:
            send_message(chat_id, "این موضوع رو نمی‌شناسم.", reply_markup=main_keyboard())
            return True
        if _unfollow(state, chat_id, topic):
            send_message(chat_id, f"دنبال کردن «{TOPICS[topic]}» لغو شد.", reply_markup=main_keyboard())
        else:
            send_message(chat_id, f"«{TOPICS[topic]}» رو دنبال نمی‌کردی.", reply_markup=main_keyboard())
        return True
    if entry:
        # جوابِ دستوری که منتظر آرگومان بود (/follow یا /unfollow)
        cmd = entry["cmd"]
        pending.pop(chat_id, None)
        if cmd == "/follow":
            topic = _norm_topic(text)
            if not topic:
                send_message(chat_id, "این موضوع رو نمی‌شناسم. موضوع‌ها: سیاسی، ورزشی، طلا و دلار",
                             reply_markup=main_keyboard())
                return True
            if _follow(state, chat_id, topic):
                send_message(chat_id, f"دنبال کردن «{TOPICS[topic]}» فعال شد ✅\nخبرهای مهمش دایرکت برات میاد.",
                             reply_markup=main_keyboard())
            else:
                send_message(chat_id, f"قبلاً «{TOPICS[topic]}» رو دنبال می‌کردی 🙂",
                             reply_markup=main_keyboard())
            return True
        if cmd == "/unfollow":
            if text == "همه":
                state.get("users", {}).get(chat_id, {})["topics"] = []
                send_message(chat_id, "دنبال کردن همه‌ی موضوع‌ها لغو شد.", reply_markup=main_keyboard())
                return True
            topic = _norm_topic(text)
            if not topic:
                send_message(chat_id, "این موضوع رو نمی‌شناسم.", reply_markup=main_keyboard())
                return True
            if _unfollow(state, chat_id, topic):
                send_message(chat_id, f"دنبال کردن «{TOPICS[topic]}» لغو شد.", reply_markup=main_keyboard())
            else:
                send_message(chat_id, f"«{TOPICS[topic]}» رو دنبال نمی‌کردی.", reply_markup=main_keyboard())
            return True
    # زدن دکمه‌ی موضوع روی کیبورد اصلی = دنبال کردن همون موضوع
    topic = _norm_topic(text)
    if topic:
        if _follow(state, chat_id, topic):
            send_message(chat_id, f"دنبال کردن «{TOPICS[topic]}» فعال شد ✅\nخبرهای مهمش دایرکت برات میاد.",
                         reply_markup=main_keyboard())
        else:
            send_message(chat_id, f"قبلاً «{TOPICS[topic]}» رو دنبال می‌کردی 🙂",
                         reply_markup=main_keyboard())
        return True
    if text.startswith("/"):
        send_message(chat_id, "این دستور رو نمی‌شناسم. /help رو بزن تا راهنما رو ببینی.",
                     reply_markup=main_keyboard())
        return True
    send_message(chat_id, "برای دنبال کردن یه موضوع، روی دکمه‌ش بزن یا /follow رو بفرست 🙂",
                 reply_markup=main_keyboard())
    return True


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
    topics = classify_post(text)
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
