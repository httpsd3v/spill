"""
Spill — full Supabase edition.
Auth: Supabase Auth (JWT verified here) · Data: Supabase Postgres (psycopg2)
Photos: Supabase Storage · Live updates: Supabase Realtime (frontend)

Env vars: SUPABASE_URL, SUPABASE_DB_URL, SUPABASE_SERVICE_KEY, SUPABASE_JWT_SECRET
Also create a PUBLIC storage bucket named: spill-photos

Run locally:  python app.py   →  http://localhost:5000
"""

import os
import re
import uuid
import hashlib
from collections import Counter, OrderedDict
from datetime import datetime, timezone
from functools import wraps

import jwt
import psycopg2
import psycopg2.extras
import psycopg2.extensions
from flask import Flask, request, jsonify, g, render_template
from flask.json.provider import DefaultJSONProvider
from supabase import create_client

# Keep UUIDs as plain strings everywhere (JWT sub is a string)
UUID_AS_STR = psycopg2.extensions.new_type((2950, 2951), "UUID", lambda v, c: v)
psycopg2.extensions.register_type(UUID_AS_STR)

BASE_DIR = os.path.dirname(os.path.abspath(__file__))

SUPABASE_URL = os.environ.get("SUPABASE_URL", "").rstrip("/")
DB_DSN = os.environ.get("SUPABASE_DB_URL", "").strip()
SERVICE_KEY = os.environ.get("SUPABASE_SERVICE_KEY", "").strip()
JWT_SECRET = os.environ.get("SUPABASE_JWT_SECRET", "").strip()
BUCKET = "spill-photos"
STORAGE_PREFIX = f"{SUPABASE_URL}/storage/v1/object/public/{BUCKET}/"

if DB_DSN and "sslmode=" not in DB_DSN:
    DB_DSN += ("&" if "?" in DB_DSN else "?") + "sslmode=require"

sb_admin = create_client(SUPABASE_URL, SERVICE_KEY) if SUPABASE_URL and SERVICE_KEY else None

app = Flask(__name__)
app.config["MAX_CONTENT_LENGTH"] = 8 * 1024 * 1024  # 8 MB per photo

POST_MAX_LEN = 500
MAX_PHOTOS = 4
ALLOWED_EXT = {"jpg", "jpeg", "png", "webp", "gif"}
VIDEO_EXT = {"mp4", "mov", "avi", "webm", "mkv", "m4v", "wmv", "flv"}
MIME_OF = {"jpg": "image/jpeg", "jpeg": "image/jpeg", "png": "image/png",
           "gif": "image/gif", "webp": "image/webp"}

# ------------------------------------------------- JSON: ISO timestamps

class SpillJSON(DefaultJSONProvider):
    @staticmethod
    def default(o):
        if isinstance(o, datetime):
            if o.tzinfo is not None:
                o = o.astimezone(timezone.utc).replace(tzinfo=None)
            return o.strftime("%Y-%m-%d %H:%M:%S")
        return DefaultJSONProvider.default(o)

app.json_provider_class = SpillJSON
app.json = SpillJSON(app)

# ---------------------------------------------------------------- schema

