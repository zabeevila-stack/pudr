import asyncio, os, sqlite3, html, json, logging, re
from pathlib import Path
from decimal import Decimal, ROUND_UP
import aiohttp, uuid
from aiogram import Bot, Dispatcher, F, BaseMiddleware
from aiogram.methods import SendMessage, SendPhoto, SendAnimation, CopyMessage, ForwardMessage
from aiogram.types import Message, CallbackQuery
from aiogram.exceptions import TelegramBadRequest
from aiogram.client.session.aiohttp import AiohttpSession
from aiogram.filters import CommandStart, Command
from aiogram.types import InlineKeyboardMarkup, InlineKeyboardButton
from aiogram.fsm.context import FSMContext
from aiogram.fsm.state import State, StatesGroup
from aiogram.client.default import DefaultBotProperties
from dotenv import load_dotenv
load_dotenv(Path(__file__).with_name('.env'))
ADMIN={int(x) for x in os.getenv('ADMIN_IDS','').split(',') if x.strip().isdigit()}
db=sqlite3.connect(Path(__file__).with_name('tripple.db'))
db.row_factory=sqlite3.Row
db.executescript('''CREATE TABLE IF NOT EXISTS users(id INTEGER PRIMARY KEY,balance INTEGER DEFAULT 0,spent INTEGER DEFAULT 0); CREATE TABLE IF NOT EXISTS products(id INTEGER PRIMARY KEY,name TEXT,price INTEGER,description TEXT); CREATE TABLE IF NOT EXISTS stock(id INTEGER PRIMARY KEY,product INTEGER,content TEXT); CREATE TABLE IF NOT EXISTS cart(uid INTEGER,product INTEGER,qty INTEGER,PRIMARY KEY(uid,product)); CREATE TABLE IF NOT EXISTS orders(id INTEGER PRIMARY KEY,uid INTEGER,product INTEGER,total INTEGER,content TEXT); CREATE TABLE IF NOT EXISTS settings(key TEXT PRIMARY KEY,value TEXT);''')
if 'points' not in [r[1] for r in db.execute('PRAGMA table_info(users)')]:
    db.execute('ALTER TABLE users ADD COLUMN points INTEGER NOT NULL DEFAULT 0')
db.executescript("CREATE TABLE IF NOT EXISTS achievements(uid INTEGER, code TEXT, PRIMARY KEY(uid,code));")
db.executescript("""CREATE TABLE IF NOT EXISTS categories(id INTEGER PRIMARY KEY,name TEXT NOT NULL,parent INTEGER DEFAULT 0); CREATE TABLE IF NOT EXISTS favorites(uid INTEGER,product INTEGER,PRIMARY KEY(uid,product)); CREATE TABLE IF NOT EXISTS tickets(id INTEGER PRIMARY KEY,uid INTEGER,body TEXT);""")
for col,decl in [('category','INTEGER DEFAULT 0'),('bulk_qty','INTEGER DEFAULT 0'),('bulk_price','INTEGER DEFAULT 0')]:
    if col not in [r[1] for r in db.execute('PRAGMA table_info(products)')]: db.execute(f'ALTER TABLE products ADD COLUMN {col} {decl}')
if 'manual_qty' not in [r[1] for r in db.execute('PRAGMA table_info(products)')]:
    db.execute('ALTER TABLE products ADD COLUMN manual_qty INTEGER DEFAULT NULL')
if 'pending_qty' not in [r[1] for r in db.execute('PRAGMA table_info(orders)')]:
    db.execute('ALTER TABLE orders ADD COLUMN pending_qty INTEGER DEFAULT 0')
db.commit()
def available(pid):
    r=db.execute('SELECT manual_qty FROM products WHERE id=?',(pid,)).fetchone()
    if not r: return 0
    return r[0] if r[0] is not None else db.execute('SELECT count(*) FROM stock WHERE product=?',(pid,)).fetchone()[0]
db.execute("CREATE TABLE IF NOT EXISTS topups(id INTEGER PRIMARY KEY, uid INTEGER NOT NULL, method TEXT NOT NULL, amount INTEGER NOT NULL, due INTEGER NOT NULL, status TEXT NOT NULL, external TEXT, quote TEXT, url TEXT, ref TEXT UNIQUE)")
db.commit()
BANK_CARD = "2204120146814724"  # Вставьте номер карты сюда
BANK_MIN_AMOUNT = 100
db.executescript("CREATE TABLE IF NOT EXISTS blocked(uid INTEGER PRIMARY KEY); CREATE TABLE IF NOT EXISTS user_names(uid INTEGER PRIMARY KEY, name TEXT); CREATE TABLE IF NOT EXISTS chat_messages(uid INTEGER, mid INTEGER, PRIMARY KEY(uid,mid)); CREATE TABLE IF NOT EXISTS deleted_ids(kind TEXT PRIMARY KEY, last_id INTEGER);")
CHANNEL=os.getenv('REQUIRED_CHANNEL','').strip()
CHANNEL_URL=os.getenv('REQUIRED_CHANNEL_URL','').strip() or ('https://t.me/'+CHANNEL[1:] if CHANNEL.startswith('@') else '')
db.execute("CREATE TABLE IF NOT EXISTS stars_requests(id INTEGER PRIMARY KEY, uid INTEGER, qty INTEGER, recipient TEXT, total INTEGER, status TEXT DEFAULT 'pending')")
db.commit()
dp=Dispatcher()
def blocked(uid): return bool(db.execute('SELECT 1 FROM blocked WHERE uid=?',(uid,)).fetchone())
def track(uid,mid):
    with db: db.execute('INSERT OR IGNORE INTO chat_messages VALUES (?,?)',(uid,mid))
class AccessGuard(BaseMiddleware):
    async def __call__(self,handler,event,data):
        u=event.from_user
        if u:
            with db: db.execute('INSERT OR REPLACE INTO user_names VALUES (?,?)',(u.id,'@'+u.username if u.username else u.full_name))
            if isinstance(event,Message) and event.chat.type=='private': track(u.id,event.message_id)
            if isinstance(event,CallbackQuery) and isinstance(event.message,Message) and event.message.chat.type=='private': track(u.id,event.message.message_id)
            if u.id not in ADMIN and blocked(u.id):
                if isinstance(event,CallbackQuery): await event.answer('Доступ заблокирован',show_alert=True)
                elif isinstance(event,Message):
                    try: await event.delete()
                    except Exception: pass
                return
            if u.id not in ADMIN and CHANNEL:
                try:
                    member=await data['bot'].get_chat_member(int(CHANNEL) if CHANNEL.lstrip('-').isdigit() else CHANNEL,u.id)
                    allowed=member.status in ('creator','administrator','member') or (member.status=='restricted' and member.is_member)
                except Exception:
                    logging.exception('Ошибка проверки подписки'); allowed=False
                if not allowed:
                    text='<b>♦ TRIPPLE SHOP | ДОСТУП</b>\n\nДобро пожаловать! Подпишись на наш канал, чтобы открыть каталог, узнавать о новинках и специальных предложениях.\n\nПосле подписки нажми «Проверить подписку».'
                    markup=kb([InlineKeyboardButton(text='Подписаться',url=CHANNEL_URL,style='danger')],[button('Проверить подписку','home')])
                    if isinstance(event,CallbackQuery):
                        await event.answer(); await render(event.message,text,markup,replace=True)
                    else: await event.answer(text,reply_markup=markup)
                    return
        return await handler(event,data)
dp.message.outer_middleware(AccessGuard())
dp.callback_query.outer_middleware(AccessGuard())
async def track_outgoing(make_request,bot,method):
    result=await make_request(bot,method)
    if isinstance(method,(SendMessage,SendPhoto,SendAnimation,CopyMessage,ForwardMessage)) and isinstance(method.chat_id,int) and method.chat_id>0 and hasattr(result,'message_id'):
        track(method.chat_id,result.message_id)
    return result
async def purge_chat(bot,uid):
    ids=[r[0] for r in db.execute('SELECT mid FROM chat_messages WHERE uid=? ORDER BY mid DESC',(uid,))]
    count=0
    for i in range(0,len(ids),100):
        batch=ids[i:i+100]
        try:
            await bot.delete_messages(chat_id=uid,message_ids=batch)
            count+=len(batch)
            with db: db.executemany('DELETE FROM chat_messages WHERE uid=? AND mid=?',[(uid,x) for x in batch])
        except Exception: logging.warning('Не удалось очистить часть сообщений пользователя %s',uid)
    return count
# Preserve historical IDs so old order references never point to new products.
def next_id(table):
    high=db.execute(f'SELECT COALESCE(MAX(id),0) FROM {table}').fetchone()[0]
    old=db.execute('SELECT last_id FROM deleted_ids WHERE kind=?',(table,)).fetchone()
    return max(high,old[0] if old else 0)+1
def delete_catalog(kind,ident):
    cats=[]; products=[]
    if kind=='cat':
        cats=[r[0] for r in db.execute('WITH RECURSIVE tree(id) AS (SELECT id FROM categories WHERE id=? UNION SELECT c.id FROM categories c JOIN tree t ON c.parent=t.id) SELECT id FROM tree',(ident,))]
        for cid in cats: products.extend(r[0] for r in db.execute('SELECT id FROM products WHERE category=?',(cid,)))
    elif kind=='prod': products=[r[0] for r in db.execute('SELECT id FROM products WHERE id=?',(ident,))]
    with db:
        for table,ids,prefix in [('products',products,'p:'),('categories',cats,'cat:')]:
            if ids:
                db.execute('INSERT INTO deleted_ids VALUES (?,?) ON CONFLICT(kind) DO UPDATE SET last_id=MAX(last_id,excluded.last_id)',(table,max(ids)))
            for x in ids:
                if table=='products':
                    for child in ('stock','cart','favorites'): db.execute(f'DELETE FROM {child} WHERE product=?',(x,))
                db.execute(f'DELETE FROM {table} WHERE id=?',(x,))
                for field in ('emoji','label','style','text','banner'): db.execute('DELETE FROM settings WHERE key=?',(field+':'+prefix+str(x),))
    return len(cats),len(products)

