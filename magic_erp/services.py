"""業務邏輯：所有計算都集中在這裡，畫面層只負責輸入與顯示。

原本 6 張 Excel 的人工計算，對應如下：
  2表 材料預算 → lesson_materials + session_requirements()
  3表 庫存清點 → stocktakes / post_stocktake()
  4表 庫存結餘 → requirement_report()（自動計算，不再人工填寫）
  5表 產出清點 → process_orders / start_process() / confirm_process()
  6表 共用器材 → classroom_equipment / equipment_checks / equipment_status()
  7表 教室取物 → pick_lists / create_pick_list() / issue_pick_list() / close_pick_list()
"""

import math
from collections import defaultdict
from datetime import date, timedelta


class BusinessError(Exception):
    """可預期的操作錯誤（例如庫存不足），會直接顯示給使用者。"""


KIND_LABELS = {"consumable": "耗材", "reusable": "非耗材", "shared": "共用器材"}
BASIS_LABELS = {"per_student": "每人", "per_group": "每組", "per_class": "每班"}
MOVE_LABELS = {
    "purchase": "採購入庫",
    "adjust": "盤點調整",
    "process_out": "加工耗用",
    "process_in": "加工產出",
    "issue": "領料出庫",
    "return": "退料歸還",
    "equip_out": "撥補教室",
    "scrap": "報廢",
}
SESSION_STATUS = {"planned": "已排定", "picked": "已領料", "done": "已結束"}
PICK_STATUS = {"draft": "待發料", "issued": "已發料", "closed": "已結案"}
PROCESS_STATUS = {"todo": "待加工", "doing": "加工中", "review": "待確認", "done": "已入庫"}
ROLE_LABELS = {
    "admin": "系統管理員",
    "designer": "課程設計師",
    "warehouse": "倉儲人員",
    "teacher": "授課人員",
    "office": "行政人員",
}


# 庫存 --------------------------------------------------------------------

def stock_on_hand(conn, material_id=None):
    if material_id is not None:
        row = conn.execute(
            "SELECT COALESCE(SUM(qty), 0) FROM stock_moves WHERE material_id = ?",
            (material_id,),
        ).fetchone()
        return row[0]
    rows = conn.execute(
        "SELECT material_id, SUM(qty) FROM stock_moves GROUP BY material_id"
    ).fetchall()
    return defaultdict(float, {r[0]: r[1] for r in rows})


def record_move(conn, material_id, qty, type_, ref_type="", ref_id=None, note="", user_id=None):
    if qty == 0:
        return
    conn.execute(
        "INSERT INTO stock_moves (material_id, qty, type, ref_type, ref_id, note, user_id)"
        " VALUES (?, ?, ?, ?, ?, ?, ?)",
        (material_id, qty, type_, ref_type, ref_id, note, user_id),
    )


def receive_stock(conn, material_id, qty, note="", user_id=None):
    if qty <= 0:
        raise BusinessError("入庫數量必須大於 0")
    record_move(conn, material_id, qty, "purchase", note=note, user_id=user_id)


def _require_stock(conn, material_id, qty):
    on_hand = stock_on_hand(conn, material_id)
    if on_hand + 1e-9 < qty:
        m = conn.execute("SELECT name, unit FROM materials WHERE id = ?", (material_id,)).fetchone()
        raise BusinessError(f"「{m['name']}」庫存不足：需要 {fmt(qty)} {m['unit']}，現有 {fmt(on_hand)}")


# 用料計算（2表） -----------------------------------------------------------

def line_requirement(qty, basis, waste_pct, student_count, group_size):
    if basis == "per_student":
        multiplier = student_count
    elif basis == "per_group":
        multiplier = math.ceil(student_count / max(group_size, 1))
    else:
        multiplier = 1
    raw = qty * multiplier * (1 + waste_pct / 100)
    # 以實物數量領取，無條件進位（容許浮點誤差）
    return math.ceil(raw - 1e-9)


def session_requirements(conn, session_id):
    rows = conn.execute(
        """SELECT lm.material_id, lm.qty, lm.basis, lm.waste_pct,
                  cg.student_count, cg.group_size
           FROM sessions s
           JOIN class_groups cg ON cg.id = s.class_group_id
           JOIN lesson_materials lm ON lm.lesson_id = s.lesson_id
           WHERE s.id = ?""",
        (session_id,),
    ).fetchall()
    return {
        r["material_id"]: line_requirement(r["qty"], r["basis"], r["waste_pct"], r["student_count"], r["group_size"])
        for r in rows
    }


