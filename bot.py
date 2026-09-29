# -*- coding: utf-8 -*-
import os, sqlite3, logging, asyncio
from datetime import datetime, timedelta
from telegram import Update, InlineKeyboardButton, InlineKeyboardMarkup, ReplyKeyboardMarkup, KeyboardButton
from telegram.ext import Application, CommandHandler, MessageHandler, CallbackQueryHandler, ContextTypes, filters

DB='kino_bot.db'
ADMIN_ID=6704504786

logging.basicConfig(level=logging.INFO, format='%(asctime)s - %(levelname)s - %(message)s')

# ---------- ENV ----------
def load_env():
    if os.path.exists('.env'):
        for line in open('.env', encoding='utf-8'):
            line=line.strip()
            if not line or line.startswith('#') or '=' not in line: continue
            k,v=line.split('=',1); os.environ.setdefault(k.strip(),v.strip().strip('"').strip("'"))
load_env()
TOKEN=os.getenv('BOT_TOKEN','').strip()

# ---------- DB ----------
def db():
    c=sqlite3.connect(DB); c.row_factory=sqlite3.Row; return c

def init_db():
    c=db(); cur=c.cursor()
    # repair old users table that used id instead of user_id
    cur.execute("SELECT name FROM sqlite_master WHERE type='table' AND name='users'")
    if cur.fetchone():
        cols=[r['name'] for r in cur.execute('PRAGMA table_info(users)').fetchall()]
        if 'user_id' not in cols:
            cur.execute('ALTER TABLE users RENAME TO users_old')
    cur.executescript('''
    CREATE TABLE IF NOT EXISTS users(
      user_id INTEGER PRIMARY KEY, username TEXT, first_name TEXT,
      joined_at TEXT, blocked INTEGER DEFAULT 0, vip_until TEXT
    );
    CREATE TABLE IF NOT EXISTS movies(
      id INTEGER PRIMARY KEY AUTOINCREMENT, code TEXT UNIQUE NOT NULL,
      title TEXT NOT NULL, year TEXT, country TEXT, genre TEXT,
      media_type TEXT NOT NULL, file_id TEXT NOT NULL, is_vip INTEGER DEFAULT 0,
      created_at TEXT
    );
    CREATE TABLE IF NOT EXISTS channels(
      id INTEGER PRIMARY KEY AUTOINCREMENT, chat_id TEXT UNIQUE, title TEXT, url TEXT, required INTEGER DEFAULT 1
    );
    CREATE TABLE IF NOT EXISTS vip_requests(
      id INTEGER PRIMARY KEY AUTOINCREMENT, user_id INTEGER, days INTEGER, amount INTEGER,
      status TEXT DEFAULT 'pending', created_at TEXT
    );
    CREATE TABLE IF NOT EXISTS favorites(
      user_id INTEGER, movie_id INTEGER, created_at TEXT, PRIMARY KEY(user_id,movie_id)
    );
    CREATE TABLE IF NOT EXISTS settings(key TEXT PRIMARY KEY, value TEXT);
    ''')
    old=cur.execute("SELECT name FROM sqlite_master WHERE type='table' AND name='users_old'").fetchone()
    if old:
        oldcols=[r['name'] for r in cur.execute('PRAGMA table_info(users_old)').fetchall()]
        if 'id' in oldcols:
            try:
                cur.execute('''INSERT OR IGNORE INTO users(user_id,username,first_name,joined_at,blocked,vip_until)
                               SELECT id, username, first_name, COALESCE(joined_at,?), COALESCE(blocked,0), vip_until FROM users_old''',(datetime.now().isoformat(),))
            except Exception: pass
        cur.execute('DROP TABLE users_old')
    defaults={'card_number':'8600 0000 0000 0000','card_name':'KINO MX','price_1':'5000','price_7':'15000','price_30':'30000','price_90':'70000','welcome':'🎬 Kino MX ga xush kelibsiz!'}
    for k,v in defaults.items(): cur.execute('INSERT OR IGNORE INTO settings(key,value) VALUES(?,?)',(k,v))
    c.commit(); c.close()

def setting(k):
    c=db(); r=c.execute('SELECT value FROM settings WHERE key=?',(k,)).fetchone(); c.close(); return r['value'] if r else ''
