-- MAGIC-ERP 資料庫結構
-- 核心原則：主檔只登記一次；庫存只由「庫存異動」累加得出，不再手動填結餘。

PRAGMA foreign_keys = ON;

-- 使用者與角色 ------------------------------------------------------------
CREATE TABLE IF NOT EXISTS users (
    id            INTEGER PRIMARY KEY,
    username      TEXT NOT NULL UNIQUE,
    display_name  TEXT NOT NULL,
    role          TEXT NOT NULL CHECK (role IN ('admin','designer','warehouse','teacher','office')),
    password_hash TEXT NOT NULL
);

-- 物料主檔（取代各表重複填寫的品名/規格） --------------------------------
-- kind: consumable 耗材（用完即消耗）
--       reusable   非耗材（領出上課、課後歸還倉庫）
--       shared     共用器材（常駐教室的教具，見教室器材）
-- 需加工品：source_material_id 指向原料，source_ratio = 每 1 單位成品耗用的原料數量
CREATE TABLE IF NOT EXISTS materials (
    id                 INTEGER PRIMARY KEY,
    code               TEXT NOT NULL UNIQUE,
    name               TEXT NOT NULL,
    spec               TEXT NOT NULL DEFAULT '',
    unit               TEXT NOT NULL DEFAULT '個',
    kind               TEXT NOT NULL CHECK (kind IN ('consumable','reusable','shared')),
    safety_stock       REAL NOT NULL DEFAULT 0,
    source_material_id INTEGER REFERENCES materials(id),
    source_ratio       REAL NOT NULL DEFAULT 1,
    process_steps      TEXT NOT NULL DEFAULT '',
    location           TEXT NOT NULL DEFAULT '',
    note               TEXT NOT NULL DEFAULT '',
    active             INTEGER NOT NULL DEFAULT 1
);

-- 課程 / 堂次 / 每堂用料（取代 2表 材料預算表） ---------------------------
CREATE TABLE IF NOT EXISTS courses (
    id          INTEGER PRIMARY KEY,
    code        TEXT NOT NULL UNIQUE,
    name        TEXT NOT NULL,
    designer_id INTEGER REFERENCES users(id),
    description TEXT NOT NULL DEFAULT ''
);

CREATE TABLE IF NOT EXISTS lessons (
    id        INTEGER PRIMARY KEY,
    course_id INTEGER NOT NULL REFERENCES courses(id) ON DELETE CASCADE,
    seq       INTEGER NOT NULL,
    title     TEXT NOT NULL,
    UNIQUE (course_id, seq)
);

-- basis: per_student 每人 / per_group 每組 / per_class 每班
CREATE TABLE IF NOT EXISTS lesson_materials (
    id          INTEGER PRIMARY KEY,
    lesson_id   INTEGER NOT NULL REFERENCES lessons(id) ON DELETE CASCADE,
    material_id INTEGER NOT NULL REFERENCES materials(id),
    qty         REAL NOT NULL,
    basis       TEXT NOT NULL DEFAULT 'per_student' CHECK (basis IN ('per_student','per_group','per_class')),
    waste_pct   REAL NOT NULL DEFAULT 0,
    UNIQUE (lesson_id, material_id)
);

-- 教室 / 開班 / 上課排程 -------------------------------------------------
CREATE TABLE IF NOT EXISTS classrooms (
    id       INTEGER PRIMARY KEY,
    name     TEXT NOT NULL UNIQUE,
    location TEXT NOT NULL DEFAULT ''
);

CREATE TABLE IF NOT EXISTS class_groups (
    id            INTEGER PRIMARY KEY,
    name          TEXT NOT NULL,
    course_id     INTEGER NOT NULL REFERENCES courses(id),
    classroom_id  INTEGER NOT NULL REFERENCES classrooms(id),
    teacher_id    INTEGER REFERENCES users(id),
    student_count INTEGER NOT NULL,
    group_size    INTEGER NOT NULL DEFAULT 4
);

-- 每一「堂」實際上課；status: planned 已排定 / picked 已領料 / done 已結束
CREATE TABLE IF NOT EXISTS sessions (
    id             INTEGER PRIMARY KEY,
    class_group_id INTEGER NOT NULL REFERENCES class_groups(id) ON DELETE CASCADE,
    lesson_id      INTEGER NOT NULL REFERENCES lessons(id),
    date           TEXT NOT NULL,
    status         TEXT NOT NULL DEFAULT 'planned' CHECK (status IN ('planned','picked','done')),
    UNIQUE (class_group_id, lesson_id)
);

