import json
from datetime import datetime
from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel
from sqlalchemy import select
from sqlalchemy.orm import Session
from app.database import get_db
from app.models.models import BomLine, Ingredient, KitchenOrder, OrderLine, PrepRun, MergedPrepRun, MergedPrepRunOrder
from app.services.bom_engine import (
    MergeRejected, ReconciliationError, build_merged_result, explode_and_merge, result_to_dict,
)
router = APIRouter(prefix="/prep", tags=["prep"])

@router.post("/run")
def run_prep(order_id: int = 1, db: Session = Depends(get_db)):
    order = db.get(KitchenOrder, order_id)
    if not order: raise HTTPException(404, "订单不存在")
    ols = [{"dish_id": l.dish_id, "portions": l.portions}
           for l in db.scalars(select(OrderLine).where(OrderLine.order_id == order_id)).all()]
    bom = [{"dish_id": b.dish_id, "ingredient_id": b.ingredient_id, "qty_per_portion": b.qty_per_portion}
           for b in db.scalars(select(BomLine)).all()]
    ings = {i.id: {"code": i.code, "name": i.name, "unit": i.unit, "stock_qty": i.stock_qty}
            for i in db.scalars(select(Ingredient)).all()}
    result = result_to_dict(explode_and_merge(ols, bom, ings))
    result["order"] = {"id": order.id, "code": order.code, "outlet": order.outlet}
    run = PrepRun(order_id=order_id, created_at=datetime.utcnow(), result_json=json.dumps(result, ensure_ascii=False))
    db.add(run); db.commit(); db.refresh(run)
    return {"id": run.id, **result}

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


class MergeRequest(BaseModel):
    order_ids: list[int]


def _load_bom_and_ingredients(db: Session):
    bom = [{"dish_id": b.dish_id, "ingredient_id": b.ingredient_id, "qty_per_portion": b.qty_per_portion}
           for b in db.scalars(select(BomLine)).all()]
    ings = {i.id: {"code": i.code, "name": i.name, "unit": i.unit, "stock_qty": i.stock_qty}
            for i in db.scalars(select(Ingredient)).all()}
    return bom, ings


@router.post("/merge")
def merge_two_stores(payload: MergeRequest, db: Session = Depends(get_db)):
    """两家门店合成一次备料。

    - 空名单 / 只给一家 / 同店重复 / 订单无行 → 400 拒绝，已有单一律不动；
    - 行、缺料贴、统计对不上加总 → 500 整次失败，两店都不落任何备料批次；
    - 只对 merged_prep_runs 做新增，源订单（含第三家）全程只读不改字。
    """
    raw_ids = payload.order_ids
    if not raw_ids or len(raw_ids) < 2:
        raise HTTPException(400, "合单必须恰好给出两家不同门店的订单：空名单或只给一家按拒绝处理")
    if len(raw_ids) > 2:
        raise HTTPException(400, "一次合单只允许两家门店")
    if len(set(raw_ids)) != len(raw_ids):
        raise HTTPException(400, "同一张门店订单不能合两次")
    order_ids = raw_ids

    stores = []
    for oid in order_ids:
        order = db.get(KitchenOrder, oid)
        if not order:
            raise HTTPException(404, f"订单不存在：order_id={oid}")
        lines = [{"dish_id": l.dish_id, "portions": l.portions}
                 for l in db.scalars(select(OrderLine).where(OrderLine.order_id == oid)).all()]
        stores.append({"order_id": order.id, "code": order.code, "outlet": order.outlet, "lines": lines})

    bom, ings = _load_bom_and_ingredients(db)
    try:
        # 展开加总 → 单一事实源派生行/缺料/统计 → 独立重算对账，全过才返回
        result = build_merged_result(stores, bom, ings)
    except MergeRejected as exc:
        raise HTTPException(400, str(exc))
    except ReconciliationError as exc:
        # 对账不过：整次失败。本路径没有任何写入，显式回滚保底，两边都不许新落。
        db.rollback()
        raise HTTPException(500, f"合单对账失败，整次作废，两店均未落单：{exc}")

    # 对账通过后才在同一事务落库；任何异常一并回滚，杜绝只落一家
    try:
        run = MergedPrepRun(created_at=datetime.utcnow(),
                            result_json=json.dumps(result, ensure_ascii=False))
        db.add(run)
        db.flush()
        for oid in order_ids:
            db.add(MergedPrepRunOrder(run_id=run.id, order_id=oid))
        db.commit()
    except Exception:
        db.rollback()
        raise HTTPException(500, "合单落库失败，已整体回滚，两店均未落单")
    db.refresh(run)
    return {"id": run.id, **result}


@router.get("/merge-runs/{run_id}")
def get_merged_run(run_id: int, db: Session = Depends(get_db)):
    run = db.get(MergedPrepRun, run_id)
    if not run:
        raise HTTPException(404, "合单批次不存在")
    return {"id": run.id, **json.loads(run.result_json)}


@router.get("/merge-runs/{run_id}/shortages")
def merged_run_shortages(run_id: int, db: Session = Depends(get_db)):
    data = get_merged_run(run_id=run_id, db=db)
    return {"id": run_id, "orders": data.get("orders", []),
            "shortages": data.get("shortages", []), "stats": data.get("stats", {})}
