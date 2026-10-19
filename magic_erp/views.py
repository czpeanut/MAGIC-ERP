import csv
import io
from datetime import date, timedelta

from flask import Blueprint, Response, abort, flash, g, redirect, render_template, request, url_for

from . import services as svc
from .auth import can, create_user, login_required, roles_required
from .db import get_db
from .services import BusinessError

bp = Blueprint("main", __name__)


def num(value, default=0.0):
    if value is None or str(value).strip() == "":
        return default
    try:
        return float(value)
    except ValueError:
        raise BusinessError(f"「{value}」不是有效的數字")


def opt_num(value):
    return None if value is None or str(value).strip() == "" else num(value)


@bp.errorhandler(BusinessError)
def business_error(exc):
    get_db().rollback()
    flash(str(exc), "error")
    return redirect(request.referrer or url_for("main.dashboard"))


def done(message, endpoint, **values):
    get_db().commit()
    flash(message, "ok")
    return redirect(url_for(endpoint, **values))


def one(sql, *params):
    row = get_db().execute(sql, params).fetchone()
    if row is None:
        abort(404)
    return row


# 儀表板 -------------------------------------------------------------------

@bp.route("/")
@login_required
def dashboard():
    return render_template("dashboard.html", d=svc.dashboard(get_db()))


# 物料主檔 -----------------------------------------------------------------

MATERIAL_FIELDS = ("code", "name", "spec", "unit", "kind", "location", "process_steps", "note")


def _material_values(form):
    values = {f: form.get(f, "").strip() for f in MATERIAL_FIELDS}
    if not values["code"] or not values["name"]:
        raise BusinessError("料號與品名為必填")
    if values["kind"] not in svc.KIND_LABELS:
        raise BusinessError("請選擇物料類別")
    values["safety_stock"] = num(form.get("safety_stock"))
    values["source_material_id"] = int(form["source_material_id"]) if form.get("source_material_id") else None
    values["source_ratio"] = num(form.get("source_ratio"), 1)
    values["active"] = 1 if form.get("active", "1") == "1" else 0
    return values


@bp.route("/materials", methods=["GET", "POST"])
@login_required
def materials():
    conn = get_db()
    if request.method == "POST":
        if not can("designer", "warehouse"):
            abort(403)
        v = _material_values(request.form)
        if conn.execute("SELECT 1 FROM materials WHERE code = ?", (v["code"],)).fetchone():
            raise BusinessError(f"料號 {v['code']} 已存在")
        cols = ", ".join(v)
        conn.execute(f"INSERT INTO materials ({cols}) VALUES ({', '.join('?' * len(v))})", list(v.values()))
        return done(f"已新增物料 {v['name']}", "main.materials")
    kind = request.args.get("kind", "")
    q = request.args.get("q", "").strip()
    sql = """SELECT m.*, s.name AS source_name FROM materials m
             LEFT JOIN materials s ON s.id = m.source_material_id WHERE 1 = 1"""
    params = []
    if kind:
        sql += " AND m.kind = ?"
        params.append(kind)
    if q:
        sql += " AND (m.code LIKE ? OR m.name LIKE ? OR m.spec LIKE ?)"
        params += [f"%{q}%"] * 3
    rows = conn.execute(sql + " ORDER BY m.active DESC, m.code", params).fetchall()
    all_materials = conn.execute("SELECT id, code, name FROM materials ORDER BY code").fetchall()
    return render_template(
        "materials.html", rows=rows, on_hand=svc.stock_on_hand(conn), all_materials=all_materials,
        kind=kind, q=q,
    )


