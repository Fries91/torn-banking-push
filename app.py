import os
import re
import json
import uuid
import sqlite3
import threading
from datetime import datetime, timedelta, timezone

import requests
from flask import Flask, request, jsonify, render_template
from flask_cors import CORS
from pywebpush import webpush, WebPushException

APP_NAME = os.getenv('APP_NAME', 'Banker Push Premium')
BASE_URL = os.getenv('BASE_URL', 'http://localhost:5000').rstrip('/')
DB_PATH = os.getenv('DATABASE_PATH', 'data/torn_banking_push.sqlite3')
VAPID_PUBLIC_KEY = os.getenv('VAPID_PUBLIC_KEY', '')
VAPID_PRIVATE_KEY = os.getenv('VAPID_PRIVATE_KEY', '')
VAPID_SUBJECT = os.getenv('VAPID_SUBJECT', 'mailto:fries91@example.com')
ADMIN_PASSWORD = os.getenv('ADMIN_PASSWORD', 'change-me-now')
PREMIUM_KEY_PRICE_XANAX_30_DAYS = int(os.getenv('PREMIUM_KEY_PRICE_XANAX_30_DAYS', os.getenv('XANAX_PER_BANKER_KEY_30_DAYS', '2')))
MAX_PAYMENT_MONTHS = int(os.getenv('MAX_PAYMENT_MONTHS', '4'))
FRIES91_TORN_API_KEY = os.getenv('FRIES91_TORN_API_KEY', '')
AUTO_SCAN_PAYMENTS = os.getenv('AUTO_SCAN_PAYMENTS', 'false').lower() in ('1', 'true', 'yes', 'on')
PAYMENT_ITEM_NAME = os.getenv('PAYMENT_ITEM_NAME', 'Xanax')
SCAN_INTERVAL_SECONDS = int(os.getenv('SCAN_INTERVAL_SECONDS', '300'))

app = Flask(__name__)
CORS(app)
_scan_thread_started = False
_scan_lock = threading.Lock()


def utcnow():
    return datetime.now(timezone.utc)


def iso(dt):
    if not dt:
        return None
    if isinstance(dt, str):
        return dt
    return dt.astimezone(timezone.utc).replace(microsecond=0).isoformat().replace('+00:00', 'Z')


def parse_dt(value):
    if not value:
        return None
    if isinstance(value, datetime):
        return value
    return datetime.fromisoformat(value.replace('Z', '+00:00'))


def ensure_db_dir():
    folder = os.path.dirname(DB_PATH)
    if folder:
        os.makedirs(folder, exist_ok=True)


def db():
    ensure_db_dir()
    con = sqlite3.connect(DB_PATH)
    con.row_factory = sqlite3.Row
    return con


def init_db():
    with db() as con:
        con.executescript('''
        CREATE TABLE IF NOT EXISTS premium_keys (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            premium_key TEXT UNIQUE NOT NULL,
            owner_name TEXT NOT NULL,
            owner_torn_id TEXT,
            key_label TEXT,
            is_active INTEGER NOT NULL DEFAULT 0,
            paid_until TEXT NOT NULL,
            created_at TEXT NOT NULL,
            last_used TEXT
        );
        CREATE TABLE IF NOT EXISTS devices (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            premium_key TEXT NOT NULL,
            device_name TEXT,
            subscription_json TEXT NOT NULL,
            is_enabled INTEGER NOT NULL DEFAULT 1,
            created_at TEXT NOT NULL,
            last_seen TEXT NOT NULL,
            UNIQUE(premium_key, subscription_json),
            FOREIGN KEY(premium_key) REFERENCES premium_keys(premium_key)
        );
        CREATE TABLE IF NOT EXISTS payment_claims (
            id TEXT PRIMARY KEY,
            premium_key TEXT,
            claimed_by TEXT NOT NULL,
            claimed_by_torn_id TEXT,
            xanax_amount INTEGER NOT NULL,
            proof_text TEXT,
            status TEXT NOT NULL DEFAULT 'pending',
            created_at TEXT NOT NULL,
            reviewed_at TEXT,
            reviewed_by TEXT,
            admin_note TEXT,
            days_added INTEGER DEFAULT 0,
            key_created INTEGER DEFAULT 0,
            created_key TEXT
        );
        CREATE TABLE IF NOT EXISTS processed_payment_events (
            event_id TEXT PRIMARY KEY,
            sender_name TEXT,
            sender_torn_id TEXT,
            xanax_amount INTEGER NOT NULL,
            event_text TEXT,
            event_time TEXT,
            premium_key TEXT,
            days_added INTEGER DEFAULT 0,
            processed_at TEXT NOT NULL
        );
        CREATE TABLE IF NOT EXISTS scan_runs (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            ran_at TEXT NOT NULL,
            ok INTEGER NOT NULL,
            events_seen INTEGER DEFAULT 0,
            payments_found INTEGER DEFAULT 0,
            payments_activated INTEGER DEFAULT 0,
            message TEXT
        );
        CREATE TABLE IF NOT EXISTS alerts (
            id TEXT PRIMARY KEY,
            premium_key TEXT NOT NULL,
            title TEXT NOT NULL,
            body TEXT NOT NULL,
            amount TEXT,
            requester_name TEXT,
            status TEXT NOT NULL DEFAULT 'active',
            created_at TEXT NOT NULL,
            data_json TEXT
        );
        CREATE INDEX IF NOT EXISTS idx_premium_keys_key ON premium_keys(premium_key);
        CREATE INDEX IF NOT EXISTS idx_premium_keys_owner ON premium_keys(owner_torn_id);
        CREATE INDEX IF NOT EXISTS idx_claims_status ON payment_claims(status);
        CREATE INDEX IF NOT EXISTS idx_devices_key ON devices(premium_key, is_enabled);
        ''')
        # migrations for older v11 DBs
        cols = {r['name'] for r in con.execute('PRAGMA table_info(premium_keys)').fetchall()}
        if 'is_active' in cols:
            pass