SCHEMA = """
CREATE EXTENSION IF NOT EXISTS citext;

CREATE TABLE IF NOT EXISTS users (
  id UUID PRIMARY KEY,
  username CITEXT UNIQUE NOT NULL,
  display_name TEXT NOT NULL,
  email TEXT DEFAULT '',
  bio TEXT DEFAULT '',
  location TEXT DEFAULT '',
  website TEXT DEFAULT '',
  avatar_url TEXT DEFAULT '',
  cover_url TEXT DEFAULT '',
  is_private BOOLEAN DEFAULT FALSE,
  created_at TIMESTAMPTZ DEFAULT now()
);
CREATE TABLE IF NOT EXISTS posts (
  id BIGSERIAL PRIMARY KEY,
  user_id UUID NOT NULL REFERENCES users(id),
  body TEXT NOT NULL DEFAULT '',
  visibility TEXT DEFAULT 'public' CHECK (visibility IN ('public','followers')),
  content_warning TEXT DEFAULT '',
  quote_of BIGINT REFERENCES posts(id),
  created_at TIMESTAMPTZ DEFAULT now(),
  deleted_at TIMESTAMPTZ
);
CREATE TABLE IF NOT EXISTS post_media (
  id BIGSERIAL PRIMARY KEY,
  post_id BIGINT NOT NULL REFERENCES posts(id) ON DELETE CASCADE,
  path TEXT NOT NULL,
  alt_text TEXT DEFAULT '',
  position INTEGER DEFAULT 0
);
CREATE TABLE IF NOT EXISTS likes (
  id BIGSERIAL PRIMARY KEY,
  user_id UUID NOT NULL REFERENCES users(id),
  post_id BIGINT NOT NULL REFERENCES posts(id),
  created_at TIMESTAMPTZ DEFAULT now(),
  UNIQUE(user_id, post_id)
);
CREATE TABLE IF NOT EXISTS comments (
  id BIGSERIAL PRIMARY KEY,
  post_id BIGINT NOT NULL REFERENCES posts(id),
  user_id UUID NOT NULL REFERENCES users(id),
  parent_id BIGINT REFERENCES comments(id),
  body TEXT NOT NULL,
  created_at TIMESTAMPTZ DEFAULT now(),
  deleted_at TIMESTAMPTZ
);
CREATE TABLE IF NOT EXISTS reposts (
  id BIGSERIAL PRIMARY KEY,
  user_id UUID NOT NULL,
  post_id BIGINT NOT NULL,
  created_at TIMESTAMPTZ DEFAULT now(),
  UNIQUE(user_id, post_id)
);
CREATE TABLE IF NOT EXISTS bookmarks (
  id BIGSERIAL PRIMARY KEY,
  user_id UUID NOT NULL,
  post_id BIGINT NOT NULL,
  created_at TIMESTAMPTZ DEFAULT now(),
  UNIQUE(user_id, post_id)
);
CREATE TABLE IF NOT EXISTS follows (
  id BIGSERIAL PRIMARY KEY,
  follower_id UUID NOT NULL,
  following_id UUID NOT NULL,
  status TEXT DEFAULT 'accepted' CHECK (status IN ('accepted','pending','rejected')),
  created_at TIMESTAMPTZ DEFAULT now(),
  UNIQUE(follower_id, following_id)
);
CREATE TABLE IF NOT EXISTS notifications (
  id BIGSERIAL PRIMARY KEY,
  recipient_id UUID NOT NULL,
  actor_id UUID NOT NULL,
  type TEXT NOT NULL,
  post_id BIGINT,
  comment_id BIGINT,
  is_read BOOLEAN DEFAULT FALSE,
  created_at TIMESTAMPTZ DEFAULT now()
);
CREATE TABLE IF NOT EXISTS conversations (
  id BIGSERIAL PRIMARY KEY,
  created_by UUID,
  created_at TIMESTAMPTZ DEFAULT now()
);
CREATE TABLE IF NOT EXISTS conversation_members (
  conversation_id BIGINT NOT NULL REFERENCES conversations(id),
  user_id UUID NOT NULL REFERENCES users(id),
  last_read_at TIMESTAMPTZ,
  UNIQUE(conversation_id, user_id)
);
CREATE TABLE IF NOT EXISTS messages (
  id BIGSERIAL PRIMARY KEY,
  conversation_id BIGINT NOT NULL REFERENCES conversations(id),
  sender_id UUID NOT NULL REFERENCES users(id),
  body TEXT DEFAULT '',
  photo_path TEXT DEFAULT '',
  created_at TIMESTAMPTZ DEFAULT now()
);
CREATE TABLE IF NOT EXISTS blocks (
  id BIGSERIAL PRIMARY KEY,
  blocker_id UUID NOT NULL,
  blocked_id UUID NOT NULL,
  UNIQUE(blocker_id, blocked_id)
);
CREATE TABLE IF NOT EXISTS reports (
  id BIGSERIAL PRIMARY KEY,
  reporter_id UUID NOT NULL,
  target_type TEXT NOT NULL,
  target_id BIGINT NOT NULL,
  reason TEXT DEFAULT '',
  created_at TIMESTAMPTZ DEFAULT now()
);

CREATE INDEX IF NOT EXISTS idx_posts_user ON posts(user_id);
CREATE INDEX IF NOT EXISTS idx_likes_post ON likes(post_id);
CREATE INDEX IF NOT EXISTS idx_comments_post ON comments(post_id);
CREATE INDEX IF NOT EXISTS idx_reposts_post ON reposts(post_id);
CREATE INDEX IF NOT EXISTS idx_bookmarks_user ON bookmarks(user_id);
CREATE INDEX IF NOT EXISTS idx_follows_follower ON follows(follower_id);
CREATE INDEX IF NOT EXISTS idx_follows_following ON follows(following_id);
CREATE INDEX IF NOT EXISTS idx_notifications_recipient ON notifications(recipient_id, id DESC);
CREATE INDEX IF NOT EXISTS idx_messages_convo ON messages(conversation_id, id);
CREATE INDEX IF NOT EXISTS idx_post_media_post ON post_media(post_id);

ALTER TABLE users ENABLE ROW LEVEL SECURITY;
ALTER TABLE posts ENABLE ROW LEVEL SECURITY;
ALTER TABLE post_media ENABLE ROW LEVEL SECURITY;
ALTER TABLE likes ENABLE ROW LEVEL SECURITY;
ALTER TABLE comments ENABLE ROW LEVEL SECURITY;
ALTER TABLE reposts ENABLE ROW LEVEL SECURITY;
ALTER TABLE bookmarks ENABLE ROW LEVEL SECURITY;
ALTER TABLE follows ENABLE ROW LEVEL SECURITY;
ALTER TABLE notifications ENABLE ROW LEVEL SECURITY;
ALTER TABLE conversations ENABLE ROW LEVEL SECURITY;
ALTER TABLE conversation_members ENABLE ROW LEVEL SECURITY;
ALTER TABLE messages ENABLE ROW LEVEL SECURITY;
ALTER TABLE blocks ENABLE ROW LEVEL SECURITY;
ALTER TABLE reports ENABLE ROW LEVEL SECURITY;

DROP POLICY IF EXISTS rt_notifications ON notifications;
CREATE POLICY rt_notifications ON notifications
  FOR SELECT USING (recipient_id = auth.uid());

DROP POLICY IF EXISTS rt_members ON conversation_members;
CREATE POLICY rt_members ON conversation_members
  FOR SELECT USING (user_id = auth.uid());

DROP POLICY IF EXISTS rt_messages ON messages;
CREATE POLICY rt_messages ON messages
  FOR SELECT USING (conversation_id IN
    (SELECT conversation_id FROM conversation_members WHERE user_id = auth.uid()));

DO $$ BEGIN ALTER PUBLICATION supabase_realtime ADD TABLE notifications;
      EXCEPTION WHEN duplicate_object OR undefined_object THEN NULL; END $$;
DO $$ BEGIN ALTER PUBLICATION supabase_realtime ADD TABLE messages;
      EXCEPTION WHEN duplicate_object OR undefined_object THEN NULL; END $$;
"""


class PgDB:
    def __init__(self, dsn):
        self.conn = psycopg2.connect(dsn, connect_timeout=10)

    def execute(self, sql, params=()):
        cur = self.conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor)
        cur.execute(sql, params)
        return cur

    def commit(self):
        self.conn.commit()

    def close(self):
        try:
            self.conn.close()
        except Exception:
            pass


def get_db():
    if "db" not in g:
        try:
            g.db = PgDB(DB_DSN)
        except psycopg2.Error as e:
            raise RuntimeError(f"Cannot reach the Supabase database: {e}")
    return g.db


@app.teardown_appcontext
def close_db(_exc):
    db = g.pop("db", None)
    if db is not None:
        db.close()


def init_db():
    conn = psycopg2.connect(DB_DSN)
    cur = conn.cursor()
    cur.execute(SCHEMA)
    conn.commit()
    conn.close()
    print("Supabase schema, RLS policies and realtime publication ready.")

# ------------------------------------------------------------- helpers

def err(msg, code=400):
    return jsonify({"error": msg}), code


def parse_ts(v):
    if isinstance(v, datetime):
        return v.astimezone(timezone.utc).replace(tzinfo=None) if v.tzinfo else v
    try:
        return datetime.strptime(v, "%Y-%m-%d %H:%M:%S")
    except Exception:
        return datetime.utcnow()


def current_jwt():
    h = request.headers.get("Authorization", "")
    if not h.startswith("Bearer "):
        return None
    token = h[7:]
    # Method 1: fast local check
    try:
        payload = jwt.decode(token, JWT_SECRET, algorithms=["HS256", "ES256", "RS256"],
                             options={"verify_aud": False})
        if payload.get("sub"):
            return payload
    except Exception as e:
        print("[Spill] local JWT check failed:", e)
    # Method 2: ask Supabase Auth directly (always authoritative)
    try:
        res = sb_admin.auth.get_user(token)
        if res and res.user:
            return {"sub": res.user.id, "email": res.user.email or "", "role": "authenticated"}
    except Exception as e:
        print("[Spill] GoTrue check failed:", e)
    return None