@bp.route("/materials/<int:mid>", methods=["GET", "POST"])
@login_required
def material_edit(mid):
    conn = get_db()
    m = one("SELECT * FROM materials WHERE id = ?", mid)
    if request.method == "POST":
        if not can("designer", "warehouse"):
            abort(403)
        v = _material_values(request.form)
        if v["source_material_id"] == mid:
            raise BusinessError("原料不可設定為自己")
        conn.execute(
            f"UPDATE materials SET {', '.join(f'{k} = ?' for k in v)} WHERE id = ?", [*v.values(), mid]
        )
        return done("已更新物料", "main.materials")
    moves = conn.execute(
        "SELECT sm.*, u.display_name FROM stock_moves sm LEFT JOIN users u ON u.id = sm.user_id"
        " WHERE material_id = ? ORDER BY sm.id DESC LIMIT 50",
        (mid,),
    ).fetchall()
    used_in = conn.execute(
        """SELECT c.id AS course_id, c.name AS course, l.seq, l.title, lm.qty, lm.basis
           FROM lesson_materials lm JOIN lessons l ON l.id = lm.lesson_id JOIN courses c ON c.id = l.course_id
           WHERE lm.material_id = ? ORDER BY c.code, l.seq""",
        (mid,),
    ).fetchall()
    all_materials = conn.execute("SELECT id, code, name FROM materials WHERE id != ? ORDER BY code", (mid,)).fetchall()
    return render_template(
        "material_edit.html", m=m, moves=moves, used_in=used_in, all_materials=all_materials,
        on_hand=svc.stock_on_hand(conn, mid),
    )


@bp.route("/materials/import", methods=["POST"])
@roles_required("designer", "warehouse")
def materials_import():
    """匯入 CSV：料號,品名,規格,單位,類別(耗材/非耗材/共用器材),安全存量,期初庫存"""
    conn = get_db()
    file = request.files.get("file")
    if not file:
        raise BusinessError("請選擇 CSV 檔案")
    text = file.read().decode("utf-8-sig")
    kinds = {v: k for k, v in svc.KIND_LABELS.items()} | {k: k for k in svc.KIND_LABELS}
    created = updated = 0
    for i, row in enumerate(csv.DictReader(io.StringIO(text)), start=2):
        code = (row.get("料號") or "").strip()
        if not code:
            continue
        kind = kinds.get((row.get("類別") or "").strip())
        if not kind:
            raise BusinessError(f"第 {i} 行類別錯誤：{row.get('類別')}")
        values = (
            (row.get("品名") or "").strip(), (row.get("規格") or "").strip(),
            (row.get("單位") or "個").strip(), kind, num(row.get("安全存量")),
        )
        existing = conn.execute("SELECT id FROM materials WHERE code = ?", (code,)).fetchone()
        if existing:
            conn.execute(
                "UPDATE materials SET name = ?, spec = ?, unit = ?, kind = ?, safety_stock = ? WHERE id = ?",
                (*values, existing["id"]),
            )
            updated += 1
        else:
            cur = conn.execute(
                "INSERT INTO materials (name, spec, unit, kind, safety_stock, code) VALUES (?, ?, ?, ?, ?, ?)",
                (*values, code),
            )
            opening = num(row.get("期初庫存"))
            if opening:
                svc.record_move(conn, cur.lastrowid, opening, "adjust", note="期初庫存匯入", user_id=g.user["id"])
            created += 1
    return done(f"匯入完成：新增 {created} 筆、更新 {updated} 筆", "main.materials")


# 課程 / 堂次 / 用料（2表） -------------------------------------------------

@bp.route("/courses", methods=["GET", "POST"])
@login_required
def courses():
    conn = get_db()
    if request.method == "POST":
        if not can("designer"):
            abort(403)
        code, name = request.form["code"].strip(), request.form["name"].strip()
        if not code or not name:
            raise BusinessError("課程代碼與名稱為必填")
        if conn.execute("SELECT 1 FROM courses WHERE code = ?", (code,)).fetchone():
            raise BusinessError(f"課程代碼 {code} 已存在")
        cur = conn.execute(
            "INSERT INTO courses (code, name, designer_id, description) VALUES (?, ?, ?, ?)",
            (code, name, g.user["id"], request.form.get("description", "")),
        )
        lessons = int(num(request.form.get("lessons"), 0))
        for seq in range(1, lessons + 1):
            conn.execute("INSERT INTO lessons (course_id, seq, title) VALUES (?, ?, ?)", (cur.lastrowid, seq, f"第 {seq} 堂"))
        return done(f"已建立課程 {name}", "main.course_detail", cid=cur.lastrowid)
    rows = conn.execute(
        """SELECT c.*, u.display_name AS designer,
                  (SELECT COUNT(*) FROM lessons l WHERE l.course_id = c.id) AS lesson_count
           FROM courses c LEFT JOIN users u ON u.id = c.designer_id ORDER BY c.code"""
    ).fetchall()
    return render_template("courses.html", rows=rows)


