from flask import Flask, render_template, request, Response, redirect, url_for, session, jsonify, g
from werkzeug.security import generate_password_hash, check_password_hash
from functools import wraps
import openai
import json
import os
import psycopg2
import psycopg2.extras
from datetime import timedelta

app = Flask(__name__)
app.secret_key = os.environ.get("SECRET_KEY", "dev-secret-change-me")
app.config["MAX_CONTENT_LENGTH"] = 20 * 1024 * 1024  # 20MB max upload
app.config["PERMANENT_SESSION_LIFETIME"] = timedelta(days=30)

DATABASE_URL = os.environ.get("DATABASE_URL")

client = openai.OpenAI(
    api_key=os.environ.get("POE_API_KEY"),
    base_url="https://api.poe.com/v1",
)

ADMIN_EMAIL = os.environ.get("ADMIN_EMAIL", "admin@example.com")
ADMIN_PASSWORD = os.environ.get("ADMIN_PASSWORD", "admin123")

MODELS = [
    # (id, label)
    ("GPT-4o", "GPT-4o — Fast & smart"),
    ("GPT-4o-mini", "GPT-4o-mini — Lightweight"),
    ("GPT-5.4", "GPT-5.4 — Most capable"),
    ("Claude-Opus-4.7", "Claude Opus 4.7 — Deep reasoning"),
    ("Claude-Sonnet-4.6", "Claude Sonnet 4.6 — Balanced"),
    ("Claude-Haiku-4.5", "Claude Haiku 4.5 — Fast"),
    ("Gemini-3.1-Pro", "Gemini 3.1 Pro — Google flagship"),
    ("Gemini-2.5-Pro", "Gemini 2.5 Pro — Advanced"),
    ("Gemini-2.5-Flash", "Gemini 2.5 Flash — Quick"),
    ("Grok-4", "Grok 4 — xAI"),
    ("Grok-4.20-Multi-Agent", "Grok 4.20 — Multi-agent"),
    ("Llama-4-Maverick", "Llama 4 Maverick — Open source"),
    ("DeepSeek-R1", "DeepSeek R1 — Reasoning"),
    ("Nano-Banana-Pro", "Nano Banana Pro — Image generation"),
    ("GPT-Image-1.5", "GPT Image 1.5 — Image generation"),
    ("Veo-3.1", "Veo 3.1 — Video generation"),
]

# ── Database ──

def get_db():
    if "db" not in g:
        g.db = psycopg2.connect(DATABASE_URL)
        g.db.autocommit = False
    return g.db

def db_execute(query, params=None):
    db = get_db()
    cur = db.cursor(cursor_factory=psycopg2.extras.RealDictCursor)
    cur.execute(query, params)
    return cur

def db_fetchone(query, params=None):
    cur = db_execute(query, params)
    return cur.fetchone()

def db_fetchall(query, params=None):
    cur = db_execute(query, params)
    return cur.fetchall()

def db_commit():
    get_db().commit()

@app.teardown_appcontext
def close_db(exc):
    db = g.pop("db", None)
    if db:
        db.close()