@app.before_request
def _boot():
    init_db()
    maybe_start_scan_thread()


def row_to_dict(row):
    return dict(row) if row else None


def new_premium_key():
    return 'premium_' + uuid.uuid4().hex[:28]


def get_key(token):
    if not token:
        return None
    with db() as con:
        return con.execute('SELECT * FROM premium_keys WHERE premium_key=?', (token,)).fetchone()


def clean_torn_id(value):
    return re.sub(r'\D+', '', str(value or '').strip())


def get_key_by_torn_id(torn_id):
    torn_id = clean_torn_id(torn_id)
    if not torn_id:
        return None
    with db() as con:
        return con.execute('''
            SELECT * FROM premium_keys
            WHERE REPLACE(REPLACE(REPLACE(owner_torn_id, ' ', ''), '#', ''), '[', '')=?
               OR owner_torn_id=?
            ORDER BY created_at DESC LIMIT 1
        ''', (torn_id, torn_id)).fetchone()


def find_existing_key(owner_name='', owner_torn_id='', premium_key=''):
    owner_name = (owner_name or '').strip()
    owner_torn_id = clean_torn_id(owner_torn_id)
    premium_key = (premium_key or '').strip()

    with db() as con:
        if premium_key:
            row = con.execute(
                'SELECT * FROM premium_keys WHERE premium_key=?',
                (premium_key,)
            ).fetchone()
            if row:
                return row

        if owner_torn_id:
            row = con.execute('''
                SELECT * FROM premium_keys
                WHERE REPLACE(REPLACE(REPLACE(REPLACE(owner_torn_id, ' ', ''), '#', ''), '[', ''), ']', '')=?
                   OR owner_torn_id=?
                ORDER BY created_at DESC
                LIMIT 1
            ''', (owner_torn_id, owner_torn_id)).fetchone()
            if row:
                return row

        if owner_name:
            row = con.execute('''
                SELECT * FROM premium_keys
                WHERE LOWER(TRIM(owner_name))=LOWER(TRIM(?))
                ORDER BY created_at DESC
                LIMIT 1
            ''', (owner_name,)).fetchone()
            if row:
                return row

    return None


def key_is_live(row):
    if not row:
        return False
    if not bool(row['is_active']):
        return False
    return parse_dt(row['paid_until']) > utcnow()



def enabled_device_count(token):
    with db() as con:
        return con.execute('SELECT COUNT(*) c FROM devices WHERE premium_key=? AND is_enabled=1', (token,)).fetchone()['c']


def total_device_count(token):
    with db() as con:
        return con.execute('SELECT COUNT(*) c FROM devices WHERE premium_key=?', (token,)).fetchone()['c']

def public_key(row):
    if not row:
        return None
    paid_until = parse_dt(row['paid_until'])
    seconds_left = max(0, int((paid_until - utcnow()).total_seconds()))
    devices = enabled_device_count(row['premium_key'])
    return {
        'premium_key': row['premium_key'],
        'owner_name': row['owner_name'],
        'owner_torn_id': row['owner_torn_id'],
        'key_label': row['key_label'],
        'is_active': bool(row['is_active']),
        'paid_until': row['paid_until'],
        'seconds_left': seconds_left,
        'locked': not key_is_live(row),
        'enabled_devices': devices,
        'price_xanax_30_days': PREMIUM_KEY_PRICE_XANAX_30_DAYS,
        'allowed_payment_amounts': [PREMIUM_KEY_PRICE_XANAX_30_DAYS * m for m in range(1, MAX_PAYMENT_MONTHS + 1)],
        'created_at': row['created_at'],
        'last_used': row['last_used'],
    }


