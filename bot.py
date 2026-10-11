# -*- coding: utf-8 -*-
"""بوت إدارة قروب المدرسة
pip install "python-telegram-bot>=20"
"""
import asyncio
import html
import json
import logging
import os
import re
import time
from datetime import date, datetime, timedelta, timezone
from types import SimpleNamespace

from telegram import InlineKeyboardButton as Btn
from telegram import InlineKeyboardMarkup as Markup
from telegram import InputMediaDocument, InputMediaPhoto, Update
from telegram.constants import ChatMemberStatus
from telegram.error import BadRequest, Forbidden, NetworkError, RetryAfter, TelegramError
from telegram.ext import (
    Application,
    CallbackQueryHandler,
    CommandHandler,
    ContextTypes,
    MessageHandler,
    filters,
)

logging.basicConfig(format="%(asctime)s %(levelname)s %(name)s: %(message)s", level=logging.INFO)
logging.getLogger("httpx").setLevel(logging.WARNING)
log = logging.getLogger("schoolbot")

# ================= الإعدادات =================
# على Railway: التوكن في Variables باسم BOT_TOKEN ، ومسار الـ Volume باسم DATA_DIR
TOKEN = os.environ.get("BOT_TOKEN", "ضع_التوكن_هنا")
DATA_DIR = os.environ.get("DATA_DIR", ".")
DB_FILE = os.path.join(DATA_DIR, "replies.json")        # الردود البسيطة
MENU_FILE = os.path.join(DATA_DIR, "menus.json")        # الردود المتعددة (الأزرار)
HW_FILE = os.path.join(DATA_DIR, "homework.json")       # التحاضير حسب التاريخ
USERS_FILE = os.path.join(DATA_DIR, "users.json")       # الطلاب المفعّلون
SETTINGS_FILE = os.path.join(DATA_DIR, "settings.json")  # إعدادات كل مجموعة
GROUPS_FILE = os.path.join(DATA_DIR, "groups.json")     # المجموعات التي يعرفها البوت
MEMBERS_FILE = os.path.join(DATA_DIR, "members.json")   # الأعضاء الذين شوهدوا (لأمر @all)
OWNER_ID = int(os.environ.get("OWNER_ID", "6970354026"))
OWNER_USERNAME = os.environ.get("OWNER_USERNAME", "p1oco")
OWNER_NAME = os.environ.get("OWNER_NAME", "المطوّر")
TZ_HOURS = float(os.environ.get("TZ_OFFSET", "3"))      # فرق التوقيت عن UTC (العراق = 3)

ANON_ADMIN_ID = 1087968824      # حساب "المشرف المجهول" في تيليجرام
STATE_TTL = 900                 # مهلة عمليات الإضافة (ثانية)
ALL_COOLDOWN = 30               # الفاصل بين استخدامات @all
FLOOD_GAP = 1.5                 # الفاصل بين أوامر الطالب الواحد
CANCEL_WORDS = ("الغاء", "إلغاء")


# ================= التخزين =================
def load(path):
    try:
        with open(path, "r", encoding="utf-8") as f:
            return json.load(f)
    except FileNotFoundError:
        return {}
    except json.JSONDecodeError:
        log.error("ملف تالف: %s — حُفظت نسخة .bak", path)
        try:
            os.replace(path, path + ".bak")
        except OSError:
            pass
        return {}


def save(path, data):
    """كتابة آمنة: ملف مؤقت ثم استبدال، حتى لا يتلف الملف إذا توقف البوت أثناء الحفظ."""
    tmp = path + ".tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(data, f, ensure_ascii=False, indent=2)
    os.replace(tmp, path)


_TASKS = set()


def spawn(coro):
    """تشغيل مهمة في الخلفية مع الاحتفاظ بمرجعها."""
    t = asyncio.create_task(coro)
    _TASKS.add(t)
    t.add_done_callback(_TASKS.discard)
    return t


def retry_secs(e):
    ra = getattr(e, "retry_after", 1)
    return ra.total_seconds() if hasattr(ra, "total_seconds") else float(ra)


# ================= حالات العمليات (لكل مستخدم) =================
def get_state(cd, uid):
    st = cd.get("states", {}).get(uid)
    if not st:
        return None
    if time.time() - st["t"] > STATE_TTL:
        cd["states"].pop(uid, None)
        return None
    st["t"] = time.time()
    return st


def set_state(cd, uid, **st):
    st["user"] = uid
    st["t"] = time.time()
    cd.setdefault("states", {})[uid] = st
    return st


def clear_state(cd, uid):
    cd.get("states", {}).pop(uid, None)


def flooded(cd, uid):
    """يمنع تكرار أوامر الطالب بسرعة (حماية من الإزعاج)."""
    last = cd.setdefault("flood", {})
    now = time.time()
    if now - last.get(uid, 0) < FLOOD_GAP:
        return True
    last[uid] = now
    return False


# ================= الصلاحيات والمجموعات =================
_ADMIN_CACHE = {}


async def is_admin(chat, user_id) -> bool:
    if user_id == OWNER_ID:
        return True
    if chat.type == "private":
        return False
    if user_id == ANON_ADMIN_ID:
        return True
    key = (chat.id, user_id)
    hit = _ADMIN_CACHE.get(key)
    if hit and time.time() - hit[0] < 60:
        return hit[1]
    try:
        member = await chat.get_member(user_id)
    except TelegramError as e:
        log.warning("تعذر فحص المشرف: %s", e)
        return False
    ok = member.status in (ChatMemberStatus.ADMINISTRATOR, ChatMemberStatus.OWNER)
    _ADMIN_CACHE[key] = (time.time(), ok)
    return ok


def data_id(chat, user_id):
    """أي مجموعة نقرأ منها البيانات؟ في الخاص: مجموعة الطالب المفعّل، أو التي حددها المالك."""
    if chat.type != "private":
        return str(chat.id)
    if user_id == OWNER_ID:
        target = load(SETTINGS_FILE).get("_owner", {}).get("target")
        return target or str(chat.id)
    for gid, reg in load(USERS_FILE).items():
        if str(user_id) in reg:
            return gid
    return str(chat.id)


def remember_group(chat):
    if chat.type == "private":
        return
    groups = load(GROUPS_FILE)
    if groups.get(str(chat.id), {}).get("title") != chat.title:
        groups[str(chat.id)] = {"title": chat.title}
        save(GROUPS_FILE, groups)


_SEEN = set()


def track_user(chat, user):
    """يحفظ كل عضو شوهد يكتب في المجموعة، ليستخدمه أمر @all."""
    if chat.type == "private" or user.is_bot or user.id == ANON_ADMIN_ID:
        return
    key = (chat.id, user.id, user.full_name, user.username)
    if key in _SEEN:
        return
    _SEEN.add(key)
    members = load(MEMBERS_FILE)
    group = members.setdefault(str(chat.id), {})
    info = {"name": user.full_name, "username": user.username}
    if group.get(str(user.id)) != info:
        group[str(user.id)] = info
        save(MEMBERS_FILE, members)


# ================= نصوص وتواريخ =================
WEEKDAYS = ["الاثنين", "الثلاثاء", "الأربعاء", "الخميس", "الجمعة", "السبت", "الأحد"]
DAY_WORDS = {"اليوم": 0, "امس": -1, "باجر": 1, "غدا": 1, "بكره": 1, "بكرا": 1}
HW_WORDS = "تحاضير|تحضير|واجبات|واجب"
HW_VIEW = re.compile(rf"^({HW_WORDS})\s+(\S+)$")
HW_ADD = re.compile(rf"^اضف\s+({HW_WORDS})\s+(\S+)$")
HW_DEL = re.compile(rf"^حذف\s+({HW_WORDS})\s+(\S+)$")
ADD_MULTI = re.compile(r"^[اأإآ]ضف\s+رد\s+متعدد$")
ADD_REPLY = re.compile(r"^[اأإآ]ضف\s+رد\s+(.+)$", re.S)
EDIT_REPLY = re.compile(r"^تعديل\s+رد\s+(.+)$", re.S)
DEL_REPLY = re.compile(r"^حذف\s+رد\s+(.+)$", re.S)
ALL_RE = re.compile(r"(?<![\w@])@(?:all|everyone|الكل)(?!\w)", re.I)
_AR_DIGITS = str.maketrans("٠١٢٣٤٥٦٧٨٩", "0123456789")


def today():
    return (datetime.now(timezone.utc) + timedelta(hours=TZ_HOURS)).date()


