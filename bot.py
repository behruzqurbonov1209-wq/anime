import logging
import os
import asyncio
import threading
from http.server import HTTPServer, BaseHTTPRequestHandler
import json
from telegram import Update, InlineKeyboardButton, InlineKeyboardMarkup
from telegram.ext import (
    Application, CommandHandler, CallbackQueryHandler,
    MessageHandler, filters, ContextTypes, ConversationHandler
)
from database import db

logging.basicConfig(
    format='%(asctime)s - %(name)s - %(levelname)s - %(message)s',
    level=logging.INFO
)
logger = logging.getLogger(__name__)

BOT_TOKEN = os.getenv("BOT_TOKEN", "8897498710:AAEnb8SdQPv-09-F14riBjAqhjfVZ70wURw")
ADMIN_IDS = [int(x) for x in os.getenv("ADMIN_IDS", "7356097969").split(",")]

# ── Conversation states ────────────────────────────────────────────────
(
    ADD_CAT, ADD_CODE, ADD_TITLE, ADD_DESC,
    ADD_EP_NUM, ADD_EP_FILE,
    ADDEP_CAT, ADDEP_CODE, ADDEP_FILE,
    DEL_CAT, DEL_CODE,
    BROADCAST,
    ADMIN_MSG,
    ADD_CHANNEL,
    ADD_ADMIN,
) = range(15)

CATEGORY_NAMES = {
    "anime": "🎌 Anime",
    "drama": "🎭 Drama",
    "kino":  "🎬 Kino"
}

def is_admin(user_id: int) -> bool:
    """Super admin (.env) yoki DB dagi admin"""
    return user_id in ADMIN_IDS or db.is_admin(user_id)


def is_super_admin(user_id: int) -> bool:
    """Faqat .env dagi super admin — adminlarni boshqara oladi"""
    return user_id in ADMIN_IDS


# ══════════════════════════════════════════════════════════
#  OBUNA TEKSHIRUVI
# ══════════════════════════════════════════════════════════

async def check_subscription(bot, user_id: int) -> tuple[bool, list]:
    """Foydalanuvchi barcha kanallarga obuna bo'lganmi?"""
    channels = db.get_channels()
    if not channels:
        return True, []

    not_subbed = []
    for ch in channels:
        try:
            member = await bot.get_chat_member(ch["chat_id"], user_id)
            if member.status in ("left", "kicked"):
                not_subbed.append(ch)
        except Exception as e:
            err = str(e).lower()
            # Bot kanalga kirish huquqi yo'q bo'lsa — tekshirmasdan o'tkazib yuboramiz
            if "not enough rights" in err or "bot is not a member" in err or "chat not found" in err:
                logger.warning(f"Kanal {ch['chat_id']} tekshirib bo'lmadi: {e}")
                continue
            # Boshqa xatolikda — obuna bo'lmagan deb hisoblaymiz
            not_subbed.append(ch)

    return len(not_subbed) == 0, not_subbed


async def subscription_wall(update: Update, context: ContextTypes.DEFAULT_TYPE, not_subbed: list):
    """Obuna bo'lmagan foydalanuvchiga obuna tugmalarini ko'rsatish"""
    keyboard = []
    for ch in not_subbed:
        label = ch.get("title") or ch.get("username") or ch["chat_id"]
        username = ch.get("username", "")
        if username:
            url = f"https://t.me/{username.lstrip('@')}"
        else:
            # Shaxsiy guruh uchun invite link
            url = f"https://t.me/c/{str(ch['chat_id']).replace('-100', '')}"
        keyboard.append([InlineKeyboardButton(f"📢 {label}", url=url)])

    keyboard.append([InlineKeyboardButton("✅ Obuna bo'ldim!", callback_data="check_sub")])

    text = (
        "⚠️ <b>Botdan foydalanish uchun quyidagi kanallarga obuna bo'ling:</b>\n\n"
        + "\n".join(f"• {ch.get('title') or ch.get('username') or ch['chat_id']}" for ch in not_subbed)
        + "\n\nObuna bo'lgach <b>✅ Obuna bo'ldim!</b> tugmasini bosing."
    )

    if update.message:
        await update.message.reply_text(text, reply_markup=InlineKeyboardMarkup(keyboard), parse_mode="HTML")
    elif update.callback_query:
        await context.bot.send_message(
            chat_id=update.callback_query.from_user.id,
            text=text,
            reply_markup=InlineKeyboardMarkup(keyboard),
            parse_mode="HTML"
        )


