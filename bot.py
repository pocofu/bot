# -*- coding: utf-8 -*-
# pip install "python-telegram-bot>=20"
import json
import os

from telegram import InlineKeyboardButton as Btn
from telegram import InlineKeyboardMarkup as Markup
from telegram import InputMediaDocument, InputMediaPhoto, Update
from telegram.constants import ChatMemberStatus
from telegram.error import BadRequest
from telegram.ext import (
    Application,
    CallbackQueryHandler,
    ContextTypes,
    MessageHandler,
    filters,
)

# على Railway: ضع التوكن في Variables باسم BOT_TOKEN، ومسار الـ Volume باسم DATA_DIR
TOKEN = os.environ.get("BOT_TOKEN", "ضع_التوكن_هنا")
DATA_DIR = os.environ.get("DATA_DIR", ".")
DB_FILE = os.path.join(DATA_DIR, "replies.json")    # الردود البسيطة
MENU_FILE = os.path.join(DATA_DIR, "menus.json")    # الردود المتعددة (الأزرار)


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
        return True
    member = await chat.get_member(user_id)
    return member.status in (ChatMemberStatus.ADMINISTRATOR, ChatMemberStatus.OWNER)


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


def keyboard(menu, mid, path):
    kids = children(menu, path)
    rows = [
        [Btn(n["name"], callback_data=f"n|{mid}|{join_path(path + [i])}")]
        for i, n in enumerate(kids)
    ]
    if path:
        rows.append([Btn("🔙 رجوع", callback_data=f"n|{mid}|{join_path(path[:-1])}")])
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
    menu = data.get(str(chat.id), {}).get(mid)
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
    chat_id = str(update.effective_chat.id)
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
        label = f"تمت إضافة {len(st['items'])} ✅\nأرسل المزيد (كتاب / ملف / صورة / نص) أو اضغط تم"
        markup = Markup([[Btn("✅ تم، حفظ الزر", callback_data="cr|fin")]])
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
    chat_id = str(q.message.chat.id)

    # ---- حفظ محتوى الزر ----
    if act == "fin":
        if st["step"] != "content":
            await q.answer()
            return
        if not st["items"]:
            await q.answer("أرسل محتوى أولاً", show_alert=True)
            return
        await q.answer()
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
    _, mid, p = q.data.split("|")
    path = parse_path(p)
    data = load(MENU_FILE)
    menu = data.get(str(q.message.chat.id), {}).get(mid)
    if not menu:
        await q.answer("هذه القائمة لم تعد موجودة", show_alert=True)
        return
    await q.answer()

    try:
        if not path:
            await q.edit_message_text("📋 اختر من القائمة:", reply_markup=keyboard(menu, mid, []))
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
            await q.edit_message_text(text, reply_markup=keyboard(menu, mid, path))
        else:
            await send_items(q.message, items)
    except BadRequest:
        pass


# ---------------- الرسائل ----------------
async def on_message(update: Update, context: ContextTypes.DEFAULT_TYPE):
    msg = update.message
    if not msg or not update.effective_user:
        return
    user_id = update.effective_user.id
    chat = update.effective_chat
    chat_id = str(chat.id)
    cd = context.chat_data

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

    # --- الرد التلقائي ---
    for mid, m in menus.items():
        if m["keyword"] == text:
            await msg.reply_text("📋 اختر من القائمة:", reply_markup=keyboard(m, mid, []))
            return
    if text in replies:
        await msg.reply_text(replies[text])


def main():
    app = Application.builder().token(TOKEN).build()
    app.add_handler(CallbackQueryHandler(on_create_cb, pattern=r"^cr\|"))
    app.add_handler(CallbackQueryHandler(on_edit_cb, pattern=r"^ed\|"))
    app.add_handler(CallbackQueryHandler(on_nav, pattern=r"^n\|"))
    app.add_handler(
        MessageHandler(
            (filters.TEXT & ~filters.COMMAND) | filters.PHOTO | filters.Document.ALL,
            on_message,
        )
    )
    app.run_polling()


if __name__ == "__main__":
    main()