def set_setting(k,v):
    c=db(); c.execute('INSERT INTO settings(key,value) VALUES(?,?) ON CONFLICT(key) DO UPDATE SET value=excluded.value',(k,v)); c.commit(); c.close()

def save_user(u):
    c=db(); now=datetime.now().isoformat(); c.execute('''INSERT INTO users(user_id,username,first_name,joined_at) VALUES(?,?,?,?)
      ON CONFLICT(user_id) DO UPDATE SET username=excluded.username, first_name=excluded.first_name''',(u.id,u.username,u.first_name,now)); c.commit(); c.close()

def vip_active(uid):
    c=db(); r=c.execute('SELECT vip_until FROM users WHERE user_id=?',(uid,)).fetchone(); c.close()
    if not r or not r['vip_until']: return False
    try: return datetime.fromisoformat(r['vip_until']) > datetime.now()
    except: return False

def grant_vip(uid,days):
    c=db(); r=c.execute('SELECT vip_until FROM users WHERE user_id=?',(uid,)).fetchone(); now=datetime.now()
    base=now
    if r and r['vip_until']:
        try:
            old=datetime.fromisoformat(r['vip_until']); base=max(now,old)
        except: pass
    until=base+timedelta(days=days)
    c.execute('UPDATE users SET vip_until=? WHERE user_id=?',(until.isoformat(),uid)); c.commit(); c.close(); return until

# ---------- keyboards ----------
def user_kb():
    return ReplyKeyboardMarkup([[KeyboardButton('🔎 Kino qidirish'),KeyboardButton('🎬 Kino kodi')],
      [KeyboardButton('🆕 Yangi kinolar'),KeyboardButton('📂 Kategoriyalar')],
      [KeyboardButton('💎 VIP'),KeyboardButton('👤 Profil')],[KeyboardButton('ℹ️ Yordam')]],resize_keyboard=True,is_persistent=True)

def admin_kb():
    return ReplyKeyboardMarkup([[KeyboardButton('➕ Kino qo‘shish'),KeyboardButton('✏️ Tahrirlash')],
      [KeyboardButton('🗑 O‘chirish'),KeyboardButton('📢 Kanallar')],[KeyboardButton('📣 Reklama'),KeyboardButton('👥 Foydalanuvchilar')],
      [KeyboardButton('💎 VIP'),KeyboardButton('📊 Statistika')],[KeyboardButton('⚙️ Sozlamalar')]],resize_keyboard=True,is_persistent=True)

def cancel_kb(): return ReplyKeyboardMarkup([[KeyboardButton('❌ Bekor qilish')]],resize_keyboard=True)

# ---------- subscription ----------
async def subscribed(uid, context):
    c=db(); chans=c.execute('SELECT * FROM channels WHERE required=1').fetchall(); c.close()
    missing=[]
    for ch in chans:
        try:
            m=await context.bot.get_chat_member(ch['chat_id'],uid)
            if m.status not in ('member','administrator','creator'): missing.append(ch)
        except Exception as e:
            logging.warning('Channel check failed %s: %s',ch['chat_id'],e)
            # If bot cannot inspect the channel, do not lock users out.
    return missing

async def subscription_prompt(update, context):
    missing=await subscribed(update.effective_user.id,context)
    if not missing: return True
    rows=[]
    for ch in missing:
        rows.append([InlineKeyboardButton('📢 '+(ch['title'] or 'Kanal'),url=ch['url'])])
    rows.append([InlineKeyboardButton('✅ Tekshirish',callback_data='check_sub')])
    text='🔒 Botdan foydalanish uchun quyidagi kanallarga a’zo bo‘ling:'
    if update.callback_query: await update.callback_query.message.reply_text(text,reply_markup=InlineKeyboardMarkup(rows))
    else: await update.effective_message.reply_text(text,reply_markup=InlineKeyboardMarkup(rows))
    return False

# ---------- common ----------
async def start(update,context):
    save_user(update.effective_user)
    if update.effective_user.id==ADMIN_ID:
        await update.message.reply_text('🛠 Admin panel tayyor.',reply_markup=admin_kb()); return
    if not await subscription_prompt(update,context): return
    await update.message.reply_text(setting('welcome'),reply_markup=user_kb())