async def check_sub_callback(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """'Obuna bo'ldim' tugmasi"""
    query = update.callback_query
    await query.answer("Tekshirilmoqda...", show_alert=False)
    ok, not_subbed = await check_subscription(context.bot, query.from_user.id)
    if ok:
        # Obuna bo'lgan — start menyusiga o'tamiz
        try:
            await query.delete_message()
        except Exception:
            pass
        await start(update, context)
    else:
        # Hali obuna bo'lmagan — yangi xabar yuboramiz (edit emas)
        try:
            await query.delete_message()
        except Exception:
            pass
        await subscription_wall(update, context, not_subbed)

# ══════════════════════════════════════════════════════════
#  FOYDALANUVCHI
# ══════════════════════════════════════════════════════════

async def start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    # Faqat shaxsiy chatda ishlaydi — guruh/kanalda e'tiborsiz
    if update.effective_chat.type != "private":
        return

    user = update.effective_user
    db.add_user(user.id, user.username or "", user.full_name or "")

    # Admin uchun obuna tekshirmaymiz
    if not is_admin(user.id):
        ok, not_subbed = await check_subscription(context.bot, user.id)
        if not ok:
            await subscription_wall(update, context, not_subbed)
            return

    keyboard = [
        [
            InlineKeyboardButton("🎌 Anime",  callback_data="cat_anime"),
            InlineKeyboardButton("🎭 Drama",  callback_data="cat_drama"),
        ],
        [InlineKeyboardButton("🎬 Kino", callback_data="cat_kino")],
    ]
    if is_admin(user.id):
        keyboard.append([InlineKeyboardButton("⚙️ Admin Panel", callback_data="admin_panel")])

    text = (
        f"Salom, {user.first_name}! 👋\n\n"
        "Kategoriya tanlang:\n\n"
        "🎌 <b>Anime</b> — Anime seriyalar\n"
        "🎭 <b>Drama</b> — Drama seriyalar\n"
        "🎬 <b>Kino</b> — Tarjima kinolar"
    )
    if update.message:
        await update.message.reply_text(text, reply_markup=InlineKeyboardMarkup(keyboard), parse_mode="HTML")
    else:
        await update.callback_query.edit_message_text(text, reply_markup=InlineKeyboardMarkup(keyboard), parse_mode="HTML")


async def show_category(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()
    category = query.data.replace("cat_", "")
    context.user_data["category"] = category

    items = db.get_all_items(category)
    cat_name = CATEGORY_NAMES.get(category, category)

    if not items:
        text = f"{cat_name}\n\n📭 Hozircha hech narsa yo'q."
    else:
        lines = [f"{cat_name} ro'yxati:\n"]
        for item in items:
            ep_count = item.get("episode_count", 0)
            ep_info = f"({ep_count} qism)" if ep_count else "(qism yo'q)"
            lines.append(f"🔹 <code>{item['code']}</code> — {item['title']} {ep_info}")
        lines.append(f"\n📩 Kodni yozing, masalan: <code>{items[0]['code']}</code>")
        text = "\n".join(lines)

    keyboard = [[InlineKeyboardButton("🔙 Orqaga", callback_data="back_main")]]
    await query.edit_message_text(text, reply_markup=InlineKeyboardMarkup(keyboard), parse_mode="HTML")


async def back_main(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()
    await start(update, context)


async def handle_code(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Foydalanuvchi kod yozganda — qismlar tugmalarini ko'rsatadi"""
    # Faqat shaxsiy chatda ishlaydi
    if update.effective_chat.type != "private":
        return

    user = update.effective_user
    db.add_user(user.id, user.username or "", user.full_name or "")

    # Admin kanal kutayotgan bo'lsa
    if is_admin(user.id) and context.user_data.get("waiting_channel"):
        return await ch_add_get_id(update, context)

    # Admin qism kodi kutayotgan bo'lsa
    if is_admin(user.id) and context.user_data.get("waiting_ep_code"):
        return await addep_get_code(update, context)

    # Admin yangi admin ID kutayotgan bo'lsa
    if is_admin(user.id) and context.user_data.get("waiting_new_admin"):
        return await adm_add_get_id(update, context)

    # Obuna tekshiruvi
    if not is_admin(user.id):
        ok, not_subbed = await check_subscription(context.bot, user.id)
        if not ok:
            await subscription_wall(update, context, not_subbed)
            return

    code = update.message.text.strip().upper()

    # Kategoriyadan qidirish
    category = context.user_data.get("category")
    found = None
    found_cat = None

    if category:
        found = db.get_item(category, code)
        found_cat = category

    if not found:
        for cat in ["anime", "drama", "kino"]:
            item = db.get_item(cat, code)
            if item:
                found = item
                found_cat = cat
                break

    if not found:
        await update.message.reply_text(
            f"❌ <b>{code}</b> kodi topilmadi.\n\n"
            "To'g'ri kod kiriting yoki /start bosing.",
            parse_mode="HTML"
        )
        return

    episodes = db.get_episodes(found_cat, code)
    cat_name = CATEGORY_NAMES.get(found_cat, found_cat)

    if not episodes:
        await update.message.reply_text(
            f"{cat_name} | <b>{found['title']}</b>\n\n"
            "📭 Hali qismlar qo'shilmagan.",
            parse_mode="HTML"
        )
        return

    # Qismlar tugmalari — 3 tadan qator
    ep_buttons = []
    row = []
    for ep in episodes:
        ep_title = ep.get("title") or f"{ep['episode_num']}-qism"
        row.append(InlineKeyboardButton(
            ep_title,
            callback_data=f"ep_{found_cat}_{code}_{ep['episode_num']}"
        ))
        if len(row) == 3:
            ep_buttons.append(row)
            row = []
    if row:
        ep_buttons.append(row)

    ep_buttons.append([InlineKeyboardButton("🔙 Orqaga", callback_data=f"cat_{found_cat}")])

    text = (
        f"{cat_name}\n\n"
        f"🎬 <b>{found['title']}</b>\n"
    )
    if found.get("description"):
        text += f"📝 {found['description']}\n"
    text += f"\n📺 {len(episodes)} ta qism mavjud. Birini tanlang:"

    await update.message.reply_text(
        text,
        reply_markup=InlineKeyboardMarkup(ep_buttons),
        parse_mode="HTML"
    )


async def send_episode(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Tugma bosilganda qismni yuboradi"""
    query = update.callback_query
    await query.answer()

    # callback: ep_anime_A001_3
    parts = query.data.split("_", 3)
    # parts = ["ep", category, code, episode_num]
    _, category, code, ep_num_str = parts
    episode_num = int(ep_num_str)

    episode = db.get_episode(category, code, episode_num)
    item = db.get_item(category, code)

    if not episode or not item:
        await query.answer("❌ Qism topilmadi!", show_alert=True)
        return

    cat_name = CATEGORY_NAMES.get(category, category)
    ep_label = episode.get("title") or f"{episode_num}-qism"
    caption = (
        f"{cat_name} | <b>{item['title']}</b>\n"
        f"📺 <b>{ep_label}</b>"
    )

    file_id   = episode["file_id"]
    file_type = episode["file_type"]

    try:
        if file_type == "video":
            await query.message.reply_video(video=file_id, caption=caption, parse_mode="HTML")
        elif file_type == "document":
            await query.message.reply_document(document=file_id, caption=caption, parse_mode="HTML")
        elif file_type == "photo":
            await query.message.reply_photo(photo=file_id, caption=caption, parse_mode="HTML")
        else:
            await query.message.reply_text(caption, parse_mode="HTML")
    except Exception as e:
        logger.error(f"Episode send error: {e}")
        await query.answer("⚠️ Faylni yuborishda xatolik!", show_alert=True)


# ══════════════════════════════════════════════════════════
#  ADMIN PANEL
# ══════════════════════════════════════════════════════════

async def admin_panel(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()
    if not is_admin(query.from_user.id):
        await query.answer("❌ Ruxsat yo'q!", show_alert=True)
        return

    stats = db.get_stats()
    text = (
        "⚙️ <b>Admin Panel</b>\n\n"
        f"👥 Foydalanuvchilar: <b>{stats['users']}</b>\n"
        f"🎌 Anime: <b>{stats['anime']}</b> ta serial\n"
        f"🎭 Drama: <b>{stats['drama']}</b> ta serial\n"
        f"🎬 Kino: <b>{stats['kino']}</b> ta\n"
        f"📺 Jami qismlar: <b>{stats.get('episodes', 0)}</b> ta\n"
    )
    keyboard = [
        [
            InlineKeyboardButton("➕ Serial qo'sh",  callback_data="admin_add"),
            InlineKeyboardButton("📺 Qism qo'sh",   callback_data="admin_addep"),
        ],
        [
            InlineKeyboardButton("🗑 O'chirish",     callback_data="admin_delete"),
            InlineKeyboardButton("📊 Statistika",    callback_data="admin_stats"),
        ],
        [
            InlineKeyboardButton("📢 Broadcast",     callback_data="admin_broadcast"),
            InlineKeyboardButton("🔔 Kanallar",      callback_data="admin_channels"),
        ],
    ]
    # Faqat super admin adminlarni boshqara oladi
    if is_super_admin(update.effective_user.id):
        keyboard.append([InlineKeyboardButton("👥 Adminlar",  callback_data="admin_admins")])
    keyboard.append([InlineKeyboardButton("🔙 Orqaga",        callback_data="back_main")])
    await query.edit_message_text(text, reply_markup=InlineKeyboardMarkup(keyboard), parse_mode="HTML")


async def admin_stats(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()
    if not is_admin(query.from_user.id):
        return

    stats = db.get_stats()
    users = db.get_recent_users(10)
    text = (
        "📊 <b>Statistika</b>\n\n"
        f"👥 Foydalanuvchilar: <b>{stats['users']}</b>\n\n"
        f"📦 Kontent:\n"
        f"  🎌 Anime: {stats['anime']} serial\n"
        f"  🎭 Drama: {stats['drama']} serial\n"
        f"  🎬 Kino: {stats['kino']} ta\n"
        f"  📺 Jami qismlar: {stats.get('episodes', 0)} ta\n\n"
        "👤 So'nggi foydalanuvchilar:\n"
    )
    for u in users:
        name = u['full_name'] or u['username'] or f"ID:{u['user_id']}"
        text += f"  • {name}\n"

    keyboard = [[InlineKeyboardButton("🔙 Admin Panel", callback_data="admin_panel")]]
    await query.edit_message_text(text, reply_markup=InlineKeyboardMarkup(keyboard), parse_mode="HTML")


# ── SERIAL QO'SHISH ────────────────────────────────────────────────────

async def admin_add_start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()
    if not is_admin(query.from_user.id):
        return

    keyboard = [
        [
            InlineKeyboardButton("🎌 Anime", callback_data="add_anime"),
            InlineKeyboardButton("🎭 Drama", callback_data="add_drama"),
            InlineKeyboardButton("🎬 Kino",  callback_data="add_kino"),
        ],
        [InlineKeyboardButton("❌ Bekor", callback_data="admin_panel")]
    ]
    await query.edit_message_text(
        "➕ <b>Yangi serial — kategoriya tanlang:</b>",
        reply_markup=InlineKeyboardMarkup(keyboard),
        parse_mode="HTML"
    )
    return ADD_CAT


async def add_select_cat(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()
    context.user_data["add_cat"] = query.data.replace("add_", "")
    await query.edit_message_text(
        "📌 <b>Kod kiriting</b> (masalan: A001, D12):\n<i>Kod unikal bo'lsin</i>",
        parse_mode="HTML"
    )
    return ADD_CODE


async def add_get_code(update: Update, context: ContextTypes.DEFAULT_TYPE):
    code = update.message.text.strip().upper()
    cat  = context.user_data["add_cat"]
    if db.get_item(cat, code):
        await update.message.reply_text(f"⚠️ <code>{code}</code> kodi mavjud. Boshqa kod kiriting:", parse_mode="HTML")
        return ADD_CODE
    context.user_data["add_code"] = code
    await update.message.reply_text("📝 <b>Sarlavha kiriting:</b>", parse_mode="HTML")
    return ADD_TITLE


async def add_get_title(update: Update, context: ContextTypes.DEFAULT_TYPE):
    context.user_data["add_title"] = update.message.text.strip()
    await update.message.reply_text(
        "📄 <b>Tavsif kiriting</b> (ixtiyoriy — o'tkazish: /skip):",
        parse_mode="HTML"
    )
    return ADD_DESC


async def add_get_desc(update: Update, context: ContextTypes.DEFAULT_TYPE):
    text = update.message.text.strip()
    context.user_data["add_desc"] = "" if text == "/skip" else text

    cat  = context.user_data["add_cat"]
    code = context.user_data["add_code"]
    db.add_item(cat, code, context.user_data["add_title"], context.user_data["add_desc"])

    cat_name = CATEGORY_NAMES.get(cat, cat)
    await update.message.reply_text(
        f"✅ <b>Serial qo'shildi!</b>\n\n"
        f"📁 {cat_name}\n"
        f"🔑 Kod: <code>{code}</code>\n"
        f"🎬 Sarlavha: {context.user_data['add_title']}\n\n"
        f"Endi <b>qismlarni qo'shishingiz</b> mumkin.\n"
        f"Admin panel → 📺 Qism qo'sh",
        parse_mode="HTML"
    )
    for k in ["add_cat","add_code","add_title","add_desc"]:
        context.user_data.pop(k, None)
    return ConversationHandler.END


# ── QISM QO'SHISH ──────────────────────────────────────────────────────

async def admin_addep_start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()
    if not is_admin(query.from_user.id):
        return

    keyboard = [
        [
            InlineKeyboardButton("🎌 Anime", callback_data="addep_anime"),
            InlineKeyboardButton("🎭 Drama", callback_data="addep_drama"),
            InlineKeyboardButton("🎬 Kino",  callback_data="addep_kino"),
        ],
        [InlineKeyboardButton("❌ Bekor", callback_data="admin_panel")]
    ]
    await query.edit_message_text(
        "📺 <b>Qism qo'shish — kategoriya tanlang:</b>",
        reply_markup=InlineKeyboardMarkup(keyboard),
        parse_mode="HTML"
    )
    return ADDEP_CAT


async def addep_select_cat(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()
    cat = query.data.replace("addep_", "")
    context.user_data["ep_cat"]          = cat
    context.user_data["waiting_ep_code"] = True  # handle_code ushlab olmasin

    items = db.get_all_items(cat)
    cat_name = CATEGORY_NAMES.get(cat, cat)

    if not items:
        await query.edit_message_text(f"{cat_name} bo'sh. Avval serial qo'shing.")
        return ConversationHandler.END

    lines = [f"📺 {cat_name} — qism qo'shish\n\nSeriallar:\n"]
    for item in items:
        ep_count = item.get("episode_count", 0)
        lines.append(f"  🔹 <code>{item['code']}</code> — {item['title']} ({ep_count} qism)")
    lines.append("\n✏️ <b>Serial kodini yozing:</b>")
    await query.edit_message_text("\n".join(lines), parse_mode="HTML")
    return ADDEP_CODE


async def addep_get_code(update: Update, context: ContextTypes.DEFAULT_TYPE):
    context.user_data.pop("waiting_ep_code", None)
    code = update.message.text.strip().upper()
    cat  = context.user_data.get("ep_cat")
    if not cat:
        return ConversationHandler.END
    item = db.get_item(cat, code)

    if not item:
        await update.message.reply_text(
            f"❌ <code>{code}</code> topilmadi. Qaytadan yozing:",
            parse_mode="HTML"
        )
        return ADDEP_CODE

    context.user_data["ep_code"] = code
    next_num = db.get_next_episode_num(cat, code)

    await update.message.reply_text(
        f"✅ <b>{item['title']}</b>\n\n"
        f"📺 Keyingi qism raqami: <b>{next_num}</b>\n\n"
        f"Endi <b>{next_num}-qism faylini yuboring</b> (video yoki hujjat):",
        parse_mode="HTML"
    )
    return ADDEP_FILE


async def addep_get_file(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """
    Admin video/hujjat yuborganda — bitta ham, ko'p ham — avtomatik ketma-ket qism qilib saqlaydi.
    Telegram media_group_id orqali bir vaqtda yuborilgan fayllarni aniqlaydi.
    """
    cat  = context.user_data.get("ep_cat")
    code = context.user_data.get("ep_code")
    if not cat or not code:
        return ADDEP_FILE

    # Fayl turini aniqlash
    if update.message.video:
        file_id   = update.message.video.file_id
        file_type = "video"
    elif update.message.document:
        file_id   = update.message.document.file_id
        file_type = "document"
    elif update.message.photo:
        file_id   = update.message.photo[-1].file_id
        file_type = "photo"
    else:
        await update.message.reply_text("❌ Video, hujjat yoki rasm yuboring.")
        return ADDEP_FILE

    # Ko'p fayl bir vaqtda yuborilganmi? (media group)
    media_group_id = update.message.media_group_id

    if media_group_id:
        # Media group — fayllarni vaqtinchalik buferga yig'amiz
        buf_key = f"mg_{media_group_id}"
        if buf_key not in context.user_data:
            context.user_data[buf_key] = []
            # Barcha fayllar kelganidan keyin saqlash uchun job qo'shamiz
            context.job_queue.run_once(
                _flush_media_group,
                when=2.5,          # 2.5 soniya kutamiz (hammasi kelsin)
                name=buf_key,
                data={
                    "cat": cat, "code": code,
                    "chat_id": update.effective_chat.id,
                    "buf_key": buf_key,
                }
            )
        context.user_data[buf_key].append((file_id, file_type))
        return ADDEP_FILE  # hali job ishlaguncha state da qolamiz

    # Bitta fayl — darhol saqlash
    next_num = db.get_next_episode_num(cat, code)
    db.add_episode(cat, code, next_num, file_id, file_type)
    item     = db.get_item(cat, code)
    cat_name = CATEGORY_NAMES.get(cat, cat)

    keyboard = [
        [
            InlineKeyboardButton(f"➕ {next_num+1}-qism qo'sh", callback_data=f"cont_ep_{cat}_{code}"),
            InlineKeyboardButton("✅ Tugat", callback_data="admin_panel"),
        ]
    ]
    await update.message.reply_text(
        f"✅ <b>{next_num}-qism qo'shildi!</b>\n\n"
        f"📁 {cat_name} | {item['title']}\n"
        f"📺 Jami: {next_num} ta qism\n\n"
        f"Keyingi qismni yuboring yoki tugating:",
        reply_markup=InlineKeyboardMarkup(keyboard),
        parse_mode="HTML"
    )
    # State da qolamiz — admin davom ettirishni tanlaydi
    return ADDEP_FILE


async def _flush_media_group(context: ContextTypes.DEFAULT_TYPE):
    """Job: media group to'liq kelgandan keyin barcha qismlarni ketma-ket saqlaydi"""
    job  = context.job
    data = job.data
    cat      = data["cat"]
    code     = data["code"]
    chat_id  = data["chat_id"]
    buf_key  = data["buf_key"]

    files = context.user_data.get(buf_key, [])
    if not files:
        return

    # Ketma-ket qism raqami bilan saqlash
    saved = []
    for file_id, file_type in files:
        next_num = db.get_next_episode_num(cat, code)
        db.add_episode(cat, code, next_num, file_id, file_type)
        saved.append(next_num)

    # Buferni tozalash
    context.user_data.pop(buf_key, None)

    item     = db.get_item(cat, code)
    cat_name = CATEGORY_NAMES.get(cat, cat)
    total    = db.get_next_episode_num(cat, code) - 1

    ep_range = f"{saved[0]}-{saved[-1]}" if len(saved) > 1 else str(saved[0])

    keyboard = [
        [
            InlineKeyboardButton(f"➕ {total+1}-qism qo'sh", callback_data=f"cont_ep_{cat}_{code}"),
            InlineKeyboardButton("✅ Tugat", callback_data="admin_panel"),
        ]
    ]
    await context.bot.send_message(
        chat_id,
        f"✅ <b>{len(saved)} ta qism qo'shildi!</b>\n\n"
        f"📁 {cat_name} | {item['title']}\n"
        f"📺 {ep_range}-qismlar saqlandi\n"
        f"📊 Jami: {total} ta qism\n\n"
        f"Davom ettirasizmi?",
        reply_markup=InlineKeyboardMarkup(keyboard),
        parse_mode="HTML"
    )


async def continue_episode(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """'Keyingi qism qo'sh' tugmasi"""
    query = update.callback_query
    await query.answer()
    # cont_ep_anime_A001
    parts = query.data.split("_", 3)
    cat  = parts[2]
    code = parts[3]
    context.user_data["ep_cat"]  = cat
    context.user_data["ep_code"] = code
    next_num = db.get_next_episode_num(cat, code)
    item = db.get_item(cat, code)

    await query.edit_message_text(
        f"📺 <b>{item['title']}</b>\n\n"
        f"{next_num}-qism faylini yuboring:",
        parse_mode="HTML"
    )
    return ADDEP_FILE


# ── O'CHIRISH ──────────────────────────────────────────────────────────

async def admin_delete_start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()
    if not is_admin(query.from_user.id):
        return

    keyboard = [
        [
            InlineKeyboardButton("🎌 Anime", callback_data="del_anime"),
            InlineKeyboardButton("🎭 Drama", callback_data="del_drama"),
            InlineKeyboardButton("🎬 Kino",  callback_data="del_kino"),
        ],
        [InlineKeyboardButton("❌ Bekor", callback_data="admin_panel")]
    ]
    await query.edit_message_text(
        "🗑 <b>O'chirish — kategoriya tanlang:</b>",
        reply_markup=InlineKeyboardMarkup(keyboard),
        parse_mode="HTML"
    )
    return DEL_CAT


async def delete_select_cat(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()
    cat = query.data.replace("del_", "")
    context.user_data["del_cat"] = cat

    items = db.get_all_items(cat)
    cat_name = CATEGORY_NAMES.get(cat, cat)

    if not items:
        await query.edit_message_text(f"{cat_name} bo'sh.")
        return ConversationHandler.END

    lines = [f"🗑 {cat_name}\n\nKodlar:\n"]
    for item in items:
        ep_count = item.get("episode_count", 0)
        lines.append(f"  🔹 <code>{item['code']}</code> — {item['title']} ({ep_count} qism)")
    lines.append("\n✏️ <b>O'chirmoqchi bo'lgan kodni yozing:</b>")
    await query.edit_message_text("\n".join(lines), parse_mode="HTML")
    return DEL_CODE


async def delete_confirm(update: Update, context: ContextTypes.DEFAULT_TYPE):
    code = update.message.text.strip().upper()
    cat  = context.user_data["del_cat"]
    item = db.get_item(cat, code)

    if not item:
        await update.message.reply_text(f"❌ <code>{code}</code> topilmadi.", parse_mode="HTML")
        return DEL_CODE

    db.delete_item(cat, code)
    cat_name = CATEGORY_NAMES.get(cat, cat)
    await update.message.reply_text(
        f"✅ <b>O'chirildi!</b>\n\n"
        f"📁 {cat_name} / <code>{code}</code> — {item['title']}\n"
        f"(Barcha qismlari ham o'chirildi)",
        parse_mode="HTML"
    )
    return ConversationHandler.END


# ── BROADCAST ─────────────────────────────────────────────────────────

async def admin_broadcast_start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()
    if not is_admin(query.from_user.id):
        return

    stats = db.get_stats()
    keyboard = [[InlineKeyboardButton("🔙 Admin Panel", callback_data="admin_panel")]]
    await query.edit_message_text(
        f"📢 <b>Broadcast</b>\n\n"
        f"👥 {stats['users']} ta foydalanuvchiga yuboriladi\n\n"
        "Xabar yozing yoki fayl yuboring.\n"
        "<i>Bekor: /cancel yoki ↙️ Admin Panel</i>",
        reply_markup=InlineKeyboardMarkup(keyboard),
        parse_mode="HTML"
    )
    return BROADCAST


async def do_broadcast(update: Update, context: ContextTypes.DEFAULT_TYPE):
    users  = db.get_all_users()
    sent   = 0
    failed = 0
    status = await update.message.reply_text(f"📤 Yuborilmoqda... 0/{len(users)}")

    for i, user in enumerate(users):
        try:
            uid = user["user_id"]
            if update.message.text:
                await context.bot.send_message(uid, update.message.text, parse_mode="HTML")
            elif update.message.video:
                await context.bot.send_video(uid, update.message.video.file_id,
                                             caption=update.message.caption or "", parse_mode="HTML")
            elif update.message.photo:
                await context.bot.send_photo(uid, update.message.photo[-1].file_id,
                                             caption=update.message.caption or "", parse_mode="HTML")
            elif update.message.document:
                await context.bot.send_document(uid, update.message.document.file_id,
                                                caption=update.message.caption or "", parse_mode="HTML")
            sent += 1
        except Exception:
            failed += 1
        if (i + 1) % 10 == 0:
            try:
                await status.edit_text(f"📤 Yuborilmoqda... {i+1}/{len(users)}")
            except Exception:
                pass

    await status.edit_text(
        f"✅ <b>Broadcast tugadi!</b>\n\n✔️ Yuborildi: {sent}\n❌ Xatolik: {failed}",
        parse_mode="HTML"
    )
    return ConversationHandler.END


async def conv_cancel(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text("❌ Bekor qilindi.")
    return ConversationHandler.END


# ══════════════════════════════════════════════════════════
#  FOYDALANUVCHI → ADMINGA XABAR
# ══════════════════════════════════════════════════════════

async def user_to_admin_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """/admin <xabar> — adminga xabar yuborish"""
    user = update.effective_user

    if not context.args:
        await update.message.reply_text(
            "📩 <b>Adminga xabar yuborish:</b>\n\n"
            "Ishlatish: <code>/admin sizning xabaringiz</code>\n\n"
            "Misol: <code>/admin Naruto 5-qism ishlamayapti</code>",
            parse_mode="HTML"
        )
        return

    msg_text = " ".join(context.args)
    user_info = f"@{user.username}" if user.username else f"{user.full_name} (ID: {user.id})"

    admin_text = (
        f"📩 <b>Foydalanuvchidan xabar:</b>\n\n"
        f"👤 {user_info}\n"
        f"🆔 ID: <code>{user.id}</code>\n\n"
        f"💬 {msg_text}"
    )

    sent = 0
    for admin_id in ADMIN_IDS:
        try:
            await context.bot.send_message(admin_id, admin_text, parse_mode="HTML")
            sent += 1
        except Exception:
            pass

    if sent:
        await update.message.reply_text(
            "✅ <b>Xabaringiz adminga yuborildi!</b>\n\n"
            "Tez orada javob beramiz. 🙏",
            parse_mode="HTML"
        )
    else:
        await update.message.reply_text("⚠️ Xabar yuborishda xatolik yuz berdi.")


# ══════════════════════════════════════════════════════════
#  KANAL/GURUH BOSHQARUVI (ADMIN)
# ══════════════════════════════════════════════════════════

async def admin_channels(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Admin paneldagi kanallar menyusi"""
    query = update.callback_query
    await query.answer()
    if not is_admin(query.from_user.id):
        return

    channels = db.get_channels()
    text = "📢 <b>Obuna kanallari</b>\n\n"

    if channels:
        for i, ch in enumerate(channels, 1):
            label = ch.get("title") or ch.get("username") or ch["chat_id"]
            text += f"{i}. {label} (<code>{ch['chat_id']}</code>)\n"
    else:
        text += "Hozircha kanal qo'shilmagan.\n"

    text += "\n<i>Kanal qo'shish uchun botni kanalga admin qilib, keyin quyida qo'shing.</i>"

    keyboard = [
        [InlineKeyboardButton("➕ Kanal qo'sh", callback_data="ch_add")],
    ]
    if channels:
        keyboard.append([InlineKeyboardButton("🗑 Kanal o'chir", callback_data="ch_del")])
    keyboard.append([InlineKeyboardButton("🔙 Admin Panel", callback_data="admin_panel")])

    await query.edit_message_text(text, reply_markup=InlineKeyboardMarkup(keyboard), parse_mode="HTML")


async def admin_admins(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Adminlar ro'yxati va boshqaruvi"""
    query = update.callback_query
    await query.answer()
    if not is_super_admin(query.from_user.id):
        await query.answer("❌ Sizda bu huquq yo'q!", show_alert=True)
        return

    admins = db.get_admins()
    text = "👥 <b>Adminlar ro'yxati</b>\n\n"
    if admins:
        for i, a in enumerate(admins, 1):
            uname = f"@{a['username']}" if a['username'] else a['full_name']
            text += f"{i}. {uname} (<code>{a['user_id']}</code>)\n"
    else:
        text += "Qo'shimcha admin yo'q.\n"
    text += "\n<i>Admin qo'shish uchun foydalanuvchi ID sini yuboring.</i>"

    keyboard = [
        [InlineKeyboardButton("➕ Admin qo'sh",   callback_data="adm_add")],
    ]
    if admins:
        keyboard.append([InlineKeyboardButton("🗑 Admin o'chir", callback_data="adm_del")])
    keyboard.append([InlineKeyboardButton("🔙 Admin Panel",      callback_data="admin_panel")])

    await query.edit_message_text(text, reply_markup=InlineKeyboardMarkup(keyboard), parse_mode="HTML")


async def adm_add_start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()
    if not is_super_admin(query.from_user.id):
        return
    context.user_data["waiting_new_admin"] = True
    keyboard = [[InlineKeyboardButton("🔙 Orqaga", callback_data="admin_admins")]]
    await query.edit_message_text(
        "➕ <b>Yangi admin qo'shish</b>\n\n"
        "Admin qilmoqchi bo'lgan foydalanuvchining <b>ID</b> sini yuboring.\n\n"
        "📌 ID ni topish: foydalanuvchi botga /start bossin,\n"
        "keyin Statistika → Oxirgi foydalanuvchilar dan ko'ring.\n\n"
        "<i>Bekor: /cancel</i>",
        reply_markup=InlineKeyboardMarkup(keyboard),
        parse_mode="HTML"
    )
    return ADD_ADMIN


async def adm_add_get_id(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not is_super_admin(update.effective_user.id):
        return ConversationHandler.END

    text = update.message.text.strip()
    context.user_data.pop("waiting_new_admin", None)
    keyboard = [[InlineKeyboardButton("🔙 Adminlar", callback_data="admin_admins")]]

    try:
        new_id = int(text)
    except ValueError:
        await update.message.reply_text(
            "❌ Noto'g'ri format. Faqat raqam (ID) yuboring.",
            reply_markup=InlineKeyboardMarkup(keyboard),
            parse_mode="HTML"
        )
        return ADD_ADMIN

    # O'zi super admin bo'lsa
    if new_id in ADMIN_IDS:
        await update.message.reply_text(
            "⚠️ Bu foydalanuvchi allaqachon super admin!",
            reply_markup=InlineKeyboardMarkup(keyboard),
            parse_mode="HTML"
        )
        return ConversationHandler.END

    # DB dan user ma'lumotini olish
    all_users = db.get_all_users()
    user_info = next((u for u in all_users if u["user_id"] == new_id), None)

    username  = user_info["username"]  if user_info else ""
    full_name = user_info["full_name"] if user_info else str(new_id)

    db.add_admin(new_id, username, full_name, update.effective_user.id)

    label = f"@{username}" if username else full_name
    # Yangi adminga xabar yuborish
    try:
        await context.bot.send_message(
            new_id,
            "🎉 <b>Tabriklaymiz!</b>\n\n"
            "Siz bot admini qildingiz!\n"
            "Admin paneliga kirish: /start → Admin panel",
            parse_mode="HTML"
        )
    except Exception:
        pass

    await update.message.reply_text(
        f"✅ <b>{label}</b> admin qilindi!\n"
        f"🆔 <code>{new_id}</code>",
        reply_markup=InlineKeyboardMarkup(keyboard),
        parse_mode="HTML"
    )
    return ConversationHandler.END


async def adm_del_start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()
    if not is_super_admin(query.from_user.id):
        return

    admins = db.get_admins()
    keyboard = []
    for a in admins:
        label = f"@{a['username']}" if a['username'] else a['full_name'] or str(a['user_id'])
        keyboard.append([InlineKeyboardButton(f"🗑 {label}", callback_data=f"adm_rm_{a['user_id']}")])
    keyboard.append([InlineKeyboardButton("🔙 Orqaga", callback_data="admin_admins")])

    await query.edit_message_text(
        "🗑 <b>Qaysi adminni o'chirish?</b>",
        reply_markup=InlineKeyboardMarkup(keyboard),
        parse_mode="HTML"
    )


async def adm_rm_callback(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    if not is_super_admin(query.from_user.id):
        await query.answer("❌ Ruxsat yo'q!", show_alert=True)
        return
    user_id = int(query.data.replace("adm_rm_", ""))
    db.remove_admin(user_id)
    # O'chirilgan adminga xabar
    try:
        await context.bot.send_message(user_id, "ℹ️ Siz admin ro'yxatidan o'chirilding.")
    except Exception:
        pass
    await query.answer("✅ Admin o'chirildi!", show_alert=True)
    await admin_admins(update, context)


async def ch_add_start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()
    context.user_data["waiting_channel"] = True
    keyboard = [[InlineKeyboardButton("🔙 Orqaga", callback_data="admin_channels")]]
    await query.edit_message_text(
        "➕ <b>Kanal/guruh qo'shish</b>\n\n"
        "<b>1-usul — @username:</b>\n"
        "  Kanal username ini kiriting\n"
        "  Misol: <code>@mening_kanalim</code>\n\n"
        "<b>2-usul — Forward:</b>\n"
        "  Kanaldan istalgan xabarni shu botga forward qiling\n"
        "  Bot ID ni o'zi topadi ✅\n\n"
        "<b>3-usul — ID:</b>\n"
        "  @username_to_id_bot orqali ID oling\n"
        "  Misol: <code>-1001234567890</code>\n\n"
        "⚠️ Bot kanalga <b>admin</b> qilinishi shart!\n\n"
        "<i>Bekor: /cancel</i>",
        reply_markup=InlineKeyboardMarkup(keyboard),
        parse_mode="HTML"
    )
    return ADD_CHANNEL


async def ch_add_get_id(update: Update, context: ContextTypes.DEFAULT_TYPE):
    msg = update.message

    # Forward qilingan xabar orqali ID olish (yangi PTB: forward_origin)
    origin = getattr(msg, "forward_origin", None)
    logger.info(f"ch_add_get_id: origin={origin}, msg_type={type(msg)}, text={msg.text}")
    forward_chat = None
    if origin:
        forward_chat = getattr(origin, "chat", None)
        logger.info(f"forward_chat={forward_chat}")

    if forward_chat:
        chat_id  = str(forward_chat.id)
        title    = getattr(forward_chat, "title", "") or ""
        uname    = getattr(forward_chat, "username", "") or ""
        username = f"@{uname}" if uname else ""
        db.add_channel(chat_id, title, username)
        context.user_data.pop("waiting_channel", None)
        label = title or username or chat_id
        keyboard = [[InlineKeyboardButton("🔙 Kanallar", callback_data="admin_channels")]]
        await msg.reply_text(
            f"✅ <b>Kanal qo'shildi!</b>\n\n"
            f"📢 {label}\n"
            f"🆔 <code>{chat_id}</code>\n\n"
            f"Endi foydalanuvchilar bu kanalga obuna bo'lishi talab qilinadi.",
            reply_markup=InlineKeyboardMarkup(keyboard),
            parse_mode="HTML"
        )
        return ConversationHandler.END

    # Matn orqali — @username yoki ID
    text = msg.text.strip() if msg.text else ""
    if not text:
        await msg.reply_text("❌ Matn yuboring yoki kanaldan xabar forward qiling.")
        return ADD_CHANNEL

    keyboard_retry = [[InlineKeyboardButton("🔙 Orqaga", callback_data="admin_channels")]]
    try:
        chat     = await context.bot.get_chat(text)
        chat_id  = str(chat.id)
        title    = chat.title or ""
        username = f"@{chat.username}" if chat.username else ""
        db.add_channel(chat_id, title, username)
        context.user_data.pop("waiting_channel", None)
        label = title or username or chat_id
        keyboard_ok = [[InlineKeyboardButton("🔙 Kanallar", callback_data="admin_channels")]]
        await msg.reply_text(
            f"✅ <b>Kanal qo'shildi!</b>\n\n"
            f"📢 {label}\n"
            f"🆔 <code>{chat_id}</code>\n\n"
            f"Endi foydalanuvchilar bu kanalga obuna bo'lishi talab qilinadi.",
            reply_markup=InlineKeyboardMarkup(keyboard_ok),
            parse_mode="HTML"
        )
        return ConversationHandler.END
    except Exception as e:
        # get_chat ishlamasa ham ID to'g'ri formatda bo'lsa — saqlab qo'yamiz
        # Telegram kanal ID formati: -100XXXXXXXXXX
        import re
        clean = text.strip()
        if re.match(r'^-100\d{8,12}$', clean):
            db.add_channel(clean, "", "")
            context.user_data.pop("waiting_channel", None)
            keyboard_ok = [[InlineKeyboardButton("🔙 Kanallar", callback_data="admin_channels")]]
            await msg.reply_text(
                f"✅ <b>Kanal ID saqlandi!</b>\n\n"
                f"🆔 <code>{clean}</code>\n\n"
                f"⚠️ Bot kanal nomini aniqlay olmadi (kirish cheklangan),\n"
                f"lekin obuna tekshiruvi ishlaydi.\n\n"
                f"Botni kanalga <b>admin</b> qilib, <b>Post Messages</b> huquqini bering — to'liq ishlaydi.",
                reply_markup=InlineKeyboardMarkup(keyboard_ok),
                parse_mode="HTML"
            )
            return ConversationHandler.END
        await msg.reply_text(
            f"❌ <b>Kanal topilmadi!</b>\n\n"
            f"Kiritilgan: <code>{text}</code>\n"
            f"Xatolik: <code>{e}</code>\n\n"
            "Mumkin bo'lgan sabablar:\n"
            "• Bot kanalga qo'shilmagan\n"
            "• ID noto'g'ri (to'g'risi: <code>-1001234567890</code>)\n\n"
            "Qaytadan kiriting:",
            reply_markup=InlineKeyboardMarkup(keyboard_retry),
            parse_mode="HTML"
        )
        return ADD_CHANNEL


async def ch_del_start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()
    channels = db.get_channels()

    keyboard = []
    for ch in channels:
        label = ch.get("title") or ch.get("username") or ch["chat_id"]
        keyboard.append([InlineKeyboardButton(f"🗑 {label}", callback_data=f"ch_rm_{ch['chat_id']}")])
    keyboard.append([InlineKeyboardButton("🔙 Orqaga", callback_data="admin_channels")])

    await query.edit_message_text(
        "🗑 <b>Qaysi kanalni o'chirish?</b>",
        reply_markup=InlineKeyboardMarkup(keyboard),
        parse_mode="HTML"
    )


async def ch_rm_callback(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()
    chat_id = query.data.replace("ch_rm_", "")
    db.remove_channel(chat_id)
    await query.answer(f"✅ O'chirildi!", show_alert=True)
    await admin_channels(update, context)


# ══════════════════════════════════════════════════════════
#  MAIN
# ══════════════════════════════════════════════════════════

def run_web_server():
    """Website uchun oddiy HTTP API server"""
    import os

    SITE_PASS = os.getenv("SITE_PASSWORD", "admin123")
    PORT      = int(os.getenv("PORT", 8080))

    class APIHandler(BaseHTTPRequestHandler):

        def log_message(self, format, *args):
            pass  # HTTP loglarni o'chirish

        def send_json(self, data, status=200):
            body = json.dumps(data, ensure_ascii=False).encode()
            self.send_response(status)
            self.send_header("Content-Type", "application/json; charset=utf-8")
            self.send_header("Access-Control-Allow-Origin", "*")
            self.send_header("Content-Length", len(body))
            self.end_headers()
            self.wfile.write(body)

        def send_html(self, path):
            try:
                with open(path, "rb") as f:
                    content = f.read()
                self.send_response(200)
                self.send_header("Content-Type", "text/html; charset=utf-8")
                self.send_header("Content-Length", len(content))
                self.end_headers()
                self.wfile.write(content)
            except FileNotFoundError:
                self.send_response(404)
                self.end_headers()

        def do_OPTIONS(self):
            self.send_response(200)
            self.send_header("Access-Control-Allow-Origin", "*")
            self.send_header("Access-Control-Allow-Methods", "GET, POST, DELETE, OPTIONS")
            self.send_header("Access-Control-Allow-Headers", "Content-Type")
            self.end_headers()

        def do_GET(self):
            p = self.path.split("?")[0]

            if p == "/" or p == "/index.html":
                self.send_html("index.html")

            elif p == "/api/stats":
                self.send_json(db.get_stats())

            elif p == "/api/content":
                rows = []
                for cat in ("anime", "drama", "kino"):
                    rows.extend(db.get_all_items(cat))
                self.send_json(rows)

            elif p == "/api/users":
                users = db.get_recent_users(50)
                self.send_json(users)

            elif p == "/api/admins":
                admins = db.get_admins()
                self.send_json(admins)

            else:
                self.send_response(404)
                self.end_headers()

        def do_POST(self):
            length = int(self.headers.get("Content-Length", 0))
            body   = json.loads(self.rfile.read(length) or b"{}")
            p      = self.path

            if p == "/api/content":
                cat   = body.get("category", "")
                code  = body.get("code", "").upper()
                title = body.get("title", "")
                desc  = body.get("description", "")
                if cat not in ("anime", "drama", "kino") or not code or not title:
                    self.send_json({"ok": False, "error": "Majburiy maydonlar yetishmayapti"}, 400)
                    return
                db.add_item(cat, code, title, desc)
                self.send_json({"ok": True})

            elif p == "/api/admins":
                user_id = body.get("user_id")
                if not user_id:
                    self.send_json({"ok": False, "error": "user_id kerak"}, 400)
                    return
                if user_id in ADMIN_IDS:
                    self.send_json({"ok": False, "error": "Bu super admin"}, 400)
                    return
                db.add_admin(int(user_id), "", str(user_id), ADMIN_IDS[0])
                self.send_json({"ok": True})

            else:
                self.send_response(404)
                self.end_headers()

        def do_DELETE(self):
            p = self.path
            # /api/content/anime/NARUTO
            if p.startswith("/api/content/"):
                parts = p.strip("/").split("/")
                if len(parts) == 4:
                    _, _, cat, code = parts
                    ok = db.delete_item(cat, code.upper())
                    self.send_json({"ok": ok})
                    return
            # /api/admins/12345
            elif p.startswith("/api/admins/"):
                parts = p.strip("/").split("/")
                if len(parts) == 3:
                    user_id = int(parts[2])
                    ok = db.remove_admin(user_id)
                    self.send_json({"ok": ok})
                    return
            self.send_response(404)
            self.end_headers()

    server = HTTPServer(("0.0.0.0", PORT), APIHandler)
    logger.info(f"Web server {PORT}-portda ishga tushdi")
    server.serve_forever()


def main():
    app = Application.builder().token(BOT_TOKEN).build()

    # Serial qo'shish
    add_conv = ConversationHandler(
        entry_points=[CallbackQueryHandler(admin_add_start, pattern="^admin_add$")],
        states={
            ADD_CAT:  [CallbackQueryHandler(add_select_cat, pattern="^add_")],
            ADD_CODE: [MessageHandler(filters.TEXT & ~filters.COMMAND, add_get_code)],
            ADD_TITLE:[MessageHandler(filters.TEXT & ~filters.COMMAND, add_get_title)],
            ADD_DESC: [MessageHandler(filters.TEXT, add_get_desc)],
        },
        fallbacks=[
            CommandHandler("cancel", conv_cancel),
            CallbackQueryHandler(admin_panel, pattern="^admin_panel$"),
        ],
        per_user=True,
        allow_reentry=True,
    )

    # Qism qo'shish
    addep_conv = ConversationHandler(
        entry_points=[
            CallbackQueryHandler(admin_addep_start, pattern="^admin_addep$"),
            CallbackQueryHandler(continue_episode,  pattern="^cont_ep_"),
        ],
        states={
            ADDEP_CAT:  [CallbackQueryHandler(addep_select_cat, pattern="^addep_")],
            ADDEP_CODE: [MessageHandler(filters.TEXT & ~filters.COMMAND, addep_get_code)],
            ADDEP_FILE: [
                MessageHandler(filters.VIDEO | filters.Document.ALL | filters.PHOTO, addep_get_file),
                CallbackQueryHandler(continue_episode, pattern="^cont_ep_"),
                CallbackQueryHandler(admin_panel,      pattern="^admin_panel$"),
            ],
        },
        fallbacks=[
            CommandHandler("cancel", conv_cancel),
            CallbackQueryHandler(admin_panel, pattern="^admin_panel$"),
        ],
        per_user=True,
        allow_reentry=True,
    )

    # O'chirish
    del_conv = ConversationHandler(
        entry_points=[CallbackQueryHandler(admin_delete_start, pattern="^admin_delete$")],
        states={
            DEL_CAT:  [CallbackQueryHandler(delete_select_cat, pattern="^del_")],
            DEL_CODE: [MessageHandler(filters.TEXT & ~filters.COMMAND, delete_confirm)],
        },
        fallbacks=[
            CommandHandler("cancel", conv_cancel),
            CallbackQueryHandler(admin_panel, pattern="^admin_panel$"),
        ],
        per_user=True,
        allow_reentry=True,
    )

    # Broadcast
    broadcast_conv = ConversationHandler(
        entry_points=[CallbackQueryHandler(admin_broadcast_start, pattern="^admin_broadcast$")],
        states={
            BROADCAST: [MessageHandler(
                filters.TEXT | filters.VIDEO | filters.PHOTO | filters.Document.ALL,
                do_broadcast
            )],
        },
        fallbacks=[
            CommandHandler("cancel", conv_cancel),
            CallbackQueryHandler(admin_panel, pattern="^admin_panel$"),
        ],
        per_user=True,
        allow_reentry=True,
    )

    # Kanal qo'shish
    channel_conv = ConversationHandler(
        entry_points=[CallbackQueryHandler(ch_add_start, pattern="^ch_add$")],
        states={
            ADD_CHANNEL: [
                MessageHandler(filters.TEXT & ~filters.COMMAND, ch_add_get_id),
                MessageHandler(filters.FORWARDED, ch_add_get_id),
            ],
        },
        fallbacks=[
            CommandHandler("cancel", conv_cancel),
            CallbackQueryHandler(admin_channels, pattern="^admin_channels$"),
        ],
        per_user=True,
        allow_reentry=True,
    )

    # Admin qo'shish conversation
    admin_mgmt_conv = ConversationHandler(
        entry_points=[CallbackQueryHandler(adm_add_start, pattern="^adm_add$")],
        states={
            ADD_ADMIN: [MessageHandler(filters.TEXT & ~filters.COMMAND, adm_add_get_id)],
        },
        fallbacks=[
            CommandHandler("cancel", conv_cancel),
            CallbackQueryHandler(admin_admins, pattern="^admin_admins$"),
        ],
        per_user=True,
        allow_reentry=True,
    )

    app.add_handler(CommandHandler("start", start))
    app.add_handler(CommandHandler("admin", user_to_admin_cmd))

    # Conversation handlerlar — 0-guruhda, birinchi tekshiriladi
    app.add_handler(add_conv,        group=0)
    app.add_handler(addep_conv,      group=0)
    app.add_handler(del_conv,        group=0)
    app.add_handler(broadcast_conv,  group=0)
    app.add_handler(channel_conv,    group=0)
    app.add_handler(admin_mgmt_conv, group=0)

    # Callback handlerlar — 1-guruhda
    app.add_handler(CallbackQueryHandler(show_category,      pattern="^cat_"),          group=1)
    app.add_handler(CallbackQueryHandler(back_main,          pattern="^back_main$"),    group=1)
    app.add_handler(CallbackQueryHandler(check_sub_callback, pattern="^check_sub$"),    group=1)
    app.add_handler(CallbackQueryHandler(admin_panel,        pattern="^admin_panel$"),  group=1)
    app.add_handler(CallbackQueryHandler(admin_stats,        pattern="^admin_stats$"),  group=1)
    app.add_handler(CallbackQueryHandler(admin_channels,     pattern="^admin_channels$"), group=1)
    app.add_handler(CallbackQueryHandler(ch_del_start,       pattern="^ch_del$"),         group=1)
    app.add_handler(CallbackQueryHandler(ch_rm_callback,     pattern="^ch_rm_"),          group=1)
    app.add_handler(CallbackQueryHandler(admin_admins,       pattern="^admin_admins$"),   group=1)
    app.add_handler(CallbackQueryHandler(adm_del_start,      pattern="^adm_del$"),        group=1)
    app.add_handler(CallbackQueryHandler(adm_rm_callback,    pattern="^adm_rm_"),         group=1)
    app.add_handler(CallbackQueryHandler(send_episode,       pattern="^ep_"),           group=1)

    # Matn handler — oxirida
    app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, handle_code),       group=1)

    logger.info("Bot ishga tushdi...")

    # Web server alohida thread da
    web_thread = threading.Thread(target=run_web_server, daemon=True)
    web_thread.start()

    app.run_polling(allowed_updates=Update.ALL_TYPES)


if __name__ == "__main__":
    main()