def months_from_amount(amount):
    try:
        amount = int(amount)
    except Exception:
        return 0
    if amount <= 0:
        return 0
    if amount % PREMIUM_KEY_PRICE_XANAX_30_DAYS != 0:
        return 0
    months = amount // PREMIUM_KEY_PRICE_XANAX_30_DAYS
    if months < 1 or months > MAX_PAYMENT_MONTHS:
        return 0
    return months


def require_admin():
    data = request.get_json(silent=True) or {}
    password = request.headers.get('X-Admin-Password') or data.get('admin_password')
    return password == ADMIN_PASSWORD


def auto_scan_payments_safe():
    """Run payment scan without breaking normal user actions."""
    if not FRIES91_TORN_API_KEY:
        return {'ok': False, 'skipped': True, 'message': 'FRIES91_TORN_API_KEY is not set in Render.'}
    try:
        return scan_payments_once()
    except Exception as exc:
        return {'ok': False, 'skipped': False, 'message': str(exc)}


@app.route('/')
def index():
    return render_template('index.html', app_name=APP_NAME, vapid_public_key=VAPID_PUBLIC_KEY, base_url=BASE_URL)


@app.route('/api/config', methods=['GET'])
def config():
    return jsonify({
        'ok': True,
        'app_name': APP_NAME,
        'vapid_public_key': VAPID_PUBLIC_KEY,
        'price_xanax_30_days': PREMIUM_KEY_PRICE_XANAX_30_DAYS,
        'allowed_payment_amounts': [PREMIUM_KEY_PRICE_XANAX_30_DAYS * m for m in range(1, MAX_PAYMENT_MONTHS + 1)],
        'max_payment_months': MAX_PAYMENT_MONTHS,
        'auto_scan_payments': AUTO_SCAN_PAYMENTS,
        'scan_interval_seconds': SCAN_INTERVAL_SECONDS,
    })


@app.route('/api/key/request', methods=['POST'])
def request_key():
    data = request.get_json(force=True)
    owner_name = (data.get('owner_name') or '').strip()
    owner_torn_id = clean_torn_id(data.get('owner_torn_id'))

    if not owner_name:
        return jsonify({'ok': False, 'error': 'Enter your Torn name.'}), 400
    if not owner_torn_id:
        return jsonify({'ok': False, 'error': 'Enter your Torn ID. Auto payment scan needs this to match your item-send event.'}), 400

    now = utcnow()
    with db() as con:
        existing = find_existing_key(owner_name=owner_name, owner_torn_id=owner_torn_id)
        if existing:
            con.execute('''UPDATE premium_keys SET owner_name=?, owner_torn_id=? WHERE premium_key=?''', (owner_name, owner_torn_id, existing['premium_key']))
            row = con.execute('SELECT * FROM premium_keys WHERE premium_key=?', (existing['premium_key'],)).fetchone()
        else:
            token = new_premium_key()
            con.execute('''INSERT INTO premium_keys
                (premium_key, owner_name, owner_torn_id, key_label, is_active, paid_until, created_at)
                VALUES (?, ?, ?, ?, 0, ?, ?)''', (token, owner_name, owner_torn_id, 'Premium banker ping key', iso(now), iso(now)))
            row = con.execute('SELECT * FROM premium_keys WHERE premium_key=?', (token,)).fetchone()

    scan_result = auto_scan_payments_safe()
    row = find_existing_key(owner_name=owner_name, owner_torn_id=owner_torn_id, premium_key=row['premium_key'])
    return jsonify({'ok': True, 'message': 'Premium key found/reserved. If payment was already sent, the app scanned Fries91 events and activated/extended it when matched.', 'key': public_key(row), 'scan': scan_result})

