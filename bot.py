# -*- coding: utf-8 -*-
# pip install "python-telegram-bot>=20"
import asyncio
import json
import os
import re
import time
from datetime import date, datetime, timedelta, timezone

from telegram import InlineKeyboardButton as Btn
from telegram import InlineKeyboardMarkup as Markup
from telegram import InputMediaDocument, InputMediaPhoto, Update
from telegram.constants import ChatMemberStatus
from telegram.error import BadRequest, Forbidden
from telegram.ext import (
    Application,
    CallbackQueryHandler,
    CommandHandler,
    ContextTypes,
    MessageHandler,
    filters,
)

# على Railway: ضع التوكن في Variables باسم BOT_TOKEN، ومسار الـ Volume باسم DATA_DIR
TOKEN = os.environ.get("BOT_TOKEN", "ضع_التوكن_هنا")
DATA_DIR = os.environ.get("DATA_DIR", ".")
DB_FILE = os.path.join(DATA_DIR, "replies.json")    # الردود البسيطة
MENU_FILE = os.path.join(DATA_DIR, "menus.json")    # الردود المتعددة (الأزرار)
HW_FILE = os.path.join(DATA_DIR, "homework.json")   # التحاضير حسب التاريخ
USERS_FILE = os.path.join(DATA_DIR, "users.json")   # الطلاب المفعّلون (راسلوا البوت)
SETTINGS_FILE = os.path.join(DATA_DIR, "settings.json")  # إعدادات كل مجموعة
GROUPS_FILE = os.path.join(DATA_DIR, "groups.json")      # المجموعات التي يعرفها البوت
OWNER_ID = int(os.environ.get("OWNER_ID", "6970354026"))
OWNER_USERNAME = os.environ.get("OWNER_USERNAME", "p1oco")
OWNER_NAME = os.environ.get("OWNER_NAME", "المطوّر")
TZ_HOURS = float(os.environ.get("TZ_OFFSET", "3"))  # فرق التوقيت عن UTC (الرياض = 3)


# ---------------- التخزين ----------------
def load(path):
    if os.path.exists(path):
        with open(path, "r", encoding="utf-8") as f:
            return json.load(f)
    return {}


def save(path, data):
    with open(path, "w", encoding="utf-8") as f:
        json.dump(data, f, ensure_ascii=False, indent=2)


async def is_admin(chat, user_id) -> bool:
    if chat.type == "private":
        return user_id == OWNER_ID      # في الخاص: المالك فقط
    member = await chat.get_member(user_id)
    return member.status in (ChatMemberStatus.ADMINISTRATOR, ChatMemberStatus.OWNER)


def data_id(chat, user_id):
    """أي مجموعة نقرأ منها البيانات؟ في الخاص: مجموعة الطالب المفعّل، أو المجموعة التي حددها المالك."""
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


# ---------------- أدوات الأزرار ----------------
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
    """محتوى الزر: قائمة عناصر (نص / صورة / ملف). يدعم الأزرار القديمة أيضاً."""
    if "items" in node:
        return node["items"]
    if "content" in node:
        return [{"type": node["type"], "content": node["content"], "caption": node.get("caption")}]
    return []