logging.basicConfig(level=logging.INFO)
def cost(p):
    return p['qty']*(p['bulk_price'] if p['bulk_qty']>0 and p['bulk_price']>0 and p['qty']>=p['bulk_qty'] else p['price'])
class InsufficientBalance(ValueError):
    pass

class Edit(StatesGroup):
    value=State()
def get(k,d=''):
    r=db.execute('SELECT value FROM settings WHERE key=?',(k,)).fetchone(); return r[0] if r else d
def put(k,v):
    with db: db.execute('INSERT OR REPLACE INTO settings VALUES (?,?)',(k,v))
def user(uid):
    if not db.execute('SELECT 1 FROM users WHERE id=?',(uid,)).fetchone():
        with db: db.execute('INSERT OR IGNORE INTO users(id) VALUES (?)',(uid,))
    return db.execute('SELECT * FROM users WHERE id=?',(uid,)).fetchone()
def money(n): return f'{n/100:.2f} ₽'
def levels():
    return [(name,int(get('threshold:'+name,str(th))),int(get('rate:'+name,str(rate)))) for name,th,rate in [('STANDARD',0,0),('BRONZE',50000,2),('SILVER',150000,3),('GOLD',500000,5),('RED ELITE',1500000,7)]]
def level(spent): return max((x for x in levels() if spent>=x[1]),key=lambda x:x[1])
def reward(uid):
    u=user(uid); count=db.execute('SELECT count(*) FROM orders WHERE uid=?',(uid,)).fetchone()[0]
    for code,ok,bonus in [('first',count>=1,500),('ten',count>=10,1500),('big',u['spent']>=500000,3000)]:
        if ok:
            cur=db.execute('INSERT OR IGNORE INTO achievements VALUES (?,?)',(uid,code))
            if cur.rowcount: db.execute('UPDATE users SET points=points+? WHERE id=?',(int(get('bonus:'+code,str(bonus))),uid))
def points(n): return f'{n/100:.2f} TP'
BUTTON_LABELS={'catalog': 'Каталог товаров', 'profile': 'Личный кабинет', 'cart': 'Корзина', 'pay': 'Пополнение баланса', 'orders': 'Мои заказы', 'rewards': 'Бонусная система', 'favorites': 'Избранное', 'support': 'Поддержка', 'home': 'Главное меню', 'admin': 'Админ-панель', 'achievements': 'Достижения', 'levels': 'Уровни лояльности', 'checkout': 'Оплата с баланса', 'checkout:points': 'Оплата с бонусами', 'search': 'Поиск товаров', 'support:write': 'Написать в поддержку', 'sort:*': 'Сортировка по цене', 'fav:*': 'Добавить / убрать избранное', 'add:*': 'В корзину / увеличить', 'minus:*': 'Уменьшить количество', 'remove:*': 'Удалить из корзины', 'o:*': 'Открыть заказ'}
BUTTON_LABELS.update({'pay:bank':'Перевод по карте','pay:crypto':'CryptoBot','pay:rocket':'xRocket','pay:history':'История пополнений','pay:sent:*':'Я оплатил','pay:check:*':'Проверить оплату','pay:open:*':'Перейти к оплате','admin:payments':'Платежи'})
def editor_label(key):
    if key in BUTTON_LABELS: return BUTTON_LABELS[key]
    for prefix,table,title in [('cat:','categories','Категория'),('p:','products','Товар')]:
        if key.startswith(prefix) and key[len(prefix):].isdigit():
            r=db.execute(f'SELECT name FROM {table} WHERE id=?',(int(key[len(prefix):]),)).fetchone()
            if r: return title+': '+r[0]
    return key

def editor_button(key):
    kw={'text':editor_label(key),'callback_data':'admin:key:'+key}
    icon=get('emoji:'+key)
    if icon.isdigit(): kw['icon_custom_emoji_id']=icon
    return InlineKeyboardButton(**kw)

def support_url():
    raw=os.getenv('SUPPORT_URL','').strip()
    if raw.startswith('@'): raw='https://t.me/'+raw[1:]
    elif raw.startswith('t.me/'): raw='https://'+raw
    if re.fullmatch(r'https://t\.me/[A-Za-z][A-Za-z0-9_]{4,31}/?',raw): return raw.rstrip('/')
    return ''

def reviews_url():
    raw=os.getenv('REVIEWS_URL','').strip()
    if raw.startswith('@'): raw='https://t.me/'+raw[1:]
    elif raw.startswith('t.me/'): raw='https://'+raw
    if re.fullmatch(r'https://t\.me/(?:[A-Za-z][A-Za-z0-9_]{4,31}|\+[A-Za-z0-9_-]+|joinchat/[A-Za-z0-9_-]+)/?',raw): return raw.rstrip('/')
    return ''

BUTTON_LABELS['reviews']='Отзывы'
BUTTON_LABELS['stars']='Категория Telegram Stars'
def stars_label():
    return get('label:stars') or ('Telegram Stars' if get('emoji:stars') else '⭐ Telegram Stars')

def button(label,key,red=False):
    family=key.rsplit(':',1)[0]+':*' if ':' in key else key
    if family not in BUTTON_LABELS: family=key
    kw={'text':get('label:'+key,get('label:'+family,label)) or label,'callback_data':key}
    if key=='support:write' and support_url():
        kw.pop('callback_data'); kw['url']=support_url()
    if key=='reviews' and reviews_url():
        kw.pop('callback_data'); kw['url']=reviews_url()
    style=get('style:'+key,get('style:'+family,'danger' if red else ''))
    if style in ('danger','primary','success'): kw['style']=style
    icon=get('emoji:'+key,get('emoji:'+family))
    if icon.isdigit():
        if 'icon_custom_emoji_id' not in InlineKeyboardButton.model_fields: raise RuntimeError('Обновите aiogram: отсутствует поддержка Premium Emoji')
        kw['icon_custom_emoji_id']=icon
    return InlineKeyboardButton(**kw)
def kb(*rows): return InlineKeyboardMarkup(inline_keyboard=[list(r) for r in rows if r])
def category_rows(buttons):
    return [buttons[i:i+2] for i in range(0,len(buttons),2)]
def product_description(p):
    return get('desc_html:p:'+str(p['id'])) or esc(p['description'])
def named_title(key,name):
    rich=get('name_html:'+key)
    if rich: return rich
    icon=get('emoji:'+key)
    return (f'<tg-emoji emoji-id="{icon}">🔹</tg-emoji> ' if icon.isdigit() else '')+esc(name)
def back(): return button('‹ Главное меню','home')
def esc(s): return html.escape(str(s))
SECTIONS={'home':'Главное меню','catalog':'Каталог','product':'Все товары','favorites':'Избранное','profile':'Профиль','cart':'Корзина','pay':'Оплата','orders':'Заказы','rewards':'Бонусы','levels':'Уровни','achievements':'Достижения','support':'Поддержка','admin':'Control','admin:studio':'Studio','admin:rewards':'Настройки бонусов'}
SECTIONS.update({'pay:bank':'Перевод по карте','pay:crypto':'CryptoBot','pay:rocket':'xRocket','pay:bankinvoice':'Реквизиты карты','pay:cryptoinvoice':'Счёт CryptoBot','pay:rocketinvoice':'Счёт xRocket','pay:success':'Успешная оплата','pay:history':'История пополнений'})
def design(key, text, uid, username=None, extra=None):
    u=user(uid); base='product' if key.startswith('p:') else key
    template=get('text:'+key) or get('text:'+base)
    values={'default':text,'balance':money(u['balance']),'points':points(u['points']),'level':esc(level(u['spent'])[0]),'spent':money(u['spent']),'user_id':str(uid),'username':esc('@'+username) if username else 'Не указан'}
    if key.startswith('p:'):
        p=db.execute('SELECT * FROM products WHERE id=?',(int(key[2:]),)).fetchone()
        if p: values.update(name=named_title(key,p['name']),description=product_description(p),price=money(p['price']),stock=str(available(p['id'])))
    values.update(extra or {})
    if template:
        import re
        text=re.sub(r'\{([a-z_]+)\}',lambda m:values.get(m[1],m[0]),template)
    raw=get('banner:'+key) or get('banner:'+base)
    return text,json.loads(raw) if raw else None
async def render(message,text,markup,banner=None,replace=False):
    sent=False
    if banner and len(text)<=1024:
        try:
            kind,file_id=banner
            if kind=='photo': await message.answer_photo(file_id,caption=text,reply_markup=markup)
            else: await message.answer_animation(file_id,caption=text,reply_markup=markup)
            sent=True
        except TelegramBadRequest as e:
            logging.warning('Баннер недоступен: %s',e)
    if not sent:
        if replace and message.text:
            try: await message.edit_text(text,reply_markup=markup); return
            except TelegramBadRequest as e:
                if 'message is not modified' in str(e): return
        await message.answer(text,reply_markup=markup)
    if replace:
        try: await message.delete()
        except Exception: pass
