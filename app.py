import os, logging, sqlite3
from datetime import datetime, timedelta
from zoneinfo import ZoneInfo

from telegram import Update, InlineKeyboardButton, InlineKeyboardMarkup
from telegram.ext import (
    Application, CommandHandler, CallbackQueryHandler, ContextTypes, ChatMemberHandler, ChatJoinRequestHandler
)

TOKEN = os.getenv("BOT_TOKEN", "").strip()
ADMIN_IDS = {int(x) for x in os.getenv("ADMIN_IDS", "").split(",") if x.strip()}
CHANNEL_ID = os.getenv("CHANNEL_ID", "").strip()
TZ = ZoneInfo(os.getenv("TIMEZONE", "Europe/Istanbul"))
DB = os.getenv("DB_PATH", "altay_vip.sqlite3")

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
LAST_ERROR_NOTICE = None

class SecretRedactionFilter(logging.Filter):
    """Remove the Telegram bot token from every emitted log record."""
    def filter(self, record):
        try:
            message = record.getMessage()
            if TOKEN and TOKEN in message:
                record.msg = message.replace(TOKEN, "[REDACTED_BOT_TOKEN]")
                record.args = ()
        except Exception:
            pass
        return True


# python-telegram-bot uses HTTPX; its INFO request logs include the full API URL.
# Keep those request details out of logs and redact the token as a second safeguard.
for handler in logging.getLogger().handlers:
    handler.addFilter(SecretRedactionFilter())
logging.getLogger("httpx").setLevel(logging.WARNING)
logging.getLogger("httpcore").setLevel(logging.WARNING)

log = logging.getLogger("altay-vip-bot")


def now():
    return datetime.now(TZ)


def connect():
    conn = sqlite3.connect(DB)
    conn.row_factory = sqlite3.Row
    return conn


def log_admin_action(actor_id, action, target_user_id=None, details=""):
    with connect() as conn:
        conn.execute("""INSERT INTO admin_audit_log
            (actor_id,action,target_user_id,details,created_at)
            VALUES(?,?,?,?,?)""",
            (int(actor_id), action[:80], target_user_id, details[:500], now().isoformat()))


def init_db():
    # Legacy tables are retained exactly; new tables are additive only.
    with connect() as c:
        c.execute("""CREATE TABLE IF NOT EXISTS applications(
            user_id INTEGER PRIMARY KEY, username TEXT, full_name TEXT,
            status TEXT NOT NULL, created_at TEXT NOT NULL)""")
        c.execute("""CREATE TABLE IF NOT EXISTS memberships(
            user_id INTEGER PRIMARY KEY, expires_at TEXT NOT NULL, status TEXT NOT NULL, username TEXT)""")
        c.execute("""CREATE TABLE IF NOT EXISTS posts(
            job_id TEXT PRIMARY KEY, run_at TEXT NOT NULL, message TEXT NOT NULL,
            status TEXT NOT NULL)""")
        c.execute("""CREATE TABLE IF NOT EXISTS vip_invite_messages(
            user_id INTEGER NOT NULL, chat_id INTEGER NOT NULL,
            message_id INTEGER NOT NULL, invite_link TEXT NOT NULL,
            PRIMARY KEY(user_id,chat_id))""")
        c.execute("""CREATE TABLE IF NOT EXISTS membership_reminders(
            membership_type TEXT NOT NULL, tenant_id TEXT NOT NULL DEFAULT '',
            channel_id INTEGER NOT NULL DEFAULT 0, user_id INTEGER NOT NULL,
            reminder_days INTEGER NOT NULL, sent_at TEXT NOT NULL,
            PRIMARY KEY(membership_type,tenant_id,channel_id,user_id,reminder_days))""")
        c.execute("""CREATE TABLE IF NOT EXISTS membership_payments(
            payment_id INTEGER PRIMARY KEY AUTOINCREMENT,
            membership_type TEXT NOT NULL, tenant_id TEXT NOT NULL DEFAULT '',
            channel_id INTEGER NOT NULL DEFAULT 0, user_id INTEGER NOT NULL,
            amount REAL NOT NULL, note TEXT, paid_at TEXT NOT NULL,
            recorded_by INTEGER NOT NULL)""")
        c.execute("""CREATE TABLE IF NOT EXISTS admin_audit_log(
            audit_id INTEGER PRIMARY KEY AUTOINCREMENT,
            actor_id INTEGER NOT NULL, action TEXT NOT NULL,
            target_user_id INTEGER, details TEXT, created_at TEXT NOT NULL)""")
        c.execute("""CREATE TABLE IF NOT EXISTS recurring_announcements(
            announcement_id INTEGER PRIMARY KEY AUTOINCREMENT,
            tenant_id TEXT NOT NULL DEFAULT 'legacy_single',
            send_time TEXT NOT NULL, message TEXT NOT NULL,
            enabled INTEGER NOT NULL DEFAULT 1, last_sent_date TEXT)""")
        recurring_columns = {row[1] for row in c.execute("PRAGMA table_info(recurring_announcements)").fetchall()}
        if "tenant_id" not in recurring_columns:
            c.execute("ALTER TABLE recurring_announcements ADD COLUMN tenant_id TEXT NOT NULL DEFAULT 'legacy_single'")
        c.execute("UPDATE recurring_announcements SET tenant_id='legacy_single' WHERE tenant_id IS NULL OR tenant_id=''")

        # Üyelik tablolarına Telegram kullanıcı adı alanlarını geriye dönük ekle.
        membership_columns = {row[1] for row in c.execute("PRAGMA table_info(memberships)").fetchall()}
        if "username" not in membership_columns:
            c.execute("ALTER TABLE memberships ADD COLUMN username TEXT")
        tenant_membership_columns = {row[1] for row in c.execute("PRAGMA table_info(tenant_memberships)").fetchall()}
        if "username" not in tenant_membership_columns:
            c.execute("ALTER TABLE tenant_memberships ADD COLUMN username TEXT")
        c.execute("""UPDATE memberships SET username=(
            SELECT NULLIF(username,'') FROM applications WHERE applications.user_id=memberships.user_id
        ) WHERE username IS NULL""")

        c.execute("""CREATE TABLE IF NOT EXISTS tenants(
            tenant_id TEXT PRIMARY KEY, tenant_name TEXT NOT NULL,
            status TEXT NOT NULL DEFAULT 'active', created_at TEXT NOT NULL)""")
        c.execute("""CREATE TABLE IF NOT EXISTS tenant_channels(
            channel_id INTEGER PRIMARY KEY, tenant_id TEXT NOT NULL,
            channel_name TEXT NOT NULL, channel_type TEXT NOT NULL,
            owner_user_id INTEGER, owner_username TEXT, status TEXT NOT NULL DEFAULT 'active')""")
        # Eski veritabanları için kanal sahibinin kullanıcı adı alanını ekle.
        channel_columns = {row[1] for row in c.execute("PRAGMA table_info(tenant_channels)").fetchall()}
        if "owner_username" not in channel_columns:
            c.execute("ALTER TABLE tenant_channels ADD COLUMN owner_username TEXT")

        c.execute("""CREATE TABLE IF NOT EXISTS tenant_admins(
            tenant_id TEXT NOT NULL, user_id INTEGER NOT NULL,
            role TEXT NOT NULL, channel_id INTEGER,
            status TEXT NOT NULL DEFAULT 'active', added_at TEXT NOT NULL,
            PRIMARY KEY(tenant_id,user_id,role,channel_id))""")
        c.execute("""CREATE TABLE IF NOT EXISTS tenant_memberships(
            tenant_id TEXT NOT NULL, channel_id INTEGER NOT NULL,
            user_id INTEGER NOT NULL, expires_at TEXT NOT NULL,
            status TEXT NOT NULL, username TEXT,
            PRIMARY KEY(tenant_id,channel_id,user_id))""")
        c.execute("""CREATE TABLE IF NOT EXISTS tenant_applications(
            application_id INTEGER PRIMARY KEY AUTOINCREMENT,
            tenant_id TEXT NOT NULL, channel_id INTEGER NOT NULL,
            user_id INTEGER NOT NULL, username TEXT, full_name TEXT,
            status TEXT NOT NULL, created_at TEXT NOT NULL)""")
        c.execute("""CREATE TABLE IF NOT EXISTS tenant_posts(
            job_id TEXT PRIMARY KEY, tenant_id TEXT NOT NULL,
            channel_id INTEGER NOT NULL, run_at TEXT NOT NULL,
            message TEXT NOT NULL, status TEXT NOT NULL)""")
        c.execute("""CREATE TABLE IF NOT EXISTS tenant_settings(
            tenant_id TEXT NOT NULL, setting_key TEXT NOT NULL,
            setting_value TEXT NOT NULL, updated_at TEXT NOT NULL,
            PRIMARY KEY(tenant_id,setting_key))""")
        c.execute("""CREATE TABLE IF NOT EXISTS support_contacts(
            contact_id INTEGER PRIMARY KEY AUTOINCREMENT,
            tenant_id TEXT NOT NULL, display_name TEXT NOT NULL,
            telegram_link TEXT NOT NULL, enabled INTEGER NOT NULL DEFAULT 1)""")
        c.execute("""CREATE TABLE IF NOT EXISTS link_items(
            link_id INTEGER PRIMARY KEY AUTOINCREMENT,
            tenant_id TEXT NOT NULL, category TEXT NOT NULL,
            display_name TEXT NOT NULL, url TEXT NOT NULL,
            enabled INTEGER NOT NULL DEFAULT 1)""")
        # Varsayılan SITE bağlantıları: yalnızca eksik kayıtları ekle.
        default_links = [
            ("site", "MASTERBETTING GİRİŞ", "https://zipt.to/y7BfPx"),
            ("site", "BİZBET GİRİŞ", "https://bizbet-28601.pro/tr/registration?tag=d_5908901m_62079c_"),
            ("social", "INSTAGRAM", "https://www.instagram.com/rodrianaliz?evlk=OHN3YjYzaHY5Mmlu"),
            ("social", "X", "https://x.com/kralrodriguez?s=11"),
            ("social", "THREADS", "https://www.threads.com/@rodrianaliz?igshid=NTc4MTIwNjQ2YQ=="),
            ("social", "KICK", "https://kick.com/kralrodriguez"),
        ]
        for tenant_key in ("altay_demo", "musteri_001"):
            for category, label, url in default_links:
                exists = c.execute(
                    "SELECT 1 FROM link_items WHERE tenant_id=? AND display_name=? LIMIT 1",
                    (tenant_key, label)
                ).fetchone()
                if not exists:
                    c.execute(
                        "INSERT INTO link_items(tenant_id,category,display_name,url,enabled) VALUES(?,?,?,?,1)",
                        (tenant_key, category, label, url)
                    )

        # Yalnızca test ortamı otomatik tanımlanır. Müşteri kanalları daha sonra
        # kullanıcı tarafından verilen gerçek ID'lerle eklenecek.
        c.execute("""INSERT OR IGNORE INTO tenants
            (tenant_id,tenant_name,status,created_at) VALUES(?,?,?,?)""",
            ("altay_demo", "ALTAY VIP - Deneme", "active", now().isoformat()))

        # Legacy test entries are retained for data compatibility but deactivated; real chats are registered with /kanalkaydet.
        c.execute("UPDATE tenant_channels SET status='inactive' WHERE channel_id IN (-5172524860,-1004367177649) AND tenant_id='altay_demo'")

        # Müşteri kayıt alanı hazır; gerçek müşteri kanalları daha sonra eklenecek.
        c.execute("""INSERT OR IGNORE INTO tenants
            (tenant_id,tenant_name,status,created_at) VALUES(?,?,?,?)""",
            ("musteri_001", "Müşteri 001", "active", now().isoformat()))

        # Keep the old single-channel setting separately identifiable.
        if CHANNEL_ID:
            try:
                legacy_channel = int(CHANNEL_ID)
                c.execute("""INSERT OR IGNORE INTO tenants
                    (tenant_id,tenant_name,status,created_at) VALUES(?,?,?,?)""",
                    ("legacy_single", "Eski ALTAY VIP", "active", now().isoformat()))
                c.execute("""INSERT OR IGNORE INTO tenant_channels
                    (channel_id,tenant_id,channel_name,channel_type,status)
                    VALUES(?,?,?,?,?)""",
                    (legacy_channel, "legacy_single", "Eski ana kanal", "legacy", "active"))
                c.execute("UPDATE tenants SET tenant_name='GOLDEN VIP', status='active' WHERE tenant_id='legacy_single'")
                c.execute("UPDATE tenant_channels SET channel_name='GOLDEN VIP' WHERE channel_id=? AND tenant_id='legacy_single'", (legacy_channel,))
                # Ana yöneticiyi GOLDEN VIP kanalının kayıtlı sahibi/yöneticisi olarak tanımla.
                for owner_id in ADMIN_IDS:
                    c.execute("UPDATE tenant_channels SET owner_user_id=? WHERE channel_id=? AND tenant_id='legacy_single'",
                              (owner_id, legacy_channel))
                    c.execute("""INSERT INTO tenant_admins
                        (tenant_id,user_id,role,channel_id,status,added_at)
                        VALUES('legacy_single',?,'owner',?,'active',?)
                        ON CONFLICT(tenant_id,user_id,role,channel_id)
                        DO UPDATE SET status='active'""",
                        (owner_id, legacy_channel, now().isoformat()))
                c.execute("UPDATE tenant_channels SET channel_type='vip' WHERE channel_id=? AND tenant_id='legacy_single' AND channel_type='legacy'", (legacy_channel,))
                c.execute("UPDATE tenant_channels SET tenant_id='legacy_single', channel_name='GOLDEN CLUB SOHBET' WHERE tenant_id='altay_demo' AND channel_type='chat' AND status='active'")
                c.execute("UPDATE tenants SET status='inactive' WHERE tenant_id IN ('altay_demo','musteri_001')")
            except ValueError:
                log.warning("CHANNEL_ID sayısal değil; eski kanal kaydı eklenemedi.")


async def cleanup_vip_invite_message(context, user_id, chat_id):
    """Remove the private invite message and revoke its link after joining."""
    with connect() as c:
        row = c.execute("""SELECT message_id,invite_link FROM vip_invite_messages
            WHERE user_id=? AND chat_id=?""", (user_id, chat_id)).fetchone()
    if not row:
        return
    try:
        await context.bot.delete_message(chat_id=user_id, message_id=int(row["message_id"]))
    except Exception:
        log.info("VIP davet mesajı silinemedi: user=%s chat=%s", user_id, chat_id)
    try:
        await context.bot.revoke_chat_invite_link(chat_id=chat_id, invite_link=row["invite_link"])
    except Exception:
        log.info("VIP davet bağlantısı iptal edilemedi: chat=%s", chat_id)
    with connect() as c:
        c.execute("DELETE FROM vip_invite_messages WHERE user_id=? AND chat_id=?", (user_id, chat_id))


def admin(uid):
    return uid in ADMIN_IDS


def tenant_permissions(uid):
    with connect() as c:
        return c.execute("""
            SELECT a.tenant_id, a.role, a.channel_id,
                   t.tenant_name, ch.channel_name
            FROM tenant_admins a
            JOIN tenants t ON t.tenant_id=a.tenant_id
            LEFT JOIN tenant_channels ch ON ch.channel_id=a.channel_id
            WHERE a.user_id=? AND a.status='active' AND t.status='active'
            ORDER BY t.tenant_name, ch.channel_name
        """, (uid,)).fetchall()


def tenant_ids_for_user(uid):
    return list(dict.fromkeys(str(r['tenant_id']) for r in tenant_permissions(uid)))


def current_tenant_id(uid, context):
    if admin(uid):
        selected = context.user_data.get('selected_tenant_id')
        if selected:
            with connect() as c:
                if c.execute('SELECT 1 FROM tenants WHERE tenant_id=? AND status=\'active\'', (selected,)).fetchone(): return selected
        return 'legacy_single'
    ids = tenant_ids_for_user(uid)
    selected = context.user_data.get('selected_tenant_id')
    if selected in ids: return selected
    return ids[0] if len(ids) == 1 else None


def tenant_panel_keyboard():
    return InlineKeyboardMarkup([
        [InlineKeyboardButton('👥 Üyelerim', callback_data='tenantpanel:members'), InlineKeyboardButton('⏳ Başvurularım', callback_data='tenantpanel:pending')],
        [InlineKeyboardButton('📡 Kanallarım', callback_data='tenantpanel:channels'), InlineKeyboardButton('💳 Ödemelerim', callback_data='tenantpanel:payments')],
        [InlineKeyboardButton('🔐 Yetkilerim', callback_data='tenantpanel:roles')]
    ])


def can_open_admin_panel(uid):
    return admin(uid) or bool(tenant_permissions(uid))


def admin_panel_keyboard():
    return InlineKeyboardMarkup([
        [InlineKeyboardButton("👥 VIP üyeler", callback_data="tenantpanel:members"),
         InlineKeyboardButton("⏳ Bekleyen başvurular", callback_data="tenantpanel:pending")],
        [InlineKeyboardButton("🏢 Müşteri ve kanallar", callback_data="tenantpanel:channels")],
        [InlineKeyboardButton("📅 Yaklaşan üyelik bitişleri", callback_data="tenantpanel:expiring")],
        [InlineKeyboardButton("💳 Ödeme takibi", callback_data="tenantpanel:payments")],
        [InlineKeyboardButton("📈 Gelir istatistikleri", callback_data="tenantpanel:revenue")],
        [InlineKeyboardButton("🧾 Yönetici işlem geçmişi", callback_data="tenantpanel:audit")],
        [InlineKeyboardButton("🛡 Yönetici yetkileri", callback_data="tenantpanel:admins"),
         InlineKeyboardButton("📣 Otomatik duyurular", callback_data="tenantpanel:autoposts")],
        [InlineKeyboardButton("🔐 Yetki kapsamım", callback_data="tenantpanel:roles"),
         InlineKeyboardButton("📊 Sistem durumu", callback_data="tenantpanel:status")],
    ])


def channel_id():
    if not CHANNEL_ID:
        return None
    try:
        return int(CHANNEL_ID)
    except ValueError:
        return CHANNEL_ID


def active_expiry(uid):
    with connect() as c:
        row = c.execute(
            "SELECT expires_at FROM memberships WHERE user_id=? AND status='active'",
            (uid,)
        ).fetchone()
    if not row:
        return None
    dt = datetime.fromisoformat(row[0])
    return dt if dt > now() else None