# 需求 / 結餘分析（4表） -----------------------------------------------------

def _depth(mid, sources, seen=None):
    seen = seen or set()
    src = sources.get(mid)
    if src is None or mid in seen:
        return 0
    return 1 + _depth(src, sources, seen | {mid})


def requirement_report(conn, date_from, date_to):
    """計算期間內尚未領料的課堂需求，並與庫存比較，產出採購 / 加工建議。

    - 耗材：需求 = 所有課堂需求加總
    - 非耗材：課後會歸還，需求 = 單日最高同時用量；已領出未歸還的視為可用
    - 需加工品：不足部分建議開加工單，並把耗用的原料加入原料需求
    """
    materials = {
        r["id"]: r
        for r in conn.execute("SELECT * FROM materials WHERE active = 1 AND kind != 'shared'")
    }
    on_hand = stock_on_hand(conn)

    sessions = conn.execute(
        "SELECT id, date FROM sessions WHERE status = 'planned' AND date BETWEEN ? AND ?",
        (date_from, date_to),
    ).fetchall()
    consumable_demand = defaultdict(float)
    reusable_by_day = defaultdict(lambda: defaultdict(float))
    for s in sessions:
        for mid, qty in session_requirements(conn, s["id"]).items():
            if mid not in materials:
                continue
            if materials[mid]["kind"] == "reusable":
                reusable_by_day[mid][s["date"]] += qty
            else:
                consumable_demand[mid] += qty

    demand = defaultdict(float, consumable_demand)
    for mid, days in reusable_by_day.items():
        demand[mid] = max(days.values())

    # 已領出、尚未歸還的非耗材
    outstanding = defaultdict(float)
    for r in conn.execute(
        """SELECT pl.material_id, SUM(COALESCE(pl.issued_qty, 0)) AS q
           FROM pick_lines pl JOIN pick_lists p ON p.id = pl.pick_list_id
           JOIN materials m ON m.id = pl.material_id
           WHERE p.status = 'issued' AND m.kind = 'reusable'
           GROUP BY pl.material_id"""
    ):
        outstanding[r["material_id"]] = r["q"]

    # 進行中的加工單：todo 尚未扣原料（原料需保留），todo/doing/review 皆為預計產出
    incoming = defaultdict(float)
    reserved = defaultdict(float)
    for r in conn.execute("SELECT material_id, qty, status FROM process_orders WHERE status != 'done'"):
        incoming[r["material_id"]] += r["qty"]
        m = materials.get(r["material_id"])
        if r["status"] == "todo" and m and m["source_material_id"]:
            reserved[m["source_material_id"]] += r["qty"] * m["source_ratio"]

    sources = {mid: m["source_material_id"] for mid, m in materials.items() if m["source_material_id"]}
    derived = defaultdict(float)
    order = sorted(materials, key=lambda mid: -_depth(mid, sources))

    rows = []
    for mid in order:
        m = materials[mid]
        total_demand = demand[mid] + derived[mid]
        available = on_hand[mid] + outstanding[mid] + incoming[mid] - reserved[mid]
        shortage = max(0.0, total_demand + m["safety_stock"] - available)
        action = ""
        if shortage > 0:
            if m["source_material_id"]:
                action = "開加工單"
                derived[m["source_material_id"]] += shortage * m["source_ratio"]
            else:
                action = "採購"
        if total_demand or shortage or on_hand[mid]:
            rows.append(
                {
                    "material": m,
                    "demand": demand[mid],
                    "derived": derived[mid],
                    "on_hand": on_hand[mid],
                    "outstanding": outstanding[mid],
                    "incoming": incoming[mid],
                    "reserved": reserved[mid],
                    "available": available,
                    "safety": m["safety_stock"],
                    "shortage": math.ceil(shortage - 1e-9),
                    "action": action,
                }
            )
    rows.sort(key=lambda r: (r["shortage"] == 0, r["material"]["code"]))
    return rows


# 盤點（3表） ---------------------------------------------------------------

def create_stocktake(conn, stocktake_date, user_id=None, note=""):
    cur = conn.execute(
        "INSERT INTO stocktakes (date, user_id, note) VALUES (?, ?, ?)",
        (stocktake_date, user_id, note),
    )
    st_id = cur.lastrowid
    on_hand = stock_on_hand(conn)
    for m in conn.execute("SELECT id FROM materials WHERE active = 1 ORDER BY code"):
        conn.execute(
            "INSERT INTO stocktake_lines (stocktake_id, material_id, system_qty) VALUES (?, ?, ?)",
            (st_id, m["id"], on_hand[m["id"]]),
        )
    return st_id