def auth_required(fn):
    """Valid Supabase JWT — profile row not required (used by /api/bootstrap)."""
    @wraps(fn)
    def wrapper(*a, **kw):
        payload = current_jwt()
        if not payload:
            return err("Not authenticated", 401)
        g.uid = payload["sub"]
        g.jwt = payload
        return fn(*a, **kw)
    return wrapper


def token_required(fn):
    """Valid Supabase JWT + existing Spill profile."""
    @wraps(fn)
    def wrapper(*a, **kw):
        payload = current_jwt()
        if not payload:
            return err("Not authenticated", 401)
        row = get_db().execute("SELECT * FROM users WHERE id=%s", (payload["sub"],)).fetchone()
        if not row:
            return err("Profile not created yet.", 401)
        g.uid = payload["sub"]
        g.jwt = payload
        g.user = row
        return fn(*a, **kw)
    return wrapper


def notify(db, recipient, actor, ntype, post_id=None, comment_id=None):
    if recipient == actor:
        return
    db.execute(
        "INSERT INTO notifications (recipient_id, actor_id, type, post_id, comment_id) VALUES (%s,%s,%s,%s,%s)",
        (recipient, actor, ntype, post_id, comment_id),
    )


CORE = """
SELECT p.id, p.body, p.visibility, p.content_warning, p.quote_of, p.created_at,
       u.id AS author_id, u.username, u.display_name, u.avatar_url,
       (SELECT COUNT(*) FROM likes l WHERE l.post_id = p.id) AS like_count,
       (SELECT COUNT(*) FROM comments c WHERE c.post_id = p.id) AS comment_count,
       (SELECT COUNT(*) FROM reposts r WHERE r.post_id = p.id) AS repost_count,
       EXISTS(SELECT 1 FROM likes l WHERE l.post_id = p.id AND l.user_id = %s) AS liked,
       EXISTS(SELECT 1 FROM bookmarks b WHERE b.post_id = p.id AND b.user_id = %s) AS bookmarked,
       EXISTS(SELECT 1 FROM reposts r WHERE r.post_id = p.id AND r.user_id = %s) AS reposted
FROM posts p JOIN users u ON u.id = p.user_id
"""

CAN_SEE = """
 AND (
    p.user_id = %s
    OR EXISTS(SELECT 1 FROM follows f WHERE f.following_id = p.user_id AND f.follower_id = %s AND f.status = 'accepted')
    OR (p.visibility = 'public' AND u.is_private = FALSE)
 )
 AND p.user_id NOT IN (SELECT blocked_id FROM blocks WHERE blocker_id = %s)
 AND p.user_id NOT IN (SELECT blocker_id FROM blocks WHERE blocked_id = %s)
"""


def query_posts(db, uid, where="", params=(), order="ORDER BY p.id DESC", limit=None):
    sql = CORE + " WHERE p.deleted_at IS NULL" + CAN_SEE
    if where:
        sql += " AND " + where
    sql += " " + order
    qp = (uid,) * 7 + tuple(params)
    if limit:
        sql += " LIMIT %s"
        qp = qp + (limit,)
    return db.execute(sql, qp).fetchall()


def hydrate(rows, db, uid):
    out = []
    for r in rows:
        d = dict(r)
        d["liked"] = bool(d["liked"])
        d["bookmarked"] = bool(d["bookmarked"])
        d["reposted"] = bool(d["reposted"])
        d["media"] = [
            {"path": m["path"], "alt": m["alt_text"]}
            for m in db.execute(
                "SELECT path, alt_text FROM post_media WHERE post_id=%s ORDER BY position, id",
                (r["id"],),
            )
        ]
        d["quoted"] = None
        if r["quote_of"]:
            qq = db.execute(
                CORE + " WHERE p.deleted_at IS NULL AND p.id=%s" + CAN_SEE,
                (uid, uid, uid, r["quote_of"], uid, uid, uid, uid),
            ).fetchone()
            if qq:
                qd = dict(qq)
                qd["media"] = [
                    {"path": m["path"], "alt": m["alt_text"]}
                    for m in db.execute(
                        "SELECT path, alt_text FROM post_media WHERE post_id=%s ORDER BY position, id",
                        (qq["id"],),
                    )
                ]
                d["quoted"] = qd
        out.append(d)
    return out


def user_dict(u, db, me):
    d = {
        "id": u["id"],
        "username": u["username"],
        "display_name": u["display_name"],
        "bio": u["bio"],
        "location": u["location"],
        "website": u["website"],
        "avatar_url": u["avatar_url"],
        "cover_url": u["cover_url"],
        "is_private": bool(u["is_private"]),
        "created_at": u["created_at"],
        "followers": db.execute("SELECT COUNT(*) AS count FROM follows WHERE following_id=%s AND status='accepted'", (u["id"],)).fetchone()["count"],
        "following": db.execute("SELECT COUNT(*) AS count FROM follows WHERE follower_id=%s AND status='accepted'", (u["id"],)).fetchone()["count"],
        "posts": db.execute("SELECT COUNT(*) AS count FROM posts WHERE user_id=%s AND deleted_at IS NULL", (u["id"],)).fetchone()["count"],
    }
    if me == u["id"]:
        d["relationship"] = "me"
        d["email"] = u["email"]
    else:
        f = db.execute("SELECT status FROM follows WHERE follower_id=%s AND following_id=%s", (me, u["id"])).fetchone()
        d["relationship"] = f["status"] if f else "none"
    d["blocked"] = bool(db.execute("SELECT 1 FROM blocks WHERE blocker_id=%s AND blocked_id=%s", (me, u["id"])).fetchone())
    d["locked_for_me"] = d["is_private"] and d["relationship"] not in ("accepted", "me")
    return d

# ----------------------------------------- storage + image safety

def sniff_image(data):
    if data.startswith(b"\xff\xd8\xff"):
        return "jpg"
    if data.startswith(b"\x89PNG\r\n\x1a\n"):
        return "png"
    if data[:6] in (b"GIF87a", b"GIF89a"):
        return "gif"
    if data[:4] == b"RIFF" and data[8:12] == b"WEBP":
        return "webp"
    return None


def sniff_video(data):
    if data[4:8] == b"ftyp":                      # MP4 / MOV family
        return True
    if data[:4] == b"RIFF" and data[8:12] in (b"AVI ",):
        return True
    if data.startswith(b"\x1a\x45\xdf\xa3"):      # WebM / MKV
        return True
    if data[4:8] in (b"moov", b"mdat"):
        return True
    return False


