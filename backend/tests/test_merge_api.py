import pytest
from fastapi.testclient import TestClient
from sqlalchemy import create_engine, select
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import StaticPool

from app.database import Base, get_db
from app.main import app
from app.models.models import (
    BomLine, Dish, Ingredient, KitchenOrder, MergedPrepRun, MergedPrepRunOrder, OrderLine,
)
from app.services.bom_engine import ReconciliationError


@pytest.fixture()
def db_session():
    engine = create_engine(
        "sqlite+pysqlite:///:memory:",
        connect_args={"check_same_thread": False},
        poolclass=StaticPool,
    )
    Base.metadata.create_all(bind=engine)
    TestSession = sessionmaker(bind=engine, autoflush=False, autocommit=False)
    db = TestSession()

    dishes = {}
    for code, name in [("D1", "红烧肉"), ("D2", "鱼香茄子")]:
        d = Dish(code=code, name=name)
        db.add(d); db.flush(); dishes[code] = d.id
    ings = {}
    for code, name, unit, stock in [("I-MEAT", "肉", "kg", 2.0), ("I-RICE", "米", "kg", 9.0)]:
        i = Ingredient(code=code, name=name, unit=unit, stock_qty=stock)
        db.add(i); db.flush(); ings[code] = i.id
    db.add_all([
        BomLine(dish_id=dishes["D1"], ingredient_id=ings["I-MEAT"], qty_per_portion=0.2),
        BomLine(dish_id=dishes["D1"], ingredient_id=ings["I-RICE"], qty_per_portion=0.1),
        BomLine(dish_id=dishes["D2"], ingredient_id=ings["I-MEAT"], qty_per_portion=0.3),
    ])

    def add_order(code, outlet, lines):
        o = KitchenOrder(code=code, outlet=outlet, status="open")
        db.add(o); db.flush()
        for dcode, portions in lines:
            db.add(OrderLine(order_id=o.id, dish_id=dishes[dcode], portions=portions))
        return o

    o1 = add_order("KO-A", "城西门店", [("D1", 10), ("D2", 5)])
    o2 = add_order("KO-B", "城东门店", [("D2", 4)])
    o3 = add_order("KO-C", "城南门店", [("D2", 7)])  # 第三家，不参与合单
    db.commit()

    def override_get_db():
        sess = TestSession()
        try:
            yield sess
        finally:
            sess.close()

    app.dependency_overrides[get_db] = override_get_db
    try:
        yield db, {"A": o1.id, "B": o2.id, "C": o3.id}
    finally:
        app.dependency_overrides.clear()
        db.close()
        engine.dispose()


@pytest.fixture()
def client():
    return TestClient(app)


def snapshot_third_store(db, oid):
    o = db.get(KitchenOrder, oid)
    return {
        "order": (o.id, o.code, o.outlet, o.status),
        "lines": [(l.id, l.order_id, l.dish_id, l.portions)
                  for l in db.scalars(select(OrderLine).where(OrderLine.order_id == oid)).all()],
    }


