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
log = logging.getLogger("altay-vip-bot")


def now():
    return datetime.now(TZ)


def connect():
    return sqlite3.connect(DB)


def init_db():
    with connect() as c:
        c.execute("""CREATE TABLE IF NOT EXISTS applications(
            user_id INTEGER PRIMARY KEY, username TEXT, full_name TEXT,
            status TEXT NOT NULL, created_at TEXT NOT NULL)""")
        c.execute("""CREATE TABLE IF NOT EXISTS memberships(
            user_id INTEGER PRIMARY KEY, expires_at TEXT NOT NULL, status TEXT NOT NULL)""")
        c.execute("""CREATE TABLE IF NOT EXISTS posts(
            job_id TEXT PRIMARY KEY, run_at TEXT NOT NULL, message TEXT NOT NULL,
            status TEXT NOT NULL)""")


def admin(uid):
    return uid in ADMIN_IDS


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


async def start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user = update.effective_user
    exp = active_expiry(user.id)
    if exp:
        msg = f"Hoş geldin {user.first_name}! VIP üyeliğin aktif. Bitiş: {exp:%d.%m.%Y %H:%M}"
    else:
        msg = f"ALTAY VIP BOT'e hoş geldin, {user.first_name}!\nVIP üyelik başvurusu için aşağıdaki butona bas."
    kb = InlineKeyboardMarkup([
        [InlineKeyboardButton("👑 VIP Başvurusu", callback_data="apply")],
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
        await q.message.reply_text("Destek için kanal yöneticisiyle iletişime geç.")


async def admin_callbacks(update: Update, context: ContextTypes.DEFAULT_TYPE):
    q = update.callback_query
    if not admin(q.from_user.id):
        await q.answer("Bu işlem yalnızca yöneticilere açık.", show_alert=True)
        return
    await q.answer()
    action, uid_s = q.data.split(":")
    uid = int(uid_s)
    if action == "approve":
        await approve(context, uid, 30)
        await q.edit_message_reply_markup(reply_markup=None)
        await q.message.reply_text(f"{uid} kullanıcısına 30 günlük VIP üyelik verildi.")
    else:
        with connect() as c:
            c.execute("UPDATE applications SET status='rejected' WHERE user_id=?", (uid,))
        try:
            await context.bot.send_message(
                uid, "VIP başvurun onaylanmadı. Bilgi için yöneticiyle görüşebilirsin."
            )
        except Exception:
            pass
        await q.edit_message_reply_markup(reply_markup=None)
        await q.message.reply_text("Başvuru reddedildi.")


async def panel(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not admin_only(update):
        return
    await update.message.reply_text(
        "ALTAY VIP BOT — YÖNETİCİ PANELİ\n\n"
        "ÜYELİK İŞLEMLERİ\n"
        "/uyeler — aktif VIP üyeler\n"
        "/onayla KULLANICI_ID GÜN — üyelik ver\n"
        "/uzat KULLANICI_ID GÜN — mevcut bitişe gün ekle\n"
        "/sil KULLANICI_ID — üyeliği iptal et ve kanaldan çıkar\n\n"
        "DUYURU İŞLEMLERİ\n"
        "/duyuru MESAJ — hemen duyuru gönder\n"
        "/planla YYYY-AA-GG SS:DD | MESAJ — duyuru planla\n"
        "/planlar — bekleyen duyurular\n"
        "/iptal PLAN_ID — planı iptal et\n\n"
        "RAPORLAR\n"
        "/istatistik — üye ve başvuru özeti"
    )


async def members(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not admin_only(update):
        return
    with connect() as c:
        rows = c.execute(
            "SELECT user_id,expires_at FROM memberships WHERE status='active' ORDER BY expires_at"
        ).fetchall()
    rows = [(u, e) for u, e in rows if datetime.fromisoformat(e) > now()]
    if not rows:
        await update.message.reply_text("Aktif VIP üye yok.")
        return
    await update.message.reply_text(
        "Aktif VIP üyeler:\n" + "\n".join(
            f"• {u} — {datetime.fromisoformat(e):%d.%m.%Y %H:%M}" for u, e in rows[:100]
        )
    )


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
        f"👑 Aktif VIP üye: {active}\n"
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


def main():
    if not TOKEN:
        raise RuntimeError("BOT_TOKEN ayarlanmamış.")
    if not ADMIN_IDS:
        raise RuntimeError("ADMIN_IDS ayarlanmamış.")
    if not CHANNEL_ID:
        raise RuntimeError("CHANNEL_ID ayarlanmamış.")
    init_db()
    app = Application.builder().token(TOKEN).post_init(post_init).build()
    app.add_handler(CommandHandler("start", start))
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
    app.add_handler(CallbackQueryHandler(admin_callbacks, pattern=r"^(approve|reject):"))
    app.add_handler(CallbackQueryHandler(buttons, pattern=r"^(apply|status|support)$"))
    log.info("ALTAY VIP BOT tek kanal botu çalışıyor.")
    app.run_polling()


if __name__ == "__main__":
    main()
