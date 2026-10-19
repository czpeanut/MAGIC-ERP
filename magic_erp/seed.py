"""範例資料：flask --app magic_erp seed"""

from datetime import date, timedelta

import click

from . import services as svc
from .auth import create_user
from .db import get_db

USERS = [
    ("admin", "系統管理員", "admin"),
    ("designer", "王設計", "designer"),
    ("warehouse", "李倉管", "warehouse"),
    ("teacher", "陳老師", "teacher"),
    ("office", "林行政", "office"),
]

# code, name, spec, unit, kind, safety, source_code, ratio, steps, location
MATERIALS = [
    ("M001", "椴木合板", "60x30cm 3mm", "片", "consumable", 20, None, 1, "", "A-01"),
    ("M002", "椴木小板", "15x15cm 3mm", "片", "consumable", 40, "M001", 0.125, "雷切裁切 → 砂紙去毛邊", "A-02"),
    ("M003", "LED 燈珠", "5mm 白光", "顆", "consumable", 100, None, 1, "", "B-01"),
    ("M004", "鈕扣電池", "CR2032", "顆", "consumable", 50, None, 1, "", "B-02"),
    ("M005", "白膠", "100ml", "瓶", "consumable", 10, None, 1, "", "C-01"),
    ("M006", "美工刀", "小型", "支", "reusable", 0, None, 1, "", "D-01"),
    ("M007", "三用電表", "數位式", "台", "reusable", 0, None, 1, "", "D-02"),
    ("M008", "熱熔膠槍", "20W", "支", "shared", 2, None, 1, "", "E-01"),
    ("M009", "切割墊", "A3", "片", "shared", 5, None, 1, "", "E-02"),
]

LESSONS = [
    ("小夜燈製作 1：認識電路", [("M003", 2, "per_student", 10), ("M004", 1, "per_student", 0), ("M007", 1, "per_group", 0)]),
    ("小夜燈製作 2：外殼組裝", [("M002", 4, "per_student", 5), ("M005", 1, "per_group", 0), ("M006", 1, "per_student", 0)]),
    ("小夜燈製作 3：成果測試", [("M004", 1, "per_student", 0), ("M007", 1, "per_group", 0)]),
]


@click.command("seed")
def seed_command():
    """建立範例資料（僅在資料庫為空時）。"""
    conn = get_db()
    if conn.execute("SELECT COUNT(*) FROM users").fetchone()[0]:
        click.echo("資料庫已有資料，略過。")
        return
    seed(conn)
    conn.commit()
    click.echo("已建立範例資料，所有帳號密碼皆為 1234：" + ", ".join(u[0] for u in USERS))


def seed(conn, today=None):
    today = today or date.today()
    for username, name, role in USERS:
        create_user(conn, username, name, role, "1234")
    uid = {r["username"]: r["id"] for r in conn.execute("SELECT id, username FROM users")}

    ids = {}
    for code, name, spec, unit, kind, safety, src, ratio, steps, loc in MATERIALS:
        cur = conn.execute(
            """INSERT INTO materials (code, name, spec, unit, kind, safety_stock, source_material_id,
               source_ratio, process_steps, location) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
            (code, name, spec, unit, kind, safety, ids.get(src), ratio, steps, loc),
        )
        ids[code] = cur.lastrowid
    for code, qty in [("M001", 30), ("M002", 60), ("M003", 120), ("M004", 40), ("M005", 15),
                      ("M006", 30), ("M007", 8), ("M008", 4), ("M009", 6)]:
        svc.record_move(conn, ids[code], qty, "adjust", note="期初庫存", user_id=uid["warehouse"])

    cur = conn.execute(
        "INSERT INTO courses (code, name, designer_id, description) VALUES (?, ?, ?, ?)",
        ("C101", "創客小夜燈", uid["designer"], "三堂課完成一盞木作 LED 小夜燈"),
    )
    course_id = cur.lastrowid
    lesson_ids = []
    for seq, (title, lines) in enumerate(LESSONS, start=1):
        lid = conn.execute("INSERT INTO lessons (course_id, seq, title) VALUES (?, ?, ?)", (course_id, seq, title)).lastrowid
        lesson_ids.append(lid)
        for code, qty, basis, waste in lines:
            conn.execute(
                "INSERT INTO lesson_materials (lesson_id, material_id, qty, basis, waste_pct) VALUES (?, ?, ?, ?, ?)",
                (lid, ids[code], qty, basis, waste),
            )

    rooms = {}
    for name, loc in [("創客教室 A", "3F"), ("創客教室 B", "3F")]:
        rooms[name] = conn.execute("INSERT INTO classrooms (name, location) VALUES (?, ?)", (name, loc)).lastrowid
        for code, qty in [("M008", 6), ("M009", 24)]:
            conn.execute(
                "INSERT INTO classroom_equipment (classroom_id, material_id, standard_qty) VALUES (?, ?, ?)",
                (rooms[name], ids[code], qty),
            )
    svc.create_equipment_check(
        conn, rooms["創客教室 A"], today.isoformat(),
        {ids["M008"]: (5, 1, 0, "一支加熱不穩"), ids["M009"]: (22, 0, 2, "")}, uid["teacher"],
    )

    for name, room, students, start in [("週一班", "創客教室 A", 24, 2), ("週三班", "創客教室 B", 18, 4)]:
        gid = conn.execute(
            """INSERT INTO class_groups (name, course_id, classroom_id, teacher_id, student_count, group_size)
               VALUES (?, ?, ?, ?, ?, 4)""",
            (name, course_id, rooms[room], uid["teacher"], students),
        ).lastrowid
        for i, lid in enumerate(lesson_ids):
            conn.execute(
                "INSERT INTO sessions (class_group_id, lesson_id, date) VALUES (?, ?, ?)",
                (gid, lid, (today + timedelta(days=start + 7 * i)).isoformat()),
            )