async def cancel(update,context):
    context.user_data.clear(); await update.message.reply_text('❌ Bekor qilindi.',reply_markup=admin_kb() if update.effective_user.id==ADMIN_ID else user_kb())

# ---------- movie send ----------
def movie_text(m):
    vip='💎 VIP' if m['is_vip'] else '🆓'
    return f"{vip} <b>{m['title']}</b>\n\n🎬 Kod: <code>{m['code']}</code>\n📅 Yil: {m['year'] or '-'}\n🌍 Davlat: {m['country'] or '-'}\n🎭 Janr: {m['genre'] or '-'}"

async def send_movie(update,context,m):
    uid=update.effective_user.id
    if m['is_vip'] and uid!=ADMIN_ID and not vip_active(uid):
        await update.effective_message.reply_text('💎 Bu kino VIP. Avval VIP paketini faollashtiring.',reply_markup=user_kb()); return
    fav=db().execute('SELECT 1 FROM favorites WHERE user_id=? AND movie_id=?',(uid,m['id'])).fetchone()
    buttons=[[InlineKeyboardButton('❤️ Sevimlilarga',callback_data=f'fav:{m["id"]}')]]
    cap=movie_text(m)
    if m['media_type']=='video': await update.effective_message.reply_video(m['file_id'],caption=cap,parse_mode='HTML',reply_markup=InlineKeyboardMarkup(buttons))
    else: await update.effective_message.reply_document(m['file_id'],caption=cap,parse_mode='HTML',reply_markup=InlineKeyboardMarkup(buttons))

def get_movie(code):
    c=db(); r=c.execute('SELECT * FROM movies WHERE code=?',(code.strip(),)).fetchone(); c.close(); return r

# ---------- user ----------
async def user_vip(update,context):
    uid=update.effective_user.id
    if vip_active(uid):
        c=db(); r=c.execute('SELECT vip_until FROM users WHERE user_id=?',(uid,)).fetchone(); c.close()
        await update.message.reply_text(f'💎 VIP faol\n⏳ Amal qilish muddati: {r["vip_until"]}')
        return
    text='💎 <b>VIP paketlar</b>\n\n'
    for d in (1,7,30,90): text+=f'• {d} kun: <b>{setting("price_"+str(d))} so‘m</b>\n'
    text+='\nTo‘lovdan keyin chekni shu botga yuborasiz. Admin tasdiqlagach VIP avtomatik beriladi.'
    kb=[[InlineKeyboardButton('1 kun',callback_data='buy:1'),InlineKeyboardButton('7 kun',callback_data='buy:7')],
        [InlineKeyboardButton('30 kun',callback_data='buy:30'),InlineKeyboardButton('90 kun',callback_data='buy:90')]]
    await update.message.reply_text(text,parse_mode='HTML',reply_markup=InlineKeyboardMarkup(kb))

async def buy_start(query,context,days):
    amount=setting('price_'+str(days)); context.user_data['state']='vip_receipt'; context.user_data['vip_days']=days; context.user_data['vip_amount']=amount
    await query.message.reply_text(f'💳 <b>{days} kunlik VIP</b>\n💰 Narx: <b>{amount} so‘m</b>\n\nKarta: <code>{setting("card_number")}</code>\n👤 Karta egasi: <b>{setting("card_name")}</b>\n\nTo‘lovni amalga oshiring va <b>chek rasmini</b> shu yerga yuboring.',parse_mode='HTML',reply_markup=cancel_kb())

async def user_profile(update,context):
    uid=update.effective_user.id; c=db(); u=c.execute('SELECT * FROM users WHERE user_id=?',(uid,)).fetchone(); fav=c.execute('SELECT COUNT(*) n FROM favorites WHERE user_id=?',(uid,)).fetchone()['n']; c.close()
    vip='Faol' if vip_active(uid) else 'Faol emas'
    await update.message.reply_text(f'👤 <b>Profil</b>\n\n🆔 ID: <code>{uid}</code>\n👤 Ism: {u["first_name"]}\n💎 VIP: {vip}\n❤️ Sevimlilar: {fav}',parse_mode='HTML')