def storage_upload(data, mime, folder):
    """Upload bytes to the spill-photos bucket; returns the public URL."""
    path = f"{folder}/{uuid.uuid4().hex}.{mime.split('/')[-1]}"
    try:
        sb_admin.storage.from_(BUCKET).upload(path, data,
            file_options={"content-type": mime, "upsert": "false"})
        return sb_admin.storage.from_(BUCKET).get_public_url(path)
    except Exception as e:
        raise RuntimeError(
            f"Storage upload failed ({e}). Did you create the public bucket '{BUCKET}'?")


@app.post("/api/upload")
@auth_required
def upload_image():
    f = request.files.get("file")
    if not f or not f.filename:
        return err("No file provided")
    ext = f.filename.rsplit(".", 1)[-1].lower() if "." in f.filename else ""
    if ext in VIDEO_EXT:
        return err("Video uploads are not allowed on Spill. Text and photos only.", 415)
    if ext not in ALLOWED_EXT:
        return err("Unsupported file type. Use JPEG, PNG, WebP or GIF.")
    if not (f.mimetype or "").startswith("image/"):
        return err("File MIME type must be an image type.")
    data = f.read()
    if len(data) > app.config["MAX_CONTENT_LENGTH"]:
        return err("Photo is larger than the 8 MB limit.")
    if sniff_video(data):
        return err("That file looks like a video in disguise. Spill is text + photos only.", 415)
    if not sniff_image(data):
        return err("File content is not a valid image (JPEG, PNG, WebP or GIF).")
    try:
        url = storage_upload(data, MIME_OF[ext], f"photos/{g.uid}")
    except RuntimeError as e:
        return err(str(e), 500)
    return jsonify({"path": url})

# ------------------------------------------------ default avatars

AVATAR_COLORS = ["#e8336d", "#8b5cf6", "#06b6d4", "#f59e0b", "#22c55e",
                 "#6366f1", "#f97316", "#14b8a6"]


def default_avatar(username, display):
    initials = "".join(w[0] for w in display.split()[:2]).upper() or username[0].upper()
    initials = initials.replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;")
    color = AVATAR_COLORS[sum(ord(c) for c in username) % len(AVATAR_COLORS)]
    svg = (
        '<svg xmlns="http://www.w3.org/2000/svg" width="200" height="200">'
        f'<rect width="200" height="200" fill="{color}"/>'
        f'<text x="100" y="128" font-family="Arial,sans-serif" font-size="82" font-weight="700" '
        f'fill="#fff" text-anchor="middle">{initials}</text></svg>'
    )
    try:
        return storage_upload(svg.encode("utf-8"), "image/svg+xml", "avatars")
    except RuntimeError:
        return ""

# ------------------------------------------------------------- auth / profile

USERNAME_RE = re.compile(r"^[A-Za-z0-9_]{3,20}$")


@app.post("/api/bootstrap")
@auth_required
def bootstrap():
    db = get_db()
    uid = g.uid
    u = db.execute("SELECT * FROM users WHERE id=%s", (uid,)).fetchone()
    if u:
        return jsonify({"user": user_dict(u, db, uid)})
    d = request.get_json(silent=True) or {}
    username = (d.get("username") or "").strip()
    name = (d.get("display_name") or username).strip()[:50] or "Spiller"
    if not USERNAME_RE.match(username):
        return err("Username must be 3–20 characters: letters, numbers, underscores.")
    if db.execute("SELECT 1 FROM users WHERE username=%s", (username,)).fetchone():
        return err("That username is already taken.")
    db.execute(
        "INSERT INTO users (id, username, display_name, email, avatar_url) VALUES (%s,%s,%s,%s,%s)",
        (uid, username, name, g.jwt.get("email", ""), default_avatar(username, name)),
    )
    db.commit()
    u = db.execute("SELECT * FROM users WHERE id=%s", (uid,)).fetchone()
    return jsonify({"user": user_dict(u, db, uid)})


@app.get("/api/me")
@token_required
def me():
    return jsonify({"user": user_dict(g.user, get_db(), g.uid)})


@app.post("/api/profile")
@token_required
def update_profile():
    d = request.get_json(force=True)
    db = get_db()
    uid = g.uid

    def url_field(val, fallback):
        return val if (val or "").startswith(STORAGE_PREFIX) else fallback

    db.execute(
        """UPDATE users SET display_name=%s, bio=%s, location=%s, website=%s,
           avatar_url=%s, cover_url=%s, is_private=%s WHERE id=%s""",
        (
            (d.get("display_name") or g.user["display_name"]).strip()[:50],
            (d.get("bio") or "").strip()[:160],
            (d.get("location") or "").strip()[:60],
            (d.get("website") or "").strip()[:120],
            url_field(d.get("avatar_url"), g.user["avatar_url"]),
            url_field(d.get("cover_url"), g.user["cover_url"]),
            bool(d.get("is_private")),
            uid,
        ),
    )
    db.commit()
    u = db.execute("SELECT * FROM users WHERE id=%s", (uid,)).fetchone()
    return jsonify({"user": user_dict(u, db, uid)})

# ------------------------------------------------------------- feeds

@app.get("/api/feed")
@token_required
def feed():
    mode = request.args.get("mode", "foryou")
    before = request.args.get("before", type=int)
    cursor = request.args.get("cursor", "")
    uid = g.uid
    db = get_db()
    where, params = "", ()
    if before:
        where, params = "p.id < %s", (before,)
    if mode == "following":
        where += (" AND " if where else "") + \
            "(p.user_id=%s OR p.user_id IN (SELECT following_id FROM follows WHERE follower_id=%s AND status='accepted'))"
        params += (uid, uid)
    limit = 20
    if mode == "foryou":
        # Per-user daily shuffled order → random, no duplicates, ends with "caught up"
        seed = f"{uid}:{datetime.utcnow().date().isoformat()}"
        cur_sql, cur_params = "", ()
        if cursor:
            cur_sql = " AND md5(CAST(p.id AS text) || %s) > %s"
            cur_params = (seed, cursor)
        rows = db.execute(
            CORE + " WHERE p.deleted_at IS NULL" + CAN_SEE + cur_sql +
            " ORDER BY md5(CAST(p.id AS text) || %s) LIMIT %s",
            (uid,) * 7 + cur_params + (seed, limit),
        ).fetchall()
    else:
        rows = query_posts(db, uid, where, params, limit=limit)
    posts = hydrate(rows, db, uid)
    if mode == "foryou":
        nxt = hashlib.md5((str(posts[-1]["id"]) + seed).encode()).hexdigest() if len(posts) == limit else None
    else:
        nxt = min((p["id"] for p in posts), default=None) if len(posts) == limit else None
    return jsonify({"posts": posts, "next": nxt})


