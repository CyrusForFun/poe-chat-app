from flask import Flask, render_template, request, Response, redirect, url_for, session, jsonify, g
from werkzeug.security import generate_password_hash, check_password_hash
from functools import wraps
import openai
import json
import os
import sqlite3
import base64
import time

from datetime import timedelta

app = Flask(__name__)
app.secret_key = os.environ.get("SECRET_KEY", "dev-secret-change-me")
app.config["MAX_CONTENT_LENGTH"] = 20 * 1024 * 1024  # 20MB max upload
app.config["PERMANENT_SESSION_LIFETIME"] = timedelta(days=30)

DATABASE = os.path.join(os.path.dirname(os.path.abspath(__file__)), "db.sqlite3")

client = openai.OpenAI(
    api_key=os.environ.get("POE_API_KEY"),
    base_url="https://api.poe.com/v1",
)

ADMIN_EMAIL = os.environ.get("ADMIN_EMAIL", "admin@example.com")
ADMIN_PASSWORD = os.environ.get("ADMIN_PASSWORD", "admin123")

MODELS = [
    # OpenAI
    "GPT-4o",
    "GPT-4o-mini",
    "GPT-5.4",
    # Anthropic
    "Claude-Opus-4.7",
    "Claude-Sonnet-4.6",
    "Claude-Haiku-4.5",
    "GPT-Image-1.5",
    # Google
    "Gemini-3.1-Pro",
    "Gemini-2.5-Pro",
    "Gemini-2.5-Flash",
    # xAI
    "Grok-4",
    "Grok-4.20-Multi-Agent",
    # Meta
    "Llama-4-Maverick",
    # DeepSeek
    "DeepSeek-R1",
    # Image
    "Nano-Banana-Pro",
    # Video
    "Veo-3.1",
]

# ── Database ──

def get_db():
    if "db" not in g:
        g.db = sqlite3.connect(DATABASE)
        g.db.row_factory = sqlite3.Row
    return g.db

@app.teardown_appcontext
def close_db(exc):
    db = g.pop("db", None)
    if db:
        db.close()

def init_db():
    db = sqlite3.connect(DATABASE)
    db.executescript("""
        CREATE TABLE IF NOT EXISTS users (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            email TEXT UNIQUE NOT NULL,
            password_hash TEXT NOT NULL,
            approved INTEGER DEFAULT 0,
            is_admin INTEGER DEFAULT 0,
            created_at REAL DEFAULT (unixepoch())
        );
        CREATE TABLE IF NOT EXISTS conversations (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            user_id INTEGER NOT NULL,
            title TEXT DEFAULT 'New Chat',
            model TEXT DEFAULT 'Claude-Sonnet-4.6',
            created_at REAL DEFAULT (unixepoch()),
            FOREIGN KEY (user_id) REFERENCES users(id)
        );
        CREATE TABLE IF NOT EXISTS messages (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            conversation_id INTEGER NOT NULL,
            role TEXT NOT NULL,
            content TEXT NOT NULL,
            created_at REAL DEFAULT (unixepoch()),
            FOREIGN KEY (conversation_id) REFERENCES conversations(id)
        );
    """)
    # Ensure admin exists
    admin = db.execute("SELECT id FROM users WHERE email = ?", (ADMIN_EMAIL,)).fetchone()
    if not admin:
        db.execute(
            "INSERT INTO users (email, password_hash, approved, is_admin) VALUES (?, ?, 1, 1)",
            (ADMIN_EMAIL, generate_password_hash(ADMIN_PASSWORD)),
        )
        db.commit()
    db.close()

init_db()

# ── Auth helpers ──

def login_required(f):
    @wraps(f)
    def decorated(*args, **kwargs):
        if "user_id" not in session:
            return redirect(url_for("login"))
        db = get_db()
        user = db.execute("SELECT * FROM users WHERE id = ?", (session["user_id"],)).fetchone()
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
        db = get_db()
        user = db.execute("SELECT * FROM users WHERE id = ?", (session["user_id"],)).fetchone()
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
        db = get_db()
        user = db.execute("SELECT * FROM users WHERE email = ?", (email,)).fetchone()
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
            db = get_db()
            existing = db.execute("SELECT id FROM users WHERE email = ?", (email,)).fetchone()
            if existing:
                error = "Email already registered."
            else:
                db.execute(
                    "INSERT INTO users (email, password_hash) VALUES (?, ?)",
                    (email, generate_password_hash(password)),
                )
                db.commit()
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
        db = get_db()
        user = db.execute("SELECT * FROM users WHERE id = ?", (session["user_id"],)).fetchone()
        if not check_password_hash(user["password_hash"], current):
            error = "Current password is incorrect."
        elif len(new_pw) < 6:
            error = "New password must be at least 6 characters."
        else:
            db.execute("UPDATE users SET password_hash = ? WHERE id = ?", (generate_password_hash(new_pw), session["user_id"]))
            db.commit()
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
    db = get_db()
    users = db.execute("""
        SELECT u.*,
            (SELECT COUNT(*) FROM conversations WHERE user_id = u.id) as conv_count,
            (SELECT COUNT(*) FROM messages WHERE conversation_id IN
                (SELECT id FROM conversations WHERE user_id = u.id) AND role = 'user') as msg_count,
            (SELECT COUNT(*) FROM messages WHERE conversation_id IN
                (SELECT id FROM conversations WHERE user_id = u.id) AND role = 'assistant') as reply_count
        FROM users u ORDER BY u.created_at DESC
    """).fetchall()
    return render_template("admin.html", users=users)