async def search_results(update,context,q):
    c=db(); rows=c.execute('SELECT * FROM movies WHERE title LIKE ? OR genre LIKE ? OR country LIKE ? OR code LIKE ? ORDER BY id DESC LIMIT 20',(f'%{q}%',f'%{q}%',f'%{q}%',f'%{q}%')).fetchall(); c.close()
    if not rows: await update.message.reply_text('❌ Kino topilmadi.'); return
    kb=[[InlineKeyboardButton(f'{r["code"]} | {r["title"]}',callback_data=f'movie:{r["id"]}')] for r in rows]
    await update.message.reply_text('🔎 Natijalar:',reply_markup=InlineKeyboardMarkup(kb))

# ---------- admin workflows ----------
def is_admin(update): return update.effective_user and update.effective_user.id==ADMIN_ID

async def admin_stats(update,context):
    c=db(); vals={k:c.execute(q).fetchone()[0] for k,q in {'users':'SELECT COUNT(*) FROM users','movies':'SELECT COUNT(*) FROM movies','vip':'SELECT COUNT(*) FROM movies WHERE is_vip=1','channels':'SELECT COUNT(*) FROM channels','fav':'SELECT COUNT(*) FROM favorites'}.items()}; c.close()
    await update.message.reply_text(f'📊 <b>Statistika</b>\n\n👥 Foydalanuvchilar: {vals["users"]}\n🎬 Kinolar: {vals["movies"]}\n💎 VIP kinolar: {vals["vip"]}\n📢 Kanallar: {vals["channels"]}\n❤️ Sevimlilar: {vals["fav"]}',parse_mode='HTML')

async def add_movie_start(update,context):
    context.user_data.clear(); context.user_data['state']='movie_video'; await update.message.reply_text('🎬 Kino videosini yuboring:',reply_markup=cancel_kb())

async def handle_movie_add(update,context):
    s=context.user_data.get('state')
    if s=='movie_video':
        if update.message.video: context.user_data['media_type']='video'; context.user_data['file_id']=update.message.video.file_id
        elif update.message.document: context.user_data['media_type']='document'; context.user_data['file_id']=update.message.document.file_id
        else: await update.message.reply_text('❗ Video yoki fayl yuboring.'); return
        context.user_data['state']='movie_title'; await update.message.reply_text('1️⃣ Kino nomini yozing:'); return
    if s=='movie_title': context.user_data['title']=update.message.text; context.user_data['state']='movie_year'; await update.message.reply_text('2️⃣ Yil:'); return
    if s=='movie_year': context.user_data['year']=update.message.text; context.user_data['state']='movie_country'; await update.message.reply_text('3️⃣ Davlat:'); return
    if s=='movie_country': context.user_data['country']=update.message.text; context.user_data['state']='movie_genre'; await update.message.reply_text('4️⃣ Janr:'); return
    if s=='movie_genre': context.user_data['genre']=update.message.text; context.user_data['state']='movie_code'; await update.message.reply_text('5️⃣ Kino kodi, masalan 1001:'); return
    if s=='movie_code':
        code=update.message.text.strip();
        if get_movie(code): await update.message.reply_text('❗ Bu kod band. Boshqa kod kiriting.'); return
        context.user_data['code']=code; context.user_data['state']='movie_vip';
        await update.message.reply_text('6️⃣ VIP kino bo‘lsinmi?',reply_markup=ReplyKeyboardMarkup([[KeyboardButton('💎 Ha'),KeyboardButton('🆓 Yo‘q')],[KeyboardButton('❌ Bekor qilish')]],resize_keyboard=True)); return
    if s=='movie_vip':
        if update.message.text not in ('💎 Ha','🆓 Yo‘q'): await update.message.reply_text('Tugmalardan birini tanlang.'); return
        context.user_data['is_vip']=1 if update.message.text=='💎 Ha' else 0; d=context.user_data
        c=db(); c.execute('INSERT INTO movies(code,title,year,country,genre,media_type,file_id,is_vip,created_at) VALUES(?,?,?,?,?,?,?,?,?)',(d['code'],d['title'],d['year'],d['country'],d['genre'],d['media_type'],d['file_id'],d['is_vip'],datetime.now().isoformat())); c.commit(); c.close(); context.user_data.clear()
        await update.message.reply_text('✅ Kino muvaffaqiyatli qo‘shildi.',reply_markup=admin_kb())

