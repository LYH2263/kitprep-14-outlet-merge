"""Central kitchen BOM explode: order lines × BOM qty, merge ingredients, shortage = need - stock.

合单（两家门店并一次备料）的关键不变量：
1. 逐店展开再加总——合单备料行 = A 店需求 + B 店需求，逐料守恒，不许只落一家；
2. 缺料贴与统计全部从同一组合总行派生（单一事实源），不存在"行是一份、缺料另算一份"；
3. 落库前用原始输入独立重算对账，任何一项对不上抛 ReconciliationError，整次作废；
4. 缺料只按原料编码挂在合单行上，每家的需求量记在自己订单名下，绝不串店。
"""
from __future__ import annotations
from dataclasses import asdict, dataclass, field

ROUND_DIGITS = 3
# 对账容差：落库数据保留 3 位小数，独立重算同口径保留 3 位后逐值比较
EPS = 1e-6


class MergeRejected(ValueError):
    """合单输入不合法（空名单/只给一家/门店重复/订单无行/菜品无 BOM/主数据缺失）。整次拒绝，不落任何数据。"""


class ReconciliationError(ValueError):
    """合单结果与独立重算对不上。整次失败，两边都不许落。"""


@dataclass
class NeedLine:
    ingredient_id: int
    ingredient_code: str
    ingredient_name: str
    unit: str
    need_qty: float
    stock_qty: float
    shortage: float


@dataclass
class StoreNeed:
    order_id: int
    code: str
    outlet: str
    need_qty: float


@dataclass
class MergedNeedLine(NeedLine):
    # 该料的需求来自哪几家门店、各自多少；缺料挂原料编码，门店份额挂各自订单，互不顶替
    stores: list[StoreNeed] = field(default_factory=list)


def _q(value: float) -> float:
    return round(value, ROUND_DIGITS)


def explode_and_merge(
    order_lines: list[dict],
    bom_lines: list[dict],
    ingredients: dict[int, dict],
) -> list[NeedLine]:
    """order_lines: dish_id, portions; bom_lines: dish_id, ingredient_id, qty_per_portion."""
    need = explode_needs(order_lines, bom_lines)
    lines: list[NeedLine] = []
    for iid, qty in sorted(need.items()):
        ing = ingredients[iid]
        stock = float(ing.get("stock_qty", 0))
        shortage = max(0.0, qty - stock)
        lines.append(NeedLine(
            ingredient_id=iid,
            ingredient_code=ing["code"],
            ingredient_name=ing["name"],
            unit=ing.get("unit", ""),
            need_qty=_q(qty),
            stock_qty=_q(stock),
            shortage=_q(shortage),
        ))
    return lines


def explode_needs(order_lines: list[dict], bom_lines: list[dict]) -> dict[int, float]:
    """单店展开：{ingredient_id: 需求总量}。"""
    need: dict[int, float] = {}
    for ol in order_lines:
        for bl in bom_lines:
            if bl["dish_id"] != ol["dish_id"]:
                continue
            need[bl["ingredient_id"]] = need.get(bl["ingredient_id"], 0.0) + ol["portions"] * bl["qty_per_portion"]
    return need


def result_to_dict(lines: list[NeedLine]) -> dict:
    return {
        "prep_lines": [asdict(l) for l in lines],
        "shortages": [asdict(l) for l in lines if l.shortage > 0],
        "stats": {
            "ingredient_count": len(lines),
            "shortage_count": sum(1 for l in lines if l.shortage > 0),
            "total_shortage_qty": _q(sum(l.shortage for l in lines)),
        },
    }


def _validate_stores(stores: list[dict]) -> list[dict]:
    """合单入口校验：必须恰好两家、不同门店、不同订单、各自带订单行。"""
    if not stores or len(stores) < 2:
        raise MergeRejected("合单必须给出两家门店的订单：空名单或只有一家一律拒绝")
    if len(stores) > 2:
        raise MergeRejected("一次合单只允许两家门店")
    norm = []
    seen_orders: set[int] = set()
    seen_outlets: set[str] = set()
    for s in stores:
        outlet = (s.get("outlet") or "").strip()
        code = (s.get("code") or "").strip()
        order_id = s.get("order_id")
        if order_id is None:
            raise MergeRejected("门店订单缺少 order_id")
        if order_id in seen_orders:
            raise MergeRejected(f"同一张订单不能合两次：{code or order_id}")
        if not outlet:
            raise MergeRejected(f"订单 {code or order_id} 缺少门店名，拒绝凑合合单")
        if outlet in seen_outlets:
            raise MergeRejected(f"两家必须是不同门店，收到的都是「{outlet}」")
        lines = list(s.get("lines") or [])
        if not lines:
            raise MergeRejected(f"门店「{outlet}」订单 {code} 没有任何订单行，拒绝合单")
        seen_orders.add(order_id)
        seen_outlets.add(outlet)
        norm.append({"order_id": order_id, "code": code, "outlet": outlet, "lines": lines})
    return norm


