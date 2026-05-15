import os
import csv
import hmac
import base64
import hashlib
import sqlite3
import requests
import tempfile

from datetime import datetime, timezone
from zoneinfo import ZoneInfo

from telegram import Update
from telegram.constants import ParseMode
from telegram.helpers import mention_html
from telegram.ext import (
    Application,
    CommandHandler,
    ChatJoinRequestHandler,
    MessageHandler,
    ContextTypes,
    filters,
    Defaults,
)

# ─────────────────────────────
# ENV
# ─────────────────────────────
BOT_TOKEN = os.environ["BOT_TOKEN"]
VIP_CHAT_ID = int(os.environ["VIP_CHAT_ID"])

OKX_API_KEY = os.environ["OKX_API_KEY"]
OKX_API_SECRET = os.environ["OKX_API_SECRET"]
OKX_API_PASSPHRASE = os.environ["OKX_API_PASSPHRASE"]

BYPASS_CODE = os.environ.get("BYPASS_CODE", "00000000010101010")
ADMIN_IDS = [int(x.strip()) for x in os.environ.get("ADMIN_IDS", "").split(",") if x.strip().isdigit()]

# En Render usar:
# DB_PATH=/var/data/trader_rolo_bot.db
DB_PATH = os.environ.get("DB_PATH", "/var/data/trader_rolo_bot.db")

TZ_AR = ZoneInfo("America/Argentina/Buenos_Aires")
GROUP_NAME = "Futuros Traders VIP by OKX"


# ─────────────────────────────
# DATABASE
# ─────────────────────────────
def db():
    db_dir = os.path.dirname(DB_PATH)
    if db_dir:
        os.makedirs(db_dir, exist_ok=True)

    conn = sqlite3.connect(DB_PATH, check_same_thread=False)
    conn.row_factory = sqlite3.Row
    return conn


def init_db():
    conn = db()
    cur = conn.cursor()

    cur.execute("""
    CREATE TABLE IF NOT EXISTS users (
        telegram_id INTEGER PRIMARY KEY,
        uid TEXT,
        first_name TEXT,
        username TEXT,
        joined_at TEXT NOT NULL,
        last_vol_month REAL DEFAULT 0,
        last_checked_at TEXT
    )
    """)

    conn.commit()
    conn.close()

    print(f"✅ DB inicializada en: {DB_PATH}")


def save_user(telegram_id, uid, first_name=None, username=None, last_vol_month=0):
    conn = db()
    cur = conn.cursor()

    now = datetime.now(timezone.utc).isoformat()

    cur.execute("""
        INSERT OR REPLACE INTO users (
            telegram_id,
            uid,
            first_name,
            username,
            joined_at,
            last_vol_month,
            last_checked_at
        )
        VALUES (?, ?, ?, ?, ?, ?, ?)
    """, (
        telegram_id,
        uid,
        first_name,
        username,
        now,
        float(last_vol_month or 0),
        now
    ))

    conn.commit()
    conn.close()

    print(f"✅ Usuario guardado: TG={telegram_id} UID={uid} VOL={last_vol_month}")


def update_user_volume_by_uid(uid, volume):
    conn = db()
    cur = conn.cursor()

    now = datetime.now(timezone.utc).isoformat()

    cur.execute("""
        UPDATE users
        SET last_vol_month = ?, last_checked_at = ?
        WHERE uid = ?
    """, (float(volume or 0), now, uid))

    conn.commit()
    conn.close()


def get_user_by_telegram_id(telegram_id):
    conn = db()
    cur = conn.cursor()

    cur.execute("SELECT * FROM users WHERE telegram_id = ?", (telegram_id,))
    row = cur.fetchone()

    conn.close()
    return row


def get_all_users():
    conn = db()
    cur = conn.cursor()

    cur.execute("""
        SELECT telegram_id, uid, first_name, username, joined_at, last_vol_month, last_checked_at
        FROM users
        ORDER BY joined_at ASC
    """)
    rows = cur.fetchall()

    conn.close()
    return rows


# ─────────────────────────────
# OKX
# ─────────────────────────────
def get_okx_server_time_iso():
    r = requests.get("https://www.okx.com/api/v5/public/time", timeout=10)
    ts_ms = r.json()["data"][0]["ts"]
    dt = datetime.fromtimestamp(int(ts_ms) / 1000, tz=timezone.utc)
    return dt.strftime("%Y-%m-%dT%H:%M:%S.%f")[:-3] + "Z"


def sign_okx(method, path, body=""):
    timestamp = get_okx_server_time_iso()
    message = timestamp + method + path + body

    mac = hmac.new(
        OKX_API_SECRET.encode(),
        msg=message.encode(),
        digestmod=hashlib.sha256
    )

    signature = base64.b64encode(mac.digest()).decode()
    return timestamp, signature