async def edit_start(update,context): context.user_data['state']='edit_code'; await update.message.reply_text('✏️ Tahrirlash uchun kino kodini yuboring:',reply_markup=cancel_kb())
async def delete_start(update,context): context.user_data['state']='delete_code'; await update.message.reply_text('🗑 O‘chirish uchun kino kodini yuboring:',reply_markup=cancel_kb())

async def handle_edit_delete(update,context):
    s=context.user_data.get('state'); txt=update.message.text.strip()
    if s=='edit_code':
        m=get_movie(txt)
        if not m: await update.message.reply_text('❌ Kino topilmadi.'); return
        context.user_data.update(state='edit_menu',movie_id=m['id'])
        kb=[[InlineKeyboardButton('Nomi',callback_data='edit:title'),InlineKeyboardButton('Yil',callback_data='edit:year')],[InlineKeyboardButton('Davlat',callback_data='edit:country'),InlineKeyboardButton('Janr',callback_data='edit:genre')],[InlineKeyboardButton('Kod',callback_data='edit:code'),InlineKeyboardButton('VIP',callback_data='edit:vip')]]
        await update.message.reply_text('Qaysi maydonni tahrirlaysiz?',reply_markup=InlineKeyboardMarkup(kb)); return
    if s=='delete_code':
        m=get_movie(txt)
        if not m: await update.message.reply_text('❌ Kino topilmadi.'); return
        context.user_data.update(movie_id=m['id'],state='delete_confirm')
        await update.message.reply_text(f'🗑 <b>{m["title"]}</b> o‘chirilsinmi?',parse_mode='HTML',reply_markup=InlineKeyboardMarkup([[InlineKeyboardButton('✅ Ha',callback_data='del_yes'),InlineKeyboardButton('❌ Yo‘q',callback_data='del_no')]]))
    if s=='edit_value':
        field=context.user_data['field']; val=txt
        c=db();
        if field=='code' and c.execute('SELECT 1 FROM movies WHERE code=? AND id!=?',(val,context.user_data['movie_id'])).fetchone(): await update.message.reply_text('❌ Bu kod band.'); c.close(); return
        c.execute(f'UPDATE movies SET {field}=? WHERE id=?',(val,context.user_data['movie_id'])); c.commit(); c.close(); context.user_data.clear(); await update.message.reply_text('✅ Tahrirlandi.',reply_markup=admin_kb())

# ---------- channels/broadcast/settings ----------
async def channels_menu(update,context):
    c=db(); rows=c.execute('SELECT * FROM channels').fetchall(); c.close(); text='📢 <b>Kanallar</b>\n\n'
    for r in rows: text+=f'• {r["title"]} | {r["chat_id"]}\n'
    text+='\nKanal qo‘shish: <code>@username | Nomi | https://t.me/username</code>\nO‘chirish: <code>del @username</code>'
    context.user_data['state']='channel_manage'; await update.message.reply_text(text,parse_mode='HTML',reply_markup=cancel_kb())

async def handle_channel(update,context):
    t=update.message.text.strip()
    c=db()
    try:
        if t.lower().startswith('del '): c.execute('DELETE FROM channels WHERE chat_id=?',(t[4:].strip(),)); c.commit(); await update.message.reply_text('✅ Kanal o‘chirildi.',reply_markup=admin_kb())
        else:
            parts=[x.strip() for x in t.split('|')]
            if len(parts)!=3: await update.message.reply_text('Format: @kanal | Nomi | https://t.me/kanal'); c.close(); return
            c.execute('INSERT OR REPLACE INTO channels(chat_id,title,url,required) VALUES(?,?,?,1)',tuple(parts)); c.commit(); await update.message.reply_text('✅ Kanal qo‘shildi.',reply_markup=admin_kb())
    finally: c.close()
    context.user_data.clear()

