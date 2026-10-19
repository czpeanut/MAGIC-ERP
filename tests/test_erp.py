from datetime import date, timedelta

import pytest

from magic_erp import create_app, services as svc
from magic_erp.db import get_db
from magic_erp.seed import seed

TODAY = date.today()


@pytest.fixture
def app(tmp_path):
    app = create_app({"TESTING": True, "DATABASE": str(tmp_path / "test.sqlite3")})
    with app.app_context():
        seed(get_db(), TODAY)
        get_db().commit()
    return app


@pytest.fixture
def conn(app):
    with app.app_context():
        yield get_db()


def mid(conn, code):
    return conn.execute("SELECT id FROM materials WHERE code = ?", (code,)).fetchone()["id"]


def first_session(conn, class_name="週一班"):
    return conn.execute(
        """SELECT s.id FROM sessions s JOIN class_groups cg ON cg.id = s.class_group_id
           JOIN lessons l ON l.id = s.lesson_id WHERE cg.name = ? ORDER BY l.seq""",
        (class_name,),
    ).fetchone()["id"]


def test_line_requirement_bases():
    assert svc.line_requirement(2, "per_student", 10, 24, 4) == 53  # 48 × 1.1 = 52.8 → 53
    assert svc.line_requirement(1, "per_group", 0, 18, 4) == 5      # ⌈18/4⌉
    assert svc.line_requirement(3, "per_class", 0, 30, 4) == 3
    assert svc.line_requirement(0.1, "per_student", 0, 30, 4) == 3  # 浮點誤差不多進一


def test_session_requirements(conn):
    reqs = svc.session_requirements(conn, first_session(conn))
    assert reqs == {mid(conn, "M003"): 53, mid(conn, "M004"): 24, mid(conn, "M007"): 6}


def test_pick_issue_and_close(conn):
    sid = first_session(conn)
    pick = svc.create_pick_list(conn, sid)
    assert svc.create_pick_list(conn, sid) == pick  # 重複產生回傳同一張
    svc.issue_pick_list(conn, pick, {mid(conn, "M004"): 20})
    assert svc.stock_on_hand(conn, mid(conn, "M003")) == 120 - 53
    assert svc.stock_on_hand(conn, mid(conn, "M004")) == 40 - 20
    assert svc.stock_on_hand(conn, mid(conn, "M007")) == 8 - 6
    svc.close_pick_list(conn, pick, {mid(conn, "M003"): 3})
    assert svc.stock_on_hand(conn, mid(conn, "M007")) == 8  # 非耗材預設全數歸還
    assert svc.stock_on_hand(conn, mid(conn, "M003")) == 120 - 50
    assert conn.execute("SELECT status FROM sessions WHERE id = ?", (sid,)).fetchone()[0] == "done"


def test_issue_rejects_insufficient_stock(conn):
    pick = svc.create_pick_list(conn, first_session(conn))
    with pytest.raises(svc.BusinessError, match="庫存不足"):
        svc.issue_pick_list(conn, pick, {mid(conn, "M003"): 999})
    assert svc.stock_on_hand(conn, mid(conn, "M004")) == 40  # 全部失敗，不會部分扣帳


def test_requirement_report(conn):
    rows = {r["material"]["code"]: r for r in svc.requirement_report(
        conn, TODAY.isoformat(), (TODAY + timedelta(days=30)).isoformat())}
    # 電池：週一班 24 × 2 堂 + 週三班 18 × 2 堂 = 84，安全存量 50，庫存 40 → 缺 94
    assert rows["M004"]["demand"] == 84
    assert rows["M004"]["shortage"] == 94 and rows["M004"]["action"] == "採購"
    # 三用電表為非耗材：取單日最高用量（6 組），庫存 8 足夠
    assert rows["M007"]["demand"] == 6 and rows["M007"]["shortage"] == 0
    # 小板需加工：需求 ⌈24×4×1.05⌉=101 + ⌈18×4×1.05⌉=76 = 177，+40 安全 − 60 庫存 = 157
    assert rows["M002"]["shortage"] == 157 and rows["M002"]["action"] == "開加工單"
    # 原料大板：157 × 0.125 = 19.625 衍生需求
    assert rows["M001"]["derived"] == pytest.approx(19.625)
    # 共用器材不列入課堂需求分析
    assert "M008" not in rows