@bp.route("/courses/<int:cid>")
@login_required
def course_detail(cid):
    conn = get_db()
    course = one("SELECT * FROM courses WHERE id = ?", cid)
    lessons = conn.execute("SELECT * FROM lessons WHERE course_id = ? ORDER BY seq", (cid,)).fetchall()
    lines = conn.execute(
        """SELECT lm.*, m.code, m.name, m.spec, m.unit, m.kind, m.source_material_id
           FROM lesson_materials lm JOIN materials m ON m.id = lm.material_id
           JOIN lessons l ON l.id = lm.lesson_id WHERE l.course_id = ? ORDER BY m.code""",
        (cid,),
    ).fetchall()
    by_lesson = {l["id"]: [] for l in lessons}
    for line in lines:
        by_lesson[line["lesson_id"]].append(line)

    # 預算試算：輸入人數，計算整門課所需數量（耗材加總、非耗材取單堂最大值）
    students = int(num(request.args.get("students"), 20))
    group_size = int(num(request.args.get("group_size"), 4))
    budget = {}
    for line in lines:
        need = svc.line_requirement(line["qty"], line["basis"], line["waste_pct"], students, group_size)
        b = budget.setdefault(line["material_id"], {"line": line, "total": 0})
        b["total"] = b["total"] + need if line["kind"] == "consumable" else max(b["total"], need)
    materials = conn.execute(
        "SELECT id, code, name, spec, unit FROM materials WHERE active = 1 AND kind != 'shared' ORDER BY code"
    ).fetchall()
    return render_template(
        "course_detail.html", course=course, lessons=lessons, by_lesson=by_lesson, materials=materials,
        budget=sorted(budget.values(), key=lambda b: (b["line"]["kind"], b["line"]["code"])),
        students=students, group_size=group_size,
    )


@bp.route("/courses/<int:cid>/lessons", methods=["POST"])
@roles_required("designer")
def lesson_save(cid):
    conn = get_db()
    one("SELECT id FROM courses WHERE id = ?", cid)
    if request.form.get("lesson_id"):
        conn.execute(
            "UPDATE lessons SET title = ? WHERE id = ? AND course_id = ?",
            (request.form["title"].strip(), request.form["lesson_id"], cid),
        )
    else:
        seq = conn.execute("SELECT COALESCE(MAX(seq), 0) + 1 FROM lessons WHERE course_id = ?", (cid,)).fetchone()[0]
        conn.execute(
            "INSERT INTO lessons (course_id, seq, title) VALUES (?, ?, ?)",
            (cid, seq, request.form.get("title", "").strip() or f"第 {seq} 堂"),
        )
    return done("已儲存堂次", "main.course_detail", cid=cid)


@bp.route("/courses/<int:cid>/lines", methods=["POST"])
@roles_required("designer")
def lesson_line_save(cid):
    conn = get_db()
    lesson = one("SELECT * FROM lessons WHERE id = ? AND course_id = ?", request.form["lesson_id"], cid)
    qty = num(request.form.get("qty"))
    if qty <= 0:
        raise BusinessError("用量必須大於 0")
    basis = request.form.get("basis", "per_student")
    if basis not in svc.BASIS_LABELS:
        raise BusinessError("計算基準錯誤")
    conn.execute(
        """INSERT INTO lesson_materials (lesson_id, material_id, qty, basis, waste_pct) VALUES (?, ?, ?, ?, ?)
           ON CONFLICT (lesson_id, material_id) DO UPDATE SET qty = excluded.qty, basis = excluded.basis,
           waste_pct = excluded.waste_pct""",
        (lesson["id"], int(request.form["material_id"]), qty, basis, num(request.form.get("waste_pct"))),
    )
    return done("已儲存用料", "main.course_detail", cid=cid)


