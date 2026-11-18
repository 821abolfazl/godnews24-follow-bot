# Tests for follow_bot.py (version 2026-11-16-0.02) — stdlib unittest, no network.
# All Telegram calls are monkeypatched; nothing is ever sent for real.
import io
import json
import os
import re
import sys
import tempfile
import time
import unittest
from contextlib import redirect_stdout
from types import SimpleNamespace

os.environ["FOLLOW_BOT_TOKEN"] = "test-token-123"
os.environ["ADMIN_CHAT_ID"] = "999"

sys.path.insert(0, "/home/hatch/workspace/godnews24-follow-bot")
os.chdir("/home/hatch/workspace/godnews24-follow-bot")

import follow_bot as fb

ADMIN = "999"
USER_A = "111"
USER_B = "222"


def make_message(chat_id, text=None, message_id=1, from_=None, photo=False, video=False):
    m = {"message_id": message_id, "chat": {"id": int(chat_id), "type": "private"}}
    if text is not None:
        m["text"] = text
    if from_ is not None:
        m["from"] = from_
    if photo:
        m["photo"] = [{"file_id": "photo-file-id"}]
        m.setdefault("caption", "")
    if video:
        m["video"] = {"file_id": "video-file-id"}
        m.setdefault("caption", "")
    return m


def tehran_date(days_ago=0):
    from datetime import datetime, timedelta, timezone
    return (datetime.now(timezone.utc) + timedelta(hours=3, minutes=30)
            - timedelta(days=days_ago)).strftime("%Y-%m-%d")


def make_channel_post(mid, text):
    return {"message_id": mid,
            "chat": {"id": -100123456, "username": "GodNews24", "type": "channel"},
            "text": text}


class FakeAPI:
    """Captures every Telegram call instead of hitting the network."""

    def __init__(self):
        self.sent = []        # (chat_id, text, reply_markup?)
        self.forwarded = []   # (chat_id, post_id)
        self.forward_calls = []  # full forwardMessage data dicts
        self.deleted = []     # (chat_id, message_id)
        self.delete_fail_ids = set()  # message_ids deleteMessage fails on
        self._mid = 0         # message ids grow like the real API
        self.member_status = "member"
        self.member_calls = []   # user_ids checked via getChatMember
        self.raise_on_member = False
        self.send_failures = {}  # chat_id -> "none" | "raise"
        self.updates = []

    def _next_id(self):
        self._mid += 1
        return self._mid

    def api(self, method, **data):
        if method == "sendMessage":
            chat_id = str(data.get("chat_id"))
            fail = self.send_failures.get(chat_id)
            if fail == "raise":
                raise RuntimeError("telegram down")
            if fail == "none":
                return None
            self.sent.append((chat_id, data.get("text", ""),
                              data.get("reply_markup")))
            return {"message_id": self._next_id()}
        if method == "forwardMessage":
            self.forwarded.append((str(data.get("chat_id")), data.get("message_id")))
            self.forward_calls.append(dict(data))
            return {"message_id": self._next_id()}
        if method == "deleteMessage":
            mid = data.get("message_id")
            self.deleted.append((str(data.get("chat_id")), mid))
            if mid in self.delete_fail_ids:
                return None
            return True
        if method == "getChatMember":
            self.member_calls.append(str(data.get("user_id")))
            if self.raise_on_member:
                raise RuntimeError("network down")
            if self.member_status is None:
                return None
            return {"status": self.member_status}
        return {}

    def api_get_updates(self, offset):
        return list(self.updates)


class FollowBotTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp()
        self.state_path = os.path.join(self.tmp, "followers.json")
        self._orig_state_file = fb.STATE_FILE
        fb.STATE_FILE = self.state_path
        self.fake = FakeAPI()
        self._orig_api = fb.api
        self._orig_get = fb.api_get_updates
        fb.api = self.fake.api
        fb.api_get_updates = self.fake.api_get_updates
        fb._MEMBER_CACHE.clear()
        fb._SUBMIT_LAST.clear()
        self.state = fb.load_state()

    def tearDown(self):
        fb.api = self._orig_api
        fb.api_get_updates = self._orig_get
        fb.STATE_FILE = self._orig_state_file

    def send(self, chat_id, text):
        fb.handle_message(self.state, make_message(chat_id, text))

    def send_as(self, chat_id, text, first_name=None, username=None, message_id=1):
        frm = {}
        if first_name:
            frm["first_name"] = first_name
        if username:
            frm["username"] = username
        fb.handle_message(self.state, make_message(
            chat_id, text, message_id=message_id, from_=frm or None))

    def send_msg(self, msg):
        fb.handle_message(self.state, msg)

    def last_text(self):
        return self.fake.sent[-1][1] if self.fake.sent else ""

    # ---- version / start / help ----
    def test_version(self):
        self.assertEqual(fb.BOT_VERSION, "2026-11-19-0.31")

    # ---- 0.12: add keywords to existing topics, inside /keywords ----
    def test_add_custom_keywords_appends_and_dedupes(self):
        self.send(ADMIN, "/addtopic سینما فیلم سینما")
        ok, reply = fb._add_custom_keywords(self.state, "سینما", ["بازیگر", "فیلم"])
        self.assertTrue(ok)
        self.assertIn("بازیگر", reply)
        self.assertEqual(self.state["custom_topics"]["سینما"]["keywords"],
                         ["فیلم", "سینما", "بازیگر"])
        ok, reply = fb._add_custom_keywords(self.state, "سینما", ["فیلم"])
        self.assertFalse(ok)
        self.assertIn("از قبل", reply)
        ok, reply = fb._add_custom_keywords(self.state, "غذا", ["پیتزا"])
        self.assertFalse(ok)

    def test_addtopic_appends_to_existing_custom(self):
        self.send(ADMIN, "/addtopic سینما فیلم سینما")
        self.send(ADMIN, "/addtopic سینما بازیگر کارگردان")
        self.assertIn("اضافه شد", self.last_text())
        self.assertEqual(self.state["custom_topics"]["سینما"]["keywords"],
                         ["فیلم", "سینما", "بازیگر", "کارگردان"])
        self.send(ADMIN, "/addtopic سینما")
        self.assertIn("از قبل هست", self.last_text())

    def test_addkeyword_flow(self):
        self.send(ADMIN, "/addtopic سینما فیلم سینما")
        self.send(ADMIN, "➕ اضافه کردن کلمه‌ی کلیدی")
        self.assertIn("کلمه رو به کدوم موضوع اضافه کنم؟", self.last_text())
        self.send(ADMIN, "سینما")
        self.assertIn("کلمه‌های کلیدی جدید برای", self.last_text())
        self.send(ADMIN, "بازیگر، کارگردان")
        self.assertIn("اضافه شد", self.last_text())
        self.assertEqual(self.state["custom_topics"]["سینما"]["keywords"],
                         ["فیلم", "سینما", "بازیگر", "کارگردان"])

    def test_addkeyword_direct_command(self):
        self.send(ADMIN, "/addtopic سینما فیلم سینما")
        self.send(ADMIN, "/addkeyword سینما بازیگر")
        self.assertIn("اضافه شد", self.last_text())
        self.assertIn("بازیگر", self.state["custom_topics"]["سینما"]["keywords"])
        self.send(ADMIN, "/addkeyword غذا پیتزا")
        self.assertIn("چنین موضوعی نداریم", self.last_text())

    def test_addkeyword_builtin(self):
        self.send(ADMIN, "/addkeyword سیاسی مجلس")
        self.assertIn("از قبل بودن", self.last_text())
        self.send(ADMIN, "/addkeyword سیاسی برجام")
        self.assertIn("اضافه شد", self.last_text())
        self.assertIn("برجام", self.state.get("topic_extra_keywords", {}).get("political", []))

    def test_keywords_keyboard_has_add_and_remove(self):
        self.send(ADMIN, "/addtopic سینما فیلم سینما")
        self.send(ADMIN, "/keywords")
        markup = json.loads(self.fake.sent[-1][2])
        texts = [b["text"] for row in markup["keyboard"] for b in row]
        # داخل /keywords فقط همون دو دستور + بازگشت هست، نه بقیه‌ی منوی مدیریت
        self.assertEqual(texts, ["➕ اضافه کردن کلمه‌ی کلیدی", "➖ حذف کلمه‌ی کلیدی",
                                 "🔙 منوی اصلی"])
        self.assertEqual(texts.count("➖ حذف کلمه‌ی کلیدی"), 1)

    def test_removekeyword_from_keywords_keyboard(self):
        self.send(ADMIN, "/addtopic سینما فیلم سینما")
        self.send(ADMIN, "/keywords")
        self.send(ADMIN, "➖ حذف کلمه‌ی کلیدی")
        self.assertIn("کلمه‌ی کدوم موضوع رو می‌خوای حذف کنی؟", self.last_text())

    def test_welcome_explains_what_bot_does(self):
        self.send(USER_A, "/start")
        txt = self.last_text()
        self.assertIn("خوش اومدی", txt)
        self.assertIn("ربات شخصی کانال", txt)
        self.assertIn("دایرکت", txt)
        # نسخه‌ی ۰.۱۹: متن خوش‌آمد دیگه دستورها رو لیست نمی‌کنه؛ به جاش به منو اشاره می‌کنه
        self.assertIn("منوی پایین", txt)
        self.assertNotIn("/follow", txt)
        self.assertNotIn("/send", txt)
        self.assertNotIn("/help", txt)

    def test_help_explains_bot_and_commands(self):
        self.send(USER_A, "/help")
        txt = self.last_text()
        self.assertIn("این ربات چیه؟", txt)
        self.assertIn("📌 انتخاب موضوع", txt)
        self.assertIn("📋 موضوع‌های من", txt)
        self.assertIn("📤 ارسال خبر", txt)
        # راهنمای اعضا با دکمه‌های منو توضیح داده می‌شه، نه با کد دستوری
        self.assertNotIn("/follow", txt)
        self.assertNotIn("/help", txt)

    def test_help_command_detail(self):
        self.send(USER_A, "/help /follow")
        txt = self.last_text()
        self.assertIn("دنبال کردن", txt)
        self.assertIn("/follow سیاسی", txt)
        self.assertNotIn("راهنمای ربات خبررسان", txt)
        self.send(USER_A, "/help /topics")
        self.assertIn("موضوع‌هایی که الان دنبال می‌کنی", self.last_text())
        self.send(USER_A, "/help /چیزی‌که‌نیست")
        self.assertIn("راهنمای ربات خبررسان", self.last_text())

    def test_help_admin_command_denied_for_member(self):
        self.send(USER_A, "/help /broadcast")
        self.assertIn("فقط برای ادمینه", self.last_text())

    def test_help_admin_command_detail(self):
        self.send(ADMIN, "/help /broadcast")
        txt = self.last_text()
        self.assertIn("پیام به دنبال‌کننده‌ها", txt)
        self.assertIn("👥 به همه", txt)

    # ---- 0.11: welcome lists custom topics, no 📌, broadcast picker, forward counts ----
    def test_welcome_lists_custom_topics(self):
        self.send(ADMIN, "➕ موضوع جدید")
        self.send(ADMIN, "🚨 جنگ")
        self.send(ADMIN, "شلیک، موشک")
        self.send(ADMIN, "/addtopic سینما فیلم سینما")
        self.send(USER_A, "/start")
        txt = self.last_text()
        self.assertIn("🔴 سیاسی", txt)
        self.assertIn("⚽ ورزشی", txt)
        self.assertIn("🟡 طلا و دلار", txt)
        self.assertIn("🚨 جنگ", txt)
        self.assertIn("سینما", txt)
        self.assertNotIn("📌 🚨 جنگ", txt)
        self.assertNotIn("📌 سینما", txt)

    def test_no_pin_in_topic_labels(self):
        self.send(ADMIN, "➕ موضوع جدید")
        self.send(ADMIN, "🚨 جنگ")
        self.send(ADMIN, "شلیک، موشک")
        kb = fb._select_topics_keyboard(self.state, USER_A)
        texts = [b["text"] for row in kb["keyboard"] for b in row]
        self.assertIn("🚨 جنگ", texts)
        self.assertNotIn("📌 🚨 جنگ", texts)
        tk = fb.topic_keyboard(self.state)
        ttexts = [b["text"] for row in tk["keyboard"] for b in row]
        self.assertIn("🚨 جنگ", ttexts)
        self.assertNotIn("📌 🚨 جنگ", ttexts)
        rk = fb.remove_topic_keyboard(self.state)
        rtexts = [b["text"] for row in rk["keyboard"] for b in row]
        self.assertIn("🚨 جنگ", rtexts)
        self.assertNotIn("📌 🚨 جنگ", rtexts)
        self.assertEqual(fb.topic_label(self.state, "🚨 جنگ"), "🚨 جنگ")
        self.assertNotIn("📌", fb._keywords_text(self.state))

    def test_broadcast_custom_topic_by_own_label(self):
        self.send(ADMIN, "➕ موضوع جدید")
        self.send(ADMIN, "🚨 جنگ")
        self.send(ADMIN, "شلیک، موشک")
        self.send(USER_A, "/follow 🚨 جنگ")
        self.send(USER_B, "/follow ورزشی")
        self.send(ADMIN, "/broadcast 🚨 جنگ خبر جنگ")
        got = [s for s in self.fake.sent if s[1] == "خبر جنگ"]
        self.assertEqual([c for c, _, _ in got], [USER_A])
        # 📌 قدیمی هنوز به‌عنوان انتخاب‌کننده‌ی تایپی کار می‌کنه
        self.send(ADMIN, "/broadcast 📌 🚨 جنگ خبر دوم")
        got = [s for s in self.fake.sent if s[1] == "خبر دوم"]
        self.assertEqual([c for c, _, _ in got], [USER_A])

    def test_forward_counts_in_followers(self):
        self.send(USER_A, "/follow سیاسی")
        self.send(USER_B, "/follow ورزشی")
        self.send(ADMIN, "/addtopic سینما فیلم سینما")
        self.send(USER_B, "/follow سینما")
        fb.handle_channel_post(self.state, make_channel_post(790, "🔴 خبر مهم سیاسی"))
        post = make_channel_post(791, "🔵 فیلم تازه اکران شد")
        fb.handle_channel_post(self.state, post)
        fb.handle_channel_post(self.state, post)  # تکراری: دوباره شمرده نمی‌شه
        fb.handle_channel_post(self.state, make_channel_post(792, "🔵 فیلم تازه اکران شد"))
        fb.handle_channel_post(self.state, make_channel_post(793, "⚽ پرسپولیس قهرمان شد"))
        fb.handle_channel_post(self.state, make_channel_post(794, "🟡 قیمت لحظه‌ای طلا و دلار\nطلا: ۱۰۰"))
        self.send(ADMIN, "/followers")
        txt = self.last_text()
        self.assertIn("📊 خبرهای ارسال‌شده برای هر موضوع:", txt)
        tail = txt.split("📊 خبرهای ارسال‌شده برای هر موضوع:")[1]
        self.assertIn("🔴 سیاسی: 1", tail)
        self.assertIn("⚽ ورزشی: 1", tail)
        self.assertIn("🟡 طلا و دلار: 0", tail)
        self.assertIn("سینما: 2", tail)

    def test_broadcast_picker_to_topic(self):
        self.send(USER_A, "/follow سیاسی")
        self.send(USER_B, "/follow ورزشی")
        self.send(ADMIN, "📣 پیام به اعضا")
        self.assertIn("پیام به کی برسه؟", self.last_text())
        markup = json.loads(self.fake.sent[-1][2])
        texts = [b["text"] for row in markup["keyboard"] for b in row]
        self.assertIn("👥 به همه", texts)
        self.assertIn("🔴 سیاسی", texts)
        self.assertIn("⚽ ورزشی", texts)
        self.send(ADMIN, "🔴 سیاسی")
        self.assertIn("متن پیام برای", self.last_text())
        self.send(ADMIN, "خبر فقط سیاسی")
        got = [s for s in self.fake.sent if s[1] == "خبر فقط سیاسی"]
        self.assertEqual([c for c, _, _ in got], [USER_A])
        self.assertIn("به 1 نفر رسید", self.last_text())

    def test_broadcast_picker_to_all(self):
        self.send(USER_A, "/follow سیاسی")
        self.send(USER_B, "/follow ورزشی")
        self.send(ADMIN, "/broadcast")
        self.assertIn("پیام به کی برسه؟", self.last_text())
        self.send(ADMIN, "👥 به همه")
        self.assertIn("متن پیام رو بفرست", self.last_text())
        self.send(ADMIN, "خبر همگانی")
        got = [s for s in self.fake.sent if s[1] == "خبر همگانی"]
        self.assertEqual({c for c, _, _ in got}, {USER_A, USER_B})
        self.assertIn("به 2 نفر رسید", self.last_text())

    # ---- 0.10: custom topics visible to members + keep leading emoji in names ----
    def test_main_keyboard_shows_custom_topics(self):
        self.send(ADMIN, "/addtopic سینما فیلم سینما")
        texts = [b["text"] for row in fb._select_topics_keyboard(self.state, USER_A)["keyboard"] for b in row]
        self.assertIn("سینما", texts)
        self.assertNotIn("📌 سینما", texts)
        self.assertIn("🔴 سیاسی", texts)

    def test_wizard_keeps_leading_emoji_in_name(self):
        self.send(ADMIN, "➕ موضوع جدید")
        self.send(ADMIN, "🚨 جنگ")
        self.send(ADMIN, "شلیک، موشک")
        self.assertIn("🚨 جنگ", self.state["custom_topics"])
        self.assertNotIn("جنگ", self.state["custom_topics"])
        self.assertEqual(self.state["custom_topics"]["🚨 جنگ"]["keywords"],
                         ["شلیک", "موشک"])

    # ---- 0.09: _norm_topic with emoji-leading names ----
    def test_norm_topic_emoji_named_topics(self):
        customs = {
            "🔴": {"name": "🔴", "keywords": ["سیاسی"]},
            "🚨 جنگ": {"name": "🚨 جنگ", "keywords": ["شلیک", "موشک"]},
            "جنگ 🚨": {"name": "جنگ 🚨", "keywords": ["شلیک", "موشک"]},
        }
        self.assertEqual(fb._norm_topic("📌 🔴", customs), "🔴")
        self.assertEqual(fb._norm_topic("📌 🚨 جنگ", customs), "🚨 جنگ")
        self.assertEqual(fb._norm_topic("📌 جنگ 🚨", customs), "جنگ 🚨")
        self.assertEqual(fb._norm_topic("🔴", customs), "🔴")
        self.assertEqual(fb._norm_topic("🚨 جنگ", customs), "🚨 جنگ")
        self.assertEqual(fb._norm_topic("🔴 سیاسی", customs), "political")
        self.assertEqual(fb._norm_topic("↩️ 🔴 سیاسی", customs), "political")
        self.assertEqual(fb._norm_topic("سیاسی", customs), "political")
        self.assertIsNone(fb._norm_topic("📌 غذا", customs))

    def test_removetopic_button_emoji_named_topic(self):
        self.state["custom_topics"]["🚨 جنگ"] = {"name": "🚨 جنگ", "keywords": ["شلیک", "موشک"]}
        self.state["custom_topics"]["🔴"] = {"name": "🔴", "keywords": ["سیاسی"]}
        self.send(ADMIN, "➖ حذف موضوع")
        self.send(ADMIN, "🚨 جنگ")
        self.assertIn("حذف شد", self.last_text())
        self.assertNotIn("🚨 جنگ", self.state.get("custom_topics", {}))
        self.send(ADMIN, "➖ حذف موضوع")
        self.send(ADMIN, "🔴")
        self.assertIn("حذف شد", self.last_text())
        self.assertNotIn("🔴", self.state.get("custom_topics", {}))

    def test_addtopic_emoji_only_name_rejected(self):
        self.send(ADMIN, "/addtopic 🔴 سیاسی")
        self.assertIn("فقط ایموجی", self.last_text())
        self.assertNotIn("🔴", self.state.get("custom_topics", {}))

    # ---- 0.09: comma-separated multi-word keywords ----
    def test_parse_keywords_comma_phrases(self):
        self.assertEqual(fb._parse_keywords("شورای امنیت، حمله موشکی"),
                         ["شورای امنیت", "حمله موشکی"])
        self.assertEqual(fb._parse_keywords("شورای امنیت, حمله موشکی"),
                         ["شورای امنیت", "حمله موشکی"])
        self.assertEqual(fb._parse_keywords("فیلم، سینما بازیگر"),
                         ["فیلم", "سینما بازیگر"])

    def test_parse_keywords_spaces_fallback(self):
        self.assertEqual(fb._parse_keywords("فیلم سینما بازیگر"),
                         ["فیلم", "سینما", "بازیگر"])
        self.assertEqual(fb._parse_keywords(""), [])

    def test_addtopic_command_comma_phrases(self):
        self.send(ADMIN, "/addtopic سینما فیلم سینمایی، بازیگر معروف")
        self.assertEqual(self.state["custom_topics"]["سینما"]["keywords"],
                         ["فیلم سینمایی", "بازیگر معروف"])

    def test_wizard_keywords_comma_phrases(self):
        self.send(ADMIN, "➕ موضوع جدید")
        self.send(ADMIN, "سینما")
        self.send(ADMIN, "فیلم سینمایی، بازیگر معروف")
        self.assertEqual(self.state["custom_topics"]["سینما"]["keywords"],
                         ["فیلم سینمایی", "بازیگر معروف"])

    def test_classify_multiword_keyword(self):
        self.send(ADMIN, "/addtopic سینما فیلم سینمایی، بازیگر معروف")
        customs = self.state["custom_topics"]
        self.assertEqual(fb.classify_post("🔵 فیلم سینمایی جدید اکران شد", customs), {"سینما"})
        self.assertEqual(fb.classify_post("🔵 فیلم جدید اکران شد", customs), set())

    # ---- 0.08: /keywords list + /removekeyword picker ----
    def test_keywords_lists_all_topics(self):
        self.send(ADMIN, "/addtopic سینما فیلم سینما")
        self.send(ADMIN, "/keywords")
        txt = self.last_text()
        self.assertIn("🔴 سیاسی", txt)
        self.assertIn("مجلس", txt)
        self.assertIn("سینما", txt)
        self.assertIn("فیلم", txt)
        self.assertIn("/removekeyword", txt)

    def test_keywords_single_topic(self):
        self.send(ADMIN, "/keywords سیاسی")
        txt = self.last_text()
        self.assertIn("🔴 سیاسی", txt)
        self.assertIn("مجلس", txt)
        self.assertNotIn("ورزشی", txt)
        self.assertNotIn("🟡 طلا و دلار", txt)

    def test_keywords_disabled_topic_rejected(self):
        self.send(ADMIN, "/removetopic سیاسی")
        self.send(ADMIN, "/keywords سیاسی")
        self.assertIn("چنین موضوعی نداریم", self.last_text())

    def test_keywords_non_admin_denied(self):
        self.send(USER_A, "/keywords")
        self.assertIn("فقط برای ادمینه", self.last_text())

    def test_keywords_button(self):
        self.send(ADMIN, "🔑 کلمه‌های کلیدی")
        self.assertIn("کلمه‌های کلیدی", self.last_text())

    def test_removekeyword_flow_removes_hardcoded(self):
        self.assertEqual(fb.classify_post("🔵 مجلس امروز جلسه داشت"), {"political"})
        self.send(ADMIN, "/removekeyword")
        self.assertIn("کدوم موضوع", self.last_text())
        self.send(ADMIN, "🔴 سیاسی")
        self.assertIn("کدوم کلمه", self.last_text())
        self.assertIn("مجلس", self.fake.sent[-1][2])
        self.send(ADMIN, "مجلس")
        self.assertIn("حذف شد", self.last_text())
        self.assertEqual(self.state["topic_removed_keywords"]["political"], ["مجلس"])
        self.assertEqual(
            fb.classify_post("🔵 مجلس امروز جلسه داشت",
                             removed_keywords=self.state["topic_removed_keywords"]), set())

    def test_removekeyword_with_topic_arg(self):
        self.send(ADMIN, "/removekeyword سیاسی")
        self.assertIn("کدوم کلمه", self.last_text())
        self.assertIn("مجلس", self.fake.sent[-1][2])

    def test_removekeyword_removes_extra(self):
        self.send(ADMIN, "/addtopic سیاسی برجام")
        self.send(ADMIN, "/removekeyword سیاسی")
        self.send(ADMIN, "برجام")
        self.assertIn("حذف شد", self.last_text())
        self.assertEqual(self.state["topic_extra_keywords"]["political"], [])

    def test_removekeyword_last_custom_keyword_refused(self):
        self.send(ADMIN, "/addtopic سینما فیلم")
        self.send(ADMIN, "/removekeyword سینما")
        self.send(ADMIN, "فیلم")
        self.assertIn("آخرین کلمه", self.last_text())
        self.assertEqual(self.state["custom_topics"]["سینما"]["keywords"], ["فیلم"])

    def test_removekeyword_unknown_topic(self):
        self.send(ADMIN, "/removekeyword غذا")
        self.assertIn("چنین موضوعی نداریم", self.last_text())

    def test_removekeyword_non_admin_denied(self):
        self.send(USER_A, "/removekeyword")
        self.assertIn("فقط برای ادمینه", self.last_text())

    def test_removekeyword_button(self):
        self.send(ADMIN, "➖ حذف کلمه‌ی کلیدی")
        self.assertIn("کدوم موضوع", self.last_text())

    def test_addtopic_restores_removed_keyword(self):
        self.send(ADMIN, "/removekeyword سیاسی")
        self.send(ADMIN, "مجلس")
        self.send(ADMIN, "/addtopic سیاسی مجلس")
        self.assertIn("برگشتن", self.last_text())
        self.assertEqual(self.state.get("topic_removed_keywords", {}).get("political", []), [])
        self.assertEqual(fb.classify_post("🔵 مجلس امروز جلسه داشت"), {"political"})

    def test_keywords_shows_added_and_removed(self):
        self.send(ADMIN, "/addtopic سیاسی برجام")
        self.send(ADMIN, "/removekeyword سیاسی")
        self.send(ADMIN, "مجلس")
        self.send(ADMIN, "/keywords سیاسی")
        txt = self.last_text()
        self.assertIn("➕ اضافه‌شده: برجام", txt)
        self.assertIn("➖ حذف‌شده: مجلس", txt)

    # ---- 0.06: membership sweep ----
    def test_sweep_removes_left_and_warns(self):
        self.send(USER_A, "/follow سیاسی")
        self.send(USER_B, "/follow ورزشی")
        self.fake.member_status = "left"
        self.assertTrue(fb._sweep_followers(self.state))
        self.assertNotIn(USER_A, self.state.get("users", {}))
        self.assertNotIn(USER_B, self.state.get("users", {}))
        warns = [s for s in self.fake.sent if "دوباره عضو کانال شو" in s[1]]
        self.assertEqual({c for c, _, _ in warns}, {USER_A, USER_B})
        self.assertIn("📢 عضویت در کانال", str(self.fake.sent[-1][2]))

    def test_sweep_mixed_statuses(self):
        self.send(USER_A, "/follow سیاسی")
        self.send(USER_B, "/follow ورزشی")
        orig = self.fake.api

        def mixed_api(method, **data):
            if method == "getChatMember":
                return {"status": "left" if str(data.get("user_id")) == USER_A else "member"}
            return orig(method, **data)

        fb.api = mixed_api
        try:
            fb._sweep_followers(self.state)
        finally:
            fb.api = orig
        self.assertNotIn(USER_A, self.state.get("users", {}))
        self.assertIn(USER_B, self.state.get("users", {}))

    def test_sweep_keeps_members(self):
        self.send(USER_A, "/follow سیاسی")
        self.assertTrue(fb._sweep_followers(self.state))
        self.assertIn(USER_A, self.state.get("users", {}))

    def test_sweep_skips_on_api_error(self):
        self.send(USER_A, "/follow سیاسی")
        self.fake.raise_on_member = True
        fb._sweep_followers(self.state)
        self.assertIn(USER_A, self.state.get("users", {}))

    def test_sweep_skips_admin(self):
        self.send_as(ADMIN, "/follow سیاسی", first_name="ادمین")
        self.fake.member_status = "left"
        fb._sweep_followers(self.state)
        self.assertIn(ADMIN, self.state.get("users", {}))

    def test_sweep_runs_at_most_once_a_day(self):
        self.send(USER_A, "/follow سیاسی")
        self.fake.member_status = "left"
        fb._sweep_followers(self.state)
        self.assertNotIn(USER_A, self.state.get("users", {}))
        self.fake.member_status = "member"
        self.send(USER_B, "/follow ورزشی")
        self.assertFalse(fb._sweep_followers(self.state))
        self.assertIn(USER_B, self.state.get("users", {}))

    def test_run_once_calls_sweep(self):
        import inspect
        self.assertIn("_sweep_followers", inspect.getsource(fb.run_once))

    def test_long_polling_params(self):
        # long polling: تلگرام تا ۳۰ ثانیه نگه می‌داره؛ مهلت درخواست بیشتر از اونه
        calls = {}

        class Resp:
            def json(self):
                return {"ok": True, "result": []}

        def fake_get(url, params=None, timeout=None):
            calls["params"] = params
            calls["timeout"] = timeout
            return Resp()

        orig_get = fb.requests.get
        orig_updates = fb.api_get_updates
        fb.requests.get = fake_get
        fb.api_get_updates = self._orig_get  # تابع واقعی، نه فیک setUp
        try:
            self.assertEqual(fb.api_get_updates(0), [])
        finally:
            fb.requests.get = orig_get
            fb.api_get_updates = orig_updates
        self.assertEqual(calls["params"]["timeout"], 30)
        self.assertGreaterEqual(calls["timeout"], 35)

    def test_poll_gap_is_small(self):
        self.assertLessEqual(fb.POLL_SECONDS, 2)

    def test_start_sends_welcome_with_keyboard(self):
        self.send(USER_A, "/start")
        self.assertIn("GodNews24", self.last_text())
        self.assertIn("سیاسی", self.last_text())
        self.assertIsNotNone(self.fake.sent[-1][2])  # reply keyboard present

    def test_start_shows_version_only_to_admin(self):
        self.send(ADMIN, "/start")
        self.assertIn(fb.BOT_VERSION, self.last_text())
        self.send(USER_A, "/start")
        self.assertNotIn(fb.BOT_VERSION, self.last_text())
        self.assertNotIn("نسخه‌ی ربات", self.last_text())

    def test_help_lists_commands(self):
        self.send(USER_A, "/help")
        txt = self.last_text()
        self.assertIn("📌 انتخاب موضوع", txt)
        self.assertIn("📋 موضوع‌های من", txt)

    def test_help_lists_send_for_members(self):
        self.send(USER_A, "/help")
        self.assertIn("📤 ارسال خبر", self.last_text())

    def test_help_admin_sees_admin_commands(self):
        self.send(ADMIN, "/help")
        txt = self.last_text()
        self.assertIn("/followers", txt)
        self.assertIn("/addtopic", txt)
        self.assertIn("/removetopic", txt)
        self.assertIn("/broadcast", txt)
        # ادمین دستورهای کاربر عادی رو نمی‌بینه
        for cmd in ("/follow ", "/unfollow", "/topics", "/send"):
            self.assertNotIn(cmd, txt)

    def test_admin_start_welcome_is_management_only(self):
        self.send(ADMIN, "/start")
        txt = self.last_text()
        self.assertIn("مدیریت", txt)
        self.assertNotIn("برای دنبال کردن", txt)
        self.assertNotIn("🔴 سیاسی", txt)

    def test_help_member_does_not_see_admin_commands(self):
        self.send(USER_A, "/help")
        txt = self.last_text()
        self.assertNotIn("/broadcast", txt)
        self.assertNotIn("/addtopic", txt)
        self.assertNotIn("/removetopic", txt)
        self.assertNotIn("/followers", txt)

    def test_unknown_command_gets_hint(self):
        self.send(USER_A, "/bogus")
        self.assertIn("/help", self.last_text())

    # ---- follow / unfollow / topics ----
    def test_follow_by_topic_button(self):
        self.send(USER_A, "🔴 سیاسی")
        self.assertEqual(self.state["users"][USER_A]["topics"], ["political"])
        self.assertIn("فعال شد", self.last_text())

    def test_follow_by_plain_name(self):
        self.send(USER_A, "ورزشی")
        self.assertEqual(self.state["users"][USER_A]["topics"], ["sport"])

    def test_follow_command_flow(self):
        self.send(USER_A, "/follow")
        self.assertIn("کدوم موضوع", self.last_text())
        self.send(USER_A, "طلا و دلار")
        self.assertEqual(self.state["users"][USER_A]["topics"], ["gold"])

    def test_follow_with_arg(self):
        self.send(USER_A, "/follow ورزشی")
        self.assertEqual(self.state["users"][USER_A]["topics"], ["sport"])

    def test_follow_twice_no_duplicate(self):
        self.send(USER_A, "/follow سیاسی")
        self.send(USER_A, "/follow سیاسی")
        self.assertEqual(self.state["users"][USER_A]["topics"], ["political"])
        self.assertIn("قبلاً", self.last_text())

    def test_follow_unknown_topic(self):
        self.send(USER_A, "/follow")
        self.send(USER_A, "غذا")
        self.assertEqual(self.state["users"].get(USER_A, {}).get("topics", []), [])
        self.assertIn("نمی‌شناسم", self.last_text())

    def test_cancel_pending(self):
        self.send(USER_A, "/follow")
        self.send(USER_A, "لغو")
        self.assertIn("لغو شد", self.last_text())
        self.send(USER_A, "ورزشی")  # after cancel, a plain name is a fresh follow
        self.assertEqual(self.state["users"][USER_A]["topics"], ["sport"])

    def test_unfollow_one_flow(self):
        self.send(USER_A, "/follow سیاسی")
        self.send(USER_A, "/follow ورزشی")
        self.send(USER_A, "/unfollow")
        self.assertIn("کدوم موضوع", self.last_text())
        self.send(USER_A, "سیاسی")
        self.assertEqual(self.state["users"][USER_A]["topics"], ["sport"])
        self.assertIn("لغو شد", self.last_text())

    def test_unfollow_all(self):
        self.send(USER_A, "/follow سیاسی")
        self.send(USER_A, "/follow ورزشی")
        self.send(USER_A, "/unfollow همه")
        self.assertEqual(self.state["users"][USER_A]["topics"], [])

    def test_unfollow_not_followed(self):
        self.send(USER_A, "/unfollow ورزشی")
        self.assertIn("دنبال نمی‌کردی", self.last_text())

    def test_topics_empty_and_listed(self):
        self.send(USER_A, "/topics")
        self.assertIn("هیچ موضوعی", self.last_text())
        self.send(USER_A, "/follow سیاسی")
        self.send(USER_A, "/follow طلا و دلار")
        self.send(USER_A, "/topics")
        self.assertIn("سیاسی", self.last_text())
        self.assertIn("طلا و دلار", self.last_text())

    # ---- admin-only /followers ----
    def test_followers_admin_only(self):
        self.send(USER_A, "/follow سیاسی")
        self.send(USER_B, "/follow ورزشی")
        self.send(USER_B, "/follow سیاسی")
        self.send(USER_A, "/followers")
        self.assertIn("فقط برای ادمینه", self.last_text())
        self.send(ADMIN, "/followers")
        txt = self.last_text()
        self.assertIn("سیاسی: 2", txt)
        self.assertIn("ورزشی: 1", txt)
        self.assertIn("طلا و دلار: 0", txt)
        self.assertIn("کل: 2", txt)

    # ---- classification ----
    def test_classify_political_by_leading_emoji(self):
        self.assertEqual(fb.classify_post("🔴 فوری: حمله موشکی به پایگاه نظامی"), {"political"})

    def test_classify_political_by_alert_emoji(self):
        self.assertEqual(fb.classify_post("🚨 هشدار: زلزله شدید در تهران"), {"political"})

    def test_classify_political_by_keyword(self):
        self.assertEqual(fb.classify_post("🔵 مذاکرات ایران و آمریکا به نتیجه رسید"), {"political"})

    def test_classify_sport_by_leading_emoji(self):
        self.assertEqual(fb.classify_post("⚽ پرسپولیس ۲-۱ استقلال را برد"), {"sport"})

    def test_classify_sport_by_keyword(self):
        self.assertEqual(fb.classify_post("🔵 لیگ برتر انگلیس امشب آغاز می‌شود"), {"sport"})

    def test_classify_gold_pricelist(self):
        self.assertEqual(
            fb.classify_post("🟡 قیمت لحظه‌ای طلا و دلار\nطلا: ۱۰۰\nدلار: ۲۰۰\nسکه: ۳۰۰"),
            {"gold"})

    def test_classify_gold_needs_money_keyword(self):
        # 🟡 اقتصادی ولی نه طلا/دلار → طلا و دلار نیست
        self.assertEqual(fb.classify_post("🟡 بورس تهران امروز مثبت بود"), set())

    def test_classify_general_matches_nothing(self):
        self.assertEqual(fb.classify_post("🔵 هوای تهران امروز آفتابی است"), set())
        self.assertEqual(fb.classify_post(""), set())

    # ---- forwarding ----
    def test_forward_matching_post_only_to_followers(self):
        self.send(USER_A, "/follow سیاسی")
        self.send(USER_B, "/follow ورزشی")
        fb.handle_channel_post(self.state, make_channel_post(777, "🔴 فوری: حمله موشکی جدید"))
        self.assertEqual(self.fake.forwarded, [(USER_A, 777)])

    def test_forward_nonmatching_post_sends_nothing(self):
        self.send(USER_A, "/follow سیاسی")
        fb.handle_channel_post(self.state, make_channel_post(778, "🔵 هوای تهران امروز آفتابی است"))
        self.assertEqual(self.fake.forwarded, [])

    def test_forward_gold_pricelist(self):
        self.send(USER_A, "/follow طلا و دلار")
        self.send(USER_B, "/follow سیاسی")
        fb.handle_channel_post(self.state, make_channel_post(779, "🟡 قیمت لحظه‌ای طلا و دلار\nطلا: ۱۰۰"))
        self.assertEqual(self.fake.forwarded, [(USER_A, 779)])

    def test_forward_multi_topic_user_gets_post_once(self):
        self.send(USER_A, "/follow سیاسی")
        self.send(USER_A, "/follow ورزشی")
        fb.handle_channel_post(self.state, make_channel_post(
            780, "🔴 فوری: حمله موشکی؛ تیم فوتبال پرسپولیس به فینال رفت"))
        self.assertEqual(self.fake.forwarded, [(USER_A, 780)])

    def test_duplicate_post_suppressed(self):
        self.send(USER_A, "/follow سیاسی")
        post = make_channel_post(781, "🔴 فوری: حمله موشکی جدید")
        fb.handle_channel_post(self.state, post)
        fb.handle_channel_post(self.state, post)  # same post again (e.g. offset lost)
        self.assertEqual(self.fake.forwarded, [(USER_A, 781)])
        self.assertEqual(self.state["seen_posts"].count(781), 1)

    def test_duplicate_via_process_updates(self):
        self.send(USER_A, "/follow ورزشی")
        self.fake.updates = [{"update_id": 5, "channel_post": make_channel_post(
            782, "⚽ پرسپولیس قهرمان لیگ شد")}]
        fb.process_updates(self.state)
        fb.process_updates(self.state)  # same update delivered again
        self.assertEqual(self.fake.forwarded, [(USER_A, 782)])

    def test_forward_logs_no_chat_ids(self):
        self.send(USER_A, "/follow سیاسی")
        buf = io.StringIO()
        with redirect_stdout(buf):
            fb.handle_channel_post(self.state, make_channel_post(783, "🔴 خبر مهم سیاسی"))
        self.assertNotIn(USER_A, buf.getvalue())

    def test_channel_post_from_other_channel_ignored(self):
        self.send(USER_A, "/follow سیاسی")
        other = {"message_id": 784,
                 "chat": {"id": -100999, "username": "SomeOther", "type": "channel"},
                 "text": "🔴 خبر مهم"}
        fb.handle_channel_post(self.state, other)
        self.assertEqual(self.fake.forwarded, [])

    # ---- 0.02: follower list ----
    def test_names_recorded_from_sender(self):
        self.send_as(USER_A, "/follow سیاسی", first_name="علی", username="ali123")
        self.assertEqual(self.state["users"][USER_A]["name"], "علی")
        self.assertEqual(self.state["users"][USER_A]["username"], "ali123")

    def test_followers_list_shows_names_and_topics(self):
        self.send_as(USER_A, "/follow سیاسی", first_name="علی", username="ali123")
        self.send_as(USER_B, "/follow ورزشی", first_name="مریم")
        self.send(ADMIN, "/followers")
        txt = self.last_text()
        self.assertIn("علی (@ali123) — 🔴 سیاسی", txt)
        self.assertIn("مریم — ⚽ ورزشی", txt)

    def test_followers_list_falls_back_to_id_without_name(self):
        self.state["users"]["333"] = {"topics": ["sport"]}
        self.send(ADMIN, "/followers")
        self.assertIn("333 — ⚽ ورزشی", self.last_text())

    def test_followers_list_caps_at_30(self):
        for i in range(35):
            uid = str(1000 + i)
            self.state["users"][uid] = {"topics": ["political"], "name": f"کاربر{i:02d}"}
        self.send(ADMIN, "/followers")
        txt = self.last_text()
        numbered = [l for l in txt.splitlines() if re.match(r"^[۰-۹]+\. ", l)]
        self.assertEqual(len(numbered), 30)
        self.assertIn("و 5 نفر دیگر", txt)

    # ---- 0.02: custom topics ----
    def test_addtopic_by_admin(self):
        self.send(ADMIN, "/addtopic سینما فیلم سینما بازیگر")
        self.assertEqual(self.state["custom_topics"]["سینما"],
                         {"name": "سینما", "keywords": ["فیلم", "سینما", "بازیگر"]})
        self.assertIn("اضافه شد", self.last_text())

    def test_addtopic_non_admin_denied(self):
        self.send(USER_A, "/addtopic سینما فیلم")
        self.assertIn("فقط برای ادمینه", self.last_text())
        self.assertEqual(self.state.get("custom_topics", {}), {})

    def test_addtopic_needs_name_and_keyword(self):
        self.send(ADMIN, "/addtopic")
        self.assertIn("/addtopic", self.last_text())
        self.send(ADMIN, "/addtopic سینما")
        self.assertIn("/addtopic", self.last_text())
        self.assertNotIn("سینما", self.state.get("custom_topics", {}))

    def test_addtopic_builtin_adds_keywords(self):
        self.send(ADMIN, "/addtopic سیاسی برجام")
        self.assertEqual(self.state["topic_extra_keywords"]["political"], ["برجام"])
        self.assertIn("اضافه شد", self.last_text())
        self.assertNotIn("سیاسی", self.state.get("custom_topics", {}))
        self.send(ADMIN, "/addtopic سیاسی برجام")
        self.assertIn("از قبل بودن", self.last_text())

    def test_addtopic_builtin_no_keywords_usage(self):
        self.send(ADMIN, "/addtopic سیاسی")
        self.assertIn("کلمه‌ها رو هم بفرست", self.last_text())

    def test_addtopic_duplicate_rejected(self):
        self.send(ADMIN, "/addtopic سینما فیلم")
        self.send(ADMIN, "/addtopic سینما فیلم")
        self.assertIn("این کلمه‌ها از قبل توی این موضوع هستن", self.last_text())

    def test_custom_topic_in_follow_keyboard_and_topics(self):
        self.send(ADMIN, "/addtopic سینما فیلم سینما")
        kb = fb.topic_keyboard(self.state)
        texts = [b["text"] for row in kb["keyboard"] for b in row]
        self.assertIn("سینما", texts)
        self.assertNotIn("📌 سینما", texts)
        self.send(USER_A, "/follow سینما")
        self.assertEqual(self.state["users"][USER_A]["topics"], ["سینما"])
        self.send(USER_A, "/topics")
        self.assertIn("سینما", self.last_text())

    def test_classify_custom_topic_by_keyword(self):
        customs = {"سینما": {"name": "سینما", "keywords": ["فیلم", "سینما"]}}
        self.assertEqual(fb.classify_post("🔵 فیلم تازه‌ی سینمای ایران اکران شد", customs),
                         {"سینما"})
        self.assertEqual(fb.classify_post("🔵 هوای تهران آفتابی است", customs), set())

    def test_forward_custom_topic_post(self):
        self.send(ADMIN, "/addtopic سینما فیلم سینما")
        self.send(USER_A, "/follow سینما")
        self.send(USER_B, "/follow سیاسی")
        fb.handle_channel_post(self.state, make_channel_post(790, "🔵 فیلم تازه اکران شد"))
        self.assertEqual(self.fake.forwarded, [(USER_A, 790)])

    def test_removetopic_drops_followers(self):
        self.send(ADMIN, "/addtopic سینما فیلم سینما")
        self.send(USER_A, "/follow سینما")
        self.send(ADMIN, "/removetopic سینما")
        self.assertIn("حذف شد", self.last_text())
        self.assertNotIn("سینما", self.state.get("custom_topics", {}))
        self.assertEqual(self.state["users"][USER_A]["topics"], [])

    def test_removetopic_builtin_disables_and_drops_followers(self):
        self.send(USER_A, "/follow سیاسی")
        self.send(USER_B, "/follow ورزشی")
        self.send(ADMIN, "/removetopic سیاسی")
        self.assertIn("حذف شد", self.last_text())
        self.assertIn("1 نفر", self.last_text())
        self.assertEqual(self.state["disabled_topics"], ["political"])
        self.assertEqual(self.state["users"][USER_A]["topics"], [])
        self.assertEqual(self.state["users"][USER_B]["topics"], ["sport"])

    def test_removetopic_builtin_twice(self):
        self.send(ADMIN, "/removetopic سیاسی")
        self.send(ADMIN, "/removetopic سیاسی")
        self.assertIn("قبلاً حذف شده", self.last_text())

    def test_addtopic_restores_disabled_builtin(self):
        self.send(ADMIN, "/removetopic سیاسی")
        self.send(ADMIN, "/addtopic سیاسی")
        self.assertIn("برگشت", self.last_text())
        self.assertEqual(self.state.get("disabled_topics", []), [])

    # ---- 0.07: built-in topics — keywords + delete/restore ----
    def test_classify_disabled_topic_ignored(self):
        self.assertEqual(
            fb.classify_post("🔴 فوری: حمله موشکی جدید", disabled=["political"]), set())
        self.assertEqual(
            fb.classify_post("⚽ پرسپولیس برد", disabled=["political"]), {"sport"})

    def test_classify_builtin_extra_keywords(self):
        self.assertEqual(
            fb.classify_post("🔵 برجام امضا شد",
                             extra_keywords={"political": ["برجام"]}), {"political"})
        self.assertEqual(fb.classify_post("🔵 برجام امضا شد"), set())

    def test_follow_disabled_topic_rejected(self):
        self.send(USER_A, "/follow سیاسی")
        self.send(ADMIN, "/removetopic سیاسی")
        self.send(USER_B, "/follow سیاسی")
        self.assertIn("حذف شده", self.last_text())
        self.assertEqual(self.state["users"][USER_B]["topics"], [])

    def test_select_picker_hides_disabled(self):
        self.state["disabled_topics"] = ["political"]
        texts = [b["text"] for row in fb._select_topics_keyboard(self.state, USER_A)["keyboard"] for b in row]
        self.assertNotIn("🔴 سیاسی", texts)
        self.assertIn("⚽ ورزشی", texts)
        self.assertIn("🟡 طلا و دلار", texts)

    def test_topic_keyboard_hides_disabled(self):
        self.state["disabled_topics"] = ["gold"]
        texts = [b["text"] for row in fb.topic_keyboard(self.state)["keyboard"] for b in row]
        self.assertNotIn("🟡 طلا و دلار", texts)
        self.assertIn("🔴 سیاسی", texts)

    def test_followers_skips_disabled_counts(self):
        self.send(USER_A, "/follow سیاسی")
        self.send(ADMIN, "/removetopic سیاسی")
        self.send(ADMIN, "/followers")
        self.assertNotIn("🔴 سیاسی", self.last_text())

    def test_wizard_builtin_keywords(self):
        self.send(ADMIN, "➕ موضوع جدید")
        self.send(ADMIN, "سیاسی")
        self.assertIn("موضوع اصلیه", self.last_text())
        self.send(ADMIN, "برجام")
        self.assertIn("اضافه شد", self.last_text())
        self.assertEqual(self.state["topic_extra_keywords"]["political"], ["برجام"])

    def test_wizard_restores_disabled_builtin(self):
        self.send(ADMIN, "/removetopic سیاسی")
        self.send(ADMIN, "➕ موضوع جدید")
        self.send(ADMIN, "سیاسی")
        self.assertIn("برگشت", self.last_text())
        self.assertEqual(self.state.get("disabled_topics", []), [])

    def test_remove_picker_restores_disabled(self):
        self.send(ADMIN, "/removetopic سیاسی")
        self.send(ADMIN, "➖ حذف موضوع")
        markup = self.fake.sent[-1][2]
        self.assertIn("↩️ 🔴 سیاسی", markup)
        self.send(ADMIN, "↩️ 🔴 سیاسی")
        self.assertIn("برگشت", self.last_text())
        self.assertEqual(self.state.get("disabled_topics", []), [])

    def test_removetopic_unknown_rejected(self):
        self.send(ADMIN, "/removetopic غذا")
        self.assertIn("چنین موضوعی نداریم", self.last_text())

    def test_removetopic_non_admin_denied(self):
        self.send(ADMIN, "/addtopic سینما فیلم")
        self.send(USER_A, "/removetopic سینما")
        self.assertIn("فقط برای ادمینه", self.last_text())
        self.assertIn("سینما", self.state["custom_topics"])

    # ---- 0.02: broadcast ----
    def test_broadcast_to_all(self):
        self.send(USER_A, "/follow سیاسی")
        self.send(USER_B, "/follow ورزشی")
        self.send(ADMIN, "/broadcast سلام به همه")
        got = [s for s in self.fake.sent if s[1] == "سلام به همه"]
        self.assertEqual({c for c, _, _ in got}, {USER_A, USER_B})
        self.assertIn("به 2 نفر رسید، 0 نفر ناموفق بود", self.last_text())

    def test_broadcast_to_one_topic(self):
        self.send(USER_A, "/follow سیاسی")
        self.send(USER_B, "/follow ورزشی")
        self.send(ADMIN, "/broadcast 🔴 سیاسی خبر مهم")
        got = [s for s in self.fake.sent if s[1] == "خبر مهم"]
        self.assertEqual([c for c, _, _ in got], [USER_A])
        self.assertIn("به 1 نفر رسید", self.last_text())

    def test_broadcast_bare_topic_name_goes_to_all(self):
        # اسم خالی موضوع به‌تنهایی انتخاب‌کننده نیست: پیام به همه می‌رسه
        self.send(USER_A, "/follow سیاسی")
        self.send(USER_B, "/follow ورزشی")
        self.send(ADMIN, "/broadcast سیاسی خبر مهم")
        got = [s for s in self.fake.sent if s[1] == "سیاسی خبر مهم"]
        self.assertEqual({c for c, _, _ in got}, {USER_A, USER_B})
        self.assertIn("به 2 نفر رسید", self.last_text())

    def test_broadcast_emoji_without_space(self):
        # «🔴سیاسی» بدون فاصله هم انتخاب‌کننده‌ست
        self.send(USER_A, "/follow سیاسی")
        self.send(USER_B, "/follow ورزشی")
        self.send(ADMIN, "/broadcast 🔴سیاسی خبر مهم")
        got = [s for s in self.fake.sent if s[1] == "خبر مهم"]
        self.assertEqual([c for c, _, _ in got], [USER_A])

    def test_broadcast_custom_topic_needs_emoji(self):
        self.send(ADMIN, "/addtopic سینما فیلم سینما")
        self.send(USER_A, "/follow سینما")
        self.send(USER_B, "/follow ورزشی")
        self.send(ADMIN, "/broadcast 📌 سینما خبر سینما")
        got = [s for s in self.fake.sent if s[1] == "خبر سینما"]
        self.assertEqual([c for c, _, _ in got], [USER_A])
        self.send(ADMIN, "/broadcast سینما خبر همگانی")
        got = [s for s in self.fake.sent if s[1] == "سینما خبر همگانی"]
        self.assertEqual({c for c, _, _ in got}, {USER_A, USER_B})

    def test_broadcast_emoji_alone_prompts_for_text(self):
        self.send(ADMIN, "/broadcast 🔴 سیاسی")
        self.assertIn("متن پیام رو هم بنویس", self.last_text())

    def test_broadcast_non_admin_denied(self):
        self.send(USER_A, "/follow سیاسی")
        self.send(USER_A, "/broadcast سلام")
        self.assertIn("فقط برای ادمینه", self.last_text())
        self.assertNotIn("سلام", [s[1] for s in self.fake.sent])

    def test_broadcast_no_arg_prompts_then_sends(self):
        self.send(USER_A, "/follow سیاسی")
        self.send(ADMIN, "/broadcast")
        self.assertIn("پیام به کی برسه؟", self.last_text())
        self.send(ADMIN, "👥 به همه")
        self.assertIn("متن پیام رو بفرست", self.last_text())
        self.send(ADMIN, "خبر فوری برای همه")
        got = [s for s in self.fake.sent if s[1] == "خبر فوری برای همه"]
        self.assertEqual([c for c, _, _ in got], [USER_A])
        self.assertIn("به 1 نفر رسید", self.last_text())

    def test_broadcast_cancel_pending(self):
        self.send(ADMIN, "/broadcast")
        self.send(ADMIN, "لغو")
        self.assertIn("لغو شد", self.last_text())
        self.send(ADMIN, "سلام")
        self.assertNotIn("به 1 نفر رسید", self.last_text())

    def test_broadcast_counts_failures(self):
        self.send(USER_A, "/follow سیاسی")
        self.send(USER_B, "/follow ورزشی")
        self.fake.send_failures = {USER_B: "none"}
        self.send(ADMIN, "/broadcast سلام")
        self.assertIn("به 1 نفر رسید، 1 نفر ناموفق بود", self.last_text())

    def test_broadcast_skips_users_without_topics(self):
        self.send(USER_A, "/follow سیاسی")
        self.send(USER_B, "/start")  # member but follows nothing
        self.send(ADMIN, "/broadcast سلام")
        got = [s for s in self.fake.sent if s[1] == "سلام"]
        self.assertEqual([c for c, _, _ in got], [USER_A])

    # ---- 0.02: submissions ----
    def test_text_submission_forwarded_to_admin(self):
        self.send_as(USER_A, "خبر مهم: زلزله در تهران", first_name="علی",
                     username="ali123", message_id=5)
        admin_msgs = [s[1] for s in self.fake.sent if s[0] == ADMIN]
        self.assertIn("📩 خبر ارسالی از علی (@ali123)", admin_msgs)
        self.assertIn((ADMIN, 5), self.fake.forwarded)
        self.assertIn("خبرت رسید", self.last_text())

    def test_photo_submission_forwarded_to_admin(self):
        msg = make_message(USER_A, None, message_id=6,
                           from_={"first_name": "مریم"}, photo=True)
        msg["caption"] = "عکس خبر"
        self.send_msg(msg)
        self.assertIn((ADMIN, 6), self.fake.forwarded)
        self.assertIn("📩 خبر ارسالی از مریم",
                      [s[1] for s in self.fake.sent if s[0] == ADMIN])
        self.assertEqual(self.fake.forward_calls[-1]["from_chat_id"], USER_A)

    def test_video_submission_forwarded_to_admin(self):
        msg = make_message(USER_A, None, message_id=7, video=True)
        self.send_msg(msg)
        self.assertIn((ADMIN, 7), self.fake.forwarded)

    def test_submission_cooldown(self):
        self.send_as(USER_A, "خبر اول", message_id=8)
        self.send_as(USER_A, "خبر دوم", message_id=9)
        self.assertIn("صبر کن", self.last_text())
        self.assertEqual([f for f in self.fake.forwarded if f[0] == ADMIN], [(ADMIN, 8)])

    def test_submission_cooldown_expires(self):
        self.send_as(USER_A, "خبر اول", message_id=10)
        fb._SUBMIT_LAST[USER_A] = time.time() - 61
        self.send_as(USER_A, "خبر دوم", message_id=11)
        self.assertIn((ADMIN, 11), self.fake.forwarded)

    def test_admin_never_self_forwarded(self):
        self.send(ADMIN, "یه متن معمولی")
        self.assertEqual([f for f in self.fake.forwarded if f[0] == ADMIN], [])
        self.assertIn("نمی‌شناسم", self.last_text())

    def test_send_command_prompts_then_forwards(self):
        self.send(USER_A, "/send")
        self.assertIn("خبر، عکس یا فیلمت", self.last_text())
        self.send_as(USER_A, "خبر با دستور", message_id=12)
        self.assertIn((ADMIN, 12), self.fake.forwarded)

    def test_send_with_text_arg(self):
        self.send_as(USER_A, "/send خبر فوری از محل", first_name="علی", message_id=13)
        admin_msgs = [s[1] for s in self.fake.sent if s[0] == ADMIN]
        self.assertIn("📩 خبر ارسالی از علی", admin_msgs)
        self.assertIn("خبر فوری از محل", admin_msgs)

    # ---- 0.02: membership gate ----
    def test_gate_member_passes(self):
        self.fake.member_status = "member"
        self.send(USER_A, "/follow سیاسی")
        self.assertEqual(self.state["users"][USER_A]["topics"], ["political"])

    def test_gate_allows_restricted_creator_administrator(self):
        cases = [("restricted", "301"), ("creator", "302"), ("administrator", "303")]
        for status, uid in cases:
            self.fake.member_status = status
            self.send(uid, "/follow ورزشی")
            self.assertIn("sport", self.state["users"][uid]["topics"])

    def test_gate_left_denied_with_join_prompt(self):
        self.fake.member_status = "left"
        self.send(USER_A, "/follow سیاسی")
        txt, markup = self.last_text(), self.fake.sent[-1][2]
        self.assertIn("عضو کانال", txt)
        self.assertIn("https://t.me/GodNews24", markup)
        self.assertEqual(self.state["users"].get(USER_A, {}).get("topics", []), [])

    def test_gate_kicked_denied(self):
        self.fake.member_status = "kicked"
        self.send(USER_A, "/follow سیاسی")
        self.assertIn("عضو کانال", self.last_text())

    def test_gate_applies_to_admin_too(self):
        self.fake.member_status = "left"
        self.send(ADMIN, "/followers")
        self.assertIn("عضو کانال", self.last_text())

    def test_gate_api_error_allows(self):
        self.fake.raise_on_member = True
        self.send(USER_A, "/follow سیاسی")
        self.assertEqual(self.state["users"][USER_A]["topics"], ["political"])

    def test_gate_api_none_allows(self):
        self.fake.member_status = None
        self.send(USER_A, "/follow سیاسی")
        self.assertEqual(self.state["users"][USER_A]["topics"], ["political"])

    def test_gate_cache_avoids_second_call(self):
        self.send(USER_A, "/follow سیاسی")
        self.send(USER_A, "/follow ورزشی")
        self.assertEqual(len(self.fake.member_calls), 1)

    def test_gate_cache_expires(self):
        self.send(USER_A, "/follow سیاسی")
        fb._MEMBER_CACHE[USER_A] = (True, time.time() - 601)
        self.fake.member_status = "left"
        self.send(USER_A, "/follow ورزشی")
        self.assertIn("عضو کانال", self.last_text())
        self.assertEqual(len(self.fake.member_calls), 2)

    def test_gate_applies_to_submissions(self):
        self.fake.member_status = "left"
        self.send_as(USER_A, "خبر مهم", message_id=20)
        self.assertIn("عضو کانال", self.last_text())
        self.assertEqual([f for f in self.fake.forwarded if f[0] == ADMIN], [])

    # ---- 0.02: admin menu buttons ----
    def test_admin_keyboard_has_admin_buttons(self):
        texts = [b["text"] for row in fb.main_keyboard(ADMIN)["keyboard"] for b in row]
        for label in ("👥 دنبال‌کننده‌ها", "📣 پیام به اعضا",
                      "🔧 تغییرات موضوع",
                      "🔑 کلمه‌های کلیدی"):
            self.assertIn(label, texts)
        # حذف کلمه‌ی کلیدی فقط داخل /keywords هست، نه توی منوی اصلی
        self.assertNotIn("➖ حذف کلمه‌ی کلیدی", texts)

    def test_member_keyboard_has_no_admin_buttons(self):
        for kb in (fb.main_keyboard(USER_A), fb.main_keyboard(), fb.main_keyboard(None)):
            texts = [b["text"] for row in kb["keyboard"] for b in row]
            for label in ("👥 دنبال‌کننده‌ها", "📣 پیام به اعضا",
                          "➕ موضوع جدید", "➖ حذف موضوع",
                          "🔧 تغییرات موضوع"):
                self.assertNotIn(label, texts)

    def test_admin_keyboard_has_no_member_buttons(self):
        # ادمین فقط دکمه‌های مدیریت رو می‌بینه، نه دکمه‌های دنبال کردن موضوع
        texts = [b["text"] for row in fb.main_keyboard(ADMIN)["keyboard"] for b in row]
        for label in ("🔴 سیاسی", "⚽ ورزشی", "🟡 طلا و دلار",
                      "📋 موضوع‌های من"):
            self.assertNotIn(label, texts)
        for label in ("👥 دنبال‌کننده‌ها", "📣 پیام به اعضا",
                      "🔧 تغییرات موضوع", "❓ راهنما"):
            self.assertIn(label, texts)

    def test_admin_sees_admin_keyboard_on_start(self):
        self.send(ADMIN, "/start")
        markup = self.fake.sent[-1][2]
        self.assertIn("👥 دنبال‌کننده‌ها", markup)
        self.assertIn("📣 پیام به اعضا", markup)

    def test_member_sees_member_keyboard_on_start(self):
        self.send(USER_A, "/start")
        markup = self.fake.sent[-1][2]
        self.assertNotIn("👥 دنبال‌کننده‌ها", markup)
        self.assertNotIn("➕ موضوع جدید", markup)

    def test_followers_button_runs_followers(self):
        self.send_as(USER_A, "/follow سیاسی", first_name="علی")
        self.send(ADMIN, "👥 دنبال‌کننده‌ها")
        txt = self.last_text()
        self.assertIn("کل: 1", txt)
        self.assertIn("علی — 🔴 سیاسی", txt)

    def test_broadcast_button_prompts_then_sends(self):
        self.send(USER_A, "/follow سیاسی")
        self.send(ADMIN, "📣 پیام به اعضا")
        self.assertIn("پیام به کی برسه؟", self.last_text())
        self.send(ADMIN, "👥 به همه")
        self.assertIn("متن پیام رو بفرست", self.last_text())
        self.send(ADMIN, "سلام از دکمه")
        got = [s for s in self.fake.sent if s[1] == "سلام از دکمه"]
        self.assertEqual([c for c, _, _ in got], [USER_A])
        self.assertIn("به 1 نفر رسید", self.last_text())

    def test_addtopic_button_flow(self):
        # جادوگر دو قدمه: اول نام، بعد کلمه‌های کلیدی
        self.send(ADMIN, "➕ موضوع جدید")
        self.assertIn("نام موضوع جدید چیه؟", self.last_text())
        self.send(ADMIN, "سینما")
        self.assertIn("کلمه‌های کلیدی", self.last_text())
        self.assertNotIn("سینما", self.state.get("custom_topics", {}))
        self.send(ADMIN, "فیلم سینما بازیگر")
        self.assertIn("اضافه شد", self.last_text())
        self.assertEqual(self.state["custom_topics"]["سینما"]["keywords"],
                         ["فیلم", "سینما", "بازیگر"])

    def test_addtopic_button_validates(self):
        self.send(ADMIN, "/addtopic سینما فیلم سینما")
        self.send(ADMIN, "➕ موضوع جدید")
        self.send(ADMIN, "سینما")  # نام تکراری سفارشی → دوباره نام می‌خواد
        self.assertIn("نمی‌شه اضافه کرد", self.last_text())
        self.assertEqual(self.state["custom_topics"]["سینما"]["keywords"], ["فیلم", "سینما"])
        self.send(ADMIN, "تئاتر")
        self.assertIn("کلمه‌های کلیدی", self.last_text())

    def test_addtopic_wizard_multiword_name(self):
        self.send(ADMIN, "➕ موضوع جدید")
        self.send(ADMIN, "فیلم و سینما")
        self.send(ADMIN, "فیلم سینما")
        self.assertIn("اضافه شد", self.last_text())
        self.assertIn("فیلم و سینما", self.state["custom_topics"])

    def test_addtopic_wizard_keywords_with_commas(self):
        self.send(ADMIN, "➕ موضوع جدید")
        self.send(ADMIN, "سینما")
        self.send(ADMIN, "فیلم، سینما، بازیگر")
        self.assertEqual(self.state["custom_topics"]["سینما"]["keywords"],
                         ["فیلم", "سینما", "بازیگر"])

    def test_removetopic_button_flow(self):
        self.send(ADMIN, "/addtopic سینما فیلم سینما")
        self.send(USER_A, "/follow سینما")
        self.send(ADMIN, "➖ حذف موضوع")
        self.assertIn("کدوم موضوع رو حذف کنم؟", self.last_text())
        markup = self.fake.sent[-1][2]
        self.assertIn('"سینما"', markup)
        self.assertNotIn("📌 سینما", markup)
        self.send(ADMIN, "سینما")
        self.assertIn("حذف شد", self.last_text())
        self.assertNotIn("سینما", self.state.get("custom_topics", {}))
        self.assertEqual(self.state["users"][USER_A]["topics"], [])

    def test_removetopic_button_bare_name_also_works(self):
        self.send(ADMIN, "/addtopic سینما فیلم سینما")
        self.send(ADMIN, "➖ حذف موضوع")
        self.send(ADMIN, "سینما")
        self.assertIn("حذف شد", self.last_text())
        self.assertNotIn("سینما", self.state.get("custom_topics", {}))

    def test_removetopic_button_no_custom_topics(self):
        self.send(ADMIN, "➖ حذف موضوع")
        self.assertIn("هیچ موضوع سفارشی‌ای نداری", self.last_text())
        self.assertIsNone(self.state.get("pending", {}).get(ADMIN))

    def test_removetopic_button_rejects_builtin(self):
        self.send(ADMIN, "/addtopic سینما فیلم سینما")
        self.send(ADMIN, "➖ حذف موضوع")
        self.send(ADMIN, "سیاسی")  # تایپ نام موضوع اصلی توی انتخاب‌گر = حذفش
        self.assertIn("حذف شد", self.last_text())
        self.assertIn("political", self.state.get("disabled_topics", []))
        self.assertIn("سینما", self.state.get("custom_topics", {}))

    def test_non_admin_button_text_gets_denial(self):
        for label in ("👥 دنبال‌کننده‌ها", "📣 پیام به اعضا",
                      "➕ موضوع جدید", "➖ حذف موضوع",
                      "🔑 کلمه‌های کلیدی", "➖ حذف کلمه‌ی کلیدی"):
            self.send(USER_A, label)
            self.assertIn("فقط برای ادمینه", self.last_text())
        self.assertEqual(self.state.get("custom_topics", {}), {})
        self.assertIsNone(self.state.get("pending", {}).get(USER_A))

    # ---- encrypted state (public repo) ----
    def test_state_file_is_not_valid_json(self):
        self.send(USER_A, "/follow سیاسی")
        fb.save_state(self.state)
        with open(self.state_path, "rb") as f:
            raw = f.read()
        with self.assertRaises(ValueError):
            json.loads(raw.decode("utf-8"))

    def test_state_file_contains_no_chat_id_in_plaintext(self):
        self.send(USER_A, "/follow سیاسی")
        self.send(USER_B, "/follow طلا و دلار")
        fb.save_state(self.state)
        with open(self.state_path, "rb") as f:
            raw = f.read()
        self.assertNotIn(USER_A.encode(), raw)
        self.assertNotIn(USER_B.encode(), raw)
        self.assertNotIn("political".encode(), raw)
        self.assertNotIn("سیاسی".encode(), raw)

    def test_encrypt_decrypt_roundtrip(self):
        self.send(USER_A, "/follow سیاسی")
        self.send(USER_B, "/follow ورزشی")
        self.send(USER_B, "/follow طلا و دلار")
        self.state["seen_posts"] = [777, 778]
        fb.save_state(self.state)
        loaded = fb.load_state()
        self.assertEqual(loaded["users"], self.state["users"])
        self.assertEqual(loaded["seen_posts"], [777, 778])

    def test_missing_token_never_writes_plaintext(self):
        saved = os.environ.pop("FOLLOW_BOT_TOKEN", None)
        try:
            with self.assertRaises(SystemExit):
                fb.save_state(self.state)
            self.assertFalse(os.path.exists(self.state_path))
        finally:
            if saved is not None:
                os.environ["FOLLOW_BOT_TOKEN"] = saved

    def test_missing_token_cannot_load_encrypted_state(self):
        self.send(USER_A, "/follow سیاسی")
        fb.save_state(self.state)
        saved = os.environ.pop("FOLLOW_BOT_TOKEN", None)
        try:
            with self.assertRaises(SystemExit):
                fb.load_state()
        finally:
            if saved is not None:
                os.environ["FOLLOW_BOT_TOKEN"] = saved

    def test_wrong_token_cannot_decrypt(self):
        self.send(USER_A, "/follow سیاسی")
        fb.save_state(self.state)
        os.environ["FOLLOW_BOT_TOKEN"] = "a-different-token"
        try:
            with self.assertRaises(SystemExit):
                fb.load_state()
        finally:
            os.environ["FOLLOW_BOT_TOKEN"] = "test-token-123"


    # ---- 0.13: «تغییرات موضوع» + ویرایش اسم موضوع ----
    def test_admin_menu_has_topic_changes_button(self):
        texts = [b["text"] for row in fb.main_keyboard(ADMIN, self.state)["keyboard"] for b in row]
        self.assertIn("🔧 تغییرات موضوع", texts)
        self.assertNotIn("➕ موضوع جدید", texts)
        self.assertNotIn("➖ حذف موضوع", texts)

    def test_managetopics_hub_keyboard(self):
        self.send(ADMIN, "🔧 تغییرات موضوع")
        markup = json.loads(self.fake.sent[-1][2])
        texts = [b["text"] for row in markup["keyboard"] for b in row]
        self.assertIn("➕ موضوع جدید", texts)
        self.assertIn("➖ حذف موضوع", texts)
        self.assertIn("✏️ ویرایش اسم موضوع", texts)
        self.send(ADMIN, "/managetopics")
        markup = json.loads(self.fake.sent[-1][2])
        texts = [b["text"] for row in markup["keyboard"] for b in row]
        self.assertIn("✏️ ویرایش اسم موضوع", texts)
        self.send(USER_A, "🔧 تغییرات موضوع")
        self.assertIn("فقط برای ادمینه", self.last_text())
        self.send(USER_A, "/managetopics")
        self.assertIn("فقط برای ادمینه", self.last_text())

    def test_rename_custom_topic_moves_followers_and_counts(self):
        self.state["custom_topics"]["سینما"] = {"name": "سینما", "keywords": ["فیلم"]}
        self.state["custom_topics"]["غذا"] = {"name": "غذا", "keywords": ["پیتزا"]}
        self.state["users"][USER_A] = {"topics": ["سینما", "political"]}
        self.state["users"][USER_B] = {"topics": ["سینما"]}
        self.state["topic_forward_counts"] = {"سینما": 5}
        ok, reply = fb._rename_custom_topic(self.state, "سینما", "فیلم و سریال")
        self.assertTrue(ok)
        self.assertIn("عوض شد", reply)
        self.assertEqual(list(self.state["custom_topics"]), ["فیلم و سریال", "غذا"])
        self.assertEqual(self.state["custom_topics"]["فیلم و سریال"]["keywords"], ["فیلم"])
        self.assertEqual(self.state["users"][USER_A]["topics"], ["فیلم و سریال", "political"])
        self.assertEqual(self.state["users"][USER_B]["topics"], ["فیلم و سریال"])
        self.assertEqual(self.state["topic_forward_counts"], {"فیلم و سریال": 5})

    def test_rename_custom_topic_rejections(self):
        self.state["custom_topics"]["سینما"] = {"name": "سینما", "keywords": ["فیلم"]}
        self.state["custom_topics"]["غذا"] = {"name": "غذا", "keywords": ["پیتزا"]}
        ok, reply = fb._rename_custom_topic(self.state, "سینما", "غذا")
        self.assertFalse(ok)
        self.assertIn("از قبل هست", reply)
        ok, reply = fb._rename_custom_topic(self.state, "سینما", "سیاسی")
        self.assertFalse(ok)
        self.assertIn("از قبل هست", reply)
        ok, reply = fb._rename_custom_topic(self.state, "سینما", "🔴")
        self.assertFalse(ok)
        self.assertIn("فقط ایموجی", reply)
        ok, reply = fb._rename_custom_topic(self.state, "سینما", "سینما")
        self.assertFalse(ok)
        self.assertIn("یک", reply)
        ok, reply = fb._rename_custom_topic(self.state, "نداریم", "جدید")
        self.assertFalse(ok)
        self.assertIn("سینما", self.state["custom_topics"])

    def test_rename_flow_via_picker(self):
        self.send(ADMIN, "/addtopic سینما فیلم سینما")
        self.send(USER_A, "/follow سینما")
        self.assertIn("سینما", self.state["users"][USER_A]["topics"])
        self.send(ADMIN, "✏️ ویرایش اسم موضوع")
        markup = json.loads(self.fake.sent[-1][2])
        texts = [b["text"] for row in markup["keyboard"] for b in row]
        self.assertIn("سینما", texts)
        self.send(ADMIN, "سینما")
        self.assertIn("اسم جدید", self.last_text())
        self.send(ADMIN, "فیلم و سریال")
        self.assertIn("عوض شد", self.last_text())
        self.assertIn("فیلم و سریال", self.state["custom_topics"])
        self.assertNotIn("سینما", self.state["custom_topics"])
        self.assertIn("فیلم و سریال", self.state["users"][USER_A]["topics"])
        self.assertNotIn("سینما", self.state["users"][USER_A]["topics"])

    def test_rename_builtin_via_command(self):
        self.send(ADMIN, "/renametopic سیاسی")
        self.assertIn("اسم جدید", self.last_text())
        self.send(ADMIN, "سیاست روز")
        self.assertIn("عوض شد", self.last_text())
        self.assertEqual(self.state["topic_labels"], {"political": "سیاست روز"})
        self.assertEqual(fb.topic_label(self.state, "political"), "🔴 سیاست روز")

    def test_rename_picker_lists_builtins_without_customs(self):
        self.send(ADMIN, "✏️ ویرایش اسم موضوع")
        markup = json.loads(self.fake.sent[-1][2])
        texts = [b["text"] for row in markup["keyboard"] for b in row]
        self.assertIn("🔴 سیاسی", texts)
        self.assertIn("⚽ ورزشی", texts)
        self.assertIn("🟡 طلا و دلار", texts)

    def test_rename_duplicate_name_reprompts(self):
        self.send(ADMIN, "/addtopic سینما فیلم سینما")
        self.send(ADMIN, "/addtopic غذا پیتزا غذا")
        self.send(ADMIN, "/renametopic سینما")
        self.assertIn("اسم جدید", self.last_text())
        self.send(ADMIN, "غذا")
        self.assertIn("از قبل هست", self.last_text())
        self.assertIn("سینما", self.state["custom_topics"])
        self.send(ADMIN, "فیلم و سریال")
        self.assertIn("عوض شد", self.last_text())

    def test_help_mentions_topic_changes(self):
        self.send(ADMIN, "/help")
        self.assertIn("تغییرات موضوع", self.last_text())
        self.assertIn("/renametopic", self.last_text())
        self.send(ADMIN, "/help /renametopic")
        self.assertIn("ویرایش اسم", self.last_text())
        self.send(ADMIN, "/help /managetopics")
        self.assertIn("تغییرات موضوع", self.last_text())
        self.send(USER_A, "/help /renametopic")
        self.assertIn("فقط برای ادمینه", self.last_text())


    # ---- 0.14: ویرایش اسم موضوع‌های اصلی ----
    def test_rename_builtin_topic_unit(self):
        self.state["users"][USER_A] = {"topics": ["political"]}
        self.state["topic_forward_counts"] = {"political": 4}
        ok, reply = fb._rename_topic(self.state, "political", "سیاست روز")
        self.assertTrue(ok)
        self.assertIn("عوض شد", reply)
        self.assertEqual(self.state["topic_labels"], {"political": "سیاست روز"})
        self.assertEqual(fb.topic_label(self.state, "political"), "🔴 سیاست روز")
        # کلید و دنبال‌کننده‌ها و آمار عوض نشدن
        self.assertEqual(self.state["users"][USER_A]["topics"], ["political"])
        self.assertEqual(self.state["topic_forward_counts"], {"political": 4})
        # اسم جدید تشخیص داده می‌شه، اسم قدیمی هم هنوز کار می‌کنه
        labels = {"political": "سیاست روز"}
        self.assertEqual(fb._norm_topic("سیاست روز", {}, labels), "political")
        self.assertEqual(fb._norm_topic("🔴 سیاست روز", {}, labels), "political")
        self.assertEqual(fb._norm_topic("سیاسی", {}, labels), "political")
        # برگردوندن به اسم اول ممکنه
        ok, reply = fb._rename_topic(self.state, "political", "سیاسی")
        self.assertTrue(ok)
        self.assertEqual(fb.topic_label(self.state, "political"), "🔴 سیاسی")

    def test_norm_topic_with_labels(self):
        labels = {"political": "سیاست روز", "gold": "🚨 ارز"}
        self.assertEqual(fb._norm_topic("سیاست روز", {}, labels), "political")
        self.assertEqual(fb._norm_topic("🔴 سیاست روز", {}, labels), "political")
        self.assertEqual(fb._norm_topic("🚨 ارز", {}, labels), "gold")
        self.assertIsNone(fb._norm_topic("ارز", {}, labels))
        self.assertEqual(fb._norm_topic("سیاسی", {}, labels), "political")

    def test_rename_builtin_flow_via_picker(self):
        self.send(USER_A, "/follow سیاسی")
        self.assertIn("political", self.state["users"][USER_A]["topics"])
        self.send(ADMIN, "✏️ ویرایش اسم موضوع")
        markup = json.loads(self.fake.sent[-1][2])
        texts = [b["text"] for row in markup["keyboard"] for b in row]
        self.assertIn("🔴 سیاسی", texts)
        self.send(ADMIN, "🔴 سیاسی")
        self.assertIn("اسم جدید", self.last_text())
        self.send(ADMIN, "سیاست روز")
        self.assertIn("عوض شد", self.last_text())
        self.assertEqual(self.state["topic_labels"], {"political": "سیاست روز"})
        # کیبوردها اسم جدید رو نشون می‌دن (سیاسی دنبال‌شده، پس تیک داره)
        texts = [b["text"] for row in fb._select_topics_keyboard(self.state, USER_A)["keyboard"] for b in row]
        self.assertIn("✅ 🔴 سیاست روز", texts)
        self.assertNotIn("🔴 سیاسی", texts)
        self.assertNotIn("✅ 🔴 سیاسی", texts)
        texts = [b["text"] for row in fb.topic_keyboard(self.state)["keyboard"] for b in row]
        self.assertIn("🔴 سیاست روز", texts)
        # دنبال کردن با اسم جدید
        self.send(USER_B, "/follow سیاست روز")
        self.assertIn("political", self.state["users"][USER_B]["topics"])
        # خبر سیاسی هنوز به همون موضوع می‌رسه
        fb.handle_channel_post(self.state, make_channel_post(900, "🔴 خبر مهم سیاسی"))
        self.assertEqual(self.fake.forwarded, [(USER_A, 900), (USER_B, 900)])

    def test_rename_builtin_rejections(self):
        ok, reply = fb._rename_topic(self.state, "political", "ورزشی")
        self.assertFalse(ok)
        self.assertIn("از قبل هست", reply)
        ok, reply = fb._rename_topic(self.state, "political", "🔴")
        self.assertFalse(ok)
        self.assertIn("فقط ایموجی", reply)
        ok, reply = fb._rename_topic(self.state, "political", "سیاسی")
        self.assertFalse(ok)
        self.assertIn("یک", reply)
        ok, reply = fb._rename_topic(self.state, "political", "سیاست روز")
        self.assertTrue(ok)
        ok, reply = fb._rename_topic(self.state, "sport", "سیاست روز")
        self.assertFalse(ok)
        self.assertIn("از قبل هست", reply)
        # اسم موضوع سفارشی نمی‌تونه برچسب موضوع اصلی بشه
        self.state["custom_topics"]["سینما"] = {"name": "سینما", "keywords": ["فیلم"]}
        ok, reply = fb._rename_topic(self.state, "سینما", "🔴 سیاست روز")
        self.assertFalse(ok)
        self.assertIn("از قبل هست", reply)
        # موضوع حذف‌شده رو نمی‌شه ویرایش کرد
        self.state["disabled_topics"] = ["sport"]
        self.send(ADMIN, "/renametopic ورزشی")
        self.assertIn("حذف شده", self.last_text())

    def test_broadcast_uses_renamed_label(self):
        self.send(USER_A, "/follow سیاسی")
        self.send(USER_B, "/follow ورزشی")
        ok, _ = fb._rename_topic(self.state, "political", "سیاست روز")
        self.assertTrue(ok)
        self.send(ADMIN, "/broadcast 🔴 سیاست روز خبر سیاسی")
        got = [s for s in self.fake.sent if s[1] == "خبر سیاسی"]
        self.assertEqual([c for c, _, _ in got], [USER_A])
        self.assertIn("به 1 نفر رسید", self.last_text())
        # اسم قدیمیِ بدون ایموجی انتخاب‌کننده نیست؛ کل متن به همه می‌رسه
        self.send(ADMIN, "/broadcast سیاسی خبر دوم")
        got = [s for s in self.fake.sent if s[1] == "سیاسی خبر دوم"]
        self.assertEqual({c for c, _, _ in got}, {USER_A, USER_B})
        self.assertIn("به 2 نفر رسید", self.last_text())

    def test_keywords_shows_renamed_label(self):
        ok, _ = fb._rename_topic(self.state, "political", "سیاست روز")
        self.assertTrue(ok)
        self.send(ADMIN, "/keywords")
        self.assertIn("🔴 سیاست روز:", self.last_text())
        self.assertNotIn("🔴 سیاسی:", self.last_text())
        self.send(ADMIN, "/keywords سیاست روز")
        self.assertIn("🔴 سیاست روز:", self.last_text())


    # ---- 0.15: /clear برای کاربرها + هاب /keywords فقط با دو دستور ----
    def test_clear_deletes_forwarded_and_broadcast_news(self):
        self.send(USER_A, "/follow سیاسی")
        self.send(USER_B, "/follow ورزشی")
        fb.handle_channel_post(self.state, make_channel_post(900, "🔴 خبر مهم سیاسی"))
        self.assertEqual(self.fake.forwarded, [(USER_A, 900)])
        self.send(ADMIN, "/broadcast سلام به همه")
        mids_a = list(self.state["sent_messages"][USER_A])
        self.assertEqual(len(mids_a), 2)  # فوروارد + پیام ادمین
        self.assertEqual(len(self.state["sent_messages"][USER_B]), 1)  # فقط پیام ادمین
        self.send(USER_A, "/clear")
        self.assertEqual(self.fake.deleted, [(USER_A, m) for m in mids_a])
        self.assertEqual(self.state["sent_messages"][USER_A], [])
        self.assertIn("2 خبر پاک شد", self.last_text())
        # خبرهای کاربر دیگه دست نخورده
        self.assertEqual(len(self.state["sent_messages"][USER_B]), 1)
        self.assertEqual([d for d in self.fake.deleted if d[0] == USER_B], [])

    def test_clear_with_nothing_to_delete(self):
        self.send(USER_A, "/clear")
        self.assertIn("خبری برای پاک کردن نیست", self.last_text())
        self.assertEqual(self.fake.deleted, [])

    def test_clear_reports_messages_it_could_not_delete(self):
        self.send(USER_A, "/follow سیاسی")
        fb.handle_channel_post(self.state, make_channel_post(901, "🔴 خبر مهم سیاسی"))
        mid = self.state["sent_messages"][USER_A][0]
        self.fake.delete_fail_ids.add(mid)
        self.send(USER_A, "/clear")
        self.assertEqual(self.fake.deleted, [(USER_A, mid)])
        self.assertIn("0 خبر پاک شد", self.last_text())
        self.assertIn("قابل پاک کردن نبود", self.last_text())
        self.assertEqual(self.state["sent_messages"][USER_A], [])

    def test_clear_button_on_member_keyboard_not_admin(self):
        texts = [b["text"] for row in fb.main_keyboard(USER_A, self.state)["keyboard"] for b in row]
        self.assertIn("🗑 پاک کردن همه‌ی خبرها", texts)
        texts = [b["text"] for row in fb.main_keyboard(ADMIN, self.state)["keyboard"] for b in row]
        self.assertNotIn("🗑 پاک کردن همه‌ی خبرها", texts)

    def test_clear_button_tap_works(self):
        self.send(USER_A, "/follow سیاسی")
        fb.handle_channel_post(self.state, make_channel_post(902, "🔴 خبر مهم سیاسی"))
        self.send(USER_A, "🗑 پاک کردن همه‌ی خبرها")
        self.assertEqual(len(self.fake.deleted), 1)
        self.assertIn("پاک شد", self.last_text())

    def test_back_button_returns_to_main_menu(self):
        self.send(ADMIN, "/keywords")
        self.send(ADMIN, "🔙 منوی اصلی")
        markup = json.loads(self.fake.sent[-1][2])
        texts = [b["text"] for row in markup["keyboard"] for b in row]
        self.assertIn("👥 دنبال‌کننده‌ها", texts)
        self.assertIn("🔑 کلمه‌های کلیدی", texts)

    def test_record_sent_caps_per_user(self):
        for i in range(305):
            fb._record_sent(self.state, USER_A, i + 1)
        self.assertEqual(len(self.state["sent_messages"][USER_A]), 300)
        self.assertEqual(self.state["sent_messages"][USER_A][0], 6)

    def test_help_lists_clear(self):
        self.send(USER_A, "/help")
        self.assertIn("🗑 پاک کردن همه‌ی خبرها", self.last_text())
        self.send(USER_A, "/help /clear")
        self.assertIn("پاک کردن همه‌ی خبرها", self.last_text())

    # ---- 0.16: commit_state هیچ‌وقت فایل خراب/درگیر رو کامیت نمی‌کنه ----
    def _run_commit_state(self, pull_rc=0, diff_rc=1, push_rc=0):
        fake_git = FakeGit(pull_rc=pull_rc, diff_rc=diff_rc, push_rc=push_rc)
        orig_subprocess = fb.subprocess
        orig_env = os.environ.get("GITHUB_ACTIONS")
        fb.subprocess = fake_git
        os.environ["GITHUB_ACTIONS"] = "true"
        try:
            fb.commit_state()
        finally:
            fb.subprocess = orig_subprocess
            if orig_env is None:
                os.environ.pop("GITHUB_ACTIONS", None)
            else:
                os.environ["GITHUB_ACTIONS"] = orig_env
        return fake_git

    def test_commit_state_skips_on_pull_conflict(self):
        fb.save_state(self.state)
        fake_git = self._run_commit_state(pull_rc=1)
        self.assertIn(["rebase", "--abort"], fake_git.calls)
        self.assertNotIn("add", [c[0] for c in fake_git.calls])
        self.assertNotIn("commit", [c[0] for c in fake_git.calls])

    def test_commit_state_skips_on_merge_markers(self):
        with open(self.state_path, "w", encoding="utf-8") as f:
            f.write("<<<<<<< Updated upstream\ngAAAAABx\n=======\ngAAAAABy\n>>>>>>> Stashed changes\n")
        fake_git = self._run_commit_state()
        self.assertNotIn("add", [c[0] for c in fake_git.calls])
        self.assertNotIn("commit", [c[0] for c in fake_git.calls])

    def test_commit_state_skips_when_file_does_not_decrypt(self):
        with open(self.state_path, "w", encoding="utf-8") as f:
            f.write("not-a-fernet-token\n")
        fake_git = self._run_commit_state()
        self.assertNotIn("add", [c[0] for c in fake_git.calls])
        self.assertNotIn("commit", [c[0] for c in fake_git.calls])

    def test_commit_state_happy_path(self):
        fb.save_state(self.state)
        fake_git = self._run_commit_state()
        self.assertIn("add", [c[0] for c in fake_git.calls])
        self.assertIn(["commit", "-m", "update followers"], fake_git.calls)
        self.assertIn(["push"], fake_git.calls)

    def test_commit_state_noop_when_nothing_changed(self):
        fb.save_state(self.state)
        fake_git = self._run_commit_state(diff_rc=0)
        self.assertIn("add", [c[0] for c in fake_git.calls])
        self.assertNotIn("commit", [c[0] for c in fake_git.calls])
        self.assertNotIn("push", [c[0] for c in fake_git.calls])


    # ---- 0.17: «انتخاب موضوع» با تیک سبز و تاگل ----
    def test_member_keyboard_has_select_button_no_topics(self):
        texts = [b["text"] for row in fb.main_keyboard(USER_A, self.state)["keyboard"] for b in row]
        self.assertIn("📌 انتخاب موضوع", texts)
        self.assertNotIn("🔴 سیاسی", texts)
        self.assertNotIn("⚽ ورزشی", texts)
        self.assertNotIn("🟡 طلا و دلار", texts)
        texts = [b["text"] for row in fb.main_keyboard(ADMIN, self.state)["keyboard"] for b in row]
        self.assertNotIn("📌 انتخاب موضوع", texts)

    def test_select_picker_ticks_followed_topics(self):
        self.send(USER_A, "/follow سیاسی")
        self.send(USER_A, "📌 انتخاب موضوع")
        markup = json.loads(self.fake.sent[-1][2])
        texts = [b["text"] for row in markup["keyboard"] for b in row]
        self.assertIn("✅ 🔴 سیاسی", texts)
        self.assertIn("⚽ ورزشی", texts)
        self.assertNotIn("✅ ⚽ ورزشی", texts)
        self.assertIn("🟡 طلا و دلار", texts)

    def test_select_toggle_unfollows(self):
        self.send(USER_A, "/follow سیاسی")
        self.send(USER_A, "📌 انتخاب موضوع")
        self.send(USER_A, "✅ 🔴 سیاسی")
        self.assertEqual(self.state["users"][USER_A]["topics"], [])
        self.assertIn("لغو شد", self.last_text())
        markup = json.loads(self.fake.sent[-1][2])
        texts = [b["text"] for row in markup["keyboard"] for b in row]
        self.assertIn("🔴 سیاسی", texts)
        self.assertNotIn("✅ 🔴 سیاسی", texts)

    def test_select_toggle_on_and_off(self):
        self.send(USER_A, "📌 انتخاب موضوع")
        self.send(USER_A, "⚽ ورزشی")
        self.assertEqual(self.state["users"][USER_A]["topics"], ["sport"])
        self.assertIn("فعال شد", self.last_text())
        markup = json.loads(self.fake.sent[-1][2])
        texts = [b["text"] for row in markup["keyboard"] for b in row]
        self.assertIn("✅ ⚽ ورزشی", texts)
        self.send(USER_A, "✅ ⚽ ورزشی")
        self.assertEqual(self.state["users"][USER_A]["topics"], [])
        self.assertIn("لغو شد", self.last_text())

    def test_select_command_opens_picker(self):
        self.send(USER_A, "/select")
        markup = json.loads(self.fake.sent[-1][2])
        texts = [b["text"] for row in markup["keyboard"] for b in row]
        self.assertIn("🔴 سیاسی", texts)
        self.assertIn("❌ لغو", texts)

    def test_follow_opens_select_picker_with_ticks(self):
        self.send(USER_A, "/follow ورزشی")
        self.send(USER_A, "/follow")
        self.assertIn("کدوم موضوع", self.last_text())
        markup = json.loads(self.fake.sent[-1][2])
        texts = [b["text"] for row in markup["keyboard"] for b in row]
        self.assertIn("✅ ⚽ ورزشی", texts)
        self.send(USER_A, "طلا و دلار")
        self.assertEqual(self.state["users"][USER_A]["topics"], ["sport", "gold"])

    def test_select_picker_stays_open_after_toggle(self):
        self.send(USER_A, "📌 انتخاب موضوع")
        self.send(USER_A, "🔴 سیاسی")
        self.send(USER_A, "⚽ ورزشی")
        self.assertEqual(self.state["users"][USER_A]["topics"], ["political", "sport"])
        # لغو از داخل انتخاب‌گر برمی‌گرده به منوی اصلی
        self.send(USER_A, "❌ لغو")
        self.assertIn("لغو شد", self.last_text())
        markup = json.loads(self.fake.sent[-1][2])
        texts = [b["text"] for row in markup["keyboard"] for b in row]
        self.assertIn("📌 انتخاب موضوع", texts)

    def test_help_lists_select(self):
        self.send(USER_A, "/help")
        self.assertIn("📌 انتخاب موضوع", self.last_text())
        self.send(USER_A, "/help /select")
        self.assertIn("✅", self.last_text())

    # ---- 0.18: emoji anywhere in the post classifies it (all built-in topics) ----
    def test_classify_political_by_emoji_anywhere(self):
        # 🔴 وسط پست، نه اولش؛ هیچ کلمه‌ی کلیدی سیاسی هم نیست
        self.assertEqual(fb.classify_post("✈️ پرواز تهران به مشهد 🔴"), {"political"})
        self.assertEqual(fb.classify_post("🔵 متن خبر 🔴 ادامه‌ی خبر"), {"political"})

    def test_classify_political_by_alert_emoji_anywhere(self):
        self.assertEqual(fb.classify_post("✈️ گزارش ویژه 🚨"), {"political"})
        self.assertEqual(fb.classify_post("🔵 متن خبر 🔥 ادامه"), {"political"})

    def test_classify_sport_by_emoji_anywhere(self):
        self.assertEqual(fb.classify_post("🏆 نتیجه‌ی مسابقه ⚽"), {"sport"})

    def test_classify_gold_by_emoji_anywhere(self):
        self.assertEqual(fb.classify_post("📈 قیمت‌ها 🟡 طلا و دلار"), {"gold"})

    def test_classify_gold_still_needs_money_keyword(self):
        # 🟡 هر جای پست باشه ولی کلمه‌ی طلا/دلار نباشه → طلا و دلار نیست
        self.assertEqual(fb.classify_post("📈 قیمت‌ها 🟡 بورس تهران"), set())

    def test_classify_emoji_beyond_head_ignored(self):
        # ایموجی بعد از ۶۰۰ کاراکتر اول (تیتر + اول متن) حساب نمی‌شه
        self.assertEqual(fb.classify_post("🔵 " + "متن " * 200 + "🔴"), set())

    # ---- 0.30: counts reset with the channel's night post, not at midnight ----
    def test_forward_counts_kept_across_midnight(self):
        # پست بعد از نیمه‌شب و قبل از شب بخیر هنوز مال روز قبلی‌ست؛ ریست نمی‌شه
        self.send(USER_A, "/follow سیاسی")
        self.state["topic_forward_counts"] = {"political": 3}
        self.state["topic_forward_day"] = tehran_date(1)
        fb.handle_channel_post(self.state, make_channel_post(900, "🔴 خبر مهم سیاسی"))
        self.assertEqual(self.state["topic_forward_counts"], {"political": 4})
        self.assertEqual(self.state["topic_forward_day"], tehran_date(1))
        self.assertEqual(self.state.get("forward_stats_week", []), [])

    def test_forward_counts_kept_same_day(self):
        self.send(USER_A, "/follow سیاسی")
        self.state["topic_forward_counts"] = {"political": 3}
        self.state["topic_forward_day"] = fb._today_tehran()
        fb.handle_channel_post(self.state, make_channel_post(901, "🔴 خبر مهم سیاسی"))
        self.assertEqual(self.state["topic_forward_counts"], {"political": 4})

    def test_followers_view_keeps_counts_across_midnight(self):
        # دیدن /followers دیگه به‌خاطر عوض شدن تاریخ آمار رو صفر نمی‌کنه
        self.send(USER_A, "/follow سیاسی")
        self.state["topic_forward_counts"] = {"political": 3}
        self.state["topic_forward_day"] = tehran_date(1)
        self.send(ADMIN, "/followers")
        tail = self.last_text().split("📊 خبرهای ارسال‌شده برای هر موضوع:")[1]
        self.assertIn("🔴 سیاسی: 3", tail)
        self.assertEqual(self.state["topic_forward_counts"], {"political": 3})


    # ---- 0.19: welcome points to the menu; /send is a menu button ----
    def test_welcome_points_to_menu_not_commands(self):
        txt = fb.welcome_text(False, self.state)
        self.assertIn("منوی پایین", txt)
        self.assertNotIn("/follow", txt)
        self.assertNotIn("/send", txt)
        self.assertNotIn("/help", txt)
        self.assertIn("سیاسی", txt)  # topics are still listed

    def test_admin_welcome_unchanged(self):
        txt = fb.welcome_text(True, self.state)
        self.assertIn("مدیریت", txt)
        self.assertIn(fb.BOT_VERSION, txt)

    def test_member_keyboard_has_send_button(self):
        texts = [b["text"] for row in fb.main_keyboard(USER_A, self.state)["keyboard"] for b in row]
        self.assertIn("📤 ارسال خبر", texts)
        self.assertLess(texts.index("📋 موضوع‌های من"), texts.index("📤 ارسال خبر"))
        self.assertLess(texts.index("📤 ارسال خبر"), texts.index("🗑 پاک کردن همه‌ی خبرها"))
        self.assertEqual(texts, ["📌 انتخاب موضوع", "📋 موضوع‌های من", "📤 ارسال خبر",
                                 "🗑 پاک کردن همه‌ی خبرها", "❓ راهنما"])

    def test_admin_keyboard_has_no_send_button(self):
        texts = [b["text"] for row in fb.main_keyboard(ADMIN, self.state)["keyboard"] for b in row]
        self.assertNotIn("📤 ارسال خبر", texts)

    def test_send_button_prompts_then_forwards(self):
        self.send(USER_A, "📤 ارسال خبر")
        self.assertIn("خبر، عکس یا فیلمت", self.last_text())
        self.send_as(USER_A, "خبر با دکمه", message_id=12)
        self.assertIn((ADMIN, 12), self.fake.forwarded)

    def test_send_button_help_mentions_button(self):
        self.assertIn("📤 ارسال خبر", fb.COMMAND_HELP["/send"])

    # ---- 0.21: per-topic blocked words/emojis, one /block hub command ----
    def test_blocked_hit_word(self):
        self.assertEqual(fb._blocked_hit("🔴 قیمت طلا امروز بالا رفت", ["طلا"]), "طلا")
        self.assertIsNone(fb._blocked_hit("🔴 قیمت سکه امروز بالا رفت", ["طلا"]))

    def test_blocked_hit_phrase_ignores_zwnj_and_spaces(self):
        self.assertEqual(fb._blocked_hit("🔴 قیمت‌طلا امروز بالا رفت", ["قیمت طلا"]), "قیمت طلا")
        self.assertEqual(fb._blocked_hit("🔴 قیمت طلا امروز بالا رفت", ["قیمت‌طلا"]), "قیمت‌طلا")

    def test_blocked_hit_emoji_anywhere(self):
        self.assertEqual(fb._blocked_hit("🔴 خبر فوری 🚫 ادامه دارد", ["🚫"]), "🚫")
        self.assertIsNone(fb._blocked_hit("🔴 خبر فوری ادامه دارد", ["🚫"]))

    def test_blocked_hit_empty(self):
        self.assertIsNone(fb._blocked_hit("🔴 خبر", []))
        self.assertIsNone(fb._blocked_hit("", ["طلا"]))

    def test_parse_block_args_longest_prefix(self):
        self.assertEqual(fb._parse_block_args("/block طلا و دلار سکه", self.state),
                         ("gold", ["سکه"]))
        self.assertEqual(fb._parse_block_args("/block سیاسی موشکی", self.state),
                         ("political", ["موشکی"]))
        self.assertEqual(fb._parse_block_args("/block سیاسی", self.state),
                         ("political", []))
        self.assertEqual(fb._parse_block_args("/block موضوع‌ناموجود کلمه", self.state),
                         (None, None))
        self.assertEqual(fb._parse_block_args("/block", self.state), (None, None))

    def test_block_direct_adds_to_that_topic_only(self):
        self.send(ADMIN, "/block سیاسی موشکی 🚫")
        self.assertEqual(self.state["topic_blocked"], {"political": ["موشکی", "🚫"]})
        self.assertIn("ممنوع شد", self.last_text())
        self.assertIn("سیاسی", self.last_text())
        self.send(ADMIN, "/block سیاسی موشکی")
        self.assertEqual(self.state["topic_blocked"], {"political": ["موشکی", "🚫"]})
        self.assertIn("از قبل ممنوع بود", self.last_text())
        self.send(ADMIN, "/block ورزشی فوتبال")
        self.assertEqual(self.state["topic_blocked"],
                         {"political": ["موشکی", "🚫"], "sport": ["فوتبال"]})

    def test_block_direct_comma_phrases(self):
        self.send(ADMIN, "/block طلا و دلار قیمت طلا، دلار آمریکا")
        self.assertEqual(self.state["topic_blocked"], {"gold": ["قیمت طلا", "دلار آمریکا"]})

    def test_block_direct_unknown_topic(self):
        self.send(ADMIN, "/block موضوع‌ناموجود کلمه")
        self.assertIn("چنین موضوعی نداریم", self.last_text())
        self.assertEqual(self.state.get("topic_blocked", {}), {})

    def test_block_no_args_opens_topic_picker(self):
        self.send(ADMIN, "/block")
        self.assertIn("کدوم موضوع", self.last_text())
        self.send(ADMIN, "سیاسی")
        self.assertIn("ممنوع برای", self.last_text())
        self.assertIn("سیاسی", self.last_text())

    def test_block_hub_add_flow(self):
        self.send(ADMIN, "/block")
        self.send(ADMIN, "سیاسی")
        self.send(ADMIN, "➕ اضافه کردن ممنوع")
        self.assertIn("بفرست", self.last_text())
        self.send(ADMIN, "موشکی 🚫")
        self.assertEqual(self.state["topic_blocked"], {"political": ["موشکی", "🚫"]})
        self.assertIn("ممنوع شد", self.last_text())

    def test_block_hub_remove_flow(self):
        self.send(ADMIN, "/block سیاسی موشکی 🚫 دلار")
        self.send(ADMIN, "/block سیاسی")
        self.send(ADMIN, "➖ حذف کردن ممنوع")
        self.assertIn("حذف کنم", self.last_text())
        self.send(ADMIN, "🚫")
        self.assertEqual(self.state["topic_blocked"], {"political": ["موشکی", "دلار"]})
        self.assertIn("حذف شد", self.last_text())
        # تایپِ موردی که نیست، دوباره می‌پرسه
        self.send(ADMIN, "➖ حذف کردن ممنوع")
        self.send(ADMIN, "سکه")
        self.assertIn("توی ممنوع‌ها نیست", self.last_text())
        self.assertEqual(self.state["topic_blocked"], {"political": ["موشکی", "دلار"]})

    def test_block_hub_remove_empty(self):
        self.send(ADMIN, "/block سیاسی")
        self.send(ADMIN, "➖ حذف کردن ممنوع")
        self.assertIn("چیزی ممنوع نیست", self.last_text())

    def test_block_button_opens_picker_for_admin(self):
        self.send(ADMIN, "🚫 کلمات ممنوع")
        self.assertIn("کدوم موضوع", self.last_text())
        texts = [b["text"] for row in fb.main_keyboard(ADMIN, self.state)["keyboard"] for b in row]
        self.assertIn("🚫 کلمات ممنوع", texts)
        # دکمه‌ی ممنوع‌ها قبل از راهنماست
        self.assertLess(texts.index("🚫 کلمات ممنوع"), texts.index("❓ راهنما"))
        texts = [b["text"] for row in fb.main_keyboard(USER_A, self.state)["keyboard"] for b in row]
        self.assertNotIn("🚫 کلمات ممنوع", texts)

    def test_block_admin_only(self):
        self.send(USER_A, "/block")
        self.assertIn("فقط برای ادمینه", self.last_text())
        self.send(USER_A, "/block سیاسی موشکی")
        self.assertIn("فقط برای ادمینه", self.last_text())
        self.assertEqual(self.state.get("topic_blocked", {}), {})

    def test_blocked_topic_only_post_not_forwarded_to_it(self):
        self.send(USER_A, "/follow سیاسی")
        self.send(USER_B, "/follow ورزشی")
        self.send(ADMIN, "/block سیاسی موشکی")
        # پست دو موضوعی: برای سیاسی ممنوع، برای ورزشی می‌رسه
        fb.handle_channel_post(self.state, make_channel_post(
            900, "🔴 فوری: حمله موشکی؛ تیم فوتبال پرسپولیس به فینال رفت"))
        self.assertEqual(self.fake.forwarded, [(USER_B, 900)])
        self.assertIn(900, self.state["seen_posts"])
        # پست بعدی بدون کلمه‌ی ممنوع به هر دو می‌رسه
        fb.handle_channel_post(self.state, make_channel_post(
            901, "🔴 فوری: حمله جدید؛ تیم فوتبال پرسپولیس به فینال رفت"))
        self.assertEqual(self.fake.forwarded, [(USER_B, 900), (USER_A, 901), (USER_B, 901)])

    def test_blocked_emoji_per_topic(self):
        self.send(USER_A, "/follow سیاسی")
        self.send(ADMIN, "/block سیاسی 🚫")
        fb.handle_channel_post(self.state, make_channel_post(902, "🔴 فوری: خبر مهم 🚫"))
        self.assertEqual(self.fake.forwarded, [])
        fb.handle_channel_post(self.state, make_channel_post(903, "🔴 فوری: خبر مهم"))
        self.assertEqual(self.fake.forwarded, [(USER_A, 903)])

    def test_blocked_post_counts_only_forwarded_topics(self):
        self.send(USER_A, "/follow سیاسی")
        self.send(USER_B, "/follow ورزشی")
        self.send(ADMIN, "/block سیاسی موشکی")
        fb.handle_channel_post(self.state, make_channel_post(
            904, "🔴 فوری: حمله موشکی؛ تیم فوتبال پرسپولیس به فینال رفت"))
        self.assertEqual(self.state.get("topic_forward_counts", {}), {"sport": 1})

    def test_blocked_then_removed_forwards_again(self):
        self.send(USER_A, "/follow سیاسی")
        self.send(ADMIN, "/block سیاسی موشکی")
        self.send(ADMIN, "/block سیاسی")
        self.send(ADMIN, "➖ حذف کردن ممنوع")
        self.send(ADMIN, "موشکی")
        fb.handle_channel_post(self.state, make_channel_post(905, "🔴 فوری: حمله موشکی جدید"))
        self.assertEqual(self.fake.forwarded, [(USER_A, 905)])

    def test_no_unblock_or_blocklist_commands(self):
        # هر سه کار توی یه دستور /blockه؛ دستورهای جدا نباید وجود داشته باشن
        self.assertNotIn("/unblock", fb.COMMAND_HELP)
        self.assertNotIn("/blocklist", fb.COMMAND_HELP)
        self.assertIn("/block", fb.COMMAND_HELP)

    def test_help_block_admin_only(self):
        self.send(ADMIN, "/help /block")
        self.assertIn("/block", self.last_text())
        self.send(USER_A, "/help /block")
        self.assertNotIn("ممنوع", self.last_text())

    # ---- 0.22: /rules shows all admin customizations ----
    def test_rules_text_full(self):
        self.send(ADMIN, "/addtopic سینما فیلم سینما")
        self.send(ADMIN, "/addtopic سیاسی کودتا")
        self.send(ADMIN, "/block سیاسی موشکی 🚫")
        self.send(ADMIN, "/block سینما خشونت")
        self.state["topic_removed_keywords"] = {"political": ["مجلس"]}
        self.send(ADMIN, "/removetopic ورزشی")
        txt = fb._rules_text(self.state)
        self.assertIn("📜 تنظیمات فعلی", txt)
        self.assertIn("🔴 سیاسی:", txt)
        self.assertIn("➕ اضافه‌شده: کودتا", txt)
        self.assertIn("➖ حذف‌شده: مجلس", txt)
        self.assertNotIn("مجلس", txt.split("کلمه‌ها:")[1].split("\n")[0])
        self.assertIn("🚫 ممنوع: موشکی، 🚫", txt)
        self.assertIn("سینما:", txt)
        self.assertIn("کلمه‌ها: فیلم، سینما", txt)
        self.assertIn("🚫 ممنوع: خشونت", txt)
        self.assertIn("❌ موضوع‌های حذف‌شده:", txt)
        self.assertIn("ورزشی", txt)

    def test_rules_text_empty_state(self):
        txt = fb._rules_text(self.state)
        self.assertIn("📜 تنظیمات فعلی", txt)
        self.assertIn("🔴 سیاسی:", txt)
        self.assertIn("⚽ ورزشی:", txt)
        self.assertIn("🟡 طلا و دلار:", txt)
        self.assertNotIn("🚫 ممنوع", txt)
        self.assertNotIn("حذف‌شده", txt)

    def test_rules_command_admin_only(self):
        self.send(ADMIN, "/rules")
        self.assertIn("📜 تنظیمات فعلی", self.last_text())
        self.send(ADMIN, "📜 قوانین")
        self.assertIn("📜 تنظیمات فعلی", self.last_text())
        self.send(USER_A, "/rules")
        self.assertIn("فقط برای ادمینه", self.last_text())
        self.send(USER_A, "📜 قوانین")
        self.assertIn("فقط برای ادمینه", self.last_text())

    def test_rules_button_is_third_command(self):
        texts = [b["text"] for row in fb.main_keyboard(ADMIN, self.state)["keyboard"] for b in row]
        self.assertEqual(texts[2], "📜 قوانین")
        self.assertEqual(texts[:3], ["👥 دنبال‌کننده‌ها", "📣 پیام به اعضا", "📜 قوانین"])
        self.assertEqual(texts[3], "🔧 تغییرات موضوع")
        texts = [b["text"] for row in fb.main_keyboard(USER_A, self.state)["keyboard"] for b in row]
        self.assertNotIn("📜 قوانین", texts)

    def test_help_rules_admin_only(self):
        self.send(ADMIN, "/help /rules")
        self.assertIn("/rules", self.last_text())
        self.send(USER_A, "/help /rules")
        self.assertNotIn("تنظیمات", self.last_text())

    # ---- 0.23: no mid-run git commits (the run #40 killer) ----
    def test_main_commits_only_at_end(self):
        # با کد قدیمی وسط حلقه هم کامیت می‌شد (last_commit=0 → فوری) پس ۲ بار
        # صدا زده می‌شد؛ حالا فقط کامیت پایانی (۱ بار) باید باشد.
        calls = {"run_once": 0, "commit": 0}
        orig_run_once = fb.run_once
        orig_commit = fb.commit_state
        orig_token, orig_loop = fb.TOKEN, fb.LOOP_MINUTES
        fb.run_once = lambda: calls.__setitem__("run_once", calls["run_once"] + 1)
        fb.commit_state = lambda: calls.__setitem__("commit", calls["commit"] + 1)
        fb.TOKEN, fb.LOOP_MINUTES = "x", 0
        try:
            fb.main()
        finally:
            fb.run_once = orig_run_once
            fb.commit_state = orig_commit
            fb.TOKEN, fb.LOOP_MINUTES = orig_token, orig_loop
        self.assertEqual(calls["run_once"], 1)
        self.assertEqual(calls["commit"], 1)

    # ---- 0.24: دنبال کردن با کلمه‌ی دلخواه (داخل «انتخاب موضوع») ----
    def _open_followword_hub(self):
        self.send(USER_A, "📌 انتخاب موضوع")
        self.send(USER_A, "🔍 کلمه‌های من")

    def test_select_picker_has_followword_button_before_cancel(self):
        kb = fb._select_topics_keyboard(self.state, USER_A)
        rows = kb["keyboard"]
        self.assertEqual(rows[-2], [{"text": "🔍 کلمه‌های من"}])
        self.assertEqual(rows[-1], [{"text": "❌ لغو"}])

    def test_followword_button_explains_what_it_is(self):
        self._open_followword_hub()
        self.assertIn("کلمه‌ی دلخواه", self.last_text())
        self.assertIn("پرسپولیس", self.last_text())
        self.assertEqual(self.state["pending"][USER_A]["cmd"], "/followword")

    def test_followword_add_via_hub(self):
        self._open_followword_hub()
        self.send(USER_A, "➕ اضافه کردن کلمه")
        self.send(USER_A, "پرسپولیس")
        self.assertIn("پرسپولیس", fb._user_keywords(self.state, USER_A))
        self.assertIn("اضافه شد", self.last_text())

    def test_followword_phrase_stays_one_keyword(self):
        self._open_followword_hub()
        self.send(USER_A, "➕ اضافه کردن کلمه")
        self.send(USER_A, "تیم ملی")
        self.assertEqual(fb._user_keywords(self.state, USER_A), ["تیم ملی"])

    def test_followword_add_comma_list(self):
        self._open_followword_hub()
        self.send(USER_A, "➕ اضافه کردن کلمه")
        self.send(USER_A, "پرسپولیس، استقلال")
        self.assertEqual(fb._user_keywords(self.state, USER_A), ["پرسپولیس", "استقلال"])

    def test_followword_rejects_dup_short_and_over_limit(self):
        self._open_followword_hub()
        self.send(USER_A, "➕ اضافه کردن کلمه")
        self.send(USER_A, "پرسپولیس")
        self.send(USER_A, "➕ اضافه کردن کلمه")
        self.send(USER_A, "پرسپولیس")
        self.assertIn("از قبل", self.last_text())
        self.send(USER_A, "➕ اضافه کردن کلمه")
        self.send(USER_A, "ا")
        self.assertIn("نشد", self.last_text())
        for w in ["آبی", "قرمز", "سبز", "زرد"]:
            self.send(USER_A, "➕ اضافه کردن کلمه")
            self.send(USER_A, w)
        self.assertEqual(len(fb._user_keywords(self.state, USER_A)), 5)
        self.send(USER_A, "➕ اضافه کردن کلمه")
        self.send(USER_A, "بنفش")
        self.assertIn("نشد", self.last_text())
        self.assertEqual(len(fb._user_keywords(self.state, USER_A)), 5)

    def test_followword_remove_and_back_to_picker(self):
        self._open_followword_hub()
        self.send(USER_A, "➕ اضافه کردن کلمه")
        self.send(USER_A, "پرسپولیس")
        self.send(USER_A, "➖ حذف کلمه")
        self.send(USER_A, "پرسپولیس")
        self.assertEqual(fb._user_keywords(self.state, USER_A), [])
        self.assertIn("حذف شد", self.last_text())
        self.send(USER_A, "🔙 برگشت به انتخاب موضوع")
        self.assertEqual(self.state["pending"][USER_A]["cmd"], "/select")

    def test_keyword_post_forwarded_without_topic(self):
        self._open_followword_hub()
        self.send(USER_A, "➕ اضافه کردن کلمه")
        self.send(USER_A, "پرسپولیس")
        fb.handle_channel_post(self.state, make_channel_post(
            950, "🔵 فیلم تازه اکران شد؛ پرسپولیس مهمان برنامه است"))
        self.assertEqual(self.fake.forwarded, [(USER_A, 950)])

    def test_keyword_and_topic_post_forwarded_once(self):
        self.send(USER_A, "/follow ورزشی")
        self._open_followword_hub()
        self.send(USER_A, "➕ اضافه کردن کلمه")
        self.send(USER_A, "پرسپولیس")
        fb.handle_channel_post(self.state, make_channel_post(951, "⚽ پرسپولیس قهرمان شد"))
        self.assertEqual(self.fake.forwarded, [(USER_A, 951)])

    def test_keyword_hit_ignores_zwnj(self):
        self.assertTrue(fb._keyword_hit("تیم‌ملی به فینال رفت", "تیم ملی"))
        self.assertFalse(fb._keyword_hit("خبر مهم سیاسی", "پرسپولیس"))

    def test_followers_lists_keyword_only_user(self):
        self._open_followword_hub()
        self.send(USER_A, "➕ اضافه کردن کلمه")
        self.send(USER_A, "پرسپولیس")
        self.send(ADMIN, "/followers")
        self.assertIn("🔍 پرسپولیس", self.last_text())

    # ---- 0.25: تعداد کلمه‌دلخواه‌ها + نمایش کلمه‌ها + شماره‌گذاری در /followers ----
    def test_followers_shows_keyword_user_count(self):
        self.state["users"]["111"] = {"topics": [], "keywords": ["پرسپولیس"], "name": "علی"}
        self.state["users"]["222"] = {"topics": ["sport"], "keywords": ["آیفون", "تیم ملی"], "name": "مریم"}
        self.state["users"]["333"] = {"topics": ["political"], "name": "رضا"}
        self.send(ADMIN, "/followers")
        self.assertIn("🔍 کلمه‌ی دلخواه: 2 نفر", self.last_text())

    def test_followers_shows_each_users_keywords(self):
        self.state["users"]["111"] = {"topics": [], "keywords": ["پرسپولیس"], "name": "علی"}
        self.state["users"]["222"] = {"topics": ["sport"], "keywords": ["آیفون", "تیم ملی"], "name": "مریم"}
        self.send(ADMIN, "/followers")
        txt = self.last_text()
        self.assertIn("علی — 🔍 پرسپولیس", txt)
        self.assertIn("مریم — ⚽ ورزشی — 🔍 آیفون، تیم ملی", txt)

    def test_followers_numbers_people(self):
        self.send_as(USER_A, "/follow سیاسی", first_name="علی", username="ali123")
        self.send_as(USER_B, "/follow ورزشی", first_name="مریم")
        self.send(ADMIN, "/followers")
        txt = self.last_text()
        self.assertIn("۱. علی (@ali123) — 🔴 سیاسی", txt)
        self.assertIn("۲. مریم — ⚽ ورزشی", txt)

    # ---- 0.26: کلمه‌های دلخواه در /topics + آمار ارسال کلمه در /followers ----
    def test_topics_shows_keywords_with_topics(self):
        self.send(USER_A, "/follow سیاسی")
        self._open_followword_hub()
        self.send(USER_A, "➕ اضافه کردن کلمه")
        self.send(USER_A, "پرسپولیس")
        self.send(USER_A, "/topics")
        txt = self.last_text()
        self.assertIn("🔴 سیاسی", txt)
        self.assertIn("🔍 کلمه‌های دلخواهت:", txt)
        self.assertIn("• پرسپولیس", txt)

    def test_topics_shows_keywords_only(self):
        self._open_followword_hub()
        self.send(USER_A, "➕ اضافه کردن کلمه")
        self.send(USER_A, "پرسپولیس")
        self.send(USER_A, "/topics")
        txt = self.last_text()
        self.assertIn("🔍 کلمه‌های دلخواهت:", txt)
        self.assertIn("• پرسپولیس", txt)
        self.assertNotIn("هیچ موضوعی", txt)

    def test_keyword_forward_count_counts_keyword_only(self):
        self._open_followword_hub()
        self.send(USER_A, "➕ اضافه کردن کلمه")
        self.send(USER_A, "پرسپولیس")
        self.send(USER_B, "/follow ورزشی")
        fb.handle_channel_post(self.state, make_channel_post(960, "⚽ پرسپولیس قهرمان شد"))
        self.assertEqual(self.fake.forwarded, [(USER_A, 960), (USER_B, 960)])
        self.assertEqual(self.state.get("keyword_forward_count"), 1)
        self.assertEqual(self.state.get("topic_forward_counts", {}), {"sport": 1})

    def test_keyword_forward_not_counted_when_topic_also_matches(self):
        self.send(USER_A, "/follow ورزشی")
        self._open_followword_hub()
        self.send(USER_A, "➕ اضافه کردن کلمه")
        self.send(USER_A, "پرسپولیس")
        fb.handle_channel_post(self.state, make_channel_post(961, "⚽ پرسپولیس قهرمان شد"))
        self.assertEqual(self.fake.forwarded, [(USER_A, 961)])
        self.assertEqual(self.state.get("keyword_forward_count", 0), 0)
        self.assertEqual(self.state.get("topic_forward_counts", {}), {"sport": 1})

    def test_followers_shows_keyword_forward_count(self):
        self.state["keyword_forward_count"] = 7
        self.state["topic_forward_day"] = fb._today_tehran()
        self.send(ADMIN, "/followers")
        tail = self.last_text().split("📊 خبرهای ارسال‌شده برای هر موضوع:")[1]
        self.assertIn("🔍 کلمه‌ی دلخواه: 7", tail)

    def test_keyword_forward_count_resets_on_night_post(self):
        # شمارنده‌ی کلمه‌ی دلخواه با پست شب بخیر صفر می‌شه، نه با عوض شدن تاریخ
        self.send(USER_A, "/follow سیاسی")
        self.state["keyword_forward_count"] = 5
        self.state["topic_forward_day"] = fb._today_tehran()
        fb.handle_channel_post(self.state, make_channel_post(962, "🔴 خبر مهم سیاسی"))
        self.assertEqual(self.state.get("keyword_forward_count"), 5)
        fb.handle_channel_post(self.state, make_channel_post(963, "شب بخیر 🌙"))
        self.assertEqual(self.state.get("keyword_forward_count"), 0)

    # ---- 0.27: راهنمای کامل و دسته‌بندی‌شده برای اعضا و ادمین ----
    def test_help_member_explains_keywords(self):
        self.send(USER_A, "/help")
        txt = self.last_text()
        self.assertIn("کلمه‌ی دلخواه", txt)
        self.assertIn("حداکثر ۵ کلمه", txt)
        self.assertIn("پرسپولیس", txt)
        self.assertIn("عضو کانال", txt)

    def test_help_followword_detail(self):
        self.send(USER_A, "/help /followword")
        txt = self.last_text()
        self.assertIn("کلمه‌ی دلخواه", txt)
        self.assertIn("۵ کلمه", txt)
        self.assertNotIn("راهنمای ربات خبررسان", txt)

    def test_help_admin_explains_keyword_stats(self):
        self.send(ADMIN, "/help")
        txt = self.last_text()
        self.assertIn("کلمه‌ی دلخواه", txt)
        self.assertIn("/followers", txt)
        self.assertNotIn("/follow ", txt)
        self.assertNotIn("/topics", txt)
        self.assertNotIn("/send", txt)

    def test_help_topics_detail_mentions_keywords(self):
        self.send(USER_A, "/help /topics")
        self.assertIn("کلمه‌های دلخواهت", self.last_text())

    # ---- 0.28: شمارش کلمه‌ی دلخواه به‌ازای هر خبر (نه هر نفر) + اجراها پشت سر هم ----
    def test_keyword_forward_count_counts_post_once_for_many_users(self):
        self._open_followword_hub()
        self.send(USER_A, "➕ اضافه کردن کلمه")
        self.send(USER_A, "پرسپولیس")
        self.send(USER_B, "📌 انتخاب موضوع")
        self.send(USER_B, "🔍 کلمه‌های من")
        self.send(USER_B, "➕ اضافه کردن کلمه")
        self.send(USER_B, "پرسپولیس")
        fb.handle_channel_post(self.state, make_channel_post(970, "⚽ پرسپولیس قهرمان شد"))
        self.assertEqual(self.fake.forwarded, [(USER_A, 970), (USER_B, 970)])
        # یه خبر، دو گیرنده‌ی کلمه‌ای — شمارنده فقط یکی بالا می‌ره (مثل موضوع‌ها)
        self.assertEqual(self.state.get("keyword_forward_count"), 1)
        self.assertEqual(self.state.get("topic_forward_counts", {}), {})

    def test_workflow_runs_serialize_instead_of_cancelling(self):
        path = os.path.join(os.path.dirname(__file__), "..", ".github",
                            "workflows", "follow.yml")
        with open(path, encoding="utf-8") as f:
            text = f.read()
        self.assertIn("cancel-in-progress: false", text)

    # ---- 0.29: /recent — خبرهای دو ساعت اخیر + حذف برای گیرنده‌ها ----
    def _follow_and_post(self, mid=790, text="🔴 خبر مهم سیاسی"):
        self.send(USER_A, "/follow سیاسی")
        self.send(USER_B, "/follow ورزشی")
        fb.handle_channel_post(self.state, make_channel_post(mid, text))
        return self.state.get("recent_forwards", [])

    def test_recent_records_forwarded_post_with_targets(self):
        recs = self._follow_and_post()
        self.assertEqual(len(recs), 1)
        self.assertEqual(recs[0]["post_id"], 790)
        self.assertIn("خبر مهم سیاسی", recs[0]["title"])
        # فقط دنبال‌کننده‌ی سیاسی خبر رو گرفته، نه ورزشی
        self.assertEqual(set(recs[0]["targets"]), {USER_A})
        self.assertIsInstance(list(recs[0]["targets"].values())[0], int)
        # خبر ورزشی جداگانه ثبت می‌شه
        fb.handle_channel_post(self.state, make_channel_post(791, "⚽ پرسپولیس قهرمان شد"))
        self.assertEqual(len(self.state["recent_forwards"]), 2)
        self.assertEqual(set(self.state["recent_forwards"][1]["targets"]), {USER_B})

    def test_recent_lists_only_last_two_hours(self):
        now = time.time()
        self.state["recent_forwards"] = [
            {"post_id": 800, "ts": now - 3 * 3600, "title": "خبر خیلی قدیمی",
             "targets": {USER_A: 5}},
            {"post_id": 801, "ts": now - 3700, "title": "خبر یک ساعت پیش",
             "targets": {USER_A: 6}},
            {"post_id": 802, "ts": now - 60, "title": "خبر تازه",
             "targets": {USER_A: 7}},
        ]
        recs = fb._recent_forwards(self.state)
        self.assertEqual([r["post_id"] for r in recs], [802, 801])
        txt = fb._recent_text(self.state)
        self.assertIn("خبر تازه", txt)
        self.assertIn("خبر یک ساعت پیش", txt)
        self.assertNotIn("خبر خیلی قدیمی", txt)
        self.assertIn("۱ نفر", txt)

    def test_recent_admin_only(self):
        self.send(USER_A, "/recent")
        self.assertIn("این دستور فقط برای ادمینه", self.last_text())
        self.send(USER_A, "🕐 خبرهای ساعت اخیر")
        self.assertIn("این دستور فقط برای ادمینه", self.last_text())

    def test_recent_empty_for_admin(self):
        self.send(ADMIN, "/recent")
        self.assertIn("در دو ساعت اخیر خبری", self.last_text())
        self.assertNotIn("/recent", self.state.get("pending", {}))

    def test_recent_delete_flow_by_number(self):
        self._follow_and_post()
        targets = dict(self.state["recent_forwards"][0]["targets"])
        self.send(ADMIN, "/recent")
        self.assertIn("خبر مهم سیاسی", self.last_text())
        self.assertIn("۱ نفر", self.last_text())
        markup = json.loads(self.fake.sent[-1][2])
        texts = [b["text"] for row in markup["keyboard"] for b in row]
        self.assertIn("🗑 حذف ۱", texts)
        self.send(ADMIN, "۱")
        self.assertEqual(set(self.fake.deleted),
                         {(uid, mid) for uid, mid in targets.items()})
        self.assertIn("پاک شد", self.last_text())
        self.assertEqual(self.state.get("recent_forwards"), [])

    def test_recent_delete_by_button(self):
        self._follow_and_post()
        self.send(ADMIN, "🕐 خبرهای ساعت اخیر")
        self.assertIn("خبر مهم سیاسی", self.last_text())
        self.send(ADMIN, "🗑 حذف ۱")
        self.assertEqual(len(self.fake.deleted), 1)
        self.assertIn("پاک شد", self.last_text())

    def test_recent_delete_counts_failures(self):
        self._follow_and_post()
        mid = list(self.state["recent_forwards"][0]["targets"].values())[0]
        self.fake.delete_fail_ids.add(mid)
        self.send(ADMIN, "/recent")
        self.send(ADMIN, "۱")
        self.assertIn("پاک نشد", self.last_text())
        self.assertIn("۰ نفر پاک شد", self.last_text())

    def test_recent_delete_invalid_number_then_cancel(self):
        self._follow_and_post()
        self.send(ADMIN, "/recent")
        self.send(ADMIN, "۹۹")
        self.assertIn("چنین خبری", self.last_text())
        self.assertIn(ADMIN, self.state.get("pending", {}))
        self.send(ADMIN, "❌ لغو")
        self.assertIn("لغو شد", self.last_text())
        self.assertNotIn(ADMIN, self.state.get("pending", {}))

    def test_recent_prunes_old_records_on_save(self):
        now = time.time()
        self.state["recent_forwards"] = [
            {"post_id": 800, "ts": now - 4 * 3600, "title": "خبر خیلی قدیمی",
             "targets": {USER_A: 5}},
            {"post_id": 801, "ts": now - int(2.5 * 3600), "title": "خبر دو ساعت و نیم پیش",
             "targets": {USER_A: 6}},
            {"post_id": 802, "ts": now, "title": "خبر تازه",
             "targets": {USER_A: 7}},
        ]
        fb.save_state(self.state)
        reloaded = fb.load_state()
        # قدیمی‌تر از ۳ ساعت پاک می‌شه؛ دو ساعت و نیم پیش هنوز نگه داشته می‌شه
        # (ولی توی لیست دو ساعته نیست)
        self.assertEqual([r["post_id"] for r in reloaded["recent_forwards"]], [801, 802])
        self.assertEqual([r["post_id"] for r in fb._recent_forwards(reloaded)], [802])

    def test_recent_button_in_admin_keyboard_only(self):
        self.send(ADMIN, "/start")
        markup = json.loads(self.fake.sent[-1][2])
        texts = [b["text"] for row in markup["keyboard"] for b in row]
        self.assertIn("🕐 خبرهای ساعت اخیر", texts)
        self.send(USER_A, "/start")
        markup = json.loads(self.fake.sent[-1][2])
        texts = [b["text"] for row in markup["keyboard"] for b in row]
        self.assertNotIn("🕐 خبرهای ساعت اخیر", texts)

    def test_recent_title_and_number_parsing(self):
        self.assertEqual(fb._recent_title(""), "خبر بدون متن")
        self.assertEqual(fb._recent_title("خط اول\nخط دوم"), "خط اول")
        long = "خ" * 100
        self.assertTrue(fb._recent_title(long).endswith("…"))
        self.assertEqual(len(fb._recent_title(long)), 78)
        self.assertEqual(fb._parse_fa_number("۱"), 1)
        self.assertEqual(fb._parse_fa_number("12"), 12)

    # ---- 0.30: ریست آمار با پست «شب بخیر» + تاریخچه‌ی ۷ روزه + /stats ----
    def test_is_night_post(self):
        self.assertTrue(fb._is_night_post("شب بخیر 🌙"))
        self.assertTrue(fb._is_night_post("شب‌بخیر دوستان"))
        self.assertTrue(fb._is_night_post("شبتون بخیر"))
        self.assertFalse(fb._is_night_post("صبح بخیر"))
        self.assertFalse(fb._is_night_post("🍳 صبحانه خبری گادنیوز۲۴"))
        self.assertFalse(fb._is_night_post("خبر مهم: " + "متن " * 150 + "شب بخیر"))
        self.assertFalse(fb._is_night_post(""))

    def test_night_post_archives_and_resets_counts(self):
        self.send(USER_A, "/follow سیاسی")
        self.state["topic_forward_counts"] = {"political": 3, "sport": 1}
        self.state["keyword_forward_count"] = 2
        self.state["topic_forward_day"] = tehran_date(1)
        fb.handle_channel_post(self.state, make_channel_post(970, "شب بخیر 🌙"))
        self.assertEqual(self.state["topic_forward_counts"], {})
        self.assertEqual(self.state["keyword_forward_count"], 0)
        self.assertEqual(self.state["topic_forward_day"], fb._today_tehran())
        week = self.state.get("forward_stats_week", [])
        self.assertEqual(len(week), 1)
        self.assertEqual(week[0]["day"], tehran_date(1))
        self.assertEqual(week[0]["counts"], {"political": 3, "sport": 1})
        self.assertEqual(week[0]["keywords"], 2)
        # پست شب بخیر خودش فوروارد نمی‌شه و شمرده نمی‌شه
        self.assertEqual(self.fake.forwarded, [])

    def test_night_post_after_midnight_counts_late_posts_to_old_day(self):
        # شام خبری بعد از نیمه‌شب و قبل از شب بخیر هنوز مال روز قبلی‌ست
        self.send(USER_A, "/follow سیاسی")
        self.state["topic_forward_counts"] = {"political": 3}
        self.state["topic_forward_day"] = tehran_date(1)
        fb.handle_channel_post(self.state, make_channel_post(971, "🔴 خبر مهم سیاسی"))
        self.assertEqual(self.state["topic_forward_counts"], {"political": 4})
        fb.handle_channel_post(self.state, make_channel_post(972, "شب بخیر 🌙"))
        week = self.state.get("forward_stats_week", [])
        self.assertEqual(len(week), 1)
        self.assertEqual(week[0]["counts"], {"political": 4})

    def test_fallback_archives_after_missed_night_post(self):
        # پست شب بخیر یه روز کامل جا مونده: با اولین خبرِ روز بعدش بایگانی و صفر می‌شه
        self.send(USER_A, "/follow سیاسی")
        self.state["topic_forward_counts"] = {"political": 3}
        self.state["keyword_forward_count"] = 5
        self.state["topic_forward_day"] = tehran_date(3)
        fb.handle_channel_post(self.state, make_channel_post(973, "🔴 خبر مهم سیاسی"))
        self.assertEqual(self.state["topic_forward_counts"], {"political": 1})
        self.assertEqual(self.state["keyword_forward_count"], 0)
        self.assertEqual(self.state["topic_forward_day"], fb._today_tehran())
        week = self.state.get("forward_stats_week", [])
        self.assertEqual(len(week), 1)
        self.assertEqual(week[0]["day"], tehran_date(3))
        self.assertEqual(week[0]["counts"], {"political": 3})
        self.assertEqual(week[0]["keywords"], 5)

    def test_archive_merges_duplicate_day(self):
        self.state["topic_forward_counts"] = {"political": 2}
        self.state["keyword_forward_count"] = 1
        self.state["topic_forward_day"] = tehran_date(1)
        fb._archive_forward_day(self.state)
        self.state["topic_forward_counts"] = {"political": 3, "sport": 2}
        self.state["keyword_forward_count"] = 1
        self.state["topic_forward_day"] = tehran_date(1)
        fb._archive_forward_day(self.state)
        week = self.state.get("forward_stats_week", [])
        self.assertEqual(len(week), 1)
        self.assertEqual(week[0]["counts"], {"political": 5, "sport": 2})
        self.assertEqual(week[0]["keywords"], 2)

    def test_archive_keeps_only_last_7_days(self):
        for ago in range(8, 0, -1):
            self.state["topic_forward_counts"] = {"political": ago}
            self.state["keyword_forward_count"] = 0
            self.state["topic_forward_day"] = tehran_date(ago)
            fb._archive_forward_day(self.state)
        week = self.state.get("forward_stats_week", [])
        self.assertEqual(len(week), 7)
        self.assertEqual([e["day"] for e in week],
                         [tehran_date(a) for a in range(7, 0, -1)])

    def test_stats_shows_week_and_today(self):
        self.state["forward_stats_week"] = [
            {"day": tehran_date(2), "counts": {"political": 5, "sport": 2}, "keywords": 1},
            {"day": tehran_date(1), "counts": {"political": 3}, "keywords": 0},
        ]
        self.state["topic_forward_counts"] = {"political": 1}
        self.state["keyword_forward_count"] = 0
        self.state["topic_forward_day"] = fb._today_tehran()
        self.send(ADMIN, "/stats")
        txt = self.last_text()
        self.assertIn("📈 آمار ۷ روز اخیر", txt)
        self.assertIn(f"{tehran_date(2)}: 🔴 سیاسی 5 | ⚽ ورزشی 2 | 🔍 کلمه‌ی دلخواه 1", txt)
        self.assertIn(f"{tehran_date(1)}: 🔴 سیاسی 3", txt)
        self.assertIn(f"امروز ({fb._today_tehran()}): 🔴 سیاسی 1", txt)
        self.assertIn("مجموع هفته: 🔴 سیاسی 9 | ⚽ ورزشی 2 | 🔍 کلمه‌ی دلخواه 1", txt)

    def test_stats_empty_state(self):
        self.state["topic_forward_day"] = fb._today_tehran()
        self.send(ADMIN, "/stats")
        txt = self.last_text()
        self.assertIn("📈 آمار ۷ روز اخیر", txt)
        self.assertIn(f"امروز ({fb._today_tehran()}): —", txt)
        self.assertNotIn("مجموع هفته", txt)

    def test_stats_admin_only(self):
        self.send(USER_A, "/stats")
        self.assertIn("فقط برای ادمینه", self.last_text())

    def test_stats_button_in_admin_keyboard_only(self):
        self.send(ADMIN, "/start")
        markup = json.loads(self.fake.sent[-1][2])
        rows = markup["keyboard"]
        texts = [b["text"] for row in rows for b in row]
        self.assertIn("📊 آمار هفته", texts)
        # دکمه‌ی آمار هفته در ردیف جدا و زیر «خبرهای ساعت اخیر» است
        recent_row = next(i for i, r in enumerate(rows) if any(b["text"] == "🕐 خبرهای ساعت اخیر" for b in r))
        stats_row = next(i for i, r in enumerate(rows) if any(b["text"] == "📊 آمار هفته" for b in r))
        self.assertEqual(stats_row, recent_row + 1)
        self.assertEqual([b["text"] for b in rows[stats_row]], ["📊 آمار هفته"])
        self.send(USER_A, "/start")
        markup = json.loads(self.fake.sent[-1][2])
        texts = [b["text"] for row in markup["keyboard"] for b in row]
        self.assertNotIn("📊 آمار هفته", texts)

    def test_stats_in_admin_help(self):
        self.send(ADMIN, "/help")
        self.assertIn("/stats", self.last_text())
        self.send(ADMIN, "/help /stats")
        self.assertIn("۷ روز اخیر", self.last_text())
        self.assertIsNone(fb._parse_fa_number("سلام"))

    def test_recent_help_lists_command(self):
        self.send(ADMIN, "/help /recent")
        self.assertIn("دو ساعت اخیر", self.last_text())
        self.send(ADMIN, "/help")
        self.assertIn("/recent", self.last_text())

    # ---- 0.31: یه پیام/پست خراب نباید کل صف رو قفل کنه ----
    def test_poison_message_skipped_rest_processed(self):
        # ارسال برای USER_A خطا می‌ده (مثل قطعی تلگرام وسط ارسال)
        self.fake.send_failures = {USER_A: "raise"}
        self.fake.updates = [
            {"update_id": 10, "message": make_message(USER_A, "/start")},
            {"update_id": 11, "message": make_message(USER_B, "/start")},
        ]
        fb.process_updates(self.state)
        # پیام دوم با وجود خرابی اولی جواب گرفته
        self.assertEqual(self.fake.sent[-1][0], USER_B)
        self.assertIn("GodNews24", self.fake.sent[-1][1])
        # آفست از هر دو رد شده — پیام خراب دوباره امتحان نمی‌شه
        self.assertEqual(self.state["tg_offset"], 12)

    def test_poison_channel_post_skipped_rest_forwarded(self):
        self.send(USER_A, "/follow ورزشی")
        orig = fb.handle_channel_post

        def boom(state, post):
            if post.get("message_id") == 900:
                raise RuntimeError("boom")
            return orig(state, post)

        fb.handle_channel_post = boom
        try:
            self.fake.updates = [
                {"update_id": 20, "channel_post": make_channel_post(
                    900, "🔴 خبر خراب")},
                {"update_id": 21, "channel_post": make_channel_post(
                    901, "⚽ پرسپولیس قهرمان لیگ شد")},
            ]
            fb.process_updates(self.state)
        finally:
            fb.handle_channel_post = orig
        self.assertEqual(self.fake.forwarded, [(USER_A, 901)])
        self.assertEqual(self.state["tg_offset"], 22)

    def test_run_once_saves_state_when_sweep_raises(self):
        self.fake.updates = [
            {"update_id": 30, "message": make_message(USER_A, "/start")},
        ]
        orig = fb._sweep_followers
        fb._sweep_followers = lambda state: (_ for _ in ()).throw(
            RuntimeError("sweep down"))
        try:
            fb.run_once()  # نباید خطا بده
        finally:
            fb._sweep_followers = orig
        # پیام پردازش و حافظه ذخیره شده — دور بعد تکراری نمی‌شه
        self.assertEqual(self.fake.sent[-1][0], USER_A)
        saved = fb.load_state()
        self.assertEqual(saved.get("tg_offset"), 31)

class FakeGit:
    """Stands in for subprocess in commit_state tests."""

    def __init__(self, pull_rc=0, diff_rc=1, push_rc=0):
        self.calls = []
        self.pull_rc = pull_rc
        self.diff_rc = diff_rc
        self.push_rc = push_rc

    def run(self, argv, capture_output=False, text=False):
        self.calls.append(list(argv[1:]))  # drop "git"
        args = argv[1:]
        rc = 0
        if args[:1] == ["pull"]:
            rc = self.pull_rc
        elif args[:2] == ["diff", "--cached"]:
            rc = self.diff_rc
        elif args[:1] == ["push"]:
            rc = self.push_rc
        return SimpleNamespace(returncode=rc, stderr="", stdout="")


if __name__ == "__main__":
    unittest.main(verbosity=2)