def merge_prep(
    stores: list[dict],
    bom_lines: list[dict],
    ingredients: dict[int, dict],
) -> tuple[list[MergedNeedLine], list[dict]]:
    """逐店展开后加总。返回 (合单行, 门店摘要)。任何输入不合法抛 MergeRejected。"""
    stores = _validate_stores(stores)

    # 主数据与 BOM 覆盖校验：菜品没有 BOM 不能静默丢行（那正是"只落一家"的根源）
    dishes_with_bom = {bl["dish_id"] for bl in bom_lines}
    per_store_need: list[dict[int, float]] = []
    for s in stores:
        for ol in s["lines"]:
            if ol["dish_id"] not in dishes_with_bom:
                raise MergeRejected(f"门店「{s['outlet']}」订单含无 BOM 的菜品 dish_id={ol['dish_id']}，拒绝合单")
        per_store_need.append(explode_needs(s["lines"], bom_lines))
    for need in per_store_need:
        for iid in need:
            if iid not in ingredients:
                raise MergeRejected(f"原料主数据缺失 ingredient_id={iid}，无法核库存，拒绝合单")

    total_need: dict[int, float] = {}
    for need in per_store_need:
        for iid, qty in need.items():
            total_need[iid] = total_need.get(iid, 0.0) + qty

    order_summary = [{"id": s["order_id"], "code": s["code"], "outlet": s["outlet"]} for s in stores]
    lines: list[MergedNeedLine] = []
    for iid, qty in sorted(total_need.items()):
        ing = ingredients[iid]
        stock = float(ing.get("stock_qty", 0))
        split = [
            StoreNeed(
                order_id=s["order_id"], code=s["code"], outlet=s["outlet"],
                need_qty=_q(per_store_need[idx][iid]),
            )
            for idx, s in enumerate(stores)
            if per_store_need[idx].get(iid, 0.0) > 0
        ]
        lines.append(MergedNeedLine(
            ingredient_id=iid,
            ingredient_code=ing["code"],
            ingredient_name=ing["name"],
            unit=ing.get("unit", ""),
            need_qty=_q(qty),
            stock_qty=_q(stock),
            shortage=_q(max(0.0, qty - stock)),
            stores=split,
        ))
    return lines, order_summary


def merged_result_to_dict(lines: list[MergedNeedLine], orders: list[dict]) -> dict:
    """行、缺料贴、统计全部从同一组 lines 派生——三者天然一致。"""
    prep_lines = [asdict(l) for l in lines]
    shortages = [p for p in prep_lines if p["shortage"] > 0]
    store_totals: dict[int, dict] = {
        o["id"]: {"order_id": o["id"], "code": o["code"], "outlet": o["outlet"], "need_qty": 0.0}
        for o in orders
    }
    for p in prep_lines:
        for st in p["stores"]:
            store_totals[st["order_id"]]["need_qty"] += st["need_qty"]
    per_store = list(store_totals.values())
    for row in per_store:
        row["need_qty"] = _q(row["need_qty"])
    return {
        "orders": orders,
        "prep_lines": prep_lines,
        "shortages": shortages,
        "stats": {
            "ingredient_count": len(prep_lines),
            "shortage_count": len(shortages),
            "total_need_qty": _q(sum(p["need_qty"] for p in prep_lines)),
            "total_shortage_qty": _q(sum(p["shortage"] for p in prep_lines)),
            "per_store": per_store,
        },
    }


