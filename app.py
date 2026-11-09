import sqlite3, os, secrets, re, json
from datetime import datetime, date, timedelta
from functools import wraps
from flask import (jsonify, Flask, g, render_template, request, redirect, url_for, session, flash, abort)
from werkzeug.security import generate_password_hash, check_password_hash
from werkzeug.utils import secure_filename

app = Flask(__name__)
app.secret_key = os.environ.get('SECRET_KEY', 'change-this-secret')
DB = os.path.join(app.root_path, 'store.db')
UP = os.path.join(app.root_path, 'static', 'uploads')
os.makedirs(UP, exist_ok=True)
STATUS_AR = {'confirmed': 'مؤكد', 'packed': 'تغليف', 'shipped': 'شحن', 'delivered': 'تم التسليم', 'cancelled': 'ملغى'}
GOVS = ['القاهرة','الجيزة','الإسكندرية','الشرقية','المنوفية','القليوبية','الغربية','الدقهلية','الفيوم','أسيوط','سوهاج','قنا',
        'الأقصر','أسوان','البحيرة','المنيا','بني سويف','دمياط','بورسعيد','السويس','الإسماعيلية','كفر الشيخ','البحر الأحمر','مطروح',
        'شمال سيناء','جنوب سيناء','الوادي الجديد']
OLD_ORDER_COLS = {'city':'TEXT','district':'TEXT','street':'TEXT','building':'TEXT','postal':'TEXT','subtotal':'REAL','discount':'REAL',
                  'tax':'REAL','shipping':'REAL','payment_method':'TEXT','coupon':'TEXT'}

SCHEMA = """
CREATE TABLE IF NOT EXISTS products(id INTEGER PRIMARY KEY, name_ar TEXT, name_en TEXT, desc_ar TEXT, desc_en TEXT, price REAL, old_price REAL,
  cost REAL DEFAULT 0, stock INTEGER DEFAULT 0, min_stock INTEGER DEFAULT 3, featured INTEGER DEFAULT 0, active INTEGER DEFAULT 1,
  sort_order INTEGER DEFAULT 0, category_ar TEXT, category_en TEXT, weight TEXT, origin_ar TEXT, origin_en TEXT, photo TEXT, image TEXT);
CREATE TABLE IF NOT EXISTS combos(id INTEGER PRIMARY KEY, name_ar TEXT, name_en TEXT, desc_ar TEXT, desc_en TEXT, price REAL, old_price REAL,
  combo1_id INTEGER, combo1_qty INTEGER DEFAULT 1, combo2_id INTEGER, combo2_qty INTEGER DEFAULT 1, photo TEXT, image TEXT,
  featured INTEGER DEFAULT 0, active INTEGER DEFAULT 1, sort_order INTEGER DEFAULT 0);
CREATE TABLE IF NOT EXISTS orders(id INTEGER PRIMARY KEY, order_number TEXT UNIQUE, full_name TEXT, phone TEXT, address TEXT, channel TEXT,
  status TEXT, grand_total REAL, paid REAL DEFAULT 0, notes TEXT, created_at TEXT, city TEXT, district TEXT, street TEXT, building TEXT,
  postal TEXT, total REAL, discount REAL, tax REAL, shipping REAL, payment_method TEXT, coupon TEXT, waybill TEXT, card_last4 TEXT);
CREATE TABLE IF NOT EXISTS order_items(id INTEGER PRIMARY KEY, order_id INTEGER, key TEXT, name TEXT, qty INTEGER, price REAL, cost REAL);
CREATE TABLE IF NOT EXISTS expenses(id INTEGER PRIMARY KEY, title TEXT, amount REAL, day TEXT);
CREATE TABLE IF NOT EXISTS suppliers(id INTEGER PRIMARY KEY, name TEXT, phone TEXT);
CREATE TABLE IF NOT EXISTS supplier_tx(id INTEGER PRIMARY KEY, supplier_id INTEGER, kind TEXT, amount REAL, day TEXT, note TEXT);
CREATE TABLE IF NOT EXISTS messages(id INTEGER PRIMARY KEY, name TEXT, phone TEXT, body TEXT, created TEXT);
CREATE TABLE IF NOT EXISTS coupons(id INTEGER PRIMARY KEY, code TEXT UNIQUE, kind TEXT, value REAL, free_ship INTEGER DEFAULT 0, active INTEGER DEFAULT 1);
CREATE TABLE IF NOT EXISTS acc_data(k TEXT PRIMARY KEY, v TEXT);
CREATE TABLE IF NOT EXISTS settings(k TEXT PRIMARY KEY, v TEXT);
"""

def db():
    if 'db' not in g:
        g.db = sqlite3.connect(DB)
        g.db.row_factory = sqlite3.Row
    return g.db

@app.teardown_appcontext
def close_db(e=None):
    d = g.pop('db', None)
    if d: d.close()