@app.route('/api/payments/claim', methods=['POST'])
def payment_claim():
    data = request.get_json(force=True)
    amount = int(data.get('xanax_amount') or 0)
    months = months_from_amount(amount)
    if not months:
        return jsonify({'ok': False, 'error': f'Payment must be one of: {", ".join(map(str, [PREMIUM_KEY_PRICE_XANAX_30_DAYS*m for m in range(1, MAX_PAYMENT_MONTHS+1)]))} Xanax.'}), 400

    existing_key = (data.get('premium_key') or '').strip()
    claimed_by = (data.get('claimed_by') or '').strip() or 'Unknown'
    claimed_by_torn_id = clean_torn_id(data.get('claimed_by_torn_id'))
    row = find_existing_key(owner_name=claimed_by, owner_torn_id=claimed_by_torn_id, premium_key=existing_key)

    scan_result = auto_scan_payments_safe()
    row = find_existing_key(owner_name=claimed_by, owner_torn_id=claimed_by_torn_id, premium_key=existing_key)
    if row and key_is_live(row):
        return jsonify({'ok': True, 'message': 'Payment was found by auto-scan and the key is active.', 'key': public_key(row), 'scan': scan_result})
    if existing_key and not row:
        return jsonify({'ok': False, 'error': 'That premium key was not found. Use Get/reserve key first, or enter your Torn ID so the app can find the right key.', 'scan': scan_result}), 404

    claim_id = 'claim_' + uuid.uuid4().hex[:18]
    now = iso(utcnow())
    with db() as con:
        con.execute('''INSERT INTO payment_claims
            (id, premium_key, claimed_by, claimed_by_torn_id, xanax_amount, proof_text, status, created_at)
            VALUES (?, ?, ?, ?, ?, ?, 'pending', ?)''', (claim_id, row['premium_key'] if row else (existing_key or None), claimed_by, claimed_by_torn_id, amount, (data.get('proof_text') or '').strip(), now))
    return jsonify({'ok': True, 'claim_id': claim_id, 'status': 'pending', 'months_requested': months, 'message': 'Auto-scan did not find the payment yet, so a manual claim was saved. Try Scan/Check again in a minute.', 'scan': scan_result, 'key': public_key(row) if row else None})

@app.route('/api/payments/status', methods=['POST'])
def payment_status():
    data = request.get_json(force=True)
    claim_id = (data.get('claim_id') or '').strip()
    with db() as con:
        row = con.execute('SELECT * FROM payment_claims WHERE id=?', (claim_id,)).fetchone()
    if not row:
        return jsonify({'ok': False, 'error': 'Claim not found'}), 404
    return jsonify({'ok': True, 'claim': row_to_dict(row)})


@app.route('/api/key/status', methods=['POST'])
def key_status():
    data = request.get_json(force=True)
    token = (data.get('premium_key') or data.get('api_token') or '').strip()
    row = get_key(token)
    if not row:
        return jsonify({'ok': False, 'error': 'Premium key not found'}), 404
    scan_result = auto_scan_payments_safe()
    row = get_key(token)
    return jsonify({'ok': True, 'key': public_key(row), 'scan': scan_result})


@app.route('/api/key/find', methods=['POST'])
def key_find():
    data = request.get_json(force=True)
    torn_id = clean_torn_id(data.get('owner_torn_id') or data.get('torn_id'))
    owner_name = (data.get('owner_name') or data.get('name') or '').strip()
    premium_key = (data.get('premium_key') or data.get('key') or '').strip()
    scan_result = auto_scan_payments_safe()
    row = find_existing_key(owner_name=owner_name, owner_torn_id=torn_id, premium_key=premium_key)
    if not row:
        return jsonify({'ok': False, 'error': 'No premium key found. Try Get/reserve premium key again with your Torn name and Torn ID.', 'scan': scan_result}), 404
    return jsonify({'ok': True, 'message': 'Premium key found. Auto-scan ran while checking.', 'key': public_key(row), 'scan': scan_result})


@app.route('/api/key/scan-my-payment', methods=['POST'])
def key_scan_my_payment():
    data = request.get_json(force=True)
    token = (data.get('premium_key') or '').strip()
    torn_id = clean_torn_id(data.get('owner_torn_id') or data.get('torn_id'))
    owner_name = (data.get('owner_name') or data.get('name') or '').strip()
    scan_result = auto_scan_payments_safe()
    row = find_existing_key(owner_name=owner_name, owner_torn_id=torn_id, premium_key=token)
    return jsonify({'ok': True, 'message': 'Scan finished. If a matching payment was found, your key is now active/extended.', 'key': public_key(row) if row else None, 'scan': scan_result})