def norm(t):
    t = t.translate(_AR_DIGITS)
    for a, b in (("أ", "ا"), ("إ", "ا"), ("آ", "ا"), ("ة", "ه"), ("ى", "ي"), ("ً", "")):
        t = t.replace(a, b)
    return t.strip()


def parse_day(word):
    w = norm(word)
    if w in DAY_WORDS:
        return today() + timedelta(days=DAY_WORDS[w])
    m = re.fullmatch(r"(\d{4})[-/](\d{1,2})[-/](\d{1,2})", w)
    if m:
        try:
            return date(int(m.group(1)), int(m.group(2)), int(m.group(3)))
        except ValueError:
            return None
    return None


def day_label(d):
    return f"{WEEKDAYS[d.weekday()]} {d.isoformat()}"


def month_label(ym, with_year):
    return f"شهر {int(ym[5:])}" + (f" ({ym[:4]})" if with_year else "")


def chunk_lines(lines, limit=3500):
    out, cur = [], ""
    for ln in lines:
        if len(cur) + len(ln) + 1 > limit:
            out.append(cur)
            cur = ""
        cur += ln + "\n"
    if cur:
        out.append(cur)
    return out


# ================= المحتوى (نصوص / صور / ملفات / فيديو) =================
class Target:
    """يجعل send_items تعمل مع الإرسال لمحادثة بدون رسالة أصلية (الأزرار والإذاعة)."""

    def __init__(self, bot, chat_id):
        self.bot, self.chat_id = bot, chat_id

    async def reply_text(self, text, **kw):
        return await self.bot.send_message(self.chat_id, text, **kw)

    async def reply_photo(self, photo, **kw):
        return await self.bot.send_photo(self.chat_id, photo, **kw)

    async def reply_document(self, doc, **kw):
        return await self.bot.send_document(self.chat_id, doc, **kw)

    async def reply_video(self, video, **kw):
        return await self.bot.send_video(self.chat_id, video, **kw)

    async def reply_audio(self, audio, **kw):
        return await self.bot.send_audio(self.chat_id, audio, **kw)

    async def reply_voice(self, voice, **kw):
        return await self.bot.send_voice(self.chat_id, voice, **kw)

    async def reply_media_group(self, media, **kw):
        return await self.bot.send_media_group(self.chat_id, media, **kw)


def extract_item(msg):
    """يحوّل رسالة المشرف إلى عنصر محتوى محفوظ (مع الحفاظ على التنسيق)."""
    if msg.text:
        return {"type": "text", "content": msg.text_html, "html": True}
    cap = msg.caption_html if msg.caption else None
    if msg.photo:
        return {"type": "photo", "content": msg.photo[-1].file_id, "caption": cap, "html": True}
    if msg.document:
        return {"type": "document", "content": msg.document.file_id, "caption": cap, "html": True}
    if msg.video:
        return {"type": "video", "content": msg.video.file_id, "caption": cap, "html": True}
    if msg.audio:
        return {"type": "audio", "content": msg.audio.file_id, "caption": cap, "html": True}
    if msg.voice:
        return {"type": "voice", "content": msg.voice.file_id, "caption": cap, "html": True}
    return None


_SENDERS = {"photo": "reply_photo", "document": "reply_document", "video": "reply_video",
            "audio": "reply_audio", "voice": "reply_voice"}


async def send_items(target, items):
    """يرسل كل العناصر. الصور/الملفات المتتالية تُرسل كمجموعة واحدة (حتى 10)."""
    i = 0
    while i < len(items):
        it = items[i]
        t = it.get("type")
        pm = "HTML" if it.get("html") else None
        if t == "text":
            await target.reply_text(it["content"], **({"parse_mode": pm} if pm else {}))
            i += 1
            continue
        if t not in _SENDERS:
            i += 1
            continue
        if t in ("photo", "document"):
            j = i
            while j < len(items) and items[j].get("type") == t and j - i < 10:
                j += 1
            if j - i > 1:
                cls = InputMediaPhoto if t == "photo" else InputMediaDocument
                group = [cls(c["content"], caption=c.get("caption"),
                             parse_mode="HTML" if c.get("html") else None) for c in items[i:j]]
                await target.reply_media_group(group)
                i = j
                continue
        kw = {"caption": it.get("caption")}
        if pm:
            kw["parse_mode"] = pm
        await getattr(target, _SENDERS[t])(it["content"], **kw)
        i += 1


def cb_target(context, q):
    """وجهة الإرسال لزر: نرسل بدون الرد على رسالة الأزرار (أضمن)."""
    return Target(context.bot, q.message.chat.id)


# ================= القوائم (الأزرار) =================
def parse_path(s):
    return [int(x) for x in s.split(".")] if s else []


def join_path(p):
    return ".".join(str(i) for i in p)


def get_node(buttons, path):
    node, lst = None, buttons
    for i in path:
        if i >= len(lst):
            return None
        node = lst[i]
        lst = node.setdefault("buttons", [])
    return node


def children(menu, path):
    if not path:
        return menu["buttons"]
    return get_node(menu["buttons"], path).setdefault("buttons", [])


def node_items(node):
    """محتوى الزر: قائمة عناصر. يدعم الأزرار القديمة أيضاً."""
    if "items" in node:
        return node["items"]
    if "content" in node:
        return [{"type": node["type"], "content": node["content"], "caption": node.get("caption")}]
    return []


def keyboard(menu, mid, path, idx=False):
    """idx=True: فُتحت من أمر (الازرار) فيظهر زر العودة للقائمة الرئيسية."""
    sfx = "|1" if idx else ""
    rows = [
        [Btn(n["name"], callback_data=f"n|{mid}|{join_path(path + [i])}{sfx}")]
        for i, n in enumerate(children(menu, path))
    ]
    if path:
        rows.append([Btn("🔙 رجوع", callback_data=f"n|{mid}|{join_path(path[:-1])}{sfx}")])
    elif idx:
        rows.append([Btn("🔙 القائمة الرئيسية", callback_data="ix")])
    return Markup(rows)


def index_markup(menus):
    rows = [[Btn(m["keyword"], callback_data=f"n|{mid}||1")] for mid, m in menus.items()]
    rows.append([Btn("📖 عرض الأوامر", callback_data="help")])
    return Markup(rows)


def breadcrumb(menu, path):
    names, lst = [menu["keyword"]], menu["buttons"]
    for i in path:
        names.append(lst[i]["name"])
        lst = lst[i].get("buttons", [])
    return " ‹ ".join(names)


def edit_text(menu, path):
    return f"✏️ تعديل: {breadcrumb(menu, path)}\nافتح زراً للدخول إليه، أو أضف زراً جديداً هنا"


def edit_markup(menu, mid, path):
    here = join_path(path)
    rows = [
        [Btn("📂 " + n["name"], callback_data=f"ed|{mid}|{join_path(path + [i])}|go")]
        for i, n in enumerate(children(menu, path))
    ]
    rows.append([Btn("➕ إضافة زر هنا", callback_data=f"ed|{mid}|{here}|add")])
    if path:
        rows.append([
            Btn("✏️ تغيير المحتوى", callback_data=f"ed|{mid}|{here}|repl"),
            Btn("🏷 تغيير الاسم", callback_data=f"ed|{mid}|{here}|ren"),
        ])
        rows.append([Btn("🗑 حذف هذا الزر", callback_data=f"ed|{mid}|{here}|del")])
        rows.append([Btn("🔙 رجوع", callback_data=f"ed|{mid}|{join_path(path[:-1])}|go")])
    rows.append([Btn("✅ إنهاء التعديل", callback_data=f"ed|{mid}||done")])
    return Markup(rows)


def choose_markup(st):
    rows = [
        [Btn("➕ زر آخر في نفس المستوى", callback_data="cr|more")],
        [Btn("📁 زر داخل هذا الزر", callback_data="cr|sub")],
    ]
    if st["parent"]:
        rows.append([Btn("⬆️ العودة للمستوى الأعلى", callback_data="cr|up")])
    rows.append([Btn("✅ انتهاء وحفظ", callback_data="cr|done")])
    return Markup(rows)