def okx_affiliate_detail(uid):
    path = f"/api/v5/affiliate/invitee/detail?uid={uid}"
    ts, signature = sign_okx("GET", path)

    headers = {
        "OK-ACCESS-KEY": OKX_API_KEY,
        "OK-ACCESS-SIGN": signature,
        "OK-ACCESS-TIMESTAMP": ts,
        "OK-ACCESS-PASSPHRASE": OKX_API_PASSPHRASE,
        "Content-Type": "application/json"
    }

    url = "https://www.okx.com" + path
    return requests.get(url, headers=headers, timeout=15).json()


def get_uid_volume(uid):
    resp = okx_affiliate_detail(uid)

    if resp.get("code") != "0" or not resp.get("data"):
        return None

    data = resp["data"][0]
    vol_month = float(data.get("volMonth") or 0)

    return vol_month


# ─────────────────────────────
# MENSAJES
# ─────────────────────────────
def group_welcome_text(user):
    return (
        f"🚀 Bienvenido {mention_html(user.id, user.first_name)} "
        f"al grupo {GROUP_NAME}.\n\n"
        "Aquí encontrarás análisis y entradas en el mercado, una comunidad de trading siempre activa y bots de trading exclusivos "
        "además de bonos y sorteos especiales operando en OKX.\n\n"
        "¡Saludos y buenos trades! 📈"
    )


def private_rules_text(user):
    return (
        f"🚀 Bienvenido {mention_html(user.id, user.first_name)} "
        f"al grupo {GROUP_NAME}.\n\n"
        "Aquí encontrarás análisis y entradas en el mercado, Bots de trading exclusivos, "
        "además de bonos y sorteos especiales operando en OKX.\n\n"
        "📌 Para pertenecer a esta comunidad VIP debes generar al menos "
        "25.000 USDT de volumen mensual operando en OKX.\n\n"
        "Activa tu trade en OKX y evita ser expulsado del grupo VIP. "
        "Puedes consultar tu volumen mensual escribiendo /volumen en este bot.\n\n"
        "¡Saludos y buenos trades! 📈"
    )


# ─────────────────────────────
# TELEGRAM HANDLERS
# ─────────────────────────────
async def start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text(
        f"👋 Bienvenido a {GROUP_NAME}.\n\n"
        "Solicita el acceso al grupo y envíame tu UID de OKX por privado para validar tu ingreso."
    )


