import logging
import os
import asyncio
import threading
from http.server import HTTPServer, BaseHTTPRequestHandler
import json
from telegram import Update, InlineKeyboardButton, InlineKeyboardMarkup, MessageEntity
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

BOT_TOKEN = os.getenv("BOT_TOKEN")
if not BOT_TOKEN:
    raise ValueError("BOT_TOKEN environment variable is not set!")

ADMIN_IDS = [int(x) for x in os.getenv("ADMIN_IDS", "").split(",") if x.strip()]

# ── Conversation states ────────────────────────────────────────────────
(
    ADD_CAT, ADD_CODE, ADD_TITLE, ADD_DESC, ADD_POSTER,
    ADD_EP_NUM, ADD_EP_FILE,
    ADDEP_CAT, ADDEP_CODE, ADDEP_FILE,
    DEL_CAT, DEL_CODE,
    BROADCAST,
    ADMIN_MSG,
    ADD_CHANNEL,
    ADD_ADMIN,
    POSTER_CODE, POSTER_IMG,
) = range(18)

CATEGORY_NAMES = {
    "anime": "🎌 Anime",
    "drama": "🎭 Drama",
    "kino":  "🎬 Kino"
}

# ── PREMIUM EMOJI — MessageEntity usuli ───────────────────────────────
# parse_mode="HTML" da tg-emoji ishlamaydi.
# To'g'ri usul: MessageEntity(type="custom_emoji") — parse_mode kerak emas.
# ID topish: @ShowJsonBot ga premium emoji forward qiling -> custom_emoji_id
EMOJI_IDS = {
    "anime": "5362097550423762417",   # sizning ID
    "drama": None,                     # keyin bering
    "kino":  None,
    "star":  None,
    "tv":    None,
    "bell":  None,
}

EMOJI_FALLBACK = {
    "anime": "🎌",
    "drama": "🎭",
    "kino":  "🎬",
    "star":  "✨",
    "tv":    "📺",
    "bell":  "🔔",
}


class PremiumText:
    """
    Premium emoji + matn birlashtiruvchi.
    Misol:
        pt = PremiumText()
        pt.add_emoji("anime").add(" Anime").newline()
        await msg.reply_text(**pt.kwargs())
    """
    def __init__(self):
        self.text = ""
        self.entities: list = []

    def _offset(self) -> int:
        return len(self.text.encode("utf-16-le")) // 2

    def add_emoji(self, key: str) -> "PremiumText":
        emoji_id = EMOJI_IDS.get(key)
        fallback = EMOJI_FALLBACK.get(key, "▪️")
        offset = self._offset()
        self.text += fallback
        if emoji_id:
            from telegram import MessageEntity as ME
            length = len(fallback.encode("utf-16-le")) // 2
            self.entities.append(ME(type=ME.CUSTOM_EMOJI, offset=offset, length=length, custom_emoji_id=emoji_id))
        return self

    def add(self, txt: str) -> "PremiumText":
        self.text += txt
        return self

    def add_bold(self, txt: str) -> "PremiumText":
        from telegram import MessageEntity as ME
        offset = self._offset()
        length = len(txt.encode("utf-16-le")) // 2
        self.text += txt
        self.entities.append(ME(type=ME.BOLD, offset=offset, length=length))
        return self

    def add_italic(self, txt: str) -> "PremiumText":
        from telegram import MessageEntity as ME
        offset = self._offset()
        length = len(txt.encode("utf-16-le")) // 2
        self.text += txt
        self.entities.append(ME(type=ME.ITALIC, offset=offset, length=length))
        return self

    def newline(self) -> "PremiumText":
        self.text += "\n"
        return self

    def kwargs(self, **extra) -> dict:
        r = {"text": self.text}
        if self.entities:
            r["entities"] = self.entities
        r.update(extra)
        return r

    def edit_kwargs(self, **extra) -> dict:
        return self.kwargs(**extra)

# Fallback konstantalar (eski kodlar uchun)
PE_ANIME  = EMOJI_FALLBACK["anime"]
PE_DRAMA  = EMOJI_FALLBACK["drama"]
PE_KINO   = EMOJI_FALLBACK["kino"]
PE_STAR   = EMOJI_FALLBACK["star"]
PE_TV     = EMOJI_FALLBACK["tv"]
PE_BELL   = EMOJI_FALLBACK["bell"]

def is_admin(user_id: int) -> bool:
    """Super admin (.env) yoki DB dagi istalgan admin"""
    return user_id in ADMIN_IDS or db.is_admin(user_id)

def is_super_admin(user_id: int) -> bool:
    """Faqat .env dagi super admin"""
    return user_id in ADMIN_IDS

def can_delete_content(user_id: int) -> bool:
    """Kontent o'chirish huquqi"""
    return db.can_delete_content(user_id, ADMIN_IDS)

def can_manage_admins(user_id: int) -> bool:
    """Admin boshqarish huquqi"""
    return db.can_manage_admins(user_id, ADMIN_IDS)


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
    """'Obuna bo'ldim' tugmasi — tekshiradi va avtomatik xabar yuboradi"""
    query = update.callback_query
    await query.answer("Tekshirilmoqda...", show_alert=False)

    ok, not_subbed = await check_subscription(context.bot, query.from_user.id)

    if ok:
        user = query.from_user
        db.add_user(user.id, user.username or "", user.full_name or "")

        keyboard = [
            [
                InlineKeyboardButton("🎌 Anime",  callback_data="cat_anime"),
                InlineKeyboardButton("🎭 Drama",  callback_data="cat_drama"),
            ],
            [
                InlineKeyboardButton("🎬 Kino",    callback_data="cat_kino"),
                InlineKeyboardButton("🌐 Веб сайт", url="https://anime-production-df87.up.railway.app"),
            ],
        ]
        if is_admin(user.id):
            keyboard.append([InlineKeyboardButton("⚙️ Admin Panel", callback_data="admin_panel")])

        text = (
            "✅ <b>Obuna tasdiqlandi!</b>\n\n"
            f"Salom, <b>{user.first_name}</b>!\n"
            "Quyidan kategoriya tanlang 👇"
        )

        # Obuna xabarini yangilaymiz — o'chirmasdan keyboard qo'shamiz
        try:
            await query.edit_message_text(
                text,
                reply_markup=InlineKeyboardMarkup(keyboard),
                parse_mode="HTML"
            )
        except Exception:
            # edit ishlamasa yangi xabar
            try:
                await query.delete_message()
            except Exception:
                pass
            await context.bot.send_message(
                chat_id=user.id,
                text=text,
                reply_markup=InlineKeyboardMarkup(keyboard),
                parse_mode="HTML"
            )
    else:
        # Hali obuna bo'lmagan
        await query.answer(
            "❌ Siz hali barcha kanallarga obuna bo'lmagansiz!",
            show_alert=True
        )
        try:
            await query.delete_message()
        except Exception:
            pass
        await subscription_wall(update, context, not_subbed)