# ================= لوحة التعديل =================
async def on_edit_cb(update: Update, context: ContextTypes.DEFAULT_TYPE):
    q = update.callback_query
    try:
        _, mid, p, act = q.data.split("|")
        path = parse_path(p)
    except ValueError:
        await q.answer()
        return
    chat, uid = q.message.chat, q.from_user.id
    if not await is_admin(chat, uid):
        await q.answer("للمشرفين فقط ⛔", show_alert=True)
        return
    data = load(MENU_FILE)
    menu = data.get(data_id(chat, uid), {}).get(mid)
    if not menu or (path and get_node(menu["buttons"], path) is None):
        await q.answer("هذا الزر لم يعد موجوداً", show_alert=True)
        return
    await q.answer()
    cd = context.chat_data

    try:
        if act == "done":
            await q.edit_message_text("تم إنهاء التعديل ✅")

        elif act == "go":
            await q.edit_message_text(edit_text(menu, path), reply_markup=edit_markup(menu, mid, path))

        elif act == "add":
            set_state(cd, uid, step="name", menu=mid, parent=path, mode="new", items=[], status=None)
            await q.edit_message_text(f"📍 {breadcrumb(menu, path)}\nاكتب اسم الزر الجديد 🔘")

        elif act == "repl":
            set_state(cd, uid, step="content", menu=mid, path=path, mode="replace", items=[], status=None)
            await q.edit_message_text(
                f"📍 {breadcrumb(menu, path)}\nأرسل المحتوى الجديد (نص / صور / كتب / فيديو)، "
                "وبعد الانتهاء اضغط ✅ تم.\nالمحتوى القديم سيُستبدل.\nللإلغاء اكتب: الغاء"
            )

        elif act == "ren":
            set_state(cd, uid, step="rename", menu=mid, path=path, mode="rename")
            await q.edit_message_text("اكتب الاسم الجديد للزر 🏷\nللإلغاء اكتب: الغاء")

        elif act == "del":
            node = get_node(menu["buttons"], path)
            await q.edit_message_text(
                f"هل تريد حذف الزر «{node['name']}» وكل ما بداخله؟",
                reply_markup=Markup([[
                    Btn("🗑 نعم، احذف", callback_data=f"ed|{mid}|{p}|delok"),
                    Btn("إلغاء", callback_data=f"ed|{mid}|{p}|go"),
                ]]),
            )

        elif act == "delok":
            parent = path[:-1]
            del children(menu, parent)[path[-1]]
            save(MENU_FILE, data)
            await q.edit_message_text(edit_text(menu, parent), reply_markup=edit_markup(menu, mid, parent))
    except BadRequest as e:
        log.info("edit_cb: %s", e)


# ================= إضافة رد متعدد =================
def cleanup_state(chat_id, st):
    """إذا أُلغيت عملية أنشأت قائمة فارغة، نحذف القائمة الفارغة."""
    if st.get("fresh") and st.get("mode") == "new":
        data = load(MENU_FILE)
        menu = data.get(chat_id, {}).get(st.get("menu"))
        if menu is not None and not menu["buttons"]:
            del data[chat_id][st["menu"]]
            save(MENU_FILE, data)


async def handle_multi(update: Update, st, chat_id) -> bool:
    """معالجة رسائل المشرف أثناء إضافة/تعديل. يرجع True إذا استُهلكت الرسالة."""
    msg = update.message
    step = st["step"]
    if step == "choose":
        return False

    if msg.text and msg.text.strip() in CANCEL_WORDS:
        st["finished"] = True
        await msg.reply_text("تم إلغاء العملية ❌ (ما تم حفظه سابقاً يبقى)")
        return True

    data = load(MENU_FILE)
    chat_menus = data.setdefault(chat_id, {})

    if step == "keyword":
        if not msg.text or not msg.text.strip():
            return True
        word = msg.text.strip().strip('"“”')
        if any(norm(v["keyword"]) == norm(word) for v in chat_menus.values()):
            await msg.reply_text(
                f"الكلمة «{word}» موجودة مسبقاً ⚠️\n"
                f"• لتعديلها اكتب: تعديل رد {word}\n"
                f"• لحذفها اكتب: حذف رد {word}\n"
                "أو اكتب كلمة أخرى، أو: الغاء"
            )
            return True
        mid = str(max([int(k) for k in chat_menus] + [0]) + 1)
        chat_menus[mid] = {"keyword": word, "buttons": []}
        save(MENU_FILE, data)
        st.update(step="name", menu=mid, parent=[], mode="new", items=[], status=None, fresh=True)
        await msg.reply_text("اكتب اسم الزر 🔘")
        return True

    if step == "name":
        if not msg.text or not msg.text.strip():
            return True
        st.update(name=msg.text.strip()[:60], items=[], status=None, step="content")
        await msg.reply_text(
            "أرسل محتوى الزر 📎\n"
            "يمكنك إرسال أكثر من كتاب / ملف / صورة / فيديو / نص، وكلها ستُرسل عند الضغط على الزر.\n"
            "وعند الانتهاء اضغط ✅ تم."
        )
        return True

    if step == "content":
        item = extract_item(msg)
        if item is None:
            await msg.reply_text("هذا النوع غير مدعوم. أرسل نصاً أو صورة أو ملفاً أو فيديو أو صوتاً.")
            return True
        st["items"].append(item)
        label = f"تمت إضافة {len(st['items'])} ✅\nأرسل المزيد (كتاب / ملف / صورة / فيديو / نص) أو اضغط الزر أدناه"
        mode = st.get("mode")
        if mode == "bc":
            if not st.get("group"):
                st.setdefault("msg_ids", []).append(msg.message_id)
            btn_text = "📨 إرسال للجميع"
        elif mode == "hw":
            btn_text = "✅ تم، حفظ التحضير"
        else:
            btn_text = "✅ تم، حفظ الزر"
        markup = Markup([[Btn(btn_text, callback_data="cr|fin")]])
        if st.get("status"):
            try:
                await st["status"].edit_text(label, reply_markup=markup)
                return True
            except BadRequest:
                pass
        st["status"] = await msg.reply_text(label, reply_markup=markup)
        return True

    if step == "rename":
        if not msg.text or not msg.text.strip():
            return True
        menu = chat_menus.get(st["menu"])
        node = get_node(menu["buttons"], st["path"]) if menu else None
        if node is None:
            await msg.reply_text("هذا الزر لم يعد موجوداً ❌")
        else:
            node["name"] = msg.text.strip()[:60]
            save(MENU_FILE, data)
            await msg.reply_text("تم تغيير الاسم ✅")
        st["finished"] = True
        return True

    return False


async def on_create_cb(update: Update, context: ContextTypes.DEFAULT_TYPE):
    q = update.callback_query
    uid, cd = q.from_user.id, context.chat_data
    st = get_state(cd, uid)
    if not st:
        await q.answer("انتهت هذه العملية أو أنها ليست لك. ابدأ من جديد.", show_alert=True)
        return
    act = q.data.split("|")[1]
    chat_id = data_id(q.message.chat, uid)

    # ---- حفظ المحتوى (زر "تم") ----
    if act == "fin":
        if st["step"] != "content":
            await q.answer()
            return
        if not st["items"]:
            await q.answer("أرسل محتوى أولاً", show_alert=True)
            return
        await q.answer()
        mode = st.get("mode")

        if mode == "bc":
            clear_state(cd, uid)
            gid = st.get("group", chat_id)
            reg_n = len(load(USERS_FILE).get(gid, {}))
            if reg_n == 0:
                await q.edit_message_text("لا يوجد أعضاء مفعّلون بعد ❌\nانشر أولاً الأمر: رسالة التفعيل")
                return
            await q.edit_message_text(f"⏳ جاري الإرسال إلى {reg_n} عضو...")
            spawn(finish_broadcast(context.bot, q.message.chat.id, q.message.message_id,
                                   gid, st["items"], st.get("msg_ids", [])))
            return

        if mode == "hw":
            hw = load(HW_FILE)
            hw.setdefault(chat_id, {}).setdefault(st["date"], []).extend(st["items"])
            save(HW_FILE, hw)
            clear_state(cd, uid)
            await q.edit_message_text(f"تم حفظ التحضير بنجاح ✅\n📅 {st['label']}")
            return

        data = load(MENU_FILE)
        menu = data.get(chat_id, {}).get(st["menu"])
        if menu is None:
            clear_state(cd, uid)
            await q.edit_message_text("هذه القائمة لم تعد موجودة ❌")
            return

        if mode == "replace":
            node = get_node(menu["buttons"], st["path"])
            if node is None:
                clear_state(cd, uid)
                await q.edit_message_text("هذا الزر لم يعد موجوداً ❌")
                return
            for k in ("type", "content", "caption"):
                node.pop(k, None)
            node["items"] = st["items"]
            save(MENU_FILE, data)
            clear_state(cd, uid)
            await q.edit_message_text("تم تغيير المحتوى بنجاح ✅")
            return

        if st["parent"] and get_node(menu["buttons"], st["parent"]) is None:
            clear_state(cd, uid)
            await q.edit_message_text("المكان الذي تضيف فيه لم يعد موجوداً ❌")
            return
        kids = children(menu, st["parent"])
        kids.append({"name": st["name"], "items": st["items"], "buttons": []})
        st["last"] = st["parent"] + [len(kids) - 1]
        save(MENU_FILE, data)
        st["step"] = "choose"
        st["status"] = None
        await q.edit_message_text(
            f"تم إضافة الزر ✅\n📍 {breadcrumb(menu, st['parent'])}\nماذا تريد الآن؟",
            reply_markup=choose_markup(st),
        )
        return

    # ---- أزرار ما بعد الحفظ ----
    await q.answer()
    if st["step"] != "choose":
        return
    if act == "done":
        clear_state(cd, uid)
        await q.edit_message_text("تم حفظ الرد المتعدد بنجاح ✅")
        return
    if act == "sub":
        st["parent"] = st["last"]
    elif act == "up":
        st["parent"] = st["parent"][:-1]
    st["step"] = "name"
    menu = load(MENU_FILE).get(chat_id, {}).get(st["menu"])
    if menu is None:
        clear_state(cd, uid)
        await q.edit_message_text("هذه القائمة لم تعد موجودة ❌")
        return
    await q.edit_message_text(f"📍 {breadcrumb(menu, st['parent'])}\nاكتب اسم الزر 🔘")


