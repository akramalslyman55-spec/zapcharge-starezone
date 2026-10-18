import os
from telegram import Update, InlineKeyboardButton, InlineKeyboardMarkup, WebAppInfo
from telegram.ext import Application, CommandHandler, ContextTypes

TOKEN = os.environ["BOT_TOKEN"]
WEBAPP_URL = os.environ["WEBAPP_URL"].rstrip("/")
ADMIN_IDS = {int(x) for x in os.environ.get("ADMIN_IDS", "").replace(" ", "").split(",") if x}


def register(user):
    """تسجيل كل من يضغط /start في قاعدة البيانات ليظهر عند الإدارة."""
    try:
        from database import Session, User, migrate
        migrate()
        u = Session.get(User, user.id)
        if not u:
            u = User(id=user.id, balance=0)
            Session.add(u)
        u.name = user.first_name or user.username or "زبون"
        u.username = user.username
        Session.commit()
    except Exception as e:
        print("register failed:", e)
    finally:
        try:
            Session.remove()
        except Exception:
            pass


async def start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    register(update.effective_user)
    kb = InlineKeyboardMarkup([[InlineKeyboardButton("فتح المتجر", web_app=WebAppInfo(url=WEBAPP_URL))]])
    await update.message.reply_text(
        "أهلاً بك في متجر StarZone SY × Zapcharge\nشحن فوري وأسعار مميزة وخدمة موثوقة.",
        reply_markup=kb,
    )


async def admin(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if update.effective_user.id not in ADMIN_IDS:
        return
    kb = InlineKeyboardMarkup([[InlineKeyboardButton("فتح لوحة الإدارة", web_app=WebAppInfo(url=WEBAPP_URL + "/admin"))]])
    await update.message.reply_text("لوحة الإدارة", reply_markup=kb)


app = Application.builder().token(TOKEN).build()
app.add_handler(CommandHandler("start", start))
app.add_handler(CommandHandler("admin", admin))
app.run_polling()
