import os
import logging
import sqlite3
from datetime import datetime, timedelta
from zoneinfo import ZoneInfo

from telegram import Update, InlineKeyboardButton, InlineKeyboardMarkup
from telegram.ext import (
    Application, CommandHandler, CallbackQueryHandler, ContextTypes
)

TOKEN = os.getenv("BOT_TOKEN", "").strip()
SUPERADMIN_IDS = {int(x) for x in os.getenv("ADMIN_IDS", "8011205445").split(",") if x.strip()}
LEGACY_CHANNEL_ID = os.getenv("CHANNEL_ID", "").strip()
TZ = ZoneInfo(os.getenv("TIMEZONE", "Europe/Istanbul"))
DB = os.getenv("DB_PATH", "altay_vip.sqlite3")

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
log = logging.getLogger("altay-vip-bot")


def now():
    return datetime.now(TZ)


def connect():
    c = sqlite3.connect(DB)
    c.row_factory = sqlite3.Row
    return c


def init_db():
    """Additive migration: keeps legacy tables and rows untouched."""
    with connect() as c:
        c.execute("PRAGMA foreign_keys=ON")
        c.execute("""CREATE TABLE IF NOT EXISTS tenants(
            tenant_id TEXT PRIMARY KEY,
            tenant_name TEXT NOT NULL,
            status TEXT NOT NULL DEFAULT 'active',
            created_at TEXT NOT NULL
        )""")
        c.execute("""CREATE TABLE IF NOT EXISTS tenant_channels(
            channel_id INTEGER PRIMARY KEY,
            tenant_id TEXT NOT NULL,
            channel_name TEXT NOT NULL,
            channel_type TEXT NOT NULL DEFAULT 'other',
            owner_user_id INTEGER,
            status TEXT NOT NULL DEFAULT 'active',
            FOREIGN KEY(tenant_id) REFERENCES tenants(tenant_id)
        )""")
        c.execute("""CREATE TABLE IF NOT EXISTS tenant_admins(
            tenant_id TEXT NOT NULL,
            user_id INTEGER NOT NULL,
            role TEXT NOT NULL,
            channel_id INTEGER,
            status TEXT NOT NULL DEFAULT 'active',
            added_at TEXT NOT NULL,
            PRIMARY KEY(tenant_id,user_id,role,channel_id),
            FOREIGN KEY(tenant_id) REFERENCES tenants(tenant_id)
        )""")
        c.execute("""CREATE TABLE IF NOT EXISTS tenant_memberships(
            tenant_id TEXT NOT NULL,
            channel_id INTEGER NOT NULL,
            user_id INTEGER NOT NULL,
            expires_at TEXT NOT NULL,
            status TEXT NOT NULL,
            PRIMARY KEY(tenant_id,channel_id,user_id)
        )""")
        c.execute("""CREATE TABLE IF NOT EXISTS tenant_applications(
            application_id INTEGER PRIMARY KEY AUTOINCREMENT,
            tenant_id TEXT NOT NULL,
            channel_id INTEGER NOT NULL,
            user_id INTEGER NOT NULL,
            username TEXT,
            full_name TEXT,
            status TEXT NOT NULL,
            created_at TEXT NOT NULL
        )""")
        c.execute("""CREATE TABLE IF NOT EXISTS tenant_posts(
            job_id TEXT PRIMARY KEY,
            tenant_id TEXT NOT NULL,
            channel_id INTEGER NOT NULL,
            run_at TEXT NOT NULL,
            message TEXT NOT NULL,
            status TEXT NOT NULL
        )""")
        c.execute("""CREATE TABLE IF NOT EXISTS tenant_settings(
            tenant_id TEXT NOT NULL,
            setting_key TEXT NOT NULL,
            setting_value TEXT NOT NULL,
            updated_at TEXT NOT NULL,
            PRIMARY KEY(tenant_id,setting_key)
        )""")
        c.execute("""CREATE TABLE IF NOT EXISTS support_contacts(
            contact_id INTEGER PRIMARY KEY AUTOINCREMENT,
            tenant_id TEXT NOT NULL,
            display_name TEXT NOT NULL,
            telegram_link TEXT NOT NULL,
            enabled INTEGER NOT NULL DEFAULT 1
        )""")
        c.execute("""CREATE TABLE IF NOT EXISTS link_items(
            link_id INTEGER PRIMARY KEY AUTOINCREMENT,
            tenant_id TEXT NOT NULL,
            category TEXT NOT NULL,
            display_name TEXT NOT NULL,
            url TEXT NOT NULL,
            enabled INTEGER NOT NULL DEFAULT 1
        )""")

        # Create the test tenant and its channel records idempotently.
        c.execute("""INSERT OR IGNORE INTO tenants
            (tenant_id,tenant_name,status,created_at) VALUES(?,?,?,?)""",
            ("altay_demo", "ALTAY VIP - Deneme", "active", now().isoformat()))
        c.execute("""INSERT OR IGNORE INTO tenant_channels
            (channel_id,tenant_id,channel_name,channel_type,owner_user_id,status)
            VALUES(?,?,?,?,?,?)""",
            (-1004367177649, "altay_demo", "Deneme", "test", 8011205445, "active"))

        # Friend's tenant and three channel records; bot is NOT invited by this migration.
        c.execute("""INSERT OR IGNORE INTO tenants
            (tenant_id,tenant_name,status,created_at) VALUES(?,?,?,?)""",
            ("musteri_001", "Müşteri 001", "active", now().isoformat()))
        channels = [
            (-1002495313859, "Sohbet", "chat", 7818013007),
            (-1004327705802, "Gizli", "private", 7818013007),
            (-1003913699711, "Elit VIP", "vip", 1811901723),
        ]
        for cid, name, kind, owner in channels:
            c.execute("""INSERT OR IGNORE INTO tenant_channels
                (channel_id,tenant_id,channel_name,channel_type,owner_user_id,status)
                VALUES(?,?,?,?,?,'active')""",
                (cid, "musteri_001", name, kind, owner))

        # Tenant-level manager scope is deliberately limited to their own channel(s).
        c.execute("""INSERT OR IGNORE INTO tenant_admins
            (tenant_id,user_id,role,channel_id,status,added_at)
            VALUES(?,?,?,?,?,?)""",
            ("musteri_001", 1811901723, "channel_manager", -1003913699711, "active", now().isoformat()))
        for cid in (-1002495313859, -1004327705802):
            c.execute("""INSERT OR IGNORE INTO tenant_admins
                (tenant_id,user_id,role,channel_id,status,added_at)
                VALUES(?,?,?,?,?,?)""",
                ("musteri_001", 7818013007, "channel_manager", cid, "active", now().isoformat()))

        # Preserve the old single-channel setup as a separate legacy tenant.
        if LEGACY_CHANNEL_ID:
            try:
                legacy_id = int(LEGACY_CHANNEL_ID)
                c.execute("""INSERT OR IGNORE INTO tenants
                    (tenant_id,tenant_name,status,created_at) VALUES(?,?,?,?)""",
                    ("legacy_single", "Eski ALTAY VIP", "active", now().isoformat()))
                c.execute("""INSERT OR IGNORE INTO tenant_channels
                    (channel_id,tenant_id,channel_name,channel_type,owner_user_id,status)
                    VALUES(?,?,?,?,?,?)""",
                    (legacy_id, "legacy_single", "Eski ana kanal", "legacy", None, "active"))
            except ValueError:
                log.warning("CHANNEL_ID sayısal değil; legacy kanal kaydı atlandı.")

        # Do not copy/delete legacy memberships automatically in phase 1.
        # The next migration phase will map them only after the live DB is backed up.