# ================= تصفح القائمة =================
async def on_nav(update: Update, context: ContextTypes.DEFAULT_TYPE):
    q = update.callback_query
    parts = q.data.split("|")
    try:
        mid, path, idx = parts[1], parse_path(parts[2]), len(parts) > 3
    except (IndexError, ValueError):
        await q.answer()
        return
    menu = load(MENU_FILE).get(data_id(q.message.chat, q.from_user.id), {}).get(mid)
    if not menu:
        await q.answer("هذه القائمة لم تعد موجودة", show_alert=True)
        return
    await q.answer()

    try:
        if not path:
            title = f"📋 {menu['keyword']}" if idx else "📋 اختر من القائمة:"
            await q.edit_message_text(title, reply_markup=keyboard(menu, mid, [], idx))
            return
        node = get_node(menu["buttons"], path)
        if not node:
            return
        items = node_items(node)
        if node.get("buttons"):
            if len(items) == 1 and items[0].get("type") == "text" and not items[0].get("html"):
                text, pm = items[0]["content"], None
            elif len(items) == 1 and items[0].get("type") == "text":
                text, pm = items[0]["content"], "HTML"
            else:
                await send_items(cb_target(context, q), items)
                text, pm = node["name"], None
            kw = {"parse_mode": pm} if pm else {}
            await q.edit_message_text(text, reply_markup=keyboard(menu, mid, path, idx), **kw)
        else:
            await send_items(cb_target(context, q), items)
    except BadRequest as e:
        log.info("nav: %s", e)


# ================= التفعيل والإذاعة =================
def reg_link(bot, group_id):
    return f"https://t.me/{bot.username}?start=reg_{group_id}"


async def delete_later(message, seconds):
    await asyncio.sleep(seconds)
    try:
        await message.delete()
    except Exception:
        pass


async def broadcast(bot, group_id, items):
    """يرسل المحتوى في الخاص لكل مفعّلي المجموعة. يرجع (نجح، فشل)."""
    ids = list(load(USERS_FILE).get(group_id, {}))
    ok = fail = 0
    removed = []
    for uid in ids:
        sent = False
        for _ in range(2):
            try:
                await send_items(Target(bot, int(uid)), items)
                sent = True
                break
            except RetryAfter as e:
                await asyncio.sleep(retry_secs(e) + 1)
            except Forbidden:           # أوقف/حظر البوت: نحذفه من المفعّلين
                removed.append(uid)
                break
            except Exception as e:
                log.warning("فشل الإرسال إلى %s: %s", uid, e)
                break
        ok += sent
        fail += not sent
        await asyncio.sleep(0.07)
    if removed:
        users = load(USERS_FILE)
        for uid in removed:
            users.get(group_id, {}).pop(uid, None)
        save(USERS_FILE, users)
    return ok, fail


async def finish_broadcast(bot, chat_id, message_id, gid, items, msg_ids):
    try:
        ok, fail = await broadcast(bot, gid, items)
    except Exception:
        log.exception("فشلت الإذاعة")
        ok, fail = 0, 0
    for m_id in msg_ids:
        try:
            await bot.delete_message(chat_id, m_id)
        except Exception:
            pass
    text = f"📨 تم الإرسال إلى {ok} عضو ✅"
    if fail:
        text += f"\n⚠️ تعذّر الإرسال إلى {fail} (أوقفوا البوت أو حظروه)"
    try:
        total = await bot.get_chat_member_count(int(gid))
        left = len(load(USERS_FILE).get(gid, {}))
        text += f"\n👥 المفعّلون {left} من {total} عضو"
    except Exception:
        pass
    try:
        await bot.edit_message_text(text, chat_id=chat_id, message_id=message_id)
    except Exception:
        await bot.send_message(chat_id, text)


def known_groups():
    """كل المجموعات التي للبوت علاقة بها (من الملفات المحفوظة)."""
    ids = set(load(GROUPS_FILE))
    for f in (MENU_FILE, DB_FILE, HW_FILE, USERS_FILE, MEMBERS_FILE):
        ids |= set(load(f))
    ids |= {k for k in load(SETTINGS_FILE) if k != "_owner"}
    return [g for g in ids if g.startswith("-")]


def _left(m):
    if m.status in (ChatMemberStatus.LEFT, ChatMemberStatus.BANNED):
        return True
    return m.status == ChatMemberStatus.RESTRICTED and not getattr(m, "is_member", True)


async def auto_register(bot, user):
    """يفعّل المستخدم تلقائياً في كل مجموعة معروفة هو عضو فيها. يرجع قائمة المجموعات."""
    done, new = [], []
    registered = load(USERS_FILE)
    for gid in known_groups():
        if str(user.id) in registered.get(gid, {}):
            done.append(gid)
            continue
        try:
            m = await bot.get_chat_member(int(gid), user.id)
        except TelegramError:
            continue
        if _left(m):
            continue
        new.append(gid)
        done.append(gid)
    if new:
        users = load(USERS_FILE)        # نقرأ من جديد: قد يكون تغيّر أثناء الانتظار
        for gid in new:
            users.setdefault(gid, {})[str(user.id)] = {"name": user.full_name, "username": user.username}
        save(USERS_FILE, users)
    return done


# ---------- بطاقة المالك والترحيب ----------
_OWNER_CACHE = {"t": 0, "data": None}


async def get_owner(bot):
    c = _OWNER_CACHE
    if c["data"] and time.time() - c["t"] < 600:
        return c["data"]
    info = {"name": OWNER_NAME, "username": OWNER_USERNAME, "id": OWNER_ID, "bio": None, "photo": None}
    try:
        chat = await bot.get_chat(OWNER_ID)
        info["name"] = chat.full_name or info["name"]
        if chat.username:
            info["username"] = chat.username
        info["bio"] = getattr(chat, "bio", None)
    except TelegramError:
        pass
    try:
        photos = await bot.get_user_profile_photos(OWNER_ID, limit=1)
        if photos.total_count:
            info["photo"] = photos.photos[0][-1].file_id
    except TelegramError:
        pass
    c.update(t=time.time(), data=info)
    return info


def build_welcome(first_name, owner):
    bio = (owner.get("bio") or "").strip()
    if len(bio) > 120:
        bio = bio[:117] + "..."
    lines = [
        f"👋 أهلاً بك {first_name}!",
        "",
        "أنا بوت إدارة مجموعة المدرسة 🎓",
        "",
        "✨ وظيفتي:",
        "📚 عرض التحاضير والواجبات (اليوم / أمس / غداً) وأرشيفها",
        "📂 عرض القوائم والكتب والأسئلة بأزرار سهلة",
        "📢 إيصال إشعارات الإدارة لك مباشرة في الخاص",
        "",
        "⚙️ كيف أعمل؟",
        "المشرفون يسجّلون المحتوى في المجموعة، وأنا أحفظه وأعرضه لأي طالب يطلبه، "
        "وأرسل لك الإشعارات المهمة بعد تفعيل حسابك.",
        "",
        "👑 المطوّر والمالك:",
        f"الاسم: {owner['name']}",
        f"اليوزر: @{owner['username']}",
        f"الايدي: {owner['id']}",
    ]
    if bio:
        lines.append(f"النبذة: {bio}")
    lines += ["", "📖 اكتب «الاوامر» لعرض جميع الأوامر."]
    return "\n".join(lines)


