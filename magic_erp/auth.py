import functools

import click
from flask import Blueprint, abort, flash, g, redirect, render_template, request, session, url_for
from werkzeug.security import check_password_hash, generate_password_hash

from .db import get_db
from .services import ROLE_LABELS

bp = Blueprint("auth", __name__)


@bp.before_app_request
def load_user():
    uid = session.get("user_id")
    g.user = get_db().execute("SELECT * FROM users WHERE id = ?", (uid,)).fetchone() if uid else None


@bp.route("/login", methods=["GET", "POST"])
def login():
    if request.method == "POST":
        user = get_db().execute(
            "SELECT * FROM users WHERE username = ?", (request.form["username"].strip(),)
        ).fetchone()
        if user and check_password_hash(user["password_hash"], request.form["password"]):
            session.clear()
            session["user_id"] = user["id"]
            return redirect(request.args.get("next") or url_for("main.dashboard"))
        flash("帳號或密碼錯誤", "error")
    return render_template("login.html")


@bp.route("/logout")
def logout():
    session.clear()
    return redirect(url_for("auth.login"))


def login_required(view):
    @functools.wraps(view)
    def wrapped(*args, **kwargs):
        if g.user is None:
            return redirect(url_for("auth.login", next=request.path))
        return view(*args, **kwargs)

    return wrapped


def can(*roles):
    """目前使用者是否具備任一角色；admin 永遠可以。"""
    return g.user is not None and (g.user["role"] == "admin" or g.user["role"] in roles)


def roles_required(*roles):
    def decorator(view):
        @functools.wraps(view)
        @login_required
        def wrapped(*args, **kwargs):
            if not can(*roles):
                abort(403)
            return view(*args, **kwargs)

        return wrapped

    return decorator


@bp.app_context_processor
def inject_can():
    return {"can": can}


def create_user(conn, username, display_name, role, password):
    if role not in ROLE_LABELS:
        raise ValueError(f"未知角色：{role}")
    conn.execute(
        "INSERT INTO users (username, display_name, role, password_hash) VALUES (?, ?, ?, ?)",
        (username, display_name, role, generate_password_hash(password)),
    )


@click.command("create-user")
@click.argument("username")
@click.argument("role", type=click.Choice(list(ROLE_LABELS)))
@click.option("--name", default=None, help="顯示名稱")
@click.password_option()
def create_user_command(username, role, name, password):
    """建立使用者帳號。"""
    conn = get_db()
    create_user(conn, username, name or username, role, password)
    conn.commit()
    click.echo(f"已建立 {username}（{ROLE_LABELS[role]}）")
