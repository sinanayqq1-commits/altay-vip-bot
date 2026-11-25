import os, logging, sqlite3
from datetime import datetime, timedelta
from zoneinfo import ZoneInfo

from telegram import Update, InlineKeyboardButton, InlineKeyboardMarkup
from telegram.ext import (
    Application, CommandHandler, CallbackQueryHandler, ContextTypes
)

TOKEN = os.getenv("BOT_TOKEN", "").strip()
ADMIN_IDS = {int(x) for x in os.getenv("ADMIN_IDS", "").split(",") if x.strip()}
CHANNEL_ID = os.getenv("CHANNEL_ID", "").strip()
TZ = ZoneInfo(os.getenv("TIMEZONE", "Europe/Istanbul"))
DB = os.getenv("DB_PATH", "altay_vip.sqlite3")

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")

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


def init_db():
    # Legacy tables are retained exactly; new tables are additive only.
    with connect() as c:
        c.execute("""CREATE TABLE IF NOT EXISTS applications(
            user_id INTEGER PRIMARY KEY, username TEXT, full_name TEXT,
            status TEXT NOT NULL, created_at TEXT NOT NULL)""")
        c.execute("""CREATE TABLE IF NOT EXISTS memberships(
            user_id INTEGER PRIMARY KEY, expires_at TEXT NOT NULL, status TEXT NOT NULL)""")
        c.execute("""CREATE TABLE IF NOT EXISTS posts(
            job_id TEXT PRIMARY KEY, run_at TEXT NOT NULL, message TEXT NOT NULL,
            status TEXT NOT NULL)""")

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
            status TEXT NOT NULL,
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
                c.execute("UPDATE tenants SET status='inactive' WHERE tenant_id IN ('altay_demo','musteri_001')")
            except ValueError:
                log.warning("CHANNEL_ID sayısal değil; eski kanal kaydı eklenemedi.")


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


def can_open_admin_panel(uid):
    # Ana yönetim paneli yalnızca ADMIN_IDS içindeki hesaplara açıktır.
    return admin(uid)