def keyboard(menu, mid, path, idx=False):
    """idx=True: القائمة فُتحت من أمر (الازرار) فيظهر زر العودة للقائمة الرئيسية."""
    sfx = "|1" if idx else ""
    kids = children(menu, path)
    rows = [
        [Btn(n["name"], callback_data=f"n|{mid}|{join_path(path + [i])}{sfx}")]
        for i, n in enumerate(kids)
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


async def send_items(msg, items):
    """يرسل كل العناصر. الملفات المتتالية تُرسل كمجموعة واحدة (حتى 10)."""
    i = 0
    while i < len(items):
        it = items[i]
        if it["type"] == "text":
            await msg.reply_text(it["content"])
            i += 1
            continue
        j = i
        while j < len(items) and items[j]["type"] == it["type"] and j - i < 10:
            j += 1
        chunk = items[i:j]
        if len(chunk) == 1:
            if it["type"] == "photo":
                await msg.reply_photo(it["content"], caption=it.get("caption"))
            else:
                await msg.reply_document(it["content"], caption=it.get("caption"))
        else:
            cls = InputMediaPhoto if it["type"] == "photo" else InputMediaDocument
            await msg.reply_media_group([cls(c["content"], caption=c.get("caption")) for c in chunk])
        i = j


# ---------------- لوحة التعديل ----------------
def edit_text(menu, path):
    return f"✏️ تعديل: {breadcrumb(menu, path)}\nافتح زراً للدخول إليه، أو أضف زراً جديداً هنا"


def edit_markup(menu, mid, path):
    kids = children(menu, path)
    here = join_path(path)
    rows = [
        [Btn("📂 " + n["name"], callback_data=f"ed|{mid}|{join_path(path + [i])}|go")]
        for i, n in enumerate(kids)
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


async def on_edit_cb(update: Update, context: ContextTypes.DEFAULT_TYPE):
    q = update.callback_query
    _, mid, p, act = q.data.split("|")
    path = parse_path(p)
    chat = q.message.chat
    if not await is_admin(chat, q.from_user.id):
        await q.answer("للمشرفين فقط ⛔", show_alert=True)
        return
    data = load(MENU_FILE)
    menu = data.get(data_id(chat, q.from_user.id), {}).get(mid)
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
            cd["multi"] = {"user": q.from_user.id, "step": "name", "menu": mid,
                           "parent": path, "mode": "new", "items": [], "status": None}
            await q.edit_message_text(f"📍 {breadcrumb(menu, path)}\nاكتب اسم الزر الجديد 🔘")

        elif act == "repl":
            cd["multi"] = {"user": q.from_user.id, "step": "content", "menu": mid,
                           "path": path, "mode": "replace", "items": [], "status": None}
            await q.edit_message_text(
                f"📍 {breadcrumb(menu, path)}\nأرسل المحتوى الجديد (نص / صور / كتب)، "
                "وبعد الانتهاء اضغط ✅ تم.\nالمحتوى القديم سيُستبدل."
            )

        elif act == "ren":
            cd["multi"] = {"user": q.from_user.id, "step": "rename", "menu": mid, "path": path}
            await q.edit_message_text("اكتب الاسم الجديد للزر 🏷")

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
    except BadRequest:
        pass


# ---------------- إضافة رد متعدد ----------------
def choose_markup(st):
    rows = [
        [Btn("➕ زر آخر في نفس المستوى", callback_data="cr|more")],
        [Btn("📁 زر داخل هذا الزر", callback_data="cr|sub")],
    ]
    if st["parent"]:
        rows.append([Btn("⬆️ العودة للمستوى الأعلى", callback_data="cr|up")])
    rows.append([Btn("✅ انتهاء وحفظ", callback_data="cr|done")])
    return Markup(rows)


async def handle_multi(update: Update, st) -> bool:
    """يرجع True إذا تم استهلاك الرسالة."""
    msg = update.message
    chat_id = data_id(update.effective_chat, update.effective_user.id)
    step = st["step"]

    if step == "choose":
        return False

    if msg.text and msg.text.strip() in ("الغاء", "إلغاء"):
        st["finished"] = True
        await msg.reply_text("تم إلغاء العملية ❌ (ما تم حفظه سابقاً يبقى)")
        return True

    data = load(MENU_FILE)
    chat_menus = data.setdefault(chat_id, {})

    if step == "keyword":
        if not msg.text:
            return True
        word = msg.text.strip().strip('"“”')
        for k in [k for k, v in chat_menus.items() if v["keyword"] == word]:
            del chat_menus[k]
        mid = str(max([int(k) for k in chat_menus] + [0]) + 1)
        chat_menus[mid] = {"keyword": word, "buttons": []}
        save(MENU_FILE, data)
        st.update(step="name", menu=mid, parent=[], mode="new", items=[], status=None)
        await msg.reply_text("اكتب اسم الزر 🔘")
        return True

    if step == "name":
        if not msg.text:
            return True
        st["name"] = msg.text.strip()[:60]
        st["items"] = []
        st["status"] = None
        st["step"] = "content"
        await msg.reply_text(
            "أرسل محتوى الزر 📎\n"
            "يمكنك إرسال أكثر من كتاب / ملف / صورة / نص، وكلها ستُرسل عند الضغط على الزر.\n"
            "وعند الانتهاء اضغط ✅ تم."
        )
        return True

    if step == "content":
        if msg.text:
            item = {"type": "text", "content": msg.text}
        elif msg.photo:
            item = {"type": "photo", "content": msg.photo[-1].file_id, "caption": msg.caption}
        elif msg.document:
            item = {"type": "document", "content": msg.document.file_id, "caption": msg.caption}
        else:
            return True
        st["items"].append(item)
        label = f"تمت إضافة {len(st['items'])} ✅\nأرسل المزيد (كتاب / ملف / صورة / نص) أو اضغط الزر أدناه"
        if st.get("mode") == "bc":
            if not st.get("group"):
                st.setdefault("msg_ids", []).append(msg.message_id)
            btn_text = "📨 إرسال للجميع"
        elif st.get("mode") == "hw":
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
        if not msg.text:
            return True
        node = get_node(chat_menus[st["menu"]]["buttons"], st["path"])
        node["name"] = msg.text.strip()[:60]
        save(MENU_FILE, data)
        st["finished"] = True
        await msg.reply_text("تم تغيير الاسم ✅")
        return True

    return False


async def on_create_cb(update: Update, context: ContextTypes.DEFAULT_TYPE):
    q = update.callback_query
    st = context.chat_data.get("multi")
    if not st or st["user"] != q.from_user.id:
        await q.answer("هذه الأزرار لمن بدأ العملية فقط", show_alert=True)
        return
    act = q.data.split("|")[1]
    chat_id = data_id(q.message.chat, q.from_user.id)

    # ---- حفظ محتوى الزر ----
    if act == "fin":
        if st["step"] != "content":
            await q.answer()
            return
        if not st["items"]:
            await q.answer("أرسل محتوى أولاً", show_alert=True)
            return
        await q.answer()

        if st.get("mode") == "bc":
            context.chat_data.pop("multi", None)
            gid = st.get("group", chat_id)
            reg_n = len(load(USERS_FILE).get(gid, {}))
            if reg_n == 0:
                await q.edit_message_text("لا يوجد أعضاء مفعّلون بعد ❌\nانشر أولاً الأمر: رسالة التفعيل")
                return
            await q.edit_message_text(f"⏳ جاري الإرسال إلى {reg_n} عضو...")
            ok, fail = await broadcast(context.bot, gid, st["items"])
            for m_id in st.get("msg_ids", []):
                try:
                    await context.bot.delete_message(q.message.chat.id, m_id)
                except Exception:
                    pass
            text = f"📨 تم الإرسال إلى {ok} عضو ✅"
            if fail:
                text += f"\n⚠️ تعذّر الإرسال إلى {fail} (أوقفوا البوت أو حظروه)"
            try:
                total = await context.bot.get_chat_member_count(int(gid))
                left = len(load(USERS_FILE).get(gid, {}))
                text += f"\n👥 المفعّلون {left} من {total} عضو"
            except Exception:
                pass
            await q.edit_message_text(text)
            return

        if st.get("mode") == "hw":
            hw = load(HW_FILE)
            hw.setdefault(chat_id, {}).setdefault(st["date"], []).extend(st["items"])
            save(HW_FILE, hw)
            context.chat_data.pop("multi", None)
            await q.edit_message_text(f"تم حفظ التحضير بنجاح ✅\n📅 {st['label']}")
            return

        data = load(MENU_FILE)
        menu = data[chat_id][st["menu"]]

        if st["mode"] == "replace":
            node = get_node(menu["buttons"], st["path"])
            for k in ("type", "content", "caption"):
                node.pop(k, None)
            node["items"] = st["items"]
            save(MENU_FILE, data)
            context.chat_data.pop("multi", None)
            await q.edit_message_text("تم تغيير المحتوى بنجاح ✅")
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
    if st["step"] != "choose":
        await q.answer()
        return
    await q.answer()

    if act == "done":
        context.chat_data.pop("multi", None)
        await q.edit_message_text("تم حفظ الرد المتعدد بنجاح ✅")
        return
    if act == "sub":
        st["parent"] = st["last"]
    elif act == "up":
        st["parent"] = st["parent"][:-1]
    st["step"] = "name"

    data = load(MENU_FILE)
    menu = data[chat_id][st["menu"]]
    await q.edit_message_text(f"📍 {breadcrumb(menu, st['parent'])}\nاكتب اسم الزر 🔘")


# ---------------- تصفح القائمة ----------------
async def on_nav(update: Update, context: ContextTypes.DEFAULT_TYPE):
    q = update.callback_query
    parts = q.data.split("|")
    mid, path, idx = parts[1], parse_path(parts[2]), len(parts) > 3
    data = load(MENU_FILE)
    menu = data.get(data_id(q.message.chat, q.from_user.id), {}).get(mid)
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
            if len(items) == 1 and items[0]["type"] == "text":
                text = items[0]["content"]
            else:
                await send_items(q.message, items)
                text = node["name"]
            await q.edit_message_text(text, reply_markup=keyboard(menu, mid, path, idx))
        else:
            await send_items(q.message, items)
    except BadRequest:
        pass


# ---------------- التفعيل والإذاعة ----------------
class Target:
    """يجعل send_items تعمل مع الإرسال لمستخدم في الخاص."""

    def __init__(self, bot, chat_id):
        self.bot, self.chat_id = bot, chat_id

    async def reply_text(self, text):
        return await self.bot.send_message(self.chat_id, text)

    async def reply_photo(self, photo, caption=None):
        return await self.bot.send_photo(self.chat_id, photo, caption=caption)

    async def reply_document(self, doc, caption=None):
        return await self.bot.send_document(self.chat_id, doc, caption=caption)

    async def reply_media_group(self, media):
        return await self.bot.send_media_group(self.chat_id, media)


def reg_link(bot, group_id):
    return f"https://t.me/{bot.username}?start=reg_{group_id}"


async def delete_later(message, seconds):
    await asyncio.sleep(seconds)
    try:
        await message.delete()
    except Exception:
        pass


async def broadcast(bot, group_id, items):
    users = load(USERS_FILE)
    reg = users.get(group_id, {})
    ok = fail = 0
    for uid in list(reg):
        try:
            await send_items(Target(bot, int(uid)), items)
            ok += 1
        except Forbidden:       # حظر البوت: نحذفه من المفعّلين
            reg.pop(uid, None)
            fail += 1
        except Exception:
            fail += 1
        await asyncio.sleep(0.07)
    save(USERS_FILE, users)
    return ok, fail


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
    except Exception:
        pass
    try:
        photos = await bot.get_user_profile_photos(OWNER_ID, limit=1)
        if photos.total_count:
            info["photo"] = photos.photos[0][-1].file_id
    except Exception:
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
    text = build_welcome(first_name, owner)
    markup = Markup([
        [Btn("👑 تواصل مع المالك", url=f"https://t.me/{owner['username']}")],
        [Btn("📖 عرض الأوامر", callback_data="help")],
    ])
    if owner["photo"]:
        if len(text) <= 1024:
            await bot.send_photo(chat_id, owner["photo"], caption=text, reply_markup=markup)
            return
        await bot.send_photo(chat_id, owner["photo"])
    await bot.send_message(chat_id, text, reply_markup=markup)


async def on_start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    msg, user = update.message, update.effective_user
    payload = context.args[0] if context.args else ""
    if payload.startswith("reg_"):
        gid = payload[4:]
        try:
            member = await context.bot.get_chat_member(int(gid), user.id)
            is_member = member.status not in (ChatMemberStatus.LEFT, ChatMemberStatus.BANNED)
        except Exception:
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


async def on_new_members(update: Update, context: ContextTypes.DEFAULT_TYPE):
    msg, chat = update.message, update.effective_chat
    remember_group(chat)
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
    asyncio.create_task(delete_later(note, 600))


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


async def enforce_registration(update: Update, context: ContextTypes.DEFAULT_TYPE) -> bool:
    """يحذف رسالة غير المفعّل إذا كان الإجبار شغالاً. يرجع True إذا حُذفت."""
    chat, msg, user = update.effective_chat, update.message, update.effective_user
    cid = str(chat.id)
    if not load(SETTINGS_FILE).get(cid, {}).get("force"):
        return False
    if user.is_bot or msg.sender_chat:
        return False
    if str(user.id) in load(USERS_FILE).get(cid, {}):
        return False
    if await is_admin(chat, user.id):
        return False
    try:
        await msg.delete()
    except Exception:
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
        asyncio.create_task(delete_later(note, 30))
    return True


# ---------------- التحاضير ----------------
WEEKDAYS = ["الاثنين", "الثلاثاء", "الأربعاء", "الخميس", "الجمعة", "السبت", "الأحد"]
DAY_WORDS = {"اليوم": 0, "امس": -1, "باجر": 1, "غدا": 1, "بكره": 1, "بكرا": 1}
HW_WORDS = "تحاضير|تحضير|واجبات|واجب"
HW_VIEW = re.compile(rf"^({HW_WORDS})\s+(\S+)$")
HW_ADD = re.compile(rf"^اضف\s+({HW_WORDS})\s+(\S+)$")
HW_DEL = re.compile(rf"^حذف\s+({HW_WORDS})\s+(\S+)$")
_AR_DIGITS = str.maketrans("٠١٢٣٤٥٦٧٨٩", "0123456789")

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
• اضغط زر «تفعيل حسابي» ثم Start
  لتصلك إشعارات الإدارة في الخاص

ℹ️ اكتب «الاوامر» في أي وقت لعرض هذه القائمة"""

HELP_ADMIN = """🔒 أوامر المشرفين (داخل المجموعة)

📚 التحاضير
• اضف تحضير اليوم — ثم أرسل المحتوى واضغط «تم»
  (يمكنك كتابة: امس / باجر / تاريخ مثل 2026-10-12)
• حذف تحضير اليوم — يحذف تحضير ذلك اليوم

💬 الردود والقوائم
• اضف رد كلمة — رد بسيط: يسألك عن الرد ثم يحفظه
• اضف رد متعدد — قائمة أزرار فيها نصوص وكتب وصور، ويمكن وضع أزرار داخل أزرار
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
    act = parts[1]
    hw = {k: v for k, v in load(HW_FILE).get(data_id(q.message.chat, q.from_user.id), {}).items() if v}
    try:
        if act == "d":
            items = hw.get(parts[2])
            if not items:
                await q.answer("لا يوجد تحضير لهذا اليوم", show_alert=True)
                return
            await q.answer()
            d = date.fromisoformat(parts[2])
            await q.message.reply_text(f"📚 تحاضير {day_label(d)}")
            await send_items(q.message, items)
            return

        if not hw:
            await q.answer("لا توجد تحاضير مسجلة", show_alert=True)
            return
        await q.answer()

        if act == "r":
            await q.edit_message_text("📚 اختر الشهر:", reply_markup=months_markup(hw))
        elif act == "m":
            ym = parts[2]
            with_year = len({k[:4] for k in hw}) > 1
            days = sorted(k for k in hw if k.startswith(ym))
            btns = []
            for k in days:
                d = date.fromisoformat(k)
                btns.append(Btn(f"{WEEKDAYS[d.weekday()]} {d.day}", callback_data=f"hw|d|{k}"))
            rows = [btns[i:i + 3] for i in range(0, len(btns), 3)]
            rows.append([Btn("🔙 رجوع", callback_data="hw|r")])
            await q.edit_message_text(
                f"📅 {month_label(ym, with_year)} — اختر اليوم:", reply_markup=Markup(rows)
            )
    except BadRequest:
        pass


async def on_help_cb(update: Update, context: ContextTypes.DEFAULT_TYPE):
    q = update.callback_query
    await q.answer()
    await q.message.reply_text(await help_for(q.message.chat, q.from_user.id))


async def on_index_cb(update: Update, context: ContextTypes.DEFAULT_TYPE):
    q = update.callback_query
    menus = load(MENU_FILE).get(data_id(q.message.chat, q.from_user.id), {})
    if not menus:
        await q.answer("لا توجد قوائم مضافة", show_alert=True)
        return
    await q.answer()
    try:
        await q.edit_message_text("📚 القوائم المتاحة، اختر ما تريد:", reply_markup=index_markup(menus))
    except BadRequest:
        pass


# ---------------- الرسائل ----------------
async def on_message(update: Update, context: ContextTypes.DEFAULT_TYPE):
    msg = update.message
    if not msg or not update.effective_user:
        return
    user_id = update.effective_user.id
    chat = update.effective_chat
    chat_id = data_id(chat, user_id)
    cd = context.chat_data

    if chat.type != "private":
        remember_group(chat)
        if await enforce_registration(update, context):
            return

    # حالة إضافة / تعديل رد متعدد
    st = cd.get("multi")
    if st and st["user"] == user_id:
        if await handle_multi(update, st):
            if st.get("finished"):
                cd.pop("multi", None)
            return

    text = (msg.text or "").strip()
    if not text:
        return

    data = load(DB_FILE)
    replies = data.setdefault(chat_id, {})
    menus = load(MENU_FILE).get(chat_id, {})

    # انتظار نص الرد البسيط
    pending = cd.get("pending")
    if pending and pending["user"] == user_id:
        replies[pending["word"]] = text
        save(DB_FILE, data)
        cd.pop("pending")
        await msg.reply_text("تم وضع الرد بنجاح ✅")
        return

    # --- أوامر المشرفين ---
    if text == "اضف رد متعدد":
        if not await is_admin(chat, user_id):
            await msg.reply_text("هذا الأمر للمشرفين فقط ⛔")
            return
        cd["multi"] = {"user": user_id, "step": "keyword"}
        await msg.reply_text("اكتب كلمة الرد (مثلاً: اسئلة) ✍️\nللإلغاء اكتب: الغاء")
        return

    if text.startswith("تعديل رد "):
        if not await is_admin(chat, user_id):
            await msg.reply_text("هذا الأمر للمشرفين فقط ⛔")
            return
        word = text[len("تعديل رد "):].strip().strip('"“”')
        for mid, m in menus.items():
            if m["keyword"] == word:
                await msg.reply_text(edit_text(m, []), reply_markup=edit_markup(m, mid, []))
                return
        await msg.reply_text("لا توجد قائمة أزرار بهذه الكلمة (التعديل للردود المتعددة فقط)")
        return

    if text.startswith("اضف رد "):
        if not await is_admin(chat, user_id):
            await msg.reply_text("هذا الأمر للمشرفين فقط ⛔")
            return
        word = text[len("اضف رد "):].strip().strip('"“”')
        if word:
            cd["pending"] = {"user": user_id, "word": word}
            await msg.reply_text("خصص الرد الذي تريده ✍️")
        return

    if text.startswith("حذف رد "):
        if not await is_admin(chat, user_id):
            await msg.reply_text("هذا الأمر للمشرفين فقط ⛔")
            return
        word = text[len("حذف رد "):].strip().strip('"“”')
        found = False
        if replies.pop(word, None) is not None:
            save(DB_FILE, data)
            found = True
        all_menus = load(MENU_FILE)
        for k in [k for k, v in all_menus.get(chat_id, {}).items() if v["keyword"] == word]:
            del all_menus[chat_id][k]
            save(MENU_FILE, all_menus)
            found = True
        await msg.reply_text("تم حذف الرد 🗑️" if found else "لا يوجد رد بهذه الكلمة")
        return

    if text == "الردود":
        lines = [f"• {w}" for w in replies] + [f"• {m['keyword']} (قائمة أزرار)" for m in menus.values()]
        await msg.reply_text("الردود الحالية:\n" + "\n".join(lines) if lines else "لا توجد ردود مضافة بعد")
        return

    ntext = norm(text)

    if ntext == "الاوامر":
        await msg.reply_text(await help_for(chat, user_id))
        return

    if chat.type == "private" and user_id != OWNER_ID and chat_id == str(chat.id):
        await msg.reply_text(
            "فعّل حسابك أولاً حتى أستطيع خدمتك هنا 🔔\n"
            "اضغط زر «تفعيل حسابي» في رسالة المجموعة ثم اضغط Start."
        )
        return

    if ntext == "التحاضير":
        hw = {k: v for k, v in load(HW_FILE).get(chat_id, {}).items() if v}
        if not hw:
            await msg.reply_text("لا توجد تحاضير مسجلة بعد")
        else:
            await msg.reply_text("📚 اختر الشهر:", reply_markup=months_markup(hw))
        return

    if chat.type == "private" and user_id == OWNER_ID and ntext in (
        "المجموعات", "تحديد المجموعه", "المجموعه الحاليه", "المفعلين", "اذاعه"
    ):
        groups = load(GROUPS_FILE)
        target = load(SETTINGS_FILE).get("_owner", {}).get("target")
        title = groups.get(target, {}).get("title", target) if target else None

        if ntext in ("المجموعات", "تحديد المجموعه"):
            if not groups:
                await msg.reply_text(
                    "لا توجد مجموعات معروفة بعد.\n"
                    "أضف البوت لمجموعتك واكتب فيها أي رسالة ليتعرف عليها، ثم أعد المحاولة."
                )
            else:
                rows = [
                    [Btn(("✅ " if gid == target else "📍 ") + g["title"], callback_data=f"tg|{gid}")]
                    for gid, g in groups.items()
                ]
                await msg.reply_text("اختر المجموعة التي تريد التعامل معها:", reply_markup=Markup(rows))
        elif ntext == "المجموعه الحاليه":
            await msg.reply_text(
                f"📍 المجموعة المحددة: {title}" if target else "لم تحدد مجموعة بعد. اكتب: المجموعات"
            )
        elif not target:
            await msg.reply_text("حدد المجموعة أولاً: اكتب «المجموعات»")
        elif ntext == "المفعلين":
            reg = load(USERS_FILE).get(target, {})
            if not reg:
                await msg.reply_text(f"لا يوجد مفعّلون في «{title}» بعد")
            else:
                lines = []
                for i, (uid, u) in enumerate(reg.items(), 1):
                    uname = f" (@{u['username']})" if u.get("username") else ""
                    lines.append(f"{i}. {u.get('name', '—')}{uname} — {uid}")
                try:
                    total = await context.bot.get_chat_member_count(int(target))
                except Exception:
                    total = None
                header = f"👥 المفعّلون في «{title}»: {len(reg)}" + (f" من {total} عضو" if total else "")
                chunks = chunk_lines(lines)
                await msg.reply_text(header + "\n\n" + chunks[0])
                for c in chunks[1:]:
                    await msg.reply_text(c)
        else:  # اذاعه
            cd["multi"] = {"user": user_id, "step": "content", "mode": "bc", "items": [],
                           "status": None, "msg_ids": [], "group": target}
            await msg.reply_text(
                f"📢 الإرسال إلى مفعّلي «{title}»\n"
                "أرسل الرسالة (نص / صور / ملفات)، ويمكنك إرسال أكثر من رسالة، "
                "ثم اضغط 📨 إرسال للجميع.\nللإلغاء اكتب: الغاء"
            )
        return

    if ntext in ("رساله التفعيل", "اجبار التفعيل", "ايقاف الاجبار", "المفعلين", "اذاعه"):
        if chat.type == "private":
            await msg.reply_text("هذا الأمر يُستخدم داخل المجموعة")
            return
        if not await is_admin(chat, user_id):
            await msg.reply_text("هذا الأمر للمشرفين فقط ⛔")
            return

        if ntext == "رساله التفعيل":
            await msg.reply_text(
                "🔔 لتفعيل حسابك واستلام إشعارات الإدارة في الخاص:\n"
                "اضغط الزر ثم اضغط Start في محادثة البوت 👇",
                reply_markup=Markup([[Btn("✅ تفعيل حسابي", url=reg_link(context.bot, chat_id))]]),
            )

        elif ntext in ("اجبار التفعيل", "ايقاف الاجبار"):
            on = ntext == "اجبار التفعيل"
            settings = load(SETTINGS_FILE)
            settings.setdefault(chat_id, {})["force"] = on
            save(SETTINGS_FILE, settings)
            if on:
                await msg.reply_text(
                    "تم تشغيل الإجبار ✅\nسيحذف البوت رسائل غير المفعّلين مع تنبيههم بزر التفعيل.\n"
                    "(يلزم أن يكون البوت مشرفاً بصلاحية حذف الرسائل)"
                )
            else:
                await msg.reply_text("تم إيقاف الإجبار ✅")

        elif ntext == "المفعلين":
            reg_n = len(load(USERS_FILE).get(chat_id, {}))
            force = load(SETTINGS_FILE).get(chat_id, {}).get("force")
            try:
                total = await chat.get_member_count()
            except Exception:
                total = "؟"
            await msg.reply_text(
                f"👥 المفعّلون: {reg_n} من {total} عضو\nالإجبار: {'شغّال ✅' if force else 'متوقف'}"
            )

        else:  # اذاعه
            cd["multi"] = {"user": user_id, "step": "content", "mode": "bc",
                           "items": [], "status": None, "msg_ids": []}
            await msg.reply_text(
                "📢 أرسل الرسالة التي تريد إرسالها لكل المفعّلين (نص / صور / ملفات)،\n"
                "ويمكنك إرسال أكثر من رسالة، ثم اضغط 📨 إرسال للجميع.\nللإلغاء اكتب: الغاء"
            )
        return

    m = HW_ADD.match(ntext)
    if m:
        if not await is_admin(chat, user_id):
            await msg.reply_text("هذا الأمر للمشرفين فقط ⛔")
            return
        d = parse_day(m.group(2))
        if not d:
            await msg.reply_text("اكتب اليوم هكذا: اليوم / امس / باجر / 2026-10-12")
            return
        cd["multi"] = {"user": user_id, "step": "content", "mode": "hw", "date": d.isoformat(),
                       "label": day_label(d), "items": [], "status": None}
        await msg.reply_text(
            f"📅 تحضير {day_label(d)}\n"
            "أرسل المحتوى (رسائل / ملفات / صور) ويمكنك إرسال أكثر من واحد، "
            "ثم اضغط ✅ تم.\nللإلغاء اكتب: الغاء"
        )
        return

    m = HW_DEL.match(ntext)
    if m:
        if not await is_admin(chat, user_id):
            await msg.reply_text("هذا الأمر للمشرفين فقط ⛔")
            return
        d = parse_day(m.group(2))
        if not d:
            await msg.reply_text("اكتب اليوم هكذا: اليوم / امس / باجر / 2026-10-12")
            return
        hw = load(HW_FILE)
        if hw.get(chat_id, {}).pop(d.isoformat(), None) is not None:
            save(HW_FILE, hw)
            await msg.reply_text(f"تم حذف تحضير {day_label(d)} 🗑️")
        else:
            await msg.reply_text("لا يوجد تحضير مسجّل بهذا اليوم")
        return

    m = HW_VIEW.match(ntext)
    if m:
        d = parse_day(m.group(2))
        if d:
            await show_hw(msg, chat_id, d)
            return

    if text == "الازرار":
        if not menus:
            await msg.reply_text("لا توجد أزرار مضافة بعد")
        else:
            await msg.reply_text("📚 القوائم المتاحة، اختر ما تريد:", reply_markup=index_markup(menus))
        return

    # --- الرد التلقائي ---
    for mid, m in menus.items():
        if m["keyword"] == text:
            await msg.reply_text("📋 اختر من القائمة:", reply_markup=keyboard(m, mid, []))
            return
    if text in replies:
        await msg.reply_text(replies[text])


def main():
    app = Application.builder().token(TOKEN).build()
    app.add_handler(CommandHandler("start", on_start, filters=filters.ChatType.PRIVATE))
    app.add_handler(MessageHandler(filters.StatusUpdate.NEW_CHAT_MEMBERS, on_new_members))
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
            | filters.Sticker.ALL | filters.VOICE | filters.VIDEO | filters.AUDIO | filters.ANIMATION,
            on_message,
        )
    )
    app.run_polling()


if __name__ == "__main__":
    main()