def test_merge_success_two_stores_lines_shortages_stats_consistent(client, db_session):
    db, ids = db_session
    res = client.post("/api/prep/merge", json={"order_ids": [ids["A"], ids["B"]]})
    assert res.status_code == 200, res.text
    data = res.json()

    by_id = {l["ingredient_id"]: l for l in data["prep_lines"]}
    meat = next(l for l in data["prep_lines"] if l["ingredient_code"] == "I-MEAT")
    # 肉：A 3.5 + B 1.2 = 4.7，库存 2，缺 2.7；两店份额都在，且编码不串
    assert meat["need_qty"] == 4.7 and meat["shortage"] == 2.7
    assert {s["order_id"]: s["need_qty"] for s in meat["stores"]} == {ids["A"]: 3.5, ids["B"]: 1.2}
    # 米只有 A 要：行必须落，且只挂 A
    rice = next(l for l in data["prep_lines"] if l["ingredient_code"] == "I-RICE")
    assert rice["need_qty"] == 1.0 and [s["order_id"] for s in rice["stores"]] == [ids["A"]]

    # 缺料贴与备料行严格同源
    assert [s["ingredient_id"] for s in data["shortages"]] == [meat["ingredient_id"]]
    assert data["stats"]["shortage_count"] == 1
    assert data["stats"]["total_shortage_qty"] == 2.7
    assert data["stats"]["total_need_qty"] == 5.7
    per = {r["order_id"]: r["need_qty"] for r in data["stats"]["per_store"]}
    assert per == {ids["A"]: 4.5, ids["B"]: 1.2}

    # 落库：一个批次 + 恰好两条关联
    assert db.scalar(select(MergedPrepRun)) is not None
    links = db.scalars(select(MergedPrepRunOrder)).all()
    assert sorted(l.order_id for l in links) == [ids["A"], ids["B"]]

    # GET 取回来的与落库一致
    got = client.get(f"/api/prep/merge-runs/{data['id']}").json()
    assert got["prep_lines"] == data["prep_lines"]
    sh = client.get(f"/api/prep/merge-runs/{data['id']}/shortages").json()
    assert sh["shortages"] == data["shortages"] and sh["stats"] == data["stats"]


@pytest.mark.parametrize("payload", [[], [1], [1, 1], [1, 2, 3]])
def test_merge_rejects_empty_single_duplicate_three(client, db_session, payload):
    db, ids = db_session
    real = [ids["A"], ids["B"], ids["C"]]
    body = {"order_ids": [real[i - 1] if isinstance(i, int) and i <= 3 else i for i in payload]}
    before = db.query(MergedPrepRun).count()
    third_before = snapshot_third_store(db, ids["C"])
    res = client.post("/api/prep/merge", json=body)
    assert res.status_code == 400, res.text
    # 拒绝后：没有任何批次落库，第三家订单一字未改
    assert db.query(MergedPrepRun).count() == before == 0
    assert snapshot_third_store(db, ids["C"]) == third_before


def test_merge_does_not_touch_third_store_order(client, db_session):
    db, ids = db_session
    third_before = snapshot_third_store(db, ids["C"])
    all_orders_before = [(o.id, o.code, o.outlet, o.status)
                         for o in db.scalars(select(KitchenOrder)).all()]
    res = client.post("/api/prep/merge", json={"order_ids": [ids["A"], ids["B"]]})
    assert res.status_code == 200
    assert snapshot_third_store(db, ids["C"]) == third_before
    assert [(o.id, o.code, o.outlet, o.status) for o in db.scalars(select(KitchenOrder)).all()] == all_orders_before


def test_merge_missing_order_404_and_nothing_written(client, db_session):
    db, ids = db_session
    res = client.post("/api/prep/merge", json={"order_ids": [ids["A"], 99999]})
    assert res.status_code == 404
    assert db.query(MergedPrepRun).count() == 0


def test_merge_reconciliation_failure_writes_nothing(client, db_session, monkeypatch):
    import app.api.prep as prep_api
    db, ids = db_session

    def boom(*a, **k):
        raise ReconciliationError("模拟对账不平：缺料贴按两店加总，行只落了一家")

    monkeypatch.setattr(prep_api, "build_merged_result", boom)
    res = client.post("/api/prep/merge", json={"order_ids": [ids["A"], ids["B"]]})
    assert res.status_code == 500
    assert "整次作废" in res.json()["detail"]
    # 一对不上，两边都不许新落：批次、关联全部为零
    assert db.query(MergedPrepRun).count() == 0
    assert db.query(MergedPrepRunOrder).count() == 0
    # 两家源订单行也原样
    for oid in (ids["A"], ids["B"]):
        assert db.query(OrderLine).filter(OrderLine.order_id == oid).count() > 0
