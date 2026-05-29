import os
import json
import uuid
import sqlite3
from datetime import datetime, timedelta, timezone
from functools import wraps
from flask import Flask, request, jsonify, render_template
import requests
from flask_cors import CORS
from pywebpush import webpush, WebPushException

APP_NAME = os.getenv('APP_NAME', 'Torn Banking Push')
BASE_URL = os.getenv('BASE_URL', 'http://localhost:5000').rstrip('/')
DB_PATH = os.getenv('DATABASE_PATH', 'data/torn_banking_push.sqlite3')
VAPID_PUBLIC_KEY = os.getenv('VAPID_PUBLIC_KEY', '')
VAPID_PRIVATE_KEY = os.getenv('VAPID_PRIVATE_KEY', '')
VAPID_SUBJECT = os.getenv('VAPID_SUBJECT', 'mailto:fries91@example.com')
ADMIN_PASSWORD = os.getenv('ADMIN_PASSWORD', 'change-me-now')
TRIAL_DAYS = int(os.getenv('TRIAL_DAYS', '14'))
XANAX_PER_BANKER_KEY_30_DAYS = int(os.getenv('XANAX_PER_BANKER_KEY_30_DAYS', '2'))
MAX_PAYMENT_MONTHS = int(os.getenv('MAX_PAYMENT_MONTHS', '4'))
TORN_API_BASE = os.getenv('TORN_API_BASE', 'https://api.torn.com')