@app.route('/api/devices/register', methods=['POST'])
def device_register():
    data = request.get_json(force=True)
    token = (data.get('premium_key') or data.get('api_token') or '').strip()
    row = get_key(token)
    if not row:
        return jsonify({'ok': False, 'error': 'Premium key not found'}), 404
    if not key_is_live(row):
        return jsonify({'ok': False, 'error': 'Premium key is locked or expired. Send payment renewal first.'}), 402

    sub = data.get('subscription')
    if not sub:
        return jsonify({'ok': False, 'error': 'Missing push subscription. Browser did not give the app a push token.'}), 400

    sub_json = json.dumps(sub, sort_keys=True)
    now = iso(utcnow())

    with db() as con:
        existing = con.execute('SELECT id, created_at FROM devices WHERE premium_key=? AND subscription_json=?', (token, sub_json)).fetchone()
        if existing:
            con.execute('UPDATE devices SET device_name=?, is_enabled=1, last_seen=? WHERE id=?',
                        ((data.get('device_name') or row['owner_name'] or 'Phone').strip(), now, existing['id']))
            device_id = existing['id']
            action = 'updated_existing_phone'
        else:
            cur = con.execute('''INSERT INTO devices
                (premium_key, device_name, subscription_json, is_enabled, created_at, last_seen)
                VALUES (?, ?, ?, 1, ?, ?)''',
                (token, (data.get('device_name') or row['owner_name'] or 'Phone').strip(), sub_json, now, now))
            device_id = cur.lastrowid
            action = 'registered_new_phone'
        con.commit()

    refreshed = get_key(token)
    return jsonify({
        'ok': True,
        'message': 'Phone alerts are connected. Now send a test alert.',
        'action': action,
        'device_id': device_id,
        'enabled_devices': enabled_device_count(token),
        'total_devices': total_device_count(token),
        'key': public_key(refreshed)
    })


@app.route('/api/devices/toggle', methods=['POST'])
def device_toggle():
    data = request.get_json(force=True)
    token = (data.get('premium_key') or data.get('api_token') or '').strip()
    row = get_key(token)
    if not row:
        return jsonify({'ok': False, 'error': 'Premium key not found'}), 404

    if total_device_count(token) <= 0:
        return jsonify({'ok': False, 'error': 'No phone is registered to this key yet. Press Enable phone alerts first and allow notifications.'}), 404

    enabled = 1 if bool(data.get('enabled')) else 0
    with db() as con:
        con.execute('UPDATE devices SET is_enabled=?, last_seen=? WHERE premium_key=?', (enabled, iso(utcnow()), token))
        con.commit()

    return jsonify({
        'ok': True,
        'enabled': bool(enabled),
        'enabled_devices': enabled_device_count(token),
        'total_devices': total_device_count(token),
        'key': public_key(get_key(token))
    })


def activate_or_extend_key(owner_name, owner_torn_id, amount, source='auto-scan'):
    months = months_from_amount(amount)
    if not months:
        return None, 0, False
    days = 30 * months
    now = utcnow()
    with db() as con:
        row = con.execute('SELECT * FROM premium_keys WHERE owner_torn_id=? ORDER BY created_at DESC LIMIT 1', (owner_torn_id,)).fetchone()
        created = False
        if not row:
            token = new_premium_key()
            created = True
            new_until = now + timedelta(days=days)
            con.execute('''INSERT INTO premium_keys
                (premium_key, owner_name, owner_torn_id, key_label, is_active, paid_until, created_at)
                VALUES (?, ?, ?, ?, 1, ?, ?)''', (
                token, owner_name or f'Torn {owner_torn_id}', owner_torn_id, 'Premium banker ping key', iso(new_until), iso(now)
            ))
        else:
            token = row['premium_key']
            start = max(parse_dt(row['paid_until']), now)
            new_until = start + timedelta(days=days)
            con.execute('UPDATE premium_keys SET paid_until=?, is_active=1, owner_name=COALESCE(NULLIF(?, ""), owner_name) WHERE premium_key=?', (iso(new_until), owner_name or '', token))
        key_row = con.execute('SELECT * FROM premium_keys WHERE premium_key=?', (token,)).fetchone()
    return key_row, days, created


def send_push_to_subscription(subscription_json, title, body, data):
    subscription = json.loads(subscription_json)
    webpush(
        subscription_info=subscription,
        data=json.dumps({'title': title, 'body': body, 'data': data}),
        vapid_private_key=VAPID_PRIVATE_KEY,
        vapid_claims={'sub': VAPID_SUBJECT},
    )


def send_to_key(token, title, body, data):
    row = get_key(token)
    if not row:
        return {'ok': False, 'error': 'Premium key not found', 'status': 404}
    if not key_is_live(row):
        return {'ok': False, 'error': 'Premium key is locked or expired', 'status': 402}

    with db() as con:
        devices = con.execute('SELECT * FROM devices WHERE premium_key=? AND is_enabled=1', (token,)).fetchall()
        con.execute('UPDATE premium_keys SET last_used=? WHERE premium_key=?', (iso(utcnow()), token))
        con.commit()

    if not devices:
        return {
            'ok': True,
            'sent': 0,
            'failed': 0,
            'warning': 'No enabled phone devices are registered to this key yet. Press Enable phone alerts first.',
            'key': public_key(get_key(token))
        }

    sent = 0
    failed = 0
    errors = []
    for d in devices:
        try:
            send_push_to_subscription(d['subscription_json'], title, body, data)
            sent += 1
        except WebPushException as exc:
            failed += 1
            errors.append(str(exc)[:250])
        except Exception as exc:
            failed += 1
            errors.append(str(exc)[:250])

    return {'ok': True, 'sent': sent, 'failed': failed, 'errors': errors[:3], 'key': public_key(get_key(token))}