@bp.route("/courses/<int:cid>/lines/<int:line_id>/delete", methods=["POST"])
@roles_required("designer")
def lesson_line_delete(cid, line_id):
    get_db().execute(
        "DELETE FROM lesson_materials WHERE id = ? AND lesson_id IN (SELECT id FROM lessons WHERE course_id = ?)",
        (line_id, cid),
    )
    return done("已刪除用料", "main.course_detail", cid=cid)


# 教室 / 開班 / 排課 -------------------------------------------------------

@bp.route("/classrooms", methods=["GET", "POST"])
@login_required
def classrooms():
    conn = get_db()
    if request.method == "POST":
        if not can("office"):
            abort(403)
        name = request.form["name"].strip()
        if not name:
            raise BusinessError("教室名稱為必填")
        conn.execute("INSERT INTO classrooms (name, location) VALUES (?, ?)", (name, request.form.get("location", "")))
        return done(f"已新增教室 {name}", "main.classrooms")
    rows = conn.execute("SELECT * FROM classrooms ORDER BY name").fetchall()
    status = svc.equipment_status(conn)
    summary = {}
    for e in status:
        s = summary.setdefault(e["classroom_id"], {"items": 0, "short": 0, "last": e["check_date"]})
        s["items"] += 1
        s["short"] += 1 if e["shortfall"] > 0 and not e["replenished"] else 0
    return render_template("classrooms.html", rows=rows, summary=summary)


@bp.route("/classrooms/<int:rid>")
@login_required
def classroom_detail(rid):
    conn = get_db()
    room = one("SELECT * FROM classrooms WHERE id = ?", rid)
    shared = conn.execute(
        "SELECT id, code, name, spec, unit FROM materials WHERE kind = 'shared' AND active = 1 ORDER BY code"
    ).fetchall()
    checks = conn.execute(
        "SELECT ec.*, u.display_name FROM equipment_checks ec LEFT JOIN users u ON u.id = ec.user_id"
        " WHERE classroom_id = ? ORDER BY ec.id DESC LIMIT 10",
        (rid,),
    ).fetchall()
    return render_template(
        "classroom_detail.html", room=room, status=svc.equipment_status(conn, rid), shared=shared,
        checks=checks, on_hand=svc.stock_on_hand(conn), today=date.today().isoformat(),
    )


@bp.route("/classrooms/<int:rid>/standard", methods=["POST"])
@roles_required("teacher", "warehouse", "office")
def classroom_standard(rid):
    one("SELECT id FROM classrooms WHERE id = ?", rid)
    qty = num(request.form.get("standard_qty"))
    mid = int(request.form["material_id"])
    conn = get_db()
    if qty <= 0:
        conn.execute("DELETE FROM classroom_equipment WHERE classroom_id = ? AND material_id = ?", (rid, mid))
    else:
        conn.execute(
            """INSERT INTO classroom_equipment (classroom_id, material_id, standard_qty) VALUES (?, ?, ?)
               ON CONFLICT (classroom_id, material_id) DO UPDATE SET standard_qty = excluded.standard_qty""",
            (rid, mid, qty),
        )
    return done("已更新教室標準配置", "main.classroom_detail", rid=rid)


@bp.route("/classrooms/<int:rid>/check", methods=["POST"])
@roles_required("teacher")
def classroom_check(rid):
    conn = get_db()
    one("SELECT id FROM classrooms WHERE id = ?", rid)
    counts = {}
    for e in conn.execute("SELECT material_id FROM classroom_equipment WHERE classroom_id = ?", (rid,)):
        mid = e["material_id"]
        counts[mid] = (
            num(request.form.get(f"good_{mid}")),
            num(request.form.get(f"damaged_{mid}")),
            num(request.form.get(f"missing_{mid}")),
            request.form.get(f"note_{mid}", "").strip(),
        )
    if not counts:
        raise BusinessError("此教室尚未設定標準器材")
    svc.create_equipment_check(conn, rid, request.form.get("date") or date.today().isoformat(), counts,
                               g.user["id"], request.form.get("note", ""))
    return done("已送出器材盤點", "main.classroom_detail", rid=rid)