async def broadcast_start(update,context): context.user_data['state']='broadcast'; await update.message.reply_text('📣 Reklama uchun xabar, rasm yoki video yuboring:',reply_markup=cancel_kb())
async def do_broadcast(update,context):
    c=db(); users=c.execute('SELECT user_id FROM users WHERE blocked=0').fetchall(); c.close(); ok=bad=0
    for r in users:
        try: await context.bot.copy_message(chat_id=r['user_id'],from_chat_id=update.effective_chat.id,message_id=update.message.message_id); ok+=1
        except Exception: bad+=1
        await asyncio.sleep(.03)
    context.user_data.clear(); await update.message.reply_text(f'📣 Yuborildi: {ok}\n❌ Yetib bormadi: {bad}',reply_markup=admin_kb())

async def settings_menu(update,context):
    text=f'⚙️ Sozlamalar\n\n💳 Karta: {setting("card_number")}\n👤 Egasi: {setting("card_name")}\n1 kun: {setting("price_1")}\n7 kun: {setting("price_7")}\n30 kun: {setting("price_30")}\n90 kun: {setting("price_90")}\n\nO‘zgartirish formati:\ncard 8600123456789012\nname KINO MX\nprice 1 5000\nprice 7 15000\nprice 30 30000\nprice 90 70000'
    context.user_data['state']='settings'; await update.message.reply_text(text,reply_markup=cancel_kb())

async def handle_settings(update,context):
    p=update.message.text.strip().split(maxsplit=2)
    if len(p)>=2 and p[0].lower()=='card': set_setting('card_number',p[1]); msg='✅ Karta yangilandi.'
    elif len(p)>=2 and p[0].lower()=='name': set_setting('card_name',' '.join(p[1:])); msg='✅ Karta egasi yangilandi.'
    elif len(p)==3 and p[0].lower()=='price' and p[1] in ('1','7','30','90'): set_setting('price_'+p[1],p[2]); msg='✅ Narx yangilandi.'
    else: msg='❗ Format noto‘g‘ri.'
    await update.message.reply_text(msg,reply_markup=admin_kb()); context.user_data.clear()

async def users_menu(update,context):
    c=db(); n=c.execute('SELECT COUNT(*) FROM users').fetchone()[0]; c.close(); await update.message.reply_text(f'👥 Foydalanuvchilar: {n}\n\nVIP berish: /givevip USER_ID KUN\nBloklash: /block USER_ID\nOchish: /unblock USER_ID')

# ---------- callbacks ----------
async def callbacks(update,context):
    q=update.callback_query; data=q.data; await q.answer()
    if data=='check_sub':
        if await subscription_prompt(update,context): await q.message.reply_text('✅ A’zolik tasdiqlandi.',reply_markup=user_kb())
        return
    if data.startswith('movie:'):
        c=db(); m=c.execute('SELECT * FROM movies WHERE id=?',(int(data.split(':')[1]),)).fetchone(); c.close()
        if m and await subscription_prompt(update,context): await send_movie(update,context,m)
        return
    if data.startswith('fav:'):
        mid=int(data.split(':')[1]); c=db()
        try: c.execute('INSERT INTO favorites(user_id,movie_id,created_at) VALUES(?,?,?)',(update.effective_user.id,mid,datetime.now().isoformat())); c.commit(); msg='❤️ Sevimlilarga qo‘shildi.'
        except sqlite3.IntegrityError: c.execute('DELETE FROM favorites WHERE user_id=? AND movie_id=?',(update.effective_user.id,mid)); c.commit(); msg='💔 Sevimlilardan olib tashlandi.'
        c.close(); await q.message.reply_text(msg); return
    if data.startswith('buy:'): await buy_start(q,context,int(data.split(':')[1])); return
    if data in ('del_yes','del_no'):
        if data=='del_yes': c=db(); c.execute('DELETE FROM movies WHERE id=?',(context.user_data['movie_id'],)); c.commit(); c.close(); await q.message.reply_text('✅ Kino o‘chirildi.',reply_markup=admin_kb())
        else: await q.message.reply_text('Bekor qilindi.',reply_markup=admin_kb())
        context.user_data.clear(); return
    if data.startswith('edit:'):
        field=data.split(':')[1]; context.user_data['state']='edit_value'; context.user_data['field']=field
        if field=='vip':
            await q.message.reply_text('VIP uchun 1, oddiy kino uchun 0 yuboring.'); return
        await q.message.reply_text(f'Yangi qiymatni yuboring: {field}'); return
    if data.startswith('vipok:'):
        rid=int(data.split(':')[1]); c=db(); r=c.execute('SELECT * FROM vip_requests WHERE id=?',(rid,)).fetchone();
        if not r or r['status']!='pending': c.close(); await q.message.reply_text('Bu so‘rov allaqachon ko‘rilgan.'); return
        c.execute('UPDATE vip_requests SET status="approved" WHERE id=?',(rid,)); c.commit(); c.close(); until=grant_vip(r['user_id'],r['days']); await context.bot.send_message(r['user_id'],f'✅ To‘lovingiz tasdiqlandi!\n💎 VIP {r["days"]} kun faol.\n⏳ {until}') ; await q.message.reply_text('✅ VIP tasdiqlandi.'); return
    if data.startswith('vipno:'):
        rid=int(data.split(':')[1]); c=db(); r=c.execute('SELECT * FROM vip_requests WHERE id=?',(rid,)).fetchone(); c.execute('UPDATE vip_requests SET status="rejected" WHERE id=?',(rid,)); c.commit(); c.close();
        if r: await context.bot.send_message(r['user_id'],'❌ To‘lov cheki rad etildi. Iltimos, to‘lovni tekshirib qayta yuboring.')
        await q.message.reply_text('❌ So‘rov bekor qilindi.'); return