def init_db():
    conn = psycopg2.connect(DATABASE_URL)
    cur = conn.cursor()
    cur.execute("""
        CREATE TABLE IF NOT EXISTS users (
            id SERIAL PRIMARY KEY,
            email TEXT UNIQUE NOT NULL,
            password_hash TEXT NOT NULL,
            approved INTEGER DEFAULT 0,
            is_admin INTEGER DEFAULT 0,
            created_at TIMESTAMPTZ DEFAULT NOW()
        );
    """)
    cur.execute("""
        CREATE TABLE IF NOT EXISTS conversations (
            id SERIAL PRIMARY KEY,
            user_id INTEGER NOT NULL REFERENCES users(id),
            title TEXT DEFAULT 'New Chat',
            model TEXT DEFAULT 'GPT-4o',
            created_at TIMESTAMPTZ DEFAULT NOW()
        );
    """)
    cur.execute("""
        CREATE TABLE IF NOT EXISTS messages (
            id SERIAL PRIMARY KEY,
            conversation_id INTEGER NOT NULL REFERENCES conversations(id),
            role TEXT NOT NULL,
            content TEXT NOT NULL,
            input_tokens INTEGER DEFAULT 0,
            output_tokens INTEGER DEFAULT 0,
            created_at TIMESTAMPTZ DEFAULT NOW()
        );
    """)
    # Add token columns if they don't exist (for existing databases)
    for col in ("input_tokens", "output_tokens"):
        try:
            cur.execute(f"ALTER TABLE messages ADD COLUMN {col} INTEGER DEFAULT 0")
        except Exception:
            conn.rollback()
    conn.commit()
    # Ensure admin exists
    cur.execute("SELECT id FROM users WHERE email = %s", (ADMIN_EMAIL,))
    if not cur.fetchone():
        cur.execute(
            "INSERT INTO users (email, password_hash, approved, is_admin) VALUES (%s, %s, 1, 1)",
            (ADMIN_EMAIL, generate_password_hash(ADMIN_PASSWORD)),
        )
        conn.commit()
    cur.close()
    conn.close()

init_db()

# ── Auth helpers ──

def login_required(f):
    @wraps(f)
    def decorated(*args, **kwargs):
        if "user_id" not in session:
            return redirect(url_for("login"))
        user = db_fetchone("SELECT * FROM users WHERE id = %s", (session["user_id"],))
        if not user or not user["approved"]:
            session.clear()
            return redirect(url_for("login"))
        g.user = user
        return f(*args, **kwargs)
    return decorated

def admin_required(f):
    @wraps(f)
    def decorated(*args, **kwargs):
        if "user_id" not in session:
            return redirect(url_for("login"))
        user = db_fetchone("SELECT * FROM users WHERE id = %s", (session["user_id"],))
        if not user or not user["is_admin"]:
            return redirect(url_for("index"))
        g.user = user
        return f(*args, **kwargs)
    return decorated

# ── Auth routes ──

@app.route("/login", methods=["GET", "POST"])
def login():
    error = None
    if request.method == "POST":
        email = request.form["email"].strip().lower()
        password = request.form["password"]
        user = db_fetchone("SELECT * FROM users WHERE email = %s", (email,))
        if not user or not check_password_hash(user["password_hash"], password):
            error = "Invalid email or password."
        elif not user["approved"]:
            error = "Your account is pending approval."
        else:
            session.permanent = True
            session["user_id"] = user["id"]
            return redirect(url_for("index"))
    return render_template("login.html", error=error)

@app.route("/register", methods=["GET", "POST"])
def register():
    error = None
    success = False
    if request.method == "POST":
        email = request.form["email"].strip().lower()
        password = request.form["password"]
        if len(password) < 6:
            error = "Password must be at least 6 characters."
        else:
            existing = db_fetchone("SELECT id FROM users WHERE email = %s", (email,))
            if existing:
                error = "Email already registered."
            else:
                db_execute(
                    "INSERT INTO users (email, password_hash) VALUES (%s, %s)",
                    (email, generate_password_hash(password)),
                )
                db_commit()
                success = True
    return render_template("register.html", error=error, success=success)

@app.route("/change-password", methods=["GET", "POST"])
@login_required
def change_password():
    error = None
    success = False
    if request.method == "POST":
        current = request.form["current_password"]
        new_pw = request.form["new_password"]
        user = db_fetchone("SELECT * FROM users WHERE id = %s", (session["user_id"],))
        if not check_password_hash(user["password_hash"], current):
            error = "Current password is incorrect."
        elif len(new_pw) < 6:
            error = "New password must be at least 6 characters."
        else:
            db_execute("UPDATE users SET password_hash = %s WHERE id = %s", (generate_password_hash(new_pw), session["user_id"]))
            db_commit()
            success = True
    return render_template("change_password.html", error=error, success=success)

@app.route("/pricing")
def pricing():
    return render_template("pricing.html")

@app.route("/logout")
def logout():
    session.clear()
    return redirect(url_for("login"))

# ── Admin ──