@bp.route("/equipment/replenish/<int:line_id>", methods=["POST"])
@roles_required("warehouse")
def equipment_replenish(line_id):
    svc.replenish_equipment(get_db(), line_id, g.user["id"])
    get_db().commit()
    flash("已從倉庫撥補", "ok")
    return redirect(request.referrer or url_for("main.classrooms"))


@bp.route("/classes", methods=["GET", "POST"])
@login_required
def classes():
    conn = get_db()
    if request.method == "POST":
        if not can("office"):
            abort(403)
        name = request.form["name"].strip()
        students = int(num(request.form.get("student_count")))
        if not name or students <= 0:
            raise BusinessError("班級名稱與學生人數為必填")
        cid = int(request.form["course_id"])
        cur = conn.execute(
            """INSERT INTO class_groups (name, course_id, classroom_id, teacher_id, student_count, group_size)
               VALUES (?, ?, ?, ?, ?, ?)""",
            (name, cid, int(request.form["classroom_id"]), request.form.get("teacher_id") or None,
             students, int(num(request.form.get("group_size"), 4))),
        )
        start = request.form.get("start_date")
        interval = int(num(request.form.get("interval_days"), 7))
        if start:
            d = date.fromisoformat(start)
            for lesson in conn.execute("SELECT id FROM lessons WHERE course_id = ? ORDER BY seq", (cid,)).fetchall():
                conn.execute(
                    "INSERT INTO sessions (class_group_id, lesson_id, date) VALUES (?, ?, ?)",
                    (cur.lastrowid, lesson["id"], d.isoformat()),
                )
                d += timedelta(days=interval)
        return done(f"已建立班級 {name}", "main.class_detail", gid=cur.lastrowid)
    rows = conn.execute(
        """SELECT cg.*, c.name AS course, r.name AS classroom, u.display_name AS teacher,
                  (SELECT MIN(date) FROM sessions s WHERE s.class_group_id = cg.id AND s.status = 'planned') AS next_date
           FROM class_groups cg JOIN courses c ON c.id = cg.course_id JOIN classrooms r ON r.id = cg.classroom_id
           LEFT JOIN users u ON u.id = cg.teacher_id ORDER BY cg.id DESC"""
    ).fetchall()
    return render_template(
        "classes.html", rows=rows,
        courses=conn.execute("SELECT * FROM courses ORDER BY code").fetchall(),
        rooms=conn.execute("SELECT * FROM classrooms ORDER BY name").fetchall(),
        teachers=conn.execute("SELECT * FROM users WHERE role IN ('teacher','admin') ORDER BY display_name").fetchall(),
        today=date.today().isoformat(),
    )


@bp.route("/classes/<int:gid>", methods=["GET", "POST"])
@login_required
def class_detail(gid):
    conn = get_db()
    cg = one(
        """SELECT cg.*, c.name AS course, r.name AS classroom FROM class_groups cg
           JOIN courses c ON c.id = cg.course_id JOIN classrooms r ON r.id = cg.classroom_id WHERE cg.id = ?""",
        gid,
    )
    if request.method == "POST":
        if not can("office"):
            abort(403)
        conn.execute(
            "UPDATE class_groups SET student_count = ?, group_size = ? WHERE id = ?",
            (int(num(request.form["student_count"])), int(num(request.form.get("group_size"), 4)), gid),
        )
        for s in conn.execute("SELECT id FROM sessions WHERE class_group_id = ? AND status = 'planned'", (gid,)).fetchall():
            new_date = request.form.get(f"date_{s['id']}")
            if new_date:
                conn.execute("UPDATE sessions SET date = ? WHERE id = ?", (new_date, s["id"]))
        return done("已更新班級與排課", "main.class_detail", gid=gid)
    sessions = conn.execute(
        """SELECT s.*, l.seq, l.title, p.id AS pick_id, p.status AS pick_status FROM sessions s
           JOIN lessons l ON l.id = s.lesson_id LEFT JOIN pick_lists p ON p.session_id = s.id
           WHERE s.class_group_id = ? ORDER BY l.seq""",
        (gid,),
    ).fetchall()
    return render_template("class_detail.html", cg=cg, sessions=sessions)