async def send_welcome(bot, chat_id, first_name):
    owner = await get_owner(bot)
    text = build_welcome(first_name or "", owner)
    markup = Markup([
        [Btn("👑 تواصل مع المالك", url=f"https://t.me/{owner['username']}")],
        [Btn("📖 عرض الأوامر", callback_data="help")],
    ])
    if owner["photo"]:
        try:
            if len(text) <= 1024:
                await bot.send_photo(chat_id, owner["photo"], caption=text, reply_markup=markup)
                return
            await bot.send_photo(chat_id, owner["photo"])
        except BadRequest as e:
            log.warning("صورة المالك: %s", e)
    await bot.send_message(chat_id, text, reply_markup=markup)


async def on_start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    msg, user = update.message, update.effective_user
    payload = context.args[0] if context.args else ""
    if payload.startswith("reg_"):
        gid = payload[4:]
        try:
            member = await context.bot.get_chat_member(int(gid), user.id)
            is_member = not _left(member)
        except (TelegramError, ValueError):
            is_member = False
        if not is_member:
            await msg.reply_text("لم أجدك عضواً في المجموعة ❌\nانضم إليها أولاً ثم اضغط زر التفعيل مجدداً.")
            return
        users = load(USERS_FILE)
        users.setdefault(gid, {})[str(user.id)] = {"name": user.full_name, "username": user.username}
        save(USERS_FILE, users)
        await msg.reply_text(
            "تم تفعيل حسابك بنجاح ✅\n"
            "ستصلك إشعارات الإدارة هنا، ويمكنك الآن الكتابة في المجموعة.\n\n"
            "📖 اكتب «الاوامر» لعرض جميع الأوامر، ويمكنك استخدامها هنا في الخاص أيضاً.",
            reply_markup=Markup([[Btn("📖 عرض الأوامر", callback_data="help")]]),
        )
        return
    await send_welcome(context.bot, msg.chat_id, user.first_name)
    if await auto_register(context.bot, user):
        await msg.reply_text("✅ تم تفعيل حسابك تلقائياً، وستصلك إشعارات الإدارة هنا.")


async def on_new_members(update: Update, context: ContextTypes.DEFAULT_TYPE):
    msg, chat = update.message, update.effective_chat
    remember_group(chat)
    for u in msg.new_chat_members:
        track_user(chat, u)
    cid = str(chat.id)
    reg = load(USERS_FILE).get(cid, {})
    users = [u for u in msg.new_chat_members if not u.is_bot and str(u.id) not in reg]
    if not users:
        return
    mentions = "، ".join(u.mention_html() for u in users[:10])
    note = await msg.reply_text(
        f"👋 أهلاً {mentions}\n\n"
        "⚠️ فعّل البوت أولاً حتى تصلك إشعارات الإدارة وتتمكن من استخدام المجموعة بشكل كامل.\n"
        "اضغط الزر ثم اضغط Start في الخاص 👇",
        parse_mode="HTML",
        reply_markup=Markup([[Btn("✅ تفعيل حسابي", url=reg_link(context.bot, cid))]]),
    )
    spawn(delete_later(note, 600))