@app.get("/api/bookmarks")
@token_required
def bookmarks():
    uid = g.uid
    db = get_db()
    rows = query_posts(
        db, uid,
        where="p.id IN (SELECT post_id FROM bookmarks WHERE user_id=%s)",
        params=(uid,),
        limit=50,
    )
    return jsonify({"posts": hydrate(rows, db, uid)})

# ------------------------------------------------------------- posts

def valid_media_list(media):
    if not isinstance(media, list) or len(media) > MAX_PHOTOS:
        return None
    clean = []
    for m in media:
        path = (m.get("path") or "") if isinstance(m, dict) else ""
        if not path.startswith(STORAGE_PREFIX):
            return None
        clean.append({"path": path, "alt": (m.get("alt") or "").strip()[:200]})
    return clean


@app.post("/api/posts")
@token_required
def create_post():
    d = request.get_json(force=True)
    body = (d.get("body") or "").strip()
    media = valid_media_list(d.get("media") or [])
    if media is None:
        return err(f"Up to {MAX_PHOTOS} photos per post, and they must live in Spill storage.")
    visibility = d.get("visibility") if d.get("visibility") in ("public", "followers") else "public"
    cw = (d.get("content_warning") or "").strip()[:80]
    quote_of = d.get("quote_of")
    db = get_db()
    uid = g.uid
    if not body and not media:
        return err("A post needs some text or at least one photo.")
    if len(body) > POST_MAX_LEN:
        return err(f"Posts are limited to {POST_MAX_LEN} characters.")
    q = None
    if quote_of:
        q = db.execute("SELECT id, user_id FROM posts WHERE id=%s AND deleted_at IS NULL", (quote_of,)).fetchone()
        if not q:
            return err("The post you are quoting no longer exists.")
        quote_of = q["id"]
    pid = db.execute(
        "INSERT INTO posts (user_id, body, visibility, content_warning, quote_of) VALUES (%s,%s,%s,%s,%s) RETURNING id",
        (uid, body, visibility, cw, quote_of),
    ).fetchone()["id"]
    for i, m in enumerate(media):
        db.execute(
            "INSERT INTO post_media (post_id, path, alt_text, position) VALUES (%s,%s,%s,%s)",
            (pid, m["path"], m["alt"], i),
        )
    if quote_of:
        notify(db, q["user_id"], uid, "quote", post_id=pid)
    for uname in re.findall(r"@(\w+)", body):
        u = db.execute("SELECT id FROM users WHERE username=%s", (uname,)).fetchone()
        if u:
            notify(db, u["id"], uid, "mention", post_id=pid)
    db.commit()
    rows = db.execute(CORE + " WHERE p.id=%s", (uid, uid, uid, pid)).fetchall()
    return jsonify({"post": hydrate(rows, db, uid)[0]})


def _fetch_post(db, uid, pid, enforce_visibility=True):
    sql = CORE + " WHERE p.deleted_at IS NULL AND p.id=%s"
    params = (uid, uid, uid, pid)
    if enforce_visibility:
        sql += CAN_SEE
        params += (uid, uid, uid, uid)
    return db.execute(sql, params).fetchone()


@app.get("/api/posts/<int:pid>")
@token_required
def post_detail(pid):
    db = get_db()
    uid = g.uid
    row = _fetch_post(db, uid, pid)
    if not row:
        return err("This post is unavailable.", 404)
    post = hydrate([row], db, uid)[0]
    crows = db.execute(
        """SELECT c.id, c.body, c.created_at, c.parent_id, c.user_id,
                  u.username, u.display_name, u.avatar_url
           FROM comments c JOIN users u ON u.id = c.user_id
           WHERE c.post_id=%s AND c.deleted_at IS NULL ORDER BY c.id DESC LIMIT 300""",
        (pid,),
    ).fetchall()
    by_id = OrderedDict((c["id"], dict(c)) for c in crows)
    for c in by_id.values():
        c["children"] = []
    tops = []
    for c in by_id.values():
        parent = by_id.get(c["parent_id"])
        (parent["children"] if parent else tops).append(c)
    for c in by_id.values():
        c["children"].reverse()
    return jsonify({"post": post, "comments": tops})


@app.delete("/api/posts/<int:pid>")
@token_required
def delete_post(pid):
    db = get_db()
    row = db.execute("SELECT user_id FROM posts WHERE id=%s AND deleted_at IS NULL", (pid,)).fetchone()
    if not row:
        return err("Post not found.", 404)
    if row["user_id"] != g.uid:
        return err("You can only delete your own posts.", 403)
    db.execute("UPDATE posts SET deleted_at = now() WHERE id=%s", (pid,))
    db.commit()
    return jsonify({"ok": True})


def _toggle(db, uid, pid, table):
    row = db.execute(f"SELECT id FROM {table} WHERE user_id=%s AND post_id=%s", (uid, pid)).fetchone()
    if row:
        db.execute(f"DELETE FROM {table} WHERE id=%s", (row["id"],))
        on = False
    else:
        db.execute(f"INSERT INTO {table} (user_id, post_id) VALUES (%s,%s)", (uid, pid))
        on = True
    count = db.execute(f"SELECT COUNT(*) AS count FROM {table} WHERE post_id=%s", (pid,)).fetchone()["count"]
    return on, count


@app.post("/api/posts/<int:pid>/like")
@token_required
def like_post(pid):
    db = get_db()
    uid = g.uid
    p = db.execute("SELECT user_id FROM posts WHERE id=%s AND deleted_at IS NULL", (pid,)).fetchone()
    if not p:
        return err("Post not found.", 404)
    on, count = _toggle(db, uid, pid, "likes")
    if on:
        notify(db, p["user_id"], uid, "like", post_id=pid)
    db.commit()
    return jsonify({"liked": on, "count": count})


@app.post("/api/posts/<int:pid>/bookmark")
@token_required
def bookmark_post(pid):
    db = get_db()
    on, _ = _toggle(db, g.uid, pid, "bookmarks")
    db.commit()
    return jsonify({"bookmarked": on})