-- 庫存異動帳（唯一的庫存事實來源；取代 4表 庫存結餘表的手動計算） --------
-- type: purchase 入庫 / adjust 盤點調整 / process_out 加工耗用原料 / process_in 加工產出
--       issue 領料出庫 / return 退料歸還 / equip_out 撥補教室器材 / scrap 報廢
CREATE TABLE IF NOT EXISTS stock_moves (
    id          INTEGER PRIMARY KEY,
    material_id INTEGER NOT NULL REFERENCES materials(id),
    qty         REAL NOT NULL,
    type        TEXT NOT NULL,
    ref_type    TEXT NOT NULL DEFAULT '',
    ref_id      INTEGER,
    note        TEXT NOT NULL DEFAULT '',
    user_id     INTEGER REFERENCES users(id),
    created_at  TEXT NOT NULL DEFAULT (datetime('now','localtime'))
);
CREATE INDEX IF NOT EXISTS idx_moves_material ON stock_moves(material_id);

-- 盤點單（取代 3表 庫存清點表） -------------------------------------------
CREATE TABLE IF NOT EXISTS stocktakes (
    id         INTEGER PRIMARY KEY,
    date       TEXT NOT NULL,
    status     TEXT NOT NULL DEFAULT 'draft' CHECK (status IN ('draft','posted')),
    user_id    INTEGER REFERENCES users(id),
    note       TEXT NOT NULL DEFAULT ''
);

CREATE TABLE IF NOT EXISTS stocktake_lines (
    id           INTEGER PRIMARY KEY,
    stocktake_id INTEGER NOT NULL REFERENCES stocktakes(id) ON DELETE CASCADE,
    material_id  INTEGER NOT NULL REFERENCES materials(id),
    system_qty   REAL NOT NULL,
    counted_qty  REAL,
    UNIQUE (stocktake_id, material_id)
);

-- 加工單（取代 5表 產出清點表） ------------------------------------------
-- status: todo 待加工 / doing 加工中 / review 待確認 / done 已入庫
CREATE TABLE IF NOT EXISTS process_orders (
    id           INTEGER PRIMARY KEY,
    material_id  INTEGER NOT NULL REFERENCES materials(id),
    qty          REAL NOT NULL,
    due_date     TEXT NOT NULL DEFAULT '',
    status       TEXT NOT NULL DEFAULT 'todo' CHECK (status IN ('todo','doing','review','done')),
    good_qty     REAL,
    created_by   INTEGER REFERENCES users(id),
    confirmed_by INTEGER REFERENCES users(id),
    note         TEXT NOT NULL DEFAULT '',
    created_at   TEXT NOT NULL DEFAULT (datetime('now','localtime'))
);

-- 教室共用器材（取代 6表 共用器材表） ------------------------------------
CREATE TABLE IF NOT EXISTS classroom_equipment (
    id           INTEGER PRIMARY KEY,
    classroom_id INTEGER NOT NULL REFERENCES classrooms(id) ON DELETE CASCADE,
    material_id  INTEGER NOT NULL REFERENCES materials(id),
    standard_qty REAL NOT NULL,
    UNIQUE (classroom_id, material_id)
);

CREATE TABLE IF NOT EXISTS equipment_checks (
    id           INTEGER PRIMARY KEY,
    classroom_id INTEGER NOT NULL REFERENCES classrooms(id) ON DELETE CASCADE,
    date         TEXT NOT NULL,
    user_id      INTEGER REFERENCES users(id),
    note         TEXT NOT NULL DEFAULT ''
);

CREATE TABLE IF NOT EXISTS equipment_check_lines (
    id          INTEGER PRIMARY KEY,
    check_id    INTEGER NOT NULL REFERENCES equipment_checks(id) ON DELETE CASCADE,
    material_id INTEGER NOT NULL REFERENCES materials(id),
    good_qty    REAL NOT NULL DEFAULT 0,
    damaged_qty REAL NOT NULL DEFAULT 0,
    missing_qty REAL NOT NULL DEFAULT 0,
    replenished INTEGER NOT NULL DEFAULT 0,
    note        TEXT NOT NULL DEFAULT ''
);

-- 領料單（取代 7表 教室取物數量表） --------------------------------------
-- status: draft 待發料 / issued 已發料 / closed 已結案（非耗材已歸還）
CREATE TABLE IF NOT EXISTS pick_lists (
    id         INTEGER PRIMARY KEY,
    session_id INTEGER NOT NULL UNIQUE REFERENCES sessions(id) ON DELETE CASCADE,
    status     TEXT NOT NULL DEFAULT 'draft' CHECK (status IN ('draft','issued','closed')),
    created_by INTEGER REFERENCES users(id),
    issued_by  INTEGER REFERENCES users(id),
    issued_at  TEXT,
    closed_at  TEXT
);

CREATE TABLE IF NOT EXISTS pick_lines (
    id           INTEGER PRIMARY KEY,
    pick_list_id INTEGER NOT NULL REFERENCES pick_lists(id) ON DELETE CASCADE,
    material_id  INTEGER NOT NULL REFERENCES materials(id),
    planned_qty  REAL NOT NULL,
    issued_qty   REAL,
    returned_qty REAL,
    UNIQUE (pick_list_id, material_id)
);