def is_superadmin(uid):
    return uid in SUPERADMIN_IDS


def user_tenant_permissions(uid):
    with connect() as c:
        return c.execute("""
            SELECT a.tenant_id,a.role,a.channel_id,t.tenant_name,ch.channel_name
            FROM tenant_admins a
            JOIN tenants t ON t.tenant_id=a.tenant_id
            LEFT JOIN tenant_channels ch ON ch.channel_id=a.channel_id
            WHERE a.user_id=? AND a.status='active' AND t.status='active'
            ORDER BY t.tenant_name,ch.channel_name
        """, (uid,)).fetchall()


def main_keyboard():
    return InlineKeyboardMarkup([
        [InlineKeyboardButton("🏢 Müşteriler ve kanallar", callback_data="panel:tenants")],
        [InlineKeyboardButton("👥 Yetkilerim", callback_data="panel:permissions")],
        [InlineKeyboardButton("🧾 Sistem bilgisi", callback_data="panel:status")],
    ])


async def start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user = update.effective_user
    await update.message.reply_text(
        f"ALTAY VIP BOT'a hoş geldin, {user.first_name}!\n"
        "Yönetim paneline erişimin varsa /admin komutunu kullanabilirsin."
    )


async def panel(update: Update, context: ContextTypes.DEFAULT_TYPE):
    uid = update.effective_user.id
    if not is_superadmin(uid) and not user_tenant_permissions(uid):
        await update.message.reply_text("Bu panel için yönetici yetkin bulunmuyor.")
        return
    await update.message.reply_text(
        "🛠 ALTAY VIP BOT — YÖNETİM PANELİ\n\n"
        "Bu ilk aşama yalnızca müşteri/kanal kayıtlarını ve yetki kapsamlarını gösterir. "
        "Henüz gerçek kanallara mesaj göndermez ve üyelik işlemlerini değiştirmez.",
        reply_markup=main_keyboard()
    )