@app.route('/api/banking/request', methods=['POST'])
def banking_request():
    data = request.get_json(force=True)
    token = (data.get('premium_key') or data.get('api_token') or '').strip()
    requester = (data.get('requester_name') or 'Bank request').strip()
    amount = (data.get('amount') or '').strip()
    note = (data.get('note') or '').strip()
    title = data.get('title') or '🪙 Banking request'
    body = data.get('body') or f'{requester} requested {amount}'.strip()
    if note:
        body += f' — {note}'
    alert_id = 'alert_' + uuid.uuid4().hex[:18]
    payload_data = {'alert_id': alert_id, 'url': data.get('url') or BASE_URL, 'requester_name': requester, 'amount': amount}
    result = send_to_key(token, title, body, payload_data)
    if not result.get('ok'):
        return jsonify({k: v for k, v in result.items() if k != 'status'}), result.get('status', 400)
    with db() as con:
        con.execute('''INSERT INTO alerts (id, premium_key, title, body, amount, requester_name, status, created_at, data_json)
                       VALUES (?, ?, ?, ?, ?, ?, 'active', ?, ?)''',
                    (alert_id, token, title, body, amount, requester, iso(utcnow()), json.dumps(payload_data)))
    return jsonify({'ok': True, 'alert_id': alert_id, **result})


def fetch_torn_events():
    if not FRIES91_TORN_API_KEY:
        raise RuntimeError('Missing FRIES91_TORN_API_KEY in Render environment.')
    urls = [
        'https://api.torn.com/user/?selections=events&key=' + FRIES91_TORN_API_KEY,
        'https://api.torn.com/v2/user/events?key=' + FRIES91_TORN_API_KEY,
    ]
    last_error = None
    for url in urls:
        try:
            r = requests.get(url, timeout=20)
            data = r.json()
            if isinstance(data, dict) and data.get('error'):
                last_error = data['error']
                continue
            return normalize_events(data)
        except Exception as exc:
            last_error = str(exc)
    raise RuntimeError(f'Could not fetch Torn events: {last_error}')


def normalize_events(data):
    events = []
    raw = data.get('events') if isinstance(data, dict) else None

    def make_event(event_id, val):
        if isinstance(val, dict):
            text = (val.get('event') or val.get('message') or val.get('text') or val.get('description') or val.get('log') or json.dumps(val))
            ts = val.get('timestamp') or val.get('time') or val.get('created_at') or val.get('created')
            sender_id = val.get('sender_id') or val.get('user_id') or val.get('player_id') or val.get('from_id')
            sender_name = val.get('sender_name') or val.get('user_name') or val.get('player_name') or val.get('from_name')
        else:
            text, ts, sender_id, sender_name = str(val), None, None, None
        return {'event_id': str(event_id), 'text': text, 'timestamp': ts, 'sender_id': str(sender_id) if sender_id else None, 'sender_name': str(sender_name).strip() if sender_name else None, 'raw': val if isinstance(val, dict) else None}

    if isinstance(raw, dict):
        for event_id, val in raw.items():
            events.append(make_event(event_id, val))
    elif isinstance(raw, list):
        for idx, val in enumerate(raw):
            event_id = (val.get('id') or val.get('event_id') or val.get('timestamp') or idx) if isinstance(val, dict) else idx
            events.append(make_event(event_id, val))
    return events


def parse_payment_event(text, event=None):
    event = event or {}
    t = text or ''
    item = re.escape(PAYMENT_ITEM_NAME)
    if PAYMENT_ITEM_NAME.lower() not in t.lower():
        return None
    if not re.search(r'\b(sent|send|given|gave|received|receive|item|items|xanax)\b', t, re.I):
        return None

    amount = None
    patterns = [rf'(\d+)\s*x\s*{item}', rf'(\d+)\s*×\s*{item}', rf'(\d+)\s+{item}', rf'{item}\s*x\s*(\d+)', rf'{item}\s*×\s*(\d+)', rf'{item}[^0-9]{{0,20}}(\d+)']
    for ptn in patterns:
        m = re.search(ptn, t, re.I)
        if m:
            amount = int(m.group(1))
            break
    if not amount or not months_from_amount(amount):
        return None

    sender_id = clean_torn_id(event.get('sender_id')) if event.get('sender_id') else None
    sender_name = event.get('sender_name')
    ids = re.findall(r'\[(\d{3,10})\]', t)
    if not sender_id and ids:
        sender_id = ids[0]
    if not sender_name:
        for ptn in [r'from\s+([^\[]+?)\s*\[\d+\]', r'([^\[]+?)\s*\[\d+\].*?\b(sent|gave|given)\b', r'You were sent.*?from\s+([^\.]+)', r'You received.*?from\s+([^\.]+)', r'([^\.]+?)\s+sent\s+you', r'([^\.]+?)\s+gave\s+you']:
            m = re.search(ptn, t, re.I)
            if m:
                sender_name = re.sub(r'\s+', ' ', m.group(1)).strip(' .:-')
                break
    if not sender_id:
        return None
    return {'sender_torn_id': sender_id, 'sender_name': sender_name or f'Torn {sender_id}', 'amount': amount}