@app.post("/api/posts/<int:pid>/repost")
@token_required
def repost_post(pid):
    db = get_db()
    uid = g.uid
    p = db.execute("SELECT user_id FROM posts WHERE id=%s AND deleted_at IS NULL", (pid,)).fetchone()
    if not p:
        return err("Post not found.", 404)
    on, count = _toggle(db, uid, pid, "reposts")
    if on:
        notify(db, p["user_id"], uid, "repost", post_id=pid)
    db.commit()
    return jsonify({"reposted": on, "count": count})


@app.post("/api/posts/<int:pid>/quote")
@token_required
def quote_post(pid):
    d = request.get_json(force=True)
    body = (d.get("body") or "").strip()
    if not body:
        return err("Add your thoughts to quote this post.")
    if len(body) > POST_MAX_LEN:
        return err(f"Posts are limited to {POST_MAX_LEN} characters.")
    db = get_db()
    uid = g.uid
    p = db.execute("SELECT id, user_id FROM posts WHERE id=%s AND deleted_at IS NULL", (pid,)).fetchone()
    if not p:
        return err("Post not found.", 404)
    new_id = db.execute(
        "INSERT INTO posts (user_id, body, visibility, quote_of) VALUES (%s,%s,'public',%s) RETURNING id",
        (uid, body, pid),
    ).fetchone()["id"]
    notify(db, p["user_id"], uid, "quote", post_id=new_id)
    db.commit()
    row = db.execute(CORE + " WHERE p.id=%s", (uid, uid, uid, new_id)).fetchone()
    return jsonify({"post": hydrate([row], db, uid)[0]})

# ------------------------------------------------------------- comments

@app.post("/api/posts/<int:pid>/comments")
@token_required
def add_comment(pid):
    d = request.get_json(force=True)
    body = (d.get("body") or "").strip()
    parent_id = d.get("parent_id")
    if not body:
        return err("Write something first.")
    if len(body) > POST_MAX_LEN:
        return err(f"Comments are limited to {POST_MAX_LEN} characters.")
    db = get_db()
    uid = g.uid
    p = db.execute("SELECT user_id FROM posts WHERE id=%s AND deleted_at IS NULL", (pid,)).fetchone()
    if not p:
        return err("Post not found.", 404)
    parent = None
    if parent_id:
        parent = db.execute("SELECT id, user_id FROM comments WHERE id=%s AND post_id=%s", (parent_id, pid)).fetchone()
        if not parent:
            return err("The comment you are replying to is gone.")
    cid = db.execute(
        "INSERT INTO comments (post_id, user_id, parent_id, body) VALUES (%s,%s,%s,%s) RETURNING id",
        (pid, uid, parent_id, body),
    ).fetchone()["id"]
    notify(db, p["user_id"], uid, "comment", post_id=pid, comment_id=cid)
    if parent_id and parent["user_id"] != p["user_id"]:
        notify(db, parent["user_id"], uid, "reply", post_id=pid, comment_id=cid)
    for uname in re.findall(r"@(\w+)", body):
        u = db.execute("SELECT id FROM users WHERE username=%s", (uname,)).fetchone()
        if u:
            notify(db, u["id"], uid, "mention", post_id=pid, comment_id=cid)
    db.commit()
    return jsonify({"id": cid})


@app.delete("/api/comments/<int:cid>")
@token_required
def delete_comment(cid):
    db = get_db()
    row = db.execute("SELECT user_id FROM comments WHERE id=%s", (cid,)).fetchone()
    if not row:
        return err("Comment not found.", 404)
    if row["user_id"] != g.uid:
        return err("You can only delete your own comments.", 403)
    db.execute("UPDATE comments SET deleted_at = now() WHERE id=%s", (cid,))
    db.commit()
    return jsonify({"ok": True})

# ------------------------------------------------------------- users / follows

@app.get("/api/users/<username>")
@token_required
def get_user(username):
    db = get_db()
    u = db.execute("SELECT * FROM users WHERE username=%s", (username,)).fetchone()
    if not u:
        return err("This account doesn't exist.", 404)
    return jsonify({"user": user_dict(u, db, g.uid)})


@app.get("/api/users/<username>/posts")
@token_required
def user_posts(username):
    db = get_db()
    uid = g.uid
    tab = request.args.get("tab", "posts")
    u = db.execute("SELECT * FROM users WHERE username=%s", (username,)).fetchone()
    if not u:
        return err("This account doesn't exist.", 404)
    if db.execute("SELECT 1 FROM blocks WHERE blocker_id=%s AND blocked_id=%s UNION SELECT 1 FROM blocks WHERE blocker_id=%s AND blocked_id=%s",
                  (uid, u["id"], u["id"], uid)).fetchone():
        return jsonify({"locked": True, "posts": []})
    rel = "me" if u["id"] == uid else (
        lambda f: f["status"] if f else "none"
    )(db.execute("SELECT status FROM follows WHERE follower_id=%s AND following_id=%s", (uid, u["id"])).fetchone())
    if u["is_private"] and rel not in ("me", "accepted"):
        return jsonify({"locked": True, "posts": []})
    where, params = "p.user_id=%s", (u["id"],)
    if tab == "media":
        where += " AND EXISTS(SELECT 1 FROM post_media pm WHERE pm.post_id=p.id)"
    elif tab == "likes":
        where = "p.id IN (SELECT post_id FROM likes WHERE user_id=%s)"
        params = (u["id"],)
    rows = query_posts(db, uid, where, params, limit=50)
    return jsonify({"locked": False, "posts": hydrate(rows, db, uid)})


@app.post("/api/follow/<username>")
@token_required
def follow(username):
    db = get_db()
    uid = g.uid
    u = db.execute("SELECT * FROM users WHERE username=%s", (username,)).fetchone()
    if not u:
        return err("This account doesn't exist.", 404)
    if u["id"] == uid:
        return err("You can't follow yourself.")
    f = db.execute("SELECT * FROM follows WHERE follower_id=%s AND following_id=%s", (uid, u["id"])).fetchone()
    if not f:
        status = "pending" if u["is_private"] else "accepted"
        db.execute("INSERT INTO follows (follower_id, following_id, status) VALUES (%s,%s,%s)", (uid, u["id"], status))
        notify(db, u["id"], uid, "follow_request" if status == "pending" else "follow")
    elif f["status"] == "accepted":
        db.execute("DELETE FROM follows WHERE id=%s", (f["id"],))
        status = "none"
    else:
        db.execute("DELETE FROM follows WHERE id=%s", (f["id"],))
        status = "none"
    db.commit()
    return jsonify({"relationship": status})


