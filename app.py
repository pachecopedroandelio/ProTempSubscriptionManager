import os
from datetime import date, datetime, timedelta
from functools import wraps
from flask import Flask, flash, redirect, render_template, request, session, url_for
from flask_sqlalchemy import SQLAlchemy
from sqlalchemy import UniqueConstraint, inspect, text
from werkzeug.security import check_password_hash, generate_password_hash

app = Flask(__name__)
app.config['SECRET_KEY'] = os.getenv('SECRET_KEY', 'local-development-only')
url = os.getenv('DATABASE_URL', 'sqlite:///protemp.db')
if url.startswith('postgres://'):
    url = url.replace('postgres://', 'postgresql+psycopg://', 1)
elif url.startswith('postgresql://'):
    url = url.replace('postgresql://', 'postgresql+psycopg://', 1)
app.config['SQLALCHEMY_DATABASE_URI'] = url
app.config['SQLALCHEMY_TRACK_MODIFICATIONS'] = False
app.config['SESSION_COOKIE_HTTPONLY'] = True
app.config['SESSION_COOKIE_SAMESITE'] = 'Lax'
app.config['SESSION_COOKIE_SECURE'] = os.getenv('RENDER', '').lower() == 'true'
db = SQLAlchemy(app)

class Customer(db.Model):
    id = db.Column(db.Integer, primary_key=True)
    name = db.Column(db.String(160), nullable=False, unique=True)
    contact = db.Column(db.String(160), default='')
    email = db.Column(db.String(160), default='')
    notes = db.Column(db.Text, default='')
    devices = db.relationship('Device', backref='customer', lazy=True)

class Device(db.Model):
    id = db.Column(db.Integer, primary_key=True)
    serial = db.Column(db.String(120), nullable=False, unique=True)
    customer_id = db.Column(db.Integer, db.ForeignKey('customer.id'), nullable=False)
    start_date = db.Column(db.Date, nullable=False)
    expiration_date = db.Column(db.Date, nullable=False)
    duration_days = db.Column(db.Integer, nullable=False)
    # Preserve existing database column and customer records while correcting its meaning.
    subscription_price = db.Column("monthly_price", db.Numeric(10, 2), default=0)
    suspension_status = db.Column(db.String(30), default='none', nullable=False)
    notes = db.Column(db.Text, default='')
    renewals = db.relationship('Renewal', backref='device', lazy=True, cascade='all,delete-orphan')

class Renewal(db.Model):
    id = db.Column(db.Integer, primary_key=True)
    device_id = db.Column(db.Integer, db.ForeignKey('device.id'), nullable=False)
    added_days = db.Column(db.Integer, nullable=False)
    previous_expiration = db.Column(db.Date, nullable=False)
    new_expiration = db.Column(db.Date, nullable=False)
    price_charged = db.Column(db.Numeric(10, 2), nullable=True)
    created_at = db.Column(db.DateTime, default=datetime.utcnow)

with app.app_context():
    # Additive migration: existing customer/device data and prior prices are preserved.
    db.create_all()
    if 'renewal' in inspect(db.engine).get_table_names():
        cols = {c['name'] for c in inspect(db.engine).get_columns('renewal')}
        if 'price_charged' not in cols:
            with db.engine.begin() as conn:
                conn.execute(text('ALTER TABLE renewal ADD COLUMN price_charged NUMERIC(10,2)'))

def login_required(f):
    @wraps(f)
    def wrapped(*args, **kwargs):
        if not session.get('logged_in'):
            return redirect(url_for('login'))
        return f(*args, **kwargs)
    return wrapped

@app.context_processor
def inject():
    def status(d):
        if d.suspension_status == 'suspended': return 'suspended'
        if d.expiration_date < date.today(): return 'expired'
        if (d.expiration_date-date.today()).days <= 7: return 'soon'
        return 'active'
    return {'today': date.today(), 'status': status}

@app.route('/login', methods=['GET','POST'])
def login():
    if request.method == 'POST':
        password = os.getenv('ADMIN_PASSWORD', '')
        candidate = request.form.get('password', '')
        import hmac
        if password and (hmac.compare_digest(candidate, password) or (password.startswith('scrypt:') and check_password_hash(password, candidate))):
            session.clear()
            session['logged_in'] = True
            return redirect(url_for('index'))
        flash('Incorrect password.', 'error')
    return render_template('login.html')

@app.route('/logout', methods=['POST'])
@login_required
def logout():
    session.clear()
    return redirect(url_for('login'))

