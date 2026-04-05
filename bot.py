import os
import time
from dataclasses import dataclass
from typing import Dict, Optional, List, Tuple

import firebase_admin
from firebase_admin import credentials, firestore

from telegram import (
    Update,
    InlineKeyboardMarkup,
    InlineKeyboardButton,
    InputFile,
)
from telegram.ext import (
    Application,
    CommandHandler,
    CallbackQueryHandler,
    MessageHandler,
    ContextTypes,
    filters,
)

# =========================
# 0) Railway: كتابة Service Account من ENV (اختياري)
# =========================
sa_json = os.getenv("FIREBASE_SA_JSON", "").strip()
if sa_json:
    with open("serviceAccountKey.json", "w", encoding="utf-8") as f:
        f.write(sa_json)

# =========================
# 1) Firebase Admin اتصال
# =========================
cred = credentials.Certificate("serviceAccountKey.json")
if not firebase_admin._apps:
    firebase_admin.initialize_app(cred)
db = firestore.client()
print("✅ Firebase Connected")

# =========================
# 2) إعدادات البوت
# =========================
BOT_TOKEN = os.getenv("BOT_TOKEN", "").strip()

BANNER_PATH = "banner.png"  # اختياري
SESSION_MINUTES = 30

# Admin Telegram ID (من @userinfobot) — الأفضل من ENV
ADMIN_CHAT_ID = int(os.getenv("ADMIN_CHAT_ID", "0"))

# =========================
# 3) Sessions في RAM فقط
# =========================
@dataclass
class Session:
    uid: str
    expires_at: float

sessions: Dict[int, Session] = {}
login_wait: Dict[int, bool] = {}
support_wait: Dict[int, bool] = {}

def now() -> float:
    return time.time()

def set_session(telegram_id: int, uid: str):
    sessions[telegram_id] = Session(uid=uid, expires_at=now() + SESSION_MINUTES * 60)

def get_session(telegram_id: int) -> Optional[Session]:
    s = sessions.get(telegram_id)
    if not s:
        return None
    if now() > s.expires_at:
        sessions.pop(telegram_id, None)
        return None
    return s

def is_logged_in(telegram_id: int) -> bool:
    return get_session(telegram_id) is not None

def require_auth(update: Update) -> Optional[Session]:
    return get_session(update.effective_user.id)

# =========================
# 4) Cache لتسريع Firestore
# =========================
CACHE_TTL = 60  # ثانية

_user_cache: Dict[str, Tuple[float, dict]] = {}
_results_cache: Dict[str, Tuple[float, List[dict]]] = {}
_ban_cache: Dict[str, Tuple[float, Tuple[bool, str, Optional[dict]]]] = {}

def _cache_get(cache: dict, key: str):
    item = cache.get(key)
    if not item:
        return None
    ts, val = item
    if (now() - ts) > CACHE_TTL:
        cache.pop(key, None)
        return None
    return val

def _cache_set(cache: dict, key: str, val):
    cache[key] = (now(), val)

# =========================
# 5) Firestore قراءة البيانات
# =========================
def get_user(uid: str) -> Optional[dict]:
    cached = _cache_get(_user_cache, uid)
    if cached is not None:
        return cached

    doc = db.collection("users").document(uid).get()
    if not doc.exists:
        return None
    data = doc.to_dict() or {}
    data["uid"] = uid

    _cache_set(_user_cache, uid, data)
    return data

def get_results(uid: str) -> List[dict]:
    cached = _cache_get(_results_cache, uid)
    if cached is not None:
        return cached

    snap = db.collection("results").where("uid", "==", uid).stream()
    data = [d.to_dict() for d in snap]

    _cache_set(_results_cache, uid, data)
    return data

def get_ban_doc(uid: str) -> Optional[dict]:
    doc = db.collection("banned_users").document(uid).get()
    if doc.exists:
        d = doc.to_dict() or {}
        d["_source"] = "banned_users/docId"
        return d

    qs = db.collection("banned_users").where("uid", "==", uid).limit(1).stream()
    for x in qs:
        d = x.to_dict() or {}
        d["_source"] = "banned_users/where(uid)"
        d["_docId"] = x.id
        return d

    return None

def normalize_bool(v) -> bool:
    if isinstance(v, bool):
        return v
    if isinstance(v, (int, float)):
        return v != 0
    if isinstance(v, str):
        return v.strip().lower() in {"true", "1", "yes", "y", "active", "banned"}
    return False