def q(sql, a=(), one=False):
    r = db().execute(sql, a).fetchall()
    return (r[0] if r else None) if one else r

def x(sql, a=()):
    c = db().execute(sql, a); db().commit(); return c.lastrowid

def S(k, d=''):
    r = q('select v from settings where k=?', (k,), one=True)
    return r['v'] if r else d

def migrate_legacy():
    """ترقية قاعدة بيانات النسخة القديمة (name/code/customer...) إلى الشكل الجديد"""
    d = db()
    cols = lambda t: {r['name'] for r in d.execute(f'pragma table_info({t})')}
    pc = cols('products')
    if not pc or 'name' not in pc or 'name_ar' in pc: return
    for t in ('products', 'combos', 'orders'): d.execute(f'alter table {t} rename to {t}_old')
    d.executescript(SCHEMA)
    oc = cols('orders_old')
    for c, t in OLD_ORDER_COLS.items():
        if c not in oc: d.execute(f'alter table orders_old add column {c} {t}')
    d.execute("""insert into products(id,name_ar,desc_ar,price,cost,stock,min_stock,category_ar,photo,image,active,featured)
      select id,name,description,price,cost,stock,min_stock,category,
      case when image is not null and image<>'' then '/static/uploads/'||image else '' end,'🏍️',1,0 from products_old""")
    d.execute("insert into combos(id,name_ar,desc_ar,price,image,active) select id,name,description,price,'🎁',1 from combos_old")
    for c in d.execute('select id from combos').fetchall():
        for i, it in enumerate(d.execute('select * from combo_items where combo_id=? limit 2', (c['id'],)).fetchall(), 1):
            d.execute(f'update combos set combo{i}_id=?, combo{i}_qty=? where id=?', (it['product_id'], it['qty'], c['id']))
    d.execute("""insert into orders(id,order_number,full_name,phone,address,channel,status,grand_total,paid,notes,created_at,city,district,street,
      building,postal,total,discount,tax,shipping,payment_method,coupon)
      select id,code,customer,phone,address,channel,case status when 'جديد' then 'confirmed' when 'قيد التجهيز' then 'packed'
      when 'تم الشحن' then 'shipped' when 'تم التسليم' then 'delivered' when 'ملغي' then 'cancelled' else status end,
      total,paid,notes,created,city,district,street,building,postal,coalesce(subtotal,total),discount,tax,shipping,'cod',coupon from orders_old""")
    for t in ('products', 'combos', 'orders'): d.execute(f'drop table {t}_old')
    d.commit()

def init_db():
    with app.app_context():
        migrate_legacy()
        db().executescript(SCHEMA)
        if not q('select 1 from settings where k="admin_user"'):
            for k, v in [('admin_user', 'admin'), ('admin_pass', generate_password_hash('admin123')),
                         ('store_name', 'M.Kawasaki'), ('store_sub', 'Motorcycle Accessories'),
                         ('phone', '01120073773'), ('address', 'القاهرة، مصر'),
                         ('about', 'متجر M.Kawasaki لقطع غيار وإكسسوارات الموتوسيكلات. جودة وسعر مناسب وخدمة سريعة.')]:
                x('insert into settings values(?,?)', (k, v))
        for k, v in [('shipping_cost', S('shipping', '50')), ('free_shipping_threshold', '500'), ('tax_rate', '14')]:
            db().execute('insert or ignore into settings values(?,?)', (k, v))
        db().commit()

@app.template_filter('m')
def money(v): return f"{(v or 0):,.2f}"

def csrf_token():
    if 'csrf' not in session: session['csrf'] = secrets.token_hex(16)
    return session['csrf']

@app.before_request
def csrf_guard():
    if request.method == 'POST' and request.path.startswith('/admin'):
        tok = request.form.get('csrf_token') or request.headers.get('X-CSRFToken')
        if not tok or tok != session.get('csrf'): abort(400)

@app.context_processor
def inject():
    st = {r['k']: r['v'] for r in q('select k,v from settings')}
    st.update(site_name_ar=st.get('store_name', ''), tagline_ar=st.get('store_sub', ''), address_ar=st.get('address', ''))
    return dict(S=S, s=st, csrf_token=csrf_token, STATUS_AR=STATUS_AR, cart_count=sum(session.get('cart', {}).values()))

def admin_required(f):
    @wraps(f)
    def w(*a, **k):
        if not session.get('admin'): return redirect(url_for('admin_login', next=request.path))
        return f(*a, **k)
    return w

# ---------- helpers ----------
def comps(key):
    if key[0] == 'p':
        p = q('select * from products where id=?', (key[1:],), one=True)
        return (p['name_ar'], p['price'], [(p, 1)]) if p else None
    c = q('select * from combos where id=?', (key[1:],), one=True)
    if not c: return None
    its = []
    for i in (1, 2):
        p = q('select * from products where id=?', (c[f'combo{i}_id'],), one=True) if c[f'combo{i}_id'] else None
        if p: its.append((p, c[f'combo{i}_qty'] or 1))
    return ('كومبو: ' + c['name_ar'], c['price'], its)