async def screen(c,text,markup,key=None):
    text,banner=design(key or c.data,text,c.from_user.id,c.from_user.username)
    await render(c.message,text,markup,banner,True)
async def home(uid):
    u=user(uid); rows=[[button('Каталог товаров','catalog',True)],[button('Личный кабинет','profile'),button('Пополнить баланс','pay')],[button('Корзина','cart'),button('Мои заказы','orders')],[button('Tripple Rewards','rewards',True)],[button('Избранное','favorites'),button('Поддержка','support')]]
    if reviews_url(): rows.append([button('Отзывы','reviews')])
    if uid in ADMIN: rows.append([button('Tripple Control','admin',True)])
    return '<b>♦ TRIPPLE SHOP</b>\n\nЦифровые товары. Быстро и удобно.'+f'\n\nБаланс: <b>{money(u["balance"])}</b>\nБонусы: {points(u["points"])}\nУровень: {level(u["spent"])[0]}',kb(*rows)
@dp.message(CommandStart())
async def start(m,state:FSMContext):
    await state.clear(); t,k=await home(m.from_user.id); t,b=design('home',t,m.from_user.id,m.from_user.username); await render(m,t,k,b)
@dp.callback_query()
async def click(c,state:FSMContext):
    uid=c.from_user.id; u=user(uid); a=c.data; await c.answer()
    if a=='admin:publishprod':
        if uid not in ADMIN: return
        d=await state.get_data()
        if d.get('key')!='draft:confirm': return
        cid=d['cid']
        if cid and not db.execute('SELECT 1 FROM categories WHERE id=?',(cid,)).fetchone(): await state.clear(); await c.message.answer('Категория удалена'); return
        with db:
            pid=next_id('products'); db.execute('INSERT INTO products(id,name,price,description,category) VALUES (?,?,?,?,?)',(pid,d['name'],d['price'],d['desc'],cid))
            for prefix,value in [('name_html:',d['name_html']),('desc_html:',d['desc_html']),('emoji:',d['icon']),('banner:',d['media'])]:
                db.execute('INSERT OR REPLACE INTO settings VALUES (?,?)',(prefix+'p:'+str(pid),value))
        await state.clear(); await c.message.answer('Товар опубликован',reply_markup=kb([button('Управление товаром','admin:item:'+str(pid))])); return
    await state.clear()
    if a.startswith(('admin','edit:')) and uid not in ADMIN: return
    if a=='stars':
        t='<b>'+esc(stars_label())+'</b>\n\nЦена: <b>1,40 ₽ / звезда</b>\nМинимум: 50 звёзд\nКоличество и получатель — на ваш выбор.'; k=kb([button('Выбрать количество','stars:qty',True)],[button('‹ Каталог','catalog')]); await screen(c,t,k); return
    if a=='stars:qty':
        await state.set_state(Edit.value); await state.update_data(key='stars_qty'); await screen(c,'Введите количество звёзд (от 50 до 1000000).',kb([button('Отмена','stars')])); return
    if a.startswith('stars:buy:'):
        # Telegram digital goods must use Stars invoices, not RUB balance.
        await screen(c,'Заказ сохранён как заявка. Оплата в рублях внутри Telegram для цифровых товаров не подключена.',kb([button('Каталог','catalog')])); return
    if a=='admin:stars':
        rows=db.execute("SELECT * FROM stars_requests WHERE status='pending' ORDER BY id DESC LIMIT 30").fetchall()
        await screen(c,'<b>Заявки Telegram Stars (не оплачены)</b>',kb(*[[button(f'#{r["id"]} · {r["qty"]} ⭐ · {r["recipient"]}',f'admin:starview:{r["id"]}')] for r in rows],[button('Назад','admin')])); return
    if a.startswith('admin:starview:'):
        r=db.execute('SELECT * FROM stars_requests WHERE id=?',(int(a.rsplit(':',1)[1]),)).fetchone()
        if r: await screen(c,f'<b>Заявка #{r["id"]}</b>\nПолучатель: {esc(r["recipient"])}\nКоличество: {r["qty"]}\nСтоимость: {money(r["total"])}\nНе оплачено',kb([button('Назад','admin:stars')]))
        return
    if a=='home': t,k=await home(uid)
    elif a=='catalog' or a.startswith('cat:') or a.startswith('sort:'):
        cid=int(a.split(':')[-1]) if a!='catalog' else 0
        order='price,id' if a.startswith('sort:') else 'id'
        rows=db.execute(f'SELECT * FROM products WHERE category=? ORDER BY {order} LIMIT 40',(cid,)).fetchall()
        cats=db.execute('SELECT * FROM categories WHERE parent=? ORDER BY id LIMIT 30',(cid,)).fetchall()
        cat=db.execute('SELECT * FROM categories WHERE id=?',(cid,)).fetchone() if cid else None
        t='<b>'+ (named_title(f'cat:{cid}',cat['name']) if cat else '♦ TRIPPLE CATALOG')+'</b>\n\nВыберите категорию или товар.'
        k=kb(*([[button(stars_label(),'stars',True)]] if not cid else []),*category_rows([button(x['name'],f'cat:{x["id"]}') for x in cats]),*[[button(f'{p["name"]} · {money(p["price"])}',f'p:{p["id"]}')] for p in rows],[button('Поиск товаров','search'),button('Сначала дешевле',f'sort:{cid}') ],[button('‹ Каталог','catalog')] if cid else [back()])
    elif a=='search' or a=='support:write':
        await state.set_state(Edit.value); await state.update_data(key=a); t='Введите название товара' if a=='search' else 'Напишите сообщение поддержке'; k=kb([back()])
    elif a=='favorites':
        rows=db.execute('SELECT p.* FROM favorites f JOIN products p ON p.id=f.product WHERE f.uid=?',(uid,)).fetchall(); t='<b>Избранное</b>'; k=kb(*[[button(p['name'],f'p:{p["id"]}')] for p in rows],[back()])
    elif a.startswith('fav:'):
        pid=int(a[4:]);
        if not db.execute('SELECT 1 FROM products WHERE id=?',(pid,)).fetchone(): return
        with db:
            if db.execute('SELECT 1 FROM favorites WHERE uid=? AND product=?',(uid,pid)).fetchone(): db.execute('DELETE FROM favorites WHERE uid=? AND product=?',(uid,pid)); t='Удалено из избранного'
            else: db.execute('INSERT INTO favorites VALUES (?,?)',(uid,pid)); t='Добавлено в избранное'
        k=kb([button('К товару',f'p:{pid}')],[button('Избранное','favorites')])
    elif a.startswith('p:'):
        pid=int(a.split(':')[1]); p=db.execute('SELECT * FROM products WHERE id=?',(pid,)).fetchone()
        if not p: return
        n=available(pid); delivery='⚡ Автоматическая выдача' if p['manual_qty'] is None else '⚡ Ручная выдача'; heading=named_title('p:'+str(pid),p['name']); t=f'<b>{heading}</b>\n\n{product_description(p)}\n\nЦена: {money(p["price"])}\nВ наличии: {n}\n{delivery}'; k=kb(*([[button('В корзину',f'add:{pid}',True)]] if n else []),[button('В избранное / убрать',f'fav:{pid}')],[button('‹ Каталог','catalog')])
    elif a.startswith(('add:','minus:','remove:')):
        op,pid=a.split(':'); pid=int(pid)
        if op=='add' and not available(pid): return
        with db:
            if op=='add': db.execute('INSERT INTO cart VALUES (?,?,1) ON CONFLICT(uid,product) DO UPDATE SET qty=MIN(qty+1,?)',(uid,pid,available(pid)))
            elif op=='minus': db.execute('UPDATE cart SET qty=qty-1 WHERE uid=? AND product=?',(uid,pid))
            else: db.execute('DELETE FROM cart WHERE uid=? AND product=?',(uid,pid))
            db.execute('DELETE FROM cart WHERE qty<=0')
        a='cart'
    if a=='cart':
        rows=db.execute('SELECT p.*,c.qty FROM cart c JOIN products p ON p.id=c.product WHERE c.uid=?',(uid,)).fetchall(); t='<b>♦ Корзина</b>'; keys=[]
        for p in rows:
            t+=f'\n{esc(p["name"])} × {p["qty"]} — {money(cost(p))}'; pid=p['id']; keys.append([button('−',f'minus:{pid}'),button('+',f'add:{pid}'),button('Удалить',f'remove:{pid}')])
        if rows: keys.extend([[button('Оплатить с баланса','checkout',True)],[button('Оплатить с бонусами','checkout:points')]])
        else: t+='\n\nКорзина пуста.'
        k=kb(*keys,[back()])
    elif a in ('checkout','checkout:points'):
        try:
            with db:
                rows=db.execute('SELECT p.*,c.qty FROM cart c JOIN products p ON p.id=c.product WHERE c.uid=?',(uid,)).fetchall(); total=sum(cost(p) for p in rows)
                if not rows: raise ValueError('Корзина пуста')
                u=user(uid); used=min(u['points'],total*int(get('points_limit','20'))//100) if a=='checkout:points' else 0; cash=total-used
                if u['balance']<cash: raise InsufficientBalance(cash-u['balance'])
                pending=[]
                for p in rows:
                    if p['manual_qty'] is not None:
                        if not db.execute('UPDATE products SET manual_qty=manual_qty-? WHERE id=? AND manual_qty>=?',(p['qty'],p['id'],p['qty'])).rowcount: raise ValueError('Недостаточно товара в наличии')
                        oid=db.execute('INSERT INTO orders(uid,product,total,content,pending_qty) VALUES (?,?,?,?,?)',(uid,p['id'],cost(p),'Ожидает ручной выдачи',p['qty'])).lastrowid
                        pending.append((oid,p['name'],p['qty']))
                        continue
                    items=db.execute('SELECT * FROM stock WHERE product=? LIMIT ?',(p['id'],p['qty'])).fetchall()
                    if len(items)!=p['qty']: raise ValueError('Недостаточно товара на складе')
                    db.execute('INSERT INTO orders(uid,product,total,content) VALUES (?,?,?,?)',(uid,p['id'],cost(p),'\n'.join(i['content'] for i in items)))
                    db.executemany('DELETE FROM stock WHERE id=?',[(i['id'],) for i in items])
                db.execute('UPDATE users SET balance=balance-?,spent=spent+?,points=points-?+? WHERE id=?',(cash,cash,used,cash*level(u['spent'])[2]//100,uid)); reward(uid); db.execute('DELETE FROM cart WHERE uid=?',(uid,))
            t='Оплата успешна. Товары доступны в разделе «Мои заказы».'
            if pending: t+='\nТовары с ручной выдачей ожидают администратора.'
            for oid,name,qty in pending:
                for admin in ADMIN:
                    try: await c.bot.send_message(admin,f'Заказ #{oid}: {esc(name)} × {qty}',reply_markup=kb([button('Выдать товар',f'edit:fulfill:{oid}')]))
                    except Exception: logging.exception('Не удалось уведомить администратора')
        except InsufficientBalance as e:
            missing=int(e.args[0]); suggested=max(missing,BANK_MIN_AMOUNT*100)
            t='<b>♦ TRIPPLE PAY</b>\n\nВыберите способ пополнения.'
            t,b=design('pay',t,uid,c.from_user.username)
            t+=f'\n\nНе хватает: <b>{money(missing)}</b>\nРекомендуем пополнить: <b>{money(suggested)}</b>'
            k=kb([button('Перевод по карте','pay:bank',True)],[button('CryptoBot','pay:crypto'),button('xRocket','pay:rocket')],[button('История пополнений','pay:history')],[button('Назад в корзину','cart')])
            await render(c.message,t,k,b,replace=True); return
        except ValueError as e: t=esc(e)
        k=kb([button('Мои заказы','orders')],[button('Назад в корзину','cart')],[back()])
    elif a=='orders':
        rows=db.execute('SELECT id,total FROM orders WHERE uid=? ORDER BY id DESC LIMIT 20',(uid,)).fetchall(); t='<b>♦ Мои заказы</b>'; k=kb(*[[button(f'Заказ #{r[0]} · {money(r[1])}',f'o:{r[0]}')] for r in rows],[back()])
    elif a.startswith('o:'):
        r=db.execute('SELECT content FROM orders WHERE uid=? AND id=?',(uid,int(a[2:]))).fetchone()
        if not r:return
        for i in range(0,len(r[0]),1000): await c.message.answer('<pre>'+esc(r[0][i:i+1000])+'</pre>')
        return
    elif a=='profile':
        username=esc('@'+c.from_user.username) if c.from_user.username else 'Не указан'
        t=(f'<b>♦ TRIPPLE PROFILE</b>\n\n'
           f'ID пользователя: <code>{uid}</code>\nЮзернейм: {username}\n\n'
           f'Баланс: <b>{money(u["balance"])}</b>\nВсего потрачено: <b>{money(u["spent"])}</b>\n'
           f'Бонусы: {points(u["points"])}\nУровень: {esc(level(u["spent"])[0])}')
        k=kb([button('Пополнить баланс','pay',True)],[button('Мои заказы','orders'),button('Избранное','favorites')],[button('Tripple Rewards','rewards'),button('Поддержка','support')],[back()])
    elif a=='pay' or a.startswith(('pay:', 'admin:payments','admin:approve:','admin:reject:')):
        await payment_click(c,state); return
    elif a=='rewards':
        name,threshold,rate=level(u['spent']); nxt=next((x for x in levels() if x[1]>u['spent']),None)
        t=f'<b>♦ TRIPPLE REWARDS</b>\n\nУровень: <b>{name}</b>\nКешбэк: {rate}%\nБаллы: {points(u["points"])}\n1 TP = 1 ₽ скидки'
        if nxt: t+=f'\nДо {nxt[0]}: {money(nxt[1]-u["spent"])}'
        k=kb([button('Достижения','achievements'),button('Все уровни','levels')],[back()])
    elif a=='levels':
        t='<b>♦ Уровни</b>\n\n'+'\n'.join(f'{n} — от {money(th)}, кешбэк {r}%' for n,th,r in levels()); k=kb([button('Назад','rewards')])
    elif a=='achievements':
        done={r[0] for r in db.execute('SELECT code FROM achievements WHERE uid=?',(uid,))}; t='<b>♦ Достижения</b>\n\n'+'\n'.join(('✓ ' if code in done else '○ ')+label for code,label in [('first','Первый заказ'),('ten','10 заказов'),('big','Покупки на 5 000 ₽')]); k=kb([button('Назад','rewards')])
    elif a=='support': t=esc(get('support','Есть вопрос? Нажмите кнопку ниже, чтобы связаться с поддержкой.')); k=kb([button('Написать','support:write',True)],[back()])
    elif a=='admin': t='<b>♦ TRIPPLE CONTROL</b>'; k=kb([button('Редактор интерфейса','admin:studio',True)],[button('Добавить товар','admin:cats:0'),button('Загрузить склад','edit:stock')],[button('Управление каталогом','admin:cats:0',True)],[button('Оптовая цена','edit:bulk'),button('Начислить баланс','edit:balance')],[button('Рассылка','edit:broadcast'),button('Поддержка','edit:support')],[button('Настройки бонусов','admin:rewards',True)],[button('Заявки Stars','admin:stars',True)],[button('Платежи','admin:payments',True)],[button('Ручные заказы','admin:pending',True)],[button('Пользователи','admin:users:0',True)],[back()])
    elif a.startswith(('admin:users:','admin:banned:')):
        mode=a.split(':')[1]; page=max(0,int(a.split(':')[-1])); condition='WHERE b.uid IS NOT NULL' if mode=='banned' else ''
        rows=db.execute('SELECT u.id,n.name,b.uid AS banned FROM users u LEFT JOIN user_names n ON n.uid=u.id LEFT JOIN blocked b ON b.uid=u.id '+condition+' ORDER BY u.id LIMIT 21 OFFSET ?',(page*20,)).fetchall()
        t='<b>Пользователи магазина</b>' if mode=='users' else '<b>Заблокированные</b>'
        nav=[]
        if page: nav.append(button('‹','admin:'+mode+':'+str(page-1)))
        if len(rows)>20: nav.append(button('›','admin:'+mode+':'+str(page+1)))
        k=kb(*category_rows([button(('⛔ ' if r['banned'] else '')+(r['name'] or str(r['id']))[:45],'admin:person:'+str(r['id'])) for r in rows[:20]]),nav,[button('Поиск','edit:finduser'),button('Заблокированные','admin:banned:0')],[button('Все пользователи','admin:users:0'),button('Назад','admin')])
    elif a.startswith(('admin:person:','admin:ban:','admin:unban:')):
        target=int(a.split(':')[-1]); op=a.split(':')[1]
        if not db.execute('SELECT 1 FROM users WHERE id=?',(target,)).fetchone(): return
        note=''
        if op=='ban' and target not in ADMIN:
            with db: db.execute('INSERT OR IGNORE INTO blocked VALUES (?)',(target,))
            await dp.fsm.get_context(bot=c.bot,chat_id=target,user_id=target).clear()
            n=await purge_chat(c.bot,target); note=f'\nЗапрос на очистку отправлен для {n} сообщений.'
        elif op=='unban':
            with db: db.execute('DELETE FROM blocked WHERE uid=?',(target,))
        r=user(target); name=db.execute('SELECT name FROM user_names WHERE uid=?',(target,)).fetchone()
        t=f'<b>Пользователь</b>\n\n{esc(name[0] if name else "Без имени")}\nID: <code>{target}</code>\nБаланс: {money(r["balance"])}\nПотрачено: {money(r["spent"])}\nСтатус: '+('Заблокирован' if blocked(target) else 'Активен')+note
        k=kb(*([[button('Разблокировать','admin:unban:'+str(target))]] if blocked(target) else [[button('Заблокировать','admin:ban:'+str(target),True)]] if target not in ADMIN else []),[button('Назад','admin:users:0')])
    elif a.startswith(('admin:del:','admin:confirmdel:')):
        _,op,kind,raw=a.split(':'); ident=int(raw)
        table='categories' if kind=='cat' else 'products'; r=db.execute(f'SELECT * FROM {table} WHERE id=?',(ident,)).fetchone()
        if not r: t='Уже удалено.'; k=kb([button('Каталог','admin:cats:0')])
        elif op=='del':
            t='Удалить <b>'+esc(r['name'])+'</b>? '+('Все вложенные категории, товары и остатки будут удалены.' if kind=='cat' else 'Остатки товара будут удалены.')+' История заказов сохранится.'
            k=kb([button('Да, удалить','admin:confirmdel:'+kind+':'+raw,True)],[button('Отмена',('admin:cats:' if kind=='cat' else 'admin:item:')+raw)])
        else:
            parent=r['parent'] if kind=='cat' else r['category']; nc,np=delete_catalog(kind,ident); t=f'Удалено категорий: {nc}. Товаров: {np}.'; k=kb([button('Назад','admin:cats:'+str(parent))])
    elif a.startswith('admin:cats:'):
        cid=int(a.rsplit(':',1)[1]); cat=db.execute('SELECT * FROM categories WHERE id=?',(cid,)).fetchone() if cid else None
        if cid and not cat: return
        t='<b>Управление каталогом</b>\n\n'+(named_title(f'cat:{cid}',cat['name']) if cat else 'Все категории')
        cats=db.execute('SELECT * FROM categories WHERE parent=? ORDER BY id',(cid,)).fetchall()
        products=db.execute('SELECT * FROM products WHERE category=? ORDER BY id',(cid,)).fetchall()
        k=kb(*([[button('Настроить '+stars_label(),'admin:key:stars')]] if not cid else []),*category_rows([button('📁 '+x['name'],f'admin:cats:{x["id"]}') for x in cats]),*[[button('📦 '+x['name'],f'admin:item:{x["id"]}')] for x in products],[button('＋ Категория','edit:newcat:'+str(cid),True),button('＋ Товар','edit:newprod:'+str(cid),True)],*([[button('Название','edit:renamecat:'+str(cid)),button('Premium Emoji','edit:emoji:cat:'+str(cid))],[button('Оформление','admin:design:cat:'+str(cid))],[button('Удалить категорию','admin:del:cat:'+str(cid),True)]] if cid else []),[button('Назад','admin:cats:'+str(cat['parent']) if cat else 'admin')])
    elif a.startswith('admin:item:'):
        pid=int(a.rsplit(':',1)[1]); p=db.execute('SELECT * FROM products WHERE id=?',(pid,)).fetchone()
        if not p: return
        t='<b>'+named_title(f'p:{pid}',p['name'])+'</b>\nЦена: '+money(p['price'])+'\nВ наличии: '+str(available(pid))
        k=kb([button('Название','edit:renameprod:'+str(pid)),button('Premium Emoji','edit:emoji:p:'+str(pid))],[button('Количество в наличии','edit:quantity:'+str(pid),True)],[button('Загрузить склад','edit:stockfor:'+str(pid))],[button('Описание','edit:proddesc:'+str(pid)),button('Цена ₽','edit:prodprice:'+str(pid))],[button('Фото / GIF','edit:prodmedia:'+str(pid)),button('Оформление','admin:design:p:'+str(pid))],[button('Удалить товар','admin:del:prod:'+str(pid),True)],[button('Назад','admin:cats:'+str(p['category']))])
    elif a=='admin:pending':
        rows=db.execute('SELECT id,uid,pending_qty FROM orders WHERE pending_qty>0 ORDER BY id LIMIT 50').fetchall()
        t='<b>Заказы на выдачу</b>'
        k=kb(*[[button(f'Заказ #{r[0]} · {r[2]} шт.',f'edit:fulfill:{r[0]}')] for r in rows],[button('Назад','admin')])
    elif a=='admin:rewards':
        t='<b>♦ Настройки Tripple Rewards</b>'; k=kb(*[[button(n+' · '+str(r)+'%', 'edit:rate:'+n)] for n,th,r in levels()],[button('Лимит оплаты баллами','edit:points_limit')],*[ [button('Награда '+code,'edit:bonus:'+code)] for code in ('first','ten','big')],[button('Назад','admin')])
    elif a=='admin:studio':
        t='<b>♦ TRIPPLE STUDIO</b>\n\nВыберите раздел оформления.'; k=kb(*[[button(label,'admin:design:'+key)] for key,label in SECTIONS.items()],[button('Отдельные товары','admin:designproducts')],[button('Редактор кнопок','admin:buttons')],[button('Назад','admin')])
    elif a=='admin:designproducts':
        t='<b>Оформление товаров</b>'; k=kb(*[[button(p['name'],'admin:design:p:'+str(p['id']))] for p in db.execute('SELECT id,name FROM products')],[button('Назад','admin:studio')])
    elif a.startswith('admin:design:'):
        key=a[len('admin:design:'):]; t='<b>Оформление: '+esc(SECTIONS.get(key,key))+'</b>\n\nБаннер: '+('установлен' if get('banner:'+key) else 'не установлен')+'\nТекст: '+('свой' if get('text:'+key) else 'стандартный'); k=kb([button('Установить фото / GIF','edit:banner:'+key,True)],[button('Изменить текст','edit:text:'+key)],[button('Удалить баннер','admin:reset:banner:'+key),button('Сбросить текст','admin:reset:text:'+key)],[button('Открыть раздел', 'admin:preview:'+key)],[button('Назад','admin:studio')])
    elif a.startswith('admin:reset:'):
        key=a[len('admin:reset:'):]; put(key,''); t='Оформление сброшено.'; k=kb([button('Назад','admin:design:'+key.split(':',1)[1])])
    elif a.startswith('admin:preview:'):
        key=a[len('admin:preview:'):]; t,b=design(key,'<b>Предпросмотр</b>\nСтандартный текст раздела',uid,c.from_user.username); await render(c.message,t,kb([button('Назад','admin:design:'+key)]),b,True); return
    elif a=='admin:buttons':
        t='<b>Редактор кнопок</b>\n\nВыберите кнопку для изменения названия, цвета или Premium Emoji.'
        keys=list(BUTTON_LABELS)
        rows=[[editor_button(x) for x in keys[i:i+2]] for i in range(0,len(keys),2)]
        rows += [[editor_button('cat:'+str(x[0]))] for x in db.execute('SELECT id FROM categories')]
        rows += [[editor_button('p:'+str(x[0]))] for x in db.execute('SELECT id FROM products')]
        k=kb(*rows,[button('Назад','admin:studio')])
    elif a.startswith('admin:key:'):
        key=a[len('admin:key:'):]; t='Настройка: '+esc(stars_label() if key=='stars' else editor_label(key))+'\nPremium Emoji ID: '+esc(get('emoji:'+key) or 'не задан'); k=kb(*[[button(label,'edit:'+field+':'+key)] for label,field in [('Название','label'),('Premium Emoji','emoji'),('Цвет','style')]],[button('Назад','admin:buttons')])
    elif a.startswith('admin:reply:'):
        await state.set_state(Edit.value); await state.update_data(key='reply:'+a.split(':')[-1]); t='Введите ответ пользователю'; k=kb([button('Отмена','admin')])
    elif a.startswith('edit:'):
        key=a[5:]; await state.set_state(Edit.value); await state.update_data(key=key); t={'newcat':'Отправьте название категории. Можно вставить Premium Emoji прямо в сообщение.', 'renamecat':'Отправьте новое название категории с Premium Emoji.', 'renameprod':'Отправьте новое название товара с Premium Emoji.', 'newprod':'Шаг 1/4. Отправьте название товара с Premium Emoji.', 'proddesc':'Отправьте новое описание с Premium Emoji.', 'prodprice':'Введите цену в рублях, например 350 или 350,50.', 'prodmedia':'Отправьте фото или GIF. Для удаления отправьте 0.', 'quantity':'Введите количество (0–1000000). Для возврата к автоматическому складу отправьте авто.', 'fulfill':'Отправьте данные товара для покупателя.', 'stockfor':'Отправьте по одной единице товара на строку.', 'category':'Название | ID родительской категории (0 — корень)','assign':'ID товара | ID категории (0 — корень)','bulk':'ID товара | минимальное количество | цена за штуку в рублях','broadcast':'Отправьте текст, фото или GIF для рассылки','product':'Название | цена в рублях | описание','stock':'ID товара, затем с новой строки по одной единице товара','balance':'ID пользователя | сумма в рублях','style':'danger / primary / success / default','banner':'Отправьте фотографию или GIF (как анимацию).','text':'Отправьте новый текст с форматированием и Premium Emoji. Переменные: {default}, {balance}, {points}, {level}, {spent}, {user_id}, {username}; для платежа: {amount}, {due}, {card}, {payment_id}; для товара: {name}, {description}, {price}, {stock}.','emoji':'Отправьте один Premium Emoji или его ID. 0 — сброс.'}.get(key.split(':')[0],'Отправьте новое значение.'); k=kb([button('Отмена','admin')])
    elif not a.startswith(('p:','add:','minus:','remove:')) and a not in ('home','catalog','search','support:write','favorites') and not a.startswith(('cat:','sort:','fav:')): return
    await screen(c,t,k,a)
def rub_price(raw):
    raw=raw.strip().replace(',', '.')
    if not re.fullmatch(r"[0-9]{1,9}(?:\.[0-9]{1,2})?",raw): raise ValueError('Введите цену в рублях, например 350,50')
    value=int(Decimal(raw)*100)
    if value<=0: raise ValueError('Цена должна быть больше нуля')
    return value

def draft_name(m):
    if not m.text or not m.text.strip() or len(m.text)>100: raise ValueError('Название: от 1 до 100 символов')
    entities=[e for e in (m.entities or []) if e.type=='custom_emoji']
    data=m.text.encode('utf-16-le')
    for e in sorted(entities,key=lambda e:e.offset,reverse=True): data=data[:e.offset*2]+data[(e.offset+e.length)*2:]
    return data.decode('utf-16-le').strip() or m.text.strip(),m.html_text,entities[0].custom_emoji_id if entities else ''

def draft_media(m):
    if m.photo: return json.dumps(['photo',m.photo[-1].file_id])
    if m.animation: return json.dumps(['animation',m.animation.file_id])
    if (m.text or '').strip()=='0': return ''
    raise ValueError('Отправьте фото, GIF или 0')

async def product_editor(m,state,key):
    if m.from_user.id not in ADMIN: return
    op,raw=key.split(':',1)
    if op in ('proddesc','prodprice','prodmedia'):
        pid=int(raw); target='p:'+raw
        if not db.execute('SELECT 1 FROM products WHERE id=?',(pid,)).fetchone(): raise ValueError('Товар удалён')
        with db:
            if op=='proddesc':
                if not m.text or len(m.text)>3000: raise ValueError('Описание: до 3000 символов')
                db.execute('UPDATE products SET description=? WHERE id=?',(m.text,pid)); db.execute('INSERT OR REPLACE INTO settings VALUES (?,?)',('desc_html:'+target,m.html_text))
            elif op=='prodprice': db.execute('UPDATE products SET price=? WHERE id=?',(rub_price(m.text or ''),pid))
            else: db.execute('INSERT OR REPLACE INTO settings VALUES (?,?)',('banner:'+target,draft_media(m)))
        await state.clear(); await m.answer('Товар обновлён',reply_markup=kb([button('К товару','admin:item:'+raw)])); return
    if op=='newprod':
        name,rich,icon=draft_name(m)
        await state.update_data(key='draft:desc',cid=int(raw),name=name,name_html=rich,icon=icon)
        prompt='Шаг 2/4. Отправьте описание с Premium Emoji и форматированием.'
    elif raw=='desc':
        if not m.text or len(m.text)>3000: raise ValueError('Описание: до 3000 символов')
        await state.update_data(key='draft:media',desc=m.text,desc_html=m.html_text)
        prompt='Шаг 3/4. Отправьте фото или GIF. Без фото — отправьте 0.'
    elif raw=='media':
        await state.update_data(key='draft:price',media=draft_media(m))
        prompt='Шаг 4/4. Введите цену в рублях, например 350,50.'
    elif raw=='price':
        price=rub_price(m.text or ''); d=await state.get_data()
        await state.update_data(key='draft:confirm',price=price)
        await render(m,'<b>'+d['name_html']+'</b>\n\n'+d['desc_html']+'\n\nЦена: '+money(price),kb([button('Опубликовать','admin:publishprod',True)],[button('Отмена','admin:cats:'+str(d['cid']))]),json.loads(d['media']) if d['media'] else None)
        return
    else: raise ValueError('Нажмите Опубликовать или Отмена')
    await m.answer(prompt,reply_markup=kb([button('Отмена','admin')]))

@dp.message(Edit.value)
async def edit(m,state:FSMContext):
    key=(await state.get_data())['key']; s=m.text or ''
    if key=='stars_qty':
        if not re.fullmatch(r'[0-9]{1,7}',s.strip()) or not 50<=int(s)<=1000000:
            await m.answer('Введите целое количество от 50 до 1000000.'); return
        await state.update_data(key='stars_recipient',qty=int(s)); await m.answer('Введите @username получателя.'); return
    if key=='stars_recipient':
        recipient=s.strip()
        if not re.fullmatch(r'@[A-Za-z0-9_]{5,32}',recipient): await m.answer('Введите корректный @username.'); return
        qty=(await state.get_data())['qty']; total=qty*140
        with db: rid=db.execute('INSERT INTO stars_requests(uid,qty,recipient,total) VALUES (?,?,?,?)',(m.from_user.id,qty,recipient,total)).lastrowid
        await state.clear()
        await m.answer(f'<b>Заявка #{rid}</b>\n{qty} ⭐ → {esc(recipient)}\nСтоимость: {money(total)}\nСтатус: не оплачено',reply_markup=kb([button('Каталог','catalog')]))
        for admin in ADMIN:
            try: await m.bot.send_message(admin,f'Новая неоплаченная заявка Stars #{rid}',reply_markup=kb([button('Открыть',f'admin:starview:{rid}')]))
            except Exception: logging.exception('Ошибка уведомления Stars')
        return
    if key.startswith('topup:'):
        await payment_amount(m,state,key.split(':')[1]); return
    if key=='search':
        rows=db.execute('SELECT * FROM products WHERE name LIKE ? LIMIT 40',('%'+s+'%',)).fetchall(); await state.clear(); await m.answer('Результаты поиска' if rows else 'Ничего не найдено',reply_markup=kb(*[[button(p['name'],f'p:{p["id"]}')] for p in rows],[button('Каталог','catalog')])); return
    if key=='support:write':
        if not s: await m.answer('Отправьте текст'); return
        with db: db.execute('INSERT INTO tickets(uid,body) VALUES (?,?)',(m.from_user.id,s))
        delivered=0
        for admin in ADMIN:
            try: await m.bot.send_message(admin,f'Поддержка · {m.from_user.id}\n'+esc(s),reply_markup=kb([button('Ответить',f'admin:reply:{m.from_user.id}')])); delivered+=1
            except Exception: logging.exception('Ошибка доставки обращения')
        await state.clear(); await m.answer('Сообщение отправлено' if delivered else 'Обращение сохранено, администратор пока недоступен',reply_markup=kb([back()])); return
    if m.from_user.id not in ADMIN: return
    try:
        if key.startswith('quantity:'):
            pid=int(key.split(':')[1]); value=None if s.strip().lower()=='авто' else int(s.strip())
            if value is not None and not 0<=value<=1000000: raise ValueError('Недопустимое количество')
            with db:
                if not db.execute('UPDATE products SET manual_qty=? WHERE id=?',(value,pid)).rowcount: raise ValueError('Товар не найден')
            await state.clear(); await m.answer('Количество сохранено: '+str(available(pid)),reply_markup=kb([button('К товару',f'admin:item:{pid}')])) ; return
        if key.startswith('fulfill:'):
            oid=int(key.split(':')[1]); r=db.execute('SELECT uid FROM orders WHERE id=? AND pending_qty>0',(oid,)).fetchone()
            if not r or not m.text or not s.strip(): raise ValueError('Заказ уже выдан или нет данных')
            with db: db.execute('UPDATE orders SET content=?,pending_qty=0 WHERE id=? AND pending_qty>0',(s,oid))
            await state.clear()
            try: await m.bot.send_message(r[0],f'Заказ #{oid} готов! Откройте «Мои заказы».',reply_markup=kb([button('Открыть заказ',f'o:{oid}')]))
            except Exception: logging.exception('Не удалось уведомить покупателя')
            await m.answer('Заказ выдан',reply_markup=kb([button('Ручные заказы','admin:pending')])) ; return
        if key=='finduser':
            rows=db.execute('SELECT u.id,n.name FROM users u LEFT JOIN user_names n ON n.uid=u.id WHERE CAST(u.id AS TEXT)=? OR n.name LIKE ? LIMIT 30',(s.strip(),'%'+s.strip()+'%')).fetchall()
            await state.clear(); await m.answer('Результаты поиска' if rows else 'Не найдено',reply_markup=kb(*category_rows([button((r['name'] or str(r['id']))[:45],'admin:person:'+str(r['id'])) for r in rows]),[button('Назад','admin:users:0')])); return
        if key.startswith(('newprod:','draft:','proddesc:','prodprice:','prodmedia:')):
            await product_editor(m,state,key); return
        if key.startswith(('newcat:','renamecat:','renameprod:','stockfor:')):
            from decimal import Decimal
            op,raw=key.split(':'); ident=int(raw)
            if op=='stockfor':
                items=[x for x in s.splitlines() if x.strip()]; assert items and db.execute('SELECT 1 FROM products WHERE id=?',(ident,)).fetchone()
                with db: db.executemany('INSERT INTO stock(product,content) VALUES (?,?)',[(ident,x) for x in items])
                dest='admin:item:'+raw; result='Склад обновлён'
            else:
                name=s.split('|',1)[0].strip() if op=='newprod' else s.strip()
                # Telegram button supports one custom emoji icon, not inline entities.
                entities=[e for e in (m.entities or []) if e.type=='custom_emoji' and e.offset+e.length<=len((s.split('|',1)[0] if op=='newprod' else s).encode('utf-16-le'))//2]
                icon=entities[0].custom_emoji_id if entities else ''
                if icon:
                    original=s.split('|',1)[0] if op=='newprod' else s
                    data=original.encode('utf-16-le')
                    for e in reversed(entities): data=data[:e.offset*2]+data[(e.offset+e.length)*2:]
                    name=data.decode('utf-16-le').strip()
                    if 'icon_custom_emoji_id' not in InlineKeyboardButton.model_fields: raise ValueError('Обновите aiogram для Premium Emoji')
                    test=await m.answer('Проверка Premium Emoji',reply_markup=kb([InlineKeyboardButton(text=name or 'Категория',callback_data='admin',icon_custom_emoji_id=icon)]))
                    if not test.reply_markup or test.reply_markup.inline_keyboard[0][0].icon_custom_emoji_id!=icon: raise ValueError('Telegram не принял Premium Emoji')
                assert name and len(name)<=100
                with db:
                    if op=='newcat':
                        assert ident==0 or db.execute('SELECT 1 FROM categories WHERE id=?',(ident,)).fetchone()
                        ident=db.execute('INSERT INTO categories(id,name,parent) VALUES (?,?,?)',(next_id('categories'),name,ident)).lastrowid
                    elif op=='renamecat': assert db.execute('UPDATE categories SET name=? WHERE id=?',(name,ident)).rowcount
                    elif op=='newprod':
                        _,price,desc=s.split('|',2); price=int(Decimal(price.strip())*100); assert price>0
                        assert ident==0 or db.execute('SELECT 1 FROM categories WHERE id=?',(ident,)).fetchone()
                        ident=db.execute('INSERT INTO products(id,name,price,description,category) VALUES (?,?,?,?,?)',(next_id('products'),name,price,desc.strip(),ident)).lastrowid
                    else: assert db.execute('UPDATE products SET name=? WHERE id=?',(name,ident)).rowcount
                    target=('cat:' if op.endswith('cat') else 'p:')+str(ident)
                    if icon: db.execute('INSERT OR REPLACE INTO settings VALUES (?,?)',('emoji:'+target,icon))
                    if op=='renameprod': db.execute('INSERT OR REPLACE INTO settings VALUES (?,?)',('name_html:'+target,m.html_text))
                    db.execute('DELETE FROM settings WHERE key=?',('label:'+target,))
                dest=('admin:cats:' if op.endswith('cat') else 'admin:item:')+str(ident); result='Сохранено'
            await state.clear(); await m.answer(result,reply_markup=kb([button('Продолжить',dest,True)])); return
        elif key=='category':
            name,parent=s.split('|'); parent=int(parent); assert name.strip() and (parent==0 or db.execute('SELECT 1 FROM categories WHERE id=?',(parent,)).fetchone())
            with db: cur=db.execute('INSERT INTO categories(id,name,parent) VALUES (?,?,?)',(next_id('categories'),name.strip(),parent))
            result=f'Категория создана. ID: {cur.lastrowid}'
        elif key=='assign':
            pid,cid=map(int,s.split('|')); assert cid==0 or db.execute('SELECT 1 FROM categories WHERE id=?',(cid,)).fetchone()
            with db: assert db.execute('UPDATE products SET category=? WHERE id=?',(cid,pid)).rowcount
            result='Категория назначена'
        elif key=='bulk':
            from decimal import Decimal
            pid,qty,price=s.split('|'); qty=int(qty); price=int(Decimal(price.strip())*100); assert qty>=0 and price>=0
            with db: assert db.execute('UPDATE products SET bulk_qty=?,bulk_price=? WHERE id=?',(qty,price,int(pid))).rowcount
            result='Оптовая цена сохранена'
        elif key.startswith('reply:'):
            if blocked(int(key[6:])): raise ValueError('Пользователь заблокирован')
            await m.bot.send_message(int(key[6:]),'<b>Ответ поддержки</b>\n'+esc(s)); result='Ответ отправлен'
        elif key=='broadcast':
            ok=fail=0
            for row in db.execute('SELECT id FROM users WHERE id NOT IN (SELECT uid FROM blocked)').fetchall():
                try: await m.copy_to(row[0]); ok+=1
                except Exception: fail+=1
                await asyncio.sleep(.06)
            result=f'Рассылка: доставлено {ok}, ошибок {fail}'
        elif key=='product':
            from decimal import Decimal
            n,p,d=s.split('|',2); price=int(Decimal(p.strip())*100); assert price>0
            with db: cur=db.execute('INSERT INTO products(id,name,price,description) VALUES (?,?,?,?)',(next_id('products'),n.strip(),price,d.strip()))
            result=f'Товар создан. ID: {cur.lastrowid}'
        elif key=='stock':
            pid,*items=s.splitlines(); pid=int(pid); assert db.execute('SELECT id FROM products WHERE id=?',(pid,)).fetchone(); assert items
            with db: db.executemany('INSERT INTO stock(product,content) VALUES (?,?)',[(pid,x) for x in items if x.strip()]); result='Склад обновлён'
        elif key=='balance':
            from decimal import Decimal
            uid,v=s.split('|'); uid=int(uid); amount=int(Decimal(v.strip())*100); assert amount>0; user(uid)
            with db: db.execute('UPDATE users SET balance=balance+? WHERE id=?',(amount,uid)); result='Баланс пополнен'
        else:
            if key.startswith('emoji:'):
                s=next((e.custom_emoji_id for e in (m.entities or m.caption_entities or []) if e.type=='custom_emoji'),s.strip()); assert s.isdigit(); s='' if s=='0' else s
                if s:
                    if 'icon_custom_emoji_id' not in InlineKeyboardButton.model_fields: raise ValueError('Версия aiogram не поддерживает Premium Emoji')
                    stickers=await m.bot.get_custom_emoji_stickers(custom_emoji_ids=[s])
                    if not stickers: raise ValueError('Telegram не нашёл этот Emoji ID')
                    test=InlineKeyboardButton(text='Проверка Premium Emoji',callback_data='admin:buttons',icon_custom_emoji_id=s)
                    sent=await m.answer('Предпросмотр кнопки:',reply_markup=kb([test]))
                    actual=sent.reply_markup.inline_keyboard[0][0].icon_custom_emoji_id if sent.reply_markup else None
                    if actual!=s: raise ValueError('Telegram не вернул ID эмодзи в кнопке. Проверьте доступность функции для бота.')
            if key=='label:stars':
                s=s.strip()
                if not 1<=len(s)<=64: raise ValueError('Название должно содержать от 1 до 64 символов')
            if key.startswith('style:'): assert s in ('danger','primary','success','default')
            if key.startswith('text:'):
                if not m.text: raise ValueError('Нужен текст')
                s=m.html_text
                if len(m.text)>1024: raise ValueError('Максимум 1024 символа')
                expected=[e.custom_emoji_id for e in (m.entities or []) if e.type=='custom_emoji']
                if expected:
                    sent=await m.answer(s)
                    actual=[e.custom_emoji_id for e in (sent.entities or []) if e.type=='custom_emoji']
                    if actual!=expected: raise ValueError('Telegram не сохранил Premium Emoji в тестовом сообщении. Проверьте доступность функции для бота.')
            if key.startswith('banner:'):
                if m.photo: s=json.dumps(['photo',m.photo[-1].file_id])
                elif m.animation: s=json.dumps(['animation',m.animation.file_id])
                else: raise ValueError('Нужно фото или GIF')
            if key.startswith(('rate:', 'bonus:')) or key=='points_limit':
                assert s.isdigit() and 0<=int(s)<= (1000000 if key.startswith('bonus:') else 100)
            put(key,s)
            if get(key)!=s: raise RuntimeError('Ошибка проверки записи в базе')
            result='Настройка сохранена'
            if key.startswith('emoji:'): result+='\nEmoji ID: '+(s or 'сброшен')
        await state.clear(); dest='admin:design:'+key.split(':',1)[1] if key.startswith(('text:','banner:')) else ('admin:key:'+key.split(':',1)[1] if key.startswith(('emoji:','style:','label:')) else 'admin'); await m.answer(result,reply_markup=kb([button('Назад',dest)]))
    except Exception as e:
        logging.exception('Ошибка редактора %s',key)
        await m.answer('Не удалось сохранить: '+esc(str(e) or 'Неверный формат')+'\nПопробуйте ещё раз.')
METHODS={'bank':'Перевод по карте','crypto':'CryptoBot','rocket':'xRocket'}
async def provider(method, action, payload):
    crypto=method=='crypto'
    token=os.getenv('CRYPTOBOT_API_TOKEN' if crypto else 'XROCKET_API_TOKEN','')
    if not token: raise ValueError('Способ оплаты пока недоступен')
    base='https://pay.crypt.bot/api/' if crypto else 'https://pay.api.xrocket.exchange/api/v1/'
    headers={'Crypto-Pay-API-Token':token} if crypto else {'Authorization':'Bearer '+token}
    async with aiohttp.ClientSession(timeout=aiohttp.ClientTimeout(total=25),headers=headers) as session:
        verb='GET' if not crypto and action=='invoice' else 'POST'
        async with session.request(verb,base+action,**({'params':payload} if verb=='GET' else {'json':payload})) as response:
            response.raise_for_status(); data=await response.json()
    if crypto:
        if not data.get('ok'): raise ValueError('CryptoBot отклонил запрос')
        return data['result']
    return data

def credit_topup(pid,method,status):
    with db:
        r=db.execute('SELECT * FROM topups WHERE id=? AND method=? AND status=?',(pid,method,status)).fetchone()
        if not r: return None
        if db.execute("UPDATE topups SET status='credited' WHERE id=? AND status=?",(pid,status)).rowcount!=1: return None
        db.execute('INSERT OR IGNORE INTO users(id) VALUES (?)',(r['uid'],))
        db.execute('UPDATE users SET balance=balance+? WHERE id=?',(r['amount'],r['uid']))
    return r

async def notify_credit(bot,r):
    if blocked(r['uid']): return
    try:
        t,b=design('pay:success','<b>PAYMENT SUCCESS</b>\n\nОплата прошла. Средства уже на балансе.\n\nЗачислено: <b>'+money(r['amount'])+'</b>',r['uid'],extra={'amount':money(r['amount'])})
        await bot.send_message(r['uid'],t,reply_markup=kb([button('В корзину','cart',True)],[button('К покупкам','catalog'),button('Личный кабинет','profile')]))
    except Exception: logging.warning('Не удалось уведомить о платеже %s',r['id'])

async def verify_topup(r,bot):
    if r['method']=='crypto':
        data=await provider('crypto','getInvoices',{'invoice_ids':str(r['external'])}); items=data.get('items',[])
        if not items: return False
        x=items[0]; valid=str(x.get('invoice_id'))==r['external'] and x.get('payload')==r['ref'] and x.get('fiat')=='RUB' and Decimal(str(x.get('amount')))==Decimal(r['quote'])
    else:
        x=await provider('rocket','invoice',{'invoiceId':r['external']}); valid=str(x.get('id'))==r['external'] and x.get('clientInvoiceId')==r['ref'] and x.get('priceCurrency')=='USDT' and Decimal(str(x.get('priceAmount')))==Decimal(r['quote'])
    if not valid: raise ValueError('Реквизиты счёта не совпадают')
    if x.get('status')=='paid':
        credited=credit_topup(r['id'],r['method'],'pending')
        if credited: await notify_credit(bot,credited)
        return True
    if x.get('status') in ('expired','cancelled'):
        with db: db.execute("UPDATE topups SET status='expired' WHERE id=? AND status='pending'",(r['id'],))
    return False

async def payment_poll(bot):
    while True:
        for r in db.execute("SELECT * FROM topups WHERE status='pending' AND method!='bank'").fetchall():
            try: await verify_topup(r,bot)
            except Exception: logging.warning('Проверка платежа %s временно недоступна',r['id'])
            await asyncio.sleep(.3)
        await asyncio.sleep(30)

async def payment_click(c,state):
    a=c.data; uid=c.from_user.id; key=a
    if a=='pay':
        t='<b>♦ TRIPPLE PAY</b>\n\nВыберите способ пополнения.'; k=kb([button('Перевод по карте','pay:bank',True)],[button('CryptoBot','pay:crypto'),button('xRocket','pay:rocket')],[button('История пополнений','pay:history')],[button('В корзину','cart')],[back()])
    elif a in ('pay:bank','pay:crypto','pay:rocket'):
        method=a.split(':')[1]; await state.set_state(Edit.value); await state.update_data(key='topup:'+method)
        t='<b>'+('TRIPPLE × '+METHODS[method].upper())+'</b>\n\n'+('Перевод без лишних шагов.' if method=='bank' else 'Крипта. Пара кликов. Готово.')+'\n\nВведите сумму в рублях (от 100 ₽).'; k=kb([button('Назад','pay')])
    elif a=='pay:history':
        rows=db.execute('SELECT * FROM topups WHERE uid=? ORDER BY id DESC LIMIT 15',(uid,)).fetchall(); t='<b>История пополнений</b>\n\n'+('\n'.join(f'#{r["id"]} · {METHODS[r["method"]]} · {money(r["amount"])} · '+{'pending':'Ожидает','review':'На проверке','credited':'Зачислено','rejected':'Отклонено','expired':'Истёк','creating':'Не создан'} .get(r['status'],r['status']) for r in rows) or 'Пока пусто.'); k=kb(*[[button('Проверить #'+str(r['id']),'pay:check:'+str(r['id']))] for r in rows if r['status']=='pending' and r['method']!='bank'],*[ [button('Я оплатил #'+str(r['id']),'pay:sent:'+str(r['id']))] for r in rows if r['status']=='pending' and r['method']=='bank'],[button('Назад','pay')])
    elif a=='admin:payments':
        rows=db.execute("SELECT * FROM topups WHERE method='bank' AND status='review' ORDER BY id LIMIT 20").fetchall(); t='<b>Платежи на проверке</b>\n\n'+('\n'.join(f'#{r["id"]} · ID {r["uid"]} · {money(r["due"])}' for r in rows) or 'Нет заявок.'); k=kb(*[[button('Подтвердить #'+str(r['id']),'admin:approve:'+str(r['id'])),button('Отклонить','admin:reject:'+str(r['id']))] for r in rows],[button('Назад','admin')])
    elif a.startswith(('admin:approve:','admin:reject:')):
        pid=int(a.split(':')[-1]); r=None
        if a.startswith('admin:approve:'): r=credit_topup(pid,'bank','review'); t='Зачислено.' if r else 'Заявка уже обработана.'
        else:
            with db: changed=db.execute("UPDATE topups SET status='rejected' WHERE id=? AND method='bank' AND status='review'",(pid,)).rowcount
            t='Отклонено.' if changed else 'Заявка уже обработана.'
        if r: await notify_credit(c.bot,r)
        k=kb([button('Платежи','admin:payments')])
    elif a.startswith(('pay:sent:','pay:check:')):
        pid=int(a.split(':')[-1]); r=db.execute('SELECT * FROM topups WHERE id=? AND uid=?',(pid,uid)).fetchone()
        if not r: return
        if a.startswith('pay:sent:') and r['method']=='bank':
            with db: changed=db.execute("UPDATE topups SET status='review' WHERE id=? AND status='pending'",(pid,)).rowcount
            if changed:
                for admin in ADMIN:
                    try: await c.bot.send_message(admin,f'Перевод #{pid} · ID {uid}\nПроверьте поступление {money(r["due"])} в банке перед подтверждением.',reply_markup=kb([button('Подтвердить','admin:approve:'+str(pid)),button('Отклонить','admin:reject:'+str(pid))]))
                    except Exception: logging.warning('Админ недоступен')
            t='Заявка на проверке.' if changed else 'Заявка уже обработана или проверяется.'
        elif r['method']!='bank' and r['status']=='pending':
            try: t='Оплата подтверждена.' if await verify_topup(r,c.bot) else 'Оплата пока не подтверждена.'
            except Exception: t='Сервис временно недоступен. Попробуйте позже.'
        else: t='Статус: '+{'credited':'Зачислено','expired':'Истёк','review':'На проверке'}.get(r['status'],'Не оплачен')
        k=kb([button('История пополнений','pay:history')],[button('В корзину','cart')],[button('Назад','pay')])
    else: return
    await screen(c,t,k,key)

async def payment_amount(m,state,method):
    try:
        amount=Decimal((m.text or '').replace(',','.').strip()); assert amount.is_finite() and BANK_MIN_AMOUNT<=amount<=100000 and amount==amount.quantize(Decimal('.01'))
        cents=int(amount*100)
    except Exception: await m.answer('Введите сумму от 100 до 100 000 ₽, не более двух знаков после запятой.'); return
    if method=='bank' and (not BANK_CARD.strip() or not ADMIN): await m.answer('Перевод пока недоступен.'); return
    ref='tripple-'+uuid.uuid4().hex; due=cents
    with db:
        if method=='bank':
            used={r[0] for r in db.execute("SELECT due FROM topups WHERE method='bank'")}; due=next((cents+i for i in range(1,100) if cents+i not in used),None)
            if due is None: await m.answer('Выберите другую сумму.'); return
        pid=db.execute('INSERT INTO topups(uid,method,amount,due,status,ref) VALUES (?,?,?,?,?,?)',(m.from_user.id,method,cents,due,'pending' if method=='bank' else 'creating',ref)).lastrowid
    await state.clear()
    extra={'amount':money(cents),'due':money(due),'card':esc(BANK_CARD),'payment_id':str(pid)}
    if method=='bank':
        t=f'<b>TRIPPLE PAY</b>\n\nПереведите сумму ровно до копейки.\n\nКарта: <code>{esc(BANK_CARD)}</code>\nК оплате: <b>{money(due)}</b>\nК зачислению: {money(cents)}\n\nПосле перевода нажмите «Я оплатил».'; k=kb([button('Я оплатил','pay:sent:'+str(pid),True)],[button('Назад','pay')]); key='pay:bankinvoice'
    else:
        try:
            if method=='crypto':
                quote=str(amount); x=await provider(method,'createInvoice',{'currency_type':'fiat','fiat':'RUB','amount':quote,'payload':ref,'expires_in':3600}); external=str(x['invoice_id']); url=x.get('bot_invoice_url') or x.get('pay_url'); key='pay:cryptoinvoice'
            else:
                rate=Decimal(os.getenv('XROCKET_RUB_PER_USDT','0')); assert rate.is_finite() and rate>0
                quote=str((amount/rate).quantize(Decimal('.01'),rounding=ROUND_UP)); x=await provider(method,'invoices',{'priceAmount':quote,'priceCurrency':'USDT','clientInvoiceId':ref,'description':'Tripple Shop','expiresIn':3600000,'customer':{'id':str(m.from_user.id)}}); external=str(x['id']); url=x['links']['telegramBotLink']; key='pay:rocketinvoice'
            if not url or not url.startswith('https://t.me/'): raise ValueError('Некорректная ссылка')
            with db: db.execute("UPDATE topups SET external=?,quote=?,url=?,status='pending' WHERE id=?",(external,quote,url,pid))
        except Exception:
            logging.warning('Не удалось создать счёт %s',pid); await m.answer('Не удалось создать счёт. Проверьте настройки оплаты или попробуйте позже.',reply_markup=kb([button('Назад','pay')])); return
        b=button('Перейти к оплате','pay:open:'+str(pid),True); b=InlineKeyboardButton(**{**b.model_dump(exclude_none=True,exclude={'callback_data'}),'url':url})
        t=f'<b>TRIPPLE × {METHODS[method].upper()}</b>\n\nСчёт готов к оплате.\n\nК зачислению: <b>{money(cents)}</b>\nСтатус: ожидает оплаты.\n\nБаланс обновится автоматически.'; k=kb([b],[button('Проверить','pay:check:'+str(pid)),button('Назад','pay')])
    t,banner=design(key,t,m.from_user.id,m.from_user.username,extra); await render(m,t,k,banner)

async def main():
    if not os.getenv('BOT_TOKEN'): raise SystemExit('Укажите BOT_TOKEN в файле .env рядом с bot.py')
    if CHANNEL and not CHANNEL_URL.startswith('https://t.me/'): raise SystemExit('Укажите REQUIRED_CHANNEL_URL в .env')
    session=AiohttpSession(timeout=60,proxy=os.getenv('BOT_PROXY') or None)
    session.middleware(track_outgoing)
    bot=Bot(os.environ['BOT_TOKEN'],session=session,default=DefaultBotProperties(parse_mode='HTML')); task=asyncio.create_task(payment_poll(bot))
    try: await dp.start_polling(bot)
    finally: task.cancel(); await asyncio.gather(task,return_exceptions=True)
if __name__=='__main__': asyncio.run(main())