# ---------- text router ----------
async def router(update,context):
    if not update.message: return
    save_user(update.effective_user); uid=update.effective_user.id; text=update.message.text or ''
    if text=='❌ Bekor qilish': await cancel(update,context); return
    s=context.user_data.get('state')
    # receipt is image/document
    if s=='vip_receipt':
        if update.message.photo:
            file_id=update.message.photo[-1].file_id
        elif update.message.document:
            file_id=update.message.document.file_id
        else: await update.message.reply_text('❗ Chek rasmini yuboring.'); return
        days=context.user_data['vip_days']; amount=context.user_data['vip_amount']; c=db(); cur=c.execute('INSERT INTO vip_requests(user_id,days,amount,created_at) VALUES(?,?,?,?)',(uid,days,int(amount),datetime.now().isoformat())); rid=cur.lastrowid; c.commit(); c.close()
        await update.message.reply_text('✅ Chekingiz adminga yuborildi. Tasdiqlashni kuting.',reply_markup=user_kb())
        kb=InlineKeyboardMarkup([[InlineKeyboardButton('✅ Tasdiqlash',callback_data=f'vipok:{rid}'),InlineKeyboardButton('❌ Bekor qilish',callback_data=f'vipno:{rid}')]])
        caption=f'💳 <b>Yangi VIP to‘lov</b>\n🆔 User: <code>{uid}</code>\n📦 Paket: {days} kun\n💰 Summa: {amount} so‘m'
        if update.message.photo: await context.bot.send_photo(ADMIN_ID,file_id,caption=caption,parse_mode='HTML',reply_markup=kb)
        else: await context.bot.send_document(ADMIN_ID,file_id,caption=caption,parse_mode='HTML',reply_markup=kb)
        context.user_data.clear(); return
    if is_admin(update):
        if s=='movie_video' or s in ('movie_title','movie_year','movie_country','movie_genre','movie_code','movie_vip'): await handle_movie_add(update,context); return
        if s in ('edit_code','edit_value','delete_code'): await handle_edit_delete(update,context); return
        if s=='channel_manage': await handle_channel(update,context); return
        if s=='broadcast': await do_broadcast(update,context); return
        if s=='settings': await handle_settings(update,context); return
        actions={'➕ Kino qo‘shish':add_movie_start,'✏️ Tahrirlash':edit_start,'🗑 O‘chirish':delete_start,'📢 Kanallar':channels_menu,'📣 Reklama':broadcast_start,'👥 Foydalanuvchilar':users_menu,'📊 Statistika':admin_stats,'⚙️ Sozlamalar':settings_menu}
        if text=='💎 VIP':
            await update.message.reply_text('💎 VIP boshqaruvi: /givevip USER_ID KUN\nNarxlarni ⚙️ Sozlamalar orqali o‘zgartirasiz.',reply_markup=admin_kb()); return
        if text in actions: await actions[text](update,context); return
        if update.message.photo or update.message.document: await update.message.reply_text('❗ Avval amal tanlang.',reply_markup=admin_kb()); return
        return
    if not await subscription_prompt(update,context): return
    if text=='🔎 Kino qidirish': context.user_data['state']='search'; await update.message.reply_text('🔎 Kino nomi, janri yoki davlatini yozing:',reply_markup=cancel_kb()); return
    if text=='🎬 Kino kodi': context.user_data['state']='code'; await update.message.reply_text('🎬 Kino kodini yuboring:',reply_markup=cancel_kb()); return
    if text=='🆕 Yangi kinolar':
        c=db(); rows=c.execute('SELECT * FROM movies ORDER BY id DESC LIMIT 15').fetchall(); c.close();
        if not rows: await update.message.reply_text('Hozircha kino yo‘q.'); return
        await update.message.reply_text('🆕 Yangi kinolar',reply_markup=InlineKeyboardMarkup([[InlineKeyboardButton(f'{r["code"]} | {r["title"]}',callback_data=f'movie:{r["id"]}')] for r in rows])); return
    if text=='📂 Kategoriyalar':
        c=db(); rows=c.execute('SELECT DISTINCT genre FROM movies WHERE genre IS NOT NULL AND genre!=""').fetchall(); c.close();
        await update.message.reply_text('📂 Kategoriyalar\n\n'+'\n'.join('• '+r['genre'] for r in rows) if rows else '📂 Kategoriyalar hozircha bo‘sh.'); return
    if text=='💎 VIP': await user_vip(update,context); return
    if text=='👤 Profil': await user_profile(update,context); return
    if text=='ℹ️ Yordam': await update.message.reply_text('ℹ️ Kino kodini yuboring yoki qidiruvdan foydalaning. VIP kinolar uchun VIP paket sotib olinadi.'); return
    if s=='search': await search_results(update,context,text); context.user_data.clear(); await update.message.reply_text('Menu:',reply_markup=user_kb()); return
    if s=='code':
        m=get_movie(text)
        if m: await send_movie(update,context,m)
        else: await update.message.reply_text('❌ Bunday koddagi kino topilmadi.')
        context.user_data.clear(); return
    m=get_movie(text)
    if m: await send_movie(update,context,m)

