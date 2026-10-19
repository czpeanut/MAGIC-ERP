import os
from pathlib import Path

from flask import Flask

from . import db, services


def create_app(test_config=None):
    app = Flask(__name__, instance_relative_config=True)
    app.config.from_mapping(
        SECRET_KEY=os.environ.get("MAGIC_ERP_SECRET", "dev-change-me"),
        DATABASE=os.environ.get("MAGIC_ERP_DB", str(Path(app.instance_path) / "magic_erp.sqlite3")),
    )
    if test_config:
        app.config.update(test_config)
    Path(app.instance_path).mkdir(parents=True, exist_ok=True)

    with app.app_context():
        conn = db.connect(app.config["DATABASE"])
        db.init_schema(conn)
        conn.close()

    app.teardown_appcontext(db.close_db)
    app.jinja_env.filters["fmt"] = services.fmt
    app.jinja_env.globals.update(
        KIND_LABELS=services.KIND_LABELS,
        BASIS_LABELS=services.BASIS_LABELS,
        MOVE_LABELS=services.MOVE_LABELS,
        SESSION_STATUS=services.SESSION_STATUS,
        PICK_STATUS=services.PICK_STATUS,
        PROCESS_STATUS=services.PROCESS_STATUS,
        ROLE_LABELS=services.ROLE_LABELS,
    )

    from . import auth, views

    app.register_blueprint(auth.bp)
    app.register_blueprint(views.bp)
    app.cli.add_command(auth.create_user_command)
    from .seed import seed_command

    app.cli.add_command(seed_command)
    return app