# 庫存 ---------------------------------------------------------------------

@bp.route("/stock", methods=["GET", "POST"])
@login_required
def stock():
    conn = get_db()
    if request.method == "POST":
        if not can("warehouse"):
            abort(403)
        mid = int(request.form["material_id"])
        qty = num(request.form.get("qty"))
        move_type = request.form.get("type", "purchase")
        if move_type == "scrap":
            if qty <= 0:
                raise BusinessError("報廢數量必須大於 0")
            svc._require_stock(conn, mid, qty)
            svc.record_move(conn, mid, -qty, "scrap", note=request.form.get("note", ""), user_id=g.user["id"])
        else:
            svc.receive_stock(conn, mid, qty, request.form.get("note", ""), g.user["id"])
        return done("已登錄庫存異動", "main.stock")
    on_hand = svc.stock_on_hand(conn)
    materials = conn.execute("SELECT * FROM materials WHERE active = 1 ORDER BY kind, code").fetchall()
    moves = conn.execute(
        """SELECT sm.*, m.code, m.name, m.unit, u.display_name FROM stock_moves sm
           JOIN materials m ON m.id = sm.material_id LEFT JOIN users u ON u.id = sm.user_id
           ORDER BY sm.id DESC LIMIT 100"""
    ).fetchall()
    return render_template("stock.html", materials=materials, on_hand=on_hand, moves=moves)


@bp.route("/stocktakes", methods=["GET", "POST"])
@login_required
def stocktakes():
    conn = get_db()
    if request.method == "POST":
        if not can("warehouse"):
            abort(403)
        st_id = svc.create_stocktake(conn, request.form.get("date") or date.today().isoformat(), g.user["id"],
                                     request.form.get("note", ""))
        return done("已建立盤點單，請輸入實盤數量", "main.stocktake_detail", sid=st_id)
    rows = conn.execute(
        """SELECT st.*, u.display_name,
                  (SELECT COUNT(*) FROM stocktake_lines l WHERE l.stocktake_id = st.id AND l.counted_qty IS NOT NULL) AS counted,
                  (SELECT COUNT(*) FROM stocktake_lines l WHERE l.stocktake_id = st.id) AS total
           FROM stocktakes st LEFT JOIN users u ON u.id = st.user_id ORDER BY st.id DESC"""
    ).fetchall()
    return render_template("stocktakes.html", rows=rows, today=date.today().isoformat())


@bp.route("/stocktakes/<int:sid>", methods=["GET", "POST"])
@login_required
def stocktake_detail(sid):
    conn = get_db()
    st = one("SELECT * FROM stocktakes WHERE id = ?", sid)
    if request.method == "POST":
        if not can("warehouse"):
            abort(403)
        if st["status"] != "draft":
            raise BusinessError("此盤點單已過帳")
        for line in conn.execute("SELECT id FROM stocktake_lines WHERE stocktake_id = ?", (sid,)).fetchall():
            conn.execute(
                "UPDATE stocktake_lines SET counted_qty = ? WHERE id = ?",
                (opt_num(request.form.get(f"count_{line['id']}")), line["id"]),
            )
        if request.form.get("action") == "post":
            svc.post_stocktake(conn, sid, g.user["id"])
            return done("盤點已過帳，差異已調整入庫存", "main.stocktake_detail", sid=sid)
        return done("已暫存盤點數量", "main.stocktake_detail", sid=sid)
    lines = conn.execute(
        """SELECT l.*, m.code, m.name, m.spec, m.unit, m.kind, m.location FROM stocktake_lines l
           JOIN materials m ON m.id = l.material_id WHERE l.stocktake_id = ? ORDER BY m.location, m.code""",
        (sid,),
    ).fetchall()
    return render_template("stocktake_detail.html", st=st, lines=lines)


# 需求分析（4表） -----------------------------------------------------------