def scan_payments_once():
    now = iso(utcnow())
    events_seen = payments_found = payments_activated = 0
    message = 'ok'
    ok = True
    results = []
    with _scan_lock:
        try:
            events = fetch_torn_events()
            events_seen = len(events)
            with db() as con:
                for ev in events:
                    event_id = str(ev.get('event_id'))
                    if con.execute('SELECT 1 FROM processed_payment_events WHERE event_id=?', (event_id,)).fetchone():
                        continue
                    parsed = parse_payment_event(ev.get('text', ''), ev)
                    if not parsed:
                        continue
                    payments_found += 1
                    key_row, days, created = activate_or_extend_key(parsed['sender_name'], parsed['sender_torn_id'], parsed['amount'], source='auto-scan')
                    if key_row:
                        payments_activated += 1
                        con.execute('''INSERT INTO processed_payment_events
                            (event_id, sender_name, sender_torn_id, xanax_amount, event_text, event_time, premium_key, days_added, processed_at)
                            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)''', (event_id, parsed['sender_name'], parsed['sender_torn_id'], parsed['amount'], ev.get('text', ''), str(ev.get('timestamp') or ''), key_row['premium_key'], days, now))
                        claim_id = 'auto_' + uuid.uuid4().hex[:18]
                        con.execute('''INSERT INTO payment_claims
                            (id, premium_key, claimed_by, claimed_by_torn_id, xanax_amount, proof_text, status, created_at, reviewed_at, reviewed_by, admin_note, days_added, key_created, created_key)
                            VALUES (?, ?, ?, ?, ?, ?, 'approved', ?, ?, 'auto-scan', ?, ?, ?, ?)''', (claim_id, key_row['premium_key'], parsed['sender_name'], parsed['sender_torn_id'], parsed['amount'], ev.get('text', ''), now, now, f'Auto approved from Torn event {event_id}', days, 1 if created else 0, key_row['premium_key']))
                        results.append({'event_id': event_id, 'sender': parsed['sender_name'], 'torn_id': parsed['sender_torn_id'], 'amount': parsed['amount'], 'premium_key': key_row['premium_key'], 'days_added': days})
                con.execute('INSERT INTO scan_runs (ran_at, ok, events_seen, payments_found, payments_activated, message) VALUES (?, 1, ?, ?, ?, ?)', (now, events_seen, payments_found, payments_activated, message))
        except Exception as exc:
            ok = False
            message = str(exc)
            with db() as con:
                con.execute('INSERT INTO scan_runs (ran_at, ok, events_seen, payments_found, payments_activated, message) VALUES (?, 0, ?, ?, ?, ?)', (now, events_seen, payments_found, payments_activated, message))
    return {'ok': ok, 'events_seen': events_seen, 'payments_found': payments_found, 'payments_activated': payments_activated, 'message': message, 'activated': results}

def scanner_loop():
    import time
    while True:
        try:
            if AUTO_SCAN_PAYMENTS and FRIES91_TORN_API_KEY:
                scan_payments_once()
        except Exception:
            pass
        time.sleep(max(60, SCAN_INTERVAL_SECONDS))


def maybe_start_scan_thread():
    global _scan_thread_started
    if _scan_thread_started or not AUTO_SCAN_PAYMENTS:
        return
    _scan_thread_started = True
    t = threading.Thread(target=scanner_loop, daemon=True)
    t.start()


@app.route('/api/admin/scan-payments', methods=['POST'])
def admin_scan_payments():
    if not require_admin():
        return jsonify({'ok': False, 'error': 'Unauthorized'}), 401
    return jsonify(scan_payments_once())


@app.route('/api/admin/scan-status', methods=['POST'])
def admin_scan_status():
    if not require_admin():
        return jsonify({'ok': False, 'error': 'Unauthorized'}), 401
    with db() as con:
        runs = con.execute('SELECT * FROM scan_runs ORDER BY ran_at DESC LIMIT 10').fetchall()
        events = con.execute('SELECT * FROM processed_payment_events ORDER BY processed_at DESC LIMIT 25').fetchall()
    return jsonify({'ok': True, 'auto_scan_payments': AUTO_SCAN_PAYMENTS, 'scan_interval_seconds': SCAN_INTERVAL_SECONDS, 'has_fries91_key': bool(FRIES91_TORN_API_KEY), 'runs': [row_to_dict(r) for r in runs], 'processed_events': [row_to_dict(e) for e in events]})