def is_user_banned(uid: str) -> Tuple[bool, str, Optional[dict]]:
    cached = _cache_get(_ban_cache, uid)
    if cached is not None:
        return cached

    user = get_user(uid) or {}
    ban = get_ban_doc(uid)

    if ban:
        status = ban.get("status")
        active = ban.get("active")

        if status is not None:
            st = str(status).strip().lower()
            if st not in {"inactive", "expired", "released", "unbanned", "false", "0"}:
                reason = ban.get("reason") or ban.get("banReason") or "محظور"
                res = (True, str(reason), ban)
                _cache_set(_ban_cache, uid, res)
                return res

        if active is not None:
            if normalize_bool(active):
                reason = ban.get("reason") or ban.get("banReason") or "محظور"
                res = (True, str(reason), ban)
                _cache_set(_ban_cache, uid, res)
                return res
        else:
            if status is None:
                reason = ban.get("reason") or ban.get("banReason") or "محظور"
                res = (True, str(reason), ban)
                _cache_set(_ban_cache, uid, res)
                return res

    for key in ["banned", "isBanned", "ban", "examBanned", "blocked"]:
        if key in user and normalize_bool(user.get(key)):
            res = (True, f"محظور (users.{key}=true)", {"_source": f"users/{key}"})
            _cache_set(_ban_cache, uid, res)
            return res

    if "status" in user:
        st = str(user.get("status")).strip().lower()
        if st in {"banned", "blocked", "suspended"}:
            res = (True, f"محظور (users.status={user.get('status')})", {"_source": "users/status"})
            _cache_set(_ban_cache, uid, res)
            return res

    res = (False, "غير محظور", None)
    _cache_set(_ban_cache, uid, res)
    return res

# =========================
# 6) واجهة الأزرار (جميلة)
# =========================
def menu_keyboard(logged_in: bool) -> InlineKeyboardMarkup:
    if not logged_in:
        return InlineKeyboardMarkup([
            [InlineKeyboardButton("🔐 تسجيل الدخول", callback_data="GO_LOGIN")],
            [InlineKeyboardButton("💬 تواصل مع الأدمن", callback_data="GO_SUPPORT")],
        ])

    return InlineKeyboardMarkup([
        [
            InlineKeyboardButton("👤 بياناتي", callback_data="GO_PROFILE"),
            InlineKeyboardButton("📊 نتائجي", callback_data="GO_RESULTS"),
        ],
        [InlineKeyboardButton("⛔ حالة الحظر", callback_data="GO_BAN")],
        [InlineKeyboardButton("💬 تواصل مع الأدمن", callback_data="GO_SUPPORT")],
        [InlineKeyboardButton("🚪 تسجيل خروج", callback_data="GO_LOGOUT")],
    ])

# =========================
# 7) أوامر البوت
# =========================
async def start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    caption = (
        "🎓 أهلاً بك في بوت منصة التربية النوعية\n\n"
        "🔐 لتسجيل الدخول:\n"
        "اكتب /login ثم أرسل UID الخاص بحسابك\n\n"
        "📌 بعد الدخول تستطيع:\n"
        "👤 /profile بياناتك\n"
        "📊 /results نتائج الامتحانات\n"
        "⛔ /ban حالة الحظر\n"
        "💬 /support تواصل مع الأدمن\n"
        "🚪 /logout تسجيل خروج\n"
    )

    logged = is_logged_in(update.effective_user.id)

    if update.message and os.path.exists(BANNER_PATH):
        with open(BANNER_PATH, "rb") as f:
            await update.message.reply_photo(
                photo=InputFile(f),
                caption=caption,
                reply_markup=menu_keyboard(logged),
            )
    else:
        await update.effective_message.reply_text(caption, reply_markup=menu_keyboard(logged))

async def login_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    login_wait[update.effective_user.id] = True
    support_wait.pop(update.effective_user.id, None)
    await update.message.reply_text("🔐 اكتب UID بتاع حسابك على المنصة:")

async def logout_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    telegram_id = update.effective_user.id
    sessions.pop(telegram_id, None)
    login_wait.pop(telegram_id, None)
    support_wait.pop(telegram_id, None)
    await update.message.reply_text("🚪 تم تسجيل الخروج ✅\nاكتب /login للدخول تاني.", reply_markup=menu_keyboard(False))

