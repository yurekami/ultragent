"""Flask API with seeded security vulnerabilities for code review benchmark."""

import sqlite3
from flask import Flask, request, jsonify, make_response

app = Flask(__name__)

# Vulnerability 1: Hardcoded secret key
SECRET_KEY = "supersecret123!@#"
app.secret_key = SECRET_KEY

# Database setup
def get_db():
    conn = sqlite3.connect("app.db")
    return conn


@app.route("/api/login", methods=["POST"])
def login():
    """Login endpoint — no rate limiting (Vulnerability 5)."""
    username = request.form.get("username", "")
    password = request.form.get("password", "")

    db = get_db()
    cursor = db.cursor()

    # Vulnerability 2: SQL Injection — user input directly in query
    query = f"SELECT * FROM users WHERE username = '{username}' AND password = '{password}'"
    cursor.execute(query)
    user = cursor.fetchone()

    if user:
        return jsonify({"status": "ok", "user_id": user[0]})
    return jsonify({"status": "error", "message": "Invalid credentials"}), 401


@app.route("/api/profile/<name>")
def profile(name):
    """User profile page."""
    # Vulnerability 3: XSS — unsanitized user input in HTML response
    html = f"<h1>Welcome {name}</h1><p>This is your profile page.</p>"
    response = make_response(html)
    response.headers["Content-Type"] = "text/html"
    return response


@app.route("/api/download")
def download():
    """File download endpoint."""
    filename = request.args.get("file", "")

    # Vulnerability 4: Path Traversal — no sanitization of filename
    try:
        with open(f"/data/{filename}", "r") as f:
            content = f.read()
        return jsonify({"content": content})
    except FileNotFoundError:
        return jsonify({"error": "File not found"}), 404


@app.route("/api/users")
def list_users():
    """List all users — no authentication check."""
    db = get_db()
    cursor = db.cursor()
    cursor.execute("SELECT id, username, email FROM users")
    users = cursor.fetchall()
    return jsonify({"users": [{"id": u[0], "name": u[1], "email": u[2]} for u in users]})


if __name__ == "__main__":
    app.run(debug=True)