@app.post("/api/block/<username>")
@token_required
def block(username):
    db = get_db()
    uid = g.uid
    u = db.execute("SELECT id FROM users WHERE username=%s", (username,)).fetchone()
    if not u or u["id"] == uid:
        return err("Can't do that.", 404)
    row = db.execute("SELECT id FROM blocks WHERE blocker_id=%s AND blocked_id=%s", (uid, u["id"])).fetchone()
    if row:
        db.execute("DELETE FROM blocks WHERE id=%s", (row["id"],))
        blocked = False
    else:
        db.execute("INSERT INTO blocks (blocker_id, blocked_id) VALUES (%s,%s)", (uid, u["id"]))
        db.execute("DELETE FROM follows WHERE (follower_id=%s AND following_id=%s) OR (follower_id=%s AND following_id=%s)",
                   (uid, u["id"], u["id"], uid))
        blocked = True
    db.commit()
    return jsonify({"blocked": blocked})


@app.get("/api/suggestions")
@token_required
def suggestions():
    db = get_db()
    uid = g.uid
    rows = db.execute(
        """SELECT u.* FROM users u
           WHERE u.id <> %s AND u.id NOT IN (SELECT following_id FROM follows WHERE follower_id=%s)
           ORDER BY (SELECT COUNT(*) FROM follows f WHERE f.following_id=u.id) DESC LIMIT 3""",
        (uid, uid),
    ).fetchall()
    return jsonify({"users": [user_dict(r, db, uid) for r in rows]})


@app.get("/api/search")
@token_required
def search():
    q = (request.args.get("q") or "").strip()
    db = get_db()
    uid = g.uid
    if not q:
        return jsonify({"users": [], "posts": []})
    like = f"%{q.lstrip('#@')}%"
    users = db.execute(
        "SELECT * FROM users WHERE username ILIKE %s OR display_name ILIKE %s LIMIT 8", (like, like)
    ).fetchall()
    rows = query_posts(
        db, uid,
        where="(p.body ILIKE %s AND p.visibility='public' AND u.is_private=FALSE)",
        params=(like,), limit=25,
    )
    return jsonify({"users": [user_dict(r, db, uid) for r in users], "posts": hydrate(rows, db, uid)})


@app.get("/api/trends")
@token_required
def trends():
    db = get_db()
    rows = db.execute(
        """SELECT p.body,
                  (SELECT COUNT(*) FROM likes l WHERE l.post_id=p.id)
                + (SELECT COUNT(*) FROM comments c WHERE c.post_id=p.id)
                + (SELECT COUNT(*) FROM reposts r WHERE r.post_id=p.id) AS eng
           FROM posts p
           WHERE p.deleted_at IS NULL AND p.created_at > now() - interval '7 days'"""
    ).fetchall()
    posts_by_tag, eng_by_tag = Counter(), Counter()
    for r in rows:
        for tag in set(re.findall(r"#(\w+)", r["body"])):
            posts_by_tag[tag] += 1
            eng_by_tag[tag] += r["eng"]
    top = sorted(posts_by_tag, key=lambda t: (eng_by_tag[t], posts_by_tag[t]), reverse=True)[:6]
    return jsonify({"trends": [{"tag": t, "posts": posts_by_tag[t], "engagement": eng_by_tag[t]} for t in top]})

# ------------------------------------------------------------- notifications

@app.get("/api/notifications")
@token_required
def notifications():
    db = get_db()
    uid = g.uid
    rows = db.execute(
        """SELECT n.id, n.type, n.post_id, n.created_at,
                  u.username, u.display_name, u.avatar_url
           FROM notifications n JOIN users u ON u.id = n.actor_id
           WHERE n.recipient_id=%s ORDER BY n.id DESC LIMIT 100""",
        (uid,),
    ).fetchall()
    groups = OrderedDict()
    for r in rows:
        key = (r["type"], r["post_id"])
        grp = groups.setdefault(key, {"type": r["type"], "post_id": r["post_id"],
                                      "actors": [], "created_at": r["created_at"]})
        if len(grp["actors"]) < 4:
            grp["actors"].append({"username": r["username"], "display_name": r["display_name"], "avatar_url": r["avatar_url"]})
        grp["count"] = grp.get("count", 0) + 1
    out = []
    for grp in groups.values():
        snippet = None
        if grp["post_id"]:
            p = db.execute("SELECT body FROM posts WHERE id=%s", (grp["post_id"],)).fetchone()
            snippet = (p["body"][:90] + "…") if p and len(p["body"]) > 90 else (p["body"] if p else None)
        grp["snippet"] = snippet
        out.append(grp)
    db.execute("UPDATE notifications SET is_read = TRUE WHERE recipient_id=%s", (uid,))
    db.commit()
    return jsonify({"groups": out})

@app.get("/api/follow-requests")
@token_required
def follow_requests():
    db = get_db()
    rows = db.execute(
        """SELECT u.id, u.username, u.display_name, u.avatar_url
           FROM follows f JOIN users u ON u.id = f.follower_id
           WHERE f.following_id=%s AND f.status='pending'
           ORDER BY f.created_at DESC LIMIT 30""", (g.uid,)).fetchall()
    return jsonify({"requests": [dict(r) for r in rows]})

@app.post("/api/follow-requests/<username>/accept")
@token_required
def accept_request(username):
    db = get_db()
    u = db.execute("SELECT id FROM users WHERE username=%s", (username,)).fetchone()
    if not u:
        return err("User not found.", 404)
    db.execute("UPDATE follows SET status='accepted' WHERE follower_id=%s AND following_id=%s AND status='pending'",
               (u["id"], g.uid))
    notify(db, u["id"], g.uid, "request_accepted")
    db.commit()
    return jsonify({"ok": True})


@app.post("/api/follow-requests/<username>/deny")
@token_required
def deny_request(username):
    db = get_db()
    u = db.execute("SELECT id FROM users WHERE username=%s", (username,)).fetchone()
    if not u:
        return err("User not found.", 404)
    db.execute("DELETE FROM follows WHERE follower_id=%s AND following_id=%s AND status='pending'",
               (u["id"], g.uid))
    db.commit()
    return jsonify({"ok": True})


@app.get("/api/notifications/latest")
@token_required
def latest_notification():
    db = get_db()
    r = db.execute(
        """SELECT n.type, n.post_id, n.created_at, u.display_name, u.username
           FROM notifications n JOIN users u ON u.id = n.actor_id
           WHERE n.recipient_id=%s ORDER BY n.id DESC LIMIT 1""", (g.uid,)).fetchone()
    return jsonify({"notification": dict(r) if r else None})


