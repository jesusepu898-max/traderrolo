import os
import csv
import hmac
import json
import time
import base64
import hashlib
import sqlite3
import requests
import tempfile

from urllib.parse import urlencode
from datetime import datetime, timezone, timedelta
from zoneinfo import ZoneInfo

from telegram import (
    Update,
    InlineKeyboardButton,
    InlineKeyboardMarkup,
)
from telegram.constants import ParseMode
from telegram.helpers import mention_html
from telegram.error import TelegramError
from telegram.ext import (
    Application,
    CommandHandler,
    CallbackQueryHandler,
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
ADMIN_IDS = [
    int(x.strip())
    for x in os.environ.get("ADMIN_IDS", "").split(",")
    if x.strip().isdigit()
]

# En Render usar:
# DB_PATH=/var/data/flanders_fred_bot.db
DB_PATH = os.environ.get("DB_PATH", "/var/data/flanders_fred_bot.db")

# Opcional en Render:
# VIP_GROUP_LINK=https://t.me/+TU_LINK_DEL_GRUPO_VIP
VIP_GROUP_LINK = os.environ.get("VIP_GROUP_LINK", "").strip()

TZ_AR = ZoneInfo("America/Argentina/Buenos_Aires")
GROUP_NAME = "Comunidad Flanders y Fred VIP by OKX"

OKX_BASE_URL = "https://www.okx.com"
INSTRUMENTS_CACHE = {}

OKX_JOIN_LINK = "https://www.okx.com/join/FLANDERSYFRED"
REBIND_FORM_LINK = "https://www.okx.com/ul/J6l2R5"
FLANDERS_PRIVATE_LINK = "https://t.me/ivandp93"

VALID_REF_CODES_TEXT = (
    "71790605\n"
    "27221066\n"
    "FLANDERSYFRED\n"
    "ELTRADERROLO"
)

# ─────────────────────────────
# UTILS
# ─────────────────────────────
def is_admin(user_id: int) -> bool:
    return user_id in ADMIN_IDS


def money(value):
    try:
        return f"{float(value or 0):,.2f}"
    except Exception:
        return "0.00"


def number(value):
    try:
        return f"{float(value or 0):,.0f}"
    except Exception:
        return "0"


def safe_float(value):
    try:
        return float(value or 0)
    except Exception:
        return 0.0


def split_uids(raw_text: str):
    raw_text = raw_text.replace(",", " ").replace("\n", " ").replace(";", " ")
    return [x.strip() for x in raw_text.split() if x.strip().isnumeric()]


def ts_to_human(value):
    if value in [None, ""]:
        return ""

    value = str(value).strip()

    if not value:
        return ""

    try:
        if value.isdigit() and len(value) >= 12:
            dt = datetime.fromtimestamp(int(value) / 1000, tz=timezone.utc)
            return dt.astimezone(TZ_AR).strftime("%Y-%m-%d %H:%M:%S")

        if value.isdigit() and len(value) == 10:
            dt = datetime.fromtimestamp(int(value), tz=timezone.utc)
            return dt.astimezone(TZ_AR).strftime("%Y-%m-%d %H:%M:%S")
    except Exception:
        pass

    return value


def format_optional_usdt(value):
    if value is None or value == "":
        return "No disponible"
    return f"{money(value)} USDT"


def now_utc():
    return datetime.now(timezone.utc).isoformat()


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


def ensure_column(cur, table_name, column_name, column_sql):
    cur.execute(f"PRAGMA table_info({table_name})")
    columns = [row[1] for row in cur.fetchall()]

    if column_name not in columns:
        cur.execute(f"ALTER TABLE {table_name} ADD COLUMN {column_sql}")


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

    ensure_column(cur, "users", "flow", "flow TEXT DEFAULT ''")
    ensure_column(cur, "users", "status", "status TEXT DEFAULT 'pending'")
    ensure_column(cur, "users", "last_interaction_at", "last_interaction_at TEXT")
    ensure_column(cur, "users", "source", "source TEXT DEFAULT ''")

    conn.commit()
    conn.close()

    print(f"✅ DB inicializada en: {DB_PATH}")


def save_user_profile(
    telegram_id,
    first_name=None,
    username=None,
    flow=None,
    status="pending",
    source=""
):
    conn = db()
    cur = conn.cursor()

    now = now_utc()

    existing = get_user_by_telegram_id(telegram_id)

    if existing:
        current_uid = existing["uid"]
        current_joined_at = existing["joined_at"]
        current_flow = flow if flow is not None else existing["flow"]
        current_status = status if status is not None else existing["status"]

        cur.execute("""
            UPDATE users
            SET first_name = ?,
                username = ?,
                joined_at = ?,
                flow = ?,
                status = ?,
                last_interaction_at = ?,
                source = ?
            WHERE telegram_id = ?
        """, (
            first_name,
            username,
            current_joined_at,
            current_flow,
            current_status,
            now,
            source,
            telegram_id
        ))
    else:
        cur.execute("""
            INSERT INTO users (
                telegram_id,
                uid,
                first_name,
                username,
                joined_at,
                last_vol_month,
                last_checked_at,
                flow,
                status,
                last_interaction_at,
                source
            )
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
        """, (
            telegram_id,
            None,
            first_name,
            username,
            now,
            0,
            None,
            flow or "",
            status or "pending",
            now,
            source
        ))

    conn.commit()
    conn.close()


def save_user_validated(
    telegram_id,
    uid,
    first_name=None,
    username=None,
    last_vol_month=0,
    flow="",
    source=""
):
    conn = db()
    cur = conn.cursor()

    now = now_utc()
    existing = get_user_by_telegram_id(telegram_id)
    joined_at = existing["joined_at"] if existing else now

    cur.execute("""
        INSERT OR REPLACE INTO users (
            telegram_id,
            uid,
            first_name,
            username,
            joined_at,
            last_vol_month,
            last_checked_at,
            flow,
            status,
            last_interaction_at,
            source
        )
        VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
    """, (
        telegram_id,
        uid,
        first_name,
        username,
        joined_at,
        float(last_vol_month or 0),
        now,
        flow or "",
        "validated",
        now,
        source
    ))

    conn.commit()
    conn.close()

    print(f"✅ Usuario validado: TG={telegram_id} UID={uid} FLOW={flow}")


def mark_user_not_affiliated(telegram_id, first_name=None, username=None, flow="", source=""):
    conn = db()
    cur = conn.cursor()

    now = now_utc()
    existing = get_user_by_telegram_id(telegram_id)

    if existing:
        cur.execute("""
            UPDATE users
            SET first_name = ?,
                username = ?,
                flow = ?,
                status = ?,
                last_interaction_at = ?,
                source = ?
            WHERE telegram_id = ?
        """, (
            first_name,
            username,
            flow or existing["flow"],
            "not_affiliated",
            now,
            source,
            telegram_id
        ))
    else:
        cur.execute("""
            INSERT INTO users (
                telegram_id,
                uid,
                first_name,
                username,
                joined_at,
                last_vol_month,
                last_checked_at,
                flow,
                status,
                last_interaction_at,
                source
            )
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
        """, (
            telegram_id,
            None,
            first_name,
            username,
            now,
            0,
            now,
            flow,
            "not_affiliated",
            now,
            source
        ))

    conn.commit()
    conn.close()


def update_user_volume_by_uid(uid, volume):
    conn = db()
    cur = conn.cursor()

    now = now_utc()

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


def get_user_by_uid(uid):
    conn = db()
    cur = conn.cursor()

    cur.execute("SELECT * FROM users WHERE uid = ?", (uid,))
    row = cur.fetchone()

    conn.close()
    return row


def get_all_users():
    conn = db()
    cur = conn.cursor()

    cur.execute("""
        SELECT telegram_id, uid, first_name, username, joined_at, last_vol_month,
               last_checked_at, flow, status, last_interaction_at, source
        FROM users
        ORDER BY joined_at ASC
    """)
    rows = cur.fetchall()

    conn.close()
    return rows


def get_uid_status_counts():
    conn = db()
    cur = conn.cursor()

    cur.execute("""
        SELECT
            COUNT(*) AS total,
            SUM(CASE WHEN uid IS NOT NULL AND uid != '' THEN 1 ELSE 0 END) AS con_uid,
            SUM(CASE WHEN uid IS NULL OR uid = '' THEN 1 ELSE 0 END) AS sin_uid,
            SUM(CASE WHEN status = 'validated' THEN 1 ELSE 0 END) AS validados,
            SUM(CASE WHEN status = 'not_affiliated' THEN 1 ELSE 0 END) AS no_afiliados,
            SUM(CASE WHEN status = 'pending' THEN 1 ELSE 0 END) AS pendientes
        FROM users
    """)
    row = cur.fetchone()

    conn.close()
    return row


# ─────────────────────────────
# OKX BASE
# ─────────────────────────────
def get_okx_server_time_iso():
    r = requests.get(f"{OKX_BASE_URL}/api/v5/public/time", timeout=10)
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


def okx_get(path):
    ts, signature = sign_okx("GET", path)

    headers = {
        "OK-ACCESS-KEY": OKX_API_KEY,
        "OK-ACCESS-SIGN": signature,
        "OK-ACCESS-TIMESTAMP": ts,
        "OK-ACCESS-PASSPHRASE": OKX_API_PASSPHRASE,
        "Content-Type": "application/json"
    }

    url = OKX_BASE_URL + path
    return requests.get(url, headers=headers, timeout=20).json()


def okx_public_get(path):
    url = OKX_BASE_URL + path
    return requests.get(url, timeout=20).json()


# ─────────────────────────────
# OKX AFFILIATE
# ─────────────────────────────
def okx_affiliate_detail(uid):
    path = f"/api/v5/affiliate/invitee/detail?uid={uid}"
    return okx_get(path)


def is_uid_affiliated(uid):
    resp = okx_affiliate_detail(uid)

    print("UID CONSULTADO:", uid)
    print("RESPUESTA OKX:", resp)

    if resp.get("code") == "0" and resp.get("data"):
        data = resp["data"][0]
        vol_month = safe_float(data.get("volMonth"))
        return True, data, vol_month, resp

    return False, None, 0, resp


def parse_okx_invitee_detail(resp):
    if resp.get("code") != "0" or not resp.get("data"):
        return None

    data = resp["data"][0]

    def pick(*keys, default=""):
        for key in keys:
            value = data.get(key)
            if value not in [None, ""]:
                return value
        return default

    vol_month = float(pick("volMonth", default=0) or 0)
    total_vol = float(pick("totalVol", "totalVolume", default=0) or 0)
    dep_amt = float(pick("depAmt", "depositAmt", "totalDepAmt", default=0) or 0)
    wd_amt = float(pick("wdAmt", "withdrawAmt", default=0) or 0)

    dep_15d = pick(
        "depAmt15d",
        "depAmt15D",
        "deposit15d",
        "deposit15D",
        "depAmtLast15Days",
        "depositLast15Days",
        default=""
    )

    vol_7d = pick(
        "vol7d",
        "vol7D",
        "volume7d",
        "volume7D",
        "tradeVol7d",
        "tradeVol7D",
        "volLast7Days",
        "volumeLast7Days",
        default=""
    )

    try:
        dep_15d = float(dep_15d) if dep_15d not in ["", None] else None
    except Exception:
        dep_15d = None

    try:
        vol_7d = float(vol_7d) if vol_7d not in ["", None] else None
    except Exception:
        vol_7d = None

    first_trade_time = pick(
        "firstTradeTime",
        "firstTradeTs",
        "firstTradeDate",
        "firstTradeAt"
    )

    register_time = pick(
        "joinTime",
        "registerTime",
        "regTime",
        "registerDate",
        "createTime",
        "kycTime"
    )

    kyc_time = pick("kycTime", "kycDate")
    affiliate_code = pick("affiliateCode", "affCode", "referralCode")
    region = pick("region", "country", "areaCode")
    invitee_level = pick("inviteeLevel", "level")

    did_first_trade = bool(first_trade_time) or vol_month > 0 or total_vol > 0

    return {
        "vol_month": vol_month,
        "total_vol": total_vol,
        "dep_amt": dep_amt,
        "wd_amt": wd_amt,
        "dep_15d": dep_15d,
        "vol_7d": vol_7d,
        "first_trade_time": ts_to_human(first_trade_time),
        "register_time": ts_to_human(register_time),
        "kyc_time": ts_to_human(kyc_time),
        "affiliate_code": affiliate_code,
        "region": region,
        "invitee_level": invitee_level,
        "did_first_trade": did_first_trade,
        "raw": data
    }


def get_uid_volume(uid):
    resp = okx_affiliate_detail(uid)

    if resp.get("code") != "0" or not resp.get("data"):
        return None

    data = resp["data"][0]
    vol_month = float(data.get("volMonth") or 0)

    return vol_month


def get_uid_report(uid):
    resp = okx_affiliate_detail(uid)
    parsed = parse_okx_invitee_detail(resp)

    if parsed is None:
        return None

    local_user = get_user_by_uid(uid)

    return {
        "uid": uid,
        "is_affiliate": True,
        "is_local_community": local_user is not None,
        "telegram_id": local_user["telegram_id"] if local_user else "",
        "first_name": local_user["first_name"] if local_user else "",
        "username": local_user["username"] if local_user else "",
        "joined_at": local_user["joined_at"] if local_user else "",
        "status": local_user["status"] if local_user else "",
        "flow": local_user["flow"] if local_user else "",
        **parsed
    }


# ─────────────────────────────
# ADMIN: VOLUMEN CUENTA MAESTRA
# ─────────────────────────────
def okx_trade_fills_history(inst_type, begin_ms, end_ms, after=None, limit=100):
    params = {
        "instType": inst_type,
        "begin": str(begin_ms),
        "end": str(end_ms),
        "limit": str(limit)
    }

    if after:
        params["after"] = str(after)

    path = "/api/v5/trade/fills-history?" + urlencode(params)
    return okx_get(path)


def get_instrument_info(inst_type, inst_id):
    cache_key = f"{inst_type}:{inst_id}"

    if cache_key in INSTRUMENTS_CACHE:
        return INSTRUMENTS_CACHE[cache_key]

    params = {
        "instType": inst_type,
        "instId": inst_id
    }

    path = "/api/v5/public/instruments?" + urlencode(params)
    resp = okx_public_get(path)

    if resp.get("code") == "0" and resp.get("data"):
        info = resp["data"][0]
        INSTRUMENTS_CACHE[cache_key] = info
        return info

    INSTRUMENTS_CACHE[cache_key] = {}
    return {}


def estimate_fill_volume_usdt(fill):
    inst_type = fill.get("instType", "")
    inst_id = fill.get("instId", "")

    fill_sz = safe_float(fill.get("fillSz"))
    fill_px = safe_float(fill.get("fillPx"))

    if fill_sz <= 0 or fill_px <= 0:
        return 0.0

    if inst_type in ["SPOT", "MARGIN"]:
        return fill_sz * fill_px

    inst_info = get_instrument_info(inst_type, inst_id)

    ct_val = safe_float(inst_info.get("ctVal"))
    ct_val_ccy = str(inst_info.get("ctValCcy") or "").upper()

    if ct_val <= 0:
        return fill_sz * fill_px

    if ct_val_ccy in ["USD", "USDT", "USDC"]:
        return fill_sz * ct_val

    return fill_sz * ct_val * fill_px


def parse_period_to_days(args):
    if not args:
        return 7

    raw = args[0].lower().strip()
    raw = raw.replace("d", "")
    raw = raw.replace("dias", "")
    raw = raw.replace("días", "")
    raw = raw.replace("dia", "")
    raw = raw.replace("día", "")

    try:
        days = int(raw)
    except Exception:
        return None

    if days <= 0:
        return None

    if days > 90:
        days = 90

    return days


def get_master_trading_volume(days=7):
    end_dt = datetime.now(timezone.utc)
    begin_dt = end_dt - timedelta(days=days)

    begin_ms = int(begin_dt.timestamp() * 1000)
    end_ms = int(end_dt.timestamp() * 1000)

    inst_types = ["SPOT", "MARGIN", "SWAP", "FUTURES", "OPTION"]

    total_volume = 0.0
    total_fills = 0

    by_type = {
        "SPOT": 0.0,
        "MARGIN": 0.0,
        "SWAP": 0.0,
        "FUTURES": 0.0,
        "OPTION": 0.0
    }

    errors = []

    for inst_type in inst_types:
        after = None
        pages = 0

        while True:
            pages += 1

            if pages > 20:
                break

            try:
                resp = okx_trade_fills_history(
                    inst_type=inst_type,
                    begin_ms=begin_ms,
                    end_ms=end_ms,
                    after=after,
                    limit=100
                )
            except Exception as e:
                errors.append(f"{inst_type}: {e}")
                break

            if resp.get("code") != "0":
                errors.append(f"{inst_type}: code={resp.get('code')} msg={resp.get('msg')}")
                break

            data = resp.get("data") or []

            if not data:
                break

            for fill in data:
                vol = estimate_fill_volume_usdt(fill)
                total_volume += vol
                by_type[inst_type] += vol
                total_fills += 1

            last_bill_id = data[-1].get("billId")

            if not last_bill_id:
                break

            after = last_bill_id

            time.sleep(0.15)

            if len(data) < 100:
                break

    return {
        "days": days,
        "begin": begin_dt.astimezone(TZ_AR).strftime("%Y-%m-%d %H:%M:%S"),
        "end": end_dt.astimezone(TZ_AR).strftime("%Y-%m-%d %H:%M:%S"),
        "total_volume": total_volume,
        "total_fills": total_fills,
        "by_type": by_type,
        "errors": errors
    }


def format_master_volume_report(report):
    errors_text = ""

    if report["errors"]:
        errors_text = "\n\n⚠️ Alertas:\n" + "\n".join(report["errors"][:5])

    return (
        f"📊 Volumen propio cuenta maestra OKX\n\n"
        f"Periodo: últimos {report['days']} días\n"
        f"Desde: {report['begin']} AR\n"
        f"Hasta: {report['end']} AR\n\n"
        f"Total estimado: {money(report['total_volume'])} USDT\n"
        f"Fills encontrados: {report['total_fills']}\n\n"
        f"Detalle por mercado:\n"
        f"SPOT: {money(report['by_type'].get('SPOT'))} USDT\n"
        f"MARGIN: {money(report['by_type'].get('MARGIN'))} USDT\n"
        f"SWAP: {money(report['by_type'].get('SWAP'))} USDT\n"
        f"FUTURES: {money(report['by_type'].get('FUTURES'))} USDT\n"
        f"OPTION: {money(report['by_type'].get('OPTION'))} USDT\n\n"
        f"Este cálculo usa únicamente trades/fills propios de la cuenta API.\n"
        f"No incluye volumen de referidos."
        f"{errors_text}"
    )


# ─────────────────────────────
# MENSAJES
# ─────────────────────────────
def start_keyboard():
    return InlineKeyboardMarkup([
        [
            InlineKeyboardButton("🆕 Soy nuevo", callback_data="flow_new"),
        ],
        [
            InlineKeyboardButton("👤 Ya tengo cuenta OKX", callback_data="flow_existing"),
        ],
        [
            InlineKeyboardButton("🔄 Ya estoy en VIP y necesito actualizar referido", callback_data="flow_update"),
        ],
    ])


def welcome_menu_text():
    return (
        f"👋 Hola, soy el asistente oficial de {GROUP_NAME}.\n\n"
        "Te voy a guiar para acceder o mantener tu acceso a los bots y copy trading de la comunidad.\n\n"
        "Elige una opción:"
    )


def terms_text():
    return (
        "📌 Términos y condiciones importantes\n\n"
        "✅ La inversión mínima depende del bot que elijas, pero ronda en 25 USDT. "
        "Actualmente hay bots sobre diferentes activos como XAU, ETH, BTC, TSLA, QQQ, XAG, XRP, SPY, "
        "petróleo, acciones tecnológicas y otros instrumentos disponibles.\n\n"
        "✅ Existen algunos bots exclusivos con mínimos de inversión mayores a 1000 USDT. "
        "Más información en el grupo VIP.\n\n"
        "✅ La inversión mínima para copy trading depende de cada trader o estrategia. "
        "En general, puede comenzar desde aproximadamente 100 USDT.\n\n"
        "✅ El servicio de bots y copy trading es gratuito. Los bots no están a la venta y no hay membresías. "
        "Funcionan únicamente en OKX. No operan en brokers externos ni en MT5.\n\n"
        "✅ Tú tienes el control total de tu cuenta de OKX. Puedes seguir o dejar de seguir un bot o copy trading "
        "cuando lo decidas. También puedes depositar más o retirar tu capital cuando quieras.\n\n"
        "❌ No garantizamos rendimientos fijos ni seguros.\n\n"
        "❌ Los bots no funcionan como un CDT, depósito a plazo o producto de renta fija.\n\n"
        "❌ No somos asesores financieros. Cada usuario debe evaluar por su cuenta qué bot, copy trading "
        "o estrategia desea seguir.\n\n"
        "❌ No captamos dinero del público ni gestionamos recursos de terceros.\n\n"
        "❌ No somos una entidad financiera, casa de valores, sociedad comisionista de bolsa "
        "ni administradores de portafolios."
    )


def new_user_text():
    return (
        "🚀 Para NUEVOS usuarios\n\n"
        "1️⃣ Crea tu cuenta en OKX con el link oficial:\n\n"
        f"{OKX_JOIN_LINK}\n\n"
        "2️⃣ Completa tu registro y verificación KYC.\n\n"
        "3️⃣ Cuando tu cuenta esté lista, envíame tu UID de OKX por este chat.\n\n"
        "4️⃣ Si tu UID está correctamente vinculado, te daré acceso al grupo VIP.\n\n"
        "Dentro del grupo VIP encontrarás:\n\n"
        "🤖 Links de bots disponibles\n"
        "📈 Copy trading\n"
        "🎁 Bonos y beneficios especiales\n"
        "📚 Información y soporte para operar en OKX\n\n"
        f"{terms_text()}\n\n"
        "🚀 Así de simple: crea tu cuenta, valida tu UID y accede a la comunidad VIP para comenzar a participar.\n\n"
        "Cuando tengas tu UID, envíamelo usando solo números."
    )


def existing_user_text():
    return (
        "👤 Si ya tienes cuenta en OKX\n\n"
        "Debes comprobar que tu cuenta esté vinculada a alguno de estos códigos/referidos:\n\n"
        f"{VALID_REF_CODES_TEXT}\n\n"
        "✅ ¿Cómo saber si estás vinculado correctamente?\n\n"
        "Envíame tu UID de OKX por este chat.\n\n"
        "Si el sistema valida tu UID, podrás avanzar al grupo VIP.\n\n"
        "Si no puedes avanzar, significa que tu cuenta no está vinculada correctamente al referido.\n\n"
        "Si ya tienes cuenta, pero no estás vinculado al código de referido, completa el siguiente formulario:\n\n"
        f"{REBIND_FORM_LINK}\n\n"
        "En el campo de código de invitación, ingresa:\n\n"
        "FLANDERSYFRED\n"
        "o el código:\n"
        "99142589\n\n"
        "En motivo, escribe:\n\n"
        "Quiero pertenecer a la comunidad de FLANDERS Y FRED\n\n"
        "Si te aparece un mensaje indicando que por el momento no puedes continuar, escribe al privado de Flanders "
        "y envía tu UID para revisar tu caso:\n\n"
        f"{FLANDERS_PRIVATE_LINK}\n\n"
        f"{terms_text()}\n\n"
        "🚀 Así de simple: valida tu UID y accede a la comunidad VIP para comenzar a participar.\n\n"
        "Ahora envíame tu UID usando solo números."
    )


def update_ref_text():
    return (
        "🔄 Actualización de referido para usuarios que ya están en el grupo VIP\n\n"
        "Debes comprobar que tu cuenta esté vinculada a alguno de estos códigos/referidos. "
        "De lo contrario, podrás ser removido del grupo el día 10 de julio y perderás el acceso "
        "a los nuevos bots y copy trading exclusivos.\n\n"
        f"{VALID_REF_CODES_TEXT}\n\n"
        "✅ ¿Cómo saber si estás vinculado correctamente?\n\n"
        "Envíame tu UID de OKX por este chat.\n\n"
        "Si el sistema valida tu UID, podrás seguir en el grupo VIP.\n\n"
        "Si no puedes avanzar, significa que tu cuenta no está vinculada correctamente al referido.\n\n"
        "Para cambiar el código de referido, completa el siguiente formulario:\n\n"
        f"{REBIND_FORM_LINK}\n\n"
        "En el campo de código de invitación, ingresa:\n\n"
        "FLANDERSYFRED\n"
        "o el código:\n"
        "99142589\n\n"
        "En motivo, escribe:\n\n"
        "Quiero pertenecer a la comunidad de FLANDERS Y FRED\n\n"
        "Si te aparece un mensaje indicando que por el momento no puedes continuar, escribe al privado de Flanders "
        "y envía tu UID para revisar tu caso:\n\n"
        f"{FLANDERS_PRIVATE_LINK}\n\n"
        "Ahora envíame tu UID usando solo números."
    )


def not_affiliated_existing_text():
    return (
        "❌ Tu UID no aparece vinculado a la comunidad de Flanders y Fred.\n\n"
        "Debes comprobar que tu cuenta esté vinculada a alguno de estos códigos/referidos:\n\n"
        f"{VALID_REF_CODES_TEXT}\n\n"
        "Si ya tienes cuenta, pero no estás vinculado al código de referido, completa el siguiente formulario:\n\n"
        f"{REBIND_FORM_LINK}\n\n"
        "En el campo de código de invitación, ingresa:\n\n"
        "FLANDERSYFRED\n"
        "o el código:\n"
        "99142589\n\n"
        "En motivo, escribe:\n\n"
        "Quiero pertenecer a la comunidad de FLANDERS Y FRED\n\n"
        "Si te aparece un mensaje indicando que por el momento no puedes continuar, escribe al privado de Flanders "
        "y envía tu UID para revisar tu caso:\n\n"
        f"{FLANDERS_PRIVATE_LINK}"
    )


def not_affiliated_update_text():
    return (
        "⚠️ Tu UID no aparece vinculado a la comunidad de Flanders y Fred.\n\n"
        "Tienes hasta el 10 de julio para mantener tus accesos a los nuevos bots y copy trading exclusivos.\n\n"
        "Debes comprobar que tu cuenta esté vinculada a alguno de estos códigos/referidos:\n\n"
        f"{VALID_REF_CODES_TEXT}\n\n"
        "Para cambiar el código de referido, completa el siguiente formulario:\n\n"
        f"{REBIND_FORM_LINK}\n\n"
        "En el campo de código de invitación, ingresa:\n\n"
        "FLANDERSYFRED\n"
        "o el código:\n"
        "99142589\n\n"
        "En motivo, escribe:\n\n"
        "Quiero pertenecer a la comunidad de FLANDERS Y FRED\n\n"
        "Si te aparece un mensaje indicando que por el momento no puedes continuar, escribe al privado de Flanders "
        "y envía tu UID para revisar tu caso:\n\n"
        f"{FLANDERS_PRIVATE_LINK}"
    )


def group_link_text():
    if VIP_GROUP_LINK:
        return f"\n\nPuedes acceder o solicitar acceso al grupo VIP aquí:\n{VIP_GROUP_LINK}"
    return "\n\nSi ya solicitaste acceso al grupo VIP, tu solicitud será aprobada automáticamente."


def validated_text(flow):
    if flow == "update":
        return (
            "✅ UID verificado correctamente.\n\n"
            "Gracias por actualizar tu información. Tu cuenta aparece vinculada a la comunidad.\n\n"
            "Puedes acceder de forma permanente a los bots y copy trading de Flanders y Fred mientras mantengas "
            "las condiciones de la comunidad."
            f"{group_link_text()}"
        )

    return (
        "✅ UID verificado correctamente.\n\n"
        "Tu cuenta aparece vinculada a la comunidad de Flanders y Fred.\n\n"
        "Acceso aprobado. Dentro del grupo VIP encontrarás links de bots, copy trading, bonos, beneficios "
        "e información para operar en OKX."
        f"{group_link_text()}"
    )


def group_welcome_text(user):
    return (
        f"🚀 Bienvenido {mention_html(user.id, user.first_name)} "
        f"al grupo {GROUP_NAME}.\n\n"
        "Aquí encontrarás links de bots disponibles, copy trading, bonos, beneficios especiales, "
        "información y soporte para operar en OKX.\n\n"
        "Recuerda: los bots y copy trading no garantizan rendimientos fijos. Cada usuario debe evaluar "
        "su propio riesgo.\n\n"
        "¡Saludos y buenos trades! 📈"
    )


# ─────────────────────────────
# ADMIN FORMATOS
# ─────────────────────────────
def format_uid_report(report):
    if report is None:
        return "❌ UID no encontrado en tu comunidad de afiliados OKX."

    first_trade = "Sí" if report["did_first_trade"] else "No"
    community = "Sí" if report["is_local_community"] else "No registrado en DB local"
    affiliate = "Sí" if report["is_affiliate"] else "No"

    username = report.get("username") or ""
    if username:
        username = f"@{username}"

    return (
        f"📊 Reporte UID: {report['uid']}\n\n"
        f"✅ Parte de tu afiliado OKX: {affiliate}\n"
        f"👥 Registrado en DB comunidad: {community}\n"
        f"📌 Estado local: {report.get('status') or 'No disponible'}\n"
        f"🧭 Flujo: {report.get('flow') or 'No disponible'}\n"
        f"📅 Fecha registro / join: {report.get('register_time') or 'No disponible'}\n"
        f"📅 Fecha KYC: {report.get('kyc_time') or 'No disponible'}\n"
        f"🌎 Región: {report.get('region') or 'No disponible'}\n"
        f"🏷️ Código afiliado: {report.get('affiliate_code') or 'No disponible'}\n"
        f"⭐ Nivel invitee: {report.get('invitee_level') or 'No disponible'}\n\n"
        f"💰 Depósito acumulado: {money(report.get('dep_amt'))} USDT\n"
        f"💰 Depósito últimos 15 días: {format_optional_usdt(report.get('dep_15d'))}\n"
        f"📈 Volumen mensual: {money(report.get('vol_month'))} USDT\n"
        f"📈 Volumen últimos 7 días: {format_optional_usdt(report.get('vol_7d'))}\n"
        f"📈 Volumen total histórico: {money(report.get('total_vol'))} USDT\n"
        f"🏦 Retiros acumulados: {money(report.get('wd_amt'))} USDT\n\n"
        f"🎯 Primer trade: {first_trade}\n"
        f"🕒 Fecha primer trade: {report.get('first_trade_time') or 'No disponible'}\n\n"
        f"Telegram: {report.get('first_name') or '-'} {username}"
    )


def format_uid_report_line(report, uid):
    if report is None:
        return f"❌ {uid} | No afiliado / no encontrado"

    first_trade = "Sí" if report["did_first_trade"] else "No"
    community = "Sí" if report["is_local_community"] else "No DB"

    dep_15d = report.get("dep_15d")
    vol_7d = report.get("vol_7d")

    dep_15d_txt = number(dep_15d) if dep_15d is not None else "N/D"
    vol_7d_txt = number(vol_7d) if vol_7d is not None else "N/D"

    return (
        f"✅ {uid} | "
        f"Comunidad: {community} | "
        f"Dep total: {number(report.get('dep_amt'))} | "
        f"Dep 15d: {dep_15d_txt} | "
        f"Vol mes: {number(report.get('vol_month'))} | "
        f"Vol 7d: {vol_7d_txt} | "
        f"Vol total: {number(report.get('total_vol'))} | "
        f"1er trade: {first_trade}"
    )


# ─────────────────────────────
# TELEGRAM HANDLERS
# ─────────────────────────────
async def start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user = update.message.from_user

    save_user_profile(
        telegram_id=user.id,
        first_name=user.first_name,
        username=user.username,
        status="pending",
        source="start"
    )

    await update.message.reply_text(
        welcome_menu_text(),
        reply_markup=start_keyboard()
    )


async def menu(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await start(update, context)


async def flow_callback(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()

    user = query.from_user
    data = query.data

    if data == "flow_new":
        flow = "new"
        text = new_user_text()
    elif data == "flow_existing":
        flow = "existing"
        text = existing_user_text()
    elif data == "flow_update":
        flow = "update"
        text = update_ref_text()
    else:
        flow = ""
        text = welcome_menu_text()

    context.user_data["flow"] = flow

    save_user_profile(
        telegram_id=user.id,
        first_name=user.first_name,
        username=user.username,
        flow=flow,
        status="pending",
        source="button"
    )

    await query.edit_message_text(text=text)


async def on_join_request(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user = update.chat_join_request.from_user

    save_user_profile(
        telegram_id=user.id,
        first_name=user.first_name,
        username=user.username,
        status="pending",
        source="join_request"
    )

    try:
        await context.bot.send_message(
            chat_id=user.id,
            text=(
                f"📌 Bienvenido a {GROUP_NAME}.\n\n"
                "Para validar tu acceso, primero elige una opción:"
            ),
            reply_markup=start_keyboard()
        )
    except Exception as e:
        print(f"⚠️ No se pudo enviar DM al usuario {user.id}: {e}")


async def track_new_group_members(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if update.effective_chat.id != VIP_CHAT_ID:
        return

    if not update.message or not update.message.new_chat_members:
        return

    for member in update.message.new_chat_members:
        if member.is_bot:
            continue

        save_user_profile(
            telegram_id=member.id,
            first_name=member.first_name,
            username=member.username,
            status="pending",
            source="joined_group"
        )


async def approve_user_if_possible(context: ContextTypes.DEFAULT_TYPE, user):
    try:
        await context.bot.approve_chat_join_request(VIP_CHAT_ID, user.id)
        print(f"✅ Solicitud aprobada TG={user.id}")
        return True
    except TelegramError as e:
        print(f"⚠️ No se pudo aprobar solicitud TG={user.id}: {e}")
        return False
    except Exception as e:
        print(f"⚠️ Error aprobando solicitud TG={user.id}: {e}")
        return False


async def handle_private(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user = update.message.from_user
    text = update.message.text.strip()

    row = get_user_by_telegram_id(user.id)
    flow = context.user_data.get("flow")

    if not flow and row:
        flow = row["flow"] or ""

    if text == BYPASS_CODE:
        save_user_validated(
            telegram_id=user.id,
            uid="BYPASS",
            first_name=user.first_name,
            username=user.username,
            flow=flow or "bypass",
            source="bypass"
        )

        await approve_user_if_possible(context, user)

        await context.bot.send_message(
            chat_id=user.id,
            text="✔️ Código interno verificado. Acceso aprobado."
        )

        try:
            await context.bot.send_message(
                chat_id=VIP_CHAT_ID,
                text=group_welcome_text(user),
                parse_mode=ParseMode.HTML
            )
        except Exception as e:
            print(f"⚠️ No se pudo enviar bienvenida al grupo: {e}")

        return

    if not text.isnumeric():
        await update.message.reply_text(
            "Para continuar, elige una opción del menú o envíame tu UID de OKX usando solo números.",
            reply_markup=start_keyboard()
        )
        return

    uid = text

    if not flow:
        flow = "existing"
        context.user_data["flow"] = flow

        save_user_profile(
            telegram_id=user.id,
            first_name=user.first_name,
            username=user.username,
            flow=flow,
            status="pending",
            source="uid_without_flow"
        )

    try:
        is_affiliated, data, vol_month, raw_resp = is_uid_affiliated(uid)
    except Exception as e:
        print(f"❌ Error consultando OKX UID={uid}: {e}")
        await update.message.reply_text(
            "❌ Hubo un error consultando OKX.\n"
            "Intenta nuevamente más tarde o escribe al privado de Flanders para revisar tu caso:\n\n"
            f"{FLANDERS_PRIVATE_LINK}"
        )
        return

    if not is_affiliated:
        mark_user_not_affiliated(
            telegram_id=user.id,
            first_name=user.first_name,
            username=user.username,
            flow=flow,
            source="uid_not_affiliated"
        )

        if flow == "update":
            await update.message.reply_text(not_affiliated_update_text())
        else:
            await update.message.reply_text(not_affiliated_existing_text())

        return

    save_user_validated(
        telegram_id=user.id,
        uid=uid,
        first_name=user.first_name,
        username=user.username,
        last_vol_month=vol_month,
        flow=flow,
        source="uid_validated"
    )

    await approve_user_if_possible(context, user)

    await context.bot.send_message(
        chat_id=user.id,
        text=validated_text(flow)
    )

    try:
        await context.bot.send_message(
            chat_id=VIP_CHAT_ID,
            text=group_welcome_text(user),
            parse_mode=ParseMode.HTML
        )
    except Exception as e:
        print(f"⚠️ No se pudo enviar bienvenida al grupo: {e}")


# ─────────────────────────────
# ADMIN: CONSULTAR VOLUMEN PROPIO CUENTA MAESTRA
# ─────────────────────────────
async def mivolumen(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if update.effective_chat.type != "private":
        await update.message.reply_text(
            "Por seguridad, usa este comando en el chat privado con el bot."
        )
        return

    admin_id = update.message.from_user.id

    if not is_admin(admin_id):
        await update.message.reply_text("❌ No autorizado.")
        return

    days = parse_period_to_days(context.args)

    if days is None:
        await update.message.reply_text(
            "Uso:\n"
            "/mivolumen\n"
            "/mivolumen 7d\n"
            "/mivolumen 15d\n"
            "/mivolumen 30d\n\n"
            "Máximo permitido: 90d."
        )
        return

    await update.message.reply_text(
        f"⏳ Consultando volumen propio de la cuenta maestra para los últimos {days} días..."
    )

    try:
        report = get_master_trading_volume(days=days)
        await update.message.reply_text(format_master_volume_report(report))
    except Exception as e:
        print(f"Error en /mivolumen: {e}")
        await update.message.reply_text(
            "❌ Error consultando el volumen propio de la cuenta maestra.\n"
            "Verifica que la API key tenga permiso Read para trading/account."
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

    if not is_admin(admin_id):
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
            await update.message.reply_text("❌ No pude consultar ese UID en OKX.")
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
# ADMIN: REPORTE POR UID
# ─────────────────────────────
async def checkuid(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if update.effective_chat.type != "private":
        await update.message.reply_text("Por seguridad, usa este comando por privado.")
        return

    admin_id = update.message.from_user.id

    if not is_admin(admin_id):
        await update.message.reply_text("❌ No autorizado.")
        return

    if not context.args:
        await update.message.reply_text("Uso: /checkuid 123456789")
        return

    uid = context.args[0].strip()

    if not uid.isnumeric():
        await update.message.reply_text("UID inválido. Usa solo números.")
        return

    try:
        report = get_uid_report(uid)
        await update.message.reply_text(format_uid_report(report))
    except Exception as e:
        print(f"Error en /checkuid UID={uid}: {e}")
        await update.message.reply_text("❌ Error consultando el UID.")


# ─────────────────────────────
# ADMIN: REPORTE MÚLTIPLE
# ─────────────────────────────
async def checkuids(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if update.effective_chat.type != "private":
        await update.message.reply_text("Por seguridad, usa este comando por privado.")
        return

    admin_id = update.message.from_user.id

    if not is_admin(admin_id):
        await update.message.reply_text("❌ No autorizado.")
        return

    if not context.args:
        await update.message.reply_text(
            "Uso:\n"
            "/checkuids 123 456 789\n\n"
            "También puedes separar por comas."
        )
        return

    raw_text = " ".join(context.args)
    uids = split_uids(raw_text)

    if not uids:
        await update.message.reply_text("No encontré UIDs válidos.")
        return

    if len(uids) > 20:
        await update.message.reply_text(
            "Por ahora consulta máximo 20 UIDs por mensaje para evitar rate limit de OKX."
        )
        return

    lines = ["📊 Reporte múltiple de UIDs\n"]

    for uid in uids:
        try:
            report = get_uid_report(uid)
            lines.append(format_uid_report_line(report, uid))
            time.sleep(0.4)
        except Exception as e:
            print(f"Error consultando UID={uid}: {e}")
            lines.append(f"⚠️ {uid} | Error consultando")

    text = "\n".join(lines)

    if len(text) <= 3900:
        await update.message.reply_text(text)
    else:
        filename = f"reporte_uids_{datetime.now(TZ_AR).strftime('%Y_%m_%d_%H_%M')}.txt"
        filepath = os.path.join(tempfile.gettempdir(), filename)

        with open(filepath, "w", encoding="utf-8") as f:
            f.write(text)

        await context.bot.send_document(
            chat_id=admin_id,
            document=open(filepath, "rb"),
            filename=filename,
            caption="📄 Reporte múltiple de UIDs"
        )


# ─────────────────────────────
# ADMIN: REPORTE CSV DE LISTA DE UIDS
# ─────────────────────────────
async def checkuidscsv(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if update.effective_chat.type != "private":
        await update.message.reply_text("Por seguridad, usa este comando por privado.")
        return

    admin_id = update.message.from_user.id

    if not is_admin(admin_id):
        await update.message.reply_text("❌ No autorizado.")
        return

    if not context.args:
        await update.message.reply_text(
            "Uso:\n"
            "/checkuidscsv 123 456 789\n\n"
            "También puedes separar por comas."
        )
        return

    raw_text = " ".join(context.args)
    uids = split_uids(raw_text)

    if not uids:
        await update.message.reply_text("No encontré UIDs válidos.")
        return

    if len(uids) > 100:
        await update.message.reply_text("Máximo 100 UIDs por CSV para evitar rate limits.")
        return

    rows = []

    for uid in uids:
        try:
            report = get_uid_report(uid)

            if report is None:
                rows.append({
                    "uid": uid,
                    "afiliado_okx": "No",
                    "comunidad_db": "No",
                    "estado_local": "",
                    "flujo": "",
                    "fecha_registro_join": "",
                    "fecha_kyc": "",
                    "region": "",
                    "codigo_afiliado": "",
                    "invitee_level": "",
                    "deposito_total_usdt": 0,
                    "deposito_15d_usdt": "",
                    "volumen_mes_usdt": 0,
                    "volumen_7d_usdt": "",
                    "volumen_total_usdt": 0,
                    "retiros_total_usdt": 0,
                    "primer_trade": "No",
                    "fecha_primer_trade": "",
                    "telegram_id": "",
                    "first_name": "",
                    "username": "",
                    "joined_at": "",
                })
            else:
                rows.append({
                    "uid": uid,
                    "afiliado_okx": "Si",
                    "comunidad_db": "Si" if report.get("is_local_community") else "No",
                    "estado_local": report.get("status") or "",
                    "flujo": report.get("flow") or "",
                    "fecha_registro_join": report.get("register_time") or "",
                    "fecha_kyc": report.get("kyc_time") or "",
                    "region": report.get("region") or "",
                    "codigo_afiliado": report.get("affiliate_code") or "",
                    "invitee_level": report.get("invitee_level") or "",
                    "deposito_total_usdt": report.get("dep_amt") or 0,
                    "deposito_15d_usdt": report.get("dep_15d") if report.get("dep_15d") is not None else "",
                    "volumen_mes_usdt": report.get("vol_month") or 0,
                    "volumen_7d_usdt": report.get("vol_7d") if report.get("vol_7d") is not None else "",
                    "volumen_total_usdt": report.get("total_vol") or 0,
                    "retiros_total_usdt": report.get("wd_amt") or 0,
                    "primer_trade": "Si" if report.get("did_first_trade") else "No",
                    "fecha_primer_trade": report.get("first_trade_time") or "",
                    "telegram_id": report.get("telegram_id") or "",
                    "first_name": report.get("first_name") or "",
                    "username": report.get("username") or "",
                    "joined_at": report.get("joined_at") or "",
                })

            time.sleep(0.4)

        except Exception as e:
            print(f"Error CSV consultando UID={uid}: {e}")
            rows.append({
                "uid": uid,
                "afiliado_okx": "Error",
                "comunidad_db": "",
                "estado_local": "",
                "flujo": "",
                "fecha_registro_join": "",
                "fecha_kyc": "",
                "region": "",
                "codigo_afiliado": "",
                "invitee_level": "",
                "deposito_total_usdt": "",
                "deposito_15d_usdt": "",
                "volumen_mes_usdt": "",
                "volumen_7d_usdt": "",
                "volumen_total_usdt": "",
                "retiros_total_usdt": "",
                "primer_trade": "",
                "fecha_primer_trade": "",
                "telegram_id": "",
                "first_name": "",
                "username": "",
                "joined_at": "",
            })

    filename = f"reporte_uids_okx_{datetime.now(TZ_AR).strftime('%Y_%m_%d_%H_%M')}.csv"
    filepath = os.path.join(tempfile.gettempdir(), filename)

    fieldnames = [
        "uid",
        "afiliado_okx",
        "comunidad_db",
        "estado_local",
        "flujo",
        "fecha_registro_join",
        "fecha_kyc",
        "region",
        "codigo_afiliado",
        "invitee_level",
        "deposito_total_usdt",
        "deposito_15d_usdt",
        "volumen_mes_usdt",
        "volumen_7d_usdt",
        "volumen_total_usdt",
        "retiros_total_usdt",
        "primer_trade",
        "fecha_primer_trade",
        "telegram_id",
        "first_name",
        "username",
        "joined_at",
    ]

    with open(filepath, mode="w", newline="", encoding="utf-8") as f:
        writer = csv.DictWriter(f, fieldnames=fieldnames)
        writer.writeheader()
        writer.writerows(rows)

    await context.bot.send_document(
        chat_id=admin_id,
        document=open(filepath, "rb"),
        filename=filename,
        caption="📄 Reporte CSV de UIDs OKX"
    )


# ─────────────────────────────
# ADMIN: DEBUG CAMPOS OKX
# ─────────────────────────────
async def debuguid(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if update.effective_chat.type != "private":
        await update.message.reply_text("Por seguridad, usa este comando por privado.")
        return

    admin_id = update.message.from_user.id

    if not is_admin(admin_id):
        await update.message.reply_text("❌ No autorizado.")
        return

    if not context.args:
        await update.message.reply_text("Uso: /debuguid 123456789")
        return

    uid = context.args[0].strip()

    if not uid.isnumeric():
        await update.message.reply_text("UID inválido. Usa solo números.")
        return

    try:
        resp = okx_affiliate_detail(uid)

        filename = f"debug_okx_uid_{uid}_{datetime.now(TZ_AR).strftime('%Y_%m_%d_%H_%M')}.json"
        filepath = os.path.join(tempfile.gettempdir(), filename)

        with open(filepath, "w", encoding="utf-8") as f:
            json.dump(resp, f, ensure_ascii=False, indent=2)

        await context.bot.send_document(
            chat_id=admin_id,
            document=open(filepath, "rb"),
            filename=filename,
            caption=(
                "🧪 Debug OKX Affiliate Detail.\n"
                "Revisa este JSON para confirmar los nombres exactos de campos disponibles."
            )
        )

    except Exception as e:
        print(f"Error en /debuguid UID={uid}: {e}")
        await update.message.reply_text("❌ Error generando debug del UID.")


# ─────────────────────────────
# ADMIN: INFORME CSV DE USUARIOS REGISTRADOS
# ─────────────────────────────
async def informe(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if update.effective_chat.type != "private":
        await update.message.reply_text(
            "Por seguridad, usa /informe en el chat privado con el bot."
        )
        return

    admin_id = update.message.from_user.id

    if not is_admin(admin_id):
        await update.message.reply_text("❌ No autorizado.")
        return

    users = get_all_users()

    if not users:
        await update.message.reply_text("No hay usuarios registrados todavía.")
        return

    rows = []

    for row in users:
        rows.append({
            "telegram_id": row["telegram_id"],
            "first_name": row["first_name"] or "",
            "username": row["username"] or "",
            "uid": row["uid"] or "",
            "tiene_uid": "Si" if row["uid"] else "No",
            "estado": row["status"] or "",
            "flujo": row["flow"] or "",
            "volumen_mes_usdt": row["last_vol_month"] or 0,
            "joined_at": row["joined_at"] or "",
            "last_checked_at": row["last_checked_at"] or "",
            "last_interaction_at": row["last_interaction_at"] or "",
            "source": row["source"] or "",
        })

    filename = f"informe_flanders_fred_{datetime.now(TZ_AR).strftime('%Y_%m_%d_%H_%M')}.csv"
    filepath = os.path.join(tempfile.gettempdir(), filename)

    fieldnames = [
        "telegram_id",
        "first_name",
        "username",
        "uid",
        "tiene_uid",
        "estado",
        "flujo",
        "volumen_mes_usdt",
        "joined_at",
        "last_checked_at",
        "last_interaction_at",
        "source",
    ]

    with open(filepath, mode="w", newline="", encoding="utf-8") as f:
        writer = csv.DictWriter(f, fieldnames=fieldnames)
        writer.writeheader()
        writer.writerows(rows)

    await context.bot.send_document(
        chat_id=admin_id,
        document=open(filepath, "rb"),
        filename=filename,
        caption="📄 Informe de usuarios registrados en el bot Flanders y Fred / OKX"
    )


# ─────────────────────────────
# ADMIN: LISTA QUIÉN TIENE UID Y QUIÉN NO
# ─────────────────────────────
async def listauids(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if update.effective_chat.type != "private":
        await update.message.reply_text("Por seguridad, usa /listauids por privado.")
        return

    admin_id = update.message.from_user.id

    if not is_admin(admin_id):
        await update.message.reply_text("❌ No autorizado.")
        return

    users = get_all_users()
    counts = get_uid_status_counts()

    if not users:
        await update.message.reply_text("Todavía no hay usuarios registrados en la base de datos del bot.")
        return

    lines = []

    lines.append("📋 Lista de usuarios registrados en el bot\n")
    lines.append(f"Total registrados en DB: {counts['total'] or 0}")
    lines.append(f"Con UID: {counts['con_uid'] or 0}")
    lines.append(f"Sin UID: {counts['sin_uid'] or 0}")
    lines.append(f"Validados: {counts['validados'] or 0}")
    lines.append(f"No afiliados: {counts['no_afiliados'] or 0}")
    lines.append(f"Pendientes: {counts['pendientes'] or 0}")
    lines.append("")
    lines.append("⚠️ Nota: Telegram no permite al bot descargar la lista completa histórica de miembros del grupo.")
    lines.append("Esta lista incluye usuarios que interactuaron con el bot, solicitaron acceso o fueron registrados desde eventos visibles para el bot.")
    lines.append("")

    lines.append("✅ USUARIOS CON UID")
    has_uid = [u for u in users if u["uid"]]

    if has_uid:
        for u in has_uid[:150]:
            username = f"@{u['username']}" if u["username"] else ""
            lines.append(
                f"- TG:{u['telegram_id']} | {u['first_name'] or '-'} {username} | UID:{u['uid']} | Estado:{u['status'] or '-'} | Flujo:{u['flow'] or '-'}"
            )
    else:
        lines.append("- Ninguno")

    lines.append("")
    lines.append("⏳ USUARIOS SIN UID")
    no_uid = [u for u in users if not u["uid"]]

    if no_uid:
        for u in no_uid[:150]:
            username = f"@{u['username']}" if u["username"] else ""
            lines.append(
                f"- TG:{u['telegram_id']} | {u['first_name'] or '-'} {username} | Estado:{u['status'] or '-'} | Flujo:{u['flow'] or '-'} | Fuente:{u['source'] or '-'}"
            )
    else:
        lines.append("- Ninguno")

    text = "\n".join(lines)

    if len(text) <= 3900:
        await update.message.reply_text(text)
    else:
        filename = f"lista_uids_flanders_fred_{datetime.now(TZ_AR).strftime('%Y_%m_%d_%H_%M')}.txt"
        filepath = os.path.join(tempfile.gettempdir(), filename)

        with open(filepath, "w", encoding="utf-8") as f:
            f.write(text)

        await context.bot.send_document(
            chat_id=admin_id,
            document=open(filepath, "rb"),
            filename=filename,
            caption="📄 Lista de usuarios con UID y sin UID"
        )


# ─────────────────────────────
# MAIN
# ─────────────────────────────
def main():
    init_db()

    defaults = Defaults(tzinfo=timezone.utc)
    app = Application.builder().token(BOT_TOKEN).defaults(defaults).build()

    # Usuario
    app.add_handler(CommandHandler("start", start))
    app.add_handler(CommandHandler("menu", menu))
    app.add_handler(CallbackQueryHandler(flow_callback))

    # Admin
    app.add_handler(CommandHandler("voluid", voluid))
    app.add_handler(CommandHandler("mivolumen", mivolumen))
    app.add_handler(CommandHandler("informe", informe))
    app.add_handler(CommandHandler("checkuid", checkuid))
    app.add_handler(CommandHandler("checkuids", checkuids))
    app.add_handler(CommandHandler("checkuidscsv", checkuidscsv))
    app.add_handler(CommandHandler("debuguid", debuguid))
    app.add_handler(CommandHandler("listauids", listauids))

    # Grupo / acceso
    app.add_handler(ChatJoinRequestHandler(on_join_request))
    app.add_handler(MessageHandler(filters.StatusUpdate.NEW_CHAT_MEMBERS, track_new_group_members))

    # Mensajes privados: UID
    app.add_handler(MessageHandler(filters.ChatType.PRIVATE & filters.TEXT, handle_private))

    print(f"🤖 BOT {GROUP_NAME} iniciado.")
    print(f"📁 DB_PATH: {DB_PATH}")

    app.run_polling()


if __name__ == "__main__":
    main()