def _report_range():
    today = date.today()
    return (
        request.args.get("from") or today.isoformat(),
        request.args.get("to") or (today + timedelta(days=30)).isoformat(),
    )


@bp.route("/requirements")
@login_required
def requirements():
    date_from, date_to = _report_range()
    rows = svc.requirement_report(get_db(), date_from, date_to)
    if request.args.get("format") == "csv":
        out = io.StringIO()
        w = csv.writer(out)
        w.writerow(["料號", "品名", "規格", "單位", "類別", "課堂需求", "加工衍生需求", "現有庫存",
                    "領出未還", "加工中預計產出", "已保留", "安全存量", "建議數量", "建議動作"])
        for r in rows:
            m = r["material"]
            w.writerow([m["code"], m["name"], m["spec"], m["unit"], svc.KIND_LABELS[m["kind"]],
                        *(svc.fmt(r[k]) for k in ("demand", "derived", "on_hand", "outstanding", "incoming",
                                                  "reserved", "safety", "shortage")), r["action"]])
        return Response(
            "﻿" + out.getvalue(), mimetype="text/csv",
            headers={"Content-Disposition": f"attachment; filename=requirements_{date_from}_{date_to}.csv"},
        )
    return render_template("requirements.html", rows=rows, date_from=date_from, date_to=date_to)


# 加工單（5表） -------------------------------------------------------------

@bp.route("/process", methods=["GET", "POST"])
@login_required
def process():
    conn = get_db()
    if request.method == "POST":
        if not can("warehouse"):
            abort(403)
        svc.create_process_order(conn, int(request.form["material_id"]), num(request.form.get("qty")),
                                 request.form.get("due_date", ""), g.user["id"], request.form.get("note", ""))
        return done("已建立加工單", "main.process")
    status = request.args.get("status", "open")
    sql = """SELECT po.*, m.code, m.name, m.unit, m.process_steps, m.source_ratio,
                    s.name AS source_name, s.unit AS source_unit, u.display_name AS confirmer
             FROM process_orders po JOIN materials m ON m.id = po.material_id
             JOIN materials s ON s.id = m.source_material_id
             LEFT JOIN users u ON u.id = po.confirmed_by"""
    sql += " WHERE po.status != 'done'" if status == "open" else ""
    rows = conn.execute(sql + " ORDER BY po.status = 'done', po.due_date, po.id").fetchall()
    products = conn.execute(
        "SELECT id, code, name, unit FROM materials WHERE source_material_id IS NOT NULL AND active = 1 ORDER BY code"
    ).fetchall()
    return render_template("process.html", rows=rows, products=products, status=status,
                           prefill_mid=request.args.get("material_id", type=int),
                           prefill_qty=request.args.get("qty", ""))


@bp.route("/process/<int:oid>/<action>", methods=["POST"])
@roles_required("warehouse")
def process_action(oid, action):
    conn = get_db()
    one("SELECT id FROM process_orders WHERE id = ?", oid)
    if action == "start":
        svc.start_process(conn, oid, g.user["id"])
    elif action == "finish":
        svc.finish_process(conn, oid, num(request.form.get("good_qty")))
    elif action == "confirm":
        svc.confirm_process(conn, oid, g.user["id"])
    else:
        abort(404)
    return done("加工單已更新", "main.process")


# 領料單（7表） -------------------------------------------------------------

@bp.route("/picks")
@login_required
def picks():
    conn = get_db()
    date_from, date_to = _report_range()
    rows = conn.execute(
        """SELECT s.*, cg.name AS class_name, cg.student_count, l.seq, l.title, r.name AS classroom,
                  p.id AS pick_id, p.status AS pick_status
           FROM sessions s JOIN class_groups cg ON cg.id = s.class_group_id
           JOIN lessons l ON l.id = s.lesson_id JOIN classrooms r ON r.id = cg.classroom_id
           LEFT JOIN pick_lists p ON p.session_id = s.id
           WHERE (s.date BETWEEN ? AND ?) OR p.status = 'issued'
           ORDER BY s.date, r.name""",
        (date_from, date_to),
    ).fetchall()
    return render_template("picks.html", rows=rows, date_from=date_from, date_to=date_to)