def admin_panel_keyboard():
    return InlineKeyboardMarkup([
        [InlineKeyboardButton("👥 VIP üyeler", callback_data="tenantpanel:members"),
         InlineKeyboardButton("⏳ Bekleyen başvurular", callback_data="tenantpanel:pending")],
        [InlineKeyboardButton("🏢 Müşteri ve kanallar", callback_data="tenantpanel:channels")],
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
                "🤖 VIP işlemlerini özelden aç",
                url=f"https://t.me/{bot_username}?start=vipc_{chat.id}"
            )]])
            await update.effective_message.reply_text(
                "VIP başvurusu ve üyelik işlemleri özel mesajdan yapılır. Aşağıdaki butona bas.",
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
        c.execute("""INSERT INTO memberships(user_id,expires_at,status) VALUES(?,?,'active')
        ON CONFLICT(user_id) DO UPDATE SET expires_at=excluded.expires_at,status='active'""",
        (uid, until.isoformat()))
        c.execute("UPDATE applications SET status='approved' WHERE user_id=?", (uid,))
    link = ""
    try:
        inv = await context.bot.create_chat_invite_link(
            chat_id=channel_id(), member_limit=1,
            expire_date=now()+timedelta(hours=24), name=f"VIP-{uid}"
        )
        link = "\n\nKanala katılım bağlantın (24 saat geçerli):\n" + inv.invite_link
    except Exception:
        log.exception("Davet bağlantısı oluşturulamadı")
        link = "\n\nDavet bağlantısı oluşturulamadı. Yöneticiyle iletişime geç."
    try:
        await context.bot.send_message(
            uid, f"🎉 VIP üyeliğin onaylandı!\nSüre: {days} gün\n"
                 f"Bitiş: {until:%d.%m.%Y %H:%M}{link}"
        )
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
    await update.message.reply_text(
        "🛠 ALTAY VIP BOT — YÖNETİCİ PANELİ\n\n"
        "VIP üyeleri ve bekleyen başvuruları buradan yönetebilirsin.\n"
        "Müşteri/kanal kayıtlarını, yönetici yetkilerini ve sistem durumunu görüntüleyebilirsin.\n"
        "Mevcut üyelik kayıtları korunur; işlemler yetki kontrolüyle yürütülür.",
        reply_markup=admin_panel_keyboard()
    )


async def tenant_panel_callbacks(update: Update, context: ContextTypes.DEFAULT_TYPE):
    q = update.callback_query
    uid = q.from_user.id
    if not can_open_admin_panel(uid):
        await q.answer("Bu panel için yetkin yok.", show_alert=True)
        return
    await q.answer()

    if q.data == "tenantpanel:channels":
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
                SELECT user_id,expires_at FROM memberships
                WHERE status='active' AND expires_at>? ORDER BY expires_at LIMIT 25
            """, (current,)).fetchall()
            tenant_rows = c.execute("""
                SELECT tm.user_id,tm.expires_at,t.tenant_name,tc.channel_name
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
        lines = ["👥 AKTİF VIP ÜYELER"]
        for r in legacy_rows:
            dt = datetime.fromisoformat(r["expires_at"])
            lines.append(f"• {r['user_id']} — Eski ana kanal — bitiş {dt:%d.%m.%Y %H:%M}")
        for r in tenant_rows:
            dt = datetime.fromisoformat(r["expires_at"])
            lines.append(f"• {r['user_id']} — {r['tenant_name'] or 'Müşteri'} / {r['channel_name'] or 'Kanal'} — {dt:%d.%m.%Y %H:%M}")
        if not legacy_rows and not tenant_rows:
            lines.append("Aktif VIP üye bulunmuyor.")
        lines += ["", f"📦 Süresi dolmuş kayıt: {expired_legacy + expired_tenant}"]
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
            "SELECT user_id,expires_at FROM memberships WHERE status='active' AND expires_at>? ORDER BY expires_at",
            (current,)
        ).fetchall()
        tenant_rows = c.execute("""
            SELECT tm.tenant_id, t.tenant_name, tm.channel_id, tc.channel_name,
                   tm.user_id, tm.expires_at
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
                f"• {r['user_id']} — {expiry:%d.%m.%Y %H:%M}\n"
                f"  {r['tenant_name'] or r['tenant_id']} / {r['channel_name'] or r['channel_id']}"
            )
    if legacy_rows:
        lines.append("\n📋 Eski sistem üyeleri")
        for r in legacy_rows[:100]:
            expiry = datetime.fromisoformat(r['expires_at'])
            lines.append(f"• {r['user_id']} — {expiry:%d.%m.%Y %H:%M}")
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

    with connect() as c:
        row = c.execute(
            "SELECT expires_at FROM memberships WHERE user_id=? AND status='active'", (uid,)
        ).fetchone()
        base = datetime.fromisoformat(row[0]) if row else now()
        if base < now():
            base = now()
        until = base + timedelta(days=days)
        c.execute("""INSERT INTO memberships(user_id,expires_at,status) VALUES(?,?,'active')
        ON CONFLICT(user_id) DO UPDATE SET expires_at=excluded.expires_at,status='active'""",
        (uid, until.isoformat()))
    try:
        await context.bot.send_message(
            uid, f"🎉 VIP üyeliğin {days} gün uzatıldı.\nYeni bitiş: {until:%d.%m.%Y %H:%M}"
        )
    except Exception:
        pass
    await update.message.reply_text(
        f"{uid} kullanıcısının üyeliği {days} gün uzatıldı.\nYeni bitiş: {until:%d.%m.%Y %H:%M}"
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
        cur = c.execute(
            "UPDATE memberships SET status='revoked' WHERE user_id=? AND status='active'", (uid,)
        )
    removed = False
    try:
        await context.bot.ban_chat_member(channel_id(), uid)
        await context.bot.unban_chat_member(channel_id(), uid, only_if_banned=True)
        removed = True
    except Exception:
        log.exception("Kullanıcı kanaldan çıkarılamadı: %s", uid)

    try:
        await context.bot.send_message(
            uid, "VIP üyeliğin yönetici tarafından sonlandırıldı. Bilgi için yöneticiyle görüş."
        )
    except Exception:
        pass

    if cur.rowcount:
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



# ---- Çok kanallı müşteri işlemleri (aynı bot / aynı SQLite dosyası) ----
from telegram.ext import MessageHandler, filters


async def register_channel(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Register a Telegram group/channel. Main admins can register channels from private chat."""
    chat = update.effective_chat
    msg = update.effective_message
    user = update.effective_user
    if not chat or not msg:
        return

    if not user or not admin(user.id):
        await msg.reply_text("Bu kurulum komutu yalnızca ana yöneticiye açık.")
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
        c.execute("INSERT OR IGNORE INTO tenants(tenant_id,tenant_name,status,created_at) VALUES('altay_demo','ALTAY VIP','active',?)", (now().isoformat(),))
        c.execute("""INSERT INTO tenant_channels(channel_id,tenant_id,channel_name,channel_type,owner_user_id,status)
            VALUES(?,?,?,?,?,'active') ON CONFLICT(channel_id) DO UPDATE SET
            tenant_id=excluded.tenant_id, channel_name=excluded.channel_name,
            channel_type=excluded.channel_type, owner_user_id=COALESCE(excluded.owner_user_id,tenant_channels.owner_user_id), status='active'""",
            (target_chat.id, "altay_demo", names[role], "chat" if role == "sohbet" else role, user.id if user else None))
    await msg.reply_text(f"✅ {names[role]} kaydedildi.\nTelegram ID: {target_chat.id}\nBotun kanalda yönetici olduğundan emin ol.")


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
        application = c.execute("""SELECT application_id,tenant_id,channel_id,user_id,status
            FROM tenant_applications WHERE application_id=?""", (app_id,)).fetchone()
    if not application:
        await q.answer("Başvuru bulunamadı.", show_alert=True)
        return
    tenant_id = application["tenant_id"]
    chat_id = int(application["channel_id"])
    user_id = int(application["user_id"])
    if not manager_can_access(q.from_user.id, tenant_id, chat_id):
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
                member_limit=1,
                expire_date=now() + timedelta(hours=24),
                name=f"ALTAY-VIP-{user_id}"
            )
        except Exception:
            log.exception("VIP davet bağlantısı oluşturulamadı")
            await q.answer("Davet linki oluşturulamadı. Botun VIP kanalda davet yetkisini kontrol et.", show_alert=True)
            return
        with connect() as c:
            c.execute("UPDATE tenant_applications SET status='approved' WHERE application_id=?", (app_id,))
            c.execute("""INSERT INTO tenant_memberships
                (tenant_id,channel_id,user_id,expires_at,status) VALUES(?,?,?,?, 'active')
                ON CONFLICT(tenant_id,channel_id,user_id) DO UPDATE SET
                expires_at=excluded.expires_at,status='active'""",
                (tenant_id, vip_channel_id, user_id, expires.isoformat()))
        try:
            await context.bot.send_message(
                user_id,
                f"🎉 VIP başvurun onaylandı!\nÜyelik süren: 30 gün\nBitiş: {expires:%d.%m.%Y %H:%M}\n\n"
                f"VIP kanalına katılım bağlantın (24 saat geçerli, tek kullanımlık):\n{invite.invite_link}"
            )
        except Exception:
            log.exception("Onaylanan kullanıcıya VIP daveti gönderilemedi: %s", user_id)
            await q.answer("Onaylandı ancak kullanıcı botu başlatmadığı için özel mesaj gönderilemedi.", show_alert=True)
            await q.edit_message_reply_markup(reply_markup=None)
            return
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
        await q.answer("Başvuru reddedildi.")
        await q.edit_message_reply_markup(reply_markup=None)
        await q.message.reply_text(f"❌ {user_id} kullanıcısının başvurusu reddedildi.")
    else:
        await q.answer("İşlem tanınmadı.", show_alert=True)


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
    if not admin(user.id):
        await msg.reply_text('Bu görseli yalnızca ana yönetici kaydedebilir.')
        return
    if not msg.photo:
        return
    file_id = msg.photo[-1].file_id
    with connect() as c:
        for tenant_key in ('altay_demo', 'altay_demo'):
            c.execute("""INSERT INTO tenant_settings(tenant_id,setting_key,setting_value,updated_at)
                VALUES(?,?,?,?) ON CONFLICT(tenant_id,setting_key) DO UPDATE SET
                setting_value=excluded.setting_value, updated_at=excluded.updated_at""",
                (tenant_key, 'site_photo_file_id', file_id, now().isoformat()))
    await msg.reply_text('✅ SITE tanıtım görseli kaydedildi. Bundan sonra SITE veya SİTE yazıldığında görsel ve bağlantı butonları birlikte paylaşılacak.')

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
    tenant_id = 'altay_demo' if not admin(uid) else 'altay_demo'
    if not manager_can_access(uid, tenant_id):
        await update.message.reply_text('Bu müşteri için yetkin yok.')
        return
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
    tenant_id = 'altay_demo'
    if not manager_can_access(uid, tenant_id):
        await update.message.reply_text('Bu müşteri için yetkin yok.')
        return
    with connect() as c:
        c.execute('INSERT INTO support_contacts(tenant_id,display_name,telegram_link,enabled) VALUES(?,?,?,1)',
                  (tenant_id, parts[0], parts[1]))
    await update.message.reply_text('Destek yetkilisi eklendi.')