# ══════════════════════════════════════════════════════════
#  FOYDALANUVCHI
# ══════════════════════════════════════════════════════════

async def start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if update.effective_chat.type != "private":
        return

    user = update.effective_user
    db.add_user(user.id, user.username or "", user.full_name or "")

    # Obuna tekshiruvi
    if not is_admin(user.id):
        ok, not_subbed = await check_subscription(context.bot, user.id)
        if not ok:
            await subscription_wall(update, context, not_subbed)
            return

    # Saytdan ?start=KOD bilan kelgan bo'lsa — to'g'ri shu serialni yuboramiz
    if context.args:
        code = context.args[0].upper()
        # Qaysi kategoriyada ekanligini topamiz
        for cat in ("anime", "drama", "kino"):
            item = db.get_item(cat, code)
            if item:
                context.user_data["category"] = cat
                # handle_code ga o'xshash ishlov
                episodes = db.get_episodes(cat, code)
                cat_name = CATEGORY_NAMES.get(cat, cat)
                ep_buttons = []
                row = []
                for ep in episodes:
                    row.append(InlineKeyboardButton(
                        f"{ep['episode_num']}-qism",
                        callback_data=f"ep_{cat}_{code}_{ep['episode_num']}"
                    ))
                    if len(row) == 3:
                        ep_buttons.append(row)
                        row = []
                if row:
                    ep_buttons.append(row)
                ep_buttons.append([InlineKeyboardButton("🔙 Orqaga", callback_data=f"cat_{cat}")])

                desc_text = f"\n📝 {item['description']}\n" if item.get("description") else "\n"
                text = (
                    f"{cat_name} | <b>{item['title']}</b>"
                    f"{desc_text}"
                    f"\n{PE_TV} Jami: <b>{len(episodes)} qism</b>\n"
                    "Qismni tanlang:"
                )
                if item.get("poster"):
                    await update.message.reply_photo(
                        photo=item["poster"],
                        caption=text,
                        reply_markup=InlineKeyboardMarkup(ep_buttons),
                        parse_mode="HTML"
                    )
                else:
                    await update.message.reply_text(
                        text,
                        reply_markup=InlineKeyboardMarkup(ep_buttons),
                        parse_mode="HTML"
                    )
                return
        # Kod topilmadi — oddiy start
        await update.message.reply_text(f"❌ <b>{code}</b> topilmadi.", parse_mode="HTML")
        return

    keyboard = [
        [
            InlineKeyboardButton("🎌 Anime",  callback_data="cat_anime"),
            InlineKeyboardButton("🎭 Drama",  callback_data="cat_drama"),
        ],
        [
            InlineKeyboardButton("🎬 Kino",    callback_data="cat_kino"),
            InlineKeyboardButton("🌐 Веб сайт", url="https://anime-production-df87.up.railway.app"),
        ],
    ]
    if is_admin(user.id):
        keyboard.append([InlineKeyboardButton("⚙️ Admin Panel", callback_data="admin_panel")])

    text = (
        f"{PE_STAR} Salom, <b>{user.first_name}</b>! {PE_STAR}\n\n"
        "Kategoriya tanlang:\n\n"
        f"{PE_ANIME} <b>Anime</b> — Anime seriyalar\n"
        f"{PE_DRAMA} <b>Drama</b> — Drama seriyalar\n"
        f"{PE_KINO} <b>Kino</b> — Tarjima kinolar"
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

    cat_name = CATEGORY_NAMES.get(category, category)
    keyboard = [[InlineKeyboardButton("🔙 Orqaga", callback_data="back_main")]]
    await query.edit_message_text(
        f"{cat_name}\n\n"
        f"🔍 Serial kodini yuboring:\n"
        f"<i>Masalan: A001, D001, K001</i>",
        reply_markup=InlineKeyboardMarkup(keyboard),
        parse_mode="HTML"
    )


async def back_main(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()
    await start(update, context)


# ══════════════════════════════════════════════════════════
#  POSTER QO'SHISH
# ══════════════════════════════════════════════════════════

async def admin_poster_start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()
    if not is_admin(query.from_user.id):
        return

    keyboard = [
        [InlineKeyboardButton("➕ Poster qo'shish",    callback_data="poster_action_add")],
        [InlineKeyboardButton("🔄 Poster almashtirish", callback_data="poster_action_change")],
        [InlineKeyboardButton("🔙 Admin Panel",         callback_data="admin_panel")],
    ]
    try:
        await query.edit_message_text(
            "🖼 <b>Poster boshqaruvi</b>\n\nNima qilmoqchisiz?",
            reply_markup=InlineKeyboardMarkup(keyboard),
            parse_mode="HTML"
        )
    except Exception:
        await query.message.reply_text(
            "🖼 <b>Poster boshqaruvi</b>\n\nNima qilmoqchisiz?",
            reply_markup=InlineKeyboardMarkup(keyboard),
            parse_mode="HTML"
        )


async def poster_select_cat(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()
    cat = query.data.replace("poster_cat_", "")
    context.user_data["poster_cat"]      = cat
    context.user_data["waiting_poster_code"] = True

    action   = context.user_data.get("poster_action", "add")
    items    = db.get_all_items(cat)
    cat_name = CATEGORY_NAMES.get(cat, cat)

    if not items:
        keyboard = [[InlineKeyboardButton("🔙 Orqaga", callback_data="admin_poster")]]
        await query.edit_message_text(
            f"{cat_name} bo'sh.",
            reply_markup=InlineKeyboardMarkup(keyboard)
        )
        return ConversationHandler.END

    keyboard = [[InlineKeyboardButton("🔙 Orqaga", callback_data="admin_poster")]]

    if action == "add":
        # Faqat postersiz seriallar
        no_poster = [i for i in items if not i.get("poster")]
        if not no_poster:
            await query.edit_message_text(
                f"✅ {cat_name} dagi barcha seriallarning posteri bor!",
                reply_markup=InlineKeyboardMarkup(keyboard),
                parse_mode="HTML"
            )
            return ConversationHandler.END
        await query.edit_message_text(
            f"➕ <b>{cat_name}</b> — poster qo'shish\n\n"
            f"Serial kodini yuboring:\n"
            f"<i>Postersizlar: {', '.join(i['code'] for i in no_poster[:10])}</i>",
            reply_markup=InlineKeyboardMarkup(keyboard),
            parse_mode="HTML"
        )
    else:
        # Almashtirish — faqat posterli seriallar
        with_poster = [i for i in items if i.get("poster")]
        if not with_poster:
            await query.edit_message_text(
                f"❌ {cat_name} da hozircha poster qo'shilmagan!",
                reply_markup=InlineKeyboardMarkup(keyboard),
                parse_mode="HTML"
            )
            return ConversationHandler.END
        await query.edit_message_text(
            f"🔄 <b>{cat_name}</b> — poster almashtirish\n\n"
            f"Serial kodini yuboring:\n"
            f"<i>Posterlilar: {', '.join(i['code'] for i in with_poster[:10])}</i>",
            reply_markup=InlineKeyboardMarkup(keyboard),
            parse_mode="HTML"
        )
    return POSTER_CODE


async def poster_action_callback(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()
    action = query.data.replace("poster_action_", "")
    context.user_data["poster_action"] = action

    label = "➕ Poster qo'shish" if action == "add" else "🔄 Poster almashtirish"
    keyboard = [
        [
            InlineKeyboardButton("🎌 Anime", callback_data="poster_cat_anime"),
            InlineKeyboardButton("🎭 Drama", callback_data="poster_cat_drama"),
            InlineKeyboardButton("🎬 Kino",  callback_data="poster_cat_kino"),
        ],
        [InlineKeyboardButton("🔙 Orqaga", callback_data="admin_poster")]
    ]
    try:
        await query.edit_message_text(
            f"{label}\n\nKategoriya tanlang:",
            reply_markup=InlineKeyboardMarkup(keyboard),
            parse_mode="HTML"
        )
    except Exception:
        await query.message.reply_text(
            f"{label}\n\nKategoriya tanlang:",
            reply_markup=InlineKeyboardMarkup(keyboard),
            parse_mode="HTML"
        )
    return POSTER_CODE
    """Tugmadan serial tanlanganda rasm so'rash"""
    query = update.callback_query
    await query.answer()
    code = query.data.replace("poster_pick_", "").upper()
    cat  = context.user_data.get("poster_cat")
    item = db.get_item(cat, code)

    if not item:
        await query.answer("Topilmadi!", show_alert=True)
        return

    context.user_data["poster_code"]     = code
    context.user_data["waiting_poster_img"] = True

    has_poster = "✅ Poster bor — almashtirish uchun yuborish" if item.get("poster") else "❌ Poster yo'q — qo'shish uchun yuborish"
    keyboard = [[InlineKeyboardButton("🔙 Orqaga", callback_data=f"poster_cat_{cat}")]]

    if item.get("poster"):
        await query.edit_message_text(
            f"🖼 <b>{item['title']}</b>\n\n"
            f"{has_poster}\n\n"
            f"Yangi rasm yuboring:",
            reply_markup=InlineKeyboardMarkup(keyboard),
            parse_mode="HTML"
        )
    else:
        await query.edit_message_text(
            f"🖼 <b>{item['title']}</b>\n\n"
            f"{has_poster}\n\n"
            f"Rasm yuboring:",
            reply_markup=InlineKeyboardMarkup(keyboard),
            parse_mode="HTML"
        )
    return POSTER_IMG


async def poster_get_code(update: Update, context: ContextTypes.DEFAULT_TYPE):
    context.user_data.pop("waiting_poster_code", None)
    code   = update.message.text.strip().upper()
    action = context.user_data.get("poster_action", "add")
    cat    = context.user_data.get("poster_cat")

    item = db.get_item(cat, code) if cat else None
    if not item:
        await update.message.reply_text(
            f"❌ <code>{code}</code> topilmadi. Qaytadan yozing:",
            parse_mode="HTML"
        )
        return POSTER_CODE

    # Almashtirish rejimida poster yo'q bo'lsa ogohlantirish
    if action == "change" and not item.get("poster"):
        await update.message.reply_text(
            f"⚠️ <b>{item['title']}</b> da poster yo'q.\n"
            f"Baribir rasm yuboring — qo'shiladi:",
            parse_mode="HTML"
        )
    # Qo'shish rejimida poster allaqachon bor bo'lsa ogohlantirish
    elif action == "add" and item.get("poster"):
        await update.message.reply_text(
            f"⚠️ <b>{item['title']}</b> da poster allaqachon bor.\n"
            f"Yangi rasm yuborsangiz — almashinadi:",
            parse_mode="HTML"
        )

    context.user_data["poster_code"]     = code
    context.user_data["waiting_poster_img"] = True

    has_poster = "✅ Poster bor" if item.get("poster") else "❌ Poster yo'q"
    keyboard   = [[InlineKeyboardButton("🔙 Bekor", callback_data="admin_poster")]]
    await update.message.reply_text(
        f"🖼 <b>{item['title']}</b> — {has_poster}\n\n"
        f"Rasm yuboring (foto sifatida):",
        reply_markup=InlineKeyboardMarkup(keyboard),
        parse_mode="HTML"
    )
    return POSTER_IMG


async def poster_get_img(update: Update, context: ContextTypes.DEFAULT_TYPE):
    context.user_data.pop("waiting_poster_img", None)
    cat  = context.user_data.get("poster_cat")
    code = context.user_data.get("poster_code")

    if not cat or not code:
        return ConversationHandler.END

    if update.message.photo:
        file_id = update.message.photo[-1].file_id
    elif update.message.document and update.message.document.mime_type and \
         update.message.document.mime_type.startswith("image"):
        file_id = update.message.document.file_id
    else:
        await update.message.reply_text("📷 Faqat rasm yuboring:")
        return POSTER_IMG

    db.update_poster(cat, code, file_id)
    item = db.get_item(cat, code)

    keyboard = [
        [InlineKeyboardButton("🖼 Yana poster qo'sh", callback_data="admin_poster")],
        [InlineKeyboardButton("🔙 Admin Panel",        callback_data="admin_panel")],
    ]
    await update.message.reply_text(
        f"✅ <b>{item['title']}</b> uchun poster saqlandi!\n\n"
        f"Website da avtomatik ko'rinadi. 🌐",
        reply_markup=InlineKeyboardMarkup(keyboard),
        parse_mode="HTML"
    )
    context.user_data.pop("poster_cat",  None)
    context.user_data.pop("poster_code", None)
    return ConversationHandler.END


async def handle_code(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """
    Foydalanuvchi kod yozganda — hamma kategoriyadan qidiradi.
    Kategoriya tanlanmagan bo'lsa ham ishlaydi.
    Admin ConversationHandler state da bo'lsa — hech narsa qilmaydi.
    """
    if update.effective_chat.type != "private":
        return

    # Admin ConversationHandler state da bo'lsa — bu handler ishlamasin
    # (addep, add_conv, del_conv va boshqalar group=0 da o'zlari ishlaydi)
    if is_admin(update.effective_user.id):
        conv_flags = [
            "waiting_channel", "waiting_ep_code", "waiting_poster_code",
            "waiting_new_admin", "waiting_add_code", "waiting_add_title",
            "waiting_add_desc", "waiting_del_code",
        ]
        if any(context.user_data.get(f) for f in conv_flags):
            return

    user = update.effective_user
    db.add_user(user.id, user.username or "", user.full_name or "")

    # Obuna tekshiruvi (faqat oddiy foydalanuvchilar)
    if not is_admin(user.id):
        ok, not_subbed = await check_subscription(context.bot, user.id)
        if not ok:
            await subscription_wall(update, context, not_subbed)
            return

    text_input = update.message.text.strip()
    code = text_input.upper()

    # Hamma kategoriyadan qidirish
    found = None
    found_cat = None

    # Avval tanlangan kategoriyadan qidirish
    category = context.user_data.get("category")
    if category:
        found = db.get_item(category, code)
        found_cat = category

    # Topilmasa — hamma kategoriyadan qidirish
    if not found:
        for cat in ["anime", "drama", "kino"]:
            item = db.get_item(cat, code)
            if item:
                found = item
                found_cat = cat
                break

    if not found:
        # Agar matn juda qisqa yoki raqam/harf kodga o'xshamasa
        if len(text_input) < 2 or len(text_input) > 10:
            keyboard = [
                [
                    InlineKeyboardButton("🎌 Anime", callback_data="cat_anime"),
                    InlineKeyboardButton("🎭 Drama", callback_data="cat_drama"),
                ],
                [
                    InlineKeyboardButton("🎬 Kino",    callback_data="cat_kino"),
                    InlineKeyboardButton("🌐 Веб сайт", url="https://anime-production-df87.up.railway.app"),
                ],
            ]
            await update.message.reply_text(
                "📋 Kategoriya tanlang yoki serial kodini yozing:",
                reply_markup=InlineKeyboardMarkup(keyboard)
            )
            return
        await update.message.reply_text(
            f"❌ <b>{code}</b> kodi topilmadi.\n\n"
            "To'g'ri kodni kiriting yoki quyidan kategoriya tanlang.",
            parse_mode="HTML",
            reply_markup=InlineKeyboardMarkup([
                [
                    InlineKeyboardButton("🎌 Anime", callback_data="cat_anime"),
                    InlineKeyboardButton("🎭 Drama", callback_data="cat_drama"),
                ],
                [
                    InlineKeyboardButton("🎬 Kino",    callback_data="cat_kino"),
                    InlineKeyboardButton("🌐 Веб сайт", url="https://anime-production-df87.up.railway.app"),
                ],
            ])
        )
        return

    episodes = db.get_episodes(found_cat, code)
    cat_name = CATEGORY_NAMES.get(found_cat, found_cat)

    if not episodes:
        text = (
            f"{cat_name} | <b>{found['title']}</b>\n\n"
            "📭 Hali qismlar qo'shilmagan."
        )
        if found.get("poster"):
            await update.message.reply_photo(
                photo=found["poster"],
                caption=text,
                parse_mode="HTML"
            )
        else:
            await update.message.reply_text(text, parse_mode="HTML")
        return

    # Qismlar tugmalari — 3 tadan qator
    ep_buttons = []
    row = []
    for ep in episodes:
        row.append(InlineKeyboardButton(
            f"{ep['episode_num']}-qism",
            callback_data=f"ep_{found_cat}_{code}_{ep['episode_num']}"
        ))
        if len(row) == 3:
            ep_buttons.append(row)
            row = []
    if row:
        ep_buttons.append(row)
    ep_buttons.append([InlineKeyboardButton("🔙 Orqaga", callback_data=f"cat_{found_cat}")])

    # Chiroyli xabar matni
    desc_text = f"\n📝 {found['description']}\n" if found.get("description") else "\n"
    text = (
        f"{cat_name} | <b>{found['title']}</b>"
        f"{desc_text}"
        f"\n{PE_TV} Jami: <b>{len(episodes)} qism</b>\n"
        f"Qismni tanlang:"
    )

    if found.get("poster"):
        await update.message.reply_photo(
            photo=found["poster"],
            caption=text,
            reply_markup=InlineKeyboardMarkup(ep_buttons),
            parse_mode="HTML"
        )
    else:
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
        f"{PE_TV} <b>{ep_label}</b>"
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
            InlineKeyboardButton("🖼 Poster qo'sh",  callback_data="admin_poster"),
        ],
        [
            InlineKeyboardButton("📊 Statistika",    callback_data="admin_stats"),
            InlineKeyboardButton("📢 Broadcast",     callback_data="admin_broadcast"),
        ],
        [
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

    await update.message.reply_text(
        "🖼 <b>Poster rasm yuboring</b> (ixtiyoriy — o'tkazish: /skip):\n\n"
        "<i>Rasm websiteda kartochkada ko'rinadi</i>",
        parse_mode="HTML"
    )
    return ADD_POSTER


async def add_get_poster(update: Update, context: ContextTypes.DEFAULT_TYPE):
    poster_id = ""

    if update.message.text and update.message.text.strip() == "/skip":
        poster_id = ""
    elif update.message.photo:
        poster_id = update.message.photo[-1].file_id
    elif update.message.document and update.message.document.mime_type.startswith("image"):
        poster_id = update.message.document.file_id
    else:
        await update.message.reply_text(
            "📷 Rasm yuboring yoki /skip bosing:",
            parse_mode="HTML"
        )
        return ADD_POSTER

    cat   = context.user_data["add_cat"]
    code  = context.user_data["add_code"]
    title = context.user_data["add_title"]
    desc  = context.user_data.get("add_desc", "")

    db.add_item(cat, code, title, desc, poster_id)

    cat_name = CATEGORY_NAMES.get(cat, cat)
    poster_text = "✅ Rasm saqlandi" if poster_id else "⏭ Rasmsiz qo'shildi"
    await update.message.reply_text(
        f"✅ <b>Serial qo'shildi!</b>\n\n"
        f"📁 {cat_name}\n"
        f"🔑 Kod: <code>{code}</code>\n"
        f"🎬 Sarlavha: {title}\n"
        f"🖼 Poster: {poster_text}\n\n"
        f"Endi <b>qismlarni qo'shishingiz</b> mumkin.\n"
        f"Admin panel → 📺 Qism qo'sh",
        parse_mode="HTML"
    )
    for k in ["add_cat", "add_code", "add_title", "add_desc"]:
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

    # Bitta fayl — atomic saqlash (race condition yo'q)
    next_num = db.add_episode_auto(cat, code, file_id, file_type)
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
        num = db.add_episode_auto(cat, code, file_id, file_type)
        saved.append(num)

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
    uid = query.from_user.id
    # Faqat super admin yoki manager broadcast yuborishi mumkin
    if not (is_super_admin(uid) or db.get_admin_role(uid) == "manager"):
        await query.answer("❌ Broadcast huquqi yo'q!", show_alert=True)
        return

    stats = db.get_stats()
    keyboard = [[InlineKeyboardButton("🔙 Admin Panel", callback_data="admin_panel")]]
    await query.edit_message_text(
        f"{PE_BELL} <b>Broadcast</b>\n\n"
        f"👥 {stats['users']} ta foydalanuvchiga yuboriladi\n\n"
        "Xabar yozing yoki fayl/rasm/video yuboring.\n"
        f"{PE_STAR} <i>Premium emoji, caption entities — hammasi saqlanadi</i>\n\n"
        "<i>Bekor: /cancel yoki ↙️ Admin Panel</i>",
        reply_markup=InlineKeyboardMarkup(keyboard),
        parse_mode="HTML"
    )
    return BROADCAST


async def do_broadcast(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """
    Broadcast: copy_message orqali yuboriladi.
    Admin qanday yuborsa — premium emoji, entities, caption, sticker, voice,
    animation, video_note — HAMMASI aynan saqlanadi.
    parse_mode="HTML" ishlatilmaydi — entities Telegram tomonidan saqlanadi.
    """
    users    = db.get_all_users()
    sent     = 0
    failed   = 0
    src_chat = update.message.chat_id
    msg_id   = update.message.message_id
    status   = await update.message.reply_text(f"📤 Yuborilmoqda... 0/{len(users)}")

    for i, user in enumerate(users):
        uid = user["user_id"]
        try:
            await context.bot.copy_message(
                chat_id=uid,
                from_chat_id=src_chat,
                message_id=msg_id,
            )
            sent += 1
        except Exception as e:
            err = str(e).lower()
            if "flood" in err or "too many" in err:
                await asyncio.sleep(30)
                try:
                    await context.bot.copy_message(
                        chat_id=uid,
                        from_chat_id=src_chat,
                        message_id=msg_id,
                    )
                    sent += 1
                except Exception:
                    failed += 1
            elif "blocked" in err or "not found" in err or "deactivated" in err or "kicked" in err:
                failed += 1
            else:
                failed += 1

        # Har 25 xabardan keyin 1 soniya — flood limitdan saqlanish
        if (i + 1) % 25 == 0:
            await asyncio.sleep(1)

        if (i + 1) % 10 == 0:
            try:
                await status.edit_text(
                    f"📤 Yuborilmoqda... {i+1}/{len(users)}\n"
                    f"✔️ {sent} | ❌ {failed}"
                )
            except Exception:
                pass

    try:
        await status.edit_text(
            f"✅ <b>Broadcast tugadi!</b>\n\n"
            f"👥 Jami: {len(users)}\n"
            f"✔️ Yuborildi: {sent}\n"
            f"❌ Xatolik: {failed}",
            parse_mode="HTML"
        )
    except Exception:
        pass
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
    if not can_manage_admins(query.from_user.id):
        await query.answer("❌ Sizda bu huquq yo'q!", show_alert=True)
        return
    context.user_data["waiting_new_admin"] = True
    keyboard = [[InlineKeyboardButton("🔙 Orqaga", callback_data="admin_admins")]]
    await query.edit_message_text(
        "➕ <b>Yangi admin qo'shish</b>\n\n"
        "Foydalanuvchi <b>ID</b> sini yuboring.\n\n"
        "📌 Keyin rol tanlanadi:\n"
        "• <b>Kontent admin</b> — serial qo'shish/o'chirish\n"
        "• <b>Menejer admin</b> — kontent + admin boshqarish\n\n"
        "<i>Bekor: /cancel</i>",
        reply_markup=InlineKeyboardMarkup(keyboard),
        parse_mode="HTML"
    )
    return ADD_ADMIN


async def adm_add_get_id(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not can_manage_admins(update.effective_user.id):
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

    if new_id in ADMIN_IDS:
        await update.message.reply_text(
            "⚠️ Bu foydalanuvchi allaqachon super admin!",
            reply_markup=InlineKeyboardMarkup(keyboard),
            parse_mode="HTML"
        )
        return ConversationHandler.END

    # Rol tanlash
    context.user_data["new_admin_id"] = new_id
    role_keyboard = [
        [InlineKeyboardButton("📝 Kontent admin", callback_data=f"adm_role_content_{new_id}")],
        [InlineKeyboardButton("👑 Menejer admin", callback_data=f"adm_role_manager_{new_id}")],
        [InlineKeyboardButton("🔙 Bekor", callback_data="admin_admins")],
    ]
    all_users = db.get_all_users()
    user_info = next((u for u in all_users if u["user_id"] == new_id), None)
    label = f"@{user_info['username']}" if user_info and user_info['username'] else str(new_id)

    await update.message.reply_text(
        f"👤 <b>{label}</b> uchun rol tanlang:\n\n"
        "📝 <b>Kontent admin</b> — serial/qism qo'shish, o'chirish\n"
        "👑 <b>Menejer admin</b> — kontent + admin qo'shish/o'chirish",
        reply_markup=InlineKeyboardMarkup(role_keyboard),
        parse_mode="HTML"
    )
    return ConversationHandler.END


async def adm_role_callback(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Rol tanlanganda"""
    query = update.callback_query
    await query.answer()
    if not can_manage_admins(query.from_user.id):
        await query.answer("❌ Ruxsat yo'q!", show_alert=True)
        return

    parts   = query.data.split("_")  # adm_role_content_12345
    role    = parts[2]               # content yoki manager
    new_id  = int(parts[3])

    all_users = db.get_all_users()
    user_info = next((u for u in all_users if u["user_id"] == new_id), None)
    username  = user_info["username"]  if user_info else ""
    full_name = user_info["full_name"] if user_info else str(new_id)

    db.add_admin(new_id, username, full_name, query.from_user.id, role)

    label    = f"@{username}" if username else full_name
    role_txt = "👑 Menejer admin" if role == "manager" else "📝 Kontent admin"

    try:
        role_info = (
            "kontent qo'shish/o'chirish" if role == "content"
            else "kontent + admin boshqarish"
        )
        await context.bot.send_message(
            new_id,
            f"🎉 <b>Tabriklaymiz!</b>\n\n"
            f"Siz bot admini qildingiz!\n"
            f"Rol: {role_txt}\n"
            f"Huquq: {role_info}\n\n"
            f"Admin paneliga kirish: /start → Admin panel",
            parse_mode="HTML"
        )
    except Exception:
        pass

    keyboard = [[InlineKeyboardButton("🔙 Adminlar", callback_data="admin_admins")]]
    await query.edit_message_text(
        f"✅ <b>{label}</b> {role_txt} qilindi!\n"
        f"🆔 <code>{new_id}</code>",
        reply_markup=InlineKeyboardMarkup(keyboard),
        parse_mode="HTML"
    )


async def adm_del_start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()
    if not can_manage_admins(query.from_user.id):
        await query.answer("❌ Ruxsat yo'q!", show_alert=True)
        return

    admins = db.get_admins()
    keyboard = []
    for a in admins:
        label    = f"@{a['username']}" if a['username'] else a['full_name'] or str(a['user_id'])
        role_txt = "👑" if a.get("role") == "manager" else "📝"
        keyboard.append([InlineKeyboardButton(
            f"🗑 {role_txt} {label}",
            callback_data=f"adm_rm_{a['user_id']}"
        )])
    keyboard.append([InlineKeyboardButton("🔙 Orqaga", callback_data="admin_admins")])

    await query.edit_message_text(
        "🗑 <b>Qaysi adminni o'chirish?</b>",
        reply_markup=InlineKeyboardMarkup(keyboard),
        parse_mode="HTML"
    )


async def adm_rm_callback(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    if not can_manage_admins(query.from_user.id):
        await query.answer("❌ Ruxsat yo'q!", show_alert=True)
        return
    user_id = int(query.data.replace("adm_rm_", ""))
    db.remove_admin(user_id)
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
        logger.error(f"Kanal qo'shishda xato: {e}")
        await msg.reply_text(
            f"❌ <b>Kanal topilmadi!</b>\n\n"
            f"Kiritilgan: <code>{text}</code>\n\n"
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
    """Website uchun xavfsiz HTTP API server"""
    import os
    import secrets
    import time
    import urllib.request

    PORT      = int(os.getenv("PORT", 8080))
    SITE_PASS = os.getenv("SITE_PASSWORD")
    if not SITE_PASS:
        raise ValueError("SITE_PASSWORD environment variable is not set!")
    BOT_TKN   = os.getenv("BOT_TOKEN", "")

    sessions  = {}   # {token: expire_time}
    fail_log  = {}   # {ip: [timestamps]}
    RATE_LIMIT  = 5
    BLOCK_TIME  = 900
    SESSION_TTL = 3600 * 8

    def make_token():
        return secrets.token_hex(32)

    def valid_session(token):
        if not token:
            return False
        exp = sessions.get(token)
        if not exp:
            return False
        if time.time() > exp:
            sessions.pop(token, None)
            return False
        return True

    def is_blocked(ip):
        now   = time.time()
        times = [t for t in fail_log.get(ip, []) if now - t < BLOCK_TIME]
        fail_log[ip] = times
        return len(times) >= RATE_LIMIT

    def record_fail(ip):
        fail_log.setdefault(ip, []).append(time.time())

    def escape(s):
        if not s:
            return ""
        return (str(s).replace("&","&amp;").replace("<","&lt;")
                      .replace(">","&gt;").replace('"',"&quot;"))

    def safe_item(item):
        return {k: escape(v) if isinstance(v, str) else v for k, v in item.items()}

    class APIHandler(BaseHTTPRequestHandler):

        def log_message(self, f, *a): pass

        def client_ip(self):
            import ipaddress
            PRIVATE = [
                ipaddress.ip_network('10.0.0.0/8'),
                ipaddress.ip_network('172.16.0.0/12'),
                ipaddress.ip_network('192.168.0.0/16'),
                ipaddress.ip_network('127.0.0.0/8'),
            ]
            def is_private(ip):
                try:
                    return any(ipaddress.ip_address(ip) in net for net in PRIVATE)
                except Exception:
                    return False

            peer = self.client_address[0]
            fwd  = self.headers.get("X-Forwarded-For", "")
            if is_private(peer) and fwd:
                return fwd.split(",")[0].strip()
            return peer

        def get_token(self):
            for p in self.headers.get("Cookie","").split(";"):
                p = p.strip()
                if p.startswith("session="):
                    return p[8:]
            return None

        def authed(self):
            return valid_session(self.get_token())

        def send_json(self, data, status=200):
            body = json.dumps(data, ensure_ascii=False).encode()
            self.send_response(status)
            self.send_header("Content-Type","application/json; charset=utf-8")
            self.send_header("X-Content-Type-Options","nosniff")
            self.send_header("X-Frame-Options","DENY")
            self.send_header("Content-Length", len(body))
            self.end_headers()
            self.wfile.write(body)

        def send_401(self):
            self.send_json({"ok":False,"error":"Autentifikatsiya talab qilinadi"},401)

        def send_file(self, path, ctype):
            try:
                with open(path,"rb") as f:
                    data = f.read()
                self.send_response(200)
                self.send_header("Content-Type", ctype)
                self.send_header("Content-Length", len(data))
                self.send_header("Cache-Control","public, max-age=86400")
                self.end_headers()
                self.wfile.write(data)
            except FileNotFoundError:
                self.send_response(404); self.end_headers()

        def send_html(self, path):
            try:
                with open(path,"rb") as f:
                    data = f.read()
                self.send_response(200)
                self.send_header("Content-Type","text/html; charset=utf-8")
                self.send_header("Content-Length", len(data))
                self.send_header("X-Frame-Options","DENY")
                self.send_header("X-Content-Type-Options","nosniff")
                self.end_headers()
                self.wfile.write(data)
            except FileNotFoundError:
                self.send_response(404); self.end_headers()

        def do_OPTIONS(self):
            self.send_response(200)
            self.send_header("Access-Control-Allow-Origin","same-origin")
            self.send_header("Access-Control-Allow-Methods","GET, POST, DELETE, OPTIONS")
            self.send_header("Access-Control-Allow-Headers","Content-Type")
            self.end_headers()

        def do_GET(self):
            p = self.path.split("?")[0]

            if p in ("/", "/index.html"):
                self.send_html("index.html"); return

            if p == "/api/hero-bg":
                self.send_file("hero-bg.png","image/png"); return

            if p == "/api/stats":
                stats = db.get_stats()
                # Foydalanuvchi sonini ommaviy ko'rsatmaymiz
                safe = {k: v for k, v in stats.items() if k != "users"}
                self.send_json(safe); return

            if p == "/api/content":
                rows = []
                for cat in ("anime","drama","kino"):
                    rows.extend(db.get_all_items(cat))
                self.send_json(rows); return

            if p.startswith("/api/episodes/"):
                parts = p.strip("/").split("/")
                if len(parts) == 4:
                    _,_,cat,code = parts
                    if cat in ("anime","drama","kino"):
                        self.send_json(db.get_episodes(cat, code.upper()))
                    else:
                        self.send_json([])
                else:
                    self.send_json([])
                return

            if p.startswith("/api/poster/"):
                import re
                file_id = p[12:].strip()
                # Telegram file_id — faqat harf, raqam, _ va - dan iborat
                if not file_id or not re.match(r'^[A-Za-z0-9_\-]{10,200}$', file_id):
                    self.send_response(400); self.end_headers(); return
                try:
                    url  = f"https://api.telegram.org/bot{BOT_TKN}/getFile?file_id={file_id}"
                    req  = urllib.request.Request(url, headers={"User-Agent":"AniStream/1.0"})
                    resp = urllib.request.urlopen(req, timeout=8)
                    data = json.loads(resp.read())
                    if data.get("ok"):
                        fp      = data["result"]["file_path"]
                        img_url = f"https://api.telegram.org/file/bot{BOT_TKN}/{fp}"
                        ir      = urllib.request.Request(img_url, headers={"User-Agent":"AniStream/1.0"})
                        ir_resp = urllib.request.urlopen(ir, timeout=10)
                        img     = ir_resp.read()
                        ctype   = ir_resp.headers.get("Content-Type","image/jpeg")
                        self.send_response(200)
                        self.send_header("Content-Type", ctype)
                        self.send_header("Content-Length", len(img))
                        self.send_header("Cache-Control","public, max-age=3600")
                        self.end_headers()
                        self.wfile.write(img)
                    else:
                        self.send_response(404); self.end_headers()
                except Exception as ex:
                    logger.error(f"Poster xato: {ex}")
                    self.send_response(502); self.end_headers()
                return

            # Himoyalangan
            if not self.authed():
                self.send_401(); return

            if p == "/api/users":
                users = db.get_recent_users(50)
                self.send_json([safe_item(u) for u in users])
            elif p == "/api/admins":
                admins = db.get_admins()
                self.send_json([safe_item(a) for a in admins])
            else:
                self.send_response(404); self.end_headers()

        def do_POST(self):
            p = self.path

            if p == "/api/login":
                ip = self.client_ip()
                if is_blocked(ip):
                    self.send_json({"ok":False,"error":"15 daqiqa kuting"},429); return
                try:
                    ln   = int(self.headers.get("Content-Length",0))
                    body = json.loads(self.rfile.read(ln) or b"{}")
                    pw   = body.get("password","")
                except Exception:
                    self.send_json({"ok":False,"error":"Noto'g'ri so'rov"},400); return
                if pw == SITE_PASS:
                    tok = make_token()
                    sessions[tok] = time.time() + SESSION_TTL
                    self.send_response(200)
                    self.send_header("Content-Type","application/json")
                    self.send_header("Set-Cookie",
                        f"session={tok}; HttpOnly; Secure; SameSite=Strict; Path=/; Max-Age={SESSION_TTL}")
                    out = json.dumps({"ok":True}).encode()
                    self.send_header("Content-Length", len(out))
                    self.end_headers()
                    self.wfile.write(out)
                else:
                    record_fail(ip)
                    self.send_json({"ok":False,"error":"Parol noto'g'ri"},401)
                return

            if p == "/api/logout":
                sessions.pop(self.get_token(), None)
                self.send_response(200)
                self.send_header("Set-Cookie","session=; HttpOnly; Max-Age=0; Path=/")
                out = json.dumps({"ok":True}).encode()
                self.send_header("Content-Length", len(out))
                self.end_headers()
                self.wfile.write(out)
                return

            if not self.authed():
                self.send_401(); return

            try:
                ln   = int(self.headers.get("Content-Length",0))
                body = json.loads(self.rfile.read(ln) or b"{}")
            except Exception:
                self.send_json({"ok":False,"error":"Noto'g'ri so'rov"},400); return

            if p == "/api/content":
                cat   = body.get("category","")
                code  = body.get("code","").upper().strip()
                title = body.get("title","").strip()
                desc  = body.get("description","").strip()
                if cat not in ("anime","drama","kino") or not code or not title:
                    self.send_json({"ok":False,"error":"Majburiy maydonlar"},400); return
                db.add_item(cat, code, title, desc)
                self.send_json({"ok":True})
            elif p == "/api/admins":
                user_id = body.get("user_id")
                role    = body.get("role","content")
                if not user_id:
                    self.send_json({"ok":False,"error":"user_id kerak"},400); return
                if role not in ("content","manager"):
                    self.send_json({"ok":False,"error":"Noto'g'ri rol"},400); return
                if int(user_id) in ADMIN_IDS:
                    self.send_json({"ok":False,"error":"Super adminni o'zgartirb bo'lmaydi"},400); return
                db.add_admin(int(user_id),"",str(user_id),ADMIN_IDS[0] if ADMIN_IDS else 0, role)
                self.send_json({"ok":True})
            else:
                self.send_response(404); self.end_headers()

        def do_DELETE(self):
            if not self.authed():
                self.send_401(); return
            p = self.path
            if p.startswith("/api/content/"):
                parts = p.strip("/").split("/")
                if len(parts) == 4:
                    _,_,cat,code = parts
                    if cat not in ("anime","drama","kino"):
                        self.send_json({"ok":False},400); return
                    self.send_json({"ok": db.delete_item(cat, code.upper())}); return
            elif p.startswith("/api/admins/"):
                parts = p.strip("/").split("/")
                if len(parts) == 3:
                    try:
                        uid = int(parts[2])
                    except ValueError:
                        self.send_json({"ok":False},400); return
                    if uid in ADMIN_IDS:
                        self.send_json({"ok":False,"error":"Super adminni o'chirib bo'lmaydi"},403); return
                    self.send_json({"ok": db.remove_admin(uid)}); return
            self.send_response(404); self.end_headers()

    server = HTTPServer(("0.0.0.0", PORT), APIHandler)
    logger.info(f"Web server {PORT}-portda ishga tushdi")
    server.serve_forever()


def main():
    app = Application.builder().token(BOT_TOKEN).build()

    # Serial qo'shish
    add_conv = ConversationHandler(
        entry_points=[CallbackQueryHandler(admin_add_start, pattern="^admin_add$")],
        states={
            ADD_CAT:    [CallbackQueryHandler(add_select_cat, pattern="^add_")],
            ADD_CODE:   [MessageHandler(filters.TEXT & ~filters.COMMAND, add_get_code)],
            ADD_TITLE:  [MessageHandler(filters.TEXT & ~filters.COMMAND, add_get_title)],
            ADD_DESC:   [MessageHandler(filters.TEXT, add_get_desc)],
            ADD_POSTER: [
                MessageHandler(filters.PHOTO | filters.Document.IMAGE | filters.TEXT, add_get_poster)
            ],
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
                # Matn yuborilsa — davom ettirish yoki bekor qilish eslatmasi
                MessageHandler(
                    filters.TEXT & ~filters.COMMAND,
                    lambda u, c: u.message.reply_text(
                        "📹 Video yoki hujjat yuboring.\n"
                        "Bekor qilish: /cancel"
                    )
                ),
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

    # Poster qo'shish
    poster_conv = ConversationHandler(
        entry_points=[CallbackQueryHandler(admin_poster_start, pattern="^admin_poster$")],
        states={
            POSTER_CODE: [
                CallbackQueryHandler(poster_action_callback, pattern="^poster_action_"),
                CallbackQueryHandler(poster_select_cat,      pattern="^poster_cat_"),
                MessageHandler(filters.TEXT & ~filters.COMMAND, poster_get_code),
            ],
            POSTER_IMG: [
                MessageHandler(filters.PHOTO | filters.Document.IMAGE, poster_get_img),
                CallbackQueryHandler(poster_select_cat, pattern="^poster_cat_"),
            ],
        },
        fallbacks=[
            CommandHandler("cancel", conv_cancel),
            CallbackQueryHandler(admin_panel,        pattern="^admin_panel$"),
            CallbackQueryHandler(admin_poster_start, pattern="^admin_poster$"),
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
    app.add_handler(poster_conv,     group=0)

    # Callback handlerlar — 1-guruhda
    app.add_handler(CallbackQueryHandler(show_category,      pattern="^cat_"),          group=1)
    app.add_handler(CallbackQueryHandler(back_main,          pattern="^back_main$"),    group=1)
    app.add_handler(CallbackQueryHandler(check_sub_callback, pattern="^check_sub$"),    group=1)
    app.add_handler(CallbackQueryHandler(admin_panel,        pattern="^admin_panel$"),  group=1)
    app.add_handler(CallbackQueryHandler(admin_stats,        pattern="^admin_stats$"),    group=1)
    app.add_handler(CallbackQueryHandler(admin_channels,     pattern="^admin_channels$"), group=1)
    app.add_handler(CallbackQueryHandler(admin_poster_start, pattern="^admin_poster$"),   group=1)
    app.add_handler(CallbackQueryHandler(poster_select_cat,  pattern="^poster_cat_"),     group=1)
    app.add_handler(CallbackQueryHandler(ch_del_start,       pattern="^ch_del$"),         group=1)
    app.add_handler(CallbackQueryHandler(ch_rm_callback,     pattern="^ch_rm_"),          group=1)
    app.add_handler(CallbackQueryHandler(admin_admins,       pattern="^admin_admins$"),   group=1)
    app.add_handler(CallbackQueryHandler(adm_del_start,      pattern="^adm_del$"),        group=1)
    app.add_handler(CallbackQueryHandler(adm_rm_callback,    pattern="^adm_rm_"),         group=1)
    app.add_handler(CallbackQueryHandler(adm_role_callback,  pattern="^adm_role_"),       group=1)
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