def post_stocktake(conn, stocktake_id, user_id=None):
    st = conn.execute("SELECT * FROM stocktakes WHERE id = ?", (stocktake_id,)).fetchone()
    if st["status"] != "draft":
        raise BusinessError("此盤點單已過帳")
    lines = conn.execute(
        "SELECT * FROM stocktake_lines WHERE stocktake_id = ? AND counted_qty IS NOT NULL",
        (stocktake_id,),
    ).fetchall()
    for line in lines:
        diff = line["counted_qty"] - line["system_qty"]
        record_move(conn, line["material_id"], diff, "adjust", "stocktake", stocktake_id,
                    f"盤點 {st['date']}", user_id)
    conn.execute("UPDATE stocktakes SET status = 'posted' WHERE id = ?", (stocktake_id,))


# 加工（5表） ---------------------------------------------------------------

def create_process_order(conn, material_id, qty, due_date="", user_id=None, note=""):
    m = conn.execute("SELECT * FROM materials WHERE id = ?", (material_id,)).fetchone()
    if not m["source_material_id"]:
        raise BusinessError(f"「{m['name']}」未設定原料，不是加工品")
    if qty <= 0:
        raise BusinessError("加工數量必須大於 0")
    cur = conn.execute(
        "INSERT INTO process_orders (material_id, qty, due_date, created_by, note) VALUES (?, ?, ?, ?, ?)",
        (material_id, qty, due_date, user_id, note),
    )
    return cur.lastrowid


def _order_with_material(conn, order_id):
    return conn.execute(
        """SELECT po.*, m.source_material_id, m.source_ratio, m.name
           FROM process_orders po JOIN materials m ON m.id = po.material_id
           WHERE po.id = ?""",
        (order_id,),
    ).fetchone()


def start_process(conn, order_id, user_id=None):
    """開始加工：原料出庫。"""
    po = _order_with_material(conn, order_id)
    if po["status"] != "todo":
        raise BusinessError("只有「待加工」的加工單可以開始")
    need = po["qty"] * po["source_ratio"]
    _require_stock(conn, po["source_material_id"], need)
    record_move(conn, po["source_material_id"], -need, "process_out", "process", order_id,
                f"加工 {po['name']}", user_id)
    conn.execute("UPDATE process_orders SET status = 'doing' WHERE id = ?", (order_id,))


def finish_process(conn, order_id, good_qty):
    """加工完成，回報良品數，等待倉儲確認。"""
    po = _order_with_material(conn, order_id)
    if po["status"] != "doing":
        raise BusinessError("只有「加工中」的加工單可以回報完成")
    if good_qty < 0 or good_qty > po["qty"]:
        raise BusinessError("良品數必須介於 0 與加工數量之間")
    conn.execute("UPDATE process_orders SET status = 'review', good_qty = ? WHERE id = ?", (good_qty, order_id))


def confirm_process(conn, order_id, user_id=None):
    """確認加工結果：良品入庫。"""
    po = _order_with_material(conn, order_id)
    if po["status"] != "review":
        raise BusinessError("只有「待確認」的加工單可以確認入庫")
    record_move(conn, po["material_id"], po["good_qty"], "process_in", "process", order_id,
                "加工完成入庫", user_id)
    conn.execute(
        "UPDATE process_orders SET status = 'done', confirmed_by = ? WHERE id = ?", (user_id, order_id)
    )


# 領料（7表） ---------------------------------------------------------------

def create_pick_list(conn, session_id, user_id=None):
    existing = conn.execute("SELECT id FROM pick_lists WHERE session_id = ?", (session_id,)).fetchone()
    if existing:
        return existing["id"]
    reqs = session_requirements(conn, session_id)
    if not reqs:
        raise BusinessError("此堂課沒有設定用料")
    cur = conn.execute("INSERT INTO pick_lists (session_id, created_by) VALUES (?, ?)", (session_id, user_id))
    pick_id = cur.lastrowid
    for mid, qty in reqs.items():
        conn.execute(
            "INSERT INTO pick_lines (pick_list_id, material_id, planned_qty) VALUES (?, ?, ?)",
            (pick_id, mid, qty),
        )
    return pick_id