async def tenant_broadcast(update: Update, context: ContextTypes.DEFAULT_TYPE):
    uid = update.effective_user.id
    tenant_id = 'altay_demo'
    if not manager_can_access(uid, tenant_id):
        await update.message.reply_text('Bu müşteri için yetkin yok.')
        return
    targets = []
    with connect() as c:
        if admin(uid):
            rows = c.execute(
                "SELECT channel_id FROM tenant_channels WHERE tenant_id=? AND status='active'", (tenant_id,)
            ).fetchall()
        else:
            # Channel managers may publish only to channels explicitly assigned to them.
            rows = c.execute("""SELECT DISTINCT channel_id FROM tenant_admins
                WHERE tenant_id=? AND user_id=? AND status='active'
                AND channel_id IS NOT NULL ORDER BY channel_id""", (tenant_id, uid)).fetchall()
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
    await update.message.reply_text(f'Toplu paylaşım tamamlandı. Başarılı: {ok} | Başarısız: {failed}')

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
    app.add_handler(CommandHandler("start", start))
    app.add_handler(CommandHandler("vip", vip_command))
    app.add_handler(CommandHandler("kimim", whoami))
    app.add_handler(CommandHandler("kanalkaydet", register_channel))
    app.add_handler(CommandHandler("admin", panel))
    app.add_handler(CommandHandler("uyeler", members))
    app.add_handler(CommandHandler("onayla", grant))
    app.add_handler(CommandHandler("uzat", extend_membership))
    app.add_handler(CommandHandler("sil", remove_member))
    app.add_handler(CommandHandler("istatistik", stats))
    app.add_handler(CommandHandler("duyuru", broadcast))
    app.add_handler(CommandHandler("planla", plan))
    app.add_handler(CommandHandler("planlar", plans))
    app.add_handler(CommandHandler("iptal", cancel))
    app.add_handler(CallbackQueryHandler(tenant_panel_callbacks, pattern=r"^tenantpanel:"))
    app.add_handler(CallbackQueryHandler(admin_callbacks, pattern=r"^(approve|reject):"))
    app.add_handler(CallbackQueryHandler(buttons, pattern=r"^(apply|status|support)$"))
    app.add_handler(CallbackQueryHandler(tenant_application_callbacks, pattern=r"^tenantapp:"))
    app.add_handler(CommandHandler("linkekle", add_link))
    app.add_handler(CommandHandler("destekekle", add_support))
    app.add_handler(CommandHandler("topluyayin", tenant_broadcast))
    app.add_handler(MessageHandler(filters.PHOTO & filters.ChatType.PRIVATE, save_site_photo))
    app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, group_text_router))
    log.info("GOLDEN CLUB BOT - çok kanallı panel aktif.")
    app.run_polling()


if __name__ == "__main__":
    main()