def avail(key):
    c = comps(key)
    if not c or not c[2]: return 0
    return min(p['stock'] // n for p, n in c[2])

def cart_lines():
    out = []
    for k, n in session.get('cart', {}).items():
        c = comps(k)
        if c: out.append(dict(key=k, name=c[0], price=c[1], qty=n, max=avail(k)))
    return out

def totals(items, city=None):
    sub = sum(i['price'] * i['qty'] for i in items)
    disc, free, code = 0, False, ''
    cp = q('select * from coupons where code=? and active=1', (session.get('coupon', ''),), one=True)
    if cp:
        code, free = cp['code'], bool(cp['free_ship'])
        disc = min(sub * cp['value'] / 100 if cp['kind'] == 'percent' else cp['value'], sub)
    elif session.get('coupon'): session.pop('coupon')
    net, rate = sub - disc, float(S('tax_rate', '14') or 14)
    thresh = float(S('free_shipping_threshold', '500') or 500)
    freeship = free or net >= thresh
    default = float(S('shipping_cost', '50') or 50)
    ship = 0 if freeship else (float(S('ship_gov_' + city) or default) if city else None)
    tax = round(net * rate / 100, 2)
    return dict(sub=sub, disc=disc, coupon=code, free=free, freeship=freeship, net=net, rate=rate, tax=tax, thresh=thresh,
                ship=ship, grand=net + tax + (ship or 0))

def make_order(lines, customer, phone, addr, channel, paid=0, notes='', status='confirmed', tax=0, shipping=0, discount=0,
               coupon='', city='', district='', street='', building='', postal='', pm='cod'):
    code = datetime.now().strftime('%y%m%d') + secrets.token_hex(2).upper()
    sub, rows = 0, []
    for l in lines:
        c = comps(l['key'])
        if not c or avail(l['key']) < l['qty']:
            raise ValueError('الكمية غير متاحة: ' + (c[0] if c else l['key']))
        price = l.get('price') if l.get('price') not in (None, '') else c[1]
        cost = sum(p['cost'] * n for p, n in c[2])
        sub += price * l['qty']
        rows.append((l['key'], c[0], l['qty'], price, cost, c[2]))
    total = sub - discount + tax + shipping
    oid = x('insert into orders(order_number,full_name,phone,address,channel,status,grand_total,paid,notes,created_at,city,district,street,building,postal,total,discount,tax,shipping,payment_method,coupon) values(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)',
            (code, customer, phone, addr, channel, status, total, paid, notes, datetime.now().strftime('%Y-%m-%d %H:%M'),
             city, district, street, building, postal, sub, discount, tax, shipping, pm, coupon))
    for key, name, qty, price, cost, cs in rows:
        x('insert into order_items(order_id,key,name,qty,price,cost) values(?,?,?,?,?,?)', (oid, key, name, qty, price, cost))
        for p, n in cs: x('update products set stock=stock-? where id=?', (n * qty, p['id']))
    return oid, code

def restock(oid):
    for it in q('select * from order_items where order_id=?', (oid,)):
        c = comps(it['key'])
        if c:
            for p, n in c[2]: x('update products set stock=stock+? where id=?', (n * it['qty'], p['id']))

def save_photo(old=''):
    if request.form.get('remove_photo'): old = ''
    f = request.files.get('photo')
    if f and f.filename:
        n = secrets.token_hex(4) + '_' + secure_filename(f.filename)
        f.save(os.path.join(UP, n)); return '/static/uploads/' + n
    return old

PSEL = 'select *, name_ar as name, category_ar as category, desc_ar as description from products'

# ---------- public ----------
def _shop(home):
    s, cat = request.args.get('q', '').strip(), request.args.get('cat', '')
    sql, a = PSEL + ' where active=1', []
    if s: sql += ' and (name_ar like ? or desc_ar like ? or category_ar like ?)'; a += [f'%{s}%'] * 3
    if cat: sql += ' and category_ar=?'; a.append(cat)
    cats = [r[0] for r in q('select distinct category_ar from products where active=1 and category_ar<>""')]
    combos = q('select *, name_ar as name, desc_ar as description from combos where active=1 order by sort_order, id desc') if home else []
    return render_template('shop.html', products=q(sql + ' order by sort_order, id desc', a), cats=cats, cat=cat, s_q=s,
                           combos=combos, home=home, avail=avail)

@app.route('/')
def index(): return _shop(True)

@app.route('/shop')
def shop(): return _shop(False)

@app.route('/product/<int:pid>')
def product(pid):
    p = q(PSEL + ' where id=?', (pid,), one=True) or abort(404)
    return render_template('product.html', p=p, rel=q(PSEL + ' where active=1 and category_ar=? and id<>? limit 4', (p['category_ar'], pid)))

@app.route('/cart')
def cart():
    items = cart_lines()
    return render_template('cart.html', items=items, t=totals(items))

@app.route('/cart/add', methods=['POST'])
def cart_add():
    k, n = request.form['key'], max(1, int(request.form.get('qty', 1)))
    cart = session.get('cart', {})
    if avail(k) < cart.get(k, 0) + n: flash('الكمية المطلوبة غير متاحة في المخزون', 'err')
    else: cart[k] = cart.get(k, 0) + n; flash('تمت الإضافة للسلة', 'ok')
    session['cart'] = cart
    return redirect(request.referrer or url_for('shop'))

@app.route('/cart/update', methods=['POST'])
def cart_update():
    k, a = request.form['cid'], request.form['action']
    cart = session.get('cart', {})
    if k in cart:
        if a == '+':
            if avail(k) > cart[k]: cart[k] += 1
            else: flash('وصلت للحد الأقصى المتاح', 'err')
        elif a == '-': cart[k] -= 1
        else: cart[k] = 0
        if cart[k] <= 0: cart.pop(k)
    session['cart'] = cart
    return redirect(url_for('cart'))

@app.route('/cart/coupon', methods=['POST'])
def cart_coupon():
    code = request.form.get('coupon_code', '').strip().upper()
    if code and q('select 1 from coupons where code=? and active=1', (code,)): session['coupon'] = code; flash('تم تفعيل كود الخصم', 'ok')
    else: flash('كود الخصم غير صحيح', 'err')
    return redirect(url_for('cart'))

@app.route('/cart/coupon/remove', methods=['POST'])
def cart_coupon_remove():
    session.pop('coupon', None); return redirect(url_for('cart'))

@app.route('/checkout', methods=['GET', 'POST'])
def checkout():
    items = cart_lines()
    if not items: return redirect(url_for('cart'))
    if request.method == 'POST':
        f = request.form
        if f.get('city') not in GOVS or not re.fullmatch(r'\d{11}', f.get('phone', '')):
            flash('راجع المحافظة ورقم الموبيل (11 رقم)', 'err'); return redirect(url_for('checkout'))
        t = totals(items, f['city'])
        try:
            _, code = make_order([dict(key=i['key'], qty=i['qty']) for i in items], f['full_name'], f['phone'], f.get('address', ''),
                                 'online', 0, '', 'confirmed', tax=t['tax'], shipping=t['ship'] or 0, discount=t['disc'], coupon=t['coupon'],
                                 city=f['city'], district=f['district'], street=f['street'], building=f['building'],
                                 postal=f.get('postal', ''), pm='cod')
        except ValueError as e:
            flash(str(e), 'err'); return redirect(url_for('cart'))
        session['cart'] = {}; session.pop('coupon', None)
        return redirect(url_for('success', code=code))
    default = float(S('shipping_cost', '50') or 50)
    return render_template('checkout.html', items=items, t=totals(items), govs=GOVS,
                           ship_map={g: float(S('ship_gov_' + g) or default) for g in GOVS})

@app.route('/success/<code>')
def success(code):
    o = q('select * from orders where order_number=?', (code,), one=True) or abort(404)
    return render_template('success.html', o=o, items=q('select * from order_items where order_id=?', (o['id'],)))

@app.route('/track', methods=['GET', 'POST'])
def track():
    src = request.form if request.method == 'POST' else request.args
    ph = src.get('phone', '').strip(); res = []
    if ph:
        for o in q("select * from orders where phone=? and channel='online' order by id desc limit 10", (ph,)):
            res.append((o, q('select * from order_items where order_id=?', (o['id'],))))
        if not res: flash('لا توجد طلبات مسجّلة بهذا الرقم، راجع رقم الموبيل', 'err')
    return render_template('track.html', res=res, ph=ph)

@app.route('/about')
def about():
    m = S('map_embed', '').strip(); src = ''
    if m:
        mm = re.search(r'src=["\']([^"\']+)', m); src = mm.group(1) if mm else (m if m.startswith('http') else '')
    return render_template('about.html', map_src=src)

@app.route('/contact', methods=['GET', 'POST'])
def contact():
    if request.method == 'POST':
        x('insert into messages(name,phone,body,created) values(?,?,?,?)', (request.form['name'], request.form['phone'],
          request.form['body'], datetime.now().strftime('%Y-%m-%d %H:%M')))
        flash('تم إرسال رسالتك، سنتواصل معك قريباً', 'ok'); return redirect(url_for('contact'))
    return render_template('contact.html')

# ---------- admin: auth / dashboard ----------
@app.route('/admin/login', methods=['GET', 'POST'])
def admin_login():
    error = False
    if request.method == 'POST':
        if request.form.get('username') == S('admin_user') and check_password_hash(S('admin_pass'), request.form.get('password', '')):
            session['admin'] = True; return redirect(request.args.get('next') or url_for('admin_dashboard'))
        error = True
    return render_template('admin/login.html', error=error)

@app.route('/admin/logout')
def admin_logout():
    session.pop('admin', None); return redirect(url_for('admin_login'))

def stats():
    r = lambda sql, a=(): (q(sql, a, one=True)[0] or 0)
    ok = "status<>'cancelled'"
    revenue = r(f'select sum(grand_total-coalesce(tax,0)-coalesce(shipping,0)) from orders where {ok}')
    cogs = r(f"select sum(i.qty*i.cost) from order_items i join orders o on o.id=i.order_id where o.{ok}")
    exp = r('select sum(amount) from expenses')
    cash_in = r(f'select sum(paid) from orders where {ok}')
    sup_pay = r("select sum(amount) from supplier_tx where kind='payment'")
    return dict(revenue=revenue, cogs=cogs, gross=revenue - cogs, expenses=exp, net=revenue - cogs - exp,
                orders=r(f'select count(*) from orders where {ok}'), cash_in=cash_in, sup_pay=sup_pay,
                treasury=cash_in - exp - sup_pay, credit=r(f'select sum(grand_total-paid) from orders where {ok}'),
                stock_value=r('select sum(stock*cost) from products'))

@app.route('/admin')
@admin_required
def admin_dashboard():
    st, ok = stats(), "status<>'cancelled'"
    days = [(date.today() - timedelta(days=i)) for i in range(6, -1, -1)]
    daily = [(d.strftime('%m-%d'), q(f"select coalesce(sum(grand_total),0) from orders where created_at like ? and {ok}", (d.strftime('%Y-%m-%d') + '%',), one=True)[0]) for d in days]
    r1 = lambda sql: (q(sql, one=True)[0] or 0)
    city = q("select city, count(*) n from orders where city<>'' and city is not null group by city order by n desc limit 1", one=True)
    n_ord = st['orders']
    return render_template('admin/dashboard.html', st=st, total_revenue=st['revenue'], total_orders=n_ord,
        total_customers=r1("select count(distinct phone) from orders"), total_products=r1('select count(*) from products where active=1'),
        avg_order=(r1(f'select sum(grand_total) from orders where {ok}') / n_ord if n_ord else 0),
        total_items_sold=r1(f"select sum(i.qty) from order_items i join orders o on o.id=i.order_id where o.{ok}"),
        top_city=city['city'] if city else '', daily_revenues=daily,
        status_counts={r['status']: r['n'] for r in q('select status, count(*) n from orders group by status')},
        recent_orders=q('select * from orders order by id desc limit 8'),
        top_products=[(r[0], '🏍️', r[1], r[2]) for r in q(f"select i.name, sum(i.qty), sum(i.qty*i.price) from order_items i join orders o on o.id=i.order_id where o.{ok} group by i.name order by 2 desc limit 6")],
        low=q('select * from products where stock<=min_stock order by stock limit 5'), recent_status_changes=None)

# ---------- admin: products ----------
@app.route('/admin/products')
@admin_required
def admin_products():
    return render_template('admin/products.html', products=q('select * from products order by sort_order, id desc'))

PCOLS = 'name_ar,name_en,desc_ar,desc_en,price,old_price,cost,stock,min_stock,featured,active,sort_order,category_ar,category_en,weight,origin_ar,origin_en,photo,image'.split(',')

def _product_form(pid):
    p = q('select * from products where id=?', (pid,), one=True) if pid else None
    if pid and not p: abort(404)
    if request.method == 'POST':
        f = request.form
        v = (f['name_ar'], f.get('name_en', ''), f.get('desc_ar', ''), f.get('desc_en', ''), float(f['price']),
             float(f['old_price']) if f.get('old_price') else None, float(f.get('cost') or 0), int(f.get('stock') or 0),
             int(f.get('min_stock') or 3), 1 if f.get('featured') else 0, 1 if f.get('active') else 0, int(f.get('sort_order') or 0),
             f.get('category_ar', ''), f.get('category_en', ''), f.get('weight', ''), f.get('origin_ar', ''), f.get('origin_en', ''),
             save_photo(p['photo'] if p else ''), f.get('image') or '🏍️')
        if p: x('update products set ' + ','.join(c + '=?' for c in PCOLS) + ' where id=?', v + (pid,))
        else: x(f"insert into products({','.join(PCOLS)}) values({','.join('?' * len(PCOLS))})", v)
        flash('تم حفظ المنتج', 'success'); return redirect(url_for('admin_products'))
    cats = [dict(ar=r['category_ar'], en=r['category_en']) for r in q('select distinct category_ar, category_en from products where category_ar<>"" and category_ar is not null')]
    return render_template('admin/product_form.html', product=p, action='edit' if p else 'add', cats=cats)

@app.route('/admin/products/add', methods=['GET', 'POST'])
@admin_required
def admin_product_add(): return _product_form(None)

@app.route('/admin/products/edit/<int:pid>', methods=['GET', 'POST'])
@admin_required
def admin_product_edit(pid): return _product_form(pid)

@app.route('/admin/products/toggle/<int:pid>', methods=['POST'])
@admin_required
def admin_product_toggle(pid):
    x('update products set active=1-coalesce(active,1) where id=?', (pid,)); return redirect(url_for('admin_products'))

@app.route('/admin/products/delete/<int:pid>', methods=['POST'])
@admin_required
def admin_product_delete(pid):
    x('delete from products where id=?', (pid,)); flash('تم حذف المنتج', 'success'); return redirect(url_for('admin_products'))

# ---------- admin: combos ----------
@app.route('/admin/combos')
@admin_required
def admin_combos():
    rows = []
    for c in q('select * from combos order by sort_order, id desc'):
        d = dict(c); d['stock'] = avail('c%d' % c['id'])
        for i in (1, 2):
            p = q('select name_ar, weight, stock from products where id=?', (c[f'combo{i}_id'],), one=True) if c[f'combo{i}_id'] else None
            d[f'combo{i}_name'] = p['name_ar'] if p else ''
            d[f'combo{i}_stock'] = p['stock'] if p else 0
        rows.append(d)
    return render_template('admin/combos.html', combos=rows)

CCOLS = 'name_ar,name_en,desc_ar,desc_en,price,old_price,combo1_id,combo1_qty,combo2_id,combo2_qty,photo,image,featured,active,sort_order'.split(',')

def _combo_form(pid):
    c = q('select * from combos where id=?', (pid,), one=True) if pid else None
    if pid and not c: abort(404)
    if request.method == 'POST':
        f = request.form
        v = (f['name_ar'], f.get('name_en', ''), f.get('desc_ar', ''), f.get('desc_en', ''), float(f['price']),
             float(f['old_price']) if f.get('old_price') else None, int(f['combo1_id']), int(f.get('combo1_qty') or 1),
             int(f['combo2_id']), int(f.get('combo2_qty') or 1), save_photo(c['photo'] if c else ''), f.get('image') or '🎁',
             1 if f.get('featured') else 0, 1 if f.get('active') else 0, int(f.get('sort_order') or 0))
        if c: x('update combos set ' + ','.join(k + '=?' for k in CCOLS) + ' where id=?', v + (pid,))
        else: x(f"insert into combos({','.join(CCOLS)}) values({','.join('?' * len(CCOLS))})", v)
        flash('تم حفظ العرض', 'success'); return redirect(url_for('admin_combos'))
    d = dict(c) if c else None
    if d: d['stock'] = avail('c%d' % pid)
    return render_template('admin/combo_form.html', combo=d, action='edit' if c else 'add',
                           component_products=q('select id, name_ar, weight, stock from products order by name_ar'))

@app.route('/admin/combos/add', methods=['GET', 'POST'])
@admin_required
def admin_combo_add(): return _combo_form(None)

@app.route('/admin/combos/edit/<int:pid>', methods=['GET', 'POST'])
@admin_required
def admin_combo_edit(pid): return _combo_form(pid)

@app.route('/admin/combos/toggle/<int:pid>', methods=['POST'])
@admin_required
def admin_combo_toggle(pid):
    x('update combos set active=1-coalesce(active,1) where id=?', (pid,)); return redirect(url_for('admin_combos'))

@app.route('/admin/combos/delete/<int:pid>', methods=['POST'])
@admin_required
def admin_combo_delete(pid):
    x('delete from combos where id=?', (pid,)); flash('تم حذف العرض', 'success'); return redirect(url_for('admin_combos'))

# ---------- admin: orders ----------
@app.route('/admin/orders')
@admin_required
def admin_orders():
    rows = q('select * from orders order by id desc limit 300')
    last = session.get('seen_order', 0)
    new_ids = [o['id'] for o in rows if o['id'] > last and o['channel'] == 'online']
    if rows: session['seen_order'] = max(rows[0]['id'], last)
    return render_template('admin/orders.html', orders=rows, new_ids=new_ids)

@app.route('/admin/orders/status/<int:oid>', methods=['POST'])
@admin_required
def admin_order_status(oid):
    o = q('select * from orders where id=?', (oid,), one=True) or abort(404)
    new = request.form['status']
    if new not in STATUS_AR: abort(400)
    if new == 'cancelled' and o['status'] != 'cancelled': restock(oid)
    x('update orders set status=? where id=?', (new, oid))
    if new == 'delivered' and o['channel'] == 'online': x('update orders set paid=grand_total where id=?', (oid,))
    if request.headers.get('Sec-Fetch-Dest') == 'empty': return '', 204
    return redirect(request.referrer or url_for('admin_orders'))

@app.route('/admin/orders/waybill/<int:oid>', methods=['POST'])
@admin_required
def admin_order_waybill(oid):
    x('update orders set waybill=? where id=?', (request.form.get('waybill', '').strip(), oid)); return '', 204

@app.route('/admin/orders/pay/<int:oid>', methods=['POST'])
@admin_required
def admin_order_pay(oid):
    x('update orders set paid=paid+? where id=?', (float(request.form['amount']), oid))
    return redirect(request.referrer or url_for('admin_orders'))

@app.route('/admin/orders/delete', methods=['POST'])
@admin_required
def admin_orders_delete():
    for oid in request.form.getlist('order_ids'):
        o = q('select status from orders where id=?', (oid,), one=True)
        if not o: continue
        if o['status'] != 'cancelled': restock(oid)
        x('delete from order_items where order_id=?', (oid,)); x('delete from orders where id=?', (oid,))
    flash('تم حذف الطلبات المحددة', 'success'); return redirect(url_for('admin_orders'))

@app.route('/admin/invoice/<int:oid>')
@admin_required
def admin_order_invoice(oid):
    o = q('select * from orders where id=?', (oid,), one=True) or abort(404)
    return render_template('admin/invoice.html', o=o, items=q('select * from order_items where order_id=?', (oid,)))

# ---------- admin: accounts (تطبيق JS بيخزّن بياناته كجداول JSON) ----------
ACC_KEYS = ['customers','sales','stock','components','expenses','transactions','treasury','zakat','orders','notes','leads','merchants']

def acc_load(k):
    r = q('select v from acc_data where k=?', (k,), one=True)
    try: return json.loads(r['v']) if r else []
    except ValueError: return []

def acc_save(k, v): x('insert or replace into acc_data values(?,?)', (k, json.dumps(v, ensure_ascii=False)))

def api_admin(f):
    @wraps(f)
    def w(*a, **k):
        if not session.get('admin'): return jsonify(ok=False, message='غير مصرح'), 401
        return f(*a, **k)
    return w

def sync_online_orders():
    """الطلبات الأونلاين المسلّمة تظهر تلقائياً فى مبيعات الحسابات (id = online-order-N) وتنتظر تأكيد الخزنة"""
    sales = acc_load('sales'); have = {s_['id'] for s_ in sales}; tr = None; changed = False
    for o in q("select * from orders where channel='online' and status='delivered'"):
        sid = f"online-order-{o['id']}"
        if sid in have: continue
        its = q('select * from order_items where order_id=?', (o['id'],))
        cart = [dict(stockId=None, name=i['name'], qty=i['qty'], price=i['price'], cost=i['cost'] or 0, unit='') for i in its]
        qty = sum(i['qty'] for i in its) or 1
        rev = (o['grand_total'] or 0) - (o['tax'] or 0); icost = sum(i['qty'] * (i['cost'] or 0) for i in its); day = (o['created_at'] or '')[:10]
        sales.append(dict(id=sid, custId='', stockId=None, itemName='، '.join(f"{i['name']} ({i['qty']})" for i in its), qty=qty, price=rev / qty,
            cost=icost / qty, items=json.dumps(cart, ensure_ascii=False), shipping=0, codingCost=0, extraCost=0, subtotal=o['total'] or rev,
            discountPercent=0, discountFlat=o['discount'] or 0, revenue=rev, totalCost=icost, profit=rev - icost,
            margin=round((rev - icost) / rev * 100, 1) if rev > 0 else 0, paidNow=rev, deferred=0, paymentMethod='cash', dueDate='',
            notes='طلب أونلاين ' + o['order_number'] + ' — ' + (o['full_name'] or ''), date=day))
        if tr is None: tr = acc_load('treasury')
        tr.append(dict(id='tr-' + sid, type='sale', amount=rev, balance=0, desc='طلب أونلاين: ' + o['order_number'], name=o['full_name'] or '',
                       source='sale', refId=sid, date=day, confirmed=0))
        changed = True
    if changed:
        acc_save('sales', sales)
        if tr is not None: acc_save('treasury', tr)

@app.route('/admin/accounts')
@admin_required
def admin_accounts(): return render_template('admin/accounts.html')

@app.route('/admin/api/accounts/all')
@api_admin
def api_acc_all():
    sync_online_orders()
    return jsonify(ok=True, data={k: acc_load(k) for k in ACC_KEYS})

@app.route('/admin/api/accounts/<key>', methods=['GET', 'POST'])
@api_admin
def api_acc_key(key):
    if key not in ACC_KEYS: return jsonify(ok=False, message='جدول غير معروف'), 404
    if request.method == 'GET': return jsonify(ok=True, data=acc_load(key))
    data = request.get_json(silent=True)
    if not isinstance(data, list): return jsonify(ok=False, message='بيانات غير صالحة'), 400
    if key == 'sales':
        new_ids = {d.get('id') for d in data if isinstance(d, dict)}
        if any(s_['id'] not in new_ids for s_ in acc_load('sales')):
            return jsonify(ok=False, error='sales_would_be_deleted', message='الحفظ هيمسح فواتير موجودة — حدّث الصفحة وحاول تانى. (الاسترجاع بيتم من ملخص الحسابات ← استرجاع بيعة)'), 409
    acc_save(key, data)
    return jsonify(ok=True)

def _adjust_product(pid, delta):
    p = q('select stock from products where id=?', (pid,), one=True)
    if not p: return None
    n = max(0, (p['stock'] or 0) + int(delta)); x('update products set stock=? where id=?', (n, pid)); return n

@app.route('/admin/api/products/simple-list')
@api_admin
def api_products_list():
    return jsonify(ok=True, products=[dict(id=p['id'], name=p['name_ar'], stock=p['stock']) for p in q('select id,name_ar,stock from products order by name_ar')])

@app.route('/admin/api/products/<int:pid>/adjust-stock', methods=['POST'])
@api_admin
def api_adjust_stock(pid):
    n = _adjust_product(pid, (request.get_json(silent=True) or {}).get('delta', 0))
    return jsonify(ok=n is not None, new_stock=n)

@app.route('/admin/api/accounts/sale-reverse/<sid>', methods=['POST'])
@api_admin
def api_sale_reverse(sid):
    sales = acc_load('sales'); sale = next((s_ for s_ in sales if s_['id'] == sid), None)
    stock = acc_load('stock')
    if sale:
        try: items = json.loads(sale.get('items') or '[]')
        except ValueError: items = []
        for it in items:
            for st in stock:
                if it.get('stockId') and st.get('id') == it['stockId']:
                    st['qty'] = (st.get('qty') or 0) + it['qty']
                    try: links = json.loads(st.get('productLinks') or '[]')
                    except ValueError: links = []
                    for pid in (links or ([st['productId']] if st.get('productId') else [])): _adjust_product(pid, it['qty'])
        acc_save('stock', stock); acc_save('sales', [s_ for s_ in sales if s_['id'] != sid])
    acc_save('treasury', [r for r in acc_load('treasury') if r.get('refId') != sid])
    return jsonify(ok=True)

@app.route('/admin/api/accounts/online-order-cancel/<int:oid>', methods=['POST'])
@api_admin
def api_online_cancel(oid):
    o = q('select status from orders where id=?', (oid,), one=True)
    if o and o['status'] != 'cancelled':
        restock(oid); x("update orders set status='cancelled' where id=?", (oid,))
    return jsonify(ok=True)

@app.route('/admin/api/soycost', methods=['GET', 'POST'])
@api_admin
def api_soycost():
    if request.method == 'POST':
        x('insert or replace into settings values(?,?)', ('soycost_json', json.dumps((request.get_json(silent=True) or {}).get('data'), ensure_ascii=False)))
        return jsonify(ok=True)
    v = S('soycost_json', '')
    return jsonify(ok=True, data=json.loads(v) if v else None)

SETTING_KEYS = ['store_name', 'store_sub', 'phone', 'email', 'address', 'whatsapp', 'facebook', 'instagram', 'tiktok', 'about', 'map_link', 'map_embed', 'work_hours',
                'shipping_cost', 'free_shipping_threshold', 'tax_rate', 'admin_user'] + ['ship_gov_' + g for g in GOVS]

@app.route('/admin/settings', methods=['GET', 'POST'])
@admin_required
def admin_settings():
    if request.method == 'POST':
        for k in SETTING_KEYS:
            if k in request.form: x('insert or replace into settings values(?,?)', (k, request.form[k]))
        if request.form.get('new_pass'):
            x('insert or replace into settings values(?,?)', ('admin_pass', generate_password_hash(request.form['new_pass'])))
        flash('تم حفظ الإعدادات', 'success'); return redirect(url_for('admin_settings'))
    return render_template('admin/settings.html', govs=GOVS, coupons=q('select * from coupons order by id desc'))

@app.route('/admin/coupons/add', methods=['POST'])
@admin_required
def coupon_add():
    f = request.form
    x('insert or replace into coupons(code,kind,value,free_ship,active) values(?,?,?,?,1)',
      (f['code'].strip().upper(), f['kind'], float(f['value'] or 0), 1 if f.get('free_ship') else 0))
    return redirect(url_for('admin_settings'))

@app.route('/admin/coupons/delete/<int:cid>', methods=['POST'])
@admin_required
def coupon_delete(cid):
    x('delete from coupons where id=?', (cid,)); return redirect(url_for('admin_settings'))

init_db()
if __name__ == '__main__':
    app.run(host='0.0.0.0', port=5000, debug=True)