def issue_pick_list(conn, pick_id, issued=None, user_id=None):
    """發料：issued = {material_id: qty}，未指定者依計畫數量發料。"""
    pick = conn.execute("SELECT * FROM pick_lists WHERE id = ?", (pick_id,)).fetchone()
    if pick["status"] != "draft":
        raise BusinessError("此領料單已發料")
    issued = issued or {}
    lines = conn.execute("SELECT * FROM pick_lines WHERE pick_list_id = ?", (pick_id,)).fetchall()
    plan = {line["material_id"]: issued.get(line["material_id"], line["planned_qty"]) for line in lines}
    for mid, qty in plan.items():
        if qty < 0:
            raise BusinessError("發料數量不可為負數")
        _require_stock(conn, mid, qty)
    for line in lines:
        qty = plan[line["material_id"]]
        conn.execute("UPDATE pick_lines SET issued_qty = ? WHERE id = ?", (qty, line["id"]))
        record_move(conn, line["material_id"], -qty, "issue", "pick", pick_id, "課堂領料", user_id)
    conn.execute(
        "UPDATE pick_lists SET status = 'issued', issued_by = ?, issued_at = datetime('now','localtime')"
        " WHERE id = ?",
        (user_id, pick_id),
    )
    conn.execute("UPDATE sessions SET status = 'picked' WHERE id = ?", (pick["session_id"],))


def close_pick_list(conn, pick_id, returned=None, user_id=None):
    """課後結案：returned = {material_id: qty}。非耗材預設全數歸還，耗材預設無剩餘。"""
    pick = conn.execute("SELECT * FROM pick_lists WHERE id = ?", (pick_id,)).fetchone()
    if pick["status"] != "issued":
        raise BusinessError("只有「已發料」的領料單可以結案")
    returned = returned or {}
    lines = conn.execute(
        """SELECT pl.*, m.kind FROM pick_lines pl JOIN materials m ON m.id = pl.material_id
           WHERE pl.pick_list_id = ?""",
        (pick_id,),
    ).fetchall()
    for line in lines:
        default = line["issued_qty"] if line["kind"] == "reusable" else 0
        qty = returned.get(line["material_id"], default)
        if qty < 0 or qty > line["issued_qty"]:
            raise BusinessError("歸還數量必須介於 0 與發料數量之間")
        conn.execute("UPDATE pick_lines SET returned_qty = ? WHERE id = ?", (qty, line["id"]))
        record_move(conn, line["material_id"], qty, "return", "pick", pick_id, "課後歸還", user_id)
    conn.execute(
        "UPDATE pick_lists SET status = 'closed', closed_at = datetime('now','localtime') WHERE id = ?",
        (pick_id,),
    )
    conn.execute("UPDATE sessions SET status = 'done' WHERE id = ?", (pick["session_id"],))


# 教室共用器材（6表） -------------------------------------------------------

def create_equipment_check(conn, classroom_id, check_date, counts, user_id=None, note=""):
    """counts = {material_id: (good, damaged, missing, note)}"""
    cur = conn.execute(
        "INSERT INTO equipment_checks (classroom_id, date, user_id, note) VALUES (?, ?, ?, ?)",
        (classroom_id, check_date, user_id, note),
    )
    check_id = cur.lastrowid
    for mid, (good, damaged, missing, line_note) in counts.items():
        conn.execute(
            """INSERT INTO equipment_check_lines
               (check_id, material_id, good_qty, damaged_qty, missing_qty, note)
               VALUES (?, ?, ?, ?, ?, ?)""",
            (check_id, mid, good, damaged, missing, line_note),
        )
    return check_id