async def on_migrate(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """عند ترقية المجموعة إلى سوبر جروب يتغير رقمها: ننقل كل البيانات للرقم الجديد."""
    msg = update.message
    new = getattr(msg, "migrate_to_chat_id", None)
    if not new:
        return
    old, new = str(msg.chat_id), str(new)
    for path in (DB_FILE, MENU_FILE, HW_FILE, USERS_FILE, GROUPS_FILE, SETTINGS_FILE, MEMBERS_FILE):
        d = load(path)
        if old in d:
            d[new] = d.pop(old)
            save(path, d)
    s = load(SETTINGS_FILE)
    if s.get("_owner", {}).get("target") == old:
        s["_owner"]["target"] = new
        save(SETTINGS_FILE, s)
    log.info("نُقلت بيانات المجموعة %s إلى %s", old, new)


async def on_target_cb(update: Update, context: ContextTypes.DEFAULT_TYPE):
    q = update.callback_query
    if q.from_user.id != OWNER_ID:
        await q.answer("للمالك فقط ⛔", show_alert=True)
        return
    gid = q.data.split("|", 1)[1]
    groups = load(GROUPS_FILE)
    if gid not in groups:
        await q.answer("المجموعة غير معروفة", show_alert=True)
        return
    settings = load(SETTINGS_FILE)
    settings.setdefault("_owner", {})["target"] = gid
    save(SETTINGS_FILE, settings)
    await q.answer()
    await q.edit_message_text(
        f"✅ تم تحديد المجموعة: {groups[gid]['title']}\n\n"
        "الآن يمكنك استخدام:\n"
        "• اذاعة — لإرسال رسالة لمفعّليها\n"
        "• المفعلين — لعرض حساباتهم"
    )


async def enforce_registration(update: Update, context: ContextTypes.DEFAULT_TYPE) -> bool:
    """يحذف رسالة غير المفعّل إذا كان الإجبار شغالاً. يرجع True إذا حُذفت."""
    chat, msg, user = update.effective_chat, update.message, update.effective_user
    cid = str(chat.id)
    if not load(SETTINGS_FILE).get(cid, {}).get("force"):
        return False
    if user.is_bot or msg.sender_chat or user.id == ANON_ADMIN_ID:
        return False
    if str(user.id) in load(USERS_FILE).get(cid, {}):
        return False
    if await is_admin(chat, user.id):
        return False
    try:
        await msg.delete()
    except TelegramError:
        return False        # البوت لا يملك صلاحية الحذف
    warned = context.chat_data.setdefault("warned", {})
    if time.time() - warned.get(user.id, 0) > 60:
        warned[user.id] = time.time()
        note = await chat.send_message(
            f"⚠️ {user.mention_html()} يجب تفعيل حسابك أولاً حتى تتمكن من الكتابة في المجموعة.\n"
            "اضغط الزر ثم اضغط Start في الخاص 👇",
            parse_mode="HTML",
            reply_markup=Markup([[Btn("✅ تفعيل حسابي", url=reg_link(context.bot, cid))]]),
        )
        spawn(delete_later(note, 30))
    return True


# ================= @all : منشن لكل الأعضاء =================
def mention(uid, name):
    return f'<a href="tg://user?id={uid}">{html.escape((name or "عضو")[:25])}</a>'


async def mention_targets(bot, chat, exclude=()):
    """كل من يعرفهم البوت في المجموعة (مشاهَدون + مفعّلون + مشرفون) بعد حذف من غادر."""
    gid = str(chat.id)
    cand = {}
    for src in (load(MEMBERS_FILE).get(gid, {}), load(USERS_FILE).get(gid, {})):
        for uid, info in src.items():
            cand[int(uid)] = info.get("name") or "عضو"
    admins = {}
    try:
        for a in await bot.get_chat_administrators(chat.id):
            if not a.user.is_bot:
                admins[a.user.id] = a.user.full_name or "مشرف"
    except TelegramError as e:
        log.warning("get_chat_administrators: %s", e)
    cand.update(admins)

    sem = asyncio.Semaphore(10)

    async def check(uid):
        async with sem:
            try:
                m = await bot.get_chat_member(chat.id, uid)
            except TelegramError:
                return uid, True        # عند الخطأ لا نحذفه
            return uid, not _left(m)

    to_check = [u for u in cand if u not in admins and u not in exclude]
    results = await asyncio.gather(*(check(u) for u in to_check))
    gone = {u for u, ok in results if not ok}
    if gone:
        for path in (MEMBERS_FILE, USERS_FILE):
            d = load(path)
            for u in gone:
                d.get(gid, {}).pop(str(u), None)
            save(path, d)
        _SEEN.difference_update({k for k in _SEEN if k[0] == chat.id and k[1] in gone})
    people = [(u, n) for u, n in cand.items() if u not in gone and u not in exclude]
    people.sort(key=lambda p: p[1])
    return people


async def send_mentions(send, names, header=""):
    """يرسل المنشن؛ وإذا رفض تيليجرام أحد الأسماء نقسّم المجموعة ونتخطى الاسم المعطوب."""
    try:
        await send(header + "، ".join(names))
    except BadRequest as e:
        if len(names) == 1:
            log.warning("تعذر منشن: %s", e)
            if header:
                await send(header.strip())
            return
        mid = len(names) // 2
        await send_mentions(send, names[:mid], header)
        await send_mentions(send, names[mid:])
    except RetryAfter as e:
        await asyncio.sleep(retry_secs(e) + 1)
        await send_mentions(send, names, header)


async def cmd_all(update, context):
    msg, chat, user = update.message, update.effective_chat, update.effective_user
    if not await is_admin(chat, user.id):
        return
    cd = context.chat_data
    if user.id != OWNER_ID and time.time() - cd.get("all_t", 0) < ALL_COOLDOWN:
        await msg.reply_text("⏳ انتظر قليلاً قبل استخدام @all مرة أخرى")
        return
    cd["all_t"] = time.time()
    exclude = {user.id} if user.id != ANON_ADMIN_ID else set()
    people = await mention_targets(context.bot, chat, exclude)
    if not people:
        await msg.reply_text(
            "لم أتعرف على أعضاء بعد 🤷\n"
            "سأتعرف على كل عضو عندما يكتب في المجموعة أو يفعّل حسابه."
        )
        return
    names = [mention(u, n) for u, n in people]

    async def send(text):
        await msg.reply_text(text, parse_mode="HTML")

    for i in range(0, len(names), 30):
        header = "📢 <b>تنبيه للجميع</b>\n\n" if i == 0 else ""
        await send_mentions(send, names[i:i + 30], header)
        await asyncio.sleep(0.3)


# ================= نصوص المساعدة =================
HELP_STUDENT = """📖 دليل الأوامر

📚 التحاضير والواجبات
• تحاضير اليوم — تحضير اليوم
• تحاضير امس — تحضير أمس
• تحاضير باجر — تحضير الغد
• واجبات اليوم / امس / باجر — نفس الشيء
• التحاضير — أرشيف كل الأيام (اختر الشهر ثم اليوم)

📂 القوائم والكتب والأسئلة
• الازرار — يعرض كل القوائم لتختار منها
• أو اكتب اسم القائمة مباشرة، مثل: اساله
• الردود — يعرض الكلمات التي يرد عليها البوت

🔔 تفعيل الحساب
• اكتب /start للبوت في الخاص، أو اضغط زر «تفعيل حسابي»
  لتصلك إشعارات الإدارة في الخاص

ℹ️ اكتب «الاوامر» في أي وقت لعرض هذه القائمة"""

HELP_ADMIN = """🔒 أوامر المشرفين (داخل المجموعة)

📣 التنبيه
• @all — منشن لكل الأعضاء (يصل الإشعار حتى للمكتومين)

📚 التحاضير
• اضف تحضير اليوم — ثم أرسل المحتوى واضغط «تم»
  (يمكنك كتابة: امس / باجر / تاريخ مثل 2026-10-12)
• حذف تحضير اليوم — يحذف تحضير ذلك اليوم

💬 الردود والقوائم
• اضف رد كلمة — رد بسيط: يسألك عن الرد ثم يحفظه
• اضف رد متعدد — قائمة أزرار فيها نصوص وكتب وصور وفيديو، ويمكن وضع أزرار داخل أزرار
• تعديل رد كلمة — تعديل القائمة (إضافة زر، حذف، تغيير الاسم أو المحتوى)
• حذف رد كلمة — يحذف الرد أو القائمة كاملة

🔔 التفعيل والإذاعة
• رسالة التفعيل — ينشر رسالة بزر تفعيل الحساب
• المفعلين — عدد المفعّلين من إجمالي الأعضاء
• اذاعة — يرسل رسالتك في الخاص لكل المفعّلين
• اجبار التفعيل — يحذف رسائل غير المفعّلين حتى يفعّلوا
• ايقاف الاجبار — يوقف الإجبار

❌ الغاء — يلغي أي عملية جارية"""

HELP_OWNER = """👑 أوامر المالك (في الخاص مع البوت)

• المجموعات — اختيار المجموعة التي تتعامل معها
• المجموعة الحالية — يعرض المجموعة المحددة
• المفعلين — قائمة حسابات المفعّلين (الاسم واليوزر والايدي)
• اذاعة — يرسل رسالتك لكل مفعّلي المجموعة المحددة

💡 بعد تحديد المجموعة تعمل أوامر المشرفين (مثل اضف تحضير) هنا في الخاص على تلك المجموعة."""


async def help_for(chat, user_id):
    parts = [HELP_STUDENT]
    if chat.type == "private":
        if user_id == OWNER_ID:
            parts += [HELP_ADMIN, HELP_OWNER]
    elif await is_admin(chat, user_id):
        parts.append(HELP_ADMIN)
    return "\n\n".join(parts)


# ================= التحاضير =================
def months_markup(hw):
    yms = sorted({k[:7] for k in hw if hw[k]})
    with_year = len({ym[:4] for ym in yms}) > 1
    btns = [Btn(month_label(ym, with_year), callback_data=f"hw|m|{ym}") for ym in yms]
    rows = [btns[i:i + 3] for i in range(0, len(btns), 3)]
    rows.append([Btn("📖 عرض الأوامر", callback_data="help")])
    return Markup(rows)


async def show_hw(msg, chat_id, d):
    items = load(HW_FILE).get(chat_id, {}).get(d.isoformat())
    if not items:
        await msg.reply_text(f"لا يوجد تحضير مسجّل ليوم {day_label(d)}")
        return
    await msg.reply_text(f"📚 تحاضير {day_label(d)}")
    await send_items(msg, items)


async def on_hw_cb(update: Update, context: ContextTypes.DEFAULT_TYPE):
    q = update.callback_query
    parts = q.data.split("|")
    act = parts[1] if len(parts) > 1 else ""
    hw = {k: v for k, v in load(HW_FILE).get(data_id(q.message.chat, q.from_user.id), {}).items() if v}
    try:
        if act == "d" and len(parts) > 2:
            items = hw.get(parts[2])
            if not items:
                await q.answer("لا يوجد تحضير لهذا اليوم", show_alert=True)
                return
            await q.answer()
            tgt = cb_target(context, q)
            await tgt.reply_text(f"📚 تحاضير {day_label(date.fromisoformat(parts[2]))}")
            await send_items(tgt, items)
            return

        if not hw:
            await q.answer("لا توجد تحاضير مسجلة", show_alert=True)
            return
        await q.answer()

        if act == "r":
            await q.edit_message_text("📚 اختر الشهر:", reply_markup=months_markup(hw))
        elif act == "m" and len(parts) > 2:
            ym = parts[2]
            with_year = len({k[:4] for k in hw}) > 1
            btns = []
            for k in sorted(k for k in hw if k.startswith(ym)):
                d = date.fromisoformat(k)
                btns.append(Btn(f"{WEEKDAYS[d.weekday()]} {d.day}", callback_data=f"hw|d|{k}"))
            rows = [btns[i:i + 3] for i in range(0, len(btns), 3)]
            rows.append([Btn("🔙 رجوع", callback_data="hw|r")])
            await q.edit_message_text(
                f"📅 {month_label(ym, with_year)} — اختر اليوم:", reply_markup=Markup(rows)
            )
    except (BadRequest, ValueError) as e:
        log.info("hw_cb: %s", e)


async def on_help_cb(update: Update, context: ContextTypes.DEFAULT_TYPE):
    q = update.callback_query
    await q.answer()
    await cb_target(context, q).reply_text(await help_for(q.message.chat, q.from_user.id))


async def on_index_cb(update: Update, context: ContextTypes.DEFAULT_TYPE):
    q = update.callback_query
    menus = load(MENU_FILE).get(data_id(q.message.chat, q.from_user.id), {})
    if not menus:
        await q.answer("لا توجد قوائم مضافة", show_alert=True)
        return
    await q.answer()
    try:
        await q.edit_message_text("📚 القوائم المتاحة، اختر ما تريد:", reply_markup=index_markup(menus))
    except BadRequest as e:
        log.info("index_cb: %s", e)


# ================= الرسائل =================
def find_menu(menus, word):
    n = norm(word)
    for mid, m in menus.items():
        if norm(m["keyword"]) == n:
            return mid, m
    return None, None


def find_reply(replies, word):
    n = norm(word)
    for w, r in replies.items():
        if norm(w) == n:
            return r
    return None


async def need_admin(c):
    if await is_admin(c.chat, c.uid):
        return True
    await c.msg.reply_text("هذا الأمر للمشرفين فقط ⛔")
    return False


async def handle_state(update, st, chat_id) -> bool:
    """رسائل المستخدم أثناء عملية جارية (رد بسيط / قائمة / تحضير / إذاعة)."""
    msg = update.message
    if st.get("mode") == "reply":
        if not msg.text:
            return False
        if msg.text.strip() in CANCEL_WORDS:
            st["finished"] = True
            await msg.reply_text("تم إلغاء العملية ❌")
            return True
        data = load(DB_FILE)
        data.setdefault(chat_id, {})[st["word"]] = msg.text
        save(DB_FILE, data)
        st["finished"] = True
        await msg.reply_text("تم وضع الرد بنجاح ✅")
        return True
    return await handle_multi(update, st, chat_id)


async def cmd_help(c):
    if c.ntext != "الاوامر":
        return False
    await c.msg.reply_text(await help_for(c.chat, c.uid))
    return True


async def gate_private(c):
    """الخاص: الطالب غير المفعّل، أو المالك بدون مجموعة محددة."""
    if c.chat.type != "private":
        return False
    unlinked = c.chat_id == str(c.chat.id)
    if not unlinked:
        return False
    if c.uid != OWNER_ID:
        await c.msg.reply_text(
            "فعّل حسابك أولاً حتى أستطيع خدمتك هنا 🔔\n"
            "اضغط زر «تفعيل حسابي» في رسالة المجموعة، أو تأكد أنك عضو في المجموعة ثم اكتب /start."
        )
        return True
    needs_group = (
        HW_ADD.match(c.ntext) or HW_DEL.match(c.ntext) or HW_VIEW.match(c.ntext)
        or c.ntext in ("التحاضير", "الازرار", "الردود")
        or ADD_REPLY.match(c.text) or EDIT_REPLY.match(c.text) or DEL_REPLY.match(c.text)
    )
    if needs_group:
        await c.msg.reply_text("حدد المجموعة أولاً: اكتب «المجموعات»")
        return True
    return False


async def cmd_replies_admin(c):
    t, msg = c.text, c.msg
    if ADD_MULTI.match(t):
        if await need_admin(c):
            set_state(c.cd, c.uid, step="keyword", mode="new")
            await msg.reply_text("اكتب كلمة الرد (مثلاً: اسئلة) ✍️\nللإلغاء اكتب: الغاء")
        return True

    m = EDIT_REPLY.match(t)
    if m:
        if not await need_admin(c):
            return True
        menus = load(MENU_FILE).get(c.chat_id, {})
        mid, menu = find_menu(menus, m.group(1).strip().strip('"“”'))
        if menu:
            await msg.reply_text(edit_text(menu, []), reply_markup=edit_markup(menu, mid, []))
        else:
            await msg.reply_text("لا توجد قائمة أزرار بهذه الكلمة (التعديل للردود المتعددة فقط)")
        return True

    m = ADD_REPLY.match(t)
    if m:
        if not await need_admin(c):
            return True
        word = m.group(1).strip().strip('"“”')
        if word:
            set_state(c.cd, c.uid, step="reply", mode="reply", word=word)
            await msg.reply_text("خصص الرد الذي تريده ✍️\nللإلغاء اكتب: الغاء")
        return True

    m = DEL_REPLY.match(t)
    if m:
        if not await need_admin(c):
            return True
        n = norm(m.group(1).strip().strip('"“”'))
        found = False
        data = load(DB_FILE)
        rep = data.get(c.chat_id, {})
        for w in [w for w in rep if norm(w) == n]:
            del rep[w]
            found = True
        if found:
            save(DB_FILE, data)
        menus = load(MENU_FILE)
        cm = menus.get(c.chat_id, {})
        for k in [k for k, v in cm.items() if norm(v["keyword"]) == n]:
            del cm[k]
            found = True
            save(MENU_FILE, menus)
        await msg.reply_text("تم حذف الرد 🗑️" if found else "لا يوجد رد بهذه الكلمة")
        return True
    return False


async def cmd_owner(c):
    """أوامر المالك في الخاص: اختيار المجموعة، قائمة المفعّلين، الإذاعة."""
    if c.chat.type != "private" or c.uid != OWNER_ID:
        return False
    nt, msg = c.ntext, c.msg
    if nt not in ("المجموعات", "تحديد المجموعه", "المجموعه الحاليه", "المفعلين", "اذاعه"):
        return False
    groups = load(GROUPS_FILE)
    target = load(SETTINGS_FILE).get("_owner", {}).get("target")
    title = groups.get(target, {}).get("title", target) if target else None

    if nt in ("المجموعات", "تحديد المجموعه"):
        if not groups:
            await msg.reply_text(
                "لا توجد مجموعات معروفة بعد.\n"
                "أضف البوت لمجموعتك واكتب فيها أي رسالة ليتعرف عليها، ثم أعد المحاولة."
            )
        else:
            rows = [[Btn(("✅ " if gid == target else "📍 ") + g["title"], callback_data=f"tg|{gid}")]
                    for gid, g in groups.items()]
            await msg.reply_text("اختر المجموعة التي تريد التعامل معها:", reply_markup=Markup(rows))
    elif nt == "المجموعه الحاليه":
        await msg.reply_text(f"📍 المجموعة المحددة: {title}" if target else "لم تحدد مجموعة بعد. اكتب: المجموعات")
    elif not target:
        await msg.reply_text("حدد المجموعة أولاً: اكتب «المجموعات»")
    elif nt == "المفعلين":
        reg = load(USERS_FILE).get(target, {})
        if not reg:
            await msg.reply_text(f"لا يوجد مفعّلون في «{title}» بعد")
        else:
            lines = []
            for i, (uid, u) in enumerate(reg.items(), 1):
                uname = f" (@{u['username']})" if u.get("username") else ""
                lines.append(f"{i}. {u.get('name', '—')}{uname} — {uid}")
            try:
                total = await c.context.bot.get_chat_member_count(int(target))
            except TelegramError:
                total = None
            header = f"👥 المفعّلون في «{title}»: {len(reg)}" + (f" من {total} عضو" if total else "")
            chunks = chunk_lines(lines)
            await msg.reply_text(header + "\n\n" + chunks[0])
            for part in chunks[1:]:
                await msg.reply_text(part)
    else:  # اذاعه
        set_state(c.cd, c.uid, step="content", mode="bc", items=[], status=None, msg_ids=[], group=target)
        await msg.reply_text(
            f"📢 الإرسال إلى مفعّلي «{title}»\n"
            "أرسل الرسالة (نص / صور / ملفات / فيديو)، ويمكنك إرسال أكثر من رسالة، "
            "ثم اضغط 📨 إرسال للجميع.\nللإلغاء اكتب: الغاء"
        )
    return True


async def cmd_group_admin(c):
    nt, msg = c.ntext, c.msg
    if nt not in ("رساله التفعيل", "اجبار التفعيل", "ايقاف الاجبار", "المفعلين", "اذاعه"):
        return False
    if c.chat.type == "private":
        await msg.reply_text("هذا الأمر يُستخدم داخل المجموعة")
        return True
    if not await need_admin(c):
        return True

    if nt == "رساله التفعيل":
        await msg.reply_text(
            "🔔 لتفعيل حسابك واستلام إشعارات الإدارة في الخاص:\n"
            "اضغط الزر ثم اضغط Start في محادثة البوت 👇",
            reply_markup=Markup([[Btn("✅ تفعيل حسابي", url=reg_link(c.context.bot, c.chat_id))]]),
        )
    elif nt in ("اجبار التفعيل", "ايقاف الاجبار"):
        on = nt == "اجبار التفعيل"
        settings = load(SETTINGS_FILE)
        settings.setdefault(c.chat_id, {})["force"] = on
        save(SETTINGS_FILE, settings)
        if on:
            await msg.reply_text(
                "تم تشغيل الإجبار ✅\nسيحذف البوت رسائل غير المفعّلين مع تنبيههم بزر التفعيل.\n"
                "(يلزم أن يكون البوت مشرفاً بصلاحية حذف الرسائل)"
            )
        else:
            await msg.reply_text("تم إيقاف الإجبار ✅")
    elif nt == "المفعلين":
        reg_n = len(load(USERS_FILE).get(c.chat_id, {}))
        force = load(SETTINGS_FILE).get(c.chat_id, {}).get("force")
        try:
            total = await c.chat.get_member_count()
        except TelegramError:
            total = "؟"
        await msg.reply_text(f"👥 المفعّلون: {reg_n} من {total} عضو\nالإجبار: {'شغّال ✅' if force else 'متوقف'}")
    else:  # اذاعه
        set_state(c.cd, c.uid, step="content", mode="bc", items=[], status=None, msg_ids=[])
        await msg.reply_text(
            "📢 أرسل الرسالة التي تريد إرسالها لكل المفعّلين (نص / صور / ملفات / فيديو)،\n"
            "ويمكنك إرسال أكثر من رسالة، ثم اضغط 📨 إرسال للجميع.\nللإلغاء اكتب: الغاء"
        )
    return True


async def cmd_homework_admin(c):
    nt, msg = c.ntext, c.msg
    m = HW_ADD.match(nt)
    if m:
        if not await need_admin(c):
            return True
        d = parse_day(m.group(2))
        if not d:
            await msg.reply_text("اكتب اليوم هكذا: اليوم / امس / باجر / 2026-10-12")
            return True
        set_state(c.cd, c.uid, step="content", mode="hw", date=d.isoformat(),
                  label=day_label(d), items=[], status=None)
        await msg.reply_text(
            f"📅 تحضير {day_label(d)}\n"
            "أرسل المحتوى (رسائل / ملفات / صور / فيديو) ويمكنك إرسال أكثر من واحد، "
            "ثم اضغط ✅ تم.\nللإلغاء اكتب: الغاء"
        )
        return True

    m = HW_DEL.match(nt)
    if m:
        if not await need_admin(c):
            return True
        d = parse_day(m.group(2))
        if not d:
            await msg.reply_text("اكتب اليوم هكذا: اليوم / امس / باجر / 2026-10-12")
            return True
        hw = load(HW_FILE)
        if hw.get(c.chat_id, {}).pop(d.isoformat(), None) is not None:
            save(HW_FILE, hw)
            await msg.reply_text(f"تم حذف تحضير {day_label(d)} 🗑️")
        else:
            await msg.reply_text("لا يوجد تحضير مسجّل بهذا اليوم")
        return True
    return False


async def cmd_public(c):
    """أوامر الجميع: الردود، التحاضير، القوائم، والردود التلقائية."""
    nt, msg = c.ntext, c.msg
    replies = load(DB_FILE).get(c.chat_id, {})
    menus = load(MENU_FILE).get(c.chat_id, {})
    day = None
    m = HW_VIEW.match(nt)
    if m:
        day = parse_day(m.group(2))
    mid, menu = find_menu(menus, c.text)
    reply = find_reply(replies, c.text) if not menu else None

    is_cmd = nt in ("الردود", "التحاضير", "الازرار") or day or menu or reply is not None
    if not is_cmd:
        return False
    if await need_flood(c):
        return True

    if nt == "الردود":
        lines = [f"• {w}" for w in replies] + [f"• {mm['keyword']} (قائمة أزرار)" for mm in menus.values()]
        await msg.reply_text("الردود الحالية:\n" + "\n".join(lines) if lines else "لا توجد ردود مضافة بعد")
    elif nt == "التحاضير":
        hw = {k: v for k, v in load(HW_FILE).get(c.chat_id, {}).items() if v}
        if not hw:
            await msg.reply_text("لا توجد تحاضير مسجلة بعد")
        else:
            await msg.reply_text("📚 اختر الشهر:", reply_markup=months_markup(hw))
    elif nt == "الازرار":
        if not menus:
            await msg.reply_text("لا توجد أزرار مضافة بعد")
        else:
            await msg.reply_text("📚 القوائم المتاحة، اختر ما تريد:", reply_markup=index_markup(menus))
    elif day:
        await show_hw(msg, c.chat_id, day)
    elif menu:
        await msg.reply_text("📋 اختر من القائمة:", reply_markup=keyboard(menu, mid, []))
    else:
        await msg.reply_text(reply)
    return True


async def need_flood(c):
    """الطالب العادي فقط يخضع لمنع التكرار السريع (المشرف حر)."""
    if c.uid == OWNER_ID or (c.chat.type != "private" and await is_admin(c.chat, c.uid)):
        return False
    return flooded(c.cd, c.uid)


async def on_message(update: Update, context: ContextTypes.DEFAULT_TYPE):
    msg, user, chat = update.message, update.effective_user, update.effective_chat
    if not msg or not user:
        return
    uid, cd = user.id, context.chat_data
    chat_id = data_id(chat, uid)

    if chat.type == "private":
        # طالب راسل البوت ولم يُسجَّل: نفعّله تلقائياً إن كان عضواً في مجموعة معروفة
        if uid != OWNER_ID and chat_id == str(chat.id) and time.time() - cd.get("reg_try", 0) > 60:
            cd["reg_try"] = time.time()
            if await auto_register(context.bot, user):
                chat_id = data_id(chat, uid)
                await msg.reply_text("✅ تم تفعيل حسابك تلقائياً، وستصلك إشعارات الإدارة هنا.")
    else:
        remember_group(chat)
        track_user(chat, user)
        if await enforce_registration(update, context):
            return

    # عملية جارية لهذا المستخدم (إضافة رد / قائمة / تحضير / إذاعة)
    st = get_state(cd, uid)
    if st and await handle_state(update, st, chat_id):
        if st.get("finished"):
            cleanup_state(chat_id, st)
            clear_state(cd, uid)
        return

    # @all
    if chat.type != "private" and ALL_RE.search(msg.text or msg.caption or ""):
        await cmd_all(update, context)
        return

    text = (msg.text or "").strip()
    if not text:
        return
    c = SimpleNamespace(update=update, context=context, msg=msg, chat=chat, uid=uid, cd=cd,
                        chat_id=chat_id, text=text, ntext=norm(text))
    for handler in (cmd_help, gate_private, cmd_replies_admin, cmd_owner,
                    cmd_group_admin, cmd_homework_admin, cmd_public):
        if await handler(c):
            return


# ================= الأخطاء والتشغيل =================
_ERR = {"t": 0}


async def on_error(update, context):
    err = context.error
    log.error("خطأ غير متوقع", exc_info=err)
    if isinstance(err, NetworkError) or type(err).__name__ == "Conflict":
        return
    if time.time() - _ERR["t"] < 600:      # إشعار واحد للمالك كل 10 دقائق كحد أقصى
        return
    _ERR["t"] = time.time()
    try:
        await context.bot.send_message(OWNER_ID, f"⚠️ خطأ في البوت:\n{type(err).__name__}: {str(err)[:300]}")
    except Exception:
        pass


def main():
    if TOKEN.startswith("ضع_"):
        raise SystemExit("ضع توكن البوت في المتغير BOT_TOKEN")
    app = Application.builder().token(TOKEN).build()
    app.add_handler(CommandHandler("start", on_start, filters=filters.ChatType.PRIVATE))
    app.add_handler(MessageHandler(filters.StatusUpdate.NEW_CHAT_MEMBERS, on_new_members))
    app.add_handler(MessageHandler(filters.StatusUpdate.MIGRATE, on_migrate))
    app.add_handler(CallbackQueryHandler(on_target_cb, pattern=r"^tg\|"))
    app.add_handler(CallbackQueryHandler(on_create_cb, pattern=r"^cr\|"))
    app.add_handler(CallbackQueryHandler(on_edit_cb, pattern=r"^ed\|"))
    app.add_handler(CallbackQueryHandler(on_nav, pattern=r"^n\|"))
    app.add_handler(CallbackQueryHandler(on_index_cb, pattern=r"^ix$"))
    app.add_handler(CallbackQueryHandler(on_hw_cb, pattern=r"^hw\|"))
    app.add_handler(CallbackQueryHandler(on_help_cb, pattern=r"^help$"))
    app.add_handler(
        MessageHandler(
            (filters.TEXT & ~filters.COMMAND) | filters.PHOTO | filters.Document.ALL
            | filters.VIDEO | filters.AUDIO | filters.VOICE | filters.Sticker.ALL | filters.ANIMATION,
            on_message,
        )
    )
    app.add_error_handler(on_error)
    log.info("البوت يعمل...")
    app.run_polling(drop_pending_updates=True)


if __name__ == "__main__":
    main()