async def profile_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    s = require_auth(update)
    if not s:
        await update.message.reply_text("⚠️ لازم تسجل دخول الأول: /login", reply_markup=menu_keyboard(False))
        return

    u = get_user(s.uid)
    if not u:
        await update.message.reply_text("❌ مش لاقي حسابك في users.", reply_markup=menu_keyboard(True))
        return

    banned, _, _ = is_user_banned(s.uid)
    status = "⛔ محظور" if banned else "✅ نشط"

    name = u.get("fullName") or u.get("name")
    if not name:
        fn = str(u.get("firstName", "")).strip()
        ln = str(u.get("lastName", "")).strip()
        name = (fn + " " + ln).strip() or "غير متاح"

    msg = (
        "👤 بياناتك:\n"
        f"🆔 UID: {s.uid}\n"
        f"🧾 الاسم: {name}\n"
        f"📧 البريد: {u.get('email','غير متاح')}\n"
        f"🎓 الرقم الجامعي: {u.get('studentId','غير متاح')}\n"
        f"🏫 القسم/الفرقة: {u.get('departmentText') or u.get('department','غير متاح')}\n"
        f"📌 الحالة الحالية: {status}\n"
    )
    await update.message.reply_text(msg, reply_markup=menu_keyboard(True))

async def results_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    s = require_auth(update)
    if not s:
        await update.message.reply_text("⚠️ لازم تسجل دخول الأول: /login", reply_markup=menu_keyboard(False))
        return

    results = get_results(s.uid)
    if not results:
        await update.message.reply_text("📊 لا توجد نتائج مسجلة لك حالياً.", reply_markup=menu_keyboard(True))
        return

    msg = "📊 نتائج الامتحانات:\n\n"
    for r in results:
        exam = r.get("examName") or r.get("exam") or r.get("subject") or "امتحان"
        score = r.get("score", r.get("mark", r.get("result", "-")))
        total = r.get("total", r.get("fullMark", "-"))
        date = r.get("date") or r.get("createdAt") or ""
        msg += f"• {exam}: {score}/{total} {f'({date})' if date else ''}\n"

    await update.message.reply_text(msg, reply_markup=menu_keyboard(True))

async def ban_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    s = require_auth(update)
    if not s:
        await update.message.reply_text("⚠️ لازم تسجل دخول الأول: /login", reply_markup=menu_keyboard(False))
        return

    banned, reason, details = is_user_banned(s.uid)
    if not banned:
        await update.message.reply_text("✅ حسابك غير محظور حالياً.", reply_markup=menu_keyboard(True))
        return

    extra = ""
    if details:
        src = details.get("_source", "unknown")
        extra += f"\n📌 مصدر الحظر: {src}"
        if details.get("from") or details.get("startDate"):
            extra += f"\n🕒 من: {details.get('from') or details.get('startDate')}"
        if details.get("to") or details.get("endDate"):
            extra += f"\n🕒 إلى: {details.get('to') or details.get('endDate')}"
        if details.get("banUntil") or details.get("banEnd"):
            extra += f"\n🕒 حتى: {details.get('banUntil') or details.get('banEnd')}"

    msg = f"⛔ حالة الحساب: محظور\n🧾 السبب: {reason}{extra}"
    await update.message.reply_text(msg, reply_markup=menu_keyboard(True))

# =========================
# 8) دعم/تواصل مع الأدمن
# =========================
async def support_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    s = require_auth(update)
    if not s:
        await update.message.reply_text("⚠️ لازم تسجل دخول الأول: /login", reply_markup=menu_keyboard(False))
        return

    if not ADMIN_CHAT_ID:
        await update.message.reply_text(
            "⚠️ ADMIN_CHAT_ID غير مضبوط.\n"
            "هات ID من @userinfobot وحطه في Railway Variables باسم ADMIN_CHAT_ID.",
            reply_markup=menu_keyboard(True)
        )
        return

    support_wait[update.effective_user.id] = True
    login_wait.pop(update.effective_user.id, None)
    await update.message.reply_text("💬 اكتب رسالتك للأدمن الآن:")

async def reply_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """الأدمن يرد على الطالب: /reply <telegram_id> <message>"""
    if update.effective_user.id != ADMIN_CHAT_ID:
        await update.message.reply_text("❌ هذا الأمر للأدمن فقط.")
        return

    if len(context.args) < 2:
        await update.message.reply_text("✅ الاستخدام:\n/reply <telegram_id> <message>")
        return

    try:
        target_id = int(context.args[0])
    except ValueError:
        await update.message.reply_text("❌ Telegram ID غير صحيح.")
        return

    msg = " ".join(context.args[1:]).strip()
    if not msg:
        await update.message.reply_text("❌ اكتب رسالة.")
        return

    await context.bot.send_message(chat_id=target_id, text=f"📩 رد الإدارة:\n{msg}")
    await update.message.reply_text("✅ تم إرسال الرد للطالب.")