@app.route("/admin")
@admin_required
def admin():
    users = db_fetchall("""
        SELECT u.*,
            (SELECT COUNT(*) FROM conversations WHERE user_id = u.id) as conv_count,
            (SELECT COUNT(*) FROM messages WHERE conversation_id IN
                (SELECT id FROM conversations WHERE user_id = u.id) AND role = 'user') as msg_count,
            (SELECT COUNT(*) FROM messages WHERE conversation_id IN
                (SELECT id FROM conversations WHERE user_id = u.id) AND role = 'assistant') as reply_count,
            (SELECT COALESCE(SUM(input_tokens), 0) FROM messages WHERE conversation_id IN
                (SELECT id FROM conversations WHERE user_id = u.id)) as total_input_tokens,
            (SELECT COALESCE(SUM(output_tokens), 0) FROM messages WHERE conversation_id IN
                (SELECT id FROM conversations WHERE user_id = u.id)) as total_output_tokens
        FROM users u ORDER BY u.created_at DESC
    """)
    return render_template("admin.html", users=users)

@app.route("/admin/approve/<int:user_id>", methods=["POST"])
@admin_required
def approve_user(user_id):
    db_execute("UPDATE users SET approved = 1 WHERE id = %s", (user_id,))
    db_commit()
    return redirect(url_for("admin"))

@app.route("/admin/revoke/<int:user_id>", methods=["POST"])
@admin_required
def revoke_user(user_id):
    db_execute("UPDATE users SET approved = 0 WHERE id = %s AND is_admin = 0", (user_id,))
    db_commit()
    return redirect(url_for("admin"))

@app.route("/admin/delete/<int:user_id>", methods=["POST"])
@admin_required
def delete_user(user_id):
    db_execute("DELETE FROM messages WHERE conversation_id IN (SELECT id FROM conversations WHERE user_id = %s)", (user_id,))
    db_execute("DELETE FROM conversations WHERE user_id = %s", (user_id,))
    db_execute("DELETE FROM users WHERE id = %s AND is_admin = 0", (user_id,))
    db_commit()
    return redirect(url_for("admin"))

# ── Chat ──

@app.route("/")
@login_required
def index():
    conversations = db_fetchall(
        "SELECT * FROM conversations WHERE user_id = %s ORDER BY created_at DESC",
        (session["user_id"],),
    )
    return render_template("chat.html", models=MODELS, conversations=conversations, user=g.user)

@app.route("/conversation/new", methods=["POST"])
@login_required
def new_conversation():
    data = request.json
    model = data.get("model", "GPT-4o")
    row = db_fetchone(
        "INSERT INTO conversations (user_id, model) VALUES (%s, %s) RETURNING id",
        (session["user_id"], model),
    )
    db_commit()
    return jsonify({"id": row["id"]})

@app.route("/conversation/<int:conv_id>")
@login_required
def get_conversation(conv_id):
    conv = db_fetchone(
        "SELECT * FROM conversations WHERE id = %s AND user_id = %s",
        (conv_id, session["user_id"]),
    )
    if not conv:
        return jsonify({"error": "not found"}), 404
    msgs = db_fetchall(
        "SELECT role, content FROM messages WHERE conversation_id = %s ORDER BY created_at",
        (conv_id,),
    )
    return jsonify({
        "id": conv["id"],
        "title": conv["title"],
        "model": conv["model"],
        "messages": [{"role": m["role"], "content": m["content"]} for m in msgs],
    })

@app.route("/conversation/<int:conv_id>/model", methods=["POST"])
@login_required
def update_model(conv_id):
    data = request.json
    db_execute(
        "UPDATE conversations SET model = %s WHERE id = %s AND user_id = %s",
        (data["model"], conv_id, session["user_id"]),
    )
    db_commit()
    return jsonify({"ok": True})

@app.route("/conversation/<int:conv_id>", methods=["DELETE"])
@login_required
def delete_conversation(conv_id):
    db_execute("DELETE FROM messages WHERE conversation_id = %s AND conversation_id IN (SELECT id FROM conversations WHERE user_id = %s)", (conv_id, session["user_id"]))
    db_execute("DELETE FROM conversations WHERE id = %s AND user_id = %s", (conv_id, session["user_id"]))
    db_commit()
    return jsonify({"ok": True})

