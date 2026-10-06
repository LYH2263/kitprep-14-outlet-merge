"""两店合单备料：加总对账、整次回滚、拒绝非法名单、第三店隔离。"""
import json
import os

os.environ.setdefault("DATABASE_URL", "sqlite://")  # 避免导入 app.database 时加载 psycopg2

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import create_engine, func, select
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import StaticPool

from app.api import prep as prep_api
from app.database import Base, get_db
from app.main import app
from app.models.models import BomLine, Dish, Ingredient, KitchenOrder, OrderLine, PrepRun

engine = create_engine("sqlite://", connect_args={"check_same_thread": False}, poolclass=StaticPool)
TestingSession = sessionmaker(autocommit=False, autoflush=False, bind=engine)


def override_get_db():
    db = TestingSession()
    try:
        yield db
    finally:
        db.close()


app.dependency_overrides[get_db] = override_get_db
client = TestClient(app)  # 不进入 lifespan，避免触碰真实库


@pytest.fixture()
def db():
    Base.metadata.create_all(bind=engine)
    session = TestingSession()
    d1 = Dish(code="D1", name="红烧肉", portion_unit="份")
    d2 = Dish(code="D2", name="鸡汤面", portion_unit="份")
    session.add_all([d1, d2]); session.flush()
    i1 = Ingredient(code="I1", name="五花肉", unit="kg", stock_qty=5.0)
    i2 = Ingredient(code="I2", name="面条", unit="kg", stock_qty=100.0)
    session.add_all([i1, i2]); session.flush()
    session.add_all([
        BomLine(dish_id=d1.id, ingredient_id=i1.id, qty_per_portion=0.2),
        BomLine(dish_id=d1.id, ingredient_id=i2.id, qty_per_portion=0.1),
        BomLine(dish_id=d2.id, ingredient_id=i2.id, qty_per_portion=0.3),
    ])
    oa = KitchenOrder(code="KO-A", outlet="城西门店", status="open")
    ob = KitchenOrder(code="KO-B", outlet="城东门店", status="open")
    oc = KitchenOrder(code="KO-C", outlet="城南门店", status="open")
    session.add_all([oa, ob, oc]); session.flush()
    session.add_all([
        OrderLine(order_id=oa.id, dish_id=d1.id, portions=10),  # I1 2.0 / I2 1.0
        OrderLine(order_id=ob.id, dish_id=d1.id, portions=20),  # I1 4.0 / I2 2.0
        OrderLine(order_id=ob.id, dish_id=d2.id, portions=10),  # I2 3.0
        OrderLine(order_id=oc.id, dish_id=d2.id, portions=5),   # I2 1.5
    ])
    session.commit()
    yield session
    session.close()
    Base.metadata.drop_all(bind=engine)


def _orders(db):
    return {o.code: o for o in db.scalars(select(KitchenOrder)).all()}


def _runs(db, order_id):
    return db.scalars(select(PrepRun).where(PrepRun.order_id == order_id)).all()


def _run_count(db):
    return db.scalar(select(func.count()).select_from(PrepRun))


def test_merge_persists_both_stores_and_reconciles(db):
    o = _orders(db)
    resp = client.post("/api/prep/merge", json={"order_ids": [o["KO-A"].id, o["KO-B"].id]})
    assert resp.status_code == 200, resp.text
    data = resp.json()
    # 合单缺料贴按两店加总：I1 需 6.0 库存 5.0 → 缺 1.0
    need = {l["ingredient_code"]: l["need_qty"] for l in data["prep_lines"]}
    assert need == {"I1": 6.0, "I2": 6.0}
    assert [s["ingredient_code"] for s in data["shortages"]] == ["I1"]
    assert data["stats"] == {"ingredient_count": 2, "shortage_count": 1, "total_shortage_qty": 1.0}
    # 两边都落下新行，且各记在各店编码下
    runs_a, runs_b = _runs(db, o["KO-A"].id), _runs(db, o["KO-B"].id)
    assert len(runs_a) == 1 and len(runs_b) == 1
    ja, jb = json.loads(runs_a[0].result_json), json.loads(runs_b[0].result_json)
    assert ja["order"]["code"] == "KO-A" and jb["order"]["code"] == "KO-B"
    need_a = {l["ingredient_code"]: l["need_qty"] for l in ja["prep_lines"]}
    need_b = {l["ingredient_code"]: l["need_qty"] for l in jb["prep_lines"]}
    assert need_a == {"I1": 2.0, "I2": 1.0}
    assert need_b == {"I1": 4.0, "I2": 5.0}
    # 两店备料行加总 == 合单备料行
    for code, qty in need.items():
        assert abs(need_a.get(code, 0.0) + need_b.get(code, 0.0) - qty) < 1e-9
    assert {x["run_id"] for x in data["orders"]} == {runs_a[0].id, runs_b[0].id}