# =========================
# 9) أزرار Callback
# =========================
async def on_button(update: Update, context: ContextTypes.DEFAULT_TYPE):
    q = update.callback_query
    await q.answer()

    tid = q.from_user.id
    logged = is_logged_in(tid)

    if q.data == "GO_LOGIN":
        login_wait[tid] = True
        support_wait.pop(tid, None)
        await q.message.reply_text("🔐 اكتب UID بتاع حسابك على المنصة:", reply_markup=menu_keyboard(logged))
        return

    if q.data == "GO_PROFILE":
        fake_update = Update(update.update_id, message=q.message)
        fake_update._effective_user = q.from_user
        await profile_cmd(fake_update, context)
        return

    if q.data == "GO_RESULTS":
        fake_update = Update(update.update_id, message=q.message)
        fake_update._effective_user = q.from_user
        await results_cmd(fake_update, context)
        return

    if q.data == "GO_BAN":
        fake_update = Update(update.update_id, message=q.message)
        fake_update._effective_user = q.from_user
        await ban_cmd(fake_update, context)
        return

    if q.data == "GO_SUPPORT":
        fake_update = Update(update.update_id, message=q.message)
        fake_update._effective_user = q.from_user
        await support_cmd(fake_update, context)
        return

    if q.data == "GO_LOGOUT":
        sessions.pop(tid, None)
        login_wait.pop(tid, None)
        support_wait.pop(tid, None)
        await q.message.reply_text("🚪 تم تسجيل الخروج ✅\nاكتب /login للدخول تاني.", reply_markup=menu_keyboard(False))
        return

# =========================
# 10) Handler واحد للنصوص
# =========================
async def handle_text(update: Update, context: ContextTypes.DEFAULT_TYPE):
    telegram_id = update.effective_user.id
    text = (update.message.text or "").strip()
    if not text:
        return

    # (1) Login flow
    if login_wait.get(telegram_id):
        login_wait.pop(telegram_id, None)
        uid = text

        user = get_user(uid)
        if not user:
            await update.message.reply_text("❌ UID غير صحيح أو الحساب غير موجود.\nاكتب /login وحاول تاني.", reply_markup=menu_keyboard(False))
            return

        set_session(telegram_id, uid)
        name = user.get("fullName") or user.get("name") or "طالب"
        await update.message.reply_text(
            f"✅ تم تسجيل الدخول بنجاح\nمرحباً: {name}",
            reply_markup=menu_keyboard(True)
        )
        return

    # (2) Support flow
    if support_wait.get(telegram_id):
        support_wait.pop(telegram_id, None)

        s = require_auth(update)
        if not s:
            await update.message.reply_text("⏳ انتهت الجلسة. سجل دخول تاني /login", reply_markup=menu_keyboard(False))
            return

        user_doc = get_user(s.uid) or {}
        name = user_doc.get("fullName") or user_doc.get("name") or "طالب"
        email = user_doc.get("email", "غير متاح")

        admin_msg = (
            "📩 رسالة دعم جديدة\n"
            f"👤 الاسم: {name}\n"
            f"🆔 UID: {s.uid}\n"
            f"📧 البريد: {email}\n"
            f"📨 Telegram ID: {telegram_id}\n\n"
            "💬 الرسالة:\n"
            f"{text}\n\n"
            "↩️ للرد على الطالب:\n"
            f"/reply {telegram_id} <اكتب ردك هنا>"
        )

        try:
            await context.bot.send_message(chat_id=ADMIN_CHAT_ID, text=admin_msg)
            await update.message.reply_text("✅ تم إرسال رسالتك للأدمن بنجاح.", reply_markup=menu_keyboard(True))
        except Exception as e:
            await update.message.reply_text(f"❌ حصل خطأ أثناء الإرسال للأدمن:\n{e}", reply_markup=menu_keyboard(True))
        return

    # (3) default
    logged = is_logged_in(telegram_id)
    await update.message.reply_text("اختر من القائمة 👇", reply_markup=menu_keyboard(logged))

# =========================
# 11) تشغيل البوت
# =========================
def main():
    if not BOT_TOKEN:
        raise RuntimeError("❌ BOT_TOKEN غير موجود. ضعه في Railway Variables باسم BOT_TOKEN")

    app = Application.builder().token(BOT_TOKEN).build()

    app.add_handler(CommandHandler("start", start))
    app.add_handler(CommandHandler("login", login_cmd))
    app.add_handler(CommandHandler("profile", profile_cmd))
    app.add_handler(CommandHandler("results", results_cmd))
    app.add_handler(CommandHandler("ban", ban_cmd))
    app.add_handler(CommandHandler("support", support_cmd))
    app.add_handler(CommandHandler("logout", logout_cmd))

    # رد الأدمن
    app.add_handler(CommandHandler("reply", reply_cmd))

    app.add_handler(CallbackQueryHandler(on_button))
    app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, handle_text))

    print("✅ Telegram Bot Running...")
    app.run_polling()

if __name__ == "__main__":
    main()