# ---------- commands ----------
async def givevip(update,context):
    if not is_admin(update): return
    try: uid=int(context.args[0]); days=int(context.args[1]); until=grant_vip(uid,days); await update.message.reply_text(f'✅ VIP berildi. {until}')
    except: await update.message.reply_text('/givevip USER_ID KUN')
async def block(update,context):
    if not is_admin(update): return
    try: uid=int(context.args[0]); c=db(); c.execute('UPDATE users SET blocked=1 WHERE user_id=?',(uid,)); c.commit(); c.close(); await update.message.reply_text('✅ Bloklandi.')
    except: await update.message.reply_text('/block USER_ID')
async def unblock(update,context):
    if not is_admin(update): return
    try: uid=int(context.args[0]); c=db(); c.execute('UPDATE users SET blocked=0 WHERE user_id=?',(uid,)); c.commit(); c.close(); await update.message.reply_text('✅ Blokdan chiqarildi.')
    except: await update.message.reply_text('/unblock USER_ID')

async def error_handler(update,context): logging.exception('Unhandled error',exc_info=context.error)

def main():
    if not TOKEN:
        print('❌ BOT_TOKEN topilmadi. .env faylga BOT_TOKEN=YANGI_TOKEN yozing.'); return
    init_db(); app=Application.builder().token(TOKEN).build()
    app.add_handler(CommandHandler('start',start)); app.add_handler(CommandHandler('cancel',cancel)); app.add_handler(CommandHandler('givevip',givevip)); app.add_handler(CommandHandler('block',block)); app.add_handler(CommandHandler('unblock',unblock))
    app.add_handler(CallbackQueryHandler(callbacks))
    app.add_handler(MessageHandler((filters.PHOTO | filters.Document.ALL | filters.VIDEO | filters.TEXT) & ~filters.COMMAND,router))
    app.add_error_handler(error_handler)
    print('✅ Kino MX bot ishga tushdi...'); app.run_polling(allowed_updates=Update.ALL_TYPES)

if __name__=='__main__': main()