@bp.route("/sessions/<int:sid>/pick", methods=["POST"])
@roles_required("office", "warehouse")
def pick_create(sid):
    one("SELECT id FROM sessions WHERE id = ?", sid)
    pick_id = svc.create_pick_list(get_db(), sid, g.user["id"])
    return done("已產生領料單", "main.pick_detail", pid=pick_id)


def _pick_context(pid):
    conn = get_db()
    pick = one(
        """SELECT p.*, s.date, s.id AS session_id, cg.name AS class_name, cg.student_count, cg.group_size,
                  l.seq, l.title, c.name AS course, r.name AS classroom, u.display_name AS issuer
           FROM pick_lists p JOIN sessions s ON s.id = p.session_id
           JOIN class_groups cg ON cg.id = s.class_group_id JOIN lessons l ON l.id = s.lesson_id
           JOIN courses c ON c.id = cg.course_id JOIN classrooms r ON r.id = cg.classroom_id
           LEFT JOIN users u ON u.id = p.issued_by WHERE p.id = ?""",
        pid,
    )
    lines = conn.execute(
        """SELECT pl.*, m.code, m.name, m.spec, m.unit, m.kind, m.location FROM pick_lines pl
           JOIN materials m ON m.id = pl.material_id WHERE pl.pick_list_id = ? ORDER BY m.location, m.code""",
        (pid,),
    ).fetchall()
    return {"pick": pick, "lines": lines, "on_hand": svc.stock_on_hand(conn)}


@bp.route("/picks/<int:pid>")
@login_required
def pick_detail(pid):
    return render_template("pick_detail.html", **_pick_context(pid))


@bp.route("/picks/<int:pid>/print")
@login_required
def pick_print(pid):
    return render_template("pick_print.html", **_pick_context(pid))


def _qty_map(prefix):
    return {
        int(k[len(prefix):]): num(v) for k, v in request.form.items() if k.startswith(prefix) and v.strip() != ""
    }


@bp.route("/picks/<int:pid>/issue", methods=["POST"])
@roles_required("office", "warehouse")
def pick_issue(pid):
    svc.issue_pick_list(get_db(), pid, _qty_map("issued_"), g.user["id"])
    return done("已發料並扣除庫存", "main.pick_detail", pid=pid)


@bp.route("/picks/<int:pid>/close", methods=["POST"])
@roles_required("office", "warehouse", "teacher")
def pick_close(pid):
    svc.close_pick_list(get_db(), pid, _qty_map("returned_"), g.user["id"])
    return done("已結案，歸還數量已入庫", "main.pick_detail", pid=pid)


@bp.route("/picks/<int:pid>/regenerate", methods=["POST"])
@roles_required("office", "warehouse")
def pick_regenerate(pid):
    conn = get_db()
    pick = one("SELECT * FROM pick_lists WHERE id = ?", pid)
    if pick["status"] != "draft":
        raise BusinessError("已發料的領料單不能重新計算")
    conn.execute("DELETE FROM pick_lists WHERE id = ?", (pid,))
    new_id = svc.create_pick_list(conn, pick["session_id"], g.user["id"])
    return done("已依最新用料與人數重新計算", "main.pick_detail", pid=new_id)


# 使用者 -------------------------------------------------------------------

@bp.route("/users", methods=["GET", "POST"])
@roles_required()
def users():
    conn = get_db()
    if request.method == "POST":
        username = request.form["username"].strip()
        if not username or not request.form.get("password"):
            raise BusinessError("帳號與密碼為必填")
        if conn.execute("SELECT 1 FROM users WHERE username = ?", (username,)).fetchone():
            raise BusinessError("帳號已存在")
        create_user(conn, username, request.form.get("display_name") or username, request.form["role"],
                    request.form["password"])
        return done(f"已建立帳號 {username}", "main.users")
    rows = conn.execute("SELECT * FROM users ORDER BY role, username").fetchall()
    return render_template("users.html", rows=rows)