@app.route("/chat", methods=["POST"])
@login_required
def chat():
    data = request.json
    conv_id = data.get("conversation_id")
    user_message = data.get("message", "")
    images = data.get("images", [])
    model = data.get("model", "GPT-4o")

    conv = db_fetchone(
        "SELECT * FROM conversations WHERE id = %s AND user_id = %s",
        (conv_id, session["user_id"]),
    )
    if not conv:
        return jsonify({"error": "not found"}), 404

    # Build content for storage
    storage_content = user_message
    if images:
        storage_content = json.dumps({"text": user_message, "images": images})

    # Save user message
    db_execute(
        "INSERT INTO messages (conversation_id, role, content) VALUES (%s, 'user', %s)",
        (conv_id, storage_content),
    )

    # Auto-title on first message
    row = db_fetchone("SELECT COUNT(*) as c FROM messages WHERE conversation_id = %s", (conv_id,))
    if row["c"] == 1:
        title_text = user_message or "Image"
        title = title_text[:40] + ("..." if len(title_text) > 40 else "")
        db_execute("UPDATE conversations SET title = %s WHERE id = %s", (title, conv_id))
    db_commit()

    # Get full history
    msgs = db_fetchall(
        "SELECT role, content FROM messages WHERE conversation_id = %s ORDER BY created_at",
        (conv_id,),
    )

    api_messages = []
    for m in msgs:
        content_raw = m["content"]
        try:
            parsed = json.loads(content_raw)
            if isinstance(parsed, dict) and "images" in parsed:
                parts = []
                if parsed.get("text"):
                    parts.append({"type": "text", "text": parsed["text"]})
                for img_uri in parsed["images"]:
                    parts.append({"type": "image_url", "image_url": {"url": img_uri}})
                api_messages.append({"role": m["role"], "content": parts})
                continue
        except (json.JSONDecodeError, TypeError):
            pass
        api_messages.append({"role": m["role"], "content": content_raw})

    MEDIA_MODELS = {"Nano-Banana-Pro", "GPT-Image-1.5", "Veo-3.1"}
    is_media = model in MEDIA_MODELS

    def generate():
        full_response = ""
        input_tokens = 0
        output_tokens = 0
        try:
            if is_media:
                # Non-streaming for image/video models
                response = client.chat.completions.create(
                    model=model,
                    messages=api_messages,
                    stream=False,
                )
                full_response = response.choices[0].message.content or ""
                if response.usage:
                    input_tokens = response.usage.prompt_tokens or 0
                    output_tokens = response.usage.completion_tokens or 0
                yield f"data: {json.dumps({'content': full_response})}\n\n"
            else:
                # Streaming for text models
                stream = client.chat.completions.create(
                    model=model,
                    messages=api_messages,
                    stream=True,
                    stream_options={"include_usage": True},
                )
                for chunk in stream:
                    if chunk.choices and chunk.choices[0].delta and chunk.choices[0].delta.content:
                        content = chunk.choices[0].delta.content
                        full_response += content
                        yield f"data: {json.dumps({'content': content})}\n\n"
                    if hasattr(chunk, 'usage') and chunk.usage:
                        input_tokens = getattr(chunk.usage, 'prompt_tokens', 0) or 0
                        output_tokens = getattr(chunk.usage, 'completion_tokens', 0) or 0
            # Save assistant response with token usage
            conn = psycopg2.connect(DATABASE_URL)
            cur = conn.cursor()
            cur.execute(
                "INSERT INTO messages (conversation_id, role, content, input_tokens, output_tokens) VALUES (%s, 'assistant', %s, %s, %s)",
                (conv_id, full_response, input_tokens, output_tokens),
            )
            conn.commit()
            cur.close()
            conn.close()
            yield "data: [DONE]\n\n"
        except Exception as e:
            yield f"data: {json.dumps({'error': str(e)})}\n\n"

    return Response(generate(), mimetype="text/event-stream")


if __name__ == "__main__":
    app.run(debug=True, port=5000)
