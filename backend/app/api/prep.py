import json
from datetime import datetime

from fastapi import APIRouter, Body, Depends, HTTPException, Query
from pydantic import BaseModel
from sqlalchemy import select
from sqlalchemy.orm import Session

from app.database import get_db
from app.models.models import BomLine, Ingredient, KitchenOrder, OrderLine, PrepRun
from app.services.bom_engine import explode_and_merge, result_to_dict, verify_merge_consistency

router = APIRouter(prefix="/prep", tags=["prep"])


class MergeRequest(BaseModel):
    order_ids: list[int]


def _load_bom(db: Session) -> list[dict]:
    return [{"dish_id": b.dish_id, "ingredient_id": b.ingredient_id, "qty_per_portion": b.qty_per_portion}
            for b in db.scalars(select(BomLine)).all()]


def _load_ingredients(db: Session) -> dict[int, dict]:
    return {i.id: {"code": i.code, "name": i.name, "unit": i.unit, "stock_qty": i.stock_qty}
            for i in db.scalars(select(Ingredient)).all()}


def _order_lines(db: Session, order_id: int) -> list[dict]:
    return [{"dish_id": l.dish_id, "portions": l.portions}
            for l in db.scalars(select(OrderLine).where(OrderLine.order_id == order_id)).all()]


def _build_result(order: KitchenOrder, ols: list[dict], bom: list[dict], ings: dict[int, dict]) -> dict:
    result = result_to_dict(explode_and_merge(ols, bom, ings))
    result["order"] = {"id": order.id, "code": order.code, "outlet": order.outlet}
    return result


def _save_run(db: Session, order_id: int, result: dict) -> PrepRun:
    run = PrepRun(order_id=order_id, created_at=datetime.utcnow(),
                  result_json=json.dumps(result, ensure_ascii=False))
    db.add(run)
    return run


def _resolve_merge_orders(db: Session, ids: list[int]) -> list[KitchenOrder]:
    """合单只接受正好两张不同的门店订单；空名单、只给一家、重复给单一律拒绝。"""
    if len(ids) != 2:
        raise HTTPException(400, "合单需要正好两家门店的订单，空名单或只给一家不予受理")
    if ids[0] == ids[1]:
        raise HTTPException(400, "两张订单不能是同一张，相当于只给一家，不予受理")
    orders = []
    for oid in ids:
        order = db.get(KitchenOrder, oid)
        if not order:
            raise HTTPException(404, f"订单 {oid} 不存在")
        orders.append(order)
    return orders


@router.post("/run")
def run_prep(order_id: int = 1, db: Session = Depends(get_db)):
    order = db.get(KitchenOrder, order_id)
    if not order: raise HTTPException(404, "订单不存在")
    result = _build_result(order, _order_lines(db, order_id), _load_bom(db), _load_ingredients(db))
    run = _save_run(db, order_id, result)
    db.commit(); db.refresh(run)
    return {"id": run.id, **result}


@router.post("/merge")
def merge_prep(
    db: Session = Depends(get_db),
    body: MergeRequest | None = Body(None),
    order_ids: list[int] | None = Query(None),
):
    """两店合单备料：两店备料行、缺料贴、统计必须对成加总。

    对不上或落库出错都整次失败回滚，两边门店都不许新落行；
    每店的备料行只记在自己订单编码下，不拿另一家的缺料凑数。
    """
    ids = body.order_ids if body is not None else (order_ids or [])
    orders = _resolve_merge_orders(db, ids)
    bom = _load_bom(db)
    ings = _load_ingredients(db)
    ols_per_order = [_order_lines(db, o.id) for o in orders]
    parts = [_build_result(o, ols, bom, ings) for o, ols in zip(orders, ols_per_order)]
    merged = result_to_dict(explode_and_merge(ols_per_order[0] + ols_per_order[1], bom, ings))
    problems = verify_merge_consistency(merged, parts)
    if problems:
        db.rollback()
        raise HTTPException(500, "合单对账失败，已整次回滚：" + "；".join(problems))
    runs: list[PrepRun] = []
    try:
        for order, part in zip(orders, parts):
            partner = orders[1].id if order.id == orders[0].id else orders[0].id
            part["merge_partner_order_id"] = partner
            runs.append(_save_run(db, order.id, part))
        db.commit()
    except Exception:
        db.rollback()
        raise HTTPException(500, "合单落库失败，已整次回滚，两边均未新落行")
    for run in runs:
        db.refresh(run)
    return {
        "merged": True,
        "orders": [{"id": o.id, "code": o.code, "outlet": o.outlet, "run_id": r.id}
                   for o, r in zip(orders, runs)],
        **merged,
    }


@router.get("/merge/shortages")
def merge_shortages(order_ids: str = Query(...), db: Session = Depends(get_db)):
    """两店合单缺料贴：按两店订单行加总现算，只读不落库。"""
    try:
        ids = [int(x) for x in order_ids.split(",") if x.strip()]
    except ValueError:
        raise HTTPException(400, "order_ids 需为逗号分隔的订单号")
    orders = _resolve_merge_orders(db, ids)
    bom = _load_bom(db)
    ings = _load_ingredients(db)
    ols: list[dict] = []
    for o in orders:
        ols.extend(_order_lines(db, o.id))
    merged = result_to_dict(explode_and_merge(ols, bom, ings))
    return {
        "order_ids": [o.id for o in orders],
        "orders": [{"id": o.id, "code": o.code, "outlet": o.outlet} for o in orders],
        "shortages": merged["shortages"],
        "stats": merged["stats"],
    }


@router.get("/latest")
def latest(order_id: int = 1, db: Session = Depends(get_db)):
    run = db.scalars(select(PrepRun).where(PrepRun.order_id == order_id).order_by(PrepRun.id.desc())).first()
    if not run:
        return run_prep(order_id=order_id, db=db)
    data = json.loads(run.result_json)
    return {"id": run.id, **data}


@router.get("/shortages")
def shortages(order_id: int = 1, db: Session = Depends(get_db)):
    data = latest(order_id=order_id, db=db)
    return {"order_id": order_id, "shortages": data.get("shortages", []), "stats": data.get("stats", {})}