def reconcile_prep_result(
    result: dict,
    stores: list[dict],
    bom_lines: list[dict],
    ingredients: dict[int, dict],
) -> None:
    """用原始输入独立重算，逐项核对 result。任何一项对不上抛 ReconciliationError。

    核对内容：
    - 逐店守恒：每家独立重算需求 == result 中挂在该订单名下的份额之和（防串店/丢店）；
    - 逐料守恒：合单行需求 == 两店份额之和；备料行原料集合 == 两店并集（防只落一家）；
    - 缺料贴必须与合单行逐条一致（同 id/编码/需求量/缺料量），统计与缺料贴一致；
    - 门店份额只允许挂这两家订单，编码只允许来自原料主数据。
    """
    def fail(msg: str):
        raise ReconciliationError(msg)

    stores = _validate_stores(stores)
    prep = result.get("prep_lines")
    shortages = result.get("shortages")
    stats = result.get("stats")
    if not isinstance(prep, list) or not isinstance(shortages, list) or not isinstance(stats, dict):
        fail("结果结构不完整：缺 prep_lines/shortages/stats")

    # 1) 从原始输入独立重算（不读 result）
    recomputed: list[dict[int, float]] = []
    union_ids: set[int] = set()
    for s in stores:
        need = explode_needs(s["lines"], bom_lines)
        recomputed.append(need)
        union_ids.update(need.keys())

    line_ids = {p["ingredient_id"] for p in prep}
    if line_ids != union_ids:
        fail(f"备料行原料集合与两店并集不一致：缺 {sorted(union_ids - line_ids)}，多 {sorted(line_ids - union_ids)}")

    order_ids = {s["order_id"] for s in stores}
    attributed: dict[int, float] = {oid: 0.0 for oid in order_ids}
    total_need_check = 0.0
    by_id = {p["ingredient_id"]: p for p in prep}
    if len(by_id) != len(prep):
        fail("备料行存在重复原料")

    for iid, p in by_id.items():
        ing = ingredients.get(iid)
        if ing is None:
            fail(f"备料行出现主数据不存在的 ingredient_id={iid}")
        if p["ingredient_code"] != ing["code"]:
            fail(f"原料编码串号：id={iid} 结果编码 {p['ingredient_code']}，主数据编码 {ing['code']}")
        split_sum = 0.0
        split_owners: set[int] = set()
        for st in p.get("stores", []):
            oid = st["order_id"]
            if oid not in order_ids:
                fail(f"原料 {ing['code']} 的份额挂到了合单之外的订单 order_id={oid}")
            attributed[oid] += st["need_qty"]
            split_sum += st["need_qty"]
            split_owners.add(oid)
        # 该行必须带上所有实际需要该料的门店——缺一家就是"只落其中一个门店的行"
        expected_owners = {stores[idx]["order_id"] for idx, need in enumerate(recomputed) if need.get(iid, 0.0) > 0}
        if split_owners != expected_owners:
            fail(f"原料 {ing['code']} 的门店归属不全：应有 {sorted(expected_owners)}，实有 {sorted(split_owners)}")
        if abs(split_sum - p["need_qty"]) > EPS:
            fail(f"原料 {ing['code']} 合单需求 {p['need_qty']} != 两店份额之和 {_q(split_sum)}")
        expect_shortage = _q(max(0.0, p["need_qty"] - float(ing.get("stock_qty", 0))))
        if abs(p["shortage"] - expect_shortage) > EPS:
            fail(f"原料 {ing['code']} 缺料量 {p['shortage']} 与行需求/库存重算值 {expect_shortage} 不符")
        total_need_check += p["need_qty"]

    # 2) 逐店守恒：挂在每家订单名下的总量必须等于该店订单行独立展开的总量
    for idx, s in enumerate(stores):
        expect = _q(sum(recomputed[idx].values()))
        actual = _q(attributed[s["order_id"]])
        if abs(actual - expect) > EPS:
            fail(f"门店「{s['outlet']}」守恒失败：独立重算 {expect}，结果中归属该店仅 {actual}")

    # 3) 缺料贴与同一组备料行逐条一致
    expect_short = [p for p in prep if p["shortage"] > 0]
    if len(shortages) != len(expect_short):
        fail(f"缺料贴 {len(shortages)} 条与备料行正缺料数 {len(expect_short)} 不符")
    for got, want in zip(shortages, expect_short):
        if got is not want:
            fail("缺料贴不是从备料行派生（对象不一致），拒绝落库")
        for key in ("ingredient_id", "ingredient_code", "need_qty", "stock_qty", "shortage"):
            if got.get(key) != want.get(key):
                fail(f"缺料贴字段 {key} 与备料行不一致：{got.get(key)} != {want.get(key)}")

    # 4) 统计与行/缺料贴一致，且两店分量之和 == 总需求
    if stats["ingredient_count"] != len(prep):
        fail("统计 ingredient_count 与备料行数不符")
    if stats["shortage_count"] != len(shortages):
        fail("统计 shortage_count 与缺料贴条数不符")
    if abs(stats["total_shortage_qty"] - _q(sum(p["shortage"] for p in prep))) > EPS:
        fail("统计 total_shortage_qty 与缺料贴加总不符")
    if abs(stats["total_need_qty"] - _q(total_need_check)) > EPS:
        fail("统计 total_need_qty 与备料行加总不符")
    per_store_sum = _q(sum(r["need_qty"] for r in stats.get("per_store", [])))
    if abs(per_store_sum - stats["total_need_qty"]) > EPS:
        fail("两店统计分量之和与总需求对不上")


def build_merged_result(
    stores: list[dict],
    bom_lines: list[dict],
    ingredients: dict[int, dict],
) -> dict:
    """合单主流程：展开加总 → 单一事实源派生 → 独立重算对账，全过才返回。"""
    lines, orders = merge_prep(stores, bom_lines, ingredients)
    result = merged_result_to_dict(lines, orders)
    reconcile_prep_result(result, stores, bom_lines, ingredients)
    return result