def test_process_flow_and_reservation(conn):
    small, big = mid(conn, "M002"), mid(conn, "M001")
    oid = svc.create_process_order(conn, small, 80)
    rows = {r["material"]["code"]: r for r in svc.requirement_report(conn, "2000-01-01", "2000-01-02")}
    assert rows["M001"]["reserved"] == 10 and rows["M002"]["incoming"] == 80
    svc.start_process(conn, oid)
    assert svc.stock_on_hand(conn, big) == 30 - 10
    svc.finish_process(conn, oid, 78)
    svc.confirm_process(conn, oid)
    assert svc.stock_on_hand(conn, small) == 60 + 78
    with pytest.raises(svc.BusinessError):
        svc.confirm_process(conn, oid)
    with pytest.raises(svc.BusinessError, match="不是加工品"):
        svc.create_process_order(conn, big, 1)


def test_stocktake_posts_differences(conn):
    st = svc.create_stocktake(conn, TODAY.isoformat())
    conn.execute(
        "UPDATE stocktake_lines SET counted_qty = 35 WHERE stocktake_id = ? AND material_id = ?",
        (st, mid(conn, "M004")),
    )
    svc.record_move(conn, mid(conn, "M004"), 10, "purchase")  # 盤點期間入庫不影響差異
    svc.post_stocktake(conn, st)
    assert svc.stock_on_hand(conn, mid(conn, "M004")) == 45
    assert svc.stock_on_hand(conn, mid(conn, "M003")) == 120  # 未盤項目不調整
    with pytest.raises(svc.BusinessError):
        svc.post_stocktake(conn, st)


def test_equipment_replenish(conn):
    status = {e["code"]: e for e in svc.equipment_status(conn) if e["classroom"] == "創客教室 A"}
    assert status["M008"]["shortfall"] == 1 and status["M009"]["shortfall"] == 2
    svc.replenish_equipment(conn, status["M009"]["line_id"])
    assert svc.stock_on_hand(conn, mid(conn, "M009")) == 4
    after = {e["code"]: e for e in svc.equipment_status(conn) if e["classroom"] == "創客教室 A"}
    assert after["M009"]["replenished"] and after["M009"]["good"] == 24
    with pytest.raises(svc.BusinessError):
        svc.replenish_equipment(conn, status["M009"]["line_id"])


def login(client, username):
    client.post("/login", data={"username": username, "password": "1234"})


PAGES = ["/", "/materials", "/materials/1", "/courses", "/courses/1", "/classes", "/classes/1", "/stock",
         "/stocktakes", "/requirements", "/requirements?format=csv", "/process", "/classrooms",
         "/classrooms/1", "/picks"]


@pytest.mark.parametrize("user", ["admin", "designer", "warehouse", "teacher", "office"])
def test_pages_render(app, user):
    client = app.test_client()
    login(client, user)
    for page in PAGES:
        assert client.get(page).status_code == 200, page


def test_login_required_and_roles(app):
    client = app.test_client()
    assert client.get("/stock").status_code == 302
    login(client, "teacher")
    assert client.post("/stock", data={"material_id": 1, "qty": 5}).status_code == 403
    assert client.get("/users").status_code == 403


def test_web_pick_flow(app):
    client = app.test_client()
    login(client, "office")
    with app.app_context():
        sid = first_session(get_db())
    resp = client.post(f"/sessions/{sid}/pick")
    assert resp.status_code == 302
    pid = int(resp.headers["Location"].rstrip("/").split("/")[-1])
    assert "53" in client.get(f"/picks/{pid}").get_data(as_text=True)
    assert client.get(f"/picks/{pid}/print").status_code == 200
    client.post(f"/picks/{pid}/issue", data={})
    page = client.get(f"/picks/{pid}").get_data(as_text=True)
    assert "已發料" in page
    login(client, "teacher")
    client.post(f"/picks/{pid}/close", data={})
    assert "已結案" in client.get(f"/picks/{pid}").get_data(as_text=True)


def test_business_error_is_flashed(app):
    client = app.test_client()
    login(client, "warehouse")
    resp = client.post("/process", data={"material_id": 1, "qty": 5}, follow_redirects=True)
    assert "不是加工品" in resp.get_data(as_text=True)


def test_csv_import(app):
    import io

    client = app.test_client()
    login(client, "warehouse")
    csv_text = "料號,品名,規格,單位,類別,安全存量,期初庫存\nX01,砂紙,#240,張,耗材,10,50\nM003,LED 燈珠,5mm 暖光,顆,耗材,100,\n"
    client.post("/materials/import", data={"file": (io.BytesIO(csv_text.encode("utf-8-sig")), "m.csv")},
                content_type="multipart/form-data")
    with app.app_context():
        conn = get_db()
        assert svc.stock_on_hand(conn, mid(conn, "X01")) == 50
        assert conn.execute("SELECT spec FROM materials WHERE code = 'M003'").fetchone()[0] == "5mm 暖光"