def test_merge_accepts_query_params(db):
    o = _orders(db)
    resp = client.post(f"/api/prep/merge?order_ids={o['KO-A'].id}&order_ids={o['KO-B'].id}")
    assert resp.status_code == 200, resp.text
    assert _run_count(db) == 2


def test_merge_rejects_empty_single_and_missing_body(db):
    o = _orders(db)
    for kwargs in ({"json": {"order_ids": []}}, {"json": {"order_ids": [o["KO-A"].id]}}, {}):
        resp = client.post("/api/prep/merge", **kwargs)
        assert resp.status_code == 400, kwargs
    assert _run_count(db) == 0


def test_merge_rejects_duplicate_and_extra_orders(db):
    o = _orders(db)
    resp = client.post("/api/prep/merge", json={"order_ids": [o["KO-A"].id, o["KO-A"].id]})
    assert resp.status_code == 400
    resp = client.post("/api/prep/merge",
                       json={"order_ids": [o["KO-A"].id, o["KO-B"].id, o["KO-C"].id]})
    assert resp.status_code == 400
    assert _run_count(db) == 0


def test_merge_rejects_unknown_order(db):
    o = _orders(db)
    resp = client.post("/api/prep/merge", json={"order_ids": [o["KO-A"].id, 99999]})
    assert resp.status_code == 404
    assert _run_count(db) == 0


def test_merge_does_not_touch_third_store(db):
    o = _orders(db)
    existing = PrepRun(order_id=o["KO-C"].id, result_json='{"marker": true}')
    db.add(existing); db.commit()
    resp = client.post("/api/prep/merge", json={"order_ids": [o["KO-A"].id, o["KO-B"].id]})
    assert resp.status_code == 200
    runs_c = _runs(db, o["KO-C"].id)
    assert [r.id for r in runs_c] == [existing.id]
    assert json.loads(runs_c[0].result_json) == {"marker": True}
    db.refresh(o["KO-C"])
    assert (o["KO-C"].code, o["KO-C"].outlet, o["KO-C"].status) == ("KO-C", "城南门店", "open")


def test_merge_rolls_back_everything_when_reconcile_fails(db, monkeypatch):
    o = _orders(db)
    monkeypatch.setattr(prep_api, "verify_merge_consistency", lambda merged, parts: ["人为制造对不上"])
    resp = client.post("/api/prep/merge", json={"order_ids": [o["KO-A"].id, o["KO-B"].id]})
    assert resp.status_code == 500
    assert _run_count(db) == 0  # 两边都不许新落


def test_merge_rolls_back_everything_when_second_run_fails(db, monkeypatch):
    o = _orders(db)
    real_dumps = json.dumps
    calls = {"n": 0}

    def flaky_dumps(*args, **kwargs):
        calls["n"] += 1
        if calls["n"] == 2:
            raise RuntimeError("第二张单落库前出错")
        return real_dumps(*args, **kwargs)

    monkeypatch.setattr(prep_api.json, "dumps", flaky_dumps)
    resp = client.post("/api/prep/merge", json={"order_ids": [o["KO-A"].id, o["KO-B"].id]})
    assert resp.status_code == 500
    assert _run_count(db) == 0  # 第一张也不能留下


def test_merge_shortages_readonly(db):
    o = _orders(db)
    resp = client.get(f"/api/prep/merge/shortages?order_ids={o['KO-A'].id},{o['KO-B'].id}")
    assert resp.status_code == 200, resp.text
    data = resp.json()
    assert [s["ingredient_code"] for s in data["shortages"]] == ["I1"]
    assert data["stats"]["total_shortage_qty"] == 1.0
    assert _run_count(db) == 0  # 只读不落库
    resp = client.get(f"/api/prep/merge/shortages?order_ids={o['KO-A'].id}")
    assert resp.status_code == 400
    resp = client.get("/api/prep/merge/shortages?order_ids=")
    assert resp.status_code == 400