def admin_only(update):
    return bool(update.effective_user and admin(update.effective_user.id))


async def whoami(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text(f"Telegram kullanıcı ID: {update.effective_user.id}")


async def start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user = update.effective_user
    chat = update.effective_chat
    # /start grup içinde kullanılırsa menüyü gruba dökme; özel sohbet bağlantısı ver.
    if chat and chat.type in ("group", "supergroup"):
        bot_username = (await context.bot.get_me()).username
        if bot_username:
            kb = InlineKeyboardMarkup([[InlineKeyboardButton(
                "💎 GOLDEN VIP başvurusu",
                url=f"https://t.me/{bot_username}?start=vipc_{chat.id}"
            )]])
            await update.effective_message.reply_text(
                "👑 GOLDEN VIP BAŞVURUSU\n\nVIP üyelik başvurunu başlatmak ve üyelik işlemlerini yapmak için aşağıdaki butona bas. Başvuru özel mesaj üzerinden alınır.",
                reply_markup=kb
            )
        return
    # Kanal VIP butonundan gelen derin bağlantı: başvuru özel sohbette alınır,
    # böylece Telegram kullanıcı kimliği güvenilir biçimde elde edilir.
    if context.args and (context.args[0].startswith("vipc_") or context.args[0].startswith("vip_")):
        try:
            # Yeni bağlantı yalnızca kanal ID'sini taşır. Tenant ID'si veritabanından
            # okunur; böylece eski INSERT OR IGNORE kanal kayıtları bağlantıyı bozmaz.
            raw = context.args[0]
            if raw.startswith("vipc_"):
                channel_id_value = int(raw[len("vipc_"):])
            else:
                # Eski v7/v8 bağlantılarıyla geriye dönük uyumluluk.
                payload = raw[len("vip_"):]
                _, channel_id_text = payload.rsplit("_", 1)
                channel_id_value = int(channel_id_text)
            channel_row = tenant_for_channel(channel_id_value)
            if not channel_row:
                raise ValueError("unknown channel")
            tenant_id = channel_row[0]
            with connect() as c:
                existing = c.execute("""SELECT application_id,status FROM tenant_applications
                    WHERE tenant_id=? AND channel_id=? AND user_id=?
                    ORDER BY application_id DESC LIMIT 1""",
                    (tenant_id, channel_id_value, user.id)).fetchone()
                if existing and existing['status'] == 'pending':
                    await update.message.reply_text("VIP başvurun zaten yöneticilere iletilmiş.")
                    return
                cur = c.execute("""INSERT INTO tenant_applications
                    (tenant_id,channel_id,user_id,username,full_name,status,created_at)
                    VALUES(?,?,?,?,?,'pending',?)""",
                    (tenant_id, channel_id_value, user.id, user.username or '', user.full_name, now().isoformat()))
                app_id = cur.lastrowid
            kb = InlineKeyboardMarkup([[
                InlineKeyboardButton('✅ 30 gün onayla', callback_data=f'tenantapp:approve:{app_id}'),
                InlineKeyboardButton('❌ Reddet', callback_data=f'tenantapp:reject:{app_id}')
            ]])
            admin_text = (f'🆕 VIP başvurusu\nKanal: {channel_row[1]}\nAd: {user.full_name}\n'
                          f'Kullanıcı: @{user.username or "yok"}\nID: {user.id}')
            for aid in tenant_manager_ids(tenant_id, channel_id_value):
                try:
                    await context.bot.send_message(aid, admin_text, reply_markup=kb)
                except Exception:
                    log.exception('VIP başvurusu yöneticiye iletilemedi: %s', aid)
            await update.message.reply_text(' VIP başvurun yöneticilere özel olarak iletildi. Sonuç sana özel mesajla bildirilecek.')
            return
        except Exception:
            # Eski veya bozuk bir derin bağlantı /start ana menüsünü engellemesin.
            # Başvuru bağlantısı çözümlenemezse normal başlangıç ekranını göster.
            log.exception('VIP derin bağlantısı işlenemedi; normal /start menüsüne dönülüyor')
    exp = active_expiry(user.id)
    if exp:
        msg = f"Hoş geldin {user.first_name}! VIP üyeliğin aktif. Bitiş: {exp:%d.%m.%Y %H:%M}"
    else:
        msg = f"ALTAY VIP BOT'e hoş geldin, {user.first_name}!\nVIP üyelik başvurusu için aşağıdaki butona bas."
    kb = InlineKeyboardMarkup([
        [InlineKeyboardButton(" VIP Başvurusu", callback_data="apply")],
        [InlineKeyboardButton("📅 Üyelik Durumum", callback_data="status")],
        [InlineKeyboardButton("🆘 Destek", callback_data="support")],
    ])
    await update.message.reply_text(msg, reply_markup=kb)


async def approve(context, uid, days=30):
    if days < 1:
        raise ValueError("Gün sayısı en az 1 olmalı.")
    until = now() + timedelta(days=days)
    with connect() as c:
        app_row = c.execute("SELECT username FROM applications WHERE user_id=?", (uid,)).fetchone()
        username = (app_row["username"] or "").strip() if app_row else ""
        c.execute("""INSERT INTO memberships(user_id,expires_at,status,username) VALUES(?,?,'active',?)
        ON CONFLICT(user_id) DO UPDATE SET expires_at=excluded.expires_at,status='active',
        username=COALESCE(NULLIF(excluded.username,''),memberships.username)""",
        (uid, until.isoformat(), username))
        c.execute("UPDATE applications SET status='approved' WHERE user_id=?", (uid,))
    link = ""
    inv = None
    try:
        inv = await context.bot.create_chat_invite_link(
            chat_id=channel_id(), name=f"GOLDEN-VIP-{uid}"
        )
        link = "\n\nVIP kanala doğrudan katılım bağlantın:\n" + inv.invite_link
    except Exception:
        log.exception("Davet bağlantısı oluşturulamadı")
        link = "\n\nDavet bağlantısı oluşturulamadı. Yöneticiyle iletişime geç."
    try:
        sent = await context.bot.send_message(
            uid, f"🎉 VIP üyeliğin onaylandı!\nSüre: {days} gün\n"
                 f"Bitiş: {until:%d.%m.%Y %H:%M}{link}"
        )
        if inv:
            with connect() as c:
                c.execute("""INSERT OR REPLACE INTO vip_invite_messages
                    (user_id,chat_id,message_id,invite_link) VALUES(?,?,?,?)""",
                    (uid, int(channel_id()), sent.message_id, inv.invite_link))
    except Exception:
        log.exception("Kullanıcıya mesaj gönderilemedi")


async def buttons(update: Update, context: ContextTypes.DEFAULT_TYPE):
    q = update.callback_query
    await q.answer()
    uid = q.from_user.id
    if q.data == "apply":
        if active_expiry(uid):
            await q.message.reply_text("Zaten aktif VIP üyeliğin var.")
            return
        u = q.from_user
        with connect() as c:
            c.execute("""INSERT INTO applications VALUES(?,?,?,?,?)
            ON CONFLICT(user_id) DO UPDATE SET username=excluded.username,
            full_name=excluded.full_name,status='pending',created_at=excluded.created_at""",
            (uid, u.username or "", u.full_name, "pending", now().isoformat()))
        await q.message.reply_text("Başvurun alındı. Yönetici incelemesinden sonra bilgilendirileceksin.")
        kb = InlineKeyboardMarkup([[
            InlineKeyboardButton("✅ 30 gün onayla", callback_data=f"approve:{uid}"),
            InlineKeyboardButton("❌ Reddet", callback_data=f"reject:{uid}")
        ]])
        for aid in ADMIN_IDS:
            try:
                await context.bot.send_message(
                    aid, f"Yeni VIP başvurusu\nAd: {u.full_name}\n"
                         f"Kullanıcı: @{u.username or 'yok'}\nID: {uid}", reply_markup=kb
                )
            except Exception:
                log.exception("Yönetici bildirimi gönderilemedi")
    elif q.data == "status":
        exp = active_expiry(uid)
        await q.message.reply_text(
            f"VIP üyeliğin aktif. Bitiş: {exp:%d.%m.%Y %H:%M}"
            if exp else "Aktif VIP üyeliğin bulunmuyor."
        )
    elif q.data == "support":
        await list_support(update, context)


async def admin_callbacks(update: Update, context: ContextTypes.DEFAULT_TYPE):
    q = update.callback_query
    if not admin(q.from_user.id):
        await q.answer("Bu işlem yalnızca yöneticilere açık.", show_alert=True)
        return
    await q.answer()
    action, uid_s = q.data.split(":")
    uid = int(uid_s)
    # Eski veya daha önce kullanılmış butonlar ikinci kez işlem yapamasın.
    with connect() as c:
        application = c.execute(
            "SELECT status FROM applications WHERE user_id=?", (uid,)
        ).fetchone()
    if not application or application["status"] != "pending":
        await q.answer("Bu başvuru bulunamadı veya daha önce işleme alınmış.", show_alert=True)
        await q.edit_message_reply_markup(reply_markup=None)
        return
    if action == "approve":
        await approve(context, uid, 30)
        await q.edit_message_reply_markup(reply_markup=None)
        await q.message.reply_text(f"{uid} kullanıcısına 30 günlük VIP üyelik verildi.")
    elif action == "reject":
        with connect() as c:
            c.execute("UPDATE applications SET status='rejected' WHERE user_id=? AND status='pending'", (uid,))
        try:
            await context.bot.send_message(
                uid, "VIP başvurun onaylanmadı. Bilgi için yöneticiyle görüşebilirsin."
            )
        except Exception:
            pass
        await q.edit_message_reply_markup(reply_markup=None)
        await q.message.reply_text("Başvuru reddedildi.")


async def panel(update: Update, context: ContextTypes.DEFAULT_TYPE):
    uid = update.effective_user.id
    if not can_open_admin_panel(uid):
        await update.message.reply_text("Bu panel için yönetici yetkin bulunmuyor.")
        return
    if not admin(uid):
        tid = current_tenant_id(uid, context)
        if not tid:
            await update.message.reply_text('Birden fazla müşteride yetkin var. /musterisec MUSTERI_ID kullan.')
            return
        await update.message.reply_text('🏢 MÜŞTERİ YÖNETİM PANELİ\\n\\nYalnızca yetkili olduğun müşterinin kayıtlarını yönetebilirsin.', reply_markup=tenant_panel_keyboard())
        return
    await update.message.reply_text(
        "🛠 ALTAY VIP BOT — YÖNETİCİ PANELİ\n\n"
        "VIP üyeleri ve bekleyen başvuruları buradan yönetebilirsin.\n"
        "Müşteri/kanal kayıtlarını, yönetici yetkilerini ve sistem durumunu görüntüleyebilirsin.\n"
        "Mevcut üyelik kayıtları korunur; işlemler yetki kontrolüyle yürütülür.",
        reply_markup=admin_panel_keyboard()
    )


async def tenant_customer_panel_callback(update: Update, context: ContextTypes.DEFAULT_TYPE):
    q = update.callback_query
    uid = q.from_user.id
    tid = current_tenant_id(uid, context)
    if not tid or not manager_can_access(uid, tid):
        await q.answer('Müşteri erişimi bulunamadı.', show_alert=True); return
    await q.answer()
    if q.data == 'tenantpanel:home':
        await q.edit_message_text('MÜŞTERİ YÖNETİM PANELİ', reply_markup=tenant_panel_keyboard())
    elif q.data == 'tenantpanel:roles':
        rows = [r for r in tenant_permissions(uid) if r['tenant_id'] == tid]
        lines = ['YETKİLERİM'] + [f"• {r['role']} — {r['channel_name'] or 'Genel yetki'}" for r in rows]
        await q.edit_message_text('\\n'.join(lines), reply_markup=tenant_panel_keyboard())
    elif q.data == 'tenantpanel:channels':
        with connect() as c:
            rows = c.execute('SELECT channel_name,channel_id,channel_type FROM tenant_channels WHERE tenant_id=? AND status=\'active\'', (tid,)).fetchall()
        lines = ['KANALLARIM'] + [f"• {r['channel_name']} — {r['channel_type']} (ID: {r['channel_id']})" for r in rows]
        if not rows: lines.append('Henüz kanal kaydedilmemiş.')
        await q.edit_message_text('\\n'.join(lines), reply_markup=tenant_panel_keyboard())
    elif q.data == 'tenantpanel:members':
        if not manager_has_permission(uid, tid, None, 'members'):
            await q.answer('Üye görüntüleme yetkin yok.', show_alert=True); return
        with connect() as c:
            rows = c.execute('SELECT user_id,username,expires_at FROM tenant_memberships WHERE tenant_id=? AND status=\'active\' AND expires_at>? ORDER BY expires_at LIMIT 50', (tid,now().isoformat())).fetchall()
        lines = ['AKTİF VIP ÜYELERİM']
        for r in rows:
            exp = datetime.fromisoformat(r['expires_at']); lines.append(f"• {r['username'] or r['user_id']} (ID: {r['user_id']}) — {exp:%d.%m.%Y}")
        if not rows: lines.append('Aktif üye bulunmuyor.')
        await q.edit_message_text('\\n'.join(lines)[:3900], reply_markup=tenant_panel_keyboard())
    elif q.data == 'tenantpanel:pending':
        if not manager_has_permission(uid, tid, None, 'applications'):
            await q.answer('Başvuru görüntüleme yetkin yok.', show_alert=True); return
        with connect() as c:
            rows = c.execute('SELECT application_id,user_id,username,full_name FROM tenant_applications WHERE tenant_id=? AND status=\'pending\' ORDER BY created_at LIMIT 20', (tid,)).fetchall()
        lines = ['BEKLEYEN BAŞVURULARIM']; buttons = []
        for r in rows:
            lines.append(f"• {r['full_name'] or r['username'] or r['user_id']} — ID {r['user_id']}")
            buttons.append([InlineKeyboardButton(f"Onayla {r['user_id']}", callback_data=f"tenantapp:approve:{r['application_id']}"), InlineKeyboardButton('Reddet', callback_data=f"tenantapp:reject:{r['application_id']}")])
        if not rows: lines.append('Bekleyen başvuru yok.')
        buttons.append([InlineKeyboardButton('Panel', callback_data='tenantpanel:home')])
        await q.edit_message_text('\\n'.join(lines)[:3500], reply_markup=InlineKeyboardMarkup(buttons))
    elif q.data == 'tenantpanel:payments':
        if not manager_has_permission(uid, tid, None, 'payments'):
            await q.answer('Ödeme görüntüleme yetkin yok.', show_alert=True); return
        with connect() as c:
            total = c.execute('SELECT COUNT(*),COALESCE(SUM(amount),0) FROM membership_payments WHERE membership_type=\'tenant\' AND tenant_id=?', (tid,)).fetchone()
            rows = c.execute('SELECT user_id,amount,note,paid_at FROM membership_payments WHERE membership_type=\'tenant\' AND tenant_id=? ORDER BY paid_at DESC LIMIT 20', (tid,)).fetchall()
        lines = [f"ÖDEME RAPORU — {total[0]} kayıt / {total[1]:,.2f} TL"]
        for r in rows:
            when = datetime.fromisoformat(r['paid_at']); lines.append(f"• {r['user_id']} — {r['amount']:,.2f} TL — {when:%d.%m.%Y}")
        await q.edit_message_text('\\n'.join(lines)[:3900], reply_markup=tenant_panel_keyboard())
    else:
        await q.answer('Bu bölüm müşteri panelinde kullanılamıyor.', show_alert=True)


async def tenant_panel_callbacks(update: Update, context: ContextTypes.DEFAULT_TYPE):
    q = update.callback_query
    uid = q.from_user.id
    if not can_open_admin_panel(uid):
        await q.answer("Bu panel için yetkin yok.", show_alert=True)
        return
    if not admin(uid):
        await tenant_customer_panel_callback(update, context)
        return
    await q.answer()

    if q.data == "tenantpanel:home":
        await q.edit_message_text(
            "🛠 GOLDEN VIP — YÖNETİM PANELİ\n\nİşlem yapmak için aşağıdan bir bölüm seç.",
            reply_markup=admin_panel_keyboard()
        )
    elif q.data == "tenantpanel:channels":
        if admin(uid):
            with connect() as c:
                # Paneli açan ana yöneticinin Telegram kullanıcı adını kanala bağla.
                if q.from_user.username:
                    c.execute("""UPDATE tenant_channels SET owner_username=?
                        WHERE owner_user_id=? AND tenant_id='legacy_single'""",
                        (q.from_user.username, uid))
                tenants = c.execute(
                    "SELECT tenant_id,tenant_name,status FROM tenants WHERE status='active' ORDER BY tenant_name"
                ).fetchall()
                channels = c.execute("""
                    SELECT t.tenant_name, ch.channel_name, ch.channel_id,
                           ch.channel_type, ch.owner_user_id, ch.owner_username
                    FROM tenant_channels ch
                    JOIN tenants t ON t.tenant_id=ch.tenant_id
                    WHERE t.status='active' AND ch.status='active'
                    ORDER BY t.tenant_name, ch.channel_name
                """).fetchall()
            lines = ["🏢 MÜŞTERİLER"]
            lines += [f"• {t['tenant_name']} ({t['tenant_id']}) — {t['status']}" for t in tenants]
            lines += ["", "📡 KANALLAR"]
            lines += [
                f"• {r['tenant_name']} / {r['channel_name']}\n"
                f"  Kanal ID: {r['channel_id']} | Tür: {r['channel_type']}\n"
                f"  Kurucu ID: {r['owner_user_id'] or '—'} | Kullanıcı adı: "
                f"@{r['owner_username']}" if r['owner_username'] else
                f"  Kanal ID: {r['channel_id']} | Tür: {r['channel_type']}\n"
                f"  Kurucu ID: {r['owner_user_id'] or '—'} | Kullanıcı adı: kayıtlı değil"
                for r in channels
            ]
        else:
            rows = tenant_permissions(uid)
            lines = ["📡 YETKİLİ OLDUĞUN KANALLAR"]
            lines += [
                f"• {r['tenant_name']} / {r['channel_name'] or 'Genel'} "
                f"(ID: {r['channel_id'] or 'genel'})"
                for r in rows
            ]
        await q.edit_message_text("\n".join(lines), reply_markup=admin_panel_keyboard())

    elif q.data == "tenantpanel:roles":
        if admin(uid):
            text = (" Ana yönetici yetkin aktif.\n"
                    "Tüm müşteri ve kanal kayıtlarını görüntüleyebilirsin.\n"
                    "Ana yönetici ID: " + str(uid))
        else:
            rows = tenant_permissions(uid)
            text = "🔐 YETKİ KAPSAMIN\n" + "\n".join(
                f"• {r['tenant_name']} / {r['channel_name']} — {r['role']}"
                for r in rows
            )
        await q.edit_message_text(text, reply_markup=admin_panel_keyboard())

    elif q.data == "tenantpanel:members":
        current = now().isoformat()
        with connect() as c:
            legacy_rows = c.execute("""
                SELECT user_id,username,expires_at FROM memberships
                WHERE status='active' AND expires_at>? ORDER BY expires_at LIMIT 25
            """, (current,)).fetchall()
            tenant_rows = c.execute("""
                SELECT tm.user_id,tm.username,tm.expires_at,tm.channel_id,t.tenant_name,tc.channel_name
                FROM tenant_memberships tm
                LEFT JOIN tenants t ON t.tenant_id=tm.tenant_id
                LEFT JOIN tenant_channels tc ON tc.channel_id=tm.channel_id
                WHERE tm.status='active' AND tm.expires_at>? ORDER BY tm.expires_at LIMIT 25
            """, (current,)).fetchall()
            expired_legacy = c.execute("""
                SELECT COUNT(*) FROM memberships WHERE status='expired'
            """).fetchone()[0]
            expired_tenant = c.execute("""
                SELECT COUNT(*) FROM tenant_memberships WHERE status='expired'
            """).fetchone()[0]
        lines = ["👥 AKTİF VIP ÜYELER", "Detayını görmek için bir üyeye dokun:"]
        member_buttons = []
        for r in legacy_rows:
            dt = datetime.fromisoformat(r["expires_at"])
            lines.append(f"• ID: {r['user_id']} | Kullanıcı adı: @{r['username']}" if r['username'] else f"• ID: {r['user_id']} | Kullanıcı adı: yok")
            lines.append(f"  Eski ana kanal — bitiş {dt:%d.%m.%Y %H:%M}")
            member_buttons.append([InlineKeyboardButton(f"👤 {r['username'] or r['user_id']} — GOLDEN VIP", callback_data=f"tenantpanel:memberdetail:legacy:{r['user_id']}")])
        for r in tenant_rows:
            dt = datetime.fromisoformat(r["expires_at"])
            lines.append(f"• ID: {r['user_id']} | Kullanıcı adı: @{r['username']}" if r['username'] else f"• ID: {r['user_id']} | Kullanıcı adı: yok")
            lines.append(f"  {r['tenant_name'] or 'Müşteri'} / {r['channel_name'] or 'Kanal'} — bitiş {dt:%d.%m.%Y %H:%M}")
            member_buttons.append([InlineKeyboardButton(f"👤 {r['username'] or r['user_id']} — {r['channel_name'] or 'VIP'}", callback_data=f"tenantpanel:memberdetail:tenant:{r['channel_id']}:{r['user_id']}")])
        if not legacy_rows and not tenant_rows:
            lines.append("Aktif VIP üye bulunmuyor.")
        lines += ["", f"📦 Süresi dolmuş kayıt: {expired_legacy + expired_tenant}"]
        keyboard = member_buttons[:25] + [[InlineKeyboardButton("🏠 Ana panel", callback_data="tenantpanel:home")]]
        await q.edit_message_text("\n".join(lines)[:4000], reply_markup=InlineKeyboardMarkup(keyboard))


    elif q.data.startswith("tenantpanel:extend:ask:"):
        parts = q.data.split(":")
        try:
            kind = parts[3]
            if kind == "legacy" and len(parts) == 6:
                member_id, days = int(parts[4]), int(parts[5])
                scope = f"legacy:{member_id}"
                with connect() as conn:
                    row = conn.execute("SELECT expires_at,status FROM memberships WHERE user_id=?", (member_id,)).fetchone()
                label = "GOLDEN VIP"
            elif kind == "tenant" and len(parts) == 7:
                channel_value, member_id, days = int(parts[4]), int(parts[5]), int(parts[6])
                scope = f"tenant:{channel_value}:{member_id}"
                with connect() as conn:
                    row = conn.execute("""SELECT tm.expires_at,tm.status,tc.channel_name
                        FROM tenant_memberships tm LEFT JOIN tenant_channels tc ON tc.channel_id=tm.channel_id
                        WHERE tm.channel_id=? AND tm.user_id=?""", (channel_value, member_id)).fetchone()
                label = row["channel_name"] if row else "VIP kanal"
            else:
                raise ValueError()
            if days not in (30, 60, 90) or not row or row["status"] != "active" or datetime.fromisoformat(row["expires_at"]) <= now():
                await q.answer("Bu üyelik artık uzatılamıyor. Listeyi yenile.", show_alert=True)
                return
        except (ValueError, IndexError, TypeError):
            await q.answer("Üyelik bilgisi geçersiz.", show_alert=True)
            return
        await q.edit_message_text(
            f"⚠️ ÜYELİK UZATMA ONAYI\n\nÜye ID: {member_id}\nKanal: {label}\n"
            f"Uzatma: {days} gün\nMevcut bitiş: {datetime.fromisoformat(row['expires_at']):%d.%m.%Y %H:%M}\n\n"
            "Onaylıyor musun? Onay vermeden üyelik süresi değişmez.",
            reply_markup=InlineKeyboardMarkup([
                [InlineKeyboardButton("✅ Evet, uzat", callback_data=f"tenantpanel:extend:do:{scope}:{days}")],
                [InlineKeyboardButton("❌ Vazgeç", callback_data=f"tenantpanel:memberdetail:{kind}:{scope.split(':',1)[1]}")],
                [InlineKeyboardButton("🏠 Ana panel", callback_data="tenantpanel:home")]
            ])
        )

    elif q.data.startswith("tenantpanel:extend:do:"):
        parts = q.data.split(":")
        try:
            kind = parts[3]
            if kind == "legacy" and len(parts) == 6:
                member_id, days = int(parts[4]), int(parts[5])
                with connect() as conn:
                    row = conn.execute("SELECT expires_at,status FROM memberships WHERE user_id=?", (member_id,)).fetchone()
                    if not row or row["status"] != "active":
                        raise ValueError()
                    until = max(datetime.fromisoformat(row["expires_at"]), now()) + timedelta(days=days)
                    conn.execute("UPDATE memberships SET expires_at=? WHERE user_id=? AND status='active'", (until.isoformat(), member_id))
                label = "GOLDEN VIP"
                back = f"tenantpanel:memberdetail:legacy:{member_id}"
            elif kind == "tenant" and len(parts) == 7:
                channel_value, member_id, days = int(parts[4]), int(parts[5]), int(parts[6])
                with connect() as conn:
                    row = conn.execute("""SELECT expires_at,status FROM tenant_memberships
                        WHERE channel_id=? AND user_id=?""", (channel_value, member_id)).fetchone()
                    if not row or row["status"] != "active":
                        raise ValueError()
                    until = max(datetime.fromisoformat(row["expires_at"]), now()) + timedelta(days=days)
                    conn.execute("""UPDATE tenant_memberships SET expires_at=?
                        WHERE channel_id=? AND user_id=? AND status='active'""", (until.isoformat(), channel_value, member_id))
                    channel = conn.execute("SELECT channel_name FROM tenant_channels WHERE channel_id=?", (channel_value,)).fetchone()
                label = channel["channel_name"] if channel else "VIP kanal"
                back = f"tenantpanel:memberdetail:tenant:{channel_value}:{member_id}"
            else:
                raise ValueError()
            if days not in (30, 60, 90):
                raise ValueError()
        except (ValueError, IndexError, TypeError):
            await q.answer("Üyelik uzatılamadı; kayıt aktif olmayabilir.", show_alert=True)
            return
        log_admin_action(uid, "Panelden üyelik uzatma", member_id, f"{days} gün; {label}")
        try:
            await context.bot.send_message(member_id, f"🎉 VIP üyeliğin {days} gün uzatıldı.\nYeni bitiş: {until:%d.%m.%Y %H:%M}")
        except Exception:
            log.info("Üyelik uzatma bildirimi gönderilemedi: %s", member_id)
        await q.answer("Üyelik süresi güncellendi.")
        await q.edit_message_text(
            f"✅ ÜYELİK UZATILDI\n\nÜye ID: {member_id}\nKanal: {label}\n"
            f"Eklenen süre: {days} gün\nYeni bitiş: {until:%d.%m.%Y %H:%M}",
            reply_markup=InlineKeyboardMarkup([
                [InlineKeyboardButton("👤 Üye detayına dön", callback_data=back)],
                [InlineKeyboardButton("⬅️ VIP üye listesi", callback_data="tenantpanel:members")]
            ])
        )

    elif q.data.startswith("tenantpanel:userhistory:"):
        parts = q.data.split(":")
        try:
            kind = parts[2]
            if kind == "legacy" and len(parts) == 4:
                member_id = int(parts[3]); back = f"tenantpanel:memberdetail:legacy:{member_id}"
            elif kind == "tenant" and len(parts) == 5:
                channel_value = int(parts[3]); member_id = int(parts[4])
                back = f"tenantpanel:memberdetail:tenant:{channel_value}:{member_id}"
            else: raise ValueError()
            with connect() as conn:
                logs = conn.execute("""SELECT actor_id,action,details,created_at FROM admin_audit_log
                    WHERE target_user_id=? ORDER BY audit_id DESC LIMIT 20""", (member_id,)).fetchall()
                pays = conn.execute("""SELECT amount,note,paid_at FROM membership_payments
                    WHERE user_id=? ORDER BY payment_id DESC LIMIT 10""", (member_id,)).fetchall()
            lines = [f"🧾 ÜYE İŞLEM GEÇMİŞİ — {member_id}", "", "Yönetici işlemleri:"]
            for item in logs:
                stamp = datetime.fromisoformat(item["created_at"])
                lines.append(f"• {stamp:%d.%m.%Y %H:%M} — {item['action']} (Yönetici: {item['actor_id']})")
                if item["details"]: lines.append(f"  {item['details'][:120]}")
            if not logs: lines.append("Yönetici işlem kaydı yok.")
            lines += ["", "Ödemeler:"]
            for item in pays:
                stamp = datetime.fromisoformat(item["paid_at"])
                lines.append(f"• {stamp:%d.%m.%Y} — {item['amount']:,.2f} TL" + (f" ({item['note']})" if item["note"] else ""))
            if not pays: lines.append("Ödeme kaydı yok.")
            await q.edit_message_text("\n".join(lines)[:3900], reply_markup=InlineKeyboardMarkup([
                [InlineKeyboardButton("⬅️ Üye detayına dön", callback_data=back)],
                [InlineKeyboardButton("🏠 Ana panel", callback_data="tenantpanel:home")]]))
        except (ValueError, IndexError, TypeError):
            await q.answer("Üye geçmişi açılamadı.", show_alert=True)
            return

    elif q.data.startswith("tenantpanel:payadd:"):
        parts = q.data.split(":")
        try:
            kind = parts[2]
            if kind == "legacy" and len(parts) == 4:
                member_id = int(parts[3]); channel_value = int(channel_id() or 0); tenant_key = "legacy_single"
            elif kind == "tenant" and len(parts) == 5:
                channel_value = int(parts[3]); member_id = int(parts[4])
                with connect() as conn:
                    tr = conn.execute("SELECT tenant_id FROM tenant_channels WHERE channel_id=?", (channel_value,)).fetchone()
                if not tr: raise ValueError()
                tenant_key = tr["tenant_id"]
            else: raise ValueError()
            if not manager_has_permission(uid, tenant_key, channel_value, "payments"):
                await q.answer("Bu kanal için ödeme yetkin bulunmuyor.", show_alert=True); return
            context.user_data["pending_payment"] = {"kind":kind,"tenant_id":tenant_key,"channel_id":channel_value,"user_id":member_id,"created_by":uid}
        except (ValueError, IndexError, TypeError):
            await q.answer("Üye bilgisi geçersiz.", show_alert=True); return
        await q.edit_message_text(
            f"💳 ÖDEME KAYDI\n\nÜye ID: {member_id}\nÖdeme tutarını TL olarak yaz. İstersen sonuna açıklama ekle.\nÖrnek: 1500 Aylık VIP",
            reply_markup=InlineKeyboardMarkup([[InlineKeyboardButton("❌ Vazgeç", callback_data=f"tenantpanel:memberdetail:{kind}:{member_id}" if kind=="legacy" else f"tenantpanel:memberdetail:tenant:{channel_value}:{member_id}")]])
        )

    elif q.data.startswith("tenantpanel:payhistory:"):
        parts = q.data.split(":")
        try:
            kind = parts[2]
            if kind == "legacy" and len(parts) == 4:
                member_id=int(parts[3]); tenant_key="legacy_single"; channel_value=int(channel_id() or 0)
            elif kind == "tenant" and len(parts) == 5:
                channel_value=int(parts[3]); member_id=int(parts[4])
                with connect() as conn:
                    tr=conn.execute("SELECT tenant_id FROM tenant_channels WHERE channel_id=?", (channel_value,)).fetchone()
                if not tr: raise ValueError()
                tenant_key=tr["tenant_id"]
            else: raise ValueError()
            with connect() as conn:
                rows=conn.execute("""SELECT amount,note,paid_at,recorded_by FROM membership_payments
                    WHERE membership_type=? AND tenant_id=? AND channel_id=? AND user_id=?
                    ORDER BY paid_at DESC LIMIT 20""",(kind,tenant_key,channel_value,member_id)).fetchall()
                total=conn.execute("""SELECT COUNT(*),COALESCE(SUM(amount),0) FROM membership_payments
                    WHERE membership_type=? AND tenant_id=? AND channel_id=? AND user_id=?""",
                    (kind,tenant_key,channel_value,member_id)).fetchone()
            lines=[f"📒 ÖDEME GEÇMİŞİ — {member_id}","",f"Kayıt: {total[0]}",f"Toplam: {total[1]:,.2f} TL",""]
            for p in rows:
                when=datetime.fromisoformat(p["paid_at"])
                lines.append(f"• {p['amount']:,.2f} TL — {when:%d.%m.%Y %H:%M}"+(f" ({p['note']})" if p["note"] else ""))
            if not rows: lines.append("Bu üyeye ait ödeme kaydı yok.")
            back=f"tenantpanel:memberdetail:{kind}:{member_id}" if kind=="legacy" else f"tenantpanel:memberdetail:tenant:{channel_value}:{member_id}"
            await q.edit_message_text("\n".join(lines)[:4000],reply_markup=InlineKeyboardMarkup([
                [InlineKeyboardButton("⬅️ Üye detayına dön",callback_data=back)],
                [InlineKeyboardButton("🏠 Ana panel",callback_data="tenantpanel:home")]
            ]))
        except (ValueError,IndexError,TypeError):
            await q.answer("Ödeme geçmişi açılamadı.",show_alert=True); return

    elif q.data.startswith("tenantpanel:memberdetail:"):
        parts = q.data.split(":")
        if len(parts) == 4 and parts[2] == "legacy":
            member_id = int(parts[3])
            with connect() as conn:
                row = conn.execute("""
                    SELECT m.user_id,m.username,m.expires_at,m.status,a.full_name
                    FROM memberships m LEFT JOIN applications a ON a.user_id=m.user_id
                    WHERE m.user_id=?
                """, (member_id,)).fetchone()
            if not row:
                await q.edit_message_text("Üyelik kaydı bulunamadı.", reply_markup=admin_panel_keyboard())
                return
            expiry = datetime.fromisoformat(row["expires_at"])
            remaining = (expiry.date() - now().date()).days
            username = f"@{row['username']}" if row["username"] else "Kayıtlı değil"
            detail_text = (
                "👤 VIP ÜYE DETAYI\n\n"
                f"Ad soyad: {row['full_name'] or 'Kayıtlı değil'}\n"
                f"Kullanıcı adı: {username}\nTelegram ID: {row['user_id']}\n"
                f"Üyelik: GOLDEN VIP (ana kanal)\nDurum: {row['status']}\n"
                f"Bitiş: {expiry:%d.%m.%Y %H:%M}\nKalan süre: {max(0, remaining)} gün"
            )
        elif len(parts) == 5 and parts[2] == "tenant":
            channel_value = int(parts[3])
            member_id = int(parts[4])
            with connect() as conn:
                row = conn.execute("""
                    SELECT tm.user_id,tm.username,tm.expires_at,tm.status,
                           t.tenant_name,tc.channel_name,
                           (SELECT ta.full_name FROM tenant_applications ta
                            WHERE ta.tenant_id=tm.tenant_id AND ta.channel_id=tm.channel_id
                              AND ta.user_id=tm.user_id
                            ORDER BY ta.application_id DESC LIMIT 1) AS full_name
                    FROM tenant_memberships tm
                    LEFT JOIN tenants t ON t.tenant_id=tm.tenant_id
                    LEFT JOIN tenant_channels tc ON tc.channel_id=tm.channel_id
                    WHERE tm.channel_id=? AND tm.user_id=?
                """, (channel_value, member_id)).fetchone()
            if not row:
                await q.edit_message_text("Üyelik kaydı bulunamadı.", reply_markup=admin_panel_keyboard())
                return
            expiry = datetime.fromisoformat(row["expires_at"])
            remaining = (expiry.date() - now().date()).days
            username = f"@{row['username']}" if row["username"] else "Kayıtlı değil"
            detail_text = (
                "👤 VIP ÜYE DETAYI\n\n"
                f"Ad soyad: {row['full_name'] or 'Kayıtlı değil'}\n"
                f"Kullanıcı adı: {username}\nTelegram ID: {row['user_id']}\n"
                f"Müşteri: {row['tenant_name'] or 'Kayıtlı değil'}\n"
                f"Kanal: {row['channel_name'] or 'Kayıtlı değil'}\nDurum: {row['status']}\n"
                f"Bitiş: {expiry:%d.%m.%Y %H:%M}\nKalan süre: {max(0, remaining)} gün"
            )
        else:
            await q.answer("Geçersiz üye kaydı.", show_alert=True)
            return
        detail_text += "\n\nNot: Üyelik başlangıç tarihi mevcut veritabanında tutulmuyor."
        extend_kind = "legacy" if len(parts) == 4 else "tenant"
        extend_scope = f"{member_id}" if extend_kind == "legacy" else f"{channel_value}:{member_id}"
        extend_buttons = [
            [InlineKeyboardButton(f"➕ {days} gün uzat", callback_data=f"tenantpanel:extend:ask:{extend_kind}:{extend_scope}:{days}")]
            for days in (30, 60, 90)
        ]
        pay_scope = f"legacy:{member_id}" if extend_kind == "legacy" else f"tenant:{channel_value}:{member_id}"
        await q.edit_message_text(detail_text, reply_markup=InlineKeyboardMarkup(
            extend_buttons + [
                [InlineKeyboardButton("🧾 Üye işlem geçmişi", callback_data=f"tenantpanel:userhistory:{pay_scope}")],
                [InlineKeyboardButton("💳 Ödeme kaydet", callback_data=f"tenantpanel:payadd:{pay_scope}")],
                [InlineKeyboardButton("📒 Ödeme geçmişi", callback_data=f"tenantpanel:payhistory:{pay_scope}")],
                [InlineKeyboardButton("⬅️ VIP üye listesi", callback_data="tenantpanel:members")],
                [InlineKeyboardButton("🏠 Ana panel", callback_data="tenantpanel:home")]
            ]
        ))

    elif q.data == "tenantpanel:admins":
        if not admin(uid):
            await q.answer("Yalnızca ana yönetici.", show_alert=True); return
        with connect() as conn:
            rows = conn.execute("""SELECT a.user_id,a.role,a.tenant_id,a.channel_id,t.tenant_name,ch.channel_name
                FROM tenant_admins a LEFT JOIN tenants t ON t.tenant_id=a.tenant_id
                LEFT JOIN tenant_channels ch ON ch.channel_id=a.channel_id
                WHERE a.status='active' ORDER BY a.user_id LIMIT 40""").fetchall()
        lines = ["🛡 YÖNETİCİ YETKİLERİ", "",
                 "/yetkiver ID rol [KANAL_ID]", "/yetkikaldir ID [KANAL_ID]", "/yetkililer",
                 "Roller: owner, manager, moderator, finance, publisher", ""]
        for row in rows:
            lines.append(f"• {row['user_id']} — {row['role']} — {row['tenant_name'] or row['tenant_id']} / {row['channel_name'] or 'Genel'}")
        if not rows: lines.append("Ek yönetici kaydı yok.")
        await q.edit_message_text("\n".join(lines)[:4000], reply_markup=admin_panel_keyboard())

    elif q.data == "tenantpanel:autoposts":
        with connect() as conn:
            rows = conn.execute("SELECT announcement_id,send_time,message,enabled FROM recurring_announcements WHERE tenant_id='legacy_single' ORDER BY send_time").fetchall()
        lines = ["📣 OTOMATİK DUYURULAR", "", "/otomatikduyuru SS:DD | Mesaj", "/otomatikduyurular", "/otomatikduyurusil ID", ""]
        for row in rows: lines.append(f"#{row['announcement_id']} — {row['send_time']} — {'Aktif' if row['enabled'] else 'Kapalı'}\n{row['message'][:100]}")
        if not rows: lines.append("Henüz otomatik duyuru yok.")
        await q.edit_message_text("\n".join(lines)[:4000], reply_markup=admin_panel_keyboard())

    elif q.data == "tenantpanel:audit":
        with connect() as conn:
            rows = conn.execute("""SELECT actor_id,action,target_user_id,details,created_at
                FROM admin_audit_log ORDER BY audit_id DESC LIMIT 30""").fetchall()
        lines = ["🧾 YÖNETİCİ İŞLEM GEÇMİŞİ", "", "Son 30 işlem:"]
        for row in rows:
            stamp = datetime.fromisoformat(row["created_at"])
            target = f" | Üye: {row['target_user_id']}" if row["target_user_id"] else ""
            extra = f" | {row['details']}" if row["details"] else ""
            lines.append(f"• {stamp:%d.%m %H:%M} | Yönetici: {row['actor_id']} | {row['action']}{target}{extra}")
        if not rows:
            lines.append("Henüz kaydedilmiş yönetici işlemi yok.")
        await q.edit_message_text("\n".join(lines)[:4000], reply_markup=admin_panel_keyboard())

    elif q.data == "tenantpanel:revenue":
        current = now()
        day_start = current.replace(hour=0, minute=0, second=0, microsecond=0).isoformat()
        month_start = current.replace(day=1, hour=0, minute=0, second=0, microsecond=0).isoformat()
        with connect() as conn:
            day = conn.execute("""SELECT COUNT(*),COALESCE(SUM(amount),0)
                FROM membership_payments WHERE paid_at>=?""", (day_start,)).fetchone()
            month = conn.execute("""SELECT COUNT(*),COALESCE(SUM(amount),0)
                FROM membership_payments WHERE paid_at>=?""", (month_start,)).fetchone()
            all_time = conn.execute("""SELECT COUNT(*),COALESCE(SUM(amount),0)
                FROM membership_payments""").fetchone()
            monthly = conn.execute("""SELECT substr(paid_at,1,7) AS period,
                COUNT(*) AS count,SUM(amount) AS total FROM membership_payments
                GROUP BY substr(paid_at,1,7) ORDER BY period DESC LIMIT 6""").fetchall()
        lines = [
            "📈 GELİR İSTATİSTİKLERİ", "",
            f"Bugün: {day[1]:,.2f} TL ({day[0]} ödeme)",
            f"Bu ay: {month[1]:,.2f} TL ({month[0]} ödeme)",
            f"Genel toplam: {all_time[1]:,.2f} TL ({all_time[0]} ödeme)",
            "", "Son 6 ay:"
        ]
        for item in monthly:
            lines.append(f"• {item['period']}: {item['total']:,.2f} TL ({item['count']} ödeme)")
        if not monthly:
            lines.append("Henüz ödeme kaydı bulunmuyor.")
        await q.edit_message_text("\n".join(lines)[:4000], reply_markup=admin_panel_keyboard())

    elif q.data == "tenantpanel:payments":
        with connect() as conn:
            totals = conn.execute("SELECT COUNT(*),COALESCE(SUM(amount),0) FROM membership_payments").fetchone()
            recent = conn.execute("""SELECT user_id,amount,note,paid_at FROM membership_payments
                ORDER BY paid_at DESC LIMIT 10""").fetchall()
        lines = ["💳 ÖDEME TAKİBİ", "", f"Toplam kayıt: {totals[0]}", f"Toplam tahsilat: {totals[1]:,.2f} TL", "", "Son ödemeler:"]
        for p in recent:
            when = datetime.fromisoformat(p["paid_at"])
            note = f" — {p['note']}" if p["note"] else ""
            lines.append(f"• ID {p['user_id']} | {p['amount']:,.2f} TL | {when:%d.%m.%Y}{note}")
        if not recent:
            lines.append("Henüz ödeme kaydı yok.")
        await q.edit_message_text("\n".join(lines)[:4000], reply_markup=admin_panel_keyboard())

    elif q.data == "tenantpanel:expiring":
        current_dt = now()
        horizon = (current_dt + timedelta(days=7)).isoformat()
        current = current_dt.isoformat()
        with connect() as c:
            legacy_rows = c.execute("""
                SELECT user_id,username,expires_at FROM memberships
                WHERE status='active' AND expires_at>? AND expires_at<=?
                ORDER BY expires_at LIMIT 100
            """, (current, horizon)).fetchall()
            tenant_rows = c.execute("""
                SELECT tm.user_id,tm.username,tm.expires_at,t.tenant_name,tc.channel_name
                FROM tenant_memberships tm
                LEFT JOIN tenants t ON t.tenant_id=tm.tenant_id
                LEFT JOIN tenant_channels tc ON tc.channel_id=tm.channel_id
                WHERE tm.status='active' AND tm.expires_at>? AND tm.expires_at<=?
                ORDER BY tm.expires_at LIMIT 100
            """, (current, horizon)).fetchall()

        lines = ["📅 YAKLAŞAN ÜYELİK BİTİŞLERİ", "Önümüzdeki 7 gün içinde süresi dolacak üyeler:", ""]
        for r in legacy_rows:
            expiry = datetime.fromisoformat(r["expires_at"])
            remaining = max(0, (expiry.date() - current_dt.date()).days)
            username = f"@{r['username']}" if r["username"] else "kullanıcı adı yok"
            lines.append(
                f"• ID: {r['user_id']} | {username}\n"
                f"  GOLDEN VIP — {expiry:%d.%m.%Y %H:%M} ({remaining} gün kaldı)"
            )
        for r in tenant_rows:
            expiry = datetime.fromisoformat(r["expires_at"])
            remaining = max(0, (expiry.date() - current_dt.date()).days)
            username = f"@{r['username']}" if r["username"] else "kullanıcı adı yok"
            lines.append(
                f"• ID: {r['user_id']} | {username}\n"
                f"  {r['tenant_name'] or 'Müşteri'} / {r['channel_name'] or 'VIP kanal'}"
                f" — {expiry:%d.%m.%Y %H:%M} ({remaining} gün kaldı)"
            )
        if not legacy_rows and not tenant_rows:
            lines.append("Önümüzdeki 7 gün içinde bitecek aktif üyelik yok.")
        await q.edit_message_text("\n".join(lines)[:4000], reply_markup=admin_panel_keyboard())

    elif q.data == "tenantpanel:pending":
        with connect() as c:
            legacy = c.execute("""
                SELECT user_id,username,full_name,created_at FROM applications
                WHERE status='pending' ORDER BY created_at LIMIT 15
            """).fetchall()
            tenant = c.execute("""
                SELECT application_id,tenant_id,channel_id,user_id,username,full_name,created_at
                FROM tenant_applications WHERE status='pending'
                ORDER BY created_at LIMIT 15
            """).fetchall()
        lines = ["⏳ BEKLEYEN VIP BAŞVURULARI"]
        keyboard = []
        for r in legacy:
            label = r["full_name"] or r["username"] or str(r["user_id"])
            lines.append(f"• {label} — ID: {r['user_id']} (ana kanal)")
            keyboard.append([
                InlineKeyboardButton(f"✅ {r['user_id']} onayla", callback_data=f"approve:{r['user_id']}"),
                InlineKeyboardButton("❌ Reddet", callback_data=f"reject:{r['user_id']}")
            ])
        for r in tenant:
            label = r["full_name"] or r["username"] or str(r["user_id"])
            lines.append(f"• {label} — ID: {r['user_id']} ({r['tenant_id']})")
            keyboard.append([
                InlineKeyboardButton(f"✅ {r['user_id']} onayla", callback_data=f"tenantapp:approve:{r['application_id']}"),
                InlineKeyboardButton("❌ Reddet", callback_data=f"tenantapp:reject:{r['application_id']}")
            ])
        if not legacy and not tenant:
            lines.append("Bekleyen başvuru yok.")
        keyboard.append([InlineKeyboardButton("↩️ Yönetim paneli", callback_data="tenantpanel:status")])
        await q.edit_message_text("\n".join(lines)[:4000], reply_markup=InlineKeyboardMarkup(keyboard))

    elif q.data == "tenantpanel:status":
        with connect() as c:
            tenant_count = c.execute(
                "SELECT COUNT(*) FROM tenants WHERE status='active'"
            ).fetchone()[0]
            channel_count = c.execute(
                "SELECT COUNT(*) FROM tenant_channels WHERE status='active'"
            ).fetchone()[0]
            admin_count = c.execute(
                "SELECT COUNT(DISTINCT user_id) FROM tenant_admins WHERE status='active'"
            ).fetchone()[0]
            legacy_counts = {}
            for table in ("applications", "memberships", "posts"):
                legacy_counts[table] = c.execute(
                    "SELECT COUNT(*) FROM " + table
                ).fetchone()[0]
        text = (
            "📊 SİSTEM DURUMU\n\n"
            f"Aktif müşteri: {tenant_count}\n"
            f"Aktif kanal kaydı: {channel_count}\n"
            f"Kayıtlı kanal yöneticisi: {admin_count}\n\n"
            "Eski kayıt sayıları (korunuyor):\n"
            f"Başvuru: {legacy_counts['applications']}\n"
            f"Üyelik: {legacy_counts['memberships']}\n"
            f"Planlı paylaşım: {legacy_counts['posts']}"
        )
        await q.edit_message_text(text, reply_markup=admin_panel_keyboard())


async def members(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """List active legacy and tenant VIP memberships for the main admin."""
    if not admin_only(update):
        return
    current = now().isoformat()
    with connect() as c:
        legacy_rows = c.execute(
            "SELECT user_id,username,expires_at FROM memberships WHERE status='active' AND expires_at>? ORDER BY expires_at",
            (current,)
        ).fetchall()
        tenant_rows = c.execute("""
            SELECT tm.tenant_id, t.tenant_name, tm.channel_id, tc.channel_name,
                   tm.user_id, tm.username, tm.expires_at
            FROM tenant_memberships tm
            LEFT JOIN tenants t ON t.tenant_id=tm.tenant_id
            LEFT JOIN tenant_channels tc ON tc.channel_id=tm.channel_id
            WHERE tm.status='active' AND tm.expires_at>?
            ORDER BY tm.expires_at
        """, (current,)).fetchall()
    lines = []
    if tenant_rows:
        lines.append("🏆 AKTİF VIP ÜYELER")
        for r in tenant_rows[:100]:
            expiry = datetime.fromisoformat(r['expires_at'])
            lines.append(
                (f"• ID: {r['user_id']} | Kullanıcı adı: @{r['username']}" if r['username'] else f"• ID: {r['user_id']} | Kullanıcı adı: yok") + "\n"
                + f"  {expiry:%d.%m.%Y %H:%M} — {r['tenant_name'] or r['tenant_id']} / {r['channel_name'] or r['channel_id']}"
            )
    if legacy_rows:
        lines.append("\n📋 Eski sistem üyeleri")
        for r in legacy_rows[:100]:
            expiry = datetime.fromisoformat(r['expires_at'])
            lines.append(f"• ID: {r['user_id']} | Kullanıcı adı: @{r['username']}" if r['username'] else f"• ID: {r['user_id']} | Kullanıcı adı: yok")
            lines.append(f"  Bitiş: {expiry:%d.%m.%Y %H:%M}")
    if not lines:
        await update.message.reply_text("Aktif VIP üye yok.")
        return
    await update.message.reply_text("\n".join(lines)[:4000])


async def grant(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not admin_only(update):
        return
    if len(context.args) != 2:
        await update.message.reply_text("Kullanım: /onayla KULLANICI_ID GÜN")
        return
    try:
        uid, days = int(context.args[0]), int(context.args[1])
        if uid <= 0 or not 1 <= days <= 3650:
            raise ValueError()
    except ValueError:
        await update.message.reply_text("Geçerli kullanıcı ID'si ve 1–3650 arası gün gir.")
        return
    await approve(context, uid, days)
    log_admin_action(update.effective_user.id, "Üyelik tanımlama", uid, f"{days} gün")
    await update.message.reply_text(f"{uid} kullanıcısına {days} günlük üyelik tanımlandı.")


async def extend_membership(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not admin_only(update):
        return
    if len(context.args) != 2:
        await update.message.reply_text("Kullanım: /uzat KULLANICI_ID GÜN")
        return
    try:
        uid, days = int(context.args[0]), int(context.args[1])
        if uid <= 0 or not 1 <= days <= 3650:
            raise ValueError()
    except ValueError:
        await update.message.reply_text("Geçerli kullanıcı ID'si ve 1–3650 arası gün gir.")
        return

    current = now()
    updated = []
    with connect() as c:
        legacy = c.execute(
            "SELECT expires_at FROM memberships WHERE user_id=? AND status='active'", (uid,)
        ).fetchone()
        if legacy:
            base = datetime.fromisoformat(legacy["expires_at"])
            until = max(base, current) + timedelta(days=days)
            c.execute("UPDATE memberships SET expires_at=? WHERE user_id=? AND status='active'",
                      (until.isoformat(), uid))
            updated.append(("GOLDEN VIP", until))
        tenant_rows = c.execute(
            """SELECT tenant_id,channel_id,expires_at FROM tenant_memberships
               WHERE user_id=? AND status='active'""", (uid,)
        ).fetchall()
        for row in tenant_rows:
            base = datetime.fromisoformat(row["expires_at"])
            until = max(base, current) + timedelta(days=days)
            c.execute("""UPDATE tenant_memberships SET expires_at=?
                         WHERE tenant_id=? AND channel_id=? AND user_id=? AND status='active'""",
                      (until.isoformat(), row["tenant_id"], row["channel_id"], uid))
            channel = c.execute(
                "SELECT channel_name FROM tenant_channels WHERE channel_id=?",
                (row["channel_id"],)
            ).fetchone()
            updated.append((channel["channel_name"] if channel else row["tenant_id"], until))
    if not updated:
        await update.message.reply_text("Bu kullanıcı için uzatılabilecek aktif VIP üyeliği bulunamadı. Yeni üyelik vermek için /onayla kullan.")
        return
    log_admin_action(update.effective_user.id, "Üyelik uzatma", uid, f"{days} gün; {len(updated)} üyelik")
    try:
        detail = "\n".join(f"• {name}: {until:%d.%m.%Y %H:%M}" for name, until in updated)
        await context.bot.send_message(
            uid, f"🎉 VIP üyeliklerin {days} gün uzatıldı.\nYeni bitiş tarihleri:\n{detail}"
        )
    except Exception:
        log.info("Üyelik uzatma bildirimi gönderilemedi: %s", uid)
    detail = "\n".join(f"• {name}: {until:%d.%m.%Y %H:%M}" for name, until in updated)
    await update.message.reply_text(
        f"{uid} kullanıcısının {len(updated)} VIP üyeliği {days} gün uzatıldı.\n{detail}"
    )


async def remove_member(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not admin_only(update):
        return
    if len(context.args) != 1:
        await update.message.reply_text("Kullanım: /sil KULLANICI_ID")
        return
    try:
        uid = int(context.args[0])
        if uid <= 0:
            raise ValueError()
    except ValueError:
        await update.message.reply_text("Geçerli bir kullanıcı ID'si gir.")
        return

    if uid in ADMIN_IDS:
        await update.message.reply_text("⛔ Güvenlik koruması: Ana yönetici hesabı bu komutla silinemez veya kanaldan çıkarılamaz.")
        return

    with connect() as c:
        vip_channels = [int(r[0]) for r in c.execute(
            "SELECT channel_id FROM tenant_memberships WHERE user_id=? AND status='active'",
            (uid,)
        ).fetchall()]
        legacy_cur = c.execute(
            "UPDATE memberships SET status='revoked' WHERE user_id=? AND status='active'", (uid,)
        )
        tenant_cur = c.execute(
            "UPDATE tenant_memberships SET status='revoked' WHERE user_id=? AND status='active'", (uid,)
        )
    removed_count = 0
    targets = set(vip_channels)
    try:
        targets.add(channel_id())
    except Exception:
        pass
    for target in targets:
        try:
            await context.bot.ban_chat_member(target, uid)
            await context.bot.unban_chat_member(target, uid, only_if_banned=True)
            removed_count += 1
        except Exception:
            log.exception("Kullanıcı VIP kanalından çıkarılamadı: %s / %s", target, uid)
    removed = removed_count > 0
    changed_count = legacy_cur.rowcount + tenant_cur.rowcount
    log_admin_action(update.effective_user.id, "Üyelik iptali", uid, f"{changed_count} kayıt; kanaldan çıkarma denendi")
    for target in targets:
        await cleanup_vip_invite_message(context, uid, int(target))

    try:
        await context.bot.send_message(
            uid, "VIP üyeliğin yönetici tarafından sonlandırıldı. Bilgi için yöneticiyle görüş."
        )
    except Exception:
        pass

    if changed_count:
        result = "Üyelik iptal edildi."
    else:
        result = "Aktif üyelik kaydı bulunamadı; yine de kanaldan çıkarma denendi."
    result += "\nKanaldan çıkarma: " + ("başarılı" if removed else "başarısız; bot izinlerini kontrol et")
    await update.message.reply_text(result)


async def stats(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not admin_only(update):
        return
    with connect() as c:
        active = c.execute(
            "SELECT COUNT(*) FROM memberships WHERE status='active' AND expires_at>?",
            (now().isoformat(),)
        ).fetchone()[0]
        pending = c.execute(
            "SELECT COUNT(*) FROM applications WHERE status='pending'"
        ).fetchone()[0]
        approved = c.execute(
            "SELECT COUNT(*) FROM applications WHERE status='approved'"
        ).fetchone()[0]
        rejected = c.execute(
            "SELECT COUNT(*) FROM applications WHERE status='rejected'"
        ).fetchone()[0]
        scheduled = c.execute(
            "SELECT COUNT(*) FROM posts WHERE status='pending'"
        ).fetchone()[0]
        sent = c.execute(
            "SELECT COUNT(*) FROM posts WHERE status='sent'"
        ).fetchone()[0]
    await update.message.reply_text(
        "📊 ALTAY VIP İSTATİSTİK\n\n"
        f" Aktif VIP üye: {active}\n"
        f"⏳ Bekleyen başvuru: {pending}\n"
        f"✅ Onaylanan başvuru: {approved}\n"
        f"❌ Reddedilen başvuru: {rejected}\n"
        f"🗓 Bekleyen duyuru: {scheduled}\n"
        f"📨 Gönderilen planlı duyuru: {sent}"
    )


async def broadcast(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not admin_only(update):
        return
    msg = " ".join(context.args).strip()
    if not msg:
        await update.message.reply_text("Kullanım: /duyuru Duyuru metni")
        return
    try:
        await context.bot.send_message(channel_id(), msg)
        await update.message.reply_text("Duyuru kanala gönderildi.")
    except Exception:
        log.exception("Duyuru gönderilemedi")
        await update.message.reply_text(
            "Gönderilemedi. Botun kanal yöneticisi ve mesaj gönderme izni olduğunu kontrol et."
        )


async def send_scheduled(context):
    data = context.job.data
    try:
        await context.bot.send_message(channel_id(), data["message"])
        status = "sent"
    except Exception:
        log.exception("Planlı duyuru gönderilemedi")
        status = "failed"
    with connect() as c:
        c.execute("UPDATE posts SET status=? WHERE job_id=?", (status, data["job_id"]))


async def plan(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not admin_only(update):
        return
    raw = update.message.text.partition(" ")[2].strip()
    if "|" not in raw:
        await update.message.reply_text("Örnek: /planla 2026-12-01 20:30 | Duyuru metni")
        return
    date_s, msg = [x.strip() for x in raw.split("|", 1)]
    try:
        when = datetime.strptime(date_s, "%Y-%m-%d %H:%M").replace(tzinfo=TZ)
        if when <= now() or not msg:
            raise ValueError()
    except ValueError:
        await update.message.reply_text(
            "Gelecekte bir tarih ve mesaj gir. Biçim: YYYY-AA-GG SS:DD | Mesaj"
        )
        return
    jid = f"post_{int(when.timestamp())}"
    with connect() as c:
        c.execute(
            "INSERT OR REPLACE INTO posts VALUES(?,?,?,'pending')",
            (jid, when.isoformat(), msg)
        )
    context.job_queue.run_once(
        send_scheduled, when=max(0, (when-now()).total_seconds()),
        data={"job_id": jid, "message": msg}, name=jid
    )
    await update.message.reply_text(
        f"Duyuru planlandı.\nID: {jid}\nTarih: {when:%d.%m.%Y %H:%M}"
    )


async def plans(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not admin_only(update):
        return
    with connect() as c:
        rows = c.execute(
            "SELECT job_id,run_at FROM posts WHERE status='pending' ORDER BY run_at"
        ).fetchall()
    await update.message.reply_text(
        "Bekleyen plan yok." if not rows else "Planlı duyurular:\n" + "\n".join(
            f"• {j} — {datetime.fromisoformat(t):%d.%m.%Y %H:%M}" for j, t in rows
        )
    )


async def cancel(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not admin_only(update):
        return
    if len(context.args) != 1:
        await update.message.reply_text("Kullanım: /iptal PLAN_ID")
        return
    jid = context.args[0]
    for job in context.job_queue.get_jobs_by_name(jid):
        job.schedule_removal()
    with connect() as c:
        cur = c.execute(
            "UPDATE posts SET status='cancelled' WHERE job_id=? AND status='pending'", (jid,)
        )
    await update.message.reply_text(
        "Plan iptal edildi." if cur.rowcount else "Bekleyen plan bulunamadı."
    )


async def expire(context: ContextTypes.DEFAULT_TYPE):
    with connect() as c:
        rows = c.execute(
            "SELECT user_id FROM memberships WHERE status='active' AND expires_at<=?",
            (now().isoformat(),)
        ).fetchall()
    for (uid,) in rows:
        try:
            await context.bot.ban_chat_member(channel_id(), uid)
            await context.bot.unban_chat_member(channel_id(), uid, only_if_banned=True)
        except Exception:
            log.exception("Kullanıcı kanaldan çıkarılamadı: %s", uid)
        with connect() as c:
            c.execute(
                "UPDATE memberships SET status='expired' WHERE user_id=?", (uid,)
            )
        try:
            await context.bot.send_message(
                uid, "VIP üyeliğinin süresi doldu. Yenilemek için /start kullan."
            )
        except Exception:
            pass


async def expire_tenant_memberships(context: ContextTypes.DEFAULT_TYPE):
    """Süresi dolan müşteri VIP üyelerini yalnızca kayıtlı VIP kanalından çıkarır."""
    with connect() as c:
        rows = c.execute("""SELECT tenant_id,channel_id,user_id FROM tenant_memberships
            WHERE status='active' AND expires_at<=?""", (now().isoformat(),)).fetchall()
    for row in rows:
        try:
            await context.bot.ban_chat_member(chat_id=int(row['channel_id']), user_id=int(row['user_id']))
            await context.bot.unban_chat_member(chat_id=int(row['channel_id']), user_id=int(row['user_id']), only_if_banned=True)
        except Exception:
            log.exception('Müşteri VIP kanalından süresi dolan üye çıkarılamadı: %s / %s', row['channel_id'], row['user_id'])
            continue
        with connect() as c:
            c.execute("""UPDATE tenant_memberships SET status='expired'
                WHERE tenant_id=? AND channel_id=? AND user_id=?""",
                (row['tenant_id'], row['channel_id'], row['user_id']))
        try:
            await context.bot.send_message(int(row['user_id']), 'VIP üyeliğinin süresi doldu.')
        except Exception:
            log.info('Süre bitiş bildirimi kullanıcıya gönderilemedi: %s', row['user_id'])


async def handle_vip_join_request(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Approve join requests only for users with an active, unexpired VIP membership."""
    req = update.chat_join_request
    if not req:
        return
    chat_id = int(req.chat.id)
    user_id = int(req.from_user.id)
    current = now().isoformat()
    with connect() as c:
        tenant_ok = c.execute("""SELECT 1 FROM tenant_memberships
            WHERE channel_id=? AND user_id=? AND status='active' AND expires_at>?
            LIMIT 1""", (chat_id, user_id, current)).fetchone()
        legacy_ok = None
        if CHANNEL_ID and str(chat_id) == str(CHANNEL_ID):
            legacy_ok = c.execute("""SELECT 1 FROM memberships
                WHERE user_id=? AND status='active' AND expires_at>? LIMIT 1""",
                (user_id, current)).fetchone()
    if tenant_ok or legacy_ok:
        try:
            await context.bot.approve_chat_join_request(chat_id=chat_id, user_id=user_id)
            log.info("VIP katılım isteği onaylandı: chat=%s user=%s", chat_id, user_id)
        except Exception:
            log.exception("VIP katılım isteği onaylanamadı: chat=%s user=%s", chat_id, user_id)
    else:
        try:
            await context.bot.decline_chat_join_request(chat_id=chat_id, user_id=user_id)
            log.info("Yetkisiz VIP katılım isteği reddedildi: chat=%s user=%s", chat_id, user_id)
        except Exception:
            log.exception("Yetkisiz VIP katılım isteği reddedilemedi: chat=%s user=%s", chat_id, user_id)


async def enforce_vip_membership(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Welcome new discussion-group members and enforce access only in VIP channels."""
    cm = update.chat_member
    if not cm or not cm.new_chat_member or cm.new_chat_member.status not in ("member", "administrator", "creator"):
        return
    chat_id = update.effective_chat.id
    user_id = cm.new_chat_member.user.id
    if cm.old_chat_member and cm.old_chat_member.status in ("member", "administrator", "creator"):
        return
    registered = tenant_for_channel(chat_id)
    if not registered: return
    tenant_key, channel_name, channel_type = registered
    if channel_type == "chat":
        with connect() as conn:
            setting = conn.execute("SELECT setting_value FROM tenant_settings WHERE tenant_id=? AND setting_key='welcome_message'",
                                   (tenant_key,)).fetchone()
        welcome = setting[0] if setting else "👋 Hoş geldin {name}! GOLDEN CLUB sohbetimize hoş geldin."
        try:
            await context.bot.send_message(chat_id, welcome.replace("{name}", cm.new_chat_member.user.first_name or "arkadaşımız"))
        except Exception:
            log.exception("Karşılama mesajı gönderilemedi: %s", chat_id)
        return
    if channel_type != "vip": return
    current = now().isoformat()
    with connect() as c:
        tenant_ok = c.execute("""SELECT 1 FROM tenant_memberships
            WHERE channel_id=? AND user_id=? AND status='active' AND expires_at>?
            LIMIT 1""", (chat_id, user_id, current)).fetchone()
        legacy_ok = None
        if CHANNEL_ID and str(chat_id) == str(CHANNEL_ID):
            legacy_ok = c.execute("""SELECT 1 FROM memberships
                WHERE user_id=? AND status='active' AND expires_at>? LIMIT 1""",
                (user_id, current)).fetchone()
    if tenant_ok or legacy_ok:
        await cleanup_vip_invite_message(context, user_id, int(chat_id))
        return
    await cleanup_vip_invite_message(context, user_id, int(chat_id))
    try:
        await context.bot.ban_chat_member(chat_id=chat_id, user_id=user_id)
        await context.bot.unban_chat_member(chat_id=chat_id, user_id=user_id, only_if_banned=True)
        log.info("Yetkisiz VIP yeniden katılım engellendi: chat=%s user=%s", chat_id, user_id)
    except Exception:
        log.exception("Yetkisiz VIP kullanıcısı çıkarılamadı: chat=%s user=%s", chat_id, user_id)


async def test_golden_group_promo(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not update.effective_user or not admin(update.effective_user.id):
        await update.effective_message.reply_text("Bu test yalnızca ana yöneticiye açık.")
        return
    if update.effective_chat.type != "private":
        await update.effective_message.reply_text("Test komutunu botla özel sohbetten kullan.")
        return

    with connect() as c:
        groups = c.execute("""SELECT channel_id,tenant_id,channel_name FROM tenant_channels
            WHERE channel_type='chat' AND status='active'
            ORDER BY tenant_id,channel_id""").fetchall()
    if not groups:
        await update.effective_message.reply_text("Test edilecek aktif sohbet kanalı bulunamadı.")
        return

    bot_username = (await context.bot.get_me()).username
    if not bot_username:
        await update.effective_message.reply_text("Bot kullanıcı adı alınamadı; test gönderilemedi.")
        return

    sent, failed, skipped = 0, 0, 0
    for group in groups:
        group_id = int(group["channel_id"])
        tenant_id = group["tenant_id"]
        try:
            with connect() as c:
                tenant = c.execute("SELECT tenant_name,status FROM tenants WHERE tenant_id=?", (tenant_id,)).fetchone()
                settings = {r["setting_key"]: r["setting_value"] for r in c.execute(
                    "SELECT setting_key,setting_value FROM tenant_settings WHERE tenant_id=? AND setting_key LIKE 'promo_%'",
                    (tenant_id,)).fetchall()}
            if not tenant or tenant["status"] != "active":
                skipped += 1
                continue
            brand = (tenant["tenant_name"] or "VIP").strip()[:60]
            promo_text = settings.get("promo_text") or (
                f"👑 {brand} | VIP ÜYELİK\n\n"
                "⚽ Özel içerikler ve düzenli paylaşımlar\n"
                "💎 VIP üyelik başvurusu için aşağıdaki butona dokun.\n\n"
                "🔞 18 yaş ve üzeri içindir. Bahis risk içerir; kazanç garantisi yoktur. Sorumlu davranın."
            )
            button_text = settings.get("promo_button") or "💎 VIP başvurusu"
            kb = InlineKeyboardMarkup([[InlineKeyboardButton(
                button_text[:60], url=f"https://t.me/{bot_username}?start=vipc_{group_id}"
            )]])
            photo_id = settings.get("promo_photo_file_id")
            if photo_id:
                await context.bot.send_photo(chat_id=group_id, photo=photo_id,
                                              caption=promo_text[:1024], parse_mode="HTML", reply_markup=kb)
            else:
                await context.bot.send_message(chat_id=group_id, text=promo_text[:4000], parse_mode="HTML", reply_markup=kb)
            sent += 1
        except Exception as exc:
            failed += 1
            log.exception("VIP tanıtım test gönderimi başarısız: tenant=%s group=%s", tenant_id, group_id)
            log.warning("Test gönderim hata türü: %s", type(exc).__name__)

    await update.effective_message.reply_text(
        f"🧪 Test gönderimi tamamlandı.\nGönderilen: {sent} | Başarısız: {failed} | Atlanan: {skipped}"
    )


async def send_recurring_announcements(context: ContextTypes.DEFAULT_TYPE):
    current = now()
    with connect() as conn:
        rows = conn.execute("SELECT announcement_id,tenant_id,send_time,message,last_sent_date FROM recurring_announcements WHERE enabled=1").fetchall()
    for row in rows:
        if row["send_time"] != current.strftime("%H:%M") or row["last_sent_date"] == current.date().isoformat(): continue
        with connect() as conn:
            targets = [int(r[0]) for r in conn.execute("""SELECT channel_id FROM tenant_channels
                WHERE tenant_id=? AND status='active' AND channel_type IN ('vip','genel','chat')
                ORDER BY channel_id""", (row["tenant_id"],)).fetchall()]
        if not targets and row["tenant_id"] == "legacy_single" and channel_id():
            targets = [int(channel_id())]
        sent = 0
        for target in sorted(set(targets)):
            try:
                await context.bot.send_message(chat_id=target, text=row["message"]); sent += 1
            except Exception: log.exception("Otomatik duyuru gönderilemedi: %s", target)
        if sent:
            with connect() as conn:
                conn.execute("UPDATE recurring_announcements SET last_sent_date=? WHERE announcement_id=?",
                             (current.date().isoformat(), row["announcement_id"]))
            log_admin_action(0, "Otomatik duyuru gönderimi", None, f"tenant={row['tenant_id']}; #{row['announcement_id']}; {sent} kanal")

async def send_golden_group_promo(context: ContextTypes.DEFAULT_TYPE):
    """Send each tenant's own configurable promo at its chosen interval."""
    with connect() as c:
        groups = c.execute("""SELECT channel_id,tenant_id,channel_name FROM tenant_channels
            WHERE channel_type='chat' AND status='active'
            ORDER BY tenant_id,channel_id""").fetchall()
    if not groups:
        return
    bot_username = (await context.bot.get_me()).username
    if not bot_username:
        log.warning("Bot kullanıcı adı alınamadı; grup tanıtımı atlandı.")
        return
    current = now()
    for group in groups:
        group_id = int(group["channel_id"])
        tenant_id = group["tenant_id"]
        try:
            with connect() as c:
                tenant = c.execute("SELECT tenant_name,status FROM tenants WHERE tenant_id=?",
                                   (tenant_id,)).fetchone()
                settings = {r["setting_key"]: r["setting_value"] for r in c.execute(
                    "SELECT setting_key,setting_value FROM tenant_settings WHERE tenant_id=? AND setting_key LIKE 'promo_%'",
                    (tenant_id,)).fetchall()}
            if not tenant or tenant["status"] != "active" or settings.get("promo_enabled", "1") != "1":
                continue
            try:
                interval = int(settings.get("promo_interval_hours", "3"))
            except ValueError:
                interval = 3
            interval = interval if interval in (1, 3, 6, 12, 24) else 3
            last_sent = settings.get("promo_last_sent_at")
            if last_sent:
                try:
                    elapsed = (current - datetime.fromisoformat(last_sent)).total_seconds()
                    if elapsed < interval * 3600:
                        continue
                except ValueError:
                    pass
            brand = (tenant["tenant_name"] or "VIP").strip()[:60]
            promo_text = settings.get("promo_text") or (
                f"👑 {brand} | VIP ÜYELİK\n\n"
                "⚽ Özel içerikler ve düzenli paylaşımlar\n"
                "💎 VIP üyelik başvurusu için aşağıdaki butona dokun.\n\n"
                "🔞 18 yaş ve üzeri içindir. Bahis risk içerir; kazanç garantisi yoktur. Sorumlu davranın."
            )
            button_text = settings.get("promo_button") or "💎 VIP başvurusu"
            kb = InlineKeyboardMarkup([[InlineKeyboardButton(
                button_text[:60], url=f"https://t.me/{bot_username}?start=vipc_{group_id}"
            )]])
            photo_id = settings.get("promo_photo_file_id")
            if photo_id:
                await context.bot.send_photo(chat_id=group_id, photo=photo_id,
                                              caption=promo_text[:1024], parse_mode="HTML", reply_markup=kb)
            else:
                await context.bot.send_message(chat_id=group_id, text=promo_text[:4000], parse_mode="HTML", reply_markup=kb)
            with connect() as c:
                c.execute("""INSERT INTO tenant_settings(tenant_id,setting_key,setting_value,updated_at)
                    VALUES(?,?,?,?) ON CONFLICT(tenant_id,setting_key) DO UPDATE SET
                    setting_value=excluded.setting_value,updated_at=excluded.updated_at""",
                    (tenant_id, "promo_last_sent_at", current.isoformat(), current.isoformat()))
            log.info("Müşteri VIP tanıtımı gönderildi: tenant=%s group=%s", tenant_id, group_id)
        except Exception:
            log.exception("Müşteri VIP tanıtımı gönderilemedi: tenant=%s group=%s", tenant_id, group_id)

async def send_membership_reminders(context: ContextTypes.DEFAULT_TYPE):
    """Send each 3-day and 1-day expiry reminder once; preserve all membership rows."""
    current = now()
    reminders = ((3, timedelta(days=3)), (1, timedelta(days=1)))
    with connect() as c:
        legacy_rows = c.execute("""SELECT user_id,expires_at FROM memberships
            WHERE status='active' AND expires_at>? AND expires_at<=?""",
            (current.isoformat(), (current + timedelta(days=3, hours=6)).isoformat())).fetchall()
        tenant_rows = c.execute("""SELECT tenant_id,channel_id,user_id,expires_at FROM tenant_memberships
            WHERE status='active' AND expires_at>? AND expires_at<=?""",
            (current.isoformat(), (current + timedelta(days=3, hours=6)).isoformat())).fetchall()
    for kind, rows in (("legacy", legacy_rows), ("tenant", tenant_rows)):
        for row in rows:
            expiry = datetime.fromisoformat(row["expires_at"])
            days_left = max(1, (expiry.date() - current.date()).days)
            reminder_day = next((d for d, _ in reminders if days_left == d), None)
            if reminder_day is None:
                continue
            tenant_id = row["tenant_id"] if kind == "tenant" else ""
            chat_id = int(row["channel_id"]) if kind == "tenant" else int(channel_id())
            uid = int(row["user_id"])
            with connect() as c:
                already = c.execute("""SELECT 1 FROM membership_reminders
                    WHERE membership_type=? AND tenant_id=? AND channel_id=? AND user_id=? AND reminder_days=?""",
                    (kind, tenant_id, chat_id, uid, reminder_day)).fetchone()
            if already:
                continue
            label = "3 gün" if reminder_day == 3 else "1 gün"
            try:
                await context.bot.send_message(
                    uid,
                    f"⏳ VIP üyeliğinin bitmesine {label} kaldı.\n"
                    f"Bitiş tarihi: {expiry:%d.%m.%Y %H:%M}\n"
                    "Üyeliğini yenilemek için yöneticinle iletişime geç."
                )
            except Exception:
                log.info("VIP bitiş hatırlatması gönderilemedi: user=%s", uid)
                continue
            with connect() as c:
                c.execute("""INSERT OR IGNORE INTO membership_reminders
                    (membership_type,tenant_id,channel_id,user_id,reminder_days,sent_at)
                    VALUES(?,?,?,?,?,?)""",
                    (kind, tenant_id, chat_id, uid, reminder_day, current.isoformat()))


async def create_database_backup(context, notify=True):
    """Create a consistent SQLite backup and deliver it privately to configured admins."""
    stamp = now().strftime("%Y%m%d_%H%M%S")
    backup_path = os.path.join("/tmp", f"golden_vip_backup_{stamp}.sqlite3")
    source = None
    target = None
    try:
        source = sqlite3.connect(DB, timeout=30)
        target = sqlite3.connect(backup_path)
        source.backup(target)
        target.close()
        target = None
        source.close()
        source = None
        sent = 0
        for admin_id in ADMIN_IDS:
            try:
                with open(backup_path, "rb") as backup_file:
                    await context.bot.send_document(
                        chat_id=admin_id,
                        document=backup_file,
                        filename=f"GOLDEN_VIP_YEDEK_{stamp}.sqlite3",
                        caption=f"🔐 GOLDEN VIP veritabanı yedeği\nTarih: {now():%d.%m.%Y %H:%M}\nBu dosyayı güvenli bir yerde sakla."
                    )
                sent += 1
            except Exception:
                log.exception("Veritabanı yedeği yöneticiye gönderilemedi: %s", admin_id)
        if notify and sent == 0:
            log.error("Veritabanı yedeği oluşturuldu ancak hiçbir yöneticiye gönderilemedi.")
        return sent
    except Exception:
        log.exception("Veritabanı yedeği oluşturulamadı.")
        if notify:
            for admin_id in ADMIN_IDS:
                try:
                    await context.bot.send_message(admin_id, "⚠️ GOLDEN VIP veritabanı yedeği oluşturulamadı. Railway loglarını kontrol et.")
                except Exception:
                    pass
        return 0
    finally:
        if target is not None:
            target.close()
        if source is not None:
            source.close()
        try:
            if os.path.exists(backup_path):
                os.remove(backup_path)
        except OSError:
            log.exception("Geçici yedek dosyası silinemedi.")


async def manual_backup(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not admin_only(update):
        return
    await update.effective_message.reply_text("🔐 Veritabanı yedeği hazırlanıyor...")
    sent = await create_database_backup(context, notify=False)
    await update.effective_message.reply_text(
        f"✅ Yedek {sent} yöneticiye özel mesaj olarak gönderildi."
        if sent else
        "⚠️ Yedek oluşturulamadı veya yöneticiye gönderilemedi. Botun özel mesajlarını ve Railway loglarını kontrol et."
    )


async def application_error_handler(update: object, context: ContextTypes.DEFAULT_TYPE):
    global LAST_ERROR_NOTICE
    log.error("Telegram uygulama hatası", exc_info=context.error)
    current = now()
    if LAST_ERROR_NOTICE and (current - LAST_ERROR_NOTICE).total_seconds() < 600:
        return
    LAST_ERROR_NOTICE = current
    error_name = type(context.error).__name__ if context.error else "UnknownError"
    for admin_id in ADMIN_IDS:
        try:
            await context.bot.send_message(
                admin_id,
                f"⚠️ GOLDEN VIP botunda bir işlem hatası oluştu.\nTür: {error_name}\nAyrıntı için Railway loglarını kontrol et."
            )
        except Exception:
            log.exception("Hata bildirimi yöneticiye gönderilemedi: %s", admin_id)


async def post_init(app):
    with connect() as c:
        rows = c.execute(
            "SELECT job_id,run_at,message FROM posts WHERE status='pending'"
        ).fetchall()
    for jid, run_at, msg in rows:
        dt = datetime.fromisoformat(run_at)
        app.job_queue.run_once(
            send_scheduled, when=max(0, (dt-now()).total_seconds()),
            data={"job_id": jid, "message": msg}, name=jid
        )
    app.job_queue.run_repeating(expire, interval=300, first=20, name="vip-expiry")
    app.job_queue.run_repeating(expire_tenant_memberships, interval=300, first=30, name="tenant-vip-expiry")
    app.job_queue.run_repeating(send_membership_reminders, interval=21600, first=60, name="membership-reminders")
    app.job_queue.run_repeating(send_recurring_announcements, interval=60, first=10, name="recurring-announcements")
    app.job_queue.run_repeating(send_golden_group_promo, interval=60, first=10, name="golden-group-promo")
    app.job_queue.run_daily(create_database_backup, time=datetime.strptime("03:00", "%H:%M").time().replace(tzinfo=TZ), days=(0,), name="weekly-database-backup")



# ---- Çok kanallı müşteri işlemleri (aynı bot / aynı SQLite dosyası) ----
from telegram.ext import MessageHandler, filters


async def register_channel(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Register a Telegram group/channel. Main admins can register channels from private chat."""
    chat = update.effective_chat
    msg = update.effective_message
    user = update.effective_user
    if not chat or not msg:
        return

    if not user:
        return
    tenant_id = current_tenant_id(user.id, context)
    if not tenant_id or not manager_can_access(user.id, tenant_id):
        await msg.reply_text('Bu kanal kaydı için müşteri yetkin bulunmuyor. Ana yönetici önce /musteriekle ile müşteri oluşturmalı.')
        return

    if len(context.args) not in (1, 2) or context.args[0].lower() not in ("sohbet", "genel", "vip"):
        await msg.reply_text(
            "Kullanım: /kanalkaydet sohbet  |  /kanalkaydet genel @kanaladi  |  /kanalkaydet vip @kanaladi\n"
            "Yayın kanallarını botla özel sohbetten de kaydedebilirsin."
        )
        return

    role = context.args[0].lower()
    names = {"sohbet": "ALTAY VIP Sohbet Grubu", "genel": "ALTAY Genel Yayın Kanalı", "vip": "ALTAY VIP Yayın Kanalı"}
    target_chat = chat

    if role == "sohbet":
        if chat.type not in ("group", "supergroup") or len(context.args) != 1:
            await msg.reply_text("sohbet kaydı yalnızca sohbet grubunun içinde /kanalkaydet sohbet komutuyla yapılır.")
            return
    else:
        # Channel can be registered in-place by a channel admin, or from private chat
        # by the configured primary admin using the channel username / numeric ID.
        if chat.type == "channel":
            if len(context.args) != 1 or not msg.sender_chat or msg.sender_chat.id != chat.id:
                await msg.reply_text("Kanal içinden kayıt için komutu kanal yöneticisi olarak paylaşmalısın.")
                return
        elif chat.type == "private" and len(context.args) == 2:
            target = context.args[1]
            try:
                target_chat = await context.bot.get_chat(target)
            except Exception:
                log.exception("Kanal bulunamadı: %s", target)
                await msg.reply_text("Kanal bulunamadı. @kanaladi doğru mu ve bot kanala yönetici olarak eklendi mi kontrol et.")
                return
            if target_chat.type != "channel":
                await msg.reply_text("Verdiğin hedef bir yayın kanalı değil.")
                return
        else:
            await msg.reply_text("Yayın kanalını özel sohbetten kaydetmek için /kanalkaydet vip @kanaladi veya /kanalkaydet genel @kanaladi yaz.")
            return

    with connect() as c:
        existing = c.execute('SELECT tenant_id FROM tenant_channels WHERE channel_id=?', (target_chat.id,)).fetchone()
        if existing and existing['tenant_id'] != tenant_id:
            await msg.reply_text('⛔ Bu kanal başka bir müşteriye kayıtlı. Güvenlik için taşınmadı.'); return
        c.execute("""INSERT INTO tenant_channels(channel_id,tenant_id,channel_name,channel_type,owner_user_id,status)
            VALUES(?,?,?,?,?,'active') ON CONFLICT(channel_id) DO UPDATE SET
            channel_name=excluded.channel_name, channel_type=excluded.channel_type,
            owner_user_id=COALESCE(tenant_channels.owner_user_id,excluded.owner_user_id), status='active'""",
            (target_chat.id, tenant_id, 'GOLDEN CLUB SOHBET' if role == 'sohbet' else names[role], 'chat' if role == 'sohbet' else role, user.id))
        c.execute("""INSERT INTO tenant_admins(tenant_id,user_id,role,channel_id,status,added_at)
            VALUES(?,?,'owner',NULL,'active',?) ON CONFLICT(tenant_id,user_id,role,channel_id) DO UPDATE SET status='active'""",
            (tenant_id,user.id,now().isoformat()))
    await msg.reply_text(f"✅ {names[role]} kaydedildi.\nTelegram ID: {target_chat.id}\nBotun kanalda yönetici olduğundan emin ol.")


def make_tenant_id(name, owner_id):
    import re
    slug = re.sub(r'[^a-z0-9]+', '_', name.lower()).strip('_')[:24] or 'musteri'
    return f'{slug}_{int(owner_id)}'


async def create_tenant_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not admin_only(update): return
    raw = update.effective_message.text.partition(' ')[2].strip()
    parts = [x.strip() for x in raw.split('|', 1)]
    if len(parts) != 2 or not parts[0] or not parts[1].isdigit():
        await update.effective_message.reply_text('Kullanım: /musteriekle Müşteri Adı | TELEGRAM_KULLANICI_ID'); return
    name, owner_id = parts[0][:80], int(parts[1])
    if owner_id <= 0:
        await update.effective_message.reply_text('Geçerli Telegram kullanıcı ID gir.'); return
    tid = make_tenant_id(name, owner_id)
    with connect() as c:
        if c.execute('SELECT 1 FROM tenants WHERE tenant_id=?', (tid,)).fetchone():
            await update.effective_message.reply_text('Bu müşteri zaten kayıtlı: ' + tid); return
        c.execute('INSERT INTO tenants(tenant_id,tenant_name,status,created_at) VALUES(?,?,\'active\',?)', (tid,name,now().isoformat()))
        c.execute('INSERT INTO tenant_admins(tenant_id,user_id,role,channel_id,status,added_at) VALUES(?,?,\'owner\',NULL,\'active\',?)', (tid,owner_id,now().isoformat()))
    log_admin_action(update.effective_user.id,'Müşteri oluşturuldu',owner_id,f'{name}; {tid}')
    await update.effective_message.reply_text(f'✅ Müşteri oluşturuldu.\\nAd: {name}\\nMüşteri kodu: {tid}\\nSahip ID: {owner_id}\\nSahip /admin ile paneli açabilir.')


async def list_tenants_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not admin_only(update): return
    with connect() as c:
        rows = c.execute('SELECT t.tenant_id,t.tenant_name,COUNT(ch.channel_id) AS channels FROM tenants t LEFT JOIN tenant_channels ch ON ch.tenant_id=t.tenant_id AND ch.status=\'active\' WHERE t.status=\'active\' GROUP BY t.tenant_id ORDER BY t.tenant_name').fetchall()
    lines = ['AKTİF MÜŞTERİLER'] + [f"• {r['tenant_name']} — {r['tenant_id']} — {r['channels']} kanal" for r in rows]
    await update.effective_message.reply_text('\\n'.join(lines)[:3900] if rows else 'Aktif müşteri yok.')


async def select_tenant_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    uid = update.effective_user.id
    if len(context.args) != 1:
        await update.effective_message.reply_text('Kullanım: /musterisec MUSTERI_KODU'); return
    tid = context.args[0].strip()
    if not admin(uid) and tid not in tenant_ids_for_user(uid):
        await update.effective_message.reply_text('Bu müşteriye erişim yetkin yok.'); return
    with connect() as c:
        row = c.execute('SELECT tenant_name FROM tenants WHERE tenant_id=? AND status=\'active\'', (tid,)).fetchone()
    if not row:
        await update.effective_message.reply_text('Aktif müşteri bulunamadı.'); return
    context.user_data['selected_tenant_id'] = tid
    await update.effective_message.reply_text(f"✅ Seçili müşteri: {row['tenant_name']} ({tid})")


def tenant_for_channel(cid):
    with connect() as c:
        return c.execute("""SELECT tenant_id,channel_name,channel_type
            FROM tenant_channels WHERE channel_id=? AND status='active'""", (cid,)).fetchone()


def tenant_manager_ids(tenant_id, channel_id_value=None):
    with connect() as c:
        if channel_id_value is None:
            rows = c.execute("""SELECT DISTINCT user_id FROM tenant_admins
                WHERE tenant_id=? AND status='active'""", (tenant_id,)).fetchall()
        else:
            rows = c.execute("""SELECT DISTINCT user_id FROM tenant_admins
                WHERE tenant_id=? AND status='active' AND (channel_id=? OR channel_id IS NULL)""",
                (tenant_id, channel_id_value)).fetchall()
    return {int(r[0]) for r in rows} | set(ADMIN_IDS)


async def tenant_application_callbacks(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Approve or reject a 30-day application for the tenant VIP channel."""
    q = update.callback_query
    try:
        _, action, app_id_text = q.data.split(":", 2)
        app_id = int(app_id_text)
    except (ValueError, AttributeError):
        await q.answer("Başvuru bilgisi geçersiz.", show_alert=True)
        return

    with connect() as c:
        application = c.execute("""SELECT application_id,tenant_id,channel_id,user_id,username,status
            FROM tenant_applications WHERE application_id=?""", (app_id,)).fetchone()
    if not application:
        await q.answer("Başvuru bulunamadı.", show_alert=True)
        return
    tenant_id = application["tenant_id"]
    chat_id = int(application["channel_id"])
    user_id = int(application["user_id"])
    if not manager_can_access(q.from_user.id, tenant_id, chat_id) or not manager_has_permission(q.from_user.id, tenant_id, chat_id, "applications"):
        await q.answer("Bu başvuru için yönetici yetkin yok.", show_alert=True)
        return
    if application["status"] != "pending":
        await q.answer("Bu başvuru daha önce işleme alınmış.", show_alert=True)
        return

    if action == "approve":
        with connect() as c:
            vip = c.execute("""SELECT channel_id FROM tenant_channels
                WHERE tenant_id=? AND channel_type='vip' AND status='active'
                ORDER BY channel_id LIMIT 1""", (tenant_id,)).fetchone()
        if not vip:
            await q.answer("Önce VIP yayın kanalını /kanalkaydet vip ile kaydet.", show_alert=True)
            return
        vip_channel_id = int(vip[0])
        expires = now() + timedelta(days=30)
        try:
            invite = await context.bot.create_chat_invite_link(
                chat_id=vip_channel_id,
                name=f"GOLDEN-VIP-{user_id}"
            )
        except Exception:
            log.exception("VIP davet bağlantısı oluşturulamadı")
            await q.answer("Davet linki oluşturulamadı. Botun VIP kanalda davet yetkisini kontrol et.", show_alert=True)
            return
        with connect() as c:
            c.execute("UPDATE tenant_applications SET status='approved' WHERE application_id=?", (app_id,))
            c.execute("""INSERT INTO tenant_memberships
                (tenant_id,channel_id,user_id,expires_at,status,username) VALUES(?,?,?,?, 'active',?)
                ON CONFLICT(tenant_id,channel_id,user_id) DO UPDATE SET
                expires_at=excluded.expires_at,status='active',
                username=COALESCE(NULLIF(excluded.username,''),tenant_memberships.username)""",
                (tenant_id, vip_channel_id, user_id, expires.isoformat(), application["username"] or ""))
        try:
            sent = await context.bot.send_message(
                user_id,
                f"🎉 VIP başvurun onaylandı!\nÜyelik süren: 30 gün\nBitiş: {expires:%d.%m.%Y %H:%M}\n\n"
                f"VIP kanalına doğrudan katılım bağlantın:\n{invite.invite_link}"
            )
            with connect() as c:
                c.execute("""INSERT OR REPLACE INTO vip_invite_messages
                    (user_id,chat_id,message_id,invite_link) VALUES(?,?,?,?)""",
                    (user_id, vip_channel_id, sent.message_id, invite.invite_link))
        except Exception:
            log.exception("Onaylanan kullanıcıya VIP daveti gönderilemedi: %s", user_id)
            await q.answer("Onaylandı ancak kullanıcı botu başlatmadığı için özel mesaj gönderilemedi.", show_alert=True)
            await q.edit_message_reply_markup(reply_markup=None)
            return
        log_admin_action(q.from_user.id, "VIP başvurusu onaylandı", user_id, f"30 gün; {tenant_id}; kanal={vip_channel_id}")
        await q.answer("VIP üyeliği 30 gün onaylandı.")
        await q.edit_message_reply_markup(reply_markup=None)
        await q.message.reply_text(f"✅ {user_id} kullanıcısına 30 günlük VIP üyelik verildi.")
    elif action == "reject":
        with connect() as c:
            c.execute("UPDATE tenant_applications SET status='rejected' WHERE application_id=?", (app_id,))
        try:
            await context.bot.send_message(user_id, "VIP başvurun onaylanmadı. Bilgi için yöneticiyle görüşebilirsin.")
        except Exception:
            log.info("Reddedilen kullanıcıya bildirim gönderilemedi: %s", user_id)
        log_admin_action(q.from_user.id, "VIP başvurusu reddedildi", user_id, f"{tenant_id}; kanal={chat_id}")
        await q.answer("Başvuru reddedildi.")
        await q.edit_message_reply_markup(reply_markup=None)
        await q.message.reply_text(f"❌ {user_id} kullanıcısının başvurusu reddedildi.")
    else:
        await q.answer("İşlem tanınmadı.", show_alert=True)


def manager_has_permission(uid, tenant_id, cid, permission):
    if admin(uid): return True
    with connect() as conn:
        if cid is None:
            rows = conn.execute("""SELECT role FROM tenant_admins
                WHERE tenant_id=? AND user_id=? AND status='active'""",
                (tenant_id, uid)).fetchall()
        else:
            rows = conn.execute("""SELECT role FROM tenant_admins
                WHERE tenant_id=? AND user_id=? AND status='active'
                  AND (channel_id=? OR channel_id IS NULL)""",
                (tenant_id, uid, cid)).fetchall()
    role_permissions = {
        "owner": {"members","applications","payments","broadcast","settings"},
        "manager": {"members","applications","payments","broadcast","settings"},
        "moderator": {"members","applications"},
        "finance": {"payments"},
        "publisher": {"broadcast"},
    }
    return any(permission in role_permissions.get(row["role"], set()) for row in rows)


def manager_can_access(uid, tenant_id, cid=None):
    if admin(uid):
        return True
    with connect() as c:
        if cid is None:
            row = c.execute("""SELECT 1 FROM tenant_admins WHERE tenant_id=? AND user_id=?
                AND status='active' LIMIT 1""", (tenant_id, uid)).fetchone()
        else:
            row = c.execute("""SELECT 1 FROM tenant_admins WHERE tenant_id=? AND user_id=?
                AND status='active' AND (channel_id=? OR channel_id IS NULL) LIMIT 1""",
                (tenant_id, uid, cid)).fetchone()
    return bool(row)


async def vip_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Create a VIP application from /vip in a registered chat group."""
    msg = update.effective_message
    chat = update.effective_chat
    user = update.effective_user
    if not msg or not chat or not user or chat.type not in ("group", "supergroup"):
        return

    channel = tenant_for_channel(chat.id)
    if not channel:
        return
    tenant_id, channel_name, channel_type = channel
    if channel_type != "chat":
        return

    with connect() as c:
        existing = c.execute(
            "SELECT application_id,status FROM tenant_applications "
            "WHERE tenant_id=? AND channel_id=? AND user_id=? "
            "ORDER BY application_id DESC LIMIT 1",
            (tenant_id, chat.id, user.id)
        ).fetchone()
        if existing and existing["status"] == "pending":
            return
        cur = c.execute(
            "INSERT INTO tenant_applications "
            "(tenant_id,channel_id,user_id,username,full_name,status,created_at) "
            "VALUES(?,?,?,?,?,'pending',?)",
            (tenant_id, chat.id, user.id, user.username or "", user.full_name, now().isoformat())
        )
        app_id = cur.lastrowid

    kb = InlineKeyboardMarkup([[
        InlineKeyboardButton("✅ 30 gün onayla", callback_data=f"tenantapp:approve:{app_id}"),
        InlineKeyboardButton("❌ Reddet", callback_data=f"tenantapp:reject:{app_id}")
    ]])
    admin_text = (
        "🔔 YENİ VIP TALEBİ\n"
        f"Grup: {chat.title or chat.id}\n"
        f"Ad: {user.full_name}\n"
        f"Kullanıcı: @{user.username or 'yok'}\n"
        f"Telegram ID: {user.id}"
    )
    sent = False
    for aid in tenant_manager_ids(tenant_id, chat.id):
        try:
            await context.bot.send_message(aid, admin_text, reply_markup=kb)
            sent = True
        except Exception:
            log.exception("VIP talebi yöneticiye iletilemedi: %s", aid)
    if not sent:
        log.error("VIP talebi için yöneticiye özel bildirim gönderilemedi; tenant=%s chat=%s", tenant_id, chat.id)


async def payment_input_router(update: Update, context: ContextTypes.DEFAULT_TYPE):
    pending = context.user_data.get("pending_payment")
    if not pending:
        return
    actor_id = update.effective_user.id
    if pending.get("created_by") != actor_id or not manager_has_permission(actor_id, pending["tenant_id"], pending["channel_id"], "payments"):
        context.user_data.pop("pending_payment", None)
        await update.effective_message.reply_text("Bu ödeme kaydını oluşturma yetkin yok; işlem iptal edildi.")
        return
    raw = (update.effective_message.text or "").strip()
    bits = raw.split(maxsplit=1)
    try:
        amount = float(bits[0].replace(",", "."))
        if amount <= 0 or amount > 100000000:
            raise ValueError()
    except (ValueError, IndexError):
        await update.effective_message.reply_text("Geçerli tutar gir. Örnek: 1500 veya 1500 Aylık VIP")
        return
    note = bits[1][:200] if len(bits) > 1 else ""
    with connect() as conn:
        conn.execute("""INSERT INTO membership_payments
            (membership_type,tenant_id,channel_id,user_id,amount,note,paid_at,recorded_by)
            VALUES(?,?,?,?,?,?,?,?)""",
            (pending["kind"],pending["tenant_id"],pending["channel_id"],pending["user_id"],
             amount,note,now().isoformat(),update.effective_user.id))
    log_admin_action(update.effective_user.id, "Ödeme kaydı", pending["user_id"], f"{amount:.2f} TL; {note}")
    context.user_data.pop("pending_payment",None)
    await update.effective_message.reply_text(
        f"✅ Ödeme kaydedildi.\nÜye ID: {pending['user_id']}\nTutar: {amount:,.2f} TL"
        + (f"\nAçıklama: {note}" if note else "")
    )


async def group_text_router(update: Update, context: ContextTypes.DEFAULT_TYPE):
    msg = update.effective_message
    chat = update.effective_chat
    if not msg or not chat or chat.type not in ("group", "supergroup", "channel") or not msg.text:
        return
    channel = tenant_for_channel(chat.id)
    if not channel:
        return
    tenant_id, channel_name, channel_type = channel
    body = msg.text.strip()

    # SITE/SİTE works in the registered general channel and the registered chat group.
    if body.upper() in ("SITE", "SİTE"):
        with connect() as c:
            rows = c.execute("SELECT display_name,url FROM link_items WHERE tenant_id=? AND enabled=1 ORDER BY link_id", (tenant_id,)).fetchall()
            photo = c.execute("SELECT setting_value FROM tenant_settings WHERE tenant_id=? AND setting_key='site_photo_file_id'", (tenant_id,)).fetchone()
        if not rows:
            await msg.reply_text("SITE bağlantıları henüz tanımlanmamış.")
            return
        buttons = []
        row = []
        for item in rows:
            row.append(InlineKeyboardButton(item[0], url=item[1]))
            if len(row) == 2:
                buttons.append(row)
                row = []
        if row:
            buttons.append(row)
        kb = InlineKeyboardMarkup(buttons)
        caption = "✨ ALTAY VIP | Resmî bağlantılar ve sosyal medya hesaplarımız"
        if photo:
            try:
                await context.bot.send_photo(chat_id=chat.id, photo=photo[0], caption=caption, reply_markup=kb, reply_to_message_id=msg.message_id)
                return
            except Exception:
                log.exception("SITE görseli gönderilemedi; metinli butonlara geçiliyor")
        await msg.reply_text(caption, reply_markup=kb)
        return



# ---- Eksik SITE / destek / çoklu yayın yardımcıları ----
async def save_site_photo(update: Update, context: ContextTypes.DEFAULT_TYPE):
    msg = update.effective_message
    user = update.effective_user
    chat = update.effective_chat
    if not msg or not user or not chat or chat.type != 'private':
        return
    if msg.photo and context.user_data.get("awaiting_promo_photo"):
        await save_promo_photo(update, context)
        return
    tenant_id = current_tenant_id(user.id, context)
    if not tenant_id or not manager_has_permission(user.id, tenant_id, None, 'settings'):
        await msg.reply_text('Bu müşteri için görsel ayarı yetkin yok.'); return
    if not msg.photo: return
    file_id = msg.photo[-1].file_id
    with connect() as c:
        c.execute("""INSERT INTO tenant_settings(tenant_id,setting_key,setting_value,updated_at)
            VALUES(?,?,?,?) ON CONFLICT(tenant_id,setting_key) DO UPDATE SET
            setting_value=excluded.setting_value, updated_at=excluded.updated_at""",
            (tenant_id, 'site_photo_file_id', file_id, now().isoformat()))
    await msg.reply_text('✅ SITE tanıtım görseli kaydedildi. Bundan sonra SITE veya SİTE yazıldığında görsel ve bağlantı butonları birlikte paylaşılacak.')


async def promo_settings(update: Update, context: ContextTypes.DEFAULT_TYPE):
    uid = update.effective_user.id
    tid = current_tenant_id(uid, context)
    if not tid or not manager_has_permission(uid, tid, None, "settings"):
        await update.effective_message.reply_text("Tanıtım ayarları için yetkin bulunmuyor."); return
    with connect() as c:
        settings = {r["setting_key"]: r["setting_value"] for r in c.execute(
            "SELECT setting_key,setting_value FROM tenant_settings WHERE tenant_id=? AND setting_key LIKE 'promo_%'",
            (tid,)).fetchall()}
    enabled = settings.get("promo_enabled", "1") == "1"
    await update.effective_message.reply_text(
        "📣 OTOMATİK VIP TANITIM AYARLARI\n"
        f"Durum: {'Açık' if enabled else 'Kapalı'}\n"
        f"Gönderim aralığı: {settings.get('promo_interval_hours','3')} saat\n"
        f"Buton: {settings.get('promo_button','💎 VIP başvurusu')}\n"
        f"Özel metin: {'Var' if settings.get('promo_text') else 'Varsayılan'}\n"
        f"Görsel: {'Var' if settings.get('promo_photo_file_id') else 'Yok'}\n\n"
        "Komutlar:\n/tanitimmetin METİN\n/tanitimbuton BUTON YAZISI\n"
        "/tanitimsure 1|3|6|12|24\n/tanitimfoto (sonra fotoğraf gönder)\n"
        "/tanitimac veya /tanitimkapat"
    )

async def set_promo_setting(update: Update, context: ContextTypes.DEFAULT_TYPE):
    uid = update.effective_user.id
    tid = current_tenant_id(uid, context)
    if not tid or not manager_has_permission(uid, tid, None, "settings"):
        await update.effective_message.reply_text("Tanıtım ayarları için yetkin bulunmuyor."); return
    command = update.effective_message.text.split()[0].split("@")[0].lower()
    raw = update.effective_message.text.partition(" ")[2].strip()
    # Telegram mesajındaki kalın/italik/altı çizili biçimleri HTML olarak sakla.
    formatted = (update.effective_message.text_html or update.effective_message.text)
    formatted = formatted.partition(" ")[2].strip()
    if command == "/tanitimmetin":
        if not raw or len(raw) > 3500:
            await update.effective_message.reply_text("Metin 1–3500 karakter olmalı. Kullanım: /tanitimmetin Tanıtım metni"); return
        key, value, reply = "promo_text", formatted, "✅ Tanıtım metni ve yazı biçimleri kaydedildi."
    elif command == "/tanitimbuton":
        if not raw or len(raw) > 60:
            await update.effective_message.reply_text("Buton yazısı 1–60 karakter olmalı."); return
        key, value, reply = "promo_button", raw, "✅ VIP başvuru butonu yazısı kaydedildi."
    elif command == "/tanitimsure":
        if raw not in ("1", "3", "6", "12", "24"):
            await update.effective_message.reply_text("Aralık 1, 3, 6, 12 veya 24 saat olabilir."); return
        key, value, reply = "promo_interval_hours", raw, f"✅ Tanıtım her {raw} saatte bir gönderilecek."
    elif command == "/tanitimac":
        key, value, reply = "promo_enabled", "1", "✅ Otomatik tanıtım açıldı."
    else:
        key, value, reply = "promo_enabled", "0", "⏸ Otomatik tanıtım kapatıldı."
    with connect() as c:
        c.execute("""INSERT INTO tenant_settings(tenant_id,setting_key,setting_value,updated_at)
            VALUES(?,?,?,?) ON CONFLICT(tenant_id,setting_key) DO UPDATE SET
            setting_value=excluded.setting_value,updated_at=excluded.updated_at""",
            (tid, key, value, now().isoformat()))
    await update.effective_message.reply_text(reply)

async def request_promo_photo(update: Update, context: ContextTypes.DEFAULT_TYPE):
    uid = update.effective_user.id
    tid = current_tenant_id(uid, context)
    if not tid or not manager_has_permission(uid, tid, None, "settings"):
        await update.effective_message.reply_text("Tanıtım görseli ayarlama yetkin yok."); return
    context.user_data["awaiting_promo_photo"] = True
    await update.effective_message.reply_text("Şimdi otomatik VIP tanıtımında kullanılacak fotoğrafı gönder.")

async def save_promo_photo(update: Update, context: ContextTypes.DEFAULT_TYPE):
    msg, user = update.effective_message, update.effective_user
    if not msg or not user or not msg.photo or not context.user_data.get("awaiting_promo_photo"):
        return
    tid = current_tenant_id(user.id, context)
    if not tid or not manager_has_permission(user.id, tid, None, "settings"):
        context.user_data.pop("awaiting_promo_photo", None)
        await msg.reply_text("Bu müşteri için tanıtım görseli yetkin yok."); return
    with connect() as c:
        c.execute("""INSERT INTO tenant_settings(tenant_id,setting_key,setting_value,updated_at)
            VALUES(?,?,?,?) ON CONFLICT(tenant_id,setting_key) DO UPDATE SET
            setting_value=excluded.setting_value,updated_at=excluded.updated_at""",
            (tid, "promo_photo_file_id", msg.photo[-1].file_id, now().isoformat()))
    context.user_data.pop("awaiting_promo_photo", None)
    await msg.reply_text("✅ Otomatik VIP tanıtım görseli kaydedildi.")


async def add_link(update: Update, context: ContextTypes.DEFAULT_TYPE):
    uid = update.effective_user.id
    if update.effective_chat.type != 'private':
        await update.message.reply_text('Bu ayar komutunu botla özel sohbetten kullan.')
        return
    raw = update.message.text.partition(' ')[2].strip()
    # /linkekle kategori | görünen ad | https://...
    parts = [x.strip() for x in raw.split('|', 2)]
    if len(parts) != 3 or not parts[0] or not parts[1] or not parts[2].startswith(('https://','http://')):
        await update.message.reply_text('Kullanım: /linkekle kategori | Ad | https://adres')
        return
    tenant_id = current_tenant_id(uid, context)
    if not tenant_id or not manager_has_permission(uid, tenant_id, None, 'settings'):
        await update.message.reply_text('Bu müşteri için SITE ayarı yetkin yok.'); return
    with connect() as c:
        c.execute('INSERT INTO link_items(tenant_id,category,display_name,url,enabled) VALUES(?,?,?,?,1)',
                  (tenant_id, parts[0], parts[1], parts[2]))
    await update.message.reply_text('Bağlantı kaydedildi. Sohbet kanalında SITE veya SİTE yazıldığında listelenecek.')

async def add_support(update: Update, context: ContextTypes.DEFAULT_TYPE):
    uid = update.effective_user.id
    if update.effective_chat.type != 'private':
        await update.message.reply_text('Bu ayar komutunu botla özel sohbetten kullan.')
        return
    raw = update.message.text.partition(' ')[2].strip()
    parts = [x.strip() for x in raw.split('|', 1)]
    if len(parts) != 2 or not parts[0] or not parts[1].startswith(('https://t.me/','http://t.me/')):
        await update.message.reply_text('Kullanım: /destekekle Ad Soyad | https://t.me/kullanici')
        return
    tenant_id = current_tenant_id(uid, context)
    if not tenant_id or not manager_has_permission(uid, tenant_id, None, 'settings'):
        await update.message.reply_text('Bu müşteri için destek ayarı yetkin yok.'); return
    with connect() as c:
        c.execute('INSERT INTO support_contacts(tenant_id,display_name,telegram_link,enabled) VALUES(?,?,?,1)',
                  (tenant_id, parts[0], parts[1]))
    await update.message.reply_text('Destek yetkilisi eklendi.')

async def tenant_broadcast(update: Update, context: ContextTypes.DEFAULT_TYPE):
    uid = update.effective_user.id
    tenant_id = current_tenant_id(uid, context)
    if not tenant_id or not manager_can_access(uid, tenant_id):
        await update.message.reply_text('Bu müşteri için yetkin yok.')
        return
    if not manager_has_permission(uid, tenant_id, None, "broadcast"):
        await update.message.reply_text('Bu işlem için yayın yetkin bulunmuyor.')
        return
    targets = []
    with connect() as c:
        if admin(uid):
            rows = c.execute(
                "SELECT channel_id FROM tenant_channels WHERE tenant_id=? AND status='active'", (tenant_id,)
            ).fetchall()
        else:
            # Channel managers may publish only to channels explicitly assigned to them.
            rows = c.execute("""SELECT channel_id FROM tenant_channels
                WHERE tenant_id=? AND status='active' AND EXISTS (
                    SELECT 1 FROM tenant_admins a WHERE a.tenant_id=tenant_channels.tenant_id
                    AND a.user_id=? AND a.status='active'
                    AND (a.channel_id IS NULL OR a.channel_id=tenant_channels.channel_id)
                ) ORDER BY channel_id""", (tenant_id, uid)).fetchall()
        targets = [int(r[0]) for r in rows]
    if not targets:
        await update.message.reply_text('Aktif kanal bulunamadı.')
        return
    source = update.message.reply_to_message
    text_arg = update.message.text.partition(' ')[2].strip()
    if not source and not text_arg:
        await update.message.reply_text('Metin: /topluyayin Duyuru metni\nGörsel: görseli yanıtlayıp /topluyayin yaz.')
        return
    ok, failed = 0, 0
    for cid in targets:
        try:
            if source:
                await context.bot.copy_message(chat_id=cid, from_chat_id=source.chat_id, message_id=source.message_id)
            else:
                await context.bot.send_message(chat_id=cid, text=text_arg)
            ok += 1
        except Exception:
            failed += 1
            log.exception('Toplu paylaşım başarısız: %s', cid)
    log_admin_action(uid, "Toplu duyuru", None, f"Başarılı: {ok}; başarısız: {failed}")
    await update.message.reply_text(f'Toplu paylaşım tamamlandı. Başarılı: {ok} | Başarısız: {failed}')

async def record_payment_as_manager(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not update.effective_user or not update.effective_message: return
    if len(context.args) < 3:
        await update.effective_message.reply_text("Kullanım: /odeme KANAL_ID UYE_ID TUTAR [AÇIKLAMA]")
        return
    try:
        cid=int(context.args[0]); member_id=int(context.args[1]); amount=float(context.args[2].replace(",", "."))
        if member_id<=0 or amount<=0 or amount>100000000: raise ValueError()
    except ValueError:
        await update.effective_message.reply_text("Kanal ID, üye ID veya tutar geçersiz."); return
    with connect() as conn:
        channel=conn.execute("SELECT tenant_id FROM tenant_channels WHERE channel_id=? AND status='active'",(cid,)).fetchone()
    if not channel:
        await update.effective_message.reply_text("Bu kanal kayıtlı veya aktif değil."); return
    tid=channel["tenant_id"]
    if not manager_has_permission(update.effective_user.id,tid,cid,"payments"):
        await update.effective_message.reply_text("Bu kanal için ödeme kaydetme yetkin yok."); return
    note=" ".join(context.args[3:])[:200]
    with connect() as conn:
        conn.execute("""INSERT INTO membership_payments
            (membership_type,tenant_id,channel_id,user_id,amount,note,paid_at,recorded_by)
            VALUES('tenant',?,?,?,?,?,?,?)""",
            (tid,cid,member_id,amount,note,now().isoformat(),update.effective_user.id))
    log_admin_action(update.effective_user.id,"Ödeme kaydı",member_id,f"{amount:.2f} TL; {note}; kanal={cid}")
    await update.effective_message.reply_text(f"✅ Ödeme kaydedildi. Üye: {member_id} | Tutar: {amount:,.2f} TL")

async def grant_manager_role(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not admin_only(update): return
    if len(context.args) not in (2,3):
        await update.effective_message.reply_text("Kullanım: /yetkiver ID owner|manager|moderator|finance|publisher [KANAL_ID]"); return
    try:
        target=int(context.args[0]); role=context.args[1].lower()
        cid=int(context.args[2]) if len(context.args)==3 else None
        if target<=0 or role not in ("owner","manager","moderator","finance","publisher"): raise ValueError()
    except ValueError:
        await update.effective_message.reply_text("ID, rol veya kanal bilgisi geçersiz."); return
    if target in ADMIN_IDS:
        await update.effective_message.reply_text("Ana yöneticiye ek rol gerekmez."); return
    with connect() as conn:
        tenant = conn.execute("SELECT tenant_id FROM tenant_channels WHERE channel_id=? AND status='active'",(cid,)).fetchone() if cid else conn.execute("SELECT tenant_id FROM tenants WHERE tenant_id='legacy_single' AND status='active'").fetchone()
        if not tenant:
            await update.effective_message.reply_text("Aktif müşteri bulunamadı. Kanal ID belirterek tekrar dene."); return
        tid=tenant["tenant_id"]
        conn.execute("""INSERT INTO tenant_admins(tenant_id,user_id,role,channel_id,status,added_at)
            VALUES(?,?,?,?, 'active',?) ON CONFLICT(tenant_id,user_id,role,channel_id) DO UPDATE SET status='active'""",
            (tid,target,role,cid,now().isoformat()))
    log_admin_action(update.effective_user.id,"Yönetici yetkisi verildi",target,f"{role}; {tid}; kanal={cid or 'genel'}")
    await update.effective_message.reply_text(f"✅ {target} kullanıcısına {role} yetkisi verildi.")

async def revoke_manager_role(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not admin_only(update): return
    if len(context.args) not in (1,2):
        await update.effective_message.reply_text("Kullanım: /yetkikaldir ID [KANAL_ID]"); return
    try: target=int(context.args[0]); cid=int(context.args[1]) if len(context.args)==2 else None
    except ValueError:
        await update.effective_message.reply_text("Geçerli ID gir."); return
    with connect() as conn:
        cur=conn.execute("UPDATE tenant_admins SET status='inactive' WHERE user_id=? AND status='active'"+(" AND channel_id=?" if cid is not None else ""),
                         (target,cid) if cid is not None else (target,))
    log_admin_action(update.effective_user.id,"Yönetici yetkisi kaldırıldı",target,f"Kanal={cid or 'tümü'}; kayıt={cur.rowcount}")
    await update.effective_message.reply_text(f"✅ {target} için {cur.rowcount} yetki kaydı kapatıldı.")

async def list_manager_roles(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not admin_only(update): return
    with connect() as conn:
        rows=conn.execute("""SELECT a.user_id,a.role,a.tenant_id,a.channel_id,t.tenant_name,ch.channel_name
            FROM tenant_admins a LEFT JOIN tenants t ON t.tenant_id=a.tenant_id
            LEFT JOIN tenant_channels ch ON ch.channel_id=a.channel_id WHERE a.status='active' ORDER BY a.user_id""").fetchall()
    lines=["🛡 AKTİF YÖNETİCİLER"]+[f"• {r['user_id']} — {r['role']} — {r['tenant_name'] or r['tenant_id']} / {r['channel_name'] or 'Genel'}" for r in rows]
    await update.effective_message.reply_text("\n".join(lines)[:4000] if rows else "Ek yönetici kaydı yok.")

async def recurring_announcement_add(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not admin_only(update): return
    raw=update.effective_message.text.partition(" ")[2].strip()
    if "|" not in raw:
        await update.effective_message.reply_text("Kullanım: /otomatikduyuru SS:DD | Her gün gönderilecek mesaj"); return
    clock,message=[x.strip() for x in raw.split("|",1)]
    try:
        datetime.strptime(clock,"%H:%M")
        if not message or len(message)>3500: raise ValueError()
    except ValueError:
        await update.effective_message.reply_text("Saat SS:DD olmalı; mesaj 1–3500 karakter olmalı."); return
    with connect() as conn:
        cur=conn.execute("INSERT INTO recurring_announcements(tenant_id,send_time,message,enabled) VALUES('legacy_single',?,?,1)",(clock,message))
    log_admin_action(update.effective_user.id,"Otomatik duyuru oluşturuldu",None,f"#{cur.lastrowid}; {clock}")
    await update.effective_message.reply_text(f"✅ Her gün {clock} için duyuru kaydedildi. ID: {cur.lastrowid}")

async def recurring_announcement_list(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not admin_only(update): return
    with connect() as conn:
        rows=conn.execute("SELECT announcement_id,send_time,message,enabled FROM recurring_announcements WHERE tenant_id='legacy_single' ORDER BY send_time").fetchall()
    lines=["📣 OTOMATİK DUYURULAR"]+[f"#{r['announcement_id']} — {r['send_time']} — {'Aktif' if r['enabled'] else 'Kapalı'}\n{r['message'][:150]}" for r in rows]
    await update.effective_message.reply_text("\n\n".join(lines)[:4000] if rows else "Otomatik duyuru yok.")

async def recurring_announcement_delete(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not admin_only(update): return
    if len(context.args)!=1 or not context.args[0].isdigit():
        await update.effective_message.reply_text("Kullanım: /otomatikduyurusil ID"); return
    aid=int(context.args[0])
    with connect() as conn: cur=conn.execute("DELETE FROM recurring_announcements WHERE announcement_id=? AND tenant_id='legacy_single'",(aid,))
    log_admin_action(update.effective_user.id,"Otomatik duyuru silindi",None,f"#{aid}")
    await update.effective_message.reply_text("Duyuru silindi." if cur.rowcount else "Duyuru bulunamadı.")

async def set_welcome_message(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not admin_only(update): return
    raw=update.effective_message.text.partition(" ")[2].strip()
    tid="legacy_single"
    if raw.lower()=="kapat":
        with connect() as conn: conn.execute("DELETE FROM tenant_settings WHERE tenant_id=? AND setting_key='welcome_message'",(tid,))
        log_admin_action(update.effective_user.id,"Karşılama mesajı kapatıldı")
        await update.effective_message.reply_text("Karşılama mesajı kapatıldı."); return
    if not raw or len(raw)>900:
        await update.effective_message.reply_text("Kullanım: /karsilama Hoş geldin {name}!\nKapatmak: /karsilama kapat"); return
    with connect() as conn:
        conn.execute("""INSERT INTO tenant_settings(tenant_id,setting_key,setting_value,updated_at) VALUES(?,?,?,?)
            ON CONFLICT(tenant_id,setting_key) DO UPDATE SET setting_value=excluded.setting_value,updated_at=excluded.updated_at""",
            (tid,"welcome_message",raw,now().isoformat()))
    log_admin_action(update.effective_user.id,"Karşılama mesajı güncellendi",None,raw[:120])
    await update.effective_message.reply_text("✅ Karşılama mesajı kaydedildi. {name} üyenin adıyla değişir.")

async def user_history_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not admin_only(update): return
    if len(context.args)!=1 or not context.args[0].isdigit():
        await update.effective_message.reply_text("Kullanım: /uyegecmis KULLANICI_ID"); return
    uid=int(context.args[0])
    with connect() as conn:
        logs=conn.execute("SELECT actor_id,action,details,created_at FROM admin_audit_log WHERE target_user_id=? ORDER BY audit_id DESC LIMIT 25",(uid,)).fetchall()
        pays=conn.execute("SELECT amount,note,paid_at FROM membership_payments WHERE user_id=? ORDER BY payment_id DESC LIMIT 15",(uid,)).fetchall()
    lines=[f"🧾 ÜYE GEÇMİŞİ — {uid}","","Yönetici işlemleri:"]
    for r in logs:
        stamp=datetime.fromisoformat(r["created_at"]); lines.append(f"• {stamp:%d.%m.%Y %H:%M} — {r['action']} — yönetici {r['actor_id']}")
        if r["details"]: lines.append("  "+r["details"][:120])
    if not logs: lines.append("İşlem kaydı yok.")
    lines.append("\nÖdemeler:")
    for r in pays:
        stamp=datetime.fromisoformat(r["paid_at"]); lines.append(f"• {stamp:%d.%m.%Y} — {r['amount']:,.2f} TL"+(f" ({r['note']})" if r["note"] else ""))
    if not pays: lines.append("Ödeme kaydı yok.")
    await update.effective_message.reply_text("\n".join(lines)[:4000])

async def list_support(update: Update, context: ContextTypes.DEFAULT_TYPE):
    # Destek butonu doğrudan botun yapılandırılmış ana yöneticisinin Telegram profiline gider.
    if not ADMIN_IDS:
        await update.callback_query.message.reply_text('Destek bağlantısı şu anda ayarlanmamış.')
        return
    support_admin_id = sorted(ADMIN_IDS)[0]
    support_url = f'tg://user?id={support_admin_id}'
    kb = InlineKeyboardMarkup([[InlineKeyboardButton('💬 Yöneticiye mesaj gönder', url=support_url)]])
    await update.callback_query.message.reply_text(
        '🆘 Destek için aşağıdaki butona basarak doğrudan yöneticiye ulaşabilirsin.',
        reply_markup=kb
    )


def main():
    if not TOKEN:
        raise RuntimeError("BOT_TOKEN ayarlanmamış.")
    if not ADMIN_IDS:
        raise RuntimeError("ADMIN_IDS ayarlanmamış.")
    init_db()
    app = Application.builder().token(TOKEN).post_init(post_init).build()
    app.add_error_handler(application_error_handler)
    app.add_handler(CommandHandler("start", start))
    app.add_handler(CommandHandler("vip", vip_command))
    app.add_handler(CommandHandler("kimim", whoami))
    app.add_handler(CommandHandler("kanalkaydet", register_channel))
    app.add_handler(CommandHandler('musteriekle', create_tenant_command))
    app.add_handler(CommandHandler('musteriler', list_tenants_command))
    app.add_handler(CommandHandler('musterisec', select_tenant_command))
    app.add_handler(CommandHandler("testtanitim", test_golden_group_promo))
    app.add_handler(ChatMemberHandler(enforce_vip_membership, ChatMemberHandler.CHAT_MEMBER))
    app.add_handler(ChatJoinRequestHandler(handle_vip_join_request))
    app.add_handler(CommandHandler("admin", panel))
    app.add_handler(CommandHandler("uyeler", members))
    app.add_handler(CommandHandler("onayla", grant))
    app.add_handler(CommandHandler("uzat", extend_membership))
    app.add_handler(CommandHandler("sil", remove_member))
    app.add_handler(CommandHandler("istatistik", stats))
    app.add_handler(CommandHandler("yedekle", manual_backup))
    app.add_handler(CommandHandler("duyuru", broadcast))
    app.add_handler(CommandHandler("planla", plan))
    app.add_handler(CommandHandler("planlar", plans))
    app.add_handler(CommandHandler("iptal", cancel))
    app.add_handler(CommandHandler("odeme", record_payment_as_manager))
    app.add_handler(CommandHandler("yetkiver", grant_manager_role))
    app.add_handler(CommandHandler("yetkikaldir", revoke_manager_role))
    app.add_handler(CommandHandler("yetkililer", list_manager_roles))
    app.add_handler(CommandHandler("otomatikduyuru", recurring_announcement_add))
    app.add_handler(CommandHandler("otomatikduyurular", recurring_announcement_list))
    app.add_handler(CommandHandler("otomatikduyurusil", recurring_announcement_delete))
    app.add_handler(CommandHandler("karsilama", set_welcome_message))
    app.add_handler(CommandHandler("uyegecmis", user_history_command))
    app.add_handler(CallbackQueryHandler(tenant_panel_callbacks, pattern=r"^tenantpanel:"))
    app.add_handler(CallbackQueryHandler(admin_callbacks, pattern=r"^(approve|reject):"))
    app.add_handler(CallbackQueryHandler(buttons, pattern=r"^(apply|status|support)$"))
    app.add_handler(CallbackQueryHandler(tenant_application_callbacks, pattern=r"^tenantapp:"))
    app.add_handler(CommandHandler("linkekle", add_link))
    app.add_handler(CommandHandler("destekekle", add_support))
    app.add_handler(CommandHandler("topluyayin", tenant_broadcast))
    app.add_handler(CommandHandler("tanitimayar", promo_settings))
    app.add_handler(CommandHandler("tanitimmetin", set_promo_setting))
    app.add_handler(CommandHandler("tanitimbuton", set_promo_setting))
    app.add_handler(CommandHandler("tanitimsure", set_promo_setting))
    app.add_handler(CommandHandler("tanitimac", set_promo_setting))
    app.add_handler(CommandHandler("tanitimkapat", set_promo_setting))
    app.add_handler(CommandHandler("tanitimfoto", request_promo_photo))
    app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND & filters.ChatType.PRIVATE, payment_input_router))
    # Tek fotoğraf yönlendiricisi: VIP tanıtım fotoğrafı bekleniyorsa onu, değilse SITE fotoğrafını kaydeder.
    app.add_handler(MessageHandler(filters.PHOTO & filters.ChatType.PRIVATE, save_site_photo))
    app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, group_text_router))
    log.info("GOLDEN CLUB BOT - çok kanallı panel aktif.")
    app.run_polling(allowed_updates=["message", "callback_query", "chat_member", "my_chat_member", "chat_join_request"])


if __name__ == "__main__":
    main()