async def panel_callbacks(update: Update, context: ContextTypes.DEFAULT_TYPE):
    q = update.callback_query
    uid = q.from_user.id
    await q.answer()
    if not is_superadmin(uid) and not user_tenant_permissions(uid):
        await q.edit_message_text("Bu işlem için yetkin bulunmuyor.")
        return

    if q.data == "panel:tenants":
        if is_superadmin(uid):
            with connect() as c:
                tenants = c.execute(
                    "SELECT tenant_id,tenant_name,status FROM tenants ORDER BY tenant_name"
                ).fetchall()
                channels = c.execute("""
                    SELECT ch.channel_name,ch.channel_id,ch.channel_type,t.tenant_name
                    FROM tenant_channels ch JOIN tenants t ON t.tenant_id=ch.tenant_id
                    ORDER BY t.tenant_name,ch.channel_name
                """).fetchall()
            lines = ["🏢 MÜŞTERİLER"]
            lines += [f"• {t['tenant_name']} ({t['tenant_id']}) — {t['status']}" for t in tenants]
            lines += ["", "📡 KANALLAR"]
            lines += [f"• {x['tenant_name']} / {x['channel_name']} — {x['channel_id']} ({x['channel_type']})" for x in channels]
        else:
            rows = user_tenant_permissions(uid)
            lines = ["🏢 YETKİLİ OLDUĞUN KANALLAR"]
            lines += [f"• {r['tenant_name']} / {r['channel_name'] or 'Tüm tenant'} — {r['channel_id'] or 'genel'}" for r in rows]
        await q.edit_message_text("\n".join(lines), reply_markup=main_keyboard())

    elif q.data == "panel:permissions":
        if is_superadmin(uid):
            await q.edit_message_text(
                "👑 Ana yönetici yetkin aktiftir.\n"
                "Tüm müşteri ve kanalları yönetebilirsin.\n\n"
                "Bu aşamada diğer yöneticilerin yetkileri yalnızca kendi kanallarıyla sınırlıdır.",
                reply_markup=main_keyboard()
            )
        else:
            rows = user_tenant_permissions(uid)
            lines = ["🔐 YETKİLERİN"]
            lines += [f"• {r['tenant_name']} — {r['channel_name'] or 'Genel'} ({r['role']})" for r in rows]
            await q.edit_message_text("\n".join(lines), reply_markup=main_keyboard())

    elif q.data == "panel:status":
        with connect() as c:
            tenant_count = c.execute("SELECT COUNT(*) FROM tenants WHERE status='active'").fetchone()[0]
            channel_count = c.execute("SELECT COUNT(*) FROM tenant_channels WHERE status='active'").fetchone()[0]
            admin_count = c.execute("SELECT COUNT(DISTINCT user_id) FROM tenant_admins WHERE status='active'").fetchone()[0]
            legacy_tables = []
            for table in ("applications", "memberships", "posts"):
                exists = c.execute(
                    "SELECT 1 FROM sqlite_master WHERE type='table' AND name=?", (table,)
                ).fetchone()
                legacy_tables.append(f"{table}: {'mevcut' if exists else 'bulunamadı'}")
        await q.edit_message_text(
            "🧾 SİSTEM DURUMU\n\n"
            f"Aktif müşteri: {tenant_count}\nAktif kanal kaydı: {channel_count}\n"
            f"Kayıtlı tenant yöneticisi: {admin_count}\n\n"
            "Eski tablolar: " + ", ".join(legacy_tables) +
            "\n\nNot: Eski üyelik tabloları silinmedi veya değiştirilmedi.",
            reply_markup=main_keyboard()
        )


async def whoami(update: Update, context: ContextTypes.DEFAULT_TYPE):
    uid = update.effective_user.id
    await update.message.reply_text(f"Telegram kullanıcı ID: {uid}")


def main():
    if not TOKEN:
        raise RuntimeError("BOT_TOKEN ayarlanmamış.")
    if not SUPERADMIN_IDS:
        raise RuntimeError("ADMIN_IDS ayarlanmamış.")
    init_db()
    app = Application.builder().token(TOKEN).build()
    app.add_handler(CommandHandler("start", start))
    app.add_handler(CommandHandler("admin", panel))
    app.add_handler(CommandHandler("kimim", whoami))
    app.add_handler(CallbackQueryHandler(panel_callbacks, pattern=r"^panel:"))
    log.info("ALTAY VIP BOT çok kanallı panel - faz 1 hazır.")
    app.run_polling()


if __name__ == "__main__":
    main()
