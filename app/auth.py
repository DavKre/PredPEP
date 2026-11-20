"""auth.py — API token authentication and the audit log.

Design notes
------------
No login, no sessions, no cookies: a bearer token in the Authorization header,
the same for the browser, a script and the DDN client.

Auth ACTIVATES ONLY ONCE A TOKEN EXISTS. With an empty store every route is
open -- which is how the node behaves today -- and POST /api/tokens is allowed
unauthenticated so the first admin token can be created. From the moment one
token exists, every protected route requires one. This avoids the usual
chicken-and-egg lockout on a node that is already running.

Tokens are stored as a SHA-256 hash plus a short display prefix. The plaintext
is returned exactly once, at creation; a lost token is replaced, not recovered.
"""

import hashlib
import json
import os
import secrets
import threading
import time
from datetime import datetime, timezone
from functools import wraps

from flask import jsonify, request

# Where the token store and audit log live. Keep these on persistent storage:
# under /tmp they are lost on reboot along with every issued token.
STATE_DIR = os.environ.get('PREDPEP_STATE_DIR', '/var/lib/spacepep-predpep')
TOKEN_STORE = os.path.join(STATE_DIR, 'tokens.json')
AUDIT_LOG = os.path.join(STATE_DIR, 'audit.jsonl')
AUDIT_KEEP = int(os.environ.get('PREDPEP_AUDIT_KEEP', '500'))

# read < submit < admin. A token satisfies a requirement if its rank is >=.
SCOPES = {'read': 0, 'submit': 1, 'admin': 2}

_lock = threading.Lock()


def _now():
    return datetime.now(timezone.utc).isoformat()


def _ensure_dir():
    try:
        os.makedirs(STATE_DIR, exist_ok=True)
        return True
    except OSError as e:
        print('[auth] cannot create %s: %s' % (STATE_DIR, e), flush=True)
        return False


def _hash(token):
    return hashlib.sha256(token.encode('utf-8')).hexdigest()


def _load():
    try:
        with open(TOKEN_STORE) as f:
            data = json.load(f)
        return data if isinstance(data, list) else []
    except (OSError, ValueError):
        return []


def _save(rows):
    if not _ensure_dir():
        return False
    tmp = TOKEN_STORE + '.tmp'
    try:
        with open(tmp, 'w') as f:
            json.dump(rows, f, indent=2)
        os.replace(tmp, TOKEN_STORE)      # atomic; never a half-written store
        try:
            os.chmod(TOKEN_STORE, 0o600)
        except OSError:
            pass
        return True
    except OSError as e:
        print('[auth] could not write token store: %s' % e, flush=True)
        return False


def any_tokens():
    """True once at least one token exists -- i.e. auth is live."""
    return bool(_load())


# ----------------------------------------------------------------- audit log

def audit(action, target=None, actor=None):
    """Append one line to the audit log. Never raises: a failed audit write must
    not fail the action the user asked for, but it is reported on stdout."""
    entry = {
        'at': _now(),
        'actor': actor or getattr(request, 'predpep_actor', None) or 'anonymous',
        'action': action,
        'target': target,
        'ip': request.headers.get('X-Forwarded-For', request.remote_addr) if request else None,
    }
    if not _ensure_dir():
        return
    try:
        with open(AUDIT_LOG, 'a') as f:
            f.write(json.dumps(entry) + '\n')
    except OSError as e:
        print('[auth] audit write failed: %s' % e, flush=True)


def read_audit(limit=None):
    """Most recent entries, newest first."""
    limit = limit or AUDIT_KEEP
    try:
        with open(AUDIT_LOG) as f:
            lines = f.readlines()[-limit:]
    except OSError:
        return []
    out = []
    for line in reversed(lines):
        try:
            out.append(json.loads(line))
        except ValueError:
            continue
    return out


# -------------------------------------------------------------------- tokens

def issue(label, owner, scope):
    """Create a token. Returns (plaintext, record) or raises ValueError."""
    if scope not in SCOPES:
        raise ValueError('scope must be one of: %s' % ', '.join(SCOPES))
    if not label:
        raise ValueError('a label is required')

    plaintext = 'pp_' + secrets.token_urlsafe(32)
    record = {
        'id': secrets.token_hex(8),
        'label': str(label)[:80],
        'owner': str(owner or label)[:80],
        'scope': scope,
        'hash': _hash(plaintext),
        'prefix': plaintext[:9],          # enough to recognise, not to use
        'created_at': _now(),
        'last_used_at': None,
    }
    with _lock:
        rows = _load()
        rows.append(record)
        if not _save(rows):
            raise ValueError('could not persist the token (is %s writable?)' % STATE_DIR)
    return plaintext, {k: v for k, v in record.items() if k != 'hash'}


def revoke(token_id):
    with _lock:
        rows = _load()
        kept = [r for r in rows if r.get('id') != token_id]
        if len(kept) == len(rows):
            return False
        _save(kept)
    return True


def listing():
    """Token records without the hash."""
    return [{k: v for k, v in r.items() if k != 'hash'} for r in _load()]


def _touch(token_id):
    """Record last use. Best-effort: a write failure must not block the request."""
    with _lock:
        rows = _load()
        for r in rows:
            if r.get('id') == token_id:
                r['last_used_at'] = _now()
                break
        _save(rows)


_last_touch = {}


def identify(presented):
    """Return the matching token record, or None."""
    if not presented:
        return None
    digest = _hash(presented)
    for r in _load():
        # compare_digest on the hashes, so a wrong token costs the same time
        # as a right one regardless of where it first differs.
        if secrets.compare_digest(str(r.get('hash', '')), digest):
            # Throttle last_used writes: one per token per minute is plenty and
            # keeps a polling dashboard from rewriting the store every 5 s.
            now = time.time()
            if now - _last_touch.get(r['id'], 0) > 60:
                _last_touch[r['id']] = now
                _touch(r['id'])
            return r
    return None


def _presented():
    header = request.headers.get('Authorization', '')
    if header.startswith('Bearer '):
        return header[7:].strip()
    # Fallback for curl convenience and the DDN client's existing shape.
    return request.headers.get('X-API-Token', '').strip()


def require(scope='read', bootstrap=False):
    """Decorator enforcing a minimum scope.

    bootstrap=True allows the call through when NO tokens exist yet, so the
    first admin token can be created on a fresh node.
    """
    needed = SCOPES.get(scope, 0)

    def decorator(fn):
        @wraps(fn)
        def wrapper(*args, **kwargs):
            tokens_exist = any_tokens()

            if not tokens_exist:
                # Open node (today's behaviour). Mark the actor so the audit log
                # is honest about it rather than attributing actions to a user.
                request.predpep_actor = 'open-node'
                return fn(*args, **kwargs)

            record = identify(_presented())
            if record is None:
                if bootstrap and not tokens_exist:
                    request.predpep_actor = 'bootstrap'
                    return fn(*args, **kwargs)
                return jsonify({
                    'success': False,
                    'error': 'A valid API token is required.',
                }), 401

            if SCOPES.get(record.get('scope'), 0) < needed:
                return jsonify({
                    'success': False,
                    'error': 'This token has scope "%s"; "%s" is required.'
                             % (record.get('scope'), scope),
                }), 403

            request.predpep_actor = '%s (%s)' % (record.get('owner'), record.get('scope'))
            request.predpep_token = record
            return fn(*args, **kwargs)
        return wrapper
    return decorator


def status():
    """Summary for /state, so the UI can say whether the node is open."""
    rows = _load()
    return {
        'enforced': bool(rows),
        'token_count': len(rows),
        'state_dir_writable': _ensure_dir(),
    }