@app.route("/admin/approve/<int:user_id>", methods=["POST"])
@admin_required
def approve_user(user_id):
    db = get_db()
    db.execute("UPDATE users SET approved = 1 WHERE id = ?", (user_id,))
    db.commit()
    return redirect(url_for("admin"))

@app.route("/admin/revoke/<int:user_id>", methods=["POST"])
@admin_required
def revoke_user(user_id):
    db = get_db()
    db.execute("UPDATE users SET approved = 0 WHERE id = ? AND is_admin = 0", (user_id,))
    db.commit()
    return redirect(url_for("admin"))

@app.route("/admin/delete/<int:user_id>", methods=["POST"])
@admin_required
def delete_user(user_id):
    db = get_db()
    db.execute("DELETE FROM messages WHERE conversation_id IN (SELECT id FROM conversations WHERE user_id = ?)", (user_id,))
    db.execute("DELETE FROM conversations WHERE user_id = ?", (user_id,))
    db.execute("DELETE FROM users WHERE id = ? AND is_admin = 0", (user_id,))
    db.commit()
    return redirect(url_for("admin"))

# ── Chat ──

@app.route("/")
@login_required
def index():
    db = get_db()
    conversations = db.execute(
        "SELECT * FROM conversations WHERE user_id = ? ORDER BY created_at DESC",
        (session["user_id"],),
    ).fetchall()
    return render_template("chat.html", models=MODELS, conversations=conversations, user=g.user)

@app.route("/conversation/new", methods=["POST"])
@login_required
def new_conversation():
    data = request.json
    model = data.get("model", "Claude-Sonnet-4.6")
    db = get_db()
    cur = db.execute(
        "INSERT INTO conversations (user_id, model) VALUES (?, ?)",
        (session["user_id"], model),
    )
    db.commit()
    return jsonify({"id": cur.lastrowid})

@app.route("/conversation/<int:conv_id>")
@login_required
def get_conversation(conv_id):
    db = get_db()
    conv = db.execute(
        "SELECT * FROM conversations WHERE id = ? AND user_id = ?",
        (conv_id, session["user_id"]),
    ).fetchone()
    if not conv:
        return jsonify({"error": "not found"}), 404
    msgs = db.execute(
        "SELECT role, content FROM messages WHERE conversation_id = ? ORDER BY created_at",
        (conv_id,),
    ).fetchall()
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
    db = get_db()
    db.execute(
        "UPDATE conversations SET model = ? WHERE id = ? AND user_id = ?",
        (data["model"], conv_id, session["user_id"]),
    )
    db.commit()
    return jsonify({"ok": True})

@app.route("/conversation/<int:conv_id>", methods=["DELETE"])
@login_required
def delete_conversation(conv_id):
    db = get_db()
    db.execute("DELETE FROM messages WHERE conversation_id = ? AND conversation_id IN (SELECT id FROM conversations WHERE user_id = ?)", (conv_id, session["user_id"]))
    db.execute("DELETE FROM conversations WHERE id = ? AND user_id = ?", (conv_id, session["user_id"]))
    db.commit()
    return jsonify({"ok": True})

@app.route("/chat", methods=["POST"])
@login_required
def chat():
    data = request.json
    conv_id = data.get("conversation_id")
    user_message = data.get("message", "")
    images = data.get("images", [])  # list of base64 data URIs
    model = data.get("model", "Claude-Sonnet-4.6")

    db = get_db()
    conv = db.execute(
        "SELECT * FROM conversations WHERE id = ? AND user_id = ?",
        (conv_id, session["user_id"]),
    ).fetchone()
    if not conv:
        return jsonify({"error": "not found"}), 404

    # Build content for storage (text + image markers)
    storage_content = user_message
    if images:
        storage_content = json.dumps({"text": user_message, "images": images})

    # Save user message
    db.execute(
        "INSERT INTO messages (conversation_id, role, content) VALUES (?, 'user', ?)",
        (conv_id, storage_content),
    )

    # Auto-title on first message
    msg_count = db.execute("SELECT COUNT(*) as c FROM messages WHERE conversation_id = ?", (conv_id,)).fetchone()["c"]
    if msg_count == 1:
        title_text = user_message or "Image"
        title = title_text[:40] + ("..." if len(title_text) > 40 else "")
        db.execute("UPDATE conversations SET title = ? WHERE id = ?", (title, conv_id))
    db.commit()

    # Get full history and build API messages
    msgs = db.execute(
        "SELECT role, content FROM messages WHERE conversation_id = ? ORDER BY created_at",
        (conv_id,),
    ).fetchall()

    api_messages = []
    for m in msgs:
        content_raw = m["content"]
        # Try parsing as JSON (multimodal message)
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

    def generate():
        full_response = ""
        try:
            stream = client.chat.completions.create(
                model=model,
                messages=api_messages,
                stream=True,
            )
            for chunk in stream:
                if chunk.choices and chunk.choices[0].delta.content:
                    content = chunk.choices[0].delta.content
                    full_response += content
                    yield f"data: {json.dumps({'content': content})}\n\n"
            # Save assistant response
            db2 = sqlite3.connect(DATABASE)
            db2.execute(
                "INSERT INTO messages (conversation_id, role, content) VALUES (?, 'assistant', ?)",
                (conv_id, full_response),
            )
            db2.commit()
            db2.close()
            yield "data: [DONE]\n\n"
        except Exception as e:
            yield f"data: {json.dumps({'error': str(e)})}\n\n"

    return Response(generate(), mimetype="text/event-stream")


if __name__ == "__main__":
    app.run(debug=True, port=5000)