async def on_join_request(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user = update.chat_join_request.from_user

    try:
        await context.bot.send_message(
            chat_id=user.id,
            text=(
                f"📌 Bienvenido a {GROUP_NAME}.\n\n"
                "Para validar tu acceso, envíame tu UID de OKX usando solo números."
            )
        )
    except Exception as e:
        print(f"⚠️ No se pudo enviar DM al usuario {user.id}: {e}")


async def handle_private(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user = update.message.from_user
    text = update.message.text.strip()

    if text == BYPASS_CODE:
        await context.bot.approve_chat_join_request(VIP_CHAT_ID, user.id)

        await context.bot.send_message(
            chat_id=VIP_CHAT_ID,
            text=group_welcome_text(user),
            parse_mode=ParseMode.HTML
        )

        await context.bot.send_message(
            chat_id=user.id,
            text=private_rules_text(user),
            parse_mode=ParseMode.HTML
        )

        return

    if not text.isnumeric():
        await update.message.reply_text("Envía solo tu UID numérico.")
        return

    uid = text
    resp = okx_affiliate_detail(uid)

    if resp.get("code") != "0" or not resp.get("data"):
        await update.message.reply_text("UID no válido. Verifica el número e intenta nuevamente.")
        return

    vol_month = float(resp["data"][0].get("volMonth") or 0)

    save_user(
        telegram_id=user.id,
        uid=uid,
        first_name=user.first_name,
        username=user.username,
        last_vol_month=vol_month
    )

    await context.bot.approve_chat_join_request(VIP_CHAT_ID, user.id)

    await context.bot.send_message(
        chat_id=user.id,
        text="✔️ UID verificado correctamente. Acceso aprobado."
    )

    await context.bot.send_message(
        chat_id=user.id,
        text=private_rules_text(user),
        parse_mode=ParseMode.HTML
    )

    await context.bot.send_message(
        chat_id=VIP_CHAT_ID,
        text=group_welcome_text(user),
        parse_mode=ParseMode.HTML
    )


# ─────────────────────────────
# USUARIO: CONSULTAR VOLUMEN
# ─────────────────────────────
async def volumen(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if update.effective_chat.type != "private":
        await update.message.reply_text(
            "Para consultar tu volumen, escríbeme por privado y usa /volumen."
        )
        return

    user = update.message.from_user
    row = get_user_by_telegram_id(user.id)

    if not row or not row["uid"]:
        await update.message.reply_text(
            "❌ No encontré un UID registrado para tu usuario.\n\n"
            "Primero debes validar tu acceso enviando tu UID de OKX."
        )
        return

    uid = row["uid"]

    try:
        vol_month = get_uid_volume(uid)

        if vol_month is None:
            await update.message.reply_text(
                "❌ No pude consultar tu volumen en OKX en este momento.\n"
                "Intenta nuevamente más tarde."
            )
            return

        update_user_volume_by_uid(uid, vol_month)

        await update.message.reply_text(
            "📊 Volumen mensual OKX\n\n"
            f"UID: {uid}\n"
            f"Volumen acumulado del mes: {vol_month:.0f} USDT\n\n"
            "Este volumen corresponde al mes en curso, contando desde el día 1 "
            "hasta el día 30 del mes.\n\n"
            "🎯 Objetivo mínimo comunidad VIP: 50.000 USDT mensuales."
        )

    except Exception as e:
        print(f"Error consultando volumen para TG={user.id}: {e}")

        await update.message.reply_text(
            "❌ Hubo un error consultando tu volumen.\n"
            "Intenta nuevamente más tarde."
        )


# ─────────────────────────────
# ADMIN: CONSULTAR VOLUMEN POR UID
# ─────────────────────────────
async def voluid(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if update.effective_chat.type != "private":
        await update.message.reply_text(
            "Por seguridad, usa este comando en el chat privado con el bot."
        )
        return

    admin_id = update.message.from_user.id

    if admin_id not in ADMIN_IDS:
        await update.message.reply_text("❌ No autorizado.")
        return

    if not context.args:
        await update.message.reply_text("Uso: /voluid 123456789")
        return

    uid = context.args[0].strip()

    if not uid.isnumeric():
        await update.message.reply_text("UID inválido. Usa solo números.")
        return

    try:
        vol_month = get_uid_volume(uid)

        if vol_month is None:
            await update.message.reply_text(
                "❌ No pude consultar ese UID en OKX."
            )
            return

        update_user_volume_by_uid(uid, vol_month)

        await update.message.reply_text(
            "📊 Consulta admin por UID\n\n"
            f"UID: {uid}\n"
            f"Volumen acumulado del mes: {vol_month:.0f} USDT"
        )

    except Exception as e:
        print(f"Error admin consultando UID={uid}: {e}")
        await update.message.reply_text("❌ Error consultando el UID.")


# ─────────────────────────────
# ADMIN: DESCARGAR INFORME CSV
# ─────────────────────────────
async def informe(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if update.effective_chat.type != "private":
        await update.message.reply_text(
            "Por seguridad, usa /informe en el chat privado con el bot."
        )
        return

    admin_id = update.message.from_user.id

    if admin_id not in ADMIN_IDS:
        await update.message.reply_text("❌ No autorizado.")
        return

    users = get_all_users()

    if not users:
        await update.message.reply_text("No hay usuarios registrados todavía.")
        return

    updated_rows = []

    for row in users:
        uid = row["uid"]
        vol_month = row["last_vol_month"] or 0

        try:
            fresh_vol = get_uid_volume(uid)
            if fresh_vol is not None:
                vol_month = fresh_vol
                update_user_volume_by_uid(uid, fresh_vol)
        except Exception as e:
            print(f"⚠️ No se pudo actualizar UID={uid}: {e}")

        updated_rows.append({
            "telegram_id": row["telegram_id"],
            "first_name": row["first_name"] or "",
            "username": row["username"] or "",
            "uid": uid,
            "volumen_mes_usdt": vol_month,
            "joined_at": row["joined_at"],
            "last_checked_at": datetime.now(timezone.utc).isoformat()
        })

    filename = f"informe_trader_rolo_{datetime.now(TZ_AR).strftime('%Y_%m_%d_%H_%M')}.csv"
    filepath = os.path.join(tempfile.gettempdir(), filename)

    with open(filepath, mode="w", newline="", encoding="utf-8") as f:
        writer = csv.DictWriter(
            f,
            fieldnames=[
                "telegram_id",
                "first_name",
                "username",
                "uid",
                "volumen_mes_usdt",
                "joined_at",
                "last_checked_at"
            ]
        )
        writer.writeheader()
        writer.writerows(updated_rows)

    await context.bot.send_document(
        chat_id=admin_id,
        document=open(filepath, "rb"),
        filename=filename,
        caption="📄 Informe de usuarios y volumen mensual Trader Rolo / OKX"
    )


# ─────────────────────────────
# MAIN
# ─────────────────────────────
def main():
    init_db()

    defaults = Defaults(tzinfo=timezone.utc)
    app = Application.builder().token(BOT_TOKEN).defaults(defaults).build()

    app.add_handler(CommandHandler("start", start))
    app.add_handler(CommandHandler("volumen", volumen))
    app.add_handler(CommandHandler("voluid", voluid))
    app.add_handler(CommandHandler("informe", informe))

    app.add_handler(ChatJoinRequestHandler(on_join_request))
    app.add_handler(MessageHandler(filters.ChatType.PRIVATE & filters.TEXT, handle_private))

    print(f"🤖 BOT {GROUP_NAME} iniciado.")
    print(f"📁 DB_PATH: {DB_PATH}")

    app.run_polling()


if __name__ == "__main__":
    main()