def equipment_status(conn, classroom_id=None):
    """每間教室每項器材：標準數量 vs 最近一次盤點良品數，算出需補數量。"""
    params = []
    where = ""
    if classroom_id is not None:
        where = "WHERE ce.classroom_id = ?"
        params.append(classroom_id)
    rows = conn.execute(
        f"""SELECT ce.classroom_id, c.name AS classroom, ce.material_id, m.code, m.name, m.spec, m.unit,
                   ce.standard_qty,
                   (SELECT MAX(id) FROM equipment_checks ec WHERE ec.classroom_id = ce.classroom_id) AS last_check
            FROM classroom_equipment ce
            JOIN classrooms c ON c.id = ce.classroom_id
            JOIN materials m ON m.id = ce.material_id
            {where}
            ORDER BY c.name, m.code""",
        params,
    ).fetchall()
    result = []
    for r in rows:
        line = None
        check = None
        if r["last_check"]:
            check = conn.execute("SELECT * FROM equipment_checks WHERE id = ?", (r["last_check"],)).fetchone()
            line = conn.execute(
                "SELECT * FROM equipment_check_lines WHERE check_id = ? AND material_id = ?",
                (r["last_check"], r["material_id"]),
            ).fetchone()
        good = line["good_qty"] if line else None
        shortfall = max(0, r["standard_qty"] - good) if line else 0
        result.append(
            {
                **dict(r),
                "check_date": check["date"] if check else None,
                "line_id": line["id"] if line else None,
                "good": good,
                "damaged": line["damaged_qty"] if line else None,
                "missing": line["missing_qty"] if line else None,
                "line_note": line["note"] if line else "",
                "shortfall": shortfall,
                "replenished": bool(line["replenished"]) if line else False,
            }
        )
    return result


def replenish_equipment(conn, line_id, user_id=None):
    """從倉庫撥補教室器材到標準數量。"""
    line = conn.execute(
        """SELECT ecl.*, ec.classroom_id, c.name AS classroom FROM equipment_check_lines ecl
           JOIN equipment_checks ec ON ec.id = ecl.check_id
           JOIN classrooms c ON c.id = ec.classroom_id
           WHERE ecl.id = ?""",
        (line_id,),
    ).fetchone()
    if line["replenished"]:
        raise BusinessError("此項目已撥補")
    std = conn.execute(
        "SELECT standard_qty FROM classroom_equipment WHERE classroom_id = ? AND material_id = ?",
        (line["classroom_id"], line["material_id"]),
    ).fetchone()
    need = max(0, std["standard_qty"] - line["good_qty"])
    if need == 0:
        raise BusinessError("此項目不需撥補")
    _require_stock(conn, line["material_id"], need)
    record_move(conn, line["material_id"], -need, "equip_out", "equipment_check", line["check_id"],
                f"撥補 {line['classroom']}", user_id)
    conn.execute(
        "UPDATE equipment_check_lines SET replenished = 1, good_qty = good_qty + ? WHERE id = ?",
        (need, line_id),
    )


# 儀表板 -------------------------------------------------------------------

def dashboard(conn, today=None):
    today = today or date.today()
    soon = (today + timedelta(days=7)).isoformat()
    on_hand = stock_on_hand(conn)
    low_stock = [
        {"material": m, "on_hand": on_hand[m["id"]]}
        for m in conn.execute("SELECT * FROM materials WHERE active = 1 AND safety_stock > 0 ORDER BY code")
        if on_hand[m["id"]] < m["safety_stock"]
    ]
    upcoming = conn.execute(
        """SELECT s.*, cg.name AS class_name, l.seq, l.title, c.name AS classroom, p.id AS pick_id, p.status AS pick_status
           FROM sessions s
           JOIN class_groups cg ON cg.id = s.class_group_id
           JOIN lessons l ON l.id = s.lesson_id
           JOIN classrooms c ON c.id = cg.classroom_id
           LEFT JOIN pick_lists p ON p.session_id = s.id
           WHERE s.status = 'planned' AND s.date <= ?
           ORDER BY s.date""",
        (soon,),
    ).fetchall()
    open_process = conn.execute(
        """SELECT po.*, m.name, m.unit FROM process_orders po JOIN materials m ON m.id = po.material_id
           WHERE po.status != 'done' ORDER BY po.due_date"""
    ).fetchall()
    unreturned = conn.execute(
        """SELECT p.id, s.date, cg.name AS class_name, l.seq, l.title
           FROM pick_lists p JOIN sessions s ON s.id = p.session_id
           JOIN class_groups cg ON cg.id = s.class_group_id
           JOIN lessons l ON l.id = s.lesson_id
           WHERE p.status = 'issued' AND s.date < ?
           ORDER BY s.date""",
        (today.isoformat(),),
    ).fetchall()
    equipment = [e for e in equipment_status(conn) if e["shortfall"] > 0 and not e["replenished"]]
    return {
        "low_stock": low_stock,
        "upcoming": upcoming,
        "open_process": open_process,
        "unreturned": unreturned,
        "equipment": equipment,
    }


def fmt(value):
    if value is None:
        return ""
    if float(value).is_integer():
        return str(int(value))
    return f"{value:.2f}".rstrip("0").rstrip(".")