@app.route('/')
@login_required
def index():
    devices = Device.query.order_by(Device.expiration_date.asc()).all()
    customers = Customer.query.order_by(Customer.name).all()
    active = sum(d.suspension_status != 'suspended' and d.expiration_date >= date.today()+timedelta(days=8) for d in devices)
    soon = sum(d.suspension_status != 'suspended' and date.today() <= d.expiration_date <= date.today()+timedelta(days=7) for d in devices)
    expired = sum(d.suspension_status != 'suspended' and d.expiration_date < date.today() for d in devices)
    pending = sum(d.suspension_status == 'requested' for d in devices)
    return render_template('index.html', devices=devices, customers=customers, active=active, soon=soon, expired=expired, pending=pending)

@app.route('/customers/add', methods=['POST'])
@login_required
def add_customer():
    name = request.form.get('name','').strip()
    if not name:
        flash('Customer name required.', 'error')
    elif Customer.query.filter(db.func.lower(Customer.name)==name.lower()).first():
        flash('Customer already exists.', 'error')
    else:
        db.session.add(Customer(name=name,contact=request.form.get('contact','').strip(),email=request.form.get('email','').strip(),notes=request.form.get('notes','').strip()))
        db.session.commit()
        flash('Customer added.', 'success')
    return redirect(url_for('index', tab='customers'))

@app.route('/devices/add', methods=['POST'])
@login_required
def add_device():
    try:
        serial = request.form['serial'].strip()
        customer_id = int(request.form['customer_id'])
        start = date.fromisoformat(request.form['start_date'])
        duration = int(request.form['duration_days'])
        price = float(request.form.get('subscription_price') or 0)
        if not serial or not 1 <= duration <= 36500 or price < 0 or price > 1000000 or not db.session.get(Customer,customer_id): raise ValueError()
        if Device.query.filter(db.func.lower(Device.serial)==serial.lower()).first():
            flash('Serial number already exists.', 'error')
        else:
            db.session.add(Device(serial=serial,customer_id=customer_id,start_date=start,expiration_date=start+timedelta(days=duration),duration_days=duration,subscription_price=price,notes=request.form.get('notes','').strip()))
            db.session.commit()
            flash('Device added.', 'success')
    except (ValueError,KeyError,OverflowError):
        flash('Please check device details.', 'error')
    return redirect(url_for('index', tab='devices'))

@app.route('/devices/<int:device_id>/renew', methods=['POST'])
@login_required
def renew(device_id):
    d = db.get_or_404(Device,device_id)
    try:
        extra = int(request.form['days'])
        if not 1 <= extra <= 36500: raise ValueError()
        charge = float(request.form.get('subscription_price') or 0)
        if not 0 <= charge <= 1000000: raise ValueError()
        old = d.expiration_date
        # Add to existing expiration; if expired, start from today.
        base = max(old,date.today())
        d.expiration_date = base+timedelta(days=extra)
        d.duration_days += extra
        db.session.add(Renewal(device_id=d.id,added_days=extra,previous_expiration=old,new_expiration=d.expiration_date,price_charged=charge))
        db.session.commit()
        flash('Renewal saved with its subscription charge. Remember to reactivate with TZONE if suspended.', 'success')
    except (ValueError,KeyError,OverflowError):
        flash('Enter valid renewal days and subscription price.', 'error')
    return redirect(url_for('index',tab='devices'))

@app.route('/devices/<int:device_id>/suspension', methods=['POST'])
@login_required
def suspension(device_id):
    d = db.get_or_404(Device,device_id)
    value = request.form.get('status')
    if value in ('none','requested','suspended'):
        d.suspension_status = value
        db.session.commit()
        flash('Suspension tracking status updated. This does not contact TZONE.', 'success')
    return redirect(url_for('index',tab='suspensions'))

@app.route('/devices/<int:device_id>/edit', methods=['POST'])
@login_required
def edit(device_id):
    d = db.get_or_404(Device,device_id)
    try:
        cid = int(request.form['customer_id'])
        price = float(request.form.get('subscription_price') or 0)
        if not db.session.get(Customer,cid) or price < 0 or price > 1000000: raise ValueError()
        d.customer_id = cid
        d.subscription_price = price
        d.notes = request.form.get('notes','').strip()
        db.session.commit()
        flash('Device updated.', 'success')
    except (ValueError,KeyError):
        flash('Invalid device details.', 'error')
    return redirect(url_for('index',tab='devices'))

if __name__ == '__main__':
    app.run(debug=os.getenv('FLASK_DEBUG')=='1')