@app.route('/api/admin/payment/pending', methods=['POST'])
def admin_pending():
    if not require_admin():
        return jsonify({'ok': False, 'error': 'Unauthorized'}), 401
    with db() as con:
        rows = con.execute('SELECT * FROM payment_claims WHERE status="pending" ORDER BY created_at ASC').fetchall()
    return jsonify({'ok': True, 'claims': [row_to_dict(r) for r in rows]})


@app.route('/api/admin/payment/approve', methods=['POST'])
def admin_approve():
    if not require_admin():
        return jsonify({'ok': False, 'error': 'Unauthorized'}), 401
    data = request.get_json(force=True)
    claim_id = (data.get('claim_id') or '').strip()
    with db() as con:
        claim = con.execute('SELECT * FROM payment_claims WHERE id=?', (claim_id,)).fetchone()
        if not claim:
            return jsonify({'ok': False, 'error': 'Claim not found'}), 404
        if claim['status'] != 'pending':
            return jsonify({'ok': False, 'error': 'Claim already reviewed'}), 400
        months = months_from_amount(claim['xanax_amount'])
        if not months:
            return jsonify({'ok': False, 'error': 'Invalid payment amount on claim'}), 400
    key_row, days, created = activate_or_extend_key(claim['claimed_by'], claim['claimed_by_torn_id'], claim['xanax_amount'], source='manual')
    now = iso(utcnow())
    with db() as con:
        con.execute('''UPDATE payment_claims SET status='approved', reviewed_at=?, reviewed_by=?, admin_note=?, days_added=?, key_created=?, created_key=? WHERE id=?''',
                    (now, data.get('reviewed_by') or 'Fries91', data.get('admin_note') or '', days, 1 if created else 0, key_row['premium_key'], claim_id))
    return jsonify({'ok': True, 'message': 'Payment approved.', 'claim_id': claim_id, 'premium_key': key_row['premium_key'], 'days_added': days, 'key': public_key(key_row)})


@app.route('/api/admin/payment/reject', methods=['POST'])
def admin_reject():
    if not require_admin():
        return jsonify({'ok': False, 'error': 'Unauthorized'}), 401
    data = request.get_json(force=True)
    claim_id = (data.get('claim_id') or '').strip()
    with db() as con:
        con.execute('''UPDATE payment_claims SET status='rejected', reviewed_at=?, reviewed_by=?, admin_note=? WHERE id=? AND status='pending' ''',
                    (iso(utcnow()), data.get('reviewed_by') or 'Fries91', data.get('admin_note') or 'Could not verify payment.', claim_id))
    return jsonify({'ok': True, 'claim_id': claim_id})


@app.route('/api/admin/keys', methods=['POST'])
def admin_keys():
    if not require_admin():
        return jsonify({'ok': False, 'error': 'Unauthorized'}), 401
    with db() as con:
        rows = con.execute('SELECT * FROM premium_keys ORDER BY paid_until DESC').fetchall()
    return jsonify({'ok': True, 'keys': [public_key(r) for r in rows]})


@app.route('/api/admin/key/toggle', methods=['POST'])
def admin_key_toggle():
    if not require_admin():
        return jsonify({'ok': False, 'error': 'Unauthorized'}), 401

    data = request.get_json(force=True)
    token = (data.get('premium_key') or '').strip()
    enabled = 1 if bool(data.get('enabled')) else 0

    row = get_key(token)
    if not row:
        return jsonify({'ok': False, 'error': 'Premium key not found'}), 404

    now = utcnow()

    with db() as con:
        if enabled:
            current_until = parse_dt(row['paid_until'])
            start = max(current_until, now)
            new_until = start + timedelta(days=30)

            con.execute(
                '''
                UPDATE premium_keys
                SET is_active=1, paid_until=?
                WHERE premium_key=?
                ''',
                (iso(new_until), token)
            )
        else:
            con.execute(
                '''
                UPDATE premium_keys
                SET is_active=0
                WHERE premium_key=?
                ''',
                (token,)
            )

        updated = con.execute(
            'SELECT * FROM premium_keys WHERE premium_key=?',
            (token,)
        ).fetchone()

    return jsonify({
        'ok': True,
        'message': 'Key activated for 30 days.' if enabled else 'Key locked.',
        'key': public_key(updated)
    })


if __name__ == '__main__':
    init_db()
    maybe_start_scan_thread()
    app.run(host='0.0.0.0', port=int(os.getenv('PORT', '5000')))