app = Flask(__name__)
CORS(app)


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
        CREATE TABLE IF NOT EXISTS factions (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            faction_id TEXT UNIQUE NOT NULL,
            faction_name TEXT NOT NULL,
            leader_name TEXT NOT NULL,
            leader_torn_id TEXT,
            leader_verified_at TEXT,
            api_token TEXT UNIQUE NOT NULL,
            created_at TEXT NOT NULL,
            trial_until TEXT NOT NULL,
            paid_until TEXT,
            locked INTEGER NOT NULL DEFAULT 0
        );
        CREATE TABLE IF NOT EXISTS devices (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            faction_id TEXT NOT NULL,
            torn_id TEXT,
            name TEXT NOT NULL,
            role TEXT NOT NULL,
            subscription_json TEXT NOT NULL,
            created_at TEXT NOT NULL,
            last_seen TEXT NOT NULL,
            UNIQUE(faction_id, torn_id, role),
            FOREIGN KEY(faction_id) REFERENCES factions(faction_id)
        );
        CREATE TABLE IF NOT EXISTS alerts (
            id TEXT PRIMARY KEY,
            faction_id TEXT NOT NULL,
            title TEXT NOT NULL,
            body TEXT NOT NULL,
            amount TEXT,
            requester_name TEXT,
            status TEXT NOT NULL DEFAULT 'active',
            completed_by TEXT,
            created_at TEXT NOT NULL,
            completed_at TEXT,
            data_json TEXT,
            FOREIGN KEY(faction_id) REFERENCES factions(faction_id)
        );
        CREATE TABLE IF NOT EXISTS payment_claims (
            id TEXT PRIMARY KEY,
            faction_id TEXT NOT NULL,
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
            FOREIGN KEY(faction_id) REFERENCES factions(faction_id)
        );
        CREATE TABLE IF NOT EXISTS banker_keys (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            faction_id TEXT NOT NULL,
            key_label TEXT,
            banker_name TEXT NOT NULL,
            banker_torn_id TEXT,
            banker_key TEXT UNIQUE NOT NULL,
            created_by TEXT,
            is_active INTEGER NOT NULL DEFAULT 1,
            created_at TEXT NOT NULL,
            last_used TEXT,
            FOREIGN KEY(faction_id) REFERENCES factions(faction_id)
        );
        CREATE INDEX IF NOT EXISTS idx_devices_faction_role ON devices(faction_id, role);
        CREATE INDEX IF NOT EXISTS idx_alerts_faction_status ON alerts(faction_id, status);
        CREATE INDEX IF NOT EXISTS idx_claims_status ON payment_claims(status);
        CREATE INDEX IF NOT EXISTS idx_banker_keys_faction ON banker_keys(faction_id, is_active);
        ''')
        cols = [r['name'] for r in con.execute('PRAGMA table_info(devices)').fetchall()]
        if 'banker_key' not in cols:
            con.execute('ALTER TABLE devices ADD COLUMN banker_key TEXT')
        claim_cols = [r['name'] for r in con.execute('PRAGMA table_info(payment_claims)').fetchall()]
        if 'active_banker_keys_at_claim' not in claim_cols:
            con.execute('ALTER TABLE payment_claims ADD COLUMN active_banker_keys_at_claim INTEGER DEFAULT 0')
        if 'monthly_cost_at_claim' not in claim_cols:
            con.execute('ALTER TABLE payment_claims ADD COLUMN monthly_cost_at_claim INTEGER DEFAULT 0')
        if 'months_paid' not in claim_cols:
            con.execute('ALTER TABLE payment_claims ADD COLUMN months_paid INTEGER DEFAULT 0')
        faction_cols = [r['name'] for r in con.execute('PRAGMA table_info(factions)').fetchall()]
        if 'leader_verified_at' not in faction_cols:
            con.execute('ALTER TABLE factions ADD COLUMN leader_verified_at TEXT')

        banker_cols = [r['name'] for r in con.execute('PRAGMA table_info(banker_keys)').fetchall()]
        if 'key_label' not in banker_cols:
            con.execute('ALTER TABLE banker_keys ADD COLUMN key_label TEXT')


@app.before_request
def _boot():
    init_db()


def count_active_banker_keys(faction_id):
    with db() as con:
        return con.execute('SELECT COUNT(*) c FROM banker_keys WHERE faction_id=? AND is_active=1', (faction_id,)).fetchone()['c']


def pricing_for_faction(faction_id):
    active_keys = count_active_banker_keys(faction_id)
    monthly_cost = active_keys * XANAX_PER_BANKER_KEY_30_DAYS
    allowed = [] if monthly_cost <= 0 else [monthly_cost * m for m in range(1, MAX_PAYMENT_MONTHS + 1)]
    return {
        'active_banker_keys': active_keys,
        'xanax_per_banker_key_30_days': XANAX_PER_BANKER_KEY_30_DAYS,
        'monthly_cost_xanax': monthly_cost,
        'allowed_payment_amounts': allowed,
        'max_payment_months': MAX_PAYMENT_MONTHS,
        'payment_rule': f'{XANAX_PER_BANKER_KEY_30_DAYS} Xanax per active banker key every 30 days',
    }


def public_faction(row):
    trial_until = parse_dt(row['trial_until'])
    paid_until = parse_dt(row['paid_until']) if row['paid_until'] else None
    active_until = max([d for d in [trial_until, paid_until] if d])
    now = utcnow()
    locked_by_time = active_until <= now
    locked = bool(row['locked']) or locked_by_time
    seconds_left = max(0, int((active_until - now).total_seconds()))
    return {
        'faction_id': row['faction_id'],
        'faction_name': row['faction_name'],
        'leader_name': row['leader_name'],
        'leader_torn_id': row['leader_torn_id'],
        'leader_verified_at': row['leader_verified_at'] if 'leader_verified_at' in row.keys() else None,
        'api_token': row['api_token'],
        'created_at': row['created_at'],
        'trial_until': row['trial_until'],
        'paid_until': row['paid_until'],
        'active_until': iso(active_until),
        'seconds_left': seconds_left,
        'locked': locked,
        **pricing_for_faction(row['faction_id']),
    }


def get_faction_by_token(token):
    with db() as con:
        return con.execute('SELECT * FROM factions WHERE api_token=?', (token,)).fetchone()


def get_banker_key(token):
    with db() as con:
        return con.execute('SELECT bk.*, f.faction_name, f.api_token, f.leader_name, f.trial_until, f.paid_until, f.locked, f.created_at, f.leader_torn_id FROM banker_keys bk JOIN factions f ON f.faction_id=bk.faction_id WHERE bk.banker_key=?', (token,)).fetchone()


def public_banker_key(row):
    return {
        'id': row['id'],
        'faction_id': row['faction_id'],
        'key_label': row['key_label'] if 'key_label' in row.keys() else None,
        'banker_name': row['banker_name'],
        'banker_torn_id': row['banker_torn_id'],
        'banker_key': row['banker_key'],
        'created_by': row['created_by'],
        'is_active': bool(row['is_active']),
        'created_at': row['created_at'],
        'last_used': row['last_used'],
    }


def torn_api_get(path, key, params=None):
    params = dict(params or {})
    params['key'] = key
    url = TORN_API_BASE.rstrip('/') + path
    res = requests.get(url, params=params, timeout=12)
    try:
        data = res.json()
    except Exception:
        return {'ok': False, 'error': 'torn_bad_json', 'status_code': res.status_code}
    if res.status_code >= 400 or data.get('error'):
        return {'ok': False, 'error': 'torn_api_error', 'details': data.get('error') or data, 'status_code': res.status_code}
    return {'ok': True, 'data': data}


def verify_torn_faction_leader(faction_id, leader_api_key):
    """Verify the supplied Torn API key belongs to the actual leader of this faction.
    The key is not stored; it is only used for this check.
    """
    if not leader_api_key:
        return {'ok': False, 'error': 'leader_torn_api_key_required'}
    profile = torn_api_get('/user/', leader_api_key, {'selections': 'profile'})
    if not profile.get('ok'):
        return profile
    data = profile['data']
    player_id = str(data.get('player_id') or data.get('id') or '').strip()
    name = str(data.get('name') or '').strip()
    faction = data.get('faction') or {}
    user_faction_id = str(faction.get('faction_id') or faction.get('id') or '').strip()
    position = str(faction.get('position') or '').strip()
    if user_faction_id != str(faction_id):
        return {'ok': False, 'error': 'api_key_not_for_this_faction', 'player_id': player_id, 'name': name, 'faction_id': user_faction_id}
    if position.lower() not in ['leader', 'faction leader']:
        return {'ok': False, 'error': 'api_key_owner_is_not_faction_leader', 'player_id': player_id, 'name': name, 'position': position}
    return {'ok': True, 'player_id': player_id, 'name': name, 'position': position, 'faction_id': user_faction_id}


def require_verified_leader_for_faction(row, leader_api_key):
    check = verify_torn_faction_leader(row['faction_id'], leader_api_key)
    if not check.get('ok'):
        return check
    now = iso(utcnow())
    with db() as con:
        con.execute('UPDATE factions SET leader_name=?, leader_torn_id=?, leader_verified_at=? WHERE faction_id=?',
                    (check.get('name') or row['leader_name'], check.get('player_id') or row['leader_torn_id'], now, row['faction_id']))
    return check


def require_admin(fn):
    @wraps(fn)
    def wrapper(*args, **kwargs):
        password = request.headers.get('X-Admin-Password') or request.json.get('admin_password') if request.is_json else request.headers.get('X-Admin-Password')
        if password != ADMIN_PASSWORD:
            return jsonify({'ok': False, 'error': 'admin_auth_failed'}), 401
        return fn(*args, **kwargs)
    return wrapper


@app.route('/')
def index():
    return render_template('index.html', app_name=APP_NAME, vapid_public_key=VAPID_PUBLIC_KEY, base_url=BASE_URL)


@app.get('/api/config')
def config():
    return jsonify({
        'ok': True,
        'app_name': APP_NAME,
        'base_url': BASE_URL,
        'vapid_public_key': VAPID_PUBLIC_KEY,
        'trial_days': TRIAL_DAYS,
        'xanax_per_banker_key_30_days': XANAX_PER_BANKER_KEY_30_DAYS,
        'max_payment_months': MAX_PAYMENT_MONTHS,
    })


@app.post('/api/factions/register')
def register_faction():
    data = request.get_json(force=True)
    faction_id = str(data.get('faction_id', '')).strip()
    faction_name = str(data.get('faction_name', '')).strip()
    leader_api_key = str(data.get('leader_api_key', '')).strip()
    if not faction_id or not faction_name or not leader_api_key:
        return jsonify({'ok': False, 'error': 'faction_id, faction_name, and leader_torn_api_key are required'}), 400

    leader_check = verify_torn_faction_leader(faction_id, leader_api_key)
    if not leader_check.get('ok'):
        return jsonify({'ok': False, 'error': 'leader_verification_failed', 'details': leader_check}), 403

    now = utcnow()
    token = 'tbp_' + uuid.uuid4().hex + uuid.uuid4().hex[:12]
    trial_until = now + timedelta(days=TRIAL_DAYS)
    leader_name = leader_check.get('name') or str(data.get('leader_name', '')).strip() or 'Faction Leader'
    leader_torn_id = leader_check.get('player_id') or str(data.get('leader_torn_id', '')).strip()
    with db() as con:
        existing = con.execute('SELECT * FROM factions WHERE faction_id=?', (faction_id,)).fetchone()
        if existing:
            con.execute('UPDATE factions SET leader_name=?, leader_torn_id=?, leader_verified_at=? WHERE faction_id=?',
                        (leader_name, leader_torn_id, iso(now), faction_id))
            existing = con.execute('SELECT * FROM factions WHERE faction_id=?', (faction_id,)).fetchone()
            return jsonify({'ok': True, 'faction': public_faction(existing), 'message': 'Leader verified. Existing faction key returned.'})
        con.execute("""INSERT INTO factions(faction_id, faction_name, leader_name, leader_torn_id, leader_verified_at, api_token, created_at, trial_until)
                       VALUES(?,?,?,?,?,?,?,?)""",
                    (faction_id, faction_name, leader_name, leader_torn_id, iso(now), token, iso(now), iso(trial_until)))
        row = con.execute('SELECT * FROM factions WHERE faction_id=?', (faction_id,)).fetchone()
    return jsonify({'ok': True, 'faction': public_faction(row), 'message': f'Leader verified. Free {TRIAL_DAYS}-day trial started.'})


@app.post('/api/factions/status')
def faction_status():
    data = request.get_json(force=True)
    token = str(data.get('api_token', '')).strip()
    row = get_faction_by_token(token)
    if not row:
        return jsonify({'ok': False, 'error': 'invalid_faction_key'}), 404
    with db() as con:
        active_alerts = con.execute('SELECT COUNT(*) c FROM alerts WHERE faction_id=? AND status="active"', (row['faction_id'],)).fetchone()['c']
        bankers = con.execute('SELECT COUNT(*) c FROM devices WHERE faction_id=? AND role="banker"', (row['faction_id'],)).fetchone()['c']
        active_banker_keys = con.execute('SELECT COUNT(*) c FROM banker_keys WHERE faction_id=? AND is_active=1', (row['faction_id'],)).fetchone()['c']
        total_banker_keys = con.execute('SELECT COUNT(*) c FROM banker_keys WHERE faction_id=?', (row['faction_id'],)).fetchone()['c']
        pending = con.execute('SELECT COUNT(*) c FROM payment_claims WHERE faction_id=? AND status="pending"', (row['faction_id'],)).fetchone()['c']
    out = public_faction(row)
    out.update({'active_alerts': active_alerts, 'registered_bankers': bankers, 'active_banker_keys': active_banker_keys, 'total_banker_keys': total_banker_keys, 'pending_payments': pending})
    return jsonify({'ok': True, 'faction': out})



@app.post('/api/banker-keys/create')
def create_banker_key():
    data = request.get_json(force=True)
    token = str(data.get('api_token', '')).strip()
    leader_api_key = str(data.get('leader_api_key', '')).strip()
    row = get_faction_by_token(token)
    if not row:
        return jsonify({'ok': False, 'error': 'invalid_leader_faction_key'}), 404
    leader_check = require_verified_leader_for_faction(row, leader_api_key)
    if not leader_check.get('ok'):
        return jsonify({'ok': False, 'error': 'leader_verification_failed', 'details': leader_check}), 403

    key_label = str(data.get('key_label', '')).strip()
    banker_name = str(data.get('banker_name', '')).strip()
    banker_torn_id = str(data.get('banker_torn_id', '')).strip()
    created_by = leader_check.get('name') or row['leader_name']
    if not banker_name:
        return jsonify({'ok': False, 'error': 'banker_name is required'}), 400

    with db() as con:
        if banker_torn_id:
            existing = con.execute('SELECT * FROM banker_keys WHERE faction_id=? AND banker_torn_id=?', (row['faction_id'], banker_torn_id)).fetchone()
        else:
            existing = con.execute('SELECT * FROM banker_keys WHERE faction_id=? AND lower(banker_name)=lower(?)', (row['faction_id'], banker_name)).fetchone()
        if existing:
            return jsonify({'ok': True, 'banker_key': public_banker_key(existing), 'message': 'This banker/user already has a key. One key per banker is enforced.'})
        key = 'banker_' + uuid.uuid4().hex + uuid.uuid4().hex[:8]
        con.execute("""INSERT INTO banker_keys(faction_id, key_label, banker_name, banker_torn_id, banker_key, created_by, created_at)
                       VALUES(?,?,?,?,?,?,?)""", (row['faction_id'], key_label or banker_name, banker_name, banker_torn_id, key, created_by, iso(utcnow())))
        bk = con.execute('SELECT * FROM banker_keys WHERE banker_key=?', (key,)).fetchone()
    return jsonify({'ok': True, 'banker_key': public_banker_key(bk), 'message': 'Give this key only to that banker/user. They open the Users tab, paste the key, and enable phone alerts.'})


@app.post('/api/banker-keys/list')
def list_banker_keys():
    data = request.get_json(force=True)
    token = str(data.get('api_token', '')).strip()
    leader_api_key = str(data.get('leader_api_key', '')).strip()
    row = get_faction_by_token(token)
    if not row:
        return jsonify({'ok': False, 'error': 'invalid_leader_faction_key'}), 404
    leader_check = require_verified_leader_for_faction(row, leader_api_key)
    if not leader_check.get('ok'):
        return jsonify({'ok': False, 'error': 'leader_verification_failed', 'details': leader_check}), 403
    with db() as con:
        keys = [public_banker_key(r) for r in con.execute('SELECT * FROM banker_keys WHERE faction_id=? ORDER BY created_at DESC', (row['faction_id'],)).fetchall()]
    return jsonify({'ok': True, 'banker_keys': keys, 'faction': public_faction(row)})


@app.post('/api/banker-keys/revoke')
def revoke_banker_key():
    data = request.get_json(force=True)
    token = str(data.get('api_token', '')).strip()
    leader_api_key = str(data.get('leader_api_key', '')).strip()
    banker_key = str(data.get('banker_key', '')).strip()
    row = get_faction_by_token(token)
    if not row:
        return jsonify({'ok': False, 'error': 'invalid_leader_faction_key'}), 404
    leader_check = require_verified_leader_for_faction(row, leader_api_key)
    if not leader_check.get('ok'):
        return jsonify({'ok': False, 'error': 'leader_verification_failed', 'details': leader_check}), 403
    with db() as con:
        con.execute('UPDATE banker_keys SET is_active=0 WHERE faction_id=? AND banker_key=?', (row['faction_id'], banker_key))
        con.execute('DELETE FROM devices WHERE faction_id=? AND banker_key=?', (row['faction_id'], banker_key))
    return jsonify({'ok': True, 'message': 'Banker key revoked and connected devices removed.'})


@app.post('/api/banker-keys/enable')
def enable_banker_key():
    data = request.get_json(force=True)
    token = str(data.get('api_token', '')).strip()
    leader_api_key = str(data.get('leader_api_key', '')).strip()
    banker_key = str(data.get('banker_key', '')).strip()
    row = get_faction_by_token(token)
    if not row:
        return jsonify({'ok': False, 'error': 'invalid_leader_faction_key'}), 404
    leader_check = require_verified_leader_for_faction(row, leader_api_key)
    if not leader_check.get('ok'):
        return jsonify({'ok': False, 'error': 'leader_verification_failed', 'details': leader_check}), 403
    with db() as con:
        con.execute('UPDATE banker_keys SET is_active=1 WHERE faction_id=? AND banker_key=?', (row['faction_id'], banker_key))
    return jsonify({'ok': True, 'message': 'Banker key enabled.'})


@app.post('/api/devices/register')
def register_device():
    data = request.get_json(force=True)
    token = str(data.get('api_token', '')).strip()
    row = get_faction_by_token(token)
    banker_key_row = None
    banker_key_value = None
    if not row:
        banker_key_row = get_banker_key(token)
        if not banker_key_row or not banker_key_row['is_active']:
            return jsonify({'ok': False, 'error': 'invalid_or_inactive_banker_key'}), 404
        row = get_faction_by_token(banker_key_row['api_token'])
        banker_key_value = token
    role = str(data.get('role', 'banker')).strip().lower()
    if banker_key_row:
        # A banker/user key can only register the phone assigned to that key.
        # The user cannot self-promote into leader/admin from the Users tab.
        role = 'banker'
    else:
        # Direct faction-key phone registration is leader-only. This prevents a copied faction key
        # from being used by random users to register banker/admin phones.
        if role != 'leader':
            return jsonify({'ok': False, 'error': 'use_a_banker_key_to_register_user_phone'}), 403
        leader_api_key = str(data.get('leader_api_key', '')).strip()
        leader_check = require_verified_leader_for_faction(row, leader_api_key)
        if not leader_check.get('ok'):
            return jsonify({'ok': False, 'error': 'leader_verification_failed', 'details': leader_check}), 403
    if role not in ['banker', 'leader']:
        return jsonify({'ok': False, 'error': 'role must be banker or leader'}), 400
    name = str(data.get('name', '')).strip() or (banker_key_row['banker_name'] if banker_key_row else (leader_check.get('name') if 'leader_check' in locals() else 'Unknown'))
    torn_id = str(data.get('torn_id', '')).strip() or (banker_key_row['banker_torn_id'] if banker_key_row else (leader_check.get('player_id') if 'leader_check' in locals() else None))
    sub = data.get('subscription')
    if not sub:
        return jsonify({'ok': False, 'error': 'missing push subscription'}), 400
    now = iso(utcnow())
    with db() as con:
        con.execute('''INSERT OR REPLACE INTO devices(faction_id, torn_id, name, role, subscription_json, created_at, last_seen, banker_key)
                       VALUES(?,?,?,?,?,?,?,?)''', (row['faction_id'], torn_id or f'anon-{uuid.uuid4().hex[:12]}', name, role, json.dumps(sub), now, now, banker_key_value))
        if banker_key_value:
            con.execute('UPDATE banker_keys SET last_used=? WHERE banker_key=?', (now, banker_key_value))
    return jsonify({'ok': True, 'message': f'{name} registered as {role}.', 'faction': public_faction(row), 'used_banker_key': bool(banker_key_value)})


def send_push(subscription, title, body, data=None):
    if not VAPID_PUBLIC_KEY or not VAPID_PRIVATE_KEY:
        return {'ok': False, 'error': 'VAPID keys are not set'}
    payload = json.dumps({'title': title, 'body': body, 'data': data or {}})
    try:
        webpush(
            subscription_info=subscription,
            data=payload,
            vapid_private_key=VAPID_PRIVATE_KEY,
            vapid_claims={'sub': VAPID_SUBJECT},
        )
        return {'ok': True}
    except WebPushException as e:
        return {'ok': False, 'error': str(e)}
    except Exception as e:
        return {'ok': False, 'error': str(e)}


@app.post('/api/banking/request')
def banking_request():
    data = request.get_json(force=True)
    token = str(data.get('api_token', '')).strip()
    row = get_faction_by_token(token)
    banker_key_row = None
    target_banker_key = None
    if not row:
        banker_key_row = get_banker_key(token)
        if not banker_key_row or not banker_key_row['is_active']:
            return jsonify({'ok': False, 'error': 'invalid_or_inactive_banker_key'}), 404
        row = get_faction_by_token(banker_key_row['api_token'])
        target_banker_key = token
    if not row:
        return jsonify({'ok': False, 'error': 'invalid_faction_or_banker_key'}), 404
    status = public_faction(row)
    if status['locked']:
        return jsonify({'ok': False, 'error': 'faction_key_locked', 'faction': status}), 402
    requester = str(data.get('requester_name', '')).strip() or 'Unknown user'
    amount = str(data.get('amount', '')).strip() or 'unknown amount'
    note = str(data.get('note', '')).strip()
    alert_id = uuid.uuid4().hex
    title = '🪙 New Torn Banking Request'
    body = f'{requester} requested {amount}.' + (f' {note}' if note else '')
    if banker_key_row:
        title = '🪙 Banking Request For You'
    now = iso(utcnow())
    data_json = {'url': data.get('url') or BASE_URL, 'type': 'banking_request', 'alert_id': alert_id, 'target_banker_key': bool(target_banker_key)}
    with db() as con:
        con.execute("""INSERT INTO alerts(id, faction_id, title, body, amount, requester_name, created_at, data_json)
                       VALUES(?,?,?,?,?,?,?,?)""",
                    (alert_id, row['faction_id'], title, body, amount, requester, now, json.dumps(data_json)))
        if target_banker_key:
            devices = con.execute("""SELECT d.* FROM devices d
                                     JOIN banker_keys bk ON bk.banker_key=d.banker_key
                                     WHERE d.faction_id=? AND d.role='banker' AND d.banker_key=? AND bk.is_active=1""",
                                  (row['faction_id'], target_banker_key)).fetchall()
        else:
            devices = con.execute("""SELECT d.* FROM devices d
                                     LEFT JOIN banker_keys bk ON bk.banker_key=d.banker_key
                                     WHERE d.faction_id=? AND (
                                       d.role IN ('leader', 'admin')
                                       OR (d.role='banker' AND (d.banker_key IS NULL OR bk.is_active=1))
                                     )""", (row['faction_id'],)).fetchall()
    sent = 0
    failed = 0
    for d in devices:
        result = send_push(json.loads(d['subscription_json']), title, body, data_json)
        if result.get('ok'):
            sent += 1
        else:
            failed += 1
    return jsonify({'ok': True, 'alert_id': alert_id, 'sent': sent, 'failed': failed, 'targeted_banker_key': bool(target_banker_key), 'faction': status})


@app.post('/api/alerts/list')
def alerts_list():
    data = request.get_json(force=True)
    token = str(data.get('api_token', '')).strip()
    row = get_faction_by_token(token)
    if not row:
        return jsonify({'ok': False, 'error': 'invalid_faction_key'}), 404
    with db() as con:
        active = [dict(r) for r in con.execute('SELECT * FROM alerts WHERE faction_id=? AND status="active" ORDER BY created_at DESC LIMIT 50', (row['faction_id'],)).fetchall()]
        done = [dict(r) for r in con.execute('SELECT * FROM alerts WHERE faction_id=? AND status="complete" ORDER BY completed_at DESC LIMIT 5', (row['faction_id'],)).fetchall()]
    return jsonify({'ok': True, 'active': active, 'completed_latest_5': done, 'faction': public_faction(row)})


@app.post('/api/alerts/complete')
def alert_complete():
    data = request.get_json(force=True)
    token = str(data.get('api_token', '')).strip()
    alert_id = str(data.get('alert_id', '')).strip()
    completed_by = str(data.get('completed_by', '')).strip() or 'Banker'
    row = get_faction_by_token(token)
    if not row:
        return jsonify({'ok': False, 'error': 'invalid_faction_key'}), 404
    now = iso(utcnow())
    with db() as con:
        con.execute('UPDATE alerts SET status="complete", completed_by=?, completed_at=? WHERE id=? AND faction_id=?', (completed_by, now, alert_id, row['faction_id']))
    return jsonify({'ok': True})


@app.post('/api/payments/claim')
def payment_claim():
    data = request.get_json(force=True)
    token = str(data.get('api_token', '')).strip()
    row = get_faction_by_token(token)
    if not row:
        return jsonify({'ok': False, 'error': 'invalid_faction_key'}), 404
    try:
        amount = int(data.get('xanax_amount'))
    except Exception:
        return jsonify({'ok': False, 'error': 'xanax_amount must match the active banker-key price'}), 400
    pricing = pricing_for_faction(row['faction_id'])
    allowed = pricing['allowed_payment_amounts']
    if pricing['active_banker_keys'] <= 0:
        return jsonify({'ok': False, 'error': 'create_at_least_one_active_banker_key_before_payment'}), 400
    if amount not in allowed:
        return jsonify({
            'ok': False,
            'error': 'payment_must_match_active_banker_key_price',
            'rule': pricing['payment_rule'],
            'active_banker_keys': pricing['active_banker_keys'],
            'monthly_cost_xanax': pricing['monthly_cost_xanax'],
            'allowed_payment_amounts': allowed
        }), 400
    months_paid = amount // pricing['monthly_cost_xanax']
    claim_id = uuid.uuid4().hex
    with db() as con:
        con.execute('''INSERT INTO payment_claims(id, faction_id, claimed_by, claimed_by_torn_id, xanax_amount, proof_text, created_at, active_banker_keys_at_claim, monthly_cost_at_claim, months_paid)
                       VALUES(?,?,?,?,?,?,?,?,?,?)''',
                    (claim_id, row['faction_id'], str(data.get('claimed_by', '')).strip() or 'Unknown', str(data.get('claimed_by_torn_id', '')).strip(), amount, str(data.get('proof_text', '')).strip(), iso(utcnow()), pricing['active_banker_keys'], pricing['monthly_cost_xanax'], months_paid))
    return jsonify({'ok': True, 'claim_id': claim_id, 'months_paid': months_paid, 'days_pending_approval': months_paid * 30, 'pricing': pricing, 'message': 'Payment claim submitted. Fries91/admin must verify it before time is extended.'})


@app.post('/api/payments/list')
def payments_list():
    data = request.get_json(force=True)
    token = str(data.get('api_token', '')).strip()
    row = get_faction_by_token(token)
    if not row:
        return jsonify({'ok': False, 'error': 'invalid_faction_key'}), 404
    with db() as con:
        claims = [dict(r) for r in con.execute('SELECT * FROM payment_claims WHERE faction_id=? ORDER BY created_at DESC LIMIT 25', (row['faction_id'],)).fetchall()]
    return jsonify({'ok': True, 'claims': claims, 'faction': public_faction(row)})


@app.post('/api/admin/payment/approve')
@require_admin
def admin_approve_payment():
    data = request.get_json(force=True)
    claim_id = str(data.get('claim_id', '')).strip()
    reviewed_by = str(data.get('reviewed_by', 'Fries91')).strip()
    note = str(data.get('admin_note', '')).strip()
    with db() as con:
        claim = con.execute('SELECT * FROM payment_claims WHERE id=?', (claim_id,)).fetchone()
        if not claim:
            return jsonify({'ok': False, 'error': 'claim_not_found'}), 404
        if claim['status'] != 'pending':
            return jsonify({'ok': False, 'error': 'claim_already_reviewed'}), 400
        faction = con.execute('SELECT * FROM factions WHERE faction_id=?', (claim['faction_id'],)).fetchone()
        amount = int(claim['xanax_amount'])
        months_paid = int(claim['months_paid'] or 0)
        if months_paid <= 0:
            monthly_cost = int(claim['monthly_cost_at_claim'] or 0) or pricing_for_faction(faction['faction_id'])['monthly_cost_xanax']
            months_paid = amount // monthly_cost if monthly_cost else 0
        days = months_paid * 30
        now = utcnow()
        current_active = max([d for d in [parse_dt(faction['trial_until']), parse_dt(faction['paid_until']) if faction['paid_until'] else None] if d])
        base = current_active if current_active > now else now
        new_paid_until = base + timedelta(days=days)
        con.execute('UPDATE factions SET paid_until=?, locked=0 WHERE faction_id=?', (iso(new_paid_until), faction['faction_id']))
        con.execute('''UPDATE payment_claims SET status="approved", reviewed_at=?, reviewed_by=?, admin_note=?, days_added=? WHERE id=?''',
                    (iso(now), reviewed_by, note, days, claim_id))
        updated = con.execute('SELECT * FROM factions WHERE faction_id=?', (faction['faction_id'],)).fetchone()
    return jsonify({'ok': True, 'days_added': days, 'faction': public_faction(updated)})


@app.post('/api/admin/payment/reject')
@require_admin
def admin_reject_payment():
    data = request.get_json(force=True)
    claim_id = str(data.get('claim_id', '')).strip()
    with db() as con:
        con.execute('''UPDATE payment_claims SET status="rejected", reviewed_at=?, reviewed_by=?, admin_note=? WHERE id=? AND status="pending"''',
                    (iso(utcnow()), str(data.get('reviewed_by', 'Fries91')), str(data.get('admin_note', 'Rejected')), claim_id))
    return jsonify({'ok': True})


@app.post('/api/admin/payment/pending')
@require_admin
def admin_pending_payments():
    with db() as con:
        claims = [dict(r) for r in con.execute('''SELECT c.*, f.faction_name, f.api_token, f.trial_until, f.paid_until
                                                  FROM payment_claims c JOIN factions f ON f.faction_id=c.faction_id
                                                  WHERE c.status="pending" ORDER BY c.created_at ASC LIMIT 100''').fetchall()]
    return jsonify({'ok': True, 'claims': claims})


@app.post('/api/admin/factions')
@require_admin
def admin_factions():
    with db() as con:
        rows = [public_faction(r) for r in con.execute('SELECT * FROM factions ORDER BY created_at DESC').fetchall()]
    return jsonify({'ok': True, 'factions': rows})


if __name__ == '__main__':
    init_db()
    app.run(host='0.0.0.0', port=int(os.getenv('PORT', '5000')), debug=True)