@app.get("/api/badges")
@token_required
def badges():
    db = get_db()
    uid = g.uid
    n = db.execute("SELECT COUNT(*) AS count FROM notifications WHERE recipient_id=%s AND is_read=FALSE", (uid,)).fetchone()["count"]
    m = db.execute(
        """SELECT COUNT(*) AS count FROM messages msg
           JOIN conversation_members cm ON cm.conversation_id = msg.conversation_id AND cm.user_id = %s
           WHERE msg.sender_id <> %s AND msg.created_at > COALESCE(cm.last_read_at, 'epoch'::timestamptz)""",
        (uid, uid),
    ).fetchone()["count"]
    return jsonify({"notifications": n, "messages": m})

# ------------------------------------------------------------- messages

def _convo_members(db, cid):
    return [r["user_id"] for r in db.execute("SELECT user_id FROM conversation_members WHERE conversation_id=%s", (cid,))]


@app.get("/api/conversations")
@token_required
def conversations():
    db = get_db()
    uid = g.uid
    cids = [r["conversation_id"] for r in db.execute("SELECT conversation_id FROM conversation_members WHERE user_id=%s", (uid,))]
    out = []
    for cid in cids:
        members = db.execute(
            "SELECT u.* FROM conversation_members cm JOIN users u ON u.id=cm.user_id WHERE cm.conversation_id=%s AND u.id<>%s",
            (cid, uid),
        ).fetchall()
        other = members[0] if members else None
        last = db.execute(
            "SELECT body, photo_path, created_at FROM messages WHERE conversation_id=%s ORDER BY id DESC LIMIT 1", (cid,)
        ).fetchone()
        member_row = db.execute("SELECT last_read_at FROM conversation_members WHERE conversation_id=%s AND user_id=%s", (cid, uid)).fetchone()
        unread = db.execute(
            """SELECT COUNT(*) AS count FROM messages WHERE conversation_id=%s AND sender_id<>%s
               AND created_at > COALESCE(%s, 'epoch'::timestamptz)""",
            (cid, uid, member_row["last_read_at"]),
        ).fetchone()["count"]
        out.append({
            "id": cid,
            "name": other["display_name"] if other else "Conversation",
            "username": other["username"] if other else "",
            "avatar": other["avatar_url"] if other else "",
            "last": ("📷 Photo" if last and last["photo_path"] else (last["body"] if last else "")),
            "last_at": last["created_at"] if last else "",
            "unread": unread,
        })
    out.sort(key=lambda c: c["last_at"] or "", reverse=True)
    return jsonify({"conversations": out})


@app.post("/api/conversations")
@token_required
def start_conversation():
    d = request.get_json(force=True)
    db = get_db()
    uid = g.uid
    u = db.execute("SELECT * FROM users WHERE username=%s", ((d.get("username") or "").strip(),)).fetchone()
    if not u:
        return err("That user doesn't exist.", 404)
    if u["id"] == uid:
        return err("You can't message yourself.")
    for r in db.execute("SELECT conversation_id FROM conversation_members WHERE user_id=%s", (uid,)):
        cid = r["conversation_id"]
        members = _convo_members(db, cid)
        if len(members) == 2 and u["id"] in members:
            return jsonify({"id": cid})
    cid = db.execute("INSERT INTO conversations (created_by) VALUES (%s) RETURNING id", (uid,)).fetchone()["id"]
    db.execute("INSERT INTO conversation_members (conversation_id, user_id) VALUES (%s,%s)", (cid, uid))
    db.execute("INSERT INTO conversation_members (conversation_id, user_id) VALUES (%s,%s)", (cid, u["id"]))
    db.commit()
    return jsonify({"id": cid})


@app.get("/api/conversations/<int:cid>/messages")
@token_required
def conversation_messages(cid):
    db = get_db()
    uid = g.uid
    if uid not in _convo_members(db, cid):
        return err("You are not part of this conversation.", 403)
    rows = db.execute(
        """SELECT m.id, m.sender_id, m.body, m.photo_path, m.created_at,
                  u.username, u.display_name, u.avatar_url
           FROM messages m JOIN users u ON u.id = m.sender_id
           WHERE m.conversation_id=%s ORDER BY m.id ASC LIMIT 500""",
        (cid,),
    ).fetchall()
    db.execute("UPDATE conversation_members SET last_read_at = now() WHERE conversation_id=%s AND user_id=%s", (cid, uid))
    db.commit()
    return jsonify({"messages": [dict(r) for r in rows]})


@app.post("/api/conversations/<int:cid>/messages")
@token_required
def send_message(cid):
    db = get_db()
    uid = g.uid
    if uid not in _convo_members(db, cid):
        return err("You are not part of this conversation.", 403)
    d = request.get_json(force=True)
    body = (d.get("body") or "").strip()
    photo = (d.get("photo_path") or "").strip()
    if photo and not photo.startswith(STORAGE_PREFIX):
        return err("Invalid photo.")
    if not body and not photo:
        return err("Message is empty.")
    if len(body) > 1000:
        return err("Messages are limited to 1000 characters.")
    mid = db.execute(
        "INSERT INTO messages (conversation_id, sender_id, body, photo_path) VALUES (%s,%s,%s,%s) RETURNING id",
        (cid, uid, body, photo),
    ).fetchone()["id"]
    db.commit()
    return jsonify({"id": mid})

# ------------------------------------------------------------- reports

@app.post("/api/report")
@token_required
def report():
    d = request.get_json(force=True)
    db = get_db()
    db.execute(
        "INSERT INTO reports (reporter_id, target_type, target_id, reason) VALUES (%s,%s,%s,%s)",
        (g.uid, d.get("target_type", ""), int(d.get("target_id") or 0), (d.get("reason") or "")[:200]),
    )
    db.commit()
    return jsonify({"ok": True})

# ------------------------------------------------------------- routes

@app.get("/")
def index():
    return render_template("index.html")

# ------------------------------------------------------------- startup

def _boot():
    missing = [k for k, v in {
        "SUPABASE_URL": SUPABASE_URL, "SUPABASE_DB_URL": DB_DSN,
        "SUPABASE_SERVICE_KEY": SERVICE_KEY, "SUPABASE_JWT_SECRET": JWT_SECRET}.items() if not v]
    if missing:
        raise SystemExit(f"\n[Spill] Missing environment variables: {', '.join(missing)}\n")
    init_db()
    print(f"[Spill] ready — talking to {SUPABASE_URL}")

_boot()  # runs on import → works for gunicorn AND `python app.py`

if __name__ == "__main__":
    # Local development only
    app.run(host="0.0.0.0", port=int(os.environ.get("PORT", "5000")))
